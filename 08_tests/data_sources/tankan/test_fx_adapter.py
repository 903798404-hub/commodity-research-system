from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pytest

from agri_research_agent.data_sources.tankan.fx_adapter import (
    FX_RAW_SCHEMA,
    FxAdapterError,
    adapt_fx,
    load_fx_config,
)
from agri_research_agent.research_data import DataAssetCatalog


ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "02_configs" / "tankan_fx.yaml"
NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


def raw_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "trade_date": date(2026, 8, 12),
        "spot": 6.7432,
        **{f"fx_{month}m": 6.74 - month / 100 for month in range(1, 13)},
        "updated_at": datetime(2026, 8, 13, 8, 30),
    }
    values.update(overrides)
    return values


def test_wide_row_becomes_thirteen_provider_candidates_without_canonical_guess() -> None:
    config = load_fx_config(CONFIG_PATH)
    result = adapt_fx(
        pa.Table.from_pylist([raw_row()], schema=FX_RAW_SCHEMA),
        config,
        snapshot_sha256="e" * 64,
        captured_at=NOW,
    )
    rows = result.table.to_pylist()
    assert [(item["tenor"], item["tenor_months"]) for item in rows] == [
        ("SPOT", 0),
        *[(f"{month}M", month) for month in range(1, 13)],
    ]
    assert len({item["provider_series_id"] for item in rows}) == 13
    assert all(item["provider_series_id"].startswith("tankan:market.exchange_rate:") for item in rows)
    assert all(item["series_id_candidate"] is None for item in rows)
    assert all(item["rate_type"] == "unspecified" for item in rows)
    assert all(item["value_date"] is None and item["maturity_date"] is None for item in rows)
    assert result.quality_report["canonical_series_status"] == "blocked_rate_type_unspecified"

    catalog = DataAssetCatalog.load_gate_a_approved(
        ROOT / "02_configs" / "public_research_data_catalog.candidate.json",
        ROOT / "02_configs" / "public_research_data_catalog.approval.json",
    )
    approved_ids = {
        str(item.provider_series_id)
        for item in catalog.get("tankan.market.exchange_rate").provider_series
    }
    assert {item["provider_series_id"] for item in rows} == approved_ids


@pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan"), None])
def test_bad_rates_are_preserved_and_marked_unusable(bad: float | None) -> None:
    result = adapt_fx(
        pa.Table.from_pylist([raw_row(fx_6m=bad)], schema=FX_RAW_SCHEMA),
        load_fx_config(CONFIG_PATH),
        snapshot_sha256="f" * 64,
        captured_at=NOW,
    )
    point = next(item for item in result.table.to_pylist() if item["tenor"] == "6M")
    assert point["is_usable"] is False
    assert point["quality_status"] != "valid"
    assert len(point["source_row_sha256"]) == 64


def test_fx_schema_drift_fails_before_standardization() -> None:
    raw = pa.Table.from_pylist([raw_row()], schema=FX_RAW_SCHEMA).drop(["fx_12m"])
    with pytest.raises(FxAdapterError, match="schema"):
        adapt_fx(
            raw,
            load_fx_config(CONFIG_PATH),
            snapshot_sha256="0" * 64,
            captured_at=NOW,
        )
