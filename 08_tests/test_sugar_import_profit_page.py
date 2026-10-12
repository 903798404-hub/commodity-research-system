from pathlib import Path
import sys
from streamlit.testing.v1 import AppTest
from agri_research_agent.sugar_import_profit import runtime
from agri_research_agent.sugar_import_profit.model import today
from agri_research_agent.sugar_import_profit.store import Store
from test_sugar_import_profit import bundle


def test_real_page_four_charts_switch_sources_and_country_without_writes(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "05_apps"))
    history = tmp_path / "history"
    bundle(history)
    db = tmp_path / "never-created.sqlite3"
    def context():
        return db, history, lambda _: None, False
    import sugar_import_profit_page
    monkeypatch.setattr(sugar_import_profit_page, "storage_context", context)
    app = AppTest.from_string("from sugar_import_profit_page import render_sugar_import_profit_page\nrender_sugar_import_profit_page()", default_timeout=30).run()
    assert not app.exception
    assert len(app.get("plotly_chart")) == 4
    assert [t.label for t in app.tabs] == ["历史利润", "当日计算", "自算记录"]
    app.radio(key="sugar-profit-country").set_value("泰国").run()
    assert not app.exception and not db.exists()
    app.radio(key="sugar-profit-source").set_value("本人自算记录").run()
    assert not app.exception
    assert len(app.get("plotly_chart")) == 4
    assert not db.exists()


def test_formal_route_save_idempotence_and_stale_widget_cas(tmp_path, monkeypatch):
    from datetime import date
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "05_apps"))
    history = tmp_path / "history"
    bundle(history)
    db = tmp_path / "own.sqlite3"
    import sugar_import_profit_page
    monkeypatch.setattr(sugar_import_profit_page, "storage_context", lambda: (db, history, lambda _: None, True))
    app = AppTest.from_file(str(root / "05_apps/streamlit_app.py"), default_timeout=30)
    app.query_params["workspace_page"] = "sugar_import_profit"
    app.run()
    assert not app.exception and len(app.get("plotly_chart")) == 4
    group = "sugar-profit-input:巴西:" + today()
    for key, value in (("ice", 17.), ("fx", 7.), ("insurance_fee", 0.)):
        app.number_input(key=group + ":" + key).set_value(value)
    app.text_input(key=group + ":contract").set_value("SBH27")
    for key in ("ice_date", "fx_date"):
        app.date_input(key=group + ":" + key).set_value(date.fromisoformat(today()))
    app.run()
    assert not app.exception and not db.exists()
    app.button(key=group + ":save").click().run()
    assert not app.exception
    store = Store(db, lambda _: None)
    record = store.records()[0]
    app.button(key=group + ":save").click().run()
    assert len(store.records()) == 1 and store.records()[0] == record
    # Another session legitimately revises the record. This browser still holds
    # its original widgets and version and must not overwrite that newer revision.
    store.save("巴西", dict(record["inputs"], fx=7.3), expected_version=record["version"], revise=True)
    newest = store.records()[0]
    app.number_input(key=group + ":fx").set_value(7.1).run()
    app.button(key=group + ":save").click().run()
    assert not app.exception
    assert store.records()[0] == newest
    assert any("另一会话" in w.value for w in app.warning)
