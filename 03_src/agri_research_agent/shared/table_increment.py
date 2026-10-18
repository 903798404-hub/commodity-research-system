"""Scoped table transport for independently validated Parquet consumers.

Only the collector and pinned offline image use Arrow. Protected host code must
never import this module or trust an unvalidated worker's change flag. Existing
formal Parquet files and their historical versions remain separate consumers.
"""
from __future__ import annotations

import base64
from datetime import date, datetime
import hashlib
import json
import math
import re

import pandas as pd
import pyarrow as pa

SCHEMA = "agri-table-increment/1"
MAX_BYTES = 64 * 1024 * 1024
MAX_ROWS = 1_000_000
FIELDS = {"schema_version", "baseline_sha256", "schema", "key_columns",
          "scope_column", "scope_values", "refresh_columns", "refresh",
          "updates", "removals", "row_count", "parquet_schema_sha256"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def equivalent(left, right, key_columns):
    """Compare exact serialized values and schema, including null positions.

    Object-column None and pandas.NA are the same Arrow null; neither is an
    empty string or zero. This is also the existing Parquet storage boundary.
    """
    def table(frame):
        ordered = frame.sort_values(list(key_columns), kind="stable").reset_index(drop=True)
        return pa.Table.from_pandas(ordered, preserve_index=False)
    return table(left).equals(table(right), check_metadata=True)


def _token(value):
    require(not pd.isna(value), "null table key or scope")
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    require(type(value) in {str, int, float, bool}, "unsupported key or scope scalar")
    require(not isinstance(value, float) or math.isfinite(value), "non-finite key")
    return value


def _equal(left, right):
    if pd.isna(left) or pd.isna(right):
        return bool(pd.isna(left) and pd.isna(right))
    return bool(left == right)


def _rows(frame, keys):
    require(len(frame) <= MAX_ROWS, "table row limit exceeded")
    require(frame.columns.is_unique and not frame.duplicated(list(keys)).any(),
            "duplicate table columns or keys")
    result = {}
    for row in frame.to_dict("records"):
        key = tuple(_token(row[column]) for column in keys)
        require(key not in result, "duplicate canonical table key")
        result[key] = {name: None if pd.isna(value) else value for name, value in row.items()}
    return result


def _validate_schema(schema):
    require(0 < len(schema) <= 128 and len(set(schema.names)) == len(schema),
            "invalid table schema")
    require(not schema.metadata or set(schema.metadata) <= {b"pandas"},
            "unexpected table schema metadata")
    for field in schema:
        kind = field.type
        require(not field.metadata and (
            pa.types.is_boolean(kind) or pa.types.is_integer(kind)
            or pa.types.is_floating(kind) or pa.types.is_string(kind)
            or pa.types.is_large_string(kind) or pa.types.is_date(kind)
            or pa.types.is_timestamp(kind) or pa.types.is_null(kind)),
            "unsupported table field type")


def _compatible_schema(candidate, baseline):
    # Pandas 3's inferred strings use Arrow large_string while mixed source
    # concatenation uses string. Both represent exactly the same text values.
    def text(kind):
        return pa.types.is_string(kind) or pa.types.is_large_string(kind)
    return (candidate.names == baseline.names and all(
        a.nullable == b.nullable and (a.type == b.type or (text(a.type) and text(b.type)))
        for a, b in zip(candidate, baseline)))


def _encode(table):
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    raw = sink.getvalue().to_pybytes()
    require(len(raw) <= MAX_BYTES, "table transport exceeds byte limit")
    return base64.b64encode(raw).decode("ascii")


def _decode(encoded):
    require(type(encoded) is str and len(encoded) <= (MAX_BYTES * 4 // 3 + 8),
            "invalid encoded table")
    raw = base64.b64decode(encoded, validate=True)
    require(len(raw) <= MAX_BYTES, "table transport exceeds byte limit")
    source = pa.BufferReader(raw)
    with pa.ipc.open_stream(source) as reader:
        _validate_schema(reader.schema)
        table = reader.read_all()
    require(source.tell() == len(raw) and table.num_rows <= MAX_ROWS,
            "trailing bytes or excessive table rows")
    return table


def _binding(baseline, baseline_sha256):
    require((baseline is None) == (baseline_sha256 is None), "baseline presence differs")
    require(baseline_sha256 is None or (
        type(baseline_sha256) is str and re.fullmatch(r"[0-9a-f]{64}", baseline_sha256)),
        "invalid baseline identity")


def _contract(columns, key_columns, scope_column, scope_values, refresh_columns):
    require(type(key_columns) in {tuple, list} and key_columns
            and len(set(key_columns)) == len(key_columns), "invalid table keys")
    require(set(key_columns) <= set(columns) and scope_column in columns,
            "missing table key or scope columns")
    require(type(scope_values) in {tuple, list} and scope_values
            and len(set(scope_values)) == len(scope_values)
            and all(type(value) in {str, int} for value in scope_values), "invalid refresh scope")
    require(type(refresh_columns) in {tuple, list}
            and len(set(refresh_columns)) == len(refresh_columns)
            and set(refresh_columns) <= set(columns)
            and not set(refresh_columns) & (set(key_columns) | {scope_column}),
            "invalid provenance refresh columns")


def _refresh(rows, scope_column, scopes, refresh):
    for row in rows.values():
        if _token(row[scope_column]) in scopes:
            row.update(refresh)


def build(candidate, baseline, *, baseline_sha256, key_columns, scope_column,
          scope_values, refresh_columns=()):
    """Encode changed rows plus one provenance projection for fetched scopes."""
    _binding(baseline, baseline_sha256)
    _contract(candidate.columns, key_columns, scope_column, scope_values, refresh_columns)
    schema = pa.Schema.from_pandas(candidate, preserve_index=False)
    _validate_schema(schema)
    encoded_schema = schema.serialize().to_pybytes()
    require(len(encoded_schema) <= 65536, "table schema exceeds byte limit")
    new = _rows(candidate, key_columns)
    old = {} if baseline is None else _rows(baseline, key_columns)
    if baseline is not None:
        prior_schema = pa.Schema.from_pandas(baseline, preserve_index=False)
        require(_compatible_schema(schema, prior_schema), "baseline schema drift")
    scopes = set(scope_values)
    fresh = candidate.loc[candidate[scope_column].isin(scopes)]
    refresh = {}
    if not fresh.empty:
        for column in refresh_columns:
            values = fresh[column].tolist()
            require(all(_equal(values[0], value) for value in values),
                    "provenance projection is not uniform")
            refresh[column] = None if pd.isna(values[0]) else values[0]
    _refresh(old, scope_column, scopes, refresh)
    updates = []
    for key, row in new.items():
        if key not in old or any(not _equal(row[name], old[key][name]) for name in candidate.columns):
            require(_token(row[scope_column]) in scopes, "changed row outside fetched scopes")
            updates.append(row)
    removals = []
    for key, row in old.items():
        if key not in new:
            require(_token(row[scope_column]) in scopes, "removed row outside fetched scopes")
            removals.append(list(key))
    refresh_table = (pa.Table.from_pandas(fresh.loc[:, list(refresh_columns)].iloc[:1], preserve_index=False)
                     if refresh_columns and not fresh.empty else None)
    update_table = pa.Table.from_pylist(updates, schema=schema) if updates else None
    return {"schema_version": SCHEMA, "baseline_sha256": baseline_sha256,
            "schema": base64.b64encode(encoded_schema).decode("ascii"),
            "key_columns": list(key_columns), "scope_column": scope_column,
            "scope_values": list(scope_values), "refresh_columns": list(refresh_columns),
            "refresh": _encode(refresh_table) if refresh_table is not None else None,
            "updates": _encode(update_table) if update_table is not None else None,
            "removals": removals, "row_count": len(new),
            "parquet_schema_sha256": hashlib.sha256(schema.remove_metadata().to_string().encode()).hexdigest()}


def resolve(payload, baseline, *, baseline_sha256, key_columns, scope_column,
            scope_values, refresh_columns=()):
    """Reconstruct in the offline image, before existing domain validation."""
    _binding(baseline, baseline_sha256)
    require(type(payload) is dict and set(payload) == FIELDS and payload["schema_version"] == SCHEMA,
            "invalid table increment schema")
    require(payload["baseline_sha256"] == baseline_sha256, "table baseline identity differs")
    require(payload["key_columns"] == list(key_columns)
            and payload["scope_column"] == scope_column
            and payload["scope_values"] == list(scope_values)
            and payload["refresh_columns"] == list(refresh_columns), "table scope contract differs")
    require(type(payload["schema"]) is str and len(payload["schema"]) < 90000, "invalid table schema bytes")
    raw_schema = base64.b64decode(payload["schema"], validate=True)
    schema_input = pa.BufferReader(raw_schema)
    schema = pa.ipc.read_schema(schema_input)
    require(schema_input.tell() == len(raw_schema), "trailing schema bytes")
    _validate_schema(schema)
    _contract(schema.names, key_columns, scope_column, scope_values, refresh_columns)
    fingerprint = hashlib.sha256(schema.remove_metadata().to_string().encode()).hexdigest()
    require(payload["parquet_schema_sha256"] == fingerprint, "table schema fingerprint differs")
    require(type(payload["row_count"]) is int and 0 <= payload["row_count"] <= MAX_ROWS,
            "invalid output table count")
    if baseline is not None:
        require(_compatible_schema(schema, pa.Schema.from_pandas(baseline, preserve_index=False)),
                "baseline schema drift")
    rows = {} if baseline is None else _rows(baseline, key_columns)
    scopes = set(scope_values)
    if payload["refresh"] is not None:
        table = _decode(payload["refresh"])
        require(table.num_rows == 1 and table.column_names == list(refresh_columns),
                "invalid provenance projection")
        require(all(table.schema.field(name).type == schema.field(name).type for name in refresh_columns),
                "provenance projection type differs")
        _refresh(rows, scope_column, scopes, table.to_pylist()[0])
    removed = payload["removals"]
    require(type(removed) is list and len(removed) <= MAX_ROWS, "invalid removal list")
    seen = set()
    for item in removed:
        require(type(item) is list and len(item) == len(key_columns)
                and all(type(value) in {str, int, float, bool} for value in item), "invalid removal key")
        key = tuple(item)
        require(key not in seen and key in rows and _token(rows[key][scope_column]) in scopes,
                "duplicate, missing or out-of-scope removal")
        seen.add(key)
        del rows[key]
    if payload["updates"] is not None:
        table = _decode(payload["updates"])
        require(table.schema.equals(schema, check_metadata=True), "update schema differs")
        fresh = _rows(table.to_pandas(), key_columns)
        for key, row in fresh.items():
            require(key not in seen and _token(row[scope_column]) in scopes, "out-of-scope update")
            rows[key] = row
    require(len(rows) == payload["row_count"], "resolved table count differs")
    result = pa.Table.from_pylist(list(rows.values()), schema=schema).to_pandas()
    metadata = json.loads((schema.metadata or {}).get(b"pandas", b'{}'))
    for column in metadata.get("columns", []):
        if column.get("numpy_type") == "object" and column.get("field_name") in result:
            result[column["field_name"]] = result[column["field_name"]].astype(object)
    return result.sort_values(list(key_columns), kind="stable").reset_index(drop=True)
