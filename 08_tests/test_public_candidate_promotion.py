from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agri_research_agent.automation import production_data_delta as delivery
from agri_research_agent.automation import historical_reconciliation as history
from agri_research_agent.pipelines import public_data_delivery as packages


ROOT = Path(__file__).resolve().parents[1]
OLD = {"id": "public-current-" + "a" * 24,
       "artifact_sha256": "b" * 64, "manifest_sha256": "c" * 64}
NEW = "public-current-" + "d" * 24
PRODUCER = "e" * 40
TREE = "f" * 40


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    candidate = tmp_path / "runs/run/packages" / NEW
    artifact = candidate / "data/consumer-artifacts/domestic-spread/historical_spread_database.parquet"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"validated spread bytes")
    write(candidate / "manifest.json", {"package_id": NEW})
    manifest_sha = sha(candidate / "manifest.json")
    package = SimpleNamespace(package_id=NEW, directory=candidate,
                              manifest={"delivery_artifacts": {"domestic-spread": {
                                  "sha256": sha(artifact),
                                  "package_path": "consumer-artifacts/domestic-spread/historical_spread_database.parquet"}}})
    monkeypatch.setattr(packages, "validate_production_package", lambda _: package)
    monkeypatch.setattr(delivery, "verify_clean_detached_clone", lambda *args, **kwargs: None)
    inputs = []
    for number in range(6):
        path = tmp_path / f"input-{number}.txt"
        path.write_text(str(number), encoding="utf-8")
        inputs.append(path)
    reconciliation = tmp_path / "reconciliation.json"
    reconciliation.write_text("{}", encoding="utf-8")
    recon_manifest = {"expected_current": OLD,
                      "source_evidence": {"path": str(inputs[0]), "sha256": sha(inputs[0])},
                      "audit_evidence": {"path": str(inputs[1]), "sha256": sha(inputs[1])},
                      "counts": {"daily_close": 630, "non_trading_underlying": 15,
                                 "non_trading_derived": 18}}
    monkeypatch.setattr(history, "load_manifest", lambda _: (
        recon_manifest, {"manifest_sha256": sha(reconciliation)}))
    inventory_path = tmp_path / "inventory.json"
    write(inventory_path, {"manifest_sha256": sha(reconciliation),
                           "current_id": OLD["id"],
                           "current_artifact_sha256": OLD["artifact_sha256"],
                           "current_manifest_sha256": OLD["manifest_sha256"],
                           "counts": recon_manifest["counts"],
                           "inputs": {str(path): sha(path) for path in inputs}})
    validation_path = tmp_path / "validation.json"
    write(validation_path, {"old_current_id": OLD["id"], "new_candidate_id": NEW,
                            "approved_daily_close_rows": 630,
                            "approved_daily_close_numeric_change_count": 0,
                            "approved_derived_trading_numeric_change_count": 0,
                            "exact_derived_deleted": 18,
                            "non_trading_daily_close_rows": 0,
                            "non_trading_derived_rows": 0,
                            "unapproved_business_diff_count": 0,
                            "unapproved_date_diff_count": 0,
                            "other_public_files_identical": True,
                            "incident_20260716_preserved": True,
                            "late_arrival_20260921_preserved": True,
                            "consumer_formal_reads": {"domestic_spread": "PASS"}})
    source = candidate.parent.parent / "source"
    source_files = {}
    for name in delivery.CONTINUATION_FILES:
        path = source / "01_data" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(artifact.read_bytes() if name.endswith(".parquet") else name.encode())
        source_files[name] = sha(path)
    result_path = candidate.parent.parent / "result.json"
    write(result_path, {"status": "CANDIDATE", "published": False,
                        "reconciliation_mode": "HISTORICAL_RECONCILIATION",
                        "candidate": str(candidate), "manifest_sha256": manifest_sha,
                        "reconciliation_manifest_sha256": sha(reconciliation),
                        "producer": {"commit": PRODUCER, "tree": TREE, "origin": delivery.ORIGIN},
                        "expected_current_id": OLD["id"],
                        "expected_current_artifact_sha256": OLD["artifact_sha256"],
                        "expected_current_manifest_sha256": OLD["manifest_sha256"],
                        "actual_start_current_id": OLD["id"],
                        "source_evidence_sha256": sha(inputs[0]),
                        "audit_evidence_sha256": sha(inputs[1]),
                        "source": str(source)})
    evidence_path = tmp_path / "promotion.json"
    write(evidence_path, {"schema_version": "public-current-candidate-promotion/1",
                          "candidate_id": NEW,
                          "candidate_artifact_sha256": sha(artifact),
                          "candidate_manifest_sha256": manifest_sha,
                          "candidate_result_sha256": sha(result_path),
                          "candidate_validation_path": str(validation_path),
                          "candidate_validation_sha256": sha(validation_path),
                          "input_hash_inventory_path": str(inventory_path),
                          "input_hash_inventory_sha256": sha(inventory_path),
                          "source_files": source_files,
                          "producer_commit": PRODUCER, "producer_tree": TREE,
                          "reconciliation_manifest_sha256": sha(reconciliation),
                          "expected_current": OLD})
    config = {"origin": delivery.ORIGIN}
    def validate():
        return delivery._validated_promotion(
            config, candidate=candidate, reconciliation_manifest=reconciliation,
            evidence_path=evidence_path, evidence_sha256=sha(evidence_path),
            expected_current=OLD)
    return validate, candidate, artifact, evidence_path, reconciliation


def test_exact_candidate_promotion_validation_does_not_rebuild(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    validate, candidate, artifact, evidence, _ = fixture(tmp_path, monkeypatch)
    original = (sha(candidate / "manifest.json"), sha(artifact))
    result = validate()
    assert result[0]["candidate_id"] == candidate.name
    assert (sha(candidate / "manifest.json"), sha(artifact)) == original


@pytest.mark.parametrize("target", ["artifact", "manifest", "reconciliation", "inventory"])
def test_promotion_rejects_changed_candidate_or_bound_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    validate, candidate, artifact, evidence, reconciliation = fixture(tmp_path, monkeypatch)
    paths = {"artifact": artifact, "manifest": candidate / "manifest.json",
             "reconciliation": reconciliation,
             "inventory": Path(json.loads(evidence.read_text(encoding="utf-8"))["input_hash_inventory_path"])}
    paths[target].write_bytes(b"tampered")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        validate()


def test_promotion_cli_uses_existing_candidate_without_running_producer(monkeypatch, tmp_path: Path):
    spec = importlib.util.spec_from_file_location(
        "public_candidate_cli_test", ROOT / "04_scripts/automation/run_production_data_delta_windows.py")
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    config = tmp_path / "config.json"; config.write_text("{}", encoding="utf-8")
    candidate = tmp_path / NEW; candidate.mkdir()
    manifest = tmp_path / "reconciliation.json"; manifest.write_text("{}", encoding="utf-8")
    evidence = tmp_path / "promotion.json"; evidence.write_text("{}", encoding="utf-8")
    seen = {}
    fake = SimpleNamespace(validate_config=lambda _: None,
                           verify_clean_detached_clone=lambda *_: None,
                           run_domain=lambda *_a, **_k: pytest.fail("producer must not run"),
                           promote_existing_candidate=lambda _config, **kwargs:
                           seen.update(kwargs) or {"status": "PUBLISHED"})
    monkeypatch.setattr(cli, "_bootstrap", lambda _: None)
    monkeypatch.setattr(cli, "load_module", lambda: fake)
    assert cli.main(["--config", str(config), "--domain", "akshare",
                     "--historical-reconciliation-manifest", str(manifest),
                     "--promote-candidate", str(candidate),
                     "--promotion-evidence", str(evidence),
                     "--promotion-evidence-sha256", "a" * 64,
                     "--expected-current-id", OLD["id"],
                     "--expected-current-artifact-sha256", OLD["artifact_sha256"],
                     "--expected-current-manifest-sha256", OLD["manifest_sha256"]]) == 0
    assert seen["candidate"] == candidate
    assert seen["expected_current"] == OLD
