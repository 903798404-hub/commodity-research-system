"""Explicit local capture; never run from rendering or manual CNF save."""
from pathlib import Path
import argparse
from datetime import datetime
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))

from agri_research_agent.commodity_import_margin.inputs import capture
from agri_research_agent.commodity_import_margin.store import read_market, publish
from agri_research_agent.commodity_import_margin.runtime import database_path, authorize_write
from agri_research_agent.soybean_margin.api_sources import SHANGHAI
from agri_research_agent.commodity_import_margin.model import START


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commodity", choices=["canola", "palm"], required=True)
    args = parser.parse_args()
    try:
        path = database_path()
        authorize_write(path)
        day = datetime.now(SHANGHAI).date()
        if day < START or day.weekday() >= 5:
            raise ValueError("本地行情采集须在2026-10-12起的实际交易日运行")
        _, previous = read_market(path, day, args.commodity)
        snapshot = capture(args.commodity)
        identity = publish(path, snapshot, expected_identity=previous, authorize=authorize_write)
        print(json.dumps(dict(business_date=snapshot["business_date"], commodity=args.commodity,
                             sha256=identity, errors=snapshot["errors"]), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps(dict(error=type(exc).__name__, detail=str(exc)), ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
