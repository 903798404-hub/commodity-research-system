"""Sealed intent and stage-aware host adapter tests; not Docker evidence."""
import contextlib
import copy
import hashlib
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


def test_hosted_formal_runtime_fixture_preserves_candidate_snapshot_and_empty_formal_store(tmp_path):
    fixture = execution.load(ROOT, '08_tests/shared/high_risk_execution_docker_e2e.py',
        '_test_formal_execution_inputs')
    manifest = json.loads((ROOT / fixture.CONTRACT).read_text(encoding='utf-8'))
    source = tmp_path / 'source'
    sources = {}
    for item in manifest['runtime_roots']:
        if item['role'] != 'capture-snapshots':
            sources[item['role']] = tmp_path / item['role']
            sources[item['role']].mkdir()
    sources['capture-snapshots'] = sources['snapshots']
    originals = {}
    for item in manifest['candidate_runtime_inputs']:
        path = ROOT / item['source_path']
        # Existing fixture contract uses Git LF JSON on both Windows and Linux.
        raw = path.read_text(encoding='utf-8').encode('utf-8') if path.suffix == '.json' else path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == item['sha256']
        copy_path = source / item['source_path']
        copy_path.parent.mkdir(parents=True, exist_ok=True)
        copy_path.write_bytes(raw)
        originals[copy_path] = raw
    result = fixture.seed_formal_runtime_inputs(manifest, sources, source)
    assert result['snapshot_state'] == 'FORMAL_PRE_CAPTURE_EMPTY'
    assert result['capture_executed'] is result['sealed_candidate_snapshot_retagged'] is False
    assert len(result['inputs']) == 15
    assert not any(sources['snapshots'].iterdir())
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    preflight = execution.load(ROOT, '04_scripts/runtime/spread_runtime_preflight.py',
        '_test_formal_execution_snapshot_reader')
    assert preflight.load_formal_preflight_snapshot(sources['snapshots']) is None
    sealed_manifest = next(path for path in originals if path.name == 'shared_intraday_snapshot_manifest.json')
    assert json.loads(sealed_manifest.read_bytes())['environment'] == 'TEST_ISOLATED_NON_PRODUCTION'


def test_hosted_formal_runtime_fixture_rejects_input_hash_mismatch(tmp_path):
    fixture = execution.load(ROOT, '08_tests/shared/high_risk_execution_docker_e2e.py',
        '_test_formal_execution_input_hash')
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'input.json').write_bytes(b'{}')
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    manifest = dict(candidate_runtime_inputs=[dict(role='history', source_path='input.json',
        relative_path='input.json', sha256='0' * 64)])
    with pytest.raises(AssertionError, match='input.json'):
        fixture.seed_formal_runtime_inputs(manifest, dict(snapshots=snapshot,
            **{'capture-snapshots': snapshot, 'history': tmp_path}), source)


@pytest.mark.parametrize('raw,valid', [
    (b'[{"Id":"network-id","Containers":{}}]', True),
    (b'{"Id":"network-id"}', False),
    (b'[]', False),
    (b'[{"Id":"network-id"},{"Id":"other"}]', False),
    (b'[{"Id":"network-id","Id":"other"}]', False),
    (b'[{"Id":"wrong-network"}]', False),
    (b'[{"Id":"network-id","invalid":NaN}]', False),
])
def test_network_observation_uses_existing_strict_single_inspect_adapter(monkeypatch, raw, valid):
    from types import SimpleNamespace
    engine = execution.load(ROOT, '04_scripts/runtime/validate_target_runtime.py',
        '_test_network_execution_engine')
    calls = []
    def docker(*args):
        calls.append(args)
        return SimpleNamespace(stdout=raw)
    monkeypatch.setattr(engine, '_docker', docker)
    backend = execution.HostBackend.__new__(execution.HostBackend)
    backend.engine = engine
    if valid:
        assert backend.network_observation('fixture-net', expected_id='network-id')['Containers'] == {}
    else:
        with pytest.raises(engine.ValidationError):
            backend.network_observation('fixture-net', expected_id='network-id')
    assert calls == [('network', 'inspect', 'fixture-net')]


@pytest.mark.parametrize('fault', ['none', 'root', 'wrong_cache', 'empty', 'command_failure'])
def test_hosted_baseline_cache_initialized_by_real_nonroot_command_before_preservation(tmp_path, monkeypatch, fault):
    from types import SimpleNamespace
    fixture = execution.load(ROOT, '08_tests/shared/high_risk_execution_docker_e2e.py',
        '_test_baseline_font_cache_' + fault)
    observation = dict(uid=65532, gid=65532, cache='/runtime/10_logs/matplotlib',
        files={'fontlist-v3.11.0.json': 'a'*64})
    if fault == 'root': observation['uid'] = 0
    if fault == 'wrong_cache': observation['cache'] = '/production/logs'
    if fault == 'empty': observation['files'] = {}
    calls = []
    def run(*args, **kwargs):
        calls.append((args, kwargs))
        if fault == 'command_failure': raise RuntimeError('actual command failed')
        return json.dumps(observation)
    monkeypatch.setattr(fixture, 'run', run)
    backend = SimpleNamespace(host=SimpleNamespace(_json=json.loads))
    if fault == 'none':
        record = json.loads(fixture.initialize_baseline_font_cache(tmp_path, backend, 'b'*64).read_bytes())
        assert record['observation'] == observation and record['exit_code'] == 0
        assert record['phase'] == 'BEFORE_SOURCE_ACCEPTANCE_AND_PRESERVATION'
    else:
        with pytest.raises(RuntimeError if fault == 'command_failure' else AssertionError):
            fixture.initialize_baseline_font_cache(tmp_path, backend, 'b'*64)
        assert not list(tmp_path.iterdir())
    assert calls[0][0][:6] == ('docker', 'exec', 'b'*64, 'python', '-B', '-c')
    assert 'matplotlib.font_manager' in calls[0][0][-1] and calls[0][1] == dict(timeout=120)
    source = (ROOT / '08_tests/shared/high_risk_execution_docker_e2e.py').read_text(encoding='utf-8')
    assert source.index('    initialize_baseline_font_cache(work, backend,') < source.index('    backend.accept(seed,')
    assert source.index('    backend.accept(seed,') < source.index('    evidence, recovery_policy, fresh, sandbox, network = rehearsal(')


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


@pytest.mark.parametrize('scenario', ['reuse', 'refresh', 'review_blocked', 'probe_failure', 'wrong_rollback', 'routine'])
def test_preparation_produces_new_reference_chain_without_editing_old_plan(monkeypatch, scenario):
    from pathlib import PurePosixPath
    from types import SimpleNamespace
    value = plan()
    objects = {}
    writes = {}
    calls = []

    def put(path, obj):
        objects[path] = copy.deepcopy(obj)
        return dict(path=path, sha256=hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest())

    old_record = put('/old/candidate.json', {'historical': True})
    review = {'reviewer': 'unchanged-existing-human-input'}
    request = dict(project_id=value['project_id'], target_commit=value['target']['commit'],
        target_source_root='/source', current=dict(commit=value['source']['commit']),
        candidate_record=old_record, maintainer_risk_review=review)
    value['release_request'] = put('/old/request.json', request)
    for role in ('target', 'primary_rollback'):
        policy = dict(approved_commit=value[role]['commit'], approved_tree=value[role]['tree'],
            image_id=value[role]['image_id'], approved_source_root='/source', candidate_record=old_record)
        pref = put('/old/' + role + '-policy.json', policy)
        transport = dict(compose='/compose.yml', environment='/env', project_directory='/project',
            policy_template=pref['path'], policy_output='/fresh/' + role + '-policy.json', writable_root='/fresh',
            grant_directory='/fresh/' + role, key_path='/keys/production.pem', container_port=8501)
        instance = dict(source_root='/source', policy=pref, transport=transport,
            application_smoke=dict(kind='dom', path='/', selectors={'body': 1}))
        value['instances'][role] = put('/old/' + role + '-instance.json', instance)
        if role == 'primary_rollback':
            value['source_policy'] = pref
    original_objects = copy.deepcopy(objects)
    original_plan = copy.deepcopy(value)

    def reviewed(request, risk, host):
        if scenario == 'review_blocked':
            raise ValueError('RISK_REVIEW_IDENTITY_CHANGED')
        return dict(STATE_CHANGE_CLASS='HIGH_RISK'), {}, None

    def ensure(project, ref, **kwargs):
        calls.append(kwargs)
        assert ref == old_record
        if scenario == 'probe_failure':
            raise ValueError('candidate engine did not complete successfully')
        if scenario in {'reuse', 'routine'}:
            return dict(reference=ref, action='REUSED', validation_attempts=0)
        return dict(reference=dict(path=str(kwargs['destination']).replace('\\', '/'), sha256='a'*64),
                    action='REVALIDATED', validation_attempts=1)

    def write(host, path, raw):
        name = str(path)
        assert name not in writes and name not in objects
        writes[name] = json.loads(raw)

    backend = execution.HostBackend.__new__(execution.HostBackend)
    backend.output = PurePosixPath('/prepared')
    backend.host = SimpleNamespace(validate_policy=lambda policy, role: None)
    backend.engine = SimpleNamespace(_canonical=lambda v: json.dumps(v, sort_keys=True).encode())
    backend.pre = SimpleNamespace(require_source=lambda *a: (value['tool']['commit'], value['tool']['tree']),
        classify_release=lambda *a: {}, reviewed_release_state=reviewed,
        ensure_candidate_record=ensure, _write_new=write, VALIDATION_TIMEOUT_SECONDS=3900)
    backend.read = lambda ref: copy.deepcopy(objects[ref['path']])
    # This test isolates immutable-reference production. The real resolver and
    # its signature/protected-source gates have separate integration tests.
    checked = []
    def resolve(policy, role, asset, **kw):
        assert not calls  # all input validation precedes every image validation
        checked.append(role)
        if role == 'primary_rollback' and scenario == 'wrong_rollback':
            raise ValueError('UNSUPPORTED_ACCEPTANCE_FAMILY')
        return dict(family='routine-candidate-acceptance/1' if role == 'primary_rollback'
            and scenario == 'routine' else 'candidate-validation-record/1',
            reference=policy['candidate_record'], trust_source='protected-file')
    backend.resolve_acceptance = resolve
    args = dict(candidate_key=Path('/test/key.pem'), remaining_seconds=dict(target=60, primary_rollback=240))
    if scenario in {'review_blocked', 'probe_failure', 'wrong_rollback'}:
        with pytest.raises(ValueError):
            backend.refresh_plan_inputs(value, **args)
        assert not writes
        assert len(calls) == (1 if scenario == 'probe_failure' else 0)
    else:
        updated = backend.refresh_plan_inputs(value, **args)
        assert checked == ['target', 'primary_rollback']
        assert [call['minimum_remaining_seconds'] for call in calls] == ([60] if scenario == 'routine' else [4140, 60])
        if scenario in {'reuse', 'routine'}:
            assert updated == value and set(writes) == {'/prepared/candidate-refresh-result.json'}
        else:
            updated_request = writes[updated['release_request']['path']]
            updated_spec = writes[updated['instances']['target']['path']]
            updated_policy = writes[updated_spec['policy']['path']]
            assert updated_request['candidate_record'] == updated_policy['candidate_record'] != old_record
            assert updated_spec['transport']['policy_template'] == updated_spec['policy']['path']
            assert updated_request['maintainer_risk_review'] == review
            assert updated['source_policy'] == value['source_policy']
        assert not any('deployment_plan' in name for name in writes)
    assert value == original_plan and objects == original_objects


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
    def prepare_execution_conditions(self, plan): self.hit('execution_conditions')
    def consume_recovery(self, plan): self.hit('consume')
    def rollback_assets(self, plan): self.hit('assets')
    def before_stop(self, plan): self.hit('record_window')
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
    def phase_window(self, seconds, failure): return contextlib.nullcontext()
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


@pytest.mark.parametrize('failure', [None, 'preconditions', 'consume', 'assets', 'record_window'])
def test_prepare_entry_seals_only_after_live_consumers_and_never_executes(tmp_path, failure):
    class Preparing(Backend):
        def refresh_plan_inputs(self, value, **kwargs):
            self.hit('refresh')
            assert kwargs['remaining_seconds'] == execution.record_consumption_windows(value)
            assert kwargs['revalidate'] is False
            return copy.deepcopy(value)

    backend = Preparing(failure)
    destination = tmp_path / 'deployment_plan.json'
    if failure:
        with pytest.raises(RuntimeError, match='controlled ' + failure):
            execution.prepare_plan(plan(), destination, backend, candidate_key=Path('/test/key.pem'))
        assert not destination.exists()
    else:
        actual = execution.prepare_plan(plan(), destination, backend, candidate_key=Path('/test/key.pem'))
        assert execution.verify_plan(destination) == actual
        assert backend.calls == ['lock', 'refresh', 'execution_conditions', 'preconditions', 'consume', 'assets', 'record_window']
    assert 'stop_source' not in backend.calls and 'cleanup' not in backend.calls
    assert not any(call.startswith(('create_', 'authorize_', 'start_')) for call in backend.calls)


def test_record_consumption_window_uses_each_consumers_actual_phases():
    value = plan()
    before = execution.record_consumption_windows(value)
    value['policy']['observation_seconds'] += 20
    after = execution.record_consumption_windows(value)
    assert before['target'] == after['target']
    assert after['primary_rollback'] == before['primary_rollback'] + 20
    value['policy']['start_timeout_seconds'] += 1
    latest = execution.record_consumption_windows(value)
    assert all(latest[role] == after[role] + 1 for role in latest)


def test_existing_plan_cli_calls_preparation_not_execution(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    class Preparing(Backend):
        def __init__(self, output):
            super().__init__()
            self.output = output
            self.host = SimpleNamespace(_require_linux_root=lambda: None,
                require_protected_authority_source=lambda: None,
                _protected_path=lambda p, **kw: p, _json=json.loads)
            self.engine = SimpleNamespace(_candidate_signing_key=lambda *a: Path('/test/key.pem'))

        def refresh_plan_inputs(self, value, **kwargs):
            self.hit('refresh')
            assert kwargs['candidate_key'] == Path('/test/key.pem')
            assert kwargs['revalidate'] is True
            return copy.deepcopy(value)

    backend = Preparing(tmp_path)
    monkeypatch.setitem(sys.modules, 'release_contract', execution.contract())
    monkeypatch.setitem(sys.modules, 'high_risk_execution', SimpleNamespace(
        HostBackend=lambda output: backend, prepare_plan=execution.prepare_plan))
    cli = execution.load(ROOT, '09_deploy/spread_release/create_deployment_plan.py', '_refresh_actual_plan_cli')
    input_path = tmp_path / 'request.json'
    input_path.write_text(json.dumps(plan()), encoding='utf-8')
    output = tmp_path / 'deployment_plan.json'
    assert cli.main(['--high-risk-input', str(input_path), '--output', str(output),
                     '--revalidate-existing-image']) == 0
    assert execution.verify_plan(output) == plan()
    assert backend.calls == ['lock', 'refresh', 'execution_conditions', 'preconditions', 'consume', 'assets', 'record_window']
    assert json.loads(capsys.readouterr().out)['production_authorized'] is False


def test_sealed_plan_and_success(tmp_path):
    path = sealed(tmp_path)
    backend = Backend()
    result = run(path, backend)
    assert result['result'] == 'SUCCESS'
    assert backend.calls == ['lock', 'preconditions', 'consume', 'assets', 'record_window', 'stop_source',
        'create_target', 'authorize_target', 'start_target', 'accept_target',
        'observe', 'observe', 'observe', 'cleanup', 'record']


@pytest.mark.parametrize('failure', ['preconditions', 'consume', 'assets', 'record_window'])
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
