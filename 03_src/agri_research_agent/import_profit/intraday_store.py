"""Append-only AM/PM soybean profit results with CNF identity enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from filelock import FileLock, Timeout

from agri_research_agent.market_data.intraday import MarketSession

from .intraday import (
    SoybeanIntradayCnfMismatchError,
    SoybeanIntradayProfitResult,
    require_shared_cnf_identity,
)


SCHEMA_VERSION = "1"
RELEASES_DIRNAME = "releases"
INDEX_FILENAME = "intraday_profit_index.json"
RESULTS_FILENAME = "soybean_intraday_profit_results.json"
MANIFEST_FILENAME = "manifest.json"


class IntradayProfitStoreError(RuntimeError):
    status = "STORE_ERROR"


class IntradayProfitStoreValidationError(IntradayProfitStoreError):
    status = "VALIDATION_FAILED"


class IntradayProfitStoreConflictError(IntradayProfitStoreError):
    status = "IMMUTABLE_CONFLICT"


class IntradayProfitStoreNotFoundError(IntradayProfitStoreError):
    status = "NOT_FOUND"


class IntradayProfitSealStatus(StrEnum):
    SEALED = "SEALED"
    NO_CHANGE = "NO_CHANGE"


@dataclass(frozen=True, slots=True)
class SoybeanIntradayResultBatch:
    business_date: date
    session: MarketSession
    market_snapshot_release_id: str
    market_snapshot_sha256: str
    market_captured_at: datetime
    cnf_identity: str
    calculated_at: datetime
    results: tuple[SoybeanIntradayProfitResult, ...]

    def __post_init__(self) -> None:
        if type(self.business_date) is not date:
            raise IntradayProfitStoreValidationError("business_date must be an exact date")
        if not isinstance(self.session, MarketSession):
            raise IntradayProfitStoreValidationError("session must be AM or PM")
        if not isinstance(self.results, tuple) or not self.results:
            raise IntradayProfitStoreValidationError("results must be a non-empty immutable tuple")
        if any(not isinstance(result, SoybeanIntradayProfitResult) for result in self.results):
            raise IntradayProfitStoreValidationError("results contain an invalid value")
        keys = set()
        environments = {result.market_inputs.snapshot_environment for result in self.results}
        if len(environments) != 1 or not environments <= {"FORMAL", "TEST_ISOLATED_NON_PRODUCTION"}:
            raise IntradayProfitStoreValidationError("result batch environment mismatch")
        for result in self.results:
            if not isinstance(result, SoybeanIntradayProfitResult):
                raise IntradayProfitStoreValidationError("results contain an invalid value")
            if result.business_key.business_date != self.business_date or result.session is not self.session:
                raise IntradayProfitStoreValidationError("result batch identity mismatch")
            if (
                result.market_snapshot_release_id != self.market_snapshot_release_id
                or result.market_snapshot_sha256 != self.market_snapshot_sha256
                or result.market_captured_at != self.market_captured_at
            ):
                raise IntradayProfitStoreValidationError("market snapshot identity differs within batch")
            if result.key in keys:
                raise IntradayProfitStoreValidationError("result batch contains a duplicate stable key")
            keys.add(result.key)
        if require_shared_cnf_identity(self.results) != self.cnf_identity:
            raise SoybeanIntradayCnfMismatchError("batch CNF identity differs from its results")

    @property
    def release_id(self) -> str:
        return f"{self.business_date.isoformat()}-{self.session.value}"

    @property
    def content_sha256(self) -> str:
        return _sha(
            {
                "schema_version": SCHEMA_VERSION,
                "business_date": self.business_date.isoformat(),
                "session": self.session.value,
                "market_snapshot_release_id": self.market_snapshot_release_id,
                "market_snapshot_sha256": self.market_snapshot_sha256,
                "market_captured_at": self.market_captured_at.isoformat(),
                "cnf_identity": self.cnf_identity,
                "results": [_result_row(item, include_calculated_at=False) for item in sorted(self.results, key=lambda x: x.key)],
            }
        )


@dataclass(frozen=True, slots=True)
class IntradayProfitSealResult:
    status: IntradayProfitSealStatus
    release_id: str
    content_sha256: str
    release_dir: Path


@dataclass(frozen=True, slots=True)
class ResolvedIntradayProfitRelease:
    business_date: date
    session: MarketSession
    release_id: str
    market_snapshot_release_id: str
    market_snapshot_sha256: str
    market_captured_at: datetime
    cnf_identity: str
    calculated_at: datetime
    content_sha256: str
    rows: tuple[dict[str, object], ...]
    environment: str | None = None


def list_intraday_profit_batches(
    root: str | Path,
    session: MarketSession | str,
    *,
    expected_environment: str | None = None,
    snapshot_root: str | Path | None = None,
) -> tuple[ResolvedIntradayProfitRelease, ...]:
    """Resolve every sealed batch for one session in business-date order."""

    resolved_session = MarketSession(session)
    index_path = Path(root).resolve() / INDEX_FILENAME
    if not index_path.exists():
        # No PM index is a valid pre-cutover state, not a prerequisite for
        # legacy history. A present but invalid index must still fail closed.
        return ()
    index = _read(index_path)
    if not isinstance(index, dict) or index.get("schema_version") != SCHEMA_VERSION:
        raise IntradayProfitStoreValidationError("intraday profit index is invalid")
    by_date = index.get("by_date")
    if not isinstance(by_date, dict):
        raise IntradayProfitStoreValidationError("intraday profit index dates are invalid")
    batches = []
    for day_text, sessions in sorted(by_date.items()):
        if not isinstance(sessions, dict) or resolved_session.value not in sessions:
            continue
        try:
            day = date.fromisoformat(day_text)
        except (TypeError, ValueError):
            raise IntradayProfitStoreValidationError(
                "intraday profit index contains an invalid business date"
            ) from None
        batch = load_intraday_profit_batch(root, day, resolved_session,
            expected_environment=expected_environment, snapshot_root=snapshot_root)
        reference = sessions[resolved_session.value]
        expected = {"release_id": batch.release_id, "content_sha256": batch.content_sha256,
                    "cnf_identity": batch.cnf_identity, "market_snapshot_sha256": batch.market_snapshot_sha256}
        if not isinstance(reference, dict) or any(reference.get(k) != v for k, v in expected.items()):
            raise IntradayProfitStoreValidationError("intraday profit index/release identity mismatch")
        batches.append(batch)
    return tuple(batches)


def seal_intraday_profit_batch(root: str | Path, batch: SoybeanIntradayResultBatch) -> IntradayProfitSealResult:
    store = Path(root).resolve()
    store.mkdir(parents=True, exist_ok=True)
    releases = store / RELEASES_DIRNAME
    releases.mkdir(exist_ok=True)
    final = releases / batch.release_id
    lock = FileLock(str(store / ".intraday-profit.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise IntradayProfitStoreError("intraday profit store is locked") from exc
    try:
        _require_other_session_cnf(store, batch)
        if final.exists():
            existing = load_intraday_profit_batch(store, batch.business_date, batch.session)
            if existing.content_sha256 != batch.content_sha256:
                raise IntradayProfitStoreConflictError(
                    "sealed soybean date/session exists with different content"
                )
            return IntradayProfitSealResult(
                IntradayProfitSealStatus.NO_CHANGE, batch.release_id, batch.content_sha256, final
            )
        building = releases / f".building-{batch.release_id}-{uuid.uuid4().hex}"
        building.mkdir()
        rows = [_result_row(item, include_calculated_at=True) for item in sorted(batch.results, key=lambda x: x.key)]
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "release_id": batch.release_id,
            "business_date": batch.business_date.isoformat(),
            "session": batch.session.value,
            "market_snapshot_release_id": batch.market_snapshot_release_id,
            "market_snapshot_sha256": batch.market_snapshot_sha256,
            "market_captured_at": batch.market_captured_at.isoformat(),
            "cnf_identity": batch.cnf_identity,
            "calculated_at": batch.calculated_at.isoformat(),
            "record_count": len(rows),
            "content_sha256": batch.content_sha256,
            "results_sha256": _sha(rows),
            "immutable": True,
            "status": "SEALED",
            "environment": batch.results[0].market_inputs.snapshot_environment,
        }
        try:
            _write_exclusive(building / RESULTS_FILENAME, rows)
            _write_exclusive(building / MANIFEST_FILENAME, manifest)
            os.replace(building, final)
        except BaseException:
            if building.exists():
                shutil.rmtree(building)
            raise
        _update_index(store, batch, manifest)
        return IntradayProfitSealResult(
            IntradayProfitSealStatus.SEALED, batch.release_id, batch.content_sha256, final
        )
    finally:
        lock.release()


def load_intraday_profit_batch(
    root: str | Path,
    business_date: date,
    session: MarketSession | str,
    *,
    expected_environment: str | None = None,
    snapshot_root: str | Path | None = None,
) -> ResolvedIntradayProfitRelease:
    resolved_session = MarketSession(session)
    release_id = f"{business_date.isoformat()}-{resolved_session.value}"
    release = Path(root).resolve() / RELEASES_DIRNAME / release_id
    manifest = _read(release / MANIFEST_FILENAME)
    rows = _read(release / RESULTS_FILENAME)
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        raise IntradayProfitStoreValidationError("intraday profit release is invalid")
    if (manifest.get("schema_version") != SCHEMA_VERSION
            or manifest.get("immutable") is not True
            or manifest.get("status", "SEALED") != "SEALED"
            or manifest.get("release_id") != release_id
            or manifest.get("business_date") != business_date.isoformat()
            or manifest.get("session") != resolved_session.value
            or manifest.get("market_snapshot_release_id") != release_id):
        raise IntradayProfitStoreValidationError("intraday profit sealed identity mismatch")
    environment = manifest.get("environment")
    allowed_environments = {"FORMAL", "TEST_ISOLATED_NON_PRODUCTION"}
    if environment is not None and environment not in allowed_environments:
        raise IntradayProfitStoreValidationError("Preview/unknown result environment is forbidden")
    if expected_environment is not None and expected_environment not in allowed_environments:
        raise IntradayProfitStoreValidationError("invalid expected result environment")
    if snapshot_root is not None:
        from agri_research_agent.market_data.intraday import load_intraday_snapshot
        snapshot = load_intraday_snapshot(snapshot_root, business_date, resolved_session,
                                          expected_environment=expected_environment)
        if (snapshot.content_sha256 != manifest.get("market_snapshot_sha256")
                or snapshot.captured_at.isoformat() != manifest.get("market_captured_at")
                or environment not in (None, snapshot.environment)):
            raise IntradayProfitStoreValidationError("result/SEALED market snapshot identity mismatch")
        # v1 results have no environment field. Derive it only from the verified
        # referenced snapshot, never from a path name or an assumed default.
        environment = snapshot.environment
    if expected_environment is not None and environment != expected_environment:
        raise IntradayProfitStoreValidationError("result environment cannot be verified")
    if manifest.get("results_sha256") != _sha(rows) or manifest.get("record_count") != len(rows):
        raise IntradayProfitStoreValidationError("intraday profit row identity mismatch")
    content_rows = []
    keys = set()
    for row in rows:
        if not isinstance(row, dict):
            raise IntradayProfitStoreValidationError("intraday profit result row is invalid")
        if any(row.get(name) != manifest.get(name) for name in (
                "business_date", "session", "market_snapshot_release_id",
                "market_snapshot_sha256", "market_captured_at", "cnf_identity")):
            raise IntradayProfitStoreValidationError("intraday profit row/batch identity mismatch")
        key = (row.get("commodity"), row.get("origin"), row.get("shipment_year"), row.get("shipment_month"))
        if key in keys:
            raise IntradayProfitStoreValidationError("duplicate intraday profit business key")
        keys.add(key)
        if row.get("cnf_cents_per_bushel") is not None and row.get("cnf_source") != "manual_ui":
            raise IntradayProfitStoreValidationError("intraday CNF must be manual_ui, not Preview/history")
        content_row = dict(row)
        content_row.pop("calculated_at", None)
        content_rows.append(content_row)
    expected_content_sha = _sha(
        {
            "schema_version": SCHEMA_VERSION,
            "business_date": business_date.isoformat(),
            "session": resolved_session.value,
            "market_snapshot_release_id": manifest.get("market_snapshot_release_id"),
            "market_snapshot_sha256": manifest.get("market_snapshot_sha256"),
            "market_captured_at": manifest.get("market_captured_at"),
            "cnf_identity": manifest.get("cnf_identity"),
            "results": content_rows,
        }
    )
    if manifest.get("content_sha256") != expected_content_sha:
        raise IntradayProfitStoreValidationError("intraday profit content identity mismatch")
    return ResolvedIntradayProfitRelease(
        business_date=business_date,
        session=resolved_session,
        release_id=release_id,
        market_snapshot_release_id=str(manifest["market_snapshot_release_id"]),
        market_snapshot_sha256=str(manifest["market_snapshot_sha256"]),
        market_captured_at=datetime.fromisoformat(str(manifest["market_captured_at"])),
        cnf_identity=str(manifest["cnf_identity"]),
        calculated_at=datetime.fromisoformat(str(manifest["calculated_at"])),
        content_sha256=str(manifest["content_sha256"]),
        rows=tuple(dict(row) for row in rows),
        environment=environment,
    )


def load_latest_intraday_profit_batch(root: str | Path, session: MarketSession | str) -> ResolvedIntradayProfitRelease:
    resolved = MarketSession(session)
    index = _read(Path(root).resolve() / INDEX_FILENAME)
    try:
        release_id = index["latest"][resolved.value]
        day = date.fromisoformat(release_id[:10])
    except (KeyError, TypeError, ValueError):
        raise IntradayProfitStoreNotFoundError(f"latest {resolved.value} profit is unavailable") from None
    return load_intraday_profit_batch(root, day, resolved)


def _result_row(result: SoybeanIntradayProfitResult, *, include_calculated_at: bool) -> dict[str, object]:
    key = result.business_key
    calculation = result.calculation
    params = result.market_inputs
    row: dict[str, object] = {
        "business_date": key.business_date.isoformat(),
        "session": result.session.value,
        "commodity": key.commodity,
        "origin": key.origin,
        "shipment_year": key.shipment_year,
        "shipment_month": key.shipment_month,
        "shipment_period": key.shipment_period,
        "cnf_identity": result.cnf_identity,
        "cnf_cents_per_bushel": result.cnf_cents_per_bushel,
        "cnf_source": result.cnf_source,
        "market_snapshot_release_id": result.market_snapshot_release_id,
        "market_snapshot_sha256": result.market_snapshot_sha256,
        "market_captured_at": result.market_captured_at.isoformat(),
        "cbot_contract": params.cbot_contract_code,
        "cbot_price_cents_per_bushel": None if params.cbot is None else params.cbot.price,
        "fx_instrument_id": params.fx.instrument_id,
        "fx_value": params.fx.price,
        "soymeal_contract": params.soymeal_contract_code,
        "soymeal_price_cny_per_tonne": None if params.soymeal is None else params.soymeal.price,
        "soyoil_contract": params.soyoil_contract_code,
        "soyoil_price_cny_per_tonne": None if params.soyoil is None else params.soyoil.price,
        "usd_cost_per_tonne": calculation.usd_cost_per_tonne,
        "duty_paid_cost_cny_per_tonne": calculation.duty_paid_cost_cny_per_tonne,
        "net_crush_margin_cny_per_tonne": calculation.net_crush_margin_cny_per_tonne,
        "calculation_status": calculation.calculation_status.value,
        "availability_status": result.availability_status,
        "unavailable_contracts": list(params.unavailable_contracts),
        "missing_reasons": [reason.value for reason in calculation.missing_reasons],
        "parameter_version": calculation.parameter_version,
        "parameter_hash": calculation.parameter_hash,
        "mapping_identity": calculation.mapping_identity,
        "mapping_hash": calculation.mapping_hash,
        "contract_override_hash": calculation.contract_override_hash,
        "provenance": dict(params.provenance),
    }
    if include_calculated_at:
        row["calculated_at"] = result.calculated_at.isoformat()
    return row


def read_intraday_profit_rows(
    root: str | Path,
    business_date: date,
    session: MarketSession | str,
) -> tuple[dict[str, object], ...]:
    return load_intraday_profit_batch(root, business_date, session).rows


def _require_other_session_cnf(root: Path, batch: SoybeanIntradayResultBatch) -> None:
    other = MarketSession.PM if batch.session is MarketSession.AM else MarketSession.AM
    other_manifest = root / RELEASES_DIRNAME / f"{batch.business_date.isoformat()}-{other.value}" / MANIFEST_FILENAME
    if not other_manifest.exists():
        return
    payload = _read(other_manifest)
    if not isinstance(payload, dict) or payload.get("cnf_identity") != batch.cnf_identity:
        raise SoybeanIntradayCnfMismatchError("AM and PM CNF identities differ")


def _update_index(root: Path, batch: SoybeanIntradayResultBatch, manifest: dict[str, object]) -> None:
    path = root / INDEX_FILENAME
    payload = _read(path) if path.exists() else {
        "schema_version": SCHEMA_VERSION, "generation": 0, "latest": {}, "by_date": {}
    }
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise IntradayProfitStoreValidationError("intraday profit index is invalid")
    latest = dict(payload.get("latest", {}))
    by_date = {key: dict(value) for key, value in dict(payload.get("by_date", {})).items()}
    latest[batch.session.value] = batch.release_id
    by_date.setdefault(batch.business_date.isoformat(), {})[batch.session.value] = {
        "release_id": batch.release_id,
        "content_sha256": batch.content_sha256,
        "cnf_identity": batch.cnf_identity,
        "market_snapshot_sha256": batch.market_snapshot_sha256,
    }
    updated = {
        "schema_version": SCHEMA_VERSION,
        "generation": int(payload.get("generation", 0)) + 1,
        "latest": latest,
        "by_date": by_date,
    }
    temporary = root / f".{INDEX_FILENAME}.{uuid.uuid4().hex}.tmp"
    _write_exclusive(temporary, updated)
    os.replace(temporary, path)


def _write_exclusive(path: Path, payload: object) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise IntradayProfitStoreConflictError("intraday profit file already exists") from None


def _read(path: Path) -> object:
    if not path.is_file():
        raise IntradayProfitStoreNotFoundError(f"intraday profit file is unavailable: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IntradayProfitStoreValidationError(
            f"intraday profit file cannot be validated: {type(exc).__name__}"
        ) from None


def _sha(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "IntradayProfitSealResult", "IntradayProfitSealStatus",
    "IntradayProfitStoreConflictError", "IntradayProfitStoreError",
    "IntradayProfitStoreNotFoundError", "IntradayProfitStoreValidationError",
    "ResolvedIntradayProfitRelease", "SoybeanIntradayResultBatch",
    "list_intraday_profit_batches",
    "load_intraday_profit_batch", "load_latest_intraday_profit_batch",
    "read_intraday_profit_rows", "seal_intraday_profit_batch",
]
