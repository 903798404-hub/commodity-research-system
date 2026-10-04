from pathlib import Path

import pytest

from agri_research_agent.soybean_margin import runtime


def test_unconfigured_save_is_rejected_without_creating_database(tmp_path, monkeypatch):
    for name in ('MARKET_DATA_GIT_HEAD', 'MARKET_DATA_EXECUTION_GRANT', 'SOYBEAN_MARGIN_LOCAL_PREVIEW'):
        monkeypatch.delenv(name, raising=False)
    target = tmp_path / 'cnf.sqlite3'
    with pytest.raises(ValueError, match='运行身份'):
        runtime.validate_cnf_write(target)
    assert not target.exists()


def test_local_preview_can_save_only_to_unaliased_existing_directory(tmp_path, monkeypatch):
    monkeypatch.setenv('SOYBEAN_MARGIN_LOCAL_PREVIEW', '1')
    runtime.validate_cnf_write(tmp_path / 'cnf.sqlite3')
    with pytest.raises(ValueError, match='目录不存在'):
        runtime.validate_cnf_write(tmp_path / 'missing' / 'cnf.sqlite3')
    assert not (tmp_path / 'missing').exists()


def test_preview_flag_cannot_bypass_deployed_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('MARKET_DATA_GIT_HEAD', 'a' * 40)
    monkeypatch.setenv('SOYBEAN_MARGIN_LOCAL_PREVIEW', '1')
    def reject(_):
        raise RuntimeError('missing grant')
    monkeypatch.setattr(runtime, 'load_runtime_identity', reject)
    with pytest.raises(RuntimeError, match='missing grant'):
        runtime.validate_cnf_write(tmp_path / 'cnf.sqlite3')
    assert not (tmp_path / 'cnf.sqlite3').exists()


def test_oci_write_requires_exact_store_and_shared_service_grant(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agri_research_agent.shared.runtime_context import RuntimeClassification
    monkeypatch.setenv('MARKET_DATA_GIT_HEAD', 'a' * 40)
    monkeypatch.setattr(runtime, 'ROOT', tmp_path)
    identity = SimpleNamespace(classification=RuntimeClassification.CANDIDATE_VALIDATION,
                               module_id='shared-intraday', runtime_id='candidate')
    monkeypatch.setattr(runtime, 'load_runtime_identity', lambda _: identity)
    authority = []
    monkeypatch.setattr(runtime, 'establish_application_service_context',
                        lambda **kw: authority.append(kw) or 'service-context')
    calls = []
    monkeypatch.setattr(runtime, 'assert_runtime_write', lambda ctx, path: calls.append(path))
    target = tmp_path / runtime.RELATIVE
    target.parent.mkdir(parents=True)
    runtime.validate_cnf_write(target)
    assert calls == [target]
    assert authority == [dict(service_id='spread-dashboard', module_id='shared-intraday', runtime_root=tmp_path)]
    with pytest.raises(ValueError, match='路径'):
        runtime.validate_cnf_write(tmp_path / 'other.sqlite3')
    assert calls == [target]
    identity.module_id = 'another-service'
    with pytest.raises(ValueError, match='运行身份'):
        runtime.validate_cnf_write(target)
