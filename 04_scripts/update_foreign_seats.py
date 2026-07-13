from __future__ import annotations
import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "03_src"))
from agri_research_agent.data_sources.foreign_seats import (VARIETIES, _snapshot, fetch_dce_with_fallback, fetch_main_prices, fetch_rank_tables, load_seat_config, normalize_rankings, source_error_rows, write_parquet, write_status)  # noqa: E402


def dates(args: argparse.Namespace) -> list[dt.date]:
    if args.date:
        return [dt.date.fromisoformat(args.date)]
    if args.start and args.end:
        return list(pd.bdate_range(args.start, args.end).date)
    return [dt.date.today()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Update exchange-published foreign/key-seat rankings.")
    parser.add_argument("--date"); parser.add_argument("--start"); parser.add_argument("--end")
    parser.add_argument("--recent", action="store_true", help="Update the most recent calendar date (default).")
    parser.add_argument("--force", action="store_true", help="Replace matching normalized keys.")
    parser.add_argument("--timeout", type=int, default=30); parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args()
    log_dir = ROOT / "10_logs"; log_dir.mkdir(exist_ok=True)
    logging.basicConfig(filename=log_dir / f"update_foreign_seats_{dt.date.today():%Y%m%d}.log", level=logging.INFO, encoding="utf-8", format="%(asctime)s %(levelname)s %(message)s")
    config = load_seat_config(ROOT / "02_configs" / "foreign_seats.yaml")
    requested = dates(args); now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    rows: list[dict[str, object]] = []; failures: list[str] = []
    for day in requested:
        for exchange in sorted(set(VARIETIES.values())):
            provenance: dict[str, object]
            try:
                if exchange == "DCE":
                    tables, provenance = fetch_dce_with_fallback(day, ROOT / "01_data" / "raw", args.retries, args.timeout)
                    if not tables:
                        raise RuntimeError(str(provenance.get("error_message", "DCE sources returned no public data")))
                else:
                    tables = fetch_rank_tables(day, exchange, args.retries, args.timeout)
                    downloaded_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
                    _snapshot(ROOT / "01_data" / "raw", day, "akshare_czce", "json", __import__("json").dumps({key: value.to_dict("records") for key, value in tables.items()}, ensure_ascii=False).encode("utf-8"), {"method": "ak.get_rank_table_czce", "requested_date": day.isoformat(), "downloaded_at": downloaded_at, "status": "success"})
                    provenance = {"source_name": "AKShare", "source_method": "get_rank_table_czce", "source_status": "success" if tables else "market_closed", "source_date": day.isoformat() if tables else "", "requested_date": day.isoformat(), "downloaded_at": downloaded_at, "error_message": "" if tables else "exchange calendar reports non-trading date"}
                for variety, mapped_exchange in VARIETIES.items():
                    if mapped_exchange == exchange:
                        rows.extend(normalize_rankings(day, exchange, variety, tables, config, now, provenance))
            except Exception as exc:  # one exchange/date failure must not stop others
                message = f"{day} {exchange}: {type(exc).__name__}: {exc}"; failures.append(message); logging.exception(message)
                failed_provenance = provenance if "provenance" in locals() and exchange == "DCE" else {"source_name": "AKShare", "source_method": "exchange ranking API", "source_status": "failed", "source_date": "", "requested_date": day.isoformat(), "downloaded_at": now, "error_message": message}
                rows.extend(source_error_rows(day, exchange, config, now, failed_provenance))
    price_start, price_end = min(requested), max(requested)
    try:
        prices = fetch_main_prices(price_start, price_end, args.retries, args.timeout)
    except Exception as exc:
        failures.append(f"prices: {type(exc).__name__}: {exc}"); logging.exception("price update failed")
        prices = pd.DataFrame()
    output = ROOT / "01_data" / "database" / "foreign_seats" / "foreign_seat_positions.parquet"
    data = write_parquet(rows, prices, output)
    status = {"foreign_seats": {"status": "success" if rows and not failures else "partial_failure", "updated_at": now, "latest_date": str(data.trade_date.max()) if not data.empty else "", "rows_written": len(rows), "failures": failures, "source": "AKShare first; DCE official public-file fallback"}}
    write_status(ROOT / "01_data" / "foreign_seats_update_status.json", status)
    print(status); return 0 if rows else 1

if __name__ == "__main__":
    raise SystemExit(main())
