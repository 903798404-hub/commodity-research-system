from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd


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


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


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
    candidates: pd.DataFrame, *, start_date: dt.date
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fetch exchange-dated closes for every configured contract.

    The spot endpoint does not expose a quote business date.  Stamping its last
    price with the server date can therefore invent weekend or holiday rows and
    cannot repair a missed run.  The daily endpoint supplies the exchange date
    and lets one run backfill every real trading day after ``start_date``.
    """

    observations: list[pd.DataFrame] = []
    raw_frames: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []
    first_date = pd.Timestamp(start_date)
    for row in candidates.itertuples(index=False):
        symbol = str(row.symbol)
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
        "to_append_rows": 0,
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
        candidates = build_candidates(config_file, database_file)
        latest_existing = existing["date"].max()
        start_date = (
            dt.date.today()
            if pd.isna(latest_existing)
            else (pd.Timestamp(latest_existing) + pd.Timedelta(days=1)).date()
        )
        success, failures, raw_spot = fetch_daily_history(
            candidates, start_date=start_date
        )
        required_contracts = int(len(candidates))
        failed_symbols = set(failures["symbol"].astype(str)) if not failures.empty else set()
        success_contracts = required_contracts - len(failed_symbols)
        failed_contracts = failures["symbol"].astype(str).tolist() if not failures.empty else []
        failure_contracts = int(len(failed_contracts))
        success_ratio = success_contracts / required_contracts if required_contracts else 0.0
        to_append = build_price_long_rows(success, list(existing.columns)) if not success.empty else pd.DataFrame(columns=existing.columns)
        unique_cols = ["date", "instrument", "delivery_month"]
        existing_today_rows = existing[
            existing["date"].isin(to_append["date"] if not to_append.empty else [])
            & existing["instrument"].isin(to_append["instrument"] if not to_append.empty else [])
            & existing["delivery_month"].isin(to_append["delivery_month"] if not to_append.empty else [])
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
            and success_ratio >= args.min_success_ratio
            and (args.min_success_ratio < 1.0 or failure_contracts == 0)
        )
        if not integrity_ok:
            raise RuntimeError(
                "contract completeness check failed: "
                f"success={success_contracts}, required={required_contracts}, "
                f"failures={failure_contracts}, min_success_ratio={args.min_success_ratio}"
            )

        if not args.dry_run and not to_append.empty:
            if not args.skip_backup:
                backup_file = str(backup_price_long(price_file, backups_dir, timestamp))
            existing_keyed = existing.set_index(unique_cols, drop=False)
            append_keyed = to_append.set_index(unique_cols, drop=False)
            overwritten_rows = int(existing_keyed.index.isin(append_keyed.index).sum())
            combined = pd.concat(
                [
                    existing_keyed[~existing_keyed.index.isin(append_keyed.index)].reset_index(drop=True),
                    to_append,
                ],
                ignore_index=True,
            )
            combined = combined.sort_values(["date", "instrument", "delivery_month"]).reset_index(drop=True)
            tmp_price_file = price_file.with_name("historical_price_long.tmp.xlsx")
            if tmp_price_file.exists():
                tmp_price_file.unlink()
            with pd.ExcelWriter(tmp_price_file, engine="openpyxl") as writer:
                combined.to_excel(writer, sheet_name=PRICE_LONG_SHEET, index=False)
            os.replace(tmp_price_file, price_file)

        resulting_latest = pd.concat(
            [existing["date"], to_append["date"] if not to_append.empty else pd.Series(dtype="datetime64[ns]")],
            ignore_index=True,
        ).max()
        latest_date = "" if pd.isna(resulting_latest) else pd.Timestamp(resulting_latest).strftime("%Y-%m-%d")
        result_payload.update(
            {
                "status": "success",
                "required_contracts": required_contracts,
                "success_contracts": success_contracts,
                "failure_contracts": failure_contracts,
                "failed_contracts": failed_contracts,
                "to_append_rows": int(len(to_append)),
                "overwritten_rows": overwritten_rows,
                "price_long_written": bool(not args.dry_run and not to_append.empty),
                "latest_date": latest_date,
                "backup_file": backup_file,
            }
        )
        logger.info("server_date=%s", result_payload["server_date"])
        logger.info("source=%s", result_payload["source"])
        logger.info("candidate_contracts=%s", required_contracts)
        logger.info("success_contracts=%s", success_contracts)
        logger.info("failure_contracts=%s", failure_contracts)
        logger.info("failed_contracts=%s", failed_contracts)
        logger.info("backfill_start_date=%s", start_date.isoformat())
        logger.info("to_append_rows=%s", len(to_append))
        logger.info("overwritten_rows=%s", overwritten_rows)
        logger.info("price_long_written=%s", result_payload["price_long_written"])
        logger.info("latest_date=%s", latest_date)
        logger.info("report_file=%s", report_file)
        logger.info("backup_file=%s", backup_file)
        logger.info("unique_key=date+instrument+delivery_month")

        print(f"candidate_contracts: {required_contracts}")
        print(f"success_contracts: {success_contracts}")
        print(f"failure_contracts: {failure_contracts}")
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
        if "success" in locals():
            result_payload["success_contracts"] = int(len(success))
        if "failures" in locals():
            failed_contracts = failures["symbol"].astype(str).tolist() if not failures.empty else []
            result_payload["failure_contracts"] = int(len(failed_contracts))
            result_payload["failed_contracts"] = failed_contracts
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

