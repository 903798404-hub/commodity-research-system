"""Manual Reuters SQL refresh entry point for morning external inputs."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "03_src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agri_research_agent.import_profit.config import load_soybean_config  # noqa: E402
from agri_research_agent.import_profit.morning_external_inputs import (  # noqa: E402
    store_morning_external_inputs_candidate,
)
from agri_research_agent.pipelines.import_profit_daily import (  # noqa: E402
    try_materialize_import_profit_business_day,
)
from build_reuters_candidate import build_reuters_candidate  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert an uploaded Reuters SQL dump and freeze same-day inputs."
    )
    parser.add_argument("--sql-path", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--business-date", required=True)
    parser.add_argument("--external-input-root", required=True)
    parser.add_argument("--dce-input-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--working-dir", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--source-file-uploaded-at", required=True)
    parser.add_argument("--prepared-at", required=True)
    parser.add_argument("--promoted-at")
    parser.add_argument("--expected-runtime-release-id", required=True)
    parser.add_argument("--expected-runtime-index-sha256", required=True)
    parser.add_argument("--expected-manual-cnf-sha256")
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--calculated-at", required=True)
    parser.add_argument("--lock-timeout-seconds", type=float, default=10.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        sql_path = Path(args.sql_path)
        if not sql_path.is_file():
            raise ValueError("sql-path must be a readable ordinary file")
        source_sha = _sha256(sql_path)
        source_size = sql_path.stat().st_size
        config = load_soybean_config(args.config)
        business_date = date.fromisoformat(args.business_date)
        prepared_at = _datetime(args.prepared_at)
        promoted_at = (
            datetime.now(timezone.utc)
            if args.promoted_at is None
            else _datetime(args.promoted_at)
        )
        working = Path(args.working_dir)
        resolved_working = working.resolve()
        if (
            resolved_working == REPOSITORY_ROOT
            or REPOSITORY_ROOT in resolved_working.parents
        ):
            raise ValueError("working-dir must remain outside the repository")
        working.mkdir(parents=True, exist_ok=True)
        reuters_dir = working / f"{args.batch_id}-reuters"
        build_reuters_candidate(
            sql_path,
            reuters_dir,
            generated_at=prepared_at,
        )
        if _sha256(sql_path) != source_sha or sql_path.stat().st_size != source_size:
            raise RuntimeError("source SQL identity changed during conversion")
        external = store_morning_external_inputs_candidate(
            args.external_input_root,
            reuters_candidate_dir=reuters_dir,
            business_date=business_date,
            config=config,
            candidate_id=args.batch_id,
            source_file_uploaded_at=_datetime(args.source_file_uploaded_at),
            prepared_at=prepared_at,
            promoted_at=promoted_at,
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
            "source_sql_filename": sql_path.name,
            "source_sql_size": source_size,
            "source_sql_sha256": source_sha,
            "external_input_status": external.status,
            "external_input_candidate_id": external.candidate.candidate_id,
            "external_candidate_status": external.candidate.candidate_status,
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


if __name__ == "__main__":
    raise SystemExit(main())
