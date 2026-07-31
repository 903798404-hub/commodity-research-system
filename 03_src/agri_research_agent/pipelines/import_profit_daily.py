"""Idempotent daily morning-input append into the immutable runtime Release."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import shutil
from time import perf_counter
import uuid

from filelock import FileLock, Timeout
import pyarrow.parquet as pq

from agri_research_agent.import_profit.config import SoybeanImportProfitConfig
from agri_research_agent.import_profit.cnf_store import CNF_SCHEMA
from agri_research_agent.import_profit.daily_increment import (
    DCE_FILENAME,
    empty_daily_cnf_records,
    generate_daily_business_keys,
    load_current_dce_outcome,
)
from agri_research_agent.import_profit.morning_external_inputs import (
    CBOT_FILENAME,
    FX_FILENAME,
    MANIFEST_FILENAME as INPUT_MANIFEST_FILENAME,
    load_current_external_input_candidate,
)
from agri_research_agent.import_profit.query import (
    HISTORICAL_BUSINESS_KEY_SCHEMA,
    load_soybean_query_dataset,
)
from agri_research_agent.import_profit.result_store import (
    RESULT_SCHEMA,
    SNAPSHOT_SCHEMA,
    _result_rows,
    _snapshot_rows,
)
from agri_research_agent.import_profit.runtime_store import (
    BUSINESS_KEYS_FILENAME,
    LOCK_FILENAME,
    MANIFEST_FILENAME,
    MANUAL_CNF_FILENAME,
    QUALITY_FILENAME,
    RESULTS_FILENAME,
    RUNTIME_CONTRACT_VERSION,
    RUNTIME_SCHEMA_VERSION,
    SNAPSHOTS_FILENAME,
    ImportProfitRuntimePaths,
    RuntimeConcurrentUpdateError,
    RuntimeLockedError,
    file_sha256,
    json_identity,
    load_runtime_release_dataset,
    parquet_identity,
    promote_prepared_runtime_release,
    read_json,
    utc_text,
    validate_release_id,
    write_json_exclusive,
    write_parquet,
)
from agri_research_agent.import_profit.standard_io import (
    load_cbot_parquet,
    load_dce_parquet,
    load_fx_parquet,
)
from agri_research_agent.pipelines.import_profit_results import (
    build_soybean_result_candidate,
)


@dataclass(frozen=True, slots=True)
class DailyMaterializationResult:
    status: str
    business_date: date
    release_id: str
    generation: int
    previous_release_id: str | None
    index_sha256: str
    appended_business_key_count: int
    success_count_delta: int
    incomplete_count_delta: int
    external_input_candidate_id: str | None
    dce_candidate_id: str | None
    message: str
    total_seconds: float


class DailyMaterializationError(RuntimeError):
    status = "validation_failed"


def try_materialize_import_profit_business_day(
    runtime_root: str | Path,
    *,
    external_input_root: str | Path,
    dce_input_root: str | Path,
    business_date: date,
    config: SoybeanImportProfitConfig,
    expected_runtime_release_id: str,
    expected_runtime_index_sha256: str,
    expected_manual_cnf_sha256: str | None,
    release_id: str,
    batch_id: str,
    calculated_at: datetime,
    lock_timeout_seconds: float = 10.0,
) -> DailyMaterializationResult:
    """Materialize exactly once when same-day external and DCE inputs are ready."""

    started = perf_counter()
    release_id = validate_release_id(release_id)
    if not isinstance(batch_id, str) or not batch_id:
        raise DailyMaterializationError("batch_id must be non-empty")
    try:
        external = load_current_external_input_candidate(external_input_root)
    except Exception as exc:
        if _is_missing_index(exc):
            return _waiting(
                "waiting_for_external_inputs",
                business_date,
                expected_runtime_release_id,
                "same-day external inputs are not ready",
                started,
            )
        raise
    try:
        dce = load_current_dce_outcome(dce_input_root)
    except Exception as exc:
        if _is_missing_index(exc):
            return _waiting(
                "waiting_for_dce_capture",
                business_date,
                expected_runtime_release_id,
                "same-day DCE capture outcome is not ready",
                started,
                external_candidate_id=external.candidate_id,
            )
        raise
    if external.business_date != business_date or dce.business_date != business_date:
        raise DailyMaterializationError(
            "input business_date conflict; prior-day inputs are forbidden"
        )
    if external.candidate_status not in {"passed", "passed_with_incomplete"}:
        raise DailyMaterializationError("external input candidate status is invalid")
    if dce.attempt_status not in {"passed", "failed"}:
        raise DailyMaterializationError("DCE capture outcome status is invalid")

    paths = ImportProfitRuntimePaths(Path(runtime_root))
    lock = FileLock(str(paths.runtime_root / LOCK_FILENAME))
    try:
        lock.acquire(timeout=lock_timeout_seconds)
    except Timeout as exc:
        raise RuntimeLockedError("runtime lock acquisition timed out") from exc
    building = paths.releases_dir / f".building-{uuid.uuid4().hex}"
    try:
        loaded = load_runtime_release_dataset(
            paths.runtime_root,
            expected_release_id=expected_runtime_release_id,
            expected_index_sha256=expected_runtime_index_sha256,
        )
        current = loaded.resolved
        if current.identity.manual_cnf_sha256 != expected_manual_cnf_sha256:
            raise RuntimeConcurrentUpdateError(
                "expected manual CNF identity is stale"
            )
        keys = generate_daily_business_keys(business_date, config)
        expected_keys = {
            (
                key.business_date,
                key.commodity,
                key.origin,
                key.shipment_year,
                key.shipment_month,
            )
            for key in keys
        }
        existing = {
            record.key
            for record in loaded.dataset.records
            if record.business_date == business_date
        }
        if existing:
            if existing == expected_keys and len(existing) == 48:
                return DailyMaterializationResult(
                    status="already_materialized",
                    business_date=business_date,
                    release_id=current.release_id,
                    generation=current.generation,
                    previous_release_id=current.previous_release_id,
                    index_sha256=current.identity.index_sha256,
                    appended_business_key_count=0,
                    success_count_delta=0,
                    incomplete_count_delta=0,
                    external_input_candidate_id=external.candidate_id,
                    dce_candidate_id=dce.candidate_id,
                    message=(
                        "business date already has a frozen market baseline; "
                        "a separate controlled market-data revision is required"
                    ),
                    total_seconds=perf_counter() - started,
                )
            raise DailyMaterializationError(
                "Current Release contains a partial or unexpected target-date key set"
            )

        external_manifest = read_json(
            external.candidate_dir / INPUT_MANIFEST_FILENAME,
            "external-input Manifest",
        )
        dce_manifest = read_json(
            dce.candidate_dir / INPUT_MANIFEST_FILENAME,
            "DCE input Manifest",
        )
        cbot = load_cbot_parquet(external.candidate_dir / CBOT_FILENAME)
        fx = load_fx_parquet(external.candidate_dir / FX_FILENAME)
        dce_records = (
            load_dce_parquet(dce.candidate_dir / DCE_FILENAME).records
            if dce.attempt_status == "passed"
            else ()
        )
        candidate = build_soybean_result_candidate(
            keys,
            config=config,
            cnf_records=empty_daily_cnf_records(
                keys, calculated_at=calculated_at, batch_id=batch_id
            ),
            cbot_records=cbot.records,
            fx_records=fx.records,
            dce_records=dce_records,
            calculated_at=calculated_at,
            generated_at=calculated_at,
            input_files=(cbot.identity, fx.identity),
            synthetic_input=False,
        )
        new_key_rows = [
            {
                "business_date": key.business_date,
                "commodity": key.commodity,
                "origin": key.origin,
                "shipment_year": key.shipment_year,
                "shipment_month": key.shipment_month,
                "shipment_period": key.shipment_period,
                "cnf_is_null": True,
                "cnf_source": "manual_ui",
            }
            for key in candidate.recalculation_batch.requested_keys
        ]
        new_snapshot_rows = _snapshot_rows(candidate)
        new_result_rows = _result_rows(candidate)
        old_key_rows = pq.read_table(current.business_keys_path).to_pylist()
        old_snapshot_rows = pq.read_table(current.snapshots_path).to_pylist()
        old_result_rows = pq.read_table(current.results_path).to_pylist()
        old_rows_copy = (
            [dict(row) for row in old_key_rows],
            [dict(row) for row in old_snapshot_rows],
            [dict(row) for row in old_result_rows],
        )
        key_rows = _sorted_rows(old_key_rows + new_key_rows)
        snapshot_rows = _sorted_rows(old_snapshot_rows + new_snapshot_rows)
        result_rows = _sorted_rows(old_result_rows + new_result_rows)

        building.mkdir()
        identities = {
            BUSINESS_KEYS_FILENAME: write_parquet(
                building / BUSINESS_KEYS_FILENAME,
                HISTORICAL_BUSINESS_KEY_SCHEMA,
                key_rows,
            ),
            SNAPSHOTS_FILENAME: write_parquet(
                building / SNAPSHOTS_FILENAME,
                SNAPSHOT_SCHEMA,
                snapshot_rows,
            ),
            RESULTS_FILENAME: write_parquet(
                building / RESULTS_FILENAME,
                RESULT_SCHEMA,
                result_rows,
            ),
        }
        manual_before = current.identity.manual_cnf_sha256
        if current.manual_cnf_exists:
            shutil.copy2(current.manual_cnf_path, building / MANUAL_CNF_FILENAME)
            identities[MANUAL_CNF_FILENAME] = parquet_identity(
                building / MANUAL_CNF_FILENAME,
                MANUAL_CNF_FILENAME,
                CNF_SCHEMA,
            )
        new_dataset = load_soybean_query_dataset(
            building / BUSINESS_KEYS_FILENAME,
            building / SNAPSHOTS_FILENAME,
            building / RESULTS_FILENAME,
        )
        if new_dataset.business_key_count != loaded.dataset.business_key_count + 48:
            raise DailyMaterializationError("daily append did not add exactly 48 keys")
        _assert_old_rows_preserved(
            old_rows_copy, key_rows, snapshot_rows, result_rows
        )
        missing_counts = dict(
            sorted(
                Counter(
                    reason
                    for record in new_dataset.records
                    for reason in record.missing_reasons
                ).items()
            )
        )
        quality = {
            "transaction": "daily_morning_inputs_append",
            "business_date": business_date.isoformat(),
            "source_file_uploaded_at": external_manifest[
                "source_file_uploaded_at"
            ],
            "external_input_candidate_id": external.candidate_id,
            "external_input_status": external.candidate_status,
            "required_cbot_contracts": list(external.required_cbot_contracts),
            "missing_cbot_contracts": list(external.missing_cbot_contracts),
            "direct_fx_count": external.direct_fx_count,
            "interpolated_fx_count": external.interpolated_fx_count,
            "missing_fx_count": external.missing_fx_count,
            "dce_capture_status": dce.attempt_status,
            "dce_requested_contracts": list(dce.requested_contracts),
            "dce_missing_contracts": list(dce.missing_contracts),
            "business_key_count_check": 48,
            "origin_counts": dict(
                sorted(Counter(key.origin for key in keys).items())
            ),
            "shipment_months": list(range(1, 13)),
            "shipment_year_samples": sorted(
                {key.shipment_period for key in keys}
            )[:12],
            "missing_reason_counts": missing_counts,
            "old_record_protection": "full_row_equality",
            "manual_cnf_protection": "byte_identity",
            "current_previous_relation": {
                "current": release_id,
                "previous": current.release_id,
            },
            "waiting_status": None,
            "fatal": [],
            "warning": ["new_records_waiting_for_manual_cnf"],
            "final_status": "passed_with_incomplete",
        }
        write_json_exclusive(building / QUALITY_FILENAME, quality)
        identities[QUALITY_FILENAME] = json_identity(
            building / QUALITY_FILENAME, QUALITY_FILENAME
        )
        start_date = current.manifest.get("morning_open_snapshot_start_date")
        if start_date is None and dce.attempt_status == "passed":
            start_date = business_date.isoformat()
        manual_count = (
            pq.read_table(building / MANUAL_CNF_FILENAME).num_rows
            if (building / MANUAL_CNF_FILENAME).is_file()
            else 0
        )
        manual_sha = (
            file_sha256(building / MANUAL_CNF_FILENAME)
            if (building / MANUAL_CNF_FILENAME).is_file()
            else None
        )
        if manual_sha != manual_before:
            raise DailyMaterializationError("manual CNF bytes changed during append")
        source_counts = _source_counts(new_dataset.records)
        price_type_counts = _price_type_counts(new_dataset.records)
        manifest = {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
            "release_id": release_id,
            "parent_release_id": current.release_id,
            "release_reason": "daily_morning_inputs_append",
            "generation": current.generation + 1,
            "business_date": business_date.isoformat(),
            "created_at": utc_text(calculated_at),
            "calculated_at": utc_text(calculated_at),
            "batch_id": batch_id,
            "external_input_candidate_id": external.candidate_id,
            "external_input_manifest_sha256": external.manifest_sha256,
            "external_input_status": external.candidate_status,
            "source_sql_sha256": external.source_sql_sha256,
            "source_file_uploaded_at": external_manifest[
                "source_file_uploaded_at"
            ],
            "external_inputs_promoted_at": external_manifest["promoted_at"],
            "dce_candidate_id": dce.candidate_id,
            "dce_capture_status": dce.attempt_status,
            "dce_snapshot_batch_id": dce.snapshot_batch_id,
            "dce_captured_at": dce.captured_at.isoformat(),
            "morning_open_snapshot_start_date": start_date,
            "appended_business_key_count": 48,
            "record_count": new_dataset.business_key_count,
            "success_count": new_dataset.success_count,
            "incomplete_count": new_dataset.incomplete_count,
            "date_range": [
                new_dataset.date_range[0].isoformat(),
                new_dataset.date_range[1].isoformat(),
            ],
            "config": current.manifest["config"],
            "origin_counts": dict(
                sorted(Counter(record.origin for record in new_dataset.records).items())
            ),
            "missing_reason_counts": missing_counts,
            "manual_cnf_exists": current.manual_cnf_exists,
            "manual_cnf_record_count": manual_count,
            "manual_cnf_sha256": manual_sha,
            "source_counts": source_counts,
            "price_type_counts": price_type_counts,
            "output_files": {
                name: identity.as_dict() for name, identity in identities.items()
            },
            "quality_report_filename": QUALITY_FILENAME,
        }
        write_json_exclusive(building / MANIFEST_FILENAME, manifest)
        promotion = promote_prepared_runtime_release(
            paths.runtime_root,
            building_dir=building,
            release_id=release_id,
            current=current,
            updated_at=calculated_at,
            index_reason="daily_morning_inputs_append",
            expected_record_count=new_dataset.business_key_count,
        )
        return DailyMaterializationResult(
            status="materialized",
            business_date=business_date,
            release_id=release_id,
            generation=promotion.generation,
            previous_release_id=promotion.previous_release_id,
            index_sha256=promotion.index_sha256,
            appended_business_key_count=48,
            success_count_delta=(
                new_dataset.success_count - loaded.dataset.success_count
            ),
            incomplete_count_delta=(
                new_dataset.incomplete_count - loaded.dataset.incomplete_count
            ),
            external_input_candidate_id=external.candidate_id,
            dce_candidate_id=dce.candidate_id,
            message="daily Release materialized",
            total_seconds=perf_counter() - started,
        )
    except Exception:
        if building.exists():
            shutil.rmtree(building)
        raise
    finally:
        if lock.is_locked:
            lock.release()


def _waiting(
    status: str,
    business_date: date,
    release_id: str,
    message: str,
    started: float,
    *,
    external_candidate_id: str | None = None,
) -> DailyMaterializationResult:
    return DailyMaterializationResult(
        status=status,
        business_date=business_date,
        release_id=release_id,
        generation=0,
        previous_release_id=None,
        index_sha256="",
        appended_business_key_count=0,
        success_count_delta=0,
        incomplete_count_delta=0,
        external_input_candidate_id=external_candidate_id,
        dce_candidate_id=None,
        message=message,
        total_seconds=perf_counter() - started,
    )


def _is_missing_index(exc: Exception) -> bool:
    return "Index does not exist" in str(exc)


def _sorted_rows(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda row: (
            row["business_date"],
            row["commodity"],
            row["origin"],
            row["shipment_year"],
            row["shipment_month"],
        ),
    )


def _assert_old_rows_preserved(
    originals: tuple[list[dict], list[dict], list[dict]],
    keys: list[dict],
    snapshots: list[dict],
    results: list[dict],
) -> None:
    for old, combined in zip(originals, (keys, snapshots, results), strict=True):
        old_by_key = {_row_key(row): row for row in old}
        combined_by_key = {_row_key(row): row for row in combined}
        if any(combined_by_key.get(key) != row for key, row in old_by_key.items()):
            raise DailyMaterializationError("an existing runtime row changed")


def _row_key(row: dict) -> tuple:
    return (
        row["business_date"],
        row["commodity"],
        row["origin"],
        row["shipment_year"],
        row["shipment_month"],
    )


def _source_counts(records) -> dict[str, int]:
    values = []
    for record in records:
        values.extend(
            source
            for source in (
                record.cnf_source,
                record.cbot_source,
                record.fx_source,
                record.soymeal_source,
                record.soyoil_source,
            )
            if source is not None
        )
    return dict(sorted(Counter(values).items()))


def _price_type_counts(records) -> dict[str, int]:
    values = []
    for record in records:
        values.extend(
            value
            for value in (
                record.soymeal_price_type,
                record.soyoil_price_type,
            )
            if value is not None
        )
    return dict(sorted(Counter(values).items()))
