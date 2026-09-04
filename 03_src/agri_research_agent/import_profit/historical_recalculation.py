"""Historical full-key generation and exact-source soybean recalculation."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from math import isfinite
from pathlib import Path
from time import perf_counter
from typing import Iterable, Sequence

import pyarrow.parquet as pq

from .config import SoybeanImportProfitConfig
from .historical_cnf_adapter import (
    HISTORICAL_CNF_SCHEMA,
    HistoricalCnfQuote,
    shipment_year_for,
)
from .historical_dce_adapter import (
    HISTORICAL_DCE_CONTINUOUS_SCHEMA,
    HistoricalDceContinuousPoint,
    resolve_historical_dce_points,
)
from .market_snapshot import (
    CbotPricePoint,
    DcePricePoint,
    FxPricePoint,
    HistoricalCnfMarketPoint,
)
from .models import BusinessKey, MissingReason
from .parameter_snapshot import build_parameter_snapshot
from .recalculation import (
    SoybeanRecalculationBatch,
    SoybeanRecalculationItem,
    recalculate_soybean_keys,
)


PIPELINE_VERSION = "historical_recalculation/1"
APPROVED_ORIGINS = frozenset({"brazil", "us_gulf", "us_pnw", "argentina"})


class HistoricalRecalculationError(ValueError):
    """Raised when historical source structure or orchestration is invalid."""


class HistoricalSourceSchemaError(HistoricalRecalculationError):
    pass


class HistoricalBusinessKeyError(HistoricalRecalculationError):
    pass


@dataclass(frozen=True, slots=True)
class HistoricalKeySet:
    source_record_count: int
    excluded_after_as_of_count: int
    weekend_count: int
    business_keys: tuple[BusinessKey, ...]
    cnf_points: tuple[HistoricalCnfMarketPoint, ...]
    origin_counts: tuple[tuple[str, int], ...]
    shipment_year_counts: tuple[tuple[int, int], ...]
    shipment_month_counts: tuple[tuple[int, int], ...]
    cnf_null_count: int
    cnf_nonnull_count: int
    cnf_zero_count: int
    cnf_negative_count: int
    shipment_year_samples: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class HistoricalRecalculationRun:
    key_set: HistoricalKeySet
    resolved_dce_points: tuple[DcePricePoint, ...]
    recalculation_batch: SoybeanRecalculationBatch
    batch_size: int
    batch_count: int
    timings: tuple[tuple[str, float], ...]


def load_historical_cnf_parquet(
    path: str | Path,
) -> tuple[HistoricalCnfQuote, ...]:
    source = Path(path)
    table = _read_exact_parquet(source, HISTORICAL_CNF_SCHEMA, "historical CNF")
    records: list[HistoricalCnfQuote] = []
    keys = []
    for row in table.to_pylist():
        value = row["cnf_cents_per_bushel"]
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(float(value))
        ):
            raise HistoricalSourceSchemaError(
                "historical CNF contains a non-finite numeric value"
            )
        record = HistoricalCnfQuote(
            business_date=row["business_date"],
            commodity=row["commodity"],
            origin=row["origin"],
            shipment_year=row["shipment_year"],
            shipment_month=row["shipment_month"],
            cnf_cents_per_bushel=None if value is None else float(value),
            source=row["source"],
            updated_at=row["updated_at"],
            batch_id=row["batch_id"],
        )
        keys.append(record.key)
        records.append(record)
    _require_unique_sorted(keys, "historical CNF")
    return tuple(records)


def load_historical_dce_continuous_parquet(
    path: str | Path,
) -> tuple[HistoricalDceContinuousPoint, ...]:
    source = Path(path)
    table = _read_exact_parquet(
        source,
        HISTORICAL_DCE_CONTINUOUS_SCHEMA,
        "historical DCE continuous",
    )
    records = tuple(
        HistoricalDceContinuousPoint(**row) for row in table.to_pylist()
    )
    _require_unique_sorted(
        [record.key for record in records],
        "historical DCE continuous",
    )
    return records


def generate_historical_key_set(
    records: Iterable[HistoricalCnfQuote],
    *,
    config: SoybeanImportProfitConfig,
    as_of_date: date,
) -> HistoricalKeySet:
    source_records = tuple(records)
    if type(as_of_date) is not date:
        raise HistoricalBusinessKeyError("as_of_date must be a real date")
    if set(config.origin_codes) != APPROVED_ORIGINS:
        raise HistoricalBusinessKeyError(
            "config must contain exactly the four approved soybean origins"
        )
    source_keys = [record.key for record in source_records]
    if len(source_keys) != len(set(source_keys)):
        raise HistoricalBusinessKeyError(
            "historical CNF contains duplicate stable keys"
        )
    weekend_records = [
        record for record in source_records if record.business_date.weekday() >= 5
    ]
    if weekend_records:
        raise HistoricalBusinessKeyError(
            f"historical CNF contains {len(weekend_records)} weekend records"
        )

    included = tuple(
        record for record in source_records if record.business_date <= as_of_date
    )
    business_keys: list[BusinessKey] = []
    cnf_points: list[HistoricalCnfMarketPoint] = []
    origin_counts = Counter()
    shipment_year_counts = Counter()
    shipment_month_counts = Counter()
    null_count = 0
    zero_count = 0
    negative_count = 0
    coverage: dict[tuple[date, str], set[int]] = defaultdict(set)
    sample_rows: list[dict[str, object]] = []
    for record in included:
        if record.commodity != config.commodity:
            raise HistoricalBusinessKeyError(
                "historical CNF commodity must remain soybean"
            )
        if record.origin not in APPROVED_ORIGINS:
            raise HistoricalBusinessKeyError(
                "historical CNF origin is not approved"
            )
        if record.source != "historical_excel":
            raise HistoricalBusinessKeyError(
                "historical CNF source must remain historical_excel"
            )
        if not 1 <= record.shipment_month <= 12:
            raise HistoricalBusinessKeyError(
                "historical CNF shipment_month is invalid"
            )
        expected_year = shipment_year_for(
            record.business_date, record.shipment_month
        )
        if record.shipment_year != expected_year:
            raise HistoricalBusinessKeyError(
                "historical CNF shipment_year conflicts with the audited rolling rule"
            )
        key = BusinessKey(
            business_date=record.business_date,
            commodity=record.commodity,
            origin=record.origin,
            shipment_year=record.shipment_year,
            shipment_month=record.shipment_month,
            allowed_origins=config.origin_codes,
            expected_commodity=config.commodity,
            expected_shipment_period=(
                f"{record.shipment_year:04d}-{record.shipment_month:02d}"
            ),
        )
        business_keys.append(key)
        cnf_points.append(
            HistoricalCnfMarketPoint(
                business_key=key,
                cnf_cents_per_bushel=record.cnf_cents_per_bushel,
                source=record.source,
                updated_at=record.updated_at,
                batch_id=record.batch_id,
            )
        )
        origin_counts[key.origin] += 1
        shipment_year_counts[key.shipment_year] += 1
        shipment_month_counts[key.shipment_month] += 1
        coverage[(key.business_date, key.origin)].add(key.shipment_month)
        null_count += record.cnf_cents_per_bushel is None
        zero_count += record.cnf_cents_per_bushel == 0
        negative_count += (
            record.cnf_cents_per_bushel is not None
            and record.cnf_cents_per_bushel < 0
        )
        if len(sample_rows) < 5:
            sample_rows.append(
                {
                    "business_date": key.business_date.isoformat(),
                    "origin": key.origin,
                    "shipment_month": key.shipment_month,
                    "shipment_year": key.shipment_year,
                }
            )

    incomplete_coverage = [
        (business_date, origin, sorted(months))
        for (business_date, origin), months in coverage.items()
        if months != set(range(1, 13))
    ]
    if incomplete_coverage:
        raise HistoricalBusinessKeyError(
            "historical CNF does not contain all 12 months for every date and origin"
        )
    origins_by_date: dict[date, set[str]] = defaultdict(set)
    for business_date, origin in coverage:
        origins_by_date[business_date].add(origin)
    if any(origins != APPROVED_ORIGINS for origins in origins_by_date.values()):
        raise HistoricalBusinessKeyError(
            "historical CNF does not contain all four origins for every date"
        )

    paired = sorted(
        zip(business_keys, cnf_points, strict=True),
        key=lambda pair: _business_key_tuple(pair[0]),
    )
    sorted_keys = tuple(pair[0] for pair in paired)
    sorted_cnf = tuple(pair[1] for pair in paired)
    canonical = [_business_key_tuple(key) for key in sorted_keys]
    if len(canonical) != len(set(canonical)):
        raise HistoricalBusinessKeyError(
            "generated historical business keys are not unique"
        )
    return HistoricalKeySet(
        source_record_count=len(source_records),
        excluded_after_as_of_count=len(source_records) - len(included),
        weekend_count=0,
        business_keys=sorted_keys,
        cnf_points=sorted_cnf,
        origin_counts=tuple(sorted(origin_counts.items())),
        shipment_year_counts=tuple(sorted(shipment_year_counts.items())),
        shipment_month_counts=tuple(sorted(shipment_month_counts.items())),
        cnf_null_count=null_count,
        cnf_nonnull_count=len(included) - null_count,
        cnf_zero_count=zero_count,
        cnf_negative_count=negative_count,
        shipment_year_samples=tuple(sample_rows),
    )


def recalculate_historical_soybean(
    cnf_records: Iterable[HistoricalCnfQuote],
    *,
    config: SoybeanImportProfitConfig,
    as_of_date: date,
    continuous_dce_points: Iterable[HistoricalDceContinuousPoint],
    cbot_records: Iterable[CbotPricePoint],
    fx_records: Iterable[FxPricePoint],
    batch_size: int,
) -> HistoricalRecalculationRun:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise HistoricalRecalculationError("batch_size must be a positive integer")
    key_started = perf_counter()
    key_set = generate_historical_key_set(
        cnf_records, config=config, as_of_date=as_of_date
    )
    key_seconds = perf_counter() - key_started

    dce_started = perf_counter()
    resolved_dce = resolve_historical_dce_points(
        key_set.business_keys,
        config=config,
        continuous_points=tuple(continuous_dce_points),
    )
    dce_seconds = perf_counter() - dce_started

    cbot_by_date = _group_by_date(
        (
            record
            for record in cbot_records
            if record.eligible_for_import_profit and record.is_usable
        ),
        lambda record: record.market_date,
    )
    fx_by_date = _group_by_date(fx_records, lambda record: record.market_date)
    dce_by_date = _group_by_date(
        resolved_dce, lambda record: record.business_date
    )
    cnf_by_date = _group_by_date(
        key_set.cnf_points, lambda record: record.business_key.business_date
    )

    calculation_started = perf_counter()
    batches: list[SoybeanRecalculationBatch] = []
    for start in range(0, len(key_set.business_keys), batch_size):
        keys = key_set.business_keys[start : start + batch_size]
        dates = {key.business_date for key in keys}
        batch = recalculate_soybean_keys(
            keys,
            config=config,
            cnf_records=_records_for_dates(cnf_by_date, dates),
            cbot_records=_records_for_dates(cbot_by_date, dates),
            fx_records=_records_for_dates(fx_by_date, dates),
            dce_records=_records_for_dates(dce_by_date, dates),
        )
        batches.append(batch)
    combined = _combine_batches(
        batches,
        requested_keys=key_set.business_keys,
        config=config,
    )
    calculation_seconds = perf_counter() - calculation_started
    return HistoricalRecalculationRun(
        key_set=key_set,
        resolved_dce_points=resolved_dce,
        recalculation_batch=combined,
        batch_size=batch_size,
        batch_count=len(batches),
        timings=(
            ("business_key_generation_seconds", key_seconds),
            ("dce_resolution_seconds", dce_seconds),
            ("snapshot_and_calculation_seconds", calculation_seconds),
        ),
    )


def business_key_rows(key_set: HistoricalKeySet) -> list[dict[str, object]]:
    cnf_by_key = {
        _business_key_tuple(point.business_key): point
        for point in key_set.cnf_points
    }
    return [
        {
            "business_date": key.business_date,
            "commodity": key.commodity,
            "origin": key.origin,
            "shipment_year": key.shipment_year,
            "shipment_month": key.shipment_month,
            "shipment_period": key.shipment_period,
            "cnf_is_null": (
                cnf_by_key[_business_key_tuple(key)].cnf_cents_per_bushel is None
            ),
            "cnf_source": cnf_by_key[_business_key_tuple(key)].source,
        }
        for key in key_set.business_keys
    ]


def _read_exact_parquet(path: Path, schema, label: str):
    if not path.is_file():
        raise HistoricalSourceSchemaError(f"{label} Parquet does not exist")
    try:
        table = pq.read_table(path)
    except Exception as exc:
        raise HistoricalSourceSchemaError(f"failed to read {label} Parquet") from exc
    if table.schema != schema:
        raise HistoricalSourceSchemaError(f"{label} Schema does not match")
    if pq.ParquetFile(path).metadata.num_rows != table.num_rows:
        raise HistoricalSourceSchemaError(f"{label} metadata row count mismatch")
    return table


def _require_unique_sorted(keys: Sequence[tuple], label: str) -> None:
    if len(keys) != len(set(keys)):
        raise HistoricalSourceSchemaError(f"{label} contains duplicate keys")
    if list(keys) != sorted(keys):
        raise HistoricalSourceSchemaError(f"{label} is not stably sorted")


def _group_by_date(records, date_function):
    result: dict[date, list[object]] = defaultdict(list)
    for record in records:
        result[date_function(record)].append(record)
    return {key: tuple(value) for key, value in result.items()}


def _records_for_dates(
    grouped: dict[date, tuple[object, ...]],
    dates: set[date],
) -> tuple[object, ...]:
    return tuple(
        record
        for business_date in sorted(dates)
        for record in grouped.get(business_date, ())
    )


def _combine_batches(
    batches: Sequence[SoybeanRecalculationBatch],
    *,
    requested_keys: tuple[BusinessKey, ...],
    config: SoybeanImportProfitConfig,
) -> SoybeanRecalculationBatch:
    items: tuple[SoybeanRecalculationItem, ...] = tuple(
        item for batch in batches for item in batch.items
    )
    item_keys = tuple(item.business_key for item in items)
    if item_keys != requested_keys:
        raise HistoricalRecalculationError(
            "batch boundaries changed historical result ordering"
        )
    missing_counts = tuple(
        (
            reason,
            sum(reason in item.calculation_result.missing_reasons for item in items),
        )
        for reason in (
            MissingReason.MISSING_CNF,
            MissingReason.MISSING_CBOT,
            MissingReason.MISSING_FX,
            MissingReason.MISSING_SOYMEAL,
            MissingReason.MISSING_SOYOIL,
        )
    )
    success_count = sum(
        not item.calculation_result.missing_reasons for item in items
    )
    provenance = build_parameter_snapshot(config)
    assert provenance.parameter_hash is not None
    return SoybeanRecalculationBatch(
        requested_count=len(requested_keys),
        success_count=success_count,
        incomplete_count=len(requested_keys) - success_count,
        missing_reason_counts=missing_counts,
        items=items,
        requested_keys=requested_keys,
        parameter_version=str(config.schema_version),
        parameter_hash=provenance.parameter_hash,
        mapping_identity=config.contract_mapping_identity,
        mapping_hash=config.contract_mapping_hash,
        contract_override_hash=config.contract_override_hash,
    )


def _business_key_tuple(key: BusinessKey) -> tuple[date, str, str, int, int]:
    return (
        key.business_date,
        key.commodity,
        key.origin,
        key.shipment_year,
        key.shipment_month,
    )
