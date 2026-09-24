"""One-way event adapters for Soybean lifecycle inputs.

FULL DAILY remains an independent producer.  Its overall status is recorded as
upstream evidence but never substitutes for Soybean market readiness.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Mapping

from agri_research_agent.market_data.intraday import MarketSession

from .config import SoybeanImportProfitConfig
from .daily_increment import shipment_year_for
from .lifecycle import (
    CnfAuthorizationState,
    CnfSubmissionEnvelope,
    CnfSubmissionValue,
    CnfValueKind,
    EventType,
    LifecycleEvent,
    LifecycleValidationError,
    MarketReadinessReport,
)


FULL_DAILY_EVENT_STATUSES = frozenset({"SUCCESS", "PARTIAL_SUCCESS"})


def full_daily_market_event(
    *,
    full_daily_status: str,
    full_daily_identity: str,
    readiness: MarketReadinessReport,
    occurred_at: datetime,
    trace_id: str | None = None,
) -> LifecycleEvent:
    """Adapt one completion fact without coupling FULL DAILY to lifecycle success."""

    if full_daily_status not in FULL_DAILY_EVENT_STATUSES:
        raise LifecycleValidationError("FULL DAILY event must be a completed success class")
    if not full_daily_identity.strip():
        raise LifecycleValidationError("FULL DAILY identity is required")
    return LifecycleEvent(
        business_date=readiness.business_date,
        session=readiness.session,
        event_type=EventType.MARKET_DATA_AVAILABLE,
        upstream_identity=f"full-daily:{full_daily_identity}:{full_daily_status}",
        occurred_at=occurred_at,
        payload_identity=readiness.content_identity,
        trace_id=trace_id,
    )


def build_cnf_submission_envelope(
    *,
    business_date: date,
    values: Mapping[tuple[str, int], float | CnfValueKind | None],
    config: SoybeanImportProfitConfig,
    revision: int,
    submitted_at: datetime,
    store_sha256: str,
    actor_id: str,
    actor_role: str,
) -> CnfSubmissionEnvelope:
    expected = {
        (origin, month)
        for origin in config.origin_codes
        for month in range(1, 13)
    }
    if not set(values).issubset(expected):
        raise LifecycleValidationError("CNF submission contains an unknown business key")
    rows = []
    for origin, month in sorted(expected):
        raw = values.get((origin, month))
        if isinstance(raw, CnfValueKind):
            kind = raw
            value = None
        elif raw is None:
            kind = CnfValueKind.NOT_PROVIDED
            value = None
        else:
            kind = CnfValueKind.VALUE
            value = raw
        rows.append(CnfSubmissionValue(
            origin=origin,
            shipment_year=shipment_year_for(business_date, month),
            shipment_month=month,
            kind=kind,
            value=value,
        ))
    return CnfSubmissionEnvelope(
        business_date=business_date,
        commodity=config.commodity,
        revision=revision,
        submitted_at=submitted_at,
        actor_id=actor_id,
        actor_role=actor_role,
        store_sha256=store_sha256,
        values=tuple(rows),
        required_record_count=len(expected),
    )


def cnf_submission_event(
    submission: CnfSubmissionEnvelope,
    *,
    session: MarketSession,
    trace_id: str | None = None,
) -> LifecycleEvent:
    return LifecycleEvent(
        business_date=submission.business_date,
        session=session,
        event_type=EventType.CNF_SUBMITTED,
        upstream_identity=submission.submission_id,
        occurred_at=submission.submitted_at,
        payload_identity=submission.content_identity,
        trace_id=trace_id,
    )


def cnf_authorization_event(
    *,
    business_date: date,
    session: MarketSession,
    state: CnfAuthorizationState,
    observed_at: datetime,
    authority_identity: str,
) -> LifecycleEvent:
    return LifecycleEvent(
        business_date=business_date,
        session=session,
        event_type=EventType.CNF_AUTHORIZATION_CHANGED,
        upstream_identity=authority_identity,
        occurred_at=observed_at,
        payload_identity=state.value,
    )


__all__ = [
    "FULL_DAILY_EVENT_STATUSES",
    "build_cnf_submission_envelope",
    "cnf_authorization_event",
    "cnf_submission_event",
    "full_daily_market_event",
]
