"""Shared release-tool current-instance and stale-resource consumer obligations."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_pre_release_runtime import runtime
from test_high_risk_execution import execution


@pytest.mark.parametrize('fault', [None, 'different_baseline', 'missing_container', 'missing_sandbox', 'permission'])
def test_stale_recovery_is_history_not_current_capability(tmp_path, fault):
    class HostError(ValueError):
        pass
    sandbox = tmp_path / 'sandbox'
    sandbox.mkdir()
    policy = dict(recovery=dict(production_container_id='A', sandbox=dict(root=str(sandbox))))
    values = {'policy': policy, 'instance': dict(container_id='sandbox-instance')}
    host = SimpleNamespace(_json=lambda raw: json.loads(raw), validate_policy=lambda *a: None,
        _protected_path=lambda path, **kw: path, HostAuthorizationError=HostError)
    def inspect(cid):
        if fault == 'missing_container':
            raise HostError('Error response from daemon: No such container: ' + cid)
        if fault == 'permission':
            raise HostError('permission denied')
        return dict(Id=cid)
    host.docker_inspect = inspect
    refs = {}
    import hashlib
    for name, value in values.items():
        path = tmp_path / (name+'.json')
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        refs[name] = dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())
    evidence = dict(recovery_policy=refs['policy'], evidence=dict(instance=refs['instance']))
    original = copy.deepcopy(evidence)
    if fault == 'missing_sandbox': sandbox.rmdir()
    if fault:
        error = HostError if fault == 'permission' else runtime.PreReleaseError
        match = 'permission denied' if fault == 'permission' else 'NEW_RECOVERY_REHEARSAL_REQUIRED'
        with pytest.raises(error, match=match):
            runtime.require_live_recovery_dependencies(evidence, host,
                source_container_id='B' if fault == 'different_baseline' else 'A')
    else:
        runtime.require_live_recovery_dependencies(evidence, host, source_container_id='A')
    assert evidence == original


def test_non_sandbox_observation_is_not_an_implicit_live_rehearsal():
    runtime.require_live_recovery_dependencies({'evidence': {}}, SimpleNamespace(), source_container_id='B')
    # It is only a retrospective fact. consume_recovery separately requires
    # isolated sandbox policy, and the formal verifier still checks signatures.


def test_hosted_two_release_cycle_uses_original_app_and_same_formal_entries():
    path = execution.ROOT / '08_tests/shared/high_risk_execution_docker_e2e.py'
    source = path.read_text(encoding='utf-8')
    assert "OLD = '88df880127bea4308ee752a37a59884b9198b2ce'" in source
    assert "('reentry-success','REENTRY_SUCCESS')" in source
    assert "('reentry-failure','REENTRY_FAILURE')" in source
    assert 'execution.prepare_plan(' in source and 'execution.execute_verified_plan(' in source
    assert 'existing=successor' in source and 'collect_retained_replacement(' in source
    assert 'application_source_root=old_source' in source and 'existing_image_id=asset_old' in source


def test_formal_issuer_api_is_checked_before_candidate_create(monkeypatch, tmp_path):
    engine = execution.load(execution.ROOT, '04_scripts/runtime/validate_target_runtime.py', '_reentry_engine_api')
    fake = SimpleNamespace()
    with pytest.raises(engine.ValidationError, match='UNSUPPORTED_APPLICATION_ISSUER_API'):
        engine._candidate_issuer_api(fake, dict(_secret_declarations={}))
    assert 'declared_secret_targets' not in engine._candidate_issuer_api.__code__.co_names


def test_no_legacy_private_observer_or_issuer_secret_helper_in_actual_transport():
    engine = execution.load(execution.ROOT, '04_scripts/runtime/validate_target_runtime.py', '_reentry_transport_audit')
    assert 'declared_secret_targets' not in engine.validate_linux.__code__.co_names
    assert '_observe' not in execution.validate_issuer_api.__code__.co_names


@pytest.mark.parametrize('acceptance_fails', [False, True])
def test_supplemental_acceptance_has_new_evidence_namespace_not_new_instance(monkeypatch, tmp_path,
                                                                          acceptance_fails):
    original = tmp_path / 'failed-attempt'
    original.mkdir()
    evidence = original / 'primary_rollback-application-preflight.json'
    evidence.write_bytes(b'{"status":"FAIL","original":true}')
    original_bytes = evidence.read_bytes()
    backend = execution.HostBackend.__new__(execution.HostBackend)
    backend.output = original
    backend.host = SimpleNamespace(_protected_path=lambda path, **kw: path)
    session, envelope = object(), {'payload': {'container_id': 'retained-B'}}
    backend.sessions = {'primary_rollback': session}
    backend.envelopes = {'primary_rollback': envelope}
    backend.acceptance_timeline = [{'status': 'FAIL', 'phase': 'ORIGINAL_TIMEOUT'}]
    original_timeline = copy.deepcopy(backend.acceptance_timeline)
    calls = []

    def initialize(attempt, output):
        attempt.output = output
        attempt.sessions, attempt.envelopes, attempt.acceptance_timeline = {}, {}, []

    def accept(attempt, plan, role):
        calls.append((attempt, plan, role))
        assert attempt is not backend
        assert attempt.sessions[role] is session and attempt.envelopes[role] is envelope
        # Exercise the actual exclusive-file requirement that failed on Linux.
        with (attempt.output / evidence.name).open('xb') as stream:
            stream.write(b'{"status":"NEW_ATTEMPT"}')
        if acceptance_fails:
            raise execution.ExecutionError('SUPPLEMENTAL_REAL_GATE_FAILED')
        attempt.acceptance_timeline.append({'phase': 'COMPLETE_ACCEPTANCE', 'status': 'PASS'})

    monkeypatch.setattr(execution.HostBackend, '__init__', initialize)
    monkeypatch.setattr(execution.HostBackend, 'accept', accept)
    destination, plan = tmp_path / 'supplemental-attempt', {'identity': 'same-plan'}
    if acceptance_fails:
        with pytest.raises(execution.ExecutionError, match='SUPPLEMENTAL_REAL_GATE_FAILED'):
            backend._accept_retained_instance(plan, destination)
    else:
        timeline = backend._accept_retained_instance(plan, destination)
        assert timeline == [{'phase': 'COMPLETE_ACCEPTANCE', 'status': 'PASS'}]
    assert len(calls) == 1 and calls[0][1:] == (plan, 'primary_rollback')
    assert evidence.read_bytes() == original_bytes
    assert backend.acceptance_timeline == original_timeline
    assert backend.output == original and backend.sessions['primary_rollback'] is session
    # A repeated attempt cannot overwrite supplemental evidence either.
    with pytest.raises(FileExistsError):
        backend._accept_retained_instance(plan, destination)
    assert len(calls) == 1


@pytest.mark.parametrize('fault', [None, 'missing_role', 'wrong_mount', 'readonly_log', 'business_alias'])
def test_retained_preservation_uses_declared_log_role_not_writable_store_bypass(fault):
    backend = execution.HostBackend.__new__(execution.HostBackend)
    session = SimpleNamespace(contract={'runtime_roots': [
        dict(role='logs', access='rw', container_path='/runtime/10_logs')]},
        current_policy=dict(grant_container_directory='/run/market-data-grants', mounts=[
            dict(source='/stores/logs', target='/runtime/10_logs', read_only=False),
            dict(source='/stores/manual-cnf', target='/runtime/import-profit/operational/cnf', read_only=False),
            dict(source='/stores/results', target='/runtime/import-profit/operational/am-results', read_only=False),
            dict(source='/stores/snapshots', target='/runtime/capture-snapshots', read_only=False),
            dict(source='/stores/snapshots', target='/runtime/import-profit/snapshots', read_only=True),
            dict(source='/stores/data', target='/runtime/01_data', read_only=True),
            dict(source='/stores/grants', target='/run/market-data-grants', read_only=True),
            dict(source='/stores/secrets/tankan.env', target='/run/secrets/tankan.env', read_only=True)]))
    if fault == 'missing_role':
        session.contract['runtime_roots'] = []
    elif fault == 'wrong_mount':
        session.current_policy['mounts'][0]['target'] = '/runtime/logs'
    elif fault == 'readonly_log':
        session.current_policy['mounts'][0]['read_only'] = True
    elif fault == 'business_alias':
        session.current_policy['mounts'].append(dict(source='/stores/logs', target='/runtime/06_outputs', read_only=False))
    if fault in {'missing_role', 'wrong_mount', 'readonly_log'}:
        with pytest.raises(execution.ExecutionError, match='DECLARED_LOG_'):
            backend._retained_preservation_sources(session)
    else:
        sources, log = backend._retained_preservation_sources(session)
        assert set(sources) == {'/stores/manual-cnf', '/stores/results', '/stores/snapshots', '/stores/data'} | (
            {'/stores/logs'} if fault == 'business_alias' else set())
        assert log == dict(target='/runtime/10_logs', source='/stores/logs', invariance_claimed=False,
                           reason='Declared writable logging role')


@pytest.mark.parametrize('changed', [False, True])
def test_retained_business_data_gate_preserves_failure_observations(tmp_path, changed):
    backend = execution.HostBackend.__new__(execution.HostBackend)
    backend.engine = execution.load(execution.ROOT, '04_scripts/runtime/validate_target_runtime.py',
                                    '_retained_observation_writer')
    before = {'/stores/manual-cnf': {'quote.json': {'sha256': '1' * 64, 'st_mtime_ns': 1}}}
    after = copy.deepcopy(before)
    if changed:
        after['/stores/manual-cnf']['quote.json']['sha256'] = '2' * 64
    log = dict(target='/runtime/10_logs', source='/stores/logs', invariance_claimed=False)
    if changed:
        with pytest.raises(execution.ExecutionError, match='SUPPLEMENTAL_ACCEPTANCE_CHANGED_DATA'):
            backend._record_retained_preservation(tmp_path, before, after, list(before), log)
    else:
        backend._record_retained_preservation(tmp_path, before, after, list(before), log)
    observation = json.loads((tmp_path / 'source-preservation.json').read_bytes())
    assert observation['before'] == before and observation['after'] == after
    assert observation['changed_sources'] == (list(before) if changed else [])
    assert observation['scope'] == list(before) and observation['excluded_log'] == log
    assert observation['whole_database_invariance_claimed'] is False


@pytest.mark.parametrize('fault', [None, 'wrong_hash', 'missing_manifest', 'changed_plan', 'unprotected'])
def test_retained_sealed_plan_uses_formal_artifact_reader_not_private_record_mode(tmp_path, fault):
    import hashlib
    from test_high_risk_execution import sealed, plan
    path = sealed(tmp_path)
    original = path.read_bytes()
    backend = execution.HostBackend.__new__(execution.HostBackend)
    protected_calls = []
    def protected(value, **kwargs):
        protected_calls.append((value, kwargs))
        assert not kwargs.get('private')
        if fault == 'unprotected':
            raise ValueError('unprotected ancestor')
        return value
    backend.host = SimpleNamespace(_protected_path=protected, _json=json.loads)
    reference = dict(path=str(path), sha256=hashlib.sha256(original).hexdigest())
    if fault == 'wrong_hash':
        reference['sha256'] = '0' * 64
    elif fault == 'missing_manifest':
        path.with_name('deployment_plan.manifest.json').unlink()
    elif fault == 'changed_plan':
        path.chmod(0o600)
        path.write_bytes(original + b' ')
        reference['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    if fault:
        with pytest.raises((execution.ExecutionError, ValueError)):
            backend.verify_execution_plan(reference)
    else:
        assert backend.execution_plan_reference(path) == reference
        assert backend.verify_execution_plan(reference) == plan()
        assert path.read_bytes() == original
        assert any(value.name == 'deployment_plan.manifest.json' for value, _ in protected_calls)


def test_private_authorization_reader_is_not_relaxed_for_sealed_plan_compatibility(tmp_path):
    path = tmp_path / 'private-policy.json'
    path.write_bytes(b'{}')
    backend = execution.HostBackend.__new__(execution.HostBackend)
    calls = []
    def protected(value, **kwargs):
        calls.append(kwargs)
        raise ValueError('private authorization file must have mode 0600')
    backend.host = SimpleNamespace(_protected_path=protected)
    with pytest.raises(ValueError, match='mode 0600'):
        backend.read(dict(path=str(path), sha256='0' * 64))
    assert calls == [{'private': True}]
