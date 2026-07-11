from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd


PRICE_FIELDS = ["close", "settle"]


def safe_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def is_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = safe_text(value).lower()
    return text in {"1", "true", "yes", "y", "是", "启用"}


def setup_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("seasonal_spread_history")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def normalize_daily(data: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if not isinstance(data, pd.DataFrame) or data.empty:
        raise ValueError(f"{symbol} daily data is empty")

    missing = [field for field in ["date", "close"] if field not in data.columns]
    if missing:
        raise ValueError(f"{symbol} daily data missing columns: {', '.join(missing)}")

    keep_columns = [column for column in ["date", *PRICE_FIELDS] if column in data.columns]
    normalized = data.loc[:, keep_columns].copy()
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    normalized = normalized.dropna(subset=["date"])

    for field in PRICE_FIELDS:
        if field in normalized.columns:
            normalized[field] = pd.to_numeric(normalized[field], errors="coerce")

    normalized = normalized.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    if normalized.empty:
        raise ValueError(f"{symbol} daily data has no valid dates")
    return normalized


def fetch_daily(symbol: str, logger: logging.Logger) -> pd.DataFrame:
    logger.info("fetching daily data: symbol=%s", symbol)
    data = ak.futures_zh_daily_sina(symbol=symbol)
    normalized = normalize_daily(data, symbol)
    logger.info("daily fetch succeeded: symbol=%s rows=%s", symbol, len(normalized))
    return normalized


def build_one_row(row: pd.Series, logger: logging.Logger) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any] | None]:
    spread_group = safe_text(row.get("spread_group"))
    season = safe_text(row.get("season"))
    leg1_symbol = safe_text(row.get("leg1_symbol")).upper()
    leg2_symbol = safe_text(row.get("leg2_symbol")).upper()
    leg1_price_field = safe_text(row.get("leg1_price_field")) or "close"
    leg2_price_field = safe_text(row.get("leg2_price_field")) or "close"
    formula = safe_text(row.get("formula")) or "leg1-leg2"
    window_start = pd.to_datetime(row.get("window_start"), errors="coerce")
    window_end = pd.to_datetime(row.get("window_end"), errors="coerce")

    base_failure = {
        "spread_group": spread_group,
        "season": season,
        "leg1_symbol": leg1_symbol,
        "leg2_symbol": leg2_symbol,
    }

    try:
        if formula != "leg1-leg2":
            raise ValueError(f"unsupported formula: {formula}")
        if pd.isna(window_start) or pd.isna(window_end):
            raise ValueError("window_start or window_end is invalid")
        if not leg1_symbol or not leg2_symbol:
            raise ValueError("leg1_symbol or leg2_symbol is missing")

        leg1 = fetch_daily(leg1_symbol, logger)
        leg2 = fetch_daily(leg2_symbol, logger)

        raw_leg1 = leg1.copy()
        raw_leg1.insert(0, "leg", "leg1")
        raw_leg1.insert(0, "symbol", leg1_symbol)
        raw_leg1.insert(0, "season", season)
        raw_leg1.insert(0, "spread_group", spread_group)

        raw_leg2 = leg2.copy()
        raw_leg2.insert(0, "leg", "leg2")
        raw_leg2.insert(0, "symbol", leg2_symbol)
        raw_leg2.insert(0, "season", season)
        raw_leg2.insert(0, "spread_group", spread_group)
        raw_daily = pd.concat([raw_leg1, raw_leg2], ignore_index=True)

        leg1 = leg1.rename(
            columns={
                leg1_price_field: "leg1_price",
                "close": "leg1_close",
                "settle": "leg1_settle",
            }
        )
        leg2 = leg2.rename(
            columns={
                leg2_price_field: "leg2_price",
                "close": "leg2_close",
                "settle": "leg2_settle",
            }
        )

        merged = leg1.merge(leg2, on="date", how="inner")
        if "leg1_price" not in merged.columns or "leg2_price" not in merged.columns:
            raise ValueError("configured price field is not available after merge")

        merged = merged[(merged["date"] >= window_start) & (merged["date"] <= window_end)].copy()
        if merged.empty:
            raise ValueError("no overlapping daily data inside configured window")

        merged["spread_group"] = spread_group
        merged["season"] = season
        merged["leg1_symbol"] = leg1_symbol
        merged["leg2_symbol"] = leg2_symbol
        merged["leg1_price_field"] = leg1_price_field
        merged["leg2_price_field"] = leg2_price_field
        merged["window_start"] = window_start
        merged["window_end"] = window_end
        merged["spread_value"] = merged["leg1_price"] - merged["leg2_price"]
        merged["calendar_offset"] = (merged["date"] - window_start).dt.days
        merged["month_day"] = merged["date"].dt.strftime("%m-%d")

        output_columns = [
            "spread_group",
            "season",
            "date",
            "calendar_offset",
            "month_day",
            "leg1_symbol",
            "leg2_symbol",
            "leg1_price",
            "leg2_price",
            "spread_value",
            "leg1_close",
            "leg2_close",
            "leg1_settle",
            "leg2_settle",
            "leg1_price_field",
            "leg2_price_field",
            "window_start",
            "window_end",
        ]
        existing_columns = [column for column in output_columns if column in merged.columns]
        return merged.loc[:, existing_columns], raw_daily, None
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "season failed: spread_group=%s season=%s leg1=%s leg2=%s error=%s",
            spread_group,
            season,
            leg1_symbol,
            leg2_symbol,
            exc,
        )
        return (
            pd.DataFrame(),
            pd.DataFrame(),
            {
                **base_failure,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    config_file = project_root / "01_data" / "seasonal_spread_config.xlsx"
    output_file = project_root / "01_data" / "seasonal_spread_history.xlsx"
    logs_dir = project_root / "10_logs"
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    log_file = logs_dir / f"seasonal_spread_history_{timestamp}.log"

    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(log_file)
    logger.info("seasonal spread history build started")

    try:
        config = pd.read_excel(config_file, sheet_name="config")
        active_config = config[config["enabled"].map(is_enabled)].copy()
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to read config: %s", exc)
        print(f"Failed to read config. See log: {log_file}")
        return 1

    history_frames: list[pd.DataFrame] = []
    raw_frames: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []

    for _, row in active_config.iterrows():
        history, raw_daily, failure = build_one_row(row, logger)
        if not history.empty:
            history_frames.append(history)
        if not raw_daily.empty:
            raw_frames.append(raw_daily)
        if failure:
            failures.append(failure)

    history_df = pd.concat(history_frames, ignore_index=True) if history_frames else pd.DataFrame()
    raw_df = pd.concat(raw_frames, ignore_index=True) if raw_frames else pd.DataFrame()
    failures_df = pd.DataFrame(failures)

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        history_df.to_excel(writer, sheet_name="spread_history", index=False)
        raw_df.to_excel(writer, sheet_name="raw_daily", index=False)
        failures_df.to_excel(writer, sheet_name="failures", index=False)
        active_config.to_excel(writer, sheet_name="config_used", index=False)

    logger.info(
        "seasonal spread history build finished: success=%s failure=%s output=%s",
        active_config["season"].nunique() - len(failures_df),
        len(failures_df),
        output_file,
    )
    print(f"Seasonal spread history generated: {output_file}")
    print(f"Seasonal spread history log generated: {log_file}")
    print(f"Success seasons: {active_config['season'].nunique() - len(failures_df)}")
    print(f"Failed seasons: {len(failures_df)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

