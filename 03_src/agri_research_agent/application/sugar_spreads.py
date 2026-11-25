"""Validated exact-contract daily quotes and white sugar spread calculations."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import re
from urllib.parse import urlsplit


DATA_PATH = Path("processed/sugar_spreads/daily_quotes.csv")
QUOTE_FIELDS = ("trade_date", "contract", "price_type", "price", "provider", "source_url")
CONTRACT = re.compile(r"(SB|W)(20\d{2})(0[1-9]|1[0-2])")
MONTHS = {"SB": (3, 5, 7, 10), "W": (3, 5, 8, 10, 12)}
RAW_PAIRS = {"03–05": (3, 5), "05–07": (5, 7), "07–10": (7, 10), "10–次年03": (10, 3)}
PREMIUM_PAIRS = {"03月": (3, 3), "05月": (5, 5), "10月": (10, 10)}
CENTS_PER_POUND_TO_USD_PER_TONNE = Decimal(10) / Decimal("0.45359237")


@dataclass(frozen=True)
class SugarQuote:
    trade_date: date
    contract: str
    price_type: str
    price: Decimal
    provider: str
    source_url: str


@dataclass(frozen=True)
class SugarSpread:
    left: SugarQuote
    right: SugarQuote
    value: Decimal

    @property
    def trade_date(self) -> date:
        return self.left.trade_date

    @property
    def delivery_month(self) -> date:
        return contract_month(self.left.contract)


def contract_month(contract: str) -> date:
    match = CONTRACT.fullmatch(contract)
    if match is None or int(match[3]) not in MONTHS[match[1]]:
        raise ValueError(f"无效的白糖期货合约 {contract}")
    return date(int(match[2]), int(match[3]), 1)


def load_quotes(path: Path, *, as_of: date) -> list[SugarQuote]:
    """Read one complete snapshot. Any ambiguous observation fails the snapshot."""
    quotes = []
    keys = set()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != QUOTE_FIELDS:
            raise ValueError("行情文件字段不符")
        for row in reader:
            if None in row or not all(row.values()):
                raise ValueError(f"行情文件第 {reader.line_num} 行字段不完整")
            try:
                day = date.fromisoformat(row["trade_date"])
                maturity = contract_month(row["contract"])
                price = Decimal(row["price"])
                source = urlsplit(row["source_url"])
            except (ValueError, ArithmeticError) as exc:
                raise ValueError(f"行情文件第 {reader.line_num} 行格式无效") from exc
            key = day, row["contract"], row["price_type"]
            if (day.isoformat() != row["trade_date"] or day.weekday() >= 5
                    or day > as_of or day >= maturity
                    or row["price_type"] not in {"close", "settlement"}
                    or not price.is_finite() or price <= 0
                    or not row["provider"].strip() or source.scheme != "https"
                    or not source.hostname or source.username is not None
                    or key in keys):
                raise ValueError(f"行情文件第 {reader.line_num} 行日期、价格、来源或唯一键无效")
            keys.add(key)
            quotes.append(SugarQuote(day, row["contract"], row["price_type"], price,
                                     row["provider"], row["source_url"]))
    return sorted(quotes, key=lambda quote: (quote.trade_date, quote.contract, quote.price_type))


def spread_history(
    quotes: list[SugarQuote], near_month: int, far_month: int, *, premium: bool, price_type: str,
) -> list[SugarSpread]:
    """Pair actual two-leg observations on the same market date and basis."""
    prices = {(quote.trade_date, quote.contract): quote
              for quote in quotes if quote.price_type == price_type}
    result = []
    market = "W" if premium else "SB"
    for left in prices.values():
        if not left.contract.startswith(market):
            continue
        maturity = contract_month(left.contract)
        if maturity.month != near_month:
            continue
        right_year = maturity.year + (far_month < near_month)
        right = prices.get((left.trade_date, f"SB{right_year:04d}{far_month:02d}"))
        if right is None or left.provider != right.provider:
            continue
        converted = right.price * CENTS_PER_POUND_TO_USD_PER_TONNE if premium else right.price
        result.append(SugarSpread(left, right, left.price - converted))
    return sorted(result, key=lambda point: (point.trade_date, point.delivery_month))


def latest_pair(points: list[SugarSpread], reference_date: date) -> SugarSpread | None:
    """Prefer the nearest pair observed within a week of the latest market date."""
    recent = [point for point in points if reference_date - timedelta(days=7) <= point.trade_date <= reference_date]
    if recent:
        maturity = min(point.delivery_month for point in recent)
        return max((point for point in recent if point.delivery_month == maturity),
                   key=lambda point: point.trade_date)
    return points[-1] if points else None
