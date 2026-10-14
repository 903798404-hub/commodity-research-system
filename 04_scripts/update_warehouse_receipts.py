"""Prepare a verified incremental warehouse snapshot in an isolated candidate."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "03_src"))
from agri_research_agent.warehouse_receipts.provider import TushareReceipts
from agri_research_agent.warehouse_receipts.update import prepare_incremental


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--secret-file", type=Path)
    args = parser.parse_args()
    try:
        for path in (args.candidate_root, args.raw_root):
            if path.resolve() == args.baseline_root.resolve() or path.resolve().is_relative_to(args.baseline_root.resolve()):
                raise ValueError("增量采集不得直接写入基线")
        if args.secret_file:
            secret = json.loads(args.secret_file.read_text(encoding="utf-8", errors="strict"))
            token, endpoint = secret["token"], secret["endpoint"]
        else:
            token, endpoint = os.getenv("TUSHARE_TOKEN", ""), os.getenv("TUSHARE_API_URL", "https://api.tushare.pro")
        result = prepare_incremental(TushareReceipts(token, endpoint), args.baseline_root, args.candidate_root,
                                     args.raw_root, dt.datetime.now(dt.timezone.utc))
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps(dict(status="FAILED", error_type=type(exc).__name__)), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
