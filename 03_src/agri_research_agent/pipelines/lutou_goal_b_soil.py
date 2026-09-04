"""Goal B reconciliation pipeline for approved 0-100 cm soil moisture."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Mapping

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from agri_research_agent.data_sources.lutou.soil_moisture_live import (
    MAPPING_VERSION,
    SoilMoistureExtraction,
    SoilMoistureSeries,
    extract_soil_moisture_live,
    load_soil_moisture_series,
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

from .lutou_goal_b import _file_identities, _read_json, _safe_metadata, _verify_files


LOOKBACK_DAYS = 31
DECIMAL_TYPE = pa.decimal128(38, 20)
CONVERSION_ID = "weather.soil_moisture.fraction_to_percent/1"
SOURCE_POLICY_VERSION = "soil-moisture-0-100cm-percent/1"
STABLE_KEY = ("series_id", "business_date")

STANDARD_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.string(), nullable=False),
        pa.field("dataset_id", pa.string(), nullable=False),
        pa.field("series_id", pa.string(), nullable=False),
        pa.field("provider_dataset_id", pa.string(), nullable=False),
        pa.field("provider_series_id", pa.string(), nullable=False),
        pa.field("provider", pa.string(), nullable=False),
        pa.field("origin_system", pa.string(), nullable=False),
        pa.field("acquisition_channel", pa.string(), nullable=False),
        pa.field("source_locator", pa.string(), nullable=False),
        pa.field("source_table", pa.string(), nullable=False),
        pa.field("source_column", pa.string(), nullable=False),
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("raw_value_text", pa.string(), nullable=False),
        pa.field("source_value", DECIMAL_TYPE, nullable=True),
        pa.field("source_unit", pa.string(), nullable=False),
        pa.field("value_percent", DECIMAL_TYPE, nullable=True),
        pa.field("unit", pa.string(), nullable=False),
        pa.field("metric", pa.string(), nullable=False),
        pa.field("soil_depth", pa.string(), nullable=False),
        pa.field("conversion_id", pa.string(), nullable=False),
        pa.field("conversion_factor", DECIMAL_TYPE, nullable=False),
        pa.field("mapping_version", pa.string(), nullable=False),
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
        if field.name
        not in {"mapping_version", "query_sha256", "quality_status", "is_usable"}
    ]
    + [
        pa.field("source_duplicate_count", pa.int32(), nullable=False),
        pa.field("source_group_sha256", pa.string(), nullable=False),
        pa.field("source_policy_version", pa.string(), nullable=False),
    ]
)


class LutouGoalBSoilError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SoilRunResult:
    run_id: str
    mode: str
    candidate_directory: Path
    canonical_directory: Path
    current_directory: Path
    candidate_manifest: Mapping[str, object]
    canonical_manifest: Mapping[str, object]
    current_manifest: Mapping[str, object]
    promoted: bool
    performance: Mapping[str, object]
    async_reports: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SoilCurrent:
    release_id: str
    directory: Path
    manifest: Mapping[str, object]
    observations: pa.Table


def run_goal_b_soil(
    client: object,
    *,
    runtime: RuntimeContext,
    run_id: str,
    end_date: date,
    full_load: bool,
    catalog_path: str | Path,
    failure_hook: str | None = None,
) -> SoilRunResult:
    total_started = perf_counter()
    stages: dict[str, dict[str, object]] = {}

    def observe(name: str, started: float, **details: object) -> float:
        duration = round(perf_counter() - started, 6)
        stages[name] = {"duration_seconds": duration, **details}
        return duration

    safe_run_id = validate_candidate_id(run_id)
    _require_runtime(runtime)
    series = load_soil_moisture_series(catalog_path)
    public_root = assert_runtime_write(
        runtime, runtime.runtime_root / "public-market-data" / "lutou-soil-moisture"
    )
    public_root.mkdir(parents=True, exist_ok=True)
    stage_started = perf_counter()
    current = load_soil_current(public_root)
    current_read_duration = observe(
        "load_current",
        stage_started,
        rows=0 if current is None else current.observations.num_rows,
    )
    if full_load and current is not None:
        raise LutouGoalBSoilError("soil full load refuses an existing Current")
    if not full_load and current is None:
        raise LutouGoalBSoilError("soil incremental requires an existing Current")
    start = (
        min(item.earliest_date for item in series)
        if current is None
        else date.fromisoformat(str(current.manifest["source_max_date"]))
        - timedelta(days=LOOKBACK_DAYS)
    )
    mode = "full" if current is None else "incremental-31-day-lookback"
    stage_started = perf_counter()
    try:
        extraction = extract_soil_moisture_live(
            client, series, start=start, end=end_date  # type: ignore[arg-type]
        )
    except Exception as exc:
        raise LutouGoalBSoilError(
            f"soil live extraction failed: {type(exc).__name__}: {exc}"
        ) from None
    observe(
        "extract",
        stage_started,
        query_count=len(extraction.queries),
        source_rows=sum(extraction.table_row_counts.values()),
        output_rows=len(extraction.records),
    )
    stage_started = perf_counter()
    standard = _standard_table(extraction)
    observe(
        "standardize",
        stage_started,
        input_rows=len(extraction.records),
        output_rows=standard.num_rows,
    )
    stage_started = perf_counter()
    gate = _candidate_gate(standard, series)
    observe("candidate_qc", stage_started, input_rows=standard.num_rows)
    if failure_hook == "candidate_qc":
        gate = {**gate, "quality_passed": False}
    if not gate["quality_passed"]:
        raise LutouGoalBSoilError("soil Candidate quality gate failed")
    stage_started = perf_counter()
    candidate_directory, candidate_manifest = _seal_candidate(
        runtime,
        public_root,
        safe_run_id,
        mode,
        start,
        end_date,
        standard,
        extraction,
        gate,
        client.proof.safe_manifest_fields(),  # type: ignore[attr-defined]
    )
    candidate_write_duration = observe(
        "candidate_build", stage_started, rows=standard.num_rows
    )
    stage_started = perf_counter()
    window, report = _canonicalize(standard, series)
    observe(
        "canonicalize",
        stage_started,
        input_rows=standard.num_rows,
        output_rows=window.num_rows,
    )
    if failure_hook == "canonical_collision":
        raise LutouGoalBSoilError("injected soil canonical collision")
    stage_started = perf_counter()
    observations = window if current is None else _merge(current.observations, window, start)
    observe(
        "merge",
        stage_started,
        previous_rows=0 if current is None else current.observations.num_rows,
        window_rows=window.num_rows,
        output_rows=observations.num_rows,
    )
    stage_started = perf_counter()
    _validate_canonical(observations, series)
    from .async_contract_rollout import observation_report
    # Roll out the existing FULL DAILY incremental mode, not initial seed policy.
    async_reports = {} if full_load else observation_report(runtime, safe_run_id, "soil_moisture", current, window, observations, series, end_date, LutouGoalBSoilError)
    observe("canonical_qc", stage_started, input_rows=observations.num_rows)
    stage_started = perf_counter()
    canonical_directory, canonical_manifest = _seal_canonical(
        runtime, public_root, safe_run_id, candidate_directory, observations, report
    )
    canonical_write_duration = observe(
        "canonical_build", stage_started, rows=observations.num_rows
    )
    io = {
        "current_read": {
            "rows": 0 if current is None else current.observations.num_rows,
            "bytes": 0
            if current is None
            else (current.directory / "observations.parquet").stat().st_size,
            "duration_seconds": current_read_duration,
        },
        "candidate_write": {
            "rows": standard.num_rows,
            "bytes": (candidate_directory / "standard.parquet").stat().st_size,
            "duration_seconds": candidate_write_duration,
        },
        "canonical_write": {
            "rows": observations.num_rows,
            "bytes": (canonical_directory / "observations.parquet").stat().st_size,
            "duration_seconds": canonical_write_duration,
        },
    }
    if current is not None and observations.equals(current.observations):
        stages["promote"] = {"duration_seconds": 0.0, "status": "SKIPPED_NO_CHANGE"}
        observe("total", total_started)
        return SoilRunResult(
            safe_run_id,
            mode,
            candidate_directory,
            canonical_directory,
            current.directory,
            candidate_manifest,
            canonical_manifest,
            current.manifest,
            False,
            {
                "schema_version": "soil-performance-telemetry/1",
                "stages": stages,
                "io": io,
            },
            async_reports=async_reports,
        )
    stage_started = perf_counter()
    current_directory, current_manifest = _promote(
        runtime,
        public_root,
        safe_run_id,
        canonical_directory,
        canonical_manifest,
        candidate_manifest,
        failure_hook,
    )
    promote_duration = observe("promote", stage_started, rows=observations.num_rows)
    io["release_write"] = {
        "rows": observations.num_rows,
        "bytes": (current_directory / "observations.parquet").stat().st_size,
        "duration_seconds": promote_duration,
    }
    observe("total", total_started)
    return SoilRunResult(
        safe_run_id,
        mode,
        candidate_directory,
        canonical_directory,
        current_directory,
        candidate_manifest,
        canonical_manifest,
        current_manifest,
        True,
        {
            "schema_version": "soil-performance-telemetry/1",
            "stages": stages,
            "io": io,
        },
        async_reports=async_reports,
    )


def load_soil_current(public_root: str | Path) -> SoilCurrent | None:
    root = Path(public_root)
    pointer = root / "current.json"
    if not pointer.exists():
        return None
    value = _read_json(pointer)
    release_id = validate_candidate_id(str(value["release_id"]))
    directory = root / "releases" / release_id
    manifest = _read_json(directory / "manifest.json")
    if identify_file(directory / "manifest.json").sha256 != value["manifest_sha256"]:
        raise LutouGoalBSoilError("soil Current pointer identity mismatch")
    _verify_files(directory, manifest["files"])
    observations = pq.read_table(directory / "observations.parquet")
    if observations.schema != CANONICAL_SCHEMA:
        raise LutouGoalBSoilError("soil Current schema is invalid")
    return SoilCurrent(release_id, directory, manifest, observations)


def _standard_table(extraction: SoilMoistureExtraction) -> pa.Table:
    rows = []
    for item in extraction.records:
        usable = (
            item.is_numeric
            and item.value_percent is not None
            and Decimal("0") <= item.value_percent <= Decimal("100")
        )
        quality_status = (
            "PASS"
            if usable
            else "NON_NUMERIC"
            if not item.is_numeric
            else "OUT_OF_RANGE"
        )
        contract = item.contract
        rows.append(
            {
                "schema_version": "lutou-soil-moisture-standard/1",
                "dataset_id": contract.dataset_id,
                "series_id": contract.series_id,
                "provider_dataset_id": contract.provider_dataset_id,
                "provider_series_id": contract.provider_series_id,
                "provider": "UNKNOWN",
                "origin_system": "lutou",
                "acquisition_channel": "direct_database",
                "source_locator": f"database:lutou/schema:天气2.0/relation:{contract.source_table}",
                "source_table": contract.source_table,
                "source_column": contract.source_column,
                "business_date": item.business_date,
                "raw_value_text": item.raw_value_text,
                "source_value": item.source_value,
                "source_unit": "fraction",
                "value_percent": item.value_percent,
                "unit": "%",
                "metric": "soil_moisture",
                "soil_depth": "0-100cm",
                "conversion_id": CONVERSION_ID,
                "conversion_factor": Decimal("100"),
                "mapping_version": MAPPING_VERSION,
                "extracted_at": item.extracted_at,
                "query_sha256": item.query_sha256,
                "source_row_sha256": item.source_row_sha256,
                "is_numeric": item.is_numeric,
                "quality_status": quality_status,
                "is_usable": usable,
            }
        )
    return pa.Table.from_pylist(rows, schema=STANDARD_SCHEMA).sort_by(
        [("series_id", "ascending"), ("business_date", "ascending")]
    )


def _candidate_gate(
    table: pa.Table, series: tuple[SoilMoistureSeries, ...]
) -> dict[str, object]:
    rows = table.to_pylist()
    usable = [row for row in rows if row["is_usable"]]
    numeric = [row for row in rows if row["is_numeric"]]
    groups: dict[tuple[str, date], set[Decimal]] = {}
    for row in usable:
        groups.setdefault((str(row["series_id"]), row["business_date"]), set()).add(
            row["source_value"]
        )
    conflicts = sum(len(values) > 1 for values in groups.values())
    observed = {str(row["series_id"]) for row in usable}
    expected = {item.series_id for item in series}
    return {
        "quality_status": "PASS" if not conflicts and observed == expected else "FAIL",
        "quality_passed": not conflicts and observed == expected,
        "row_count": table.num_rows,
        "usable_row_count": len(usable),
        "non_numeric_row_count": table.num_rows - len(numeric),
        "out_of_range_row_count": len(numeric) - len(usable),
        "series_count": len(observed),
        "expected_series_count": len(expected),
        "conflicting_duplicate_date_count": conflicts,
        "same_value_duplicate_row_count": len(usable) - len(groups),
        "source_range_min": str(min(row["source_value"] for row in numeric)),
        "source_range_max": str(max(row["source_value"] for row in numeric)),
        "percent_range_min": str(min(row["value_percent"] for row in numeric)),
        "percent_range_max": str(max(row["value_percent"] for row in numeric)),
    }


def _canonicalize(
    table: pa.Table, series: tuple[SoilMoistureSeries, ...]
) -> tuple[pa.Table, dict[str, object]]:
    groups: dict[tuple[str, date], list[dict[str, object]]] = {}
    for row in table.to_pylist():
        if row["is_usable"]:
            groups.setdefault((str(row["series_id"]), row["business_date"]), []).append(row)
    output = []
    duplicates = 0
    for rows in groups.values():
        values = {row["source_value"] for row in rows}
        if len(values) != 1:
            raise LutouGoalBSoilError("soil canonical source collision")
        selected = sorted(rows, key=lambda row: str(row["source_row_sha256"]))[0]
        duplicates += len(rows) - 1
        output.append(
            {
                key: value
                for key, value in selected.items()
                if key
                not in {"mapping_version", "query_sha256", "quality_status", "is_usable"}
            }
            | {
                "source_duplicate_count": len(rows) - 1,
                "source_group_sha256": _group_hash(rows),
                "source_policy_version": SOURCE_POLICY_VERSION,
            }
        )
    result = pa.Table.from_pylist(output, schema=CANONICAL_SCHEMA).sort_by(
        [("series_id", "ascending"), ("business_date", "ascending")]
    )
    return result, {
        "quality_status": "PASS",
        "collision_count": 0,
        "stable_key_duplicates": _duplicate_count(result),
        "same_value_duplicate_row_count": duplicates,
        "source_policy_version": SOURCE_POLICY_VERSION,
    }


def _merge_reference(previous: pa.Table, window: pa.Table, start: date) -> pa.Table:
    """Reference row semantics retained for golden equivalence tests only."""

    old = [row for row in previous.to_pylist() if row["business_date"] < start]
    prior = {
        (str(row["series_id"]), row["business_date"]): row
        for row in previous.to_pylist()
        if row["business_date"] >= start
    }
    merged = []
    for row in window.to_pylist():
        key = (str(row["series_id"]), row["business_date"])
        existing = prior.get(key)
        merged.append(
            existing
            if existing is not None
            and existing["source_group_sha256"] == row["source_group_sha256"]
            else row
        )
    return pa.Table.from_pylist([*old, *merged], schema=CANONICAL_SCHEMA).sort_by(
        [("series_id", "ascending"), ("business_date", "ascending")]
    )


def _merge(previous: pa.Table, window: pa.Table, start: date) -> pa.Table:
    """Columnar equivalent of the authoritative lookback-window upsert."""

    if previous.schema != CANONICAL_SCHEMA or window.schema != CANONICAL_SCHEMA:
        raise LutouGoalBSoilError("soil incremental merge schema is invalid")
    _require_unique_stable_keys(previous, "previous")
    _require_unique_stable_keys(window, "window")

    before_window = previous.filter(
        pc.less(previous["business_date"], pa.scalar(start, type=pa.date32()))
    )
    replaceable = previous.filter(
        pc.greater_equal(previous["business_date"], pa.scalar(start, type=pa.date32()))
    )
    lookup = replaceable.select([*STABLE_KEY, "source_group_sha256"]).rename_columns(
        [*STABLE_KEY, "__previous_group_sha256"]
    )
    lookup = lookup.append_column(
        "__previous_index", pa.array(range(replaceable.num_rows), type=pa.int64())
    )
    joined = window.join(lookup, keys=list(STABLE_KEY), join_type="left outer")
    unchanged = pc.fill_null(
        pc.equal(joined["source_group_sha256"], joined["__previous_group_sha256"]),
        False,
    )
    unchanged_previous = replaceable.take(
        joined.filter(unchanged)["__previous_index"]
    )
    changed_window = joined.filter(pc.invert(unchanged)).select(window.column_names).cast(
        CANONICAL_SCHEMA
    )
    return pa.concat_tables([before_window, unchanged_previous, changed_window]).sort_by(
        [("series_id", "ascending"), ("business_date", "ascending")]
    )


def _require_unique_stable_keys(table: pa.Table, label: str) -> None:
    distinct = table.select(STABLE_KEY).group_by(list(STABLE_KEY)).aggregate([])
    if distinct.num_rows != table.num_rows:
        raise LutouGoalBSoilError(f"soil {label} stable-key is duplicated")


def _validate_canonical(table: pa.Table, series: tuple[SoilMoistureSeries, ...]) -> None:
    if table.schema != CANONICAL_SCHEMA or _duplicate_count(table):
        raise LutouGoalBSoilError("soil Canonical stable-key gate failed")
    if set(table["series_id"].to_pylist()) != {item.series_id for item in series}:
        raise LutouGoalBSoilError("soil Canonical coverage is incomplete")
    if set(table["unit"].to_pylist()) != {"%"} or set(
        table["soil_depth"].to_pylist()
    ) != {"0-100cm"}:
        raise LutouGoalBSoilError("soil Canonical semantics are invalid")


def _seal_candidate(
    runtime: RuntimeContext,
    public_root: Path,
    run_id: str,
    mode: str,
    start: date,
    end: date,
    standard: pa.Table,
    extraction: SoilMoistureExtraction,
    gate: Mapping[str, object],
    proof: Mapping[str, object],
) -> tuple[Path, dict[str, object]]:
    result: dict[str, object] = {}

    def build(directory: Path) -> dict[str, object]:
        pq.write_table(standard, directory / "standard.parquet")
        dates = standard["business_date"].to_pylist()
        manifest = {
            "schema_version": "lutou-goal-b-soil-candidate/1",
            "run_id": run_id,
            "mode": mode,
            "source": "lutou",
            "scope": "soil-moisture-0-100cm-current-weather-consumers",
            "connection_proof": dict(proof),
            "query_window_start": start.isoformat(),
            "query_window_end": end.isoformat(),
            "source_min_date": min(dates).isoformat(),
            "source_max_date": max(dates).isoformat(),
            "metadata": {
                "metric": "soil_moisture",
                "soil_depth": "0-100cm",
                "source_unit": "fraction",
                "unit": "%",
                "conversion_id": CONVERSION_ID,
                "conversion_factor": "100",
                "semantic_authority": "user_approved_business_definition_plus_live_range_validation",
            },
            "query_plans": [item.safe_manifest_fields() for item in extraction.plans],
            "queries": [item.safe_manifest_fields() for item in extraction.queries],
            "table_row_counts": dict(extraction.table_row_counts),
            "quality": dict(gate),
            "files": _file_identities(directory, ("standard.parquet",)),
        }
        _write_json(directory / "manifest.json", manifest)
        _verify_files(directory, manifest["files"])
        result.update(manifest)
        return manifest

    directory, _ = seal_immutable_candidate(
        assert_runtime_write(runtime, public_root / "candidates"), run_id, build
    )
    return directory, result


def _seal_canonical(
    runtime: RuntimeContext,
    public_root: Path,
    run_id: str,
    candidate_directory: Path,
    observations: pa.Table,
    report: Mapping[str, object],
) -> tuple[Path, dict[str, object]]:
    result: dict[str, object] = {}

    def build(directory: Path) -> dict[str, object]:
        pq.write_table(observations, directory / "observations.parquet")
        dates = observations["business_date"].to_pylist()
        manifest = {
            "schema_version": "lutou-goal-b-soil-canonical/1",
            "run_id": run_id,
            "candidate_manifest_sha256": identify_file(
                candidate_directory / "manifest.json"
            ).sha256,
            "row_count": observations.num_rows,
            "series_count": len(set(observations["series_id"].to_pylist())),
            "min_date": min(dates).isoformat(),
            "max_date": max(dates).isoformat(),
            "quality": dict(report),
            "files": _file_identities(directory, ("observations.parquet",)),
        }
        _write_json(directory / "manifest.json", manifest)
        _verify_files(directory, manifest["files"])
        result.update(manifest)
        return manifest

    directory, _ = seal_immutable_candidate(
        assert_runtime_write(runtime, public_root / "canonical-candidates"), run_id, build
    )
    return directory, result


def _promote(
    runtime: RuntimeContext,
    public_root: Path,
    run_id: str,
    canonical_directory: Path,
    canonical_manifest: Mapping[str, object],
    candidate_manifest: Mapping[str, object],
    failure_hook: str | None,
) -> tuple[Path, dict[str, object]]:
    result: dict[str, object] = {}

    def build(directory: Path) -> dict[str, object]:
        destination = directory / "observations.parquet"
        destination.write_bytes((canonical_directory / "observations.parquet").read_bytes())
        manifest = {
            "schema_version": "lutou-goal-b-soil-current/1",
            "release_id": run_id,
            "source": "lutou",
            "scope": "soil-moisture-0-100cm-current-weather-consumers",
            "source_max_date": candidate_manifest["source_max_date"],
            "row_count": canonical_manifest["row_count"],
            "series_count": canonical_manifest["series_count"],
            "min_date": canonical_manifest["min_date"],
            "max_date": canonical_manifest["max_date"],
            "quality_status": "PASS",
            "weather_evidence": {
                "schema_version": "lutou-soil-weather-evidence/1",
                "source_tables": sorted(candidate_manifest["table_row_counts"]),
                "retained_exception_count": (
                    int(candidate_manifest["quality"]["non_numeric_row_count"])
                    + int(candidate_manifest["quality"]["out_of_range_row_count"])
                ),
                "retained_exception_count_status": "RECORDED",
            },
            "files": _file_identities(directory, ("observations.parquet",)),
        }
        _write_json(directory / "manifest.json", manifest)
        _verify_files(directory, manifest["files"])
        result.update(manifest)
        return manifest

    directory, _ = seal_immutable_candidate(
        assert_runtime_write(runtime, public_root / "releases"), run_id, build
    )
    if failure_hook == "promote_before_pointer":
        raise LutouGoalBSoilError("injected soil pre-pointer failure")
    manifest_id = identify_file(directory / "manifest.json")
    atomic_write_json(
        assert_runtime_write(runtime, public_root / "current.json"),
        {
            "schema_version": 1,
            "release_id": run_id,
            "manifest_sha256": manifest_id.sha256,
        },
    )
    promoted = load_soil_current(public_root)
    if promoted is None or promoted.release_id != run_id:
        raise LutouGoalBSoilError("soil Current post-promotion verification failed")
    if not promoted.observations.equals(pq.read_table(directory / "observations.parquet")):
        raise LutouGoalBSoilError("soil Current content verification failed")
    return directory, result


def _write_json(path: Path, payload: object) -> None:
    _safe_metadata(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _duplicate_count(table: pa.Table) -> int:
    seen: set[tuple[object, object]] = set()
    duplicates = 0
    for row in table.select(STABLE_KEY).to_pylist():
        key = (row["series_id"], row["business_date"])
        duplicates += int(key in seen)
        seen.add(key)
    return duplicates


def _group_hash(rows: list[dict[str, object]]) -> str:
    payload = json.dumps(
        sorted(str(row["source_row_sha256"]) for row in rows), separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _require_runtime(runtime: RuntimeContext) -> None:
    if runtime.mode is not RuntimeMode.ISOLATED_DEV or runtime.module_id != "international-spread":
        raise LutouGoalBSoilError("soil Goal B requires isolated International Spread runtime")
