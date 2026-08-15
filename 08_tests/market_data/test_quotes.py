from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone

import pytest

from agri_research_agent.market_data.contracts import ContractId, ContinuousInstrumentId, Exchange
from agri_research_agent.market_data.quotes import (
    Currency,
    MarketQuote,
    PriceType,
    PriceUnit,
    TradingSession,
    quote_business_key,
)


NOW = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)


def quote(**overrides: object) -> MarketQuote:
    values: dict[str, object] = {
        "schema_version": 1,
        "instrument": ContractId(Exchange.DCE, "P", 2026, 9),
        "business_date": date(2026, 8, 14),
        "price": 8120.0,
        "price_type": PriceType.CLOSE,
        "session": TradingSession.DAY,
        "currency": Currency.CNY,
        "unit": PriceUnit.CNY_PER_METRIC_TONNE,
        "source": "fixture",
        "captured_at": NOW,
        "source_identity": "sha256:abc",
        "observed_at": NOW,
    }
    values.update(overrides)
    return MarketQuote(**values)  # type: ignore[arg-type]


def test_market_quote_is_immutable_and_instrument_type_is_derived() -> None:
    value = quote()
    assert value.instrument_type is value.instrument.instrument_type
    with pytest.raises(FrozenInstanceError):
        value.price = 1.0  # type: ignore[misc]


@pytest.mark.parametrize("field", ["captured_at", "observed_at"])
def test_timestamps_must_be_timezone_aware(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        quote(**{field: datetime(2026, 8, 15, 12)})


def test_business_date_rejects_datetime_even_when_aware() -> None:
    with pytest.raises(TypeError, match="exact date"):
        quote(business_date=NOW)


@pytest.mark.parametrize("price", [0, -1, float("inf"), float("nan")])
def test_price_must_be_positive_and_finite(price: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        quote(price=price)


def test_currency_and_unit_combination_is_validated() -> None:
    with pytest.raises(ValueError, match="incompatible"):
        quote(currency=Currency.USD, unit=PriceUnit.CNY_PER_METRIC_TONNE)


def test_price_and_session_semantics_are_distinct_business_keys() -> None:
    night_last = quote(price_type=PriceType.LAST, session=TradingSession.NIGHT)
    day_close = quote(price_type=PriceType.CLOSE, session=TradingSession.DAY)
    settlement = quote(price_type=PriceType.SETTLEMENT, session=TradingSession.DAY)
    assert len({quote_business_key(item) for item in (night_last, day_close, settlement)}) == 3


def test_continuous_and_delivery_are_distinct_even_with_same_market_values() -> None:
    p0 = quote(instrument=ContinuousInstrumentId(Exchange.DCE, "P"))
    p2609 = quote(instrument=ContractId(Exchange.DCE, "P", 2026, 9))
    assert quote_business_key(p0) != quote_business_key(p2609)
