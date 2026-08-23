"""Shared loader for the Domestic Spread page and delivery validation."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


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


__all__ = ["load_domestic_spread_database"]
