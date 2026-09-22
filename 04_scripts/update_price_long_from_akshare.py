from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "03_src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agri_research_agent.data_sources.tankan.domestic_spread import (  # noqa: E402
    contract_code_from_source_column,
    full_contract_code,
    resolve_contract_season,
)
from agri_research_agent.pipelines.domestic_spread_integrity import (  # noqa: E402
    PriceSemantic,
    infer_price_semantic,
)


DEFAULT_INSTRUMENTS = ["M", "RM", "Y", "OI", "P"]
DEFAULT_MONTHS = [1, 5, 9]
INSTRUMENT_CN = {
    "M": "豆粕",
    "RM": "菜粕",
    "Y": "豆油",
    "OI": "菜油",
    "P": "棕榈油",
}
PRICE_FIELD_PRIORITY = ["current_price", "last_close", "last_settle_price", "avg_price"]
PRICE_LONG_SHEET = "price_long"
CONFIG_SHEET = "spread_config"
SPREAD_LONG_SHEET = "spread_long"
LATE_ARRIVAL_LOOKBACK_DAYS = 2
NEW_CONTRACT_LOOKBACK_DAYS = 7


class HistoricalDailyCloseConflict(RuntimeError):
    """Provider close conflicts with an existing canonical daily close."""


def project_root() -> Path:
    return ROOT


def setup_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("update_price_long_from_akshare")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


def is_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y", "是", "启用"}


def latest_season_from_database(database_file: Path) -> str | None:
    spread_long = pd.read_excel(database_file, sheet_name=SPREAD_LONG_SHEET, usecols=["season", "status"])
    if "status" in spread_long.columns:
        spread_long = spread_long[spread_long["status"] == "success"]
    seasons = sorted(spread_long["season"].dropna().astype(str).unique().tolist(), key=season_sort_key)
    return seasons[-1] if seasons else None


def season_sort_key(season: str) -> int:
    try:
        return int(str(season).split("/")[0])
    except (TypeError, ValueError):
        return -1


def contract_year_for_month(season: str, month: int) -> int:
    start_year_text, end_year_text = str(season).split("/", maxsplit=1)
    start_year = int(start_year_text)
    end_year = int(end_year_text)
    return start_year if int(month) == 9 else end_year


def contract_symbol(instrument: str, month: int, season: str) -> str:
    year = contract_year_for_month(season, int(month))
    return f"{instrument.upper()}{year % 100:02d}{int(month):02d}"


def akshare_display_symbol(instrument: str, month: int, season: str) -> str:
    year = contract_year_for_month(season, int(month))
    prefix = INSTRUMENT_CN.get(instrument.upper(), instrument.upper())
    return f"{prefix}{year % 100:02d}{int(month):02d}"


def instruments_and_months_from_config(config_file: Path) -> list[tuple[str, int]]:
    config = pd.read_excel(config_file, sheet_name=CONFIG_SHEET)
    if "enabled" in config.columns:
        config = config[config["enabled"].map(is_enabled)].copy()
    pairs: set[tuple[str, int]] = set()
    for instrument_col, month_col in [
        ("leg1_instrument", "leg1_month"),
        ("leg2_instrument", "leg2_month"),
    ]:
        if instrument_col not in config.columns or month_col not in config.columns:
            continue
        for _, row in config[[instrument_col, month_col]].dropna().iterrows():
            try:
                pairs.add((str(row[instrument_col]).upper(), int(row[month_col])))
            except (TypeError, ValueError):
                continue
    return sorted(pairs)


def fallback_pairs() -> list[tuple[str, int]]:
    return [(instrument, month) for instrument in DEFAULT_INSTRUMENTS for month in DEFAULT_MONTHS]


def build_candidates(config_file: Path, database_file: Path) -> pd.DataFrame:
    pairs = instruments_and_months_from_config(config_file)
    candidate_source = "config"
    if not pairs:
        pairs = fallback_pairs()
        candidate_source = "fallback"

    season = latest_season_from_database(database_file)
    if not season:
        today = dt.date.today()
        season = f"{today.year}/{today.year + 1}"
        candidate_source = f"{candidate_source}+date_fallback"

    rows = []
    for instrument, month in pairs:
        rows.append(
            {
                "instrument": instrument,
                "delivery_month": int(month),
                "season": season,
                "symbol": contract_symbol(instrument, int(month), season),
                "akshare_display_symbol": akshare_display_symbol(instrument, int(month), season),
                "candidate_source": candidate_source,
            }
        )
    return pd.DataFrame(rows).drop_duplicates(subset=["instrument", "delivery_month", "symbol"])


def build_active_candidates(config_file: Path, target_date: dt.date) -> pd.DataFrame:
    """Resolve the exact contracts required on ``target_date`` from config windows."""

    config = pd.read_excel(config_file, sheet_name=CONFIG_SHEET)
    if "enabled" in config.columns:
        config = config[config["enabled"].map(is_enabled)].copy()
    required_columns = {
        "leg1_instrument", "leg1_month", "leg2_instrument", "leg2_month",
        "window_start_month", "window_start_day", "window_end_month",
        "window_end_day",
    }
    missing = sorted(required_columns - set(config.columns))
    if missing:
        raise ValueError(f"spread config is missing active-contract columns: {missing}")

    rows: list[dict[str, object]] = []
    for rule in config.itertuples(index=False):
        season = resolve_contract_season(
            target_date,
            window_start_month=int(rule.window_start_month),
            window_start_day=int(rule.window_start_day),
            window_end_month=int(rule.window_end_month),
            window_end_day=int(rule.window_end_day),
        )
        if season is None:
            continue
        for prefix in ("leg1", "leg2"):
            instrument = str(getattr(rule, f"{prefix}_instrument")).strip().upper()
            month = int(getattr(rule, f"{prefix}_month"))
            symbol = full_contract_code(instrument, season.label, month)
            rows.append(
                {
                    "instrument": instrument,
                    "delivery_month": month,
                    "season": season.label,
                    "symbol": symbol,
                    "akshare_display_symbol": akshare_display_symbol(
                        instrument, month, season.label
                    ),
                    "candidate_source": "active_config_window",
                }
            )
    if not rows:
        return pd.DataFrame(
            columns=[
                "instrument", "delivery_month", "season", "symbol",
                "akshare_display_symbol", "candidate_source",
            ]
        )
    return (
        pd.DataFrame(rows)
        .drop_duplicates(subset=["symbol"])
        .sort_values(["instrument", "delivery_month"])
        .reset_index(drop=True)
    )


def _exact_daily_close_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Return DAILY_CLOSE rows carrying a valid full-contract identity."""

    required = {"date", "instrument", "delivery_month", "source_file", "source_column"}
    if frame.empty or not required <= set(frame.columns):
        return pd.DataFrame(columns=[*frame.columns, "contract"])
    rows = frame.copy()
    rows["date"] = pd.to_datetime(rows["date"], errors="coerce").dt.normalize()
    rows["contract"] = [
        contract_code_from_source_column(
            source_column,
            instrument=str(instrument),
            delivery_month=int(month),
        )
        if pd.notna(month)
        else None
        for source_column, instrument, month in zip(
            rows["source_column"],
            rows["instrument"],
            pd.to_numeric(rows["delivery_month"], errors="coerce"),
            strict=True,
        )
    ]
    rows["price_semantic"] = [
        infer_price_semantic(source, column).value
        for source, column in zip(
            rows["source_file"], rows["source_column"], strict=True
        )
    ]
    return rows.loc[
        rows["date"].notna()
        & rows["contract"].notna()
        & rows["price_semantic"].eq(PriceSemantic.DAILY_CLOSE.value)
    ].copy()


def per_contract_watermarks(
    existing: pd.DataFrame, candidates: pd.DataFrame
) -> dict[str, dt.date | None]:
    """Read independent exact-contract DAILY_CLOSE watermarks."""

    exact = _exact_daily_close_rows(existing)
    watermarks: dict[str, dt.date | None] = {}
    for symbol in candidates.get("symbol", pd.Series(dtype=str)).astype(str):
        matching = exact.loc[exact["contract"].eq(symbol), "date"]
        latest = matching.max() if not matching.empty else pd.NaT
        watermarks[symbol] = None if pd.isna(latest) else pd.Timestamp(latest).date()
    return watermarks


def plan_contract_refreshes(
    candidates: pd.DataFrame,
    watermarks: dict[str, dt.date | None],
    *,
    target_date: dt.date,
) -> pd.DataFrame:
    """Plan only contracts missing the target date, with a bounded overlap."""

    planned: list[dict[str, object]] = []
    lower_bound = target_date - dt.timedelta(days=NEW_CONTRACT_LOOKBACK_DAYS)
    for record in candidates.to_dict("records"):
        symbol = str(record["symbol"])
        watermark = watermarks.get(symbol)
        if watermark is not None and watermark >= target_date:
            continue
        gap_start = lower_bound if watermark is None else watermark + dt.timedelta(days=1)
        overlap_start = (
            lower_bound
            if watermark is None
            else watermark - dt.timedelta(days=LATE_ARRIVAL_LOOKBACK_DAYS)
        )
        record.update(
            {
                "watermark": watermark,
                "missing_gap_start": max(gap_start, lower_bound),
                "refresh_start": max(overlap_start, lower_bound),
                "target_date": target_date,
            }
        )
        planned.append(record)
    return pd.DataFrame(planned, columns=[*candidates.columns, "watermark", "missing_gap_start", "refresh_start", "target_date"])


def normalize_spot(raw: pd.DataFrame) -> pd.DataFrame:
    normalized = raw.copy()
    normalized.columns = [str(column).strip() for column in normalized.columns]
    return normalized


def find_symbol_column(data: pd.DataFrame) -> str | None:
    candidates = ["symbol", "代码", "合约", "合约代码", "contract", "contract_code"]
    lowered = {str(column).lower(): column for column in data.columns}
    for candidate in candidates:
        if candidate in data.columns:
            return candidate
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return data.columns[0] if len(data.columns) else None


def select_price(row: pd.Series) -> tuple[float | None, str, str]:
    for field in PRICE_FIELD_PRIORITY:
        if field not in row.index:
            continue
        value = pd.to_numeric(row[field], errors="coerce")
        if pd.notna(value) and float(value) > 0:
            return float(value), field, ""
    available = ", ".join(str(column) for column in row.index)
    return None, "", f"no usable positive price field; available_columns={available}"


def quote_time_from_row(row: pd.Series) -> tuple[str, str]:
    if "time" not in row.index:
        return "", ""
    raw_value = row.get("time")
    if pd.isna(raw_value):
        return "", "AkShare time field is empty"
    if isinstance(raw_value, (int, float)):
        value = str(int(raw_value)).zfill(6)
    else:
        value = str(raw_value).strip().replace(":", "")
    if not value or not value.isdigit() or len(value) != 6:
        return "", f"invalid AkShare time: {raw_value}"
    hour, minute, second = int(value[:2]), int(value[2:4]), int(value[4:6])
    if hour > 23 or minute > 59 or second > 59:
        return "", f"invalid AkShare time: {raw_value}"
    return f"{hour:02d}:{minute:02d}:{second:02d}", ""


def contract_is_expired(symbol: str, server_date: dt.date) -> bool:
    digits = "".join(character for character in str(symbol) if character.isdigit())
    if len(digits) < 4:
        return True
    contract_year = 2000 + int(digits[-4:-2])
    contract_month = int(digits[-2:])
    return (contract_year, contract_month) < (server_date.year, server_date.month)


def fetch_spot(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    symbols = candidates["symbol"].dropna().astype(str).unique().tolist()
    batch_symbol = ",".join(symbols)
    raw_frames: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []

    try:
        raw = ak.futures_zh_spot(symbol=batch_symbol, market="CF", adjust="0")
        if not isinstance(raw, pd.DataFrame) or raw.empty:
            raise ValueError("empty futures_zh_spot batch response")
        raw = normalize_spot(raw)
        raw["requested_batch"] = batch_symbol
        raw_frames.append(raw)
    except Exception:  # noqa: BLE001
        for symbol in symbols:
            try:
                raw = ak.futures_zh_spot(symbol=symbol, market="CF", adjust="0")
                if not isinstance(raw, pd.DataFrame) or raw.empty:
                    raise ValueError("empty futures_zh_spot response")
                raw = normalize_spot(raw)
                raw["requested_symbol"] = symbol
                raw_frames.append(raw)
            except Exception as single_exc:  # noqa: BLE001
                failures.append({"symbol": symbol, "error": f"{type(single_exc).__name__}: {single_exc}"})

    raw_spot = pd.concat(raw_frames, ignore_index=True) if raw_frames else pd.DataFrame()
    success_rows: list[dict[str, Any]] = []
    if raw_spot.empty:
        return pd.DataFrame(), pd.DataFrame(failures), raw_spot

    symbol_column = find_symbol_column(raw_spot)
    if symbol_column is None:
        failures.extend({"symbol": symbol, "error": "raw response has no symbol column"} for symbol in symbols)
        return pd.DataFrame(), pd.DataFrame(failures), raw_spot

    raw_spot["_normalized_symbol"] = raw_spot[symbol_column].astype(str).str.upper()
    candidate_map = candidates.set_index("symbol").to_dict("index")
    server_date = dt.date.today()
    for symbol in symbols:
        if contract_is_expired(symbol, server_date):
            failures.append({"symbol": symbol, "error": f"contract expired before server_date={server_date.isoformat()}"})
            continue
        display_symbol = str(candidate_map[symbol].get("akshare_display_symbol", "")).upper()
        match = raw_spot[
            (raw_spot["_normalized_symbol"] == symbol.upper())
            | (raw_spot["_normalized_symbol"] == display_symbol)
        ]
        if match.empty:
            failures.append({"symbol": symbol, "error": "symbol not found in futures_zh_spot response"})
            continue
        row = match.iloc[0]
        price, price_field, error = select_price(row)
        akshare_time, time_error = quote_time_from_row(row)
        meta = candidate_map[symbol]
        if price is None:
            failures.append({"symbol": symbol, "error": error})
            continue
        if time_error:
            failures.append({"symbol": symbol, "error": time_error})
            continue
        success_rows.append(
            {
                "date": pd.Timestamp(server_date),
                "instrument": meta["instrument"],
                "delivery_month": int(meta["delivery_month"]),
                "symbol": symbol,
                "akshare_display_symbol": meta.get("akshare_display_symbol", ""),
                "season": meta["season"],
                "price": price,
                "price_field": price_field,
                "akshare_time": akshare_time,
                "server_date": server_date.isoformat(),
                "source": "akshare_futures_zh_spot",
            }
        )

    return pd.DataFrame(success_rows), pd.DataFrame(failures), raw_spot


def fetch_daily_history(
    planned: pd.DataFrame,
    *,
    target_date: dt.date | None = None,
    start_date: dt.date | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fetch exchange-dated closes for every configured contract.

    The spot endpoint does not expose a quote business date.  Stamping its last
    price with the server date can therefore invent weekend or holiday rows and
    cannot repair a missed run.  The daily endpoint supplies the exchange date
    and lets each contract use its own bounded refresh window.  Contracts that
    already contain ``target_date`` are absent from ``planned`` and are not
    queried.
    """

    if start_date is not None:
        if "refresh_start" in planned.columns:
            raise ValueError("start_date cannot override a per-contract refresh plan")
        planned = planned.copy()
        planned["refresh_start"] = start_date
    if target_date is None and start_date is None:
        raise ValueError("target_date or legacy start_date is required")

    observations: list[pd.DataFrame] = []
    raw_frames: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []
    last_date = pd.Timestamp.max.normalize() if target_date is None else pd.Timestamp(target_date)
    for row in planned.itertuples(index=False):
        symbol = str(row.symbol)
        first_date = pd.Timestamp(row.refresh_start)
        try:
            raw = ak.futures_zh_daily_sina(symbol=symbol)
            if not isinstance(raw, pd.DataFrame) or raw.empty:
                raise ValueError("empty futures_zh_daily_sina response")
            normalized = raw.copy()
            normalized.columns = [str(column).strip().lower() for column in normalized.columns]
            if not {"date", "close"}.issubset(normalized.columns):
                raise ValueError("daily response is missing date or close")
            normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
            normalized["close"] = pd.to_numeric(normalized["close"], errors="coerce")
            normalized["requested_symbol"] = symbol
            raw_frames.append(normalized)
            selected = normalized[
                normalized["date"].ge(first_date)
                & normalized["date"].le(last_date)
                & normalized["date"].notna()
                & normalized["close"].gt(0)
            ].copy()
            if selected.empty:
                continue
            selected["instrument"] = str(row.instrument)
            selected["delivery_month"] = int(row.delivery_month)
            selected["symbol"] = symbol
            selected["season"] = str(row.season)
            selected["price"] = selected["close"]
            selected["price_field"] = "close"
            selected["source"] = "akshare_futures_zh_daily_sina"
            observations.append(
                selected[
                    [
                        "date", "instrument", "delivery_month", "symbol", "season",
                        "price", "price_field", "source",
                    ]
                ]
            )
        except Exception as exc:  # noqa: BLE001
            failures.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
    success = pd.concat(observations, ignore_index=True) if observations else pd.DataFrame()
    raw_history = pd.concat(raw_frames, ignore_index=True) if raw_frames else pd.DataFrame()
    return success, pd.DataFrame(failures), raw_history


def upsert_daily_closes(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Insert missing exact DAILY_CLOSE keys without overwriting history."""

    if incoming.empty:
        return existing.copy(), incoming.copy(), 0
    current = _exact_daily_close_rows(existing)
    incoming_exact = _exact_daily_close_rows(incoming)
    if len(incoming_exact) != len(incoming):
        raise ValueError("incoming AkShare rows are not exact DAILY_CLOSE observations")

    existing_prices: dict[tuple[pd.Timestamp, str], list[float]] = {}
    for row in current.itertuples(index=False):
        key = (pd.Timestamp(row.date).normalize(), str(row.contract))
        value = pd.to_numeric(row.price, errors="coerce")
        if pd.notna(value):
            existing_prices.setdefault(key, []).append(float(value))

    insert_indices: list[int] = []
    no_change = 0
    seen_incoming: dict[tuple[pd.Timestamp, str], float] = {}
    for index, row in incoming_exact.iterrows():
        key = (pd.Timestamp(row["date"]).normalize(), str(row["contract"]))
        value = pd.to_numeric(row["price"], errors="coerce")
        if pd.isna(value):
            raise ValueError(f"incoming daily close is invalid: {key}")
        price = float(value)
        prior_incoming = seen_incoming.get(key)
        if prior_incoming is not None and not math.isclose(
            prior_incoming, price, rel_tol=0.0, abs_tol=1e-9
        ):
            raise HistoricalDailyCloseConflict(
                f"provider returned conflicting daily closes for {key[1]} on {key[0].date()}"
            )
        seen_incoming[key] = price
        existing_values = existing_prices.get(key, [])
        if existing_values:
            if all(
                math.isclose(previous, price, rel_tol=0.0, abs_tol=1e-9)
                for previous in existing_values
            ):
                no_change += 1
                continue
            raise HistoricalDailyCloseConflict(
                f"historical daily close conflict for {key[1]} on {key[0].date()}"
            )
        insert_indices.append(index)
        existing_prices[key] = [price]

    to_insert = incoming.loc[insert_indices].copy()
    if to_insert.empty:
        return existing.copy(), to_insert, no_change
    combined = pd.concat([existing, to_insert], ignore_index=True)
    sort_columns = [
        column
        for column in ("date", "instrument", "delivery_month", "source_column")
        if column in combined.columns
    ]
    combined["date"] = pd.to_datetime(combined["date"], errors="coerce")
    combined = combined.sort_values(sort_columns).reset_index(drop=True)
    return combined, to_insert, no_change


def target_date_completeness(
    candidate: pd.DataFrame,
    required_symbols: set[str],
    *,
    target_date: dt.date,
) -> tuple[set[str], set[str]]:
    """Measure target-date coverage from exact DAILY_CLOSE keys only."""

    exact = _exact_daily_close_rows(candidate)
    present = set(
        exact.loc[
            exact["date"].eq(pd.Timestamp(target_date)), "contract"
        ].astype(str)
    )
    successful = required_symbols & present
    return successful, required_symbols - present


def build_price_long_rows(success: pd.DataFrame, existing_columns: list[str]) -> pd.DataFrame:
    now_text = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows: list[dict[str, object]] = []
    for _, row in success.iterrows():
        record = {
            "date": pd.to_datetime(row["date"]),
            "instrument": row["instrument"],
            "instrument_cn": INSTRUMENT_CN.get(str(row["instrument"]).upper(), ""),
            "delivery_month": int(row["delivery_month"]),
            "price": row["price"],
            "source_column": f"{row['symbol']}:{row['price_field']}",
            "source_file": row["source"],
            "updated_at": now_text,
            "status": "success",
            "error": "",
        }
        rows.append({column: record.get(column, "") for column in existing_columns})
    return pd.DataFrame(rows, columns=existing_columns)


def backup_price_long(price_file: Path, backups_dir: Path, timestamp: str) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    backup_file = backups_dir / f"historical_price_long_{timestamp}.xlsx"
    shutil.copy2(price_file, backup_file)
    return backup_file


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def write_report(
    report_file: Path,
    *,
    success: pd.DataFrame,
    failures: pd.DataFrame,
    raw_spot: pd.DataFrame,
    to_append: pd.DataFrame,
    existing_today_rows: pd.DataFrame,
) -> None:
    report_file.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(report_file, engine="openpyxl") as writer:
        success.to_excel(writer, sheet_name="success", index=False)
        failures.to_excel(writer, sheet_name="failures", index=False)
        raw_spot.to_excel(writer, sheet_name="raw_spot", index=False)
        to_append.to_excel(writer, sheet_name="to_append", index=False)
        existing_today_rows.to_excel(writer, sheet_name="existing_today_rows", index=False)


def parse_business_end_date(value: str) -> dt.date:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise argparse.ArgumentTypeError("business end date must use YYYY-MM-DD")
    try:
        selected = dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("business end date is invalid") from None
    if selected > dt.date.today():
        raise argparse.ArgumentTypeError("future business end date is forbidden")
    return selected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill missing exchange-dated AkShare futures closes.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and report only; do not write historical_price_long.xlsx.")
    parser.add_argument(
        "--min-success-ratio",
        type=float,
        default=1.0,
        help="Minimum required successful contract ratio. Default 1.0 requires every contract.",
    )
    parser.add_argument("--skip-backup", action="store_true", help="Skip local price_long backup when the parent transaction already backed it up.")
    parser.add_argument("--result-json", type=Path, help="Write a machine-readable update result to this path.")
    parser.add_argument(
        "--target-business-date",
        type=parse_business_end_date,
        default=dt.date.today(),
        help="Exact business date whose active DAILY_CLOSE contract set must be complete.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = project_root()
    data_dir = root / "01_data"
    logs_dir = root / "10_logs"
    output_dir = root / "06_outputs" / "daily_price_updates"
    backups_dir = data_dir / "backups"
    price_file = data_dir / "historical_price_long.xlsx"
    config_file = root / "02_configs" / "historical_spread_config.xlsx"
    database_file = data_dir / "historical_spread_database.xlsx"
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    log_file = logs_dir / f"update_price_long_from_akshare_{dt.datetime.now().strftime('%Y%m%d')}.log"
    report_file = output_dir / f"akshare_price_update_{timestamp}.xlsx"
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(log_file)
    result_payload: dict[str, object] = {
        "status": "failed",
        "server_date": dt.date.today().isoformat(),
        "source": "akshare_futures_zh_daily_sina",
        "required_contracts": 0,
        "success_contracts": 0,
        "failure_contracts": 0,
        "failed_contracts": [],
        "job_execution_status": "FAILED",
        "endpoint_requested_contracts": 0,
        "endpoint_success_contracts": 0,
        "endpoint_failure_contracts": 0,
        "endpoint_skipped_current_contracts": 0,
        "target_business_date": args.target_business_date.isoformat(),
        "requested_end_date": args.target_business_date.isoformat(),
        "effective_end_date": args.target_business_date.isoformat(),
        "target_date_data_completeness": "MISSING",
        "target_required_contract_keys": [],
        "target_present_contract_keys": [],
        "target_missing_contract_keys": [],
        "to_append_rows": 0,
        "idempotent_no_change_rows": 0,
        "overwritten_rows": 0,
        "price_long_written": False,
        "latest_date": "",
        "report_file": str(report_file),
        "backup_file": "",
        "error_message": "",
    }

    try:
        if not 0 < args.min_success_ratio <= 1:
            raise ValueError("--min-success-ratio must be greater than 0 and at most 1")
        logger.info("update_price_long_from_akshare started dry_run=%s", args.dry_run)
        for required in [price_file, config_file, database_file]:
            if not required.exists():
                raise FileNotFoundError(f"required file not found: {required}")

        existing = pd.read_excel(price_file, sheet_name=PRICE_LONG_SHEET)
        existing["date"] = pd.to_datetime(existing["date"], errors="coerce")
        candidates = build_active_candidates(config_file, args.target_business_date)
        if candidates.empty:
            raise ValueError(
                f"no active Domestic Spread contracts for {args.target_business_date}"
            )
        watermarks = per_contract_watermarks(existing, candidates)
        planned = plan_contract_refreshes(
            candidates, watermarks, target_date=args.target_business_date
        )
        success, failures, raw_spot = fetch_daily_history(
            planned, target_date=args.target_business_date
        )
        endpoint_failed = (
            set(failures["symbol"].astype(str)) if not failures.empty else set()
        )
        endpoint_requested = set(planned.get("symbol", pd.Series(dtype=str)).astype(str))
        endpoint_success = endpoint_requested - endpoint_failed
        job_execution_status = "SUCCESS" if not endpoint_failed else "FAILED"
        incoming = (
            build_price_long_rows(success, list(existing.columns))
            if not success.empty
            else pd.DataFrame(columns=existing.columns)
        )
        combined, to_append, no_change_rows = upsert_daily_closes(existing, incoming)
        required_symbols = set(candidates["symbol"].astype(str))
        present_symbols, missing_symbols = target_date_completeness(
            combined,
            required_symbols,
            target_date=args.target_business_date,
        )
        required_contracts = len(required_symbols)
        success_contracts = len(present_symbols)
        failed_contracts = sorted(missing_symbols)
        failure_contracts = len(missing_symbols)
        success_ratio = success_contracts / required_contracts if required_contracts else 0.0
        data_completeness = "COMPLETE" if not missing_symbols else "PARTIAL"
        existing_today_rows = existing[
            existing["date"].eq(pd.Timestamp(args.target_business_date))
        ].copy()
        write_report(
            report_file,
            success=success,
            failures=failures,
            raw_spot=raw_spot,
            to_append=to_append,
            existing_today_rows=existing_today_rows,
        )

        overwritten_rows = 0
        backup_file = ""
        integrity_ok = (
            required_contracts > 0
            and job_execution_status == "SUCCESS"
            and success_ratio >= args.min_success_ratio
            and (args.min_success_ratio < 1.0 or failure_contracts == 0)
        )
        if not integrity_ok:
            raise RuntimeError(
                "contract completeness check failed: "
                f"success={success_contracts}, required={required_contracts}, "
                f"failures={failure_contracts}, job_execution={job_execution_status}, "
                f"min_success_ratio={args.min_success_ratio}"
            )

        if not args.dry_run and not to_append.empty:
            if not args.skip_backup:
                backup_file = str(backup_price_long(price_file, backups_dir, timestamp))
            tmp_price_file = price_file.with_name("historical_price_long.tmp.xlsx")
            if tmp_price_file.exists():
                tmp_price_file.unlink()
            with pd.ExcelWriter(tmp_price_file, engine="openpyxl") as writer:
                combined.to_excel(writer, sheet_name=PRICE_LONG_SHEET, index=False)
            os.replace(tmp_price_file, price_file)

        resulting_latest = pd.to_datetime(combined["date"], errors="coerce").max()
        latest_date = "" if pd.isna(resulting_latest) else pd.Timestamp(resulting_latest).strftime("%Y-%m-%d")
        earliest_inserted = (
            ""
            if to_append.empty
            else pd.to_datetime(to_append["date"], errors="coerce").min().strftime("%Y-%m-%d")
        )
        result_payload.update(
            {
                "status": "success",
                "required_contracts": required_contracts,
                "success_contracts": success_contracts,
                "failure_contracts": failure_contracts,
                "failed_contracts": failed_contracts,
                "job_execution_status": job_execution_status,
                "endpoint_requested_contracts": len(endpoint_requested),
                "endpoint_success_contracts": len(endpoint_success),
                "endpoint_failure_contracts": len(endpoint_failed),
                "endpoint_skipped_current_contracts": required_contracts - len(endpoint_requested),
                "target_date_data_completeness": data_completeness,
                "target_required_contract_keys": sorted(required_symbols),
                "target_present_contract_keys": sorted(present_symbols),
                "target_missing_contract_keys": failed_contracts,
                "to_append_rows": int(len(to_append)),
                "idempotent_no_change_rows": int(no_change_rows),
                "overwritten_rows": overwritten_rows,
                "price_long_written": bool(not args.dry_run and not to_append.empty),
                "latest_date": latest_date,
                "earliest_inserted_date": earliest_inserted,
                "backup_file": backup_file,
            }
        )
        logger.info("server_date=%s", result_payload["server_date"])
        logger.info("source=%s", result_payload["source"])
        logger.info("candidate_contracts=%s", required_contracts)
        logger.info("success_contracts=%s", success_contracts)
        logger.info("failure_contracts=%s", failure_contracts)
        logger.info("failed_contracts=%s", failed_contracts)
        logger.info("job_execution_status=%s", job_execution_status)
        logger.info("target_business_date=%s", args.target_business_date)
        logger.info("target_date_data_completeness=%s", data_completeness)
        logger.info("per_contract_watermarks=%s", watermarks)
        logger.info(
            "contract_refresh_plan=%s",
            planned[["symbol", "watermark", "missing_gap_start", "refresh_start"]].to_dict("records"),
        )
        logger.info("to_append_rows=%s", len(to_append))
        logger.info("idempotent_no_change_rows=%s", no_change_rows)
        logger.info("overwritten_rows=%s", overwritten_rows)
        logger.info("price_long_written=%s", result_payload["price_long_written"])
        logger.info("latest_date=%s", latest_date)
        logger.info("report_file=%s", report_file)
        logger.info("backup_file=%s", backup_file)
        logger.info("unique_key=full_contract_identity+date+DAILY_CLOSE")

        print(f"candidate_contracts: {required_contracts}")
        print(f"success_contracts: {success_contracts}")
        print(f"failure_contracts: {failure_contracts}")
        print(f"job_execution_status: {job_execution_status}")
        print(f"target_date_data_completeness: {data_completeness}")
        if not failures.empty:
            print("failed_symbols: " + ", ".join(failures["symbol"].astype(str).tolist()))
        print(f"to_append_rows: {len(to_append)}")
        print(f"overwritten_rows: {overwritten_rows}")
        print(f"latest_date: {latest_date}")
        print(f"report_file: {report_file}")
        print(f"log_file: {log_file}")
        if backup_file:
            print(f"backup_file: {backup_file}")
        if args.dry_run:
            print("dry_run: true")
        if args.result_json:
            atomic_write_json(args.result_json, result_payload)
        return 0
    except Exception as exc:  # noqa: BLE001
        result_payload["error_message"] = f"{type(exc).__name__}: {exc}"
        if "candidates" in locals():
            result_payload["required_contracts"] = int(len(candidates))
            result_payload["target_required_contract_keys"] = sorted(
                candidates["symbol"].astype(str).tolist()
            )
        if "present_symbols" in locals():
            result_payload["success_contracts"] = int(len(present_symbols))
            result_payload["target_present_contract_keys"] = sorted(present_symbols)
        if "missing_symbols" in locals():
            result_payload["failure_contracts"] = int(len(missing_symbols))
            result_payload["failed_contracts"] = sorted(missing_symbols)
            result_payload["target_missing_contract_keys"] = sorted(missing_symbols)
            result_payload["target_date_data_completeness"] = (
                "COMPLETE" if not missing_symbols else "PARTIAL"
            )
        if "failures" in locals():
            endpoint_failed = (
                failures["symbol"].astype(str).tolist() if not failures.empty else []
            )
            result_payload["endpoint_failure_contracts"] = len(endpoint_failed)
            result_payload["job_execution_status"] = (
                "SUCCESS" if not endpoint_failed else "FAILED"
            )
        if "planned" in locals():
            result_payload["endpoint_requested_contracts"] = int(len(planned))
        if "to_append" in locals():
            result_payload["to_append_rows"] = int(len(to_append))
        logger.exception("update_price_long_from_akshare failed: %s", exc)
        if args.result_json:
            atomic_write_json(args.result_json, result_payload)
        print(f"update_price_long_from_akshare failed: {type(exc).__name__}: {exc}")
        print(f"log_file: {log_file}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

