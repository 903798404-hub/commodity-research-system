"""Independent intraday capture; no DAILY provider registration or business mappings."""
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo

from agri_research_agent.data_sources.tankan.intraday_adapter import ExactRequest, read_intraday
from agri_research_agent.market_data.calendars import CalendarBusinessDayPolicy
from agri_research_agent.market_data.intraday import (
    FreshnessStatus, IntradayQuote, IntradaySnapshot, IntradaySnapshotValidationError,
    MarketSession, require_aware, seal_intraday_snapshot, load_intraday_snapshot,
    IntradaySnapshotConflictError, canonical_json,
)
from agri_research_agent.shared.async_update import FreshnessPolicy, SeriesUpdate, evaluate_update, validate_update_summary
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode, assert_runtime_write

BEIJING=ZoneInfo("Asia/Shanghai")
AM_WINDOW_START,AM_WINDOW_END=time(8),time(12)
PM_WINDOW_START,PM_WINDOW_END=time(15),time(21)


class PublicIntradayCaptureError(IntradaySnapshotValidationError):
    pass


class PublicIntradaySourceStaleError(PublicIntradayCaptureError):
    status="SOURCE_STALE"


class PublicIntradayNotScheduledError(PublicIntradayCaptureError):
    status="NOT_SCHEDULED"


@dataclass(frozen=True,slots=True)
class CapturePolicy:
    """Explicit dated source windows; no invented calendar or freshness thresholds.

    Prior-night dates may be accepted only if the caller's approved evidence window
    includes them. These are observation windows, not fabricated source trade dates.
    """
    business_date: date
    session: MarketSession
    source_not_before: Mapping[str,datetime]
    policy_identity: str

    def __post_init__(self):
        if type(self.business_date) is not date or not isinstance(self.session,MarketSession) or not self.policy_identity.strip():
            raise PublicIntradayCaptureError("Explicit capture policy required")
        values=dict(self.source_not_before)
        if not values:
            raise PublicIntradayCaptureError("Source windows missing")
        for value in values.values():
            require_aware(value,"source_not_before")
        object.__setattr__(self,"source_not_before",MappingProxyType(values))


@dataclass(frozen=True,slots=True)
class CaptureResult:
    snapshot: IntradaySnapshot
    seal: object
    evidence: Mapping


def capture_public_intraday(*, client, requested: tuple[ExactRequest,...], context: RuntimeContext,
                            store_root: Path, policy: CapturePolicy,
                            business_day_policy: CalendarBusinessDayPolicy, clock) -> CaptureResult:
    assert_runtime_write(context,store_root)
    if context.module_id!="shared-intraday":
        raise PublicIntradayCaptureError("Wrong runtime module")
    decision=business_day_policy.decide(policy.business_date)
    if decision.business_date!=policy.business_date or not decision.is_business_day or not decision.source_identity or not decision.policy_name:
        raise PublicIntradayNotScheduledError("Calendar rejected date")
    if {r.instrument_id for r in requested}!=set(policy.source_not_before):
        raise PublicIntradayCaptureError("Policy must cover exact requested identities")
    def valid_capture(value):
        local=require_aware(value,"captured_at").astimezone(BEIJING)
        start,end=(AM_WINDOW_START,AM_WINDOW_END) if policy.session is MarketSession.AM else (PM_WINDOW_START,PM_WINDOW_END)
        if local.date()!=policy.business_date or not start<=local.time()<end:
            raise PublicIntradayNotScheduledError("Outside business session window")
        return local
    started=valid_capture(clock())
    for request in requested:
        if request.contract_code:
            suffix=request.contract_code[-4:]
            if (2000+int(suffix[:2]),int(suffix[2:])) < (policy.business_date.year,policy.business_date.month):
                raise PublicIntradayCaptureError("Expired requested contract month")
        if policy.source_not_before[request.instrument_id]>started:
            raise PublicIntradayCaptureError("Policy observation window is in the future")
    source=read_intraday(client,requested)
    captured_at=valid_capture(clock())
    if captured_at<started:
        raise PublicIntradayCaptureError("Capture clock regressed")
    quotes=[]
    updates={}
    available_ids=set()
    empty_ids=set()
    for item in source.available:
        key=item.request.instrument_id
        if not policy.source_not_before[key]<=item.source_updated_at<=item.retrieved_at<=captured_at:
            raise PublicIntradaySourceStaleError("Source outside approved observation window")
        if item.source_trade_date is not None and item.source_trade_date>policy.business_date:
            raise PublicIntradaySourceStaleError("Future source trade date")
        quote=IntradayQuote(policy.business_date,policy.session,captured_at,key,item.request.contract_code,
                           item.request.exchange,item.request.product,item.price,item.quote_type,item.currency,item.unit,
                           "TANKAN",item.source_table,item.source_updated_at,item.source_trade_date,FreshnessStatus.FRESH,
                           {**item.provenance,"freshness_policy_identity":policy.policy_identity,
                            "source_not_before":policy.source_not_before[key]},item.retrieved_at)
        quotes.append(quote)
        available_ids.add(key)
        observed=item.source_updated_at.astimezone(BEIJING).date()
        updates[key]=SeriesUpdate(None,observed,observed,new_row_count=1,source_window_row_count=1)
    for item in source.unavailable:
        key=item.instrument_id
        # Only a proven empty exact read is a nonblocking source absence. Broken
        # timestamps/prices and duplicate identities are data errors, never holes.
        errors=() if item.reason=="NOT_FOUND" else (item.reason,)
        updates[key]=SeriesUpdate(None,None,None,errors=errors)
        if not errors:
            empty_ids.add(key)
    required={r.instrument_id for r in requested}
    accounting=evaluate_update(dataset_id="public-intraday",required=required,series=updates,
                               next_identities=available_ids,as_of_date=policy.business_date,
                               policy=FreshnessPolicy("intraday-source-date-accounting/1"),verified_empty_source=empty_ids)
    validate_update_summary(accounting,required=required)
    if not accounting["promotion_allowed"] or not quotes:
        raise PublicIntradayCaptureError("Source integrity gate failed")
    environment="FORMAL" if context.mode is RuntimeMode.PRODUCTION_WRITE else "TEST_ISOLATED_NON_PRODUCTION"
    snapshot=IntradaySnapshot(policy.business_date,policy.session,captured_at,tuple(quotes),decision.policy_name,
                              decision.source_identity,environment,source.unavailable)
    sealed=seal_intraday_snapshot(store_root,snapshot,context=context)
    verified=load_intraday_snapshot(store_root,policy.business_date,policy.session,expected_environment=environment)
    evidence=dict(schema_version="public-intraday-evidence/1",run_id=verified.release_id,business_date=policy.business_date,
                  session=policy.session.value,captured_at=verified.captured_at,requested_count=len(required),available_count=len(quotes),
                  unavailable_count=len(source.unavailable),source_timestamps={q.instrument_id:q.source_updated_at for q in quotes},
                  source_query_evidence=source.query_evidence,freshness="FRESH",freshness_policy_identity=policy.policy_identity,
                  async_accounting=accounting,content_sha256=verified.content_sha256,quotes_sha256=verified.quotes_sha256,
                  sealed_result=sealed.status.value,immutable_conflict=False,runtime_id=context.identity.runtime_id,
                  environment=environment)
    # Round-trip through the canonical contract before it crosses the CLI boundary.
    import json
    return CaptureResult(verified,sealed,json.loads(canonical_json(evidence)))
