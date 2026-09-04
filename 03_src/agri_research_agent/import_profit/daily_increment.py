"""Immutable DCE night-session-close outcomes and daily business-key helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
import shutil
from typing import Callable
import uuid

from filelock import FileLock, Timeout
import pyarrow as pa
import pyarrow.parquet as pq

from .business_days import require_business_weekday
from .cnf_store import ALLOWED_SOURCE, CnfQuoteRecord
from .config import SoybeanImportProfitConfig
from .contract_override import select_soybean_contracts
from .dce_daily import (
    CAPTURE_ZONE,
    DceSpotBatchResult,
    SpotFetcher,
    TradeCalendarFetcher,
    default_spot_fetcher,
    fetch_dce_night_session_close_snapshot,
    records_as_dicts,
)
from .historical_cnf_adapter import shipment_year_for
from .models import BusinessKey
from .morning_external_inputs import (
    _file_identity,
    _index_references,
    _read_json,
    _replace_json_atomically,
    _reject_repository_path,
    _required_sha,
    _safe_id,
    _sha256,
    _utc_text,
)
from .standard_io import DCE_INCREMENTAL_SCHEMA, load_dce_parquet


DCE_FILENAME = "dce_night_session_close_prices.parquet"
MANIFEST_FILENAME = "manifest.json"
QUALITY_FILENAME = "quality_report.json"
INDEX_FILENAME = "dce_input_index.json"
LOCK_FILENAME = ".dce-input.lock"
SCHEMA_VERSION = "1"


class DailyIncrementError(RuntimeError):
    """Base error for DCE capture storage and daily-key generation."""


class DceInputLockedError(DailyIncrementError):
    status = "locked"


@dataclass(frozen=True, slots=True)
class DceNightSessionCloseOutcome:
    business_date: date
    attempt_status: str
    attempt_started_at: datetime
    attempt_finished_at: datetime
    captured_at: datetime
    snapshot_batch_id: str
    requested_contracts: tuple[str, ...]
    available_contracts: tuple[str, ...]
    missing_contracts: tuple[str, ...]
    record_count: int
    failure_reasons: tuple[str, ...]
    candidate_id: str
    candidate_dir: Path
    manifest_sha256: str


DceMorningCaptureOutcome = DceNightSessionCloseOutcome


@dataclass(frozen=True, slots=True)
class DceInputPromotionResult:
    status: str
    outcome: DceNightSessionCloseOutcome
    generation: int
    previous_candidate_id: str | None
    index_sha256: str


def generate_daily_business_keys(
    business_date: date, config: SoybeanImportProfitConfig
) -> tuple[BusinessKey, ...]:
    """Generate the fixed four-origin by twelve-shipment daily key set."""

    require_business_weekday(business_date)
    return tuple(
        BusinessKey(
            business_date=business_date,
            commodity=config.commodity,
            origin=origin,
            shipment_year=shipment_year_for(business_date, month),
            shipment_month=month,
            allowed_origins=config.origin_codes,
            expected_commodity=config.commodity,
        )
        for origin in ("brazil", "us_gulf", "us_pnw", "argentina")
        for month in range(1, 13)
    )


def required_dce_contracts(
    business_date: date, config: SoybeanImportProfitConfig
) -> tuple[str, ...]:
    required: dict[str, None] = {}
    for business_key in generate_daily_business_keys(business_date, config):
        selection = select_soybean_contracts(config, business_key)
        required[selection.soymeal.effective_contract.code] = None
        required[selection.soyoil.effective_contract.code] = None
    return tuple(sorted(required))


def empty_daily_cnf_records(
    keys: tuple[BusinessKey, ...],
    *,
    calculated_at: datetime,
    batch_id: str,
) -> tuple[CnfQuoteRecord, ...]:
    """Represent pending manual CNF in memory without writing CNF placeholders."""

    return tuple(
        CnfQuoteRecord(
            business_key=key,
            cnf_cents_per_bushel=None,
            source=ALLOWED_SOURCE,
            updated_at=calculated_at,
            batch_id=batch_id,
        )
        for key in keys
    )


def capture_and_store_dce_night_session_close(
    dce_input_root: str | Path,
    *,
    business_date: date,
    config: SoybeanImportProfitConfig,
    candidate_id: str,
    snapshot_batch_id: str,
    fetcher: SpotFetcher = default_spot_fetcher,
    trade_calendar_fetcher: TradeCalendarFetcher | None = None,
    clock: Callable[[], datetime] | None = None,
    lock_timeout_seconds: float = 10.0,
) -> DceInputPromotionResult:
    """Perform one legal-window batch request and atomically freeze its outcome."""

    require_business_weekday(business_date)
    chosen_id = _safe_id(candidate_id)
    batch_id = _safe_id(snapshot_batch_id)
    now = clock or (lambda: datetime.now(CAPTURE_ZONE))
    started = now()
    requested = required_dce_contracts(business_date, config)
    batch = fetch_dce_night_session_close_snapshot(
        requested,
        business_date,
        fetcher=fetcher,
        trade_calendar_fetcher=trade_calendar_fetcher,
        captured_at=started,
        enforce_capture_window=True,
    )
    finished = now()
    if batch.capture_gate_status != "valid":
        raise DailyIncrementError(
            "DCE capture was not executed inside the legal 08:30:00-08:32:59 window"
        )
    available = tuple(
        sorted(
            item.contract.code
            for item in batch.contract_results
            if item.is_usable and item.record is not None
        )
    )
    missing = tuple(sorted(set(requested) - set(available)))
    status = batch.attempt_status
    reasons = tuple(
        sorted(
            {
                item.quality_status
                for item in batch.contract_results
                if not item.is_usable
            }
            | ({batch.source_error_type} if batch.source_error_type else set())
        )
    )
    root = Path(dce_input_root)
    _reject_repository_path(root)
    candidates = root / "candidates"
    final = candidates / chosen_id
    root.mkdir(parents=True, exist_ok=True)
    candidates.mkdir(exist_ok=True)
    lock = FileLock(str(root / LOCK_FILENAME))
    try:
        lock.acquire(timeout=lock_timeout_seconds)
    except Timeout as exc:
        raise DceInputLockedError("DCE input lock acquisition timed out") from exc
    building = candidates / f".building-{uuid.uuid4().hex}"
    try:
        current = _optional_dce_index(root / INDEX_FILENAME)
        if final.exists():
            raise DailyIncrementError("DCE candidate_id already exists")
        building.mkdir()
        outputs = {}
        if batch.records:
            _write_dce_parquet(building / DCE_FILENAME, batch)
            outputs[DCE_FILENAME] = _file_identity(building / DCE_FILENAME)
        quality = {
            "schema_version": SCHEMA_VERSION,
            "business_date": business_date.isoformat(),
            "attempt_status": status,
            "capture_gate_status": batch.capture_gate_status,
            "requested_contracts": list(requested),
            "available_contracts": list(available),
            "missing_contracts": list(missing),
            "failure_reasons": list(reasons),
            "record_count": len(batch.records),
            "fatal": [],
            "warning": (
                []
                if status == "success"
                else [
                    "dce_contracts_incomplete"
                    if status == "passed_with_incomplete"
                    else "dce_capture_failed"
                ]
            ),
            "final_status": status,
        }
        _write_json(building / QUALITY_FILENAME, quality)
        outputs[QUALITY_FILENAME] = _file_identity(building / QUALITY_FILENAME)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": chosen_id,
            "business_date": business_date.isoformat(),
            "attempt_status": status,
            "attempt_started_at": started.isoformat(),
            "attempt_finished_at": finished.isoformat(),
            "captured_at": batch.captured_at.isoformat(),
            "capture_timezone": "Asia/Shanghai",
            "capture_gate_status": batch.capture_gate_status,
            "snapshot_batch_id": batch_id,
            "requested_contracts": list(requested),
            "available_contracts": list(available),
            "missing_contracts": list(missing),
            "record_count": len(batch.records),
            "failure_reasons": list(reasons),
            "source": "akshare",
            "source_function": "futures_zh_spot",
            "price_field": "current_price",
            "price_type": "night_session_close",
            "scheduled_time": "08:30:00",
            "previous_trading_date": (
                batch.previous_trading_date.isoformat()
                if batch.previous_trading_date is not None
                else None
            ),
            "trade_calendar_source": batch.trade_calendar_source,
            "output_files": outputs,
            "quality_report_filename": QUALITY_FILENAME,
        }
        _write_json(building / MANIFEST_FILENAME, manifest)
        manifest_sha = _sha256(building / MANIFEST_FILENAME)
        _validate_dce_candidate(building, manifest)
        building.replace(final)
        generation = 1 if current is None else current["generation"] + 1
        previous = None if current is None else current["current_candidate_id"]
        index = {
            "schema_version": SCHEMA_VERSION,
            "generation": generation,
            "current_candidate_id": chosen_id,
            "previous_candidate_id": previous,
            "business_date": business_date.isoformat(),
            "capture_status": status,
            "captured_at": batch.captured_at.isoformat(),
            "snapshot_batch_id": batch_id,
            "current_manifest_sha256": manifest_sha,
        }
        previous_sha = None if current is None else _sha256(root / INDEX_FILENAME)
        index_sha = _replace_json_atomically(
            root / INDEX_FILENAME, index, previous_sha
        )
        outcome = DceNightSessionCloseOutcome(
            business_date=business_date,
            attempt_status=status,
            attempt_started_at=started,
            attempt_finished_at=finished,
            captured_at=batch.captured_at,
            snapshot_batch_id=batch_id,
            requested_contracts=requested,
            available_contracts=available,
            missing_contracts=missing,
            record_count=len(batch.records),
            failure_reasons=reasons,
            candidate_id=chosen_id,
            candidate_dir=final,
            manifest_sha256=manifest_sha,
        )
        return DceInputPromotionResult(
            status="promoted",
            outcome=outcome,
            generation=generation,
            previous_candidate_id=previous,
            index_sha256=index_sha,
        )
    except Exception:
        if building.exists():
            shutil.rmtree(building)
        if final.exists() and not _index_references(root / INDEX_FILENAME, chosen_id):
            shutil.rmtree(final)
        raise
    finally:
        if lock.is_locked:
            lock.release()


def load_current_dce_outcome(
    dce_input_root: str | Path,
) -> DceNightSessionCloseOutcome:
    root = Path(dce_input_root)
    index = _read_json(root / INDEX_FILENAME, "DCE input Index")
    _require_exact_dce_index(index)
    candidate_id = _safe_id(index["current_candidate_id"])
    candidate_dir = root / "candidates" / candidate_id
    manifest_path = candidate_dir / MANIFEST_FILENAME
    if _sha256(manifest_path) != index["current_manifest_sha256"]:
        raise DailyIncrementError("DCE input Manifest identity mismatch")
    manifest = _read_json(manifest_path, "DCE input Manifest")
    _validate_dce_candidate(candidate_dir, manifest)
    if (
        manifest["business_date"] != index["business_date"]
        or manifest["attempt_status"] != index["capture_status"]
        or manifest["snapshot_batch_id"] != index["snapshot_batch_id"]
    ):
        raise DailyIncrementError("DCE input Index and Manifest disagree")
    return DceNightSessionCloseOutcome(
        business_date=date.fromisoformat(manifest["business_date"]),
        attempt_status=manifest["attempt_status"],
        attempt_started_at=datetime.fromisoformat(manifest["attempt_started_at"]),
        attempt_finished_at=datetime.fromisoformat(manifest["attempt_finished_at"]),
        captured_at=datetime.fromisoformat(manifest["captured_at"]),
        snapshot_batch_id=manifest["snapshot_batch_id"],
        requested_contracts=tuple(manifest["requested_contracts"]),
        available_contracts=tuple(manifest["available_contracts"]),
        missing_contracts=tuple(manifest["missing_contracts"]),
        record_count=manifest["record_count"],
        failure_reasons=tuple(manifest["failure_reasons"]),
        candidate_id=candidate_id,
        candidate_dir=candidate_dir,
        manifest_sha256=index["current_manifest_sha256"],
    )


def _write_dce_parquet(path: Path, batch: DceSpotBatchResult) -> None:
    table = pa.Table.from_pylist(
        records_as_dicts(batch.records), schema=DCE_INCREMENTAL_SCHEMA
    )
    pq.write_table(table, path, compression="zstd")
    loaded = load_dce_parquet(path)
    if len(loaded.records) != len(batch.records):
        raise DailyIncrementError("DCE Parquet readback count mismatch")


def _validate_dce_candidate(candidate_dir: Path, manifest: dict) -> None:
    status = manifest.get("attempt_status")
    if status not in {"success", "passed_with_incomplete", "failed"}:
        raise DailyIncrementError("DCE attempt status is invalid")
    outputs = manifest.get("output_files")
    if not isinstance(outputs, dict):
        raise DailyIncrementError("DCE output identities are invalid")
    quality_path = candidate_dir / QUALITY_FILENAME
    if outputs.get(QUALITY_FILENAME) != _file_identity(quality_path):
        raise DailyIncrementError("DCE quality identity mismatch")
    parquet_path = candidate_dir / DCE_FILENAME
    if manifest.get("record_count", 0) > 0:
        if outputs.get(DCE_FILENAME) != _file_identity(parquet_path):
            raise DailyIncrementError("DCE Parquet identity mismatch")
        loaded = load_dce_parquet(parquet_path)
        if len(loaded.records) != manifest["record_count"]:
            raise DailyIncrementError("DCE record count mismatch")
    elif parquet_path.exists() or DCE_FILENAME in outputs:
        raise DailyIncrementError("empty DCE outcome must not contain Parquet")
    if manifest.get("capture_gate_status") != "valid":
        raise DailyIncrementError("DCE outcome lacks a legal capture attempt")


def _optional_dce_index(path: Path) -> dict | None:
    if not path.exists():
        return None
    payload = _read_json(path, "DCE input Index")
    _require_exact_dce_index(payload)
    return payload


def _require_exact_dce_index(payload: dict) -> None:
    expected = {
        "schema_version",
        "generation",
        "current_candidate_id",
        "previous_candidate_id",
        "business_date",
        "capture_status",
        "captured_at",
        "snapshot_batch_id",
        "current_manifest_sha256",
    }
    if set(payload) != expected:
        raise DailyIncrementError("DCE input Index fields are invalid")
    _safe_id(payload["current_candidate_id"])
    _safe_id(payload["snapshot_batch_id"])
    _required_sha(payload["current_manifest_sha256"], "DCE Manifest SHA")
    date.fromisoformat(payload["business_date"])
    if payload["capture_status"] not in {"success", "passed_with_incomplete", "failed"}:
        raise DailyIncrementError("DCE input Index status is invalid")


# Compatibility alias for pre-production callers; no separate 09:00 path remains.
capture_and_store_dce_morning_input = capture_and_store_dce_night_session_close


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
