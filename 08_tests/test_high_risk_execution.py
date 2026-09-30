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
        lock_file='/run/lock/market-data-spread-release.lock', policy=dict(
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


def test_failed_rollback_preserves_recovery_resources(tmp_path):
    backend = Backend('accept_primary_rollback', ['FATAL'])
    result = run(sealed(tmp_path), backend)
    assert result['rollback'] == 'FAIL' and result['resources'] == 'RETAINED_FOR_RECOVERY'
    assert 'cleanup' not in backend.calls
