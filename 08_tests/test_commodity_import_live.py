from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from agri_research_agent.commodity_import_margin import live, model, store
from agri_research_agent.commodity_import_margin.manual import import_manual_cnf
APPS = Path(__file__).resolve().parents[1]/"05_apps"
if str(APPS) not in sys.path: sys.path.insert(0,str(APPS))
import commodity_import_margin_page as page
NOW = datetime(2026,10,10,17,0,tzinfo=live.SHANGHAI)


def setup_local(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA",str(tmp_path))
    monkeypatch.setenv("COMMODITY_IMPORT_LOCAL_PREVIEW","1")
    monkeypatch.delenv("MARKET_DATA_GIT_HEAD",raising=False)
    monkeypatch.delenv("MARKET_DATA_EXECUTION_GRANT",raising=False)


def fx_bodies():
    return dict(spot=dict(data=dict(showDateCN="2026-10-10 03:00:00"),records=[dict(ccyPair="USD/CNY",bidPrc="---",askPrc="---")]),
        trade=dict(data=dict(lastDate="2026-10-10 3:00",spotPriceStr="6.6929")),
        swap=dict(data=dict(showDateCN="2026-10-10 03:00:00"),records=[dict(ccyPair="USD/CNY",label_1M="-156/-147",label_3M="-472/-459",label_6M="-973/-935",label_9M="-1503/-1455",label_1Y="-2053/-2005")]))


def domestic_text(symbol, price=9742, *, suffix=None, stamp="2026-10-09"):
    f=["0"]*28;f[0]="商品"+(suffix or symbol[-4:]);f[1]="230000";f[8]=str(price);f[17]=stamp
    return f'var hq_str_nf_{symbol}="'+",".join(f)+'";'


def mock_transport(monkeypatch):
    def get(url, **kwargs):
        if url.startswith("https://hq.sinajs.cn/"):
            symbols=[s.removeprefix("nf_") for s in url.split("list=")[1].split(",")]
            return "\n".join(domestic_text(s,9742 if s.startswith("P") else (2400 if s.startswith("RM") else 10000)) for s in symbols if not s.endswith("2801"))
        return json.dumps(fx_bodies()[next(k for k,u in live.FX_URLS.items() if u==url)])
    monkeypatch.setattr(live,"get_text",get)


def test_available_onshore_trade_spot_forwards_and_soybean_tenor_policy():
    curve,evidence,errors=live.parse_fx(fx_bodies(),NOW)
    assert not errors and curve["0"]==6.6929 and evidence["rate_kind"]=="latest_trade"
    assert curve["3"]==pytest.approx(6.6929-.04655)
    assert model.select_fx(curve,1)==(6.6929,False,0,0)
    assert model.select_fx(curve,2)==(6.6929,False,0,0)
    assert model.select_fx(curve,3)[0]==curve["3"]
    assert model.select_fx(curve,4)[1]
    b=fx_bodies();b["spot"]["records"][0].update(bidPrc="6.6928",askPrc="6.6932")
    curve,source,_=live.parse_fx(b,NOW)
    assert curve["0"]==pytest.approx(6.6930) and source["rate_kind"]=="spot_bid_ask_mid"
    b.pop("swap");curve,_,errors=live.parse_fx(b,NOW)
    assert curve.keys()=={"0"} and errors["fx_forward"]
    assert model.select_fx(curve,3)[0] is None


def test_domestic_contract_year_missing_and_source_date_preserved():
    text=domestic_text("P2701")+'\n'+domestic_text("P2705",suffix="2605")+'\nvar hq_str_nf_P2801="";'
    prices,source,errors=live.parse_domestic(text,["P2701","P2705","P2801"],NOW)
    assert prices=={"P2701":9742.,"P2705":None,"P2801":None}
    assert source["quotes"]["P2701"]["quoted_at"]=="2026-10-09T23:00:00+08:00"
    assert set(errors)=={"P2705","P2801"}
    prices,_,_=live.parse_domestic(domestic_text("P2701",stamp="2026-09-01"),["P2701"],NOW)
    assert prices["P2701"] is None


def test_capture_inputs_atomic_save_revision_and_failure_isolation(monkeypatch,tmp_path):
    mock_transport(monkeypatch)
    value=live.capture("palm",now=NOW)
    assert live.validate(value)==NOW.date() and value["domestic"]["P2801"] is None
    path=tmp_path/"research.sqlite3";quotes=dict.fromkeys(range(1,13));quotes[11]=1169.
    store.save_cnf(path,NOW.date(),"palm",quotes,0,authorize=lambda _:None,market_snapshot=value)
    assert store.read_market(path,NOW.date(),"palm")[0]==value
    assert model.daily_rows(NOW.date(),"palm",quotes,value)[0]["net_margin"]==pytest.approx(9742-(1169*6.6929*1.09*1.09+80))
    with pytest.raises(ValueError,match="另一会话"):
        store.save_cnf(path,NOW.date(),"palm",quotes,1,authorize=lambda _:None,market_snapshot=value)
    assert store.load_cnf(path,NOW.date(),"palm")[1]==1
    def broken(*args,**kwargs): raise live.LiveError("source unavailable")
    monkeypatch.setattr(live,"get_text",broken)
    failed=live.capture("canola",now=NOW)
    assert failed["fx_curve"]=={} and all(v is None for v in failed["domestic"].values())
    assert all(r["net_margin"] is None for r in model.daily_rows(NOW.date(),"canola",{11:662},failed))


@pytest.mark.parametrize("change",["currency","date","contract","stale","parameters","altered_spot","altered_forward"])
def test_live_validation_rejects_invalid_inputs(monkeypatch,change):
    mock_transport(monkeypatch);v=live.capture("palm",now=NOW)
    if change=="currency":v["fx_currency"]="USD/CNH"
    if change=="date":v["business_date"]="2026-10-09"
    if change=="contract":v["contracts"]["P2701"]="DCE:P:2028-01"
    if change=="stale":v["sources"]["fx"]["published_at"][0]="2026-09-01T03:00:00+08:00"
    if change=="parameters":v["parameters"]["port_fee"]=50
    if change=="altered_spot":v["fx_curve"]["0"]+=.1
    if change=="altered_forward":v["fx_curve"]["3"]+=.1
    with pytest.raises(ValueError):live.validate(v)


@pytest.mark.parametrize("commodity",["canola","palm"])
def test_page_cnfs_refresh_calculation_and_save_without_bmd(monkeypatch,tmp_path,commodity):
    setup_local(monkeypatch,tmp_path);mock_transport(monkeypatch)
    value=live.capture(commodity,now=NOW);calls=[];callbacks=[]
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW
    monkeypatch.setattr(page,"datetime",Clock)
    monkeypatch.setattr(page,"capture_live",lambda c:calls.append(c) or deepcopy(value))
    def editor(frame,**kwargs):
        callbacks.append((kwargs["on_change"],kwargs["args"]))
        frame=frame.copy();frame.loc[0,"CNF"]=1169 if commodity=="palm" else 662
        return frame
    monkeypatch.setattr(st,"data_editor",editor)
    code=f"from commodity_import_margin_page import render_commodity_import_margin_page\nfrom datetime import date\nrender_commodity_import_margin_page('{commodity}',today=date(2026,10,10))"
    app=AppTest.from_string(code).run()
    assert not app.exception and callbacks[-1]==(page._refresh_live,(commodity,NOW.date())) and not calls
    next(b for b in app.button if b.label=="刷新国内盘面与汇率").click().run()
    assert not app.exception and calls==[commodity]
    assert not any("BMD" in h.value for h in app.subheader)
    next(b for b in app.button if b.label=="保存CNF").click().run()
    assert not app.exception and calls==[commodity]
    saved,version=store.load_cnf(store.local_database(),NOW.date(),commodity)
    assert version==1
    market,identity=store.read_market(store.local_database(),NOW.date(),commodity)
    assert market==value and identity and model.daily_rows(NOW.date(),commodity,saved,market)[0]["net_margin"] is not None


def test_historical_editor_preserves_saved_market_and_failure_clears_current(monkeypatch,tmp_path):
    setup_local(monkeypatch,tmp_path);mock_transport(monkeypatch)
    value=live.capture("palm",now=NOW)
    store.publish(store.local_database(),value,authorize=store.authorize_local,expected_identity=None)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW+timedelta(days=2)
    monkeypatch.setattr(page,"datetime",Clock)
    def forbidden(*args,**kwargs):raise AssertionError("历史编辑不能抓取当前行情")
    monkeypatch.setattr(page,"capture_live",forbidden)
    code="from commodity_import_margin_page import _refresh_live,render_commodity_import_margin_page\nfrom datetime import date\n_refresh_live('palm',date(2026,10,10))\nrender_commodity_import_margin_page('palm',today=date(2026,10,10))"
    # Render's today is the actual selected view date; historical determination must use the real clock.
    app=AppTest.from_string(code.replace("today=date(2026,10,10)","today=date(2026,10,12)")).run()
    app.date_input[0].set_value(NOW.date()).run()
    assert not app.exception
    assert any("2026-10-09T23:00" in c.value for c in app.caption)
    class CurrentClock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW
    monkeypatch.setattr(page,"datetime",CurrentClock)
    def failed(*args,**kwargs):raise live.LiveError("行情连接失败")
    monkeypatch.setattr(page,"capture_live",failed)
    code="from commodity_import_margin_page import _refresh_live,render_commodity_import_margin_page\nfrom datetime import date\n_refresh_live('palm',date(2026,10,10))\nrender_commodity_import_margin_page('palm',today=date(2026,10,10))"
    app=AppTest.from_string(code).run()
    assert not app.exception and any("行情连接失败" in w.value for w in app.warning)
    assert not any("国内合约报价时间" in c.value for c in app.caption)


def test_current_codex_entry_refreshes_and_historical_entry_does_not(monkeypatch,tmp_path):
    import agri_research_agent.commodity_import_margin.manual as manual
    mock_transport(monkeypatch);value=live.capture("canola",now=NOW);calls=[]
    monkeypatch.setattr(manual,"capture",lambda c:calls.append(c) or value)
    request=tmp_path/"quotes.json"
    request.write_text(json.dumps(dict(business_date=NOW.date().isoformat(),commodity="canola",unit="USD/tonne",source="验收用报价",quotes={"2026-11":662})),encoding="utf-8")
    db=tmp_path/"quotes.sqlite3"
    manual.import_manual_cnf(db,request,expected_version=0,authorize=lambda _:None,now=NOW,refresh_current=True)
    assert calls==["canola"] and store.read_market(db,NOW.date(),"canola")[0]==value
    manual.import_manual_cnf(db,request,expected_version=1,authorize=lambda _:None,now=NOW+timedelta(days=2),refresh_current=True)
    assert calls==["canola"]


def manual_request(tmp_path):
    value = dict(business_date="2026-10-12", commodity="canola", unit="USD/tonne",
                 source="用户提供的当日报价", quotes={"2026-11":662., "2027-01":0.})
    request = tmp_path / "manual.json"
    request.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return request, value


def test_manual_partial_quotes_exact_ship_year_cas_and_retained_audit(tmp_path):
    request, value = manual_request(tmp_path)
    database = tmp_path / "test.sqlite3"
    now = datetime(2026,10,12,14,0,tzinfo=live.SHANGHAI)
    assert import_manual_cnf(database, request, expected_version=0, authorize=lambda _:None, now=now) == 1
    quotes, _ = store.load_cnf(database, date(2026,10,12), "canola")
    assert quotes[11] == 662. and quotes[1] == 0 and quotes[12] is None
    audit = store.cnf_provenance(database, date(2026,10,12), "canola", 1)
    assert audit["entry_method"] == "manual_codex" and audit["request"] == value
    assert audit["quote_time"] is None and len(audit["request_sha256"]) == 64
    value["quotes"] = {"2026-11": None}
    request.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="版本"):
        import_manual_cnf(database, request, expected_version=0, authorize=lambda _:None, now=now)
    import_manual_cnf(database, request, expected_version=1, authorize=lambda _:None, now=now)
    assert store.load_cnf(database,date(2026,10,12),"canola")[0][11] is None
    assert store.cnf_provenance(database,date(2026,10,12),"canola",1) == audit


@pytest.mark.parametrize("change", ["year", "month_only", "unit", "future_date", "source", "negative"])
def test_invalid_manual_request_never_creates_database(tmp_path, change):
    request, value = manual_request(tmp_path)
    if change == "year": value["quotes"] = {"2026-01":662}
    if change == "month_only": value["quotes"] = {"1":662}
    if change == "unit": value["unit"] = "MYR/tonne"
    if change == "future_date": value["business_date"] = "2026-10-13"
    if change == "source": value["source"] = ""
    if change == "negative": value["quotes"] = {"2026-11":-1}
    request.write_text(json.dumps(value), encoding="utf-8")
    database = tmp_path / "test.sqlite3"
    with pytest.raises(ValueError):
        import_manual_cnf(database,request,expected_version=0,authorize=lambda _:None,
                          now=datetime(2026,10,12,14,0,tzinfo=live.SHANGHAI))
    assert not database.exists()


def test_windows_virtualized_cache_is_canonical_but_linked_root_denied(monkeypatch, tmp_path):
    setup_local(monkeypatch, tmp_path)
    cache = tmp_path / "market-data-runtime"
    canonical = tmp_path / "Packages" / "Codex" / "LocalCache" / "market-data-runtime"
    real_resolve = Path.resolve
    def resolve(path, *args, **kwargs):
        if path == cache:
            return canonical
        return real_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    path = store.local_database()
    assert path == canonical / "canola-palm-local" / "research.sqlite3"
    store.authorize_local(path)
    monkeypatch.setattr(Path, "is_junction", lambda path: path == cache)
    with pytest.raises(ValueError, match="别名"):
        store.local_database()
