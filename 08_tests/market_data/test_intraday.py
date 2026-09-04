"""Pure contract, legacy verification and isolated immutable storage tests."""
from dataclasses import replace
from datetime import date, datetime, timezone
from hashlib import sha256
import json

import pytest

from agri_research_agent.market_data import intraday as m
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode

DAY=date(2026,8,31)
STAMP=datetime(2026,8,31,7,30,tzinfo=timezone.utc)


def quote(session=m.MarketSession.PM):
    return m.IntradayQuote(DAY,session,STAMP,"DCE:SOYMEAL:2027-01","M2701","DCE","SOYMEAL",3000,
                           "LAST","CNY","CNY_PER_METRIC_TONNE","TANKAN","market.futures_live",STAMP,None,
                           m.FreshnessStatus.FRESH,{"query":{"sha":"fixture"}},STAMP)


def snapshot(session=m.MarketSession.PM):
    return m.IntradaySnapshot(DAY,session,STAMP,(quote(session),),"fixture-calendar","fixture-calendar-sha",
                              "TEST_ISOLATED_NON_PRODUCTION")


def context(tmp_path):
    (tmp_path/".market-data-runtime.json").write_text(json.dumps(dict(schema_version=1,runtime_id="test",
        classification="fixture",module_id="shared-intraday",created_at=STAMP.isoformat())),encoding="utf-8")
    return RuntimeContext(RuntimeMode.FIXTURE,"shared-intraday",tmp_path)


def test_seal_idempotence_and_independent_sessions(tmp_path):
    ctx=context(tmp_path)
    am,pm=snapshot(m.MarketSession.AM),snapshot()
    first=m.seal_intraday_snapshot(tmp_path/"snapshots",am,context=ctx)
    before={p.name:p.read_bytes() for p in first.release_dir.iterdir()}
    m.seal_intraday_snapshot(tmp_path/"snapshots",pm,context=ctx)
    assert m.seal_intraday_snapshot(tmp_path/"snapshots",am,context=ctx).status is m.SealStatus.NO_CHANGE
    assert before=={p.name:p.read_bytes() for p in first.release_dir.iterdir()}
    assert m.load_latest_intraday_snapshot(tmp_path/"snapshots","AM").release_id=="2026-08-31-AM"
    assert m.load_latest_intraday_snapshot(tmp_path/"snapshots","PM").release_id=="2026-08-31-PM"


def test_conflict_never_overwrites(tmp_path):
    ctx=context(tmp_path)
    value=snapshot()
    result=m.seal_intraday_snapshot(tmp_path,value,context=ctx)
    before=(result.release_dir/"quotes.json").read_bytes()
    with pytest.raises(m.IntradaySnapshotConflictError):
        m.seal_intraday_snapshot(tmp_path,replace(value,quotes=(replace(quote(),price=3100),)),context=ctx)
    assert (result.release_dir/"quotes.json").read_bytes()==before


def test_ordering_and_nested_immutability():
    first=quote()
    second=replace(first,instrument_id="DCE:SOYOIL:2027-01",contract_code="Y2701",product="SOYOIL")
    a=replace(snapshot(),quotes=(first,second))
    b=replace(snapshot(),quotes=(second,replace(first,provenance={"query":{"sha":"fixture"}})))
    assert a.content_sha256==b.content_sha256 and a.quotes_sha256==b.quotes_sha256
    with pytest.raises(TypeError):
        first.provenance["query"]["sha"]="changed"
    assert m.canonical_json({"b":2,"a":1})==m.canonical_json({"a":1,"b":2})


@pytest.mark.parametrize("change",[{"price":0},{"price":None},{"price":True},{"price":float("nan")},
    {"contract_code":"M2801"},{"source_updated_at":datetime(2026,8,31)},
    {"instrument_id":"DCE:SOYMEAL:CONTINUOUS_MAIN"},{"retrieved_at":datetime(2026,8,30,tzinfo=timezone.utc)}])
def test_bad_quote_rejected(change):
    with pytest.raises(m.IntradaySnapshotValidationError):
        replace(quote(),**change)


def test_preview_duplicate_and_environment_rejection(tmp_path):
    with pytest.raises(m.IntradaySnapshotValidationError):
        replace(snapshot(),environment="PREVIEW")
    with pytest.raises(m.IntradaySnapshotValidationError):
        replace(snapshot(),quotes=(quote(),quote()))
    ctx=context(tmp_path)
    with pytest.raises(m.IntradaySnapshotValidationError):
        m.seal_intraday_snapshot(tmp_path,replace(snapshot(),environment="FORMAL"),context=ctx)


@pytest.mark.parametrize("field,value",[("session","AM"),("release_id","other"),("immutable",False),
    ("status","PREVIEW"),("record_count",0),("content_sha256","0"*64),("environment","FORMAL")])
def test_manifest_tampering_fails(tmp_path,field,value):
    sealed=m.seal_intraday_snapshot(tmp_path,snapshot(),context=context(tmp_path))
    path=sealed.release_dir/"manifest.json"
    data=json.loads(path.read_bytes());data[field]=value
    path.write_text(json.dumps(data),encoding="utf-8")
    with pytest.raises(m.IntradaySnapshotValidationError):
        m.load_intraday_snapshot(tmp_path,DAY,"PM",expected_environment="TEST_ISOLATED_NON_PRODUCTION")


def test_legacy_v1_read_without_rewrite(tmp_path):
    value=replace(snapshot(),schema_version="1",quotes=(replace(quote(),retrieved_at=None),))
    directory=tmp_path/"releases"/value.release_id;directory.mkdir(parents=True)
    rows=[q.as_dict() for q in value.quotes]
    manifest=m._manifest(value);manifest.pop("status")
    for name,data in (("quotes.json",rows),("manifest.json",manifest)):
        (directory/name).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    before={p.name:sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}
    loaded=m.load_intraday_snapshot(tmp_path,DAY,"PM")
    assert loaded.content_sha256==value.content_sha256
    assert loaded.quotes[0].retrieved_at is None
    assert before=={p.name:sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}


def test_unavailable_keeps_exact_identity():
    absent=m.IntradayUnavailableInstrument("DCE:SOYMEAL:2028-01","M2801","DCE","SOYMEAL",
        m.InstrumentAvailabilityStatus.CONTRACT_NOT_AVAILABLE,"NOT_FOUND",{"query":"fixture"})
    value=replace(snapshot(),unavailable_instruments=(absent,))
    assert absent.key in value.requested_identities
    assert "price" not in absent.as_dict()
    with pytest.raises(m.IntradaySnapshotNotFoundError):
        m.quote_by_instrument(value,*absent.key)


def test_context_and_missing_store_fail_closed(tmp_path):
    with pytest.raises(Exception):
        m.seal_intraday_snapshot(tmp_path,snapshot(),context=None)
    assert not (tmp_path/"releases").exists()
    with pytest.raises(m.IntradaySnapshotNotFoundError):
        m.load_intraday_snapshot(tmp_path,DAY,"PM")
