#!/usr/bin/env python
"""Materialize one local AM/PM soybean result Release from sealed inputs."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "03_src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agri_research_agent.import_profit.config import load_soybean_config  # noqa: E402
from agri_research_agent.market_data.intraday import MarketSession  # noqa: E402
from agri_research_agent.pipelines.soybean_intraday import (  # noqa: E402
    materialize_soybean_intraday_profit,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-date", required=True, type=date.fromisoformat)
    parser.add_argument("--session", required=True, choices=("AM", "PM"))
    parser.add_argument("--snapshot-root", required=True)
    parser.add_argument("--result-root", required=True)
    parser.add_argument("--cnf-store", required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "02_configs" / "import_profit_soybean.yaml",
    )
    args = parser.parse_args(argv)
    result = materialize_soybean_intraday_profit(
        snapshot_root=args.snapshot_root,
        result_root=args.result_root,
        cnf_store_path=args.cnf_store,
        business_date=args.business_date,
        session=MarketSession(args.session),
        config=load_soybean_config(args.config),
        calculated_at=datetime.now(timezone.utc),
    )
    print(
        json.dumps(
            {
                "status": result.status.value,
                "release_id": result.release_id,
                "content_sha256": result.content_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
