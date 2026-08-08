from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "03_src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agri_research_agent.soybean_exports.fgis import (
    FgisAdapter,
    FgisYearlyAdapter,
    run_fgis_pipeline,
)


def _date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run USDA FGIS soybean inspections pipeline")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument(
        "--source",
        choices=("yearly", "socrata"),
        default="yearly",
        help="Production defaults to the official FGIS Yearly CSV; Socrata is audit-only.",
    )
    parser.add_argument("--cert-date-start", type=_date)
    parser.add_argument("--cert-date-end", type=_date)
    parser.add_argument("--page-size", type=int, default=50_000)
    parser.add_argument("--timeout", type=float, default=50)
    parser.add_argument("--ignore-environment-proxy", action="store_true")
    parser.add_argument("--candidate-only", action="store_true")
    args = parser.parse_args()
    if args.source == "yearly" and (
        args.cert_date_start is not None or args.cert_date_end is not None
    ):
        parser.error("--cert-date-start/--cert-date-end are only valid with --source socrata")
    git_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    adapter = (
        FgisYearlyAdapter(
            timeout_seconds=args.timeout,
            use_environment_proxy=not args.ignore_environment_proxy,
        )
        if args.source == "yearly"
        else FgisAdapter(
            timeout_seconds=args.timeout,
            page_size=args.page_size,
            use_environment_proxy=not args.ignore_environment_proxy,
        )
    )
    result = run_fgis_pipeline(
        runtime_root=args.runtime_root.resolve(),
        adapter=adapter,
        git_head=git_head,
        cert_date_start=args.cert_date_start,
        cert_date_end=args.cert_date_end,
        publish=not args.candidate_only,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
