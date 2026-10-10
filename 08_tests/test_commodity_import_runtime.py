"""Formal commodity writes cannot inherit local flags or overwrite soybean state."""
from datetime import date
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agri_research_agent.commodity_import_margin import runtime, store
from agri_research_agent.shared.runtime_context import RuntimeAuthorizationError, RuntimeClassification


@pytest.fixture
def formal(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_GIT_HEAD", "a" * 40)
    monkeypatch.setenv("COMMODITY_IMPORT_SERVER_ENABLED", "1")
    monkeypatch.delenv("MARKET_DATA_EXECUTION_GRANT", raising=False)
    monkeypatch.delenv("COMMODITY_IMPORT_LOCAL_PREVIEW", raising=False)
    monkeypatch.delenv("COMMODITY_IMPORT_ALLOW_SAVE", raising=False)
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    (tmp_path / runtime.STORAGE.parent).mkdir(parents=True)
    identity = SimpleNamespace(classification=RuntimeClassification.FORMAL, module_id="shared-intraday")
    monkeypatch.setattr(runtime, "load_runtime_identity", lambda _: identity)
    return tmp_path, identity


def test_formal_read_does_not_create_store_and_save_defaults_off(formal):
    root, _ = formal
    path = runtime.database_path()
    assert path == root / runtime.STORAGE / "research.sqlite3"
    assert store.load_cnf(path, date(2026, 10, 10), "canola") == ({}, 0)
    assert not runtime.save_enabled() and not path.parent.exists()
    with pytest.raises(ValueError, match="未启用"):
        runtime.authorize_write(path)


@pytest.mark.parametrize("classification", [RuntimeClassification.FIXTURE, RuntimeClassification.ISOLATED_DEV])
def test_formal_identity_rejects_fixture_or_dev(formal, classification):
    _, identity = formal
    identity.classification = classification
    with pytest.raises(ValueError, match="身份或模块"):
        runtime.database_path()


def test_formal_identity_rejects_wrong_module(formal):
    _, identity = formal
    identity.module_id = "other-module"
    with pytest.raises(ValueError, match="身份或模块"):
        runtime.database_path()


def test_formal_never_falls_back_to_local_storage(formal, monkeypatch):
    monkeypatch.setattr(runtime, "load_runtime_identity", lambda _: (_ for _ in ()).throw(RuntimeAuthorizationError("private identity detail")))
    monkeypatch.setattr(runtime, "local_database", lambda: pytest.fail("formal must not fall back"))
    with pytest.raises(ValueError, match="有效的工作台") as exc:
        runtime.database_path()
    assert "private" not in str(exc.value)


def test_formal_rejects_local_preview_flag(formal, monkeypatch):
    monkeypatch.setenv("COMMODITY_IMPORT_LOCAL_PREVIEW", "1")
    monkeypatch.setenv("COMMODITY_IMPORT_ALLOW_SAVE", "1")
    with pytest.raises(ValueError, match="不能启用本地"):
        runtime.database_path()


def test_formal_missing_mount_is_not_created(formal):
    root, _ = formal
    (root / runtime.STORAGE.parent).rmdir()
    with pytest.raises(ValueError, match="目录缺失"):
        runtime.database_path()
    assert not (root / runtime.STORAGE.parent).exists()


def test_save_switch_alone_does_not_authorize_writes(formal, monkeypatch):
    monkeypatch.setenv("COMMODITY_IMPORT_ALLOW_SAVE", "1")
    monkeypatch.setattr(runtime, "establish_application_service_context", lambda **_: (_ for _ in ()).throw(RuntimeAuthorizationError("secret must not be shown")))
    path = runtime.database_path()
    with pytest.raises(ValueError, match="服务写入权限") as exc:
        store.save_cnf(path, date(2026, 10, 10), "palm", {m: None for m in range(1, 13)}, 0, authorize=runtime.authorize_write)
    assert "secret" not in str(exc.value) and not path.exists()


@pytest.mark.parametrize("role", [RuntimeClassification.FORMAL, RuntimeClassification.CANDIDATE_VALIDATION])
def test_authorized_store_retains_zero_null_cas_and_soybean_bytes(formal, monkeypatch, role):
    root, identity = formal
    identity.classification = role
    monkeypatch.setenv("COMMODITY_IMPORT_ALLOW_SAVE", "1")
    service_calls, write_calls = [], []
    context = object()
    def service(**values):
        service_calls.append(values)
        return context
    def write(actual, path):
        assert actual is context
        write_calls.append(path)
    monkeypatch.setattr(runtime, "establish_application_service_context", service)
    monkeypatch.setattr(runtime, "assert_runtime_write", write)
    soybean = root / runtime.STORAGE.parent / "cnf.sqlite3"
    soybean.write_bytes(b"existing soybean state must remain byte identical")
    old_sha = hashlib.sha256(soybean.read_bytes()).hexdigest()
    path = runtime.database_path()
    day = date(2026, 10, 10)
    values = {m: None for m in range(1, 13)}
    values[1] = 0
    assert store.save_cnf(path, day, "palm", values, 0, authorize=runtime.authorize_write) == 1
    assert store.load_cnf(path, day, "palm")[0][1] == 0
    assert store.load_cnf(path, day, "canola") == ({}, 0)
    with pytest.raises(ValueError, match="另一会话"):
        store.save_cnf(path, day, "palm", values, 0, authorize=runtime.authorize_write)
    values[1] = None
    assert store.save_cnf(path, day, "palm", values, 1, authorize=runtime.authorize_write) == 2
    assert store.load_cnf(path, day, "palm")[0][1] is None
    assert service_calls and all(c == dict(service_id="spread-dashboard", module_id="shared-intraday", runtime_root=root) for c in service_calls)
    assert len(write_calls) >= 6 and set(write_calls) == {path}
    assert hashlib.sha256(soybean.read_bytes()).hexdigest() == old_sha
    with pytest.raises(ValueError, match="独立业务库"):
        runtime.authorize_write(soybean)


def test_runtime_write_rechecks_shared_authority(formal, monkeypatch):
    monkeypatch.setenv("COMMODITY_IMPORT_ALLOW_SAVE", "1")
    monkeypatch.setattr(runtime, "establish_application_service_context", lambda **_: object())
    monkeypatch.setattr(runtime, "assert_runtime_write", lambda *_: (_ for _ in ()).throw(RuntimeAuthorizationError("stale deployment")))
    path = runtime.database_path()
    with pytest.raises(ValueError, match="服务写入权限"):
        runtime.authorize_write(path)
    assert not path.exists()


def test_packaging_keeps_new_runtime_sources_and_disabled_default():
    from agri_research_agent.shared.runtime_manifest import parse_runtime_manifest
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "02_configs/runtime_contracts/spread-production-runtime.json").read_text(encoding="utf-8"))
    parse_runtime_manifest(manifest)
    declared = {entry["path"] for entry in manifest["source_inputs"]}
    docker = (root / "09_deploy/spread_runtime/Dockerfile.spread-runtime").read_text(encoding="utf-8")
    for source in (root / "03_src/agri_research_agent/commodity_import_margin").glob("*.py"):
        relative = source.relative_to(root).as_posix()
        assert relative in declared and json.dumps(relative) in docker
    assert "05_apps/commodity_import_margin_page.py" in declared
    assert "COMMODITY_IMPORT_ALLOW_SAVE" in manifest["required_environment"]
    compose = (root / "09_deploy/spread_runtime/compose.yml").read_text(encoding="utf-8")
    assert "COMMODITY_IMPORT_ALLOW_SAVE: ${COMMODITY_IMPORT_ALLOW_SAVE:-0}" in compose


@pytest.mark.parametrize("flag", ["COMMODITY_IMPORT_SERVER_ENABLED", "COMMODITY_IMPORT_ALLOW_SAVE"])
def test_server_flags_without_identity_cannot_reach_local_store(monkeypatch, tmp_path, flag):
    monkeypatch.delenv("MARKET_DATA_GIT_HEAD", raising=False)
    monkeypatch.delenv("MARKET_DATA_EXECUTION_GRANT", raising=False)
    monkeypatch.setenv("COMMODITY_IMPORT_LOCAL_PREVIEW", "1")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv(flag, "1")
    with pytest.raises(ValueError, match="受保护"):
        runtime.database_path()
    with pytest.raises(ValueError, match="受保护"):
        runtime.authorize_write(tmp_path / "research.sqlite3")
    assert not list(tmp_path.iterdir())


def test_both_server_switches_are_required_and_reads_remain_available(formal, monkeypatch):
    path = runtime.database_path()
    monkeypatch.setenv("COMMODITY_IMPORT_ALLOW_SAVE", "1")
    monkeypatch.delenv("COMMODITY_IMPORT_SERVER_ENABLED", raising=False)
    assert runtime.database_path() == path and not runtime.save_enabled()
    with pytest.raises(ValueError, match="未启用"):
        runtime.authorize_write(path)
    assert not path.exists()
