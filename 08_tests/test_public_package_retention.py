from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import stat

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_retention", ROOT / "04_scripts/maintenance/public_package_retention.py")
retention = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retention)
NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def windows_algorithm_fixture(monkeypatch):
    # NT chmod does not implement POSIX directory permission bits. Exercise the
    # byte/path algorithm locally; Hosted Linux uses the real owner/mode guard.
    if os.name == "nt":
        monkeypatch.setattr(retention, "private_pool", lambda pool: None)


def package(store, days, content, *, marker):
    identities = {"weather": {"release_id": marker, "manifest_sha256": "a" * 64}}
    current = retention.digest(identities)
    delivery = retention.digest({"current_identity_sha256": current, "delivery_artifacts": {}})
    name = "public-current-" + delivery[:24]
    root = store / "releases" / name
    (root / "data").mkdir(parents=True)
    (root / "data/history.parquet").write_bytes(content)
    records = [{"path": "history.parquet", "size_bytes": len(content), "sha256": retention.sha(root / "data/history.parquet")}]
    manifest = dict(schema_version=retention.SCHEMA, package_id=name, created_at=(NOW - timedelta(days=days)).isoformat(),
                    current_identity_sha256=current, current_identities=identities, delivery_identity_sha256=delivery,
                    delivery_artifacts={}, source_max_dates={}, file_count=1, files=records, bundle_sha256=retention.digest(records))
    (root / "manifest.json").write_bytes(retention.canonical(manifest))
    return root, manifest


def fixture(tmp_path):
    store = tmp_path / "public-data-server-store"
    packages = [package(store, days, b"same complete history", marker=str(days)) for days in (0, 1, 2, 4, 8, 9, 10, 11)]
    p, m = packages[0]
    (store / "current.json").write_bytes(retention.canonical({"package_id": p.name, **{k: m[k] for k in ("current_identity_sha256", "delivery_identity_sha256", "bundle_sha256")}}))
    pool = tmp_path / "objects"; pool.mkdir(mode=0o700); pool.chmod(0o700)
    return store, packages, pool


def test_current_recent_newest_and_explicit_rollback_keep_independent(tmp_path):
    store, packages, _ = fixture(tmp_path)
    pinned = packages[-1][0].name
    result = retention.plan(store, now=NOW, pins=[pinned])
    states = {e["package_id"]: e["action"] for e in result["entries"]}
    assert all(states[p.name] == "KEEP_INDEPENDENT" for p, _ in packages[:4])
    assert states[pinned] == "KEEP_INDEPENDENT"
    assert all(states[p.name] == "COMPACT" for p, _ in packages[4:-1])


def test_content_manifests_all_versions_and_current_preserved_and_idempotent(tmp_path):
    store, packages, pool = fixture(tmp_path)
    before = {p: p.read_bytes() for p in store.rglob("*") if p.is_file()}
    current_file = packages[0][0] / "data/history.parquet"
    current_inode = current_file.stat().st_ino
    result = retention.plan(store, now=NOW, pins=[packages[-1][0].name])
    changed = retention.compact(result, pool)
    assert len(changed) == 3
    assert len(list(pool.iterdir())) == 1
    assert current_file.stat().st_ino == current_inode
    for path, raw in before.items(): assert path.read_bytes() == raw
    old_files = [p / "data/history.parquet" for p, _ in packages[4:-1]]
    assert len({p.stat().st_ino for p in old_files}) == 1
    assert all(not p.stat().st_mode & 0o222 for p in old_files)
    assert retention.compact(result, pool) == []
    assert len(list((store / "releases").iterdir())) == 8


def test_stale_current_refused_before_any_file_change(tmp_path):
    store, _, pool = fixture(tmp_path); result = retention.plan(store, now=NOW)
    (store / "current.json").write_bytes(b"new current")
    with pytest.raises(ValueError, match="current changed"): retention.compact(result, pool)
    assert not list(pool.iterdir())


def test_damaged_inactive_package_refused(tmp_path):
    store, packages, _ = fixture(tmp_path)
    (packages[-1][0] / "data/history.parquet").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="bytes differ"): retention.plan(store, now=NOW)


def test_missing_rollback_refused(tmp_path):
    store, _, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="pin missing"): retention.plan(store, now=NOW, pins=["public-current-" + "b" * 24])


def test_exact_live_file_pins_its_package_not_all_other_packages(tmp_path):
    store, packages, _ = fixture(tmp_path)
    live = packages[-1][0] / "data/history.parquet"
    result = retention.plan(store, now=NOW, busy_paths=[live])
    states = {e["package_id"]: e for e in result["entries"]}
    assert states[packages[-1][0].name]["reasons"] == ["LIVE_REFERENCE"]
    assert states[packages[-2][0].name]["action"] == "COMPACT"


def test_unique_history_stays_byte_exact_in_its_version(tmp_path):
    store, _, pool = fixture(tmp_path)
    unique, _ = package(store, 30, b"unique historical revision including missing values", marker="unique")
    before = (unique / "data/history.parquet").read_bytes()
    retention.compact(retention.plan(store, now=NOW), pool)
    assert (unique / "data/history.parquet").read_bytes() == before
    retention.validate(unique)


def test_external_hardlink_refused_without_mutating_source(tmp_path):
    store, packages, pool = fixture(tmp_path)
    source = packages[-1][0] / "data/history.parquet"
    os.link(source, tmp_path / "external")
    with pytest.raises(ValueError, match="external hardlink"): retention.compact(retention.plan(store, now=NOW), pool)
    assert source.read_bytes() == (tmp_path / "external").read_bytes()


def test_mutable_pool_object_refused(tmp_path):
    store, packages, pool = fixture(tmp_path)
    record = packages[-1][1]["files"][0]
    obj = pool / record["sha256"]; obj.write_bytes(b"same complete history"); obj.chmod(0o644)
    with pytest.raises(ValueError, match="mutable or corrupt"): retention.compact(retention.plan(store, now=NOW), pool)


def test_duplicate_json_and_invalid_current_bundle_refused(tmp_path):
    store, _, _ = fixture(tmp_path)
    (store / "current.json").write_text('{"package_id":"a","package_id":"b"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON"): retention.plan(store, now=NOW)


def test_retention_bounds_refuse_zero_days_or_no_rollback(tmp_path):
    store, _, _ = fixture(tmp_path)
    for days, count in [(0, 3), (7, 1)]:
        with pytest.raises(ValueError, match="bounds"): retention.plan(store, now=NOW, keep_days=days, keep_count=count)


def test_symlink_path_checks_fail_closed_without_following(monkeypatch, tmp_path):
    store, _, _ = fixture(tmp_path)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == store or original(p))
    with pytest.raises(ValueError, match="symlink"): retention.plan(store, now=NOW)


def test_interruption_preserves_every_byte_and_can_resume(monkeypatch, tmp_path):
    store, packages, pool = fixture(tmp_path)
    original = {p: p.read_bytes() for p in store.rglob("*") if p.is_file()}
    result = retention.plan(store, now=NOW)
    operation = "link" if os.name == "nt" else "replace"
    actual = getattr(os, operation)
    def fault(src, dst):
        if Path(dst).parent.parent.name.startswith("public-current-"):
            raise OSError("injected replacement interruption")
        return actual(src, dst)
    with monkeypatch.context() as context:
        context.setattr(os, operation, fault)
        with pytest.raises(OSError, match="injected"): retention.compact(result, pool)
    for path, content in original.items(): assert path.read_bytes() == content
    assert not list(store.rglob(".link-*"))
    retention.compact(result, pool)
    for path, content in original.items(): assert path.read_bytes() == content


def test_changed_manifest_after_planning_refused(tmp_path):
    store, packages, pool = fixture(tmp_path); result = retention.plan(store, now=NOW)
    (packages[-1][0] / "manifest.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="manifest changed"): retention.compact(result, pool)


def test_wrong_current_bundle_refused(tmp_path):
    store, _, _ = fixture(tmp_path); current = retention.read(store / "current.json")
    current["bundle_sha256"] = "b" * 64
    (store / "current.json").write_bytes(retention.canonical(current))
    with pytest.raises(ValueError, match="pointer differs"): retention.plan(store, now=NOW)


def test_unknown_root_file_refused(tmp_path):
    store, packages, _ = fixture(tmp_path)
    (packages[-1][0] / "untracked-incident-evidence").write_bytes(b"retain")
    with pytest.raises(ValueError, match="unexpected package files"): retention.plan(store, now=NOW)


def test_units_explicit_china_timezone_and_no_prune_or_business_expiration():
    folder = ROOT / "04_scripts/maintenance"
    timer = (folder / "market-data-package-retention.timer").read_text()
    service = (folder / "market-data-package-retention.service").read_text()
    assert "04:00:00 Asia/Shanghai" in timer and "Persistent=true" in timer
    assert "--apply" in service and "NoNewPrivileges=true" in service and "IOSchedulingClass=idle" in service
    assert "prune" not in service and "10_logs" not in service
