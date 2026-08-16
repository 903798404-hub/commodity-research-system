"""Read-only adapter from the sealed Lutou snapshot to canonical source records."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from agri_research_agent.data_sources.navicat_sql_stream import (
    SqlStatement,
    SqlStatementType,
)
from agri_research_agent.import_profit.reuters_adapter import (
    extract_create_columns,
    parse_insert_values,
)
from agri_research_agent.research_data.three_oil_v1 import ThreeOilV1Catalog


_CREATE = re.compile(r"^\s*CREATE\s+TABLE\s+`([^`]+)`", re.IGNORECASE)
_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+`([^`]+)`", re.IGNORECASE)


class ThreeOilSnapshotError(RuntimeError):
    """Raised when the sealed read-only snapshot cannot produce canonical records."""


def load_three_oil_snapshot_records(
    catalog: ThreeOilV1Catalog,
    source_path: str | Path,
    *,
    series_ids: Iterable[str] | None = None,
) -> Mapping[str, tuple[Mapping[str, object], ...]]:
    """Read source-native fields once and return records keyed by canonical Series ID.

    The adapter validates the complete file hash against the sealed manifest before
    returning any observations. It never writes data or executes SQL.
    """

    source = Path(source_path)
    if not source.is_file():
        raise ThreeOilSnapshotError("approved reference snapshot is unavailable")

    requested = (
        tuple(item.series_id for item in catalog.series)
        if series_ids is None
        else tuple(series_ids)
    )
    if not requested or len(requested) != len(set(requested)):
        raise ThreeOilSnapshotError("requested canonical Series must be unique")
    try:
        contracts = tuple(catalog.series_by_id(series_id) for series_id in requested)
    except KeyError as exc:
        raise ThreeOilSnapshotError("requested canonical Series is unknown") from exc

    by_native_key = {
        (item.source_native_table, item.source_native_series): item
        for item in contracts
    }
    if len(by_native_key) != len(contracts):
        raise ThreeOilSnapshotError("source-native Series mapping is ambiguous")
    target_tables = {item.source_native_table for item in contracts}
    target_fields: dict[str, set[str]] = defaultdict(set)
    for item in contracts:
        target_fields[item.source_native_table].add(item.source_native_series)

    columns_by_table: dict[str, tuple[str, ...]] = {}
    records: dict[str, list[Mapping[str, object]]] = {
        item.series_id: [] for item in contracts
    }
    digest = hashlib.sha256()
    active_table: str | None = None
    active_start_line = 0
    active_lines: list[str] = []
    statement_index = 0

    try:
        with source.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                digest.update(raw_line)
                line = raw_line.decode("utf-8", errors="strict")

                if active_table is not None:
                    active_lines.append(line)
                    if line.rstrip().endswith(";"):
                        statement_index += 1
                        statement = SqlStatement(
                            sql="".join(active_lines),
                            statement_type=SqlStatementType.CREATE_TABLE,
                            table_name=active_table,
                            start_line=active_start_line,
                            end_line=line_number,
                            statement_index=statement_index,
                        )
                        columns_by_table[active_table] = extract_create_columns(
                            statement
                        )
                        active_table = None
                        active_lines = []
                    continue

                create_match = _CREATE.match(line)
                if create_match and create_match.group(1) in target_tables:
                    active_table = create_match.group(1)
                    active_start_line = line_number
                    active_lines = [line]
                    if line.rstrip().endswith(";"):
                        statement_index += 1
                        statement = SqlStatement(
                            sql=line,
                            statement_type=SqlStatementType.CREATE_TABLE,
                            table_name=active_table,
                            start_line=line_number,
                            end_line=line_number,
                            statement_index=statement_index,
                        )
                        columns_by_table[active_table] = extract_create_columns(
                            statement
                        )
                        active_table = None
                        active_lines = []
                    continue

                insert_match = _INSERT.match(line)
                if not insert_match or insert_match.group(1) not in target_tables:
                    continue
                table = insert_match.group(1)
                columns = columns_by_table.get(table)
                if columns is None:
                    raise ThreeOilSnapshotError(
                        f"target INSERT appeared before CREATE TABLE: {table}"
                    )
                if not line.rstrip().endswith(";"):
                    raise ThreeOilSnapshotError(
                        f"multi-line target INSERT is not supported: {table}"
                    )
                statement_index += 1
                statement = SqlStatement(
                    sql=line,
                    statement_type=SqlStatementType.INSERT,
                    table_name=table,
                    start_line=line_number,
                    end_line=line_number,
                    statement_index=statement_index,
                )
                values = parse_insert_values(statement, columns)
                row = dict(zip(columns, values, strict=True))
                business_date = _business_date(row.get("Date"), table)
                for field in target_fields[table]:
                    raw_price = row.get(field)
                    if raw_price is None:
                        continue
                    contract = by_native_key[(table, field)]
                    records[contract.series_id].append(
                        MappingProxyType(
                            {
                                "provider_series_id": contract.provider_series_id,
                                "business_date": business_date,
                                "price": _price(raw_price, table, field),
                            }
                        )
                    )
    except UnicodeDecodeError as exc:
        raise ThreeOilSnapshotError("reference snapshot is not valid UTF-8") from exc
    except OSError as exc:
        raise ThreeOilSnapshotError(
            f"reference snapshot read failed: {type(exc).__name__}"
        ) from exc

    if active_table is not None:
        raise ThreeOilSnapshotError(f"unclosed target CREATE TABLE: {active_table}")
    actual_sha = digest.hexdigest()
    if actual_sha != catalog.source_snapshot_sha256.lower():
        raise ThreeOilSnapshotError("reference snapshot hash does not match sealed contract")
    missing_tables = target_tables - set(columns_by_table)
    if missing_tables:
        raise ThreeOilSnapshotError(
            f"reference snapshot is missing target tables: {sorted(missing_tables)}"
        )
    missing_fields = {
        (table, field)
        for table, fields in target_fields.items()
        for field in fields
        if field not in columns_by_table[table]
    }
    if missing_fields:
        raise ThreeOilSnapshotError("reference snapshot is missing sealed fields")
    if any(not items for items in records.values()):
        raise ThreeOilSnapshotError("reference snapshot contains an empty sealed Series")
    return MappingProxyType(
        {series_id: tuple(items) for series_id, items in records.items()}
    )


def _business_date(value: object, table: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise ThreeOilSnapshotError(f"invalid business date in {table}") from exc


def _price(value: object, table: str, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ThreeOilSnapshotError(
            f"invalid target price in {table}.{field}"
        ) from exc
    if not result.is_finite():
        raise ThreeOilSnapshotError(f"non-finite target price in {table}.{field}")
    return result
