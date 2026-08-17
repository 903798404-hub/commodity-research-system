from __future__ import annotations

from datetime import date, datetime, timezone

import pyarrow as pa
import pytest

from agri_research_agent.data_sources.tankan.fx_adapter import FX_CANDIDATE_SCHEMA
from agri_research_agent.data_sources.tankan.market_price_adapter import (
    MARKET_CANDIDATE_SCHEMA,
)
from agri_research_agent.market_data.contracts import ContractId, Exchange
from agri_research_agent.market_data.soybean_shadow import (
    CbotReferenceQuote,
    SoybeanShadowError,
    build_cbot_soybean_shadow,
    build_usd_cnh_spot_shadow,
    compare_cbot_reference,
)


NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


def market_row(
    provider_series_id: str,
    price: float = 1_025.5,
    *,
    contract_code: str = "2611",
    instrument_id: str | None = "CBOT:SOYBEAN:2026-11",
    business_date: date = date(2026, 8, 14),
    usable: bool = True,
) -> dict[str, object]:
    return {
        "schema_version": "tankan-market-candidate/1",
        "dataset_id": "tankan.market.foreign_futures_price_raw",
        "series_id_candidate": None,
        "provider_dataset_id": "tankan:market.foreign_futures_price_raw",
        "provider_series_id": provider_series_id,
        "source_series_id": provider_series_id,
        "origin_system": "tankan",
        "acquisition_channel": "direct_database",
        "source_locator": "database:quanyong/schema:market/relation:foreign_futures_price_raw",
        "business_date": business_date,
        "exchange": "CBOT",
        "product": "SOYBEAN",
        "contract_code": contract_code,
        "instrument_id": instrument_id,
        "price": price,
        "price_type": "CLOSE",
        "session": "UNKNOWN",
        "currency": "USD",
        "price_unit": "US_cents/bushel",
        "source_updated_at": datetime(2026, 8, 15, 8, 20),
        "captured_at": NOW,
        "snapshot_sha256": "a" * 64,
        "source_row_sha256": "b" * 64,
        "quality_status": "valid" if usable else "missing_price",
        "is_usable": usable,
    }


def fx_row(tenor: str, months: int, column: str, rate: float) -> dict[str, object]:
    return {
        "schema_version": "tankan-fx-candidate/1",
        "dataset_id": "tankan.market.exchange_rate",
        "series_id_candidate": None,
        "provider_dataset_id": "tankan:market.exchange_rate",
        "provider_series_id": f"tankan:market.exchange_rate:{column}",
        "origin_system": "tankan",
        "acquisition_channel": "direct_database",
        "source_locator": "database:quanyong/schema:market/relation:exchange_rate",
        "quote_date": date(2026, 8, 14),
        "value_date": None,
        "maturity_date": None,
        "base_currency": "USD",
        "quote_currency": "CNH",
        "tenor": tenor,
        "tenor_months": months,
        "rate": rate,
        "rate_type": "unspecified",
        "rate_unit": "CNH_per_USD",
        "source_column": column,
        "source_updated_at": datetime(2026, 8, 15, 8, 25),
        "captured_at": NOW,
        "snapshot_sha256": "c" * 64,
        "source_row_sha256": "d" * 64,
        "quality_status": "valid",
        "is_usable": True,
    }


def test_cbot_shadow_parses_exact_yymm_and_keeps_providers_separate() -> None:
    result = build_cbot_soybean_shadow(
        pa.Table.from_pylist(
            [
                market_row("tankan.ffpr.cbot.soybean.en", 1_025.5),
                market_row("tankan.ffpr.cbot.soybean.zh", 1_025.5),
            ],
            schema=MARKET_CANDIDATE_SCHEMA,
        )
    )

    rows = result.table.to_pylist()
    assert [(row["contract_year"], row["contract_month"]) for row in rows] == [
        (2026, 11),
        (2026, 11),
    ]
    assert {row["provider_role"] for row in rows} == {
        "current_candidate",
        "legacy_comparison",
    }
    assert result.source_policy["overlap_equal_key_count"] == 1
    assert result.source_policy["overlap_different_key_count"] == 0
    assert result.source_policy["cutover_authorized"] is False
    assert all(row["promotion_authorized"] is False for row in rows)


@pytest.mark.parametrize(
    ("contract_code", "instrument_id"),
    [("main", None), ("0", None), ("2613", None), ("2611", "CBOT:SOYBEAN:2026-12")],
)
def test_cbot_shadow_rejects_non_exact_or_inconsistent_contracts(
    contract_code: str, instrument_id: str | None
) -> None:
    with pytest.raises(SoybeanShadowError, match="exact contract"):
        build_cbot_soybean_shadow(
            pa.Table.from_pylist(
                [
                    market_row(
                        "tankan.ffpr.cbot.soybean.en",
                        contract_code=contract_code,
                        instrument_id=instrument_id,
                    )
                ],
                schema=MARKET_CANDIDATE_SCHEMA,
            )
        )


def test_cbot_reference_comparison_marks_semantic_difference() -> None:
    shadow = build_cbot_soybean_shadow(
        pa.Table.from_pylist(
            [market_row("tankan.ffpr.cbot.soybean.en")],
            schema=MARKET_CANDIDATE_SCHEMA,
        )
    )
    comparison = compare_cbot_reference(
        shadow,
        [
            CbotReferenceQuote(
                business_date=date(2026, 8, 14),
                contract_year=2026,
                contract_month=11,
                price=1_026.0,
                price_type="settlement",
                source_date=date(2026, 8, 14),
            )
        ],
    )
    assert comparison[0].availability == "both"
    assert comparison[0].semantic_status == "SEMANTIC_DIFFERENCE"
    assert comparison[0].absolute_difference == pytest.approx(0.5)


def test_cbot_shadow_fails_closed_on_semantic_identity_drift() -> None:
    bad = market_row("tankan.ffpr.cbot.soybean.en")
    bad["price_type"] = "SETTLEMENT"
    with pytest.raises(SoybeanShadowError, match="semantic identity"):
        build_cbot_soybean_shadow(
            pa.Table.from_pylist([bad], schema=MARKET_CANDIDATE_SCHEMA)
        )


def test_fx_shadow_promotes_only_spot_and_keeps_forward_blocked() -> None:
    result = build_usd_cnh_spot_shadow(
        pa.Table.from_pylist(
            [fx_row("SPOT", 0, "spot", 7.1821), fx_row("1M", 1, "fx_1m", 7.16)],
            schema=FX_CANDIDATE_SCHEMA,
        )
    )
    rows = result.table.to_pylist()
    assert len(rows) == 1
    assert rows[0]["rate_type"] == "spot"
    assert rows[0]["rate_unit"] == "CNH_per_USD"
    assert rows[0]["provider_series_id"] == "tankan:market.exchange_rate:spot"
    assert result.source_policy["forward_canonical_promotion"] is False
    assert result.source_policy["blocked_forward_row_count"] == 1


def test_fx_shadow_fails_closed_on_spot_identity_drift() -> None:
    bad = fx_row("SPOT", 0, "spot", 7.1821)
    bad["quote_currency"] = "CNY"
    with pytest.raises(SoybeanShadowError, match="identity"):
        build_usd_cnh_spot_shadow(
            pa.Table.from_pylist([bad], schema=FX_CANDIDATE_SCHEMA)
        )
