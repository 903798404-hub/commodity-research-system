"""Strict, concurrent-safe Parquet storage for manual soybean CNF quotes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
from math import isfinite
from numbers import Real
import os
from pathlib import Path
import re
import shutil
from typing import Any, Collection, Iterable, Sequence
import uuid

from filelock import FileLock, Timeout
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .models import BusinessKey, BusinessKeyError


KEY_FIELDS = (
    "business_date",
    "commodity",
    "origin",
    "shipment_year",
    "shipment_month",
)
CNF_FIELDS = (
    *KEY_FIELDS,
    "cnf_cents_per_bushel",
    "source",
    "updated_at",
    "batch_id",
)
CNF_SCHEMA = pa.schema(
    [
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("commodity", pa.string(), nullable=False),
        pa.field("origin", pa.string(), nullable=False),
        pa.field("shipment_year", pa.int16(), nullable=False),
        pa.field("shipment_month", pa.int8(), nullable=False),
        pa.field("cnf_cents_per_bushel", pa.float64(), nullable=True),
        pa.field("source", pa.string(), nullable=False),
        pa.field("updated_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("batch_id", pa.string(), nullable=False),
    ]
)
ALLOWED_SOURCE = "manual_ui"
MAX_LOCK_TIMEOUT_SECONDS = 300.0
SAFE_BATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

CnfKeyTuple = tuple[date, str, str, int, int]


class CnfStoreError(RuntimeError):
    status = "validation_failed"


class CnfStoreFileError(CnfStoreError):
    pass


class CnfStoreSchemaError(CnfStoreError):
    pass


class CnfStoreValidationError(CnfStoreError):
    pass


class CnfDuplicateKeyError(CnfStoreValidationError):
    pass


class CnfConcurrentUpdateError(CnfStoreError):
    status = "concurrent_conflict"


class CnfStoreLockedError(CnfStoreError):
    status = "locked"


class CnfStoreWriteError(CnfStoreError):
    status = "write_failed"


@dataclass(frozen=True, slots=True)
class CnfQuoteUpdate:
    business_key: BusinessKey
    cnf_cents_per_bushel: float | None
    source: str
    updated_at: datetime
    batch_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.business_key, BusinessKey):
            raise CnfStoreValidationError("business_key must be a BusinessKey")
        object.__setattr__(
            self,
            "cnf_cents_per_bushel",
            _validate_cnf(self.cnf_cents_per_bushel),
        )
        _validate_source(self.source)
        object.__setattr__(self, "updated_at", _utc_datetime(self.updated_at))
        _validate_batch_id(self.batch_id)


@dataclass(frozen=True, slots=True)
class CnfQuoteRecord:
    business_key: BusinessKey
    cnf_cents_per_bushel: float | None
    source: str
    updated_at: datetime
    batch_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.business_key, BusinessKey):
            raise CnfStoreValidationError("business_key must be a BusinessKey")
        object.__setattr__(
            self,
            "cnf_cents_per_bushel",
            _validate_cnf(self.cnf_cents_per_bushel),
        )
        _validate_source(self.source)
        object.__setattr__(self, "updated_at", _utc_datetime(self.updated_at))
        _validate_batch_id(self.batch_id)

    @property
    def key(self) -> CnfKeyTuple:
        return business_key_tuple(self.business_key)

    def as_row(self) -> dict[str, Any]:
        key = self.business_key
        return {
            "business_date": key.business_date,
            "commodity": key.commodity,
            "origin": key.origin,
            "shipment_year": key.shipment_year,
            "shipment_month": key.shipment_month,
            "cnf_cents_per_bushel": self.cnf_cents_per_bushel,
            "source": self.source,
            "updated_at": self.updated_at,
            "batch_id": self.batch_id,
        }


@dataclass(frozen=True, slots=True)
class CnfStoreSnapshot:
    records: tuple[CnfQuoteRecord, ...]
    store_exists: bool
    record_count: int
    store_sha256: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.records, tuple):
            raise CnfStoreValidationError("snapshot records must be an immutable tuple")
        if self.record_count != len(self.records):
            raise CnfStoreValidationError("snapshot record_count does not match records")
        if self.store_exists != (self.store_sha256 is not None):
            raise CnfStoreValidationError("snapshot identity and existence disagree")


@dataclass(frozen=True, slots=True)
class CnfWriteResult:
    status: str
    previous_sha256: str | None
    new_sha256: str | None
    previous_record_count: int
    new_record_count: int
    inserted_count: int
    updated_count: int
    cleared_to_null_count: int
    unchanged_count: int
    batch_id: str
    updated_keys: tuple[CnfKeyTuple, ...]
    backup_created: bool
    backup_filename: str | None


def empty_cnf_dataframe() -> pd.DataFrame:
    """Return a caller-owned empty frame with the fixed logical columns."""
    return CNF_SCHEMA.empty_table().to_pandas()


def records_as_dataframe(records: Sequence[CnfQuoteRecord]) -> pd.DataFrame:
    """Return a caller-owned DataFrame; records remain immutable."""
    return pa.Table.from_pylist(
        [record.as_row() for record in records],
        schema=CNF_SCHEMA,
    ).to_pandas()


def load_cnf_store(
    path: str | Path,
    *,
    allowed_origins: Collection[str],
) -> CnfStoreSnapshot:
    store_path = Path(path)
    origins = _validate_allowed_origins(allowed_origins)
    if not store_path.exists():
        return CnfStoreSnapshot((), False, 0, None)
    if not store_path.is_file():
        raise CnfStoreFileError("CNF store path is not a file")
    return _read_store_file(store_path, origins)


def upsert_cnf_quotes(
    path: str | Path,
    updates: Iterable[CnfQuoteUpdate],
    *,
    allowed_origins: Collection[str],
    expected_store_sha256: str | None,
    lock_timeout_seconds: float = 10.0,
    backup_dir: str | Path | None = None,
) -> CnfWriteResult:
    store_path = Path(path)
    origins = _validate_allowed_origins(allowed_origins)
    prepared = tuple(updates)
    _validate_updates(prepared, origins)
    timeout = _validate_lock_timeout(lock_timeout_seconds)
    batch_ids = {item.batch_id for item in prepared}
    if len(batch_ids) != 1:
        raise CnfStoreValidationError("all updates must share one batch_id")
    batch_id = prepared[0].batch_id
    lock = FileLock(f"{store_path}.lock")
    try:
        lock.acquire(timeout=timeout)
    except Timeout as exc:
        raise CnfStoreLockedError("CNF store lock acquisition timed out") from exc
    try:
        current = load_cnf_store(store_path, allowed_origins=origins)
        if current.store_sha256 != expected_store_sha256:
            raise CnfConcurrentUpdateError(
                "expected CNF store identity does not match the locked store"
            )
        current_by_key = {record.key: record for record in current.records}
        inserted_count = 0
        updated_count = 0
        cleared_count = 0
        unchanged_count = 0
        updated_keys: list[CnfKeyTuple] = []
        final_by_key = dict(current_by_key)
        for update in prepared:
            key = business_key_tuple(update.business_key)
            previous = current_by_key.get(key)
            if previous is not None and _same_business_value(previous, update):
                unchanged_count += 1
                continue
            record = CnfQuoteRecord(
                business_key=update.business_key,
                cnf_cents_per_bushel=update.cnf_cents_per_bushel,
                source=update.source,
                updated_at=update.updated_at,
                batch_id=update.batch_id,
            )
            final_by_key[key] = record
            updated_keys.append(key)
            if previous is None:
                inserted_count += 1
            else:
                updated_count += 1
                if (
                    previous.cnf_cents_per_bushel is not None
                    and update.cnf_cents_per_bushel is None
                ):
                    cleared_count += 1
        if not updated_keys:
            return CnfWriteResult(
                status="no_change",
                previous_sha256=current.store_sha256,
                new_sha256=current.store_sha256,
                previous_record_count=current.record_count,
                new_record_count=current.record_count,
                inserted_count=0,
                updated_count=0,
                cleared_to_null_count=0,
                unchanged_count=unchanged_count,
                batch_id=batch_id,
                updated_keys=(),
                backup_created=False,
                backup_filename=None,
            )
        final_records = tuple(
            sorted(final_by_key.values(), key=lambda record: record.key)
        )
        return _write_store_atomically(
            store_path,
            final_records,
            origins,
            current,
            inserted_count=inserted_count,
            updated_count=updated_count,
            cleared_count=cleared_count,
            unchanged_count=unchanged_count,
            batch_id=batch_id,
            updated_keys=tuple(sorted(updated_keys)),
            backup_dir=Path(backup_dir) if backup_dir is not None else None,
        )
    finally:
        if lock.is_locked:
            lock.release()


def business_key_tuple(key: BusinessKey) -> CnfKeyTuple:
    return (
        key.business_date,
        key.commodity,
        key.origin,
        key.shipment_year,
        key.shipment_month,
    )


def _read_store_file(
    path: Path,
    allowed_origins: tuple[str, ...],
) -> CnfStoreSnapshot:
    identity = _file_sha256(path)
    try:
        table = pq.read_table(path)
    except Exception as exc:
        raise CnfStoreFileError(
            f"failed to read CNF Parquet: {_bounded_message(exc)}"
        ) from exc
    if table.schema != CNF_SCHEMA:
        raise CnfStoreSchemaError("CNF Parquet schema does not match the fixed schema")
    rows = table.to_pylist()
    records: list[CnfQuoteRecord] = []
    keys: list[CnfKeyTuple] = []
    for row in rows:
        if tuple(row) != CNF_FIELDS:
            raise CnfStoreSchemaError("CNF Parquet fields are missing or reordered")
        if any(row[field] is None for field in KEY_FIELDS):
            raise CnfStoreValidationError("CNF stable key fields must be non-null")
        try:
            business_key = BusinessKey(
                row["business_date"],
                row["commodity"],
                row["origin"],
                row["shipment_year"],
                row["shipment_month"],
                allowed_origins,
                "soybean",
            )
        except BusinessKeyError as exc:
            raise CnfStoreValidationError(str(exc)) from exc
        cnf = _validate_cnf(row["cnf_cents_per_bushel"])
        _validate_source(row["source"])
        updated_at = _utc_datetime(row["updated_at"])
        _validate_batch_id(row["batch_id"])
        record = CnfQuoteRecord(
            business_key=business_key,
            cnf_cents_per_bushel=cnf,
            source=row["source"],
            updated_at=updated_at,
            batch_id=row["batch_id"],
        )
        records.append(record)
        keys.append(record.key)
    if len(keys) != len(set(keys)):
        raise CnfDuplicateKeyError("CNF store contains duplicate stable keys")
    if keys != sorted(keys):
        raise CnfStoreValidationError("CNF store records are not stably sorted")
    return CnfStoreSnapshot(tuple(records), True, len(records), identity)


def _write_store_atomically(
    store_path: Path,
    records: tuple[CnfQuoteRecord, ...],
    allowed_origins: tuple[str, ...],
    current: CnfStoreSnapshot,
    *,
    inserted_count: int,
    updated_count: int,
    cleared_count: int,
    unchanged_count: int,
    batch_id: str,
    updated_keys: tuple[CnfKeyTuple, ...],
    backup_dir: Path | None,
) -> CnfWriteResult:
    if not store_path.parent.is_dir():
        raise CnfStoreWriteError("CNF store parent directory does not exist")
    token = uuid.uuid4().hex
    temporary = store_path.parent / f".{store_path.name}.{token}.tmp"
    rollback = store_path.parent / f".{store_path.name}.{token}.rollback"
    backup_path: Path | None = None
    replaced = False
    try:
        table = pa.Table.from_pylist(
            [record.as_row() for record in records],
            schema=CNF_SCHEMA,
        )
        pq.write_table(table, temporary, compression="zstd")
        _fsync_file(temporary)
        temporary_snapshot = _read_store_file(temporary, allowed_origins)
        if temporary_snapshot.record_count != len(records):
            raise CnfStoreWriteError("temporary CNF row count mismatch")
        if tuple(record.key for record in temporary_snapshot.records) != tuple(
            record.key for record in records
        ):
            raise CnfStoreWriteError("temporary CNF key verification failed")
        temporary_sha = temporary_snapshot.store_sha256
        if current.store_exists:
            shutil.copy2(store_path, rollback)
            _fsync_file(rollback)
            if _file_sha256(rollback) != current.store_sha256:
                raise CnfStoreWriteError("rollback copy identity mismatch")
        if backup_dir is not None and current.store_exists:
            backup_dir.mkdir(parents=True, exist_ok=True)
            timestamp = max(record.updated_at for record in records).strftime(
                "%Y%m%dT%H%M%S%fZ"
            )
            backup_path = backup_dir / (
                f"{store_path.name}.before-{current.store_sha256[:12]}."
                f"{timestamp}{store_path.suffix}"
            )
            if backup_path.exists():
                raise CnfStoreWriteError("CNF backup already exists")
            try:
                shutil.copy2(store_path, backup_path)
                _fsync_file(backup_path)
            except Exception:
                if backup_path.exists():
                    backup_path.unlink()
                raise
            if _file_sha256(backup_path) != current.store_sha256:
                backup_path.unlink()
                raise CnfStoreWriteError("CNF backup identity mismatch")
        os.replace(temporary, store_path)
        replaced = True
        final_snapshot = _read_store_file(store_path, allowed_origins)
        if (
            final_snapshot.store_sha256 != temporary_sha
            or final_snapshot.record_count != len(records)
            or tuple(record.key for record in final_snapshot.records)
            != tuple(record.key for record in records)
        ):
            raise CnfStoreWriteError("formal CNF store verification failed")
        if rollback.exists():
            rollback.unlink()
        return CnfWriteResult(
            status="success",
            previous_sha256=current.store_sha256,
            new_sha256=final_snapshot.store_sha256,
            previous_record_count=current.record_count,
            new_record_count=final_snapshot.record_count,
            inserted_count=inserted_count,
            updated_count=updated_count,
            cleared_to_null_count=cleared_count,
            unchanged_count=unchanged_count,
            batch_id=batch_id,
            updated_keys=updated_keys,
            backup_created=backup_path is not None,
            backup_filename=backup_path.name if backup_path is not None else None,
        )
    except Exception as exc:
        restore_error: Exception | None = None
        if replaced:
            try:
                if current.store_exists:
                    os.replace(rollback, store_path)
                    if _file_sha256(store_path) != current.store_sha256:
                        raise CnfStoreWriteError("restored CNF identity mismatch")
                elif store_path.exists():
                    store_path.unlink()
            except Exception as restore_exc:
                restore_error = restore_exc
        for path in (temporary, rollback):
            if path.exists():
                path.unlink()
        if restore_error is not None:
            raise CnfStoreWriteError(
                f"CNF write and rollback failed: {_bounded_message(restore_error)}"
            ) from exc
        if isinstance(exc, CnfStoreWriteError):
            raise
        raise CnfStoreWriteError(
            f"CNF atomic write failed: {_bounded_message(exc)}"
        ) from exc


def _validate_updates(
    updates: tuple[CnfQuoteUpdate, ...],
    allowed_origins: tuple[str, ...],
) -> None:
    if not updates:
        raise CnfStoreValidationError("updates must not be empty")
    keys: list[CnfKeyTuple] = []
    for item in updates:
        if not isinstance(item, CnfQuoteUpdate):
            raise CnfStoreValidationError("updates must contain CnfQuoteUpdate objects")
        key = item.business_key
        try:
            BusinessKey(
                key.business_date,
                key.commodity,
                key.origin,
                key.shipment_year,
                key.shipment_month,
                allowed_origins,
                "soybean",
            )
        except BusinessKeyError as exc:
            raise CnfStoreValidationError(str(exc)) from exc
        keys.append(business_key_tuple(key))
    if len(keys) != len(set(keys)):
        raise CnfDuplicateKeyError("update batch contains duplicate stable keys")


def _same_business_value(
    record: CnfQuoteRecord,
    update: CnfQuoteUpdate,
) -> bool:
    return (
        record.cnf_cents_per_bushel == update.cnf_cents_per_bushel
        and record.source == update.source
    )


def _validate_cnf(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (bool, str, bytes)) or not isinstance(value, Real):
        raise CnfStoreValidationError(
            "cnf_cents_per_bushel must be None or a finite numeric value"
        )
    result = float(value)
    if not isfinite(result):
        raise CnfStoreValidationError("cnf_cents_per_bushel must be finite")
    return result


def _validate_source(value: object) -> None:
    if value != ALLOWED_SOURCE:
        raise CnfStoreValidationError("source must be manual_ui")


def _validate_batch_id(value: object) -> None:
    if not isinstance(value, str) or SAFE_BATCH_ID.fullmatch(value) is None:
        raise CnfStoreValidationError("batch_id must be a non-empty safe identifier")


def _utc_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CnfStoreValidationError("updated_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_allowed_origins(values: Collection[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise CnfStoreValidationError("allowed_origins must be a collection")
    origins = tuple(values)
    if not origins or any(
        not isinstance(value, str) or not value.strip() for value in origins
    ):
        raise CnfStoreValidationError("allowed_origins must contain non-empty strings")
    if len(origins) != len(set(origins)):
        raise CnfStoreValidationError("allowed_origins must be unique")
    return origins


def _validate_lock_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise CnfStoreValidationError("lock_timeout_seconds must be numeric")
    result = float(value)
    if not isfinite(result) or not 0 <= result <= MAX_LOCK_TIMEOUT_SECONDS:
        raise CnfStoreValidationError(
            f"lock_timeout_seconds must be between 0 and {MAX_LOCK_TIMEOUT_SECONDS:g}"
        )
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CnfStoreFileError(
            f"failed to hash CNF store: {_bounded_message(exc)}"
        ) from exc
    return digest.hexdigest().upper()


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _bounded_message(exc: BaseException) -> str:
    return str(exc).replace("\r", " ").replace("\n", " ")[:300]
