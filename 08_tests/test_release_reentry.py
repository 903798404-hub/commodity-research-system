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
