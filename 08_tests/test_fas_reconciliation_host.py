"""Real receipt/state logic on temporary fixtures; Linux authority is mocked.

No network, official observations, Docker image admission or production writes
are involved. Exact Linux ownership/isolation is a separate required gate.
"""
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sys
import subprocess
from types import SimpleNamespace

import pytest

from agri_research_agent.automation import production_data_delta as delivery
from agri_research_agent.soybean_exports import fas
from test_production_data_delta_activation import load_module, path_identity, ORIGIN
from test_table_delta_worker import fas_fixture


def setup_monthly(tmp_path, monkeypatch, *, stale_source=False):
    module, fixture = load_module(), fas_fixture()
    clock = [datetime(2026, 8, 7, tzinfo=timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return clock[0].astimezone(tz or timezone.utc)
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(delivery, "datetime", Clock)
    monkeypatch.setattr(fas, "utc_now", lambda: clock[0])
    rows = [fixture.fas_row(report_year=year, week=f"{year}-07-30T00:00:00") for year in range(2020, 2027)]
    class Adapter(fixture.StaticAdapter):
        def __init__(self): super().__init__(rows); self.queries = []
        def fetch_export_sales(self, years=None, *, recent_years=7, retained_years=()):
            self.queries.append(recent_years)
            selected = [row for row in rows if row["_report_market_year_end"] >= 2026-recent_years+1
                        or row["_report_market_year_end"] in retained_years]
            fetch_time = datetime(2026, 8, 7, tzinfo=timezone.utc) if stale_source else clock[0]
            return replace(fixture.StaticAdapter(selected).fetch_export_sales(years), fetch_time_utc=fas.iso_utc(fetch_time))
    allocation = tmp_path / "allocations/fas"
    source = tmp_path / "collection"
    commit, tree = fixture.GIT_HEAD, "a" * 40
    producer = {"commit": commit, "tree": tree, "origin": ORIGIN}
    fas.run_fas_pipeline(runtime_root=allocation, adapter=Adapter(), git_head=commit, batch_id="seed", incremental=True)
    paths = fas.fas_paths(allocation, "unused")
    files = {relative: path_identity(allocation / relative)
             for relative in module.DOMAIN_CONTRACTS["soybean_export_sales"]["baseline"]}
    before = {key: paths[key].read_bytes() for key in ("stable", "stable_manifest", "status")}
    clock[0] = datetime(2026, 9, 5, tzinfo=timezone.utc)
    adapter = Adapter()
    audit = fas.run_fas_pipeline(runtime_root=source, baseline_root=allocation, adapter=adapter, git_head=commit,
        batch_id="monthly", incremental=True, incremental_transport=True, publish=False)
    assert adapter.queries == [7]
    policy = {"policy_id": "monthly-fas-fixture-001", "domain": "soybean_export_sales", "allocation_root": str(allocation),
        "approved_producer": producer, "validation_image": {"image_id": "sha256:" + "c" * 64, "commit": commit, "tree": tree}}
    for name, dirname in (("UPLOAD_ROOT", "uploads"), ("INCOMING_ROOT", "incoming"), ("CANDIDATE_ROOT", "candidates"),
                           ("EVIDENCE_ROOT", "evidence"), ("BACKUP_ROOT", "backups")):
        root = tmp_path / dirname
        (root / policy["policy_id"]).mkdir(parents=True)
        monkeypatch.setattr(module, name, root)
    monkeypatch.setattr(module, "ALLOCATION_ROOT", tmp_path / "allocations")
    upload = module.UPLOAD_ROOT / policy["policy_id"] / "monthly-fas-fixture-001"
    manifest = delivery.collect_delta("soybean_export_sales", source, upload, producer,
        {key.removeprefix("01_data/"): value for key, value in files.items()},
        run_id="monthly-fas-fixture-001", baseline_root=allocation)
    delta = json.loads(manifest.read_text(encoding="utf-8"))
    assert delta["run"]["business_status"] == "no_change"
    app = tmp_path / "app"
    app.mkdir()
    (app / "RELEASE.json").write_text(json.dumps({"git_commit": commit, "git_tree": tree}), encoding="utf-8")
    worker_candidate = [None]
    def mapped(value):
        raw = str(value).replace("\\", "/")
        for prefix, destination in {"/candidate": worker_candidate[0], "/allocation": allocation, "/app": app}.items():
            if raw == prefix or raw.startswith(prefix + "/"):
                return destination / raw[len(prefix):].lstrip("/")
        return Path(value)
    monkeypatch.setattr(module, "Path", mapped)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("MARKET_DATA_GIT_HEAD", commit)
    monkeypatch.setenv("MARKET_DATA_GIT_TREE", tree)
    monkeypatch.setattr(module, "_load_policy", lambda *_: (policy, "f" * 64))
    monkeypatch.setattr(module, "_protected", lambda path, **_: Path(path))
    monkeypatch.setattr(module, "_protected_tree", lambda path: Path(path))
    monkeypatch.setattr(module, "_lock", lambda *_: nullcontext())
    monkeypatch.setattr(module, "_policy_unchanged", lambda *_: None)
    monkeypatch.setattr(module, "_git_identity", lambda *_: producer)
    monkeypatch.setattr(module, "_worker", lambda *_: module.worker_validate("soybean_export_sales", commit, tree))
    staged = module.stage_upload(tmp_path / "policy", upload, module.sha256_file(manifest))
    received = module.receive(tmp_path / "policy", staged["incoming_path"], module.sha256_file(manifest))
    worker_candidate[0] = Path(received["candidate_path"])
    clock[0] += timedelta(hours=1)
    return SimpleNamespace(module=module, fixture=fixture, clock=clock, policy=policy, delta=delta, files=files,
        allocation=allocation, source=source, paths=paths, before=before, adapter=Adapter,
        worker_candidate=worker_candidate, audit=audit)


def complete(context):
    m, delta = context.module, context.delta
    validation = m.validate("policy", delta["delta_id"])
    return m.publish("policy", delta["delta_id"], validation["report_path"], validation["report_sha256"])


def test_monthly_nochange_records_attested_check_and_next_week_fetches_two_years(tmp_path, monkeypatch):
    c = setup_monthly(tmp_path, monkeypatch)
    result = complete(c)
    assert result["status"] == "NO_CHANGE" and result["backup_path"] is None
    assert {key: c.paths[key].read_bytes() for key in c.before} == c.before
    snapshot = c.module.snapshot_fx_baseline("policy")
    checkpoint = snapshot["fas_reconciliation_check"]
    assert c.module._fas_reconciliation_contract().timestamp(checkpoint["reconciliation_at_utc"]) == datetime(2026, 9, 5, tzinfo=timezone.utc)
    assert checkpoint["report_market_years"] == list(range(2020, 2027))
    # Expanded candidate retention is independent of the immutable receipt chain.
    c.worker_candidate[0].rename(c.worker_candidate[0].with_name("archived-fixture-candidate"))
    assert c.module._load_fas_reconciliation(c.policy, "f" * 64, c.files) == checkpoint
    c.clock[0] = datetime(2026, 9, 12, tzinfo=timezone.utc)
    adapter = c.adapter()
    actual = fas.run_fas_pipeline(runtime_root=tmp_path / "next", baseline_root=c.allocation, adapter=adapter,
        git_head=c.policy["approved_producer"]["commit"], batch_id="next", incremental=True,
        incremental_transport=True, publish=False, reconciliation_check=checkpoint)
    assert adapter.queries == [2] and actual["collection_metrics"]["queried_years"] == [2025, 2026]
    new_manifest = json.loads((Path(actual["candidate_path"]).parent / "manifest.json").read_text(encoding="utf-8"))
    claim = json.loads(json.dumps(c.delta))
    claim["domain_metadata"]["fetch_scope"] = new_manifest["fetch_scope"]
    c.module._check_fas_reconciliation_claim(c.policy, "f" * 64, claim)
    assert {key: c.paths[key].read_bytes() for key in c.before} == c.before
    assert not list((tmp_path / "next").rglob("*.parquet"))


def test_stale_source_cannot_advance_monthly_reconciliation_state(tmp_path, monkeypatch):
    c = setup_monthly(tmp_path, monkeypatch, stale_source=True)
    with pytest.raises(ValueError, match="predates"):
        c.module.validate("policy", c.delta["delta_id"])
    evidence = c.module.EVIDENCE_ROOT / c.policy["policy_id"]
    assert json.loads((evidence / (c.delta["delta_id"] + ".validation.json")).read_text())["status"] == "FAIL"
    assert not c.module._fas_reconciliation_pointer(c.policy).exists()
    assert {key: c.paths[key].read_bytes() for key in c.before} == c.before


@pytest.mark.parametrize("tamper", ["checkpoint", "validation", "publication", "received", "future"])
def test_protected_check_loader_rejects_corrupt_or_forged_evidence(tmp_path, monkeypatch, tamper):
    c = setup_monthly(tmp_path, monkeypatch)
    complete(c)
    m = c.module
    suffix = {"checkpoint": ".fas-reconciliation.json", "validation": ".validation.json",
              "publication": ".publication.json", "received": ".received.json", "future": ".fas-reconciliation.json"}[tamper]
    path = m._derived(m.EVIDENCE_ROOT, c.policy, c.delta["delta_id"], suffix)
    value = json.loads(path.read_text(encoding="utf-8"))
    if tamper == "future": value["checked_at_utc"] = "2026-10-01T00:00:00Z"
    else: value["status"] = "UNTRUSTED"
    path.chmod(0o600)
    path.write_text(json.dumps(value), encoding="utf-8")
    if tamper == "future":
        pointer_path = m._fas_reconciliation_pointer(c.policy)
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer["check_sha256"] = m.sha256_file(path)
        pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    with pytest.raises(ValueError): m._load_fas_reconciliation(c.policy, "f" * 64, c.files)
    assert {key: c.paths[key].read_bytes() for key in c.before} == c.before


def test_verified_check_for_changed_baseline_is_not_reused(tmp_path, monkeypatch):
    c = setup_monthly(tmp_path, monkeypatch)
    complete(c)
    changed = dict(c.files)
    changed[c.module.FAS_STABLE] = {"sha256": "0" * 64, "size_bytes": 1}
    assert c.module._load_fas_reconciliation(c.policy, "f" * 64, changed) is None


def test_unprivileged_watermark_claim_cannot_skip_without_protected_receipts(tmp_path, monkeypatch):
    c = setup_monthly(tmp_path, monkeypatch)
    claim = json.loads(json.dumps(c.delta))
    claim["domain_metadata"]["fetch_scope"]["mode"] = "incremental"
    with pytest.raises(ValueError, match="lacks current protected evidence"):
        c.module._check_fas_reconciliation_claim(c.policy, "f" * 64, claim)


def test_reconciliation_pointer_fsync_failure_preserves_business_and_keeps_failed_evidence(tmp_path, monkeypatch):
    c = setup_monthly(tmp_path, monkeypatch)
    m = c.module
    real_write = m._atomic_json
    def fail_after_write(path, value):
        real_write(path, value)
        raise OSError("fixture pointer fsync failure")
    monkeypatch.setattr(m, "_atomic_json", fail_after_write)
    with pytest.raises(OSError, match="pointer fsync"):
        complete(c)
    assert not m._fas_reconciliation_pointer(c.policy).exists()
    assert m._derived(m.EVIDENCE_ROOT, c.policy, c.delta["delta_id"], ".fas-reconciliation.json").exists()
    assert json.loads(m._derived(m.EVIDENCE_ROOT, c.policy, c.delta["delta_id"], ".publication.json").read_text())["status"] == "NO_CHANGE"
    assert {key: c.paths[key].read_bytes() for key in c.before} == c.before


def test_existing_broken_pointer_is_not_silently_overwritten_by_new_check(tmp_path, monkeypatch):
    c = setup_monthly(tmp_path, monkeypatch)
    pointer = c.module._fas_reconciliation_pointer(c.policy)
    pointer.write_bytes(b"broken-pointer-fixture")
    with pytest.raises(ValueError): complete(c)
    assert pointer.read_bytes() == b"broken-pointer-fixture"
    assert {key: c.paths[key].read_bytes() for key in c.before} == c.before


def test_protected_host_reconciliation_contract_imports_with_standard_library_only():
    path = Path(__file__).resolve().parents[1] / "09_deploy/production_data_delivery/activate_production_data_delta.py"
    code = """import importlib.util,sys
spec=importlib.util.spec_from_file_location('host_contract',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
contract=module._fas_reconciliation_contract()
contract.require_complete_coverage(list(range(2020,2027)),[2024,2026])
assert 'pandas' not in sys.modules and 'pyarrow' not in sys.modules
print('STDLIB_ONLY_PASS')
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-I", "-S", "-B", "-c", code, str(path)],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "STDLIB_ONLY_PASS"
