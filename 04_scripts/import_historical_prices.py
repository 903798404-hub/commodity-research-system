from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd


INSTRUMENT_MAP = {
    "豆粕": "M",
    "菜籽粕": "RM",
    "豆油": "Y",
    "菜籽油": "OI",
    "棕榈油": "P",
}

REQUIRED_COLUMNS = [
    "date",
    "instrument",
    "instrument_cn",
    "delivery_month",
    "price",
    "source_column",
    "source_file",
    "updated_at",
    "status",
    "error",
]


def setup_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("import_historical_prices")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def detect_date_column(columns: list[Any]) -> Any:
    for column in columns:
        if str(column).strip().lower() in {"日期", "date"}:
            return column
    return columns[0]


def parse_price_column(column: object) -> tuple[str, int, str] | None:
    text = str(column).strip()
    if "期货收盘价" not in text:
        return None
    month_match = re.search(r"(\d{1,2})\s*月\s*交割\s*连续", text)
    product_match = re.search(r"[:：]\s*([\u4e00-\u9fffA-Za-z0-9_-]+)\s*$", text)
    if not month_match or not product_match:
        return None
    instrument_cn = product_match.group(1).strip()
    instrument = INSTRUMENT_MAP.get(instrument_cn)
    if not instrument:
        return None
    return instrument, int(month_match.group(1)), instrument_cn


def read_wind_table(path: Path, sheet_name: str) -> pd.DataFrame:
    raw = pd.read_excel(path, sheet_name=sheet_name, header=None)
    if raw.empty:
        return raw

    best_header_index = 0
    best_score = -1
    for index in range(min(10, len(raw))):
        values = [str(value) for value in raw.iloc[index].tolist()]
        score = sum("期货收盘价" in value for value in values)
        if score > best_score:
            best_score = score
            best_header_index = index

    columns = raw.iloc[best_header_index].tolist()
    data = raw.iloc[best_header_index + 1 :].copy()
    data.columns = columns
    data = data.dropna(how="all")
    return data


def choose_sheet(path: Path, logger: logging.Logger) -> tuple[str, list[dict[str, Any]]]:
    excel = pd.ExcelFile(path)
    log_rows: list[dict[str, Any]] = []
    if len(excel.sheet_names) == 1:
        logger.info("single sheet workbook, selected sheet=%s", excel.sheet_names[0])
        return excel.sheet_names[0], [{"event": "selected_sheet", "sheet": excel.sheet_names[0], "reason": "single_sheet"}]

    best_sheet = excel.sheet_names[0]
    best_score = -1
    for sheet_name in excel.sheet_names:
        sample = pd.read_excel(path, sheet_name=sheet_name, header=None, nrows=10)
        flattened = [str(value) for value in sample.to_numpy().ravel()]
        price_count = sum("期货收盘价" in value for value in flattened)
        date_count = sum(value.strip().lower() in {"日期", "date"} for value in flattened)
        score = price_count * 10 + date_count
        log_rows.append(
            {
                "event": "sheet_scored",
                "sheet": sheet_name,
                "reason": f"price_fields={price_count}; date_fields={date_count}; score={score}",
            }
        )
        if score > best_score:
            best_score = score
            best_sheet = sheet_name

    logger.info("selected sheet=%s score=%s", best_sheet, best_score)
    log_rows.append({"event": "selected_sheet", "sheet": best_sheet, "reason": f"best_score={best_score}"})
    return best_sheet, log_rows


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    source_file = project_root / "01_data" / "manual_history" / "historical_price_base.xlsx"
    output_file = project_root / "01_data" / "historical_price_long.xlsx"
    logs_dir = project_root / "10_logs"
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    log_file = logs_dir / f"import_historical_prices_{timestamp}.log"

    for directory in [project_root / "01_data", project_root / "01_data" / "manual_history", project_root / "06_outputs", project_root / "06_outputs" / "charts", logs_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    logger = setup_logger(log_file)
    updated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    import_log: list[dict[str, Any]] = []

    try:
        sheet_name, sheet_log = choose_sheet(source_file, logger)
        import_log.extend(sheet_log)
        table = read_wind_table(source_file, sheet_name)
        if table.empty:
            raise ValueError("selected sheet has no data")

        date_column = detect_date_column(list(table.columns))
        dates = pd.to_datetime(table[date_column], errors="coerce")
        valid_date_rows = dates.notna()
        table = table.loc[valid_date_rows].copy()
        dates = dates.loc[valid_date_rows]

        price_frames: list[pd.DataFrame] = []
        recognized: list[tuple[str, int]] = []

        for column in table.columns:
            if column == date_column:
                continue
            parsed = parse_price_column(column)
            if not parsed:
                if "期货收盘价" in str(column):
                    import_log.append({"event": "unrecognized_price_column", "sheet": sheet_name, "reason": str(column)})
                    logger.warning("unrecognized price column: %s", column)
                continue

            instrument, delivery_month, instrument_cn = parsed
            prices = pd.to_numeric(table[column], errors="coerce")
            status = pd.Series("success", index=table.index, dtype="object")
            status[prices.isna()] = "missing_price"
            status[prices == 0] = "suspicious_zero"
            error = pd.Series("", index=table.index, dtype="object")
            error[prices.isna()] = "missing price"
            error[prices == 0] = "zero price"

            frame = pd.DataFrame(
                {
                    "date": dates.dt.date,
                    "instrument": instrument,
                    "instrument_cn": instrument_cn,
                    "delivery_month": delivery_month,
                    "price": prices,
                    "source_column": str(column),
                    "source_file": str(source_file),
                    "updated_at": updated_at,
                    "status": status,
                    "error": error,
                }
            )
            price_frames.append(frame)
            recognized.append((instrument, delivery_month))

        price_long = pd.concat(price_frames, ignore_index=True) if price_frames else pd.DataFrame(columns=REQUIRED_COLUMNS)
        price_long = price_long.loc[:, REQUIRED_COLUMNS]
        recognized_text = ", ".join(f"{instrument}-{month}" for instrument, month in sorted(set(recognized)))
        import_log.append(
            {
                "event": "import_summary",
                "sheet": sheet_name,
                "reason": f"rows={len(price_long)}; recognized={recognized_text}",
            }
        )
        logger.info("price_long rows=%s recognized=%s", len(price_long), recognized_text)

        with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
            price_long.to_excel(writer, sheet_name="price_long", index=False)
            pd.DataFrame(import_log).to_excel(writer, sheet_name="import_log", index=False)

        print(f"historical_price_long: {output_file}")
        print(f"import_log: {log_file}")
        print(f"price_long_rows: {len(price_long)}")
        print(f"recognized_instruments_months: {recognized_text}")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("import failed: %s", exc)
        import_log.append({"event": "failed", "sheet": "", "reason": f"{type(exc).__name__}: {exc}"})
        with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
            pd.DataFrame(columns=REQUIRED_COLUMNS).to_excel(writer, sheet_name="price_long", index=False)
            pd.DataFrame(import_log).to_excel(writer, sheet_name="import_log", index=False)
        print(f"import_historical_prices failed: {exc}")
        print(f"import_log: {log_file}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

