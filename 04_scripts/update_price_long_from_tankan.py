#!/usr/bin/env python
"""Incrementally append validated Tankan closes to Domestic Spread price_long."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "03_src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agri_research_agent.data_sources.tankan.client import (  # noqa: E402
    TankanClient,
    TankanConnectionSettings,
)
from agri_research_agent.data_sources.tankan.domestic_spread import (  # noqa: E402
    SOURCE_NAME,
    build_price_long_rows,
    normalize_rows,
    required_symbols,
    validate_relation_columns,
)
from agri_research_agent.data_sources.tankan.queries import (  # noqa: E402
    DOMESTIC_SPREAD_WINDOW_QUERY,
)


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def latest_season(database_file: Path) -> str:
    spreads = pd.read_excel(database_file, sheet_name="spread_long", usecols=["season", "status"])
    if "status" in spreads.columns:
        spreads = spreads[spreads["status"] == "success"]
    seasons = spreads["season"].dropna().astype(str).unique().tolist()
    if not seasons:
        raise ValueError("Domestic Spread database has no active season")
    try:
        return max(seasons, key=lambda value: int(value.split("/", 1)[0]))
    except (TypeError, ValueError):
        raise ValueError("Domestic Spread database season is invalid") from None


def extract_prices(
    client: TankanClient,
    *,
    existing_latest: dt.date,
    season: str,
    end_date: dt.date,
) -> tuple[pd.DataFrame, dt.date]:
    relation = client.inspect_relation("market", "futures_spread")
    validate_relation_columns(column.column_name for column in relation)
    source_latest = client.latest_source_dates()["domestic_spread"]
    effective_end = min(source_latest, end_date)
    if effective_end < existing_latest:
        raise ValueError("Tankan Domestic Spread latest date regressed behind active price_long")
    start_date = existing_latest if effective_end == existing_latest else existing_latest + dt.timedelta(days=1)
    if (effective_end - start_date).days > DOMESTIC_SPREAD_WINDOW_QUERY.max_window_days:
        raise ValueError("Tankan Domestic Spread incremental window exceeds approved bound")
    _, batches = client.plan_stream(
        DOMESTIC_SPREAD_WINDOW_QUERY,
        (start_date, effective_end),
    )
    rows = [row for batch in batches for row in batch.rows]
    normalized = normalize_rows(rows, season=season)
    return normalized, source_latest


def append_new_prices(
    existing: pd.DataFrame,
    normalized: pd.DataFrame,
    *,
    season: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    expected = set(required_symbols(season))
    latest_existing = pd.to_datetime(existing["date"], errors="coerce").max()
    if pd.isna(latest_existing):
        raise ValueError("active Domestic Spread price_long has no business date")
    selected = normalized[normalized["date"] > latest_existing].copy()
    to_append = build_price_long_rows(
        selected,
        existing_columns=list(existing.columns),
        updated_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    if not selected.empty and set(selected["symbol"].astype(str)) != expected:
        raise ValueError("Tankan Domestic Spread required contract set is incomplete")
    if to_append.empty:
        return existing.copy(), to_append
    keys = ["date", "instrument", "delivery_month"]
    combined = pd.concat([existing, to_append], ignore_index=True)
    if combined.duplicated(keys).any():
        raise ValueError("Domestic Spread price_long append would create duplicate keys")
    combined["date"] = pd.to_datetime(combined["date"], errors="coerce")
    return combined.sort_values(keys).reset_index(drop=True), to_append


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Append validated Tankan Domestic Spread closes")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-backup", action="store_true")
    parser.add_argument("--result-json", type=Path)
    parser.add_argument("--end-date", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument(
        "--tankan-secret-file",
        type=Path,
        default=Path.home() / ".market-data-secrets" / "tankan.env",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    price_file = ROOT / "01_data" / "historical_price_long.xlsx"
    database_file = ROOT / "01_data" / "historical_spread_database.xlsx"
    payload: dict[str, object] = {
        "status": "failed",
        "source": SOURCE_NAME,
        "price_field": "close_price",
        "required_contracts": 15,
        "success_contracts": 0,
        "failure_contracts": 15,
        "failed_contracts": [],
        "to_append_rows": 0,
        "overwritten_rows": 0,
        "price_long_written": False,
        "latest_date": "",
        "source_latest_date": "",
        "error_message": "",
    }
    try:
        existing = pd.read_excel(price_file, sheet_name="price_long")
        existing["date"] = pd.to_datetime(existing["date"], errors="coerce")
        existing_latest_value = existing["date"].max()
        if pd.isna(existing_latest_value):
            raise ValueError("active Domestic Spread price_long has no business date")
        existing_latest = pd.Timestamp(existing_latest_value).date()
        season = latest_season(database_file)
        settings = TankanConnectionSettings.from_secret_file(args.tankan_secret_file)
        with TankanClient(settings) as client:
            normalized, source_latest = extract_prices(
                client,
                existing_latest=existing_latest,
                season=season,
                end_date=args.end_date,
            )
        combined, to_append = append_new_prices(existing, normalized, season=season)
        if not args.dry_run and not to_append.empty:
            temporary = price_file.with_name("historical_price_long.tmp.xlsx")
            if temporary.exists():
                temporary.unlink()
            with pd.ExcelWriter(temporary, engine="openpyxl") as writer:
                combined.to_excel(writer, sheet_name="price_long", index=False)
            os.replace(temporary, price_file)
        latest = pd.to_datetime(combined["date"], errors="coerce").max()
        payload.update(
            {
                "status": "success",
                "success_contracts": 15,
                "failure_contracts": 0,
                "to_append_rows": int(len(to_append)),
                "price_long_written": bool(not args.dry_run and not to_append.empty),
                "latest_date": pd.Timestamp(latest).strftime("%Y-%m-%d"),
                "source_latest_date": source_latest.isoformat(),
            }
        )
        if args.result_json:
            atomic_write_json(args.result_json, payload)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        payload["error_message"] = f"{type(exc).__name__}: {exc}"
        if args.result_json:
            atomic_write_json(args.result_json, payload)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
