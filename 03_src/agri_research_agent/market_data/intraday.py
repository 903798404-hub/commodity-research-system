"""Immutable AM/PM Public Intraday Market Snapshot contracts and storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from types import MappingProxyType
from typing import Iterable, Mapping
import uuid

from filelock import FileLock, Timeout


SCHEMA_VERSION = "1"
INDEX_FILENAME = "intraday_index.json"
RELEASES_DIRNAME = "releases"
QUOTES_FILENAME = "quotes.json"
MANIFEST_FILENAME = "manifest.json"
SAFE_INSTRUMENT = re.compile(r"^[A-Z0-9:/._-]+$")
FUTURES_CONTRACT = re.compile(r"^(?:[MY])?[0-9]{4}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class IntradaySnapshotError(RuntimeError):
    status = "ERROR"


class IntradaySnapshotValidationError(IntradaySnapshotError):
    status = "NOT_READY"


class IntradaySnapshotConflictError(IntradaySnapshotError):
    status = "IMMUTABLE_CONFLICT"


class IntradaySnapshotNotFoundError(IntradaySnapshotError):
    status = "NOT_FOUND"


class IntradaySnapshotLockedError(IntradaySnapshotError):
    status = "LOCKED"


class MarketSession(StrEnum):
    AM = "AM"
    PM = "PM"


class FreshnessStatus(StrEnum):
    FRESH = "FRESH"


class InstrumentAvailabilityStatus(StrEnum):
    CONTRACT_NOT_AVAILABLE = "CONTRACT_NOT_AVAILABLE"


class SealStatus(StrEnum):
    SEALED = "SEALED"
    NO_CHANGE = "NO_CHANGE"


def require_aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise IntradaySnapshotValidationError(f"{field_name} must be timezone-aware")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntradaySnapshotValidationError(f"{field_name} must be non-empty")
    return value.strip()


def _positive(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IntradaySnapshotValidationError(f"{field_name} must be numeric")
    result = float(value)
    if not result > 0 or result == float("inf"):
        raise IntradaySnapshotValidationError(f"{field_name} must be positive and finite")
    return result


@dataclass(frozen=True, slots=True)
class IntradayQuote:
    business_date: date
    session: MarketSession
    captured_at: datetime
    instrument_id: str
    contract_code: str
    exchange: str
    product: str
    price: float
    quote_type: str
    currency: str
    unit: str
    source_system: str
    source_table: str
    source_updated_at: datetime
    source_trade_date: date | None
    freshness_status: FreshnessStatus
    provenance: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.business_date) is not date:
            raise IntradaySnapshotValidationError("business_date must be an exact date")
        if not isinstance(self.session, MarketSession):
            raise IntradaySnapshotValidationError("session must be AM or PM")
        object.__setattr__(self, "captured_at", require_aware(self.captured_at, "captured_at"))
        instrument = _text(self.instrument_id, "instrument_id")
        if SAFE_INSTRUMENT.fullmatch(instrument) is None:
            raise IntradaySnapshotValidationError("instrument_id is not canonical")
        object.__setattr__(self, "instrument_id", instrument)
        contract = self.contract_code.strip() if isinstance(self.contract_code, str) else ""
        if contract and FUTURES_CONTRACT.fullmatch(contract) is None:
            raise IntradaySnapshotValidationError("contract_code is not an exact YYMM futures code")
        if instrument == "FX:USD/CNH:SPOT" and contract:
            raise IntradaySnapshotValidationError("FX Spot contract_code must be empty")
        if instrument != "FX:USD/CNH:SPOT" and not contract:
            raise IntradaySnapshotValidationError("futures contract_code must be explicit")
        object.__setattr__(self, "contract_code", contract)
        for field_name in (
            "exchange", "product", "quote_type", "currency", "unit",
            "source_system", "source_table",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        object.__setattr__(self, "price", _positive(self.price, "price"))
        object.__setattr__(
            self,
            "source_updated_at",
            require_aware(self.source_updated_at, "source_updated_at"),
        )
        if self.source_updated_at > self.captured_at:
            raise IntradaySnapshotValidationError("source_updated_at cannot follow captured_at")
        if self.source_trade_date is not None and type(self.source_trade_date) is not date:
            raise IntradaySnapshotValidationError("source_trade_date must be a date or None")
        if self.freshness_status is not FreshnessStatus.FRESH:
            raise IntradaySnapshotValidationError("sealed quotes must be FRESH")
        if not isinstance(self.provenance, Mapping) or not self.provenance:
            raise IntradaySnapshotValidationError("provenance must be complete")
        canonical_provenance = json.loads(
            json.dumps(dict(self.provenance), ensure_ascii=False, sort_keys=True, default=_json_default)
        )
        object.__setattr__(self, "provenance", MappingProxyType(canonical_provenance))

    @property
    def key(self) -> tuple[str, str]:
        return self.instrument_id, self.contract_code

    def as_dict(self) -> dict[str, object]:
        return {
            "business_date": self.business_date.isoformat(),
            "session": self.session.value,
            "captured_at": self.captured_at.isoformat(),
            "instrument_id": self.instrument_id,
            "contract_code": self.contract_code,
            "exchange": self.exchange,
            "product": self.product,
            "price": self.price,
            "quote_type": self.quote_type,
            "currency": self.currency,
            "unit": self.unit,
            "source_system": self.source_system,
            "source_table": self.source_table,
            "source_updated_at": self.source_updated_at.isoformat(),
            "source_trade_date": (
                None if self.source_trade_date is None else self.source_trade_date.isoformat()
            ),
            "freshness_status": self.freshness_status.value,
            "provenance": dict(self.provenance),
        }

    def content_dict(self) -> dict[str, object]:
        payload = self.as_dict()
        payload.pop("captured_at")
        return payload


@dataclass(frozen=True, slots=True)
class IntradayUnavailableInstrument:
    instrument_id: str
    contract_code: str
    exchange: str
    product: str
    status: InstrumentAvailabilityStatus
    reason: str
    provenance: Mapping[str, object]

    def __post_init__(self) -> None:
        instrument = _text(self.instrument_id, "instrument_id")
        if not SAFE_INSTRUMENT.fullmatch(instrument):
            raise IntradaySnapshotValidationError("instrument_id contains unsafe characters")
        object.__setattr__(self, "instrument_id", instrument)
        if not isinstance(self.contract_code, str) or (
            self.contract_code and not FUTURES_CONTRACT.fullmatch(self.contract_code)
        ):
            raise IntradaySnapshotValidationError("contract_code must be an exact YYMM code")
        for field_name in ("exchange", "product", "reason"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        if self.status is not InstrumentAvailabilityStatus.CONTRACT_NOT_AVAILABLE:
            raise IntradaySnapshotValidationError("unsupported unavailable instrument status")
        if not isinstance(self.provenance, Mapping) or not self.provenance:
            raise IntradaySnapshotValidationError("unavailable instrument provenance must be complete")
        canonical = json.loads(
            json.dumps(dict(self.provenance), ensure_ascii=False, sort_keys=True, default=_json_default)
        )
        object.__setattr__(self, "provenance", MappingProxyType(canonical))

    @property
    def key(self) -> tuple[str, str]:
        return self.instrument_id, self.contract_code

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "contract_code": self.contract_code,
            "exchange": self.exchange,
            "product": self.product,
            "status": self.status.value,
            "reason": self.reason,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class IntradaySnapshot:
    business_date: date
    session: MarketSession
    captured_at: datetime
    quotes: tuple[IntradayQuote, ...]
    calendar_policy: str
    calendar_source_identity: str
    environment: str = "FORMAL"
    unavailable_instruments: tuple[IntradayUnavailableInstrument, ...] = ()

    def __post_init__(self) -> None:
        if type(self.business_date) is not date:
            raise IntradaySnapshotValidationError("business_date must be an exact date")
        if not isinstance(self.session, MarketSession):
            raise IntradaySnapshotValidationError("session must be AM or PM")
        object.__setattr__(self, "captured_at", require_aware(self.captured_at, "captured_at"))
        if not isinstance(self.quotes, tuple) or not self.quotes:
            raise IntradaySnapshotValidationError("quotes must be a non-empty immutable tuple")
        keys: set[tuple[str, str]] = set()
        for quote in self.quotes:
            if not isinstance(quote, IntradayQuote):
                raise IntradaySnapshotValidationError("quotes must contain IntradayQuote values")
            if (
                quote.business_date != self.business_date
                or quote.session is not self.session
                or quote.captured_at != self.captured_at
            ):
                raise IntradaySnapshotValidationError("quote snapshot identity mismatch")
            if quote.key in keys:
                raise IntradaySnapshotValidationError("snapshot contains a duplicate instrument key")
            keys.add(quote.key)
        if not isinstance(self.unavailable_instruments, tuple):
            raise IntradaySnapshotValidationError("unavailable instruments must be an immutable tuple")
        for unavailable in self.unavailable_instruments:
            if not isinstance(unavailable, IntradayUnavailableInstrument):
                raise IntradaySnapshotValidationError("unavailable instrument evidence is invalid")
            if unavailable.key in keys:
                raise IntradaySnapshotValidationError("instrument cannot be both available and unavailable")
            keys.add(unavailable.key)
        _text(self.calendar_policy, "calendar_policy")
        _text(self.calendar_source_identity, "calendar_source_identity")
        if self.environment not in {"FORMAL", "TEST_ISOLATED_NON_PRODUCTION"}:
            raise IntradaySnapshotValidationError("unsupported snapshot environment")

    @property
    def release_id(self) -> str:
        return f"{self.business_date.isoformat()}-{self.session.value}"

    @property
    def content_sha256(self) -> str:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "business_date": self.business_date.isoformat(),
            "session": self.session.value,
            "quotes": [quote.content_dict() for quote in sorted(self.quotes, key=lambda q: q.key)],
            "unavailable_instruments": [
                item.as_dict() for item in sorted(self.unavailable_instruments, key=lambda x: x.key)
            ],
            "calendar_policy": self.calendar_policy,
            "calendar_source_identity": self.calendar_source_identity,
            "environment": self.environment,
        }
        return _sha_payload(payload)


@dataclass(frozen=True, slots=True)
class IntradaySealResult:
    status: SealStatus
    release_id: str
    content_sha256: str
    release_dir: Path


def seal_intraday_snapshot(root: str | Path, snapshot: IntradaySnapshot) -> IntradaySealResult:
    """Seal one date/session exactly once and update session-aware indexes."""

    if not isinstance(snapshot, IntradaySnapshot):
        raise IntradaySnapshotValidationError("snapshot must be IntradaySnapshot")
    store_root = Path(root).resolve()
    store_root.mkdir(parents=True, exist_ok=True)
    releases = store_root / RELEASES_DIRNAME
    releases.mkdir(exist_ok=True)
    final = releases / snapshot.release_id
    lock = FileLock(str(store_root / ".intraday.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise IntradaySnapshotLockedError("intraday store is locked") from exc
    try:
        if final.exists():
            existing = load_intraday_snapshot(store_root, snapshot.business_date, snapshot.session)
            if existing.content_sha256 != snapshot.content_sha256:
                raise IntradaySnapshotConflictError(
                    "sealed date/session exists with different source content"
                )
            return IntradaySealResult(
                SealStatus.NO_CHANGE,
                snapshot.release_id,
                snapshot.content_sha256,
                final,
            )
        building = releases / f".building-{snapshot.release_id}-{uuid.uuid4().hex}"
        building.mkdir()
        try:
            quotes_payload = [quote.as_dict() for quote in sorted(snapshot.quotes, key=lambda q: q.key)]
            _write_json_exclusive(building / QUOTES_FILENAME, quotes_payload)
            manifest = _manifest(snapshot, quotes_payload)
            _write_json_exclusive(building / MANIFEST_FILENAME, manifest)
            _fsync_files(building)
            os.replace(building, final)
        except BaseException:
            if building.exists():
                shutil.rmtree(building)
            raise
        _update_index(store_root, snapshot, manifest)
        return IntradaySealResult(
            SealStatus.SEALED,
            snapshot.release_id,
            snapshot.content_sha256,
            final,
        )
    finally:
        lock.release()


def load_intraday_snapshot(
    root: str | Path,
    business_date: date,
    session: MarketSession | str,
) -> IntradaySnapshot:
    if type(business_date) is not date:
        raise IntradaySnapshotValidationError("business_date must be an exact date")
    resolved_session = MarketSession(session)
    release_id = f"{business_date.isoformat()}-{resolved_session.value}"
    release = Path(root).resolve() / RELEASES_DIRNAME / release_id
    manifest = _read_json(release / MANIFEST_FILENAME)
    rows = _read_json(release / QUOTES_FILENAME)
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        raise IntradaySnapshotValidationError("intraday release files are invalid")
    quotes = tuple(_quote_from_dict(row) for row in rows)
    snapshot = IntradaySnapshot(
        business_date=business_date,
        session=resolved_session,
        captured_at=datetime.fromisoformat(str(manifest["captured_at"])),
        quotes=quotes,
        calendar_policy=str(manifest["calendar_policy"]),
        calendar_source_identity=str(manifest["calendar_source_identity"]),
        environment=str(manifest["environment"]),
        unavailable_instruments=tuple(
            _unavailable_from_dict(row)
            for row in manifest.get("requested_but_unavailable", [])
        ),
    )
    expected_available = [
        {"instrument_id": quote.instrument_id, "contract_code": quote.contract_code}
        for quote in sorted(snapshot.quotes, key=lambda q: q.key)
    ]
    if manifest.get("available_instruments") != expected_available:
        raise IntradaySnapshotValidationError("available instrument manifest is inconsistent")
    if manifest.get("content_sha256") != snapshot.content_sha256:
        raise IntradaySnapshotValidationError("intraday content identity mismatch")
    if manifest.get("quotes_sha256") != _sha_payload(rows):
        raise IntradaySnapshotValidationError("intraday quote-file identity mismatch")
    return snapshot


def load_latest_intraday_snapshot(root: str | Path, session: MarketSession | str) -> IntradaySnapshot:
    resolved = MarketSession(session)
    payload = _read_json(Path(root).resolve() / INDEX_FILENAME)
    try:
        release_id = payload["latest"][resolved.value]
        business_date = date.fromisoformat(release_id[:10])
    except (KeyError, TypeError, ValueError):
        raise IntradaySnapshotNotFoundError(f"latest {resolved.value} snapshot is unavailable") from None
    return load_intraday_snapshot(root, business_date, resolved)


def quote_by_instrument(
    snapshot: IntradaySnapshot,
    instrument_id: str,
    contract_code: str = "",
) -> IntradayQuote:
    matches = [q for q in snapshot.quotes if q.key == (instrument_id, contract_code)]
    if len(matches) != 1:
        raise IntradaySnapshotNotFoundError(
            f"snapshot quote is unavailable: {instrument_id}/{contract_code}"
        )
    return matches[0]


def _manifest(snapshot: IntradaySnapshot, rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "release_id": snapshot.release_id,
        "business_date": snapshot.business_date.isoformat(),
        "session": snapshot.session.value,
        "captured_at": snapshot.captured_at.isoformat(),
        "environment": snapshot.environment,
        "calendar_policy": snapshot.calendar_policy,
        "calendar_source_identity": snapshot.calendar_source_identity,
        "record_count": len(rows),
        "available_instruments": [
            {"instrument_id": quote.instrument_id, "contract_code": quote.contract_code}
            for quote in sorted(snapshot.quotes, key=lambda q: q.key)
        ],
        "requested_but_unavailable": [
            item.as_dict() for item in sorted(snapshot.unavailable_instruments, key=lambda x: x.key)
        ],
        "content_sha256": snapshot.content_sha256,
        "quotes_sha256": _sha_payload(rows),
        "files": [QUOTES_FILENAME],
        "immutable": True,
    }


def _update_index(root: Path, snapshot: IntradaySnapshot, manifest: Mapping[str, object]) -> None:
    path = root / INDEX_FILENAME
    if path.exists():
        payload = _read_json(path)
    else:
        payload = {"schema_version": SCHEMA_VERSION, "generation": 0, "latest": {}, "by_date": {}}
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise IntradaySnapshotValidationError("intraday index is invalid")
    latest = dict(payload.get("latest", {}))
    by_date = {key: dict(value) for key, value in dict(payload.get("by_date", {})).items()}
    latest[snapshot.session.value] = snapshot.release_id
    sessions = by_date.setdefault(snapshot.business_date.isoformat(), {})
    sessions[snapshot.session.value] = {
        "release_id": snapshot.release_id,
        "content_sha256": manifest["content_sha256"],
        "captured_at": manifest["captured_at"],
    }
    final = {
        "schema_version": SCHEMA_VERSION,
        "generation": int(payload.get("generation", 0)) + 1,
        "latest": latest,
        "by_date": by_date,
    }
    temporary = root / f".{INDEX_FILENAME}.{uuid.uuid4().hex}.tmp"
    _write_json_exclusive(temporary, final)
    os.replace(temporary, path)


def _quote_from_dict(row: object) -> IntradayQuote:
    if not isinstance(row, dict):
        raise IntradaySnapshotValidationError("intraday quote row is invalid")
    return IntradayQuote(
        business_date=date.fromisoformat(str(row["business_date"])),
        session=MarketSession(str(row["session"])),
        captured_at=datetime.fromisoformat(str(row["captured_at"])),
        instrument_id=str(row["instrument_id"]),
        contract_code=str(row["contract_code"]),
        exchange=str(row["exchange"]),
        product=str(row["product"]),
        price=float(row["price"]),
        quote_type=str(row["quote_type"]),
        currency=str(row["currency"]),
        unit=str(row["unit"]),
        source_system=str(row["source_system"]),
        source_table=str(row["source_table"]),
        source_updated_at=datetime.fromisoformat(str(row["source_updated_at"])),
        source_trade_date=(
            None if row["source_trade_date"] is None else date.fromisoformat(str(row["source_trade_date"]))
        ),
        freshness_status=FreshnessStatus(str(row["freshness_status"])),
        provenance=dict(row["provenance"]),
    )


def _unavailable_from_dict(row: object) -> IntradayUnavailableInstrument:
    if not isinstance(row, dict):
        raise IntradaySnapshotValidationError("unavailable instrument row is invalid")
    return IntradayUnavailableInstrument(
        instrument_id=str(row["instrument_id"]),
        contract_code=str(row["contract_code"]),
        exchange=str(row["exchange"]),
        product=str(row["product"]),
        status=InstrumentAvailabilityStatus(str(row["status"])),
        reason=str(row["reason"]),
        provenance=dict(row["provenance"]),
    )


def _write_json_exclusive(path: Path, payload: object) -> None:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise IntradaySnapshotConflictError("intraday file already exists") from None


def _read_json(path: Path) -> object:
    if not path.is_file():
        raise IntradaySnapshotNotFoundError(f"intraday file is unavailable: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IntradaySnapshotValidationError(
            f"intraday file cannot be validated: {type(exc).__name__}"
        ) from None


def _fsync_files(root: Path) -> None:
    for path in root.iterdir():
        if path.is_file():
            with path.open("r+b") as handle:
                os.fsync(handle.fileno())


def _sha_payload(payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


__all__ = [
    "FreshnessStatus", "InstrumentAvailabilityStatus", "IntradayQuote",
    "IntradaySealResult", "IntradaySnapshot", "IntradayUnavailableInstrument",
    "IntradaySnapshotConflictError", "IntradaySnapshotError",
    "IntradaySnapshotNotFoundError", "IntradaySnapshotValidationError",
    "MarketSession", "SealStatus", "load_intraday_snapshot",
    "load_latest_intraday_snapshot", "quote_by_instrument", "seal_intraday_snapshot",
]
