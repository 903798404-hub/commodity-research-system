"""Strict readers for standardized import-profit market Parquet files."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Generic, TypeVar

import pyarrow as pa
import pyarrow.parquet as pq

from .market_snapshot import (
    LEGACY_UNKNOWN,
    QUOTE_DATE_LEGACY_UNKNOWN,
    CbotPricePoint,
    DcePricePoint,
    FxPricePoint,
)


CBOT_SCHEMA_VERSION = "reuters-cbot-v2"
FX_SCHEMA_VERSION = "reuters-fx-v2"
DCE_INCREMENTAL_SCHEMA_VERSION = "dce-post-close-v4"
DCE_HISTORICAL_SCHEMA_VERSION = "dce-explicit-history-v3"

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
LEGACY_DCE_INCREMENTAL_SCHEMA = pa.schema(
    [
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("instrument", pa.string(), nullable=False),
        pa.field("commodity", pa.string(), nullable=False),
        pa.field("exchange", pa.string(), nullable=False),
        pa.field("contract_code", pa.string(), nullable=False),
        pa.field("contract_year", pa.int16(), nullable=False),
        pa.field("contract_month", pa.int8(), nullable=False),
        pa.field("price_cny_per_tonne", pa.float64(), nullable=False),
        pa.field("price_type", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("source_function", pa.string(), nullable=False),
        pa.field("source_quote_date", pa.date32(), nullable=True),
        pa.field("source_quote_time", pa.time64("us"), nullable=False),
        pa.field(
            "captured_at",
            pa.timestamp("us", tz="Asia/Shanghai"),
            nullable=False,
        ),
        pa.field("capture_timezone", pa.string(), nullable=False),
        pa.field("quality_status", pa.string(), nullable=False),
        pa.field("is_usable", pa.bool_(), nullable=False),
    ]
)
LEGACY_QUOTE_DATE_DCE_INCREMENTAL_SCHEMA = pa.schema(
    [
        *LEGACY_DCE_INCREMENTAL_SCHEMA,
        pa.field("contract_identity_status", pa.string(), nullable=False),
        pa.field("source_contract_code", pa.string(), nullable=True),
        pa.field("source_delivery_month", pa.int8(), nullable=True),
    ]
)
DCE_INCREMENTAL_SCHEMA = pa.schema(
    [
        *LEGACY_QUOTE_DATE_DCE_INCREMENTAL_SCHEMA,
        pa.field("quote_date_evidence_status", pa.string(), nullable=False),
    ]
)
LEGACY_DCE_HISTORICAL_SCHEMA = pa.schema(
    [
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("instrument", pa.string(), nullable=False),
        pa.field("commodity", pa.string(), nullable=False),
        pa.field("exchange", pa.string(), nullable=False),
        pa.field("contract_code", pa.string(), nullable=False),
        pa.field("contract_year", pa.int16(), nullable=False),
        pa.field("contract_month", pa.int8(), nullable=False),
        pa.field("price_cny_per_tonne", pa.float64(), nullable=False),
        pa.field("price_type", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("source_function", pa.string(), nullable=False),
        pa.field("source_quote_date", pa.date32(), nullable=True),
        pa.field("source_quote_time", pa.time64("us"), nullable=True),
        pa.field("source_snapshot_sha256", pa.string(), nullable=False),
        pa.field("quality_status", pa.string(), nullable=False),
        pa.field("is_usable", pa.bool_(), nullable=False),
    ]
)
LEGACY_QUOTE_DATE_DCE_HISTORICAL_SCHEMA = pa.schema(
    [
        *LEGACY_DCE_HISTORICAL_SCHEMA,
        pa.field("contract_identity_status", pa.string(), nullable=False),
        pa.field("source_contract_code", pa.string(), nullable=True),
        pa.field("source_delivery_month", pa.int8(), nullable=True),
    ]
)
DCE_HISTORICAL_SCHEMA = pa.schema(
    [
        *LEGACY_QUOTE_DATE_DCE_HISTORICAL_SCHEMA,
        pa.field("quote_date_evidence_status", pa.string(), nullable=False),
    ]
)

CBOT_QUALITY_STATUSES = frozenset(
    {
        "zero_price",
        "negative_price",
        "invalid_negative_lead",
        "standard_window",
        "extended_window_unverified",
        "extreme_window_warning",
        "extreme_source_anomaly",
    }
)
FX_QUALITY_STATUSES = frozenset({"valid", "zero_fx", "negative_fx"})


class StandardIoError(RuntimeError):
    """Base error for standardized file loading."""


class StandardFileError(StandardIoError):
    """Raised when a source path cannot be safely read."""


class StandardSchemaError(StandardIoError):
    """Raised when ordered Arrow fields do not match a fixed schema."""


class StandardDataError(StandardIoError):
    """Raised when a standardized record violates its contract."""


class StandardDuplicateKeyError(StandardDataError):
    """Raised when a standardized source contains duplicate keys."""


@dataclass(frozen=True, slots=True)
class StandardFileIdentity:
    filename: str
    exists: bool
    size_bytes: int | None
    sha256: str | None
    schema_version: str | None
    schema_fingerprint: str | None
    record_count: int
    earliest_date: date | None
    latest_date: date | None
    duplicate_key_count: int

    def as_manifest_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "exists": self.exists,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "schema_version": self.schema_version,
            "schema_fingerprint": self.schema_fingerprint,
            "record_count": self.record_count,
            "date_range": {
                "earliest": _date_text(self.earliest_date),
                "latest": _date_text(self.latest_date),
            },
        }


RecordT = TypeVar("RecordT")


@dataclass(frozen=True, slots=True)
class LoadedStandardDataset(Generic[RecordT]):
    identity: StandardFileIdentity
    records: tuple[RecordT, ...]


LoadedCbotDataset = LoadedStandardDataset[CbotPricePoint]
LoadedFxDataset = LoadedStandardDataset[FxPricePoint]
LoadedDceDataset = LoadedStandardDataset[DcePricePoint]


def load_cbot_parquet(path: str | Path) -> LoadedCbotDataset:
    table, source_path = _read_exact_parquet(path, CBOT_SCHEMA)
    records = []
    keys = []
    for row in table.to_pylist():
        status = row["exchange_quality_status"]
        if status not in CBOT_QUALITY_STATUSES:
            raise StandardDataError(
                f"CBOT exchange_quality_status is not recognized: {status!r}"
            )
        expected_lead = (
            (row["contract_year"] - row["market_date"].year) * 12
            + row["contract_month"]
            - row["market_date"].month
        )
        if row["lead_months"] != expected_lead:
            raise StandardDataError(
                "CBOT lead_months does not match market date and contract"
            )
        if row["eligible_for_import_profit"] and not row["is_usable"]:
            raise StandardDataError(
                "CBOT eligible_for_import_profit requires is_usable=true"
            )
        try:
            record = CbotPricePoint(
                market_date=row["market_date"],
                contract_year=row["contract_year"],
                contract_month=row["contract_month"],
                price_cents_per_bushel=row["price_cents_per_bushel"],
                exchange_quality_status=status,
                is_usable=row["is_usable"],
                eligible_for_import_profit=row["eligible_for_import_profit"],
                source="reuters_sql",
                source_table=row["source_table"],
                source_column=row["source_column"],
                source_statement_index=row["source_statement_index"],
                source_snapshot_sha256=row["source_snapshot_sha256"],
            )
        except ValueError as exc:
            raise StandardDataError(f"invalid CBOT record: {exc}") from exc
        records.append(record)
        keys.append(record.key)
    _validate_keys(keys, "CBOT")
    return LoadedStandardDataset(
        identity=_identity(
            source_path,
            CBOT_SCHEMA,
            CBOT_SCHEMA_VERSION,
            len(records),
            [record.market_date for record in records],
        ),
        records=tuple(records),
    )


def load_fx_parquet(path: str | Path) -> LoadedFxDataset:
    table, source_path = _read_exact_parquet(path, FX_SCHEMA)
    records = []
    keys = []
    for row in table.to_pylist():
        status = row["quality_status"]
        if status not in FX_QUALITY_STATUSES:
            raise StandardDataError(f"FX quality_status is not recognized: {status!r}")
        if row["unit"] != "cnh_per_usd":
            raise StandardDataError("FX unit must remain cnh_per_usd")
        if status != "valid" or row["is_usable"] is not True:
            raise StandardDataError(
                "FX standardized records must be valid and usable"
            )
        try:
            record = FxPricePoint(
                market_date=row["market_date"],
                tenor_months=row["tenor_months"],
                fx_value=row["fx_value"],
                source="reuters_sql",
                source_table=row["source_table"],
                source_column=row["source_column"],
                source_snapshot_sha256=row["source_snapshot_sha256"],
            )
        except ValueError as exc:
            raise StandardDataError(f"invalid FX record: {exc}") from exc
        records.append(record)
        keys.append(record.key)
    _validate_keys(keys, "FX")
    return LoadedStandardDataset(
        identity=_identity(
            source_path,
            FX_SCHEMA,
            FX_SCHEMA_VERSION,
            len(records),
            [record.market_date for record in records],
        ),
        records=tuple(records),
    )


def load_dce_parquet(path: str | Path) -> LoadedDceDataset:
    source_path = _require_file(path)
    table = _read_parquet(source_path)
    if table.schema == DCE_INCREMENTAL_SCHEMA:
        schema = DCE_INCREMENTAL_SCHEMA
        schema_version = DCE_INCREMENTAL_SCHEMA_VERSION
        historical = False
        legacy_identity = False
        legacy_quote_date = False
    elif table.schema == LEGACY_QUOTE_DATE_DCE_INCREMENTAL_SCHEMA:
        schema = LEGACY_QUOTE_DATE_DCE_INCREMENTAL_SCHEMA
        schema_version = "dce-post-close-v3"
        historical = False
        legacy_identity = False
        legacy_quote_date = True
    elif table.schema == LEGACY_DCE_INCREMENTAL_SCHEMA:
        schema = LEGACY_DCE_INCREMENTAL_SCHEMA
        schema_version = "dce-post-close-v2"
        historical = False
        legacy_identity = True
        legacy_quote_date = True
    elif table.schema == DCE_HISTORICAL_SCHEMA:
        schema = DCE_HISTORICAL_SCHEMA
        schema_version = DCE_HISTORICAL_SCHEMA_VERSION
        historical = True
        legacy_identity = False
        legacy_quote_date = False
    elif table.schema == LEGACY_QUOTE_DATE_DCE_HISTORICAL_SCHEMA:
        schema = LEGACY_QUOTE_DATE_DCE_HISTORICAL_SCHEMA
        schema_version = "dce-explicit-history-v2"
        historical = True
        legacy_identity = False
        legacy_quote_date = True
    elif table.schema == LEGACY_DCE_HISTORICAL_SCHEMA:
        schema = LEGACY_DCE_HISTORICAL_SCHEMA
        schema_version = "dce-explicit-history-v1"
        historical = True
        legacy_identity = True
        legacy_quote_date = True
    else:
        raise StandardSchemaError(
            "DCE Parquet schema does not match an approved ordered schema"
        )
    _validate_metadata_count(source_path, table)
    records = []
    keys = []
    for row in table.to_pylist():
        _validate_dce_contract_metadata(row)
        if historical and not row["source_snapshot_sha256"].strip():
            raise StandardDataError(
                "historical DCE source_snapshot_sha256 must be non-empty"
            )
        try:
            record = DcePricePoint(
                business_date=row["business_date"],
                contract_code=row["contract_code"],
                price_cny_per_tonne=row["price_cny_per_tonne"],
                price_type=row["price_type"],
                source=row["source"],
                source_function=row["source_function"],
                is_usable=row["is_usable"],
                quote_date_evidence_status=(
                    QUOTE_DATE_LEGACY_UNKNOWN
                    if legacy_quote_date
                    else row["quote_date_evidence_status"]
                ),
                source_quote_date=(
                    None if legacy_quote_date else row["source_quote_date"]
                ),
                source_quote_time=(
                    None if legacy_quote_date else row["source_quote_time"]
                ),
                source_snapshot_sha256=(
                    row["source_snapshot_sha256"] if historical else None
                ),
                contract_identity_status=(
                    LEGACY_UNKNOWN
                    if legacy_identity
                    else row["contract_identity_status"]
                ),
                source_contract_code=(
                    None if legacy_identity else row["source_contract_code"]
                ),
                source_delivery_month=(
                    None if legacy_identity else row["source_delivery_month"]
                ),
            )
        except ValueError as exc:
            raise StandardDataError(f"invalid DCE record: {exc}") from exc
        records.append(record)
        keys.append(record.key)
    _validate_keys(keys, "DCE")
    return LoadedStandardDataset(
        identity=_identity(
            source_path,
            schema,
            schema_version,
            len(records),
            [record.business_date for record in records],
        ),
        records=tuple(records),
    )


def optional_file_identity(
    path: str | Path,
    *,
    schema: pa.Schema,
    schema_version: str,
    record_count: int,
    dates: tuple[date, ...],
) -> StandardFileIdentity:
    """Build a safe identity for an optionally absent, separately parsed file."""

    source_path = Path(path)
    if not source_path.exists():
        return StandardFileIdentity(
            filename=source_path.name,
            exists=False,
            size_bytes=None,
            sha256=None,
            schema_version=schema_version,
            schema_fingerprint=_schema_fingerprint(schema),
            record_count=0,
            earliest_date=None,
            latest_date=None,
            duplicate_key_count=0,
        )
    source_path = _require_file(source_path)
    return _identity(
        source_path,
        schema,
        schema_version,
        record_count,
        list(dates),
    )


def _read_exact_parquet(
    path: str | Path,
    schema: pa.Schema,
) -> tuple[pa.Table, Path]:
    source_path = _require_file(path)
    table = _read_parquet(source_path)
    if table.schema != schema:
        raise StandardSchemaError(
            "Parquet schema fields, order, types, or nullability do not match"
        )
    _validate_metadata_count(source_path, table)
    return table, source_path


def _require_file(path: str | Path) -> Path:
    source_path = Path(path)
    if not source_path.exists():
        raise StandardFileError(f"standard file does not exist: {source_path.name}")
    if not source_path.is_file():
        raise StandardFileError(
            f"standard file path is not a regular file: {source_path.name}"
        )
    return source_path


def _read_parquet(path: Path) -> pa.Table:
    try:
        return pq.read_table(path)
    except Exception as exc:
        raise StandardFileError(f"failed to read Parquet {path.name}: {exc}") from exc


def _validate_metadata_count(path: Path, table: pa.Table) -> None:
    try:
        metadata_count = pq.ParquetFile(path).metadata.num_rows
    except Exception as exc:
        raise StandardFileError(
            f"failed to inspect Parquet metadata {path.name}: {exc}"
        ) from exc
    if metadata_count != table.num_rows:
        raise StandardDataError("Parquet metadata and loaded row counts differ")


def _validate_keys(keys: list[tuple], label: str) -> None:
    if len(keys) != len(set(keys)):
        raise StandardDuplicateKeyError(
            f"{label} standardized file contains duplicate keys"
        )
    if keys != sorted(keys):
        raise StandardDataError(
            f"{label} standardized file is not sorted by its canonical key"
        )


def _validate_dce_contract_metadata(row: dict[str, object]) -> None:
    code = row["contract_code"]
    if not isinstance(code, str) or len(code) != 5:
        raise StandardDataError("DCE contract_code must be complete")
    expected_instrument = {"M": "soymeal", "Y": "soyoil"}.get(code[0])
    if expected_instrument is None or not code[1:].isdigit():
        raise StandardDataError("DCE contract_code must be a complete M/Y code")
    expected_year = 2000 + int(code[1:3])
    expected_month = int(code[3:5])
    if (
        row["instrument"] != expected_instrument
        or row["commodity"] != "soybean"
        or row["exchange"] != "DCE"
        or row["contract_year"] != expected_year
        or row["contract_month"] != expected_month
    ):
        raise StandardDataError(
            "DCE contract metadata does not match the full contract_code"
        )
    if not isinstance(row["quality_status"], str) or not row["quality_status"].strip():
        raise StandardDataError("DCE quality_status must be non-empty")


def _identity(
    path: Path,
    schema: pa.Schema,
    schema_version: str,
    record_count: int,
    dates: list[date],
) -> StandardFileIdentity:
    return StandardFileIdentity(
        filename=path.name,
        exists=True,
        size_bytes=path.stat().st_size,
        sha256=_file_sha256(path),
        schema_version=schema_version,
        schema_fingerprint=_schema_fingerprint(schema),
        record_count=record_count,
        earliest_date=min(dates, default=None),
        latest_date=max(dates, default=None),
        duplicate_key_count=0,
    )


def _schema_fingerprint(schema: pa.Schema) -> str:
    return hashlib.sha256(schema.serialize().to_pybytes()).hexdigest().upper()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise StandardFileError(f"failed to hash {path.name}: {exc}") from exc
    return digest.hexdigest().upper()


def _date_text(value: date | None) -> str | None:
    return None if value is None else value.isoformat()
