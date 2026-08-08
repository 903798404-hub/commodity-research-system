from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "03_src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agri_research_agent.soybean_exports.common import resolve_runtime_git_head
from agri_research_agent.soybean_exports.fas import (
    FasAdapter,
    resolve_fas_api_key,
    run_fas_pipeline,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run USDA FAS ESR soybean export sales pipeline")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--market-year", type=int, action="append", dest="market_years")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--candidate-only", action="store_true")
    parser.add_argument("--allow-usda-api-key-fallback", action="store_true")
    parser.add_argument("--ignore-environment-proxy", action="store_true")
    args = parser.parse_args()
    api_key, _credential_source = resolve_fas_api_key(
        os.environ,
        allow_development_fallback=args.allow_usda_api_key_fallback,
    )
    git_head = resolve_runtime_git_head(project_root=PROJECT_ROOT)
    result = run_fas_pipeline(
        runtime_root=args.runtime_root.resolve(),
        adapter=FasAdapter(
            api_key=api_key,
            timeout_seconds=args.timeout,
            use_environment_proxy=not args.ignore_environment_proxy,
        ),
        git_head=git_head,
        report_market_years=args.market_years,
        publish=not args.candidate_only,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
