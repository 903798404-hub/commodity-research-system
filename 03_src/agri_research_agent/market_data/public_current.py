"""Fail-closed consumer reader for immutable Public Market Data Currents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

import pyarrow as pa
import pyarrow.compute as pc

from agri_research_agent.pipelines.lutou_goal_b import (
    GoalBCurrent,
    LutouGoalBError,
    load_current,
)
from agri_research_agent.shared.file_identity import identify_file


class PublicCurrentErrorCode(StrEnum):
    PUBLIC_CURRENT_UNAVAILABLE = "PUBLIC_CURRENT_UNAVAILABLE"
    INVALID_CURRENT_MANIFEST = "INVALID_CURRENT_MANIFEST"
    SERIES_NOT_FOUND = "SERIES_NOT_FOUND"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    SERIES_METADATA_MISMATCH = "SERIES_METADATA_MISMATCH"


class PublicCurrentError(RuntimeError):
    def __init__(self, code: PublicCurrentErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code


@dataclass(frozen=True, slots=True)
class PublicSeriesRequirement:
    series_id: str
    currency: str
    unit: str
    price_type: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.series_id, self.currency, self.unit, self.price_type)
        ):
            raise ValueError("Public Series requirement fields must be non-empty")


@dataclass(frozen=True, slots=True)
class PublicCurrentIdentity:
    release_id: str
    manifest_sha256: str
    schema_version: str
    source_max_date: date


@dataclass(frozen=True, slots=True)
class PublicCurrentSnapshot:
    identity: PublicCurrentIdentity
    records_by_series_id: Mapping[str, tuple[Mapping[str, object], ...]]


def resolve_three_oil_current_identity(
    public_current_root: str | Path,
) -> PublicCurrentIdentity:
    identity, _ = _resolve_current(public_current_root)
    return identity


def load_three_oil_public_current(
    public_current_root: str | Path,
    requirements: Sequence[PublicSeriesRequirement],
) -> PublicCurrentSnapshot:
    """Load exact required Series from one validated Three-Oil Current release."""

    if not requirements:
        raise ValueError("at least one Public Series requirement is required")
    by_id = {item.series_id: item for item in requirements}
    if len(by_id) != len(requirements):
        raise ValueError("Public Series requirements contain duplicate series_id values")
    identity, current = _resolve_current(public_current_root)
    table = _select_required_series(current.observations, tuple(by_id))

    output: dict[str, list[Mapping[str, object]]] = {
        series_id: [] for series_id in by_id
    }
    metadata: dict[str, tuple[str, str, str]] = {}
    keys: set[tuple[str, date]] = set()
    for row in table.to_pylist():
        series_id = str(row["series_id"])
        requirement = by_id.get(series_id)
        if requirement is None:
            continue
        currency = str(row["currency"])
        unit = str(row["unit"])
        price_type = str(row["price_type"])
        if currency != requirement.currency:
            raise PublicCurrentError(
                PublicCurrentErrorCode.CURRENCY_MISMATCH,
                f"currency differs for required Series {series_id}",
            )
        if unit != requirement.unit:
            raise PublicCurrentError(
                PublicCurrentErrorCode.UNIT_MISMATCH,
                f"unit differs for required Series {series_id}",
            )
        if price_type != requirement.price_type:
            raise PublicCurrentError(
                PublicCurrentErrorCode.SERIES_METADATA_MISMATCH,
                f"price_type differs for required Series {series_id}",
            )
        observed_metadata = (currency, unit, price_type)
        previous_metadata = metadata.setdefault(series_id, observed_metadata)
        if previous_metadata != observed_metadata:
            raise PublicCurrentError(
                PublicCurrentErrorCode.SERIES_METADATA_MISMATCH,
                f"metadata is inconsistent for required Series {series_id}",
            )
        business_date = row["business_date"]
        if type(business_date) is not date:
            raise PublicCurrentError(
                PublicCurrentErrorCode.INVALID_CURRENT_MANIFEST,
                "Public Current business_date is invalid",
            )
        key = (series_id, business_date)
        if key in keys:
            raise PublicCurrentError(
                PublicCurrentErrorCode.INVALID_CURRENT_MANIFEST,
                f"duplicate Public Current stable key for {series_id}",
            )
        keys.add(key)
        value = row["value"]
        if not isinstance(value, Decimal) or not value.is_finite():
            raise PublicCurrentError(
                PublicCurrentErrorCode.INVALID_CURRENT_MANIFEST,
                f"Public Current value is invalid for {series_id}",
            )
        output[series_id].append(
            MappingProxyType(
                {
                    **row,
                    "price": value,
                    "value_semantics": "canonical",
                    "current_release_id": identity.release_id,
                    "current_manifest_sha256": identity.manifest_sha256,
                }
            )
        )
    missing = tuple(sorted(series_id for series_id, rows in output.items() if not rows))
    if missing:
        raise PublicCurrentError(
            PublicCurrentErrorCode.SERIES_NOT_FOUND,
            f"required Public Series are absent: {', '.join(missing)}",
        )
    records = MappingProxyType(
        {
            series_id: tuple(sorted(rows, key=lambda item: item["business_date"]))
            for series_id, rows in output.items()
        }
    )
    return PublicCurrentSnapshot(identity, records)


def _select_required_series(
    table: pa.Table, series_ids: Sequence[str]
) -> pa.Table:
    """Filter validated Current rows in Arrow before Python materialization."""

    if "series_id" not in table.column_names:
        raise PublicCurrentError(
            PublicCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            "Public Current series_id column is missing",
        )
    series_type = table.schema.field("series_id").type
    try:
        requested = pa.array(series_ids, type=series_type)
        return table.filter(pc.is_in(table["series_id"], value_set=requested))
    except (pa.ArrowException, TypeError, ValueError):
        raise PublicCurrentError(
            PublicCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            "Public Current series_id filtering failed",
        ) from None


def _resolve_current(
    public_current_root: str | Path,
) -> tuple[PublicCurrentIdentity, GoalBCurrent]:
    try:
        current = load_current(public_current_root)
    except (LutouGoalBError, OSError, ValueError) as exc:
        raise PublicCurrentError(
            PublicCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            f"Three-Oil Public Current is invalid: {type(exc).__name__}",
        ) from None
    if current is None:
        raise PublicCurrentError(
            PublicCurrentErrorCode.PUBLIC_CURRENT_UNAVAILABLE,
            "Three-Oil Public Current pointer is unavailable",
        )
    try:
        manifest_path = current.directory / "manifest.json"
        manifest_identity = identify_file(manifest_path)
        source_max_date = date.fromisoformat(str(current.manifest["source_max_date"]))
    except (OSError, ValueError, KeyError):
        raise PublicCurrentError(
            PublicCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            "Public Current traceability identity is invalid",
        ) from None
    if (
        current.manifest.get("schema_version") != "lutou-goal-b-current/2"
        or current.manifest.get("release_id") != current.release_id
        or current.manifest.get("scope") != "three-oil-v1-sealed-series"
        or current.manifest.get("quality_status") != "PASS"
        or current.manifest.get("series_count") != 20
    ):
        raise PublicCurrentError(
            PublicCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            "Public Current release manifest contract is invalid",
        )
    identity = PublicCurrentIdentity(
        current.release_id,
        manifest_identity.sha256,
        str(current.manifest["schema_version"]),
        source_max_date,
    )
    return identity, current


__all__ = [
    "PublicCurrentError", "PublicCurrentErrorCode", "PublicCurrentIdentity",
    "PublicCurrentSnapshot", "PublicSeriesRequirement",
    "load_three_oil_public_current", "resolve_three_oil_current_identity",
]
