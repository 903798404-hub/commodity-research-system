"""Collect a server-side holdings candidate without deployment or stable publication."""
import argparse
from datetime import date, datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '03_src'))
from agri_research_agent.positions.delivery import read_json
from agri_research_agent.positions.server_collection import collect
from agri_research_agent.soybean_margin.api_sources import SHANGHAI


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path, help='Verified four-board positions_archive.json')
    parser.add_argument('--output', required=True, type=Path, help='Fresh external candidate directory')
    parser.add_argument('--date', required=True, type=date.fromisoformat)
    parser.add_argument('--contracts', nargs='+', help='Only these exact contracts; omit for all disclosed reports')
    parser.add_argument('--dce-only', action='store_true', help='Skip unchanged CZCE official collection')
    args = parser.parse_args(argv)
    try:
        if args.date > datetime.now(SHANGHAI).date():
            parser.error('不允许请求未来持仓日期')
        baseline = read_json(args.baseline)
        if (args.output.absolute().is_relative_to(args.baseline.absolute().parent)
                or args.baseline.absolute().is_relative_to(args.output.absolute())):
            parser.error('候选不能写入基线目录')
        receipt = collect(baseline, args.output, ROOT, args.date, contracts=args.contracts,
                          include_official=not args.dce_only)
        print(json.dumps(receipt, ensure_ascii=False))
        return 2 if any(a['status'] == 'failed' for a in receipt['attempts']) else 0
    except (OSError, ValueError, KeyError):
        print(json.dumps(dict(error='server_positions_collection_failed', published=False)))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
