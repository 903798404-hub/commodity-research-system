"""Shared loader for the Domestic Spread page and delivery validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from agri_research_agent.data_sources.tankan.domestic_spread import (
    full_contract_code,
    normalize_full_contract_code,
    resolve_contract_season,
)


TANKAN_DOMESTIC_SPREAD_INSTRUMENTS = ("M", "RM", "Y", "OI", "P")
TANKAN_DOMESTIC_SPREAD_MONTHS = (1, 5, 9)
TANKAN_DOMESTIC_SPREAD_SOURCE = "Goal E / Tankan Domestic Spread"
DEFAULT_SPREAD_CONFIG = (
    Path(__file__).resolve().parents[3] / "02_configs" / "historical_spread_config.xlsx"
)


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
    for column in ("leg1_contract", "leg2_contract"):
        if column not in data.columns:
            data[column] = None
    data = data.dropna(subset=["date", "calendar_offset", "season"])
    data["season"] = data["season"].astype(str)
    data["spread_name"] = data["spread_name"].astype(str)
    return data


def load_domestic_spread_status(
    data: pd.DataFrame,
    config: pd.DataFrame | str | Path | None = None,
) -> DomesticSpreadStatusMetadata:
    """Build truthful status metadata without consulting legacy update markers.

    The latest date and contract coverage are computed from the already-loaded
    formal Domestic Spread payload.  The function intentionally does not use the
    row-level ``updated_at`` value as a producer start/end timestamp.
    """

    columns = {
        "date", "status", "leg1_instrument", "leg1_month",
        "leg2_instrument", "leg2_month",
    }
    if data.empty or not columns <= set(data.columns):
        return DomesticSpreadStatusMetadata(
            "failed", None, 0, 0, 0
        )

    successful = data.loc[data["status"].astype(str).eq("success")]
    dates = pd.to_datetime(successful["date"], errors="coerce")
    if dates.notna().sum() == 0:
        return DomesticSpreadStatusMetadata(
            "failed", None, 0, 0, 0
        )

    latest = dates.max()
    required = _active_required_identities(
        pd.Timestamp(latest), DEFAULT_SPREAD_CONFIG if config is None else config
    )
    required_count = len(required)
    if not required:
        return DomesticSpreadStatusMetadata(
            "failed", pd.Timestamp(latest).date().isoformat(), 0, 0, 0
        )
    latest_rows = successful.loc[dates.eq(latest)]
    actual: set[str] = set()
    for prefix in ("leg1", "leg2"):
        column = f"{prefix}_contract"
        if column not in latest_rows.columns:
            continue
        for value in latest_rows[column]:
            try:
                actual.add(normalize_full_contract_code(value))
            except ValueError:
                continue

    success_count = len(required & actual)
    failure_count = required_count - success_count
    return DomesticSpreadStatusMetadata(
        "success" if failure_count == 0 else "failed",
        pd.Timestamp(latest).date().isoformat(),
        success_count,
        required_count,
        failure_count,
    )


def _active_required_identities(
    business_date: pd.Timestamp,
    config: pd.DataFrame | str | Path,
) -> set[str]:
    if isinstance(config, pd.DataFrame):
        rules = config.copy()
    else:
        rules = pd.read_excel(Path(config), sheet_name="spread_config")
    required_columns = {
        "leg1_instrument", "leg1_month", "leg2_instrument", "leg2_month",
        "window_start_month", "window_start_day", "window_end_month",
        "window_end_day",
    }
    if not required_columns <= set(rules.columns):
        return set()
    if "enabled" in rules.columns:
        rules = rules.loc[rules["enabled"].map(_is_enabled)].copy()
    active: set[str] = set()
    for row in rules.itertuples(index=False):
        season = resolve_contract_season(
            business_date,
            window_start_month=int(row.window_start_month),
            window_start_day=int(row.window_start_day),
            window_end_month=int(row.window_end_month),
            window_end_day=int(row.window_end_day),
        )
        if season is None:
            continue
        active.add(
            full_contract_code(
                str(row.leg1_instrument), season.label, int(row.leg1_month)
            )
        )
        active.add(
            full_contract_code(
                str(row.leg2_instrument), season.label, int(row.leg2_month)
            )
        )
    return active


def _is_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y", "是", "启用"}


__all__ = [
    "DomesticSpreadStatusMetadata", "TANKAN_DOMESTIC_SPREAD_INSTRUMENTS",
    "TANKAN_DOMESTIC_SPREAD_MONTHS", "TANKAN_DOMESTIC_SPREAD_SOURCE",
    "load_domestic_spread_database", "load_domestic_spread_status",
]
