"""Goal E2 contract mapping and hard QC for Tankan Domestic Spread closes."""

from __future__ import annotations

import re
from dataclasses import dataclass
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
_INSTRUMENTS = frozenset(instrument for _, instrument, _ in PRODUCTS.values())
_FULL_CONTRACT = re.compile(r"^(RM|OI|M|Y|P)(\d{2})(01|05|09)$")


class DomesticSpreadSourceError(RuntimeError):
    """Fail-closed source-contract or completeness error."""


@dataclass(frozen=True, slots=True)
class DomesticSpreadSeason:
    """One configured business window and its contract-year season."""

    label: str
    start_date: date
    end_date: date


def resolve_contract_season(
    business_date: date | datetime,
    *,
    window_start_month: int,
    window_start_day: int,
    window_end_month: int,
    window_end_day: int,
) -> DomesticSpreadSeason | None:
    """Resolve the active contract season from one configured calendar window."""

    value = business_date.date() if isinstance(business_date, datetime) else business_date
    if type(value) is not date:
        raise ValueError("Domestic Spread business_date must be a date")
    start_tuple = (int(window_start_month), int(window_start_day))
    end_tuple = (int(window_end_month), int(window_end_day))
    current = (value.month, value.day)
    crosses_year = start_tuple > end_tuple
    if crosses_year:
        if current >= start_tuple:
            start_year = value.year
        elif current <= end_tuple:
            start_year = value.year - 1
        else:
            return None
        end_year = start_year + 1
    else:
        if not start_tuple <= current <= end_tuple:
            return None
        start_year = value.year
        end_year = value.year
    start = date(start_year, *start_tuple)
    end = date(end_year, *end_tuple)
    if not start <= value <= end:
        return None
    return DomesticSpreadSeason(f"{start_year}/{start_year + 1}", start, end)


def contract_year_for_month(season: str, month: int) -> int:
    """Resolve one listed delivery month to its full year in the season."""

    try:
        start_year, end_year = (int(part) for part in str(season).split("/", 1))
    except (TypeError, ValueError):
        raise DomesticSpreadSourceError("Domestic Spread season is invalid") from None
    if end_year != start_year + 1 or int(month) not in MONTHS:
        raise DomesticSpreadSourceError(
            "Domestic Spread season or delivery month is invalid"
        )
    return start_year if int(month) == 9 else end_year


def full_contract_code(instrument: str, season: str, month: int) -> str:
    """Build the canonical Instrument+YYMM identity without a fixed year."""

    normalized_instrument = str(instrument).strip().upper()
    normalized_month = int(month)
    if normalized_instrument not in _INSTRUMENTS:
        raise ValueError("Domestic Spread instrument is invalid")
    year = contract_year_for_month(season, normalized_month)
    return f"{normalized_instrument}{year % 100:02d}{normalized_month:02d}"


def window_contract_code(
    instrument: str, season: str, month: int, *, window_start_month: int,
) -> str:
    """Bind delivery year to the configured observation window.

    October--April observes May and September of the following year; the
    February--August window observes September then January of the next year.
    The season label alone cannot distinguish those September contracts.
    """
    full_contract_code(instrument, season, month)  # validate the governed identity
    start_year = int(season.split("/")[0])
    start_month = int(window_start_month)
    if not 1 <= start_month <= 12:
        raise ValueError("Domestic Spread window start month is invalid")
    year = start_year + int(int(month) < start_month)
    return f"{str(instrument).strip().upper()}{year % 100:02d}{int(month):02d}"


def normalize_full_contract_code(value: object) -> str:
    """Validate and normalize one complete Domestic Spread contract code."""

    text = str(value).strip().upper()
    match = _FULL_CONTRACT.fullmatch(text)
    if match is None:
        raise ValueError("Domestic Spread contract code must be Instrument+YYMM")
    return f"{match.group(1)}{match.group(2)}{match.group(3)}"


def contract_code_from_source_column(
    source_column: object,
    *,
    instrument: str,
    delivery_month: int,
) -> str | None:
    """Recover the exact source contract while rejecting month-only aliases."""

    candidate = str(source_column).split(":", 1)[0].strip()
    try:
        code = normalize_full_contract_code(candidate)
    except ValueError:
        return None
    match = _FULL_CONTRACT.fullmatch(code)
    assert match is not None
    if match.group(1) != str(instrument).strip().upper():
        return None
    if int(match.group(3)) != int(delivery_month):
        return None
    return code


def required_symbols(season: str) -> tuple[str, ...]:
    try:
        return tuple(
            full_contract_code(instrument, season, month)
            for _, instrument, _ in PRODUCTS.values()
            for month in MONTHS
        )
    except ValueError as exc:
        raise DomesticSpreadSourceError(str(exc)) from None


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
        try:
            symbol = full_contract_code(instrument, season, month)
        except ValueError as exc:
            raise DomesticSpreadSourceError(str(exc)) from None
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
    "DomesticSpreadSeason", "DomesticSpreadSourceError", "MONTHS", "PRICE_FIELD",
    "PRODUCTS",
    "REQUIRED_RELATION_COLUMNS", "SOURCE_NAME", "build_price_long_rows",
    "contract_code_from_source_column", "contract_year_for_month", "full_contract_code",
    "normalize_full_contract_code", "normalize_rows", "required_symbols",
    "resolve_contract_season", "validate_relation_columns",
]
