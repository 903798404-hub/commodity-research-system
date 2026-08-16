"""Immutable, semantically explicit market quote model."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from .contracts import InstrumentId, InstrumentType


class PriceType(StrEnum):
    LAST = "LAST"
    CLOSE = "CLOSE"
    SETTLEMENT = "SETTLEMENT"


class TradingSession(StrEnum):
    DAY = "DAY"
    NIGHT = "NIGHT"
    UNKNOWN = "UNKNOWN"


class Currency(StrEnum):
    CNY = "CNY"
    USD = "USD"
    CAD = "CAD"
    MYR = "MYR"
    EUR = "EUR"


class PriceUnit(StrEnum):
    CNY_PER_METRIC_TONNE = "CNY/metric_tonne"
    USD_PER_METRIC_TONNE = "USD/metric_tonne"
    US_CENTS_PER_BUSHEL = "US_cents/bushel"
    USD_PER_SHORT_TON = "USD/short_ton"
    US_CENTS_PER_POUND = "US_cents/pound"
    CAD_PER_METRIC_TONNE = "CAD/metric_tonne"
    MYR_PER_METRIC_TONNE = "MYR/metric_tonne"
    EUR_PER_METRIC_TONNE = "EUR/metric_tonne"


_CURRENCY_UNITS = {
    Currency.CNY: {PriceUnit.CNY_PER_METRIC_TONNE},
    Currency.USD: {
        PriceUnit.USD_PER_METRIC_TONNE,
        PriceUnit.US_CENTS_PER_BUSHEL,
        PriceUnit.USD_PER_SHORT_TON,
        PriceUnit.US_CENTS_PER_POUND,
    },
    Currency.CAD: {PriceUnit.CAD_PER_METRIC_TONNE},
    Currency.MYR: {PriceUnit.MYR_PER_METRIC_TONNE},
    Currency.EUR: {PriceUnit.EUR_PER_METRIC_TONNE},
}


def _aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class MarketQuote:
    schema_version: int
    instrument: InstrumentId
    business_date: date
    price: float
    price_type: PriceType
    session: TradingSession
    currency: Currency
    unit: PriceUnit
    source: str
    captured_at: datetime
    source_identity: str
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported MarketQuote schema_version")
        if type(self.business_date) is not date:
            raise TypeError("business_date must be an exact date, not datetime")
        if isinstance(self.price, bool) or not isinstance(self.price, (int, float)):
            raise TypeError("price must be numeric")
        price = float(self.price)
        if not math.isfinite(price) or price <= 0:
            raise ValueError("price must be finite and greater than zero")
        object.__setattr__(self, "price", price)
        for field_name, expected in (
            ("price_type", PriceType),
            ("session", TradingSession),
            ("currency", Currency),
            ("unit", PriceUnit),
        ):
            if not isinstance(getattr(self, field_name), expected):
                raise TypeError(f"{field_name} must be {expected.__name__}")
        if self.unit not in _CURRENCY_UNITS[self.currency]:
            raise ValueError(f"unit {self.unit} is incompatible with currency {self.currency}")
        _aware(self.captured_at, "captured_at")
        if self.observed_at is not None:
            _aware(self.observed_at, "observed_at")
        for field_name in ("source", "source_identity"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty")
            object.__setattr__(self, field_name, value.strip())

    @property
    def instrument_type(self) -> InstrumentType:
        return self.instrument.instrument_type


def quote_business_key(quote: MarketQuote) -> tuple[object, ...]:
    return (
        quote.instrument.exchange,
        quote.instrument.product,
        quote.instrument,
        quote.business_date,
        quote.price_type,
        quote.session,
        quote.source,
    )
