from __future__ import annotations

from contextlib import nullcontext
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pandas as pd
import pytest
from jsonschema import Draft202012Validator, ValidationError


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "09_deploy/production_data_delivery/activate_production_data_delta.py"
COMMIT = "a" * 40
TREE = "b" * 40
IMAGE = "sha256:" + "c" * 64
ORIGIN = "https://github.com/903798404-hub/commodity-research-system.git"


def load_module():
    spec = importlib.util.spec_from_file_location("production_data_delta_activation_under_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def identity(seed: str) -> dict[str, object]:
    return {"sha256": hashlib.sha256(seed.encode()).hexdigest(), "size_bytes": len(seed) + 1}


def path_identity(path: Path) -> dict[str, object]:
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size}


def crop_delta(module) -> dict[str, object]:
    contract = module.contract_for_domain("soybean_crop_progress")
    baseline = {path: identity(path) for path in contract["baseline"]}
    payloads = {
        name: {**identity(name), "row_count": 1, "parquet_schema_sha256": "d" * 64}
        for name in contract["payloads"]
    }
    return {
        "schema_version": "production-data-delta/1", "status": "CANDIDATE",
        "delta_id": "crop-delta-001", "generated_at_utc": "2026-09-08T00:00:00Z",
        "domain": "soybean_crop_progress",
        "producer": {"commit": COMMIT, "tree": TREE, "origin": ORIGIN},
        "run": {"run_id": "run-001", "git_head": COMMIT, "business_status": "updated", "published": False},
        "baseline": {"files": baseline},
        "domain_metadata": {
            "current_year": 2026, "retrieved_at_utc": "2026-09-08T00:00:00Z",
            "raw_snapshot": "01_data/raw/soybean_crop_progress/nass_soybeans_crop_weekly_2026_20260908T100835123456Z.json",
            "duplicate_counts": {"PROGRESS": 0, "CONDITION": 0},
            "source_manifest_sha256": "e" * 64,
        },
        "payloads": payloads,
    }


def materialize_crop_candidate(module, root: Path) -> dict[str, object]:
    delta = crop_delta(module)
    root.mkdir(parents=True)
    payloads = {}
    for name in module.contract_for_domain("soybean_crop_progress")["payloads"]:
        path = root / name
        path.write_bytes(name.encode("utf-8"))
        payloads[name] = {**path_identity(path), "row_count": 1, "parquet_schema_sha256": "d" * 64}
    delta["payloads"] = payloads
    (root / module.MANIFEST_NAME).write_text(json.dumps(delta), encoding="utf-8")
    return delta


def fas_delta(module) -> dict[str, object]:
    contract = module.contract_for_domain("soybean_export_sales")
    return {
        "schema_version": "production-data-delta/1", "status": "CANDIDATE",
        "delta_id": "fas-delta-001", "generated_at_utc": "2026-09-08T00:00:00Z",
        "domain": "soybean_export_sales",
        "producer": {"commit": COMMIT, "tree": TREE, "origin": ORIGIN},
        "run": {"run_id": "fas-run-001", "git_head": COMMIT, "business_status": "updated", "published": False},
        "baseline": {"files": {path: identity(path) for path in contract["baseline"]}},
        "domain_metadata": {
            "batch_id": "fas-batch-001", "source": "usda_fas_esr", "source_latest_week": "2026-09-03",
            "source_release_time_raw": "2026-09-03 12:00 ET", "source_release_timezone": "America/New_York",
            "fetch_scope": {"report_market_years": [2025, 2026]}, "raw_snapshot_sha256": "e" * 64,
            "raw_manifest_sha256": "f" * 64, "quality_status": "passed",
        },
        "payloads": {"soybean_export_sales_weekly.parquet": {
            **identity("soybean_export_sales_weekly.parquet"), "row_count": 1, "parquet_schema_sha256": "d" * 64,
        }},
    }


def crop_semantic() -> dict[str, object]:
    return {
        "business_changed": True,
        "business_changes": {
            "progress": {"added": 0, "corrected": 0, "deleted": 0},
            "condition": {"added": 0, "corrected": 0, "deleted": 0},
        },
        "old_max_week": {"progress": None, "condition": None},
        "new_max_week": {"progress": None, "condition": None},
        "progress_old_rows": 0, "condition_old_rows": 0,
    }


def test_delta_contract_is_closed_and_rejects_preview_origin_path_escape_and_baseline_drift_shape():
    module = load_module()
    document = crop_delta(module)
    assert module.validate_delta_document(document) == document
    schema = json.loads((ROOT / "09_deploy/production_data_delivery/delta_contract.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    validator.validate(document)

    second_precision = json.loads(json.dumps(document))
    second_precision["domain_metadata"]["raw_snapshot"] = (
        "01_data/raw/soybean_crop_progress/"
        "nass_soybeans_crop_weekly_2026_20260908T100835Z.json")
    assert module.validate_delta_document(second_precision) == second_precision
    validator.validate(second_precision)

    invalid_precision = json.loads(json.dumps(document))
    invalid_precision["domain_metadata"]["raw_snapshot"] = (
        "01_data/raw/soybean_crop_progress/"
        "nass_soybeans_crop_weekly_2026_20260908T100835123Z.json")
    with pytest.raises(module.DeltaError, match="crop raw snapshot identity"):
        module.validate_delta_document(invalid_precision)
    with pytest.raises(ValidationError):
        validator.validate(invalid_precision)

    wrong_year = json.loads(json.dumps(document))
    wrong_year["domain_metadata"]["raw_snapshot"] = (
        "01_data/raw/soybean_crop_progress/"
        "nass_soybeans_crop_weekly_2025_20250908T100835123456Z.json")
    with pytest.raises(module.DeltaError, match="crop raw snapshot identity"):
        module.validate_delta_document(wrong_year)

    invalid_calendar = json.loads(json.dumps(document))
    invalid_calendar["domain_metadata"]["raw_snapshot"] = (
        "01_data/raw/soybean_crop_progress/"
        "nass_soybeans_crop_weekly_2026_20261399T999999123456Z.json")
    with pytest.raises(module.DeltaError, match="crop raw snapshot identity"):
        module.validate_delta_document(invalid_calendar)

    preview = json.loads(json.dumps(document))
    preview["producer"]["origin"] = "https://example.invalid/preview.git"
    with pytest.raises(module.DeltaError):
        module.validate_delta_document(preview)

    escaped = json.loads(json.dumps(document))
    escaped["domain_metadata"]["raw_snapshot"] = "../01_data/raw/escape.json"
    with pytest.raises(module.DeltaError):
        module.validate_delta_document(escaped)

    legacy_nested = json.loads(json.dumps(document))
    legacy_nested["domain_metadata"]["raw_snapshot"] = "01_data/raw/soybean_crop_progress/2026/raw.json"
    with pytest.raises(module.DeltaError):
        module.validate_delta_document(legacy_nested)

    partial_pair = json.loads(json.dumps(document))
    partial_pair["baseline"]["files"][module.CROP_PROGRESS] = None
    with pytest.raises(module.DeltaError):
        module.validate_delta_document(partial_pair)

    extra_payload = json.loads(json.dumps(document))
    extra_payload["payloads"]["preview.parquet"] = extra_payload["payloads"]["soybeans_crop_progress_weekly.parquet"]
    with pytest.raises(module.DeltaError):
        module.validate_delta_document(extra_payload)


def test_worker_is_exact_image_networkless_readonly_and_never_runs_provider_or_host_shell(monkeypatch, tmp_path: Path):
    module = load_module()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    policy = {
        "domain": "soybean_crop_progress",
        "allocation_root": str(tmp_path / "allocation"),
        "validation_image": {"image_id": IMAGE, "commit": COMMIT, "tree": TREE},
        "approved_producer": {"commit": COMMIT, "tree": TREE, "origin": ORIGIN},
    }
    calls: list[list[str]] = []
    monkeypatch.setattr(module, "_inspect_image", lambda image: dict(image))
    monkeypatch.setattr(module, "_git_identity", lambda _root: policy["approved_producer"])

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        if command[:3] == ["docker", "run", "--rm"]:
            body = {"schema_version": "production-data-delta-worker/1", "status": "PASS", "domain": "soybean_crop_progress", "observations": {}}
            return subprocess.CompletedProcess(command, 0, module.canonical_json_bytes(body), b"")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.subprocess, "check_output", lambda command, **_kwargs: b"")
    assert module._worker(policy, candidate)["status"] == "PASS"
    worker = calls[0]
    assert "--network" in worker and worker[worker.index("--network") + 1] == "none"
    assert "--read-only" in worker and "--pull" in worker and worker[worker.index("--pull") + 1] == "never"
    assert "--user" in worker and worker[worker.index("--user") + 1] == "65532:65532"
    assert "--mount" in worker
    rendered = " ".join(worker)
    assert "--privileged" not in rendered and "docker.sock" not in rendered
    assert "akshare" not in rendered.lower() and "nass" not in rendered.lower() and "fas" not in rendered.lower()
    assert calls[-1][:4] == ["docker", "rm", "-f", worker[worker.index("--name") + 1]]


def test_publish_rejects_baseline_drift_before_any_exchange(monkeypatch, tmp_path: Path):
    module = load_module()
    policy = {"policy_id": "policy-001", "domain": "soybean_crop_progress", "allocation_root": str(tmp_path / "allocation")}
    delta = crop_delta(module)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / module.MANIFEST_NAME).write_text(json.dumps(delta), encoding="utf-8")
    monkeypatch.setattr(module, "_load_policy", lambda _path: (policy, "f" * 64))
    monkeypatch.setattr(module, "_protected", lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(module, "_protected_tree", lambda path: Path(path))
    monkeypatch.setattr(module, "_under", lambda path, _parent, **_kwargs: Path(path))
    monkeypatch.setattr(module, "_derived", lambda _root, _policy, _delta_id, suffix="": candidate if not suffix else tmp_path / ("evidence" + suffix))
    monkeypatch.setattr(module, "_candidate_files", lambda *_args: {})
    monkeypatch.setattr(module, "_load_pass_report", lambda *_args: {
        "candidate_files": {}, "semantic": {"observations": crop_semantic()},
    })
    monkeypatch.setattr(module, "_check_baseline", lambda *_args: (_ for _ in ()).throw(module.DeltaError("production baseline drift detected")))
    monkeypatch.setattr(module, "_lock", lambda _allocation: nullcontext())
    monkeypatch.setattr(module, "_renameat2", lambda *_args: pytest.fail("exchange must not occur after drift"))
    with pytest.raises(module.DeltaError, match="baseline drift"):
        module.publish(tmp_path / "policy.json", delta["delta_id"], tmp_path / "report.json", "0" * 64)


def test_rollback_refuses_wrong_or_nonpublished_receipt_before_data_mutation(monkeypatch, tmp_path: Path):
    module = load_module()
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"schema_version": "production-data-delta-publication/1", "status": "NO_CHANGE"}), encoding="utf-8")
    monkeypatch.setattr(module, "_load_policy", lambda _path: ({"policy_id": "policy-001", "domain": "soybean_crop_progress"}, "f" * 64))
    monkeypatch.setattr(module, "_protected", lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(module, "_under", lambda path, _parent, **_kwargs: Path(path))
    with pytest.raises(module.DeltaError):
        module.rollback(tmp_path / "policy.json", receipt, "0" * 64)


def test_worker_crop_observations_make_real_pandas_timestamps_json_safe_and_reject_unknown_values():
    module = load_module()
    value = {
        "latest_week": pd.Timestamp("2026-09-03T00:00:00Z"),
        "nested": [pd.Timestamp("2026-09-04"), pd.NaT],
    }
    safe = module._safe_worker_value(value, pd)
    assert safe == {
        "latest_week": "2026-09-03T00:00:00+00:00",
        "nested": ["2026-09-04T00:00:00", None],
    }
    assert json.loads(module.canonical_json_bytes(safe)) == safe
    with pytest.raises(module.DeltaError):
        module._safe_worker_value({"unsupported": object()}, pd)


def test_publish_status_write_failure_exchanges_back_old_domain_and_restores_status(monkeypatch, tmp_path: Path):
    module = load_module()
    allocation = tmp_path / "allocation"; formal = allocation / module.DOMAIN_CONTRACTS["soybean_crop_progress"]["domain_dir"]
    formal.mkdir(parents=True); (formal / "old.txt").write_text("old", encoding="utf-8")
    status = allocation / module.CROP_STATUS; status.parent.mkdir(parents=True); status.write_text('{"old":true}', encoding="utf-8")
    candidate = tmp_path / "candidate"; candidate.mkdir()
    payloads = {}
    for name in module.DOMAIN_CONTRACTS["soybean_crop_progress"]["payloads"]:
        path = candidate / name; path.write_bytes(name.encode()); payloads[name] = {**identity(name), "row_count": 1, "parquet_schema_sha256": "d" * 64}
    delta = crop_delta(module); delta["payloads"] = payloads
    (candidate / module.MANIFEST_NAME).write_text("{}", encoding="utf-8")
    report = tmp_path / "report.json"; report.write_text("{}", encoding="utf-8")
    policy = {"policy_id": "policy-001", "domain": "soybean_crop_progress", "allocation_root": str(allocation), "approved_producer": delta["producer"]}
    monkeypatch.setattr(module, "_load_policy", lambda _path: (policy, "f" * 64))
    monkeypatch.setattr(module, "_protected", lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(module, "_protected_tree", lambda path: Path(path))
    monkeypatch.setattr(module, "_under", lambda path, _parent, **_kwargs: Path(path))
    monkeypatch.setattr(module, "_derived", lambda root, _policy, _delta_id, suffix="": candidate if root == module.CANDIDATE_ROOT else tmp_path / ("backup" if root == module.BACKUP_ROOT else "evidence" + suffix))
    monkeypatch.setattr(module, "read_json", lambda _path: delta)
    monkeypatch.setattr(module, "_candidate_files", lambda *_args: {})
    monkeypatch.setattr(module, "_load_pass_report", lambda *_args: {
        "candidate_files": {}, "semantic": {"observations": crop_semantic()},
    })
    monkeypatch.setattr(module, "_check_baseline", lambda *_args: None)
    monkeypatch.setattr(module, "_policy_unchanged", lambda *_args: None)
    monkeypatch.setattr(module, "_git_identity", lambda _root: delta["producer"])
    def exchange(left, right, _flag):
        swap = left.with_name(left.name + ".swap")
        left.rename(swap); right.rename(left); swap.rename(right)
    monkeypatch.setattr(module, "_renameat2", exchange)
    monkeypatch.setattr(module, "_lock", lambda _allocation: nullcontext())
    monkeypatch.setattr(module.os, "chown", lambda *_args: None, raising=False)
    monkeypatch.setattr(module.os, "fsync", lambda _fd: None)
    monkeypatch.setattr(module, "_atomic_json", lambda *_args: (_ for _ in ()).throw(OSError("status fsync")))
    with pytest.raises(OSError, match="status fsync"):
        module.publish(tmp_path / "policy", delta["delta_id"], report, module.sha256_file(report))
    assert (formal / "old.txt").read_text(encoding="utf-8") == "old"
    assert status.read_text(encoding="utf-8") == '{"old":true}'
    assert not (tmp_path / "evidence.publication.json").exists()
    assert not (tmp_path / "evidence.broken.json").exists()


def test_validate_rejects_orphan_candidate_without_a_matching_receive_receipt(monkeypatch, tmp_path: Path):
    module = load_module()
    policy = {
        "policy_id": "policy-001", "domain": "soybean_crop_progress",
        "allocation_root": str(tmp_path / "allocation"),
        "approved_producer": {"commit": COMMIT, "tree": TREE, "origin": ORIGIN},
    }
    candidate_root = tmp_path / "candidates"
    evidence_root = tmp_path / "evidence"
    delta_id = "crop-delta-001"
    candidate = candidate_root / policy["policy_id"] / delta_id
    delta = materialize_crop_candidate(module, candidate)
    receipt = evidence_root / policy["policy_id"] / f"{delta_id}.received.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(module, "CANDIDATE_ROOT", candidate_root)
    monkeypatch.setattr(module, "EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(module, "_load_policy", lambda _path: (policy, "f" * 64))
    monkeypatch.setattr(module, "_protected", lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(module, "_protected_tree", lambda path: Path(path))
    monkeypatch.setattr(module, "_check_baseline", lambda *_args: None)
    monkeypatch.setattr(module, "_worker", lambda *_args: pytest.fail("orphan candidate must not reach worker"))

    with pytest.raises(module.DeltaError, match="receive receipt"):
        module.validate(tmp_path / "policy.json", delta["delta_id"])


def test_publish_rechecks_formal_tree_before_exchange_when_concurrent_drift_occurs(monkeypatch, tmp_path: Path):
    module = load_module()
    allocation = tmp_path / "allocation"
    formal = allocation / module.DOMAIN_CONTRACTS["soybean_crop_progress"]["domain_dir"]
    formal.mkdir(parents=True)
    watched = formal / "concurrent.txt"
    watched.write_text("before", encoding="utf-8")
    candidate = tmp_path / "candidate"
    delta = materialize_crop_candidate(module, candidate)
    report = tmp_path / "evidence" / "report.json"
    report.parent.mkdir()
    report.write_text("{}", encoding="utf-8")
    policy = {
        "policy_id": "policy-001", "domain": delta["domain"], "allocation_root": str(allocation),
        "approved_producer": delta["producer"],
    }
    monkeypatch.setattr(module, "ALLOCATION_ROOT", tmp_path)
    monkeypatch.setattr(module, "EVIDENCE_ROOT", report.parent)
    monkeypatch.setattr(module, "_load_policy", lambda _path: (policy, "f" * 64))
    monkeypatch.setattr(module, "_protected", lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(module, "_protected_tree", lambda path: Path(path))
    monkeypatch.setattr(module, "_derived", lambda root, _policy, _delta_id, suffix="": candidate if root == module.CANDIDATE_ROOT else tmp_path / ("backup" if root == module.BACKUP_ROOT else "evidence" + suffix))
    monkeypatch.setattr(module, "_candidate_files", lambda *_args: module._file_set(candidate))
    monkeypatch.setattr(module, "_load_pass_report", lambda *_args: {
        "candidate_files": module._file_set(candidate), "semantic": {"observations": {"business_changed": True}},
    })
    monkeypatch.setattr(module, "_check_baseline", lambda *_args: None)
    monkeypatch.setattr(module, "_policy_unchanged", lambda *_args: None)
    monkeypatch.setattr(module, "_git_identity", lambda _root: delta["producer"])
    monkeypatch.setattr(module, "_lock", lambda _allocation: nullcontext())
    monkeypatch.setattr(module.os, "fsync", lambda _fd: None)

    def mutate_formal(*_args):
        watched.write_text("concurrent-change", encoding="utf-8")
        return {"schema_version": 1}

    monkeypatch.setattr(module, "_status_document", mutate_formal)
    monkeypatch.setattr(module, "_renameat2", lambda *_args: pytest.fail("exchange must follow the final drift check"))
    with pytest.raises(module.DeltaError, match="production domain changed while staging publication"):
        module.publish(tmp_path / "policy.json", delta["delta_id"], report, module.sha256_file(report))
    assert watched.read_text(encoding="utf-8") == "concurrent-change"


def test_fas_status_and_manifest_preserve_existing_uppercase_consumer_hash_format():
    module = load_module()
    delta = fas_delta(module)
    assert module.validate_delta_document(delta) == delta
    identity_value = {**identity("stable"), "row_count": 1, "parquet_schema_sha256": "d" * 64}
    manifest = module._fas_manifest(delta, identity_value, "2026-09-08T00:00:00+00:00")
    status = module._status_document(
        "soybean_export_sales", delta, {module.FAS_STABLE: identity_value},
        "2026-09-08T00:00:00+00:00", False, {"business_changed": True},
    )
    assert manifest["sha256"] == identity_value["sha256"].upper()
    assert manifest["parquet_schema_sha256"] == identity_value["parquet_schema_sha256"].upper()
    assert manifest["raw_snapshot_sha256"] == delta["domain_metadata"]["raw_snapshot_sha256"].upper()
    assert status["last_success"]["stable_sha256"] == identity_value["sha256"].upper()


def test_rollback_rejects_backup_tamper_before_exchange(monkeypatch, tmp_path: Path):
    module = load_module()
    policy = {
        "policy_id": "policy-001", "domain": "soybean_crop_progress",
        "allocation_root": str(tmp_path / "allocation"),
        "approved_producer": {"commit": COMMIT, "tree": TREE, "origin": ORIGIN},
    }
    delta_id = "crop-delta-001"
    backup = tmp_path / "backups" / policy["policy_id"] / delta_id
    backup.mkdir(parents=True)
    tampered = backup / "status.json"
    tampered.write_text("tampered", encoding="utf-8")
    expected_backup = {"status.json": {"sha256": "0" * 64, "size_bytes": 1}}
    receipt = {
        "schema_version": "production-data-delta-publication/1", "status": "PUBLISHED",
        "published_at_utc": "2026-09-08T00:00:00Z", "policy_id": policy["policy_id"],
        "policy_sha256": "f" * 64, "delta_id": delta_id, "domain": policy["domain"],
        "validation_report_sha256": "e" * 64,
        "formal_files": {
            path: identity(path)
            for path in (*module.DOMAIN_CONTRACTS[policy["domain"]]["payloads"].values(), module.CROP_STATUS)
        },
        "backup_path": str(backup), "backup_files": expected_backup,
        "displaced_path": str(tmp_path / "displaced"), "previous_domain_present": True,
    }
    evidence = tmp_path / "evidence"
    receipt_path = evidence / policy["policy_id"] / f"{delta_id}.publication.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(module, "BACKUP_ROOT", tmp_path / "backups")
    monkeypatch.setattr(module, "EVIDENCE_ROOT", evidence)
    monkeypatch.setattr(module, "_load_policy", lambda _path: (policy, "f" * 64))
    monkeypatch.setattr(module, "_protected", lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(module, "_protected_tree", lambda path: Path(path))
    monkeypatch.setattr(module, "_renameat2", lambda *_args: pytest.fail("tampered backup must not exchange formal data"))

    with pytest.raises(module.DeltaError, match="rollback backup bytes differ"):
        module.rollback(tmp_path / "policy.json", receipt_path, module.sha256_file(receipt_path))
