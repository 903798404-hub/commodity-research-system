"""Publish one strict manual-CNF transaction to a runtime Release."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "03_src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agri_research_agent.import_profit.config import load_soybean_config  # noqa: E402
from agri_research_agent.import_profit.models import BusinessKey  # noqa: E402
from agri_research_agent.import_profit.runtime_store import (  # noqa: E402
    parse_utc_text,
)
from agri_research_agent.pipelines.import_profit_runtime import (  # noqa: E402
    RuntimeCnfUpdate,
    update_runtime_cnf_quotes,
)


UPDATE_FIELDS = {
    "business_date",
    "origin",
    "shipment_period",
    "cnf_cents_per_bushel",
}


class RuntimeCliError(ValueError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Atomically update manual soybean CNF quotes."
    )
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--expected-release-id", required=True)
    parser.add_argument("--expected-index-sha256", required=True)
    parser.add_argument(
        "--expected-manual-cnf-sha256",
        required=True,
        help="Use NONE when the current Release has no manual CNF file.",
    )
    parser.add_argument("--updates-json", required=True)
    parser.add_argument("--calculated-at", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument(
        "--lock-timeout-seconds", type=float, default=10.0
    )
    return parser


def load_updates(path: Path, *, config, updated_at, batch_id):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeCliError("updates JSON is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"updates"}:
        raise RuntimeCliError(
            "updates JSON root must contain only updates"
        )
    raw_updates = payload["updates"]
    if not isinstance(raw_updates, list) or not raw_updates:
        raise RuntimeCliError("updates must be a non-empty list")
    updates = []
    for item in raw_updates:
        if not isinstance(item, dict) or set(item) != UPDATE_FIELDS:
            raise RuntimeCliError(
                "each update must contain the fixed fields"
            )
        try:
            business_date = date.fromisoformat(item["business_date"])
            year_text, month_text = item["shipment_period"].split("-")
            shipment_year = int(year_text)
            shipment_month = int(month_text)
        except Exception as exc:
            raise RuntimeCliError(
                "update date or shipment_period is invalid"
            ) from exc
        key = BusinessKey(
            business_date,
            config.commodity,
            item["origin"],
            shipment_year,
            shipment_month,
            config.origin_codes,
            config.commodity,
            item["shipment_period"],
        )
        updates.append(
            RuntimeCnfUpdate(
                business_key=key,
                cnf_cents_per_bushel=item["cnf_cents_per_bushel"],
                updated_at=updated_at,
                batch_id=batch_id,
            )
        )
    return tuple(updates)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config_path = Path(args.config)
        config = load_soybean_config(config_path)
        calculated_at = parse_utc_text(
            args.calculated_at, "calculated_at"
        )
        updates = load_updates(
            Path(args.updates_json),
            config=config,
            updated_at=calculated_at,
            batch_id=args.batch_id,
        )
        expected_manual = (
            None
            if args.expected_manual_cnf_sha256.upper() == "NONE"
            else args.expected_manual_cnf_sha256.upper()
        )
        result = update_runtime_cnf_quotes(
            Path(args.runtime_root),
            updates,
            config=config,
            config_path=config_path,
            expected_release_id=args.expected_release_id,
            expected_index_sha256=args.expected_index_sha256.upper(),
            expected_manual_cnf_sha256=expected_manual,
            calculated_at=calculated_at,
            batch_id=args.batch_id,
            release_id=args.release_id,
            lock_timeout_seconds=args.lock_timeout_seconds,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": getattr(exc, "status", "failed"),
                    "error": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": result.status,
                "release_id": result.release_id,
                "generation": result.generation,
                "changed_count": result.changed_count,
                "unchanged_count": result.unchanged_count,
                "index_sha256": result.index_sha256,
                "manual_cnf_sha256": result.manual_cnf_sha256,
                "timings": dict(result.timings),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
