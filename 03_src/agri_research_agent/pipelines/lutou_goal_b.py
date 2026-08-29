"""Goal B: sealed Three-Oil V1 ingestion from Lutou into isolated Current."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Mapping, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from agri_research_agent.data_sources.lutou.three_oil_live import (
    MAPPING_VERSION,
    LiveThreeOilExtraction,
    extract_three_oil_live,
)
from agri_research_agent.research_data.three_oil_v1 import (
    CanonicalSeriesContract,
    ThreeOilV1Catalog,
    load_three_oil_v1,
)
from agri_research_agent.shared.atomic_storage import atomic_write_json
from agri_research_agent.shared.arrow_window_upsert import (
    ArrowWindowUpsertError,
    merge_authoritative_window,
)
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


LOOKBACK_DAYS = 31
SOURCE_POLICY_VERSION = "three-oil-v1-sealed-direct-database/2"
STABLE_KEY = ("series_id", "business_date")
# Lutou exposes several approved source columns as MySQL DOUBLE.  PyMySQL's
# shortest round-trippable representation can carry up to 17 significant
# decimal digits, and the sealed cents/lb conversion adds four fractional
# places.  Scale 20 preserves both source evidence and deterministic
# conversion output without rounding.
DECIMAL_TYPE = pa.decimal128(38, 20)

RAW_SCHEMA = pa.schema(
    [
        pa.field("source_table", pa.string(), nullable=False),
        pa.field("source_column", pa.string(), nullable=False),
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("raw_price", DECIMAL_TYPE, nullable=False),
        pa.field("extracted_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("query_sha256", pa.string(), nullable=False),
        pa.field("source_row_sha256", pa.string(), nullable=False),
    ]
)

STANDARD_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.string(), nullable=False),
        pa.field("dataset_id", pa.string(), nullable=False),
        pa.field("series_id", pa.string(), nullable=False),
        pa.field("provider_dataset_id", pa.string(), nullable=False),
        pa.field("provider_series_id", pa.string(), nullable=False),
        pa.field("provider", pa.string(), nullable=False),
        pa.field("metadata_source_type", pa.string(), nullable=False),
        pa.field("metadata_status", pa.string(), nullable=False),
        pa.field("source_quote_unit", pa.string(), nullable=False),
        pa.field("origin_system", pa.string(), nullable=False),
        pa.field("acquisition_channel", pa.string(), nullable=False),
        pa.field("source_locator", pa.string(), nullable=False),
        pa.field("source_table", pa.string(), nullable=False),
        pa.field("source_column", pa.string(), nullable=False),
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("raw_price", DECIMAL_TYPE, nullable=False),
        pa.field("source_currency", pa.string(), nullable=False),
        pa.field("source_unit", pa.string(), nullable=False),
        pa.field("conversion_id", pa.string(), nullable=True),
        pa.field("mapping_version", pa.string(), nullable=False),
        pa.field("extracted_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("query_sha256", pa.string(), nullable=False),
        pa.field("source_row_sha256", pa.string(), nullable=False),
        pa.field("quality_status", pa.string(), nullable=False),
        pa.field("is_usable", pa.bool_(), nullable=False),
    ]
)

CANONICAL_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.string(), nullable=False),
        pa.field("dataset_id", pa.string(), nullable=False),
        pa.field("series_id", pa.string(), nullable=False),
        pa.field("provider_dataset_id", pa.string(), nullable=False),
        pa.field("provider_series_id", pa.string(), nullable=False),
        pa.field("provider", pa.string(), nullable=False),
        pa.field("metadata_source_type", pa.string(), nullable=False),
        pa.field("metadata_status", pa.string(), nullable=False),
        pa.field("source_quote_unit", pa.string(), nullable=False),
        pa.field("origin_system", pa.string(), nullable=False),
        pa.field("acquisition_channel", pa.string(), nullable=False),
        pa.field("source_locator", pa.string(), nullable=False),
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("value", DECIMAL_TYPE, nullable=False),
        pa.field("currency", pa.string(), nullable=False),
        pa.field("unit", pa.string(), nullable=False),
        pa.field("product", pa.string(), nullable=False),
        pa.field("product_grade", pa.string(), nullable=False),
        pa.field("country", pa.string(), nullable=False),
        pa.field("region", pa.string(), nullable=False),
        pa.field("location_native", pa.string(), nullable=False),
        pa.field("quote_basis", pa.string(), nullable=False),
        pa.field("tenor", pa.string(), nullable=False),
        pa.field("price_type", pa.string(), nullable=False),
        pa.field("source_duplicate_count", pa.int32(), nullable=False),
        pa.field("source_group_sha256", pa.string(), nullable=False),
        pa.field("source_policy_version", pa.string(), nullable=False),
        pa.field("captured_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)
LEGACY_CANONICAL_SCHEMA = pa.schema(
    [
        field
        for field in CANONICAL_SCHEMA
        if field.name
        not in {"provider", "metadata_source_type", "metadata_status", "source_quote_unit"}
    ]
)


class LutouGoalBError(RuntimeError):
    """Raised before isolated Current can change when a Goal B gate fails."""


@dataclass(frozen=True, slots=True)
class GoalBRunResult:
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


@dataclass(frozen=True, slots=True)
class GoalBCurrent:
    release_id: str
    directory: Path
    manifest: Mapping[str, object]
    observations: pa.Table


def run_goal_b(
    client: object,
    *,
    runtime: RuntimeContext,
    run_id: str,
    end_date: date,
    full_load: bool,
    catalog_path: str | Path | None = None,
    failure_hook: str | None = None,
) -> GoalBRunResult:
    total_started = perf_counter()
    stages: dict[str, dict[str, object]] = {}

    def observe(name: str, started: float, **details: object) -> float:
        duration = round(perf_counter() - started, 6)
        stages[name] = {"duration_seconds": duration, **details}
        return duration

    safe_run_id = validate_candidate_id(run_id)
    _require_runtime(runtime)
    if type(end_date) is not date:
        raise LutouGoalBError("Goal B end_date must be an exact date")
    catalog = load_three_oil_v1(catalog_path)
    public_root = assert_runtime_write(
        runtime, runtime.runtime_root / "public-market-data" / "lutou-three-oil"
    )
    public_root.mkdir(parents=True, exist_ok=True)
    stage_started = perf_counter()
    current = load_current(public_root)
    current_read_duration = observe(
        "load_current",
        stage_started,
        rows=0 if current is None else current.observations.num_rows,
    )
    if full_load and current is not None:
        raise LutouGoalBError("Goal B full load refuses to replace an existing Current")
    if not full_load and current is None:
        raise LutouGoalBError("Goal B incremental load requires an existing Current")

    start = (
        min(item.earliest_date for item in catalog.series)
        if current is None
        else _manifest_date(current.manifest, "source_max_date")
        - timedelta(days=LOOKBACK_DAYS)
    )
    mode = "full" if full_load else "incremental-31-day-lookback"
    stage_started = perf_counter()
    try:
        extraction = extract_three_oil_live(
            client, catalog, start=start, end=end_date  # type: ignore[arg-type]
        )
    except Exception as exc:
        raise LutouGoalBError(
            f"Goal B live extraction failed: {type(exc).__name__}"
        ) from None
    observe(
        "extract",
        stage_started,
        query_count=len(extraction.queries),
        source_rows=sum(extraction.table_row_counts.values()),
        output_rows=len(extraction.records),
    )
    if failure_hook == "connection":
        raise LutouGoalBError("injected Goal B connection failure")

    stage_started = perf_counter()
    raw = _raw_table(extraction)
    standard = _standard_table(extraction)
    observe(
        "standardize",
        stage_started,
        input_rows=len(extraction.records),
        output_rows=standard.num_rows,
    )
    stage_started = perf_counter()
    candidate_gate = _candidate_gate(standard, catalog)
    observe("candidate_qc", stage_started, input_rows=standard.num_rows)
    if failure_hook == "candidate_qc":
        candidate_gate = {**candidate_gate, "quality_passed": False, "quality_status": "FAIL"}
    if not candidate_gate["quality_passed"]:
        raise LutouGoalBError("Goal B Candidate quality gate failed")

    stage_started = perf_counter()
    candidate_directory, candidate_manifest = _seal_candidate(
        runtime,
        public_root,
        safe_run_id,
        mode,
        start,
        end_date,
        raw,
        standard,
        extraction,
        candidate_gate,
        client.proof.safe_manifest_fields(),  # type: ignore[attr-defined]
        failure_hook,
    )
    candidate_write_duration = observe(
        "candidate_build", stage_started, rows=standard.num_rows
    )

    stage_started = perf_counter()
    window_canonical, report = _canonicalize(standard, catalog)
    observe(
        "canonicalize",
        stage_started,
        input_rows=standard.num_rows,
        output_rows=window_canonical.num_rows,
    )
    if failure_hook == "canonical_collision":
        raise LutouGoalBError("injected Goal B canonical collision")
    stage_started = perf_counter()
    observations = (
        window_canonical
        if current is None
        else _merge_current(current.observations, window_canonical, start)
    )
    observe(
        "merge",
        stage_started,
        previous_rows=0 if current is None else current.observations.num_rows,
        window_rows=window_canonical.num_rows,
        output_rows=observations.num_rows,
    )
    stage_started = perf_counter()
    _validate_canonical(observations, catalog)
    observe("canonical_qc", stage_started, input_rows=observations.num_rows)
    stage_started = perf_counter()
    canonical_directory, canonical_manifest = _seal_canonical(
        runtime,
        public_root,
        safe_run_id,
        candidate_directory,
        observations,
        report,
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
            "bytes": sum(
                int(value["size_bytes"])
                for value in candidate_manifest["files"].values()
            ),
            "duration_seconds": candidate_write_duration,
        },
        "canonical_write": {
            "rows": observations.num_rows,
            "bytes": (canonical_directory / "observations.parquet").stat().st_size,
            "duration_seconds": canonical_write_duration,
        },
    }

    if (
        current is not None
        and current.manifest.get("schema_version") == "lutou-goal-b-current/2"
        and current.manifest.get("metadata_provenance")
        == candidate_manifest.get("metadata_provenance")
        and observations.equals(current.observations)
    ):
        stages["promote"] = {"duration_seconds": 0.0, "status": "SKIPPED_NO_CHANGE"}
        observe("total", total_started)
        return GoalBRunResult(
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
                "schema_version": "three-oil-performance-telemetry/1",
                "stages": stages,
                "io": io,
            },
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
    return GoalBRunResult(
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
            "schema_version": "three-oil-performance-telemetry/1",
            "stages": stages,
            "io": io,
        },
    )


def load_current(
    public_root: str | Path,
    *,
    columns: Sequence[str] | None = None,
) -> GoalBCurrent | None:
    """Load a validated Current, optionally projecting canonical columns.

    The pointer, manifest, file identity, and full Parquet schema are always
    validated before a projection is read.  The default remains the complete
    producer-facing table used by existing callers.
    """

    root = Path(public_root)
    pointer = root / "current.json"
    if not pointer.exists():
        return None
    value = _read_json(pointer)
    if set(value) != {"schema_version", "release_id", "manifest_sha256"}:
        raise LutouGoalBError("Goal B Current pointer is invalid")
    release_id = validate_candidate_id(str(value["release_id"]))
    directory = root / "releases" / release_id
    manifest_path = directory / "manifest.json"
    if identify_file(manifest_path).sha256 != value["manifest_sha256"]:
        raise LutouGoalBError("Goal B Current manifest identity differs from pointer")
    manifest = _read_json(manifest_path)
    _verify_release(directory, manifest)
    observations_path = directory / "observations.parquet"
    parquet_schema = pq.read_schema(observations_path)
    if parquet_schema not in (CANONICAL_SCHEMA, LEGACY_CANONICAL_SCHEMA):
        raise LutouGoalBError("Goal B Current schema is invalid")
    projected = None if columns is None else tuple(columns)
    if projected is not None:
        if len(set(projected)) != len(projected):
            raise LutouGoalBError("Goal B Current projection contains duplicates")
        unknown = set(projected) - set(CANONICAL_SCHEMA.names)
        if unknown:
            raise LutouGoalBError("Goal B Current projection is invalid")
        _validate_full_parquet_readability(observations_path)
    if parquet_schema == LEGACY_CANONICAL_SCHEMA:
        observations = _upgrade_legacy_current(pq.read_table(observations_path))
        if projected is not None:
            observations = observations.select(projected)
    else:
        observations = pq.read_table(
            observations_path,
            columns=None if projected is None else list(projected),
        )
        expected_schema = (
            CANONICAL_SCHEMA
            if projected is None
            else pa.schema(CANONICAL_SCHEMA.field(name) for name in projected)
        )
        if observations.schema != expected_schema:
            raise LutouGoalBError("Goal B Current projected schema is invalid")
    return GoalBCurrent(release_id, directory, manifest, observations)


def _validate_full_parquet_readability(observations_path: Path) -> None:
    """Decode every physical column without creating Python row objects."""

    try:
        parquet_file = pq.ParquetFile(observations_path)
        scanned_rows = parquet_file.scan_contents(columns=None)
    except (OSError, pa.ArrowException, ValueError):
        raise LutouGoalBError("Goal B Current Parquet readability is invalid") from None
    if scanned_rows != parquet_file.metadata.num_rows:
        raise LutouGoalBError("Goal B Current Parquet readability is invalid")


def _upgrade_legacy_current(table: pa.Table) -> pa.Table:
    """Add the approved upstream provider to a pre-reconciliation isolated Current."""

    rows = table.to_pylist()
    for row in rows:
        provider_dataset_id = str(row["provider_dataset_id"])
        if provider_dataset_id == "lutou:oils:oil_world_prices":
            provider = "Oil World"
        elif provider_dataset_id.startswith("lutou:oils:"):
            provider = "Reuters"
        else:
            raise LutouGoalBError("Goal B legacy Current provider cannot be reconciled")
        row["provider"] = provider
        if provider == "Oil World":
            row["metadata_source_type"] = "official_provider_website"
            row["source_quote_unit"] = "US-$/T"
        else:
            row["metadata_source_type"] = "approved_source_mapping"
            series_id = str(row["series_id"])
            if series_id in {
                "market.basis.soybean_oil.argentina.upper_river.spot",
                "market.basis.soybean_oil.brazil.paranagua.spot",
            }:
                row["source_quote_unit"] = "raw_basis_x100"
            elif series_id == "market.environmental_credit.rin.d4":
                row["source_quote_unit"] = "raw_credit_x100"
            else:
                row["source_quote_unit"] = {
                    ("USD", "metric_tonne"): "USD/T",
                    ("INR", "metric_tonne"): "INR/T",
                    ("USD", "US_cents_per_lb"): "USC/LB",
                    ("USD", "gallon"): "USD/GAL",
                }[(str(row["currency"]), str(row["unit"]))]
        row["metadata_status"] = "proven"
        row["schema_version"] = "lutou-three-oil-canonical/2"
        row["source_policy_version"] = SOURCE_POLICY_VERSION
    return pa.Table.from_pylist(rows, schema=CANONICAL_SCHEMA).sort_by(
        [("series_id", "ascending"), ("business_date", "ascending")]
    )


def _raw_table(extraction: LiveThreeOilExtraction) -> pa.Table:
    rows = [
        {
            "source_table": item.source_table,
            "source_column": item.source_column,
            "business_date": item.business_date,
            "raw_price": item.raw_price,
            "extracted_at": item.extracted_at,
            "query_sha256": item.query_sha256,
            "source_row_sha256": item.source_row_sha256,
        }
        for item in extraction.records
    ]
    return pa.Table.from_pylist(rows, schema=RAW_SCHEMA).sort_by(
        [("source_table", "ascending"), ("source_column", "ascending"), ("business_date", "ascending")]
    )


def _standard_table(extraction: LiveThreeOilExtraction) -> pa.Table:
    rows = [
        {
            "schema_version": "lutou-three-oil-standard/2",
            "dataset_id": item.dataset_id,
            "series_id": item.series_id,
            "provider_dataset_id": item.provider_dataset_id,
            "provider_series_id": item.provider_series_id,
            "provider": item.provider,
            "metadata_source_type": item.metadata_source_type,
            "metadata_status": item.metadata_status,
            "source_quote_unit": item.source_quote_unit,
            "origin_system": "lutou",
            "acquisition_channel": "direct_database",
            "source_locator": item.source_locator,
            "source_table": item.source_table,
            "source_column": item.source_column,
            "business_date": item.business_date,
            "raw_price": item.raw_price,
            "source_currency": item.source_currency,
            "source_unit": item.source_unit,
            "conversion_id": item.conversion_id,
            "mapping_version": MAPPING_VERSION,
            "extracted_at": item.extracted_at,
            "query_sha256": item.query_sha256,
            "source_row_sha256": item.source_row_sha256,
            "quality_status": "PASS",
            "is_usable": True,
        }
        for item in extraction.records
    ]
    return pa.Table.from_pylist(rows, schema=STANDARD_SCHEMA).sort_by(
        [("series_id", "ascending"), ("business_date", "ascending"), ("source_row_sha256", "ascending")]
    )


def _candidate_gate(table: pa.Table, catalog: ThreeOilV1Catalog) -> dict[str, object]:
    rows = table.to_pylist()
    expected = {item.series_id for item in catalog.series}
    observed = {str(item["series_id"]) for item in rows}
    conflicts = 0
    groups: dict[tuple[str, date], set[Decimal]] = {}
    for row in rows:
        key = (str(row["series_id"]), row["business_date"])
        groups.setdefault(key, set()).add(row["raw_price"])
    for prices in groups.values():
        conflicts += int(len(prices) > 1)
    passed = observed == expected and conflicts == 0 and all(row["is_usable"] for row in rows)
    return {
        "quality_status": "PASS" if passed else "FAIL",
        "quality_passed": passed,
        "row_count": table.num_rows,
        "series_count": len(observed),
        "expected_series_count": len(expected),
        "conflicting_duplicate_date_count": conflicts,
        "same_value_duplicate_row_count": table.num_rows - len(groups),
    }


def _canonicalize(
    table: pa.Table,
    catalog: ThreeOilV1Catalog,
) -> tuple[pa.Table, dict[str, object]]:
    by_series = {item.series_id: item for item in catalog.series}
    groups: dict[tuple[str, date], list[dict[str, object]]] = {}
    for row in table.to_pylist():
        groups.setdefault((str(row["series_id"]), row["business_date"]), []).append(row)
    output: list[dict[str, object]] = []
    duplicates = 0
    for (series_id, business_date), rows in groups.items():
        prices = {row["raw_price"] for row in rows}
        if len(prices) != 1:
            raise LutouGoalBError("Goal B canonical source collision")
        contract = by_series[series_id]
        selected = sorted(rows, key=lambda item: str(item["source_row_sha256"]))[0]
        duplicates += len(rows) - 1
        output.append(
            {
                "schema_version": "lutou-three-oil-canonical/2",
                "dataset_id": contract.dataset_id,
                "series_id": contract.series_id,
                "provider_dataset_id": selected["provider_dataset_id"],
                "provider_series_id": contract.provider_series_id,
                "provider": contract.provider,
                "metadata_source_type": selected["metadata_source_type"],
                "metadata_status": selected["metadata_status"],
                "source_quote_unit": selected["source_quote_unit"],
                "origin_system": "lutou",
                "acquisition_channel": "direct_database",
                "source_locator": selected["source_locator"],
                "business_date": business_date,
                "value": _convert(next(iter(prices)), contract, catalog),
                "currency": contract.currency,
                "unit": contract.unit,
                "product": contract.product,
                "product_grade": contract.product_grade,
                "country": contract.country,
                "region": contract.region,
                "location_native": contract.location_native,
                "quote_basis": contract.quote_basis,
                "tenor": contract.tenor,
                "price_type": contract.price_type,
                "source_duplicate_count": len(rows) - 1,
                "source_group_sha256": _group_hash(rows),
                "source_policy_version": SOURCE_POLICY_VERSION,
                "captured_at": selected["extracted_at"],
            }
        )
    result = pa.Table.from_pylist(output, schema=CANONICAL_SCHEMA).sort_by(
        [("series_id", "ascending"), ("business_date", "ascending")]
    )
    return result, {
        "quality_status": "PASS",
        "stable_key_duplicates": _duplicate_count(result, STABLE_KEY),
        "collision_count": 0,
        "same_value_duplicate_row_count": duplicates,
        "source_policy_version": SOURCE_POLICY_VERSION,
    }


def _convert(
    raw_price: Decimal,
    contract: CanonicalSeriesContract,
    catalog: ThreeOilV1Catalog,
) -> Decimal:
    if contract.conversion_id is None:
        return raw_price
    rule = catalog.conversion_by_id(contract.conversion_id)
    if rule.operation == "multiply":
        return raw_price * rule.operand
    if rule.operation == "divide":
        return raw_price / rule.operand
    raise LutouGoalBError("Goal B conversion operation is unsupported")


def _group_hash(rows: Sequence[Mapping[str, object]]) -> str:
    payload = json.dumps(
        sorted(str(item["source_row_sha256"]) for item in rows),
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _merge_current_reference(previous: pa.Table, window: pa.Table, start: date) -> pa.Table:
    """Reference row semantics retained for golden equivalence tests only."""

    if previous.schema != CANONICAL_SCHEMA or window.schema != CANONICAL_SCHEMA:
        raise LutouGoalBError("Goal B incremental schemas differ")
    old_rows = [item for item in previous.to_pylist() if item["business_date"] < start]
    previous_window = {
        (str(item["series_id"]), item["business_date"]): item
        for item in previous.to_pylist()
        if item["business_date"] >= start
    }
    merged: list[dict[str, object]] = []
    for row in window.to_pylist():
        key = (str(row["series_id"]), row["business_date"])
        old = previous_window.get(key)
        merged.append(
            old
            if old is not None and old["source_group_sha256"] == row["source_group_sha256"]
            else row
        )
    result = pa.Table.from_pylist([*old_rows, *merged], schema=CANONICAL_SCHEMA).sort_by(
        [("series_id", "ascending"), ("business_date", "ascending")]
    )
    if _duplicate_count(result, STABLE_KEY):
        raise LutouGoalBError("Goal B incremental merge produced duplicate keys")
    return result


def _merge_current(previous: pa.Table, window: pa.Table, start: date) -> pa.Table:
    if previous.schema != CANONICAL_SCHEMA or window.schema != CANONICAL_SCHEMA:
        raise LutouGoalBError("Goal B incremental schemas differ")
    try:
        return merge_authoritative_window(
            previous,
            window,
            keys=STABLE_KEY,
            date_column="business_date",
            lower=start,
            hash_column="source_group_sha256",
        )
    except ArrowWindowUpsertError as exc:
        raise LutouGoalBError(f"Goal B incremental merge failed: {exc}") from None


def _validate_canonical(table: pa.Table, catalog: ThreeOilV1Catalog) -> None:
    if table.schema != CANONICAL_SCHEMA or table.num_rows == 0:
        raise LutouGoalBError("Goal B Canonical table is invalid")
    if _duplicate_count(table, STABLE_KEY):
        raise LutouGoalBError("Goal B Canonical stable key is duplicated")
    if set(table["series_id"].to_pylist()) != {item.series_id for item in catalog.series}:
        raise LutouGoalBError("Goal B Canonical series coverage is incomplete")
    if set(table["acquisition_channel"].to_pylist()) != {"direct_database"}:
        raise LutouGoalBError("Goal B Canonical acquisition channel is invalid")
    expected_providers = {item.series_id: item.provider for item in catalog.series}
    if any(
        row["provider"] != expected_providers[str(row["series_id"])]
        for row in table.select(("series_id", "provider")).to_pylist()
    ):
        raise LutouGoalBError("Goal B Canonical provider lineage is invalid")
    if set(table["metadata_status"].to_pylist()) != {"proven"}:
        raise LutouGoalBError("Goal B Canonical metadata proof is incomplete")


def _seal_candidate(
    runtime: RuntimeContext,
    public_root: Path,
    run_id: str,
    mode: str,
    start: date,
    end: date,
    raw: pa.Table,
    standard: pa.Table,
    extraction: LiveThreeOilExtraction,
    gate: Mapping[str, object],
    connection_proof: Mapping[str, object],
    failure_hook: str | None,
) -> tuple[Path, dict[str, object]]:
    root = assert_runtime_write(runtime, public_root / "candidates")
    result: dict[str, object] = {}

    def build(directory: Path) -> dict[str, object]:
        pq.write_table(raw, directory / "raw.parquet")
        pq.write_table(standard, directory / "standard.parquet")
        files = _file_identities(directory, ("raw.parquet", "standard.parquet"))
        dates = standard["business_date"].to_pylist()
        manifest = {
            "schema_version": "lutou-goal-b-candidate/2",
            "run_id": run_id,
            "source": "lutou",
            "scope": "three-oil-v1-sealed-series",
            "mode": mode,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "connection_proof": dict(connection_proof),
            "query_window_start": start.isoformat(),
            "query_window_end": end.isoformat(),
            "source_min_date": min(dates).isoformat(),
            "source_max_date": max(dates).isoformat(),
            "query_plans": [item.safe_manifest_fields() for item in extraction.plans],
            "queries": [item.safe_manifest_fields() for item in extraction.queries],
            "table_row_counts": dict(extraction.table_row_counts),
            "metadata_provenance": _metadata_provenance(standard),
            "quality": dict(gate),
            "files": files,
            "promotion_authorized": True,
        }
        _write_json(directory / "manifest.json", manifest)
        if failure_hook == "manifest_missing":
            (directory / "manifest.json").unlink()
        _verify_candidate(directory, manifest)
        result.update(manifest)
        return manifest

    directory, _ = seal_immutable_candidate(root, run_id, build)
    return directory, result


def _seal_canonical(
    runtime: RuntimeContext,
    public_root: Path,
    run_id: str,
    candidate_directory: Path,
    observations: pa.Table,
    report: Mapping[str, object],
) -> tuple[Path, dict[str, object]]:
    root = assert_runtime_write(runtime, public_root / "canonical-candidates")
    result: dict[str, object] = {}

    def build(directory: Path) -> dict[str, object]:
        pq.write_table(observations, directory / "observations.parquet")
        dates = observations["business_date"].to_pylist()
        candidate_manifest = _read_json(candidate_directory / "manifest.json")
        manifest = {
            "schema_version": "lutou-goal-b-canonical/2",
            "run_id": run_id,
            "source": "lutou",
            "scope": "three-oil-v1-sealed-series",
            "candidate_manifest_sha256": identify_file(candidate_directory / "manifest.json").sha256,
            "row_count": observations.num_rows,
            "series_count": len(set(observations["series_id"].to_pylist())),
            "min_date": min(dates).isoformat(),
            "max_date": max(dates).isoformat(),
            "metadata_provenance": candidate_manifest["metadata_provenance"],
            "quality": dict(report),
            "files": _file_identities(directory, ("observations.parquet",)),
        }
        _write_json(directory / "manifest.json", manifest)
        _verify_canonical(directory, manifest)
        result.update(manifest)
        return manifest

    directory, _ = seal_immutable_candidate(root, run_id, build)
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
    root = assert_runtime_write(runtime, public_root / "releases")
    result: dict[str, object] = {}

    def build(directory: Path) -> dict[str, object]:
        source = canonical_directory / "observations.parquet"
        destination = directory / "observations.parquet"
        destination.write_bytes(source.read_bytes())
        manifest = {
            "schema_version": "lutou-goal-b-current/2",
            "release_id": run_id,
            "source": "lutou",
            "scope": "three-oil-v1-sealed-series",
            "promoted_at": datetime.now(timezone.utc).isoformat(),
            "canonical_manifest_sha256": identify_file(canonical_directory / "manifest.json").sha256,
            "source_max_date": candidate_manifest["source_max_date"],
            "row_count": canonical_manifest["row_count"],
            "series_count": canonical_manifest["series_count"],
            "min_date": canonical_manifest["min_date"],
            "max_date": canonical_manifest["max_date"],
            "metadata_provenance": candidate_manifest["metadata_provenance"],
            "quality_status": "PASS",
            "files": _file_identities(directory, ("observations.parquet",)),
        }
        _write_json(directory / "manifest.json", manifest)
        _verify_release(directory, manifest)
        result.update(manifest)
        return manifest

    directory, _ = seal_immutable_candidate(root, run_id, build)
    if failure_hook == "promote_before_pointer":
        raise LutouGoalBError("injected Goal B pre-pointer promotion failure")
    pointer = assert_runtime_write(runtime, public_root / "current.json")
    manifest_id = identify_file(directory / "manifest.json")
    atomic_write_json(
        pointer,
        {
            "schema_version": 1,
            "release_id": run_id,
            "manifest_sha256": manifest_id.sha256,
        },
    )
    loaded = load_current(public_root)
    if loaded is None or loaded.release_id != run_id:
        raise LutouGoalBError("Goal B post-promotion verification failed")
    return directory, result


def _verify_candidate(directory: Path, manifest: Mapping[str, object]) -> None:
    if manifest["source"] != "lutou" or not manifest["promotion_authorized"]:
        raise LutouGoalBError("Goal B Candidate manifest is blocked")
    if not (directory / "manifest.json").is_file():
        raise LutouGoalBError("Goal B Candidate manifest is missing")
    _verify_files(directory, manifest["files"])


def _verify_canonical(directory: Path, manifest: Mapping[str, object]) -> None:
    quality = manifest["quality"]
    if quality["quality_status"] != "PASS" or quality["stable_key_duplicates"]:
        raise LutouGoalBError("Goal B Canonical manifest is blocked")
    _verify_files(directory, manifest["files"])


def _verify_release(directory: Path, manifest: Mapping[str, object]) -> None:
    if manifest.get("quality_status") != "PASS":
        raise LutouGoalBError("Goal B release manifest is blocked")
    _verify_files(directory, manifest["files"])


def _metadata_provenance(standard: pa.Table) -> dict[str, object]:
    official = {}
    for row in standard.select(
        (
            "provider_series_id",
            "provider",
            "metadata_source_type",
            "metadata_status",
            "source_quote_unit",
            "source_currency",
        )
    ).to_pylist():
        if row["metadata_source_type"] != "official_provider_website":
            continue
        official[str(row["provider_series_id"])] = {
            "provider": row["provider"],
            "metadata_source_type": row["metadata_source_type"],
            "metadata_status": row["metadata_status"],
            "currency": row["source_currency"],
            "source_quote_unit": row["source_quote_unit"],
            "business_quote_unit": "USD/T",
        }
    return {
        "official_provider_series": [official[key] | {"provider_series_id": key} for key in sorted(official)],
        "scope_rule": "exact official-series identity matches only",
    }


def _verify_files(directory: Path, files: object) -> None:
    if not isinstance(files, dict):
        raise LutouGoalBError("Goal B file manifest is invalid")
    for name, expected in files.items():
        identity = identify_file(directory / str(name))
        if identity.sha256 != expected["sha256"] or identity.size_bytes != expected["size_bytes"]:
            raise LutouGoalBError("Goal B file identity mismatch")


def _file_identities(directory: Path, names: Sequence[str]) -> dict[str, object]:
    return {
        name: {
            "sha256": identify_file(directory / name).sha256,
            "size_bytes": identify_file(directory / name).size_bytes,
        }
        for name in names
    }


def _duplicate_count(table: pa.Table, keys: Sequence[str]) -> int:
    seen: set[tuple[object, ...]] = set()
    duplicates = 0
    for row in table.select(keys).to_pylist():
        key = tuple(row[item] for item in keys)
        duplicates += int(key in seen)
        seen.add(key)
    return duplicates


def _manifest_date(manifest: Mapping[str, object], field: str) -> date:
    try:
        return date.fromisoformat(str(manifest[field]))
    except (KeyError, TypeError, ValueError) as exc:
        raise LutouGoalBError("Goal B Current source date is invalid") from exc


def _require_runtime(runtime: RuntimeContext) -> None:
    if runtime.mode is not RuntimeMode.ISOLATED_DEV or runtime.module_id != "international-spread":
        raise LutouGoalBError("Goal B requires the International Spread isolated runtime")


def _write_json(path: Path, payload: object) -> None:
    _safe_metadata(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LutouGoalBError(
            f"Goal B JSON artifact cannot be read: {type(exc).__name__}"
        ) from None
    if not isinstance(value, dict):
        raise LutouGoalBError("Goal B JSON artifact must be an object")
    return value


def _safe_metadata(payload: object) -> None:
    forbidden = {
        "password",
        "passwd",
        "secret",
        "token",
        "dsn",
        "host",
        "user",
        "username",
        "private_key",
    }
    absolute = re.compile(r"(?:[A-Za-z]:[\\/]|/home/|/Users/)")

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).casefold() in forbidden:
                    raise LutouGoalBError("Goal B manifest contains a sensitive field")
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
        elif isinstance(value, str) and absolute.search(value):
            raise LutouGoalBError("Goal B manifest contains an absolute path")

    visit(payload)
