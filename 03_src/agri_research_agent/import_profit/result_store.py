"""Atomic persistence for isolated soybean result candidates."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from agri_research_agent.pipelines.import_profit_results import (
    SoybeanResultCandidate,
)


SNAPSHOT_FILENAME = "soybean_market_snapshots.parquet"
RESULT_FILENAME = "soybean_net_crush_results.parquet"
MANIFEST_FILENAME = "manifest.json"
QUALITY_FILENAME = "quality_report.json"
RESULT_SCHEMA_VERSION = "1"

SNAPSHOT_KEY_FIELDS = (
    "business_date",
    "commodity",
    "origin",
    "shipment_year",
    "shipment_month",
)

SNAPSHOT_SCHEMA = pa.schema(
    [
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("commodity", pa.string(), nullable=False),
        pa.field("origin", pa.string(), nullable=False),
        pa.field("shipment_year", pa.int16(), nullable=False),
        pa.field("shipment_month", pa.int8(), nullable=False),
        pa.field("shipment_period", pa.string(), nullable=False),
        pa.field("mapping_identity", pa.string(), nullable=False),
        pa.field("parameter_version", pa.string(), nullable=False),
        pa.field("cnf_cents_per_bushel", pa.float64(), nullable=True),
        pa.field("cnf_source", pa.string(), nullable=True),
        pa.field("cbot_contract_year", pa.int16(), nullable=False),
        pa.field("cbot_contract_month", pa.int8(), nullable=False),
        pa.field("cbot_price_cents_per_bushel", pa.float64(), nullable=True),
        pa.field("cbot_quality_status", pa.string(), nullable=True),
        pa.field("cbot_source", pa.string(), nullable=True),
        pa.field("cbot_source_snapshot_sha256", pa.string(), nullable=True),
        pa.field("fx_target_tenor", pa.int8(), nullable=False),
        pa.field("fx_value", pa.float64(), nullable=True),
        pa.field("fx_is_interpolated", pa.bool_(), nullable=False),
        pa.field("fx_lower_tenor", pa.int8(), nullable=True),
        pa.field("fx_upper_tenor", pa.int8(), nullable=True),
        pa.field("fx_selection_status", pa.string(), nullable=False),
        pa.field("fx_source", pa.string(), nullable=True),
        pa.field("fx_source_snapshot_sha256", pa.string(), nullable=True),
        pa.field("soymeal_contract_code", pa.string(), nullable=False),
        pa.field("soymeal_price_cny_per_tonne", pa.float64(), nullable=True),
        pa.field("soymeal_price_type", pa.string(), nullable=True),
        pa.field("soymeal_source", pa.string(), nullable=True),
        pa.field("soyoil_contract_code", pa.string(), nullable=False),
        pa.field("soyoil_price_cny_per_tonne", pa.float64(), nullable=True),
        pa.field("soyoil_price_type", pa.string(), nullable=True),
        pa.field("soyoil_source", pa.string(), nullable=True),
        pa.field("snapshot_status", pa.string(), nullable=False),
        pa.field("missing_reasons", pa.list_(pa.string()), nullable=False),
    ]
)
RESULT_SCHEMA = pa.schema(
    [
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("commodity", pa.string(), nullable=False),
        pa.field("origin", pa.string(), nullable=False),
        pa.field("shipment_year", pa.int16(), nullable=False),
        pa.field("shipment_month", pa.int8(), nullable=False),
        pa.field("shipment_period", pa.string(), nullable=False),
        pa.field("usd_cost_per_tonne", pa.float64(), nullable=True),
        pa.field("duty_paid_cost_cny_per_tonne", pa.float64(), nullable=True),
        pa.field("net_crush_margin_cny_per_tonne", pa.float64(), nullable=True),
        pa.field("calculation_status", pa.string(), nullable=False),
        pa.field("missing_reasons", pa.list_(pa.string()), nullable=False),
        pa.field("parameter_version", pa.string(), nullable=False),
        pa.field("mapping_identity", pa.string(), nullable=False),
        pa.field(
            "calculated_at",
            pa.timestamp("us", tz="UTC"),
            nullable=False,
        ),
    ]
)


class ResultStoreError(RuntimeError):
    """Base error for candidate persistence."""


class ResultStoreValidationError(ResultStoreError):
    """Raised before formal output replacement."""


class ResultStoreWriteError(ResultStoreError):
    """Raised when a candidate cannot be atomically persisted."""


@dataclass(frozen=True, slots=True)
class OutputFileIdentity:
    filename: str
    size_bytes: int
    sha256: str
    record_count: int | None
    schema_fingerprint: str | None

    def as_manifest_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "record_count": self.record_count,
            "schema_fingerprint": self.schema_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class CandidateWriteResult:
    output_dir_name: str
    candidate_status: str
    output_files: tuple[OutputFileIdentity, ...]


def write_soybean_result_candidate(
    candidate: SoybeanResultCandidate,
    output_dir: str | Path,
    *,
    repository_root: str | Path | None = None,
) -> CandidateWriteResult:
    """Write four verified files, cleaning all formal outputs on any failure."""

    if not isinstance(candidate, SoybeanResultCandidate):
        raise ResultStoreValidationError(
            "candidate must be a SoybeanResultCandidate"
        )
    destination = Path(output_dir)
    _validate_destination(destination, repository_root)
    destination.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    names = (
        SNAPSHOT_FILENAME,
        RESULT_FILENAME,
        QUALITY_FILENAME,
        MANIFEST_FILENAME,
    )
    temporaries = {
        name: destination / f".{name}.{token}.tmp" for name in names
    }
    finals = {name: destination / name for name in names}
    placed: list[Path] = []
    try:
        snapshot_rows = _snapshot_rows(candidate)
        result_rows = _result_rows(candidate)
        _write_parquet(
            temporaries[SNAPSHOT_FILENAME],
            SNAPSHOT_SCHEMA,
            snapshot_rows,
        )
        _write_parquet(
            temporaries[RESULT_FILENAME],
            RESULT_SCHEMA,
            result_rows,
        )
        _verify_parquet(
            temporaries[SNAPSHOT_FILENAME],
            SNAPSHOT_SCHEMA,
            len(snapshot_rows),
        )
        _verify_parquet(
            temporaries[RESULT_FILENAME],
            RESULT_SCHEMA,
            len(result_rows),
        )
        snapshot_identity = _parquet_identity(
            temporaries[SNAPSHOT_FILENAME],
            SNAPSHOT_FILENAME,
            SNAPSHOT_SCHEMA,
            len(snapshot_rows),
        )
        result_identity = _parquet_identity(
            temporaries[RESULT_FILENAME],
            RESULT_FILENAME,
            RESULT_SCHEMA,
            len(result_rows),
        )

        quality_payload = _quality_payload(candidate)
        _write_json(temporaries[QUALITY_FILENAME], quality_payload)
        _verify_json(temporaries[QUALITY_FILENAME], quality_payload)
        quality_identity = _json_identity(
            temporaries[QUALITY_FILENAME],
            QUALITY_FILENAME,
        )
        manifest_payload = _manifest_payload(
            candidate,
            (snapshot_identity, result_identity, quality_identity),
        )
        _write_json(temporaries[MANIFEST_FILENAME], manifest_payload)
        _verify_json(temporaries[MANIFEST_FILENAME], manifest_payload)
        manifest_identity = _json_identity(
            temporaries[MANIFEST_FILENAME],
            MANIFEST_FILENAME,
        )

        for name in names:
            os.replace(temporaries[name], finals[name])
            placed.append(finals[name])
        _verify_final_identity(finals[SNAPSHOT_FILENAME], snapshot_identity)
        _verify_final_identity(finals[RESULT_FILENAME], result_identity)
        _verify_final_identity(finals[QUALITY_FILENAME], quality_identity)
        _verify_final_identity(finals[MANIFEST_FILENAME], manifest_identity)
        return CandidateWriteResult(
            output_dir_name=destination.name,
            candidate_status=candidate.candidate_status.value,
            output_files=(
                snapshot_identity,
                result_identity,
                quality_identity,
                manifest_identity,
            ),
        )
    except Exception as exc:
        for path in placed:
            if path.exists():
                path.unlink()
        for path in temporaries.values():
            if path.exists():
                path.unlink()
        if isinstance(exc, ResultStoreError):
            raise
        raise ResultStoreWriteError(f"candidate write failed: {exc}") from exc


def _validate_destination(
    destination: Path,
    repository_root: str | Path | None,
) -> None:
    if destination.exists() and not destination.is_dir():
        raise ResultStoreValidationError(
            "output-dir exists and is not a directory"
        )
    if destination.exists() and any(destination.iterdir()):
        raise ResultStoreValidationError("output-dir must be empty")
    if repository_root is not None:
        root = Path(repository_root).resolve()
        resolved = destination.resolve()
        if resolved == root or root in resolved.parents:
            raise ResultStoreValidationError(
                "candidate output-dir must be outside the repository"
            )


def _snapshot_rows(candidate: SoybeanResultCandidate) -> list[dict[str, Any]]:
    rows = []
    for item in candidate.recalculation_batch.items:
        snapshot = item.market_snapshot
        key = snapshot.business_key
        rows.append(
            {
                "business_date": key.business_date,
                "commodity": key.commodity,
                "origin": key.origin,
                "shipment_year": key.shipment_year,
                "shipment_month": key.shipment_month,
                "shipment_period": key.shipment_period,
                "mapping_identity": snapshot.mapping_identity,
                "parameter_version": snapshot.parameter_version,
                "cnf_cents_per_bushel": snapshot.cnf_cents_per_bushel,
                "cnf_source": snapshot.cnf_source,
                "cbot_contract_year": snapshot.cbot_contract_year,
                "cbot_contract_month": snapshot.cbot_contract_month,
                "cbot_price_cents_per_bushel": snapshot.cbot_price_cents_per_bushel,
                "cbot_quality_status": snapshot.cbot_exchange_quality_status,
                "cbot_source": snapshot.cbot_source,
                "cbot_source_snapshot_sha256": snapshot.cbot_source_snapshot_sha256,
                "fx_target_tenor": snapshot.fx_target_tenor,
                "fx_value": snapshot.fx_value,
                "fx_is_interpolated": snapshot.fx_is_interpolated,
                "fx_lower_tenor": snapshot.fx_lower_tenor,
                "fx_upper_tenor": snapshot.fx_upper_tenor,
                "fx_selection_status": snapshot.fx_selection_status.value,
                "fx_source": snapshot.fx_source,
                "fx_source_snapshot_sha256": snapshot.fx_source_snapshot_sha256,
                "soymeal_contract_code": snapshot.soymeal_contract_code,
                "soymeal_price_cny_per_tonne": snapshot.soymeal_price_cny_per_tonne,
                "soymeal_price_type": snapshot.soymeal_price_type,
                "soymeal_source": snapshot.soymeal_source,
                "soyoil_contract_code": snapshot.soyoil_contract_code,
                "soyoil_price_cny_per_tonne": snapshot.soyoil_price_cny_per_tonne,
                "soyoil_price_type": snapshot.soyoil_price_type,
                "soyoil_source": snapshot.soyoil_source,
                "snapshot_status": snapshot.snapshot_status.value,
                "missing_reasons": [
                    reason.value for reason in snapshot.missing_reasons
                ],
            }
        )
    _validate_rows(rows)
    return rows


def _result_rows(candidate: SoybeanResultCandidate) -> list[dict[str, Any]]:
    rows = []
    for item in candidate.recalculation_batch.items:
        result = item.calculation_result
        key = item.business_key
        rows.append(
            {
                "business_date": key.business_date,
                "commodity": key.commodity,
                "origin": key.origin,
                "shipment_year": key.shipment_year,
                "shipment_month": key.shipment_month,
                "shipment_period": key.shipment_period,
                "usd_cost_per_tonne": result.usd_cost_per_tonne,
                "duty_paid_cost_cny_per_tonne": result.duty_paid_cost_cny_per_tonne,
                "net_crush_margin_cny_per_tonne": result.net_crush_margin_cny_per_tonne,
                "calculation_status": result.calculation_status.value,
                "missing_reasons": [
                    reason.value for reason in result.missing_reasons
                ],
                "parameter_version": result.parameter_version,
                "mapping_identity": result.mapping_identity,
                "calculated_at": candidate.calculated_at,
            }
        )
    _validate_rows(rows)
    return rows


def _validate_rows(rows: list[dict[str, Any]]) -> None:
    keys = [tuple(row[field] for field in SNAPSHOT_KEY_FIELDS) for row in rows]
    if len(keys) != len(set(keys)):
        raise ResultStoreValidationError("candidate rows contain duplicate keys")
    if keys != sorted(keys):
        raise ResultStoreValidationError("candidate rows are not stably sorted")


def _quality_payload(candidate: SoybeanResultCandidate) -> dict[str, Any]:
    batch = candidate.recalculation_batch
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "synthetic_input": candidate.synthetic_input,
        "requested_keys": [_key_dict(key) for key in batch.requested_keys],
        "key_results": [
            {
                "business_key": _key_dict(item.business_key),
                "snapshot_status": item.market_snapshot.snapshot_status.value,
                "calculation_status": item.calculation_result.calculation_status.value,
                "missing_reasons": [
                    reason.value
                    for reason in item.calculation_result.missing_reasons
                ],
            }
            for item in batch.items
        ],
        "sources": [
            {
                "filename": identity.filename,
                "record_count": identity.record_count,
                "schema_check": "passed",
                "duplicate_key_count": identity.duplicate_key_count,
                "date_range": {
                    "earliest": _date_text(identity.earliest_date),
                    "latest": _date_text(identity.latest_date),
                },
            }
            for identity in candidate.input_files
        ],
        "success_count": batch.success_count,
        "incomplete_count": batch.incomplete_count,
        "missing_reason_counts": {
            reason.value: count for reason, count in batch.missing_reason_counts
        },
        "fatal_issues": [],
        "warnings": (
            []
            if batch.incomplete_count == 0
            else ["one_or_more_requested_keys_are_incomplete"]
        ),
        "candidate_status": candidate.candidate_status.value,
    }


def _manifest_payload(
    candidate: SoybeanResultCandidate,
    output_files: tuple[OutputFileIdentity, ...],
) -> dict[str, Any]:
    batch = candidate.recalculation_batch
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "pipeline_version": candidate.pipeline_version,
        "candidate_status": candidate.candidate_status.value,
        "synthetic_input": candidate.synthetic_input,
        "commodity": "soybean",
        "requested_key_count": batch.requested_count,
        "success_count": batch.success_count,
        "incomplete_count": batch.incomplete_count,
        "missing_reason_counts": {
            reason.value: count for reason, count in batch.missing_reason_counts
        },
        "requested_keys": [_key_dict(key) for key in batch.requested_keys],
        "config_schema_version": int(batch.parameter_version),
        "parameter_version": batch.parameter_version,
        "mapping_identity": batch.mapping_identity,
        "input_files": [
            identity.as_manifest_dict() for identity in candidate.input_files
        ],
        "output_files": [
            identity.as_manifest_dict() for identity in output_files
        ],
        "generated_at": candidate.generated_at.isoformat().replace("+00:00", "Z"),
    }


def _write_parquet(
    path: Path,
    schema: pa.Schema,
    rows: list[dict[str, Any]],
) -> None:
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, path, compression="zstd")
    _fsync(path)


def _verify_parquet(
    path: Path,
    schema: pa.Schema,
    expected_rows: int,
) -> None:
    arrow_table = pq.read_table(path)
    if arrow_table.schema != schema or arrow_table.num_rows != expected_rows:
        raise ResultStoreValidationError(
            "Arrow Parquet verification failed"
        )
    pandas_frame = pd.read_parquet(path)
    if (
        tuple(pandas_frame.columns) != tuple(schema.names)
        or len(pandas_frame) != expected_rows
    ):
        raise ResultStoreValidationError(
            "pandas Parquet verification failed"
        )
    pandas_keys = [
        tuple(row[field] for field in SNAPSHOT_KEY_FIELDS)
        for row in pandas_frame.to_dict("records")
    ]
    if len(pandas_keys) != len(set(pandas_keys)) or pandas_keys != sorted(
        pandas_keys
    ):
        raise ResultStoreValidationError(
            "Parquet key verification failed"
        )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _verify_json(path: Path, expected: dict[str, Any]) -> None:
    with path.open("r", encoding="utf-8", errors="strict") as stream:
        actual = json.load(stream)
    if actual != expected:
        raise ResultStoreValidationError("JSON round-trip verification failed")


def _parquet_identity(
    path: Path,
    filename: str,
    schema: pa.Schema,
    record_count: int,
) -> OutputFileIdentity:
    return OutputFileIdentity(
        filename=filename,
        size_bytes=path.stat().st_size,
        sha256=_file_sha256(path),
        record_count=record_count,
        schema_fingerprint=_schema_fingerprint(schema),
    )


def _json_identity(path: Path, filename: str) -> OutputFileIdentity:
    return OutputFileIdentity(
        filename=filename,
        size_bytes=path.stat().st_size,
        sha256=_file_sha256(path),
        record_count=None,
        schema_fingerprint=None,
    )


def _verify_final_identity(path: Path, identity: OutputFileIdentity) -> None:
    if path.stat().st_size != identity.size_bytes or _file_sha256(path) != identity.sha256:
        raise ResultStoreWriteError(
            f"final output identity mismatch: {identity.filename}"
        )


def _key_dict(key) -> dict[str, object]:
    return {
        "business_date": key.business_date.isoformat(),
        "commodity": key.commodity,
        "origin": key.origin,
        "shipment_year": key.shipment_year,
        "shipment_month": key.shipment_month,
        "shipment_period": key.shipment_period,
    }


def _schema_fingerprint(schema: pa.Schema) -> str:
    return hashlib.sha256(schema.serialize().to_pybytes()).hexdigest().upper()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _fsync(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _date_text(value: date | None) -> str | None:
    return None if value is None else value.isoformat()
