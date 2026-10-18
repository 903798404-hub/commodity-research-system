"""Exercise actual offline worker logic with synthetic retained source fixtures."""
from datetime import datetime, timezone
from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

import pandas as pd
import pytest

from agri_research_agent.soybean_exports import fas
from agri_research_agent.shared import table_delivery
from test_production_data_delta_activation import load_module, path_identity, ORIGIN
from test_scheduled_server import load as load_scheduled


@pytest.mark.parametrize("fault", [None, "materializer", "baseline-cas"])
def test_changed_sparse_fas_real_file_lifecycle_and_exact_history_rollback(tmp_path, monkeypatch, fault):
    """Run host receipts and byte copies; mock only Docker/Linux authority.

    This portable fixture is not evidence of atomic Linux renameat2 or UID
    isolation. Those boundaries must also run in the admitted-image replay.
    """
    module, delta, allocation, upload, commit, tree = prepare(tmp_path, monkeypatch, True)
    before = {str(p.relative_to(allocation)): p.read_bytes() for p in allocation.rglob("*") if p.is_file()}
    policy = {"policy_id": "table-lifecycle-001", "domain": delta["domain"],
        "allocation_root": str(allocation), "approved_producer": delta["producer"],
        "validation_image": {"image_id": "sha256:" + "c" * 64, "commit": commit, "tree": tree}}
    for name, dirname in (("UPLOAD_ROOT", "uploads"), ("INCOMING_ROOT", "incoming"),
                          ("CANDIDATE_ROOT", "candidates"), ("EVIDENCE_ROOT", "evidence"), ("BACKUP_ROOT", "backups")):
        root = tmp_path / dirname
        (root / policy["policy_id"]).mkdir(parents=True)
        monkeypatch.setattr(module, name, root)
    monkeypatch.setattr(module, "UPLOAD_ROOT", upload.parent)
    monkeypatch.setattr(module, "ALLOCATION_ROOT", tmp_path)
    monkeypatch.setattr(module, "_load_policy", lambda *_: (policy, "f" * 64))
    monkeypatch.setattr(module, "_protected", lambda path, **_: Path(path))
    monkeypatch.setattr(module, "_protected_tree", lambda path: Path(path))
    monkeypatch.setattr(module, "_policy_unchanged", lambda *_: None)
    monkeypatch.setattr(module, "_lock", lambda *_: nullcontext())
    monkeypatch.setattr(module, "_git_identity", lambda *_: delta["producer"])
    monkeypatch.setattr(module.os, "chown", lambda *_: None, raising=False)
    monkeypatch.setattr(module, "_seal_materialized", lambda path, files: None)
    app = tmp_path / "app"
    current_candidate = [upload]
    def mapped(value):
        raw = str(value).replace("\\", "/")
        for prefix, destination in {"/candidate": current_candidate[0], "/allocation": allocation, "/app": app}.items():
            if raw == prefix or raw.startswith(prefix + "/"):
                return destination / raw[len(prefix):].lstrip("/")
        return Path(value)
    monkeypatch.setattr(module, "Path", mapped)
    materialization_calls = []
    def worker(_policy, candidate, *, materialization=None):
        current_candidate[0] = candidate
        materialization_calls.append(materialization)
        if materialization is not None and fault == "materializer":
            (materialization / "partial.parquet").write_bytes(b"partial evidence")
            raise module.DeltaError("injected materializer failure")
        return module.worker_validate(delta["domain"], commit, tree, materialize_to=materialization)
    monkeypatch.setattr(module, "_worker", worker)
    def exchange(source, target, flag):
        assert flag == 2  # Portable fixture only; production uses renameat2.
        temporary = source.with_name(source.name + "-exchange")
        os.replace(source, temporary)
        os.replace(target, source)
        os.replace(temporary, target)
    monkeypatch.setattr(module, "_renameat2", exchange)
    digest = module.sha256_file(upload / module.MANIFEST_NAME)
    staged = module.stage_upload("policy", upload, digest)
    received = module.receive("policy", staged["incoming_path"], digest)
    validation = module.validate("policy", delta["delta_id"])
    assert validation["status"] == "PASS"
    if fault == "baseline-cas":
        path = allocation / module.FAS_STATUS
        path.chmod(0o600)
        path.write_bytes(before[str(Path(module.FAS_STATUS))] + b" ")
        with pytest.raises(module.DeltaError, match="baseline"):
            module.publish("policy", delta["delta_id"], validation["report_path"], validation["report_sha256"])
        assert len(materialization_calls) == 1
        assert not list(module.BACKUP_ROOT.rglob("*.parquet"))
        return
    if fault:
        with pytest.raises(module.DeltaError, match="materializer failure"):
            module.publish("policy", delta["delta_id"], validation["report_path"], validation["report_sha256"])
        assert {str(p.relative_to(allocation)): p.read_bytes() for p in allocation.rglob("*") if p.is_file()} == before
        assert list(module.CANDIDATE_ROOT.rglob("partial.parquet"))
        assert not list(module.EVIDENCE_ROOT.rglob("*.publication.json"))
        return
    publication = module.publish("policy", delta["delta_id"], validation["report_path"], validation["report_sha256"])
    assert publication["status"] == "PUBLISHED" and len(materialization_calls) == 2
    formal = pd.read_parquet(allocation / module.FAS_STABLE)
    old = pd.read_parquet(Path(publication["backup_path"]) / "domain" / Path(module.FAS_STABLE).name)
    assert len(formal) == 3 and len(old) == 2
    pd.testing.assert_frame_equal(formal.loc[formal.report_market_year_end.eq(2024)].reset_index(drop=True),
                                  old.loc[old.report_market_year_end.eq(2024)].reset_index(drop=True))
    fas.validate_fas_stable(formal)
    assert module._file_set(Path(received["candidate_path"])) == module._file_set(upload)
    reverted = module.rollback("policy", publication["receipt_path"], publication["receipt_sha256"])
    assert reverted["status"] == "ROLLED_BACK"
    assert all((allocation / name).read_bytes() == raw for name, raw in before.items())
    # Both exchanged versions are deliberately retained as recovery evidence.
    assert Path(publication["displaced_path"]).exists() and Path(reverted["displaced_path"]).exists()
    assert Path(publication["backup_path"]).exists()


def fas_fixture():
    path = Path(__file__).parent / "soybean_exports/test_fas_data_layer.py"
    spec = importlib.util.spec_from_file_location("table_worker_fas_fixture", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    return fixture


def prepare(tmp_path, monkeypatch, changed):
    module, fixture = load_module(), fas_fixture()
    allocation, output, candidate, app = (tmp_path / name for name in ("allocation", "output", "candidate", "app"))
    monkeypatch.setattr(fas, "utc_now", lambda: datetime(2026, 8, 7, tzinfo=timezone.utc))
    rows = [fixture.fas_row(report_year=2024, week="2024-07-25T00:00:00"), fixture.fas_row()]
    class Adapter(fixture.StaticAdapter):
        def fetch_export_sales(self, years=None, *, recent_years=7, retained_years=()):
            selected = [row for row in self.rows if row["_report_market_year_end"] >= 2026 - recent_years + 1
                        or row["_report_market_year_end"] in retained_years]
            return fixture.StaticAdapter(selected).fetch_export_sales(years)
    fas.run_fas_pipeline(runtime_root=allocation, adapter=Adapter(rows), git_head=fixture.GIT_HEAD,
                         batch_id="fixture-seed", incremental=True)
    if changed:
        rows = rows + [fixture.fas_row(week="2026-08-06T00:00:00")]
    audit = fas.run_fas_pipeline(runtime_root=output, baseline_root=allocation, adapter=Adapter(rows),
        git_head=fixture.GIT_HEAD, batch_id="fixture-candidate", incremental=True,
        incremental_transport=True, publish=False)
    source = Path(audit["candidate_path"])
    manifest = json.loads((source.parent / "manifest.json").read_text(encoding="utf-8"))
    candidate.mkdir()
    name = "soybean_export_sales_weekly.parquet"
    shutil.copyfile(source, candidate / name)
    contract = module.contract_for_domain("soybean_export_sales")
    metadata = {key: manifest[key] for key in contract["metadata"]}
    for key in ("raw_snapshot_sha256", "raw_manifest_sha256"):
        metadata[key] = metadata[key].lower()
    tree = "a" * 40
    delta = {"schema_version": "production-data-delta/1", "status": "CANDIDATE",
        "delta_id": "table-worker-fixture-001", "generated_at_utc": "2026-08-07T00:00:00Z",
        "domain": "soybean_export_sales", "producer": {"commit": fixture.GIT_HEAD, "tree": tree, "origin": ORIGIN},
        "run": {"run_id": "table-worker-fixture-001", "git_head": fixture.GIT_HEAD,
                "business_status": "updated" if changed else "no_change", "published": False},
        "baseline": {"files": {relative: path_identity(allocation / relative) for relative in contract["baseline"]}},
        "domain_metadata": metadata, "payloads": {name: {**path_identity(candidate / name),
            "row_count": manifest["row_count"], "parquet_schema_sha256": manifest["parquet_schema_sha256"]}}}
    module.validate_delta_document(delta)
    (candidate / module.MANIFEST_NAME).write_text(json.dumps(delta), encoding="utf-8")
    app.mkdir()
    (app / "RELEASE.json").write_text(json.dumps({"git_commit": fixture.GIT_HEAD, "git_tree": tree}), encoding="utf-8")
    mapping = {"/candidate": candidate, "/allocation": allocation, "/app": app}
    def mapped(value):
        raw = str(value).replace("\\", "/")
        for prefix, destination in mapping.items():
            if raw == prefix or raw.startswith(prefix + "/"):
                return destination / raw[len(prefix):].lstrip("/")
        return Path(value)
    monkeypatch.setattr(module, "Path", mapped)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("MARKET_DATA_GIT_HEAD", fixture.GIT_HEAD)
    monkeypatch.setenv("MARKET_DATA_GIT_TREE", tree)
    return module, delta, allocation, candidate, fixture.GIT_HEAD, tree


@pytest.mark.parametrize("changed", [False, True])
def test_actual_worker_reconstructs_then_runs_original_fas_domain_gate(tmp_path, monkeypatch, changed):
    module, delta, allocation, candidate, commit, tree = prepare(tmp_path, monkeypatch, changed)
    before = {str(p.relative_to(allocation)): p.read_bytes() for p in allocation.rglob("*") if p.is_file()}
    if not changed:
        monkeypatch.setattr(table_delivery, "parquet_bytes", lambda *a, **k: pytest.fail("NO_CHANGE encoded full Parquet"))
    report = module.worker_validate("soybean_export_sales", commit, tree)
    assert report["status"] == "PASS" and report["table_transport"] is True
    assert report["observations"]["business_changed"] is changed
    if changed:
        assert report["compatibility_files"]["soybean_export_sales_weekly.parquet"]["row_count"] == 3
    else:
        assert report["compatibility_files"] == {}
    assert {str(p.relative_to(allocation)): p.read_bytes() for p in allocation.rglob("*") if p.is_file()} == before
    assert len(list(candidate.iterdir())) == 2
    assert (candidate / "soybean_export_sales_weekly.parquet").read_bytes().startswith(b"{")


@pytest.mark.parametrize("tamper", ["rows", "schema", "baseline", "provenance"])
def test_actual_worker_rejects_transport_identity_and_source_drift(tmp_path, monkeypatch, tamper):
    module, delta, allocation, candidate, commit, tree = prepare(tmp_path, monkeypatch, True)
    name = "soybean_export_sales_weekly.parquet"
    if tamper == "rows": delta["payloads"][name]["row_count"] += 1
    elif tamper == "schema": delta["payloads"][name]["parquet_schema_sha256"] = "0" * 64
    elif tamper == "baseline": delta["baseline"]["files"][module.FAS_STABLE]["sha256"] = "0" * 64
    else: delta["domain_metadata"]["raw_snapshot_sha256"] = "0" * 64
    (candidate / module.MANIFEST_NAME).write_text(json.dumps(delta), encoding="utf-8")
    with pytest.raises((ValueError, fas.PipelineError)):
        module.worker_validate("soybean_export_sales", commit, tree)


def test_separate_actual_worker_materializes_exact_validated_bytes_for_legacy_reader(tmp_path, monkeypatch):
    module, delta, allocation, candidate, commit, tree = prepare(tmp_path, monkeypatch, True)
    independent = module.worker_validate("soybean_export_sales", commit, tree)
    scratch = tmp_path / "materialization"
    scratch.mkdir()
    actual = module.worker_validate("soybean_export_sales", commit, tree, materialize_to=scratch)
    assert actual == independent
    path = scratch / "soybean_export_sales_weekly.parquet"
    expected = independent["compatibility_files"][path.name]
    assert path_identity(path) == {key: expected[key] for key in ("sha256", "size_bytes")}
    frame = pd.read_parquet(path)
    assert len(frame) == 3
    fas.validate_fas_stable(frame)
    fas.validate_fas_increment(frame, pd.read_parquet(allocation / module.FAS_STABLE), delta["domain_metadata"])
    assert frame.loc[frame.report_market_year_end.eq(2024), "batch_id"].tolist() == ["fixture-seed"]
    assert (candidate / path.name).read_bytes().startswith(b"{")


def test_actual_nochange_worker_refuses_even_empty_materialization_scratch(tmp_path, monkeypatch):
    module, _delta, _allocation, _candidate, commit, tree = prepare(tmp_path, monkeypatch, False)
    scratch = tmp_path / "materialization"
    scratch.mkdir()
    with pytest.raises(ValueError, match="NO_CHANGE"):
        module.worker_validate("soybean_export_sales", commit, tree, materialize_to=scratch)
    assert not list(scratch.iterdir())


def test_host_nochange_returns_before_materializer_backup_or_formal_writes(tmp_path, monkeypatch):
    module, delta, allocation, candidate, commit, tree = prepare(tmp_path, monkeypatch, False)
    semantic = module.worker_validate(delta["domain"], commit, tree)
    before = {str(p.relative_to(allocation)): p.read_bytes() for p in allocation.rglob("*") if p.is_file()}
    policy = {"policy_id": "table-fixture-001", "domain": delta["domain"], "allocation_root": str(allocation),
              "approved_producer": delta["producer"]}
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(module, "ALLOCATION_ROOT", tmp_path)
    monkeypatch.setattr(module, "EVIDENCE_ROOT", evidence)
    derived = module._derived
    monkeypatch.setattr(module, "_derived", lambda root, pol, key, suffix="": candidate
                        if root == module.CANDIDATE_ROOT and not suffix else derived(root, pol, key, suffix))
    monkeypatch.setattr(module, "_load_policy", lambda *_: (policy, "f" * 64))
    monkeypatch.setattr(module, "_load_pass_report", lambda *_: {
        "candidate_files": module._file_set(candidate), "semantic": semantic})
    monkeypatch.setattr(module, "_protected", lambda path, **_: Path(path))
    monkeypatch.setattr(module, "_protected_tree", lambda path: Path(path))
    monkeypatch.setattr(module, "_lock", lambda *_: nullcontext())
    monkeypatch.setattr(module, "_git_identity", lambda *_: delta["producer"])
    monkeypatch.setattr(module, "_policy_unchanged", lambda *_: None)
    monkeypatch.setattr(module, "_copy_tree", lambda *_: pytest.fail("NO_CHANGE copied history"))
    monkeypatch.setattr(module, "_materialize_tables", lambda *_: pytest.fail("NO_CHANGE invoked materializer"))
    result = module.publish(tmp_path / "policy", delta["delta_id"], tmp_path / "report", "f" * 64)
    assert result["status"] == "NO_CHANGE" and result["backup_path"] is None
    assert {str(p.relative_to(allocation)): p.read_bytes() for p in allocation.rglob("*") if p.is_file()} == before
    assert len(list(evidence.rglob("*.json"))) == 1


@pytest.mark.parametrize("tamper", [None, "report", "bytes", "extra"])
def test_host_materializer_requires_independent_report_and_exact_closed_outputs(tmp_path, monkeypatch, tamper):
    module, delta, allocation, candidate, commit, tree = prepare(tmp_path, monkeypatch, True)
    semantic = module.worker_validate(delta["domain"], commit, tree)
    policy = {"policy_id": "table-fixture-001", "domain": delta["domain"],
              "validation_image": {"image_id": "sha256:" + "c" * 64, "commit": commit, "tree": tree}}
    parent, evidence = tmp_path / "candidates", tmp_path / "evidence"
    (parent / policy["policy_id"]).mkdir(parents=True)
    (evidence / policy["policy_id"]).mkdir(parents=True)
    monkeypatch.setattr(module, "CANDIDATE_ROOT", parent)
    monkeypatch.setattr(module, "EVIDENCE_ROOT", evidence)
    monkeypatch.setattr(module, "_protected", lambda path, **_: Path(path))
    monkeypatch.setattr(module.os, "chown", lambda *_: None, raising=False)
    # Windows does not supply Linux root/UID semantics. This unit test checks
    # dispatch/identity gates; actual sealing/mount permissions require Linux.
    sealed = []
    monkeypatch.setattr(module, "_seal_materialized", lambda path, files: sealed.append((path, files)))
    def materialize(_policy, _candidate, *, materialization):
        result = module.worker_validate(delta["domain"], commit, tree, materialize_to=materialization)
        if tamper == "report": result["observations"]["business_changed"] = False
        elif tamper == "bytes":
            path = materialization / "soybean_export_sales_weekly.parquet"
            path.chmod(0o600)
            path.write_bytes(b"not independently validated bytes")
        elif tamper == "extra": (materialization / "unmanaged.txt").write_text("unexpected")
        return result
    monkeypatch.setattr(module, "_worker", materialize)
    if tamper:
        with pytest.raises(module.DeltaError):
            module._materialize_tables(policy, candidate, delta, {"semantic": semantic})
        assert not sealed
        assert not list(evidence.rglob("*.json"))
    else:
        root, identities = module._materialize_tables(policy, candidate, delta, {"semantic": semantic})
        assert identities == semantic["compatibility_files"] and len(sealed) == 1
        assert module._file_set(root) == {name: {key: identity[key] for key in ("sha256", "size_bytes")}
                                        for name, identity in identities.items()}
        assert len(list(evidence.rglob("*.materialization.json"))) == 1


@pytest.mark.parametrize("changed", [False, True])
def test_actual_scheduled_fas_collection_transfers_sparse_tables_without_project_history(tmp_path, monkeypatch, changed):
    module, initial, allocation, _candidate, commit, tree = prepare(tmp_path, monkeypatch, False)
    fixture = fas_fixture()
    rows = [fixture.fas_row(report_year=2024, week="2024-07-25T00:00:00"), fixture.fas_row()]
    if changed: rows += [fixture.fas_row(week="2026-08-06T00:00:00")]
    class Adapter(fixture.StaticAdapter):
        def fetch_export_sales(self, years=None, *, recent_years=7, retained_years=()):
            selected = [row for row in self.rows if row["_report_market_year_end"] >= 2026-recent_years+1
                        or row["_report_market_year_end"] in retained_years]
            return fixture.StaticAdapter(selected).fetch_export_sales(years)
    monkeypatch.setattr(fas, "FasAdapter", lambda **_: Adapter(rows))
    collector = load_scheduled("table_collector_under_test", "09_deploy/scheduled_server/collect.py")
    monkeypatch.setattr(collector, "SOURCE", Path(__file__).resolve().parents[1])
    output = tmp_path / "scheduled-output"
    output.mkdir()
    before = {str(p.relative_to(allocation)): p.read_bytes() for p in allocation.rglob("*") if p.is_file()}
    request = {"job": "soybean_export_sales", "run_id": "table-scheduled-fixture-001",
               "producer": initial["producer"], "baseline_files": initial["baseline"]["files"]}
    result = collector.prepare(request, allocation, output, {"FAS_EXPORT_SALES_API_KEY": "fixture-only"})
    assert result["metrics"]["full_transport_bundle_written"] is False
    assert result["metrics"]["baseline_copied"] is False
    assert not (output / "project/01_data/processed").exists()
    assert not list((output / "project").rglob("*.parquet"))
    package = output / "package"
    transfer = json.loads((package / module.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert transfer["run"]["business_status"] == ("updated" if changed else "no_change")
    assert transfer["baseline"] == initial["baseline"]
    original_path = module.Path
    monkeypatch.setattr(module, "Path", lambda value: package if str(value) == "/candidate" else original_path(value))
    report = module.worker_validate("soybean_export_sales", commit, tree)
    assert report["status"] == "PASS" and report["observations"]["business_changed"] is changed
    assert {str(p.relative_to(allocation)): p.read_bytes() for p in allocation.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("changed", [False, True])
def test_actual_scheduled_crop_collection_and_offline_worker_keep_prior_years_without_copies(tmp_path, monkeypatch, changed):
    from agri_research_agent.pipelines import soybean_crop_weekly_update as crop
    fixture_path = Path(__file__).parent / "soybean_crop_progress/test_soybean_crop_weekly_update.py"
    spec = importlib.util.spec_from_file_location("table_worker_crop_fixture", fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    module = load_module()
    allocation, output, app = (tmp_path / name for name in ("allocation", "output", "app"))
    paths = fixture._prepare_root(allocation)
    fixture._run(allocation, fixture.CurrentYearTransport())
    before = {str(p.relative_to(allocation)): p.read_bytes() for p in allocation.rglob("*") if p.is_file()}
    real_pipeline = crop.run_soybean_crop_weekly_update
    def offline(**kwargs):
        return real_pipeline(**kwargs, now=datetime(2026, 7, 18, 12, tzinfo=timezone.utc),
            transport=fixture.CurrentYearTransport(planted_value=99 if changed else 100),
            retries=0, sleep=lambda _: None)
    monkeypatch.setattr(crop, "run_soybean_crop_weekly_update", offline)
    collector = load_scheduled("table_crop_collector_under_test", "09_deploy/scheduled_server/collect.py")
    monkeypatch.setattr(collector, "SOURCE", Path(__file__).resolve().parents[1])
    output.mkdir()
    commit, tree = fixture.TEST_GIT_HEAD, "a" * 40
    producer = {"commit": commit, "tree": tree, "origin": ORIGIN}
    baseline = {relative: path_identity(allocation / relative)
                for relative in module.DOMAIN_CONTRACTS["soybean_crop_progress"]["baseline"]}
    result = collector.prepare({"job": "soybean_crop_progress", "run_id": "table-crop-fixture-001",
        "producer": producer, "baseline_files": baseline}, allocation, output, {"NASS_API_KEY": fixture.FAKE_SECRET})
    assert not result["metrics"]["baseline_copied"] and not result["metrics"]["full_transport_bundle_written"]
    assert not (output / "project/01_data/processed").exists()
    assert not list((output / "project").rglob("*.parquet"))
    package = output / "package"
    delta = json.loads((package / module.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert delta["run"]["business_status"] == ("updated" if changed else "no_change")
    assert delta["baseline"]["files"] == baseline
    app.mkdir()
    (app / "RELEASE.json").write_text(json.dumps({"git_commit": commit, "git_tree": tree}), encoding="utf-8")
    display = app / "02_configs/soybean_crop_progress_display.yaml"
    display.parent.mkdir()
    display.write_bytes(paths["display_config"].read_bytes())
    def mapped(value):
        raw = str(value).replace("\\", "/")
        for prefix, destination in {"/candidate": package, "/allocation": allocation, "/app": app}.items():
            if raw == prefix or raw.startswith(prefix + "/"):
                return destination / raw[len(prefix):].lstrip("/")
        return Path(value)
    monkeypatch.setattr(module, "Path", mapped)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("MARKET_DATA_GIT_HEAD", commit)
    monkeypatch.setenv("MARKET_DATA_GIT_TREE", tree)
    semantic = module.worker_validate("soybean_crop_progress", commit, tree)
    assert semantic["status"] == "PASS" and semantic["observations"]["business_changed"] is changed
    assert semantic["table_transport"] is True
    if changed:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        assert module.worker_validate("soybean_crop_progress", commit, tree, materialize_to=scratch) == semantic
        assert len(list(scratch.iterdir())) == 2
    else:
        assert semantic["compatibility_files"] == {}
    assert {str(p.relative_to(allocation)): p.read_bytes() for p in allocation.rglob("*") if p.is_file()} == before
