from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "03_src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agri_research_agent.data_sources.tankan.domestic_spread import (  # noqa: E402
    contract_code_from_source_column,
    full_contract_code,
    normalize_full_contract_code,
    resolve_contract_season,
    window_contract_code,
)
from agri_research_agent.pipelines.domestic_spread_integrity import (  # noqa: E402
    select_canonical_historical_prices,
)


SPREAD_COLUMNS = [
    "date",
    "spread_group",
    "spread_name",
    "leg1_instrument",
    "leg1_month",
    "leg1_contract",
    "leg1_price",
    "leg2_instrument",
    "leg2_month",
    "leg2_contract",
    "leg2_price",
    "spread_value",
    "season",
    "calendar_offset",
    "month_day",
    "status",
    "error",
    "updated_at",
]


def setup_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("calculate_historical_spreads")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def is_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y", "是", "启用"}


def season_for_date(
    date_value: pd.Timestamp,
    *,
    window_start_month: int,
    window_start_day: int,
    window_end_month: int,
    window_end_day: int,
) -> tuple[str, pd.Timestamp, pd.Timestamp] | None:
    resolved = resolve_contract_season(
        date_value,
        window_start_month=window_start_month,
        window_start_day=window_start_day,
        window_end_month=window_end_month,
        window_end_day=window_end_day,
    )
    if resolved is None:
        return None
    return (
        resolved.label,
        pd.Timestamp(resolved.start_date),
        pd.Timestamp(resolved.end_date),
    )


def prepare_leg(price_long: pd.DataFrame, instrument: str, month: int, prefix: str) -> pd.DataFrame:
    canonical = select_canonical_historical_prices(price_long)
    leg = canonical[
        (canonical["instrument"] == instrument)
        & (pd.to_numeric(canonical["delivery_month"], errors="coerce") == int(month))
    ].copy()
    if "source_column" in leg.columns:
        leg[f"{prefix}_contract"] = leg["source_column"].map(
            lambda value: contract_code_from_source_column(
                value, instrument=instrument, delivery_month=month
            )
        )
    else:
        leg[f"{prefix}_contract"] = None
    keep = ["date", "price", "status", "error", f"{prefix}_contract"]
    leg = leg.loc[:, keep].rename(
        columns={
            "price": f"{prefix}_price",
            "status": f"{prefix}_source_status",
            "error": f"{prefix}_source_error",
        }
    )
    return leg


def add_canonical_contract_identities(
    spread_long: pd.DataFrame, *, window_start_month: int | None = None,
) -> pd.DataFrame:
    """Add missing full-contract identities without changing existing business fields.

    Explicit producer identities remain authoritative.  Legacy rows that predate
    those identity columns are completed from the producer's already-resolved
    season and configured leg identity, using the same canonical resolver as the
    live Tankan source.
    """

    required = {
        "season",
        "leg1_instrument",
        "leg1_month",
        "leg2_instrument",
        "leg2_month",
    }
    missing = sorted(required - set(spread_long.columns))
    if missing:
        raise ValueError(f"Domestic Spread identity source columns missing: {missing}")

    result = spread_long.copy()
    for prefix in ("leg1", "leg2"):
        contract_column = f"{prefix}_contract"
        instrument_column = f"{prefix}_instrument"
        month_column = f"{prefix}_month"
        if contract_column not in result.columns:
            result[contract_column] = None

        completed: list[str] = []
        for row in result[
            [contract_column, instrument_column, month_column, "season"]
        ].itertuples(index=False, name=None):
            existing, instrument, month, season = row
            if pd.notna(existing) and str(existing).strip():
                completed.append(normalize_full_contract_code(existing))
            else:
                completed.append(
                    window_contract_code(
                        str(instrument), str(season), int(month),
                        window_start_month=window_start_month,
                    ) if window_start_month is not None else
                    full_contract_code(str(instrument), str(season), int(month))
                )
        result[contract_column] = completed
    return result


def calculate_one_spread(config: pd.Series, price_long: pd.DataFrame, updated_at: str) -> tuple[pd.DataFrame, dict[str, Any] | None]:
    spread_name = str(config["spread_name"])
    leg1_instrument = str(config["leg1_instrument"])
    leg2_instrument = str(config["leg2_instrument"])
    leg1_month = int(config["leg1_month"])
    leg2_month = int(config["leg2_month"])
    window_start_month = int(config.get("window_start_month", config.get("season_start_month", 10)))
    window_start_day = int(config.get("window_start_day", 1))
    window_end_month = int(config.get("window_end_month", config.get("season_end_month", 4)))
    window_end_day = int(config.get("window_end_day", 30))

    if str(config["formula"]) != "leg1-leg2":
        return pd.DataFrame(columns=SPREAD_COLUMNS), {
            "spread_name": spread_name,
            "error": f"unsupported formula: {config['formula']}",
        }

    leg1 = prepare_leg(price_long, leg1_instrument, leg1_month, "leg1")
    leg2 = prepare_leg(price_long, leg2_instrument, leg2_month, "leg2")
    if leg1.empty or leg2.empty:
        return pd.DataFrame(columns=SPREAD_COLUMNS), {
            "spread_name": spread_name,
            "error": "one or both legs have no source price rows",
            "leg1_rows": len(leg1),
            "leg2_rows": len(leg2),
        }

    merged = leg1.merge(leg2, on="date", how="outer").sort_values("date")
    merged["date"] = pd.to_datetime(merged["date"], errors="coerce")
    merged = merged.dropna(subset=["date"])

    season_info = merged["date"].map(
        lambda value: season_for_date(
            value,
            window_start_month=window_start_month,
            window_start_day=window_start_day,
            window_end_month=window_end_month,
            window_end_day=window_end_day,
        )
    )
    merged = merged[season_info.notna()].copy()
    if merged.empty:
        return pd.DataFrame(columns=SPREAD_COLUMNS), {
            "spread_name": spread_name,
            "error": "no rows in configured season window",
        }

    season_info = merged["date"].map(
        lambda value: season_for_date(
            value,
            window_start_month=window_start_month,
            window_start_day=window_start_day,
            window_end_month=window_end_month,
            window_end_day=window_end_day,
        )
    )
    merged["season"] = season_info.map(lambda value: value[0] if value else None)
    merged["season_start"] = season_info.map(lambda value: value[1] if value else pd.NaT)
    merged["season_end"] = season_info.map(lambda value: value[2] if value else pd.NaT)
    merged["calendar_offset"] = (merged["date"] - merged["season_start"]).dt.days
    merged["month_day"] = merged["date"].dt.strftime("%m-%d")

    merged["leg1_price"] = pd.to_numeric(merged["leg1_price"], errors="coerce")
    merged["leg2_price"] = pd.to_numeric(merged["leg2_price"], errors="coerce")

    missing_mask = merged["leg1_price"].isna() | merged["leg2_price"].isna()
    zero_mask = (merged["leg1_price"] == 0) | (merged["leg2_price"] == 0)
    source_zero_mask = (merged["leg1_source_status"] == "suspicious_zero") | (merged["leg2_source_status"] == "suspicious_zero")

    merged["status"] = "success"
    merged.loc[missing_mask, "status"] = "missing_price"
    merged.loc[zero_mask | source_zero_mask, "status"] = "suspicious_zero"
    merged["error"] = ""
    merged.loc[missing_mask, "error"] = "one or both leg prices are missing"
    merged.loc[zero_mask | source_zero_mask, "error"] = "one or both leg prices are zero"
    merged["spread_value"] = merged["leg1_price"] - merged["leg2_price"]
    merged.loc[merged["status"] != "success", "spread_value"] = pd.NA

    merged["spread_group"] = config["spread_group"]
    merged["spread_name"] = spread_name
    merged["leg1_instrument"] = leg1_instrument
    merged["leg1_month"] = leg1_month
    merged["leg2_instrument"] = leg2_instrument
    merged["leg2_month"] = leg2_month
    merged["updated_at"] = updated_at

    # A positive price for the wrong listed year is not a valid spread leg.
    for prefix in ("leg1", "leg2"):
        column = f"{prefix}_contract"
        expected = [
            window_contract_code(
                str(config[f"{prefix}_instrument"]), str(season),
                int(config[f"{prefix}_month"]), window_start_month=window_start_month,
            )
            for season in merged["season"]
        ]
        mismatch = merged[column].notna() & merged[column].ne(pd.Series(expected, index=merged.index))
        merged.loc[mismatch, "status"] = "wrong_contract"
        merged.loc[mismatch, "error"] = "leg contract differs from configured observation window"
        merged.loc[mismatch, "spread_value"] = pd.NA
    return add_canonical_contract_identities(
        merged, window_start_month=window_start_month,
    ).loc[:, SPREAD_COLUMNS], None


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    price_file = project_root / "01_data" / "historical_price_long.xlsx"
    config_file = project_root / "02_configs" / "historical_spread_config.xlsx"
    output_file = project_root / "01_data" / "historical_spread_database.xlsx"
    parquet_file = project_root / "01_data" / "historical_spread_database.parquet"
    tmp_output_file = project_root / "01_data" / "historical_spread_database.tmp.xlsx"
    tmp_parquet_file = project_root / "01_data" / "historical_spread_database.tmp.parquet"
    logs_dir = project_root / "10_logs"
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    log_file = logs_dir / f"calculate_historical_spreads_{timestamp}.log"
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(log_file)
    updated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        price_long = pd.read_excel(price_file, sheet_name="price_long")
        config = pd.read_excel(config_file, sheet_name="spread_config")
        price_long["date"] = pd.to_datetime(price_long["date"], errors="coerce")
        active_config = config[config["enabled"].map(is_enabled)].copy()

        frames: list[pd.DataFrame] = []
        failures: list[dict[str, Any]] = []
        for _, row in active_config.iterrows():
            try:
                frame, failure = calculate_one_spread(row, price_long, updated_at)
                if not frame.empty:
                    frames.append(frame)
                if failure:
                    failures.append(failure)
                    logger.warning("spread failed: %s", failure)
            except Exception as exc:  # noqa: BLE001
                failure = {"spread_name": row.get("spread_name", ""), "error": f"{type(exc).__name__}: {exc}"}
                failures.append(failure)
                logger.exception("spread failed: %s", failure)

        spread_long = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=SPREAD_COLUMNS)
        failures_df = pd.DataFrame(failures)

        status_counts = spread_long["status"].value_counts(dropna=False).to_dict() if not spread_long.empty else {}
        summary_rows: list[dict[str, Any]] = [
            {"metric": "price_long_rows", "value": len(price_long)},
            {"metric": "spread_config_count", "value": len(active_config)},
            {"metric": "spread_long_rows", "value": len(spread_long)},
            {"metric": "success_rows", "value": int(status_counts.get("success", 0))},
            {"metric": "missing_price_rows", "value": int(status_counts.get("missing_price", 0))},
            {"metric": "suspicious_zero_rows", "value": int(status_counts.get("suspicious_zero", 0))},
            {"metric": "failure_rows", "value": len(failures_df)},
        ]
        if not spread_long.empty:
            success_by_spread = (
                spread_long[spread_long["status"] == "success"]
                .groupby("spread_name", as_index=False)
                .size()
                .rename(columns={"size": "value"})
            )
            for _, row in success_by_spread.iterrows():
                summary_rows.append({"metric": f"success_count::{row['spread_name']}", "value": int(row["value"])})
        summary = pd.DataFrame(summary_rows)

        for tmp_file in [tmp_output_file, tmp_parquet_file]:
            if tmp_file.exists():
                tmp_file.unlink()

        try:
            with pd.ExcelWriter(tmp_output_file, engine="openpyxl") as writer:
                spread_long.to_excel(writer, sheet_name="spread_long", index=False)
                failures_df.to_excel(writer, sheet_name="failures", index=False)
                active_config.to_excel(writer, sheet_name="config_used", index=False)
                summary.to_excel(writer, sheet_name="summary", index=False)
            spread_long.to_parquet(tmp_parquet_file, index=False)
            os.replace(tmp_output_file, output_file)
            os.replace(tmp_parquet_file, parquet_file)
        finally:
            for tmp_file in [tmp_output_file, tmp_parquet_file]:
                if tmp_file.exists():
                    tmp_file.unlink()

        logger.info("spread_long rows=%s status_counts=%s failures=%s", len(spread_long), status_counts, len(failures_df))
        print(f"historical_spread_database: {output_file}")
        print(f"historical_spread_database_parquet: {parquet_file}")
        print(f"calculate_historical_spreads_log: {log_file}")
        print(f"spread_long_rows: {len(spread_long)}")
        print(f"status_counts: {status_counts}")
        print(f"failure_rows: {len(failures_df)}")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("calculate_historical_spreads failed: %s", exc)
        print(f"calculate_historical_spreads failed: {exc}")
        print(f"calculate_historical_spreads_log: {log_file}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

