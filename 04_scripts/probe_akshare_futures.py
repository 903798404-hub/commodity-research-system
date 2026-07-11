from __future__ import annotations

import datetime
import pathlib
import traceback

import akshare as ak
import pandas as pd


SYMBOLS = [
    "Y0",
    "P0",
    "OI0",
    "M0",
    "RM0",
    "Y2609",
    "P2609",
    "M2609",
    "Y2701",
    "P2701",
    "M2701",
    "OI609",
    "RM609",
    "OI701",
    "RM701",
]

CODE_KEYWORDS = [
    "symbol",
    "code",
    "contract",
    "\u5408\u7ea6",
    "\u54c1\u79cd",
    "\u4ee3\u7801",
    "\u540d\u79f0",
]

PRICE_KEYWORDS = [
    "current_price",
    "price",
    "close",
    "settle",
    "last_close",
    "\u6700\u65b0\u4ef7",
    "\u73b0\u4ef7",
    "\u6536\u76d8\u4ef7",
    "\u7ed3\u7b97\u4ef7",
    "\u6628\u6536",
    "open",
    "high",
    "low",
]


def safe_text(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def find_candidate_columns(columns: list[str], keywords: list[str]) -> list[str]:
    candidates: list[str] = []
    lower_columns = [(column, column.lower()) for column in columns]
    for keyword in keywords:
        keyword_lower = keyword.lower()
        for column, column_lower in lower_columns:
            if keyword_lower in column_lower and column not in candidates:
                candidates.append(column)
    return candidates


def append_log(log_file: pathlib.Path, message: str) -> None:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_file.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {message}\n")


def record_success(
    *,
    interface_name: str,
    symbol: str,
    data: pd.DataFrame,
    summary_rows: list[dict[str, object]],
    fields_rows: list[dict[str, object]],
    head_rows: list[dict[str, object]],
    candidate_rows: list[dict[str, object]],
) -> None:
    columns = [safe_text(column) for column in data.columns]
    code_candidates = find_candidate_columns(columns, CODE_KEYWORDS)
    price_candidates = find_candidate_columns(columns, PRICE_KEYWORDS)

    summary_rows.append(
        {
            "interface": interface_name,
            "symbol": symbol,
            "success": True,
            "row_count": len(data),
            "column_count": len(columns),
            "fields": ", ".join(columns),
            "possible_contract_columns": ", ".join(code_candidates),
            "possible_price_columns": ", ".join(price_candidates),
            "error_summary": "",
        }
    )

    for index, column in enumerate(columns, start=1):
        fields_rows.append(
            {
                "interface": interface_name,
                "symbol": symbol,
                "field_order": index,
                "field_name": column,
            }
        )

    for candidate_type, candidates in (
        ("contract_code", code_candidates),
        ("price", price_candidates),
    ):
        for column in candidates:
            candidate_rows.append(
                {
                    "interface": interface_name,
                    "symbol": symbol,
                    "candidate_type": candidate_type,
                    "field_name": column,
                }
            )

    head_df = data.head(5).copy()
    head_df.insert(0, "probe_symbol", symbol)
    head_df.insert(0, "interface", interface_name)
    head_rows.extend(head_df.to_dict("records"))


def record_failure(
    *,
    interface_name: str,
    symbol: str,
    exc: Exception,
    log_file: pathlib.Path,
    summary_rows: list[dict[str, object]],
) -> None:
    error_type = type(exc).__name__
    error_message = safe_text(exc)
    error_summary = f"{error_type}: {error_message}"
    append_log(log_file, f"{interface_name} symbol={symbol} failed: {error_summary}")
    append_log(log_file, traceback.format_exc())
    summary_rows.append(
        {
            "interface": interface_name,
            "symbol": symbol,
            "success": False,
            "row_count": 0,
            "column_count": 0,
            "fields": "",
            "possible_contract_columns": "",
            "possible_price_columns": "",
            "error_summary": error_summary,
        }
    )


def probe_spot(
    *,
    log_file: pathlib.Path,
    summary_rows: list[dict[str, object]],
    fields_rows: list[dict[str, object]],
    head_rows: list[dict[str, object]],
    candidate_rows: list[dict[str, object]],
) -> None:
    interface_name = "futures_zh_spot"
    batch_symbol = ",".join(SYMBOLS)

    try:
        data = ak.futures_zh_spot(symbol=batch_symbol, market="CF", adjust="0")
        record_success(
            interface_name=interface_name,
            symbol=batch_symbol,
            data=data,
            summary_rows=summary_rows,
            fields_rows=fields_rows,
            head_rows=head_rows,
            candidate_rows=candidate_rows,
        )
        return
    except Exception as exc:  # noqa: BLE001
        record_failure(
            interface_name=interface_name,
            symbol=batch_symbol,
            exc=exc,
            log_file=log_file,
            summary_rows=summary_rows,
        )

    for symbol in SYMBOLS:
        try:
            data = ak.futures_zh_spot(symbol=symbol, market="CF", adjust="0")
            record_success(
                interface_name=interface_name,
                symbol=symbol,
                data=data,
                summary_rows=summary_rows,
                fields_rows=fields_rows,
                head_rows=head_rows,
                candidate_rows=candidate_rows,
            )
        except Exception as exc:  # noqa: BLE001
            record_failure(
                interface_name=interface_name,
                symbol=symbol,
                exc=exc,
                log_file=log_file,
                summary_rows=summary_rows,
            )


def probe_daily(
    *,
    log_file: pathlib.Path,
    summary_rows: list[dict[str, object]],
    fields_rows: list[dict[str, object]],
    head_rows: list[dict[str, object]],
    candidate_rows: list[dict[str, object]],
) -> None:
    interface_name = "futures_zh_daily_sina"
    for symbol in SYMBOLS:
        try:
            data = ak.futures_zh_daily_sina(symbol=symbol)
            record_success(
                interface_name=interface_name,
                symbol=symbol,
                data=data,
                summary_rows=summary_rows,
                fields_rows=fields_rows,
                head_rows=head_rows,
                candidate_rows=candidate_rows,
            )
        except Exception as exc:  # noqa: BLE001
            record_failure(
                interface_name=interface_name,
                symbol=symbol,
                exc=exc,
                log_file=log_file,
                summary_rows=summary_rows,
            )


def build_dataframe(rows: list[dict[str, object]], columns: list[str]) -> pd.DataFrame:
    if rows:
        return pd.DataFrame(rows)
    return pd.DataFrame(columns=columns)


def main() -> int:
    project_root = pathlib.Path(__file__).resolve().parents[1]
    output_dir = project_root / "06_outputs"
    logs_dir = project_root / "10_logs"
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")

    output_file = output_dir / f"akshare_futures_probe_{timestamp}.xlsx"
    log_file = logs_dir / f"akshare_futures_probe_{timestamp}.log"

    if output_file.exists():
        print(f"Output file already exists, will not overwrite: {output_file}")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    append_log(log_file, "AkShare futures probe started.")

    summary_rows: list[dict[str, object]] = []
    spot_fields_rows: list[dict[str, object]] = []
    spot_head_rows: list[dict[str, object]] = []
    daily_fields_rows: list[dict[str, object]] = []
    daily_head_rows: list[dict[str, object]] = []
    candidate_rows: list[dict[str, object]] = []

    probe_spot(
        log_file=log_file,
        summary_rows=summary_rows,
        fields_rows=spot_fields_rows,
        head_rows=spot_head_rows,
        candidate_rows=candidate_rows,
    )
    probe_daily(
        log_file=log_file,
        summary_rows=summary_rows,
        fields_rows=daily_fields_rows,
        head_rows=daily_head_rows,
        candidate_rows=candidate_rows,
    )

    summary_df = build_dataframe(
        summary_rows,
        [
            "interface",
            "symbol",
            "success",
            "row_count",
            "column_count",
            "fields",
            "possible_contract_columns",
            "possible_price_columns",
            "error_summary",
        ],
    )
    spot_fields_df = build_dataframe(
        spot_fields_rows, ["interface", "symbol", "field_order", "field_name"]
    )
    spot_head_df = build_dataframe(spot_head_rows, ["interface", "probe_symbol"])
    daily_fields_df = build_dataframe(
        daily_fields_rows, ["interface", "symbol", "field_order", "field_name"]
    )
    daily_head_df = build_dataframe(daily_head_rows, ["interface", "probe_symbol"])
    candidate_df = build_dataframe(
        candidate_rows, ["interface", "symbol", "candidate_type", "field_name"]
    )

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="summary", index=False)
        spot_fields_df.to_excel(writer, sheet_name="spot_fields", index=False)
        spot_head_df.to_excel(writer, sheet_name="spot_head", index=False)
        daily_fields_df.to_excel(writer, sheet_name="daily_fields", index=False)
        daily_head_df.to_excel(writer, sheet_name="daily_head", index=False)
        candidate_df.to_excel(writer, sheet_name="candidate_columns", index=False)

    append_log(log_file, f"AkShare futures probe finished. Output: {output_file}")
    print(f"Probe report generated: {output_file}")
    print(f"Probe log generated: {log_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

