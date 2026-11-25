"""Collect or replay a sugar quote candidate using read-only baseline inputs."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from gzip import compress
from hashlib import sha256
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "03_src"))

from agri_research_agent.application.sugar_spread_sources import (
    LONDON, SHANGHAI, collect_reports, parse_czapp_settlements, parse_czce_daily, preview_dates, report_url,
)
from agri_research_agent.application.sugar_spreads import (
    MARKET_FILES, SERVER_SCHEMA, load_quotes, merge_quotes, serialize_quotes, server_snapshots,
)


LOOKBACK = 10
MAX_REPORT_BYTES = 2 * 1024**2
EMPTY_POINTER = b'{"schema_version":"sugar-spread-quotes/1","markets":{}}\n'
SOURCE_PARSERS = {"domestic": parse_czce_daily, "foreign": parse_czapp_settlements}


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def trading_windows(as_of: datetime) -> dict[str, list[date]]:
    beijing, london = as_of.astimezone(SHANGHAI), as_of.astimezone(LONDON)
    domestic = beijing.date() - timedelta(days=beijing.time() < time(18, 30))
    foreign = london.date() - timedelta(days=london.time() < time(18, 30))
    return {"domestic": preview_dates(domestic, LOOKBACK), "foreign": preview_dates(foreign, LOOKBACK)}


def regular_bytes(path: Path, limit: int) -> bytes:
    if (path.resolve() != path.absolute() or not path.is_file()
            or path.stat().st_nlink != 1 or path.stat().st_size > limit):
        raise ValueError(f"Invalid sugar candidate file: {path.name}")
    return path.read_bytes()


def read_baseline(baseline: Path, as_of: datetime) -> tuple[bytes, dict]:
    store = baseline / "official-releases"
    path = store / "current.json"
    pointer = regular_bytes(path, 4096) if path.exists() or path.is_symlink() else EMPTY_POINTER
    files = server_snapshots(store, pointer)
    today = as_of.astimezone(SHANGHAI).date()
    quotes = {}
    for market, filename in MARKET_FILES.items():
        path = files.get(market, baseline / filename)
        quotes[market] = load_quotes(path, as_of=today) if path.exists() else []
        if any(q.contract.startswith("SR") != (market == "domestic") for q in quotes[market]):
            raise ValueError(f"Unexpected contracts in the {market} baseline")
    return pointer, quotes


def candidate_result(baseline: bytes, old: dict, verified: dict, receipts: list) -> tuple[dict, dict[str, bytes]]:
    markets = dict(json.loads(baseline)["markets"])
    changes, files, latest = {}, {}, {}
    for market in MARKET_FILES:
        merged = merge_quotes(old[market] + verified[market])
        latest[market] = max((q.trade_date.isoformat() for q in merged if
                              (q.provider, q.price_type) == (("郑商所", "close") if market == "domestic"
                                                             else ("Czapp", "settlement"))), default=None)
        changes[market] = len(merged) - len(old[market])
        if changes[market] or (verified[market] and market not in markets):
            raw = compress(serialize_quotes(merged), compresslevel=6, mtime=0)
            markets[market] = sha256(raw).hexdigest()
            files[markets[market] + ".csv.gz"] = raw
    failed = any(receipt["status"] == "failed" for receipt in receipts)
    status = ("PARTIAL_FAILURE" if files else "FAILED") if failed else ("UPDATED" if files else "NO_CHANGE")
    return dict(status=status, baseline_sha256=sha256(baseline).hexdigest(),
                pointer=dict(schema_version=SERVER_SCHEMA, markets=markets),
                changes=changes, latest=latest, receipts=receipts), files


def collect(baseline: Path, target: Path, as_of: datetime, *, fetcher=None) -> dict:
    pointer, old = read_baseline(baseline, as_of)
    target.mkdir()
    (target / "raw").mkdir()
    (target / "snapshots").mkdir()
    verified, receipts = {}, []
    for market, days in trading_windows(as_of).items():
        batches, checked, reports = collect_reports(days, market, old[market], fetcher=fetcher)
        verified[market] = [quote for batch in batches for quote in batch]
        receipts.extend(checked)
        for filename, raw in reports.items():
            (target / "raw" / filename).write_bytes(raw)
    result, files = candidate_result(pointer, old, verified, receipts)
    for filename, raw in files.items():
        (target / "snapshots" / filename).write_bytes(raw)
    (target / "result.json").write_bytes(json_bytes(result))
    return result


def validate(baseline: Path, target: Path, as_of: datetime) -> dict:
    """Reparse the exact original reports in a second, offline worker."""
    pointer, old = read_baseline(baseline, as_of)
    if {path.name for path in target.iterdir()} != {"result.json", "raw", "snapshots"}:
        raise ValueError("Unexpected sugar candidate inventory")
    result = json.loads(regular_bytes(target / "result.json", 64 * 1024))
    receipts = result["receipts"]
    windows = trading_windows(as_of)
    verified, seen, raw_names = {market: [] for market in MARKET_FILES}, set(), set()
    for receipt in receipts:
        market, day = receipt["market"], date.fromisoformat(receipt["date"])
        key = (market, day)
        if key in seen or day not in windows[market] or receipt["url"] != report_url(day, market):
            raise ValueError("Unexpected sugar source receipt")
        seen.add(key)
        if receipt["status"] in {"failed", "not_published"}:
            continue
        suffix = "xlsx" if market == "domestic" else "html"
        filename = f"{market}-{day}.{suffix}"
        raw = regular_bytes(target / "raw" / filename, MAX_REPORT_BYTES)
        if receipt["status"] != "verified" or receipt["file"] != filename or receipt["sha256"] != sha256(raw).hexdigest():
            raise ValueError("Sugar raw report identity differs")
        raw_names.add(filename)
        quotes = SOURCE_PARSERS[market](raw, day, receipt["url"])
        if receipt["rows"] != len(quotes):
            raise ValueError("Sugar report row count differs")
        verified[market].extend(quotes)
    expected, files = candidate_result(pointer, old, verified, receipts)
    if (result != expected or {p.name for p in (target / "raw").iterdir()} != raw_names
            or {p.name for p in (target / "snapshots").iterdir()} != files.keys()):
        raise ValueError("Sugar candidate does not match replay")
    for filename, expected_raw in files.items():
        if regular_bytes(target / "snapshots" / filename, 8 * 1024**2) != expected_raw:
            raise ValueError("Sugar snapshot does not match the verified sources")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("collect", "validate"))
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    args = parser.parse_args()
    if args.as_of.utcoffset() is None:
        parser.error("An explicit as-of timezone is required")
    result = (collect if args.mode == "collect" else validate)(args.baseline, args.candidate, args.as_of)
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
