from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from agri_research_agent.market_data.basis import (
    BasisMarket,
    BasisQuote,
    BasisQuoteType,
    basis_business_key,
)
from agri_research_agent.market_data.contracts import ContractId, Exchange
from agri_research_agent.market_data.quotes import Currency, PriceUnit


def _quote(**overrides: object) -> BasisQuote:
    values: dict[str, object] = {
        "schema_version": 1,
        "series_id": "market.basis.domestic.china.soybean_oil.east_china.spot",
        "market": BasisMarket.DOMESTIC_CHINA,
        "business_date": date(2026, 8, 4),
        "product": "soybean_oil",
        "location": "华东",
        "value": Decimal("-20"),
        "currency": Currency.CNY,
        "unit": PriceUnit.CNY_PER_METRIC_TONNE,
        "quote_type": BasisQuoteType.DOMESTIC_SPOT,
        "underlying_futures": ContractId(Exchange.DCE, "Y", 2026, 9),
        "source": "Lutou",
        "source_series_id": "lutou:domestic_basis:basis_price:大豆油:华东:现货基差",
        "provider_dataset_id": "lutou:domestic_basis:basis_price",
        "provider_series_id": "lutou:domestic_basis:basis_price:大豆油:华东:现货基差",
        "source_locator": "database:lutou/schema:pending/relation:basis_price",
        "source_row_identity": "row-1",
        "query_identity": "query-v1",
        "snapshot_identity": "fixture-v1",
        "captured_at": datetime(2026, 8, 20, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return BasisQuote(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [Decimal("-210"), Decimal("0"), Decimal("2100")])
def test_basis_contract_accepts_finite_negative_zero_and_positive_values(value: Decimal) -> None:
    quote = _quote(value=value)
    assert quote.value == value
    assert basis_business_key(quote) == (
        BasisMarket.DOMESTIC_CHINA,
        quote.series_id,
        quote.business_date,
    )


def test_domestic_and_international_basis_identities_and_units_cannot_merge() -> None:
    domestic = _quote()
    international = _quote(
        series_id="market.basis.soybean_oil.argentina.upper_river.spot",
        market=BasisMarket.INTERNATIONAL,
        location="Argentina Upper River",
        currency=Currency.USD,
        unit=PriceUnit.US_CENTS_PER_POUND,
        quote_type=BasisQuoteType.INTERNATIONAL_SPOT,
    )
    assert basis_business_key(domestic) != basis_business_key(international)
    with pytest.raises(ValueError, match="domestic series identity"):
        _quote(
            market=BasisMarket.INTERNATIONAL,
            currency=Currency.USD,
            unit=PriceUnit.US_CENTS_PER_POUND,
            quote_type=BasisQuoteType.INTERNATIONAL_SPOT,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"series_id": ""}, "series_id"),
        ({"location": ""}, "location"),
        ({"source_row_identity": ""}, "source_row_identity"),
        ({"value": Decimal("NaN")}, "finite"),
        ({"currency": Currency.USD}, "CNY/metric_tonne"),
        ({"unit": PriceUnit.US_CENTS_PER_POUND}, "CNY/metric_tonne"),
    ],
)
def test_basis_contract_rejects_missing_or_semantically_wrong_fields(
    overrides: dict[str, object], message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        _quote(**overrides)
