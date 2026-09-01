"""Transactional bootstrap and manual-CNF promotion for runtime Releases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
from math import isfinite
from numbers import Real
from pathlib import Path
import shutil
from time import perf_counter
from typing import Iterable
import uuid

from filelock import FileLock, Timeout
import pyarrow.parquet as pq

from agri_research_agent.import_profit.cnf_repricing import (
    CnfRepricingResult,
    reprice_query_record_with_manual_cnf,
)
from agri_research_agent.import_profit.cnf_store import (
    ALLOWED_SOURCE,
    CNF_SCHEMA,
    CnfQuoteUpdate,
    business_key_tuple,
    load_cnf_store,
    upsert_cnf_quotes,
)
from agri_research_agent.import_profit.config import (
    SoybeanImportProfitConfig,
)
from agri_research_agent.import_profit.models import BusinessKey
from agri_research_agent.import_profit.parameter_snapshot import (
    ParameterSnapshotError,
    build_parameter_snapshot,
    read_parameter_provenance,
)
from agri_research_agent.import_profit.mapping_snapshot import (
    MappingSnapshotError,
    build_mapping_snapshot,
    read_mapping_provenance,
)
from agri_research_agent.import_profit.override_snapshot import (
    ContractOverrideSnapshotError,
    build_contract_override_snapshot,
    read_contract_override_provenance,
)
from agri_research_agent.import_profit.query import (
    HISTORICAL_BUSINESS_KEY_SCHEMA,
    CanonicalKey,
    load_soybean_query_dataset,
)
from agri_research_agent.import_profit.result_store import (
    RESULT_SCHEMA,
    SNAPSHOT_SCHEMA,
    with_legacy_contract_identity,
)
from agri_research_agent.import_profit.runtime_store import (
    BUSINESS_KEYS_FILENAME,
    INDEX_FILENAME,
    MANIFEST_FILENAME,
    MANUAL_CNF_FILENAME,
    QUALITY_FILENAME,
    RELEASES_DIRNAME,
    RESULTS_FILENAME,
    RUNTIME_CONTRACT_VERSION,
    RUNTIME_SCHEMA_VERSION,
    SNAPSHOTS_FILENAME,
    ImportProfitRuntimePaths,
    RuntimeConcurrentUpdateError,
    RuntimeLockedError,
    RuntimePromotionResult,
    RuntimeReleaseIndex,
    RuntimeReleaseValidationError,
    RuntimeWriteError,
    file_sha256,
    json_identity,
    load_runtime_release_dataset,
    parquet_identity,
    parse_utc_text,
    promote_prepared_runtime_release,
    read_json,
    resolve_current_runtime_release,
    utc_text,
    validate_release_id,
    write_json_exclusive,
    write_parquet,
    write_release_index_atomically,
)
from agri_research_agent.shared.runtime_context import (
    MARKER_FILENAME,
    RuntimeAuthorizationError,
    RuntimeContext,
    RuntimeMode,
    assert_runtime_write,
)


HISTORICAL_KEYS_FILENAME = "historical_business_keys.parquet"
HISTORICAL_SNAPSHOTS_FILENAME = (
    "historical_soybean_market_snapshots.parquet"
)
HISTORICAL_RESULTS_FILENAME = (
    "historical_soybean_net_crush_results.parquet"
)


class RuntimePipelineError(RuntimeError):
    status = "validation_failed"


@dataclass(frozen=True, slots=True)
class RuntimeCnfUpdate:
    business_key: BusinessKey
    cnf_cents_per_bushel: float | None
    updated_at: datetime
    batch_id: str
    source: str = ALLOWED_SOURCE

    def __post_init__(self) -> None:
        CnfQuoteUpdate(
            business_key=self.business_key,
            cnf_cents_per_bushel=self.cnf_cents_per_bushel,
            source=self.source,
            updated_at=self.updated_at,
            batch_id=self.batch_id,
        )


@dataclass(frozen=True, slots=True)
class RuntimeBootstrapResult:
    status: str
    promotion: RuntimePromotionResult
    record_count: int
    success_count: int
    incomplete_count: int
    initialization_seconds: float


@dataclass(frozen=True, slots=True)
class RuntimeCnfUpdateResult:
    status: str
    release_id: str
    generation: int
    previous_release_id: str | None
    index_sha256: str
    manual_cnf_sha256: str | None
    requested_count: int
    changed_count: int
    unchanged_count: int
    inserted_manual_cnf_count: int
    updated_manual_cnf_count: int
    cleared_to_null_count: int
    updated_keys: tuple[CanonicalKey, ...]
    timings: tuple[tuple[str, float], ...]


def bootstrap_import_profit_runtime(
    historical_candidate_dir: str | Path,
    runtime_root: str | Path,
    *,
    config: SoybeanImportProfitConfig,
    config_path: str | Path,
    release_id: str,
    generated_at: datetime,
) -> RuntimeBootstrapResult:
    """Create generation 1 from a verified historical candidate."""

    started = perf_counter()
    release_id = validate_release_id(release_id)
    generated_at_text = utc_text(generated_at)
    root = Path(runtime_root)
    _reject_repository_path(root)
    existed = root.exists()
    candidate = Path(historical_candidate_dir)
    _validate_bootstrap_runtime_root(root, candidate)
    candidate_files = _validate_historical_candidate(candidate)
    parameter_provenance = build_parameter_snapshot(config)
    assert parameter_provenance.parameter_hash is not None
    mapping_provenance = build_mapping_snapshot(config)
    assert mapping_provenance.mapping_hash is not None
    override_provenance = build_contract_override_snapshot(config)
    assert override_provenance.contract_override_hash is not None
    candidate_manifest = read_json(
        candidate / MANIFEST_FILENAME, "historical Manifest"
    )
    candidate_provenance = read_parameter_provenance(candidate_manifest)
    candidate_mapping = read_mapping_provenance(candidate_manifest)
    candidate_override = read_contract_override_provenance(candidate_manifest)
    if not candidate_provenance.available:
        raise RuntimePipelineError(
            "legacy historical candidate parameter snapshot is unavailable; "
            "rebuild the candidate before bootstrap"
        )
    if candidate_provenance.parameter_hash != parameter_provenance.parameter_hash:
        raise RuntimePipelineError(
            "historical candidate parameter identity does not match config"
        )
    if not candidate_mapping.available:
        raise RuntimePipelineError(
            "legacy historical candidate mapping snapshot is unavailable; "
            "rebuild the candidate before bootstrap"
        )
    if candidate_mapping.mapping_hash != mapping_provenance.mapping_hash:
        raise RuntimePipelineError(
            "historical candidate mapping identity does not match config"
        )
    if not candidate_override.available:
        raise RuntimePipelineError(
            "legacy historical candidate contract override snapshot is "
            "unavailable; rebuild the candidate before bootstrap"
        )
    if (
        candidate_override.contract_override_hash
        != override_provenance.contract_override_hash
    ):
        raise RuntimePipelineError(
            "historical candidate contract override identity does not match config"
        )
    dataset = load_soybean_query_dataset(
        candidate / HISTORICAL_KEYS_FILENAME,
        candidate / HISTORICAL_SNAPSHOTS_FILENAME,
        candidate / HISTORICAL_RESULTS_FILENAME,
    )
    if {
        record.parameter_hash for record in dataset.records
    } != {candidate_provenance.parameter_hash}:
        raise RuntimePipelineError(
            "historical candidate rows do not match parameter identity"
        )
    if {record.mapping_hash for record in dataset.records} != {
        candidate_mapping.mapping_hash
    }:
        raise RuntimePipelineError(
            "historical candidate rows do not match mapping identity"
        )
    if {record.contract_override_hash for record in dataset.records} != {
        candidate_override.contract_override_hash
    }:
        raise RuntimePipelineError(
            "historical candidate rows do not match contract override identity"
        )
    paths = ImportProfitRuntimePaths(root)
    building: Path | None = None
    final: Path | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        paths.releases_dir.mkdir()
        building = paths.releases_dir / f".building-{uuid.uuid4().hex}"
        building.mkdir()
        shutil.copy2(
            candidate / HISTORICAL_KEYS_FILENAME,
            building / BUSINESS_KEYS_FILENAME,
        )
        snapshot_rows = [
            with_legacy_contract_identity(row)
            for row in pq.read_table(
                candidate / HISTORICAL_SNAPSHOTS_FILENAME
            ).to_pylist()
        ]
        result_rows = pq.read_table(
            candidate / HISTORICAL_RESULTS_FILENAME
        ).to_pylist()
        for row in snapshot_rows:
            row["parameter_hash"] = parameter_provenance.parameter_hash
            row["mapping_hash"] = mapping_provenance.mapping_hash
            row["contract_override_hash"] = (
                override_provenance.contract_override_hash
            )
        for row in result_rows:
            row["parameter_hash"] = parameter_provenance.parameter_hash
            row["mapping_hash"] = mapping_provenance.mapping_hash
            row["contract_override_hash"] = (
                override_provenance.contract_override_hash
            )
        write_parquet(
            building / SNAPSHOTS_FILENAME,
            SNAPSHOT_SCHEMA,
            snapshot_rows,
        )
        write_parquet(
            building / RESULTS_FILENAME,
            RESULT_SCHEMA,
            result_rows,
        )
        identities = {
            BUSINESS_KEYS_FILENAME: parquet_identity(
                building / BUSINESS_KEYS_FILENAME,
                BUSINESS_KEYS_FILENAME,
                HISTORICAL_BUSINESS_KEY_SCHEMA,
            ),
            SNAPSHOTS_FILENAME: parquet_identity(
                building / SNAPSHOTS_FILENAME,
                SNAPSHOTS_FILENAME,
                SNAPSHOT_SCHEMA,
            ),
            RESULTS_FILENAME: parquet_identity(
                building / RESULTS_FILENAME,
                RESULTS_FILENAME,
                RESULT_SCHEMA,
            ),
        }
        quality = _bootstrap_quality_payload(
            release_id,
            dataset,
            candidate_files,
            parameter_hash=parameter_provenance.parameter_hash,
            mapping_hash=mapping_provenance.mapping_hash,
            contract_override_hash=(
                override_provenance.contract_override_hash
            ),
        )
        write_json_exclusive(building / QUALITY_FILENAME, quality)
        identities[QUALITY_FILENAME] = json_identity(
            building / QUALITY_FILENAME, QUALITY_FILENAME
        )
        calculated_at = candidate_manifest.get(
            "calculated_at", generated_at_text
        )
        manifest = _bootstrap_manifest(
            release_id=release_id,
            generated_at=generated_at_text,
            calculated_at=calculated_at,
            config_path=Path(config_path),
            candidate_dir=candidate,
            candidate_files=candidate_files,
            dataset=dataset,
            identities=identities,
            parameter_snapshot=parameter_provenance.snapshot,
            parameter_hash=parameter_provenance.parameter_hash,
            mapping_snapshot=mapping_provenance.snapshot,
            mapping_hash=mapping_provenance.mapping_hash,
            contract_override_snapshot=override_provenance.snapshot,
            contract_override_hash=(
                override_provenance.contract_override_hash
            ),
        )
        write_json_exclusive(building / MANIFEST_FILENAME, manifest)
        manifest_sha = file_sha256(building / MANIFEST_FILENAME)
        _validate_materialized_release(
            building, dataset.business_key_count
        )
        final = paths.release_dir(release_id)
        if final.exists():
            raise RuntimePipelineError("release_id already exists")
        building.replace(final)
        building = None
        index = RuntimeReleaseIndex(
            schema_version=RUNTIME_SCHEMA_VERSION,
            generation=1,
            current_release_id=release_id,
            previous_release_id=None,
            updated_at=generated_at,
            index_reason="bootstrap_history",
            current_manifest_sha256=manifest_sha,
        )
        index_sha = write_release_index_atomically(
            root, index, expected_previous_sha256=None
        )
        resolved = resolve_current_runtime_release(root)
        if resolved.identity.index_sha256 != index_sha:
            raise RuntimeWriteError(
                "bootstrap Current identity verification failed"
            )
        return RuntimeBootstrapResult(
            status="success",
            promotion=RuntimePromotionResult(
                release_id=release_id,
                generation=1,
                previous_release_id=None,
                index_sha256=index_sha,
                manifest_sha256=manifest_sha,
                manual_cnf_sha256=None,
            ),
            record_count=dataset.business_key_count,
            success_count=dataset.success_count,
            incomplete_count=dataset.incomplete_count,
            initialization_seconds=perf_counter() - started,
        )
    except Exception:
        if building is not None and building.exists():
            shutil.rmtree(building)
        if final is not None and final.exists():
            shutil.rmtree(final)
        index_path = root / INDEX_FILENAME
        if index_path.exists():
            index_path.unlink()
        releases = root / RELEASES_DIRNAME
        if releases.exists() and not any(releases.iterdir()):
            releases.rmdir()
        if not existed and root.exists() and not any(root.iterdir()):
            root.rmdir()
        raise


def update_runtime_cnf_quotes(
    runtime_root: str | Path,
    updates: Iterable[RuntimeCnfUpdate],
    *,
    config: SoybeanImportProfitConfig,
    config_path: str | Path,
    expected_release_id: str,
    expected_index_sha256: str,
    expected_manual_cnf_sha256: str | None,
    calculated_at: datetime,
    batch_id: str,
    release_id: str,
    lock_timeout_seconds: float = 10.0,
) -> RuntimeCnfUpdateResult:
    """Atomically publish one multi-key manual-CNF transaction."""

    overall_started = perf_counter()
    calculated_at = parse_utc_text(
        utc_text(calculated_at), "calculated_at"
    )
    release_id = validate_release_id(release_id)
    prepared = tuple(updates)
    _validate_runtime_updates(prepared, config, batch_id)
    lock_timeout_seconds = _validate_lock_timeout(lock_timeout_seconds)
    paths = ImportProfitRuntimePaths(Path(runtime_root))
    lock = FileLock(str(paths.runtime_lock_path))
    try:
        lock.acquire(timeout=lock_timeout_seconds)
    except Timeout as exc:
        raise RuntimeLockedError(
            "runtime lock acquisition timed out"
        ) from exc
    try:
        loaded = load_runtime_release_dataset(
            paths.runtime_root,
            expected_release_id=expected_release_id,
            expected_index_sha256=expected_index_sha256,
        )
        current = loaded.resolved
        config_provenance = build_parameter_snapshot(config)
        config_mapping = build_mapping_snapshot(config)
        config_override = build_contract_override_snapshot(config)
        if not current.parameter_provenance.available:
            raise RuntimePipelineError(
                "legacy Release parameter snapshot is unavailable; CNF update refused"
            )
        if (
            config_provenance.parameter_hash
            != current.parameter_provenance.parameter_hash
        ):
            raise RuntimePipelineError(
                "Current Release parameters do not match the supplied config"
            )
        if not current.mapping_provenance.available:
            raise RuntimePipelineError(
                "legacy Release mapping snapshot is unavailable; CNF update refused"
            )
        if config_mapping.mapping_hash != current.mapping_provenance.mapping_hash:
            raise RuntimePipelineError(
                "Current Release mapping does not match the supplied config"
            )
        if not current.contract_override_provenance.available:
            raise RuntimePipelineError(
                "legacy Release contract override snapshot is unavailable; "
                "CNF update refused"
            )
        if (
            config_override.contract_override_hash
            != current.contract_override_provenance.contract_override_hash
        ):
            raise RuntimePipelineError(
                "Current Release contract override does not match the supplied config"
            )
        if (
            current.identity.manual_cnf_sha256
            != expected_manual_cnf_sha256
        ):
            raise RuntimeConcurrentUpdateError(
                "expected manual CNF identity is stale"
            )
        if paths.release_dir(release_id).exists():
            raise RuntimePipelineError("release_id already exists")
        current_manual = load_cnf_store(
            current.manual_cnf_path,
            allowed_origins=config.origin_codes,
        )
        current_manual_by_key = {
            record.key: record for record in current_manual.records
        }
        missing = [
            update.business_key
            for update in prepared
            if loaded.dataset.get(
                business_date=update.business_key.business_date,
                origin=update.business_key.origin,
                shipment_year=update.business_key.shipment_year,
                shipment_month=update.business_key.shipment_month,
            )
            is None
        ]
        if missing:
            raise RuntimePipelineError(
                "all manual CNF keys must exist in Current Release"
            )
        actual = tuple(
            update
            for update in prepared
            if not _manual_value_is_unchanged(
                current_manual_by_key.get(
                    business_key_tuple(update.business_key)
                ),
                update,
            )
        )
        if not actual:
            return RuntimeCnfUpdateResult(
                status="no_change",
                release_id=current.release_id,
                generation=current.generation,
                previous_release_id=current.previous_release_id,
                index_sha256=current.identity.index_sha256,
                manual_cnf_sha256=(
                    current.identity.manual_cnf_sha256
                ),
                requested_count=len(prepared),
                changed_count=0,
                unchanged_count=len(prepared),
                inserted_manual_cnf_count=0,
                updated_manual_cnf_count=0,
                cleared_to_null_count=0,
                updated_keys=(),
                timings=(("total_seconds", perf_counter() - overall_started),),
            )
        return _publish_cnf_update(
            paths=paths,
            loaded=loaded,
            prepared=prepared,
            actual=actual,
            config=config,
            config_path=Path(config_path),
            calculated_at=calculated_at,
            batch_id=batch_id,
            release_id=release_id,
            overall_started=overall_started,
        )
    finally:
        if lock.is_locked:
            lock.release()


def _publish_cnf_update(
    *,
    paths: ImportProfitRuntimePaths,
    loaded,
    prepared: tuple[RuntimeCnfUpdate, ...],
    actual: tuple[RuntimeCnfUpdate, ...],
    config: SoybeanImportProfitConfig,
    config_path: Path,
    calculated_at: datetime,
    batch_id: str,
    release_id: str,
    overall_started: float,
) -> RuntimeCnfUpdateResult:
    current = loaded.resolved
    building = paths.releases_dir / f".building-{uuid.uuid4().hex}"
    final = paths.release_dir(release_id)
    renamed = False
    promoted = False
    timings: dict[str, float] = {}
    try:
        building.mkdir()
        for filename in (
            BUSINESS_KEYS_FILENAME,
            SNAPSHOTS_FILENAME,
            RESULTS_FILENAME,
        ):
            shutil.copy2(current.release_dir / filename, building / filename)
        if current.manual_cnf_exists:
            shutil.copy2(
                current.manual_cnf_path, building / MANUAL_CNF_FILENAME
            )

        upsert_started = perf_counter()
        cnf_result = upsert_cnf_quotes(
            building / MANUAL_CNF_FILENAME,
            (
                CnfQuoteUpdate(
                    business_key=update.business_key,
                    cnf_cents_per_bushel=update.cnf_cents_per_bushel,
                    source=update.source,
                    updated_at=update.updated_at,
                    batch_id=update.batch_id,
                )
                for update in prepared
            ),
            allowed_origins=config.origin_codes,
            expected_store_sha256=current.identity.manual_cnf_sha256,
            lock_timeout_seconds=10.0,
            backup_dir=None,
        )
        timings["manual_cnf_upsert_seconds"] = (
            perf_counter() - upsert_started
        )
        cnf_lock = building / f"{MANUAL_CNF_FILENAME}.lock"
        if cnf_lock.exists():
            cnf_lock.unlink()
        actual_keys = set(cnf_result.updated_keys)
        if actual_keys != {
            business_key_tuple(update.business_key) for update in actual
        }:
            raise RuntimePipelineError(
                "manual CNF upsert changed an unexpected key set"
            )

        rewrite_started = perf_counter()
        key_table = pq.read_table(current.business_keys_path)
        snapshot_table = pq.read_table(current.snapshots_path)
        result_table = pq.read_table(current.results_path)
        key_rows = key_table.to_pylist()
        snapshot_rows = [
            with_legacy_contract_identity(row)
            for row in snapshot_table.to_pylist()
        ]
        result_rows = result_table.to_pylist()
        row_positions = {
            _row_key(row): index for index, row in enumerate(key_rows)
        }
        original_key_rows = [dict(row) for row in key_rows]
        original_snapshot_rows = [dict(row) for row in snapshot_rows]
        original_result_rows = [dict(row) for row in result_rows]

        repricing_started = perf_counter()
        repriced: list[CnfRepricingResult] = []
        by_key = {
            business_key_tuple(update.business_key): update
            for update in actual
        }
        for key_tuple in sorted(by_key):
            update = by_key[key_tuple]
            position = row_positions[key_tuple]
            record = loaded.dataset.get(
                business_date=update.business_key.business_date,
                origin=update.business_key.origin,
                shipment_year=update.business_key.shipment_year,
                shipment_month=update.business_key.shipment_month,
            )
            if record is None:
                raise RuntimePipelineError(
                    "target key disappeared during locked transaction"
                )
            outcome = reprice_query_record_with_manual_cnf(
                record,
                current_business_key_row=key_rows[position],
                current_snapshot_row=snapshot_rows[position],
                config=config,
                cnf_cents_per_bushel=update.cnf_cents_per_bushel,
                calculated_at=calculated_at,
            )
            key_rows[position] = dict(outcome.updated_business_key_row)
            snapshot_rows[position] = dict(outcome.updated_snapshot_row)
            result_rows[position] = dict(outcome.updated_result_row)
            repriced.append(outcome)
        timings["repricing_seconds"] = perf_counter() - repricing_started
        _validate_unchanged_rows(
            original_key_rows,
            original_snapshot_rows,
            original_result_rows,
            key_rows,
            snapshot_rows,
            result_rows,
            {row_positions[key] for key in actual_keys},
        )
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
        timings["materialized_rewrite_seconds"] = (
            perf_counter() - rewrite_started
        )
        manual_identity = parquet_identity(
            building / MANUAL_CNF_FILENAME,
            MANUAL_CNF_FILENAME,
            CNF_SCHEMA,
        )
        identities[MANUAL_CNF_FILENAME] = manual_identity
        new_dataset = load_soybean_query_dataset(
            building / BUSINESS_KEYS_FILENAME,
            building / SNAPSHOTS_FILENAME,
            building / RESULTS_FILENAME,
        )
        quality = _update_quality_payload(
            current=current,
            release_id=release_id,
            batch_id=batch_id,
            prepared=prepared,
            repriced=tuple(repriced),
            cnf_result=cnf_result,
            old_count=loaded.dataset.business_key_count,
            new_dataset=new_dataset,
        )
        write_json_exclusive(building / QUALITY_FILENAME, quality)
        identities[QUALITY_FILENAME] = json_identity(
            building / QUALITY_FILENAME, QUALITY_FILENAME
        )
        manifest = _update_manifest(
            current=current,
            release_id=release_id,
            calculated_at=calculated_at,
            batch_id=batch_id,
            config_path=config_path,
            new_dataset=new_dataset,
            cnf_result=cnf_result,
            repriced=tuple(repriced),
            identities=identities,
        )
        write_json_exclusive(building / MANIFEST_FILENAME, manifest)
        validation_started = perf_counter()
        _validate_materialized_release(
            building, loaded.dataset.business_key_count
        )
        timings["release_validation_seconds"] = (
            perf_counter() - validation_started
        )
        index_started = perf_counter()
        promotion = promote_prepared_runtime_release(
            paths.runtime_root,
            building_dir=building,
            release_id=release_id,
            current=current,
            updated_at=calculated_at,
            index_reason="manual_cnf_update",
            expected_record_count=loaded.dataset.business_key_count,
        )
        renamed = True
        promoted = True
        timings["index_promotion_seconds"] = (
            perf_counter() - index_started
        )
        timings["total_seconds"] = perf_counter() - overall_started
        return RuntimeCnfUpdateResult(
            status="success",
            release_id=release_id,
            generation=current.generation + 1,
            previous_release_id=current.release_id,
            index_sha256=promotion.index_sha256,
            manual_cnf_sha256=manual_identity.sha256,
            requested_count=len(prepared),
            changed_count=len(actual),
            unchanged_count=cnf_result.unchanged_count,
            inserted_manual_cnf_count=cnf_result.inserted_count,
            updated_manual_cnf_count=cnf_result.updated_count,
            cleared_to_null_count=cnf_result.cleared_to_null_count,
            updated_keys=tuple(sorted(actual_keys)),
            timings=tuple(timings.items()),
        )
    except Exception:
        if building.exists():
            shutil.rmtree(building)
        if renamed and not promoted and final.exists():
            shutil.rmtree(final)
        raise


def _validate_historical_candidate(
    candidate: Path,
) -> dict[str, dict[str, object]]:
    if not candidate.is_dir():
        raise RuntimePipelineError(
            "historical candidate directory does not exist"
        )
    manifest_path = candidate / MANIFEST_FILENAME
    quality_path = candidate / QUALITY_FILENAME
    if not manifest_path.is_file() or not quality_path.is_file():
        raise RuntimePipelineError(
            "historical candidate Manifest or quality report is missing"
        )
    manifest = read_json(manifest_path, "historical Manifest")
    outputs = manifest.get("output_files")
    if not isinstance(outputs, dict):
        raise RuntimePipelineError(
            "historical Manifest output_files is invalid"
        )
    required = (
        HISTORICAL_KEYS_FILENAME,
        HISTORICAL_SNAPSHOTS_FILENAME,
        HISTORICAL_RESULTS_FILENAME,
        QUALITY_FILENAME,
    )
    identities: dict[str, dict[str, object]] = {}
    for filename in required:
        payload = outputs.get(filename)
        path = candidate / filename
        if (
            not isinstance(payload, dict)
            or not path.is_file()
            or payload.get("filename") != filename
            or payload.get("size_bytes") != path.stat().st_size
            or payload.get("sha256") != file_sha256(path)
        ):
            raise RuntimePipelineError(
                f"historical candidate identity mismatch: {filename}"
            )
        identities[filename] = payload
    identities[MANIFEST_FILENAME] = {
        "filename": MANIFEST_FILENAME,
        "size_bytes": manifest_path.stat().st_size,
        "sha256": file_sha256(manifest_path),
    }
    return identities


def _validate_bootstrap_runtime_root(root: Path, candidate: Path) -> None:
    if not root.exists():
        return
    if not root.is_dir():
        raise RuntimePipelineError(
            "runtime_root must not exist or must be an empty directory"
        )
    entries = {path.name for path in root.iterdir()}
    if not entries:
        return
    marker_path = root / MARKER_FILENAME
    if not marker_path.is_file():
        raise RuntimePipelineError(
            "runtime_root must not exist or must be an empty directory"
        )
    unexpected = entries - {MARKER_FILENAME, "candidate", "tmp"}
    if unexpected:
        raise RuntimePipelineError(
            "marked isolated runtime_root contains unexpected entries"
        )
    try:
        context = RuntimeContext(
            mode=RuntimeMode.ISOLATED_DEV,
            module_id="soybean-import-crush",
            runtime_root=root,
            candidate_root=candidate,
        )
        for target in (
            root / RELEASES_DIRNAME,
            root / INDEX_FILENAME,
            root / ".runtime.lock",
        ):
            assert_runtime_write(context, target)
    except RuntimeAuthorizationError as exc:
        raise RuntimePipelineError(
            "marked runtime_root is not an authorized soybean isolated-dev runtime"
        ) from exc


def _bootstrap_manifest(
    *,
    release_id: str,
    generated_at: str,
    calculated_at: object,
    config_path: Path,
    candidate_dir: Path,
    candidate_files: dict[str, dict[str, object]],
    dataset,
    identities,
    parameter_snapshot,
    parameter_hash: str,
    mapping_snapshot,
    mapping_hash: str,
    contract_override_snapshot,
    contract_override_hash: str,
) -> dict[str, object]:
    return {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "release_id": release_id,
        "parent_release_id": None,
        "release_reason": "bootstrap_history",
        "generation": 1,
        "created_at": generated_at,
        "calculated_at": str(calculated_at),
        "config": _safe_file_payload(config_path),
        "parameter_snapshot": parameter_snapshot,
        "parameter_hash": parameter_hash,
        "mapping_snapshot": mapping_snapshot,
        "mapping_hash": mapping_hash,
        "contract_override_snapshot": contract_override_snapshot,
        "contract_override_hash": contract_override_hash,
        "source_candidate": {
            "directory_name": candidate_dir.name,
            "manifest": candidate_files[MANIFEST_FILENAME],
        },
        "record_count": dataset.business_key_count,
        "success_count": dataset.success_count,
        "incomplete_count": dataset.incomplete_count,
        "date_range": [
            dataset.date_range[0].isoformat(),
            dataset.date_range[1].isoformat(),
        ],
        "origin_counts": _origin_counts(dataset.records),
        "missing_reason_counts": _missing_reason_counts(dataset.records),
        "manual_cnf_exists": False,
        "manual_cnf_record_count": 0,
        "manual_cnf_sha256": None,
        "output_files": {
            name: identity.as_dict()
            for name, identity in identities.items()
        },
        "quality_report_filename": QUALITY_FILENAME,
    }


def _update_manifest(
    *,
    current,
    release_id: str,
    calculated_at: datetime,
    batch_id: str,
    config_path: Path,
    new_dataset,
    cnf_result,
    repriced: tuple[CnfRepricingResult, ...],
    identities,
) -> dict[str, object]:
    payload = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "release_id": release_id,
        "parent_release_id": current.release_id,
        "release_reason": "manual_cnf_update",
        "generation": current.generation + 1,
        "created_at": utc_text(calculated_at),
        "calculated_at": utc_text(calculated_at),
        "batch_id": batch_id,
        "updated_keys": [
            _key_payload(item.business_key)
            for item in sorted(
                repriced,
                key=lambda item: business_key_tuple(item.business_key),
            )
        ],
        "inserted_manual_cnf_count": cnf_result.inserted_count,
        "updated_manual_cnf_count": cnf_result.updated_count,
        "cleared_to_null_count": cnf_result.cleared_to_null_count,
        "unchanged_count": cnf_result.unchanged_count,
        "record_count": new_dataset.business_key_count,
        "success_count": new_dataset.success_count,
        "incomplete_count": new_dataset.incomplete_count,
        "date_range": [
            new_dataset.date_range[0].isoformat(),
            new_dataset.date_range[1].isoformat(),
        ],
        "origin_counts": _origin_counts(new_dataset.records),
        "missing_reason_counts": _missing_reason_counts(
            new_dataset.records
        ),
        "previous_manual_cnf_sha256": (
            current.identity.manual_cnf_sha256
        ),
        "new_manual_cnf_sha256": cnf_result.new_sha256,
        "previous_release_manifest_sha256": (
            current.identity.manifest_sha256
        ),
        "manual_cnf_exists": True,
        "manual_cnf_record_count": cnf_result.new_record_count,
        "manual_cnf_sha256": cnf_result.new_sha256,
        "config": _safe_file_payload(config_path),
        "parameter_snapshot": current.parameter_provenance.snapshot,
        "parameter_hash": current.parameter_provenance.parameter_hash,
        "mapping_snapshot": current.mapping_provenance.snapshot,
        "mapping_hash": current.mapping_provenance.mapping_hash,
        "contract_override_snapshot": (
            current.contract_override_provenance.snapshot
        ),
        "contract_override_hash": (
            current.contract_override_provenance.contract_override_hash
        ),
        "output_files": {
            name: identity.as_dict()
            for name, identity in identities.items()
        },
        "quality_report_filename": QUALITY_FILENAME,
    }
    if "night_session_close_start_date" in current.manifest:
        payload["night_session_close_start_date"] = current.manifest[
            "night_session_close_start_date"
        ]
    return payload


def _bootstrap_quality_payload(
    release_id: str,
    dataset,
    candidate_files,
    *,
    parameter_hash: str,
    mapping_hash: str,
    contract_override_hash: str,
) -> dict[str, object]:
    return {
        "transaction": "bootstrap_history",
        "release_id": release_id,
        "record_count_consistent": True,
        "key_sets_consistent": True,
        "parameter_snapshot_present": True,
        "parameter_hash_valid": True,
        "result_parameter_identity_consistent": all(
            record.parameter_hash == parameter_hash
            for record in dataset.records
            if record.parameter_hash is not None
        ),
        "mapping_snapshot_present": True,
        "mapping_hash_valid": True,
        "result_mapping_identity_consistent": all(
            record.mapping_hash == mapping_hash
            for record in dataset.records
        ),
        "contract_override_snapshot_present": True,
        "contract_override_hash_valid": True,
        "result_contract_override_identity_consistent": all(
            record.contract_override_hash == contract_override_hash
            for record in dataset.records
        ),
        "source_candidate_files": sorted(candidate_files),
        "fatal": [],
        "warning": (
            []
            if dataset.incomplete_count == 0
            else ["historical_records_incomplete"]
        ),
        "final_status": (
            "passed"
            if dataset.incomplete_count == 0
            else "passed_with_incomplete"
        ),
    }


def _update_quality_payload(
    *,
    current,
    release_id,
    batch_id,
    prepared,
    repriced,
    cnf_result,
    old_count,
    new_dataset,
) -> dict[str, object]:
    samples = [
        {
            "business_key": _key_payload(item.business_key),
            "previous_cnf": item.previous_cnf,
            "new_cnf": item.new_cnf,
            "previous_status": item.previous_status,
            "new_status": item.new_status,
            "previous_missing_reasons": list(
                item.previous_missing_reasons
            ),
            "new_missing_reasons": list(item.new_missing_reasons),
            "calculation_formed": item.new_status == "success",
        }
        for item in repriced[:20]
    ]
    return {
        "transaction": "manual_cnf_update",
        "batch_id": batch_id,
        "previous_release_id": current.release_id,
        "new_release_id": release_id,
        "requested_update_count": len(prepared),
        "actual_change_count": len(repriced),
        "no_change_count": cnf_result.unchanged_count,
        "updated_keys": [
            _key_payload(item.business_key) for item in repriced
        ],
        "key_samples": samples,
        "record_count_consistent": (
            old_count == new_dataset.business_key_count
        ),
        "key_sets_consistent": True,
        "unchanged_record_check": "full_row_equality",
        "manifest_and_output_identity_check": "passed",
        "parameter_snapshot_present": True,
        "parameter_hash_valid": True,
        "result_parameter_identity_consistent": True,
        "mapping_snapshot_present": True,
        "mapping_hash_valid": True,
        "result_mapping_identity_consistent": True,
        "contract_override_snapshot_present": True,
        "contract_override_hash_valid": True,
        "result_contract_override_identity_consistent": True,
        "fatal": [],
        "warning": (
            []
            if new_dataset.incomplete_count == 0
            else ["runtime_records_incomplete"]
        ),
        "final_status": (
            "passed"
            if new_dataset.incomplete_count == 0
            else "passed_with_incomplete"
        ),
    }


def _validate_materialized_release(
    release_dir: Path, expected_count: int
) -> None:
    dataset = load_soybean_query_dataset(
        release_dir / BUSINESS_KEYS_FILENAME,
        release_dir / SNAPSHOTS_FILENAME,
        release_dir / RESULTS_FILENAME,
    )
    if dataset.business_key_count != expected_count:
        raise RuntimeWriteError("materialized Release row count mismatch")
    read_json(release_dir / QUALITY_FILENAME, "quality report")
    manifest = read_json(
        release_dir / MANIFEST_FILENAME, "Release Manifest"
    )
    try:
        provenance = read_parameter_provenance(manifest)
    except ParameterSnapshotError as exc:
        raise RuntimeWriteError(
            "materialized Release parameter provenance is invalid"
        ) from exc
    try:
        mapping_provenance = read_mapping_provenance(manifest)
    except MappingSnapshotError as exc:
        raise RuntimeWriteError(
            "materialized Release mapping provenance is invalid"
        ) from exc
    try:
        override_provenance = read_contract_override_provenance(manifest)
    except ContractOverrideSnapshotError as exc:
        raise RuntimeWriteError(
            "materialized Release contract override provenance is invalid"
        ) from exc
    if not provenance.available or {
        record.parameter_hash for record in dataset.records
    } != {provenance.parameter_hash}:
        raise RuntimeWriteError(
            "materialized Release parameter identities are inconsistent"
        )
    if not mapping_provenance.available or {
        record.mapping_hash for record in dataset.records
    } != {mapping_provenance.mapping_hash}:
        raise RuntimeWriteError(
            "materialized Release mapping identities are inconsistent"
        )
    if not override_provenance.available or {
        record.contract_override_hash for record in dataset.records
    } != {override_provenance.contract_override_hash}:
        raise RuntimeWriteError(
            "materialized Release contract override identities are inconsistent"
        )


def _validate_runtime_updates(
    updates: tuple[RuntimeCnfUpdate, ...],
    config: SoybeanImportProfitConfig,
    batch_id: str,
) -> None:
    if not updates:
        raise RuntimePipelineError("updates must not be empty")
    keys = []
    for update in updates:
        if not isinstance(update, RuntimeCnfUpdate):
            raise RuntimePipelineError(
                "updates must contain RuntimeCnfUpdate objects"
            )
        if update.batch_id != batch_id:
            raise RuntimePipelineError(
                "all updates must match the transaction batch_id"
            )
        BusinessKey(
            update.business_key.business_date,
            update.business_key.commodity,
            update.business_key.origin,
            update.business_key.shipment_year,
            update.business_key.shipment_month,
            config.origin_codes,
            config.commodity,
            update.business_key.shipment_period,
        )
        keys.append(business_key_tuple(update.business_key))
    if len(keys) != len(set(keys)):
        raise RuntimePipelineError(
            "update batch contains duplicate business keys"
        )


def _validate_lock_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not isfinite(float(value))
        or not 0.0 <= float(value) <= 300.0
    ):
        raise RuntimePipelineError(
            "lock_timeout_seconds must be finite and between 0 and 300"
        )
    return float(value)


def _manual_value_is_unchanged(record, update: RuntimeCnfUpdate) -> bool:
    return (
        record is not None
        and record.cnf_cents_per_bushel == update.cnf_cents_per_bushel
        and record.source == ALLOWED_SOURCE
    )


def _validate_unchanged_rows(
    old_keys,
    old_snapshots,
    old_results,
    new_keys,
    new_snapshots,
    new_results,
    changed_positions: set[int],
) -> None:
    if not (
        len(old_keys)
        == len(old_snapshots)
        == len(old_results)
        == len(new_keys)
        == len(new_snapshots)
        == len(new_results)
    ):
        raise RuntimeWriteError("materialized row counts diverged")
    for index in range(len(old_keys)):
        if index in changed_positions:
            continue
        if (
            old_keys[index] != new_keys[index]
            or old_snapshots[index] != new_snapshots[index]
            or old_results[index] != new_results[index]
        ):
            raise RuntimeWriteError(
                "an unrequested business row changed"
            )


def _row_key(row: dict[str, object]) -> CanonicalKey:
    return (
        row["business_date"],
        row["commodity"],
        row["origin"],
        row["shipment_year"],
        row["shipment_month"],
    )


def _origin_counts(records) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.origin] = counts.get(record.origin, 0) + 1
    return dict(sorted(counts.items()))


def _missing_reason_counts(records) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        for reason in record.missing_reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _safe_file_payload(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise RuntimePipelineError("configuration file does not exist")
    return {
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": file_sha256(path),
    }


def _key_payload(key: BusinessKey) -> dict[str, object]:
    return {
        "business_date": key.business_date.isoformat(),
        "commodity": key.commodity,
        "origin": key.origin,
        "shipment_year": key.shipment_year,
        "shipment_month": key.shipment_month,
        "shipment_period": key.shipment_period,
    }


def _reject_repository_path(path: Path) -> None:
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            raise RuntimePipelineError(
                "runtime_root must be outside the Git repository"
            )
