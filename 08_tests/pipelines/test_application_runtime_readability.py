from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "04_scripts" / "application_runtime_readability_gate.py"
SPEC = importlib.util.spec_from_file_location("application_runtime_readability_test", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "public-current-test"
    data = package / "data"
    data.mkdir(parents=True)
    (package / "manifest.json").write_text(
        json.dumps({"package_id": package.name}), encoding="utf-8"
    )
    (data / "metadata.json").write_text(json.dumps({"ready": True}), encoding="utf-8")
    pq.write_table(pa.table({"value": [1]}), data / "sample.parquet")
    return package


@pytest.fixture(autouse=True)
def _critical_readers_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    if not hasattr(gate.os, "getuid"):
        monkeypatch.setattr(gate.os, "getuid", lambda: 1000, raising=False)
        monkeypatch.setattr(gate.os, "getgid", lambda: 1000, raising=False)
    monkeypatch.setattr(gate, "validate_activated_public_currents", lambda _root: None)
    monkeypatch.setattr(
        gate,
        "validate_formal_consumer_reads",
        lambda **_kwargs: SimpleNamespace(targets={
            "domestic_spread": "PASS",
            "international_spread": "PASS",
            "domestic_basis": "PASS",
            "weather": "PASS",
        }),
    )


def _validate(package: Path, *, uid: int | None = None, gid: int | None = None):
    return gate.validate_application_runtime_readability(
        phase="PRE_SWITCH",
        package_root=package,
        store_root=package.parent / "store",
        project_root=ROOT,
        expected_package_id=package.name,
        expected_uid=os.getuid() if uid is None else uid,
        expected_gid=os.getgid() if gid is None else gid,
        application_container="spread-dashboard",
        identity_source="running-container:test",
    )


def test_gate_uses_discovered_dynamic_uid_not_historical_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(tmp_path)
    monkeypatch.setattr(gate.os, "getuid", lambda: 7123)
    monkeypatch.setattr(gate.os, "getgid", lambda: 8123)
    evidence = _validate(package, uid=7123, gid=8123)
    assert evidence["STATUS"] == "PASS"
    assert evidence["APPLICATION_RUNTIME_UID"] == 7123
    assert evidence["APPLICATION_RUNTIME_UID"] != 65532
    assert evidence["JSON_PARSE"] == "PASS"
    assert evidence["PARQUET_METADATA_READ"] == "PASS"
    assert evidence["THREE_OIL_READER"] == "PASS"


def test_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    package = _package(tmp_path)
    with pytest.raises(RuntimeError) as caught:
        _validate(package, uid=os.getuid() + 1)
    evidence = caught.value.evidence
    assert evidence["STATUS"] == "FAIL"
    assert evidence["SAFE_REASON"] == "PermissionError"
    assert evidence["MANIFEST_READ"] == "FAIL"


def test_required_internal_file_permission_error_is_blocking_on_every_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(tmp_path)
    blocked = package / "data" / "metadata.json"
    original_open = Path.open

    def permission_fault(path: Path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("deterministic unreadable package artifact")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", permission_fault)
    with pytest.raises(RuntimeError) as caught:
        _validate(package)
    evidence = caught.value.evidence
    assert evidence["MANIFEST_READ"] == "PASS"
    assert evidence["STATUS"] == "FAIL"
    assert evidence["SAFE_REASON"] == "PermissionError"


def test_directory_traversal_permission_error_is_blocking_on_every_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(tmp_path)
    blocked = package / "data"
    original_scandir = gate.os.scandir

    def permission_fault(path):
        if Path(path) == blocked:
            raise PermissionError("deterministic directory traversal failure")
        return original_scandir(path)

    monkeypatch.setattr(gate.os, "scandir", permission_fault)
    with pytest.raises(RuntimeError) as caught:
        _validate(package)
    evidence = caught.value.evidence
    assert evidence["DIRECTORY_TRAVERSAL"] == "FAIL"
    assert evidence["STATUS"] == "FAIL"
    assert evidence["SAFE_REASON"] == "PermissionError"


@pytest.mark.skipif(os.name == "nt" or getattr(os, "geteuid", lambda: 0)() == 0,
                    reason="POSIX permission fault injection requires non-root Linux")
def test_manifest_readable_but_required_internal_file_unreadable_fails(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    blocked = package / "data" / "metadata.json"
    blocked.chmod(0)
    try:
        with pytest.raises(RuntimeError) as caught:
            _validate(package)
        evidence = caught.value.evidence
        assert evidence["MANIFEST_READ"] == "PASS"
        assert evidence["STATUS"] == "FAIL"
    finally:
        blocked.chmod(stat.S_IRUSR | stat.S_IWUSR)


@pytest.mark.skipif(os.name == "nt" or getattr(os, "geteuid", lambda: 0)() == 0,
                    reason="POSIX traversal fault injection requires non-root Linux")
def test_directory_traversal_permission_failure_is_blocking(tmp_path: Path) -> None:
    package = _package(tmp_path)
    blocked = package / "data"
    blocked.chmod(stat.S_IRUSR | stat.S_IWUSR)
    try:
        with pytest.raises(RuntimeError) as caught:
            _validate(package)
        evidence = caught.value.evidence
        assert evidence["DIRECTORY_TRAVERSAL"] == "FAIL"
        assert evidence["STATUS"] == "FAIL"
    finally:
        blocked.chmod(stat.S_IRWXU)


def test_post_switch_resolver_and_critical_consumers_are_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package(tmp_path)
    spread = package / "data" / "spread.parquet"
    pq.write_table(pa.table({"value": [1]}), spread)
    monkeypatch.setattr(gate, "resolve_server_current_data_root", lambda _root: package / "data")
    monkeypatch.setattr(gate, "resolve_domestic_spread_path", lambda _root: spread)
    evidence = gate.validate_application_runtime_readability(
        phase="POST_SWITCH", package_root=None, store_root=tmp_path / "store",
        project_root=ROOT, expected_package_id=package.name,
        expected_uid=os.getuid(), expected_gid=os.getgid(),
        application_container="spread-dashboard", identity_source="running-container:test",
    )
    assert evidence["STATUS"] == "PASS"
    assert evidence["ACTIVATED_RUNTIME_RESOLVER"] == "PASS"
    assert all(evidence[key] == "PASS" for key in (
        "DOMESTIC_SPREAD_READER", "THREE_OIL_READER",
        "DOMESTIC_BASIS_READER", "WEATHER_READER",
    ))
