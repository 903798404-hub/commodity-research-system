"""09:00 DCE morning-open capture entry point, independent of Reuters inputs."""

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
from agri_research_agent.import_profit.daily_increment import (  # noqa: E402
    capture_and_store_dce_morning_input,
)
from agri_research_agent.pipelines.import_profit_daily import (  # noqa: E402
    try_materialize_import_profit_business_day,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture one complete DCE morning-open batch and try daily materialization."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--business-date", required=True)
    parser.add_argument("--dce-input-root", required=True)
    parser.add_argument("--external-input-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--snapshot-batch-id", required=True)
    parser.add_argument("--expected-runtime-release-id", required=True)
    parser.add_argument("--expected-runtime-index-sha256", required=True)
    parser.add_argument("--expected-manual-cnf-sha256")
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--calculated-at", required=True)
    parser.add_argument("--lock-timeout-seconds", type=float, default=10.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_soybean_config(args.config)
        business_date = date.fromisoformat(args.business_date)
        capture = capture_and_store_dce_morning_input(
            args.dce_input_root,
            business_date=business_date,
            config=config,
            candidate_id=args.candidate_id,
            snapshot_batch_id=args.snapshot_batch_id,
            lock_timeout_seconds=args.lock_timeout_seconds,
        )
        materialized = try_materialize_import_profit_business_day(
            args.runtime_root,
            external_input_root=args.external_input_root,
            dce_input_root=args.dce_input_root,
            business_date=business_date,
            config=config,
            expected_runtime_release_id=args.expected_runtime_release_id,
            expected_runtime_index_sha256=args.expected_runtime_index_sha256,
            expected_manual_cnf_sha256=args.expected_manual_cnf_sha256,
            release_id=args.release_id,
            batch_id=args.batch_id,
            calculated_at=_datetime(args.calculated_at),
            lock_timeout_seconds=args.lock_timeout_seconds,
        )
        payload = {
            "status": "success",
            "business_date": business_date.isoformat(),
            "dce_candidate_id": capture.outcome.candidate_id,
            "dce_capture_status": capture.outcome.attempt_status,
            "requested_contract_count": len(capture.outcome.requested_contracts),
            "available_contract_count": len(capture.outcome.available_contracts),
            "missing_contract_count": len(capture.outcome.missing_contracts),
            "materialization_status": materialized.status,
            "release_id": materialized.release_id,
            "generation": materialized.generation,
        }
    except Exception as exc:
        payload = {
            "status": getattr(exc, "status", "failed"),
            "error_type": type(exc).__name__,
            "message": str(exc)[:300],
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


if __name__ == "__main__":
    raise SystemExit(main())
