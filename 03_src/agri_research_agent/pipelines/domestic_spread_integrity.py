"""Domestic Spread historical price and publication integrity contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


HISTORICAL_KEY_COLUMNS = ("date", "spread_name", "season")
HISTORICAL_METADATA_COLUMNS = frozenset({"updated_at"})


class PriceSemantic(StrEnum):
    DAILY_CLOSE = "DAILY_CLOSE"
    INTRADAY_CURRENT_PRICE = "INTRADAY_CURRENT_PRICE"
    UNKNOWN = "UNKNOWN"


class HistoricalPublicationMode(StrEnum):
    NORMAL = "NORMAL"
    HISTORICAL_RECONCILIATION = "HISTORICAL_RECONCILIATION"


class HistoricalPublicationBlocked(RuntimeError):
    """Candidate contains a business mutation outside the declared boundary."""

    def __init__(self, report: "HistoricalDiffReport") -> None:
        self.report = report
        keys = ",".join("|".join(key) for key in report.blocked_keys)
        super().__init__(f"Domestic Spread historical publication blocked: {keys}")


@dataclass(frozen=True, slots=True)
class HistoricalMutationPolicy:
    mode: HistoricalPublicationMode
    start_date: date
    end_date: date
    allowed_keys: frozenset[tuple[str, str, str]] = frozenset()

    def __post_init__(self) -> None:
        if self.mode is HistoricalPublicationMode.NORMAL and self.allowed_keys:
            raise ValueError("normal refresh cannot declare historical allowed keys")
        if (
            self.mode is HistoricalPublicationMode.HISTORICAL_RECONCILIATION
            and not self.allowed_keys
        ):
            raise ValueError("historical reconciliation requires exact allowed keys")


@dataclass(frozen=True, slots=True)
class HistoricalDiffReport:
    mode: str
    changed_keys: tuple[tuple[str, str, str], ...]
    blocked_keys: tuple[tuple[str, str, str], ...]

    @property
    def publication_allowed(self) -> bool:
        return not self.blocked_keys

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["publication_allowed"] = self.publication_allowed
        return payload


def infer_price_semantic(source_file: object, source_column: object) -> PriceSemantic:
    """Classify only source/field pairs with an explicit historical meaning."""

    source = str(source_file).strip().lower()
    column = str(source_column).strip().lower()
    field = column.rsplit(":", 1)[-1]
    source_name = source.replace("\\", "/").rsplit("/", 1)[-1]
    if source_name == "historical_price_base.xlsx":
        # The approved imported base is the pre-existing canonical daily-close
        # history.  Its legacy column labels predate explicit semantic fields.
        return PriceSemantic.DAILY_CLOSE
    if source == "akshare_futures_zh_daily_sina" and field == "close":
        return PriceSemantic.DAILY_CLOSE
    if source == "tankan.market.futures_spread" and field == "close_price":
        return PriceSemantic.DAILY_CLOSE
    if source == "akshare_futures_zh_spot" and field == "current_price":
        return PriceSemantic.INTRADAY_CURRENT_PRICE
    return PriceSemantic.UNKNOWN


def select_canonical_historical_prices(price_long: pd.DataFrame) -> pd.DataFrame:
    """Return one DAILY_CLOSE row per historical contract/date identity.

    A provenance-free frame is treated as an already-normalized internal frame
    for backwards-compatible pure calculation tests.  Formal price_long inputs
    carry both provenance columns and are filtered strictly.
    """

    frame = price_long.copy()
    provenance = {"source_file", "source_column"}
    if provenance <= set(frame.columns):
        frame["price_semantic"] = [
            infer_price_semantic(source, column).value
            for source, column in zip(
                frame["source_file"], frame["source_column"], strict=True
            )
        ]
        frame = frame.loc[
            frame["price_semantic"].eq(PriceSemantic.DAILY_CLOSE.value)
        ].copy()
    keys = ["date", "instrument", "delivery_month"]
    if not set(keys) <= set(frame.columns):
        return frame
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if "updated_at" in frame.columns:
        frame["_integrity_updated_at"] = pd.to_datetime(
            frame["updated_at"], errors="coerce"
        )
        frame = frame.sort_values(keys + ["_integrity_updated_at"], na_position="first")
        frame = frame.drop(columns="_integrity_updated_at")
    else:
        frame = frame.sort_values(keys)
    return frame.drop_duplicates(keys, keep="last").reset_index(drop=True)


def validate_historical_publication(
    current: pd.DataFrame | str | Path,
    candidate: pd.DataFrame | str | Path,
    policy: HistoricalMutationPolicy,
) -> HistoricalDiffReport:
    """Compare business rows and enforce the normal/reconciliation boundary."""

    before = _read_spreads(current)
    after = _read_spreads(candidate)
    before_rows = _business_rows(before)
    after_rows = _business_rows(after)
    changed: list[tuple[str, str, str]] = []
    blocked: list[tuple[str, str, str]] = []
    for key in sorted(set(before_rows) | set(after_rows)):
        if before_rows.get(key) == after_rows.get(key):
            continue
        changed.append(key)
        business_date = date.fromisoformat(key[0])
        in_requested_range = policy.start_date <= business_date <= policy.end_date
        allowed = in_requested_range or (
            policy.mode is HistoricalPublicationMode.HISTORICAL_RECONCILIATION
            and key in policy.allowed_keys
        )
        if not allowed:
            blocked.append(key)
    report = HistoricalDiffReport(
        policy.mode.value, tuple(changed), tuple(blocked)
    )
    if not report.publication_allowed:
        raise HistoricalPublicationBlocked(report)
    return report


def parse_allowed_key(value: str) -> tuple[str, str, str]:
    parts = tuple(part.strip() for part in value.split("|"))
    if len(parts) != 3 or not all(parts):
        raise ValueError("allowed key must be DATE|SPREAD_NAME|SEASON")
    date.fromisoformat(parts[0])
    return parts


def _read_spreads(value: pd.DataFrame | str | Path) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    path = Path(value)
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_excel(path, sheet_name="spread_long")


def _business_rows(frame: pd.DataFrame) -> dict[tuple[str, str, str], tuple[Any, ...]]:
    missing = set(HISTORICAL_KEY_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(
            f"Domestic Spread diff schema is missing: {','.join(sorted(missing))}"
        )
    normalized = frame.copy()
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    if normalized["date"].isna().any():
        raise ValueError("Domestic Spread diff contains an invalid date")
    value_columns = sorted(
        column
        for column in normalized.columns
        if column not in HISTORICAL_METADATA_COLUMNS
        and column not in HISTORICAL_KEY_COLUMNS
    )
    rows: dict[tuple[str, str, str], tuple[Any, ...]] = {}
    for record in normalized.to_dict("records"):
        key = (
            pd.Timestamp(record["date"]).date().isoformat(),
            str(record["spread_name"]),
            str(record["season"]),
        )
        if key in rows:
            raise ValueError(f"Domestic Spread diff contains duplicate key: {'|'.join(key)}")
        rows[key] = tuple(_stable_value(record.get(column)) for column in value_columns)
    return rows


def _stable_value(value: object) -> object:
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 10)
    return value


__all__ = [
    "HistoricalDiffReport", "HistoricalMutationPolicy",
    "HistoricalPublicationBlocked", "HistoricalPublicationMode",
    "PriceSemantic", "infer_price_semantic", "parse_allowed_key",
    "select_canonical_historical_prices", "validate_historical_publication",
]
