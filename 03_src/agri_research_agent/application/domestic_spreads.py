"""Shared loader for the Domestic Spread page and delivery validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd


TANKAN_DOMESTIC_SPREAD_INSTRUMENTS = ("M", "RM", "Y", "OI", "P")
TANKAN_DOMESTIC_SPREAD_MONTHS = (1, 5, 9)
TANKAN_DOMESTIC_SPREAD_SOURCE = "Goal E / Tankan Domestic Spread"


@dataclass(frozen=True, slots=True)
class DomesticSpreadStatusMetadata:
    """Page status derived from the same formal artifact as the chart payload."""

    status: str
    latest_business_date: str | None
    success_contracts: int
    required_contracts: int
    failure_contracts: int
    source: str = TANKAN_DOMESTIC_SPREAD_SOURCE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_domestic_spread_database(database_path: str | Path) -> pd.DataFrame:
    path = Path(database_path)
    if path.suffix.lower() == ".parquet":
        data = pd.read_parquet(path)
    else:
        data = pd.read_excel(path, sheet_name="spread_long")
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data["calendar_offset"] = pd.to_numeric(data["calendar_offset"], errors="coerce")
    data["spread_value"] = pd.to_numeric(data["spread_value"], errors="coerce")
    data["leg1_price"] = pd.to_numeric(data["leg1_price"], errors="coerce")
    data["leg2_price"] = pd.to_numeric(data["leg2_price"], errors="coerce")
    data = data.dropna(subset=["date", "calendar_offset", "season"])
    data["season"] = data["season"].astype(str)
    data["spread_name"] = data["spread_name"].astype(str)
    return data


def load_domestic_spread_status(
    data: pd.DataFrame,
) -> DomesticSpreadStatusMetadata:
    """Build truthful status metadata without consulting legacy update markers.

    The latest date and contract coverage are computed from the already-loaded
    formal Domestic Spread payload.  The function intentionally does not use the
    row-level ``updated_at`` value as a producer start/end timestamp.
    """

    required = {
        (instrument, month)
        for instrument in TANKAN_DOMESTIC_SPREAD_INSTRUMENTS
        for month in TANKAN_DOMESTIC_SPREAD_MONTHS
    }
    required_count = len(required)
    columns = {
        "date", "status", "leg1_instrument", "leg1_month",
        "leg2_instrument", "leg2_month",
    }
    if data.empty or not columns <= set(data.columns):
        return DomesticSpreadStatusMetadata(
            "failed", None, 0, required_count, required_count
        )

    successful = data.loc[data["status"].astype(str).eq("success")]
    dates = pd.to_datetime(successful["date"], errors="coerce")
    if dates.notna().sum() == 0:
        return DomesticSpreadStatusMetadata(
            "failed", None, 0, required_count, required_count
        )

    latest = dates.max()
    latest_rows = successful.loc[dates.eq(latest)]
    actual: set[tuple[str, int]] = set()
    for prefix in ("leg1", "leg2"):
        instruments = latest_rows[f"{prefix}_instrument"]
        months = pd.to_numeric(latest_rows[f"{prefix}_month"], errors="coerce")
        for instrument, month in zip(instruments, months, strict=True):
            if pd.notna(instrument) and pd.notna(month):
                actual.add((str(instrument), int(month)))

    success_count = len(required & actual)
    failure_count = required_count - success_count
    return DomesticSpreadStatusMetadata(
        "success" if failure_count == 0 else "failed",
        pd.Timestamp(latest).date().isoformat(),
        success_count,
        required_count,
        failure_count,
    )


__all__ = [
    "DomesticSpreadStatusMetadata", "TANKAN_DOMESTIC_SPREAD_INSTRUMENTS",
    "TANKAN_DOMESTIC_SPREAD_MONTHS", "TANKAN_DOMESTIC_SPREAD_SOURCE",
    "load_domestic_spread_database", "load_domestic_spread_status",
]
