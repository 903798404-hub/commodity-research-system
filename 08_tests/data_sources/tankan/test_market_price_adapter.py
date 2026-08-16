from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pytest

from agri_research_agent.data_sources.tankan.market_price_adapter import (
    MARKET_RAW_SCHEMA,
    MarketAdapterConfig,
    MarketPriceAdapterError,
    MarketSeriesMapping,
    adapt_market_price,
    load_market_config,
)
from agri_research_agent.market_data.contracts import Exchange
from agri_research_agent.market_data.quotes import Currency, PriceUnit


ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "02_configs" / "tankan_market_price.yaml"
CATALOG_PATH = ROOT / "02_configs" / "public_research_data_catalog.candidate.json"
NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


def mapping(source_product: str, provider_id: str) -> MarketSeriesMapping:
    return MarketSeriesMapping(
        source_exchange="ICE",
        source_product_name=source_product,
        source_series_id=provider_id,
        exchange=Exchange.ICE,
        product="CANOLA",
        currency=Currency.CAD,
        price_unit=PriceUnit.CAD_PER_METRIC_TONNE,
        quantity_unit="metric_tonne",
        contract_size=20,
        contract_size_unit="metric_tonne",
    )


def config() -> MarketAdapterConfig:
    zh = mapping("菜籽", "tankan.ffpr.ice.canola.zh")
    en = mapping("canola", "tankan.ffpr.ice.canola.en")
    return MarketAdapterConfig(
        5,
        frozenset({"CANOLA"}),
        {("ICE", "菜籽"): zh, ("ICE", "canola"): en},
    )


def table(rows: list[dict[str, object]]) -> pa.Table:
    return pa.Table.from_pylist(rows, schema=MARKET_RAW_SCHEMA)


def row(
    product: str,
    price: float | None,
    contract: str = "2611",
    trade_date: date = date(2026, 8, 13),
) -> dict[str, object]:
    return {
        "trade_date": trade_date,
        "exchange": "ICE",
        "product_name": product,
        "contract": contract,
        "close_price": price,
        "updated_at": datetime(2026, 8, 14, 8, 30),
    }


def test_mapping_ids_are_provider_evidence_not_canonical_series() -> None:
    loaded = load_market_config(CONFIG_PATH)
    provider_ids = {str(item.provider_series_id) for item in loaded.mappings.values()}
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    evidence = {
        item
        for mapping_item in catalog["existing_provider_identity_evidence"]["mappings"]
        for item in mapping_item["existing_source_series_ids"]
    }
    assert len(provider_ids) == 14
    assert provider_ids == evidence
    assert {item.source_series_id for item in loaded.mappings.values()} == provider_ids


def test_provider_series_are_preserved_and_collision_blocks_only_promotion() -> None:
    result = adapt_market_price(
        table([row("菜籽", 800.0), row("canola", 802.5)]),
        config(),
        snapshot_sha256="a" * 64,
        captured_at=NOW,
    )
    values = result.table.to_pylist()
    assert {item["provider_series_id"] for item in values} == {
        "tankan.ffpr.ice.canola.zh",
        "tankan.ffpr.ice.canola.en",
    }
    assert all(item["provider_series_id"] == item["source_series_id"] for item in values)
    assert all(item["series_id_candidate"] is None for item in values)
    assert all(item["dataset_id"] == "tankan.market.foreign_futures_price_raw" for item in values)
    assert all(item["instrument_id"] == "ICE:CANOLA:2026-11" for item in values)
    assert result.collision_report["different_price_key_count"] == 1
    assert result.collision_report["collision_status"] == "BLOCKED_DIFFERING_PROVIDER_VALUES"
    assert result.collision_report["candidate_only"] is True
    assert result.collision_report["promotion_authorized"] is False
    assert result.collision_report["samples"][0]["prices_differ"] is True
    assert {
        item["provider_series_id"]
        for item in result.collision_report["samples"][0]["observations"]
    } == {"tankan.ffpr.ice.canola.zh", "tankan.ffpr.ice.canola.en"}


@pytest.mark.parametrize("bad", [-1.0, 0.0, float("inf"), float("nan"), None])
def test_bad_prices_remain_as_unusable_provider_candidates(bad: float | None) -> None:
    result = adapt_market_price(
        table([row("菜籽", bad)]),
        MarketAdapterConfig(
            5,
            frozenset({"CANOLA"}),
            {("ICE", "菜籽"): mapping("菜籽", "tankan.ffpr.ice.canola.zh")},
        ),
        snapshot_sha256="b" * 64,
        captured_at=NOW,
    )
    candidate = result.table.to_pylist()[0]
    assert candidate["is_usable"] is False
    assert candidate["quality_status"] != "valid"
    assert len(candidate["source_row_sha256"]) == 64


def test_schema_drift_and_unmapped_series_fail_closed() -> None:
    good = table([row("菜籽", 800.0)])
    with pytest.raises(MarketPriceAdapterError, match="schema"):
        adapt_market_price(
            good.drop(["updated_at"]),
            config(),
            snapshot_sha256="c" * 64,
            captured_at=NOW,
        )
    with pytest.raises(MarketPriceAdapterError, match="unmapped"):
        adapt_market_price(
            table([row("unknown", 800.0)]),
            config(),
            snapshot_sha256="d" * 64,
            captured_at=NOW,
        )
