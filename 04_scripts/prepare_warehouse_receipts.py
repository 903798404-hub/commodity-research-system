"""Acquire warehouse receipts into a local candidate; never writes production."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "03_src"))
from agri_research_agent.warehouse_receipts.model import PRODUCTS, aggregate_daily, normalize_rows, write_snapshot
from agri_research_agent.warehouse_receipts.provider import TushareReceipts, acquire_history


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=dt.date.fromisoformat, default=dt.date(2022, 1, 1))
    parser.add_argument("--end-date", type=dt.date.fromisoformat, default=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date())
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.start_date > args.end_date or args.end_date > dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date():
            raise ValueError("仓单查询日期无效")
        for path, parent in ((args.candidate_root, ROOT / "01_data/candidates/warehouse_receipts"),
                             (args.raw_root, ROOT / "01_data/raw/warehouse_receipts")):
            if not path.resolve().is_relative_to(parent.resolve()):
                raise ValueError("仅允许写本开发检出的独立仓单 raw / candidate 目录")
        client = TushareReceipts(os.getenv("TUSHARE_TOKEN", ""), os.getenv("TUSHARE_API_URL", "https://api.tushare.pro"))
        last_month = {}
        def progress(symbol, end, rows):
            month = end.strftime("%Y-%m")
            if month != last_month.get(symbol):
                print(json.dumps(dict(symbol=symbol, through=end.isoformat(), window_rows=rows)), flush=True)
                last_month[symbol] = month
        records = acquire_history(client, args.start_date, args.end_date, args.raw_root, progress)
        raw = normalize_rows([row for record in records for row in record["rows"]])
        daily = aggregate_daily(raw)
        if set(daily.symbol) != set(PRODUCTS):
            raise ValueError("部分品种没有任何仓单观测，未生成完整候选")
        evidence = dict(source="user-approved Tushare-compatible gateway" if client.endpoint != "https://api.tushare.pro" else "Tushare Pro",
                        endpoint=client.endpoint, api_name="fut_wsr", requested_start=args.start_date.isoformat(),
                        requested_end=args.end_date.isoformat(), captured_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                        query_windows=len(records), empty_windows=sum(not r["rows"] for r in records),
                        invalid_quantity_rows=int((raw.vol.lt(0) | raw.pre_vol.lt(0)).sum()),
                        invalid_daily_totals=int(daily.status.eq("invalid_component").sum()),
                        coverage={symbol: dict(first=block.date.min().date().isoformat(), last=block.date.max().date().isoformat(),
                                               observed_days=len(block), units=sorted(block.unit.unique().tolist()))
                                  for symbol, block in daily.groupby("symbol")})
        manifest = write_snapshot(args.candidate_root, raw, daily, evidence)
        print(json.dumps(dict(status="PASS", candidate_root=str(args.candidate_root.resolve()), **manifest), ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        # Provider errors have already been sanitized; never print request bodies or env.
        safe = str(exc)[:200] if isinstance(exc, ValueError) else type(exc).__name__
        print(json.dumps(dict(status="FAILED", error=safe), ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
