"""Goal A Tankan ingestion into an isolated public Market Data Current.

This module deliberately owns no page or consumer logic.  It composes the
existing read-only Tankan client, provider adapters, immutable candidates and
runtime authorization primitives into one fail-closed producer pipeline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Sequence

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from agri_research_agent.data_sources.tankan.fx_adapter import (
    FX_CANDIDATE_SCHEMA,
    FX_RAW_SCHEMA,
    adapt_fx,
    load_fx_config,
)
from agri_research_agent.data_sources.tankan.market_price_adapter import (
    MARKET_CANDIDATE_SCHEMA,
    MARKET_RAW_SCHEMA,
    adapt_market_price,
    load_market_config,
)
from agri_research_agent.data_sources.tankan.models import QueryPlanProof, QuerySpec
from agri_research_agent.data_sources.tankan.queries import FX_WINDOW_QUERY, MARKET_WINDOW_QUERY
from agri_research_agent.shared.atomic_storage import atomic_write_json
from agri_research_agent.shared.file_identity import FileIdentity, identify_file
from agri_research_agent.shared.immutable_candidate import seal_immutable_candidate, validate_candidate_id
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode, assert_runtime_write


LOOKBACK_DAYS = 31
MARKET_FULL_START = date(1969, 6, 26)
FX_FULL_START = date(1981, 1, 2)
SOURCE_POLICY_VERSION = "goal-a-english-then-zh-if-equal-v1"
MARKET_PRODUCTS = frozenset({"SOYBEAN", "SOYBEAN_MEAL", "SOYBEAN_OIL", "PALM_OIL"})
MARKET_SERIES = {
    "SOYBEAN": "market.quote.cbot.soybean.delivery.close.unknown",
    "SOYBEAN_MEAL": "market.quote.cbot.soybean-meal.delivery.close.unknown",
    "SOYBEAN_OIL": "market.quote.cbot.soybean-oil.delivery.close.unknown",
    "PALM_OIL": "market.quote.bmd.palm-oil.delivery.close.unknown",
}
MARKET_STABLE_KEY = (
    "business_date",
    "exchange",
    "product",
    "instrument_id",
    "price_type",
    "session",
)
FX_STABLE_KEY = (
    "quote_date",
    "base_currency",
    "quote_currency",
    "tenor",
    "rate_type",
)


def _canonical_schema(source: pa.Schema) -> pa.Schema:
    fields: list[pa.Field] = []
    for field in source:
        if field.name == "series_id_candidate":
            fields.append(pa.field("series_id", pa.string(), nullable=False))
        else:
            fields.append(field)
    fields.append(pa.field("source_policy_version", pa.string(), nullable=False))
    return pa.schema(fields)


MARKET_CANONICAL_SCHEMA = _canonical_schema(MARKET_CANDIDATE_SCHEMA)
FX_CANONICAL_SCHEMA = _canonical_schema(FX_CANDIDATE_SCHEMA)


class TankanGoalAError(RuntimeError):
    """Raised before Current can be changed when any Goal A gate fails."""


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    mode: str
    candidate_directory: Path
    canonical_directory: Path
    current_directory: Path
    current_manifest: dict[str, object]
    candidate_manifest: dict[str, object]
    canonical_manifest: dict[str, object]
    promoted: bool


@dataclass(frozen=True, slots=True)
class CurrentRelease:
    release_id: str
    directory: Path
    manifest: dict[str, object]
    market: pa.Table
    fx: pa.Table


def run_goal_a(
    client: object,
    *,
    runtime: RuntimeContext,
    run_id: str,
    market_config_path: str | Path,
    fx_config_path: str | Path,
    end_date: date,
    full_load: bool,
    failure_hook: str | None = None,
    market_full_start: date = MARKET_FULL_START,
    fx_full_start: date = FX_FULL_START,
) -> RunResult:
    """Build Candidate, Canonical and optionally atomically promote Current."""

    safe_run_id = validate_candidate_id(run_id)
    _require_runtime(runtime)
    if type(end_date) is not date:
        raise TankanGoalAError("end_date must be an exact date")
    public_root = assert_runtime_write(runtime, runtime.runtime_root / "public-market-data" / "tankan")
    public_root.mkdir(parents=True, exist_ok=True)
    current = load_current(public_root)
    if full_load and current is not None:
        raise TankanGoalAError("full load refuses to overwrite an existing Current")
    if not full_load and current is None:
        raise TankanGoalAError("incremental run requires an existing Current")

    mode = "full" if full_load else "incremental-31-day-lookback"
    market_start = market_full_start if current is None else _date_field(current.manifest, "market", "source_max_date") - timedelta(days=LOOKBACK_DAYS)
    fx_start = fx_full_start if current is None else _date_field(current.manifest, "fx", "source_max_date") - timedelta(days=LOOKBACK_DAYS)
    captured_at = datetime.now(timezone.utc)

    raw_market, market_plans = _extract(client, MARKET_WINDOW_QUERY, MARKET_RAW_SCHEMA, market_start, end_date)
    raw_fx, fx_plans = _extract(client, FX_WINDOW_QUERY, FX_RAW_SCHEMA, fx_start, end_date)
    if raw_market.num_rows == 0 or raw_fx.num_rows == 0:
        raise TankanGoalAError("empty source window is blocked")
    if failure_hook == "connection":
        raise TankanGoalAError("injected connection failure")

    market_config_file = Path(market_config_path)
    fx_config_file = Path(fx_config_path)
    previous_market_date = None if current is None else _date_field(current.manifest, "market", "source_max_date")
    previous_fx_date = None if current is None else _date_field(current.manifest, "fx", "source_max_date")

    candidate_root = assert_runtime_write(runtime, public_root / "candidates")
    candidate_directory, candidate_manifest = _seal_candidate(
        candidate_root,
        safe_run_id,
        client=client,
        raw_market=raw_market,
        raw_fx=raw_fx,
        market_plans=market_plans,
        fx_plans=fx_plans,
        market_query_window=(market_start, end_date),
        fx_query_window=(fx_start, end_date),
        market_config_path=market_config_file,
        fx_config_path=fx_config_file,
        captured_at=captured_at,
        previous_market_date=previous_market_date,
        previous_fx_date=previous_fx_date,
        mode=mode,
        failure_hook=failure_hook,
    )

    canonical_root = assert_runtime_write(runtime, public_root / "canonical-candidates")
    canonical_directory, canonical_manifest = _seal_canonical(
        canonical_root,
        safe_run_id,
        candidate_directory=candidate_directory,
        candidate_manifest=candidate_manifest,
        current=current,
        failure_hook=failure_hook,
    )
    market = pq.read_table(canonical_directory / "market.parquet")
    fx = pq.read_table(canonical_directory / "fx.parquet")

    if current is not None and market.equals(current.market) and fx.equals(current.fx):
        return RunResult(
            run_id=safe_run_id,
            mode=mode,
            candidate_directory=candidate_directory,
            canonical_directory=canonical_directory,
            current_directory=current.directory,
            current_manifest=current.manifest,
            candidate_manifest=candidate_manifest,
            canonical_manifest=canonical_manifest,
            promoted=False,
        )

    current_directory, current_manifest = _promote(
        runtime,
        public_root,
        safe_run_id,
        canonical_directory,
        canonical_manifest,
        failure_hook=failure_hook,
    )
    return RunResult(
        run_id=safe_run_id,
        mode=mode,
        candidate_directory=candidate_directory,
        canonical_directory=canonical_directory,
        current_directory=current_directory,
        current_manifest=current_manifest,
        candidate_manifest=candidate_manifest,
        canonical_manifest=canonical_manifest,
        promoted=True,
    )


def canonicalize_market(source: pa.Table) -> tuple[pa.Table, dict[str, object]]:
    if source.schema != MARKET_CANDIDATE_SCHEMA:
        raise TankanGoalAError("market Standard schema mismatch")
    groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for row in source.to_pylist():
        if row["product"] not in MARKET_PRODUCTS:
            raise TankanGoalAError("market Standard contains a non-Goal-A product")
        key = tuple(row[name] for name in MARKET_STABLE_KEY)
        groups.setdefault(key, []).append(row)

    output: list[dict[str, object]] = []
    collision_count = 0
    same_price_overlap_count = 0
    exception_count = 0
    for rows in groups.values():
        prices = {row["price"] for row in rows}
        if len(rows) > 1:
            if len(prices) != 1:
                collision_count += 1
                continue
            same_price_overlap_count += 1
        usable = [row for row in rows if row["is_usable"]]
        if not usable:
            exception_count += len(rows)
            continue
        selected = sorted(
            usable,
            key=lambda row: (0 if str(row["provider_series_id"]).endswith(".en") else 1, str(row["provider_series_id"])),
        )[0]
        canonical = dict(selected)
        canonical["series_id"] = MARKET_SERIES[str(selected["product"])]
        canonical.pop("series_id_candidate")
        canonical["source_policy_version"] = SOURCE_POLICY_VERSION
        output.append(canonical)
    if collision_count:
        raise TankanGoalAError("canonical market source collision")
    table = pa.Table.from_pylist(output, schema=MARKET_CANONICAL_SCHEMA)
    table = _sort(table, MARKET_STABLE_KEY)
    duplicates = _duplicate_count(table, MARKET_STABLE_KEY)
    if duplicates:
        raise TankanGoalAError("canonical market stable key is duplicated")
    return table, {
        "quality_status": "PASS",
        "source_policy_version": SOURCE_POLICY_VERSION,
        "row_count": table.num_rows,
        "stable_key_duplicates": duplicates,
        "collision_count": collision_count,
        "same_price_overlap_count": same_price_overlap_count,
        "retained_standard_exception_count": exception_count,
    }


def canonicalize_fx(source: pa.Table) -> tuple[pa.Table, dict[str, object]]:
    if source.schema != FX_CANDIDATE_SCHEMA:
        raise TankanGoalAError("FX Standard schema mismatch")
    output: list[dict[str, object]] = []
    exceptions = 0
    for row in source.to_pylist():
        if row["base_currency"] != "USD" or row["quote_currency"] != "CNH" or row["rate_unit"] != "CNH_per_USD":
            raise TankanGoalAError("FX direction is not USD/CNH")
        if not row["is_usable"]:
            exceptions += 1
            continue
        canonical = dict(row)
        canonical["series_id"] = f"fx.usd.cnh.{str(row['tenor']).lower()}.unspecified"
        canonical.pop("series_id_candidate")
        canonical["source_policy_version"] = "tankan-only-unspecified-rate-type-v1"
        output.append(canonical)
    table = pa.Table.from_pylist(output, schema=FX_CANONICAL_SCHEMA)
    table = _sort(table, FX_STABLE_KEY)
    duplicates = _duplicate_count(table, FX_STABLE_KEY)
    if duplicates:
        raise TankanGoalAError("canonical FX stable key is duplicated")
    return table, {
        "quality_status": "PASS",
        "row_count": table.num_rows,
        "stable_key_duplicates": duplicates,
        "collision_count": 0,
        "retained_standard_exception_count": exceptions,
        "direction": "USD/CNH",
        "unit": "CNH_per_USD",
        "rate_type": "unspecified",
    }


def load_current(public_root: str | Path) -> CurrentRelease | None:
    root = Path(public_root)
    pointer = root / "current.json"
    if not pointer.exists():
        return None
    payload = _read_json(pointer)
    if set(payload) != {"schema_version", "release_id", "manifest_sha256"} or payload["schema_version"] != 1:
        raise TankanGoalAError("Current pointer is invalid")
    release_id = validate_candidate_id(str(payload["release_id"]))
    directory = (root / "releases" / release_id).resolve()
    if directory.parent != (root / "releases").resolve() or not directory.is_dir():
        raise TankanGoalAError("Current release directory is invalid")
    manifest_path = directory / "manifest.json"
    identity = identify_file(manifest_path)
    if identity.sha256 != payload["manifest_sha256"]:
        raise TankanGoalAError("Current manifest identity mismatch")
    manifest = _read_json(manifest_path)
    _verify_release_manifest(directory, manifest)
    market = pq.read_table(directory / "market.parquet")
    fx = pq.read_table(directory / "fx.parquet")
    if market.schema != MARKET_CANONICAL_SCHEMA or fx.schema != FX_CANONICAL_SCHEMA:
        raise TankanGoalAError("Current schema mismatch")
    return CurrentRelease(release_id, directory, manifest, market, fx)


def _extract(
    client: object,
    query: QuerySpec,
    schema: pa.Schema,
    start: date,
    end: date,
) -> tuple[pa.Table, list[QueryPlanProof]]:
    if start > end:
        raise TankanGoalAError("query window start follows end")
    rows: list[dict[str, object]] = []
    plans: list[QueryPlanProof] = []
    for window_start, window_end in _windows(start, end, query.max_window_days):
        plan, batches = client.plan_stream(query, (window_start, window_end), batch_size=10_000)
        plans.append(plan)
        for batch in batches:
            if batch.query != query or batch.plan != plan:
                raise TankanGoalAError("source batch identity drifted")
            rows.extend(dict(row) for row in batch.rows)
    return pa.Table.from_pylist(rows, schema=schema), plans


def _windows(start: date, end: date, max_days: int) -> Iterator[tuple[date, date]]:
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=max_days))
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


def _seal_candidate(
    root: Path,
    run_id: str,
    *,
    client: object,
    raw_market: pa.Table,
    raw_fx: pa.Table,
    market_plans: Sequence[QueryPlanProof],
    fx_plans: Sequence[QueryPlanProof],
    market_query_window: tuple[date, date],
    fx_query_window: tuple[date, date],
    market_config_path: Path,
    fx_config_path: Path,
    captured_at: datetime,
    previous_market_date: date | None,
    previous_fx_date: date | None,
    mode: str,
    failure_hook: str | None,
) -> tuple[Path, dict[str, object]]:
    manifest_result: dict[str, object] = {}

    def build(directory: Path) -> None:
        raw_market_file = directory / "market_raw.parquet"
        raw_fx_file = directory / "fx_raw.parquet"
        pq.write_table(raw_market, raw_market_file, compression="zstd")
        pq.write_table(raw_fx, raw_fx_file, compression="zstd")
        raw_market_id = identify_file(raw_market_file)
        raw_fx_id = identify_file(raw_fx_file)
        market_result = adapt_market_price(
            raw_market,
            load_market_config(market_config_path),
            snapshot_sha256=raw_market_id.sha256,
            captured_at=captured_at,
            previous_latest_date=previous_market_date,
        )
        fx_result = adapt_fx(
            raw_fx,
            load_fx_config(fx_config_path),
            snapshot_sha256=raw_fx_id.sha256,
            captured_at=captured_at,
            previous_latest_date=previous_fx_date,
        )
        market_gate = _candidate_market_gate(market_result.table, market_result.collision_report)
        fx_gate = _candidate_fx_gate(fx_result.table)
        if failure_hook == "candidate_qc":
            market_gate["quality_passed"] = False
        if not market_gate["quality_passed"] or not fx_gate["quality_passed"]:
            raise TankanGoalAError("Candidate quality gate failed")
        files = {
            "market_standard.parquet": market_result.table,
            "fx_standard.parquet": fx_result.table,
        }
        identities: dict[str, dict[str, object]] = {
            "market_raw.parquet": _identity(raw_market_id),
            "fx_raw.parquet": _identity(raw_fx_id),
        }
        for filename, table in files.items():
            path = directory / filename
            pq.write_table(table, path, compression="zstd")
            identities[filename] = _identity(identify_file(path))
        reports = {
            "market_quality.json": market_result.quality_report,
            "market_collision.json": market_result.collision_report,
            "market_gate.json": market_gate,
            "fx_quality.json": fx_result.quality_report,
            "fx_gate.json": fx_gate,
        }
        for filename, report in reports.items():
            _write_json(directory / filename, report)
            identities[filename] = _identity(identify_file(directory / filename))
        manifest = {
            "schema_version": "tankan-goal-a-candidate/1",
            "run_id": run_id,
            "source": "tankan",
            "mode": mode,
            "generated_at": captured_at.isoformat(),
            "connection_proof": client.proof.safe_manifest_fields(),
            "market": _domain_manifest(raw_market, market_result.table, MARKET_WINDOW_QUERY, market_query_window, market_plans, market_gate),
            "fx": _domain_manifest(raw_fx, fx_result.table, FX_WINDOW_QUERY, fx_query_window, fx_plans, fx_gate),
            "files": identities,
            "quality_status": "PASS_WITH_RETAINED_EXCEPTIONS" if int(market_gate["exception_count"]) else "PASS",
            "quality_passed": True,
            "promotion_authorized": True,
        }
        _write_json(directory / "manifest.json", manifest)
        manifest_result.update(manifest)

    directory, _ = seal_immutable_candidate(root, run_id, build)
    return directory, manifest_result


def _seal_canonical(
    root: Path,
    run_id: str,
    *,
    candidate_directory: Path,
    candidate_manifest: dict[str, object],
    current: CurrentRelease | None,
    failure_hook: str | None,
) -> tuple[Path, dict[str, object]]:
    _verify_candidate_manifest(candidate_directory, candidate_manifest)
    source_market = pq.read_table(candidate_directory / "market_standard.parquet")
    source_fx = pq.read_table(candidate_directory / "fx_standard.parquet")
    market_window, market_report = canonicalize_market(source_market)
    fx_window, fx_report = canonicalize_fx(source_fx)
    if failure_hook == "canonical_collision":
        raise TankanGoalAError("injected canonical collision")
    market = market_window if current is None else _merge_current(current.market, market_window, MARKET_STABLE_KEY, "business_date")
    fx = fx_window if current is None else _merge_current(current.fx, fx_window, FX_STABLE_KEY, "quote_date")
    market_report = {**market_report, "row_count": market.num_rows, "stable_key_duplicates": _duplicate_count(market, MARKET_STABLE_KEY)}
    fx_report = {**fx_report, "row_count": fx.num_rows, "stable_key_duplicates": _duplicate_count(fx, FX_STABLE_KEY)}
    if market_report["stable_key_duplicates"] or fx_report["stable_key_duplicates"]:
        raise TankanGoalAError("Canonical stable key validation failed")
    result: dict[str, object] = {}

    def build(directory: Path) -> None:
        files: dict[str, dict[str, object]] = {}
        for filename, table in (("market.parquet", market), ("fx.parquet", fx)):
            path = directory / filename
            pq.write_table(table, path, compression="zstd")
            files[filename] = _identity(identify_file(path))
        reports = {"market_qc.json": market_report, "fx_qc.json": fx_report}
        for filename, payload in reports.items():
            _write_json(directory / filename, payload)
            files[filename] = _identity(identify_file(directory / filename))
        manifest = {
            "schema_version": "tankan-goal-a-canonical/1",
            "run_id": run_id,
            "source": "tankan",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_candidate_manifest_sha256": identify_file(candidate_directory / "manifest.json").sha256,
            "market": _canonical_domain_manifest(market, "business_date", market_report),
            "fx": _canonical_domain_manifest(fx, "quote_date", fx_report),
            "files": files,
            "quality_status": "PASS",
            "quality_passed": True,
            "promotion_authorized": True,
        }
        _write_json(directory / "manifest.json", manifest)
        result.update(manifest)

    directory, _ = seal_immutable_candidate(root, run_id, build)
    return directory, result


def _promote(
    runtime: RuntimeContext,
    public_root: Path,
    run_id: str,
    canonical_directory: Path,
    canonical_manifest: dict[str, object],
    *,
    failure_hook: str | None,
) -> tuple[Path, dict[str, object]]:
    if not canonical_manifest.get("quality_passed") or not canonical_manifest.get("promotion_authorized"):
        raise TankanGoalAError("Canonical manifest is not promotable")
    if failure_hook == "manifest_missing":
        raise TankanGoalAError("injected incomplete manifest")
    canonical_manifest_path = canonical_directory / "manifest.json"
    if not canonical_manifest_path.is_file():
        raise TankanGoalAError("Canonical manifest is missing")
    market = pq.read_table(canonical_directory / "market.parquet")
    fx = pq.read_table(canonical_directory / "fx.parquet")
    releases_root = assert_runtime_write(runtime, public_root / "releases")
    result: dict[str, object] = {}

    def build(directory: Path) -> None:
        files: dict[str, dict[str, object]] = {}
        for filename, table in (("market.parquet", market), ("fx.parquet", fx)):
            path = directory / filename
            pq.write_table(table, path, compression="zstd")
            files[filename] = _identity(identify_file(path))
        candidate = _read_json(canonical_directory.parent.parent / "candidates" / run_id / "manifest.json")
        manifest = {
            "schema_version": "tankan-goal-a-current/1",
            "release_id": run_id,
            "source": "tankan",
            "promoted_at": datetime.now(timezone.utc).isoformat(),
            "canonical_manifest_sha256": identify_file(canonical_manifest_path).sha256,
            "market": {**canonical_manifest["market"], "source_max_date": candidate["market"]["source_max_date"]},
            "fx": {**canonical_manifest["fx"], "source_max_date": candidate["fx"]["source_max_date"]},
            "files": files,
            "quality_status": "PASS",
        }
        _write_json(directory / "manifest.json", manifest)
        result.update(manifest)

    directory, _ = seal_immutable_candidate(releases_root, run_id, build)
    _verify_release_manifest(directory, result)
    if failure_hook == "promote_before_pointer":
        raise TankanGoalAError("injected pre-pointer promotion failure")
    pointer = assert_runtime_write(runtime, public_root / "current.json")
    pointer.parent.mkdir(parents=True, exist_ok=True)
    manifest_id = identify_file(directory / "manifest.json")
    atomic_write_json(pointer, {"schema_version": 1, "release_id": run_id, "manifest_sha256": manifest_id.sha256})
    loaded = load_current(public_root)
    if loaded is None or loaded.release_id != run_id:
        raise TankanGoalAError("post-promotion Current verification failed")
    return directory, result


def _merge_current(previous: pa.Table, window: pa.Table, keys: Sequence[str], date_column: str) -> pa.Table:
    if previous.schema != window.schema or window.num_rows == 0:
        raise TankanGoalAError("incremental merge schema/window is invalid")
    lower = pc.min(window[date_column]).as_py()
    old_rows = previous.filter(pc.less(previous[date_column], pa.scalar(lower, type=pa.date32()))).to_pylist()
    previous_window = {
        tuple(row[name] for name in keys): row
        for row in previous.filter(pc.greater_equal(previous[date_column], pa.scalar(lower, type=pa.date32()))).to_pylist()
    }
    merged_window: list[dict[str, object]] = []
    for row in window.to_pylist():
        key = tuple(row[name] for name in keys)
        old = previous_window.get(key)
        if old is not None and old.get("source_row_sha256") == row.get("source_row_sha256"):
            merged_window.append(old)
        else:
            merged_window.append(row)
    table = pa.Table.from_pylist([*old_rows, *merged_window], schema=previous.schema)
    table = _sort(table, keys)
    if _duplicate_count(table, keys):
        raise TankanGoalAError("incremental merge produced duplicate stable keys")
    return table


def _candidate_market_gate(table: pa.Table, collision: dict[str, object]) -> dict[str, object]:
    products = set(table["product"].to_pylist())
    unexpected = products - MARKET_PRODUCTS
    exception_count = table.num_rows - sum(bool(value) for value in table["is_usable"].to_pylist())
    differing = int(collision["different_price_key_count"])
    passed = products == MARKET_PRODUCTS and not unexpected and differing == 0
    return {
        "quality_status": "PASS_WITH_RETAINED_EXCEPTIONS" if passed and exception_count else ("PASS" if passed else "FAIL"),
        "quality_passed": passed,
        "row_count": table.num_rows,
        "exception_count": exception_count,
        "products": sorted(products),
        "unexpected_products": sorted(unexpected),
        "different_price_collision_count": differing,
        "stable_provider_key_duplicates": 0,
    }


def _candidate_fx_gate(table: pa.Table) -> dict[str, object]:
    tenors = set(table["tenor"].to_pylist())
    expected = {"SPOT", *(f"{month}M" for month in range(1, 13))}
    directions = {(row["base_currency"], row["quote_currency"], row["rate_unit"]) for row in table.select(["base_currency", "quote_currency", "rate_unit"]).to_pylist()}
    exception_count = table.num_rows - sum(bool(value) for value in table["is_usable"].to_pylist())
    duplicates = _duplicate_count(table, FX_STABLE_KEY)
    passed = tenors == expected and directions == {("USD", "CNH", "CNH_per_USD")} and not duplicates and not exception_count
    return {
        "quality_status": "PASS" if passed else "FAIL",
        "quality_passed": passed,
        "row_count": table.num_rows,
        "exception_count": exception_count,
        "tenors": sorted(tenors),
        "direction": "USD/CNH",
        "unit": "CNH_per_USD",
        "stable_key_duplicates": duplicates,
    }


def _domain_manifest(raw: pa.Table, standard: pa.Table, query: QuerySpec, window: tuple[date, date], plans: Sequence[QueryPlanProof], gate: dict[str, object]) -> dict[str, object]:
    dates = raw[raw.schema.names[0]].to_pylist()
    return {
        "source_table": str(query.provider.source_locator),
        "query_name": query.name,
        "query_version": query.version,
        "query_sha256": query.sha256,
        "query_window_start": window[0].isoformat(),
        "query_window_end": window[1].isoformat(),
        "query_window_count": len(plans),
        "max_estimated_rows": max(plan.estimated_rows for plan in plans),
        "max_total_cost": max(plan.total_cost for plan in plans),
        "source_min_date": min(dates).isoformat(),
        "source_max_date": max(dates).isoformat(),
        "raw_row_count": raw.num_rows,
        "row_count": standard.num_rows,
        "quality_status": gate["quality_status"],
        "quality_passed": gate["quality_passed"],
    }


def _canonical_domain_manifest(table: pa.Table, date_column: str, report: dict[str, object]) -> dict[str, object]:
    dates = table[date_column].to_pylist()
    return {
        "row_count": table.num_rows,
        "min_date": min(dates).isoformat(),
        "max_date": max(dates).isoformat(),
        "stable_key_duplicates": report["stable_key_duplicates"],
        "collision_count": report["collision_count"],
        "quality_status": report["quality_status"],
    }


def _verify_candidate_manifest(directory: Path, manifest: dict[str, object]) -> None:
    required = {"schema_version", "run_id", "source", "mode", "generated_at", "connection_proof", "market", "fx", "files", "quality_status", "quality_passed", "promotion_authorized"}
    if set(manifest) != required or not manifest["quality_passed"] or not manifest["promotion_authorized"]:
        raise TankanGoalAError("Candidate manifest is incomplete or blocked")
    for filename, expected in manifest["files"].items():
        identity = identify_file(directory / filename)
        if identity.sha256 != expected["sha256"] or identity.size_bytes != expected["size_bytes"]:
            raise TankanGoalAError("Candidate file identity mismatch")
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise TankanGoalAError("Candidate manifest is missing")


def _verify_release_manifest(directory: Path, manifest: dict[str, object]) -> None:
    required = {"schema_version", "release_id", "source", "promoted_at", "canonical_manifest_sha256", "market", "fx", "files", "quality_status"}
    if set(manifest) != required or manifest["quality_status"] != "PASS":
        raise TankanGoalAError("Current manifest is incomplete")
    for filename, expected in manifest["files"].items():
        identity = identify_file(directory / filename)
        if identity.sha256 != expected["sha256"] or identity.size_bytes != expected["size_bytes"]:
            raise TankanGoalAError("Current file identity mismatch")


def _sort(table: pa.Table, keys: Sequence[str]) -> pa.Table:
    return table.sort_by([(name, "ascending") for name in keys]) if table.num_rows else table


def _duplicate_count(table: pa.Table, keys: Sequence[str]) -> int:
    seen: set[tuple[object, ...]] = set()
    duplicates = 0
    for row in table.select(keys).to_pylist():
        key = tuple(row[name] for name in keys)
        if key in seen:
            duplicates += 1
        seen.add(key)
    return duplicates


def _date_field(manifest: dict[str, object], domain: str, name: str) -> date:
    try:
        return date.fromisoformat(str(manifest[domain][name]))
    except (KeyError, TypeError, ValueError) as exc:
        raise TankanGoalAError("Current source date is invalid") from exc


def _require_runtime(runtime: RuntimeContext) -> None:
    if runtime.mode is not RuntimeMode.ISOLATED_DEV or runtime.module_id != "international-spread":
        raise TankanGoalAError("Goal A requires the International Spread isolated-dev runtime")


def _identity(value: FileIdentity) -> dict[str, object]:
    return {"sha256": value.sha256, "size_bytes": value.size_bytes}


def _write_json(path: Path, payload: object) -> None:
    _safe_metadata(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TankanGoalAError("JSON artifact cannot be read") from exc
    if not isinstance(value, dict):
        raise TankanGoalAError("JSON artifact must be an object")
    return value


def _safe_metadata(payload: object) -> None:
    forbidden = {"password", "passwd", "secret", "token", "dsn", "host", "user", "username", "private_key"}
    absolute = re.compile(r"(?:[A-Za-z]:[\\/]|/home/|/Users/)")

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).casefold() in forbidden:
                    raise TankanGoalAError("manifest contains a forbidden sensitive field")
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
        elif isinstance(value, str) and absolute.search(value):
            raise TankanGoalAError("manifest contains an absolute path")

    visit(payload)
