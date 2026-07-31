"""Historical DCE continuous-month adapter for the reviewed Reuters SQL table."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime
import math
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa

from agri_research_agent.data_sources.navicat_sql_stream import (
    SqlStatementType,
    iter_sql_statements,
)

from .config import SoybeanImportProfitConfig
from .contract_mapping import map_soybean_contracts
from .market_snapshot import DcePricePoint
from .models import BusinessKey
from .reuters_adapter import (
    SourceIdentity,
    extract_create_columns,
    inspect_source_identity,
    parse_insert_values,
)


ADAPTER_VERSION = "historical_dce_adapter/1"
SCHEMA_VERSION = "historical-dce-continuous-v1"
SOURCE = "reuters_sql"
SOURCE_TABLE = "内盘期货价格_收盘"
PRICE_TYPE = "historical_continuous_close"
FIELD_MAPPING = (
    ("DCE_豆粕_01月", "soymeal", 1),
    ("DCE_豆粕_05月", "soymeal", 5),
    ("DCE_豆粕_09月", "soymeal", 9),
    ("DCE_豆油_01月", "soyoil", 1),
    ("DCE_豆油_05月", "soyoil", 5),
    ("DCE_豆油_09月", "soyoil", 9),
)
REQUIRED_COLUMNS = ("Date", *(item[0] for item in FIELD_MAPPING))
CONTINUOUS_FIELDS = (
    "business_date",
    "instrument",
    "delivery_month",
    "price_cny_per_tonne",
    "price_type",
    "source",
    "source_table",
    "source_column",
    "source_snapshot_sha256",
)
HISTORICAL_DCE_CONTINUOUS_SCHEMA = pa.schema(
    [
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("instrument", pa.string(), nullable=False),
        pa.field("delivery_month", pa.int8(), nullable=False),
        pa.field("price_cny_per_tonne", pa.float64(), nullable=False),
        pa.field("price_type", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("source_table", pa.string(), nullable=False),
        pa.field("source_column", pa.string(), nullable=False),
        pa.field("source_snapshot_sha256", pa.string(), nullable=False),
    ]
)


class HistoricalDceError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        quality_report: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.quality_report = quality_report


@dataclass(frozen=True, slots=True)
class HistoricalDceContinuousPoint:
    business_date: date
    instrument: str
    delivery_month: int
    price_cny_per_tonne: float
    price_type: str
    source: str
    source_table: str
    source_column: str
    source_snapshot_sha256: str

    def __post_init__(self) -> None:
        if type(self.business_date) is not date:
            raise HistoricalDceError("business_date must be a real date")
        if self.instrument not in {"soymeal", "soyoil"}:
            raise HistoricalDceError("instrument must be soymeal or soyoil")
        if self.delivery_month not in {1, 5, 9}:
            raise HistoricalDceError("delivery_month must be 1, 5, or 9")
        if (
            isinstance(self.price_cny_per_tonne, bool)
            or not isinstance(self.price_cny_per_tonne, (int, float))
            or not math.isfinite(float(self.price_cny_per_tonne))
            or float(self.price_cny_per_tonne) <= 0
        ):
            raise HistoricalDceError("price_cny_per_tonne must be positive and finite")
        object.__setattr__(
            self, "price_cny_per_tonne", float(self.price_cny_per_tonne)
        )
        if self.price_type != PRICE_TYPE:
            raise HistoricalDceError(f"price_type must be {PRICE_TYPE}")
        if self.source != SOURCE:
            raise HistoricalDceError(f"source must be {SOURCE}")
        if self.source_table != SOURCE_TABLE:
            raise HistoricalDceError(f"source_table must be {SOURCE_TABLE}")
        if not self.source_column:
            raise HistoricalDceError("source_column must be non-empty")
        if not self.source_snapshot_sha256:
            raise HistoricalDceError("source_snapshot_sha256 must be non-empty")

    @property
    def key(self) -> tuple[date, str, int]:
        return (self.business_date, self.instrument, self.delivery_month)


@dataclass(frozen=True, slots=True)
class HistoricalDceResult:
    source_identity: SourceIdentity
    records: tuple[HistoricalDceContinuousPoint, ...]
    manifest_core: dict[str, Any]
    quality_report: dict[str, Any]


def adapt_historical_dce_sql(path: str | Path) -> HistoricalDceResult:
    source = Path(path)
    identity = inspect_source_identity(source)
    ddl_columns: tuple[str, ...] | None = None
    create_count = 0
    source_row_count = 0
    source_dates: list[date] = []
    seen_dates: set[date] = set()
    duplicate_dates = 0
    records: list[HistoricalDceContinuousPoint] = []
    keys: set[tuple[date, str, int]] = set()
    duplicate_keys = 0
    counts = Counter()
    coverage = Counter()
    fatal_issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    field_count_errors = 0

    for statement in iter_sql_statements(source):
        if statement.table_name != SOURCE_TABLE:
            continue
        if statement.statement_type is SqlStatementType.CREATE_TABLE:
            create_count += 1
            if create_count > 1:
                fatal_issues.append({"code": "duplicate_target_ddl", "count": 1})
                continue
            ddl_columns = extract_create_columns(statement)
            ddl_errors = _validate_ddl(ddl_columns)
            fatal_issues.extend(ddl_errors)
            continue
        if statement.statement_type is not SqlStatementType.INSERT:
            continue
        source_row_count += 1
        if ddl_columns is None:
            fatal_issues.append({"code": "insert_before_target_ddl", "count": 1})
            continue
        try:
            values = parse_insert_values(statement, ddl_columns)
        except ValueError as exc:
            field_count_errors += 1
            if field_count_errors <= 5:
                fatal_issues.append(
                    {
                        "code": "insert_field_count_or_literal_error",
                        "count": 1,
                        "statement_index": statement.statement_index,
                        "reason": str(exc)[:160],
                    }
                )
            continue
        row = dict(zip(ddl_columns, values, strict=True))
        try:
            business_date = _parse_market_date(row["Date"])
        except (KeyError, HistoricalDceError) as exc:
            fatal_issues.append(
                {
                    "code": "dce_date_parse_error",
                    "count": 1,
                    "statement_index": statement.statement_index,
                    "reason": str(exc)[:160],
                }
            )
            continue
        source_dates.append(business_date)
        if business_date in seen_dates:
            duplicate_dates += 1
        seen_dates.add(business_date)
        for column, instrument, delivery_month in FIELD_MAPPING:
            value = row.get(column)
            if value is None:
                counts["null"] += 1
                coverage[(instrument, delivery_month, "null")] += 1
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                counts["invalid"] += 1
                coverage[(instrument, delivery_month, "invalid")] += 1
                continue
            number = float(value)
            if not math.isfinite(number):
                counts["invalid"] += 1
                coverage[(instrument, delivery_month, "invalid")] += 1
                continue
            if number == 0:
                counts["zero"] += 1
                coverage[(instrument, delivery_month, "zero")] += 1
                continue
            if number < 0:
                counts["negative"] += 1
                coverage[(instrument, delivery_month, "negative")] += 1
                continue
            point = HistoricalDceContinuousPoint(
                business_date=business_date,
                instrument=instrument,
                delivery_month=delivery_month,
                price_cny_per_tonne=number,
                price_type=PRICE_TYPE,
                source=SOURCE,
                source_table=SOURCE_TABLE,
                source_column=column,
                source_snapshot_sha256=identity.sha256,
            )
            if point.key in keys:
                duplicate_keys += 1
            keys.add(point.key)
            records.append(point)
            counts["valid"] += 1
            coverage[(instrument, delivery_month, "valid")] += 1

    if create_count == 0:
        fatal_issues.append({"code": "target_table_missing", "count": 1})
    if source_row_count == 0:
        fatal_issues.append({"code": "target_table_has_no_rows", "count": 1})
    if duplicate_dates:
        fatal_issues.append({"code": "duplicate_source_date", "count": duplicate_dates})
    if duplicate_keys:
        fatal_issues.append(
            {"code": "duplicate_continuous_key", "count": duplicate_keys}
        )
    if field_count_errors > 5:
        fatal_issues.append(
            {
                "code": "insert_field_count_or_literal_error",
                "count": field_count_errors - 5,
            }
        )
    for code in ("zero", "negative", "invalid"):
        if counts[code]:
            fatal_issues.append(
                {"code": f"dce_{code}_price", "count": counts[code]}
            )
    if counts["null"]:
        warnings.append({"code": "dce_null_prices", "count": counts["null"]})

    records.sort(key=lambda item: item.key)
    status = "failed" if fatal_issues else ("passed_with_warnings" if warnings else "passed")
    coverage_report = {
        f"{instrument}_{month:02d}": {
            kind: coverage[(instrument, month, kind)]
            for kind in ("valid", "null", "zero", "negative", "invalid")
        }
        for _, instrument, month in FIELD_MAPPING
    }
    quality = {
        "candidate_status": status,
        "fatal_issues": fatal_issues[:30],
        "warnings": warnings[:20],
        "target_table_exists": create_count == 1,
        "ddl_matches": ddl_columns is not None and not _validate_ddl(ddl_columns),
        "source_row_count": source_row_count,
        "date_range": _date_range(source_dates),
        "duplicate_date_count": duplicate_dates,
        "duplicate_key_count": duplicate_keys,
        "field_count_error_count": field_count_errors,
        "price_counts": {
            name: counts[name]
            for name in ("valid", "null", "zero", "negative", "invalid")
        },
        "coverage": coverage_report,
    }
    if fatal_issues:
        raise HistoricalDceError(
            "historical DCE quality checks failed", quality_report=quality
        )

    manifest_core = {
        "adapter_version": ADAPTER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "source_table": SOURCE_TABLE,
        "source_columns": list(REQUIRED_COLUMNS),
        "source_ddl_column_count": len(ddl_columns or ()),
        "source_row_count": source_row_count,
        "business_date_range": _date_range(source_dates),
        "standard_record_count": len(records),
        "coverage": coverage_report,
        "duplicate_key_count": duplicate_keys,
    }
    return HistoricalDceResult(identity, tuple(records), manifest_core, quality)


def resolve_historical_dce_points(
    business_keys: Iterable[BusinessKey],
    *,
    config: SoybeanImportProfitConfig,
    continuous_points: Iterable[HistoricalDceContinuousPoint],
) -> tuple[DcePricePoint, ...]:
    """Resolve only explicitly supplied business keys into complete DCE contracts."""

    requested = tuple(business_keys)
    available_records = tuple(continuous_points)
    if any(not isinstance(item, BusinessKey) for item in requested):
        raise HistoricalDceError("business_keys must contain only BusinessKey values")
    if any(
        not isinstance(item, HistoricalDceContinuousPoint)
        for item in available_records
    ):
        raise HistoricalDceError(
            "continuous_points must contain only HistoricalDceContinuousPoint values"
        )
    available: dict[
        tuple[date, str, int], HistoricalDceContinuousPoint
    ] = {}
    for item in available_records:
        previous = available.get(item.key)
        if previous is not None and previous != item:
            raise HistoricalDceError("duplicate continuous key has conflicting prices")
        available[item.key] = item

    resolved: dict[tuple[date, str], DcePricePoint] = {}
    for business_key in requested:
        mapped = map_soybean_contracts(
            config, business_key.shipment_year, business_key.shipment_month
        )
        for instrument, contract in (
            ("soymeal", mapped.soymeal),
            ("soyoil", mapped.soyoil),
        ):
            source_point = available.get(
                (business_key.business_date, instrument, contract.contract_month)
            )
            if source_point is None:
                continue
            point = DcePricePoint(
                business_date=business_key.business_date,
                contract_code=contract.code,
                price_cny_per_tonne=source_point.price_cny_per_tonne,
                price_type=source_point.price_type,
                source=source_point.source,
                source_function=source_point.source_table,
                is_usable=True,
                source_quote_date=source_point.business_date,
                source_quote_time=None,
                source_snapshot_sha256=source_point.source_snapshot_sha256,
            )
            previous = resolved.get(point.key)
            if (
                previous is not None
                and previous.price_cny_per_tonne != point.price_cny_per_tonne
            ):
                raise HistoricalDceError(
                    "multiple business keys resolved one contract to conflicting prices"
                )
            resolved[point.key] = point
    return tuple(resolved[key] for key in sorted(resolved))


def records_as_dicts(
    records: Iterable[HistoricalDceContinuousPoint],
) -> list[dict[str, Any]]:
    return [asdict(record) for record in records]


def _validate_ddl(columns: tuple[str, ...]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if len(columns) != len(set(columns)):
        errors.append({"code": "ddl_duplicate_column", "count": 1})
    missing = [column for column in REQUIRED_COLUMNS if column not in columns]
    if missing:
        errors.append(
            {"code": "ddl_missing_required_columns", "count": len(missing), "fields": missing}
        )
    selected = tuple(column for column in columns if column in REQUIRED_COLUMNS)
    if not missing and selected != REQUIRED_COLUMNS:
        errors.append({"code": "ddl_required_column_order_mismatch", "count": 1})
    return errors


def _parse_market_date(value: object) -> date:
    if type(value) is date:
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError as exc:
            raise HistoricalDceError("Date is not ISO-compatible") from exc
    raise HistoricalDceError("Date must be a date or ISO-compatible text")


def _date_range(values: list[date]) -> dict[str, str | None]:
    return {
        "earliest": min(values).isoformat() if values else None,
        "latest": max(values).isoformat() if values else None,
    }
