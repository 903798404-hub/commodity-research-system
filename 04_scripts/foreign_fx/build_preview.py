"""Fetch official sources into this checkout's disposable preview output only."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))
from agri_research_agent.market_data.foreign_fx_update import collect_snapshot, run_update


def build(start: date, end: date) -> dict:
    return collect_snapshot(start, end)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2021, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    # This entry has no production/root/output override and never writes 01_data.
    print("started: official daily FX preview", flush=True)
    result = run_update(ROOT / "06_outputs/foreign_fx_preview", args.start, args.end)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
