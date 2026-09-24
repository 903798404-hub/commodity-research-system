"""Atomic JSON store for Soybean session lifecycle state and transition evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
import json
import os
from pathlib import Path
import tempfile

from filelock import FileLock, Timeout

from agri_research_agent.market_data.intraday import MarketSession

from .lifecycle import (
    CnfAuthorizationState,
    CnfState,
    CnfSubmissionEnvelope,
    CnfSubmissionValue,
    CnfValueKind,
    EventType,
    LifecycleValidationError,
    MarketState,
    ProfitState,
    SCHEMA_VERSION,
    SoybeanLifecycleState,
    TransitionEvidence,
)


class LifecycleStoreError(RuntimeError):
    pass


class LifecycleStoreConflictError(LifecycleStoreError):
    pass


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise LifecycleStoreError("duplicate lifecycle JSON key")
        value[key] = item
    return value


class SoybeanLifecycleStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def state_path(self, business_date: date, session: MarketSession | str) -> Path:
        resolved = MarketSession(session)
        return self.root / "sessions" / f"{business_date.isoformat()}-{resolved.value}.json"

    def current_submission_path(self, business_date: date) -> Path:
        return self.root / "cnf-submissions" / business_date.isoformat() / "current.json"

    def load_cnf_submission(self, business_date: date) -> CnfSubmissionEnvelope | None:
        path = self.current_submission_path(business_date)
        if not path.is_file():
            return None
        return _submission_from_dict(_read_json(path))

    def save_cnf_submission(self, submission: CnfSubmissionEnvelope) -> Path:
        directory = self.current_submission_path(submission.business_date).parent
        directory.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(directory / ".cnf-submission.lock"))
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise LifecycleStoreConflictError("CNF submission store is locked") from exc
        try:
            current = self.load_cnf_submission(submission.business_date)
            if current is not None:
                if current.content_identity == submission.content_identity:
                    return directory / "revisions" / (
                        f"r{submission.revision:06d}-{submission.content_identity}.json"
                    )
                if submission.revision <= current.revision:
                    raise LifecycleStoreConflictError("CNF submission revision is not monotonic")
            revision_path = directory / "revisions" / (
                f"r{submission.revision:06d}-{submission.content_identity}.json"
            )
            payload = json.dumps(
                submission.as_dict(), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode("utf-8") + b"\n"
            revision_path.parent.mkdir(exist_ok=True)
            if revision_path.exists():
                if revision_path.read_bytes() != payload:
                    raise LifecycleStoreConflictError("CNF revision path has different content")
            else:
                _write_exclusive(revision_path, payload)
            _atomic_replace(self.current_submission_path(submission.business_date), payload)
            verified = self.load_cnf_submission(submission.business_date)
            if verified is None or verified.content_identity != submission.content_identity:
                raise LifecycleStoreError("CNF submission failed durable round-trip")
            return revision_path
        finally:
            lock.release()

    def load(self, business_date: date, session: MarketSession | str) -> SoybeanLifecycleState:
        resolved = MarketSession(session)
        path = self.state_path(business_date, resolved)
        if not path.is_file():
            return SoybeanLifecycleState(business_date, resolved)
        before = path.stat()
        try:
            raw = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    LifecycleStoreError(f"nonfinite lifecycle JSON value: {value}")
                ),
            )
            state = _state_from_dict(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, LifecycleStoreError):
                raise
            raise LifecycleStoreError("invalid lifecycle state") from exc
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise LifecycleStoreError("lifecycle state changed during read")
        if (state.business_date, state.session) != (business_date, resolved):
            raise LifecycleStoreError("lifecycle path identity mismatch")
        return state

    def save(
        self,
        state: SoybeanLifecycleState,
        *,
        expected_revision: int,
    ) -> SoybeanLifecycleState:
        if state.state_revision != expected_revision:
            raise LifecycleStoreConflictError("caller lifecycle revision is stale")
        path = self.state_path(state.business_date, state.session)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(path.parent / f".{path.name}.lock"))
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise LifecycleStoreConflictError("lifecycle state is locked") from exc
        try:
            current = self.load(state.business_date, state.session)
            if current.state_revision != expected_revision:
                raise LifecycleStoreConflictError("lifecycle revision changed")
            saved = replace(state, state_revision=expected_revision + 1)
            payload = json.dumps(
                saved.as_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8") + b"\n"
            _atomic_replace(path, payload)
            verified = self.load(state.business_date, state.session)
            if verified != saved:
                raise LifecycleStoreError("lifecycle state failed atomic round-trip")
            return verified
        finally:
            lock.release()


def _state_from_dict(raw: object) -> SoybeanLifecycleState:
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise LifecycleStoreError("unsupported lifecycle schema")
    transitions = tuple(_transition_from_dict(item) for item in raw["transitions"])
    state = SoybeanLifecycleState(
        business_date=date.fromisoformat(raw["business_date"]),
        session=MarketSession(raw["session"]),
        market_state=MarketState(raw["market_state"]),
        cnf_state=CnfState(raw["cnf_state"]),
        profit_state=ProfitState(raw["profit_state"]),
        cnf_authorization_state=CnfAuthorizationState(raw["cnf_authorization_state"]),
        market_snapshot_release_id=raw.get("market_snapshot_release_id"),
        market_snapshot_content_identity=raw.get("market_snapshot_content_identity"),
        cnf_submission_identity=raw.get("cnf_submission_identity"),
        profit_release_id=raw.get("profit_release_id"),
        last_transition_at=(
            None if raw.get("last_transition_at") is None
            else datetime.fromisoformat(raw["last_transition_at"])
        ),
        last_event_type=(
            None if raw.get("last_event_type") is None
            else EventType(raw["last_event_type"])
        ),
        last_event_identity=raw.get("last_event_identity"),
        blocking_reason=raw.get("blocking_reason"),
        attempt_evidence_reference=raw.get("attempt_evidence_reference"),
        state_revision=int(raw["state_revision"]),
        processed_event_keys=tuple(raw["processed_event_keys"]),
        transitions=transitions,
    )
    if raw.get("lifecycle_id") != state.lifecycle_id:
        raise LifecycleStoreError("lifecycle id mismatch")
    if state.state_revision < 0:
        raise LifecycleStoreError("negative lifecycle revision")
    if len(state.processed_event_keys) != len(set(state.processed_event_keys)):
        raise LifecycleStoreError("duplicate processed lifecycle event")
    if tuple(item.sequence for item in transitions) != tuple(range(1, len(transitions) + 1)):
        raise LifecycleStoreError("transition sequence is invalid")
    return state


def _submission_from_dict(raw: object) -> CnfSubmissionEnvelope:
    if not isinstance(raw, dict):
        raise LifecycleStoreError("CNF submission is invalid")
    values = tuple(CnfSubmissionValue(
        origin=str(item["origin"]),
        shipment_year=int(item["shipment_year"]),
        shipment_month=int(item["shipment_month"]),
        kind=CnfValueKind(item["kind"]),
        value=item.get("value"),
    ) for item in raw["values"])
    envelope = CnfSubmissionEnvelope(
        business_date=date.fromisoformat(raw["business_date"]),
        commodity=str(raw["commodity"]),
        revision=int(raw["revision"]),
        submitted_at=datetime.fromisoformat(raw["submitted_at"]),
        actor_id=str(raw["actor_id"]),
        actor_role=str(raw["actor_role"]),
        store_sha256=str(raw["store_sha256"]),
        values=values,
        required_record_count=int(raw["required_record_count"]),
    )
    if (raw.get("submission_id") != envelope.submission_id
            or raw.get("content_identity") != envelope.content_identity
            or raw.get("status") != envelope.status.value
            or raw.get("record_count") != envelope.record_count
            or raw.get("completeness") != envelope.completeness):
        raise LifecycleStoreError("CNF submission derived identity mismatch")
    return envelope


def _read_json(path: Path) -> object:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                LifecycleStoreError(f"nonfinite JSON value: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleStoreError("invalid JSON state") from exc


def _write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if path.exists():
            path.unlink()
        raise


def _atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _transition_from_dict(raw: object) -> TransitionEvidence:
    if not isinstance(raw, dict):
        raise LifecycleStoreError("transition evidence is invalid")
    return TransitionEvidence(
        sequence=int(raw["sequence"]),
        transitioned_at=datetime.fromisoformat(raw["transitioned_at"]),
        event_type=EventType(raw["event_type"]),
        event_identity=str(raw["event_identity"]),
        action=str(raw["action"]),
        previous_market_state=MarketState(raw["previous_market_state"]),
        next_market_state=MarketState(raw["next_market_state"]),
        previous_cnf_state=CnfState(raw["previous_cnf_state"]),
        next_cnf_state=CnfState(raw["next_cnf_state"]),
        previous_profit_state=ProfitState(raw["previous_profit_state"]),
        next_profit_state=ProfitState(raw["next_profit_state"]),
        reason=raw.get("reason"),
        noop=bool(raw["noop"]),
        blocker=bool(raw["blocker"]),
        snapshot_identity=raw.get("snapshot_identity"),
        cnf_identity=raw.get("cnf_identity"),
        evidence_reference=raw.get("evidence_reference"),
    )


__all__ = [
    "LifecycleStoreConflictError",
    "LifecycleStoreError",
    "SoybeanLifecycleStore",
]
