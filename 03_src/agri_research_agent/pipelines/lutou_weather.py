"""Complete Public Weather producer for the current formal consumers."""

from __future__ import annotations

import errno
import hashlib
import json
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import perf_counter, sleep
from types import TracebackType

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import yaml

from agri_research_agent.data_sources.lutou.soil_moisture_live import (
    load_soil_moisture_series,
)
from agri_research_agent.data_sources.lutou.weather_live import (
    WeatherSeries,
    WeatherSourceCatalog,
    WeatherTable,
    build_soil_consumer_bindings,
    forecast_run_id,
    load_weather_source_catalog,
)
from agri_research_agent.shared.atomic_storage import atomic_write_json
from agri_research_agent.shared.file_identity import identify_file
from agri_research_agent.shared.immutable_candidate import (
    seal_immutable_candidate,
    validate_candidate_id,
)
from agri_research_agent.shared.runtime_context import (
    RuntimeContext,
    RuntimeMode,
    assert_runtime_write,
)
from agri_research_agent.weather.crop_weather import load_weather_config

from .lutou_goal_b import _file_identities, _read_json, _verify_files
from .lutou_goal_b_soil import CANONICAL_SCHEMA as SOIL_CANONICAL_SCHEMA
from .lutou_goal_b_soil import load_soil_current

LOOKBACK_DAYS = 31
DECIMAL_TYPE = pa.decimal128(38, 20)
STABLE_KEY = ("series_id", "valid_date", "forecast_run_id")
NORMAL_STABLE_KEY = ("series_id", "month_day")
SOURCE_POLICY_VERSION = "public-weather-current-consumer-scope/1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CLEANUP_MAX_ATTEMPTS = 4
_CLEANUP_BACKOFF_SECONDS = (0.05, 0.1, 0.2)

STANDARD_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.string(), nullable=False),
        pa.field("series_id", pa.string(), nullable=False),
        pa.field("provider_dataset_id", pa.string(), nullable=False),
        pa.field("provider_series_id", pa.string(), nullable=False),
        pa.field("provider", pa.string(), nullable=False),
        pa.field("origin_system", pa.string(), nullable=False),
        pa.field("acquisition_channel", pa.string(), nullable=False),
        pa.field("source_locator", pa.string(), nullable=False),
        pa.field("source_table", pa.string(), nullable=False),
        pa.field("source_column", pa.string(), nullable=False),
        pa.field("crop", pa.string(), nullable=False),
        pa.field("country", pa.string(), nullable=False),
        pa.field("region", pa.string(), nullable=False),
        pa.field("region_label", pa.string(), nullable=False),
        pa.field("region_type", pa.string(), nullable=False),
        pa.field("metric", pa.string(), nullable=False),
        pa.field("data_family", pa.string(), nullable=False),
        pa.field("forecast_model", pa.string(), nullable=False),
        pa.field("forecast_issue_date", pa.date32(), nullable=True),
        pa.field("forecast_issue_status", pa.string(), nullable=False),
        pa.field("forecast_run_id", pa.string(), nullable=False),
        pa.field("valid_date", pa.date32(), nullable=False),
        pa.field("raw_value_text", pa.string(), nullable=False),
        pa.field("source_value", DECIMAL_TYPE, nullable=True),
        pa.field("source_unit", pa.string(), nullable=False),
        pa.field("value", DECIMAL_TYPE, nullable=True),
        pa.field("unit", pa.string(), nullable=False),
        pa.field("observation_interval", pa.string(), nullable=False),
        pa.field("aggregation", pa.string(), nullable=False),
        pa.field("soil_depth", pa.string(), nullable=True),
        pa.field("transformation_id", pa.string(), nullable=False),
        pa.field("source_update_status", pa.string(), nullable=False),
        pa.field("extracted_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("query_sha256", pa.string(), nullable=False),
        pa.field("source_row_sha256", pa.string(), nullable=False),
        pa.field("is_numeric", pa.bool_(), nullable=False),
        pa.field("quality_status", pa.string(), nullable=False),
        pa.field("is_usable", pa.bool_(), nullable=False),
    ]
)

CANONICAL_SCHEMA = pa.schema(
    [
        field
        for field in STANDARD_SCHEMA
        if field.name not in {"is_numeric", "quality_status", "is_usable"}
    ]
    + [pa.field("source_policy_version", pa.string(), nullable=False)]
)

NORMAL_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.string(), nullable=False),
        pa.field("series_id", pa.string(), nullable=False),
        pa.field("provider", pa.string(), nullable=False),
        pa.field("origin_system", pa.string(), nullable=False),
        pa.field("acquisition_channel", pa.string(), nullable=False),
        pa.field("source_locator", pa.string(), nullable=False),
        pa.field("source_artifact_sha256", pa.string(), nullable=False),
        pa.field("source_workbook_sha256", pa.string(), nullable=False),
        pa.field("source_sheet", pa.string(), nullable=False),
        pa.field("crop", pa.string(), nullable=False),
        pa.field("country", pa.string(), nullable=False),
        pa.field("region", pa.string(), nullable=False),
        pa.field("metric", pa.string(), nullable=False),
        pa.field("data_family", pa.string(), nullable=False),
        pa.field("month_day", pa.string(), nullable=False),
        pa.field("source_value", DECIMAL_TYPE, nullable=False),
        pa.field("source_unit", pa.string(), nullable=False),
        pa.field("value", DECIMAL_TYPE, nullable=False),
        pa.field("unit", pa.string(), nullable=False),
        pa.field("baseline_label", pa.string(), nullable=False),
        pa.field("normal_period", pa.string(), nullable=True),
        pa.field("normal_period_status", pa.string(), nullable=False),
        pa.field("transformation_id", pa.string(), nullable=False),
        pa.field("source_row_sha256", pa.string(), nullable=False),
    ]
)


class LutouWeatherError(RuntimeError):
    pass


class LutouWeatherStageError(LutouWeatherError):
    def __init__(self, stage: str, exc: Exception) -> None:
        super().__init__(f"Weather {stage} failed: {type(exc).__name__}: {exc}")
        self.stage = stage
        self.cleanup_failure = getattr(exc, "cleanup_failure", None)
        self.cleanup_evidence: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class WeatherCurrent:
    release_id: str
    directory: Path
    manifest: Mapping[str, object]
    observations_path: Path
    soil_observations_path: Path
    normals_path: Path


@dataclass(frozen=True, slots=True)
class _BaselineEvidence:
    table: pa.Table
    row_count: int
    series_count: int
    content_sha256: str
    sources: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class WeatherRunResult:
    run_id: str
    mode: str
    candidate_directory: Path
    canonical_directory: Path
    current_directory: Path
    candidate_manifest: Mapping[str, object]
    canonical_manifest: Mapping[str, object]
    current_manifest: Mapping[str, object]
    promoted: bool
    async_reports: Mapping[str, object] = field(default_factory=dict)


def validate_weather_normal_baselines(
    policy_path: str | Path, baseline_root: str | Path
) -> dict[str, object]:
    baseline = _baseline_evidence(policy_path, baseline_root)
    return {
        "row_count": baseline.row_count,
        "series_count": baseline.series_count,
        "content_sha256": baseline.content_sha256,
        "sources": list(baseline.sources),
    }


def run_lutou_weather(
    client: object,
    *,
    runtime: RuntimeContext,
    run_id: str,
    as_of_date: date,
    full_load: bool,
    policy_path: str | Path,
    baseline_root: str | Path,
    source_catalog: WeatherSourceCatalog | None = None,
    failure_hook: str | None = None,
    async_report_sink: dict | None = None,
) -> WeatherRunResult:
    safe_run_id = validate_candidate_id(run_id)
    _require_runtime(runtime)
    if type(as_of_date) is not date:
        raise ValueError("Weather as-of date must be an exact date")
    public_root = assert_runtime_write(
        runtime, runtime.runtime_root / "public-market-data" / "lutou-weather"
    )
    public_root.mkdir(parents=True, exist_ok=True)
    current = load_weather_current(
        public_root, allow_legacy_without_normals=not full_load
    )
    if full_load and current is not None:
        raise LutouWeatherError("Weather full load refuses an existing Current")
    if not full_load and current is None:
        raise LutouWeatherError("Weather incremental requires an existing Current")
    catalog = source_catalog or load_weather_source_catalog(
        client,
        policy_path,  # type: ignore[arg-type]
    )
    if catalog.observation_lookback_days != LOOKBACK_DAYS:
        raise LutouWeatherError("Weather observation lookback policy changed")
    observation_start = (
        min(
            item.min_date
            for item in catalog.tables
            if item.data_family == "observation"
        )
        if current is None
        else date.fromisoformat(
            str(current.manifest["source_max_dates"]["observation"])
        )
        - timedelta(days=LOOKBACK_DAYS)
    )
    mode = (
        "full"
        if current is None
        else "incremental-31-day-observation-plus-full-forecast"
    )
    try:
        soil = _soil_evidence(runtime, policy_path)
    except Exception as exc:
        if isinstance(exc, LutouWeatherStageError):
            raise
        raise LutouWeatherStageError("SOIL_EVIDENCE", exc) from exc
    baseline = _baseline_evidence(policy_path, baseline_root)

    candidate_directory, candidate_manifest = _seal_candidate(
        client,
        runtime,
        public_root,
        safe_run_id,
        mode,
        observation_start,
        as_of_date,
        catalog,
        soil,
        baseline,
        failure_hook,
    )
    canonical_directory, _canonical_manifest = _seal_canonical_window(
        runtime,
        public_root,
        safe_run_id,
        candidate_directory,
        candidate_manifest,
        catalog,
        failure_hook,
    )
    merged_directory, merged_manifest = _seal_merged_canonical(
        runtime,
        public_root,
        safe_run_id,
        canonical_directory,
        current,
        catalog,
        observation_start,
        soil,
        baseline,
    )
    from .async_contract_rollout import weather_reports
    # Roll out the existing FULL DAILY incremental mode, not initial seed policy.
    async_reports = {} if full_load else weather_reports(runtime, safe_run_id, current, candidate_directory, merged_directory, catalog, as_of_date, LutouWeatherError, report_sink=async_report_sink)
    if current is not None and (
        str(current.manifest["content_sha256"])
        == str(merged_manifest["content_sha256"])
        and str(current.manifest["soil_current"]["manifest_sha256"])
        == str(soil["manifest_sha256"])
        and int(current.manifest.get("soil_binding_count", -1)) == len(soil["bindings"])
        and str(current.manifest.get("normal_content_sha256", ""))
        == baseline.content_sha256
    ):
        return WeatherRunResult(
            safe_run_id,
            mode,
            candidate_directory,
            merged_directory,
            current.directory,
            candidate_manifest,
            merged_manifest,
            current.manifest,
            False,
            async_reports=async_reports,
        )
    current_directory, current_manifest = _promote(
        runtime,
        public_root,
        safe_run_id,
        merged_directory,
        merged_manifest,
        candidate_manifest,
        soil,
        baseline,
        failure_hook,
    )
    return WeatherRunResult(
        safe_run_id,
        mode,
        candidate_directory,
        merged_directory,
        current_directory,
        candidate_manifest,
        merged_manifest,
        current_manifest,
        True,
        async_reports=async_reports,
    )


def load_weather_current(
    public_root: str | Path,
    *,
    allow_legacy_without_normals: bool = False,
) -> WeatherCurrent | None:
    root = Path(public_root)
    pointer = root / "current.json"
    if not pointer.exists():
        return None
    value = _read_json(pointer)
    release_id = validate_candidate_id(str(value["release_id"]))
    directory = root / "releases" / release_id
    manifest_path = directory / "manifest.json"
    manifest = _read_json(manifest_path)
    if identify_file(manifest_path).sha256 != value["manifest_sha256"]:
        raise LutouWeatherError("Weather Current pointer identity mismatch")
    _verify_files(directory, manifest["files"])
    observations = directory / "observations.parquet"
    soil_observations = directory / "soil_observations.parquet"
    normals = directory / "normals.parquet"
    if pq.read_schema(observations) != CANONICAL_SCHEMA:
        raise LutouWeatherError("Weather Current schema is invalid")
    if pq.read_schema(soil_observations) != SOIL_CANONICAL_SCHEMA:
        raise LutouWeatherError("Weather soil Current schema is invalid")
    if normals.exists():
        if pq.read_schema(normals) != NORMAL_SCHEMA:
            raise LutouWeatherError("Weather normal Current schema is invalid")
    elif not allow_legacy_without_normals:
        raise LutouWeatherError("Weather normal Current is missing")
    return WeatherCurrent(
        release_id, directory, manifest, observations, soil_observations, normals
    )


def _seal_candidate(
    client: object,
    runtime: RuntimeContext,
    public_root: Path,
    run_id: str,
    mode: str,
    observation_start: date,
    as_of_date: date,
    catalog: WeatherSourceCatalog,
    soil: Mapping[str, object],
    baseline: _BaselineEvidence,
    failure_hook: str | None,
) -> tuple[Path, dict[str, object]]:
    result: dict[str, object] = {}
    seed_partition_root = None
    if mode == "full":
        seed_partition_root = assert_runtime_write(
            runtime,
            public_root.parent
            / "lutou-weather-seed-partitions"
            / _seed_partition_set_id(catalog, observation_start, as_of_date),
        )

    def build(directory: Path) -> dict[str, object]:
        stats = _extract_standard(
            client,
            catalog,
            observation_start,
            as_of_date,
            directory / "standard.parquet",
            failure_hook,
            seed_partition_root=seed_partition_root,
            cleanup_evidence_path=assert_runtime_write(
                runtime,
                public_root
                / "failure-evidence"
                / f"{run_id}-table-partition-cleanup.json",
            ),
            candidate_run_id=run_id,
        )
        if failure_hook == "candidate_qc":
            stats["quality_passed"] = False
        if not stats["quality_passed"]:
            raise LutouWeatherError("Weather Candidate quality gate failed")
        source_catalog = _source_catalog_document(catalog, stats, soil, baseline)
        pq.write_table(
            baseline.table, directory / "normals.parquet", compression="zstd"
        )
        _write_json(directory / "source_catalog.json", source_catalog)
        manifest = {
            "schema_version": "lutou-public-weather-candidate/1",
            "run_id": run_id,
            "mode": mode,
            "source": "lutou",
            "scope": "current-weather-consumers",
            "connection_proof": client.proof.safe_manifest_fields(),
            "query_windows": {
                "observation_start": observation_start.isoformat(),
                "observation_end": as_of_date.isoformat(),
                "forecast_start": as_of_date.isoformat(),
                "forecast_end": (
                    as_of_date
                    + timedelta(days=catalog.forecast_horizon_days - 1)
                ).isoformat(),
                "forecast_horizon_days": catalog.forecast_horizon_days,
                "forecast_policy": "sealed-current-valid-horizon",
            },
            "source_catalog": catalog.safe_manifest_fields(),
            "source_max_dates": stats["source_max_dates"],
            "forecast": {
                "issue_date": None,
                "issue_date_status": "SOURCE_NOT_PROVIDED",
                "run_identity": "source_content_sha256",
                "valid_date_min": stats["forecast_valid_date_min"],
                "valid_date_max": stats["forecast_valid_date_max"],
            },
            "quality": stats,
            "normal_baselines": {
                "row_count": baseline.row_count,
                "series_count": baseline.series_count,
                "content_sha256": baseline.content_sha256,
                "stable_key_duplicate_count": 0,
                "sources": list(baseline.sources),
            },
            "soil_current": _safe_soil_manifest(soil),
            "files": _file_identities(
                directory,
                ("standard.parquet", "normals.parquet", "source_catalog.json"),
            ),
        }
        _write_json(directory / "manifest.json", manifest)
        _verify_files(directory, manifest["files"])
        result.update(manifest)
        return manifest

    directory, _ = seal_immutable_candidate(
        assert_runtime_write(runtime, public_root / "candidates"), run_id, build
    )
    return directory, result


def _extract_standard(
    client: object,
    catalog: WeatherSourceCatalog,
    observation_start: date,
    as_of_date: date,
    destination: Path,
    failure_hook: str | None,
    *,
    seed_partition_root: Path | None,
    cleanup_evidence_path: Path,
    candidate_run_id: str,
) -> dict[str, object]:
    writer = pq.ParquetWriter(destination, STANDARD_SCHEMA, compression="zstd")
    row_count = usable_count = missing_count = non_numeric_count = 0
    negative_rainfall_count = duplicate_date_count = 0
    series_seen: set[str] = set()
    observation_maxima: list[date] = []
    forecast_minima: list[date] = []
    forecast_maxima: list[date] = []
    table_counts: dict[str, int] = {}
    table_nulls: dict[str, int] = {}
    table_query_performance: list[dict[str, object]] = []
    plan_fields = []
    partition_manifests: list[dict[str, object]] = []
    resumed_partition_count = 0
    local_partition_root = destination.parent / ".table-partitions"
    partition_root = seed_partition_root or local_partition_root
    partition_root.mkdir(parents=True, exist_ok=True)
    completed_windows = (
        _completed_seed_partition_windows(partition_root)
        if seed_partition_root is not None
        else {}
    )
    query_partitions = [
        (table, partition_start, partition_end)
        for table in catalog.tables
        for partition_start, partition_end in _table_query_windows(
            table,
            observation_start
            if table.data_family == "observation"
            else as_of_date,
            as_of_date
            if table.data_family == "observation"
            else as_of_date + timedelta(days=catalog.forecast_horizon_days - 1),
            resumable_seed=seed_partition_root is not None,
            completed_windows=completed_windows.get(
                (table.source_table, table.query.sha256), ()
            ),
        )
    ]
    primary_error: tuple[BaseException, TracebackType | None] | None = None
    try:
        for table, start, end in query_partitions:
            partition_id = _table_partition_id(table, start, end)
            partition_directory = partition_root / partition_id
            reused = partition_directory.is_dir()
            if reused:
                partition_manifest = _load_table_partition(
                    partition_directory, partition_id, table, start, end
                )
                resumed_partition_count += 1
            else:
                partition_manifest = {}

                def build_partition(
                    directory: Path,
                    *,
                    table: WeatherTable = table,
                    start: date = start,
                    end: date = end,
                    partition_id: str = partition_id,
                ) -> dict[str, object]:
                    stats = _extract_table_standard(
                        client,
                        table,
                        start,
                        end,
                        directory / "standard.parquet",
                        failure_hook,
                    )
                    manifest = {
                        "schema_version": "lutou-weather-seed-partition/1",
                        "partition_id": partition_id,
                        "status": "COMPLETE",
                        "source_identity": {
                            "source_schema": catalog.source_schema,
                            "source_table": table.source_table,
                            "source_series_ids": [
                                item.series_id for item in table.series
                            ],
                        },
                        "data_family": table.data_family,
                        "forecast_model": table.model,
                        "query_window": {
                            "start": start.isoformat(),
                            "end": end.isoformat(),
                        },
                        "query": table.query.safe_manifest_fields(),
                        "row_count": stats["standard_row_count"],
                        "source_row_count": stats["source_row_count"],
                        "min_date": stats["min_date"],
                        "max_date": stats["max_date"],
                        "data_sha256": identify_file(
                            directory / "standard.parquet"
                        ).sha256,
                        "completed_at": datetime.now(UTC).isoformat(),
                        "quality": stats,
                        "files": _file_identities(
                            directory, ("standard.parquet",)
                        ),
                    }
                    _write_json(directory / "manifest.json", manifest)
                    return manifest

                partition_directory, partition_manifest = seal_immutable_candidate(
                    partition_root, partition_id, build_partition
                )
            partition_table = pq.ParquetFile(
                partition_directory / "standard.parquet"
            )
            try:
                for row_group in range(partition_table.num_row_groups):
                    writer.write_table(partition_table.read_row_group(row_group))
            finally:
                partition_table.close()
            stats = partition_manifest["quality"]
            row_count += int(stats["standard_row_count"])
            usable_count += int(stats["usable_row_count"])
            missing_count += int(stats["missing_value_count"])
            non_numeric_count += int(stats["non_numeric_count"])
            negative_rainfall_count += int(stats["negative_rainfall_count"])
            duplicate_date_count += int(stats["duplicate_source_date_count"])
            series_seen.update(str(item) for item in stats["series_ids"])
            if table.data_family == "observation" and stats["max_date"]:
                observation_maxima.append(date.fromisoformat(str(stats["max_date"])))
            if table.data_family == "forecast":
                if stats["min_date"]:
                    forecast_minima.append(date.fromisoformat(str(stats["min_date"])))
                if stats["max_date"]:
                    forecast_maxima.append(date.fromisoformat(str(stats["max_date"])))
            table_counts[table.source_table] = table_counts.get(
                table.source_table, 0
            ) + int(stats["source_row_count"])
            table_nulls[table.source_table] = table_nulls.get(
                table.source_table, 0
            ) + int(stats["missing_value_count"])
            performance = dict(stats["query_performance"])
            performance["reused_seed_partition"] = reused
            table_query_performance.append(performance)
            plan_fields.append(dict(stats["query_plan"]))
            partition_manifests.append(
                {
                    "partition_id": partition_id,
                    "source_table": table.source_table,
                    "status": partition_manifest["status"],
                    "row_count": partition_manifest["row_count"],
                    "min_date": partition_manifest["min_date"],
                    "max_date": partition_manifest["max_date"],
                    "query_sha256": table.query.sha256,
                    "query_window": {
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                    },
                    "data_sha256": partition_manifest["data_sha256"],
                    "completed_at": partition_manifest["completed_at"],
                    "reused": reused,
                }
            )
    except BaseException as exc:
        primary_error = (exc, exc.__traceback__)
    try:
        writer.close()
    except BaseException as exc:
        if primary_error is None:
            primary_error = (exc, exc.__traceback__)
    cleanup_telemetry: Mapping[str, object] = {
        "status": "NOT_REQUIRED", "attempt_count": 0, "retry_count": 0,
    }
    if seed_partition_root is None and local_partition_root.exists():
        try:
            cleanup_telemetry = _cleanup_table_partitions(
                outer_building=destination.parent,
                target=local_partition_root,
                candidate_run_id=candidate_run_id,
                evidence_path=cleanup_evidence_path,
            )
        except LutouWeatherStageError as cleanup_error:
            if primary_error is None:
                raise
            primary_exception = primary_error[0]
            if (
                isinstance(primary_exception, Exception)
                and cleanup_error.cleanup_evidence is not None
            ):
                primary_exception.cleanup_failure = cleanup_error.cleanup_evidence  # type: ignore[attr-defined]
    if primary_error is not None:
        raise primary_error[0].with_traceback(primary_error[1])
    expected = {item.series_id for item in catalog.series}
    quality_passed = series_seen == expected and duplicate_date_count == 0
    return {
        "quality_status": "PASS" if quality_passed else "FAIL",
        "quality_passed": quality_passed,
        "table_partition_cleanup": dict(cleanup_telemetry),
        "standard_row_count": row_count,
        "usable_row_count": usable_count,
        "retained_exception_count": row_count - usable_count,
        "missing_value_count": missing_count,
        "non_numeric_count": non_numeric_count,
        "negative_rainfall_count": negative_rainfall_count,
        "duplicate_source_date_count": duplicate_date_count,
        "stable_key_duplicate_count": 0,
        "expected_series_count": len(expected),
        "observed_series_count": len(series_seen),
        "table_source_row_counts": dict(sorted(table_counts.items())),
        "table_null_cell_counts": dict(sorted(table_nulls.items())),
        "table_query_performance": sorted(
            table_query_performance,
            key=lambda item: (-float(item["duration_seconds"]), item["source_table"]),
        ),
        "observation_query_duration_seconds": round(
            sum(
                float(item["duration_seconds"])
                for item in table_query_performance
                if item["data_family"] == "observation"
            ),
            6,
        ),
        "forecast_query_duration_seconds": round(
            sum(
                float(item["duration_seconds"])
                for item in table_query_performance
                if item["data_family"] == "forecast"
            ),
            6,
        ),
        "query_plans": plan_fields,
        "partition_status": partition_manifests,
        "required_partition_count": len(query_partitions),
        "completed_partition_count": len(partition_manifests),
        "resumed_partition_count": resumed_partition_count,
        "resumable_seed_enabled": seed_partition_root is not None,
        "source_max_dates": {
            "observation": max(observation_maxima).isoformat(),
            "forecast_valid": max(forecast_maxima).isoformat(),
        },
        "forecast_valid_date_min": min(forecast_minima).isoformat(),
        "forecast_valid_date_max": max(forecast_maxima).isoformat(),
    }


def _remaining_cleanup_entries(target: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    try:
        paths = sorted(
            target.rglob("*"),
            key=lambda item: item.relative_to(target).as_posix(),
        )
    except OSError as exc:
        return [
            {
                "relative_path": "<enumeration-failed>",
                "entry_type": type(exc).__name__,
            }
        ]
    for path in paths[:200]:
        try:
            entry_type = (
                "directory"
                if path.is_dir()
                else "file"
                if path.is_file()
                else "other"
            )
        except OSError:
            entry_type = "unavailable"
        entries.append(
            {
                "relative_path": path.relative_to(target).as_posix(),
                "entry_type": entry_type,
            }
        )
    if len(paths) > 200:
        entries.append(
            {"relative_path": "<truncated>", "entry_type": "metadata"}
        )
    return entries


def _cleanup_table_partitions(
    *,
    outer_building: Path,
    target: Path,
    candidate_run_id: str,
    evidence_path: Path,
    max_attempts: int = _CLEANUP_MAX_ATTEMPTS,
    backoff_seconds: tuple[float, ...] = _CLEANUP_BACKOFF_SECONDS,
    rmtree: Callable[[Path], None] = shutil.rmtree,
    sleep: Callable[[float], None] = sleep,
) -> Mapping[str, object]:
    safe_run_id = validate_candidate_id(candidate_run_id)
    outer = outer_building.resolve(strict=False)
    owned_target = target.resolve(strict=False)
    expected_target = (outer / ".table-partitions").resolve(strict=False)
    weather_root = outer.parent.parent
    expected_evidence_parent = (weather_root / "failure-evidence").resolve(
        strict=False
    )
    if (
        outer.parent.name != "candidates"
        or weather_root.name != "lutou-weather"
        or not outer.name.startswith(f".building-{safe_run_id}-")
        or owned_target != expected_target
        or evidence_path.parent.resolve(strict=False) != expected_evidence_parent
        or outer_building.is_symlink()
        or target.is_symlink()
    ):
        raise LutouWeatherStageError(
            "WEATHER_TABLE_PARTITION_CLEANUP",
            ValueError(
                "cleanup target is not the owned current-run table partition directory"
            ),
        )
    if max_attempts < 1 or len(backoff_seconds) < max_attempts - 1:
        raise ValueError("cleanup retry policy is invalid")

    attempts: list[dict[str, object]] = []
    for attempt in range(1, max_attempts + 1):
        try:
            rmtree(owned_target)
            result: dict[str, object] = {
                "status": "PASS",
                "operation": "shutil.rmtree",
                "owned_relative_path": ".table-partitions",
                "attempt_count": attempt,
                "retry_count": attempt - 1,
            }
            if attempts:
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(
                    evidence_path,
                    {
                        "schema_version": "lutou-weather-cleanup-evidence/1",
                        "stage": "WEATHER_TABLE_PARTITION_CLEANUP",
                        **result,
                        "attempts": attempts,
                        "completed_at": datetime.now(UTC).isoformat(),
                    },
                )
                result["evidence_file"] = evidence_path.name
            return result
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            transient = winerror == 145 or exc.errno == errno.ENOTEMPTY
            attempts.append(
                {
                    "attempt": attempt,
                    "operation": "shutil.rmtree",
                    "owned_relative_path": ".table-partitions",
                    "exception_type": type(exc).__name__,
                    "winerror": winerror,
                    "errno": exc.errno,
                    "remaining_entries": _remaining_cleanup_entries(owned_target),
                }
            )
            exhausted = attempt == max_attempts
            if not transient or exhausted:
                retry_count = attempt - 1
                evidence = {
                    "schema_version": "lutou-weather-cleanup-evidence/1",
                    "stage": "WEATHER_TABLE_PARTITION_CLEANUP",
                    "status": "FAILED",
                    "operation": "shutil.rmtree",
                    "owned_relative_path": ".table-partitions",
                    "attempt_count": attempt,
                    "retry_count": retry_count,
                    "attempts": attempts,
                    "completed_at": datetime.now(UTC).isoformat(),
                }
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(evidence_path, evidence)
                safe_error = OSError(
                    exc.errno,
                    f"{exc}; operation=shutil.rmtree; "
                    f"owned_relative_path=.table-partitions; "
                    f"retry_count={retry_count}",
                    ".table-partitions",
                    winerror,
                )
                stage_error = LutouWeatherStageError(
                    "WEATHER_TABLE_PARTITION_CLEANUP", safe_error
                )
                stage_error.cleanup_evidence = {
                    "stage": "WEATHER_TABLE_PARTITION_CLEANUP",
                    "exception_type": type(exc).__name__,
                    "winerror": winerror,
                    "operation": "shutil.rmtree",
                    "owned_relative_path": ".table-partitions",
                    "retry_count": retry_count,
                    "evidence_file": evidence_path.name,
                }
                raise stage_error from exc
            sleep(backoff_seconds[attempt - 1])
    raise AssertionError("cleanup retry loop terminated unexpectedly")


def _table_query_windows(
    table: object,
    start: date,
    end: date,
    *,
    resumable_seed: bool,
    completed_windows: tuple[tuple[date, date], ...] = (),
) -> tuple[tuple[date, date], ...]:
    if start > end:
        raise LutouWeatherError("Weather query window is inverted")
    if not resumable_seed or table.data_family != "observation":
        return ((start, end),)
    windows: list[tuple[date, date]] = []
    cursor = start
    ordered_completed = sorted(
        {
            window
            for window in completed_windows
            if start <= window[0] <= window[1] <= end
        },
        key=lambda window: (window[0], -window[1].toordinal()),
    )
    for existing_start, existing_end in ordered_completed:
        if existing_start < cursor:
            continue
        if existing_start != cursor:
            break
        windows.append((existing_start, existing_end))
        cursor = existing_end + timedelta(days=1)
        if cursor > end:
            return tuple(windows)
    while cursor <= end:
        window_end = min(end, date(cursor.year, 12, 31))
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return tuple(windows)


def _completed_seed_partition_windows(
    root: Path,
) -> dict[tuple[str, str], tuple[tuple[date, date], ...]]:
    output: dict[tuple[str, str], list[tuple[date, date]]] = {}
    for manifest_path in root.glob("table-*/manifest.json"):
        try:
            manifest = _read_json(manifest_path)
            if (
                manifest.get("schema_version")
                != "lutou-weather-seed-partition/1"
                or manifest.get("status") != "COMPLETE"
            ):
                continue
            identity = manifest["source_identity"]
            query = manifest["query"]
            window = manifest["query_window"]
            source_table = str(identity["source_table"])
            query_sha256 = str(query["query_sha256"])
            start = date.fromisoformat(str(window["start"]))
            end = date.fromisoformat(str(window["end"]))
            expected_id = str(manifest["partition_id"])
            if manifest_path.parent.name != expected_id:
                continue
            output.setdefault((source_table, query_sha256), []).append((start, end))
        except (KeyError, TypeError, ValueError, LutouWeatherError):
            continue
    return {
        key: tuple(sorted(value))
        for key, value in output.items()
    }


def _seed_partition_set_id(
    catalog: WeatherSourceCatalog, observation_start: date, as_of_date: date
) -> str:
    payload = {
        "source_policy_version": SOURCE_POLICY_VERSION,
        "source_schema": catalog.source_schema,
        "observation_start": observation_start.isoformat(),
        "observation_end": as_of_date.isoformat(),
        "tables": [
            {
                "source_table": item.source_table,
                "query_sha256": item.query.sha256,
                "source_min_date": item.min_date.isoformat(),
                "source_max_date": item.max_date.isoformat(),
            }
            for item in catalog.tables
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"seed-{hashlib.sha256(encoded).hexdigest()}"


def _table_partition_id(table: object, start: date, end: date) -> str:
    payload = {
        "source_table": table.source_table,
        "query_sha256": table.query.sha256,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "series_ids": [item.series_id for item in table.series],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"table-{hashlib.sha256(encoded).hexdigest()}"


def _load_table_partition(
    directory: Path,
    partition_id: str,
    table: object,
    start: date,
    end: date,
) -> dict[str, object]:
    manifest = _read_json(directory / "manifest.json")
    if (
        manifest.get("schema_version") != "lutou-weather-seed-partition/1"
        or manifest.get("partition_id") != partition_id
        or manifest.get("status") != "COMPLETE"
        or manifest.get("query_window")
        != {"start": start.isoformat(), "end": end.isoformat()}
        or manifest.get("query", {}).get("query_sha256") != table.query.sha256
    ):
        raise LutouWeatherError("Weather seed partition identity is invalid")
    identity = manifest.get("source_identity", {})
    if (
        identity.get("source_table") != table.source_table
        or identity.get("source_series_ids")
        != [item.series_id for item in table.series]
    ):
        raise LutouWeatherError("Weather seed partition source identity changed")
    _verify_files(directory, manifest["files"])
    data = directory / "standard.parquet"
    if pq.read_schema(data) != STANDARD_SCHEMA:
        raise LutouWeatherError("Weather seed partition schema is invalid")
    if identify_file(data).sha256 != manifest.get("data_sha256"):
        raise LutouWeatherError("Weather seed partition data identity changed")
    return manifest


def _extract_table_standard(
    client: object,
    table: object,
    start: date,
    end: date,
    destination: Path,
    failure_hook: str | None,
) -> dict[str, object]:
    if (
        failure_hook == "rainfall_extraction"
        and table.series[0].metric == "precipitation"
    ):
        raise LutouWeatherError("injected rainfall extraction failure")
    if failure_hook == "temperature_extraction" and table.series[
        0
    ].metric.startswith("temperature"):
        raise LutouWeatherError("injected temperature extraction failure")
    if failure_hook == "forecast_extraction" and table.data_family == "forecast":
        raise LutouWeatherError("injected forecast extraction failure")
    query_started = perf_counter()
    plan, batches = client.plan_stream(table.query, start, end)
    source_rows: list[Mapping[str, object]] = []
    extracted_at: datetime | None = None
    for batch in batches:
        source_rows.extend(batch.rows)
        extracted_at = batch.extracted_at
    if extracted_at is None:
        extracted_at = datetime.now(UTC)
    query_duration_seconds = round(perf_counter() - query_started, 6)
    dates = [_exact_date(row.get(table.date_column)) for row in source_rows]
    duplicate_date_count = len(dates) - len(set(dates))
    if duplicate_date_count:
        raise LutouWeatherError("Weather source contains duplicate table dates")
    if table.data_family == "observation":
        run_id = "NOT_APPLICABLE"
        issue_status = "NOT_APPLICABLE"
    else:
        run_id = forecast_run_id(table, tuple(source_rows))
        issue_status = "SOURCE_NOT_PROVIDED"
    if failure_hook == "forecast_collision" and table.data_family == "forecast":
        run_id = "injected-collision"
        source_rows = [*source_rows, *source_rows[:1]]
    row_count = usable_count = missing_count = non_numeric_count = 0
    negative_rainfall_count = table_null_count = 0
    series_seen: set[str] = set()
    writer = pq.ParquetWriter(destination, STANDARD_SCHEMA, compression="zstd")
    try:
        for series in table.series:
            rows = []
            seen_dates: set[date] = set()
            for source in source_rows:
                valid_date = _exact_date(source.get(table.date_column))
                if valid_date in seen_dates:
                    raise LutouWeatherError("Weather forecast stable-key collision")
                seen_dates.add(valid_date)
                raw = source.get(series.source_column)
                raw_text = "" if raw is None else str(raw)
                numeric = _decimal(raw)
                if raw is None or (isinstance(raw, str) and not raw.strip()):
                    quality = "MISSING_VALUE"
                    usable = False
                    missing_count += 1
                    table_null_count += 1
                elif numeric is None:
                    quality = "NON_NUMERIC"
                    usable = False
                    non_numeric_count += 1
                elif series.metric == "precipitation" and numeric < 0:
                    quality = "NEGATIVE_RAINFALL"
                    usable = False
                    negative_rainfall_count += 1
                else:
                    quality = "PASS"
                    usable = True
                rows.append(
                    {
                        "schema_version": "lutou-public-weather-standard/1",
                        "series_id": series.series_id,
                        "provider_dataset_id": series.provider_dataset_id,
                        "provider_series_id": series.provider_series_id,
                        "provider": "UNKNOWN",
                        "origin_system": "lutou",
                        "acquisition_channel": "direct_database",
                        "source_locator": (
                            "database:lutou/schema:天气2.0/relation:"
                            f"{series.source_table}"
                        ),
                        "source_table": series.source_table,
                        "source_column": series.source_column,
                        "crop": series.crop,
                        "country": series.country,
                        "region": series.region,
                        "region_label": series.region_label,
                        "region_type": series.region_type,
                        "metric": series.metric,
                        "data_family": series.data_family,
                        "forecast_model": series.model,
                        "forecast_issue_date": None,
                        "forecast_issue_status": issue_status,
                        "forecast_run_id": run_id,
                        "valid_date": valid_date,
                        "raw_value_text": raw_text,
                        "source_value": numeric,
                        "source_unit": series.source_unit,
                        "value": numeric,
                        "unit": series.unit,
                        "observation_interval": series.interval,
                        "aggregation": series.aggregation,
                        "soil_depth": None,
                        "transformation_id": series.transformation_id,
                        "source_update_status": "SOURCE_NOT_PROVIDED",
                        "extracted_at": extracted_at,
                        "query_sha256": table.query.sha256,
                        "source_row_sha256": _row_hash(
                            series, valid_date, run_id, raw_text
                        ),
                        "is_numeric": numeric is not None,
                        "quality_status": quality,
                        "is_usable": usable,
                    }
                )
                row_count += 1
                usable_count += int(usable)
            writer.write_table(pa.Table.from_pylist(rows, schema=STANDARD_SCHEMA))
            series_seen.add(series.series_id)
    finally:
        writer.close()
    return {
        "standard_row_count": row_count,
        "usable_row_count": usable_count,
        "retained_exception_count": row_count - usable_count,
        "missing_value_count": missing_count,
        "non_numeric_count": non_numeric_count,
        "negative_rainfall_count": negative_rainfall_count,
        "duplicate_source_date_count": duplicate_date_count,
        "series_ids": sorted(series_seen),
        "source_row_count": len(source_rows),
        "table_null_cell_count": table_null_count,
        "min_date": min(dates).isoformat() if dates else None,
        "max_date": max(dates).isoformat() if dates else None,
        "query_plan": plan.safe_manifest_fields(),
        "query_performance": {
            "source_table": table.source_table,
            "data_family": table.data_family,
            "forecast_model": table.model,
            "query_sha256": table.query.sha256,
            "source_row_count": len(source_rows),
            "duration_seconds": query_duration_seconds,
        },
    }


def _seal_canonical_window(
    runtime: RuntimeContext,
    public_root: Path,
    run_id: str,
    candidate_directory: Path,
    candidate_manifest: Mapping[str, object],
    catalog: WeatherSourceCatalog,
    failure_hook: str | None,
) -> tuple[Path, dict[str, object]]:
    result: dict[str, object] = {}

    def build(directory: Path) -> dict[str, object]:
        source = pq.ParquetFile(candidate_directory / "standard.parquet")
        destination = directory / "window.parquet"
        writer = pq.ParquetWriter(destination, CANONICAL_SCHEMA, compression="zstd")
        row_count = 0
        try:
            for index in range(source.num_row_groups):
                table = source.read_row_group(index)
                usable = table.filter(pc.equal(table["is_usable"], True))
                if not usable.num_rows:
                    continue
                columns = [
                    usable[field.name]
                    for field in STANDARD_SCHEMA
                    if field.name not in {"is_numeric", "quality_status", "is_usable"}
                ]
                columns.append(
                    pa.array(
                        [SOURCE_POLICY_VERSION] * usable.num_rows,
                        type=pa.string(),
                    )
                )
                output = pa.Table.from_arrays(columns, schema=CANONICAL_SCHEMA)
                writer.write_table(output)
                row_count += output.num_rows
        finally:
            writer.close()
        if failure_hook == "canonical_collision":
            raise LutouWeatherError("injected Weather canonical collision")
        manifest = {
            "schema_version": "lutou-public-weather-canonical-window/1",
            "run_id": run_id,
            "candidate_manifest_sha256": identify_file(
                candidate_directory / "manifest.json"
            ).sha256,
            "row_count": row_count,
            "series_count": len(catalog.series),
            "stable_key_duplicate_count": 0,
            "source_policy_version": SOURCE_POLICY_VERSION,
            "files": _file_identities(directory, ("window.parquet",)),
        }
        _write_json(directory / "manifest.json", manifest)
        _verify_files(directory, manifest["files"])
        result.update(manifest)
        return manifest

    directory, _ = seal_immutable_candidate(
        assert_runtime_write(runtime, public_root / "canonical-windows"), run_id, build
    )
    return directory, result


def _seal_merged_canonical(
    runtime: RuntimeContext,
    public_root: Path,
    run_id: str,
    window_directory: Path,
    current: WeatherCurrent | None,
    catalog: WeatherSourceCatalog,
    observation_start: date,
    soil: Mapping[str, object],
    baseline: _BaselineEvidence,
) -> tuple[Path, dict[str, object]]:
    result: dict[str, object] = {}

    def build(directory: Path) -> dict[str, object]:
        destination = directory / "observations.parquet"
        writer = pq.ParquetWriter(destination, CANONICAL_SCHEMA, compression="zstd")
        digest = hashlib.sha256()
        row_count = 0
        stable_key_duplicate_count = 0
        forecast_runs: set[str] = set()
        previous_reader = (
            _SeriesParquetReader(current.observations_path)
            if current is not None
            else None
        )
        window_reader = _SeriesParquetReader(window_directory / "window.parquet")
        try:
            for series in catalog.series:
                previous = (
                    previous_reader.read(series.series_id)
                    if previous_reader is not None
                    else pa.Table.from_pylist([], schema=CANONICAL_SCHEMA)
                )
                window = window_reader.read(series.series_id)
                merged = _merge_series(previous, window, series, observation_start)
                if merged.num_rows:
                    identity_rows = merged.select(
                        list(STABLE_KEY) + ["source_row_sha256"]
                    ).to_pylist()
                    unique_keys = {
                        tuple(row[field] for field in STABLE_KEY)
                        for row in identity_rows
                    }
                    duplicates = len(identity_rows) - len(unique_keys)
                    stable_key_duplicate_count += duplicates
                    if duplicates:
                        raise LutouWeatherError(
                            "Weather merged Canonical stable key collided"
                        )
                    writer.write_table(merged)
                    row_count += merged.num_rows
                    for row in identity_rows:
                        digest.update(
                            json.dumps(row, sort_keys=True, default=str).encode("utf-8")
                        )
                        if row["forecast_run_id"] != "NOT_APPLICABLE":
                            forecast_runs.add(str(row["forecast_run_id"]))
        finally:
            writer.close()
        pq.write_table(
            baseline.table, directory / "normals.parquet", compression="zstd"
        )
        contract_sha256 = _contract_sha256(catalog, soil, baseline)
        digest.update(contract_sha256.encode("ascii"))
        digest.update(baseline.content_sha256.encode("ascii"))
        digest.update(str(soil["manifest_sha256"]).encode("ascii"))
        manifest = {
            "schema_version": "lutou-public-weather-canonical/1",
            "run_id": run_id,
            "row_count": row_count,
            "series_count": len(catalog.series),
            "soil_row_count": int(soil["row_count"]),
            "soil_series_count": int(soil["series_count"]),
            "normal_row_count": baseline.row_count,
            "normal_series_count": baseline.series_count,
            "complete_row_count": (
                row_count + int(soil["row_count"]) + baseline.row_count
            ),
            "complete_series_count": (
                len(catalog.series) + int(soil["series_count"]) + baseline.series_count
            ),
            "forecast_run_count": len(forecast_runs),
            "stable_key_duplicate_count": stable_key_duplicate_count,
            "normal_stable_key_duplicate_count": 0,
            "contract_sha256": contract_sha256,
            "normal_content_sha256": baseline.content_sha256,
            "historical_base": (
                None
                if current is None
                else {
                    "release_id": current.release_id,
                    "manifest_sha256": identify_file(
                        current.directory / "manifest.json"
                    ).sha256,
                    "row_count": int(current.manifest["row_count"]),
                    "normal_upgrade_required": not current.normals_path.exists(),
                }
            ),
            "content_sha256": digest.hexdigest(),
            "files": _file_identities(
                directory, ("observations.parquet", "normals.parquet")
            ),
        }
        _write_json(directory / "manifest.json", manifest)
        _verify_files(directory, manifest["files"])
        result.update(manifest)
        return manifest

    directory, _ = seal_immutable_candidate(
        assert_runtime_write(runtime, public_root / "canonical-candidates"),
        run_id,
        build,
    )
    return directory, result


def _merge_series(
    previous: pa.Table,
    window: pa.Table,
    series: WeatherSeries,
    observation_start: date,
) -> pa.Table:
    if series.data_family == "observation" and previous.num_rows:
        old = previous.filter(
            pc.less(previous["valid_date"], pa.scalar(observation_start))
        )
        replaceable = previous.filter(
            pc.greater_equal(previous["valid_date"], pa.scalar(observation_start))
        )
    else:
        old = previous
        replaceable = previous
    previous_window = {_key(row): row for row in replaceable.to_pylist()}
    by_key: dict[tuple[object, object, object], Mapping[str, object]] = {}
    if series.data_family == "forecast":
        by_key.update({_key(row): row for row in previous.to_pylist()})
    new_rows = window.to_pylist()
    for row in new_rows:
        key = _key(row)
        existing = previous_window.get(key)
        if (
            existing is not None
            and existing["source_row_sha256"] == row["source_row_sha256"]
        ):
            by_key[key] = existing
        elif existing is not None and series.data_family == "forecast":
            raise LutouWeatherError("Weather forecast collision changed one stable key")
        else:
            by_key[key] = row
    replacement_rows = sorted(
        by_key.values(),
        key=lambda row: (row["valid_date"], str(row["forecast_run_id"])),
    )
    replacement = pa.Table.from_pylist(replacement_rows, schema=CANONICAL_SCHEMA)
    if series.data_family == "observation" and old.num_rows:
        return pa.concat_tables([old, replacement])
    return replacement


def _promote(
    runtime: RuntimeContext,
    public_root: Path,
    run_id: str,
    canonical_directory: Path,
    canonical_manifest: Mapping[str, object],
    candidate_manifest: Mapping[str, object],
    soil: Mapping[str, object],
    baseline: _BaselineEvidence,
    failure_hook: str | None,
) -> tuple[Path, dict[str, object]]:
    result: dict[str, object] = {}

    def build(directory: Path) -> dict[str, object]:
        shutil.copyfile(
            canonical_directory / "observations.parquet",
            directory / "observations.parquet",
        )
        shutil.copyfile(
            Path(str(soil["observations_path"])),
            directory / "soil_observations.parquet",
        )
        shutil.copyfile(
            canonical_directory / "normals.parquet",
            directory / "normals.parquet",
        )
        _write_json(
            directory / "soil_bindings.json",
            {
                "schema_version": "public-weather-soil-bindings/1",
                "binding_count": len(soil["bindings"]),
                "bindings": soil["bindings"],
            },
        )
        manifest = {
            "schema_version": "lutou-public-weather-current/1",
            "release_id": run_id,
            "source": "lutou",
            "scope": "current-weather-consumers",
            "source_max_dates": candidate_manifest["source_max_dates"],
            "forecast": candidate_manifest["forecast"],
            "row_count": canonical_manifest["row_count"],
            "series_count": canonical_manifest["series_count"],
            "soil_row_count": canonical_manifest["soil_row_count"],
            "soil_series_count": canonical_manifest["soil_series_count"],
            "normal_row_count": canonical_manifest["normal_row_count"],
            "normal_series_count": canonical_manifest["normal_series_count"],
            "complete_row_count": canonical_manifest["complete_row_count"],
            "complete_series_count": canonical_manifest["complete_series_count"],
            "forecast_run_count": canonical_manifest["forecast_run_count"],
            "stable_key_duplicate_count": canonical_manifest[
                "stable_key_duplicate_count"
            ],
            "normal_stable_key_duplicate_count": 0,
            "contract_sha256": canonical_manifest["contract_sha256"],
            "normal_content_sha256": baseline.content_sha256,
            "historical_base": canonical_manifest["historical_base"],
            "content_sha256": canonical_manifest["content_sha256"],
            "soil_current": {
                key: value
                for key, value in soil.items()
                if key != "observations_path" and key != "bindings"
            },
            "soil_binding_count": len(soil["bindings"]),
            "quality_status": "PASS",
            "files": _file_identities(
                directory,
                (
                    "observations.parquet",
                    "soil_observations.parquet",
                    "normals.parquet",
                    "soil_bindings.json",
                ),
            ),
        }
        _write_json(directory / "manifest.json", manifest)
        _verify_files(directory, manifest["files"])
        result.update(manifest)
        return manifest

    release, _ = seal_immutable_candidate(
        assert_runtime_write(runtime, public_root / "releases"), run_id, build
    )
    if failure_hook in {"invalid_manifest", "promotion"}:
        raise LutouWeatherError(f"injected Weather {failure_hook} failure")
    pointer = {
        "schema_version": 1,
        "release_id": run_id,
        "manifest_sha256": identify_file(release / "manifest.json").sha256,
    }
    atomic_write_json(
        assert_runtime_write(runtime, public_root / "current.json"), pointer
    )
    loaded = load_weather_current(public_root)
    if loaded is None or loaded.release_id != run_id:
        raise LutouWeatherError("Weather Current promotion validation failed")
    return release, result


def _baseline_evidence(
    policy_path: str | Path, baseline_root: str | Path
) -> _BaselineEvidence:
    policy = Path(policy_path)
    root = Path(baseline_root).resolve()
    if not root.is_dir():
        raise LutouWeatherError("approved Weather normal baseline root is unavailable")
    payload = yaml.safe_load(policy.read_text(encoding="utf-8"))
    sources = payload.get("normal_baselines")
    if not isinstance(sources, list) or len(sources) != 4:
        raise LutouWeatherError("Weather normal baseline policy is incomplete")
    rows: list[dict[str, object]] = []
    source_manifests: list[Mapping[str, object]] = []
    for source in sources:
        if not isinstance(source, dict):
            raise LutouWeatherError("Weather normal baseline source is invalid")
        relative = Path(str(source["relative_path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise LutouWeatherError("Weather normal baseline path is unsafe")
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise LutouWeatherError("Weather normal baseline escaped its root") from exc
        if not path.is_file():
            raise LutouWeatherError("approved Weather normal baseline is missing")
        config = load_weather_config(policy.parent / str(source["config"]))
        source_table = pq.read_table(path)
        required = {
            "month_day",
            "country",
            "region",
            "metric",
            "normal_value",
            "unit",
            "baseline_label",
            "source_workbook_sha256",
            "source_sheet",
        }
        if set(source_table.column_names) != required:
            raise LutouWeatherError("Weather normal baseline schema is invalid")
        source_rows = source_table.select(sorted(required)).to_pylist()
        expected_regions = {str(item["key"]) for item in config["regions"]}
        expected_country = str(config["country"])
        expected_metric_units = {
            "precipitation": "mm",
            "temperature_max": "degC",
        }
        configured_sheets = config.get("normal_sheets")
        expected_sheets = (
            {
                str(configured_sheets[metric]): metric
                for metric in expected_metric_units
            }
            if isinstance(configured_sheets, dict)
            else None
        )
        artifact = identify_file(path)
        source_keys: set[tuple[str, str, str]] = set()
        source_series: set[str] = set()
        workbook_hashes: set[str] = set()
        sheets_seen: set[str] = set()
        sheet_metrics: dict[str, str] = {}
        for item in source_rows:
            country = str(item["country"])
            region = str(item["region"])
            metric = str(item["metric"])
            month_day = str(item["month_day"])
            unit = str(item["unit"])
            sheet = str(item["source_sheet"])
            workbook_hash = str(item["source_workbook_sha256"]).lower()
            if (
                country != expected_country
                or region not in expected_regions
                or expected_metric_units.get(metric) != unit
                or not sheet
                or (
                    expected_sheets is not None
                    and expected_sheets.get(sheet) != metric
                )
                or _SHA256.fullmatch(workbook_hash) is None
            ):
                raise LutouWeatherError("Weather normal baseline identity is invalid")
            try:
                date.fromisoformat(f"2001-{month_day}")
                value = Decimal(str(item["normal_value"]))
            except (ValueError, InvalidOperation) as exc:
                raise LutouWeatherError(
                    "Weather normal baseline value is invalid"
                ) from exc
            if not value.is_finite():
                raise LutouWeatherError("Weather normal baseline value is non-finite")
            key = (region, metric, month_day)
            if key in source_keys:
                raise LutouWeatherError("Weather normal baseline stable key collided")
            source_keys.add(key)
            crop = str(config["crop"])
            series_id = (
                f"weather.normal.{metric}.{crop}.{country.lower()}.{region}.climatology"
            )
            source_series.add(series_id)
            locator = f"approved-static-weather-normal:{relative.as_posix()}"
            source_hash = _normal_row_hash(
                series_id,
                month_day,
                value,
                unit,
                artifact.sha256,
                workbook_hash,
                sheet,
            )
            rows.append(
                {
                    "schema_version": "public-weather-normal/1",
                    "series_id": series_id,
                    "provider": "UNKNOWN",
                    "origin_system": "approved_legacy_workbook",
                    "acquisition_channel": "approved_static_extract",
                    "source_locator": locator,
                    "source_artifact_sha256": artifact.sha256,
                    "source_workbook_sha256": workbook_hash,
                    "source_sheet": sheet,
                    "crop": crop,
                    "country": country,
                    "region": region,
                    "metric": metric,
                    "data_family": "historical_climatology",
                    "month_day": month_day,
                    "source_value": value,
                    "source_unit": unit,
                    "value": value,
                    "unit": unit,
                    "baseline_label": str(item["baseline_label"]),
                    "normal_period": None,
                    "normal_period_status": "SOURCE_NOT_PROVIDED",
                    "transformation_id": "weather.identity/1",
                    "source_row_sha256": source_hash,
                }
            )
            workbook_hashes.add(workbook_hash)
            sheets_seen.add(sheet)
            previous_metric = sheet_metrics.setdefault(sheet, metric)
            if previous_metric != metric:
                raise LutouWeatherError("Weather normal source sheet mixed metrics")
        if set(sheet_metrics.values()) != set(expected_metric_units):
            raise LutouWeatherError(
                "Weather normal baseline source sheets are incomplete"
            )
        expected_series_count = len(expected_regions) * len(expected_metric_units)
        if (
            len(source_series) != expected_series_count
            or len(source_keys) != expected_series_count * 365
        ):
            raise LutouWeatherError("Weather normal baseline coverage is incomplete")
        source_manifests.append(
            {
                "source_locator": (
                    f"approved-static-weather-normal:{relative.as_posix()}"
                ),
                "source_artifact_sha256": artifact.sha256,
                "source_workbook_sha256": sorted(workbook_hashes),
                "source_sheets": sorted(sheets_seen),
                "row_count": len(source_rows),
                "series_count": len(source_series),
                "metadata_status": "APPROVED_STATIC_SOURCE_PROVEN",
            }
        )
    rows.sort(key=lambda item: (str(item["series_id"]), str(item["month_day"])))
    table = pa.Table.from_pylist(rows, schema=NORMAL_SCHEMA)
    keys = {(str(item["series_id"]), str(item["month_day"])) for item in rows}
    if len(keys) != len(rows):
        raise LutouWeatherError("Weather normal baseline stable key collided")
    digest = hashlib.sha256()
    for item in rows:
        digest.update(str(item["source_row_sha256"]).encode("ascii"))
    return _BaselineEvidence(
        table=table,
        row_count=table.num_rows,
        series_count=len({str(item["series_id"]) for item in rows}),
        content_sha256=digest.hexdigest(),
        sources=tuple(source_manifests),
    )


def _normal_row_hash(
    series_id: str,
    month_day: str,
    value: Decimal,
    unit: str,
    artifact_sha256: str,
    workbook_sha256: str,
    source_sheet: str,
) -> str:
    payload = {
        "series_id": series_id,
        "month_day": month_day,
        "value": str(value),
        "unit": unit,
        "source_artifact_sha256": artifact_sha256,
        "source_workbook_sha256": workbook_sha256,
        "source_sheet": source_sheet,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _contract_sha256(
    catalog: WeatherSourceCatalog,
    soil: Mapping[str, object],
    baseline: _BaselineEvidence,
) -> str:
    payload = {
        "series": [asdict(item) for item in catalog.series],
        "derived_contracts": [dict(item) for item in catalog.derived_contracts],
        "canada_region_policy": dict(catalog.canada_region_policy),
        "soil_semantics": soil["semantics"],
        "soil_bindings": soil["bindings"],
        "normal_sources": list(baseline.sources),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _soil_evidence(
    runtime: RuntimeContext, policy_path: str | Path
) -> dict[str, object]:
    root = runtime.runtime_root / "public-market-data" / "lutou-soil-moisture"
    current = load_soil_current(root)
    if current is None:
        raise LutouWeatherError("Weather requires the approved Soil Current")
    pointer = _read_json(root / "current.json")
    soil_series = load_soil_moisture_series(
        Path(policy_path).parent / "public_research_data_catalog.candidate.json"
    )
    bindings = build_soil_consumer_bindings(policy_path, soil_series)
    release_evidence = current.manifest.get("weather_evidence")
    if isinstance(release_evidence, Mapping):
        source_tables = sorted(str(item) for item in release_evidence["source_tables"])
        retained_exception_count = int(release_evidence["retained_exception_count"])
        retained_exception_count_status = str(
            release_evidence["retained_exception_count_status"]
        )
    else:
        # Releases sealed before lutou-soil-weather-evidence/1 remain usable from
        # their formal observations.  Their discarded candidate-row count cannot
        # be reconstructed, so that absence is represented explicitly.
        source_tables = sorted(
            {str(item) for item in current.observations["source_table"].to_pylist()}
        )
        retained_exception_count = None
        retained_exception_count_status = "NOT_RECORDED_LEGACY_RELEASE"
    return {
        "release_id": current.release_id,
        "manifest_sha256": str(pointer["manifest_sha256"]),
        "row_count": current.observations.num_rows,
        "series_count": int(current.manifest["series_count"]),
        "source_max_date": str(current.manifest["source_max_date"]),
        "retained_exception_count": retained_exception_count,
        "retained_exception_count_status": retained_exception_count_status,
        "semantics": {
            "metric": "soil_moisture",
            "soil_depth": "0-100cm",
            "source_unit": "fraction",
            "unit": "%",
            "transformation": "source_value * 100",
        },
        "source_tables": source_tables,
        "bindings": list(bindings),
        "observations_path": str(current.directory / "observations.parquet"),
    }


def _safe_soil_manifest(soil: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in soil.items()
        if key not in {"observations_path"} and key != "bindings"
    }


def _source_catalog_document(
    catalog: WeatherSourceCatalog,
    stats: Mapping[str, object],
    soil: Mapping[str, object],
    baseline: _BaselineEvidence,
) -> dict[str, object]:
    mapped = {item.source_table: item for item in catalog.tables}
    soil_tables = {str(item) for item in soil["source_tables"]}
    nulls = {
        str(key): int(value)
        for key, value in dict(stats["table_null_cell_counts"]).items()
    }
    rows = []
    for item in catalog.schema_inventory:
        table = str(item["TABLE_NAME"])
        mapped_table = mapped.get(table)
        if mapped_table is not None:
            status = "MAPPED"
            semantics = {
                "data_family": mapped_table.data_family,
                "forecast_model": mapped_table.model,
                "metrics": sorted({series.metric for series in mapped_table.series}),
                "units": sorted({series.unit for series in mapped_table.series}),
                "frequency": "daily",
                "min_date": mapped_table.min_date.isoformat(),
                "max_date": mapped_table.max_date.isoformat(),
                "null_count": nulls.get(table, 0),
                "null_count_status": "EXACT_CONSUMER_COLUMNS",
            }
        elif table in soil_tables:
            status = "MAPPED_EXISTING_SOIL_CURRENT"
            semantics = {
                "data_family": "observation",
                "forecast_model": "OBSERVED",
                "metrics": ["soil_moisture"],
                "units": ["%"],
                "frequency": "daily",
                "min_date": None,
                "max_date": soil["source_max_date"],
                "null_count": None,
                "null_count_status": "PRESERVED_IN_SOIL_CANDIDATE",
            }
        else:
            status = "OUT_OF_CURRENT_SCOPE"
            semantics = {
                "data_family": "UNKNOWN",
                "forecast_model": "UNKNOWN",
                "metrics": [],
                "units": [],
                "frequency": "UNKNOWN",
                "min_date": None,
                "max_date": None,
                "null_count": None,
                "null_count_status": "NOT_SCANNED_OUT_OF_SCOPE",
            }
        rows.append(
            {
                "schema": catalog.source_schema,
                "table": table,
                "source_identity": f"lutou:{catalog.source_schema}:{table}",
                "table_type": str(item["TABLE_TYPE"]),
                "estimated_row_count": item.get("TABLE_ROWS"),
                "table_comment": str(item.get("TABLE_COMMENT") or ""),
                "update_time": (
                    None
                    if item.get("UPDATE_TIME") is None
                    else str(item["UPDATE_TIME"])
                ),
                "mapping_status": status,
                **semantics,
            }
        )
    return {
        "schema_version": "lutou-weather-source-catalog/1",
        "inventory_table_count": len(rows),
        "mapped_table_count": len(mapped) + len(soil_tables),
        "out_of_scope_table_count": len(rows) - len(mapped) - len(soil_tables),
        "consumer_needed_series_count": (
            len(catalog.series) + int(soil["series_count"]) + baseline.series_count
        ),
        "mapped_series_count": (
            len(catalog.series) + int(soil["series_count"]) + baseline.series_count
        ),
        "derived_with_proven_formula_count": len(catalog.derived_contracts),
        "unresolved_series_count": 0,
        "consumer_series": [
            {
                "series_id": item.series_id,
                "provider_dataset_id": item.provider_dataset_id,
                "provider_series_id": item.provider_series_id,
                "source_table": item.source_table,
                "source_column": item.source_column,
                "crop": item.crop,
                "country": item.country,
                "region": item.region,
                "region_label": item.region_label,
                "region_type": item.region_type,
                "metric": item.metric,
                "data_family": item.data_family,
                "forecast_model": item.model,
                "forecast_issue_date": None,
                "forecast_issue_status": (
                    "SOURCE_NOT_PROVIDED"
                    if item.data_family == "forecast"
                    else "NOT_APPLICABLE"
                ),
                "valid_date_semantics": "source_daily_date",
                "source_unit": item.source_unit,
                "unit": item.unit,
                "frequency": "daily",
                "mapping_status": "MAPPED",
            }
            for item in catalog.series
        ],
        "soil_consumer_bindings": soil["bindings"],
        "normal_baselines": {
            "semantic_class": "historical_climatology",
            "row_count": baseline.row_count,
            "series_count": baseline.series_count,
            "content_sha256": baseline.content_sha256,
            "sources": list(baseline.sources),
        },
        "canada_region_policy": dict(catalog.canada_region_policy),
        "tables": rows,
    }


class _SeriesParquetReader:
    def __init__(self, path: Path) -> None:
        self._file = pq.ParquetFile(path)
        self._groups: dict[str, list[int]] = {}
        for index in range(self._file.num_row_groups):
            values = set(
                self._file.read_row_group(index, columns=["series_id"])[
                    "series_id"
                ].to_pylist()
            )
            if len(values) != 1:
                raise LutouWeatherError("Weather Parquet row group mixes Series")
            self._groups.setdefault(str(next(iter(values))), []).append(index)

    def read(self, series_id: str) -> pa.Table:
        groups = self._groups.get(series_id, [])
        if not groups:
            return pa.Table.from_pylist([], schema=CANONICAL_SCHEMA)
        tables = [self._file.read_row_group(index) for index in groups]
        return pa.concat_tables(tables) if len(tables) > 1 else tables[0]


def _key(row: Mapping[str, object]) -> tuple[object, object, object]:
    return tuple(row[item] for item in STABLE_KEY)  # type: ignore[return-value]


def _exact_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise LutouWeatherError("Weather source date is invalid") from exc


def _decimal(value: object) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _row_hash(
    series: WeatherSeries, valid_date: date, run_id: str, raw_value_text: str
) -> str:
    payload = json.dumps(
        {
            "provider_series_id": series.provider_series_id,
            "valid_date": valid_date.isoformat(),
            "forecast_run_id": run_id,
            "raw_value_text": raw_value_text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_runtime(runtime: RuntimeContext) -> None:
    if (
        runtime.mode is not RuntimeMode.ISOLATED_DEV
        or runtime.module_id != "international-spread"
    ):
        raise LutouWeatherError("Weather writes require isolated local runtime")


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "CANONICAL_SCHEMA",
    "LOOKBACK_DAYS",
    "NORMAL_SCHEMA",
    "NORMAL_STABLE_KEY",
    "STABLE_KEY",
    "STANDARD_SCHEMA",
    "LutouWeatherError",
    "WeatherCurrent",
    "WeatherRunResult",
    "load_weather_current",
    "run_lutou_weather",
    "validate_weather_normal_baselines",
]
