"""Explicit independent intraday CLI; never calls Public refresh or DAILY."""
import argparse
from datetime import date, datetime, timezone
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"03_src"))

from agri_research_agent.data_sources.tankan.client import TankanClient, TankanConnectionSettings
from agri_research_agent.data_sources.tankan.intraday_adapter import ExactRequest
from agri_research_agent.market_data.calendars import DeterministicBusinessDayPolicy
from agri_research_agent.market_data.intraday import MarketSession, canonical_json, _read_json
from agri_research_agent.pipelines.public_intraday import CapturePolicy, capture_public_intraday
from agri_research_agent.shared.file_identity import identify_file
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode, load_runtime_identity, assert_runtime_write


def parser():
    result=argparse.ArgumentParser(description=__doc__)
    result.add_argument("--business-date",type=date.fromisoformat,required=True)
    result.add_argument("--session",choices=("AM","PM"),required=True)
    result.add_argument("--request-file",type=Path,required=True)
    result.add_argument("--calendar-file",type=Path,required=True)
    result.add_argument("--runtime-root",type=Path,required=True)
    result.add_argument("--runtime-mode",choices=("FIXTURE","ISOLATED_DEV","PRODUCTION_WRITE"),required=True)
    result.add_argument("--expected-runtime-id",required=True)
    result.add_argument("--expected-marker-sha256",required=True)
    result.add_argument("--secret-file",type=Path,required=True)
    return result


def main(argv=None,*,client_factory=None,clock=None):
    args=parser().parse_args(argv)
    try:
        identity=load_runtime_identity(args.runtime_root)
        if (identity.runtime_id,identity.marker_sha256)!=(args.expected_runtime_id,args.expected_marker_sha256):
            raise ValueError("Runtime identity mismatch")
        mode=RuntimeMode(args.runtime_mode)
        formal=dict(formal_identity=identity,expected_runtime_id=identity.runtime_id,repository_root=ROOT) if mode is RuntimeMode.PRODUCTION_WRITE else {}
        context=RuntimeContext(mode,"shared-intraday",args.runtime_root,**formal)
        store=assert_runtime_write(context,context.runtime_root/"snapshots")
        request_identity=identify_file(args.request_file)
        calendar_identity=identify_file(args.calendar_file)
        request=_read_json(args.request_file)
        calendar=_read_json(args.calendar_file)
        if request["business_date"]!=args.business_date.isoformat() or request["session"]!=args.session:
            raise ValueError("Request date/session mismatch")
        requested=tuple(ExactRequest(**row) for row in request["requested"])
        policy=CapturePolicy(args.business_date,MarketSession(args.session),
                             {key:datetime.fromisoformat(value) for key,value in request["source_not_before"].items()},
                             request_identity.sha256)
        day_policy=DeterministicBusinessDayPolicy(frozenset(date.fromisoformat(d) for d in calendar["business_days"]),
                                                  "explicit-calendar-file/1",calendar_identity.sha256)
        if identify_file(args.request_file)!=request_identity or identify_file(args.calendar_file)!=calendar_identity:
            raise ValueError("Input identity changed during read")
        factory=client_factory or (lambda: TankanClient(TankanConnectionSettings.from_secret_file(args.secret_file)))
        with factory() as client:
            result=capture_public_intraday(client=client,requested=requested,context=context,store_root=store,
                                           policy=policy,business_day_policy=day_policy,
                                           clock=clock or (lambda:datetime.now(timezone.utc)))
        print(canonical_json(result.evidence).decode("utf-8"))
        return 0
    except Exception as exc:
        print(canonical_json({"schema_version":"public-intraday-evidence/1","status":"FAILED",
                              "business_date":args.business_date,"session":args.session,
                              "error_type":type(exc).__name__,"reason":getattr(exc,"status","ERROR"),
                              "immutable_conflict":getattr(exc,"status","")=="IMMUTABLE_CONFLICT"}).decode("utf-8"))
        return 1


if __name__=="__main__":
    raise SystemExit(main())
