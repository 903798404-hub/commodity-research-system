"""Sealed intent and stage-aware host adapter tests; not Docker evidence."""
import contextlib
import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('_test_high_risk_execution', ROOT / '09_deploy/spread_release/high_risk_execution.py')
execution = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = execution
spec.loader.exec_module(execution)


def plan():
    ref = dict(path='/protected/evidence.json', sha256='1' * 64)
    asset = dict(commit='2' * 40, tree='3' * 40, image_id='sha256:' + '4' * 64)
    return dict(schema_version='1.8.0', release_id='spread-20260930-' + '2' * 12 + '-b01',
        git_commit=asset['commit'], git_tree=asset['tree'], candidate_image_id=asset['image_id'],
        formal_containers={}, tool=dict(commit='5' * 40, tree='6' * 40), source=copy.deepcopy(asset),
        target=copy.deepcopy(asset), primary_rollback=copy.deepcopy(asset),
        project_id='spread-production-runtime-wiring', source_container_id='7' * 64,
        release_request=ref, source_policy=ref, instances=dict(target=ref, primary_rollback=ref),
        lock_file='/var/lib/market-data/production-runtime/spread-release.lock', policy=dict(
            readiness=copy.deepcopy(execution.contract().DEFAULT_READINESS_POLICY), observation_seconds=2,
            poll_interval_seconds=1, consecutive_failures=2, stop_timeout_seconds=10,
            start_timeout_seconds=60, rollback_timeout_seconds=90, rollback_on=[
                'create_failure', 'authorization_failure', 'start_failure', 'acceptance_failure',
                'runtime_identity_failure', 'health_failure_threshold', 'observation_ended_unhealthy']))


def sealed(tmp_path, value=None):
    path = tmp_path / 'deployment_plan.json'
    execution.seal_plan(value or plan(), path)
    return path


class Backend:
    def __init__(self, fail=None, observations=None):
        self.calls = []
        self.fail = fail
        self.observations = iter(observations or [])
        self.grants = {}
        self.result = None

    def hit(self, name):
        self.calls.append(name)
        if name == self.fail:
            raise RuntimeError('controlled ' + name)

    @contextlib.contextmanager
    def lock(self, plan):
        self.hit('lock')
        yield

    def preconditions(self, plan): self.hit('preconditions')
    def consume_recovery(self, plan): self.hit('consume')
    def rollback_assets(self, plan): self.hit('assets')
    def stop_source(self, plan): self.hit('stop_source')
    def create(self, plan, role): self.hit('create_' + role)
    def authorize(self, plan, role):
        self.hit('authorize_' + role)
        self.grants[role] = object()
    def start(self, plan, role):
        assert role in self.grants
        self.hit('start_' + role)
    def accept(self, plan, role): self.hit('accept_' + role)
    def observe(self, plan):
        self.hit('observe')
        return next(self.observations, 'PASS')
    def stop_target(self, plan): self.hit('stop_target')
    def rollback_window(self, seconds): return contextlib.nullcontext()
    def record(self, result):
        self.hit('record')
        self.result = copy.deepcopy(result)
    def cleanup_temporary(self, plan): self.hit('cleanup')


class Clock:
    value = 0
    def now(self): return self.value
    def sleep(self, interval): self.value += interval


def run(path, backend):
    clock = Clock()
    return execution.execute_verified_plan(path, backend, monotonic=clock.now, sleep=clock.sleep)


def test_sealed_plan_and_success(tmp_path):
    path = sealed(tmp_path)
    backend = Backend()
    result = run(path, backend)
    assert result['result'] == 'SUCCESS'
    assert backend.calls == ['lock', 'preconditions', 'consume', 'assets', 'stop_source',
        'create_target', 'authorize_target', 'start_target', 'accept_target',
        'observe', 'observe', 'observe', 'cleanup', 'record']


@pytest.mark.parametrize('failure', ['preconditions', 'consume', 'assets'])
def test_failure_before_stop_preserves_source(tmp_path, failure):
    backend = Backend(failure)
    result = run(sealed(tmp_path), backend)
    assert result['result'] == 'FAIL'
    assert result['source'] == 'PRESERVED_NOT_STOPPED'
    assert 'stop_source' not in backend.calls


@pytest.mark.parametrize('failure', ['stop_source', 'create_target', 'authorize_target',
                                  'start_target', 'accept_target'])
def test_after_stop_failure_fresh_rollback(tmp_path, failure):
    backend = Backend(failure)
    result = run(sealed(tmp_path), backend)
    assert result['result'] == 'FAIL' and result['rollback'] == 'PASS'
    assert result['target'] != 'PASS'
    assert result['target'] == ('NOT_EXECUTED' if failure == 'stop_source' else 'FAIL')
    assert 'authorize_primary_rollback' in backend.calls
    assert backend.calls.index('authorize_primary_rollback') < backend.calls.index('start_primary_rollback')
    if failure == 'authorize_target':
        assert 'start_target' not in backend.calls
    if 'target' in backend.grants:
        assert backend.grants['target'] is not backend.grants['primary_rollback']


@pytest.mark.parametrize('observations', [['FATAL'], ['TRANSIENT', 'TRANSIENT'], ['PASS', 'PASS', 'TRANSIENT']])
def test_observation_recovery(tmp_path, observations):
    result = run(sealed(tmp_path), Backend(observations=observations))
    assert result['result'] == 'FAIL' and result['rollback'] == 'PASS'


def test_missing_plan_before_lock_or_stop(tmp_path):
    backend = Backend()
    with pytest.raises(ValueError): run(tmp_path / 'deployment_plan.json', backend)
    assert backend.calls == []


def test_tampered_plan_before_stop(tmp_path):
    path = sealed(tmp_path)
    # Deliberately corrupt an owner's disposable fixture. Production producer
    # keeps the sealed artifact read-only; no permission rule is relaxed.
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b' ')
    backend = Backend()
    with pytest.raises(ValueError): run(path, backend)
    assert backend.calls == []


@pytest.mark.parametrize('mutation', ['unknown', 'no_observation', 'future_id', 'wrong_image', 'float_interval'])
def test_invalid_plan_rejected(tmp_path, mutation):
    value = plan()
    if mutation == 'unknown': value['allow_extra_fields'] = True
    if mutation == 'no_observation': del value['policy']['observation_seconds']
    if mutation == 'future_id': value['formal_containers']['future'] = '8' * 64
    if mutation == 'wrong_image': value['target']['image_id'] = 'sha256:' + '8' * 64
    if mutation == 'float_interval': value['policy']['poll_interval_seconds'] = 1.5
    with pytest.raises(ValueError): sealed(tmp_path, value)


def test_lease_requires_actual_consumption_and_terminal_state():
    lease = execution.EvidenceLease()
    clean = []
    with pytest.raises(ValueError): lease.release(lambda: clean.append(True))
    lease.consume(lambda: None)
    with pytest.raises(ValueError): lease.release(lambda: clean.append(True))
    lease.terminal = True
    lease.release(lambda: clean.append(True))
    assert clean == [True]


def test_bad_semantic_intent_not_sealed(tmp_path):
    value = plan()
    value['policy']['observation_seconds'] = 2
    value['policy']['consecutive_failures'] = 3
    with pytest.raises(ValueError, match='OBSERVATION_TOO_SHORT'):
        execution.seal_plan(value, tmp_path / 'deployment_plan.json')
    assert not list(tmp_path.iterdir())


def test_failed_rollback_preserves_recovery_resources(tmp_path):
    backend = Backend('accept_primary_rollback', ['FATAL'])
    result = run(sealed(tmp_path), backend)
    assert result['rollback'] == 'FAIL' and result['resources'] == 'RETAINED_FOR_RECOVERY'
    assert 'cleanup' not in backend.calls


@pytest.mark.parametrize('revision', ['88df880127bea4308ee752a37a59884b9198b2ce',
                                    '0d7b86ddafbed0e7b063ae1097d7e07ee36e9f00'])
def test_actual_application_issuer_abi_is_supported_without_tool_observe(tmp_path, revision):
    import subprocess
    raw = subprocess.check_output(['git', '-C', str(ROOT), 'show',
        revision + ':09_deploy/runtime_identity/host_authorization.py'])
    path = tmp_path / '09_deploy/runtime_identity/host_authorization.py'
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    if revision.startswith('0d7'):
        path.with_name('runtime_observation.py').write_bytes(subprocess.check_output([
            'git', '-C', str(ROOT), 'show', revision + ':09_deploy/runtime_identity/runtime_observation.py']))
    issuer = execution.load(tmp_path, '09_deploy/runtime_identity/host_authorization.py',
                            '_test_actual_issuer_' + revision)
    execution.validate_issuer_api(issuer, application_service=revision.startswith('0d7'))
    assert Path(issuer.__file__) == path
    if revision.startswith('88df'):
        assert '_observe' not in vars(issuer)
    # Current formal transport interprets Compose independently; do not add
    # _observe to the old module or change its grant format.
    routine = execution.load(ROOT, '04_scripts/runtime/routine_release.py', '_abi_routine_' + revision)
    session = routine.DockerSession({}, None, issuer, {}, dict(project_id='test', service_id='test'))
    mounts = [dict(type='bind', source='/isolated/data', target='/runtime/data', read_only=True)]
    assert session.resolved_compose_mounts(dict(services=dict(test=dict(volumes=mounts)))) == mounts
    # Exercise the original failed transport method with the real old/new
    # manifest validator. Only host file I/O is replaced by this inert fixture.
    session.contract.update(runtime_roots=[dict(role='data', container_path='/runtime/data', access='ro')],
        required_mounts=[dict(role='data', container_path='/runtime/data', read_only=True)], secret_references=[])
    session.role, session.spec = 'production', dict(policy_template='inert', writable_root=str(tmp_path))
    session.protected_json = lambda path: dict(schema_version='host-runtime-policy/5',
        grant_container_directory='/run/market-data-grants', mounts=[
            {k: v for k, v in mounts[0].items() if k != 'type'}])
    session._check_mounts(mounts)
    with pytest.raises(ValueError):
        session._check_mounts([dict(mounts[0], read_only=False)])


@pytest.mark.parametrize('fault', ['missing', 'wrong_signature', 'missing_service_issuer'])
def test_unsupported_issuer_rejected_by_execution_before_stop(tmp_path, fault):
    from types import ModuleType
    current = execution.load(ROOT, '09_deploy/runtime_identity/host_authorization.py', '_abi_current_host')
    broken = ModuleType('_unsupported_fixture_issuer')
    broken.__dict__.update(vars(current))
    if fault == 'missing':
        del broken._render_actual_compose
    elif fault == 'wrong_signature':
        broken.issue_execution_grant = lambda container_id: None
    else:
        del broken.issue_application_service_credential
    class Unsupported(Backend):
        def preconditions(self, value):
            self.hit('preconditions')
            execution.validate_issuer_api(broken, application_service=True)
    backend = Unsupported()
    result = run(sealed(tmp_path), backend)
    assert result['result'] == 'FAIL' and result['source'] == 'PRESERVED_NOT_STOPPED'
    assert 'UNSUPPORTED_APPLICATION_ISSUER_API' in result['failure']
    assert backend.calls == ['lock', 'preconditions', 'record']


@pytest.mark.parametrize('exit_code', [0, 1])
def test_formal_application_preflight_keeps_streams_and_never_promotes_diagnostic_success(tmp_path, exit_code):
    from types import SimpleNamespace
    import subprocess
    ready = execution.load(ROOT, '09_deploy/spread_release/wait_for_service_ready.py', '_preflight_evidence_ready')
    backend = execution.HostBackend.__new__(execution.HostBackend)
    backend.output = tmp_path
    backend.sessions = dict(target=SimpleNamespace(container_id='a'*64))
    calls = []
    def docker(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            return subprocess.CompletedProcess(args, exit_code, b'{"status":"FAILED"}\n', b'')
        return subprocess.CompletedProcess(args, 0, b'diagnostic-only\n', b'token=private-value\ntraceback\n')
    backend.engine = SimpleNamespace(_docker=docker,
        _canonical=lambda value: json.dumps(value).encode(), _write_new=lambda path, raw: path.write_bytes(raw))
    if exit_code:
        with pytest.raises(execution.ExecutionError, match='APPLICATION_RUNTIME_PREFLIGHT_FAILED'):
            backend._application_preflight(plan(), 'target', ready)
        diagnostic = json.loads((tmp_path/'target-application-preflight-diagnostic.json').read_bytes())
        assert diagnostic['purpose'] == 'READONLY_DIAGNOSIS_NOT_ACCEPTANCE'
        assert 'private-value' not in diagnostic['stderr'] and 'traceback' in diagnostic['stderr']
        assert 'readonly_preflight' in diagnostic['argv'][-1]
    else:
        backend._application_preflight(plan(), 'target', ready)
        assert len(calls) == 1
    evidence = json.loads((tmp_path/'target-application-preflight.json').read_bytes())
    assert evidence['exit_code'] == exit_code and evidence['stdout'] == '{"status":"FAILED"}\n'
    assert evidence['stderr'] == '' and all(kwargs == {'check': False} for _, kwargs in calls)
