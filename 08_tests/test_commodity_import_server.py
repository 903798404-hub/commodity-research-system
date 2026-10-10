"""Server storage, quote revision isolation and new hedge cutover regressions."""
from copy import deepcopy
from datetime import date, datetime
import json
import sqlite3
from types import SimpleNamespace
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from agri_research_agent.commodity_import_margin import runtime, live, store, model, manual
from agri_research_agent.soybean_margin import runtime as soybean_runtime, store as soybean_store
from agri_research_agent.soybean_margin.model import contracts as soybean_contracts
from agri_research_agent.shared.runtime_context import RuntimeClassification

NOW = datetime(2026, 10, 12, 9, 30, tzinfo=live.SHANGHAI)


def server(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("COMMODITY_IMPORT_SERVER_ENABLED", "1")
    monkeypatch.setenv("COMMODITY_IMPORT_ALLOW_SAVE", "1")
    monkeypatch.setenv("MARKET_DATA_GIT_HEAD", "a" * 40)
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    identity = SimpleNamespace(classification=RuntimeClassification.CANDIDATE_VALIDATION,
                               module_id="shared-intraday")
    monkeypatch.setattr(runtime, "load_runtime_identity", lambda _: identity)
    calls = []
    monkeypatch.setattr(runtime, "establish_application_service_context",
                        lambda **kw: calls.append(kw) or "verified-service")
    monkeypatch.setattr(runtime, "assert_runtime_write",
                        lambda ctx, p: calls.append((ctx, p)))
    path = tmp_path / runtime.STORAGE / "research.sqlite3"
    (tmp_path / runtime.STORAGE.parent).mkdir(parents=True)
    return path, calls, identity


def quotes(monkeypatch, commodity="canola"):
    def transport(url, **kw):
        if url.startswith("https://hq.sinajs.cn/"):
            records = []
            for symbol in model.expected_contracts(NOW.date(), commodity):
                f = [""] * 44
                f[0], f[1], f[8], f[17] = "商品"+symbol[-4:], "092500", "8000", NOW.date().isoformat()
                records.append(f'var hq_str_nf_{symbol}="' + ",".join(f) + '";')
            return "\n".join(records)
        body = dict(data=dict(showDateCN="2026-10-12 09:25:00"), records=[dict(
            ccyPair="USD/CNY", bidPrc="6.99", askPrc="7.01", **{
                "label_"+m:"-10/10" for m in ("1M","3M","6M","9M","1Y")})])
        if url == live.FX_URLS["trade"]:
            body = dict(data=dict(lastDate="2026-10-12 09:25", spotPriceStr="7"))
        return json.dumps(body)
    monkeypatch.setattr(live, "get_text", transport)
    return live.capture(commodity, now=NOW)


def test_protected_server_store_needs_no_windows_path_and_shares_only_declared_db(monkeypatch, tmp_path):
    path, calls, _ = server(monkeypatch, tmp_path)
    selected, authorize, allowed = runtime.storage_context(server=True)
    assert selected == path and allowed and authorize is runtime.authorize_server
    assert calls[0] == dict(service_id="spread-dashboard", module_id="shared-intraday", runtime_root=tmp_path)
    assert not path.exists()
    with pytest.raises(ValueError, match="独立业务库"):
        authorize(tmp_path / "research.sqlite3")


def test_opt_in_and_preview_flags_do_not_bypass_server_credential(monkeypatch, tmp_path):
    path, _, identity = server(monkeypatch, tmp_path)
    monkeypatch.setenv("SOYBEAN_MARGIN_LOCAL_PREVIEW", "1")
    monkeypatch.delenv("COMMODITY_IMPORT_SERVER_ENABLED")
    with pytest.raises(ValueError, match="尚未启用"):
        runtime.authorize_server(path)
    monkeypatch.setenv("COMMODITY_IMPORT_SERVER_ENABLED", "1")
    monkeypatch.setenv("COMMODITY_IMPORT_ALLOW_SAVE", "1")
    identity.module_id = "other-service"
    with pytest.raises(ValueError, match="身份或模块"):
        runtime.authorize_server(path)
    identity.module_id = "shared-intraday"
    def denied(**_):
        raise RuntimeError("protected credential missing")
    monkeypatch.setattr(runtime, "establish_application_service_context", denied)
    monkeypatch.setattr(runtime, "assert_runtime_write", lambda *_: None)
    with pytest.raises(RuntimeError, match="credential missing"):
        store.save_cnf(path, NOW.date(), "palm", dict.fromkeys(range(1,13)), 0, authorize=runtime.authorize_server)
    assert not path.exists()


def test_server_switch_without_deployed_identity_never_falls_back_to_local(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("COMMODITY_IMPORT_SERVER_ENABLED", "1")
    monkeypatch.setenv("COMMODITY_IMPORT_ALLOW_SAVE", "1")
    for name in ("MARKET_DATA_GIT_HEAD", "MARKET_DATA_EXECUTION_GRANT"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="受保护"):
        runtime.storage_context()
    assert not (tmp_path / "market-data-runtime").exists()


@pytest.mark.parametrize("first", ["soybean", "canola"])
def test_coexisting_tables_keep_all_commodity_cnf_versions_independent(monkeypatch, tmp_path, first):
    path, _, _ = server(monkeypatch, tmp_path)
    cnf = dict.fromkeys(range(1,13)); cnf[11] = 0.
    if first == "soybean":
        soybean_store.save(path, NOW.date(), "brazil", cnf, 0)
        assert store.load_cnf(path, NOW.date(), "canola") == ({}, 0)
        assert store.read_market(path, NOW.date(), "canola") == (None, None)
        assert store.history_dates(path, "canola", NOW.date()) == []
    else:
        store.save_cnf(path, NOW.date(), "canola", cnf, 0, authorize=runtime.authorize_server)
        assert soybean_store.load(path, NOW.date(), "brazil") == ({}, 0)
        assert soybean_store.read_all(path) == []
    if first == "soybean":
        store.save_cnf(path, NOW.date(), "canola", cnf, 0, authorize=runtime.authorize_server)
    else:
        soybean_store.save(path, NOW.date(), "brazil", cnf, 0)
    store.save_cnf(path, NOW.date(), "palm", cnf, 0, authorize=runtime.authorize_server)
    assert soybean_store.load(path, NOW.date(), "brazil") == (cnf, 1)
    assert store.load_cnf(path, NOW.date(), "canola") == (cnf, 1)
    assert store.load_cnf(path, NOW.date(), "palm") == (cnf, 1)


def test_cnf_binds_immutable_quote_and_failed_attempt_does_not_restore_old_quote(monkeypatch, tmp_path):
    path, _, _ = server(monkeypatch, tmp_path)
    value = quotes(monkeypatch)
    values = dict.fromkeys(range(1,13), 662.)
    store.save_cnf(path, NOW.date(), "canola", values, 0, authorize=runtime.authorize_server, market_snapshot=value)
    old, sha = store.read_market(path, NOW.date(), "canola", revision=1)
    new = deepcopy(value); new["captured_at"] = "2026-10-12T09:31:00+08:00"
    store.publish(path, new, expected_identity=sha, authorize=runtime.authorize_server)
    assert store.read_market(path, NOW.date(), "canola", revision=1) == (old, sha)
    store.save_cnf(path, NOW.date(), "canola", values, 1, authorize=runtime.authorize_server, bind_market=True)
    assert store.read_market(path, NOW.date(), "canola", revision=2) == (None, None)
    assert store.read_market(path, NOW.date(), "canola", revision=1) == (old, sha)
    # A conflict changes neither CNF nor the existing snapshot pointer.
    current = store.read_market(path, NOW.date(), "canola")
    with pytest.raises(ValueError, match="另一会话"):
        store.save_cnf(path, NOW.date(), "canola", values, 2, authorize=runtime.authorize_server,
                       market_snapshot=value, expected_identity=sha)
    assert store.load_cnf(path, NOW.date(), "canola")[1] == 2
    assert store.read_market(path, NOW.date(), "canola") == current
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM market_revisions WHERE sha256=?", (sha,))
    with pytest.raises(ValueError, match="修订缺失"):
        store.read_market(path, NOW.date(), "canola", revision=1)


def test_manual_server_entry_captures_current_only_and_saves_cnf_on_failure(monkeypatch, tmp_path):
    path, _, _ = server(monkeypatch, tmp_path)
    request = tmp_path / "quotes.json"
    request.write_text(json.dumps(dict(business_date=NOW.date().isoformat(), commodity="palm",
        unit="USD/tonne", source="test CNF", quotes={"2026-11":1169.})), encoding="utf-8")
    calls = []
    def failure(c):
        calls.append(c)
        raise live.LiveError("transport failure")
    monkeypatch.setattr(manual, "capture", failure)
    manual.import_manual_cnf(path, request, expected_version=0, authorize=runtime.authorize_server,
                             now=NOW, refresh_current=True)
    assert calls == ["palm"] and store.load_cnf(path, NOW.date(), "palm")[0][11] == 1169.
    assert store.read_market(path, NOW.date(), "palm", revision=1) == (None, None)
    assert store.cnf_provenance(path, NOW.date(), "palm", 1)["market_status"] == "capture_failed"
    manual.import_manual_cnf(path, request, expected_version=1, authorize=runtime.authorize_server,
                             now=NOW.replace(day=13), refresh_current=True)
    assert calls == ["palm"]


@pytest.mark.parametrize("commodity", ["canola", "palm"])
def test_server_page_reload_does_not_fetch_or_create_db(monkeypatch, tmp_path, commodity):
    path, _, _ = server(monkeypatch, tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "05_apps"))
    import commodity_import_margin_page as page
    monkeypatch.setattr(page, "capture_live", lambda *_: pytest.fail("ordinary render must not fetch"))
    code = f"from commodity_import_margin_page import render_commodity_import_margin_page\nfrom datetime import date\nrender_commodity_import_margin_page('{commodity}',today=date(2026,10,12))"
    app = AppTest.from_string(code).run()
    assert not app.exception and not path.exists()
    assert next(b for b in app.button if b.label == "保存CNF").disabled is False
    app.run()
    assert not app.exception and not path.exists()


@pytest.mark.parametrize("year", [2026, 2027, 2029])
def test_new_soybean_and_canola_mapping_full_years_and_eight_symbols(year):
    day = date(year, 10, 12)
    expected = {1:(5,0),2:(5,0),3:(5,0),4:(9,0),5:(9,0),6:(9,0),7:(9,0),
                8:(1,1),9:(1,1),10:(1,1),11:(1,1),12:(5,1)}
    symbols = set()
    for month, (delivery, offset) in expected.items():
        cbot, domestic, shipment = soybean_contracts(day, month)
        full_year = shipment + offset
        assert domestic == f"{full_year%100:02}{delivery:02}"
        canola = model.contracts(day, "canola", month)
        assert [str(c) for c in canola] == [f"CZCE:{p}:{full_year}-{delivery:02}" for p in ("RM","OI")]
        symbols.update(p+domestic for p in ("M","Y"))
    assert len(symbols) == 8
    assert soybean_contracts(date(2026,10,9),4) == ("2705", "2705", 2027)
    assert soybean_contracts(date(2026,10,9),12) == ("2701", "2701", 2026)
    assert soybean_contracts(date(2026,10,12),4) == ("2705", "2709", 2027)
    assert soybean_contracts(date(2026,10,12),12) == ("2701", "2705", 2026)


def test_raw_quote_contract_and_time_tampering_rejected(monkeypatch):
    value = quotes(monkeypatch)
    symbol = next(iter(value["sources"]["domestic"]["quotes"]))
    assert set(value["requests"]) == {"domestic","spot","swap","trade"}
    altered = deepcopy(value)
    altered["sources"]["domestic"]["quotes"][symbol]["raw_fields"][8] = "1234"
    with pytest.raises(ValueError, match="原始字段"):
        live.validate(altered)
    altered = deepcopy(value)
    altered["sources"]["domestic"]["quotes"][symbol]["raw_fields"][1] = "092400"
    with pytest.raises(ValueError, match="原始时间"):
        live.validate(altered)


@pytest.mark.parametrize("commodity", ["canola", "palm"])
def test_server_page_save_keeps_bound_snapshot_after_reload_and_clears_failed_quotes(monkeypatch, tmp_path, commodity):
    import streamlit as st
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "05_apps"))
    import commodity_import_margin_page as page
    path, _, _ = server(monkeypatch, tmp_path)
    value = quotes(monkeypatch, commodity)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(page, "datetime", Clock)
    calls = []
    monkeypatch.setattr(page, "capture_live", lambda c: calls.append(c) or deepcopy(value))
    def editor(frame, **kw):
        frame = frame.copy(); frame.loc[0,"CNF"] = 662.
        return frame
    monkeypatch.setattr(st, "data_editor", editor)
    app = AppTest.from_string(f"from commodity_import_margin_page import render_commodity_import_margin_page\nrender_commodity_import_margin_page('{commodity}')").run()
    next(b for b in app.button if b.label == "刷新国内盘面与汇率").click().run()
    next(b for b in app.button if b.label == "保存CNF").click().run()
    assert not app.exception and calls == [commodity]
    assert store.read_market(path, NOW.date(), commodity, revision=1)[0] == value
    app.run()
    assert not app.exception and calls == [commodity]
    def failed(*_):
        raise live.LiveError("latest source unavailable")
    monkeypatch.setattr(page, "capture_live", failed)
    next(b for b in app.button if b.label == "刷新国内盘面与汇率").click().run()
    next(b for b in app.button if b.label == "保存CNF").click().run()
    assert not app.exception
    assert store.read_market(path, NOW.date(), commodity, revision=2) == (None, None)
    assert not any("国内合约报价时间" in c.value for c in app.caption)
    assert store.read_market(path, NOW.date(), commodity, revision=1)[0] == value


def test_daily_worker_accepts_new_eight_contract_set_and_rejects_ambiguous_symbols(monkeypatch):
    from agri_research_agent.soybean_margin import api_sources
    symbols = sorted({p+soybean_contracts(NOW.date(), m)[1] for p in ("M","Y") for m in range(1,13)})
    calls = []
    monkeypatch.setattr(api_sources, "_worker", lambda s, t: calls.append((json.loads(s),t)) or "captured")
    assert api_sources.domestic(symbols) == "captured" and calls == [(symbols,100)]
    for bad in (symbols + ["M2901"], ["M2701", "M2701"], ["M0"], ["RM705"], [None]):
        with pytest.raises(ValueError, match="contract_set_invalid"):
            api_sources.domestic(bad)


@pytest.mark.parametrize("commodity", ["canola", "palm"])
def test_disabled_server_capture_keeps_page_readable_and_does_not_fetch(monkeypatch, tmp_path, commodity):
    server(monkeypatch, tmp_path)
    monkeypatch.delenv("COMMODITY_IMPORT_SERVER_ENABLED", raising=False)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "05_apps"))
    import commodity_import_margin_page as page
    monkeypatch.setattr(page, "capture_live", lambda *_: pytest.fail("disabled capture must not fetch"))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(page, "datetime", Clock)
    code = f"import commodity_import_margin_page as page\nfrom datetime import date\npage._refresh_live('{commodity}', date(2026,10,12))\npage.render_commodity_import_margin_page('{commodity}',today=date(2026,10,12))"
    app = AppTest.from_string(code).run()
    assert not app.exception
    assert next(b for b in app.button if b.label == "保存CNF").disabled
    assert next(b for b in app.button if b.label == "刷新国内盘面与汇率").disabled
    assert any("服务器取价入口尚未启用" in w.value for w in app.warning)
