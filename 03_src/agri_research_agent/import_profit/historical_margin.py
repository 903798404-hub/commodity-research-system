"""Lightweight read-only provider for soybean margin seasonality charts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import pyarrow.parquet as pq

from .historical_cnf_adapter import shipment_year_for
from .query import QueryMetric, UnknownMetricError
from .seasonality import seasonal_window


MarginKey = tuple[date, str, str, int, int]


class HistoricalMarginError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HistoricalMarginRecord:
    value: float | None
    missing_reasons: tuple[str, ...]

    def metric_value(self, metric: QueryMetric) -> float | None:
        if metric is not QueryMetric.NET_CRUSH_MARGIN:
            raise UnknownMetricError(
                "lightweight margin provider only supports net_crush_margin"
            )
        return self.value


@dataclass(frozen=True, slots=True)
class HistoricalMarginIndex:
    origin: str
    source_sha256: str
    as_of_date: date
    prepared_row_count: int
    _by_key: Mapping[MarginKey, HistoricalMarginRecord] = field(
        repr=False, compare=False
    )

    def require_origin(self, origin: str) -> None:
        if origin != self.origin:
            raise HistoricalMarginError(f"origin is unavailable: {origin}")

    def get(
        self,
        *,
        business_date: date,
        origin: str,
        shipment_year: int,
        shipment_month: int,
        commodity: str = "soybean",
    ) -> HistoricalMarginRecord | None:
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


def load_historical_margin_index(
    path: str | Path,
    *,
    source_sha256: str,
    origin: str,
    as_of_date: date,
) -> HistoricalMarginIndex:
    """Read and normalize the one materialized result source exactly once."""

    source = Path(path)
    if not source.is_file():
        raise HistoricalMarginError("historical result source is unavailable")
    normalized_sha = source_sha256.upper()
    if len(normalized_sha) != 64 or any(
        character not in "0123456789ABCDEF" for character in normalized_sha
    ):
        raise HistoricalMarginError("historical result source identity is invalid")
    earliest = min(
        seasonal_window(
            shipment_year_for(as_of_date, month) - 5, month
        )[0]
        for month in range(1, 13)
    )
    columns = (
        "business_date",
        "commodity",
        "origin",
        "shipment_year",
        "shipment_month",
        "shipment_period",
        "net_crush_margin_cny_per_tonne",
        "calculation_status",
        "missing_reasons",
    )
    try:
        table = pq.read_table(
            source,
            columns=list(columns),
            filters=[
                ("origin", "=", origin),
                ("business_date", ">=", earliest),
                ("business_date", "<=", as_of_date),
            ],
        )
    except Exception as exc:
        raise HistoricalMarginError(
            "failed to read historical margin source"
        ) from exc
    rows = table.to_pylist()
    by_key: dict[MarginKey, HistoricalMarginRecord] = {}
    previous: MarginKey | None = None
    for row in rows:
        key = (
            row["business_date"],
            row["commodity"],
            row["origin"],
            row["shipment_year"],
            row["shipment_month"],
        )
        if key in by_key:
            raise HistoricalMarginError("historical margin keys are duplicated")
        if previous is not None and key < previous:
            raise HistoricalMarginError("historical margin keys are not sorted")
        previous = key
        expected_period = (
            f"{int(row['shipment_year']):04d}-{int(row['shipment_month']):02d}"
        )
        if row["shipment_period"] != expected_period:
            raise HistoricalMarginError("historical shipment period is invalid")
        value = row["net_crush_margin_cny_per_tonne"]
        status = row["calculation_status"]
        if (status == "success") != (value is not None):
            raise HistoricalMarginError("historical margin status is invalid")
        by_key[key] = HistoricalMarginRecord(
            value=None if value is None else float(value),
            missing_reasons=tuple(row["missing_reasons"]),
        )
    return HistoricalMarginIndex(
        origin=origin,
        source_sha256=normalized_sha,
        as_of_date=as_of_date,
        prepared_row_count=len(rows),
        _by_key=MappingProxyType(by_key),
    )


__all__ = [
    "HistoricalMarginError",
    "HistoricalMarginIndex",
    "HistoricalMarginRecord",
    "load_historical_margin_index",
]
