"""CLI for the idempotent same-day import-profit Release materializer."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "03_src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agri_research_agent.import_profit.config import load_soybean_config  # noqa: E402
from agri_research_agent.pipelines.import_profit_daily import (  # noqa: E402
    try_materialize_import_profit_business_day,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize one same-day import-profit runtime Release."
    )
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--external-input-root", required=True)
    parser.add_argument("--dce-input-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--business-date", required=True)
    parser.add_argument("--expected-runtime-release-id", required=True)
    parser.add_argument("--expected-runtime-index-sha256", required=True)
    parser.add_argument("--expected-manual-cnf-sha256")
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--calculated-at", required=True)
    parser.add_argument("--lock-timeout-seconds", type=float, default=10.0)
    return parser


def run_materialization(args: argparse.Namespace):
    return try_materialize_import_profit_business_day(
        args.runtime_root,
        external_input_root=args.external_input_root,
        dce_input_root=args.dce_input_root,
        business_date=date.fromisoformat(args.business_date),
        config=load_soybean_config(args.config),
        expected_runtime_release_id=args.expected_runtime_release_id,
        expected_runtime_index_sha256=args.expected_runtime_index_sha256,
        expected_manual_cnf_sha256=args.expected_manual_cnf_sha256,
        release_id=args.release_id,
        batch_id=args.batch_id,
        calculated_at=_datetime(args.calculated_at),
        lock_timeout_seconds=args.lock_timeout_seconds,
    )


def summary(result) -> dict[str, object]:
    return {
        "status": result.status,
        "business_date": result.business_date.isoformat(),
        "release_id": result.release_id,
        "generation": result.generation,
        "previous_release_id": result.previous_release_id,
        "appended_business_key_count": result.appended_business_key_count,
        "success_count_delta": result.success_count_delta,
        "incomplete_count_delta": result.incomplete_count_delta,
        "external_input_candidate_id": result.external_input_candidate_id,
        "dce_candidate_id": result.dce_candidate_id,
        "message": result.message,
        "total_seconds": round(result.total_seconds, 6),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_materialization(args)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": getattr(exc, "status", "failed"),
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:300],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(summary(result), ensure_ascii=False, sort_keys=True))
    return 0


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


if __name__ == "__main__":
    raise SystemExit(main())
