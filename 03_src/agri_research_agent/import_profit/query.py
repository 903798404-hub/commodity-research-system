"""Strict, read-only querying for persisted soybean import-profit history."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
import hashlib
from pathlib import Path
from time import perf_counter
from types import MappingProxyType
from typing import Mapping

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .historical_cnf_adapter import shipment_year_for
from .result_store import RESULT_SCHEMA, SNAPSHOT_SCHEMA


KEY_FIELDS = (
    "business_date",
    "commodity",
    "origin",
    "shipment_year",
    "shipment_month",
)
MAX_LOOKBACK_DAYS = 260
HISTORICAL_BUSINESS_KEY_SCHEMA = pa.schema(
    [
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("commodity", pa.string(), nullable=False),
        pa.field("origin", pa.string(), nullable=False),
        pa.field("shipment_year", pa.int16(), nullable=False),
        pa.field("shipment_month", pa.int8(), nullable=False),
        pa.field("shipment_period", pa.string(), nullable=False),
        pa.field("cnf_is_null", pa.bool_(), nullable=False),
        pa.field("cnf_source", pa.string(), nullable=False),
    ]
)

CanonicalKey = tuple[date, str, str, int, int]
DateOriginMonthKey = tuple[date, str, int]


class ImportProfitQueryError(ValueError):
    """Base error for strict historical querying."""


class QuerySchemaError(ImportProfitQueryError):
    pass


class QueryDataIntegrityError(ImportProfitQueryError):
    pass


class UnknownOriginError(ImportProfitQueryError):
    pass


class UnknownMetricError(ImportProfitQueryError):
    pass


class InvalidLookbackError(ImportProfitQueryError):
    pass


class QueryMetric(StrEnum):
    CNF = "cnf"
    DUTY_PAID_COST = "duty_paid_cost"
    NET_CRUSH_MARGIN = "net_crush_margin"


class MatrixCellStatus(StrEnum):
    AVAILABLE = "available"
    MISSING_VALUE = "missing_value"
    MISSING_BUSINESS_DATE = "missing_business_date"
    MISSING_SHIPMENT_PERIOD = "missing_shipment_period"


@dataclass(frozen=True, slots=True)
class SafeFileIdentity:
    filename: str
    size_bytes: int
    sha256: str
    schema_fingerprint: str
    record_count: int


@dataclass(frozen=True, slots=True)
class SoybeanQueryRecord:
    business_date: date
    commodity: str
    origin: str
    shipment_year: int
    shipment_month: int
    shipment_period: str
    cnf_cents_per_bushel: float | None
    usd_cost_per_tonne: float | None
    duty_paid_cost_cny_per_tonne: float | None
    net_crush_margin_cny_per_tonne: float | None
    cbot_price_cents_per_bushel: float | None
    fx_value: float | None
    soymeal_price_cny_per_tonne: float | None
    soyoil_price_cny_per_tonne: float | None
    snapshot_status: str
    calculation_status: str
    missing_reasons: tuple[str, ...]
    cbot_contract: str
    soymeal_contract: str
    soyoil_contract: str
    fx_target_tenor: int
    fx_is_interpolated: bool
    cnf_source: str | None
    cbot_source: str | None
    fx_source: str | None
    soymeal_source: str | None
    soyoil_source: str | None
    soymeal_price_type: str | None
    soyoil_price_type: str | None
    parameter_version: str
    mapping_identity: str

    @property
    def key(self) -> CanonicalKey:
        return (
            self.business_date,
            self.commodity,
            self.origin,
            self.shipment_year,
            self.shipment_month,
        )

    def metric_value(self, metric: QueryMetric) -> float | None:
        if metric is QueryMetric.CNF:
            return self.cnf_cents_per_bushel
        if metric is QueryMetric.DUTY_PAID_COST:
            return self.duty_paid_cost_cny_per_tonne
        return self.net_crush_margin_cny_per_tonne


@dataclass(frozen=True, slots=True)
class SoybeanQueryDataset:
    business_keys_identity: SafeFileIdentity
    snapshots_identity: SafeFileIdentity
    results_identity: SafeFileIdentity
    date_range: tuple[date | None, date | None]
    business_key_count: int
    success_count: int
    incomplete_count: int
    origins: tuple[str, ...]
    records: tuple[SoybeanQueryRecord, ...]
    load_seconds: float
    index_seconds: float
    _by_key: Mapping[CanonicalKey, SoybeanQueryRecord] = field(
        repr=False, compare=False
    )
    _by_date_origin_month: Mapping[
        DateOriginMonthKey, SoybeanQueryRecord
    ] = field(repr=False, compare=False)
    _date_origins: frozenset[tuple[date, str]] = field(
        repr=False, compare=False
    )

    def get(
        self,
        *,
        business_date: date,
        origin: str,
        shipment_year: int,
        shipment_month: int,
        commodity: str = "soybean",
    ) -> SoybeanQueryRecord | None:
        self.require_origin(origin)
        return self._by_key.get(
            (
                business_date,
                commodity,
                origin,
                shipment_year,
                shipment_month,
            )
        )

    def get_by_date_origin_month(
        self, business_date: date, origin: str, shipment_month: int
    ) -> SoybeanQueryRecord | None:
        self.require_origin(origin)
        return self._by_date_origin_month.get(
            (business_date, origin, shipment_month)
        )

    def has_date_origin(self, business_date: date, origin: str) -> bool:
        self.require_origin(origin)
        return (business_date, origin) in self._date_origins

    def require_origin(self, origin: str) -> None:
        if origin not in self.origins:
            raise UnknownOriginError(f"unknown soybean origin: {origin!r}")


@dataclass(frozen=True, slots=True)
class RecentMatrixCell:
    business_date: date
    display_month: str
    shipment_year: int
    shipment_month: int
    shipment_period: str
    value: float | None
    status: MatrixCellStatus
    missing_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecentBusinessDayRow:
    business_date: date
    cells: tuple[RecentMatrixCell, ...]


@dataclass(frozen=True, slots=True)
class RecentMetricMatrix:
    metric: QueryMetric
    origin: str
    as_of_date: date
    lookback_days: int
    row_order: str
    column_months: tuple[int, ...]
    rows: tuple[RecentBusinessDayRow, ...]
    _cell_index: Mapping[tuple[date, int], RecentMatrixCell] = field(
        repr=False, compare=False
    )

    def get_cell(
        self, business_date: date, shipment_month: int
    ) -> RecentMatrixCell | None:
        return self._cell_index.get((business_date, shipment_month))

    def to_dataframe(self) -> pd.DataFrame:
        data = []
        for row in self.rows:
            values: dict[str, object] = {"日期": row.business_date}
            values.update(
                {
                    f"{cell.shipment_month}月船期": cell.value
                    for cell in row.cells
                }
            )
            data.append(values)
        return pd.DataFrame(
            data,
            columns=["日期", *(f"{month}月船期" for month in self.column_months)],
        )


def load_soybean_query_dataset(
    business_keys_path: str | Path,
    snapshots_path: str | Path,
    results_path: str | Path,
) -> SoybeanQueryDataset:
    started = perf_counter()
    key_table, key_identity = _read_exact(
        business_keys_path, HISTORICAL_BUSINESS_KEY_SCHEMA, "business keys"
    )
    snapshot_table, snapshot_identity = _read_exact(
        snapshots_path, SNAPSHOT_SCHEMA, "snapshots"
    )
    result_table, result_identity = _read_exact(
        results_path, RESULT_SCHEMA, "results"
    )
    load_seconds = perf_counter() - started

    index_started = perf_counter()
    key_rows = key_table.to_pylist()
    snapshot_rows = snapshot_table.to_pylist()
    result_rows = result_table.to_pylist()
    key_keys = _validated_keys(key_rows, "business keys")
    snapshot_keys = _validated_keys(snapshot_rows, "snapshots")
    result_keys = _validated_keys(result_rows, "results")
    if key_keys != snapshot_keys or key_keys != result_keys:
        raise QueryDataIntegrityError(
            "business keys, snapshots, and results must have identical ordered keys"
        )

    records: list[SoybeanQueryRecord] = []
    by_key: dict[CanonicalKey, SoybeanQueryRecord] = {}
    by_date_origin_month: dict[DateOriginMonthKey, SoybeanQueryRecord] = {}
    date_origins: set[tuple[date, str]] = set()
    success_count = 0
    for key_row, snapshot, result in zip(
        key_rows, snapshot_rows, result_rows, strict=True
    ):
        _validate_joined_rows(key_row, snapshot, result)
        record = SoybeanQueryRecord(
            business_date=snapshot["business_date"],
            commodity=snapshot["commodity"],
            origin=snapshot["origin"],
            shipment_year=snapshot["shipment_year"],
            shipment_month=snapshot["shipment_month"],
            shipment_period=snapshot["shipment_period"],
            cnf_cents_per_bushel=snapshot["cnf_cents_per_bushel"],
            usd_cost_per_tonne=result["usd_cost_per_tonne"],
            duty_paid_cost_cny_per_tonne=result[
                "duty_paid_cost_cny_per_tonne"
            ],
            net_crush_margin_cny_per_tonne=result[
                "net_crush_margin_cny_per_tonne"
            ],
            cbot_price_cents_per_bushel=snapshot[
                "cbot_price_cents_per_bushel"
            ],
            fx_value=snapshot["fx_value"],
            soymeal_price_cny_per_tonne=snapshot[
                "soymeal_price_cny_per_tonne"
            ],
            soyoil_price_cny_per_tonne=snapshot[
                "soyoil_price_cny_per_tonne"
            ],
            snapshot_status=snapshot["snapshot_status"],
            calculation_status=result["calculation_status"],
            missing_reasons=tuple(snapshot["missing_reasons"]),
            cbot_contract=(
                f"{snapshot['cbot_contract_year']:04d}-"
                f"{snapshot['cbot_contract_month']:02d}"
            ),
            soymeal_contract=snapshot["soymeal_contract_code"],
            soyoil_contract=snapshot["soyoil_contract_code"],
            fx_target_tenor=snapshot["fx_target_tenor"],
            fx_is_interpolated=snapshot["fx_is_interpolated"],
            cnf_source=snapshot["cnf_source"],
            cbot_source=snapshot["cbot_source"],
            fx_source=snapshot["fx_source"],
            soymeal_source=snapshot["soymeal_source"],
            soyoil_source=snapshot["soyoil_source"],
            soymeal_price_type=snapshot["soymeal_price_type"],
            soyoil_price_type=snapshot["soyoil_price_type"],
            parameter_version=snapshot["parameter_version"],
            mapping_identity=snapshot["mapping_identity"],
        )
        by_key[record.key] = record
        month_key = (
            record.business_date,
            record.origin,
            record.shipment_month,
        )
        if month_key in by_date_origin_month:
            raise QueryDataIntegrityError(
                "date, origin, and shipment month are not unique"
            )
        by_date_origin_month[month_key] = record
        date_origins.add((record.business_date, record.origin))
        records.append(record)
        success_count += record.calculation_status == "success"
    index_seconds = perf_counter() - index_started

    dates = [record.business_date for record in records]
    return SoybeanQueryDataset(
        business_keys_identity=key_identity,
        snapshots_identity=snapshot_identity,
        results_identity=result_identity,
        date_range=(
            min(dates) if dates else None,
            max(dates) if dates else None,
        ),
        business_key_count=len(records),
        success_count=success_count,
        incomplete_count=len(records) - success_count,
        origins=tuple(sorted({record.origin for record in records})),
        records=tuple(records),
        load_seconds=load_seconds,
        index_seconds=index_seconds,
        _by_key=MappingProxyType(by_key),
        _by_date_origin_month=MappingProxyType(by_date_origin_month),
        _date_origins=frozenset(date_origins),
    )


def iter_recent_business_weekdays(
    as_of_date: date, count: int
) -> tuple[date, ...]:
    if type(as_of_date) is not date:
        raise InvalidLookbackError("as_of_date must be a real date")
    if as_of_date.weekday() >= 5:
        raise InvalidLookbackError("as_of_date must be Monday through Friday")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 1 <= count <= MAX_LOOKBACK_DAYS
    ):
        raise InvalidLookbackError(
            f"count must be between 1 and {MAX_LOOKBACK_DAYS}"
        )
    result: list[date] = []
    cursor = as_of_date
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor)
        cursor -= timedelta(days=1)
    return tuple(result)


def build_recent_metric_matrix(
    dataset: SoybeanQueryDataset,
    *,
    origin: str,
    as_of_date: date,
    metric: QueryMetric | str,
    lookback_days: int = 10,
) -> RecentMetricMatrix:
    if not isinstance(dataset, SoybeanQueryDataset):
        raise ImportProfitQueryError("dataset must be a SoybeanQueryDataset")
    dataset.require_origin(origin)
    selected_metric = _metric(metric)
    business_dates = iter_recent_business_weekdays(as_of_date, lookback_days)
    rows: list[RecentBusinessDayRow] = []
    cell_index: dict[tuple[date, int], RecentMatrixCell] = {}
    for business_date in business_dates:
        has_date = dataset.has_date_origin(business_date, origin)
        cells = []
        for shipment_month in range(1, 13):
            record = dataset.get_by_date_origin_month(
                business_date, origin, shipment_month
            )
            if record is None:
                shipment_year = shipment_year_for(
                    business_date, shipment_month
                )
                status = (
                    MatrixCellStatus.MISSING_SHIPMENT_PERIOD
                    if has_date
                    else MatrixCellStatus.MISSING_BUSINESS_DATE
                )
                value = None
                reasons = (status.value,)
                shipment_period = (
                    f"{shipment_year:04d}-{shipment_month:02d}"
                )
            else:
                shipment_year = record.shipment_year
                shipment_period = record.shipment_period
                value = record.metric_value(selected_metric)
                status = (
                    MatrixCellStatus.AVAILABLE
                    if value is not None
                    else MatrixCellStatus.MISSING_VALUE
                )
                reasons = record.missing_reasons
            cell = RecentMatrixCell(
                business_date=business_date,
                display_month=f"{shipment_month}月船期",
                shipment_year=shipment_year,
                shipment_month=shipment_month,
                shipment_period=shipment_period,
                value=value,
                status=status,
                missing_reasons=reasons,
            )
            cells.append(cell)
            cell_index[(business_date, shipment_month)] = cell
        rows.append(RecentBusinessDayRow(business_date, tuple(cells)))
    return RecentMetricMatrix(
        metric=selected_metric,
        origin=origin,
        as_of_date=as_of_date,
        lookback_days=lookback_days,
        row_order="descending",
        column_months=tuple(range(1, 13)),
        rows=tuple(rows),
        _cell_index=MappingProxyType(cell_index),
    )


def _metric(metric: QueryMetric | str) -> QueryMetric:
    try:
        return metric if isinstance(metric, QueryMetric) else QueryMetric(metric)
    except (TypeError, ValueError) as exc:
        raise UnknownMetricError(f"unknown query metric: {metric!r}") from exc


def _read_exact(
    path: str | Path, schema: pa.Schema, label: str
) -> tuple[pa.Table, SafeFileIdentity]:
    source = Path(path)
    if not source.is_file():
        raise QuerySchemaError(f"{label} file does not exist")
    try:
        table = pq.read_table(source)
        metadata_count = pq.ParquetFile(source).metadata.num_rows
    except Exception as exc:
        raise QuerySchemaError(f"failed to read {label} Parquet") from exc
    if table.schema != schema:
        raise QuerySchemaError(f"{label} Schema or field order does not match")
    if metadata_count != table.num_rows:
        raise QuerySchemaError(f"{label} metadata row count does not match")
    digest = hashlib.sha256(source.read_bytes()).hexdigest().upper()
    fingerprint = hashlib.sha256(
        schema.serialize().to_pybytes()
    ).hexdigest().upper()
    return table, SafeFileIdentity(
        filename=source.name,
        size_bytes=source.stat().st_size,
        sha256=digest,
        schema_fingerprint=fingerprint,
        record_count=table.num_rows,
    )


def _validated_keys(rows: list[dict], label: str) -> tuple[CanonicalKey, ...]:
    keys = tuple(
        (
            row["business_date"],
            row["commodity"],
            row["origin"],
            row["shipment_year"],
            row["shipment_month"],
        )
        for row in rows
    )
    if len(keys) != len(set(keys)):
        raise QueryDataIntegrityError(f"{label} contains duplicate keys")
    if keys != tuple(sorted(keys)):
        raise QueryDataIntegrityError(f"{label} is not stably sorted")
    return keys


def _validate_joined_rows(
    key_row: dict, snapshot: dict, result: dict
) -> None:
    period = (
        f"{snapshot['shipment_year']:04d}-{snapshot['shipment_month']:02d}"
    )
    if any(row["shipment_period"] != period for row in (key_row, snapshot, result)):
        raise QueryDataIntegrityError("shipment_period is inconsistent")
    status_pair = (
        snapshot["snapshot_status"],
        result["calculation_status"],
    )
    if status_pair not in {
        ("complete", "success"),
        ("incomplete", "incomplete"),
    }:
        raise QueryDataIntegrityError(
            "snapshot and calculation statuses are inconsistent"
        )
    calculation_fields = (
        result["usd_cost_per_tonne"],
        result["duty_paid_cost_cny_per_tonne"],
        result["net_crush_margin_cny_per_tonne"],
    )
    snapshot_inputs = (
        snapshot["cnf_cents_per_bushel"],
        snapshot["cbot_price_cents_per_bushel"],
        snapshot["fx_value"],
        snapshot["soymeal_price_cny_per_tonne"],
        snapshot["soyoil_price_cny_per_tonne"],
    )
    if result["calculation_status"] == "success" and any(
        value is None for value in calculation_fields
    ):
        raise QueryDataIntegrityError(
            "successful calculation contains a null result"
        )
    if result["calculation_status"] == "success" and any(
        value is None for value in snapshot_inputs
    ):
        raise QueryDataIntegrityError(
            "successful calculation contains a null required market input"
        )
    if snapshot["snapshot_status"] == "complete" and any(
        value is None for value in snapshot_inputs
    ):
        raise QueryDataIntegrityError(
            "complete snapshot contains a null required market input"
        )
    if result["calculation_status"] == "incomplete" and any(
        value is not None for value in calculation_fields
    ):
        raise QueryDataIntegrityError(
            "incomplete calculation contains a non-null result"
        )
    if snapshot["missing_reasons"] != result["missing_reasons"]:
        raise QueryDataIntegrityError("missing reasons are inconsistent")
    if snapshot["parameter_version"] != result["parameter_version"]:
        raise QueryDataIntegrityError("parameter versions are inconsistent")
    if snapshot["mapping_identity"] != result["mapping_identity"]:
        raise QueryDataIntegrityError("mapping identities are inconsistent")
    if key_row["cnf_source"] != snapshot["cnf_source"]:
        raise QueryDataIntegrityError("CNF sources are inconsistent")
    cnf_value = snapshot["cnf_cents_per_bushel"]
    if key_row["cnf_is_null"] != (cnf_value is None):
        raise QueryDataIntegrityError("business-key CNF null marker is inconsistent")
    for value, reason in (
        (cnf_value, "missing_cnf"),
        (snapshot["cbot_price_cents_per_bushel"], "missing_cbot"),
        (snapshot["fx_value"], "missing_fx"),
        (snapshot["soymeal_price_cny_per_tonne"], "missing_soymeal"),
        (snapshot["soyoil_price_cny_per_tonne"], "missing_soyoil"),
    ):
        if (value is None) != (reason in snapshot["missing_reasons"]):
            raise QueryDataIntegrityError(
                f"{reason} value and missing reason are inconsistent"
            )
    if (snapshot["snapshot_status"] == "complete") != (
        len(snapshot["missing_reasons"]) == 0
    ):
        raise QueryDataIntegrityError(
            "snapshot status and missing reasons are inconsistent"
        )
