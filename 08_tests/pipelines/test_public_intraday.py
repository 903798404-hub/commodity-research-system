"""Real storage + public read API fixture, with fail-closed temporal gates."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from agri_research_agent.data_sources.tankan.client import LiveReadResult
from agri_research_agent.data_sources.tankan.models import ConnectionProof, QueryPlanProof
from agri_research_agent.data_sources.tankan.intraday_adapter import ExactRequest
from agri_research_agent.market_data.calendars import DeterministicBusinessDayPolicy
from agri_research_agent.market_data.intraday import MarketSession, load_intraday_snapshot, IntradaySnapshotConflictError
from agri_research_agent.pipelines.public_intraday import CapturePolicy, capture_public_intraday, PublicIntradayCaptureError
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode

DAY=date(2026,8,31)
STAMP=datetime(2026,8,31,7,30,tzinfo=timezone.utc)
REQUESTS=(ExactRequest("DCE:SOYMEAL:2027-01","M2701","DCE","SOYMEAL"),
          ExactRequest("DCE:SOYMEAL:2028-01","M2801","DCE","SOYMEAL"))


class Client:
    calls=0
    def __init__(self,stamp=STAMP,price=3000,missing=(),reason="NOT_FOUND"):
        self.stamp,self.price,self.missing,self.reason=stamp,price,missing,reason

    def read_live(self,query,identities):
        self.calls+=1
        rows=tuple(dict(requested_identity=key,price=self.price,source_updated_at=self.stamp,update_time=self.stamp)
                   for key in identities if key not in self.missing)
        absent=tuple(dict(requested_identity=key,price=None,reason=self.reason) for key in identities if key in self.missing)
        return LiveReadResult(identities,rows,absent,query,QueryPlanProof(query.sha256,len(rows),1,128,10000),
                             ConnectionProof("quanyong","test","Asia/Shanghai","on","on"),self.stamp)


def context(root):
    (root/".market-data-runtime.json").write_text(json.dumps(dict(schema_version=1,runtime_id="test",
        classification="fixture",module_id="shared-intraday",created_at=STAMP.isoformat())),encoding="utf-8")
    return RuntimeContext(RuntimeMode.FIXTURE,"shared-intraday",root)


def run(root,**kwargs):
    args=dict(client=Client(missing=("M2801",)),requested=REQUESTS,context=context(root),store_root=root/"snapshots",
              policy=CapturePolicy(DAY,MarketSession.PM,{r.instrument_id:STAMP-timedelta(hours=1) for r in REQUESTS},"fixture-policy"),
              business_day_policy=DeterministicBusinessDayPolicy(frozenset({DAY})),clock=lambda:STAMP)
    args.update(kwargs)
    return capture_public_intraday(**args)


def test_capture_partial_and_machine_evidence(tmp_path):
    result=run(tmp_path)
    evidence=result.evidence
    assert (evidence["requested_count"],evidence["available_count"],evidence["unavailable_count"])==(2,1,1)
    assert evidence["sealed_result"]=="SEALED" and evidence["environment"]=="TEST_ISOLATED_NON_PRODUCTION"
    assert evidence["async_accounting"]["summary"]["coverage"]==dict(PRESENT=1,MISSING=1,ERROR=0)
    assert evidence["async_accounting"]["summary"]["freshness"]["UNASSESSED"]==2
    assert evidence["freshness"]=="FRESH"  # separate intraday timestamp gate
    assert load_intraday_snapshot(tmp_path/"snapshots",DAY,"PM").content_sha256==result.snapshot.content_sha256


def test_idempotent_and_conflicting_capture(tmp_path):
    first=run(tmp_path)
    assert run(tmp_path).seal.status=="NO_CHANGE"
    before=(first.seal.release_dir/"quotes.json").read_bytes()
    with pytest.raises(IntradaySnapshotConflictError):
        run(tmp_path,client=Client(price=3100,missing=("M2801",)))
    assert (first.seal.release_dir/"quotes.json").read_bytes()==before


@pytest.mark.parametrize("hour",[6,14,22])
def test_session_gate_before_provider(tmp_path,hour):
    client=Client()
    # Convert Shanghai wall time to UTC.
    value=STAMP.replace(hour=hour-8) if hour>=8 else STAMP-timedelta(hours=9.5)
    with pytest.raises(PublicIntradayCaptureError):
        run(tmp_path,client=client,clock=lambda:value)
    assert client.calls==0 and not (tmp_path/"snapshots").exists()


def test_calendar_gate_before_provider(tmp_path):
    client=Client()
    with pytest.raises(PublicIntradayCaptureError):
        run(tmp_path,client=client,business_day_policy=DeterministicBusinessDayPolicy(frozenset()))
    assert client.calls==0


def test_stale_source_does_not_seal(tmp_path):
    with pytest.raises(PublicIntradayCaptureError):
        run(tmp_path,client=Client(stamp=STAMP-timedelta(hours=2)))
    assert not (tmp_path/"snapshots"/"releases").exists()


@pytest.mark.parametrize("reason",["INVALID_PRICE","SOURCE_TIMESTAMP_UNAVAILABLE","DUPLICATE_SOURCE_IDENTITY"])
def test_source_errors_are_not_nonblocking_absence(tmp_path,reason):
    with pytest.raises(PublicIntradayCaptureError):
        run(tmp_path,client=Client(missing=("M2801",),reason=reason))
    assert not (tmp_path/"snapshots"/"releases").exists()


def test_am_previous_stage_allowed_only_by_explicit_window(tmp_path):
    capture=datetime(2026,8,31,0,45,tzinfo=timezone.utc)
    night=datetime(2026,8,28,15,0,tzinfo=timezone.utc)
    policy=CapturePolicy(DAY,MarketSession.AM,{r.instrument_id:night for r in REQUESTS},"fixture-approved-night-window")
    result=run(tmp_path,client=Client(stamp=night),policy=policy,clock=lambda:capture)
    assert result.snapshot.business_date==DAY
    assert all(q.source_updated_at==night and q.source_trade_date is None for q in result.snapshot.quotes)


def test_missing_policy_identity_and_contract_expiry(tmp_path):
    with pytest.raises(PublicIntradayCaptureError):
        CapturePolicy(DAY,MarketSession.PM,{},"")
    expired=ExactRequest("DCE:SOYMEAL:2026-07","M2607","DCE","SOYMEAL")
    client=Client()
    with pytest.raises(PublicIntradayCaptureError):
        run(tmp_path,client=client,requested=(expired,),policy=CapturePolicy(DAY,MarketSession.PM,{expired.instrument_id:STAMP},"fixture"))
    assert client.calls==0


def test_daily_does_not_import_intraday():
    root=Path(__file__).resolve().parents[2]
    for name in ("03_src/agri_research_agent/pipelines/public_data_daily.py",
                 "03_src/agri_research_agent/pipelines/public_data_refresh.py",
                 "03_src/agri_research_agent/automation/full_daily_windows.py"):
        assert "intraday" not in (root/name).read_text(encoding="utf-8").lower()
