"""Explicit authorized capture; never run from rendering or manual CNF save."""
from pathlib import Path
import argparse
from datetime import datetime
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))

from agri_research_agent.commodity_import_margin.inputs import capture as capture_daily
from agri_research_agent.commodity_import_margin.live import capture as capture_latest
from agri_research_agent.commodity_import_margin.runtime import storage_context
from agri_research_agent.commodity_import_margin.store import read_market, publish
from agri_research_agent.soybean_margin.api_sources import SHANGHAI
from agri_research_agent.commodity_import_margin.model import START


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commodity", choices=["canola", "palm"], required=True)
    parser.add_argument("--server", action="store_true", help="Use the protected workbench service storage")
    parser.add_argument("--latest", action="store_true", help="Fetch latest available dated quotes without a fixed daily window")
    args = parser.parse_args()
    try:
        path, authorize, _ = storage_context(server=args.server)
        authorize(path)
        day = datetime.now(SHANGHAI).date()
        if not args.latest and (day < START or day.weekday() >= 5):
            raise ValueError("本地行情采集须在2026-10-12起的实际交易日运行")
        _, previous = read_market(path, day, args.commodity)
        snapshot = (capture_latest if args.latest else capture_daily)(args.commodity)
        identity = publish(path, snapshot, expected_identity=previous, authorize=authorize)
        print(json.dumps(dict(business_date=snapshot["business_date"], commodity=args.commodity,
                             sha256=identity, errors=snapshot["errors"]), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps(dict(error=type(exc).__name__), ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
