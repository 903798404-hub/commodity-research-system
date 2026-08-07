"""Build an isolated DCE night-session-close snapshot candidate."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
from time import sleep as real_sleep
from typing import Any, Callable, Sequence
import uuid

import pyarrow as pa
import pyarrow.parquet as pq


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "03_src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agri_research_agent.import_profit.config import load_soybean_config  # noqa: E402
from agri_research_agent.import_profit.contract_mapping import (  # noqa: E402
    map_soybean_contracts,
)
from agri_research_agent.import_profit.dce_daily import (  # noqa: E402
    ADAPTER_VERSION,
    CAPTURE_TIMEZONE,
    CAPTURE_ZONE,
    PRICE_FIELD,
    PRICE_TYPE,
    SOURCE,
    SOURCE_FUNCTION,
    DceNightSessionCloseRecord,
    DceSpotContractResult,
    SpotFetcher,
    TradeCalendarFetcher,
    capture_gate_status,
    default_spot_fetcher,
    fetch_dce_night_session_close_snapshot,
    records_as_dicts,
)


PARQUET_FILENAME = "dce_night_session_close_prices.parquet"
MANIFEST_FILENAME = "manifest.json"
QUALITY_FILENAME = "quality_report.json"
MAX_ATTEMPTS = 5
MAX_RETRY_SECONDS = 60.0
SHIPMENT_PATTERN = re.compile(r"^(20\d{2})-(0[1-9]|1[0-2])$")

DCE_NIGHT_SESSION_CLOSE_SCHEMA = pa.schema(
    [
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("instrument", pa.string(), nullable=False),
        pa.field("commodity", pa.string(), nullable=False),
        pa.field("exchange", pa.string(), nullable=False),
        pa.field("contract_code", pa.string(), nullable=False),
        pa.field("contract_year", pa.int16(), nullable=False),
        pa.field("contract_month", pa.int8(), nullable=False),
        pa.field("price_cny_per_tonne", pa.float64(), nullable=False),
        pa.field("price_type", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("source_function", pa.string(), nullable=False),
        pa.field("source_quote_date", pa.date32(), nullable=True),
        pa.field("source_quote_time", pa.time64("us"), nullable=False),
        pa.field("captured_at", pa.timestamp("us", tz=CAPTURE_TIMEZONE), nullable=False),
        pa.field("capture_timezone", pa.string(), nullable=False),
        pa.field("quality_status", pa.string(), nullable=False),
        pa.field("is_usable", pa.bool_(), nullable=False),
    ]
)
DCE_MORNING_OPEN_SCHEMA = DCE_NIGHT_SESSION_CLOSE_SCHEMA


class DceCandidateBuildError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        quality_report: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.quality_report = quality_report


def parse_shipment_period(value: str) -> tuple[int, int]:
    match = SHIPMENT_PATTERN.fullmatch(value)
    if match is None:
        raise DceCandidateBuildError("shipment-period must use YYYY-MM in 2000-2099")
    return int(match.group(1)), int(match.group(2))


def resolve_required_contracts(
    config_path: str | Path,
    shipment_periods: Sequence[str],
) -> dict[str, Any]:
    if not shipment_periods:
        raise DceCandidateBuildError("at least one shipment-period is required")
    config = load_soybean_config(config_path)
    mappings: list[dict[str, str]] = []
    required: dict[str, None] = {}
    for text in shipment_periods:
        year, month = parse_shipment_period(text)
        mapped = map_soybean_contracts(config, year, month)
        mappings.append(
            {
                "shipment_period": text,
                "soymeal_contract": mapped.soymeal.code,
                "soyoil_contract": mapped.soyoil.code,
            }
        )
        required[mapped.soymeal.code] = None
        required[mapped.soyoil.code] = None
    return {
        "shipment_periods": list(shipment_periods),
        "contract_mappings": mappings,
        "required_contracts": sorted(required),
        "mapping_identity": config.contract_mapping_identity,
    }


def build_dce_daily_candidate(
    config_path: str | Path,
    business_date: date,
    shipment_periods: Sequence[str],
    output_dir: str | Path,
    *,
    attempts: int = 1,
    retry_seconds: float = 0.0,
    fetcher: SpotFetcher = default_spot_fetcher,
    trade_calendar_fetcher: TradeCalendarFetcher | None = None,
    sleeper: Callable[[float], None] = real_sleep,
    clock: Callable[[], datetime] | None = None,
    akshare_version: str | None = None,
) -> dict[str, Any]:
    target_date = _require_date(business_date)
    _validate_retry(attempts, retry_seconds)
    destination = Path(output_dir)
    _validate_destination(destination)
    mapping = resolve_required_contracts(config_path, shipment_periods)
    now = clock or (lambda: datetime.now(CAPTURE_ZONE))
    capture_started = _aware_shanghai(now())
    initial_gate = capture_gate_status(target_date, capture_started)
    if initial_gate != "valid":
        report = _gate_failure_report(
            target_date, mapping, capture_started, initial_gate
        )
        raise DceCandidateBuildError(initial_gate, quality_report=report)

    final_results: dict[str, DceSpotContractResult] = {}
    attempt_counts: Counter[str] = Counter()
    request_audit: list[dict[str, Any]] = []
    required_contracts = list(mapping["required_contracts"])
    snapshot_batch_id: str | None = None
    snapshot_captured_at: datetime | None = None
    for attempt_index in range(attempts):
        attempt_time = _aware_shanghai(now())
        gate = capture_gate_status(target_date, attempt_time)
        for code in required_contracts:
            attempt_counts[code] += 1
        batch = fetch_dce_night_session_close_snapshot(
            required_contracts,
            target_date,
            fetcher=fetcher,
            trade_calendar_fetcher=trade_calendar_fetcher,
            captured_at=attempt_time,
            enforce_capture_window=True,
        )
        request_audit.append(
            {
                "attempt": attempt_index + 1,
                "request_mode": "batch",
                "requested_contracts": list(batch.requested_contracts),
                "request_symbol": batch.request_symbol,
                "elapsed_seconds": batch.elapsed_seconds,
                "returned_fields": list(batch.returned_fields),
                "source_row_count": batch.source_row_count,
                "captured_at": batch.captured_at.isoformat(),
                "capture_gate_status": batch.capture_gate_status,
                "exception_type": batch.source_error_type,
                "exception": batch.source_error_message,
            }
        )
        final_results = {
            item.contract.code: item for item in batch.contract_results
        }
        if batch.records:
            snapshot_captured_at = batch.captured_at
            snapshot_batch_id = _snapshot_batch_id(
                target_date,
                attempt_time,
                batch.request_symbol,
            )
            if batch.is_usable:
                break
        if gate != "valid":
            break
        if attempt_index + 1 < attempts and retry_seconds:
            sleeper(retry_seconds)

    capture_completed = _aware_shanghai(now())
    missing = sorted(
        code
        for code in mapping["required_contracts"]
        if code not in final_results or not final_results[code].is_usable
    )
    quality_report = _build_quality_report(
        target_date,
        mapping,
        capture_started,
        capture_completed,
        final_results,
        attempt_counts,
        request_audit,
        missing,
        snapshot_batch_id,
        snapshot_captured_at,
    )
    if len(missing) == len(mapping["required_contracts"]):
        raise DceCandidateBuildError(
            "all required contracts failed",
            quality_report=quality_report,
        )

    records: tuple[DceNightSessionCloseRecord, ...] = tuple(
        final_results[code].record
        for code in mapping["required_contracts"]
        if final_results[code].record is not None
    )
    keys = [(record.business_date, record.contract_code) for record in records]
    duplicate_key_count = len(keys) - len(set(keys))
    if duplicate_key_count:
        quality_report["fatal_issues"].append(
            {"code": "duplicate_standardized_key", "count": duplicate_key_count}
        )
        quality_report["candidate_status"] = "failed"
        raise DceCandidateBuildError(
            "standardized business_date + contract_code keys are not unique",
            quality_report=quality_report,
        )

    version = akshare_version or importlib.metadata.version("akshare")
    destination.mkdir(parents=True, exist_ok=True)
    finals = {
        PARQUET_FILENAME: destination / PARQUET_FILENAME,
        QUALITY_FILENAME: destination / QUALITY_FILENAME,
        MANIFEST_FILENAME: destination / MANIFEST_FILENAME,
    }
    if any(path.exists() for path in finals.values()):
        raise DceCandidateBuildError("candidate output already exists")
    token = uuid.uuid4().hex
    temporaries = {
        name: destination / f".{name}.{token}.tmp" for name in finals
    }
    created_finals: list[Path] = []
    try:
        table = pa.Table.from_pylist(
            records_as_dicts(records),
            schema=DCE_NIGHT_SESSION_CLOSE_SCHEMA,
        )
        pq.write_table(table, temporaries[PARQUET_FILENAME], compression="zstd")
        _fsync_file(temporaries[PARQUET_FILENAME])
        _verify_parquet(temporaries[PARQUET_FILENAME], len(records))
        _write_json(temporaries[QUALITY_FILENAME], quality_report)
        output_identity = _file_identity(
            temporaries[PARQUET_FILENAME], filename=PARQUET_FILENAME
        )
        quality_identity = _file_identity(
            temporaries[QUALITY_FILENAME], filename=QUALITY_FILENAME
        )
        status_counts = dict(
            sorted(Counter(record.quality_status for record in records).items())
        )
        manifest = {
            "schema_version": 2,
            "adapter_version": ADAPTER_VERSION,
            "candidate_status": (
                "success" if not missing else "passed_with_incomplete"
            ),
            "business_date": target_date.isoformat(),
            "commodity": "soybean",
            "shipment_periods": mapping["shipment_periods"],
            "contract_mappings": mapping["contract_mappings"],
            "required_contracts": mapping["required_contracts"],
            "required_contract_count": len(mapping["required_contracts"]),
            "successful_contract_count": len(records),
            "missing_contracts": missing,
            "mapping_identity": mapping["mapping_identity"],
            "source": SOURCE,
            "source_function": SOURCE_FUNCTION,
            "price_field": PRICE_FIELD,
            "price_type": PRICE_TYPE,
            "snapshot_batch_id": snapshot_batch_id,
            "snapshot_captured_at": (
                None
                if snapshot_captured_at is None
                else snapshot_captured_at.isoformat()
            ),
            "capture_timezone": CAPTURE_TIMEZONE,
            "capture_started_at": capture_started.isoformat(),
            "capture_completed_at": capture_completed.isoformat(),
            "akshare_version": version,
            "standardized_record_count": len(records),
            "duplicate_key_count": duplicate_key_count,
            "quality_status_counts": status_counts,
            "output_filename": PARQUET_FILENAME,
            "output_size": output_identity["size"],
            "output_sha256": output_identity["sha256"],
            "output_files": {
                PARQUET_FILENAME: output_identity,
                QUALITY_FILENAME: quality_identity,
            },
        }
        _assert_manifest_safe(manifest)
        _write_json(temporaries[MANIFEST_FILENAME], manifest)
        for name in (PARQUET_FILENAME, QUALITY_FILENAME, MANIFEST_FILENAME):
            os.replace(temporaries[name], finals[name])
            created_finals.append(finals[name])
        _verify_parquet(finals[PARQUET_FILENAME], len(records))
        if json.loads(finals[QUALITY_FILENAME].read_text("utf-8")) != quality_report:
            raise DceCandidateBuildError("quality report readback mismatch")
        if json.loads(finals[MANIFEST_FILENAME].read_text("utf-8")) != manifest:
            raise DceCandidateBuildError("manifest readback mismatch")
        for name, identity in manifest["output_files"].items():
            if _file_identity(finals[name]) != identity:
                raise DceCandidateBuildError(f"candidate file identity mismatch: {name}")
        _file_identity(finals[MANIFEST_FILENAME])
    except Exception:
        _cleanup_paths(temporaries.values())
        _cleanup_paths(created_finals)
        raise
    return {
        "output_dir": destination,
        "files": finals,
        "manifest": manifest,
        "quality_report": quality_report,
        "records": records,
    }


def _gate_failure_report(
    target_date: date,
    mapping: dict[str, Any],
    captured_at: datetime,
    status: str,
) -> dict[str, Any]:
    return {
        "business_date": target_date.isoformat(),
        "current_shanghai_date": captured_at.date().isoformat(),
        "current_shanghai_time": captured_at.time().isoformat(),
        "capture_timezone": CAPTURE_TIMEZONE,
        "capture_window_status": status,
        "source": SOURCE,
        "source_function": SOURCE_FUNCTION,
        "price_field": PRICE_FIELD,
        "price_type": PRICE_TYPE,
        "snapshot_batch_id": None,
        "shipment_periods": mapping["shipment_periods"],
        "required_contracts": mapping["required_contracts"],
        "request_mode": "batch",
        "requests": [],
        "contract_results": [],
        "fatal_issues": [{"code": status, "count": 1}],
        "warnings": [],
        "candidate_status": "failed",
    }


def _build_quality_report(
    target_date: date,
    mapping: dict[str, Any],
    started: datetime,
    completed: datetime,
    results: dict[str, DceSpotContractResult],
    attempt_counts: Counter[str],
    requests: list[dict[str, Any]],
    missing: list[str],
    snapshot_batch_id: str | None,
    snapshot_captured_at: datetime | None,
) -> dict[str, Any]:
    contracts: list[dict[str, Any]] = []
    for code in mapping["required_contracts"]:
        item = results.get(code)
        if item is None:
            contract_payload = {
                "contract_code": code,
                "request_status": "source_error",
                "is_usable": False,
                "matched_source_symbol": None,
                "source_quote_date": None,
                "source_quote_time": None,
                "current_price": None,
                "error_type": "MissingResult",
                "error_message": "no adapter result",
            }
        else:
            contract_payload = item.bounded_quality_dict()
        contract_payload["attempt_count"] = attempt_counts[code]
        contracts.append(contract_payload)
    return {
        "business_date": target_date.isoformat(),
        "current_shanghai_date": completed.date().isoformat(),
        "current_shanghai_time": completed.time().isoformat(),
        "capture_timezone": CAPTURE_TIMEZONE,
        "capture_started_at": started.isoformat(),
        "capture_completed_at": completed.isoformat(),
        "capture_window_status": capture_gate_status(
            target_date,
            snapshot_captured_at or completed,
        ),
        "source": SOURCE,
        "source_function": SOURCE_FUNCTION,
        "price_field": PRICE_FIELD,
        "price_type": PRICE_TYPE,
        "snapshot_batch_id": snapshot_batch_id,
        "shipment_periods": mapping["shipment_periods"],
        "required_contracts": mapping["required_contracts"],
        "request_mode": "batch",
        "requests": requests,
        "contract_results": contracts,
        "fatal_issues": (
            [{"code": "all_required_contracts_unavailable", "contracts": missing}]
            if len(missing) == len(mapping["required_contracts"])
            else []
        ),
        "warnings": (
            [{"code": "required_contracts_incomplete", "contracts": missing}]
            if missing and len(missing) < len(mapping["required_contracts"])
            else []
        ),
        "candidate_status": (
            "failed"
            if len(missing) == len(mapping["required_contracts"])
            else "passed_with_incomplete" if missing else "success"
        ),
    }


def _validate_retry(attempts: int, retry_seconds: float) -> None:
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        raise DceCandidateBuildError("attempts must be an integer")
    if not 1 <= attempts <= MAX_ATTEMPTS:
        raise DceCandidateBuildError(f"attempts must be between 1 and {MAX_ATTEMPTS}")
    if isinstance(retry_seconds, bool) or not isinstance(retry_seconds, (int, float)):
        raise DceCandidateBuildError("retry-seconds must be numeric")
    if not 0 <= float(retry_seconds) <= MAX_RETRY_SECONDS:
        raise DceCandidateBuildError(
            f"retry-seconds must be between 0 and {MAX_RETRY_SECONDS:g}"
        )


def _validate_destination(destination: Path) -> None:
    resolved = destination.resolve()
    root = REPOSITORY_ROOT.resolve()
    if resolved == root or resolved.is_relative_to(root):
        raise DceCandidateBuildError("output-dir must be outside the repository")
    if destination.exists() and not destination.is_dir():
        raise DceCandidateBuildError("output-dir exists and is not a directory")
    if destination.exists() and any(destination.iterdir()):
        raise DceCandidateBuildError("output-dir must be empty")


def _aware_shanghai(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DceCandidateBuildError("clock must return a timezone-aware datetime")
    return value.astimezone(CAPTURE_ZONE)


def _require_date(value: date) -> date:
    if type(value) is not date:
        raise DceCandidateBuildError("business-date must be a real date")
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    data = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if json.loads(path.read_text("utf-8")) != payload:
        raise DceCandidateBuildError(f"JSON round-trip failed: {path.name}")


def _verify_parquet(path: Path, expected_rows: int) -> None:
    table = pq.read_table(path)
    if table.schema != DCE_NIGHT_SESSION_CLOSE_SCHEMA:
        raise DceCandidateBuildError(f"Parquet schema mismatch: {path.name}")
    if table.num_rows != expected_rows:
        raise DceCandidateBuildError(f"Parquet row count mismatch: {path.name}")


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _file_identity(path: Path, *, filename: str | None = None) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "filename": filename or path.name,
        "size": path.stat().st_size,
        "sha256": digest.hexdigest().upper(),
    }


def _snapshot_batch_id(
    business_date: date,
    captured_at: datetime,
    request_symbol: str,
) -> str:
    payload = (
        f"{business_date.isoformat()}|{captured_at.isoformat()}|{request_symbol}"
    ).encode("utf-8")
    suffix = hashlib.sha256(payload).hexdigest().upper()[:16]
    return f"dce-night-close-{business_date:%Y%m%d}-{suffix}"


def _assert_manifest_safe(manifest: dict[str, Any]) -> None:
    serialized = json.dumps(manifest, ensure_ascii=False)
    if re.search(r"(?i)(?:[A-Z]:[\\/]|/home/|/Users/)", serialized):
        raise DceCandidateBuildError("manifest contains an absolute path")
    forbidden = ("proxy", "username", "password", "token", "secret")
    if any(marker in serialized.casefold() for marker in forbidden):
        raise DceCandidateBuildError("manifest contains forbidden connection identity")


def _cleanup_paths(paths: Any) -> None:
    for path in paths:
        if path.exists():
            path.unlink()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an isolated AkShare DCE night-session-close candidate."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--business-date", required=True, type=date.fromisoformat)
    parser.add_argument("--shipment-period", required=True, action="append")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--retry-seconds", type=float, default=0.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = build_dce_daily_candidate(
            args.config,
            args.business_date,
            args.shipment_period,
            args.output_dir,
            attempts=args.attempts,
            retry_seconds=args.retry_seconds,
        )
    except DceCandidateBuildError as exc:
        missing: list[str] = []
        if exc.quality_report is not None:
            missing = [
                item["contract_code"]
                for item in exc.quality_report.get("contract_results", [])
                if not item["is_usable"]
            ]
        print(
            json.dumps(
                {
                    "candidate_status": "failed",
                    "missing_contracts": missing,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "candidate_status": result["manifest"]["candidate_status"],
                "business_date": result["manifest"]["business_date"],
                "required_contracts": result["manifest"]["required_contracts"],
                "output_files": sorted(path.name for path in result["files"].values()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
