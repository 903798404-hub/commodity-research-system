from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

import pandas as pd


CONFIG_COLUMNS = [
    "spread_group",
    "spread_name",
    "leg1_instrument",
    "leg1_month",
    "leg2_instrument",
    "leg2_month",
    "formula",
    "season_start_month",
    "season_end_month",
    "window_start_month",
    "window_start_day",
    "window_end_month",
    "window_end_day",
    "full_history",
    "enabled",
    "note",
]


def setup_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("build_spread_config")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def config_row(
    spread_group: str,
    spread_name: str,
    leg1_instrument: str,
    leg1_month: int,
    leg2_instrument: str,
    leg2_month: int,
    window_start_month: int,
    window_start_day: int,
    window_end_month: int,
    window_end_day: int,
    note: str,
) -> dict[str, Any]:
    return {
        "spread_group": spread_group,
        "spread_name": spread_name,
        "leg1_instrument": leg1_instrument,
        "leg1_month": leg1_month,
        "leg2_instrument": leg2_instrument,
        "leg2_month": leg2_month,
        "formula": "leg1-leg2",
        "season_start_month": window_start_month,
        "season_end_month": window_end_month,
        "window_start_month": window_start_month,
        "window_start_day": window_start_day,
        "window_end_month": window_end_month,
        "window_end_day": window_end_day,
        "full_history": False,
        "enabled": True,
        "note": note,
    }


def window_for_month_pair(leg1_month: int, leg2_month: int) -> tuple[int, int, int, int]:
    if (leg1_month, leg2_month) == (1, 5):
        return 6, 1, 1, 10
    if (leg1_month, leg2_month) == (5, 9):
        return 10, 1, 4, 30
    if (leg1_month, leg2_month) == (9, 1):
        return 2, 1, 8, 31
    raise ValueError(f"unsupported month pair: {leg1_month}-{leg2_month}")


def window_for_delivery_month(month: int) -> tuple[int, int, int, int]:
    if month == 1:
        return 6, 1, 1, 10
    if month == 5:
        return 10, 1, 4, 30
    if month == 9:
        return 2, 1, 8, 31
    raise ValueError(f"unsupported delivery month: {month}")


def build_configs() -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    for instrument in ["M", "RM", "Y", "OI", "P"]:
        for leg1_month, leg2_month in [(1, 5), (5, 9), (9, 1)]:
            spread_name = f"{instrument} {leg1_month}-{leg2_month}"
            window_start_month, window_start_day, window_end_month, window_end_day = window_for_month_pair(
                leg1_month, leg2_month
            )
            rows.append(
                config_row(
                    spread_group=instrument,
                    spread_name=spread_name,
                    leg1_instrument=instrument,
                    leg1_month=leg1_month,
                    leg2_instrument=instrument,
                    leg2_month=leg2_month,
                    window_start_month=window_start_month,
                    window_start_day=window_start_day,
                    window_end_month=window_end_month,
                    window_end_day=window_end_day,
                    note="calendar spread",
                )
            )

    for leg1_instrument, leg2_instrument in [("M", "RM"), ("Y", "P"), ("OI", "Y"), ("OI", "P")]:
        group = f"{leg1_instrument}-{leg2_instrument}"
        for month in [1, 5, 9]:
            spread_name = f"{group} {month}"
            window_start_month, window_start_day, window_end_month, window_end_day = window_for_delivery_month(month)
            rows.append(
                config_row(
                    spread_group=group,
                    spread_name=spread_name,
                    leg1_instrument=leg1_instrument,
                    leg1_month=month,
                    leg2_instrument=leg2_instrument,
                    leg2_month=month,
                    window_start_month=window_start_month,
                    window_start_day=window_start_day,
                    window_end_month=window_end_month,
                    window_end_day=window_end_day,
                    note="cross commodity spread",
                )
            )

    return pd.DataFrame(rows, columns=CONFIG_COLUMNS)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    source_file = project_root / "01_data" / "manual_history" / "spread_system_base.xlsx"
    output_file = project_root / "02_configs" / "historical_spread_config.xlsx"
    logs_dir = project_root / "10_logs"
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    log_file = logs_dir / f"build_spread_config_{timestamp}.log"

    for directory in [project_root / "01_data", project_root / "01_data" / "manual_history", project_root / "06_outputs", project_root / "06_outputs" / "charts", logs_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    logger = setup_logger(log_file)
    build_log: list[dict[str, Any]] = []

    try:
        excel = pd.ExcelFile(source_file)
        sheet_rows = [{"sheet_name": sheet_name, "source_file": str(source_file)} for sheet_name in excel.sheet_names]
        logger.info("read spread system workbook: %s", source_file)
        logger.info("sheet names: %s", ", ".join(excel.sheet_names))
        build_log.append({"event": "read_spread_system_workbook", "message": f"sheet_count={len(excel.sheet_names)}"})
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to read spread system workbook: %s", exc)
        sheet_rows = []
        build_log.append({"event": "read_spread_system_workbook_failed", "message": f"{type(exc).__name__}: {exc}"})

    spread_config = build_configs()
    build_log.append({"event": "build_config", "message": f"spread_config_rows={len(spread_config)}"})
    logger.info("spread_config rows=%s", len(spread_config))

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        spread_config.to_excel(writer, sheet_name="spread_config", index=False)
        pd.DataFrame(sheet_rows).to_excel(writer, sheet_name="system_workbook_sheets", index=False)
        pd.DataFrame(build_log).to_excel(writer, sheet_name="build_log", index=False)

    print(f"historical_spread_config: {output_file}")
    print(f"build_spread_config_log: {log_file}")
    print(f"spread_config_rows: {len(spread_config)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

