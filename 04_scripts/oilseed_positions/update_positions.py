"""Collect official positions into one isolated local preview namespace."""
import argparse
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))
from agri_research_agent.oilseed_positions.model import config, preview_root
from agri_research_agent.oilseed_positions.sources import Sources
from agri_research_agent.sugar_positions.storage import publish, read_snapshot
from agri_research_agent.shared.atomic_storage import atomic_write_json


def collect(source, spec, *, markets, start_year, start_day, end_day, euro_start,
            existing_dates=(), seed_contract=None):
    tasks, foreign, domestic, captures, attempts = [], [], [], [], []
    if markets in ("all", "foreign"):
        for market, source_spec in spec["foreign"].items():
            if source_spec["source"] == "cftc":
                for kind in ("futures_only", "combined"):
                    tasks.append((f"cftc_{market}_{kind}", "foreign", "json",
                        lambda m=market, s=source_spec, k=kind: source.cftc(m, s, k, start_year)))
            elif source_spec["source"] == "euronext":
                cursor = euro_start
                while cursor <= end_day:
                    if cursor.weekday() == 2:
                        tasks.append((f"euronext_{cursor:%Y%m%d}", "foreign", "html",
                            lambda d=cursor: source.euronext(d)))
                    cursor += timedelta(days=1)
            else:
                attempts.append(dict(source_id=market, status="pending", error="尚未确认分类基金持仓来源"))
    if markets in ("all", "domestic"):
        varieties = tuple(spec["domestic"])
        if set(varieties) <= {"RS", "OI", "RM"}:
            cursor = start_day
            while cursor <= end_day:
                if cursor.weekday() < 5 and cursor.isoformat() not in existing_dates:
                    tasks.append((f"czce_{cursor:%Y%m%d}", "domestic", "xlsx",
                        lambda d=cursor: source.czce(d, varieties)))
                cursor += timedelta(days=1)
        elif seed_contract:
            # One bounded request; HTTP failures do not trigger a date-by-date retry storm.
            tasks.append((f"dce_{end_day:%Y%m%d}", "domestic", "zip",
                lambda: source.dce(end_day, varieties, seed_contract)))
        else:
            attempts.append(dict(source_id="dce", status="pending",
                error="公开接口访问失败；未自动重试，待确认可用来源"))
    for source_id, category, extension, task in tasks:
        try:
            rows, raw, url = task()
            (foreign if category == "foreign" else domestic).extend(rows)
            captures.append((source_id, raw, url, extension))
            attempts.append(dict(source_id=source_id, status="ok", rows=len(rows)))
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            attempts.append(dict(source_id=source_id, status="not_published" if status == 404 else "failed",
                error=f"HTTP {status}"))
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            attempts.append(dict(source_id=source_id, status="failed", error=f"{type(exc).__name__}: {exc}"))
        print(json.dumps(attempts[-1], ensure_ascii=False), flush=True)
    return foreign, domestic, captures, attempts


def main():
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", choices=tuple(config(ROOT)), required=True)
    parser.add_argument("--markets", choices=("all", "foreign", "domestic"), default="all")
    parser.add_argument("--start-year", type=int, default=today.year - 1)
    parser.add_argument("--domestic-start", type=date.fromisoformat, default=today-timedelta(days=42))
    parser.add_argument("--end", type=date.fromisoformat, default=today)
    parser.add_argument("--euronext-start", type=date.fromisoformat, default=date(2026,9,30),
        help="首版接入2026-09-30起明确标注FUTR/COMB的报告；旧报告待核实口径")
    parser.add_argument("--recheck-domestic", action="store_true")
    parser.add_argument("--dce-seed-contract", help="可选：明确的下载种子合约；不会猜测主力合约")
    args = parser.parse_args()
    if not (2006 <= args.start_year <= today.year and date(2025,11,2) <= args.domestic_start <= args.end <= today
            and (args.end-args.domestic_start).days <= 120 and args.euronext_start <= args.end
            and (args.end-args.euronext_start).days <= 370):
        parser.error("日期或年份超出本地采集范围")
    root = preview_root(ROOT, args.domain)
    previous = read_snapshot(root)
    existing = {r["report_date"] for r in previous["domestic"]} if not args.recheck_domestic else set()
    foreign, domestic, captures, attempts = collect(Sources(), config(ROOT)[args.domain], markets=args.markets,
        start_year=args.start_year, start_day=args.domestic_start, end_day=args.end,
        euro_start=args.euronext_start, existing_dates=existing, seed_contract=args.dce_seed_contract)
    root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(root / "last_attempt.json", dict(attempted_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), attempts=attempts))
    if foreign or domestic:
        payload = publish(root, foreign, domestic, captures, attempts)
        print(json.dumps(dict(release_id=payload["release_id"], root=str(root),
            foreign_rows=len(payload["foreign"]), domestic_rows=len(payload["domestic"])), ensure_ascii=False))
    return 2 if any(a["status"] in ("failed", "pending") for a in attempts) else 0


if __name__ == "__main__":
    raise SystemExit(main())
