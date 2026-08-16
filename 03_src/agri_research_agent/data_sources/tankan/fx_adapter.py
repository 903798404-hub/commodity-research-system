"""Tankan-specific wide-to-long FX candidate adapter."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import yaml

from agri_research_agent.research_data import ProviderSeriesId

from .queries import FX_WINDOW_QUERY


FX_SOURCE_COLUMNS = ("spot", *(f"fx_{month}m" for month in range(1, 13)))
FX_RAW_SCHEMA = pa.schema(
    [
        pa.field("trade_date", pa.date32(), nullable=False),
        *(pa.field(column, pa.float64(), nullable=True) for column in FX_SOURCE_COLUMNS),
        pa.field("updated_at", pa.timestamp("us"), nullable=True),
    ]
)
FX_CANDIDATE_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.string(), nullable=False),
        pa.field("dataset_id", pa.string(), nullable=False),
        pa.field("series_id_candidate", pa.string(), nullable=True),
        pa.field("provider_dataset_id", pa.string(), nullable=False),
        pa.field("provider_series_id", pa.string(), nullable=False),
        pa.field("origin_system", pa.string(), nullable=False),
        pa.field("acquisition_channel", pa.string(), nullable=False),
        pa.field("source_locator", pa.string(), nullable=False),
        pa.field("quote_date", pa.date32(), nullable=False),
        pa.field("value_date", pa.date32(), nullable=True),
        pa.field("maturity_date", pa.date32(), nullable=True),
        pa.field("base_currency", pa.string(), nullable=False),
        pa.field("quote_currency", pa.string(), nullable=False),
        pa.field("tenor", pa.string(), nullable=False),
        pa.field("tenor_months", pa.int8(), nullable=False),
        pa.field("rate", pa.float64(), nullable=True),
        pa.field("rate_type", pa.string(), nullable=False),
        pa.field("rate_unit", pa.string(), nullable=False),
        pa.field("source_column", pa.string(), nullable=False),
        pa.field("source_updated_at", pa.timestamp("us", tz="Asia/Shanghai"), nullable=True),
        pa.field("captured_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("snapshot_sha256", pa.string(), nullable=False),
        pa.field("source_row_sha256", pa.string(), nullable=False),
        pa.field("quality_status", pa.string(), nullable=False),
        pa.field("is_usable", pa.bool_(), nullable=False),
    ]
)
FX_SCHEMA_VERSION = "tankan-fx-candidate/1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FxAdapterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FxTenorMapping:
    source_column: str
    tenor: str
    tenor_months: int

    @property
    def provider_series_id(self) -> ProviderSeriesId:
        return ProviderSeriesId(f"tankan:market.exchange_rate:{self.source_column}")


@dataclass(frozen=True, slots=True)
class FxAdapterConfig:
    base_currency: str
    quote_currency: str
    rate_unit: str
    rate_type: str
    tenors: tuple[FxTenorMapping, ...]


@dataclass(frozen=True, slots=True)
class FxAdapterResult:
    table: pa.Table
    quality_report: dict[str, object]


def load_fx_config(path: str | Path) -> FxAdapterConfig:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "base_currency",
        "quote_currency",
        "rate_unit",
        "rate_type",
        "tenors",
    }:
        raise FxAdapterError("FX config fields are invalid")
    if payload["schema_version"] != 1:
        raise FxAdapterError("unsupported FX config version")
    raw_tenors = payload["tenors"]
    if not isinstance(raw_tenors, list) or any(
        not isinstance(item, dict)
        or set(item) != {"source_column", "tenor", "tenor_months"}
        for item in raw_tenors
    ):
        raise FxAdapterError("FX tenor mapping fields are invalid")
    tenors = tuple(
        FxTenorMapping(
            source_column=str(item["source_column"]),
            tenor=str(item["tenor"]),
            tenor_months=int(item["tenor_months"]),
        )
        for item in raw_tenors
    )
    config = FxAdapterConfig(
        base_currency=str(payload["base_currency"]),
        quote_currency=str(payload["quote_currency"]),
        rate_unit=str(payload["rate_unit"]),
        rate_type=str(payload["rate_type"]),
        tenors=tenors,
    )
    if (
        config.base_currency,
        config.quote_currency,
        config.rate_unit,
        config.rate_type,
    ) != ("USD", "CNH", "CNH_per_USD", "unspecified"):
        raise FxAdapterError("FX direction or rate semantics are not approved")
    if (
        {item.source_column for item in tenors} != set(FX_SOURCE_COLUMNS)
        or {item.tenor_months for item in tenors} != set(range(13))
        or len({str(item.provider_series_id) for item in tenors}) != 13
    ):
        raise FxAdapterError("FX config must define exactly SPOT and 1M through 12M")
    return config


def adapt_fx(
    raw: pa.Table,
    config: FxAdapterConfig,
    *,
    snapshot_sha256: str,
    captured_at: datetime,
    previous_latest_date: date | None = None,
) -> FxAdapterResult:
    if raw.schema != FX_RAW_SCHEMA:
        raise FxAdapterError("FX raw schema does not match the exact contract")
    if _SHA256.fullmatch(snapshot_sha256) is None:
        raise FxAdapterError("snapshot_sha256 is invalid")
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise FxAdapterError("captured_at must be timezone-aware")
    provider = FX_WINDOW_QUERY.provider
    source_timezone = ZoneInfo("Asia/Shanghai")
    records: list[dict[str, object]] = []
    statuses: Counter[str] = Counter()
    keys: set[tuple[object, ...]] = set()
    tenor_counts: Counter[str] = Counter()
    curve_warning_dates = 0

    for source_row in raw.to_pylist():
        updated = source_row["updated_at"]
        if updated is not None and updated.tzinfo is None:
            updated = updated.replace(tzinfo=source_timezone)
        usable_day_rates: list[float] = []
        for tenor in config.tenors:
            value = source_row[tenor.source_column]
            quality = "valid"
            if value is None:
                quality = "missing_rate"
            elif not math.isfinite(float(value)):
                quality = "nonfinite_rate"
            elif float(value) <= 0:
                quality = "nonpositive_rate"
            else:
                usable_day_rates.append(float(value))
            record = {
                "schema_version": FX_SCHEMA_VERSION,
                "dataset_id": str(provider.dataset.dataset_id),
                "series_id_candidate": None,
                "provider_dataset_id": str(provider.provider_dataset_id),
                "provider_series_id": str(tenor.provider_series_id),
                "origin_system": str(provider.dataset.origin_system),
                "acquisition_channel": provider.acquisition_channel.value,
                "source_locator": str(provider.source_locator),
                "quote_date": source_row["trade_date"],
                "value_date": None,
                "maturity_date": None,
                "base_currency": config.base_currency,
                "quote_currency": config.quote_currency,
                "tenor": tenor.tenor,
                "tenor_months": tenor.tenor_months,
                "rate": None if value is None else float(value),
                "rate_type": config.rate_type,
                "rate_unit": config.rate_unit,
                "source_column": tenor.source_column,
                "source_updated_at": updated,
                "captured_at": captured_at.astimezone(timezone.utc),
                "snapshot_sha256": snapshot_sha256,
                "source_row_sha256": _point_sha(source_row, tenor.source_column),
                "quality_status": quality,
                "is_usable": quality == "valid",
            }
            key = (
                record["provider_series_id"],
                record["quote_date"],
                record["rate_type"],
            )
            if key in keys:
                raise FxAdapterError("FX provider candidate key is duplicated")
            keys.add(key)
            records.append(record)
            statuses[quality] += 1
            tenor_counts[tenor.tenor] += 1
        if (
            len(usable_day_rates) == 13
            and max(usable_day_rates) / min(usable_day_rates) > 1.25
        ):
            curve_warning_dates += 1

    latest = max((item["quote_date"] for item in records), default=None)
    if previous_latest_date is not None and (
        latest is None or latest < previous_latest_date
    ):
        raise FxAdapterError("FX latest date regressed")
    expected_per_tenor = raw.num_rows
    if any(tenor_counts[item.tenor] != expected_per_tenor for item in config.tenors):
        raise FxAdapterError("FX tenor completeness failed")
    report = {
        "schema_version": 1,
        "candidate_only": True,
        "promotion_authorized": False,
        "dataset_id": str(provider.dataset.dataset_id),
        "row_count": len(records),
        "usable_row_count": sum(bool(item["is_usable"]) for item in records),
        "unusable_row_count": sum(not bool(item["is_usable"]) for item in records),
        "quality_status_counts": dict(sorted(statuses.items())),
        "tenor_row_counts": {
            item.tenor: tenor_counts[item.tenor]
            for item in sorted(config.tenors, key=lambda value: value.tenor_months)
        },
        "curve_warning_date_count": curve_warning_dates,
        "source_min_date": (
            min(item["quote_date"] for item in records).isoformat()
            if records
            else None
        ),
        "source_max_date": latest.isoformat() if latest else None,
        "canonical_series_status": "blocked_rate_type_unspecified",
    }
    return FxAdapterResult(
        table=pa.Table.from_pylist(records, schema=FX_CANDIDATE_SCHEMA),
        quality_report=report,
    )


def _point_sha(row: dict[str, object], source_column: str) -> str:
    updated = row["updated_at"]
    value = row[source_column]
    payload = {
        "trade_date": row["trade_date"].isoformat(),
        "source_column": source_column,
        "rate": (
            {"nonfinite_float": repr(value)}
            if isinstance(value, float) and not math.isfinite(value)
            else value
        ),
        "updated_at": updated.isoformat() if isinstance(updated, datetime) else None,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
