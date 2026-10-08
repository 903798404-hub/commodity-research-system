"""Collect only into this independent worktree's local sugar preview store."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))
from agri_research_agent.sugar_positions.model import load_members
from agri_research_agent.sugar_positions.sources import OfficialSources
from agri_research_agent.sugar_positions.storage import preview_root, publish, read_snapshot
from agri_research_agent.shared.atomic_storage import atomic_write_json


def collect(source, *, markets, start_year, start_day, end_day, existing_dates=()):
    foreign, domestic, captures, attempts = [], [], [], []
    tasks = []
    if markets in ("all", "foreign"):
        for kind in ("futures_only", "combined"):
            tasks.append((f"cftc_{kind}", "foreign", "json", lambda k=kind: source.cftc(k, start_year)))
        for year in range(start_year, end_day.year + 1):
            tasks.append((f"ice_{year}", "foreign", "csv", lambda y=year: source.ice(y)))
    if markets in ("all", "domestic"):
        cursor = start_day
        while cursor <= end_day:
            if cursor.weekday() < 5 and cursor.isoformat() not in existing_dates:
                tasks.append((f"czce_{cursor:%Y%m%d}", "domestic", "xlsx", lambda d=cursor: source.czce(d)))
            cursor += timedelta(days=1)
    for source_id, category, extension, task in tasks:
        try:
            rows, raw, url = task()
            (foreign if category == "foreign" else domestic).extend(rows)
            captures.append((source_id, raw, url, extension))
            attempts.append(dict(source_id=source_id, status="ok", rows=len(rows)))
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            attempts.append(dict(source_id=source_id,
                status="not_published" if category == "domestic" and status == 404 else "failed",
                error=f"HTTP {status}"))
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            attempts.append(dict(source_id=source_id, status="failed", error=f"{type(exc).__name__}: {exc}"))
        print(json.dumps(attempts[-1], ensure_ascii=False), flush=True)
    return foreign, domestic, captures, attempts


def main():
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markets", choices=("all", "foreign", "domestic"), default="all")
    parser.add_argument("--start-year", type=int, default=today.year - 1)
    parser.add_argument("--czce-start", type=date.fromisoformat, default=today - timedelta(days=42))
    parser.add_argument("--czce-end", type=date.fromisoformat, default=today)
    parser.add_argument("--recheck-domestic", action="store_true", help="重新校验已有交易日")
    args = parser.parse_args()
    if not 2006 <= args.start_year <= today.year or not (
            date(2025, 11, 2) <= args.czce_start <= args.czce_end <= today
            and (args.czce_end - args.czce_start).days <= 120):
        parser.error("年份或国内日期区间无效；国内首版支持2025-11-02后、不超过120日的区间")
    root = preview_root(ROOT)
    load_members(ROOT / "02_configs" / "sugar_positions.json")
    previous = read_snapshot(root)
    existing = {r["report_date"] for r in previous["domestic"]} if not args.recheck_domestic else set()
    foreign, domestic, captures, attempts = collect(OfficialSources(), markets=args.markets,
        start_year=args.start_year, start_day=args.czce_start, end_day=args.czce_end,
        existing_dates=existing)
    root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(root / "last_attempt.json", dict(attempted_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        attempts=attempts))
    if foreign or domestic:
        payload = publish(root, foreign, domestic, captures, attempts)
        print(json.dumps(dict(release_id=payload["release_id"], root=str(root),
            foreign_rows=len(payload["foreign"]), domestic_rows=len(payload["domestic"])), ensure_ascii=False))
    failed = any(a["status"] == "failed" for a in attempts)
    return 2 if failed or (not foreign and not domestic and attempts) else 0


if __name__ == "__main__":
    raise SystemExit(main())
