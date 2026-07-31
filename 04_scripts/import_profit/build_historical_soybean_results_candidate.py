"""Build an isolated full-history soybean net-crush recalculation candidate."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from time import perf_counter
from typing import Any, Iterable, Sequence
import uuid

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "03_src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agri_research_agent.import_profit.config import load_soybean_config  # noqa: E402
from agri_research_agent.import_profit.historical_cnf_adapter import (  # noqa: E402
    HISTORICAL_CNF_SCHEMA,
)
from agri_research_agent.import_profit.historical_dce_adapter import (  # noqa: E402
    HISTORICAL_DCE_CONTINUOUS_SCHEMA,
)
from agri_research_agent.import_profit.historical_recalculation import (  # noqa: E402
    PIPELINE_VERSION,
    HistoricalRecalculationRun,
    business_key_rows,
    load_historical_cnf_parquet,
    load_historical_dce_continuous_parquet,
    recalculate_historical_soybean,
)
from agri_research_agent.import_profit.models import CalculationStatus  # noqa: E402
from agri_research_agent.import_profit.result_store import (  # noqa: E402
    RESULT_SCHEMA,
    SNAPSHOT_SCHEMA,
)
from agri_research_agent.import_profit.standard_io import (  # noqa: E402
    CBOT_SCHEMA,
    FX_SCHEMA,
    load_cbot_parquet,
    load_fx_parquet,
)


BUSINESS_KEYS_FILENAME = "historical_business_keys.parquet"
SNAPSHOTS_FILENAME = "historical_soybean_market_snapshots.parquet"
RESULTS_FILENAME = "historical_soybean_net_crush_results.parquet"
MANIFEST_FILENAME = "manifest.json"
QUALITY_FILENAME = "quality_report.json"
SCHEMA_VERSION = "historical-soybean-results-v1"
AS_OF_POLICY = "explicit_latest_real_cnf_date"
DEFAULT_BATCH_SIZE = 1000
MAX_SAMPLE_COUNT = 5
KEY_FIELDS = (
    "business_date",
    "commodity",
    "origin",
    "shipment_year",
    "shipment_month",
)
BUSINESS_KEY_SCHEMA = pa.schema(
    [
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("commodity", pa.string(), nullable=False),
        pa.field("origin", pa.string(), nullable=False),
        pa.field("shipment_year", pa.int16(), nullable=False),
        pa.field("shipment_month", pa.int8(), nullable=False),
        pa.field("shipment_period", pa.string(), nullable=False),
        pa.field("cnf_is_null", pa.bool_(), nullable=False),
        pa.field("cnf_source", pa.string(), nullable=False),
    ]
)


class HistoricalResultsCandidateError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        quality_report: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.quality_report = quality_report


def parse_utc_datetime(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HistoricalResultsCandidateError(
            "calculated-at must be an ISO-8601 UTC datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise HistoricalResultsCandidateError(
            "calculated-at must be an ISO-8601 UTC datetime"
        )
    return parsed.astimezone(timezone.utc)


def build_historical_soybean_results_candidate(
    *,
    config_path: str | Path,
    historical_cnf_path: str | Path,
    historical_dce_path: str | Path,
    cbot_parquet_path: str | Path,
    fx_parquet_path: str | Path,
    as_of_date: date,
    output_dir: str | Path,
    batch_size: int = DEFAULT_BATCH_SIZE,
    calculated_at: datetime | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    if type(as_of_date) is not date:
        raise HistoricalResultsCandidateError("as_of_date must be a real date")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise HistoricalResultsCandidateError("batch_size must be a positive integer")
    calculation_time = calculated_at or datetime.now(timezone.utc)
    if calculation_time.tzinfo is None:
        raise HistoricalResultsCandidateError("calculated_at must be timezone-aware")
    calculation_time = calculation_time.astimezone(timezone.utc)
    destination = Path(output_dir)
    _validate_destination(destination)

    input_started = perf_counter()
    cnf_path = Path(historical_cnf_path)
    dce_path = Path(historical_dce_path)
    cbot_path = Path(cbot_parquet_path)
    fx_path = Path(fx_parquet_path)
    config_source = Path(config_path)
    expected_inputs = (
        _inspect_plain_input_file(config_source, "soybean config"),
        _validate_prior_candidate_file(
            cnf_path, HISTORICAL_CNF_SCHEMA, "historical CNF"
        ),
        _validate_prior_candidate_file(
            dce_path, HISTORICAL_DCE_CONTINUOUS_SCHEMA, "historical DCE"
        ),
        _validate_prior_candidate_file(cbot_path, CBOT_SCHEMA, "CBOT"),
        _validate_prior_candidate_file(fx_path, FX_SCHEMA, "FX"),
    )
    config = load_soybean_config(config_source)
    historical_cnf = load_historical_cnf_parquet(cnf_path)
    historical_dce = load_historical_dce_continuous_parquet(dce_path)
    cbot = load_cbot_parquet(cbot_path)
    fx = load_fx_parquet(fx_path)
    input_seconds = perf_counter() - input_started

    run = recalculate_historical_soybean(
        historical_cnf,
        config=config,
        as_of_date=as_of_date,
        continuous_dce_points=historical_dce,
        cbot_records=cbot.records,
        fx_records=fx.records,
        batch_size=batch_size,
    )
    if run.recalculation_batch.requested_count != len(run.key_set.business_keys):
        raise HistoricalResultsCandidateError(
            "historical result count does not match business key count"
        )
    key_rows = business_key_rows(run.key_set)
    snapshot_rows = _snapshot_rows(run)
    result_rows = _result_rows(run, calculation_time)
    quality = _quality_report(run, snapshot_rows, result_rows)
    if quality["fatal_issues"]:
        raise HistoricalResultsCandidateError(
            "historical candidate quality checks failed",
            quality_report=quality,
        )

    existed_before = destination.exists()
    destination.mkdir(parents=True, exist_ok=True)
    finals = {
        BUSINESS_KEYS_FILENAME: destination / BUSINESS_KEYS_FILENAME,
        SNAPSHOTS_FILENAME: destination / SNAPSHOTS_FILENAME,
        RESULTS_FILENAME: destination / RESULTS_FILENAME,
        QUALITY_FILENAME: destination / QUALITY_FILENAME,
        MANIFEST_FILENAME: destination / MANIFEST_FILENAME,
    }
    token = uuid.uuid4().hex
    temporaries = {
        name: destination / f".{name}.{token}.tmp" for name in finals
    }
    placed: list[Path] = []
    write_started = perf_counter()
    try:
        _write_parquet(
            temporaries[BUSINESS_KEYS_FILENAME],
            BUSINESS_KEY_SCHEMA,
            key_rows,
        )
        _write_parquet(
            temporaries[SNAPSHOTS_FILENAME],
            SNAPSHOT_SCHEMA,
            snapshot_rows,
        )
        _write_parquet(
            temporaries[RESULTS_FILENAME],
            RESULT_SCHEMA,
            result_rows,
        )
        _verify_parquet(
            temporaries[BUSINESS_KEYS_FILENAME],
            BUSINESS_KEY_SCHEMA,
            len(key_rows),
            nullable_result_fields=(),
        )
        _verify_parquet(
            temporaries[SNAPSHOTS_FILENAME],
            SNAPSHOT_SCHEMA,
            len(snapshot_rows),
            nullable_result_fields=(),
        )
        _verify_parquet(
            temporaries[RESULTS_FILENAME],
            RESULT_SCHEMA,
            len(result_rows),
            nullable_result_fields=(
                "usd_cost_per_tonne",
                "duty_paid_cost_cny_per_tonne",
                "net_crush_margin_cny_per_tonne",
            ),
        )
        _write_json(temporaries[QUALITY_FILENAME], quality)
        output_identities = {
            BUSINESS_KEYS_FILENAME: {
                **_file_identity(
                    temporaries[BUSINESS_KEYS_FILENAME],
                    schema=BUSINESS_KEY_SCHEMA,
                    record_count=len(key_rows),
                ),
                "filename": BUSINESS_KEYS_FILENAME,
            },
            SNAPSHOTS_FILENAME: {
                **_file_identity(
                    temporaries[SNAPSHOTS_FILENAME],
                    schema=SNAPSHOT_SCHEMA,
                    record_count=len(snapshot_rows),
                ),
                "filename": SNAPSHOTS_FILENAME,
            },
            RESULTS_FILENAME: {
                **_file_identity(
                    temporaries[RESULTS_FILENAME],
                    schema=RESULT_SCHEMA,
                    record_count=len(result_rows),
                ),
                "filename": RESULTS_FILENAME,
            },
            QUALITY_FILENAME: {
                **_file_identity(
                    temporaries[QUALITY_FILENAME],
                    schema=None,
                    record_count=None,
                ),
                "filename": QUALITY_FILENAME,
            },
        }
        generated_at = datetime.now(timezone.utc)
        timings = {
            "input_loading_seconds": input_seconds,
            **dict(run.timings),
            "candidate_write_seconds": perf_counter() - write_started,
            "total_seconds": perf_counter() - started,
        }
        manifest = _manifest(
            run,
            as_of_date=as_of_date,
            calculated_at=calculation_time,
            generated_at=generated_at,
            input_files=expected_inputs,
            output_files=output_identities,
            quality=quality,
            timings=timings,
            cbot_record_count=len(cbot.records),
            fx_record_count=len(fx.records),
        )
        _assert_safe_json(manifest)
        _assert_safe_json(quality)
        _write_json(temporaries[MANIFEST_FILENAME], manifest)

        for expected in expected_inputs:
            _verify_identity_unchanged(expected)
        for name in (
            BUSINESS_KEYS_FILENAME,
            SNAPSHOTS_FILENAME,
            RESULTS_FILENAME,
            QUALITY_FILENAME,
            MANIFEST_FILENAME,
        ):
            os.replace(temporaries[name], finals[name])
            placed.append(finals[name])
        _verify_final_outputs(
            finals,
            output_identities,
            manifest=manifest,
            quality=quality,
        )
    except Exception:
        for path in temporaries.values():
            if path.exists():
                path.unlink()
        for path in placed:
            if path.exists():
                path.unlink()
        if not existed_before and destination.exists() and not any(destination.iterdir()):
            destination.rmdir()
        raise

    final_identities = {
        name: _file_identity(
            path,
            schema=_schema_for(name),
            record_count=(
                len(key_rows)
                if name == BUSINESS_KEYS_FILENAME
                else len(snapshot_rows)
                if name == SNAPSHOTS_FILENAME
                else len(result_rows)
                if name == RESULTS_FILENAME
                else None
            ),
        )
        for name, path in finals.items()
    }
    return {
        "output_dir": destination,
        "files": finals,
        "manifest": manifest,
        "quality_report": quality,
        "output_identities": final_identities,
        "run": run,
        "timings": {
            **manifest["timings"],
            "candidate_write_seconds": perf_counter() - write_started,
            "total_seconds": perf_counter() - started,
        },
    }


def _validate_prior_candidate_file(
    path: Path,
    schema: pa.Schema,
    label: str,
) -> dict[str, Any]:
    if not path.is_file():
        raise HistoricalResultsCandidateError(f"{label} input does not exist")
    manifest_path = path.parent / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise HistoricalResultsCandidateError(
            f"{label} sibling manifest.json does not exist"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HistoricalResultsCandidateError(
            f"{label} manifest is not valid JSON"
        ) from exc
    if manifest.get("candidate_status") not in {
        "passed",
        "passed_with_warnings",
    }:
        raise HistoricalResultsCandidateError(
            f"{label} prior candidate status is not accepted"
        )
    outputs = manifest.get("output_files")
    if not isinstance(outputs, dict) or path.name not in outputs:
        raise HistoricalResultsCandidateError(
            f"{label} file is absent from its manifest"
        )
    declared = outputs[path.name]
    actual = _file_identity(
        path,
        schema=schema,
        record_count=pq.ParquetFile(path).metadata.num_rows,
    )
    declared_size = declared.get("size", declared.get("size_bytes"))
    if (
        declared.get("filename") != path.name
        or declared_size != actual["size_bytes"]
        or declared.get("sha256") != actual["sha256"]
    ):
        raise HistoricalResultsCandidateError(
            f"{label} file identity does not match its manifest"
        )
    table = pq.read_table(path)
    if table.schema != schema:
        raise HistoricalResultsCandidateError(f"{label} Schema does not match")
    dates = _table_dates(table)
    return {
        **actual,
        "path": path,
        "date_range": {
            "earliest": min(dates).isoformat() if dates else None,
            "latest": max(dates).isoformat() if dates else None,
        },
    }


def _inspect_plain_input_file(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise HistoricalResultsCandidateError(f"{label} input does not exist")
    return {
        **_file_identity(path, schema=None, record_count=None),
        "path": path,
        "date_range": {"earliest": None, "latest": None},
    }


def _snapshot_rows(run: HistoricalRecalculationRun) -> list[dict[str, Any]]:
    rows = []
    for item in run.recalculation_batch.items:
        snapshot = item.market_snapshot
        key = snapshot.business_key
        rows.append(
            {
                "business_date": key.business_date,
                "commodity": key.commodity,
                "origin": key.origin,
                "shipment_year": key.shipment_year,
                "shipment_month": key.shipment_month,
                "shipment_period": key.shipment_period,
                "mapping_identity": snapshot.mapping_identity,
                "parameter_version": snapshot.parameter_version,
                "cnf_cents_per_bushel": snapshot.cnf_cents_per_bushel,
                "cnf_source": snapshot.cnf_source,
                "cbot_contract_year": snapshot.cbot_contract_year,
                "cbot_contract_month": snapshot.cbot_contract_month,
                "cbot_price_cents_per_bushel": snapshot.cbot_price_cents_per_bushel,
                "cbot_quality_status": snapshot.cbot_exchange_quality_status,
                "cbot_source": snapshot.cbot_source,
                "cbot_source_snapshot_sha256": snapshot.cbot_source_snapshot_sha256,
                "fx_target_tenor": snapshot.fx_target_tenor,
                "fx_value": snapshot.fx_value,
                "fx_is_interpolated": snapshot.fx_is_interpolated,
                "fx_lower_tenor": snapshot.fx_lower_tenor,
                "fx_upper_tenor": snapshot.fx_upper_tenor,
                "fx_selection_status": snapshot.fx_selection_status.value,
                "fx_source": snapshot.fx_source,
                "fx_source_snapshot_sha256": snapshot.fx_source_snapshot_sha256,
                "soymeal_contract_code": snapshot.soymeal_contract_code,
                "soymeal_price_cny_per_tonne": snapshot.soymeal_price_cny_per_tonne,
                "soymeal_price_type": snapshot.soymeal_price_type,
                "soymeal_source": snapshot.soymeal_source,
                "soyoil_contract_code": snapshot.soyoil_contract_code,
                "soyoil_price_cny_per_tonne": snapshot.soyoil_price_cny_per_tonne,
                "soyoil_price_type": snapshot.soyoil_price_type,
                "soyoil_source": snapshot.soyoil_source,
                "snapshot_status": snapshot.snapshot_status.value,
                "missing_reasons": [reason.value for reason in snapshot.missing_reasons],
            }
        )
    return rows


def _result_rows(
    run: HistoricalRecalculationRun,
    calculated_at: datetime,
) -> list[dict[str, Any]]:
    rows = []
    for item in run.recalculation_batch.items:
        result = item.calculation_result
        key = item.business_key
        rows.append(
            {
                "business_date": key.business_date,
                "commodity": key.commodity,
                "origin": key.origin,
                "shipment_year": key.shipment_year,
                "shipment_month": key.shipment_month,
                "shipment_period": key.shipment_period,
                "usd_cost_per_tonne": result.usd_cost_per_tonne,
                "duty_paid_cost_cny_per_tonne": result.duty_paid_cost_cny_per_tonne,
                "net_crush_margin_cny_per_tonne": result.net_crush_margin_cny_per_tonne,
                "calculation_status": result.calculation_status.value,
                "missing_reasons": [reason.value for reason in result.missing_reasons],
                "parameter_version": result.parameter_version,
                "mapping_identity": result.mapping_identity,
                "calculated_at": calculated_at,
            }
        )
    return rows


def _quality_report(
    run: HistoricalRecalculationRun,
    snapshots: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    key_set = run.key_set
    missing_counts = dict(run.recalculation_batch.missing_reason_counts)
    missing_text = {reason.value: count for reason, count in missing_counts.items()}
    direct_count = sum(row["fx_selection_status"] == "direct" for row in snapshots)
    interpolated_count = sum(row["fx_is_interpolated"] for row in snapshots)
    fx_missing_count = sum(row["fx_value"] is None for row in snapshots)
    complete = run.recalculation_batch.success_count
    incomplete = run.recalculation_batch.incomplete_count
    fatal: list[dict[str, Any]] = []
    if any(row["cnf_source"] != "historical_excel" for row in snapshots):
        fatal.append({"code": "historical_cnf_source_changed", "count": 1})
    if any(
        row["soymeal_source"] not in {None, "reuters_sql"}
        or row["soyoil_source"] not in {None, "reuters_sql"}
        for row in snapshots
    ):
        fatal.append({"code": "historical_dce_source_changed", "count": 1})
    if any(
        row["soymeal_price_type"] not in {None, "historical_continuous_close"}
        or row["soyoil_price_type"] not in {None, "historical_continuous_close"}
        for row in snapshots
    ):
        fatal.append({"code": "historical_dce_price_type_changed", "count": 1})
    if any(
        row["calculation_status"] == "incomplete"
        and any(
            row[field] is not None
            for field in (
                "usd_cost_per_tonne",
                "duty_paid_cost_cny_per_tonne",
                "net_crush_margin_cny_per_tonne",
            )
        )
        for row in results
    ):
        fatal.append({"code": "incomplete_result_contains_value", "count": 1})
    warnings = (
        []
        if incomplete == 0
        else [{"code": "historical_results_incomplete", "count": incomplete}]
    )
    status = (
        "failed"
        if fatal
        else "passed"
        if incomplete == 0
        else "passed_with_incomplete"
    )
    samples = {
        "complete": _sample_rows(
            snapshots, lambda row: row["snapshot_status"] == "complete"
        ),
        "cnf_null": _sample_rows(
            snapshots, lambda row: row["cnf_cents_per_bushel"] is None
        ),
        "fx_interpolated": _sample_rows(
            snapshots, lambda row: row["fx_is_interpolated"]
        ),
        "soyoil_missing": _sample_rows(
            snapshots, lambda row: row["soyoil_price_cny_per_tonne"] is None
        ),
        "required_2026_06_10_brazil_2026_12": _sample_rows(
            snapshots,
            lambda row: (
                row["business_date"] == date(2026, 6, 10)
                and row["origin"] == "brazil"
                and row["shipment_period"] == "2026-12"
            ),
        ),
        "cnf_zero": _sample_rows(
            snapshots, lambda row: row["cnf_cents_per_bushel"] == 0
        ),
        "cnf_negative": _sample_rows(
            snapshots,
            lambda row: (
                row["cnf_cents_per_bushel"] is not None
                and row["cnf_cents_per_bushel"] < 0
            ),
        ),
    }
    return {
        "candidate_status": status,
        "fatal_issues": fatal,
        "warnings": warnings,
        "business_key_checks": {
            "source_record_count": key_set.source_record_count,
            "excluded_after_as_of_count": key_set.excluded_after_as_of_count,
            "weekend_count": key_set.weekend_count,
            "final_business_key_count": len(key_set.business_keys),
            "duplicate_key_count": 0,
            "origin_counts": dict(key_set.origin_counts),
            "shipment_year_counts": {
                str(year): count for year, count in key_set.shipment_year_counts
            },
            "shipment_month_counts": {
                f"{month:02d}": count
                for month, count in key_set.shipment_month_counts
            },
            "shipment_year_samples": list(key_set.shipment_year_samples),
        },
        "cnf_counts": {
            "null": key_set.cnf_null_count,
            "nonnull": key_set.cnf_nonnull_count,
            "zero": key_set.cnf_zero_count,
            "negative": key_set.cnf_negative_count,
        },
        "market_match_counts": {
            "cbot_matched": sum(
                row["cbot_price_cents_per_bushel"] is not None for row in snapshots
            ),
            "cbot_missing": missing_text["missing_cbot"],
            "fx_direct": direct_count,
            "fx_interpolated": interpolated_count,
            "fx_missing": fx_missing_count,
            "soymeal_matched": sum(
                row["soymeal_price_cny_per_tonne"] is not None for row in snapshots
            ),
            "soymeal_missing": missing_text["missing_soymeal"],
            "soyoil_matched": sum(
                row["soyoil_price_cny_per_tonne"] is not None for row in snapshots
            ),
            "soyoil_missing": missing_text["missing_soyoil"],
        },
        "snapshot_counts": {"complete": complete, "incomplete": incomplete},
        "calculation_counts": {"success": complete, "incomplete": incomplete},
        "missing_reason_counts": missing_text,
        "samples": samples,
    }


def _manifest(
    run: HistoricalRecalculationRun,
    *,
    as_of_date: date,
    calculated_at: datetime,
    generated_at: datetime,
    input_files: tuple[dict[str, Any], ...],
    output_files: dict[str, dict[str, Any]],
    quality: dict[str, Any],
    timings: dict[str, float],
    cbot_record_count: int,
    fx_record_count: int,
) -> dict[str, Any]:
    snapshots = run.recalculation_batch.items
    source_counts = {
        "cnf": dict(
            Counter(item.market_snapshot.cnf_source for item in snapshots)
        ),
        "cbot": dict(
            Counter(
                item.market_snapshot.cbot_source
                for item in snapshots
                if item.market_snapshot.cbot_source is not None
            )
        ),
        "fx": dict(
            Counter(
                item.market_snapshot.fx_source
                for item in snapshots
                if item.market_snapshot.fx_source is not None
            )
        ),
        "soymeal": dict(
            Counter(
                item.market_snapshot.soymeal_source
                for item in snapshots
                if item.market_snapshot.soymeal_source is not None
            )
        ),
        "soyoil": dict(
            Counter(
                item.market_snapshot.soyoil_source
                for item in snapshots
                if item.market_snapshot.soyoil_source is not None
            )
        ),
    }
    dce_price_type_counts = dict(
        Counter(
            value
            for item in snapshots
            for value in (
                item.market_snapshot.soymeal_price_type,
                item.market_snapshot.soyoil_price_type,
            )
            if value is not None
        )
    )
    safe_inputs = [
        {key: value for key, value in identity.items() if key != "path"}
        for identity in input_files
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "candidate_status": quality["candidate_status"],
        "historical_input": True,
        "as_of_date": as_of_date.isoformat(),
        "as_of_policy": AS_OF_POLICY,
        "calculated_at": calculated_at.isoformat().replace("+00:00", "Z"),
        "batch_size": run.batch_size,
        "batch_count": run.batch_count,
        "input_files": safe_inputs,
        "original_cnf_record_count": run.key_set.source_record_count,
        "excluded_after_as_of_count": run.key_set.excluded_after_as_of_count,
        "final_business_key_count": len(run.key_set.business_keys),
        "origin_key_counts": dict(run.key_set.origin_counts),
        "shipment_year_key_counts": {
            str(year): count for year, count in run.key_set.shipment_year_counts
        },
        "shipment_month_key_counts": {
            f"{month:02d}": count
            for month, count in run.key_set.shipment_month_counts
        },
        "cnf_nonnull_key_count": run.key_set.cnf_nonnull_count,
        "cnf_null_key_count": run.key_set.cnf_null_count,
        "resolved_dce_record_count": len(run.resolved_dce_points),
        "cbot_record_count": cbot_record_count,
        "fx_record_count": fx_record_count,
        "snapshot_count": len(snapshots),
        "result_count": len(snapshots),
        "success_count": run.recalculation_batch.success_count,
        "incomplete_count": run.recalculation_batch.incomplete_count,
        "missing_reason_counts": quality["missing_reason_counts"],
        "fx_interpolation_count": quality["market_match_counts"]["fx_interpolated"],
        "dce_price_type_counts": dce_price_type_counts,
        "source_counts": source_counts,
        "output_files": output_files,
        "output_file_names": [*output_files, MANIFEST_FILENAME],
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "timings": timings,
    }


def _sample_rows(rows, predicate) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if predicate(row):
            result.append(
                {
                    "business_date": row["business_date"].isoformat(),
                    "origin": row["origin"],
                    "shipment_period": row["shipment_period"],
                    "cnf_cents_per_bushel": row["cnf_cents_per_bushel"],
                    "cbot_price_cents_per_bushel": row[
                        "cbot_price_cents_per_bushel"
                    ],
                    "fx_value": row["fx_value"],
                    "fx_selection_status": row["fx_selection_status"],
                    "soymeal_price_cny_per_tonne": row[
                        "soymeal_price_cny_per_tonne"
                    ],
                    "soyoil_price_cny_per_tonne": row[
                        "soyoil_price_cny_per_tonne"
                    ],
                    "missing_reasons": row["missing_reasons"],
                }
            )
            if len(result) >= MAX_SAMPLE_COUNT:
                break
    return result


def _write_parquet(path: Path, schema: pa.Schema, rows: list[dict[str, Any]]) -> None:
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, path, compression="zstd")
    _fsync_file(path)


def _verify_parquet(
    path: Path,
    schema: pa.Schema,
    expected_rows: int,
    *,
    nullable_result_fields: tuple[str, ...],
) -> None:
    table = pq.read_table(path)
    if table.schema != schema or table.num_rows != expected_rows:
        raise HistoricalResultsCandidateError(
            f"Parquet Schema or row count mismatch: {path.name}"
        )
    rows = table.to_pylist()
    keys = [tuple(row[field] for field in KEY_FIELDS) for row in rows]
    if len(keys) != len(set(keys)) or keys != sorted(keys):
        raise HistoricalResultsCandidateError(
            f"Parquet key uniqueness or sorting failed: {path.name}"
        )
    if nullable_result_fields:
        for row in rows:
            if row["calculation_status"] == CalculationStatus.INCOMPLETE.value and any(
                row[field] is not None for field in nullable_result_fields
            ):
                raise HistoricalResultsCandidateError(
                    "incomplete result contains non-null calculation values"
                )
    frame = pd.read_parquet(path)
    if list(frame.columns) != schema.names or len(frame) != expected_rows:
        raise HistoricalResultsCandidateError(
            f"pandas Parquet readback failed: {path.name}"
        )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    data = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if json.loads(path.read_text(encoding="utf-8")) != payload:
        raise HistoricalResultsCandidateError(f"JSON round-trip failed: {path.name}")


def _file_identity(
    path: Path,
    *,
    schema: pa.Schema | None,
    record_count: int | None,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    result: dict[str, Any] = {
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": digest.hexdigest().upper(),
        "record_count": record_count,
        "schema_fingerprint": (
            hashlib.sha256(schema.serialize().to_pybytes()).hexdigest().upper()
            if schema is not None
            else None
        ),
    }
    return result


def _verify_identity_unchanged(identity: dict[str, Any]) -> None:
    path = identity["path"]
    actual = _file_identity(
        path,
        schema=_schema_for(path.name),
        record_count=identity["record_count"],
    )
    for field in ("filename", "size_bytes", "sha256", "schema_fingerprint"):
        if actual[field] != identity[field]:
            raise HistoricalResultsCandidateError(
                f"input identity changed during build: {path.name}"
            )


def _verify_final_outputs(
    finals: dict[str, Path],
    expected: dict[str, dict[str, Any]],
    *,
    manifest: dict[str, Any],
    quality: dict[str, Any],
) -> None:
    for name, identity in expected.items():
        actual = _file_identity(
            finals[name],
            schema=_schema_for(name),
            record_count=identity["record_count"],
        )
        if actual != identity:
            raise HistoricalResultsCandidateError(
                f"final output identity mismatch: {name}"
            )
    if json.loads(finals[MANIFEST_FILENAME].read_text("utf-8")) != manifest:
        raise HistoricalResultsCandidateError("final manifest mismatch")
    if json.loads(finals[QUALITY_FILENAME].read_text("utf-8")) != quality:
        raise HistoricalResultsCandidateError("final quality report mismatch")


def _schema_for(filename: str) -> pa.Schema | None:
    return {
        BUSINESS_KEYS_FILENAME: BUSINESS_KEY_SCHEMA,
        SNAPSHOTS_FILENAME: SNAPSHOT_SCHEMA,
        RESULTS_FILENAME: RESULT_SCHEMA,
        "historical_cnf_quotes.parquet": HISTORICAL_CNF_SCHEMA,
        "historical_dce_continuous.parquet": HISTORICAL_DCE_CONTINUOUS_SCHEMA,
        "cbot_soybean_daily.parquet": CBOT_SCHEMA,
        "usdcny_forward_daily.parquet": FX_SCHEMA,
    }.get(filename)


def _table_dates(table: pa.Table) -> list[date]:
    for field in ("business_date", "market_date"):
        if field in table.schema.names:
            return [value for value in table[field].to_pylist() if value is not None]
    return []


def _validate_destination(destination: Path) -> None:
    resolved = destination.resolve()
    root = REPOSITORY_ROOT.resolve()
    if resolved == root or resolved.is_relative_to(root):
        raise HistoricalResultsCandidateError(
            "output-dir must be outside the repository"
        )
    if destination.exists() and not destination.is_dir():
        raise HistoricalResultsCandidateError(
            "output-dir exists and is not a directory"
        )
    if destination.exists() and any(destination.iterdir()):
        raise HistoricalResultsCandidateError("output-dir must be empty")


def _assert_safe_json(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    if re.search(r"(?i)(?:[A-Z]:[\\/]|/home/|/Users/)", text):
        raise HistoricalResultsCandidateError("candidate JSON contains absolute path")
    forbidden = ("Source Server", "Source Host", "username", "password")
    if any(value in text for value in forbidden):
        raise HistoricalResultsCandidateError(
            "candidate JSON contains forbidden connection identity"
        )


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build isolated full-history soybean net-crush results."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--historical-cnf", required=True, type=Path)
    parser.add_argument("--historical-dce", required=True, type=Path)
    parser.add_argument("--cbot-parquet", required=True, type=Path)
    parser.add_argument("--fx-parquet", required=True, type=Path)
    parser.add_argument("--as-of-date", required=True, type=date.fromisoformat)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--calculated-at")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build_historical_soybean_results_candidate(
            config_path=args.config,
            historical_cnf_path=args.historical_cnf,
            historical_dce_path=args.historical_dce,
            cbot_parquet_path=args.cbot_parquet,
            fx_parquet_path=args.fx_parquet,
            as_of_date=args.as_of_date,
            output_dir=args.output_dir,
            batch_size=args.batch_size,
            calculated_at=(
                None
                if args.calculated_at is None
                else parse_utc_datetime(args.calculated_at)
            ),
        )
    except Exception as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "candidate_status": result["manifest"]["candidate_status"],
                "business_key_count": result["manifest"][
                    "final_business_key_count"
                ],
                "success_count": result["manifest"]["success_count"],
                "incomplete_count": result["manifest"]["incomplete_count"],
                "output_dir": str(result["output_dir"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
