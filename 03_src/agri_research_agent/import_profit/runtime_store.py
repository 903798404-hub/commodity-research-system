"""Immutable Release storage and strict current-Release resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Mapping
import uuid

import pyarrow as pa
import pyarrow.parquet as pq

from .cnf_store import CNF_SCHEMA, load_cnf_store
from .query import (
    HISTORICAL_BUSINESS_KEY_SCHEMA,
    SoybeanQueryDataset,
    load_soybean_query_dataset,
)
from .parameter_snapshot import (
    ParameterProvenance,
    ParameterSnapshotError,
    read_parameter_provenance,
)
from .mapping_snapshot import (
    MappingProvenance,
    MappingSnapshotError,
    read_mapping_provenance,
)
from .override_snapshot import (
    ContractOverrideProvenance,
    ContractOverrideSnapshotError,
    read_contract_override_provenance,
)
from .result_store import (
    LEGACY_CONTRACT_IDENTITY_SNAPSHOT_SCHEMA,
    LEGACY_QUOTE_DATE_SNAPSHOT_SCHEMA,
    LEGACY_RESULT_SCHEMA,
    LEGACY_SNAPSHOT_SCHEMA,
    LEGACY_MAPPING_PROVENANCE_RESULT_SCHEMA,
    LEGACY_MAPPING_PROVENANCE_SNAPSHOT_SCHEMA,
    LEGACY_OVERRIDE_PROVENANCE_RESULT_SCHEMA,
    LEGACY_OVERRIDE_PROVENANCE_SNAPSHOT_SCHEMA,
    RESULT_SCHEMA,
    SNAPSHOT_SCHEMA,
)


RUNTIME_CONTRACT_VERSION = "1"
RUNTIME_SCHEMA_VERSION = "1"
INDEX_FILENAME = "release_index.json"
LOCK_FILENAME = ".runtime.lock"
RELEASES_DIRNAME = "releases"
BUSINESS_KEYS_FILENAME = "soybean_business_keys.parquet"
SNAPSHOTS_FILENAME = "soybean_market_snapshots.parquet"
RESULTS_FILENAME = "soybean_net_crush_results.parquet"
MANUAL_CNF_FILENAME = "manual_cnf_quotes.parquet"
MANIFEST_FILENAME = "manifest.json"
QUALITY_FILENAME = "quality_report.json"
SAFE_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
INDEX_FIELDS = (
    "schema_version",
    "generation",
    "current_release_id",
    "previous_release_id",
    "updated_at",
    "index_reason",
    "current_manifest_sha256",
)


class RuntimeStoreError(RuntimeError):
    status = "validation_failed"


class RuntimeReleaseValidationError(RuntimeStoreError):
    pass


class RuntimeConcurrentUpdateError(RuntimeStoreError):
    status = "concurrent_conflict"


class RuntimeLockedError(RuntimeStoreError):
    status = "locked"


class RuntimeWriteError(RuntimeStoreError):
    status = "write_failed"


@dataclass(frozen=True, slots=True)
class ImportProfitRuntimePaths:
    runtime_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "runtime_root", Path(self.runtime_root))

    @property
    def release_index_path(self) -> Path:
        return self.runtime_root / INDEX_FILENAME

    @property
    def runtime_lock_path(self) -> Path:
        return self.runtime_root / LOCK_FILENAME

    @property
    def releases_dir(self) -> Path:
        return self.runtime_root / RELEASES_DIRNAME

    def release_dir(self, release_id: str) -> Path:
        validate_release_id(release_id)
        return self.releases_dir / release_id


@dataclass(frozen=True, slots=True)
class RuntimeFileIdentity:
    filename: str
    size_bytes: int
    sha256: str
    record_count: int | None
    schema_fingerprint: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "record_count": self.record_count,
            "schema_fingerprint": self.schema_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class RuntimeReleaseIndex:
    schema_version: str
    generation: int
    current_release_id: str
    previous_release_id: str | None
    updated_at: datetime
    index_reason: str
    current_manifest_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != RUNTIME_SCHEMA_VERSION:
            raise RuntimeReleaseValidationError(
                "unsupported Release Index schema version"
            )
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise RuntimeReleaseValidationError(
                "Release Index generation must be positive"
            )
        validate_release_id(self.current_release_id)
        if self.previous_release_id is not None:
            validate_release_id(self.previous_release_id)
            if self.previous_release_id == self.current_release_id:
                raise RuntimeReleaseValidationError(
                    "Current and Previous Releases must differ"
                )
        object.__setattr__(
            self, "updated_at", require_utc_datetime(self.updated_at)
        )
        if not isinstance(self.index_reason, str) or not self.index_reason:
            raise RuntimeReleaseValidationError(
                "Release Index reason must be non-empty"
            )
        _validate_sha(self.current_manifest_sha256, "manifest SHA")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generation": self.generation,
            "current_release_id": self.current_release_id,
            "previous_release_id": self.previous_release_id,
            "updated_at": utc_text(self.updated_at),
            "index_reason": self.index_reason,
            "current_manifest_sha256": self.current_manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class RuntimeReleaseIdentity:
    release_id: str
    generation: int
    index_sha256: str
    manifest_sha256: str
    manual_cnf_sha256: str | None


@dataclass(frozen=True, slots=True)
class ResolvedRuntimeRelease:
    runtime_root_name: str
    release_id: str
    generation: int
    previous_release_id: str | None
    index: RuntimeReleaseIndex
    identity: RuntimeReleaseIdentity
    release_dir: Path
    business_keys_path: Path
    snapshots_path: Path
    results_path: Path
    manual_cnf_path: Path
    manual_cnf_exists: bool
    manifest_path: Path
    quality_report_path: Path
    files: tuple[RuntimeFileIdentity, ...]
    manifest: Mapping[str, object]
    parameter_provenance: ParameterProvenance
    mapping_provenance: MappingProvenance
    contract_override_provenance: ContractOverrideProvenance


@dataclass(frozen=True, slots=True)
class LoadedRuntimeRelease:
    resolved: ResolvedRuntimeRelease
    dataset: SoybeanQueryDataset


@dataclass(frozen=True, slots=True)
class RuntimePromotionResult:
    release_id: str
    generation: int
    previous_release_id: str | None
    index_sha256: str
    manifest_sha256: str
    manual_cnf_sha256: str | None


def resolve_current_runtime_release(
    runtime_root: str | Path,
) -> ResolvedRuntimeRelease:
    """Resolve and fully verify the indexed immutable current Release."""

    paths = ImportProfitRuntimePaths(Path(runtime_root))
    index_path = paths.release_index_path
    if not index_path.is_file():
        raise RuntimeReleaseValidationError(
            "Release Index does not exist"
        )
    index_payload = read_json(index_path, "Release Index")
    if tuple(index_payload) != INDEX_FIELDS:
        raise RuntimeReleaseValidationError(
            "Release Index fields are missing, extra, or reordered"
        )
    index = RuntimeReleaseIndex(
        schema_version=index_payload["schema_version"],
        generation=index_payload["generation"],
        current_release_id=index_payload["current_release_id"],
        previous_release_id=index_payload["previous_release_id"],
        updated_at=parse_utc_text(index_payload["updated_at"], "updated_at"),
        index_reason=index_payload["index_reason"],
        current_manifest_sha256=index_payload[
            "current_manifest_sha256"
        ],
    )
    index_sha = file_sha256(index_path)
    release_dir = paths.release_dir(index.current_release_id)
    if not release_dir.is_dir():
        raise RuntimeReleaseValidationError(
            "Current Release directory does not exist"
        )
    if (
        index.previous_release_id is not None
        and not paths.release_dir(index.previous_release_id).is_dir()
    ):
        raise RuntimeReleaseValidationError(
            "Previous Release directory does not exist"
        )
    manifest_path = release_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise RuntimeReleaseValidationError(
            "Current Release Manifest does not exist"
        )
    manifest_sha = file_sha256(manifest_path)
    if manifest_sha != index.current_manifest_sha256:
        raise RuntimeReleaseValidationError(
            "Current Manifest identity does not match Release Index"
        )
    manifest = read_json(manifest_path, "Release Manifest")
    _validate_manifest_header(manifest, index)
    try:
        parameter_provenance = read_parameter_provenance(manifest)
    except ParameterSnapshotError as exc:
        raise RuntimeReleaseValidationError(
            "Release parameter provenance is invalid"
        ) from exc
    try:
        mapping_provenance = read_mapping_provenance(manifest)
    except MappingSnapshotError as exc:
        raise RuntimeReleaseValidationError(
            "Release mapping provenance is invalid"
        ) from exc
    try:
        contract_override_provenance = read_contract_override_provenance(
            manifest
        )
    except ContractOverrideSnapshotError as exc:
        raise RuntimeReleaseValidationError(
            "Release contract override provenance is invalid"
        ) from exc
    output_payload = manifest.get("output_files")
    if not isinstance(output_payload, dict):
        raise RuntimeReleaseValidationError(
            "Release Manifest output_files must be an object"
        )

    snapshot_schemas: list[pa.Schema] = [SNAPSHOT_SCHEMA]
    result_schemas: list[pa.Schema] = [RESULT_SCHEMA]
    if not contract_override_provenance.available:
        snapshot_schemas.append(LEGACY_OVERRIDE_PROVENANCE_SNAPSHOT_SCHEMA)
        result_schemas.append(LEGACY_OVERRIDE_PROVENANCE_RESULT_SCHEMA)
    if not mapping_provenance.available:
        snapshot_schemas.extend(
            [
                LEGACY_MAPPING_PROVENANCE_SNAPSHOT_SCHEMA,
                LEGACY_QUOTE_DATE_SNAPSHOT_SCHEMA,
                LEGACY_CONTRACT_IDENTITY_SNAPSHOT_SCHEMA,
            ]
        )
        result_schemas.append(LEGACY_MAPPING_PROVENANCE_RESULT_SCHEMA)
    if not parameter_provenance.available:
        snapshot_schemas.append(LEGACY_SNAPSHOT_SCHEMA)
        result_schemas.append(LEGACY_RESULT_SCHEMA)
    required: dict[str, pa.Schema | tuple[pa.Schema, ...]] = {
        BUSINESS_KEYS_FILENAME: HISTORICAL_BUSINESS_KEY_SCHEMA,
        SNAPSHOTS_FILENAME: tuple(snapshot_schemas),
        RESULTS_FILENAME: tuple(result_schemas),
    }
    files: list[RuntimeFileIdentity] = []
    for filename, schema in required.items():
        identity = _manifest_file_identity(
            output_payload, filename, required=True
        )
        _verify_file_identity(release_dir / filename, identity, schema)
        files.append(identity)
    quality_identity = _manifest_file_identity(
        output_payload, QUALITY_FILENAME, required=True
    )
    _verify_file_identity(
        release_dir / QUALITY_FILENAME, quality_identity, None
    )
    files.append(quality_identity)

    manual_exists = manifest.get("manual_cnf_exists")
    manual_sha = manifest.get("manual_cnf_sha256")
    manual_count = manifest.get("manual_cnf_record_count")
    if not isinstance(manual_exists, bool):
        raise RuntimeReleaseValidationError(
            "manual_cnf_exists must be boolean"
        )
    manual_path = release_dir / MANUAL_CNF_FILENAME
    if manual_exists:
        manual_identity = _manifest_file_identity(
            output_payload, MANUAL_CNF_FILENAME, required=True
        )
        _verify_file_identity(manual_path, manual_identity, CNF_SCHEMA)
        if (
            manual_identity.sha256 != manual_sha
            or manual_identity.record_count != manual_count
        ):
            raise RuntimeReleaseValidationError(
                "manual CNF Manifest identity is inconsistent"
            )
        files.append(manual_identity)
    elif (
        manual_path.exists()
        or manual_sha is not None
        or manual_count != 0
        or MANUAL_CNF_FILENAME in output_payload
    ):
        raise RuntimeReleaseValidationError(
            "absent manual CNF semantics are inconsistent"
        )
    if manifest.get("quality_report_filename") != QUALITY_FILENAME:
        raise RuntimeReleaseValidationError(
            "quality report filename is inconsistent"
        )
    quality = read_json(
        release_dir / QUALITY_FILENAME, "quality report"
    )
    if quality.get("final_status") not in {"passed", "passed_with_incomplete"}:
        raise RuntimeReleaseValidationError(
            "quality report final status is invalid"
        )
    return ResolvedRuntimeRelease(
        runtime_root_name=paths.runtime_root.name,
        release_id=index.current_release_id,
        generation=index.generation,
        previous_release_id=index.previous_release_id,
        index=index,
        identity=RuntimeReleaseIdentity(
            release_id=index.current_release_id,
            generation=index.generation,
            index_sha256=index_sha,
            manifest_sha256=manifest_sha,
            manual_cnf_sha256=manual_sha,
        ),
        release_dir=release_dir,
        business_keys_path=release_dir / BUSINESS_KEYS_FILENAME,
        snapshots_path=release_dir / SNAPSHOTS_FILENAME,
        results_path=release_dir / RESULTS_FILENAME,
        manual_cnf_path=manual_path,
        manual_cnf_exists=manual_exists,
        manifest_path=manifest_path,
        quality_report_path=release_dir / QUALITY_FILENAME,
        files=tuple(files),
        manifest=manifest,
        parameter_provenance=parameter_provenance,
        mapping_provenance=mapping_provenance,
        contract_override_provenance=contract_override_provenance,
    )


def load_runtime_release_dataset(
    runtime_root: str | Path,
    *,
    expected_release_id: str | None = None,
    expected_index_sha256: str | None = None,
) -> LoadedRuntimeRelease:
    resolved = resolve_current_runtime_release(runtime_root)
    if (
        expected_release_id is not None
        and expected_release_id != resolved.release_id
    ):
        raise RuntimeConcurrentUpdateError(
            "expected Release is no longer Current"
        )
    if (
        expected_index_sha256 is not None
        and expected_index_sha256 != resolved.identity.index_sha256
    ):
        raise RuntimeConcurrentUpdateError(
            "expected Release Index identity is stale"
        )
    dataset = load_soybean_query_dataset(
        resolved.business_keys_path,
        resolved.snapshots_path,
        resolved.results_path,
    )
    if (
        dataset.business_key_count != resolved.manifest["record_count"]
        or dataset.success_count != resolved.manifest["success_count"]
        or dataset.incomplete_count != resolved.manifest["incomplete_count"]
    ):
        raise RuntimeReleaseValidationError(
            "Release record statistics do not match Manifest"
        )
    row_hashes = {record.parameter_hash for record in dataset.records}
    if resolved.parameter_provenance.available:
        if row_hashes != {resolved.parameter_provenance.parameter_hash}:
            raise RuntimeReleaseValidationError(
                "Result parameter identity does not match Release Manifest"
            )
    elif row_hashes != {None} and row_hashes:
        raise RuntimeReleaseValidationError(
            "legacy Release contains unsealed parameter identities"
        )
    row_mapping_hashes = {record.mapping_hash for record in dataset.records}
    if resolved.mapping_provenance.available:
        if row_mapping_hashes != {resolved.mapping_provenance.mapping_hash}:
            raise RuntimeReleaseValidationError(
                "Result mapping identity does not match Release Manifest"
            )
    elif row_mapping_hashes != {None} and row_mapping_hashes:
        raise RuntimeReleaseValidationError(
            "legacy Release contains unsealed mapping identities"
        )
    row_override_hashes = {
        record.contract_override_hash for record in dataset.records
    }
    if resolved.contract_override_provenance.available:
        if row_override_hashes != {
            resolved.contract_override_provenance.contract_override_hash
        }:
            raise RuntimeReleaseValidationError(
                "Result contract override identity does not match Release Manifest"
            )
    elif row_override_hashes != {None} and row_override_hashes:
        raise RuntimeReleaseValidationError(
            "legacy Release contains unsealed contract override identities"
        )
    manual = load_cnf_store(
        resolved.manual_cnf_path,
        allowed_origins=dataset.origins,
    )
    if (
        manual.store_exists != resolved.manual_cnf_exists
        or manual.store_sha256 != resolved.identity.manual_cnf_sha256
        or manual.record_count
        != resolved.manifest["manual_cnf_record_count"]
    ):
        raise RuntimeReleaseValidationError(
            "manual CNF store does not match resolved identity"
        )
    for quote in manual.records:
        business_key = quote.business_key
        if dataset.get(
            business_date=business_key.business_date,
            origin=business_key.origin,
            shipment_year=business_key.shipment_year,
            shipment_month=business_key.shipment_month,
        ) is None:
            raise RuntimeReleaseValidationError(
                "manual CNF key does not exist in the Release dataset"
            )
    return LoadedRuntimeRelease(resolved, dataset)


def write_release_index_atomically(
    runtime_root: str | Path,
    index: RuntimeReleaseIndex,
    *,
    expected_previous_sha256: str | None,
) -> str:
    """Atomically replace the single Current/Previous index JSON."""

    paths = ImportProfitRuntimePaths(Path(runtime_root))
    paths.runtime_root.mkdir(parents=True, exist_ok=True)
    current_path = paths.release_index_path
    current_sha = (
        file_sha256(current_path) if current_path.is_file() else None
    )
    if current_sha != expected_previous_sha256:
        raise RuntimeConcurrentUpdateError(
            "Release Index changed before atomic promotion"
        )
    token = uuid.uuid4().hex
    temporary = paths.runtime_root / f".{INDEX_FILENAME}.{token}.tmp"
    old_bytes = current_path.read_bytes() if current_path.is_file() else None
    replaced = False
    try:
        write_json_exclusive(temporary, index.as_dict())
        payload = read_json(temporary, "temporary Release Index")
        if payload != index.as_dict() or tuple(payload) != INDEX_FIELDS:
            raise RuntimeWriteError(
                "temporary Release Index round-trip failed"
            )
        os.replace(temporary, current_path)
        replaced = True
        final = read_json(current_path, "Release Index")
        if final != index.as_dict():
            raise RuntimeWriteError(
                "promoted Release Index verification failed"
            )
        return file_sha256(current_path)
    except Exception as exc:
        if temporary.exists():
            temporary.unlink()
        if replaced:
            try:
                if old_bytes is None:
                    current_path.unlink(missing_ok=True)
                else:
                    rollback = (
                        paths.runtime_root
                        / f".{INDEX_FILENAME}.{token}.rollback"
                    )
                    write_bytes_exclusive(rollback, old_bytes)
                    os.replace(rollback, current_path)
            except Exception as rollback_exc:
                raise RuntimeWriteError(
                    "Release Index write and rollback both failed"
                ) from rollback_exc
        if isinstance(exc, RuntimeStoreError):
            raise
        raise RuntimeWriteError("atomic Release Index write failed") from exc


def promote_prepared_runtime_release(
    runtime_root: str | Path,
    *,
    building_dir: str | Path,
    release_id: str,
    current: ResolvedRuntimeRelease,
    updated_at: datetime,
    index_reason: str,
    expected_record_count: int,
) -> RuntimePromotionResult:
    """Promote a fully prepared Release using the one runtime index transaction."""

    paths = ImportProfitRuntimePaths(Path(runtime_root))
    building = Path(building_dir)
    release_id = validate_release_id(release_id)
    if building.parent != paths.releases_dir or not building.name.startswith(
        ".building-"
    ):
        raise RuntimeWriteError("prepared Release directory is outside runtime releases")
    if not isinstance(index_reason, str) or not index_reason:
        raise RuntimeWriteError("index_reason must be non-empty")
    final = paths.release_dir(release_id)
    if final.exists():
        raise RuntimeWriteError("release_id already exists")
    manifest_path = building / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise RuntimeWriteError("prepared Release Manifest is missing")
    manifest_sha = file_sha256(manifest_path)
    dataset = load_soybean_query_dataset(
        building / BUSINESS_KEYS_FILENAME,
        building / SNAPSHOTS_FILENAME,
        building / RESULTS_FILENAME,
    )
    if dataset.business_key_count != expected_record_count:
        raise RuntimeWriteError("prepared Release row count mismatch")
    read_json(building / QUALITY_FILENAME, "quality report")
    manifest = read_json(manifest_path, "Release Manifest")
    if (
        manifest.get("release_id") != release_id
        or manifest.get("parent_release_id") != current.release_id
        or manifest.get("generation") != current.generation + 1
    ):
        raise RuntimeWriteError("prepared Release lineage is invalid")
    manual_sha = (
        file_sha256(building / MANUAL_CNF_FILENAME)
        if (building / MANUAL_CNF_FILENAME).is_file()
        else None
    )
    renamed = False
    promoted = False
    new_index_sha: str | None = None
    try:
        building.replace(final)
        renamed = True
        new_index = RuntimeReleaseIndex(
            schema_version=RUNTIME_SCHEMA_VERSION,
            generation=current.generation + 1,
            current_release_id=release_id,
            previous_release_id=current.release_id,
            updated_at=updated_at,
            index_reason=index_reason,
            current_manifest_sha256=manifest_sha,
        )
        new_index_sha = write_release_index_atomically(
            paths.runtime_root,
            new_index,
            expected_previous_sha256=current.identity.index_sha256,
        )
        promoted = True
        try:
            loaded = load_runtime_release_dataset(
                paths.runtime_root,
                expected_release_id=release_id,
                expected_index_sha256=new_index_sha,
            )
            if loaded.dataset.business_key_count != expected_record_count:
                raise RuntimeWriteError("promoted Release row count mismatch")
        except Exception:
            write_release_index_atomically(
                paths.runtime_root,
                current.index,
                expected_previous_sha256=new_index_sha,
            )
            promoted = False
            raise
        return RuntimePromotionResult(
            release_id=release_id,
            generation=current.generation + 1,
            previous_release_id=current.release_id,
            index_sha256=new_index_sha,
            manifest_sha256=manifest_sha,
            manual_cnf_sha256=manual_sha,
        )
    except Exception:
        if renamed and not promoted and final.exists():
            shutil.rmtree(final)
        raise


def validate_release_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or SAFE_RELEASE_ID.fullmatch(value) is None
    ):
        raise RuntimeReleaseValidationError(
            "release_id must be a safe ASCII identifier"
        )
    return value


def parquet_identity(
    path: Path, filename: str, schema: pa.Schema
) -> RuntimeFileIdentity:
    table = pq.read_table(path)
    if table.schema != schema:
        raise RuntimeReleaseValidationError(
            f"{filename} does not match its fixed schema"
        )
    return RuntimeFileIdentity(
        filename=filename,
        size_bytes=path.stat().st_size,
        sha256=file_sha256(path),
        record_count=table.num_rows,
        schema_fingerprint=schema_fingerprint(schema),
    )


def json_identity(path: Path, filename: str) -> RuntimeFileIdentity:
    read_json(path, filename)
    return RuntimeFileIdentity(
        filename=filename,
        size_bytes=path.stat().st_size,
        sha256=file_sha256(path),
        record_count=None,
        schema_fingerprint=None,
    )


def write_json_exclusive(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(
            payload, ensure_ascii=False, sort_keys=False, indent=2
        )
        + "\n"
    ).encode("utf-8")
    write_bytes_exclusive(path, encoded)
    if read_json(path, path.name) != payload:
        raise RuntimeWriteError("JSON round-trip verification failed")


def write_bytes_exclusive(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def write_parquet(
    path: Path, schema: pa.Schema, rows: list[dict[str, object]]
) -> RuntimeFileIdentity:
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, path, compression="zstd")
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())
    identity = parquet_identity(path, path.name, schema)
    if identity.record_count != len(rows):
        raise RuntimeWriteError("Parquet row count verification failed")
    return identity


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8", errors="strict") as stream:
            payload = json.load(stream)
    except Exception as exc:
        raise RuntimeReleaseValidationError(
            f"{label} is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeReleaseValidationError(
            f"{label} root must be an object"
        )
    return payload


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeReleaseValidationError(
            "failed to read a Release file"
        ) from exc
    return digest.hexdigest().upper()


def schema_fingerprint(schema: pa.Schema) -> str:
    return hashlib.sha256(
        schema.serialize().to_pybytes()
    ).hexdigest().upper()


def utc_text(value: datetime) -> str:
    return require_utc_datetime(value).isoformat().replace("+00:00", "Z")


def parse_utc_text(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeReleaseValidationError(f"{label} must be UTC text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeReleaseValidationError(
            f"{label} must be valid UTC text"
        ) from exc
    return require_utc_datetime(parsed)


def require_utc_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeReleaseValidationError(
            "runtime datetime must be timezone-aware"
        )
    converted = value.astimezone(timezone.utc)
    if converted.utcoffset() != timezone.utc.utcoffset(converted):
        raise RuntimeReleaseValidationError(
            "runtime datetime must normalize to UTC"
        )
    return converted


def _validate_manifest_header(
    manifest: dict[str, Any], index: RuntimeReleaseIndex
) -> None:
    required = {
        "schema_version",
        "runtime_contract_version",
        "release_id",
        "parent_release_id",
        "release_reason",
        "generation",
        "created_at",
        "calculated_at",
        "config",
        "record_count",
        "success_count",
        "incomplete_count",
        "date_range",
        "origin_counts",
        "missing_reason_counts",
        "manual_cnf_exists",
        "manual_cnf_record_count",
        "manual_cnf_sha256",
        "output_files",
        "quality_report_filename",
    }
    if not required.issubset(manifest):
        raise RuntimeReleaseValidationError(
            "Release Manifest is missing required fields"
        )
    if (
        manifest["schema_version"] != RUNTIME_SCHEMA_VERSION
        or manifest["runtime_contract_version"]
        != RUNTIME_CONTRACT_VERSION
        or manifest["release_id"] != index.current_release_id
        or manifest["generation"] != index.generation
        or manifest["parent_release_id"] != index.previous_release_id
    ):
        raise RuntimeReleaseValidationError(
            "Release Manifest header does not match Release Index"
        )
    parse_utc_text(manifest["created_at"], "created_at")
    parse_utc_text(manifest["calculated_at"], "calculated_at")
    counts = (
        manifest["record_count"],
        manifest["success_count"],
        manifest["incomplete_count"],
    )
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in counts):
        raise RuntimeReleaseValidationError(
            "Release Manifest counts are invalid"
        )
    if counts[1] + counts[2] != counts[0]:
        raise RuntimeReleaseValidationError(
            "Release Manifest status counts do not reconcile"
        )
    serialized = json.dumps(manifest, ensure_ascii=False)
    if ":\\" in serialized or ":/" in serialized:
        raise RuntimeReleaseValidationError(
            "Release Manifest must not contain absolute paths"
        )


def _manifest_file_identity(
    output_files: dict[str, Any], filename: str, *, required: bool
) -> RuntimeFileIdentity:
    payload = output_files.get(filename)
    if payload is None:
        if required:
            raise RuntimeReleaseValidationError(
                f"Release Manifest omits {filename}"
            )
        raise RuntimeReleaseValidationError("optional file is absent")
    expected_fields = {
        "filename",
        "size_bytes",
        "sha256",
        "record_count",
        "schema_fingerprint",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise RuntimeReleaseValidationError(
            f"Release file identity is invalid: {filename}"
        )
    if payload["filename"] != filename:
        raise RuntimeReleaseValidationError(
            "Release file identity filename mismatch"
        )
    _validate_sha(payload["sha256"], f"{filename} SHA")
    return RuntimeFileIdentity(
        filename=filename,
        size_bytes=payload["size_bytes"],
        sha256=payload["sha256"],
        record_count=payload["record_count"],
        schema_fingerprint=payload["schema_fingerprint"],
    )


def _verify_file_identity(
    path: Path,
    identity: RuntimeFileIdentity,
    schema: pa.Schema | tuple[pa.Schema, ...] | None,
) -> None:
    if not path.is_file():
        raise RuntimeReleaseValidationError(
            f"Release file does not exist: {identity.filename}"
        )
    if (
        path.stat().st_size != identity.size_bytes
        or file_sha256(path) != identity.sha256
    ):
        raise RuntimeReleaseValidationError(
            f"Release file identity mismatch: {identity.filename}"
        )
    if schema is not None:
        schemas = schema if isinstance(schema, tuple) else (schema,)
        table = pq.read_table(path)
        actual_schema = next(
            (candidate for candidate in schemas if table.schema == candidate),
            None,
        )
        if actual_schema is None:
            raise RuntimeReleaseValidationError(
                f"{identity.filename} does not match a supported schema"
            )
        actual = parquet_identity(path, identity.filename, actual_schema)
        if (
            actual.record_count != identity.record_count
            or actual.schema_fingerprint != identity.schema_fingerprint
        ):
            raise RuntimeReleaseValidationError(
                f"Release Parquet metadata mismatch: {identity.filename}"
            )
    else:
        read_json(path, identity.filename)


def _validate_sha(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789ABCDEF" for character in value)
    ):
        raise RuntimeReleaseValidationError(f"{label} is invalid")
