"""Thin accounting over validated Producer outputs; no data transformation."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from agri_research_agent.shared.async_update import (
    FreshnessPolicy, SeriesUpdate, evaluate_update, validate_update_summary,
)
from agri_research_agent.shared.immutable_candidate import seal_immutable_candidate
from agri_research_agent.shared.runtime_context import assert_runtime_write


DOMAINS = ("tankan_market", "fx", "three_oil", "soil_moisture", "weather_observation", "weather_forecast")


def _identity(values: tuple) -> str:
    return str(values[0]) if len(values) == 1 else json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _stats(table: pa.Table, identity: tuple[str, ...], date_column: str) -> dict:
    if not table.num_rows:
        return {}
    grouped = table.group_by(list(identity)).aggregate([(date_column, "max"), (date_column, "count")])
    return {_identity(tuple(row[key] for key in identity)): (date.fromisoformat(row[date_column + "_max"]), row[date_column + "_count"])
            for row in grouped.to_pylist()}


def _anti(left, right, columns):
    return left if not right.num_rows else left.join(right.select(columns), keys=columns, join_type="left anti")


def account_table(*, dataset_id: str, previous: pa.Table | None, source: pa.Table,
                  following: pa.Table, keys: tuple[str, ...], identity: tuple[str, ...],
                  date_column: str, values: tuple[str, ...], as_of_date: date,
                  required: set[str] | None = None, forecast: bool = False,
                  policy: FreshnessPolicy | None = None,
                  source_errors: dict[str, set[str]] | None = None) -> dict:
    """Compare Arrow key sets and values, not date-only/global maxima or promotion IDs."""
    if following.schema != source.schema or (previous is not None and previous.schema != source.schema):
        raise ValueError("ASYNC_CANONICAL_SCHEMA_MISMATCH")
    columns = list(dict.fromkeys((*keys, *identity, *values)))
    def project(table):
        if table is None:
            return pa.table({name: pa.array([], type=pa.string()) for name in columns})
        if any(table[name].null_count for name in keys):
            raise ValueError("ASYNC_NULL_STABLE_KEY")
        arrays = [pc.fill_null(pc.cast(table[name], pa.string()), "<NULL>") for name in columns]
        return pa.Table.from_arrays(arrays, names=columns)
    old, window, nxt = map(project, (previous, source, following))
    before, extracted, after = (_stats(table, identity, date_column) for table in (old, window, nxt))
    expected = required if required is not None else set(before) | set(extracted)
    if (set(before) | set(extracted) | set(source_errors or ())) - set(expected):
        raise ValueError("ASYNC_MAPPING_IDENTITY_NOT_IN_CATALOG")
    errors: dict[str, set[str]] = {name: set() for name in expected}
    for name, reasons in (source_errors or {}).items():
        errors.setdefault(name, set()).update(reasons)
    def note(table, reason):
        for name in _stats(table, identity, date_column):
            errors.setdefault(name, set()).add(reason)
    for table in (old, window, nxt):
        counts = table.group_by(list(keys)).aggregate([(date_column, "count")]) if table.num_rows else None
        if counts is not None:
            note(counts.filter(pc.greater(counts[date_column + "_count"], 1)), "DUPLICATE_STABLE_KEY")
    note(_anti(window, nxt, columns), "SOURCE_PRESENT_CANDIDATE_DROPPED_OR_CHANGED")
    # A source revision may replace the same key; unrelated last-good rows may not disappear.
    note(_anti(_anti(old, window, list(keys)), nxt, columns), "CURRENT_PRESENT_CANDIDATE_DROPPED_OR_CHANGED")
    note(_anti(nxt, pa.concat_tables([old, window]), columns), "NEXT_ROW_NOT_SUPPORTED_BY_SOURCE_OR_CURRENT")
    new = _stats(_anti(nxt, old, list(keys)), identity, date_column)
    changed = _stats(_anti(nxt, old, columns), identity, date_column)
    evidence = {}
    for name in expected:
        evidence[name] = SeriesUpdate(
            before.get(name, (None, 0))[0], extracted.get(name, (None, 0))[0], after.get(name, (None, 0))[0],
            new.get(name, (None, 0))[1],
            changed.get(name, (None, 0))[1] - new.get(name, (None, 0))[1],
            extracted.get(name, (None, 0))[1], tuple(sorted(errors.get(name, ()))),
        )
    report = evaluate_update(
        dataset_id=dataset_id, required=set(expected), series=evidence, next_identities=set(after),
        as_of_date=as_of_date,
        policy=policy or FreshnessPolicy(dataset_id + "/async-1", stale_is_blocking=False),
        date_axis="forecast_valid" if forecast else "business_date", update_basis="content", source_is_window=True,
        verified_empty_source=set(expected) - set(extracted),
    )
    report["required_identities"] = sorted(expected)
    report["source_latest_definition"] = "validated canonical extraction window maximum; null means no row in that window, not a fabricated source inventory date"
    report["stable_row_key"] = list(keys)
    report["stable_identity_key"] = list(identity)
    validate_report(report)
    return report


def validate_report(report: dict) -> None:
    required = set(report.get("required_identities", [row["identity"] for row in report["series"]]))
    validate_update_summary(report, required=required)
    # Basis already has its own validated /2 policy; only validate its summary here.
    if "required_identities" not in report:
        return
    if len(report["required_identities"]) != len(required):
        raise ValueError("ASYNC_REQUIRED_IDENTITY_DUPLICATE")
    if (report["identity_coverage"]["expected_count"] != len(required)
            or report["promotion_allowed"] != (not report["blocking_reasons"])):
        raise ValueError("ASYNC_RESULT_INCONSISTENT")
    failed = report["summary"]["updates"]["ERROR"] or report["summary"]["coverage"]["ERROR"]
    if failed and (report["promotion_allowed"] or report["dataset_status"] != "FAILED"):
        raise ValueError("ASYNC_FAILURE_NOT_BLOCKING")
    def day(value):
        return None if value is None else date.fromisoformat(value)
    evidence = {row["identity"]: SeriesUpdate(
        day(row["previous_latest_date"]), day(row["source_latest_date"]), day(row["next_latest_date"]),
        row["new_row_count"], row["revision_row_count"], row["source_window_row_count"],
        tuple(row["reason"].split(";")) if row["update_status"] == "ERROR" else (),
    ) for row in report["series"]}
    coverage = report["identity_coverage"]
    recomputed = evaluate_update(dataset_id=report["dataset_id"], required=required, series=evidence,
        next_identities=(required - set(coverage["missing"])) | set(coverage["unexpected"]),
        as_of_date=date.fromisoformat(report["as_of_date"]), policy=FreshnessPolicy.from_mapping(report["policy"]),
        date_axis=report["date_axis"], update_basis=report["update_basis"], source_is_window=report["source_is_window"],
        verified_empty_source=set(report["verified_empty_source"]) if "verified_empty_source" in report else None)
    if any(report.get(key) != recomputed.get(key) for key in (
        "summary", "series", "dataset_status", "promotion_allowed", "blocking_reasons", "identity_coverage",
        "missing_source_is_blocking", "verified_empty_source"
    )):
        raise ValueError("ASYNC_RESULT_INCONSISTENT")


def seal_reports(runtime, run_id: str, reports: dict, error_type) -> dict:
    for report in reports.values():
        validate_report(report)
    def build(directory):
        (directory / "manifest.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    seal_immutable_candidate(assert_runtime_write(runtime, runtime.runtime_root / "async-contract-reports"), run_id, build)
    if any(not report["promotion_allowed"] for report in reports.values()):
        raise error_type("Async identity accounting failed; see immutable async-contract-reports")
    return reports


def tankan_reports(runtime, run_id, current, market, fx, candidate_directory, as_of_date, error_type):
    from .tankan_goal_a import MARKET_STABLE_KEY, FX_STABLE_KEY, canonicalize_market, canonicalize_fx
    market_window, _ = canonicalize_market(pq.read_table(candidate_directory / "market_standard.parquet"))
    fx_window, _ = canonicalize_fx(pq.read_table(candidate_directory / "fx_standard.parquet"))
    reports = {}
    for name, previous, source, following, keys, day, values in (
        ("tankan_market", None if current is None else current.market, market_window, market, MARKET_STABLE_KEY, "business_date", ("series_id", "price", "currency", "price_unit")),
        ("fx", None if current is None else current.fx, fx_window, fx, FX_STABLE_KEY, "quote_date", ("series_id", "rate", "rate_unit")),
    ):
        reports[name] = account_table(dataset_id=name, previous=previous, source=source, following=following,
            keys=keys, identity=tuple(key for key in keys if key != day), date_column=day, values=values, as_of_date=as_of_date)
    return seal_reports(runtime, run_id, reports, error_type)


def observation_report(runtime, run_id, name, current, window, following, series, as_of_date, error_type):
    report = account_table(dataset_id=name, previous=None if current is None else current.observations,
        source=window, following=following, keys=("series_id", "business_date"), identity=("series_id",),
        date_column="business_date", values=("value", "unit") if name == "three_oil" else ("source_value", "value_percent", "unit"),
        required={item.series_id for item in series}, as_of_date=as_of_date)
    return seal_reports(runtime, run_id, {name: report}, error_type)


def weather_reports(runtime, run_id, current, candidate_directory, following_directory, catalog, as_of_date, error_type, report_sink=None):
    from .lutou_weather import STABLE_KEY, CANONICAL_SCHEMA, STANDARD_SCHEMA, _decimal
    # Read only the accounting columns, not millions of full Python observation rows.
    columns = [*STABLE_KEY, "data_family", "value", "unit", "source_row_sha256"]
    if pq.read_schema(following_directory / "observations.parquet") != CANONICAL_SCHEMA:
        raise error_type("Weather Async canonical schema mismatch")
    if pq.read_schema(candidate_directory / "standard.parquet") != STANDARD_SCHEMA:
        raise error_type("Weather Async standard schema mismatch")
    old = None if current is None else pq.read_table(current.observations_path, columns=columns)
    standard = pq.read_table(candidate_directory / "standard.parquet",
                             columns=[*columns, "is_usable", "quality_status", "raw_value_text", "metric"])
    mapping = {item.series_id: item for item in catalog.series}
    for item in standard.select(["series_id", "data_family", "metric", "unit"]).group_by(
            ["series_id", "data_family", "metric", "unit"]).aggregate([]).to_pylist():
        contract = mapping.get(item["series_id"])
        if contract is None or any(item[key] != getattr(contract, key) for key in ("data_family", "metric", "unit")):
            raise error_type("Weather Async mapping identity mismatch")
    # Audit the existing normalization decision independently. Allowed source NULL
    # and producer-defined invalid rainfall are not lost valid observations.
    errors = {}
    fields = ["series_id", "raw_value_text", "metric", "value", "quality_status", "is_usable"]
    distinct = standard.select(fields).group_by(fields).aggregate([])
    for item in distinct.to_pylist():
        raw = item["raw_value_text"]
        numeric = _decimal(raw)
        quality = ("MISSING_VALUE" if raw is None or not raw.strip() else
                   "NON_NUMERIC" if numeric is None else
                   "NEGATIVE_RAINFALL" if item["metric"] == "precipitation" and numeric < 0 else "PASS")
        if (item["quality_status"] != quality or item["is_usable"] != (quality == "PASS")
                or item["value"] != numeric):
            errors.setdefault(item["series_id"], set()).add("SOURCE_NORMALIZATION_CORRUPTION")
    # Use the Producer's existing usability decision, but independently account
    # for every usable source row, including a row lost by canonical assembly.
    window = standard.filter(pc.equal(standard["is_usable"], True)).select(columns)
    nxt = pq.read_table(following_directory / "observations.parquet", columns=columns)
    reports = {}
    for family in ("observation", "forecast"):
        def select(table):
            return None if table is None else table.filter(pc.equal(table["data_family"], family))
        name = "weather_" + family
        required = {item.series_id for item in catalog.series if item.data_family == family}
        reports[name] = account_table(dataset_id=name, previous=select(old), source=select(window), following=select(nxt),
            keys=STABLE_KEY, identity=("series_id",), date_column="valid_date", values=("value", "unit", "source_row_sha256"),
            required=required,
            as_of_date=as_of_date, forecast=family == "forecast",
            source_errors={identity: reasons for identity, reasons in errors.items()
                           if identity in required})
        if report_sink is not None:
            report_sink[name] = reports[name]
    return seal_reports(runtime, run_id, reports, error_type)


def collect_reports(outcomes) -> dict:
    reports = {}
    for item in outcomes:
        supplied = item.performance.get("provider_details", {}).get("async_updates", {})
        for name, report in supplied.items():
            if name in reports:
                raise ValueError("DUPLICATE_ASYNC_DATASET")
            validate_report(report)
            reports[name] = report
    return reports


def validate_provider_reports(performance, *, already_failed=False) -> None:
    for name, report in performance.get("async_updates", {}).items():
        if name in DOMAINS and report["dataset_id"] != name:
            raise ValueError("ASYNC_DATASET_IDENTITY_MISMATCH")
        validate_report(report)
        if not report["promotion_allowed"] and not already_failed:
            raise ValueError("ASYNC_BLOCKING_ERROR")


def render_reports(reports: dict) -> list[str]:
    lines = []
    for name, report in reports.items():
        validate_report(report)
        counts = report["summary"]
        lines.append(name + " | " + " | ".join(
            dimension + ": " + " / ".join(f"{status}={count}" for status, count in counts[dimension].items())
            for dimension in ("coverage", "updates", "freshness")))
        lines.extend(f"  MISSING {item['identity']} | {item['reason']} | blocking={item.get('blocking', True)}"
                     for item in report["series"] if item["coverage_status"] == "MISSING")
    return lines
