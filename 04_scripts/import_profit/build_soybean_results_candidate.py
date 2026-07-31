"""Build an isolated soybean result candidate from standardized inputs."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "03_src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agri_research_agent.import_profit.cnf_store import (  # noqa: E402
    CNF_SCHEMA,
    load_cnf_store,
)
from agri_research_agent.import_profit.config import (  # noqa: E402
    SoybeanImportProfitConfig,
    load_soybean_config,
)
from agri_research_agent.import_profit.models import BusinessKey  # noqa: E402
from agri_research_agent.import_profit.result_store import (  # noqa: E402
    write_soybean_result_candidate,
)
from agri_research_agent.import_profit.standard_io import (  # noqa: E402
    load_cbot_parquet,
    load_dce_parquet,
    load_fx_parquet,
    optional_file_identity,
)
from agri_research_agent.pipelines.import_profit_results import (  # noqa: E402
    build_soybean_result_candidate,
)


KEY_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}),"
    r"(?P<origin>[^,]+),"
    r"(?P<shipment>\d{4}-(?:0[1-9]|1[0-2]))$"
)


class CandidateCliError(ValueError):
    """Raised for strict CLI input errors."""


def parse_explicit_key(
    value: str,
    config: SoybeanImportProfitConfig,
) -> BusinessKey:
    match = KEY_PATTERN.fullmatch(value)
    if match is None:
        raise CandidateCliError(
            "--key must use business_date,origin,shipment_period"
        )
    try:
        business_date = date.fromisoformat(match.group("date"))
    except ValueError as exc:
        raise CandidateCliError("--key business_date is invalid") from exc
    origin_text = match.group("origin").strip()
    origin_by_label = {origin.label: origin.code for origin in config.origins}
    origin = (
        origin_text
        if origin_text in config.origin_codes
        else origin_by_label.get(origin_text)
    )
    if origin is None:
        raise CandidateCliError("--key origin is not configured")
    shipment_year, shipment_month = (
        int(part) for part in match.group("shipment").split("-")
    )
    return BusinessKey(
        business_date,
        config.commodity,
        origin,
        shipment_year,
        shipment_month,
        config.origin_codes,
        config.commodity,
        match.group("shipment"),
    )


def parse_utc_datetime(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CandidateCliError(
            "--calculated-at must be an ISO-8601 UTC datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise CandidateCliError(
            "--calculated-at must be an ISO-8601 UTC datetime"
        )
    return parsed.astimezone(timezone.utc)


def build_from_args(args: argparse.Namespace) -> dict[str, object]:
    config = load_soybean_config(args.config)
    business_keys = tuple(
        parse_explicit_key(value, config) for value in args.key
    )
    calculated_at = (
        datetime.now(timezone.utc)
        if args.calculated_at is None
        else parse_utc_datetime(args.calculated_at)
    )
    cbot = load_cbot_parquet(args.cbot_parquet)
    fx = load_fx_parquet(args.fx_parquet)
    dce = load_dce_parquet(args.dce_parquet)
    cnf = load_cnf_store(
        args.cnf_store,
        allowed_origins=config.origin_codes,
    )
    cnf_identity = optional_file_identity(
        args.cnf_store,
        schema=CNF_SCHEMA,
        schema_version="cnf-store-v1",
        record_count=cnf.record_count,
        dates=tuple(
            record.business_key.business_date for record in cnf.records
        ),
    )
    candidate = build_soybean_result_candidate(
        business_keys,
        config=config,
        cnf_records=cnf.records,
        cbot_records=cbot.records,
        fx_records=fx.records,
        dce_records=dce.records,
        calculated_at=calculated_at,
        generated_at=calculated_at,
        input_files=(
            cnf_identity,
            cbot.identity,
            fx.identity,
            dce.identity,
        ),
        synthetic_input=False,
    )
    write_result = write_soybean_result_candidate(
        candidate,
        args.output_dir,
        repository_root=REPOSITORY_ROOT,
    )
    return {
        "candidate_status": write_result.candidate_status,
        "requested_key_count": candidate.recalculation_batch.requested_count,
        "success_count": candidate.recalculation_batch.success_count,
        "incomplete_count": candidate.recalculation_batch.incomplete_count,
        "output_dir_name": write_result.output_dir_name,
        "output_files": [
            {
                "filename": item.filename,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
            }
            for item in write_result.output_files
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build an isolated soybean snapshot and net-crush result candidate"
        )
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--cnf-store", required=True)
    parser.add_argument("--cbot-parquet", required=True)
    parser.add_argument("--fx-parquet", required=True)
    parser.add_argument("--dce-parquet", required=True)
    parser.add_argument(
        "--key",
        action="append",
        required=True,
        help="business_date,origin,shipment_period; repeat for multiple keys",
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--calculated-at",
        help="optional ISO-8601 UTC timestamp for reproducible acceptance",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        payload = build_from_args(parser.parse_args(argv))
    except Exception as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
