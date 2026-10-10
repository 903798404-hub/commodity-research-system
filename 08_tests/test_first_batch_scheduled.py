"""Scheduled state, protected read-only inputs, scope and concurrency guards."""
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import sys
import json
import hashlib
import xml.etree.ElementTree as ET

import pytest

from agri_research_agent.automation import first_batch_scheduled as jobs
from agri_research_agent.automation import scheduled_baselines as baselines
from agri_research_agent.data_sources import nutstore_basis as nutstore


def _public_copy_fixture(tmp_path, monkeypatch, *, changed=False, large=False):
    cache = tmp_path / "approved-package"
    source = cache / "data/test/history.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"verified immutable history")
    entry = {"path": "test/history.bin", **jobs.delivery._identity(source)}
    old = {"files": [entry], "package_id": "public-current-old"}
    (cache / "manifest.json").write_bytes(jobs.delivery.canonical_json_bytes(old))
    value = {"baseline_package": str(cache), "baseline_manifest_sha256": jobs.delivery.sha256_file(cache / "manifest.json"),
             "ssh_target": "verified-host", "remote_store_root": "/var/lib/market-data/production-runtime/test/public"}
    downloaded = b"verified new report"
    new = {"files": [{"path": "test/new.bin", "sha256": hashlib.sha256(downloaded).hexdigest(),
                     "size_bytes": 132182680 if large else len(downloaded)}] if changed else [entry],
           "package_id": "public-current-new"}
    raw = jobs.delivery.canonical_json_bytes(new)
    pointer = {"package_id": new["package_id"]}
    monkeypatch.setattr(jobs.delivery, "_public_pointer", lambda _: pointer)
    monkeypatch.setattr(jobs.delivery, "_check_public_pointer", lambda p, m: None)
    monkeypatch.setattr(jobs.delivery, "_remote_hash", lambda *_: hashlib.sha256(raw).hexdigest())
    from agri_research_agent.pipelines import public_data_delivery
    monkeypatch.setattr(public_data_delivery, "validate_production_package",
                        lambda root: SimpleNamespace(manifest=json.loads((root / "manifest.json").read_bytes())))
    transfers = []
    def transport(args, **kwargs):
        if args[0] == "ssh":
            return SimpleNamespace(stdout=raw)
        assert args[0] == "scp"
        transfers.append(kwargs["timeout"])
        # Simulate a valid large report on the measured slow link, without
        # allocating 132 MiB in a control-flow test.
        if large and kwargs["timeout"] < 250:
            raise jobs.delivery.ProductionDataError("simulated large transfer exceeded bound")
        Path(args[-1]).write_bytes(downloaded)
        return SimpleNamespace(stdout=b"")
    monkeypatch.setattr(jobs.delivery, "_run", transport)
    if large:
        original = jobs.delivery._identity
        monkeypatch.setattr(jobs.delivery, "_identity",
            lambda path: {"sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(), "size_bytes": 132182680}
            if Path(path).name == "new.bin" else original(path))
    return value, cache, source, transfers, pointer


def test_public_refresh_reuses_only_verified_unchanged_bytes_and_keeps_input_read_only(tmp_path, monkeypatch):
    value, cache, source, transfers, _ = _public_copy_fixture(tmp_path, monkeypatch)
    before = {p.relative_to(cache): p.read_bytes() for p in cache.rglob("*") if p.is_file()}
    result = baselines.public_baseline(value, tmp_path / "fresh")
    assert (result / "data/test/history.bin").read_bytes() == source.read_bytes()
    assert not transfers
    assert before == {p.relative_to(cache): p.read_bytes() for p in cache.rglob("*") if p.is_file()}


def test_public_refresh_accepts_valid_large_slow_transfer_with_finite_bound(tmp_path, monkeypatch):
    value, _, _, transfers, _ = _public_copy_fixture(tmp_path, monkeypatch, changed=True, large=True)
    result = baselines.public_baseline(value, tmp_path / "fresh")
    assert (result / "data/test/new.bin").read_bytes() == b"verified new report"
    assert len(transfers) == 1 and 250 < transfers[0] <= 1200


def test_public_refresh_rejects_corrupt_cache_without_falling_back_to_network(tmp_path, monkeypatch):
    value, _, source, transfers, _ = _public_copy_fixture(tmp_path, monkeypatch)
    source.write_bytes(b"external cache corruption")
    with pytest.raises(ValueError, match="cached baseline file differs"):
        baselines.public_baseline(value, tmp_path / "fresh")
    assert not transfers


def test_public_refresh_still_rejects_a_moved_formal_pointer(tmp_path, monkeypatch):
    value, _, _, _, pointer = _public_copy_fixture(tmp_path, monkeypatch)
    pointers = iter([pointer, {"package_id": "public-current-other"}])
    monkeypatch.setattr(jobs.delivery, "_public_pointer", lambda _: next(pointers))
    with pytest.raises(ValueError, match="moved while copying"):
        baselines.public_baseline(value, tmp_path / "fresh")


def test_public_refresh_exhausted_total_budget_stops_before_next_copy(tmp_path, monkeypatch):
    value, _, _, transfers, _ = _public_copy_fixture(tmp_path, monkeypatch, changed=True)
    clock = iter([0, 2701])
    monkeypatch.setattr(baselines.time, "monotonic", lambda: next(clock))
    with pytest.raises(ValueError, match="time budget exhausted"):
        baselines.public_baseline(value, tmp_path / "fresh")
    assert not transfers


def test_protected_host_entry_keeps_business_imports_read_only_without_caller_bytecode_flag(tmp_path):
    import subprocess
    module = tmp_path / "readonly_business_probe.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    code = """
import runpy, sys
from pathlib import Path
assert not sys.dont_write_bytecode
runpy.run_path(sys.argv[1], run_name='protected_host_probe')
sys.path.insert(0,sys.argv[2])
import readonly_business_probe
assert readonly_business_probe.VALUE == 1 and sys.dont_write_bytecode
assert not (Path(sys.argv[2])/'__pycache__').exists()
print('READ_ONLY_IMPORT_PASS')
"""
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code,
        str(jobs.delivery.ROOT / "09_deploy/production_data_delivery/activate_production_data_delta.py"),
        str(tmp_path)],capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "READ_ONLY_IMPORT_PASS"


def config(tmp_path, job="canola_exports"):
    inner = {"schema_version": jobs.exports.CONFIG_SCHEMA, "approved_commit": "a" * 40,
        "approved_tree": "b" * 40, "origin": jobs.delivery.ORIGIN, "python": str(Path(sys.executable).resolve()),
        "runtime_root": str(tmp_path / "producer"), "baseline_root": str(tmp_path / "initial-baseline"),
        "baseline_manifest_sha256": "c" * 64, "ssh_target": "verified-host",
        "publisher": "/opt/market-data/publisher.py", "publisher_sha256": "d" * 64,
        "image_id": "sha256:" + "e" * 64, "remote_allocation": "/var/lib/market-data/production-runtime/test",
        "policy": {"canola_exports": "/etc/market-data/production-data-delivery/exports.json"},
        "policy_sha256": {"canola_exports": "f" * 64}, "initial_years": 2, "expected_cutoff": None}
    if job.startswith("positions_"):
        inner["schema_version"] = jobs.positions.CONFIG_SCHEMA
        inner.pop("initial_years")
        inner.pop("expected_cutoff")
        inner.update(bundle_root=str(tmp_path / "initial-bundle"), bundle_manifest_sha256="a" * 64,
                     policy={"commodity_positions": "/etc/market-data/production-data-delivery/positions.json"},
                     policy_sha256={"commodity_positions": "f" * 64})
    if job == "nutstore_basis":
        inner = {k: v for k, v in inner.items() if k in jobs.BASIS_KEYS}
        inner.pop("baseline_root", None)
        inner.update(schema_version="nutstore-basis-scheduled-delivery/1", baseline_package=str(tmp_path / "initial-package"),
                     source=str(nutstore.SOURCE_PATH), full_daily_lock_path=str(tmp_path / "shared" / "full-daily.lock"),
                     remote_store_root="/var/lib/market-data/production-runtime/test/01_data/public-data-server-store")
    return {"schema_version": jobs.SCHEMA, "job": job, "runtime_root": str(tmp_path / "scheduler"), "delivery": inner}


@pytest.mark.parametrize("job", ["canola_exports", "nutstore_basis", "positions_domestic", "positions_foreign"])
def test_task_has_exact_scope_hidden_windows_and_no_overlapping_execution(tmp_path, job):
    value = config(tmp_path, job)
    xml = jobs.task_xml(config=value, control_root=jobs.delivery.ROOT, config_path=tmp_path / "config.json",
                        user_id="local-user", start_date=date(2026, 10, 10))
    root = ET.fromstring(xml)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns) == "InteractiveToken"
    assert root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=ns) == "IgnoreNew"
    assert root.findtext("t:Settings/t:StartWhenAvailable", namespaces=ns) == "true"
    boundaries = [x.text for x in root.findall("t:Triggers/t:CalendarTrigger/t:StartBoundary", ns)]
    assert boundaries == ["2026-10-10T" + clock + "+08:00" for clock in jobs.JOBS[job]["clocks"]]
    assert "launch_first_batch_scheduled.ps1" in root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns)
    assert not (tmp_path / "scheduler").exists()  # XML rendering alone is read only.


@pytest.mark.parametrize("key", ["runtime_root", "delivery.runtime_root", "delivery.baseline_package", "delivery.full_daily_lock_path"])
def test_entire_nutstore_tree_rejected_before_any_write(tmp_path, monkeypatch, key):
    protected = tmp_path / "123"
    monkeypatch.setattr(nutstore, "PROTECTED_ROOT", protected)
    value = config(tmp_path, "nutstore_basis")
    target = value["delivery"] if key.startswith("delivery.") else value
    target[key.split(".")[-1]] = str(protected / "forbidden")
    with pytest.raises(ValueError, match="forbidden for writes"):
        jobs.validate_config(value)
    assert not protected.exists()


def test_basis_rejects_sibling_source_even_when_readable(tmp_path):
    value = config(tmp_path, "nutstore_basis")
    value["delivery"]["source"] = str(tmp_path / "other.xlsx")
    with pytest.raises(ValueError, match="exact allowed workbook"):
        jobs.validate_config(value)


@pytest.mark.parametrize("job", ["canola_exports", "nutstore_basis", "positions_domestic", "positions_foreign"])
def test_schema_closed_and_unrelated_modules_rejected(tmp_path, job):
    value = config(tmp_path, job)
    value["additional_domain"] = "weather"
    with pytest.raises(ValueError, match="configuration invalid"):
        jobs.validate_config(value)


def test_exports_refreshes_before_collection_and_persists_no_change_receipt(tmp_path, monkeypatch):
    value = config(tmp_path)
    flags = {key: getattr(sys.flags, key) for key in dir(sys.flags) if isinstance(getattr(sys.flags, key), int)}
    monkeypatch.setattr(jobs.sys, "flags", SimpleNamespace(**{**flags, "isolated": 1}))
    monkeypatch.setattr(jobs.sys, "dont_write_bytecode", True)
    monkeypatch.setattr(jobs.delivery, "verify_clean_detached_clone", lambda *a, **k: {"commit": "a" * 40})
    calls = []
    def refresh(inner, work, domain, schema):
        calls.append("refresh")
        return {**inner, "baseline_root": str(work / "fresh")}
    def produce(inner, publish):
        calls.append("collect")
        assert inner["baseline_root"].endswith("fresh") and publish
        return {"status": "NO_CHANGE", "published": False, "receipt": {"status": "NO_CHANGE"}}
    monkeypatch.setattr(jobs, "domain_baseline", refresh)
    monkeypatch.setattr(jobs.exports, "run_exports", produce)
    result = jobs.run(value, publish=True)
    assert calls == ["refresh", "collect"] and result["status"] == "NO_CHANGE"
    assert (Path(value["runtime_root"]) / "canola_exports-last-run.json").is_file()


@pytest.mark.parametrize("failure", ["pin", "domain", "identity", "partial", "oversized", "bytes"])
def test_domain_snapshot_rejects_untrusted_input_before_materializing(tmp_path, monkeypatch, failure):
    value = config(tmp_path)["delivery"]
    host = jobs.delivery._host_contract()
    files = {name: {"sha256": "a" * 64, "size_bytes": 1} for name in host.DOMAIN_CONTRACTS["canola_exports"]["baseline"]}
    response = {"schema_version": "production-domain-baseline-snapshot/1", "domain": "canola_exports",
        "producer": {"commit": "a" * 40, "tree": "b" * 40, "origin": jobs.delivery.ORIGIN},
        "image_id": value["image_id"], "allocation_root": value["remote_allocation"],
        "policy_sha256": "f" * 64, "files": files, "contents": dict.fromkeys(files, "eA==")}
    if failure == "domain": response["domain"] = "foreign_fx"
    if failure == "identity": response["producer"]["commit"] = "b" * 40
    if failure == "partial": files[next(iter(files))] = None
    if failure == "oversized": files[next(iter(files))]["size_bytes"] = 384 * 1024 * 1024 + 1
    monkeypatch.setattr(jobs.delivery, "_remote_hash", lambda c, p: "0" * 64 if failure == "pin" else
                        ("d" * 64 if p == c["publisher"] else "f" * 64))
    monkeypatch.setattr(jobs.delivery, "_ssh", lambda *a: jobs.delivery.canonical_json_bytes(response))
    with pytest.raises(ValueError):
        baselines.domain_baseline(value, tmp_path, "canola_exports", jobs.exports.BASELINE_SCHEMA)
    assert not (tmp_path / "baseline").exists()


def test_local_consumer_does_not_require_driver_and_real_connection_still_fails_closed():
    import subprocess
    code = '''
import importlib.abc, sys
sys.path.insert(0, sys.argv[1])
attempts = []
class DenyDriver(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'psycopg' or fullname.startswith('psycopg.'):
            attempts.append(fullname)
            raise ImportError('driver blocked')
sys.meta_path.insert(0, DenyDriver())
from agri_research_agent.application.domestic_spreads import normalize_full_contract_code
from agri_research_agent.data_sources.tankan.client import TankanClient, TankanConnectionSettings, TankanConnectionError
assert not attempts
settings = TankanConnectionSettings(host='unreachable.invalid', port=5432, database='test', user='test', password='private-input')
try:
    with TankanClient(settings):
        raise AssertionError('unavailable driver must never connect')
except TankanConnectionError as error:
    assert str(error) == 'Tankan connection failed: ImportError'
    assert attempts == ['psycopg']
    assert not settings.password
print('PASS')
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", "-c", code,
                             str(jobs.delivery.ROOT / "03_src")], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PASS"


@pytest.mark.parametrize("domain", ["canola_exports", "commodity_positions"])
def test_protected_snapshot_returns_only_domain_contract_and_retains_read_only_files(tmp_path, monkeypatch, domain):
    import base64
    from contextlib import nullcontext
    host = jobs.delivery._host_contract()
    allocation = tmp_path / "allocation"
    policy = {"domain": domain, "allocation_root": str(allocation), "approved_producer": {"commit": "a" * 40},
              "validation_image": {"image_id": "sha256:" + "b" * 64}}
    expected = host.DOMAIN_CONTRACTS[domain]["baseline"]
    original = {}
    for name in expected:
        path = allocation / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("verified " + name).encode())
        original[name] = path.read_bytes()
    unrelated = allocation / "01_data/manual/do-not-export.txt"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_bytes(b"private unrelated domain")
    monkeypatch.setattr(host, "_load_policy", lambda _: (policy, "c" * 64))
    monkeypatch.setattr(host, "_protected", lambda path, **_: Path(path))
    monkeypatch.setattr(host, "_under", lambda path, parent: Path(path))
    monkeypatch.setattr(host, "_lock", lambda _: nullcontext())
    monkeypatch.setattr(host, "_policy_unchanged", lambda *_: None)
    result = host.snapshot_fx_baseline("policy")
    assert result["schema_version"] == "production-domain-baseline-snapshot/1"
    assert set(result["contents"]) == set(expected)
    for name, raw in original.items():
        assert base64.b64decode(result["contents"][name], validate=True) == (allocation / name).read_bytes() == raw
    assert unrelated.read_bytes() == b"private unrelated domain"
    (allocation / expected[-1]).unlink()
    with pytest.raises(ValueError, match="incomplete"):
        host.snapshot_fx_baseline("policy")
