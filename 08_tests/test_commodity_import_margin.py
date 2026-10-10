from datetime import date, datetime, timedelta
from pathlib import Path
import sqlite3
import sys

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from agri_research_agent.commodity_import_margin import model, inputs, store
from agri_research_agent.soybean_margin import model as soybean
from agri_research_agent.soybean_margin import api_sources as provider

DAY = date(2026, 10, 12)
STAMP = datetime(2026, 10, 12, 9, 35, tzinfo=provider.SHANGHAI)
APPS = Path(__file__).resolve().parents[1]/"05_apps"
if str(APPS) not in sys.path:
    sys.path.insert(0,str(APPS))


def snapshot(commodity="canola"):
    expected = model.expected_contracts(DAY, commodity)
    return dict(schema_version=inputs.SCHEMA, commodity=commodity, business_date=DAY.isoformat(),
        captured_at=STAMP.isoformat(), fx_currency="USD/CNY",
        units=dict(cnf="USD/tonne", domestic="CNY/tonne", fx="CNY_per_USD"),
        parameters=dict(model.PROFILES[commodity]), fx_policy="soybean-spot-forward/1",
        contracts=expected, domestic={s: 2400 if s.startswith("RM") else 10000 for s in expected},
        fx_curve={"0":7., "1":6.99, "3":6.97, "6":6.94, "9":6.91, "12":6.88},
        sources=dict(domestic=dict(provider="AkShare/Sina", quotes={s:dict(quoted_at=STAMP.isoformat(),
            price_field="current_price", raw_contract="商品"+s[-4:]) for s in expected}),
            fx=dict(currency="USD/CNY", rate_kind="mid", published_at=[STAMP.isoformat()]*2)),
        calendar=dict(provider="AkShare/Sina", covered_through=DAY.isoformat(), is_trading_day=True), errors={})


@pytest.mark.parametrize("month,year,delivery", [(1,2027,5),(3,2027,5),(4,2027,9),
    (5,2027,9),(6,2027,9),(7,2027,9),(8,2028,1),(9,2028,1),(10,2028,1),(11,2027,1),(12,2027,5)])
def test_long_transit_mapping_and_soybean_cutover(month,year,delivery):
    codes = model.contracts(DAY,"canola",month)
    assert [(c.year,c.month) for c in codes] == [(year,delivery)]*2
    assert soybean.contracts(DAY,month)[1] == f"{year%100:02}{delivery:02}"


def test_future_twelve_months_and_historical_mapping_not_rewritten():
    rows = model.daily_rows(DAY,"canola",{})
    assert [r["shipment_period"] for r in rows] == [f"2026-{m:02}" for m in (11,12)]+[f"2027-{m:02}" for m in range(1,11)]
    assert [r["fx_tenor_months"] for r in rows] == list(range(1,13))
    assert soybean.contracts(date(2026,10,9),12)[1] == "2701"
    assert soybean.contracts(DAY,12)[1] == "2705"
    assert soybean.contracts(DAY,4)[0] == "2705"  # CBOT unchanged
    assert len(model.expected_contracts(DAY,"canola")) == 8
    dec = model.daily_rows(date(2026,12,15),"palm",{})
    assert dec[0]["shipment_period"] == "2027-01" and dec[-1]["shipment_period"] == "2027-12"


@pytest.mark.parametrize("month,year,delivery", [(1,2027,1),(2,2027,5),(5,2027,5),
    (6,2027,9),(9,2027,9),(10,2028,1),(11,2027,1),(12,2027,1)])
def test_short_transit_mapping(month,year,delivery):
    c, = model.contracts(DAY,"palm",month)
    assert (c.year,c.month,c.product) == (year,delivery,"P")


def test_independent_formulas_fees_units_and_missing_inputs():
    cost = 662*6.7*1.149*1.09
    result = model.calculate("canola",662,6.7,(2400,10000))
    assert result["duty_paid_cost"] == pytest.approx(cost)
    assert result["net_margin"] == pytest.approx(2400*.573+10000*.417-cost-270)
    palm = model.calculate("palm",1000,6.7,(9000,))
    assert palm["duty_paid_cost"] == pytest.approx(1000*6.7*1.09*1.09+80)
    assert palm["net_margin"] == pytest.approx(9000-palm["duty_paid_cost"])
    assert model.calculate("canola",None,6.7,(2400,10000))["net_margin"] is None
    assert model.calculate("palm",1000,6.7,(None,))["net_margin"] is None
    assert model.calculate("palm",0,6.7,(9000,))["net_margin"] == 8920
    assert model.calculate("palm",True,6.7,(9000,))["net_margin"] is None


def test_fx_same_as_soybean_spot_then_forwards_never_extrapolate():
    assert model.select_fx(snapshot()["fx_curve"],1) == (7.,False,0,0)
    assert model.select_fx(snapshot()["fx_curve"],2)[0] == pytest.approx(7.)
    assert model.select_fx({"0":7.,"3":6.97},2)[0] == 7.
    assert model.select_fx({"0":7.,"6":6.94},3)[0] is None
    assert model.select_fx({"3":6.97,"6":6.94},12)[0] is None
    with pytest.raises(ValueError, match="业务日期"):
        model.daily_rows(DAY+timedelta(days=1),"canola",{},snapshot())


def test_store_null_clear_concurrency_commodity_isolation_and_read_no_creation(tmp_path):
    path = tmp_path/"isolated"/"research.sqlite3"
    assert store.load_cnf(path,DAY,"canola") == ({},0)
    assert not path.parent.exists()
    quotes = dict.fromkeys(range(1,13)); quotes[11]=662.; quotes[12]=0.
    assert store.save_cnf(path,DAY,"canola",quotes,0,authorize=lambda _:None) == 1
    assert store.load_cnf(path,DAY,"palm") == ({},0)
    assert store.load_cnf(path,DAY,"canola") == (quotes,1)
    with pytest.raises(ValueError, match="另一会话"):
        store.save_cnf(path,DAY,"canola",quotes,0,authorize=lambda _:None)
    quotes[11]=None
    store.save_cnf(path,DAY,"canola",quotes,1,authorize=lambda _:None)
    assert store.load_cnf(path,DAY,"canola")[0][11] is None
    with pytest.raises(ValueError, match="非负"):
        store.save_cnf(path,DAY,"palm",dict.fromkeys(range(1,13),-1),0,authorize=lambda _:None)


def test_local_guard_denies_formal_context_and_path_alias(monkeypatch,tmp_path):
    monkeypatch.setenv("LOCALAPPDATA",str(tmp_path))
    monkeypatch.setenv("COMMODITY_IMPORT_LOCAL_PREVIEW","1")
    monkeypatch.delenv("MARKET_DATA_GIT_HEAD",raising=False)
    monkeypatch.delenv("MARKET_DATA_EXECUTION_GRANT",raising=False)
    target = store.local_database()
    store.authorize_local(target)
    with pytest.raises(ValueError,match="独立本地"):
        store.authorize_local(tmp_path/"foreign.sqlite3")
    monkeypatch.setenv("MARKET_DATA_GIT_HEAD","formal")
    with pytest.raises(ValueError,match="正式写入"):
        store.save_cnf(target,DAY,"canola",dict.fromkeys(range(1,13)),0,authorize=store.authorize_local)
    assert not target.exists()


def test_snapshot_revisions_identity_and_cas(tmp_path):
    path = tmp_path/"research.sqlite3"
    first = store.publish(path,snapshot(),expected_identity=None,authorize=lambda _:None)
    assert store.read_market(path,DAY,"canola") == (snapshot(),first)
    new = snapshot(); new["domestic"]["RM2705"]=2500
    second = store.publish(path,new,expected_identity=first,authorize=lambda _:None)
    with pytest.raises(ValueError,match="另一会话"):
        store.publish(path,snapshot(),expected_identity=first,authorize=lambda _:None)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM market_revisions").fetchone()[0] == 2
        db.execute("UPDATE market_revisions SET payload='{}' WHERE sha256=?",(second,))
    with pytest.raises(ValueError,match="身份验证"):
        store.read_market(path,DAY,"canola")


@pytest.mark.parametrize("change", ["date","contract","currency","stale","parameters"])
def test_snapshot_validation_rejects_wrong_identity(change):
    value = snapshot()
    if change == "date": value["business_date"]="2026-10-13"
    if change == "contract": value["contracts"]["RM2705"]="CZCE:RM:2028-05"
    if change == "currency": value["fx_currency"]="USD/CNH"
    if change == "stale": value["sources"]["fx"]["published_at"][0]="2026-10-09T09:35:00+08:00"
    if change == "parameters": value["parameters"]["tariff"]=.09
    with pytest.raises(ValueError): inputs.validate(value)


def test_capture_independent_failure_calendar_and_missing_contracts(monkeypatch):
    monkeypatch.setattr(provider,"_worker",lambda *_:[DAY.isoformat()])
    def broken_fx(*_): raise provider.SourceError("fx_missing")
    monkeypatch.setattr(provider,"fx_curve",broken_fx)
    value = snapshot()
    monkeypatch.setattr(provider,"domestic",lambda symbols:(value["domestic"],value["sources"]["domestic"],{}))
    result = inputs.capture("canola",now=STAMP)
    assert result["fx_curve"] == {} and result["domestic"]["RM2705"] == 2400
    assert result["errors"] == {"fx":"fx_missing"}
    assert all(r["net_margin"] is None for r in model.daily_rows(DAY,"canola",{11:662},result))
    monkeypatch.setattr(provider,"_worker",lambda *_:["2026-10-09"])
    with pytest.raises(provider.SourceError,match="out_of_range"):
        inputs.capture("palm",now=STAMP)


def test_local_page_no_network_and_history_without_fabricated_prices(monkeypatch,tmp_path):
    monkeypatch.setenv("LOCALAPPDATA",str(tmp_path))
    monkeypatch.setenv("COMMODITY_IMPORT_LOCAL_PREVIEW","1")
    monkeypatch.delenv("MARKET_DATA_GIT_HEAD",raising=False)
    monkeypatch.delenv("MARKET_DATA_EXECUTION_GRANT",raising=False)
    apps = Path(__file__).resolve().parents[1]/"05_apps"
    sys.path.insert(0,str(apps))
    monkeypatch.setattr(provider,"capture",lambda *_:pytest.fail("page must not fetch"))
    monkeypatch.setattr(provider,"fx_curve",lambda *_:pytest.fail("page must not fetch"))
    code = "from commodity_import_margin_page import render_commodity_import_margin_page\nfrom datetime import date\nrender_commodity_import_margin_page('canola',today=date(2026,10,12))"
    app = AppTest.from_string(code).run()
    assert not app.exception
    assert any("暂无已保存行情" in i.value for i in app.info)
    assert not store.local_database().exists()
    store.save_cnf(store.local_database(),DAY,"canola",dict.fromkeys(range(1,13),662),0,authorize=store.authorize_local)
    app.run()
    assert not app.exception
    assert any("图表保持空白" in i.value for i in app.info)
    store.publish(store.local_database(),snapshot(),expected_identity=None,authorize=store.authorize_local)
    app.run()
    assert not app.exception
    assert "plotly_chart" in [e.type for e in app.main]


def test_red_table_shows_full_contract_identity_and_missing_values():
    from commodity_import_margin_page import daily_html
    html = daily_html(model.daily_rows(DAY,"canola",{}),"canola")
    assert "CZCE:RM:2027-09" in html
    assert "background:#ba2924" in html
    assert "<td>--</td>" in html and "nan" not in html


def test_page_save_uses_editor_month_key_in_rolling_order(monkeypatch,tmp_path):
    monkeypatch.setenv("LOCALAPPDATA",str(tmp_path))
    monkeypatch.setenv("COMMODITY_IMPORT_LOCAL_PREVIEW","1")
    monkeypatch.delenv("MARKET_DATA_GIT_HEAD",raising=False)
    monkeypatch.delenv("MARKET_DATA_EXECUTION_GRANT",raising=False)
    def edited(frame, **_):
        frame=frame.copy()
        frame.loc[0,"CNF"]=1000
        frame.loc[1,"CNF"]=0
        return frame
    monkeypatch.setattr(st,"data_editor",edited)
    code = "from commodity_import_margin_page import render_commodity_import_margin_page\nfrom datetime import date\nrender_commodity_import_margin_page('palm',today=date(2026,10,12))"
    app = AppTest.from_string(code).run()
    next(b for b in app.button if b.label=="保存CNF").click().run()
    assert not app.exception
    saved,version = store.load_cnf(store.local_database(),DAY,"palm")
    assert version==1 and saved[11]==1000 and saved[12]==0 and saved[1] is None
