"""Goal E2 contract mapping and hard QC for Tankan Domestic Spread closes."""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Mapping

import pandas as pd


PRICE_FIELD = "close_price"
SOURCE_NAME = "tankan.market.futures_spread"
MONTHS = (1, 5, 9)
PRODUCTS = {
    "豆粕": ("DCE", "M", "豆粕"),
    "菜籽粕": ("CZCE", "RM", "菜粕"),
    "豆油": ("DCE", "Y", "豆油"),
    "菜籽油": ("CZCE", "OI", "菜油"),
    "棕榈油": ("DCE", "P", "棕榈油"),
}
REQUIRED_RELATION_COLUMNS = frozenset(
    {"trade_date", "product_name", "contract", PRICE_FIELD, "updated_at"}
)


class DomesticSpreadSourceError(RuntimeError):
    """Fail-closed source-contract or completeness error."""


def contract_year_for_month(season: str, month: int) -> int:
    try:
        start_year, end_year = (int(part) for part in str(season).split("/", 1))
    except (TypeError, ValueError):
        raise DomesticSpreadSourceError("Domestic Spread season is invalid") from None
    if end_year != start_year + 1:
        raise DomesticSpreadSourceError("Domestic Spread season is invalid")
    return start_year if month == 9 else end_year


def required_symbols(season: str) -> tuple[str, ...]:
    return tuple(
        f"{instrument}{contract_year_for_month(season, month) % 100:02d}{month:02d}"
        for _, instrument, _ in PRODUCTS.values()
        for month in MONTHS
    )


def validate_relation_columns(column_names: Iterable[str]) -> None:
    if not REQUIRED_RELATION_COLUMNS <= set(column_names):
        raise DomesticSpreadSourceError(
            "Tankan Domestic Spread relation does not expose the approved close-price schema"
        )


def normalize_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    season: str,
) -> pd.DataFrame:
    """Normalize source-native month series and apply hard source-domain QC."""

    records: list[dict[str, object]] = []
    for row in rows:
        if not REQUIRED_RELATION_COLUMNS <= set(row):
            raise DomesticSpreadSourceError("Tankan Domestic Spread row schema is invalid")
        raw_date = row["trade_date"]
        if isinstance(raw_date, datetime):
            raw_date = raw_date.date()
        if type(raw_date) is not date:
            raise DomesticSpreadSourceError("Tankan Domestic Spread business_date is invalid")
        if raw_date.weekday() >= 5:
            raise DomesticSpreadSourceError(
                "Tankan Domestic Spread source contains a weekend business_date"
            )
        product_name = str(row["product_name"])
        mapping = PRODUCTS.get(product_name)
        contract = str(row["contract"])
        if mapping is None or contract not in {"01", "05", "09"}:
            raise DomesticSpreadSourceError("Tankan Domestic Spread series mapping is invalid")
        try:
            price = float(row[PRICE_FIELD])
        except (TypeError, ValueError):
            raise DomesticSpreadSourceError("Tankan Domestic Spread close_price is invalid") from None
        if not pd.notna(price) or price <= 0:
            raise DomesticSpreadSourceError("Tankan Domestic Spread close_price is invalid")
        exchange, instrument, instrument_cn = mapping
        month = int(contract)
        symbol = f"{instrument}{contract_year_for_month(season, month) % 100:02d}{month:02d}"
        records.append(
            {
                "date": pd.Timestamp(raw_date),
                "exchange": exchange,
                "product_name": product_name,
                "instrument": instrument,
                "instrument_cn": instrument_cn,
                "delivery_month": month,
                "symbol": symbol,
                "price": price,
                "price_field": PRICE_FIELD,
                "source": SOURCE_NAME,
            }
        )
    frame = pd.DataFrame.from_records(records)
    if frame.empty:
        raise DomesticSpreadSourceError("Tankan Domestic Spread source window is empty")
    keys = ["date", "instrument", "delivery_month"]
    if frame.duplicated(keys).any():
        raise DomesticSpreadSourceError("Tankan Domestic Spread source contains duplicate series dates")
    expected = set(required_symbols(season))
    for business_date, day in frame.groupby("date", sort=True):
        actual = set(day["symbol"].astype(str))
        if actual != expected:
            missing = sorted(expected - actual)
            raise DomesticSpreadSourceError(
                f"Tankan Domestic Spread 15-contract completeness failed for "
                f"{business_date:%Y-%m-%d}: missing={','.join(missing)}"
            )
    return frame.sort_values(keys).reset_index(drop=True)


def build_price_long_rows(
    normalized: pd.DataFrame,
    *,
    existing_columns: list[str],
    updated_at: str,
) -> pd.DataFrame:
    records = []
    for row in normalized.itertuples(index=False):
        record = {
            "date": row.date,
            "instrument": row.instrument,
            "instrument_cn": row.instrument_cn,
            "delivery_month": row.delivery_month,
            "price": row.price,
            "source_column": f"{row.symbol}:{row.price_field}",
            "source_file": row.source,
            "updated_at": updated_at,
            "status": "success",
            "error": "",
        }
        records.append({column: record.get(column, "") for column in existing_columns})
    return pd.DataFrame(records, columns=existing_columns)


__all__ = [
    "DomesticSpreadSourceError", "MONTHS", "PRICE_FIELD", "PRODUCTS",
    "REQUIRED_RELATION_COLUMNS", "SOURCE_NAME", "build_price_long_rows",
    "normalize_rows", "required_symbols", "validate_relation_columns",
]
