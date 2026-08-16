from __future__ import annotations

import json
from pathlib import Path

import pytest

from agri_research_agent.research_data.catalog import CatalogError, DataAssetCatalog
from agri_research_agent.research_data.identities import DatasetId


ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "02_configs" / "public_research_data_catalog.candidate.json"
APPROVAL_PATH = ROOT / "02_configs" / "public_research_data_catalog.approval.json"


def test_approved_catalog_loads_with_exact_declared_counts() -> None:
    catalog = DataAssetCatalog.load_gate_a_approved(CATALOG_PATH, APPROVAL_PATH)

    assert len(catalog.datasets) == 652
    assert len(catalog.find(origin_system="tankan")) == 48
    assert len(catalog.find(origin_system="lutou")) == 604
    assert len(catalog.find(domain="weather")) == 565
    assert catalog.declared_summary["provider_series_candidate_count"] == 6432
    assert catalog.status == "HUMAN_GATE_A_APPROVED"
    assert catalog.source_catalog_status == "HUMAN_GATE_A_PENDING"
    assert catalog.iter_promotable() == ()
    with pytest.raises(CatalogError, match="candidate-only"):
        catalog.require_promotable("tankan.market.exchange_rate")


def test_catalog_lookup_returns_typed_source_identity() -> None:
    catalog = DataAssetCatalog.load_gate_a_approved(CATALOG_PATH, APPROVAL_PATH)
    asset = catalog.get(DatasetId("tankan.market.exchange_rate"))

    assert str(asset.dataset.origin_system) == "tankan"
    assert asset.acquisition_channel.value == "direct_database"
    assert str(asset.provider_dataset_id) == "tankan:market.exchange_rate"
    assert str(asset.source_locator) == "database:quanyong/schema:market/relation:exchange_rate"
    assert asset.domain == "fx"
    with pytest.raises(TypeError):
        asset.raw["domain"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        asset.raw["source_columns"][0]["name"] = "changed"  # type: ignore[index]


def test_catalog_is_discovery_only_and_rejects_unapproved_series(tmp_path: Path) -> None:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    payload["datasets"][0]["provider_series_candidates"][0]["series_id_candidate"] = (
        "fundamental.guessed.value"
    )
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CatalogError, match="unapproved canonical series"):
        DataAssetCatalog.load_candidate_for_audit(path)


def test_catalog_rejects_declared_count_drift(tmp_path: Path) -> None:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    payload["summary"]["dataset_count"] += 1
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CatalogError, match="declared dataset count"):
        DataAssetCatalog.load_candidate_for_audit(path)


def test_approved_loader_rejects_catalog_byte_drift(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_bytes(CATALOG_PATH.read_bytes() + b"\n")

    with pytest.raises(CatalogError, match="do not match Gate A approval"):
        DataAssetCatalog.load_gate_a_approved(path, APPROVAL_PATH)


def test_catalog_rejects_provider_series_with_unknown_source_column(tmp_path: Path) -> None:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    payload["datasets"][0]["provider_series_candidates"][0]["source_column"] = "missing"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CatalogError, match="unknown source column"):
        DataAssetCatalog.load_candidate_for_audit(path)


def test_catalog_rejects_sensitive_fields(tmp_path: Path) -> None:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    payload["password"] = "must-not-exist"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CatalogError, match="sensitive field"):
        DataAssetCatalog.load_candidate_for_audit(path)


def test_gate_a_approval_hash_is_cross_platform_line_ending_safe(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_bytes(CATALOG_PATH.read_bytes().replace(b"\n", b"\r\n"))

    catalog = DataAssetCatalog.load_gate_a_approved(path, APPROVAL_PATH)
    assert len(catalog.datasets) == 652


def test_catalog_rejects_duplicate_provider_series_identity(tmp_path: Path) -> None:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    first = payload["datasets"][0]["provider_series_candidates"][0]
    second = payload["datasets"][1]["provider_series_candidates"][0]
    second["provider_series_id_candidate"] = first["provider_series_id_candidate"]
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CatalogError, match="not unique"):
        DataAssetCatalog.load_candidate_for_audit(path)


def test_catalog_constructor_cannot_bypass_validated_loaders() -> None:
    with pytest.raises(TypeError, match="validated loader"):
        DataAssetCatalog(  # type: ignore[call-arg]
            schema_version="0.1-candidate",
            status="HUMAN_GATE_A_APPROVED",
            source_catalog_status="HUMAN_GATE_A_PENDING",
            datasets=(),
            declared_summary={},
            _token=object(),
        )
