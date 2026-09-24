"""Event-driven reconciler for Soybean AM/PM readiness and snapshot capture."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from agri_research_agent.market_data.intraday import (
    IntradaySnapshot,
    IntradaySnapshotNotFoundError,
    MarketSession,
    load_intraday_snapshot,
)

from .lifecycle import (
    CnfAuthorizationState,
    CnfState,
    CnfSubmissionEnvelope,
    EventType,
    LifecycleEvent,
    LifecycleValidationError,
    MarketReadinessReport,
    MarketState,
    ProfitState,
    SoybeanLifecycleState,
    TransitionEvidence,
)
from .lifecycle_store import SoybeanLifecycleStore


BEIJING = ZoneInfo("Asia/Shanghai")
SESSION_WINDOWS = {
    MarketSession.AM: (time(8), time(12)),
    MarketSession.PM: (time(15), time(21)),
}
CNF_BLOCKERS = {
    CnfState.NOT_SUBMITTED: "CNF_NOT_SUBMITTED",
    CnfState.PARTIAL: "CNF_PARTIAL",
}


class LifecycleReconcileError(RuntimeError):
    pass


CaptureCallback = Callable[[LifecycleEvent, MarketReadinessReport], object]


class SoybeanLifecycleReconciler:
    def __init__(self, *, store: SoybeanLifecycleStore, snapshot_root: str | Path):
        self.store = store
        self.snapshot_root = Path(snapshot_root)

    def reconcile(
        self,
        event: LifecycleEvent,
        *,
        now: datetime,
        market_readiness: MarketReadinessReport | None = None,
        cnf_submission: CnfSubmissionEnvelope | None = None,
        cnf_authorization: CnfAuthorizationState | None = None,
        capture: CaptureCallback | None = None,
    ) -> SoybeanLifecycleState:
        local_now = now.astimezone(BEIJING)
        state = self.store.load(event.business_date, event.session)
        state = self._converge_existing_snapshot(state, event, local_now)
        if event.idempotency_key in state.processed_event_keys:
            return self._persist_transition(
                state,
                event,
                local_now,
                action="NOOP_DUPLICATE_EVENT",
                noop=True,
                reason="deterministic event idempotency key was already processed",
            )
        if event.event_type is EventType.CNF_AUTHORIZATION_CHANGED:
            if cnf_authorization is None:
                raise LifecycleValidationError("CNF authorization event requires a state")
            updated = replace(state, cnf_authorization_state=cnf_authorization)
            return self._persist_transition(
                updated,
                event,
                local_now,
                action="CNF_AUTHORIZATION_RECORDED",
                processed=True,
                reason="human authorization is informational to market eligibility",
            )
        if event.event_type is EventType.CNF_SUBMITTED:
            return self._reconcile_cnf(state, event, local_now, cnf_submission)
        if event.event_type is EventType.MARKET_DATA_AVAILABLE:
            return self._reconcile_market(
                state,
                event,
                local_now,
                market_readiness,
                capture,
            )
        raise LifecycleValidationError("unsupported lifecycle event")

    def _reconcile_cnf(
        self,
        state: SoybeanLifecycleState,
        event: LifecycleEvent,
        now: datetime,
        submission: CnfSubmissionEnvelope | None,
    ) -> SoybeanLifecycleState:
        if submission is None:
            raise LifecycleValidationError("CNF event requires a submission envelope")
        if submission.business_date != event.business_date:
            raise LifecycleValidationError("CNF submission/event date mismatch")
        if submission.content_identity != event.payload_identity:
            raise LifecycleValidationError("CNF submission/event identity mismatch")
        self.store.save_cnf_submission(submission)
        updated = replace(
            state,
            cnf_state=submission.status,
            cnf_submission_identity=submission.content_identity,
        )
        updated = self._derive_profit(updated)
        return self._persist_transition(
            updated,
            event,
            now,
            action="CNF_SUBMISSION_RECONCILED",
            processed=True,
            reason=(
                None if submission.status is CnfState.SUBMITTED
                else "CNF submission is incomplete"
            ),
            blocker=submission.status is not CnfState.SUBMITTED,
        )

    def _reconcile_market(
        self,
        state: SoybeanLifecycleState,
        event: LifecycleEvent,
        now: datetime,
        readiness: MarketReadinessReport | None,
        capture: CaptureCallback | None,
    ) -> SoybeanLifecycleState:
        if state.market_state is MarketState.SEALED:
            return self._persist_transition(
                state,
                event,
                now,
                action="NOOP_ALREADY_SEALED",
                processed=True,
                noop=True,
                reason="valid immutable snapshot already exists",
            )
        if readiness is None:
            raise LifecycleValidationError("market event requires readiness evidence")
        if (readiness.business_date, readiness.session) != (
            event.business_date,
            event.session,
        ):
            raise LifecycleValidationError("market readiness/event identity mismatch")
        if readiness.content_identity != event.payload_identity:
            raise LifecycleValidationError("market readiness/event payload mismatch")
        window = self._window_status(event, now)
        if window == "MISSED":
            updated = self._derive_profit(replace(
                state,
                market_state=MarketState.MISSED_WINDOW,
                blocking_reason="MISSED_WINDOW: no legal snapshot was sealed before session end",
            ))
            return self._persist_transition(
                updated,
                event,
                now,
                action="MARKET_WINDOW_MISSED",
                processed=True,
                blocker=True,
                reason=updated.blocking_reason,
            )
        if window == "NOT_OPEN":
            updated = self._derive_profit(replace(
                state,
                market_state=MarketState.NOT_READY,
                blocking_reason="SESSION_WINDOW_NOT_OPEN",
            ))
            return self._persist_transition(
                updated,
                event,
                now,
                action="MARKET_WAITING_FOR_WINDOW",
                processed=True,
                blocker=True,
                reason=updated.blocking_reason,
            )
        if not readiness.market_ready:
            reason = ";".join(readiness.blocking_reasons)
            updated = self._derive_profit(replace(
                state,
                market_state=MarketState.NOT_READY,
                blocking_reason=reason,
            ))
            return self._persist_transition(
                updated,
                event,
                now,
                action="MARKET_INPUT_NOT_READY",
                processed=True,
                blocker=True,
                reason=reason,
                evidence_reference=f"market-readiness:{readiness.content_identity}",
            )
        if capture is None:
            updated = self._derive_profit(replace(
                state,
                market_state=MarketState.READY_TO_CAPTURE,
                blocking_reason="MACHINE_IDENTITY_BLOCKER",
            ))
            return self._persist_transition(
                updated,
                event,
                now,
                action="MARKET_READY_TO_CAPTURE",
                processed=True,
                blocker=True,
                reason="MACHINE_IDENTITY_BLOCKER",
                evidence_reference=f"market-readiness:{readiness.content_identity}",
            )
        ready = self._derive_profit(replace(
            state,
            market_state=MarketState.READY_TO_CAPTURE,
            blocking_reason=None,
        ))
        ready = self._persist_transition(
            ready,
            event,
            now,
            action="MARKET_READY_TO_CAPTURE",
            evidence_reference=f"market-readiness:{readiness.content_identity}",
        )
        capturing = self._derive_profit(replace(
            ready,
            market_state=MarketState.CAPTURING,
            blocking_reason=None,
        ))
        capturing = self._persist_transition(
            capturing,
            event,
            now,
            action="MARKET_CAPTURE_STARTED",
            evidence_reference=f"market-readiness:{readiness.content_identity}",
        )
        try:
            capture(event, readiness)
            snapshot = load_intraday_snapshot(
                self.snapshot_root,
                event.business_date,
                event.session,
            )
            expected = {
                identity
                for component in readiness.components
                for identity in component.required_identities
            }
            actual = {
                f"{quote.instrument_id}|{quote.contract_code}"
                for quote in snapshot.quotes
            }
            if not expected.issubset(actual):
                raise LifecycleReconcileError(
                    "sealed snapshot lacks market-ready exact identities"
                )
        except Exception as exc:
            failed = self._derive_profit(replace(
                capturing,
                market_state=MarketState.FAILED,
                blocking_reason=f"{type(exc).__name__}: {exc}",
            ))
            self._persist_transition(
                failed,
                event,
                now,
                action="MARKET_CAPTURE_FAILED",
                blocker=True,
                reason=failed.blocking_reason,
                evidence_reference=f"market-readiness:{readiness.content_identity}",
            )
            raise LifecycleReconcileError("market capture failed") from exc
        sealed = self._state_with_snapshot(capturing, snapshot)
        sealed = self._derive_profit(sealed)
        return self._persist_transition(
            sealed,
            event,
            now,
            action="MARKET_CAPTURE_SEALED",
            processed=True,
            evidence_reference=f"snapshot:{snapshot.content_sha256}",
        )

    def _converge_existing_snapshot(
        self,
        state: SoybeanLifecycleState,
        event: LifecycleEvent,
        now: datetime,
    ) -> SoybeanLifecycleState:
        try:
            snapshot = load_intraday_snapshot(
                self.snapshot_root,
                event.business_date,
                event.session,
            )
        except IntradaySnapshotNotFoundError:
            return state
        sealed = self._derive_profit(self._state_with_snapshot(state, snapshot))
        if sealed == state:
            return state
        return self._persist_transition(
            sealed,
            event,
            now,
            action="RECOVERED_EXISTING_SEALED_SNAPSHOT",
            noop=True,
            reason="durable state converged from immutable snapshot store",
            evidence_reference=f"snapshot:{snapshot.content_sha256}",
        )

    @staticmethod
    def _state_with_snapshot(
        state: SoybeanLifecycleState,
        snapshot: IntradaySnapshot,
    ) -> SoybeanLifecycleState:
        return replace(
            state,
            market_state=MarketState.SEALED,
            market_snapshot_release_id=snapshot.release_id,
            market_snapshot_content_identity=snapshot.content_sha256,
            blocking_reason=None,
        )

    @staticmethod
    def _derive_profit(state: SoybeanLifecycleState) -> SoybeanLifecycleState:
        if state.profit_state in {ProfitState.SEALED, ProfitState.MATERIALIZING}:
            return state
        if state.market_state is not MarketState.SEALED:
            profit = ProfitState.WAITING_FOR_MARKET
        elif state.cnf_state is not CnfState.SUBMITTED:
            profit = ProfitState.WAITING_FOR_CNF
        else:
            profit = ProfitState.READY_TO_MATERIALIZE
        market_reasons = tuple(
            reason
            for reason in (state.blocking_reason or "").split(";")
            if reason and reason not in CNF_BLOCKERS.values()
        )
        cnf_reason = CNF_BLOCKERS.get(state.cnf_state)
        blockers = (*market_reasons, *((cnf_reason,) if cnf_reason else ()))
        return replace(
            state,
            profit_state=profit,
            blocking_reason=";".join(blockers) or None,
        )

    @staticmethod
    def _window_status(event: LifecycleEvent, now: datetime) -> str:
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

    def _persist_transition(
        self,
        state: SoybeanLifecycleState,
        event: LifecycleEvent,
        now: datetime,
        *,
        action: str,
        processed: bool = False,
        noop: bool = False,
        blocker: bool = False,
        reason: str | None = None,
        evidence_reference: str | None = None,
    ) -> SoybeanLifecycleState:
        current = self.store.load(state.business_date, state.session)
        if current.state_revision != state.state_revision:
            raise LifecycleReconcileError("lifecycle state changed during reconcile")
        sequence = len(state.transitions) + 1
        transition = TransitionEvidence(
            sequence=sequence,
            transitioned_at=now,
            event_type=event.event_type,
            event_identity=event.idempotency_key,
            action=action,
            previous_market_state=current.market_state,
            next_market_state=state.market_state,
            previous_cnf_state=current.cnf_state,
            next_cnf_state=state.cnf_state,
            previous_profit_state=current.profit_state,
            next_profit_state=state.profit_state,
            reason=reason,
            noop=noop,
            blocker=blocker,
            snapshot_identity=state.market_snapshot_content_identity,
            cnf_identity=state.cnf_submission_identity,
            evidence_reference=evidence_reference,
        )
        processed_keys = state.processed_event_keys
        if processed and event.idempotency_key not in processed_keys:
            processed_keys = (*processed_keys, event.idempotency_key)
        updated = replace(
            state,
            last_transition_at=now,
            last_event_type=event.event_type,
            last_event_identity=event.idempotency_key,
            blocking_reason=state.blocking_reason,
            attempt_evidence_reference=f"ledger:{state.lifecycle_id}:{sequence}",
            processed_event_keys=processed_keys,
            transitions=(*state.transitions, transition),
        )
        return self.store.save(updated, expected_revision=state.state_revision)


__all__ = [
    "BEIJING",
    "LifecycleReconcileError",
    "SESSION_WINDOWS",
    "SoybeanLifecycleReconciler",
]
