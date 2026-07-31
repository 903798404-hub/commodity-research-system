"""Reuters table adapter consuming the shared Navicat/MySQL SQL statement stream.

For CBOT records, ``is_usable`` means the observation is safe as general
standardized market data. ``eligible_for_import_profit`` is deliberately
narrower: it additionally requires an exact contract lead of zero through
twelve months. The two fields must not be treated as aliases.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from math import isfinite
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

from agri_research_agent.data_sources.navicat_sql_stream import (
    SqlStatement,
    SqlStatementType,
    iter_sql_statements,
)


ADAPTER_SCHEMA_VERSION = "2"
ADAPTER_VERSION = "reuters_adapter/2"
PARSER_VERSION = "navicat_sql_stream/1"
CBOT_TABLE = "us_cbot_soybean"
FX_TABLE = "美元兑人民币历史汇率"
TARGET_TABLES = (CBOT_TABLE, FX_TABLE)
CBOT_MONTHS = (1, 3, 5, 7, 8, 9, 11)
CBOT_COLUMNS = (
    "Date",
    *(
        f"CBOT大豆 {year}年{month}月"
        for year in range(2000, 2029)
        for month in CBOT_MONTHS
    ),
)
FX_COLUMNS = (
    "Date",
    "Spot",
    "Fwd_1M",
    "Fwd_2M",
    "Fwd_3M",
    "Fwd_4M",
    "Fwd_5M",
    "Fwd_6M",
    "Fwd_7M",
    "Fwd_8M",
    "Fwd_9M",
    "Fwd_10M",
    "Fwd_11M",
    "Fwd_1Y",
)
FX_TENORS = {column: index for index, column in enumerate(FX_COLUMNS[1:])}
MAX_QUALITY_SAMPLES = 5
KNOWN_EXTREME_ANOMALY = {
    "market_date": date(1969, 6, 26),
    "contract_year": 2007,
    "contract_month": 11,
    "price": 633.0,
    "lead_months": 461,
}


class ReutersAdapterError(ValueError):
    def __init__(
        self,
        reason: str,
        *,
        table_name: str | None = None,
        statement_index: int | None = None,
        sample: str = "",
    ) -> None:
        self.reason = reason
        self.table_name = table_name
        self.statement_index = statement_index
        self.sample = " ".join(sample.split())[:160]
        details = []
        if table_name is not None:
            details.append(f"table={table_name}")
        if statement_index is not None:
            details.append(f"statement_index={statement_index}")
        if self.sample:
            details.append(f"sample={self.sample!r}")
        suffix = f": {', '.join(details)}" if details else ""
        super().__init__(f"{reason}{suffix}")


class ReutersDdlError(ReutersAdapterError):
    pass


class ReutersValuesError(ReutersAdapterError):
    pass


class ReutersQualityError(ReutersAdapterError):
    pass


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    filename: str
    size: int
    sha256: str
    mtime: str
    export_time: str | None


@dataclass(frozen=True, slots=True)
class CbotDailyRecord:
    market_date: date
    contract_year: int
    contract_month: int
    price_cents_per_bushel: float
    price_name: str
    unit: str
    source_table: str
    source_column: str
    source_statement_index: int
    source_snapshot_sha256: str
    lead_months: int
    exchange_quality_status: str
    is_usable: bool
    eligible_for_import_profit: bool


@dataclass(frozen=True, slots=True)
class FxDailyRecord:
    market_date: date
    tenor_months: int
    fx_value: float
    unit: str
    source_table: str
    source_column: str
    source_statement_index: int
    source_snapshot_sha256: str
    quality_status: str
    is_usable: bool


@dataclass(frozen=True, slots=True)
class ReutersAdapterResult:
    source_identity: SourceIdentity
    cbot_records: tuple[CbotDailyRecord, ...]
    fx_records: tuple[FxDailyRecord, ...]
    manifest_core: dict[str, Any]
    quality_report: dict[str, Any]
    timings: dict[str, float]


def adapt_reuters_sql(source_sql: str | Path) -> ReutersAdapterResult:
    """Scan the dump exactly once for both approved Reuters target tables."""

    source_path = Path(source_sql)
    identity_started = perf_counter()
    source_identity = inspect_source_identity(source_path)
    identity_seconds = perf_counter() - identity_started
    ddl_columns: dict[str, tuple[str, ...]] = {}
    ddl_fingerprints: dict[str, dict[str, Any]] = {}
    create_counts = Counter()
    insert_counts = Counter()
    source_dates: dict[str, set[date]] = {table: set() for table in TARGET_TABLES}
    source_date_ranges: dict[str, list[date | None]] = {
        table: [None, None] for table in TARGET_TABLES
    }
    null_counts = Counter()
    issue_counts = Counter()
    issue_samples: dict[str, list[dict[str, Any]]] = {}
    cbot_records: list[CbotDailyRecord] = []
    fx_records: list[FxDailyRecord] = []
    cbot_keys: set[tuple[date, int, int]] = set()
    fx_keys: set[tuple[date, int]] = set()

    target_parse_seconds = 0.0
    scan_started = perf_counter()
    for statement in iter_sql_statements(source_path):
        table_name = statement.table_name
        if table_name not in TARGET_TABLES:
            continue
        target_started = perf_counter()
        if statement.statement_type is SqlStatementType.CREATE_TABLE:
            create_counts[table_name] += 1
            if create_counts[table_name] > 1:
                raise ReutersDdlError(
                    "duplicate target CREATE TABLE",
                    table_name=table_name,
                    statement_index=statement.statement_index,
                )
            columns = extract_create_columns(statement)
            _validate_target_ddl(table_name, columns, statement.statement_index)
            ddl_columns[table_name] = columns
            ddl_fingerprints[table_name] = _ddl_fingerprint(columns)
            target_parse_seconds += perf_counter() - target_started
            continue
        if statement.statement_type is not SqlStatementType.INSERT:
            target_parse_seconds += perf_counter() - target_started
            continue
        if table_name not in ddl_columns:
            raise ReutersDdlError(
                "target INSERT appeared before validated CREATE TABLE",
                table_name=table_name,
                statement_index=statement.statement_index,
            )

        insert_counts[table_name] += 1
        values = parse_insert_values(statement, ddl_columns[table_name])
        market_date = _parse_market_date(
            values[0],
            table_name=table_name,
            statement_index=statement.statement_index,
        )
        if market_date in source_dates[table_name]:
            raise ReutersQualityError(
                "duplicate source date",
                table_name=table_name,
                statement_index=statement.statement_index,
                sample=market_date.isoformat(),
            )
        source_dates[table_name].add(market_date)
        _update_date_range(source_date_ranges[table_name], market_date)

        if table_name == CBOT_TABLE:
            _normalize_cbot_row(
                statement,
                market_date,
                ddl_columns[table_name],
                values,
                source_identity,
                cbot_records,
                cbot_keys,
                null_counts,
                issue_counts,
                issue_samples,
            )
        else:
            _normalize_fx_row(
                statement,
                market_date,
                ddl_columns[table_name],
                values,
                source_identity,
                fx_records,
                fx_keys,
                null_counts,
                issue_counts,
                issue_samples,
            )
        target_parse_seconds += perf_counter() - target_started
    scan_seconds = perf_counter() - scan_started

    missing_tables = [table for table in TARGET_TABLES if create_counts[table] != 1]
    if missing_tables:
        raise ReutersDdlError(f"target tables are missing or duplicated: {missing_tables}")
    missing_insert_tables = [table for table in TARGET_TABLES if insert_counts[table] == 0]
    if missing_insert_tables:
        raise ReutersQualityError(f"target tables contain no INSERT rows: {missing_insert_tables}")

    cbot_records.sort(
        key=lambda record: (record.market_date, record.contract_year, record.contract_month)
    )
    fx_records.sort(key=lambda record: (record.market_date, record.tenor_months))
    quality_report = _build_quality_report(
        create_counts=create_counts,
        insert_counts=insert_counts,
        source_dates=source_dates,
        source_date_ranges=source_date_ranges,
        cbot_records=cbot_records,
        fx_records=fx_records,
        null_counts=null_counts,
        issue_counts=issue_counts,
        issue_samples=issue_samples,
    )
    manifest_core = {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "parser_version": PARSER_VERSION,
        "source_filename": source_identity.filename,
        "source_size": source_identity.size,
        "source_sha256": source_identity.sha256,
        "source_mtime": source_identity.mtime,
        "source_export_time": source_identity.export_time,
        "target_tables": list(TARGET_TABLES),
        "ddl_fingerprints": ddl_fingerprints,
        "source_table_rows": {table: insert_counts[table] for table in TARGET_TABLES},
        "source_table_date_ranges": {
            table: _date_range_dict(source_date_ranges[table]) for table in TARGET_TABLES
        },
        "standardized_records": {
            CBOT_TABLE: len(cbot_records),
            FX_TABLE: len(fx_records),
        },
        "usable_records": {
            CBOT_TABLE: sum(record.is_usable for record in cbot_records),
            FX_TABLE: sum(record.is_usable for record in fx_records),
        },
        "unusable_records": {
            CBOT_TABLE: sum(not record.is_usable for record in cbot_records),
            FX_TABLE: sum(not record.is_usable for record in fx_records),
        },
        "null_source_values": {
            CBOT_TABLE: null_counts[f"{CBOT_TABLE}:null"],
            FX_TABLE: null_counts[f"{FX_TABLE}:null"],
        },
        "duplicate_standard_keys": {
            CBOT_TABLE: 0,
            FX_TABLE: 0,
        },
        "quality_issue_counts": dict(sorted(issue_counts.items())),
        "candidate_status": quality_report["candidate_status"],
        "fatal_issue_count": quality_report["fatal_issue_count"],
        "warning_count": quality_report["warning_count"],
        "cbot_total_records": quality_report["cbot_total_records"],
        "cbot_is_usable_records": quality_report["cbot_is_usable_records"],
        "cbot_import_profit_eligible_records": quality_report[
            "cbot_import_profit_eligible_records"
        ],
        "cbot_quality_status_counts": quality_report["cbot_quality_status_counts"],
        "cbot_lead_month_buckets": quality_report["cbot_lead_month_buckets"],
        "known_extreme_anomaly_count": quality_report[
            "known_extreme_anomaly_count"
        ],
        "unknown_extreme_anomaly_count": quality_report[
            "unknown_extreme_anomaly_count"
        ],
        "fx_total_records": len(fx_records),
        "source_table_counts": {
            table: insert_counts[table] for table in TARGET_TABLES
        },
        "source_date_ranges": {
            table: _date_range_dict(source_date_ranges[table])
            for table in TARGET_TABLES
        },
        "standardized_date_ranges": quality_report["standardized_date_ranges"],
    }
    return ReutersAdapterResult(
        source_identity=source_identity,
        cbot_records=tuple(cbot_records),
        fx_records=tuple(fx_records),
        manifest_core=manifest_core,
        quality_report=quality_report,
        timings={
            "source_identity_seconds": identity_seconds,
            "sql_scan_seconds": scan_seconds,
            "target_parse_seconds": target_parse_seconds,
        },
    )


def inspect_source_identity(path: str | Path) -> SourceIdentity:
    source = Path(path)
    if not source.is_file():
        raise ReutersAdapterError("source SQL is not a readable regular file")
    digest = hashlib.sha256()
    try:
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        with source.open("r", encoding="utf-8", errors="strict", newline="") as stream:
            header = stream.read(32768)
    except (OSError, UnicodeError) as exc:
        raise ReutersAdapterError(f"source SQL identity read failed: {type(exc).__name__}") from exc
    export_match = re.search(
        r"^Date:\s*(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})\s*$",
        header,
        flags=re.MULTILINE,
    )
    item = source.stat()
    return SourceIdentity(
        filename=source.name,
        size=item.st_size,
        sha256=digest.hexdigest().upper(),
        mtime=datetime.fromtimestamp(item.st_mtime, tz=timezone.utc).isoformat(),
        export_time=export_match.group(1) if export_match else None,
    )


def extract_create_columns(statement: SqlStatement) -> tuple[str, ...]:
    if statement.statement_type is not SqlStatementType.CREATE_TABLE:
        raise ReutersDdlError(
            "statement is not CREATE TABLE",
            table_name=statement.table_name,
            statement_index=statement.statement_index,
        )
    bounds = _outer_parentheses(statement.sql)
    if bounds is None:
        raise ReutersDdlError(
            "CREATE TABLE has no balanced definition parentheses",
            table_name=statement.table_name,
            statement_index=statement.statement_index,
        )
    start, end = bounds
    definitions = _split_top_level(statement.sql[start + 1 : end], ",")
    columns: list[str] = []
    constraint_prefixes = (
        "PRIMARY ",
        "UNIQUE ",
        "KEY ",
        "INDEX ",
        "CONSTRAINT ",
        "FOREIGN ",
        "CHECK ",
        "FULLTEXT ",
        "SPATIAL ",
    )
    for definition in definitions:
        stripped = definition.strip()
        if not stripped or stripped.upper().startswith(constraint_prefixes):
            continue
        parsed = _parse_identifier_at(stripped, 0)
        if parsed is None:
            raise ReutersDdlError(
                "unable to parse target DDL field",
                table_name=statement.table_name,
                statement_index=statement.statement_index,
                sample=stripped[:80],
            )
        column, _ = parsed
        columns.append(column)
    return tuple(columns)


def parse_insert_values(
    statement: SqlStatement,
    ddl_columns: tuple[str, ...],
) -> tuple[Any, ...]:
    if statement.statement_type is not SqlStatementType.INSERT:
        raise ReutersValuesError(
            "statement is not INSERT",
            table_name=statement.table_name,
            statement_index=statement.statement_index,
        )
    match = re.match(r"\s*INSERT\s+INTO\s+", statement.sql, flags=re.IGNORECASE)
    if match is None:
        raise ReutersValuesError(
            "INSERT prefix is invalid",
            table_name=statement.table_name,
            statement_index=statement.statement_index,
        )
    table_end = _consume_qualified_identifier(statement.sql, match.end())
    if table_end is None:
        raise ReutersValuesError(
            "INSERT target identifier is invalid",
            table_name=statement.table_name,
            statement_index=statement.statement_index,
        )
    values_position = _find_keyword_outside(statement.sql, "VALUES", table_end)
    if values_position is None:
        raise ReutersValuesError(
            "INSERT has no VALUES clause",
            table_name=statement.table_name,
            statement_index=statement.statement_index,
        )
    columns_text = statement.sql[table_end:values_position].strip()
    explicit_columns: tuple[str, ...] | None = None
    if columns_text:
        if not (columns_text.startswith("(") and columns_text.endswith(")")):
            raise ReutersValuesError(
                "explicit INSERT fields are malformed",
                table_name=statement.table_name,
                statement_index=statement.statement_index,
            )
        parsed_columns = []
        for item in _split_top_level(columns_text[1:-1], ","):
            parsed = _parse_identifier_at(item.strip(), 0)
            if parsed is None or item.strip()[parsed[1] :].strip():
                raise ReutersValuesError(
                    "explicit INSERT field is invalid",
                    table_name=statement.table_name,
                    statement_index=statement.statement_index,
                    sample=item,
                )
            parsed_columns.append(parsed[0])
        explicit_columns = tuple(parsed_columns)
        if len(explicit_columns) != len(set(explicit_columns)):
            raise ReutersValuesError(
                "explicit INSERT fields contain duplicates",
                table_name=statement.table_name,
                statement_index=statement.statement_index,
            )
        unknown = set(explicit_columns) - set(ddl_columns)
        if unknown:
            raise ReutersValuesError(
                f"explicit INSERT fields are unknown: {sorted(unknown)}",
                table_name=statement.table_name,
                statement_index=statement.statement_index,
            )

    after_values = values_position + len("VALUES")
    tuple_bounds = _parentheses_at(statement.sql, after_values)
    if tuple_bounds is None:
        raise ReutersValuesError(
            "VALUES must contain one balanced tuple",
            table_name=statement.table_name,
            statement_index=statement.statement_index,
        )
    start, end = tuple_bounds
    remainder = statement.sql[end + 1 :].strip()
    if remainder == ";":
        remainder = ""
    if remainder:
        raise ReutersValuesError(
            "multi-row VALUES or trailing SQL is not supported",
            table_name=statement.table_name,
            statement_index=statement.statement_index,
            sample=remainder[:80],
        )
    raw_values = _split_top_level(statement.sql[start + 1 : end], ",")
    parsed_values = tuple(_parse_mysql_literal(value.strip(), statement) for value in raw_values)

    target_columns = explicit_columns or ddl_columns
    if len(parsed_values) != len(target_columns):
        raise ReutersValuesError(
            f"INSERT value count {len(parsed_values)} does not match field count {len(target_columns)}",
            table_name=statement.table_name,
            statement_index=statement.statement_index,
        )
    if explicit_columns is None:
        return parsed_values
    by_column = dict(zip(explicit_columns, parsed_values, strict=True))
    return tuple(by_column.get(column) for column in ddl_columns)


def _validate_target_ddl(
    table_name: str,
    columns: tuple[str, ...],
    statement_index: int,
) -> None:
    expected = CBOT_COLUMNS if table_name == CBOT_TABLE else FX_COLUMNS
    if columns != expected:
        mismatch = next(
            (
                index
                for index, pair in enumerate(zip(columns, expected, strict=False))
                if pair[0] != pair[1]
            ),
            min(len(columns), len(expected)),
        )
        raise ReutersDdlError(
            f"DDL structure fingerprint mismatch at field {mismatch}; "
            f"expected {len(expected)} fields, got {len(columns)}",
            table_name=table_name,
            statement_index=statement_index,
        )


def _normalize_cbot_row(
    statement: SqlStatement,
    market_date: date,
    columns: tuple[str, ...],
    values: tuple[Any, ...],
    source: SourceIdentity,
    records: list[CbotDailyRecord],
    keys: set[tuple[date, int, int]],
    null_counts: Counter[str],
    issue_counts: Counter[str],
    issue_samples: dict[str, list[dict[str, Any]]],
) -> None:
    for column, raw_value in zip(columns[1:], values[1:], strict=True):
        year, month = _parse_cbot_column(column)
        if raw_value is None:
            null_counts[f"{CBOT_TABLE}:null"] += 1
            continue
        price = _parse_finite_number(
            raw_value,
            table_name=CBOT_TABLE,
            statement_index=statement.statement_index,
            field_name=column,
        )
        key = (market_date, year, month)
        if key in keys:
            raise ReutersQualityError(
                "duplicate CBOT standard key",
                table_name=CBOT_TABLE,
                statement_index=statement.statement_index,
                sample=f"{market_date.isoformat()} {year}-{month:02d}",
            )
        keys.add(key)
        lead_months = calculate_lead_months(market_date, year, month)
        if price == 0:
            exchange_quality_status = "zero_price"
            is_usable = False
            eligible_for_import_profit = False
            issue_counts[f"{CBOT_TABLE}:{exchange_quality_status}"] += 1
            _add_sample(
                issue_samples,
                f"{CBOT_TABLE}:{exchange_quality_status}",
                {"market_date": market_date.isoformat(), "contract_year": year, "contract_month": month},
            )
        elif price < 0:
            exchange_quality_status = "negative_price"
            is_usable = False
            eligible_for_import_profit = False
            issue_counts[f"{CBOT_TABLE}:{exchange_quality_status}"] += 1
            _add_sample(
                issue_samples,
                f"{CBOT_TABLE}:{exchange_quality_status}",
                {"market_date": market_date.isoformat(), "contract_year": year, "contract_month": month},
            )
        else:
            (
                exchange_quality_status,
                is_usable,
                eligible_for_import_profit,
            ) = classify_cbot_quality(lead_months)
            if exchange_quality_status != "standard_window":
                issue_key = f"{CBOT_TABLE}:{exchange_quality_status}"
                issue_counts[issue_key] += 1
                _add_sample(
                    issue_samples,
                    issue_key,
                    {
                        "market_date": market_date.isoformat(),
                        "contract_year": year,
                        "contract_month": month,
                        "price": price,
                        "lead_months": lead_months,
                    },
                )
        records.append(
            CbotDailyRecord(
                market_date=market_date,
                contract_year=year,
                contract_month=month,
                price_cents_per_bushel=price,
                price_name="CBOT日度价格",
                unit="cents_per_bushel",
                source_table=CBOT_TABLE,
                source_column=column,
                source_statement_index=statement.statement_index,
                source_snapshot_sha256=source.sha256,
                lead_months=lead_months,
                exchange_quality_status=exchange_quality_status,
                is_usable=is_usable,
                eligible_for_import_profit=eligible_for_import_profit,
            )
        )


def calculate_lead_months(
    market_date: date,
    contract_year: int,
    contract_month: int,
) -> int:
    """Return the exact integer month lead from market month to contract month."""

    return (
        (contract_year - market_date.year) * 12
        + contract_month
        - market_date.month
    )


def classify_cbot_quality(lead_months: int) -> tuple[str, bool, bool]:
    """Classify a positive finite CBOT price by its exact contract lead."""

    if lead_months < 0:
        return "invalid_negative_lead", False, False
    if lead_months <= 42:
        return "standard_window", True, lead_months <= 12
    if lead_months <= 48:
        return "extended_window_unverified", True, False
    if lead_months <= 299:
        return "extreme_window_warning", False, False
    return "extreme_source_anomaly", False, False


def _normalize_fx_row(
    statement: SqlStatement,
    market_date: date,
    columns: tuple[str, ...],
    values: tuple[Any, ...],
    source: SourceIdentity,
    records: list[FxDailyRecord],
    keys: set[tuple[date, int]],
    null_counts: Counter[str],
    issue_counts: Counter[str],
    issue_samples: dict[str, list[dict[str, Any]]],
) -> None:
    for column, raw_value in zip(columns[1:], values[1:], strict=True):
        tenor = FX_TENORS[column]
        if raw_value is None:
            null_counts[f"{FX_TABLE}:null"] += 1
            continue
        fx_value = _parse_finite_number(
            raw_value,
            table_name=FX_TABLE,
            statement_index=statement.statement_index,
            field_name=column,
        )
        key = (market_date, tenor)
        if key in keys:
            raise ReutersQualityError(
                "duplicate FX standard key",
                table_name=FX_TABLE,
                statement_index=statement.statement_index,
                sample=f"{market_date.isoformat()} tenor={tenor}",
            )
        keys.add(key)
        if fx_value == 0:
            quality_status = "zero_fx"
            is_usable = False
            issue_counts[f"{FX_TABLE}:{quality_status}"] += 1
            _add_sample(
                issue_samples,
                f"{FX_TABLE}:{quality_status}",
                {"market_date": market_date.isoformat(), "tenor_months": tenor},
            )
        elif fx_value < 0:
            quality_status = "negative_fx"
            is_usable = False
            issue_counts[f"{FX_TABLE}:{quality_status}"] += 1
            _add_sample(
                issue_samples,
                f"{FX_TABLE}:{quality_status}",
                {"market_date": market_date.isoformat(), "tenor_months": tenor},
            )
        else:
            quality_status = "valid"
            is_usable = True
        records.append(
            FxDailyRecord(
                market_date=market_date,
                tenor_months=tenor,
                fx_value=fx_value,
                unit="cnh_per_usd",
                source_table=FX_TABLE,
                source_column=column,
                source_statement_index=statement.statement_index,
                source_snapshot_sha256=source.sha256,
                quality_status=quality_status,
                is_usable=is_usable,
            )
        )


def _build_quality_report(
    *,
    create_counts: Counter[str],
    insert_counts: Counter[str],
    source_dates: dict[str, set[date]],
    source_date_ranges: dict[str, list[date | None]],
    cbot_records: list[CbotDailyRecord],
    fx_records: list[FxDailyRecord],
    null_counts: Counter[str],
    issue_counts: Counter[str],
    issue_samples: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    cbot_month_coverage = {
        str(month): sum(
            record.is_usable and record.contract_month == month for record in cbot_records
        )
        for month in CBOT_MONTHS
    }
    fx_tenor_coverage = {
        str(tenor): sum(record.is_usable and record.tenor_months == tenor for record in fx_records)
        for tenor in range(13)
    }
    cbot_zero = issue_counts[f"{CBOT_TABLE}:zero_price"]
    cbot_negative = issue_counts[f"{CBOT_TABLE}:negative_price"]
    fx_zero = issue_counts[f"{FX_TABLE}:zero_fx"]
    fx_negative = issue_counts[f"{FX_TABLE}:negative_fx"]
    status_counts = Counter(record.exchange_quality_status for record in cbot_records)
    lead_distribution = Counter(record.lead_months for record in cbot_records)
    lead_buckets = {
        "0_12": sum(0 <= record.lead_months <= 12 for record in cbot_records),
        "13_42": sum(13 <= record.lead_months <= 42 for record in cbot_records),
        "43_48": sum(43 <= record.lead_months <= 48 for record in cbot_records),
        "49_299": sum(49 <= record.lead_months <= 299 for record in cbot_records),
        "gte_300": sum(record.lead_months >= 300 for record in cbot_records),
        "negative": sum(record.lead_months < 0 for record in cbot_records),
    }
    extreme_records = [
        record
        for record in cbot_records
        if record.exchange_quality_status == "extreme_source_anomaly"
    ]
    known_extremes = [record for record in extreme_records if _is_known_extreme(record)]
    unknown_extremes = [
        record for record in extreme_records if not _is_known_extreme(record)
    ]
    eligible_integrity_errors = sum(
        record.eligible_for_import_profit
        and (
            not record.is_usable
            or not 0 <= record.lead_months <= 12
            or record.price_cents_per_bushel <= 0
            or not isfinite(record.price_cents_per_bushel)
        )
        for record in cbot_records
    )
    fatal_counts = {
        "cbot_zero_price": cbot_zero,
        "cbot_negative_price": cbot_negative,
        "fx_zero_value": fx_zero,
        "fx_negative_value": fx_negative,
        "eligible_for_import_profit_integrity": eligible_integrity_errors,
        "unknown_extreme_source_anomaly": len(unknown_extremes),
    }
    fatal_issues = [
        {"code": code, "count": count}
        for code, count in fatal_counts.items()
        if count
    ]
    warnings = []
    warning_specs = (
        ("extended_window_unverified", status_counts["extended_window_unverified"]),
        ("extreme_window_warning", status_counts["extreme_window_warning"]),
        ("known_extreme_source_anomaly", len(known_extremes)),
        ("invalid_negative_lead", status_counts["invalid_negative_lead"]),
    )
    for code, count in warning_specs:
        if count:
            warnings.append(
                {
                    "code": code,
                    "count": count,
                    "samples": _quality_samples_for_code(
                        code,
                        cbot_records,
                        known_extremes=known_extremes,
                    ),
                }
            )
    fatal_issue_count = sum(item["count"] for item in fatal_issues)
    warning_count = sum(item["count"] for item in warnings)
    candidate_status = (
        "failed"
        if fatal_issue_count
        else "passed_with_warnings"
        if warning_count
        else "passed"
    )
    fatal_checks = {
        "target_tables_present_once": {
            "passed": all(create_counts[table] == 1 for table in TARGET_TABLES),
            "count": sum(create_counts[table] != 1 for table in TARGET_TABLES),
        },
        "target_tables_have_rows": {
            "passed": all(insert_counts[table] > 0 for table in TARGET_TABLES),
            "count": sum(insert_counts[table] == 0 for table in TARGET_TABLES),
        },
        "ddl_structure_matches": {"passed": True, "count": 0},
        "row_field_counts_match": {"passed": True, "count": 0},
        "values_parse": {"passed": True, "count": 0},
        "dates_parse": {"passed": True, "count": 0},
        "numbers_finite": {"passed": True, "count": 0},
        "duplicate_source_dates": {"passed": True, "count": 0},
        "duplicate_standard_keys": {"passed": True, "count": 0},
        **{
            code: {"passed": count == 0, "count": count}
            for code, count in fatal_counts.items()
        },
    }
    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "target_tables": {
            table: {
                "exists": create_counts[table] == 1,
                "ddl_matches": create_counts[table] == 1,
                "create_count": create_counts[table],
                "insert_count": insert_counts[table],
                "source_rows": len(source_dates[table]),
                "source_date_range": _date_range_dict(source_date_ranges[table]),
                "row_field_count_errors": 0,
                "unparseable_dates": 0,
                "unparseable_numbers": 0,
                "zero_values": issue_counts[
                    f"{table}:{'zero_price' if table == CBOT_TABLE else 'zero_fx'}"
                ],
                "negative_values": issue_counts[
                    f"{table}:{'negative_price' if table == CBOT_TABLE else 'negative_fx'}"
                ],
                "duplicate_source_dates": 0,
                "duplicate_standard_keys": 0,
                "null_source_values": null_counts[f"{table}:null"],
            }
            for table in TARGET_TABLES
        },
        "cbot_contract_month_usable_coverage": cbot_month_coverage,
        "fx_tenor_usable_coverage": fx_tenor_coverage,
        "standardized_date_ranges": {
            CBOT_TABLE: _record_date_range(cbot_records),
            FX_TABLE: _record_date_range(fx_records),
        },
        "quality_issue_counts": dict(sorted(issue_counts.items())),
        "quality_issue_samples": {
            key: samples[:MAX_QUALITY_SAMPLES] for key, samples in sorted(issue_samples.items())
        },
        "candidate_status": candidate_status,
        "candidate_pass": candidate_status != "failed",
        "fatal_issue_count": fatal_issue_count,
        "warning_count": warning_count,
        "fatal_issues": fatal_issues,
        "warnings": warnings,
        "fatal_checks": fatal_checks,
        "informational_counts": {
            "cbot_null_source_values": null_counts[f"{CBOT_TABLE}:null"],
            "fx_null_source_values": null_counts[f"{FX_TABLE}:null"],
            "cbot_unusable_records": sum(
                not record.is_usable for record in cbot_records
            ),
        },
        "cbot_total_records": len(cbot_records),
        "cbot_is_usable_records": sum(record.is_usable for record in cbot_records),
        "cbot_import_profit_eligible_records": sum(
            record.eligible_for_import_profit for record in cbot_records
        ),
        "cbot_quality_status_counts": dict(sorted(status_counts.items())),
        "cbot_lead_month_distribution": {
            str(key): value for key, value in sorted(lead_distribution.items())
        },
        "cbot_lead_month_buckets": lead_buckets,
        "known_extreme_anomaly_count": len(known_extremes),
        "unknown_extreme_anomaly_count": len(unknown_extremes),
        "known_extreme_anomaly_samples": [
            _cbot_quality_sample(record) for record in known_extremes[:MAX_QUALITY_SAMPLES]
        ],
        "extreme_window_warning_samples": [
            _cbot_quality_sample(record)
            for record in cbot_records
            if record.exchange_quality_status == "extreme_window_warning"
        ][:MAX_QUALITY_SAMPLES],
        "interpolation_performed": False,
    }


def _is_known_extreme(record: CbotDailyRecord) -> bool:
    return (
        record.market_date == KNOWN_EXTREME_ANOMALY["market_date"]
        and record.contract_year == KNOWN_EXTREME_ANOMALY["contract_year"]
        and record.contract_month == KNOWN_EXTREME_ANOMALY["contract_month"]
        and record.price_cents_per_bushel == KNOWN_EXTREME_ANOMALY["price"]
        and record.lead_months == KNOWN_EXTREME_ANOMALY["lead_months"]
    )


def _cbot_quality_sample(record: CbotDailyRecord) -> dict[str, Any]:
    return {
        "market_date": record.market_date.isoformat(),
        "contract_year": record.contract_year,
        "contract_month": record.contract_month,
        "price": record.price_cents_per_bushel,
        "lead_months": record.lead_months,
        "source_column": record.source_column,
        "source_statement_index": record.source_statement_index,
    }


def _quality_samples_for_code(
    code: str,
    cbot_records: list[CbotDailyRecord],
    *,
    known_extremes: list[CbotDailyRecord],
) -> list[dict[str, Any]]:
    if code == "known_extreme_source_anomaly":
        records = known_extremes
    else:
        records = [
            record
            for record in cbot_records
            if record.exchange_quality_status == code
        ]
    return [
        _cbot_quality_sample(record) for record in records[:MAX_QUALITY_SAMPLES]
    ]


def _parse_mysql_literal(value: str, statement: SqlStatement) -> Any:
    if value.upper() == "NULL":
        return None
    if len(value) >= 2 and value[0] in {"'", '"'}:
        quote = value[0]
        if value[-1] != quote:
            raise ReutersValuesError(
                "unclosed quoted SQL literal",
                table_name=statement.table_name,
                statement_index=statement.statement_index,
            )
        output: list[str] = []
        index = 1
        while index < len(value) - 1:
            char = value[index]
            if char == "\\":
                index += 1
                if index >= len(value) - 1:
                    raise ReutersValuesError(
                        "trailing backslash in SQL literal",
                        table_name=statement.table_name,
                        statement_index=statement.statement_index,
                    )
                escaped = value[index]
                output.append(
                    {
                        "0": "\0",
                        "b": "\b",
                        "n": "\n",
                        "r": "\r",
                        "t": "\t",
                        "Z": "\x1a",
                    }.get(escaped, escaped)
                )
                index += 1
                continue
            if char == quote and index + 1 < len(value) - 1 and value[index + 1] == quote:
                output.append(quote)
                index += 2
                continue
            if char == quote:
                raise ReutersValuesError(
                    "unexpected quote inside SQL literal",
                    table_name=statement.table_name,
                    statement_index=statement.statement_index,
                )
            output.append(char)
            index += 1
        return "".join(output)
    if re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
        number = float(value)
        if isfinite(number):
            return number
    raise ReutersValuesError(
        "unsupported SQL literal",
        table_name=statement.table_name,
        statement_index=statement.statement_index,
        sample=value[:80],
    )


def _parse_market_date(value: Any, *, table_name: str, statement_index: int) -> date:
    if not isinstance(value, str):
        raise ReutersQualityError(
            "Date value must be a string",
            table_name=table_name,
            statement_index=statement_index,
        )
    try:
        parsed = datetime.fromisoformat(value).date()
    except ValueError as exc:
        raise ReutersQualityError(
            "Date value is invalid",
            table_name=table_name,
            statement_index=statement_index,
            sample=value,
        ) from exc
    return parsed


def _parse_finite_number(
    value: Any,
    *,
    table_name: str,
    statement_index: int,
    field_name: str,
) -> float:
    if isinstance(value, bool):
        raise ReutersQualityError(
            "numeric value cannot be boolean",
            table_name=table_name,
            statement_index=statement_index,
            sample=field_name,
        )
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and re.fullmatch(
        r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?",
        value.strip(),
    ):
        number = float(value)
    else:
        raise ReutersQualityError(
            "numeric value is invalid",
            table_name=table_name,
            statement_index=statement_index,
            sample=field_name,
        )
    if not isfinite(number):
        raise ReutersQualityError(
            "numeric value must be finite",
            table_name=table_name,
            statement_index=statement_index,
            sample=field_name,
        )
    return number


def _parse_cbot_column(column: str) -> tuple[int, int]:
    match = re.fullmatch(r"CBOT大豆 (\d{4})年(\d{1,2})月", column)
    if match is None:
        raise ReutersDdlError("CBOT source column is invalid", table_name=CBOT_TABLE, sample=column)
    return int(match.group(1)), int(match.group(2))


def _ddl_fingerprint(columns: tuple[str, ...]) -> dict[str, Any]:
    serialized = json.dumps(list(columns), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {
        "field_count": len(columns),
        "ordered_fields_sha256": hashlib.sha256(serialized).hexdigest().upper(),
        "first_field": columns[0],
        "last_field": columns[-1],
    }


def _outer_parentheses(value: str) -> tuple[int, int] | None:
    start = _find_outside_character(value, "(")
    if start is None:
        return None
    return _balanced_parentheses(value, start)


def _parentheses_at(value: str, position: int) -> tuple[int, int] | None:
    position = _skip_whitespace(value, position)
    if position >= len(value) or value[position] != "(":
        return None
    return _balanced_parentheses(value, position)


def _balanced_parentheses(value: str, start: int) -> tuple[int, int] | None:
    depth = 0
    quote: str | None = None
    index = start
    while index < len(value):
        char = value[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return start, index
        index += 1
    return None


def _split_top_level(value: str, delimiter: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ReutersValuesError("unbalanced parentheses")
        elif char == delimiter and depth == 0:
            parts.append(value[start:index])
            start = index + 1
        index += 1
    if quote is not None or depth != 0:
        raise ReutersValuesError("unbalanced quoted value or parentheses")
    parts.append(value[start:])
    return parts


def _find_keyword_outside(value: str, keyword: str, position: int) -> int | None:
    quote: str | None = None
    index = position
    upper_keyword = keyword.upper()
    while index < len(value):
        char = value[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if value[index : index + len(keyword)].upper() == upper_keyword:
            before_ok = index == 0 or not (value[index - 1].isalnum() or value[index - 1] == "_")
            end = index + len(keyword)
            after_ok = end == len(value) or not (value[end].isalnum() or value[end] == "_")
            if before_ok and after_ok:
                return index
        index += 1
    return None


def _find_outside_character(value: str, target: str) -> int | None:
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == target:
            return index
        index += 1
    return None


def _consume_qualified_identifier(value: str, position: int) -> int | None:
    first = _parse_identifier_at(value, position)
    if first is None:
        return None
    _, position = first
    position = _skip_whitespace(value, position)
    if position < len(value) and value[position] == ".":
        second = _parse_identifier_at(value, position + 1)
        if second is None:
            return None
        _, position = second
    return position


def _parse_identifier_at(value: str, position: int) -> tuple[str, int] | None:
    position = _skip_whitespace(value, position)
    if position >= len(value):
        return None
    if value[position] == "`":
        output: list[str] = []
        index = position + 1
        while index < len(value):
            char = value[index]
            if char == "\\" and index + 1 < len(value):
                output.append(value[index + 1])
                index += 2
                continue
            if char == "`":
                if index + 1 < len(value) and value[index + 1] == "`":
                    output.append("`")
                    index += 2
                    continue
                return "".join(output), index + 1
            output.append(char)
            index += 1
        return None
    match = re.match(r"[\w$]+", value[position:], flags=re.UNICODE)
    if match is None:
        return None
    return match.group(0), position + match.end()


def _skip_whitespace(value: str, position: int) -> int:
    while position < len(value) and value[position].isspace():
        position += 1
    return position


def _update_date_range(bounds: list[date | None], value: date) -> None:
    if bounds[0] is None or value < bounds[0]:
        bounds[0] = value
    if bounds[1] is None or value > bounds[1]:
        bounds[1] = value


def _date_range_dict(bounds: list[date | None]) -> dict[str, str | None]:
    return {
        "minimum": bounds[0].isoformat() if bounds[0] else None,
        "maximum": bounds[1].isoformat() if bounds[1] else None,
    }


def _record_date_range(records: Iterable[CbotDailyRecord | FxDailyRecord]) -> dict[str, str | None]:
    usable_dates = [record.market_date for record in records if record.is_usable]
    return {
        "minimum": min(usable_dates).isoformat() if usable_dates else None,
        "maximum": max(usable_dates).isoformat() if usable_dates else None,
    }


def _add_sample(
    samples: dict[str, list[dict[str, Any]]],
    key: str,
    value: dict[str, Any],
) -> None:
    bucket = samples.setdefault(key, [])
    if len(bucket) < MAX_QUALITY_SAMPLES:
        bucket.append(value)


def records_as_dicts(
    records: Iterable[CbotDailyRecord | FxDailyRecord],
) -> list[dict[str, Any]]:
    return [asdict(record) for record in records]
