"""Immutable same-day Reuters inputs for daily import-profit materialization."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any
import uuid

from filelock import FileLock, Timeout
import pyarrow as pa
import pyarrow.parquet as pq

from .business_days import require_business_weekday
from .config import SoybeanImportProfitConfig
from .contract_mapping import map_soybean_contracts
from .fx import calculate_tenor_months, select_fx
from .historical_cnf_adapter import shipment_year_for
from .models import FxCurve, FxSelectionStatus
from .standard_io import (
    CBOT_SCHEMA,
    FX_SCHEMA,
    load_cbot_parquet,
    load_fx_parquet,
)


CBOT_SOURCE_FILENAME = "cbot_soybean_daily.parquet"
FX_SOURCE_FILENAME = "usdcny_forward_daily.parquet"
CBOT_FILENAME = "cbot_morning_inputs.parquet"
FX_FILENAME = "fx_morning_inputs.parquet"
MANIFEST_FILENAME = "manifest.json"
QUALITY_FILENAME = "quality_report.json"
INDEX_FILENAME = "external_input_index.json"
LOCK_FILENAME = ".external-input.lock"
SCHEMA_VERSION = "1"
CANDIDATE_VERSION = "1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:\\|/(?:home|tmp|var|Users)/)")
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class MorningExternalInputsError(RuntimeError):
    """Base error for immutable morning external-input candidates."""

    status = "failed"


class MorningExternalInputsLockedError(MorningExternalInputsError):
    status = "locked"


@dataclass(frozen=True, slots=True)
class MorningExternalInputsCandidateResult:
    candidate_id: str
    candidate_status: str
    business_date: date
    candidate_dir: Path
    manifest_sha256: str
    source_sql_sha256: str
    required_cbot_contracts: tuple[str, ...]
    available_cbot_contracts: tuple[str, ...]
    missing_cbot_contracts: tuple[str, ...]
    direct_fx_count: int
    interpolated_fx_count: int
    missing_fx_count: int


@dataclass(frozen=True, slots=True)
class ExternalInputPromotionResult:
    status: str
    candidate: MorningExternalInputsCandidateResult
    generation: int
    previous_candidate_id: str | None
    index_sha256: str


def build_morning_external_inputs_candidate(
    *,
    reuters_candidate_dir: str | Path,
    business_date: date,
    config: SoybeanImportProfitConfig,
    output_dir: str | Path,
    source_file_uploaded_at: datetime,
    prepared_at: datetime,
    candidate_id: str | None = None,
    promoted_at: datetime | None = None,
) -> MorningExternalInputsCandidateResult:
    """Freeze only the exact same-day CBOT and FX inputs needed by 12 shipments."""

    require_business_weekday(business_date)
    uploaded = _aware_utc(source_file_uploaded_at, "source_file_uploaded_at")
    prepared = _aware_utc(prepared_at, "prepared_at")
    promoted = _aware_utc(promoted_at or prepared, "promoted_at")
    source_dir = Path(reuters_candidate_dir)
    destination = Path(output_dir)
    _reject_repository_path(destination)
    if not source_dir.is_dir():
        raise MorningExternalInputsError("Reuters candidate directory does not exist")
    if destination.exists():
        raise MorningExternalInputsError("candidate output directory already exists")
    manifest_path = source_dir / MANIFEST_FILENAME
    source_manifest = _read_json(manifest_path, "Reuters Manifest")
    source_manifest_sha = _sha256(manifest_path)
    source_sql_sha = _required_sha(
        source_manifest.get("source_sha256"), "Reuters source SQL SHA"
    )
    _verify_reuters_output(source_dir, source_manifest, CBOT_SOURCE_FILENAME)
    _verify_reuters_output(source_dir, source_manifest, FX_SOURCE_FILENAME)
    loaded_cbot = load_cbot_parquet(source_dir / CBOT_SOURCE_FILENAME)
    loaded_fx = load_fx_parquet(source_dir / FX_SOURCE_FILENAME)

    shipment_mappings = []
    required_contracts: dict[tuple[int, int], None] = {}
    target_tenors: list[int] = []
    for month in range(1, 13):
        year = shipment_year_for(business_date, month)
        mapped = map_soybean_contracts(config, year, month)
        tenor = calculate_tenor_months(
            business_date, year, month, config.fx_policy
        )
        required_contracts[(mapped.cbot.contract_year, mapped.cbot.contract_month)] = None
        target_tenors.append(tenor)
        shipment_mappings.append(
            {
                "shipment_period": f"{year:04d}-{month:02d}",
                "cbot_contract": _contract_text(
                    mapped.cbot.contract_year, mapped.cbot.contract_month
                ),
                "soymeal_contract": mapped.soymeal.code,
                "soyoil_contract": mapped.soyoil.code,
                "fx_target_tenor": tenor,
            }
        )

    required_set = set(required_contracts)
    selected_cbot = tuple(
        record
        for record in loaded_cbot.records
        if record.market_date == business_date and record.key[1:] in required_set
    )
    available_set = {
        (record.contract_year, record.contract_month)
        for record in selected_cbot
        if record.eligible_for_import_profit
    }
    missing_set = required_set - available_set
    selected_fx = tuple(
        record
        for record in loaded_fx.records
        if record.market_date == business_date
        and config.fx_policy.minimum_tenor_months
        <= record.tenor_months
        <= config.fx_policy.maximum_tenor_months
    )
    curve = FxCurve(
        business_date,
        tuple(record.as_curve_point() for record in selected_fx),
    )
    fx_selections = [
        select_fx(business_date, tenor, curve, config.fx_policy)
        for tenor in target_tenors
    ]
    selection_counts = Counter(item.selection_status.value for item in fx_selections)
    direct_count = selection_counts[FxSelectionStatus.DIRECT.value]
    interpolated_count = selection_counts[FxSelectionStatus.INTERPOLATED.value]
    missing_fx_count = len(fx_selections) - direct_count - interpolated_count
    status = (
        "passed"
        if not missing_set and missing_fx_count == 0
        else "passed_with_incomplete"
    )
    chosen_id = _safe_id(
        candidate_id
        or f"external-{business_date:%Y%m%d}-{source_sql_sha[:12].lower()}"
    )

    destination.mkdir(parents=True)
    created = []
    try:
        cbot_rows = [
            row
            for row in pq.read_table(source_dir / CBOT_SOURCE_FILENAME).to_pylist()
            if row["market_date"] == business_date
            and (row["contract_year"], row["contract_month"]) in required_set
        ]
        fx_rows = [
            row
            for row in pq.read_table(source_dir / FX_SOURCE_FILENAME).to_pylist()
            if row["market_date"] == business_date
            and config.fx_policy.minimum_tenor_months
            <= row["tenor_months"]
            <= config.fx_policy.maximum_tenor_months
        ]
        _write_parquet(destination / CBOT_FILENAME, CBOT_SCHEMA, cbot_rows)
        created.append(destination / CBOT_FILENAME)
        _write_parquet(destination / FX_FILENAME, FX_SCHEMA, fx_rows)
        created.append(destination / FX_FILENAME)
        available_text = tuple(
            _contract_text(*item) for item in sorted(available_set)
        )
        missing_text = tuple(_contract_text(*item) for item in sorted(missing_set))
        required_text = tuple(_contract_text(*item) for item in sorted(required_set))
        warnings = []
        if missing_text:
            warnings.append("required_cbot_contracts_missing")
        if missing_fx_count:
            warnings.append("fx_selection_incomplete")
        quality = {
            "schema_version": SCHEMA_VERSION,
            "business_date": business_date.isoformat(),
            "candidate_status": status,
            "required_cbot_contracts": list(required_text),
            "available_cbot_contracts": list(available_text),
            "missing_cbot_contracts": list(missing_text),
            "fx_available_tenors": sorted(
                record.tenor_months for record in selected_fx
            ),
            "direct_fx_count": direct_count,
            "interpolated_fx_count": interpolated_count,
            "missing_fx_count": missing_fx_count,
            "fatal": [],
            "warning": warnings,
            "final_status": status,
        }
        _write_json(destination / QUALITY_FILENAME, quality)
        created.append(destination / QUALITY_FILENAME)
        outputs = {
            name: _file_identity(destination / name)
            for name in (CBOT_FILENAME, FX_FILENAME, QUALITY_FILENAME)
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "candidate_version": CANDIDATE_VERSION,
            "candidate_id": chosen_id,
            "candidate_status": status,
            "business_date": business_date.isoformat(),
            "source_file_uploaded_at": _utc_text(uploaded),
            "prepared_at": _utc_text(prepared),
            "promoted_at": _utc_text(promoted),
            "source_sql_filename": source_manifest.get("source_filename"),
            "source_sql_size": source_manifest.get("source_size"),
            "source_sql_sha256": source_sql_sha,
            "source_snapshot_sha256": source_sql_sha,
            "source_reuters_candidate": {
                "directory_name": source_dir.name,
                "manifest_filename": MANIFEST_FILENAME,
            },
            "source_manifest_sha256": source_manifest_sha,
            "required_cbot_contracts": list(required_text),
            "available_cbot_contracts": list(available_text),
            "missing_cbot_contracts": list(missing_text),
            "fx_available_tenors": sorted(
                record.tenor_months for record in selected_fx
            ),
            "shipment_mappings": shipment_mappings,
            "fx_selection_status_by_shipment": [
                {
                    "shipment_month": month,
                    "target_tenor": target_tenors[month - 1],
                    "status": fx_selections[month - 1].selection_status.value,
                }
                for month in range(1, 13)
            ],
            "direct_fx_count": direct_count,
            "interpolated_fx_count": interpolated_count,
            "missing_fx_count": missing_fx_count,
            "output_files": outputs,
            "warning": warnings,
            "fatal": [],
        }
        _assert_safe_payload(manifest)
        _write_json(destination / MANIFEST_FILENAME, manifest)
        created.append(destination / MANIFEST_FILENAME)
        _verify_candidate_files(destination, manifest)
        return MorningExternalInputsCandidateResult(
            candidate_id=chosen_id,
            candidate_status=status,
            business_date=business_date,
            candidate_dir=destination,
            manifest_sha256=_sha256(destination / MANIFEST_FILENAME),
            source_sql_sha256=source_sql_sha,
            required_cbot_contracts=required_text,
            available_cbot_contracts=available_text,
            missing_cbot_contracts=missing_text,
            direct_fx_count=direct_count,
            interpolated_fx_count=interpolated_count,
            missing_fx_count=missing_fx_count,
        )
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        raise


def store_morning_external_inputs_candidate(
    external_input_root: str | Path,
    *,
    reuters_candidate_dir: str | Path,
    business_date: date,
    config: SoybeanImportProfitConfig,
    candidate_id: str,
    source_file_uploaded_at: datetime,
    prepared_at: datetime,
    promoted_at: datetime,
    lock_timeout_seconds: float = 10.0,
) -> ExternalInputPromotionResult:
    """Build, freeze, and atomically promote one external-input candidate."""

    root = Path(external_input_root)
    _reject_repository_path(root)
    candidates = root / "candidates"
    chosen_id = _safe_id(candidate_id)
    final = candidates / chosen_id
    root.mkdir(parents=True, exist_ok=True)
    candidates.mkdir(exist_ok=True)
    lock = FileLock(str(root / LOCK_FILENAME))
    try:
        lock.acquire(timeout=lock_timeout_seconds)
    except Timeout as exc:
        raise MorningExternalInputsLockedError(
            "external-input lock acquisition timed out"
        ) from exc
    building = candidates / f".building-{uuid.uuid4().hex}"
    try:
        current = _optional_index(root / INDEX_FILENAME)
        source_manifest = Path(reuters_candidate_dir) / MANIFEST_FILENAME
        source_sql_sha = _required_sha(
            _read_json(source_manifest, "Reuters Manifest").get("source_sha256"),
            "Reuters source SQL SHA",
        )
        if (
            current is not None
            and current["business_date"] == business_date.isoformat()
            and current["source_sql_sha256"] == source_sql_sha
        ):
            candidate = load_current_external_input_candidate(root)
            return ExternalInputPromotionResult(
                status="no_change",
                candidate=candidate,
                generation=current["generation"],
                previous_candidate_id=current["previous_candidate_id"],
                index_sha256=_sha256(root / INDEX_FILENAME),
            )
        if final.exists():
            raise MorningExternalInputsError("candidate_id already exists")
        candidate = build_morning_external_inputs_candidate(
            reuters_candidate_dir=reuters_candidate_dir,
            business_date=business_date,
            config=config,
            output_dir=building,
            source_file_uploaded_at=source_file_uploaded_at,
            prepared_at=prepared_at,
            promoted_at=promoted_at,
            candidate_id=chosen_id,
        )
        building.replace(final)
        candidate = replace(candidate, candidate_dir=final)
        generation = 1 if current is None else current["generation"] + 1
        previous = None if current is None else current["current_candidate_id"]
        index = {
            "schema_version": SCHEMA_VERSION,
            "generation": generation,
            "current_candidate_id": chosen_id,
            "previous_candidate_id": previous,
            "business_date": business_date.isoformat(),
            "updated_at": _utc_text(promoted_at),
            "source_sql_sha256": candidate.source_sql_sha256,
            "current_manifest_sha256": candidate.manifest_sha256,
        }
        previous_sha = (
            None if current is None else _sha256(root / INDEX_FILENAME)
        )
        index_sha = _replace_json_atomically(
            root / INDEX_FILENAME, index, previous_sha
        )
        return ExternalInputPromotionResult(
            status="promoted",
            candidate=candidate,
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


def load_current_external_input_candidate(
    external_input_root: str | Path,
) -> MorningExternalInputsCandidateResult:
    root = Path(external_input_root)
    index = _read_json(root / INDEX_FILENAME, "external-input Index")
    _require_exact_index(index)
    candidate_id = _safe_id(index["current_candidate_id"])
    candidate_dir = root / "candidates" / candidate_id
    manifest_path = candidate_dir / MANIFEST_FILENAME
    if _sha256(manifest_path) != index["current_manifest_sha256"]:
        raise MorningExternalInputsError("external-input Manifest identity mismatch")
    manifest = _read_json(manifest_path, "external-input Manifest")
    _verify_candidate_files(candidate_dir, manifest)
    if manifest["business_date"] != index["business_date"]:
        raise MorningExternalInputsError("external-input business_date mismatch")
    return MorningExternalInputsCandidateResult(
        candidate_id=candidate_id,
        candidate_status=manifest["candidate_status"],
        business_date=date.fromisoformat(manifest["business_date"]),
        candidate_dir=candidate_dir,
        manifest_sha256=index["current_manifest_sha256"],
        source_sql_sha256=manifest["source_sql_sha256"],
        required_cbot_contracts=tuple(manifest["required_cbot_contracts"]),
        available_cbot_contracts=tuple(manifest["available_cbot_contracts"]),
        missing_cbot_contracts=tuple(manifest["missing_cbot_contracts"]),
        direct_fx_count=manifest["direct_fx_count"],
        interpolated_fx_count=manifest["interpolated_fx_count"],
        missing_fx_count=manifest["missing_fx_count"],
    )


def _verify_reuters_output(
    source_dir: Path, manifest: dict[str, Any], filename: str
) -> None:
    identity = manifest.get("output_files", {}).get(filename)
    path = source_dir / filename
    if (
        not isinstance(identity, dict)
        or not path.is_file()
        or identity.get("filename") != filename
        or identity.get("size") != path.stat().st_size
        or identity.get("sha256") != _sha256(path)
    ):
        raise MorningExternalInputsError(
            f"Reuters candidate identity mismatch: {filename}"
        )


def _verify_candidate_files(
    candidate_dir: Path, manifest: dict[str, Any]
) -> None:
    if manifest.get("candidate_status") not in {
        "passed",
        "passed_with_incomplete",
    }:
        raise MorningExternalInputsError("external-input status is invalid")
    outputs = manifest.get("output_files")
    if not isinstance(outputs, dict):
        raise MorningExternalInputsError("external-input output identity is invalid")
    for filename, schema in (
        (CBOT_FILENAME, CBOT_SCHEMA),
        (FX_FILENAME, FX_SCHEMA),
    ):
        identity = outputs.get(filename)
        path = candidate_dir / filename
        if (
            not isinstance(identity, dict)
            or identity != _file_identity(path)
            or pq.read_table(path).schema != schema
        ):
            raise MorningExternalInputsError(
                f"external-input file identity mismatch: {filename}"
            )
    quality = outputs.get(QUALITY_FILENAME)
    if quality != _file_identity(candidate_dir / QUALITY_FILENAME):
        raise MorningExternalInputsError("external-input quality identity mismatch")


def _write_parquet(path: Path, schema: pa.Schema, rows: list[dict]) -> None:
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path, compression="zstd")
    if pq.read_table(path).schema != schema:
        raise MorningExternalInputsError("Parquet schema readback mismatch")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _replace_json_atomically(
    path: Path, payload: dict[str, Any], expected_previous_sha256: str | None
) -> str:
    current_sha = _sha256(path) if path.is_file() else None
    if current_sha != expected_previous_sha256:
        raise MorningExternalInputsError("input Index changed before promotion")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_json(temporary, payload)
        os.replace(temporary, path)
        if _read_json(path, "input Index") != payload:
            raise MorningExternalInputsError("input Index readback mismatch")
        return _sha256(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _require_exact_index(payload: dict[str, Any]) -> None:
    expected = {
        "schema_version",
        "generation",
        "current_candidate_id",
        "previous_candidate_id",
        "business_date",
        "updated_at",
        "source_sql_sha256",
        "current_manifest_sha256",
    }
    if set(payload) != expected:
        raise MorningExternalInputsError("external-input Index fields are invalid")
    _safe_id(payload["current_candidate_id"])
    _required_sha(payload["source_sql_sha256"], "source SQL SHA")
    _required_sha(payload["current_manifest_sha256"], "Manifest SHA")
    date.fromisoformat(payload["business_date"])


def _optional_index(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = _read_json(path, "external-input Index")
    _require_exact_index(payload)
    return payload


def _index_references(path: Path, candidate_id: str) -> bool:
    try:
        return _read_json(path, "input Index").get("current_candidate_id") == candidate_id
    except Exception:
        return False


def _file_identity(path: Path) -> dict[str, Any]:
    return {
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise MorningExternalInputsError(f"{label} does not exist")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MorningExternalInputsError(f"{label} is unreadable") from exc
    if not isinstance(payload, dict):
        raise MorningExternalInputsError(f"{label} must be a JSON object")
    return payload


def _safe_id(value: object) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise MorningExternalInputsError("candidate_id is unsafe")
    return value


def _required_sha(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9A-Fa-f]{64}", value) is None
    ):
        raise MorningExternalInputsError(f"{label} is invalid")
    return value.upper()


def _aware_utc(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise MorningExternalInputsError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _aware_utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _contract_text(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _assert_safe_payload(payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False)
    if _ABSOLUTE_PATH.search(serialized):
        raise MorningExternalInputsError("Manifest contains an absolute path")
    for forbidden in ("password", "token", "proxy", "username"):
        if forbidden in serialized.lower():
            raise MorningExternalInputsError("Manifest contains forbidden identity")


def _reject_repository_path(path: Path) -> None:
    resolved = path.resolve()
    if resolved == _REPOSITORY_ROOT or _REPOSITORY_ROOT in resolved.parents:
        raise MorningExternalInputsError(
            "morning input storage must remain outside the repository"
        )
