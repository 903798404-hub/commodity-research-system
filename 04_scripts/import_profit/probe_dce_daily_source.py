"""Bounded, read-only probe for AkShare futures_zh_spot."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import inspect
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "03_src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agri_research_agent.import_profit.dce_daily import (  # noqa: E402
    CAPTURE_TIMEZONE,
    CAPTURE_ZONE,
    PRICE_TYPE,
    SOURCE_FUNCTION,
    SpotFetcher,
    capture_gate_status,
    default_spot_fetcher,
    fetch_dce_morning_open_snapshot,
)


def run_probe(
    contract_codes: list[str],
    *,
    clock: Callable[[], datetime] | None = None,
    fetcher: SpotFetcher = default_spot_fetcher,
) -> dict[str, Any]:
    import akshare as ak

    now = clock or (lambda: datetime.now(CAPTURE_ZONE))
    started = now()
    if started.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    local = started.astimezone(CAPTURE_ZONE)
    business_date = local.date()
    result = fetch_dce_morning_open_snapshot(
        contract_codes,
        business_date,
        fetcher=fetcher,
        captured_at=local,
        enforce_capture_window=False,
    )
    completed = now()
    if completed.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    completed_local = completed.astimezone(CAPTURE_ZONE)
    contract_results = [
        item.bounded_quality_dict() for item in result.contract_results
    ]
    source_probe_success = result.is_usable
    gate = capture_gate_status(business_date, local)
    candidate_eligible_now = source_probe_success and gate == "valid"
    proxy_environment_present = any(
        bool(os.environ.get(name))
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    )
    return {
        "akshare_version": ak.__version__,
        "source_function": SOURCE_FUNCTION,
        "price_type": PRICE_TYPE,
        "function_exists": hasattr(ak, "futures_zh_spot"),
        "function_signature": str(inspect.signature(ak.futures_zh_spot)),
        "call_parameters": {
            "symbol": result.request_symbol,
            "market": "CF",
            "adjust": "0",
        },
        "capture_timezone": CAPTURE_TIMEZONE,
        "request_started_at": local.isoformat(),
        "request_completed_at": completed_local.isoformat(),
        "captured_at": result.captured_at.isoformat(),
        "business_date": business_date.isoformat(),
        "capture_window_status": gate,
        "proxy_environment_present": proxy_environment_present,
        "returned_fields": list(result.returned_fields),
        "record_count": result.source_row_count,
        "requested_contracts": list(result.requested_contracts),
        "target_contracts_complete": source_probe_success,
        "matched_contracts": [
            item["contract_code"]
            for item in contract_results
            if item["matched_source_symbol"] is not None
        ],
        "contracts": contract_results,
        "request_elapsed_seconds": result.elapsed_seconds,
        "source_probe_success": source_probe_success,
        "candidate_eligible_now": candidate_eligible_now,
        "final_status": "passed" if candidate_eligible_now else "failed",
        "exception_type": result.source_error_type,
        "exception": result.source_error_message,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a bounded read-only AkShare futures_zh_spot probe."
    )
    parser.add_argument("--contract-code", required=True, action="append")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = run_probe(args.contract_code)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["source_probe_success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
