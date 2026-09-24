"""Durable Soybean AM/PM lifecycle domain contracts.

The domain deliberately separates market capture, human CNF submission and
profit materialization.  It contains no scheduler, provider, authorization or
materialization side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
import hashlib
import json
import math
import re
from typing import Iterable, Mapping

from agri_research_agent.market_data.contracts import ContractId, Exchange
from agri_research_agent.market_data.intraday import MarketSession, require_aware

from .config import SoybeanImportProfitConfig
from .intraday import required_intraday_contracts_for_date


SCHEMA_VERSION = "soybean-lifecycle/1"
EVENT_SCHEMA_VERSION = "soybean-lifecycle-event/1"
CNF_SUBMISSION_SCHEMA_VERSION = "soybean-cnf-submission/1"
READINESS_SCHEMA_VERSION = "soybean-market-readiness/1"
_SHA256 = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)


class LifecycleValidationError(ValueError):
    pass


class MarketState(StrEnum):
    NOT_READY = "NOT_READY"
    READY_TO_CAPTURE = "READY_TO_CAPTURE"
    CAPTURING = "CAPTURING"
    SEALED = "SEALED"
    FAILED = "FAILED"
    MISSED_WINDOW = "MISSED_WINDOW"


class CnfState(StrEnum):
    NOT_SUBMITTED = "NOT_SUBMITTED"
    PARTIAL = "PARTIAL"
    SUBMITTED = "SUBMITTED"


class ProfitState(StrEnum):
    WAITING_FOR_MARKET = "WAITING_FOR_MARKET"
    WAITING_FOR_CNF = "WAITING_FOR_CNF"
    READY_TO_MATERIALIZE = "READY_TO_MATERIALIZE"
    MATERIALIZING = "MATERIALIZING"
    SEALED = "SEALED"
    FAILED = "FAILED"


class CnfAuthorizationState(StrEnum):
    UNKNOWN = "UNKNOWN"
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class EventType(StrEnum):
    MARKET_DATA_AVAILABLE = "MARKET_DATA_AVAILABLE"
    CNF_SUBMITTED = "CNF_SUBMITTED"
    CNF_AUTHORIZATION_CHANGED = "CNF_AUTHORIZATION_CHANGED"


class CnfValueKind(StrEnum):
    NOT_PROVIDED = "NOT_PROVIDED"
    NO_QUOTE = "NO_QUOTE"
    VALUE = "VALUE"


class MarketComponentStatus(StrEnum):
    READY = "READY"
    STALE = "STALE"
    MISSING = "MISSING"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _aware_utc(value: datetime, label: str) -> datetime:
    return require_aware(value, label).astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class CnfSubmissionValue:
    origin: str
    shipment_year: int
    shipment_month: int
    kind: CnfValueKind
    value: float | None = None

    def __post_init__(self) -> None:
        if not self.origin.strip() or not 1 <= self.shipment_month <= 12:
            raise LifecycleValidationError("invalid CNF submission business key")
        if self.shipment_year < 2000:
            raise LifecycleValidationError("invalid CNF shipment year")
        if self.kind is CnfValueKind.VALUE:
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
                raise LifecycleValidationError("CNF VALUE requires a finite number")
            if not math.isfinite(float(self.value)):
                raise LifecycleValidationError("CNF VALUE requires a finite number")
            object.__setattr__(self, "value", float(self.value))
        elif self.value is not None:
            raise LifecycleValidationError("NO_QUOTE and NOT_PROVIDED cannot carry a value")

    @property
    def key(self) -> tuple[str, int, int]:
        return self.origin, self.shipment_year, self.shipment_month

    def as_dict(self) -> dict[str, object]:
        return {
            "origin": self.origin,
            "shipment_year": self.shipment_year,
            "shipment_month": self.shipment_month,
            "kind": self.kind.value,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class CnfSubmissionEnvelope:
    business_date: date
    commodity: str
    revision: int
    submitted_at: datetime
    actor_id: str
    actor_role: str
    store_sha256: str
    values: tuple[CnfSubmissionValue, ...]
    required_record_count: int
    schema_version: str = CNF_SUBMISSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.business_date) is not date or self.commodity != "soybean":
            raise LifecycleValidationError("CNF submission identity is invalid")
        if self.revision < 1 or self.required_record_count < 1:
            raise LifecycleValidationError("CNF submission revision/count is invalid")
        _aware_utc(self.submitted_at, "submitted_at")
        if not self.actor_id.strip() or not self.actor_role.strip():
            raise LifecycleValidationError("CNF actor identity is required")
        if not _SHA256.fullmatch(self.store_sha256):
            raise LifecycleValidationError("CNF store identity must be SHA-256")
        keys = [value.key for value in self.values]
        if len(keys) != len(set(keys)) or len(keys) > self.required_record_count:
            raise LifecycleValidationError("CNF submission keys are duplicated or excessive")
        object.__setattr__(
            self,
            "values",
            tuple(sorted(self.values, key=lambda item: item.key)),
        )

    @property
    def provided_record_count(self) -> int:
        return sum(value.kind is not CnfValueKind.NOT_PROVIDED for value in self.values)

    @property
    def record_count(self) -> int:
        return self.provided_record_count

    @property
    def completeness(self) -> float:
        return self.provided_record_count / self.required_record_count

    @property
    def status(self) -> CnfState:
        return (
            CnfState.SUBMITTED
            if self.provided_record_count == self.required_record_count
            else CnfState.PARTIAL
        )

    def content_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "business_date": self.business_date.isoformat(),
            "commodity": self.commodity,
            "revision": self.revision,
            "submitted_at": _aware_utc(self.submitted_at, "submitted_at").isoformat(),
            "status": self.status.value,
            "record_count": self.record_count,
            "required_record_count": self.required_record_count,
            "completeness": self.completeness,
            "store_sha256": self.store_sha256,
            "actor_id": self.actor_id,
            "actor_role": self.actor_role,
            "values": [value.as_dict() for value in sorted(self.values, key=lambda item: item.key)],
        }

    @property
    def content_identity(self) -> str:
        return _sha(self.content_dict())

    @property
    def submission_id(self) -> str:
        return (
            f"soybean-cnf-{self.business_date.isoformat()}-r{self.revision}-"
            f"{self.content_identity[:12]}"
        )

    def as_dict(self) -> dict[str, object]:
        return {**self.content_dict(), "submission_id": self.submission_id,
                "content_identity": self.content_identity}


@dataclass(frozen=True, slots=True)
class MarketObservation:
    instrument_id: str
    contract_code: str
    source_updated_at: datetime
    retrieved_at: datetime
    available: bool = True

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise LifecycleValidationError("market observation identity is required")
        updated = _aware_utc(self.source_updated_at, "source_updated_at")
        retrieved = _aware_utc(self.retrieved_at, "retrieved_at")
        if updated > retrieved:
            raise LifecycleValidationError("market observation chronology is invalid")

    @property
    def key(self) -> tuple[str, str]:
        return self.instrument_id, self.contract_code


@dataclass(frozen=True, slots=True)
class MarketComponentReadiness:
    component: str
    status: MarketComponentStatus
    required_identities: tuple[str, ...]
    available_identities: tuple[str, ...]
    stale_identities: tuple[str, ...] = ()
    missing_identities: tuple[str, ...] = ()
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "component": self.component,
            "status": self.status.value,
            "required_identities": list(self.required_identities),
            "available_identities": list(self.available_identities),
            "stale_identities": list(self.stale_identities),
            "missing_identities": list(self.missing_identities),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class MarketReadinessReport:
    business_date: date
    session: MarketSession
    evaluated_at: datetime
    upstream_identity: str
    components: tuple[MarketComponentReadiness, ...]
    schema_version: str = READINESS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _aware_utc(self.evaluated_at, "evaluated_at")
        if not self.upstream_identity.strip() or not self.components:
            raise LifecycleValidationError("market readiness evidence is incomplete")
        names = [component.component for component in self.components]
        if len(names) != len(set(names)):
            raise LifecycleValidationError("market readiness component is duplicated")

    @property
    def market_ready(self) -> bool:
        return all(component.status is MarketComponentStatus.READY for component in self.components)

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        return tuple(
            f"{component.component}:{component.status.value}"
            + (f":{component.reason}" if component.reason else "")
            for component in self.components
            if component.status is not MarketComponentStatus.READY
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "business_date": self.business_date.isoformat(),
            "session": self.session.value,
            "evaluated_at": _aware_utc(self.evaluated_at, "evaluated_at").isoformat(),
            "upstream_identity": self.upstream_identity,
            "market_ready": self.market_ready,
            "blocking_reasons": list(self.blocking_reasons),
            "components": [component.as_dict() for component in self.components],
        }

    @property
    def content_identity(self) -> str:
        return _sha(self.as_dict())


def required_market_identities(
    business_date: date,
    config: SoybeanImportProfitConfig,
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Return the exact union required by the existing soybean snapshot ACL."""

    cbot, meal, oil = required_intraday_contracts_for_date(business_date, config)
    result: dict[str, tuple[tuple[str, str], ...]] = {
        "CBOT": tuple(
            (
                str(ContractId(Exchange.CBOT, "SOYBEAN", 2000 + int(code[:2]), int(code[2:]))),
                code,
            )
            for code in cbot
        ),
        "DCE_MEAL": tuple(
            (
                str(ContractId(Exchange.DCE, "SOYMEAL", 2000 + int(code[1:3]), int(code[3:]))),
                code,
            )
            for code in meal
        ),
        "DCE_OIL": tuple(
            (
                str(ContractId(Exchange.DCE, "SOYOIL", 2000 + int(code[1:3]), int(code[3:]))),
                code,
            )
            for code in oil
        ),
        "FX": (("FX:USD/CNH:SPOT", ""),),
    }
    return result


def evaluate_market_readiness(
    *,
    business_date: date,
    session: MarketSession,
    config: SoybeanImportProfitConfig,
    observations: Iterable[MarketObservation],
    source_not_before: Mapping[str, datetime],
    evaluated_at: datetime,
    upstream_identity: str,
) -> MarketReadinessReport:
    """Evaluate exact identity and freshness without considering FULL DAILY status."""

    evaluated = _aware_utc(evaluated_at, "evaluated_at")
    rows = tuple(observations)
    by_key: dict[tuple[str, str], MarketObservation] = {}
    duplicates: set[tuple[str, str]] = set()
    for row in rows:
        if row.key in by_key:
            duplicates.add(row.key)
        by_key[row.key] = row
    components = []
    for name, required in required_market_identities(business_date, config).items():
        required_labels = tuple(f"{instrument}|{contract}" for instrument, contract in required)
        available: list[str] = []
        stale: list[str] = []
        missing: list[str] = []
        mismatch: list[str] = []
        for key, label in zip(required, required_labels):
            row = by_key.get(key)
            if key in duplicates:
                mismatch.append(label)
                continue
            if row is None or not row.available:
                missing.append(label)
                continue
            floor = source_not_before.get(row.instrument_id)
            if floor is None:
                missing.append(label)
                continue
            floor = _aware_utc(floor, "source_not_before")
            updated = _aware_utc(row.source_updated_at, "source_updated_at")
            retrieved = _aware_utc(row.retrieved_at, "retrieved_at")
            if updated > retrieved or retrieved > evaluated:
                mismatch.append(label)
            elif updated < floor:
                stale.append(label)
            else:
                available.append(label)
        if mismatch:
            status = MarketComponentStatus.IDENTITY_MISMATCH
            reason = "duplicate identity or invalid observation chronology"
        elif stale:
            status = MarketComponentStatus.STALE
            reason = "source_updated_at precedes approved source_not_before"
        elif missing:
            status = MarketComponentStatus.MISSING
            reason = "required exact identity or freshness policy is missing"
        else:
            status = MarketComponentStatus.READY
            reason = None
        components.append(MarketComponentReadiness(
            name,
            status,
            required_labels,
            tuple(available),
            tuple(stale),
            tuple(missing),
            reason,
        ))
    return MarketReadinessReport(
        business_date,
        MarketSession(session),
        evaluated,
        upstream_identity,
        tuple(components),
    )


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    business_date: date
    session: MarketSession
    event_type: EventType
    upstream_identity: str
    occurred_at: datetime
    payload_identity: str
    trace_id: str | None = None
    schema_version: str = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _aware_utc(self.occurred_at, "occurred_at")
        if not self.upstream_identity.strip() or not self.payload_identity.strip():
            raise LifecycleValidationError("event identities are required")

    @property
    def idempotency_key(self) -> str:
        return _sha({
            "business_date": self.business_date.isoformat(),
            "session": self.session.value,
            "event_type": self.event_type.value,
            "upstream_identity": self.upstream_identity,
            "payload_identity": self.payload_identity,
        })

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "business_date": self.business_date.isoformat(),
            "session": self.session.value,
            "event_type": self.event_type.value,
            "upstream_identity": self.upstream_identity,
            "occurred_at": _aware_utc(self.occurred_at, "occurred_at").isoformat(),
            "payload_identity": self.payload_identity,
            "trace_id": self.trace_id,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class TransitionEvidence:
    sequence: int
    transitioned_at: datetime
    event_type: EventType
    event_identity: str
    action: str
    previous_market_state: MarketState
    next_market_state: MarketState
    previous_cnf_state: CnfState
    next_cnf_state: CnfState
    previous_profit_state: ProfitState
    next_profit_state: ProfitState
    reason: str | None = None
    noop: bool = False
    blocker: bool = False
    snapshot_identity: str | None = None
    cnf_identity: str | None = None
    evidence_reference: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "transitioned_at": _aware_utc(self.transitioned_at, "transitioned_at").isoformat(),
            "event_type": self.event_type.value,
            "event_identity": self.event_identity,
            "action": self.action,
            "previous_market_state": self.previous_market_state.value,
            "next_market_state": self.next_market_state.value,
            "previous_cnf_state": self.previous_cnf_state.value,
            "next_cnf_state": self.next_cnf_state.value,
            "previous_profit_state": self.previous_profit_state.value,
            "next_profit_state": self.next_profit_state.value,
            "reason": self.reason,
            "noop": self.noop,
            "blocker": self.blocker,
            "snapshot_identity": self.snapshot_identity,
            "cnf_identity": self.cnf_identity,
            "evidence_reference": self.evidence_reference,
        }


@dataclass(frozen=True, slots=True)
class SoybeanLifecycleState:
    business_date: date
    session: MarketSession
    market_state: MarketState = MarketState.NOT_READY
    cnf_state: CnfState = CnfState.NOT_SUBMITTED
    profit_state: ProfitState = ProfitState.WAITING_FOR_MARKET
    cnf_authorization_state: CnfAuthorizationState = CnfAuthorizationState.UNKNOWN
    market_snapshot_release_id: str | None = None
    market_snapshot_content_identity: str | None = None
    cnf_submission_identity: str | None = None
    profit_release_id: str | None = None
    last_transition_at: datetime | None = None
    last_event_type: EventType | None = None
    last_event_identity: str | None = None
    blocking_reason: str | None = None
    attempt_evidence_reference: str | None = None
    state_revision: int = 0
    processed_event_keys: tuple[str, ...] = ()
    transitions: tuple[TransitionEvidence, ...] = field(default_factory=tuple)
    schema_version: str = SCHEMA_VERSION

    @property
    def lifecycle_id(self) -> str:
        return f"{self.business_date.isoformat()}-{self.session.value}"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "lifecycle_id": self.lifecycle_id,
            "business_date": self.business_date.isoformat(),
            "session": self.session.value,
            "market_state": self.market_state.value,
            "cnf_state": self.cnf_state.value,
            "profit_state": self.profit_state.value,
            "cnf_authorization_state": self.cnf_authorization_state.value,
            "market_snapshot_release_id": self.market_snapshot_release_id,
            "market_snapshot_content_identity": self.market_snapshot_content_identity,
            "cnf_submission_identity": self.cnf_submission_identity,
            "profit_release_id": self.profit_release_id,
            "last_transition_at": None if self.last_transition_at is None else _aware_utc(
                self.last_transition_at, "last_transition_at"
            ).isoformat(),
            "last_event_type": None if self.last_event_type is None else self.last_event_type.value,
            "last_event_identity": self.last_event_identity,
            "blocking_reason": self.blocking_reason,
            "attempt_evidence_reference": self.attempt_evidence_reference,
            "state_revision": self.state_revision,
            "processed_event_keys": list(self.processed_event_keys),
            "transitions": [transition.as_dict() for transition in self.transitions],
        }


__all__ = [
    "CNF_SUBMISSION_SCHEMA_VERSION",
    "CnfAuthorizationState",
    "CnfState",
    "CnfSubmissionEnvelope",
    "CnfSubmissionValue",
    "CnfValueKind",
    "EventType",
    "LifecycleEvent",
    "LifecycleValidationError",
    "MarketComponentReadiness",
    "MarketComponentStatus",
    "MarketObservation",
    "MarketReadinessReport",
    "MarketState",
    "ProfitState",
    "SoybeanLifecycleState",
    "TransitionEvidence",
    "evaluate_market_readiness",
    "required_market_identities",
]
