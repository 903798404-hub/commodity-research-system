"""Offline-capable AkShare-shaped frame adapter with explicit source evidence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from ..contracts import ChinaFuturesSymbolParser, Exchange
from ..quotes import Currency, MarketQuote, PriceType, PriceUnit, TradingSession


@dataclass(frozen=True, slots=True)
class AkShareAdapterConfig:
    exchange: Exchange
    price_type: PriceType
    session: TradingSession
    currency: Currency
    unit: PriceUnit
    source: str
    source_identity: str
    captured_at: datetime
    symbol_column: str = "symbol"
    business_date_column: str = "business_date"
    price_column: str = "price"
    observed_at_column: str | None = "observed_at"


class AkShareQuoteAdapter:
    """Normalize caller-supplied evidence; this module owns no network client."""

    def __init__(self, config: AkShareAdapterConfig) -> None:
        self.config = config
        self._parser = ChinaFuturesSymbolParser()

    def from_frame(self, frame: pd.DataFrame) -> tuple[MarketQuote, ...]:
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("frame must be a pandas DataFrame")
        required = {
            self.config.symbol_column,
            self.config.business_date_column,
            self.config.price_column,
        }
        if self.config.observed_at_column is not None:
            required.add(self.config.observed_at_column)
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"AkShare evidence columns missing: {sorted(missing)}")
        quotes: list[MarketQuote] = []
        for index, row in frame.iterrows():
            try:
                parsed = self._parser.parse(str(row[self.config.symbol_column]), self.config.exchange)
                business_value = row[self.config.business_date_column]
                if isinstance(business_value, pd.Timestamp):
                    business_date = business_value.date()
                elif type(business_value) is date:
                    business_date = business_value
                elif isinstance(business_value, str):
                    business_date = date.fromisoformat(business_value)
                else:
                    raise ValueError("business_date must be an ISO date or date")
                observed_at = None
                if self.config.observed_at_column is not None:
                    observed_value = row[self.config.observed_at_column]
                    if not pd.isna(observed_value):
                        if isinstance(observed_value, pd.Timestamp):
                            observed_at = observed_value.to_pydatetime()
                        elif isinstance(observed_value, datetime):
                            observed_at = observed_value
                        elif isinstance(observed_value, str):
                            observed_at = datetime.fromisoformat(observed_value.replace("Z", "+00:00"))
                        else:
                            raise ValueError("observed_at must be an ISO datetime or datetime")
                quotes.append(
                    MarketQuote(
                        schema_version=1,
                        instrument=parsed.instrument,
                        business_date=business_date,
                        price=float(row[self.config.price_column]),
                        price_type=self.config.price_type,
                        session=self.config.session,
                        currency=self.config.currency,
                        unit=self.config.unit,
                        source=self.config.source,
                        captured_at=self.config.captured_at,
                        source_identity=f"{self.config.source_identity};raw_symbol={parsed.raw_code}",
                        observed_at=observed_at,
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid AkShare evidence row {index}: {exc}") from exc
        return tuple(quotes)

    def from_callable(self, fetch: Callable[[], pd.DataFrame]) -> tuple[MarketQuote, ...]:
        """Call only the explicitly injected evidence function."""
        return self.from_frame(fetch())
