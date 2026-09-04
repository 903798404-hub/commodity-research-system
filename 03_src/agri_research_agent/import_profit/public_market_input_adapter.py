"""Thin translation ACL from canonical Public Market Data shadow contracts.

Provider choice, source access, quality policy, and cutover authorization remain
outside the Import Profit bounded context.  These functions are intentionally
not wired into the formal Import Profit pipelines.
"""

from __future__ import annotations

from agri_research_agent.market_data.soybean_shadow import (
    CbotReferenceQuote,
    CbotSoybeanShadowQuote,
    FxSpotShadowQuote,
)

from .market_snapshot import CbotPricePoint, FxPricePoint


class PublicMarketInputTranslationError(ValueError):
    pass


def to_cbot_price_point(value: CbotSoybeanShadowQuote) -> CbotPricePoint:
    if not value.is_usable or value.price is None:
        raise PublicMarketInputTranslationError("CBOT shadow quote is not translatable")
    return CbotPricePoint(
        market_date=value.business_date,
        contract_year=value.contract.year,
        contract_month=value.contract.month,
        price_cents_per_bushel=value.price,
        exchange_quality_status=value.quality_status,
        is_usable=True,
        eligible_for_import_profit=True,
        source=value.provider_series_id,
        source_table="tankan.market.foreign_futures_price_raw",
        source_column="close_price",
        source_snapshot_sha256=value.source_snapshot_sha256,
    )


def to_fx_price_point(value: FxSpotShadowQuote) -> FxPricePoint:
    if not value.is_usable or value.rate is None:
        raise PublicMarketInputTranslationError("FX Spot shadow quote is not translatable")
    return FxPricePoint(
        market_date=value.quote_date,
        tenor_months=0,
        fx_value=value.rate,
        source=value.provider_series_id,
        source_table="tankan.market.exchange_rate",
        source_column="spot",
        source_snapshot_sha256=value.source_snapshot_sha256,
    )


def to_cbot_reference_quote(
    value: CbotPricePoint,
    *,
    price_type: str,
) -> CbotReferenceQuote:
    """Expose an existing Import Profit observation for shadow comparison."""

    if not price_type.strip():
        raise PublicMarketInputTranslationError("reference price_type must be explicit")
    return CbotReferenceQuote(
        business_date=value.market_date,
        contract_year=value.contract_year,
        contract_month=value.contract_month,
        price=value.price_cents_per_bushel,
        price_type=price_type,
        source_date=value.market_date,
    )


__all__ = [
    "PublicMarketInputTranslationError",
    "to_cbot_price_point",
    "to_cbot_reference_quote",
    "to_fx_price_point",
]
