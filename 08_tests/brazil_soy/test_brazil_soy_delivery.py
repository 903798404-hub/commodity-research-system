from __future__ import annotations

import base64
from copy import deepcopy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agri_research_agent.automation import production_data_delta_brazil as producer
from agri_research_agent.pipelines import brazil_soy as soy
from agri_research_agent.shared.atomic_storage import atomic_write_json

ROOT = Path(__file__).resolve().parents[2]


def report_data():
    raw = b"Official CONAB soybean table: 9.4 percent planted, cutoff 2026-10-02"
    host = producer.delivery._host_contract()
    row = {"season": "2026/2027", "region": "BR", "metric": "PLANTED", "date": "2026-10-02", "value": 9.4,
        "source_url": soy.SOURCE_URL, "source_sha256": host.sha256_bytes(raw), "source_locator": "table 1 national soybean",
        "date_basis": "report_cutoff", "published_at": "2026-10-05", "retrieved_at": "2026-10-07T00:00:00+00:00", "reference": None}
    data = {"schema_version": soy.SCHEMA, "generated_at": row["retrieved_at"], "records": [row], "import_notes": []}
    evidence = {"schema_version": "brazil-soy-source-evidence/1", "sources": [{"kind": "report",
        "sha256": row["source_sha256"], "bytes_base64": base64.b64encode(raw).decode(), "source_url": row["source_url"],
        "retrieved_at": row["retrieved_at"], "published_at": row["published_at"]}]}
    return host, data, evidence


def test_host_sources_revision_history_and_no_change(tmp_path):
    host, data, evidence = report_data()
    candidate = tmp_path / "soy_weekly.json"
    atomic_write_json(candidate, data)
    initial = host._brazil_observations(candidate, None, evidence, [])
    assert initial["added"] == 1 and initial["business_changed"]
    base = tmp_path / "base.json"
    atomic_write_json(base, data)
    assert not host._brazil_observations(candidate, base, {"schema_version": evidence["schema_version"], "sources": []}, [])["business_changed"]
    changed = deepcopy(data)
    changed["records"][0]["value"] = 10
    atomic_write_json(candidate, changed)
    with pytest.raises(host.DeltaError, match="revision"):
        host._brazil_observations(candidate, base, evidence, [])
    result = host._brazil_observations(candidate, base, evidence, ["2026/2027/BR/PLANTED/2026-10-02"])
    assert result["revised"] == 1
    missing = deepcopy(data)
    missing["records"][0]["date"] = "2026-10-01"
    atomic_write_json(candidate, missing)
    with pytest.raises(host.DeltaError, match="deletion"):
        host._brazil_observations(candidate, base, evidence, [])


@pytest.mark.parametrize("field,value", [("sha256", "f" * 64), ("published_at", "2026-10-04"),
    ("retrieved_at", "2026-10-06T00:00:00+00:00"), ("source_url", "https://example.com")])
def test_host_rejects_mismatched_source_identity(tmp_path, field, value):
    host, data, evidence = report_data()
    candidate = tmp_path / "soy_weekly.json"
    atomic_write_json(candidate, data)
    evidence["sources"][0][field] = value
    with pytest.raises(ValueError):
        host._brazil_observations(candidate, None, evidence, [])


def test_closed_delta_schema_and_national_stage_key():
    host, _, _ = report_data()
    identity = {"sha256": "e" * 64, "size_bytes": 10}
    delta = {"schema_version": host.SCHEMA_VERSION, "status": "CANDIDATE", "delta_id": "brazil-test-001",
        "generated_at_utc": "2026-10-07T00:00:00Z", "domain": "brazil_soy", "producer": {
            "commit": "a" * 40, "tree": "b" * 40, "origin": producer.delivery.ORIGIN},
        "run": {"run_id": "brazil-test-001", "git_head": "a" * 40, "business_status": "initialized", "published": False},
        "baseline": {"files": {path: None for path in host.DOMAIN_CONTRACTS["brazil_soy"]["baseline"]}},
        "domain_metadata": {"record_count": 1, "latest_dates": {"2026/2027/BR/PLANTED": "2026-10-02"}, "revision_keys": []},
        "payloads": {name: identity for name in host.DOMAIN_CONTRACTS["brazil_soy"]["payloads"]}}
    schema = json.loads((ROOT / "09_deploy/production_data_delivery/delta_contract.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(delta)
    assert host.validate_delta_document(delta) == delta
    bad = deepcopy(delta)
    bad["domain_metadata"]["latest_dates"] = {"2026/2027/MT/FLOWERING": "2026-10-02"}
    with pytest.raises(host.DeltaError, match="national"):
        host.validate_delta_document(bad)
    with pytest.raises(host.DeltaError):
        host._brazil_revision_keys(["2026/2028/BR/PLANTED/2026-10-02"])
    bad = deepcopy(delta)
    bad["baseline"]["files"][host.BRAZIL_STABLE] = identity
    with pytest.raises(host.DeltaError, match="baseline"):
        host.validate_delta_document(bad)


def test_producer_archive_evidence_and_missing_report(tmp_path):
    host, data, expected = report_data()
    config = {"source_root": str(tmp_path), "workbook_path": None, "workbook_sha256": None}
    with pytest.raises(ValueError, match="archive"):
        producer.source_evidence(config, data, None)
    archive = tmp_path / "raw/brazil_soy/test"
    archive.mkdir(parents=True)
    entry = expected["sources"][0]
    (archive / "report.bin").write_bytes(base64.b64decode(entry["bytes_base64"]))
    atomic_write_json(archive / "source.json", {"schema_version": "brazil-soy-source/1", "final_url": entry["source_url"],
        **{k: entry[k] for k in ("source_url", "sha256", "published_at", "retrieved_at")}})
    actual, inventory = producer.source_evidence(config, data, None)
    assert actual == expected and len(inventory) == 2
