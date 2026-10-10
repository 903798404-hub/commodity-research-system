from copy import deepcopy
from datetime import date
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from agri_research_agent.commodity_import_margin import history, store


def bundle(tmp_path, commodity="canola"):
    source = tmp_path / "original.xlsx"
    source.write_bytes(b"read-only workbook test identity")
    value = dict(schema_version=history.SCHEMA, commodity=commodity,
        imported_at="2026-10-10T17:40:00+08:00",
        source=dict(original_path=str(source), filename=source.name, sheet="source",
                    size=source.stat().st_size, sha256=hashlib.sha256(source.read_bytes()).hexdigest()),
        units=dict(cnf="USD/tonne", profit="CNY/tonne", fx="CNH_per_USD"),
        semantics="source-cached-values; current-month-is-current-year; continuous-contract-prices",
        rows=[dict(business_date="2020-01-02", shipment_month=1, shipment_period="2020-01",
            source_tenor_months=0, cnf_usd_per_tonne=808., net_margin=-310.72622,
            fx_value=6.9592, duty_paid_cost=6760.72622, meal_price=None, oil_price=6450.,
            profit_quality="verified", source_cells={"cnf":"B3","profit":"N3"},
            source_formulas={"profit":"=BW3-AY3"}, source_errors={}, domestic_contract_year=None),
              dict(business_date="2020-01-03", shipment_month=1, shipment_period="2020-01",
            source_tenor_months=0, cnf_usd_per_tonne=0., net_margin=None,
            fx_value=None, duty_paid_cost=None, meal_price=None, oil_price=None,
            profit_quality="missing", source_cells={"cnf":"B4","profit":"N4"},
            source_formulas={"profit":"=BW4-AY4"}, source_errors={"fx":"#N/A"}, domestic_contract_year=None)])
    return value, source


def test_history_frozen_identity_archive_and_no_operational_inputs(tmp_path):
    path = tmp_path / "isolated" / "research.sqlite3"
    assert history.read_history(path,"canola",date(2026,10,10)) == (None,None)
    assert not path.parent.exists()
    value, source = bundle(tmp_path)
    before = source.read_bytes()
    rev = history.publish_history(path,value,source,expected_revision=None,authorize=lambda _:None)
    actual, identity = history.read_history(path,"canola",date(2026,10,10))
    assert actual == value and identity == rev
    assert actual["rows"][0]["net_margin"] == -310.72622
    assert actual["rows"][1]["cnf_usd_per_tonne"] == 0
    assert actual["rows"][1]["fx_value"] is None
    assert actual["rows"][0]["domestic_contract_year"] is None
    assert source.read_bytes() == before
    assert (path.parent/"historical-sources"/f"{value['source']['sha256']}.xlsx").read_bytes() == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM cnf_day").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM market_current").fetchone()[0] == 0
    filtered,_ = history.read_history(path,"canola",date(2020,1,2))
    assert len(filtered["rows"]) == 1
    assert history.read_history(path,"palm",date(2026,10,10)) == (None,None)


def test_history_revisions_compare_and_swap_and_tamper_fail_closed(tmp_path):
    path=tmp_path/"local"/"research.sqlite3"
    value,source=bundle(tmp_path)
    first=history.publish_history(path,value,source,expected_revision=None,authorize=lambda _:None)
    changed=deepcopy(value); changed["imported_at"]="2026-10-10T17:41:00+08:00"
    with pytest.raises(ValueError,match="另一会话"):
        history.publish_history(path,changed,source,expected_revision=None,authorize=lambda _:None)
    second=history.publish_history(path,changed,source,expected_revision=first,authorize=lambda _:None)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM excel_history_revisions").fetchone()[0] == 2
        assert db.execute("SELECT payload FROM excel_history_revisions WHERE revision=?",(first,)).fetchone()[0].encode()==history.encode(value)
        db.execute("UPDATE excel_history_revisions SET payload='{}' WHERE revision=?",(second,))
    with pytest.raises(ValueError,match="身份"):
        history.read_history(path,"canola",date(2026,10,10))


def test_source_change_and_local_guard_prevent_publication(tmp_path):
    value,source=bundle(tmp_path)
    path=tmp_path/"local"/"research.sqlite3"
    with pytest.raises(ValueError,match="保存入口"):
        history.publish_history(path,value,source,expected_revision=None,authorize=store.authorize_local)
    assert not path.parent.exists()
    source.write_bytes(b"changed")
    with pytest.raises(ValueError,match="已变化"):
        history.publish_history(path,value,source,expected_revision=None,authorize=lambda _:None)
    assert not path.parent.exists()


@pytest.mark.parametrize("mutation",["duplicate","future","new_current_month","new_currency","nan","negative","invented_contract_year","missing_quality"])
def test_history_rejects_invalid_records(tmp_path,mutation):
    value,_=bundle(tmp_path)
    if mutation=="duplicate": value["rows"].append(deepcopy(value["rows"][0]))
    elif mutation=="future": value["rows"][0]["business_date"]="2027-01-02"
    elif mutation=="new_current_month": value["rows"][0]["shipment_period"]="2021-01"
    elif mutation=="new_currency": value["units"]["fx"]="CNY_per_USD"
    elif mutation=="nan": value["rows"][0]["fx_value"]=float("nan")
    elif mutation=="invented_contract_year": value["rows"][0]["domestic_contract_year"]=2020
    elif mutation=="missing_quality": value["rows"][1]["profit_quality"]="verified"
    else: value["rows"][0]["cnf_usd_per_tonne"]=-1
    with pytest.raises(ValueError): history.validate(value)


def test_page_history_does_not_fetch_or_recalculate_and_oil_is_separate(monkeypatch,tmp_path):
    monkeypatch.setenv("LOCALAPPDATA",str(tmp_path))
    monkeypatch.setenv("COMMODITY_IMPORT_LOCAL_PREVIEW","1")
    monkeypatch.delenv("MARKET_DATA_GIT_HEAD",raising=False)
    monkeypatch.delenv("MARKET_DATA_EXECUTION_GRANT",raising=False)
    apps=Path(__file__).resolve().parents[1]/"05_apps"
    sys.path.insert(0,str(apps))
    import commodity_import_margin_page as page
    monkeypatch.setattr(page,"capture_live",lambda *_:pytest.fail("history must not fetch"))
    for kind in ("canola","canola_oil"):
        value,source=bundle(tmp_path,kind)
        if kind=="canola_oil": value["rows"][0]["net_margin"]=123.45
        history.publish_history(store.local_database(),value,source,expected_revision=None,authorize=store.authorize_local)
    app=AppTest.from_string("from commodity_import_margin_page import render_commodity_import_margin_page\nfrom datetime import date\nrender_commodity_import_margin_page('canola',today=date(2026,10,10))").run()
    assert not app.exception
    assert any("出粕58%" in i.value for i in app.caption)
    assert any("离岸USD/CNH" in i.value for i in app.caption)
    assert any("暂无已保存行情" in i.value for i in app.info)
    details=next(d.value for d in app.dataframe if "原表利润 (元/吨)" in d.value.columns)
    assert details.loc[details["报价日期"].eq("2020-01-02"),"原表利润 (元/吨)"].iloc[0] == -310.72622
    next(s for s in app.selectbox if s.label=="历史品种").select("canola_oil").run()
    assert not app.exception
    assert any("直接进口利润" in i.value for i in app.caption)
    details=next(d.value for d in app.dataframe if "原表利润 (元/吨)" in d.value.columns)
    assert details.loc[details["报价日期"].eq("2020-01-02"),"原表利润 (元/吨)"].iloc[0] == 123.45
    assert len(app.get("plotly_chart"))==12


@pytest.mark.parametrize("commodity", ["canola", "palm"])
def test_three_chart_default_replaces_previous_preview_layout(monkeypatch, tmp_path, commodity):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("COMMODITY_IMPORT_LOCAL_PREVIEW", "1")
    monkeypatch.delenv("MARKET_DATA_GIT_HEAD", raising=False)
    monkeypatch.delenv("MARKET_DATA_EXECUTION_GRANT", raising=False)
    value, source = bundle(tmp_path, commodity)
    history.publish_history(store.local_database(), value, source, expected_revision=None, authorize=store.authorize_local)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "05_apps"))
    app = AppTest.from_string(f"from commodity_import_margin_page import render_commodity_import_margin_page\nfrom datetime import date\nrender_commodity_import_margin_page('{commodity}',today=date(2026,10,10))", default_timeout=15)
    app.session_state[f"season-columns-excel-{commodity}"] = 1
    app.run()
    assert not app.exception
    layout = next(s for s in app.selectbox if s.label == "每行图数")
    assert layout.value == 3 and len(app.get("plotly_chart")) == 12
    layout.select(2).run()
    assert not app.exception
    assert next(s for s in app.selectbox if s.label == "每行图数").value == 2


def test_history_cli_uses_authorized_store_source_hash_and_revision_cas(monkeypatch, tmp_path):
    import importlib.util
    import json
    value, source = bundle(tmp_path)
    request = tmp_path / "history.json"
    request.write_text(json.dumps(value), encoding="utf-8")
    path = tmp_path / "authorized" / "research.sqlite3"
    cli_path = Path(__file__).resolve().parents[1] / "04_scripts/import_profit/import_commodity_history.py"
    spec = importlib.util.spec_from_file_location("commodity_history_cli", cli_path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    calls = []
    monkeypatch.setattr(cli, "database_path", lambda: path)
    monkeypatch.setattr(cli, "authorize_write", lambda p: calls.append(p))
    monkeypatch.setattr(sys, "argv", [str(cli_path), "--history", str(request), "--source-file", str(source)])
    cli.main()
    saved, revision = history.read_history(path, "canola", date(2026,10,10))
    assert saved == value and calls and set(calls) == {path}
    with pytest.raises(ValueError, match="另一会话"):
        cli.main()
    assert history.read_history(path, "canola", date(2026,10,10))[1] == revision
    source.write_bytes(b"changed source")
    with pytest.raises(ValueError, match="原始文件已变化"):
        cli.main()
