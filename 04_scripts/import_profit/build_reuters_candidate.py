"""Build an isolated Reuters candidate from a Navicat/MySQL SQL dump."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "03_src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agri_research_agent.import_profit.reuters_adapter import (  # noqa: E402
    CBOT_TABLE,
    FX_TABLE,
    ReutersAdapterResult,
    adapt_reuters_sql,
    inspect_source_identity,
    records_as_dicts,
)


CBOT_FILENAME = "cbot_soybean_daily.parquet"
FX_FILENAME = "usdcny_forward_daily.parquet"
MANIFEST_FILENAME = "manifest.json"
QUALITY_FILENAME = "quality_report.json"

CBOT_SCHEMA = pa.schema(
    [
        pa.field("market_date", pa.date32(), nullable=False),
        pa.field("contract_year", pa.int16(), nullable=False),
        pa.field("contract_month", pa.int8(), nullable=False),
        pa.field("price_cents_per_bushel", pa.float64(), nullable=False),
        pa.field("lead_months", pa.int16(), nullable=False),
        pa.field("exchange_quality_status", pa.string(), nullable=False),
        pa.field("is_usable", pa.bool_(), nullable=False),
        pa.field("eligible_for_import_profit", pa.bool_(), nullable=False),
        pa.field("source_table", pa.string(), nullable=False),
        pa.field("source_column", pa.string(), nullable=False),
        pa.field("source_statement_index", pa.int64(), nullable=False),
        pa.field("source_snapshot_sha256", pa.string(), nullable=False),
    ]
)
FX_SCHEMA = pa.schema(
    [
        pa.field("market_date", pa.date32(), nullable=False),
        pa.field("tenor_months", pa.int8(), nullable=False),
        pa.field("fx_value", pa.float64(), nullable=False),
        pa.field("unit", pa.string(), nullable=False),
        pa.field("source_table", pa.string(), nullable=False),
        pa.field("source_column", pa.string(), nullable=False),
        pa.field("source_statement_index", pa.int64(), nullable=False),
        pa.field("source_snapshot_sha256", pa.string(), nullable=False),
        pa.field("quality_status", pa.string(), nullable=False),
        pa.field("is_usable", pa.bool_(), nullable=False),
    ]
)


class ReutersCandidateBuildError(RuntimeError):
    pass


def build_reuters_candidate(
    source_sql: str | Path,
    output_dir: str | Path,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    source_path = Path(source_sql)
    destination = Path(output_dir)
    if source_path.resolve() == destination.resolve():
        raise ReutersCandidateBuildError("source SQL and output directory must be different")
    if destination.exists() and not destination.is_dir():
        raise ReutersCandidateBuildError("output-dir exists and is not a directory")
    if destination.exists() and any(destination.iterdir()):
        raise ReutersCandidateBuildError("output-dir must be empty")
    destination.mkdir(parents=True, exist_ok=True)

    result = adapt_reuters_sql(source_path)
    if not result.quality_report["candidate_pass"]:
        raise ReutersCandidateBuildError("adapter quality report rejected the candidate")

    timestamp = generated_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ReutersCandidateBuildError("generated_at must be timezone-aware")
    generated_at_text = timestamp.astimezone(timezone.utc).isoformat()

    finals = {
        CBOT_FILENAME: destination / CBOT_FILENAME,
        FX_FILENAME: destination / FX_FILENAME,
        QUALITY_FILENAME: destination / QUALITY_FILENAME,
        MANIFEST_FILENAME: destination / MANIFEST_FILENAME,
    }
    if any(path.exists() for path in finals.values()):
        raise ReutersCandidateBuildError("candidate output already exists")
    token = uuid.uuid4().hex
    temporaries = {
        name: destination / f".{name}.{token}.tmp" for name in finals
    }
    created_finals: list[Path] = []
    write_started = perf_counter()
    try:
        cbot_table = pa.Table.from_pylist(records_as_dicts(result.cbot_records), schema=CBOT_SCHEMA)
        fx_table = pa.Table.from_pylist(records_as_dicts(result.fx_records), schema=FX_SCHEMA)
        pq.write_table(cbot_table, temporaries[CBOT_FILENAME], compression="zstd")
        pq.write_table(fx_table, temporaries[FX_FILENAME], compression="zstd")
        _fsync_file(temporaries[CBOT_FILENAME])
        _fsync_file(temporaries[FX_FILENAME])
        _verify_parquet(temporaries[CBOT_FILENAME], CBOT_SCHEMA, len(result.cbot_records))
        _verify_parquet(temporaries[FX_FILENAME], FX_SCHEMA, len(result.fx_records))

        quality_report = dict(result.quality_report)
        quality_report["source_filename"] = result.source_identity.filename
        current_source_identity = inspect_source_identity(source_path)
        if current_source_identity != result.source_identity:
            raise ReutersCandidateBuildError(
                "source file identity changed during candidate build"
            )
        quality_report["fatal_checks"] = dict(quality_report["fatal_checks"])
        quality_report["fatal_checks"].update(
            {
                "source_identity_consistent": {"passed": True, "count": 0},
                "parquet_readback": {"passed": True, "count": 0},
                "manifest_hashes_match": {"passed": True, "count": 0},
                "json_round_trip": {"passed": True, "count": 0},
            }
        )
        _write_json(temporaries[QUALITY_FILENAME], quality_report)
        quality_identity = _file_identity(
            temporaries[QUALITY_FILENAME], filename=QUALITY_FILENAME
        )
        parquet_identities = {
            CBOT_FILENAME: _file_identity(
                temporaries[CBOT_FILENAME], filename=CBOT_FILENAME
            ),
            FX_FILENAME: _file_identity(
                temporaries[FX_FILENAME], filename=FX_FILENAME
            ),
        }
        manifest = _build_manifest(
            result,
            generated_at=generated_at_text,
            output_identities={
                **parquet_identities,
                QUALITY_FILENAME: quality_identity,
            },
        )
        _assert_manifest_is_safe(manifest)
        _write_json(temporaries[MANIFEST_FILENAME], manifest)

        for name in (CBOT_FILENAME, FX_FILENAME, QUALITY_FILENAME, MANIFEST_FILENAME):
            os.replace(temporaries[name], finals[name])
            created_finals.append(finals[name])

        _verify_parquet(finals[CBOT_FILENAME], CBOT_SCHEMA, len(result.cbot_records))
        _verify_parquet(finals[FX_FILENAME], FX_SCHEMA, len(result.fx_records))
        loaded_manifest = json.loads(finals[MANIFEST_FILENAME].read_text(encoding="utf-8"))
        loaded_quality = json.loads(finals[QUALITY_FILENAME].read_text(encoding="utf-8"))
        if loaded_manifest != manifest or loaded_quality != quality_report:
            raise ReutersCandidateBuildError("JSON candidate verification failed")
        for name, identity in manifest["output_files"].items():
            actual = _file_identity(finals[name])
            if actual != identity:
                raise ReutersCandidateBuildError(f"candidate file identity mismatch: {name}")
    except Exception:
        for path in temporaries.values():
            if path.exists():
                path.unlink()
        for path in created_finals:
            if path.exists():
                path.unlink()
        raise

    write_seconds = perf_counter() - write_started
    total_seconds = perf_counter() - started
    return {
        "output_dir": destination,
        "files": finals,
        "manifest": manifest,
        "quality_report": quality_report,
        "timings": {
            **result.timings,
            "candidate_write_seconds": write_seconds,
            "total_seconds": total_seconds,
        },
        "record_counts": {
            CBOT_TABLE: len(result.cbot_records),
            FX_TABLE: len(result.fx_records),
        },
    }


def _build_manifest(
    result: ReutersAdapterResult,
    *,
    generated_at: str,
    output_identities: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    manifest = dict(result.manifest_core)
    manifest["generated_at"] = generated_at
    manifest["output_files"] = output_identities
    manifest["output_file_names"] = [
        CBOT_FILENAME,
        FX_FILENAME,
        QUALITY_FILENAME,
        MANIFEST_FILENAME,
    ]
    manifest["output_sizes"] = {
        name: identity["size"] for name, identity in output_identities.items()
    }
    manifest["output_sha256"] = {
        name: identity["sha256"] for name, identity in output_identities.items()
    }
    manifest["parquet_schemas"] = {
        CBOT_FILENAME: str(CBOT_SCHEMA),
        FX_FILENAME: str(FX_SCHEMA),
    }
    return manifest


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    data = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if loaded != payload:
        raise ReutersCandidateBuildError(f"JSON round-trip failed: {path.name}")


def _verify_parquet(path: Path, expected_schema: pa.Schema, expected_rows: int) -> None:
    table = pq.read_table(path)
    if table.schema != expected_schema:
        raise ReutersCandidateBuildError(f"Parquet schema mismatch: {path.name}")
    if table.num_rows != expected_rows:
        raise ReutersCandidateBuildError(f"Parquet row count mismatch: {path.name}")


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _file_identity(path: Path, *, filename: str | None = None) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "filename": filename or path.name,
        "size": path.stat().st_size,
        "sha256": digest.hexdigest().upper(),
    }


def _assert_manifest_is_safe(manifest: dict[str, Any]) -> None:
    serialized = json.dumps(manifest, ensure_ascii=False)
    if re_search_absolute_path(serialized):
        raise ReutersCandidateBuildError("manifest contains an absolute path")
    forbidden = ("Source Server", "Source Host", "username", "password")
    if any(marker in serialized for marker in forbidden):
        raise ReutersCandidateBuildError("manifest contains forbidden connection identity")


def re_search_absolute_path(value: str) -> bool:
    import re

    return bool(re.search(r"(?i)(?:[A-Z]:[\\/]|/home/|/Users/)", value))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build isolated CBOT soybean and USD/CNY forward candidates from a SQL dump."
    )
    parser.add_argument("--source-sql", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = build_reuters_candidate(args.source_sql, args.output_dir)
    safe_output = {
        "output_dir": str(result["output_dir"]),
        "files": sorted(path.name for path in result["files"].values()),
        "record_counts": result["record_counts"],
        "candidate_status": result["quality_report"]["candidate_status"],
        "warning_count": result["quality_report"]["warning_count"],
        "warnings": [
            {"code": item["code"], "count": item["count"]}
            for item in result["quality_report"]["warnings"]
        ],
        "timings": result["timings"],
    }
    print(json.dumps(safe_output, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
