"""Arrow-native authoritative-window upsert shared by public-data producers."""

from __future__ import annotations

from datetime import date
from typing import Sequence

import pyarrow as pa
import pyarrow.compute as pc


class ArrowWindowUpsertError(ValueError):
    """Raised when an authoritative-window merge cannot fail closed."""


def merge_authoritative_window(
    previous: pa.Table,
    window: pa.Table,
    *,
    keys: Sequence[str],
    date_column: str,
    lower: date,
    hash_column: str,
    require_non_empty_window: bool = False,
) -> pa.Table:
    """Replace the canonical window while reusing byte-identical prior rows."""

    if previous.schema != window.schema:
        raise ArrowWindowUpsertError("incremental schemas differ")
    if require_non_empty_window and window.num_rows == 0:
        raise ArrowWindowUpsertError("incremental window is empty")
    _require_unique(previous, keys, "previous")
    _require_unique(window, keys, "window")

    boundary = pa.scalar(lower, type=pa.date32())
    before_window = previous.filter(pc.less(previous[date_column], boundary))
    replaceable = previous.filter(pc.greater_equal(previous[date_column], boundary))
    prior_hash = f"__previous_{hash_column}"
    lookup = replaceable.select([*keys, hash_column]).rename_columns(
        [*keys, prior_hash]
    )
    lookup = lookup.append_column(
        "__previous_index", pa.array(range(replaceable.num_rows), type=pa.int64())
    )
    joined = window.join(lookup, keys=list(keys), join_type="left outer")
    unchanged = pc.fill_null(
        pc.equal(joined[hash_column], joined[prior_hash]),
        False,
    )
    unchanged_previous = replaceable.take(
        joined.filter(unchanged)["__previous_index"]
    )
    changed_window = (
        joined.filter(pc.invert(unchanged))
        .select(window.column_names)
        .cast(previous.schema)
    )
    return pa.concat_tables(
        [before_window, unchanged_previous, changed_window]
    ).sort_by([(key, "ascending") for key in keys])


def _require_unique(table: pa.Table, keys: Sequence[str], label: str) -> None:
    distinct = table.select(keys).group_by(list(keys)).aggregate([])
    if distinct.num_rows != table.num_rows:
        raise ArrowWindowUpsertError(f"{label} stable key is duplicated")
