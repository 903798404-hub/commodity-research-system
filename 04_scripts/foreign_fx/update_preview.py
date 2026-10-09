"""Run the scheduled-update preparation in this checkout's preview workspace only."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))
from agri_research_agent.market_data.foreign_fx_update import run_update


def main() -> int:
    # Fixed development path: no production/root/output overrides or scheduling side effects.
    result = run_update(ROOT / "06_outputs/foreign_fx_preview", date(2021, 1, 1), date.today())
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
