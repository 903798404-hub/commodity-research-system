"""Fail-closed, predicate-filtered consumer reader for Public Weather Current."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from agri_research_agent.data_sources.weather_adapter import validate_weather_records
from agri_research_agent.pipelines.lutou_weather import (
    LutouWeatherError,
    WeatherCurrent,
    load_weather_current,
)
from agri_research_agent.shared.file_identity import identify_file


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CURRENT_SCHEMA = "lutou-public-weather-current/1"
_CURRENT_SCOPE = "current-weather-consumers"
_CURRENT_CONTRACT_SHA256 = "70fc364d3ce2bfd52f624f3dd5227e7c36ceba1dd329eab56720864c1957474d"
_METRIC_UNITS = {
    "precipitation": "mm",
    "temperature_max": "degC",
    "temperature_min": "degC",
    "soil_moisture": "%",
}


class PublicWeatherCurrentErrorCode(StrEnum):
    PUBLIC_CURRENT_UNAVAILABLE = "PUBLIC_CURRENT_UNAVAILABLE"
    INVALID_CURRENT_MANIFEST = "INVALID_CURRENT_MANIFEST"
    SERIES_NOT_FOUND = "SERIES_NOT_FOUND"
    METRIC_MISMATCH = "METRIC_MISMATCH"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    SERIES_METADATA_MISMATCH = "SERIES_METADATA_MISMATCH"
    CURRENT_IDENTITY_MISMATCH = "CURRENT_IDENTITY_MISMATCH"


class PublicWeatherCurrentError(RuntimeError):
    def __init__(self, code: PublicWeatherCurrentErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code


@dataclass(frozen=True, slots=True)
class PublicWeatherCurrentIdentity:
    release_id: str
    manifest_sha256: str
    schema_version: str
    observation_source_max: date
    forecast_valid_max: date
    content_sha256: str


@dataclass(frozen=True, slots=True)
class PublicWeatherCurrentSnapshot:
    identity: PublicWeatherCurrentIdentity
    records: pd.DataFrame
    normals: pd.DataFrame


@dataclass(frozen=True, slots=True)
class _ResolvedWeatherCurrent:
    current: WeatherCurrent
    identity: PublicWeatherCurrentIdentity
    soil_bindings: tuple[Mapping[str, object], ...]


def resolve_weather_current_identity(
    public_current_root: str | Path,
) -> PublicWeatherCurrentIdentity:
    """Resolve and validate the active immutable Weather Current identity."""

    root = Path(public_current_root).resolve()
    release_id, manifest_sha256 = _read_pointer_identity(root)
    return _resolve_current_versioned(str(root), release_id, manifest_sha256).identity


def load_public_weather_current(
    public_current_root: str | Path,
    *,
    crop: str,
    country: str,
    metrics: Sequence[str],
    regions: Iterable[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    expected_release_id: str | None = None,
    expected_manifest_sha256: str | None = None,
) -> PublicWeatherCurrentSnapshot:
    """Load one consumer slice without materializing the complete Weather Current."""

    requested_metrics = tuple(dict.fromkeys(str(item) for item in metrics))
    if not requested_metrics or any(item not in _METRIC_UNITS for item in requested_metrics):
        raise ValueError("Weather Current metrics must be a non-empty approved sequence")
    if start_date is not None and type(start_date) is not date:
        raise TypeError("Weather Current start_date must be an exact date")
    if end_date is not None and type(end_date) is not date:
        raise TypeError("Weather Current end_date must be an exact date")
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("Weather Current date window is inverted")
    normalized_regions = tuple(
        sorted({_region_key(item) for item in regions or () if str(item).strip()})
    )
    root = Path(public_current_root).resolve()
    release_id, manifest_sha256 = _read_pointer_identity(root)
    if expected_release_id is not None and release_id != expected_release_id:
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.CURRENT_IDENTITY_MISMATCH,
            "Weather Current release changed before the query started",
        )
    if (
        expected_manifest_sha256 is not None
        and manifest_sha256 != expected_manifest_sha256
    ):
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.CURRENT_IDENTITY_MISMATCH,
            "Weather Current manifest changed before the query started",
        )
    resolved = _resolve_current_versioned(str(root), release_id, manifest_sha256)
    frames = [
        _read_metric_versioned(
            str(root),
            release_id,
            manifest_sha256,
            crop,
            country,
            metric,
            normalized_regions,
            start_date,
            end_date,
        ).copy(deep=True)
        for metric in requested_metrics
    ]
    records = pd.concat(frames, ignore_index=True)
    normal_frames = [
        _read_normals_versioned(
            str(root),
            release_id,
            manifest_sha256,
            crop,
            country,
            metric,
            normalized_regions,
        ).copy(deep=True)
        for metric in requested_metrics
        if metric != "soil_moisture"
    ]
    normals = (
        pd.concat(normal_frames, ignore_index=True)
        if normal_frames
        else _empty_normals()
    )
    after_release, after_manifest = _read_pointer_identity(root)
    if (after_release, after_manifest) != (release_id, manifest_sha256):
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.CURRENT_IDENTITY_MISMATCH,
            "Weather Current changed while the consumer slice was loading",
        )
    return PublicWeatherCurrentSnapshot(resolved.identity, records, normals)


def clear_public_weather_current_cache() -> None:
    """Clear in-process identity and predicate caches for tests and diagnostics."""

    _resolve_current_versioned.cache_clear()
    _read_metric_versioned.cache_clear()
    _read_normals_versioned.cache_clear()


def _read_pointer_identity(root: Path) -> tuple[str, str]:
    pointer = root / "current.json"
    if not pointer.is_file():
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.PUBLIC_CURRENT_UNAVAILABLE,
            "Weather Public Current pointer is unavailable",
        )
    try:
        value = json.loads(pointer.read_text(encoding="utf-8"))
        if set(value) != {"schema_version", "release_id", "manifest_sha256"}:
            raise ValueError("pointer keys differ")
        if value["schema_version"] != 1:
            raise ValueError("pointer schema differs")
        release_id = str(value["release_id"])
        manifest_sha256 = str(value["manifest_sha256"])
        if _RELEASE_ID.fullmatch(release_id) is None:
            raise ValueError("release identity is invalid")
        if _SHA256.fullmatch(manifest_sha256) is None:
            raise ValueError("manifest identity is invalid")
        manifest_path = root / "releases" / release_id / "manifest.json"
        if identify_file(manifest_path).sha256 != manifest_sha256:
            raise ValueError("manifest bytes differ")
    except PublicWeatherCurrentError:
        raise
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            "Weather Public Current pointer identity is invalid",
        ) from None
    return release_id, manifest_sha256


@lru_cache(maxsize=8)
def _resolve_current_versioned(
    root: str, release_id: str, manifest_sha256: str
) -> _ResolvedWeatherCurrent:
    try:
        current = load_weather_current(root)
    except (LutouWeatherError, OSError, ValueError, KeyError):
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            "Weather Public Current release is invalid",
        ) from None
    if current is None:
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.PUBLIC_CURRENT_UNAVAILABLE,
            "Weather Public Current release is unavailable",
        )
    manifest = current.manifest
    try:
        actual_manifest_sha = identify_file(current.directory / "manifest.json").sha256
        observation_source_max = date.fromisoformat(
            str(manifest["source_max_dates"]["observation"])
        )
        forecast_valid_max = date.fromisoformat(
            str(manifest["source_max_dates"]["forecast_valid"])
        )
        content_sha256 = str(manifest["content_sha256"])
        if _SHA256.fullmatch(content_sha256) is None:
            raise ValueError("content identity is invalid")
        forecast_run_count = manifest["forecast_run_count"]
        if type(forecast_run_count) is not int or forecast_run_count <= 0:
            raise ValueError("forecast run count is invalid")
        actual_forecast_run_count = _count_forecast_runs(current.observations_path)
    except (OSError, KeyError, TypeError, ValueError):
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            "Weather Public Current traceability is invalid",
        ) from None
    if (
        current.release_id != release_id
        or actual_manifest_sha != manifest_sha256
        or manifest.get("release_id") != release_id
        or manifest.get("schema_version") != _CURRENT_SCHEMA
        or manifest.get("scope") != _CURRENT_SCOPE
        or manifest.get("quality_status") != "PASS"
        or manifest.get("contract_sha256") != _CURRENT_CONTRACT_SHA256
        or manifest.get("complete_series_count") != 842
        or manifest.get("series_count") != 686
        or manifest.get("soil_series_count") != 94
        or manifest.get("normal_series_count") != 62
        or manifest.get("stable_key_duplicate_count") != 0
        or manifest.get("normal_stable_key_duplicate_count") != 0
        or forecast_run_count != actual_forecast_run_count
    ):
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            "Weather Public Current manifest contract is invalid",
        )
    component_rows = (
        manifest.get("row_count", 0)
        + manifest.get("soil_row_count", 0)
        + manifest.get("normal_row_count", 0)
    )
    component_series = (
        manifest.get("series_count", 0)
        + manifest.get("soil_series_count", 0)
        + manifest.get("normal_series_count", 0)
    )
    if (
        manifest.get("complete_row_count") != component_rows
        or manifest.get("complete_series_count") != component_series
        or component_rows <= 0
    ):
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            "Weather Public Current component counts are inconsistent",
        )
    try:
        bindings_value = json.loads(
            (current.directory / "soil_bindings.json").read_text(encoding="utf-8")
        )
        bindings = tuple(bindings_value["bindings"])
        if bindings_value.get("binding_count") != 102 or len(bindings) != 102:
            raise ValueError("binding count differs")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            "Weather soil binding contract is invalid",
        ) from None
    identity = PublicWeatherCurrentIdentity(
        release_id,
        manifest_sha256,
        _CURRENT_SCHEMA,
        observation_source_max,
        forecast_valid_max,
        content_sha256,
    )
    return _ResolvedWeatherCurrent(current, identity, bindings)


def _count_forecast_runs(observations_path: Path) -> int:
    table = pq.read_table(
        observations_path,
        columns=["forecast_run_id"],
        filters=[("data_family", "=", "forecast")],
    )
    values = table.column("forecast_run_id").to_pylist()
    if not values or any(not isinstance(value, str) or not value for value in values):
        raise ValueError("forecast run identity is invalid")
    return len(set(values))


@lru_cache(maxsize=256)
def _read_metric_versioned(
    root: str,
    release_id: str,
    manifest_sha256: str,
    crop: str,
    country: str,
    metric: str,
    regions: tuple[str, ...],
    start_date: date | None,
    end_date: date | None,
) -> pd.DataFrame:
    resolved = _resolve_current_versioned(root, release_id, manifest_sha256)
    if metric == "soil_moisture":
        return _read_soil_metric(
            resolved, crop, country, regions, start_date, end_date
        )
    filters: list[tuple[str, str, object]] = [
        ("crop", "=", crop),
        ("country", "=", country),
        ("metric", "=", metric),
    ]
    if regions:
        filters.append(("region", "in", list(regions)))
    if start_date is not None:
        filters.append(("valid_date", ">=", start_date))
    if end_date is not None:
        filters.append(("valid_date", "<=", end_date))
    columns = [
        "series_id",
        "provider_series_id",
        "source_locator",
        "source_table",
        "source_column",
        "crop",
        "country",
        "region",
        "region_label",
        "region_type",
        "metric",
        "data_family",
        "forecast_model",
        "forecast_issue_date",
        "forecast_issue_status",
        "forecast_run_id",
        "valid_date",
        "value",
        "unit",
        "observation_interval",
        "aggregation",
        "soil_depth",
        "extracted_at",
        "source_row_sha256",
    ]
    table = pq.read_table(
        resolved.current.observations_path, columns=columns, filters=filters
    )
    rows = table.to_pandas()
    if rows.empty:
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.SERIES_NOT_FOUND,
            f"required Weather series are absent for {crop}/{country}/{metric}",
        )
    expected_unit = _METRIC_UNITS[metric]
    if set(rows["metric"]) != {metric}:
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.METRIC_MISMATCH,
            f"Weather metric differs for {crop}/{country}/{metric}",
        )
    if set(rows["unit"]) != {expected_unit}:
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.UNIT_MISMATCH,
            f"Weather unit differs for {crop}/{country}/{metric}",
        )
    observed = rows["data_family"].eq("observation")
    forecast = rows["data_family"].eq("forecast")
    if not (observed | forecast).all() or not rows.loc[observed, "forecast_model"].eq(
        "OBSERVED"
    ).all():
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.SERIES_METADATA_MISMATCH,
            "Weather observation/forecast family identity is invalid",
        )
    if not rows.loc[forecast, "forecast_model"].isin({"ECMWF", "GFS"}).all():
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.SERIES_METADATA_MISMATCH,
            "Weather forecast model identity is invalid",
        )
    if not rows.loc[forecast, "forecast_issue_date"].isna().all() or not rows.loc[
        forecast, "forecast_issue_status"
    ].eq("SOURCE_NOT_PROVIDED").all():
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.SERIES_METADATA_MISMATCH,
            "Weather forecast issue-date semantics are invalid",
        )
    rows = _select_active_forecast_runs(rows)
    _require_regions(rows, regions, crop, country, metric)
    _require_unique(rows, ["series_id", "valid_date", "forecast_run_id"])
    rows = rows.rename(
        columns={
            "valid_date": "date",
            "data_family": "data_type",
            "forecast_model": "model",
            "extracted_at": "source_updated_at",
        }
    )
    rows["data_type"] = rows["data_type"].replace({"observation": "observed"})
    rows["forecast_run_at"] = pd.NaT
    rows["current_release_id"] = release_id
    rows["current_manifest_sha256"] = manifest_sha256
    rows["value_semantics"] = "canonical"
    rows = validate_weather_records(rows)
    return rows.sort_values(
        ["date", "region", "data_type", "model", "series_id"]
    ).reset_index(drop=True)


def _select_active_forecast_runs(rows: pd.DataFrame) -> pd.DataFrame:
    forecast = rows[rows["data_family"].eq("forecast")].copy()
    if forecast.empty:
        return rows
    run_metadata = forecast[
        ["series_id", "forecast_run_id", "extracted_at"]
    ].drop_duplicates()
    if run_metadata.duplicated(["series_id", "forecast_run_id"]).any():
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.SERIES_METADATA_MISMATCH,
            "Weather forecast run has inconsistent extraction identity",
        )
    latest = run_metadata.groupby("series_id")["extracted_at"].transform("max")
    active = run_metadata[run_metadata["extracted_at"].eq(latest)]
    if active.duplicated("series_id", keep=False).any():
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.SERIES_METADATA_MISMATCH,
            "Weather forecast active run identity is ambiguous",
        )
    selected = forecast.merge(
        active[["series_id", "forecast_run_id"]],
        on=["series_id", "forecast_run_id"],
        how="inner",
        validate="many_to_one",
    )
    observed = rows[rows["data_family"].eq("observation")]
    return pd.concat([observed, selected], ignore_index=True)


def _read_soil_metric(
    resolved: _ResolvedWeatherCurrent,
    crop: str,
    country: str,
    regions: tuple[str, ...],
    start_date: date | None,
    end_date: date | None,
) -> pd.DataFrame:
    bindings = pd.DataFrame(resolved.soil_bindings)
    selected = bindings[
        bindings["crop"].eq(crop)
        & bindings["country"].eq(country)
        & bindings["metric"].eq("soil_moisture")
    ].copy()
    if regions:
        selected = selected[selected["region"].isin(regions)].copy()
    if selected.empty:
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.SERIES_NOT_FOUND,
            f"required soil series are absent for {crop}/{country}",
        )
    if selected["region"].duplicated().any():
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.SERIES_METADATA_MISMATCH,
            "Weather soil consumer region identity is ambiguous",
        )
    if set(selected["unit"]) != {"%"} or set(selected["soil_depth"]) != {
        "0-100cm"
    }:
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.UNIT_MISMATCH,
            "Weather soil binding unit/depth semantics are invalid",
        )
    filters: list[tuple[str, str, object]] = [
        ("series_id", "in", selected["series_id"].tolist())
    ]
    if start_date is not None:
        filters.append(("business_date", ">=", start_date))
    if end_date is not None:
        filters.append(("business_date", "<=", end_date))
    columns = [
        "series_id",
        "provider_series_id",
        "source_locator",
        "source_table",
        "source_column",
        "business_date",
        "value_percent",
        "unit",
        "metric",
        "soil_depth",
        "extracted_at",
        "source_row_sha256",
    ]
    rows = pq.read_table(
        resolved.current.soil_observations_path, columns=columns, filters=filters
    ).to_pandas()
    if rows.empty:
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.SERIES_NOT_FOUND,
            f"required soil observations are absent for {crop}/{country}",
        )
    rows = rows.merge(
        selected[["series_id", "crop", "country", "region", "region_label"]],
        on="series_id",
        how="inner",
        validate="many_to_one",
    )
    if set(rows["unit"]) != {"%"} or set(rows["soil_depth"]) != {"0-100cm"}:
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.UNIT_MISMATCH,
            "Weather soil Current unit/depth semantics are invalid",
        )
    _require_regions(rows, regions, crop, country, "soil_moisture")
    _require_unique(rows, ["series_id", "business_date"])
    rows = rows.rename(
        columns={
            "business_date": "date",
            "value_percent": "value",
            "extracted_at": "source_updated_at",
        }
    )
    rows["region_type"] = "consumer_binding"
    rows["data_type"] = "observed"
    rows["model"] = "OBSERVED"
    rows["forecast_issue_date"] = pd.NaT
    rows["forecast_issue_status"] = "NOT_APPLICABLE"
    rows["forecast_run_id"] = "NOT_APPLICABLE"
    rows["forecast_run_at"] = pd.NaT
    rows["observation_interval"] = "daily"
    rows["aggregation"] = "source_value"
    rows["current_release_id"] = resolved.identity.release_id
    rows["current_manifest_sha256"] = resolved.identity.manifest_sha256
    rows["value_semantics"] = "canonical"
    rows = validate_weather_records(rows)
    return rows.sort_values(["date", "region", "series_id"]).reset_index(drop=True)


@lru_cache(maxsize=256)
def _read_normals_versioned(
    root: str,
    release_id: str,
    manifest_sha256: str,
    crop: str,
    country: str,
    metric: str,
    regions: tuple[str, ...],
) -> pd.DataFrame:
    resolved = _resolve_current_versioned(root, release_id, manifest_sha256)
    filters: list[tuple[str, str, object]] = [
        ("crop", "=", crop),
        ("country", "=", country),
        ("metric", "=", metric),
    ]
    if regions:
        filters.append(("region", "in", list(regions)))
    columns = [
        "series_id",
        "source_locator",
        "source_workbook_sha256",
        "source_sheet",
        "crop",
        "country",
        "region",
        "metric",
        "data_family",
        "month_day",
        "value",
        "unit",
        "baseline_label",
        "normal_period",
        "normal_period_status",
        "source_row_sha256",
    ]
    rows = pq.read_table(
        resolved.current.normals_path, columns=columns, filters=filters
    ).to_pandas()
    if rows.empty:
        return _empty_normals()
    if set(rows["data_family"]) != {"historical_climatology"}:
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.METRIC_MISMATCH,
            "Weather normal data-family identity is invalid",
        )
    if set(rows["unit"]) != {_METRIC_UNITS[metric]}:
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.UNIT_MISMATCH,
            f"Weather normal unit differs for {crop}/{country}/{metric}",
        )
    _require_unique(rows, ["series_id", "month_day"])
    rows = rows.rename(columns={"value": "normal_value"})
    rows["current_release_id"] = release_id
    rows["current_manifest_sha256"] = manifest_sha256
    return rows.sort_values(["metric", "region", "month_day"]).reset_index(drop=True)


def _require_regions(
    rows: pd.DataFrame,
    regions: tuple[str, ...],
    crop: str,
    country: str,
    metric: str,
) -> None:
    if not regions:
        return
    missing = sorted(set(regions) - set(rows["region"]))
    if missing:
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.SERIES_NOT_FOUND,
            f"required Weather regions are absent for {crop}/{country}/{metric}: "
            + ", ".join(missing),
        )


def _require_unique(rows: pd.DataFrame, keys: list[str]) -> None:
    if rows.duplicated(keys).any():
        raise PublicWeatherCurrentError(
            PublicWeatherCurrentErrorCode.INVALID_CURRENT_MANIFEST,
            "Weather Public Current contains duplicate stable keys",
        )


def _region_key(value: object) -> str:
    return "_".join(str(value).strip().lower().replace("-", " ").split())


def _empty_normals() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "series_id",
            "source_locator",
            "source_workbook_sha256",
            "source_sheet",
            "crop",
            "country",
            "region",
            "metric",
            "data_family",
            "month_day",
            "normal_value",
            "unit",
            "baseline_label",
            "normal_period",
            "normal_period_status",
            "source_row_sha256",
            "current_release_id",
            "current_manifest_sha256",
        ]
    )


__all__ = [
    "PublicWeatherCurrentError",
    "PublicWeatherCurrentErrorCode",
    "PublicWeatherCurrentIdentity",
    "PublicWeatherCurrentSnapshot",
    "clear_public_weather_current_cache",
    "load_public_weather_current",
    "resolve_weather_current_identity",
]
