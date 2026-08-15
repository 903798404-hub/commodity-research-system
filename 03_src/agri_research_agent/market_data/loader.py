"""Read-only loader for the standardized MarketQuote schema."""

from __future__ import annotations

import math
from datetime import date, datetime
from numbers import Real
from pathlib import Path

import pandas as pd

from .contracts import ContractId, ContinuousInstrumentId, Exchange, InstrumentType
from .quotes import Currency, MarketQuote, PriceType, PriceUnit, TradingSession, quote_business_key


STANDARD_QUOTE_COLUMNS = (
    "schema_version",
    "exchange",
    "product",
    "instrument_type",
    "contract_year",
    "contract_month",
    "business_date",
    "price",
    "price_type",
    "session",
    "currency",
    "unit",
    "source",
    "captured_at",
    "source_identity",
    "observed_at",
)


class MarketDataSchemaError(ValueError):
    pass


def _whole_number(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise MarketDataSchemaError(f"{field_name} must be an integer")
    number = float(value)
    if not math.isfinite(number) or not number.is_integer():
        raise MarketDataSchemaError(f"{field_name} must be an integer")
    return int(number)


def _date(value: object, field_name: str) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise MarketDataSchemaError(f"invalid {field_name}: {value!r}") from exc
    raise MarketDataSchemaError(f"{field_name} must be an ISO date or date")


def _datetime(value: object, field_name: str, *, optional: bool = False) -> datetime | None:
    if optional and (value is None or pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        result = value.to_pydatetime()
    elif isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MarketDataSchemaError(f"invalid {field_name}: {value!r}") from exc
    else:
        raise MarketDataSchemaError(f"invalid {field_name}: {value!r}")
    return result


def _instrument(row: pd.Series) -> ContractId | ContinuousInstrumentId:
    exchange = Exchange(row["exchange"])
    kind = InstrumentType(row["instrument_type"])
    if kind is InstrumentType.CONTINUOUS_MAIN:
        if not pd.isna(row["contract_year"]) or not pd.isna(row["contract_month"]):
            raise MarketDataSchemaError("continuous instruments cannot have contract year/month")
        return ContinuousInstrumentId(exchange, row["product"])
    if pd.isna(row["contract_year"]) or pd.isna(row["contract_month"]):
        raise MarketDataSchemaError("delivery instruments require contract year/month")
    return ContractId(
        exchange,
        row["product"],
        _whole_number(row["contract_year"], "contract_year"),
        _whole_number(row["contract_month"], "contract_month"),
    )


def load_market_quotes_frame(frame: pd.DataFrame) -> tuple[MarketQuote, ...]:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame")
    actual = set(frame.columns)
    required = set(STANDARD_QUOTE_COLUMNS)
    if actual != required:
        raise MarketDataSchemaError(
            f"standard quote schema mismatch; missing={sorted(required - actual)}, extra={sorted(actual - required)}"
        )
    quotes: list[MarketQuote] = []
    seen: set[tuple[object, ...]] = set()
    for index, row in frame.iterrows():
        try:
            quote = MarketQuote(
                schema_version=_whole_number(row["schema_version"], "schema_version"),
                instrument=_instrument(row),
                business_date=_date(row["business_date"], "business_date"),
                price=float(row["price"]),
                price_type=PriceType(row["price_type"]),
                session=TradingSession(row["session"]),
                currency=Currency(row["currency"]),
                unit=PriceUnit(row["unit"]),
                source=row["source"],
                captured_at=_datetime(row["captured_at"], "captured_at"),  # type: ignore[arg-type]
                source_identity=row["source_identity"],
                observed_at=_datetime(row["observed_at"], "observed_at", optional=True),
            )
        except (TypeError, ValueError) as exc:
            raise MarketDataSchemaError(f"invalid standardized quote row {index}: {exc}") from exc
        key = quote_business_key(quote)
        if key in seen:
            raise MarketDataSchemaError(f"duplicate quote business key at row {index}")
        seen.add(key)
        quotes.append(quote)
    return tuple(quotes)


def load_market_quotes_parquet(path: str | Path) -> tuple[MarketQuote, ...]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"explicit standardized parquet path does not exist: {source}")
    return load_market_quotes_frame(pd.read_parquet(source))
