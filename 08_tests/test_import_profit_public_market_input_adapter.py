from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from agri_research_agent.import_profit.public_market_input_adapter import (
    PublicMarketInputTranslationError,
    to_cbot_price_point,
    to_cbot_reference_quote,
    to_fx_price_point,
)
from agri_research_agent.import_profit.market_snapshot import CbotPricePoint
from agri_research_agent.market_data.contracts import ContractId, Exchange
from agri_research_agent.market_data.soybean_shadow import (
    CbotSoybeanShadowQuote,
    FxSpotShadowQuote,
)


NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


def test_public_market_acl_only_translates_canonical_shadow_contracts() -> None:
    cbot = to_cbot_price_point(
        CbotSoybeanShadowQuote(
            business_date=date(2026, 8, 14),
            contract=ContractId(Exchange.CBOT, "SOYBEAN", 2026, 11),
            price=1_025.5,
            provider_dataset_id="tankan:market.foreign_futures_price_raw",
            provider_series_id="tankan.ffpr.cbot.soybean.en",
            source_updated_at=NOW,
            captured_at=NOW,
            source_snapshot_sha256="a" * 64,
            source_row_sha256="b" * 64,
            quality_status="valid",
            is_usable=True,
        )
    )
    fx = to_fx_price_point(
        FxSpotShadowQuote(
            quote_date=date(2026, 8, 14),
            rate=7.1821,
            provider_dataset_id="tankan:market.exchange_rate",
            provider_series_id="tankan:market.exchange_rate:spot",
            source_updated_at=NOW,
            captured_at=NOW,
            source_snapshot_sha256="c" * 64,
            source_row_sha256="d" * 64,
            quality_status="valid",
            is_usable=True,
        )
    )
    assert (cbot.contract_year, cbot.contract_month, cbot.price_cents_per_bushel) == (
        2026,
        11,
        1_025.5,
    )
    assert cbot.source_table == "tankan.market.foreign_futures_price_raw"
    assert (fx.tenor_months, fx.fx_value) == (0, 7.1821)
    assert fx.source_table == "tankan.market.exchange_rate"


def test_acl_contains_no_provider_query_or_selection_policy() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "03_src"
        / "agri_research_agent"
        / "import_profit"
        / "public_market_input_adapter.py"
    )
    source = path.read_text(encoding="utf-8")
    assert "data_sources.tankan" not in source
    assert "TankanClient" not in source
    assert "fallback" not in source.casefold()
    assert "provider_series_id ==" not in source


def test_existing_reuters_point_becomes_semantically_explicit_reference() -> None:
    reference = to_cbot_reference_quote(
        CbotPricePoint(
            market_date=date(2026, 8, 14),
            contract_year=2026,
            contract_month=11,
            price_cents_per_bushel=1_026.0,
            exchange_quality_status="valid",
            is_usable=True,
            eligible_for_import_profit=True,
            source="reuters",
            source_table="legacy",
            source_column="settlement",
            source_snapshot_sha256="e" * 64,
        ),
        price_type="settlement",
    )
    assert reference.price == 1_026.0
    assert reference.price_type == "settlement"
    assert reference.source_date == date(2026, 8, 14)


def test_acl_rejects_unusable_shadow_instead_of_filling_or_falling_back() -> None:
    unusable = CbotSoybeanShadowQuote(
        business_date=date(2026, 8, 14),
        contract=ContractId(Exchange.CBOT, "SOYBEAN", 2026, 11),
        price=None,
        provider_dataset_id="tankan:market.foreign_futures_price_raw",
        provider_series_id="tankan.ffpr.cbot.soybean.en",
        source_updated_at=NOW,
        captured_at=NOW,
        source_snapshot_sha256="a" * 64,
        source_row_sha256="b" * 64,
        quality_status="missing_price",
        is_usable=False,
    )
    with pytest.raises(PublicMarketInputTranslationError, match="not translatable"):
        to_cbot_price_point(unusable)
