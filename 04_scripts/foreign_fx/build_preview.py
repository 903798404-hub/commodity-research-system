"""Fetch official sources into this checkout's disposable preview output only."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))
from agri_research_agent.market_data.foreign_fx import (
    SCHEMA_VERSION, ECB_HISTORY_URL, bcb_url, parse_bcb, parse_ecb, validate_snapshot,
)


def fetch(url: str) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": "AgriculturalResearch/1.0"}), timeout=45) as response:
        if response.status != 200:
            raise ValueError(f"Official source HTTP {response.status}")
        return response.read(16 * 1024 * 1024)


def build(start: date, end: date) -> dict:
    if not start <= end <= date.today():
        raise ValueError("Request dates are invalid")
    sources, rows = [], []
    for provider, url, parser in (
        ("BCB_SGS1", bcb_url(start, end), parse_bcb),
        ("ECB_CROSS", ECB_HISTORY_URL, parse_ecb),
    ):
        raw = fetch(url)
        rows.extend(parser(raw, start, end))
        sources.append(dict(provider=provider, url=url, raw_sha256=hashlib.sha256(raw).hexdigest(),
                            retrieved_at=datetime.now(timezone.utc).isoformat(),
                            method="PTAX卖出参考汇率" if provider == "BCB_SGS1" else "同日EUR/本币除以EUR/USD"))
    payload = dict(schema_version=SCHEMA_VERSION, generated_at=datetime.now(timezone.utc).isoformat(),
                   sources=sources, observations=sorted(rows, key=lambda r: (r["currency"], r["date"])))
    frame = validate_snapshot(payload)
    if set(frame["currency"]) != {"BRL", "CAD", "AUD", "MYR", "IDR", "THB", "INR", "CNY"}:
        raise ValueError("Official source coverage is incomplete; previous preview remains unchanged")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2021, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    # This entry has no production/root/output override and never writes 01_data.
    output = ROOT / "06_outputs" / "foreign_fx_preview" / "daily.json"
    print("started: official daily FX preview", flush=True)
    payload = build(args.start, args.end)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert encoded.decode("utf-8").encode("utf-8") == encoded
    temp = output.with_suffix(".tmp")
    with temp.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, output)
    print(json.dumps(dict(result="PASS", output=str(output), records=len(payload["observations"]),
                          latest=max(r["date"] for r in payload["observations"])), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
