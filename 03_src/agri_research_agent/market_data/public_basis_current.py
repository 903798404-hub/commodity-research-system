"""Fail-closed consumer reader for the Formal Public Domestic Basis Current."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import pandas as pd

from agri_research_agent.pipelines.lutou_domestic_basis import (
    FORMAL_BASELINE_SHA256,
    FORMAL_CUTOVER_DATE,
    DomesticBasisPipelineError,
    load_domestic_basis_current,
)
from agri_research_agent.shared.file_identity import identify_file


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CURRENT_SCHEMA = "lutou-domestic-basis-current/3"
_CURRENT_SCOPE = "formal-domestic-basis"
_EXPECTED_SERIES = 29
_EXPECTED_HISTORICAL_ROWS = 16_331
_EXPECTED_BASELINE_ROWS = 17_042
_EXPECTED_HISTORICAL_COMMODITIES = {
    "一豆",
    "24度",
    "三菜",
    "豆粕",
    "菜粕",
    "一葵",
    "一级玉米油",
    "葵粕",
}
_CONSUMER_FIELDS = (
    "date",
    "commodity",
    "region",
    "quote_type",
    "delivery_month",
    "futures_contract",
    "cash_price",
    "futures_price",
    "basis",
    "source_sheet",
)
_TRACE_FIELDS = (
    "series_id",
    "business_date",
    "segment",
    "provider",
    "source_series_id",
    "source_locator",
    "currency",
    "unit",
)
_STABLE_KEY = (
    "series_id",
    "business_date",
    "quote_type",
    "delivery_month",
    "futures_contract",
)


class PublicBasisCurrentErrorCode(StrEnum):
    PUBLIC_CURRENT_UNAVAILABLE = "PUBLIC_CURRENT_UNAVAILABLE"
    INVALID_CURRENT_MANIFEST = "INVALID_CURRENT_MANIFEST"
    CURRENT_IDENTITY_MISMATCH = "CURRENT_IDENTITY_MISMATCH"
    SERIES_NOT_FOUND = "SERIES_NOT_FOUND"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    CONTRACT_MISMATCH = "CONTRACT_MISMATCH"


class PublicBasisCurrentError(RuntimeError):
    def __init__(self, code: PublicBasisCurrentErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code


@dataclass(frozen=True, slots=True)
class PublicBasisCurrentIdentity:
    release_id: str
    manifest_sha256: str
    schema_version: str
    row_count: int
    series_count: int
    min_date: date
    max_date: date
    source_max_date: date


@dataclass(frozen=True, slots=True)
class PublicBasisCurrentSnapshot:
    identity: PublicBasisCurrentIdentity
    records: pd.DataFrame

    @property
    def source_identity(self) -> dict[str, object]:
        return {
            "release_id": self.identity.release_id,
            "manifest_sha256": self.identity.manifest_sha256,
            "schema_version": self.identity.schema_version,
            "row_count": self.identity.row_count,
            "series_count": self.identity.series_count,
            "source_max_date": self.identity.source_max_date.isoformat(),
        }


def resolve_public_basis_current_identity(
    public_current_root: str | Path,
) -> PublicBasisCurrentIdentity:
    root = Path(public_current_root).resolve()
    release_id, manifest_sha256 = _read_pointer_identity(root)
    return _resolve_current_versioned(str(root), release_id, manifest_sha256)[0]


def load_public_basis_current(
    public_current_root: str | Path,
    *,
    expected_release_id: str | None = None,
    expected_manifest_sha256: str | None = None,
) -> PublicBasisCurrentSnapshot:
    """Load all formal consumer rows from one immutable Current identity."""

    root = Path(public_current_root).resolve()
    release_id, manifest_sha256 = _read_pointer_identity(root)
    if expected_release_id is not None and expected_release_id != release_id:
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.CURRENT_IDENTITY_MISMATCH,
            "Domestic Basis Current release changed before loading",
        )
    if (
        expected_manifest_sha256 is not None
        and expected_manifest_sha256 != manifest_sha256
    ):
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.CURRENT_IDENTITY_MISMATCH,
            "Domestic Basis Current manifest changed before loading",
        )
    identity, cached = _resolve_current_versioned(
        str(root), release_id, manifest_sha256
    )
    after_release, after_manifest = _read_pointer_identity(root)
    if (after_release, after_manifest) != (release_id, manifest_sha256):
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.CURRENT_IDENTITY_MISMATCH,
            "Domestic Basis Current changed while loading",
        )
    return PublicBasisCurrentSnapshot(identity, cached.copy(deep=True))


def clear_public_basis_current_cache() -> None:
    _resolve_current_versioned.cache_clear()


def _read_pointer_identity(root: Path) -> tuple[str, str]:
    pointer_path = root / "current.json"
    if not pointer_path.is_file():
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.PUBLIC_CURRENT_UNAVAILABLE,
            "Formal Domestic Basis Current pointer is unavailable",
        )
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        if set(pointer) != {"schema_version", "release_id", "manifest_sha256"}:
            raise ValueError("pointer keys differ")
        if pointer["schema_version"] != 1:
            raise ValueError("pointer schema differs")
        release_id = str(pointer["release_id"])
        manifest_sha256 = str(pointer["manifest_sha256"])
        if _RELEASE_ID.fullmatch(release_id) is None:
            raise ValueError("release identity is invalid")
        if _SHA256.fullmatch(manifest_sha256) is None:
            raise ValueError("manifest identity is invalid")
        manifest_path = root / "releases" / release_id / "manifest.json"
        if identify_file(manifest_path).sha256 != manifest_sha256:
            raise ValueError("manifest bytes differ")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            "Formal Domestic Basis Current pointer identity is invalid",
        ) from None
    return release_id, manifest_sha256


@lru_cache(maxsize=8)
def _resolve_current_versioned(
    root_value: str, release_id: str, manifest_sha256: str
) -> tuple[PublicBasisCurrentIdentity, pd.DataFrame]:
    try:
        current = load_domestic_basis_current(root_value)
    except (DomesticBasisPipelineError, OSError, KeyError, TypeError, ValueError):
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            "Formal Domestic Basis Current release is invalid",
        ) from None
    if current is None:
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.PUBLIC_CURRENT_UNAVAILABLE,
            "Formal Domestic Basis Current release is unavailable",
        )
    manifest = current.manifest
    try:
        actual_manifest_sha256 = identify_file(
            current.directory / "manifest.json"
        ).sha256
        min_date = date.fromisoformat(str(manifest["min_date"]))
        max_date = date.fromisoformat(str(manifest["max_date"]))
        source_max_date = date.fromisoformat(str(manifest["source_max_date"]))
    except (OSError, KeyError, TypeError, ValueError):
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            "Formal Domestic Basis traceability identity is invalid",
        ) from None
    parity = manifest.get("formal_contract_parity")
    quality = manifest.get("quality")
    field_differences = (
        parity.get("field_differences") if isinstance(parity, dict) else None
    )
    if (
        current.release_id != release_id
        or actual_manifest_sha256 != manifest_sha256
        or manifest.get("release_id") != release_id
        or manifest.get("schema_version") != _CURRENT_SCHEMA
        or manifest.get("source") != "sealed_history_plus_lutou"
        or manifest.get("scope") != _CURRENT_SCOPE
        or manifest.get("quality_status") != "PASS"
        or manifest.get("cutover_date") != FORMAL_CUTOVER_DATE.isoformat()
        or manifest.get("series_count") != _EXPECTED_SERIES
        or manifest.get("historical_row_count") != _EXPECTED_HISTORICAL_ROWS
        or not isinstance(parity, dict)
        or parity.get("quality_status") != "PASS"
        or parity.get("baseline_sha256") != FORMAL_BASELINE_SHA256
        or parity.get("baseline_rows") != _EXPECTED_BASELINE_ROWS
        or parity.get("common_rows") != _EXPECTED_BASELINE_ROWS
        or parity.get("baseline_only_rows") != 0
        or parity.get("public_only_nonextension_rows") != 0
        or not isinstance(field_differences, dict)
        or set(field_differences) != set(_CONSUMER_FIELDS)
        or any(field_differences.values())
        or not isinstance(quality, dict)
        or quality.get("stable_key_duplicate_count") != 0
        or quality.get("legacy_contract_collision_count") != 0
    ):
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            "Formal Domestic Basis manifest contract is invalid",
        )
    records = current.observations.to_pandas()
    required = {*_CONSUMER_FIELDS, *_TRACE_FIELDS}
    if required - set(records.columns):
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.CONTRACT_MISMATCH,
            "Formal Domestic Basis consumer fields are incomplete",
        )
    try:
        _validate_records(records, manifest)
    except PublicBasisCurrentError:
        raise
    except (KeyError, TypeError, ValueError):
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.CONTRACT_MISMATCH,
            "Formal Domestic Basis record contract is invalid",
        ) from None
    records["date"] = pd.to_datetime(records["date"], errors="raise")
    records["business_date"] = pd.to_datetime(
        records["business_date"], errors="raise"
    ).dt.date
    records["current_release_id"] = release_id
    records["current_manifest_sha256"] = manifest_sha256
    records["source_segment_identity"] = records["segment"]
    records = records.sort_values(
        ["date", "commodity", "region", "quote_type", "delivery_month", "series_id"]
    ).reset_index(drop=True)
    identity = PublicBasisCurrentIdentity(
        release_id,
        manifest_sha256,
        _CURRENT_SCHEMA,
        len(records),
        records["series_id"].nunique(),
        min_date,
        max_date,
        source_max_date,
    )
    return identity, records


def _validate_records(records: pd.DataFrame, manifest: dict[str, object]) -> None:
    if len(records) != manifest.get("row_count") or len(records) < _EXPECTED_BASELINE_ROWS:
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.CONTRACT_MISMATCH,
            "Formal Domestic Basis row count is inconsistent",
        )
    if records["series_id"].nunique() != _EXPECTED_SERIES:
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.SERIES_NOT_FOUND,
            "Formal Domestic Basis 29-series coverage is incomplete",
        )
    if set(records["currency"]) != {"CNY"}:
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.CURRENCY_MISMATCH,
            "Formal Domestic Basis currency differs from CNY",
        )
    if set(records["unit"]) != {"CNY/metric_tonne"}:
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.UNIT_MISMATCH,
            "Formal Domestic Basis unit differs from CNY/metric_tonne",
        )
    if records.duplicated(list(_STABLE_KEY)).any():
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.CONTRACT_MISMATCH,
            "Formal Domestic Basis stable key is duplicated",
        )
    if not records["date"].eq(records["business_date"]).all():
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.CONTRACT_MISMATCH,
            "Formal Domestic Basis date aliases differ",
        )
    historical = records[records["segment"].eq("SEALED_HISTORICAL")]
    live = records[records["segment"].eq("LIVE_LUTOU")]
    observed_dates = pd.to_datetime(records["date"], errors="raise")
    live_dates = pd.to_datetime(live["date"], errors="raise")
    if (
        len(historical) != _EXPECTED_HISTORICAL_ROWS
        or live.empty
        or len(live) != manifest.get("live_row_count")
        or set(historical["commodity"]) != _EXPECTED_HISTORICAL_COMMODITIES
        or set(historical["quote_type"]) != {"基差报价", "一口价"}
        or not pd.to_datetime(historical["date"])
        .lt(pd.Timestamp(FORMAL_CUTOVER_DATE))
        .all()
        or not pd.to_datetime(live["date"])
        .ge(pd.Timestamp(FORMAL_CUTOVER_DATE))
        .all()
        or set(historical["provider"]) != {"Historical Domestic Basis Excel"}
        or set(live["provider"]) != {"Lutou"}
        or observed_dates.min().date().isoformat() != manifest.get("min_date")
        or observed_dates.max().date().isoformat() != manifest.get("max_date")
        or live_dates.max().date().isoformat() != manifest.get("source_max_date")
    ):
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.SERIES_NOT_FOUND,
            "Formal Domestic Basis historical/live segment coverage is incomplete",
        )
    historical_basis = historical[historical["quote_type"].eq("基差报价")]
    historical_cash = historical[historical["quote_type"].eq("一口价")]
    if (
        historical_basis[["cash_price", "futures_price", "basis"]].isna().any().any()
        or historical_cash["cash_price"].isna().any()
        or historical_cash[["futures_price", "basis"]].notna().any().any()
        or live[["cash_price", "futures_price"]].notna().any().any()
        or live["basis"].isna().any()
    ):
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.CONTRACT_MISMATCH,
            "Formal Domestic Basis cash/futures/basis segment semantics differ",
        )


__all__ = [
    "PublicBasisCurrentError",
    "PublicBasisCurrentErrorCode",
    "PublicBasisCurrentIdentity",
    "PublicBasisCurrentSnapshot",
    "clear_public_basis_current_cache",
    "load_public_basis_current",
    "resolve_public_basis_current_identity",
]
