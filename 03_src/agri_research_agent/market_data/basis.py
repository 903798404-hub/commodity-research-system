"""Typed, source-preserving basis observations for Public Market Data."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from .contracts import ContractId
from .quotes import Currency, PriceUnit


class BasisMarket(StrEnum):
    DOMESTIC_CHINA = "DOMESTIC_CHINA"
    INTERNATIONAL = "INTERNATIONAL"


class BasisQuoteType(StrEnum):
    DOMESTIC_SPOT = "DOMESTIC_SPOT_BASIS"
    INTERNATIONAL_SPOT = "INTERNATIONAL_SPOT_BASIS"


_SERIES_ID = re.compile(r"^market\.basis\.[a-z0-9_.-]+$")


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
    return value.strip()


@dataclass(frozen=True, slots=True)
class BasisQuote:
    """One canonical basis observation without assuming basis is positive."""

    schema_version: int
    series_id: str
    market: BasisMarket
    business_date: date
    product: str
    location: str
    value: Decimal
    currency: Currency
    unit: PriceUnit
    quote_type: BasisQuoteType
    underlying_futures: ContractId
    source: str
    source_series_id: str
    provider_dataset_id: str
    provider_series_id: str
    source_locator: str
    source_row_identity: str
    query_identity: str
    snapshot_identity: str
    captured_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported BasisQuote schema_version")
        if not isinstance(self.market, BasisMarket):
            raise TypeError("market must be BasisMarket")
        if type(self.business_date) is not date:
            raise TypeError("business_date must be an exact date")
        try:
            value = self.value if isinstance(self.value, Decimal) else Decimal(str(self.value))
        except (InvalidOperation, ValueError) as exc:
            raise TypeError("value must be decimal-compatible") from exc
        if not value.is_finite():
            raise ValueError("basis value must be finite")
        object.__setattr__(self, "value", value)
        if not isinstance(self.currency, Currency):
            raise TypeError("currency must be Currency")
        if not isinstance(self.unit, PriceUnit):
            raise TypeError("unit must be PriceUnit")
        if not isinstance(self.quote_type, BasisQuoteType):
            raise TypeError("quote_type must be BasisQuoteType")
        for field_name in (
            "series_id",
            "product",
            "location",
            "source",
            "source_series_id",
            "provider_dataset_id",
            "provider_series_id",
            "source_locator",
            "source_row_identity",
            "query_identity",
            "snapshot_identity",
        ):
            value_text = _nonempty(getattr(self, field_name), field_name)
            object.__setattr__(self, field_name, value_text)
        if self.market is BasisMarket.DOMESTIC_CHINA:
            if self.quote_type is not BasisQuoteType.DOMESTIC_SPOT:
                raise ValueError("Domestic Basis requires DOMESTIC_SPOT_BASIS")
            if not self.series_id.startswith("market.basis.domestic.china."):
                raise ValueError("Domestic Basis series identity must include domestic.china")
            if (
                self.currency is not Currency.CNY
                or self.unit is not PriceUnit.CNY_PER_METRIC_TONNE
            ):
                raise ValueError("Domestic Basis requires CNY/metric_tonne")
        else:
            if self.quote_type is not BasisQuoteType.INTERNATIONAL_SPOT:
                raise ValueError("International Basis requires INTERNATIONAL_SPOT_BASIS")
            if self.series_id.startswith("market.basis.domestic."):
                raise ValueError("International Basis must not use a domestic series identity")
            if (
                self.currency is not Currency.USD
                or self.unit is not PriceUnit.US_CENTS_PER_POUND
            ):
                raise ValueError("International Basis requires explicit USD cents/pound")
        if not isinstance(self.underlying_futures, ContractId):
            raise TypeError("underlying_futures must be a delivery ContractId")
        if _SERIES_ID.fullmatch(self.series_id) is None:
            raise ValueError("invalid basis series_id")
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")


def basis_business_key(quote: BasisQuote) -> tuple[BasisMarket, str, date]:
    return quote.market, quote.series_id, quote.business_date


__all__ = [
    "BasisMarket",
    "BasisQuote",
    "BasisQuoteType",
    "basis_business_key",
]
