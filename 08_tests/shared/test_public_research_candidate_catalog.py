from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "02_configs" / "public_research_data_catalog.candidate.json"


def _catalog() -> dict[str, object]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def test_gate_a_catalog_is_complete_and_identity_safe() -> None:
    catalog = _catalog()
    assert catalog["catalog_schema_version"] == "0.1-candidate"
    assert catalog["status"] == "HUMAN_GATE_A_PENDING"

    datasets = catalog["datasets"]
    assert len(datasets) == 652
    assert catalog["summary"]["datasets_by_origin"] == {"lutou": 604, "tankan": 48}

    dataset_ids = [item["dataset_id_candidate"] for item in datasets]
    assert len(dataset_ids) == len(set(dataset_ids))
    assert all("manual_snapshot" not in item for item in dataset_ids)
    assert all("direct_database" not in item for item in dataset_ids)

    provider_series_ids = [
        series["provider_series_id_candidate"]
        for dataset in datasets
        for series in dataset["provider_series_candidates"]
    ]
    assert len(provider_series_ids) == 6432
    assert len(provider_series_ids) == len(set(provider_series_ids))


def test_origin_and_acquisition_channel_are_not_conflated() -> None:
    catalog = _catalog()
    datasets = catalog["datasets"]
    tankan = [item for item in datasets if item["origin_system"] == "tankan"]
    lutou = [item for item in datasets if item["origin_system"] == "lutou"]

    assert tankan and lutou
    assert {item["acquisition_channel"] for item in tankan} == {"direct_database"}
    assert {item["acquisition_channel"] for item in lutou} == {"manual_snapshot"}
    assert all(item["dataset_id_candidate"].startswith("lutou.") for item in lutou)

    workbook = next(
        item for item in catalog["audit_sources"]
        if item["source_locator"].endswith("大豆-进口榨利.xlsx")
    )
    assert workbook["role"] == "consumer_and_historical_definition_evidence_only"


def test_catalog_contains_no_secrets_or_user_specific_paths() -> None:
    text = CATALOG_PATH.read_text(encoding="utf-8")
    assert not re.search(r"[A-Za-z]:[\\/]Users[\\/]", text, re.IGNORECASE)
    assert not re.search(r'"(?:password|passwd|secret|token|api_key)"\s*:', text, re.IGNORECASE)

    catalog = json.loads(text)
    proof = catalog["audit_sources"][0]["read_only_proof"]
    assert proof["write_guard_enabled"] is True
    assert proof["write_statements_attempted"] is False
    assert proof["explain_analyze_used"] is False


def test_unverified_semantics_are_not_guessed() -> None:
    catalog = _catalog()
    datasets = catalog["datasets"]
    weather = [item for item in datasets if item["domain"] == "weather"]
    assert len(weather) == 565
    assert {item["unit"] for item in weather} == {"unclassified"}

    for dataset in datasets:
        for series in dataset["provider_series_candidates"]:
            assert series["series_id_candidate"] is None
            assert series["canonicalization_status"].startswith(("requires_", "blocked_"))


def test_positions_operational_tables_and_weather_acl_are_conservative() -> None:
    catalog = _catalog()
    by_name = {item["source_native_name"]: item for item in catalog["datasets"]}

    position = by_name["market.foreign_position"]
    assert position["domain"] == "position"
    assert position["canonical_contract_candidate"] == "fundamental_or_typed_observation_candidate"
    assert position["potential_consumers"] == ["research_workbench_or_future_typed_consumer"]

    for name in ("market.futures_live", "market.futures_warehouse", "market.ric_mapping"):
        item = by_name[name]
        assert item["domain"] == "unclassified"
        assert item["canonical_contract_candidate"] == "unclassified"
        assert item["potential_consumers"] == []

    weather = [item for item in catalog["datasets"] if item["domain"] == "weather"]
    existing_acl = [item for item in weather if item["canonicalization_status"] == "existing_acl"]
    assert len(existing_acl) == 10


def test_existing_tankan_source_series_ids_are_provider_evidence_only() -> None:
    evidence = _catalog()["existing_provider_identity_evidence"]
    assert len(evidence["mappings"]) == 7
    values = [
        source_id
        for mapping in evidence["mappings"]
        for source_id in mapping["existing_source_series_ids"]
    ]
    assert len(values) == 14
    assert all(value.startswith("tankan.ffpr.") for value in values)
    assert "not canonical series_id" in evidence["migration_rule"]
