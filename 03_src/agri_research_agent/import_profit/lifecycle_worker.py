"""Durable delivery and task-scoped execution for Soybean market capture.

The module is deliberately independent from the FULL DAILY process.  A small
observer consumes its already-durable completion status and writes a Soybean
event.  FULL DAILY never imports, waits for, or rolls back because of this
consumer.

Machine authority is also deliberately external: the worker asks an injected
ephemeral-task issuer for a fresh identity only after immutable-snapshot,
deadline, and readiness gates pass.  The production implementation of that
issuer is the existing root-controlled OCI signer/task launcher; this module
does not mint grants or reuse the dashboard grant.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import re
from typing import Callable, Mapping, Protocol

from filelock import FileLock, Timeout

from agri_research_agent.market_data.intraday import (
    IntradaySnapshotNotFoundError,
    MarketSession,
    load_intraday_snapshot,
    require_aware,
)
from agri_research_agent.shared.atomic_storage import atomic_write_json

from .lifecycle import (
    EventType,
    LifecycleEvent,
    MarketComponentReadiness,
    MarketComponentStatus,
    MarketReadinessReport,
    MarketState,
    SoybeanLifecycleState,
)
from .lifecycle_events import FULL_DAILY_EVENT_STATUSES, full_daily_market_event
from .lifecycle_reconciler import BEIJING, SESSION_WINDOWS, SoybeanLifecycleReconciler
from .lifecycle_store import SoybeanLifecycleStore


EVENT_STORE_SCHEMA = "soybean-durable-event/1"
DELIVERY_EVIDENCE_SCHEMA = "soybean-event-delivery/1"
MACHINE_IDENTITY_SCHEMA = "soybean-machine-capture-identity/1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")

# These paths are the complete task capability contract.  In particular they
# exclude manual CNF, profit results, Public Current, and FULL DAILY writes.
MACHINE_CAPTURE_READ_PATHS = (
    "/runtime/import-profit/capture-requests",
    "/runtime/import-profit/calendars",
    "/runtime/import-profit/snapshots",
)
MACHINE_CAPTURE_WRITE_PATHS = (
    "/runtime/capture-snapshots",
    "/runtime/import-profit/operational/lifecycle",
    "/runtime/import-profit/operational/soybean-events",
)
MACHINE_CAPTURE_SECRET_ACCESS = ("/run/secrets/tankan.env",)
MACHINE_IDENTITY_SOURCE = "host-authorization.issue_execution_grant/ephemeral-oci-task"


class DurableEventError(RuntimeError):
    pass


class DurableEventConflictError(DurableEventError):
    pass


class MachineIdentityError(RuntimeError):
    pass


class EventDeliveryStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def _utc(value: datetime, label: str) -> datetime:
    return require_aware(value, label).astimezone(timezone.utc)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DurableEventError("duplicate durable event JSON key")
        result[key] = value
    return result


def _read_json(path: Path) -> object:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                DurableEventError(f"nonfinite durable event value: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DurableEventError("durable event JSON is invalid") from exc


@dataclass(frozen=True, slots=True)
class DurableSoybeanEvent:
    event_identity: str
    business_idempotency_key: str
    lifecycle_event: LifecycleEvent
    readiness: MarketReadinessReport
    status: EventDeliveryStatus
    created_at: datetime
    revision: int = 1
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    completed_at: datetime | None = None
    claim_owner: str | None = None
    attempt_count: int = 0
    last_error: str | None = None
    outcome: str | None = None
    schema_version: str = EVENT_STORE_SCHEMA

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.event_identity):
            raise DurableEventError("event identity must be SHA-256")
        if self.business_idempotency_key != self.lifecycle_event.idempotency_key:
            raise DurableEventError("business idempotency key mismatch")
        if self.readiness.content_identity != self.lifecycle_event.payload_identity:
            raise DurableEventError("event/readiness payload identity mismatch")
        if (self.readiness.business_date, self.readiness.session) != (
            self.lifecycle_event.business_date, self.lifecycle_event.session,
        ):
            raise DurableEventError("event/readiness business identity mismatch")
        _utc(self.created_at, "created_at")
        for label, value in (
            ("claimed_at", self.claimed_at),
            ("lease_expires_at", self.lease_expires_at),
            ("completed_at", self.completed_at),
        ):
            if value is not None:
                _utc(value, label)
        if self.revision < 1 or self.attempt_count < 0:
            raise DurableEventError("event revision/attempt count is invalid")
        if self.claim_owner is not None and not _SAFE_OWNER.fullmatch(self.claim_owner):
            raise DurableEventError("invalid event claim owner")

    @property
    def business_date(self) -> date:
        return self.lifecycle_event.business_date

    @property
    def session(self) -> MarketSession:
        return self.lifecycle_event.session

    @property
    def event_type(self) -> EventType:
        return self.lifecycle_event.event_type

    @property
    def upstream_identity(self) -> str:
        return self.lifecycle_event.upstream_identity

    @property
    def payload_identity(self) -> str:
        return self.lifecycle_event.payload_identity

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_identity": self.event_identity,
            "business_idempotency_key": self.business_idempotency_key,
            "business_date": self.business_date.isoformat(),
            "session": self.session.value,
            "event_type": self.event_type.value,
            "upstream_identity": self.upstream_identity,
            "payload_identity": self.payload_identity,
            "created_at": _utc(self.created_at, "created_at").isoformat(),
            "claimed_at": None if self.claimed_at is None else _utc(self.claimed_at, "claimed_at").isoformat(),
            "lease_expires_at": None if self.lease_expires_at is None else _utc(self.lease_expires_at, "lease_expires_at").isoformat(),
            "completed_at": None if self.completed_at is None else _utc(self.completed_at, "completed_at").isoformat(),
            "status": self.status.value,
            "claim_owner": self.claim_owner,
            "attempt_count": self.attempt_count,
            "last_error": self.last_error,
            "outcome": self.outcome,
            "revision": self.revision,
            "lifecycle_event": self.lifecycle_event.as_dict(),
            "readiness": self.readiness.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class EventDeliveryReceipt:
    status: str
    event_identity: str | None
    business_idempotency_key: str | None
    evidence_path: Path
    error: str | None = None


class SoybeanDurableEventStore:
    """Atomic file outbox with a recoverable process-safe claim lease."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.events = self.root / "events"
        self.delivery = self.root / "delivery-evidence"

    def event_path(self, business_idempotency_key: str) -> Path:
        if not _SHA256.fullmatch(business_idempotency_key):
            raise DurableEventError("business idempotency key must be SHA-256")
        return self.events / f"{business_idempotency_key}.json"

    def enqueue(
        self,
        lifecycle_event: LifecycleEvent,
        readiness: MarketReadinessReport,
        *,
        created_at: datetime,
    ) -> tuple[DurableSoybeanEvent, bool]:
        created = _utc(created_at, "created_at")
        identity = _sha({
            "lifecycle_event": lifecycle_event.as_dict(),
            "readiness": readiness.as_dict(),
        })
        candidate = DurableSoybeanEvent(
            identity,
            lifecycle_event.idempotency_key,
            lifecycle_event,
            readiness,
            EventDeliveryStatus.PENDING,
            created,
        )
        self.events.mkdir(parents=True, exist_ok=True)
        with self._locked():
            path = self.event_path(candidate.business_idempotency_key)
            if path.is_file():
                current = self._load_path(path)
                if current.event_identity != candidate.event_identity:
                    raise DurableEventConflictError(
                        "business idempotency key has different durable content"
                    )
                return current, False
            self._write(candidate)
            return self._load_path(path), True

    def load(self, business_idempotency_key: str) -> DurableSoybeanEvent:
        path = self.event_path(business_idempotency_key)
        if not path.is_file():
            raise DurableEventError("durable event does not exist")
        before = path.stat()
        event = self._load_path(path)
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
        ):
            raise DurableEventError("durable event changed during read")
        return event

    def claim_next(
        self,
        *,
        owner: str,
        now: datetime,
        lease_seconds: int = 120,
    ) -> DurableSoybeanEvent | None:
        if not _SAFE_OWNER.fullmatch(owner):
            raise DurableEventError("invalid claim owner")
        claimed = _utc(now, "now")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 900:
            raise DurableEventError("claim lease must be 1..900 seconds")
        self.events.mkdir(parents=True, exist_ok=True)
        with self._locked():
            for path in sorted(self.events.glob("*.json")):
                current = self._load_path(path)
                expired = (
                    current.status in {EventDeliveryStatus.CLAIMED, EventDeliveryStatus.PROCESSING}
                    and current.lease_expires_at is not None
                    and current.lease_expires_at <= claimed
                )
                if current.status is not EventDeliveryStatus.PENDING and not expired:
                    continue
                updated = replace(
                    current,
                    status=EventDeliveryStatus.CLAIMED,
                    claimed_at=claimed,
                    lease_expires_at=claimed + timedelta(seconds=lease_seconds),
                    completed_at=None,
                    claim_owner=owner,
                    attempt_count=current.attempt_count + 1,
                    last_error=(
                        "RECOVERED_EXPIRED_LEASE"
                        if expired else current.last_error
                    ),
                    outcome=None,
                    revision=current.revision + 1,
                )
                self._write(updated)
                return self._load_path(path)
        return None

    def mark_processing(
        self,
        event: DurableSoybeanEvent,
        *,
        owner: str,
        now: datetime,
    ) -> DurableSoybeanEvent:
        return self._transition(
            event, owner=owner, now=now,
            expected_status=EventDeliveryStatus.CLAIMED,
            next_status=EventDeliveryStatus.PROCESSING,
        )

    def complete(
        self,
        event: DurableSoybeanEvent,
        *,
        owner: str,
        now: datetime,
        outcome: str,
    ) -> DurableSoybeanEvent:
        return self._transition(
            event, owner=owner, now=now,
            expected_status=EventDeliveryStatus.PROCESSING,
            next_status=EventDeliveryStatus.COMPLETED,
            outcome=outcome,
        )

    def fail(
        self,
        event: DurableSoybeanEvent,
        *,
        owner: str,
        now: datetime,
        error: str,
    ) -> DurableSoybeanEvent:
        return self._transition(
            event, owner=owner, now=now,
            expected_status=EventDeliveryStatus.PROCESSING,
            next_status=EventDeliveryStatus.FAILED,
            error=error,
        )

    def record_delivery_failure(
        self,
        *,
        full_daily_identity: str,
        business_date: date,
        session: MarketSession,
        observed_at: datetime,
        error: str,
    ) -> Path:
        self.delivery.mkdir(parents=True, exist_ok=True)
        key = _sha({
            "full_daily_identity": full_daily_identity,
            "business_date": business_date.isoformat(),
            "session": session.value,
        })
        path = self.delivery / f"{key}.json"
        payload = {
            "schema_version": DELIVERY_EVIDENCE_SCHEMA,
            "status": "FAILED",
            "full_daily_identity": full_daily_identity,
            "business_date": business_date.isoformat(),
            "session": session.value,
            "observed_at": _utc(observed_at, "observed_at").isoformat(),
            "last_error": error,
        }
        with self._locked():
            atomic_write_json(path, payload, file_mode=0o600)
        return path

    def _transition(
        self,
        event: DurableSoybeanEvent,
        *,
        owner: str,
        now: datetime,
        expected_status: EventDeliveryStatus,
        next_status: EventDeliveryStatus,
        outcome: str | None = None,
        error: str | None = None,
    ) -> DurableSoybeanEvent:
        at = _utc(now, "now")
        with self._locked():
            current = self._load_path(self.event_path(event.business_idempotency_key))
            if current.revision != event.revision:
                raise DurableEventConflictError("durable event revision changed")
            if current.status is not expected_status or current.claim_owner != owner:
                raise DurableEventConflictError("durable event claim/status changed")
            if current.lease_expires_at is None or current.lease_expires_at <= at:
                raise DurableEventConflictError("durable event claim lease expired")
            completed = at if next_status in {
                EventDeliveryStatus.COMPLETED, EventDeliveryStatus.FAILED,
            } else None
            updated = replace(
                current,
                status=next_status,
                completed_at=completed,
                last_error=error,
                outcome=outcome,
                revision=current.revision + 1,
            )
            self._write(updated)
            return self._load_path(self.event_path(event.business_idempotency_key))

    def _load_path(self, path: Path) -> DurableSoybeanEvent:
        return _event_from_dict(_read_json(path))

    def _write(self, event: DurableSoybeanEvent) -> None:
        path = self.event_path(event.business_idempotency_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, event.as_dict(), file_mode=0o600)

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self.root / ".event-store.lock"))
        try:
            lock.acquire(timeout=30)
        except Timeout as exc:
            raise DurableEventConflictError("durable event store is locked") from exc
        try:
            yield
        finally:
            lock.release()


ReadinessProvider = Callable[
    [date, MarketSession, datetime, str], MarketReadinessReport
]


class FullDailyCompletionObserver:
    """One-way consumer of completed FULL DAILY status; producer bytes stay untouched."""

    def __init__(self, *, store: SoybeanDurableEventStore, readiness_provider: ReadinessProvider):
        self.store = store
        self.readiness_provider = readiness_provider

    def observe(
        self,
        status_path: str | Path,
        *,
        business_date: date,
        session: MarketSession,
        observed_at: datetime,
    ) -> EventDeliveryReceipt:
        source = Path(status_path)
        before = source.read_bytes()
        status = _read_json(source)
        if not isinstance(status, dict):
            raise DurableEventError("FULL DAILY status must be an object")
        wrapper_status = str(status.get("status", ""))
        business_status = str(status.get("business_status", ""))
        run_id = str(status.get("run_id", ""))
        if wrapper_status not in FULL_DAILY_EVENT_STATUSES or business_status not in {
            "UPDATED", "NO_CHANGE", "PARTIAL_SUCCESS",
        }:
            evidence = self.store.record_delivery_failure(
                full_daily_identity=run_id or "unknown",
                business_date=business_date,
                session=session,
                observed_at=observed_at,
                error="FULL_DAILY_NOT_COMPLETED_SUCCESS_CLASS",
            )
            if source.read_bytes() != before:
                raise DurableEventError("FULL DAILY status changed during observation")
            return EventDeliveryReceipt("IGNORED", None, None, evidence)
        try:
            readiness = self.readiness_provider(
                business_date, MarketSession(session), observed_at, run_id
            )
            event = full_daily_market_event(
                full_daily_status=wrapper_status,
                full_daily_identity=run_id,
                readiness=readiness,
                occurred_at=observed_at,
                trace_id=f"full-daily:{run_id}",
            )
            durable, created = self.store.enqueue(
                event, readiness, created_at=observed_at
            )
            evidence = self.store.event_path(durable.business_idempotency_key)
            receipt = EventDeliveryReceipt(
                "QUEUED" if created else "DEDUPLICATED",
                durable.event_identity,
                durable.business_idempotency_key,
                evidence,
            )
        except Exception as exc:
            evidence = self.store.record_delivery_failure(
                full_daily_identity=run_id,
                business_date=business_date,
                session=MarketSession(session),
                observed_at=observed_at,
                error=f"{type(exc).__name__}: {exc}",
            )
            receipt = EventDeliveryReceipt(
                "DELIVERY_FAILED", None, None, evidence,
                f"{type(exc).__name__}: {exc}",
            )
        if source.read_bytes() != before:
            raise DurableEventError("FULL DAILY status changed during observation")
        return receipt


@dataclass(frozen=True, slots=True)
class MachineCaptureIdentity:
    identity_id: str
    task_id: str
    business_date: date
    session: MarketSession
    issued_at: datetime
    expires_at: datetime
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    secret_access: tuple[str, ...]
    source: str = MACHINE_IDENTITY_SOURCE
    schema_version: str = MACHINE_IDENTITY_SCHEMA

    def validate_for(self, event: DurableSoybeanEvent, *, now: datetime) -> None:
        at = _utc(now, "now")
        issued = _utc(self.issued_at, "issued_at")
        expires = _utc(self.expires_at, "expires_at")
        expected_task = machine_capture_task_id(event)
        if not self.identity_id.strip() or self.task_id != expected_task:
            raise MachineIdentityError("machine identity does not belong to this capture task")
        if (self.business_date, self.session) != (event.business_date, event.session):
            raise MachineIdentityError("machine identity business scope mismatch")
        if not issued <= at < expires or expires - issued > timedelta(seconds=900):
            raise MachineIdentityError("machine identity is expired or overlong")
        if self.source != MACHINE_IDENTITY_SOURCE:
            raise MachineIdentityError("machine identity source is not the host OCI signer")
        if self.read_paths != MACHINE_CAPTURE_READ_PATHS:
            raise MachineIdentityError("machine capture read scope is not minimal")
        if self.write_paths != MACHINE_CAPTURE_WRITE_PATHS:
            raise MachineIdentityError("machine capture write scope is not minimal")
        if self.secret_access != MACHINE_CAPTURE_SECRET_ACCESS:
            raise MachineIdentityError("machine capture secret scope is not minimal")


def machine_capture_task_id(event: DurableSoybeanEvent) -> str:
    return (
        f"soybean-capture:{event.business_date.isoformat()}:{event.session.value}:"
        f"{event.event_identity[:16]}"
    )


class MachineIdentityIssuer(Protocol):
    def __call__(
        self, event: DurableSoybeanEvent, *, now: datetime
    ) -> MachineCaptureIdentity: ...


class CaptureExecutor(Protocol):
    def __call__(
        self,
        event: LifecycleEvent,
        readiness: MarketReadinessReport,
        identity: MachineCaptureIdentity,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class CaptureWorkerReceipt:
    event_identity: str
    event_status: EventDeliveryStatus
    outcome: str
    lifecycle_state: SoybeanLifecycleState
    machine_identity_id: str | None


class SoybeanCaptureWorker:
    """Claim one durable event and converge the immutable snapshot lifecycle."""

    def __init__(
        self,
        *,
        event_store: SoybeanDurableEventStore,
        lifecycle_store: SoybeanLifecycleStore,
        snapshot_root: str | Path,
        identity_issuer: MachineIdentityIssuer,
        capture_executor: CaptureExecutor,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        lease_seconds: int = 120,
    ):
        self.event_store = event_store
        self.lifecycle_store = lifecycle_store
        self.snapshot_root = Path(snapshot_root)
        self.identity_issuer = identity_issuer
        self.capture_executor = capture_executor
        self.clock = clock
        self.lease_seconds = lease_seconds

    def run_once(self, *, owner: str) -> CaptureWorkerReceipt | None:
        now = _utc(self.clock(), "clock")
        claimed = self.event_store.claim_next(
            owner=owner, now=now, lease_seconds=self.lease_seconds
        )
        if claimed is None:
            return None
        processing = self.event_store.mark_processing(
            claimed, owner=owner, now=now
        )
        reconciler = SoybeanLifecycleReconciler(
            store=self.lifecycle_store,
            snapshot_root=self.snapshot_root,
        )
        identity: MachineCaptureIdentity | None = None
        try:
            current = self.lifecycle_store.load(
                processing.business_date, processing.session
            )
            if current.market_state is MarketState.SEALED or self._snapshot_exists(processing):
                state = self._converge_sealed(reconciler, processing, current, now)
                outcome = "NOOP_ALREADY_SEALED"
            else:
                window = self._window_status(processing, now)
                if window != "OPEN" or not processing.readiness.market_ready:
                    state = reconciler.reconcile(
                        processing.lifecycle_event,
                        now=now,
                        market_readiness=processing.readiness,
                    )
                    outcome = state.transitions[-1].action
                else:
                    identity = self.identity_issuer(processing, now=now)
                    identity.validate_for(processing, now=now)

                    def capture(event: LifecycleEvent, readiness: MarketReadinessReport):
                        identity.validate_for(processing, now=_utc(self.clock(), "clock"))
                        return self.capture_executor(event, readiness, identity)

                    state = reconciler.reconcile(
                        processing.lifecycle_event,
                        now=now,
                        market_readiness=processing.readiness,
                        capture=capture,
                    )
                    outcome = state.transitions[-1].action
            completed = self.event_store.complete(
                processing,
                owner=owner,
                now=_utc(self.clock(), "clock"),
                outcome=outcome,
            )
            return CaptureWorkerReceipt(
                processing.event_identity,
                completed.status,
                outcome,
                state,
                None if identity is None else identity.identity_id,
            )
        except Exception as exc:
            try:
                self.event_store.fail(
                    processing,
                    owner=owner,
                    now=_utc(self.clock(), "clock"),
                    error=f"{type(exc).__name__}: {exc}",
                )
            except DurableEventConflictError:
                pass
            raise

    def _snapshot_exists(self, event: DurableSoybeanEvent) -> bool:
        try:
            load_intraday_snapshot(
                self.snapshot_root, event.business_date, event.session
            )
        except IntradaySnapshotNotFoundError:
            return False
        return True

    def _converge_sealed(
        self,
        reconciler: SoybeanLifecycleReconciler,
        event: DurableSoybeanEvent,
        current: SoybeanLifecycleState,
        now: datetime,
    ) -> SoybeanLifecycleState:
        if current.market_state is MarketState.SEALED:
            return current
        return reconciler.reconcile(
            event.lifecycle_event,
            now=now,
            market_readiness=event.readiness,
            capture=lambda *_: (_ for _ in ()).throw(
                RuntimeError("sealed recovery must not invoke capture")
            ),
        )

    @staticmethod
    def _window_status(event: DurableSoybeanEvent, now: datetime) -> str:
        local = now.astimezone(BEIJING)
        if local.date() < event.business_date:
            return "NOT_OPEN"
        if local.date() > event.business_date:
            return "MISSED"
        start, end = SESSION_WINDOWS[event.session]
        if local.time() < start:
            return "NOT_OPEN"
        if local.time() >= end:
            return "MISSED"
        return "OPEN"


def _event_from_dict(raw: object) -> DurableSoybeanEvent:
    if not isinstance(raw, dict) or raw.get("schema_version") != EVENT_STORE_SCHEMA:
        raise DurableEventError("unsupported durable event schema")
    lifecycle = _lifecycle_event_from_dict(raw.get("lifecycle_event"))
    readiness = _readiness_from_dict(raw.get("readiness"))
    event = DurableSoybeanEvent(
        event_identity=str(raw["event_identity"]),
        business_idempotency_key=str(raw["business_idempotency_key"]),
        lifecycle_event=lifecycle,
        readiness=readiness,
        status=EventDeliveryStatus(raw["status"]),
        created_at=datetime.fromisoformat(str(raw["created_at"])),
        revision=int(raw["revision"]),
        claimed_at=None if raw.get("claimed_at") is None else datetime.fromisoformat(str(raw["claimed_at"])),
        lease_expires_at=None if raw.get("lease_expires_at") is None else datetime.fromisoformat(str(raw["lease_expires_at"])),
        completed_at=None if raw.get("completed_at") is None else datetime.fromisoformat(str(raw["completed_at"])),
        claim_owner=raw.get("claim_owner"),
        attempt_count=int(raw["attempt_count"]),
        last_error=raw.get("last_error"),
        outcome=raw.get("outcome"),
    )
    derived = _sha({
        "lifecycle_event": lifecycle.as_dict(),
        "readiness": readiness.as_dict(),
    })
    if event.event_identity != derived:
        raise DurableEventError("durable event content identity mismatch")
    for key, expected in (
        ("business_date", event.business_date.isoformat()),
        ("session", event.session.value),
        ("event_type", event.event_type.value),
        ("upstream_identity", event.upstream_identity),
        ("payload_identity", event.payload_identity),
    ):
        if raw.get(key) != expected:
            raise DurableEventError(f"durable event derived {key} mismatch")
    return event


def _lifecycle_event_from_dict(raw: object) -> LifecycleEvent:
    if not isinstance(raw, dict):
        raise DurableEventError("lifecycle event payload is invalid")
    event = LifecycleEvent(
        business_date=date.fromisoformat(str(raw["business_date"])),
        session=MarketSession(raw["session"]),
        event_type=EventType(raw["event_type"]),
        upstream_identity=str(raw["upstream_identity"]),
        occurred_at=datetime.fromisoformat(str(raw["occurred_at"])),
        payload_identity=str(raw["payload_identity"]),
        trace_id=raw.get("trace_id"),
    )
    if raw.get("idempotency_key") != event.idempotency_key:
        raise DurableEventError("lifecycle event idempotency key mismatch")
    if raw.get("schema_version") != event.schema_version:
        raise DurableEventError("unsupported lifecycle event schema")
    return event


def _readiness_from_dict(raw: object) -> MarketReadinessReport:
    if not isinstance(raw, dict):
        raise DurableEventError("market readiness payload is invalid")
    components = tuple(
        MarketComponentReadiness(
            component=str(item["component"]),
            status=MarketComponentStatus(item["status"]),
            required_identities=tuple(map(str, item["required_identities"])),
            available_identities=tuple(map(str, item["available_identities"])),
            stale_identities=tuple(map(str, item["stale_identities"])),
            missing_identities=tuple(map(str, item["missing_identities"])),
            reason=item.get("reason"),
        )
        for item in raw["components"]
    )
    report = MarketReadinessReport(
        business_date=date.fromisoformat(str(raw["business_date"])),
        session=MarketSession(raw["session"]),
        evaluated_at=datetime.fromisoformat(str(raw["evaluated_at"])),
        upstream_identity=str(raw["upstream_identity"]),
        components=components,
    )
    if raw.get("schema_version") != report.schema_version:
        raise DurableEventError("unsupported market readiness schema")
    if raw.get("market_ready") != report.market_ready:
        raise DurableEventError("market readiness derived status mismatch")
    if tuple(raw.get("blocking_reasons", ())) != report.blocking_reasons:
        raise DurableEventError("market readiness blocking reasons mismatch")
    return report


__all__ = [
    "CaptureWorkerReceipt",
    "DurableEventConflictError",
    "DurableEventError",
    "DurableSoybeanEvent",
    "EventDeliveryReceipt",
    "EventDeliveryStatus",
    "FullDailyCompletionObserver",
    "MACHINE_CAPTURE_READ_PATHS",
    "MACHINE_CAPTURE_SECRET_ACCESS",
    "MACHINE_CAPTURE_WRITE_PATHS",
    "MACHINE_IDENTITY_SOURCE",
    "MachineCaptureIdentity",
    "MachineIdentityError",
    "SoybeanCaptureWorker",
    "SoybeanDurableEventStore",
    "machine_capture_task_id",
]
