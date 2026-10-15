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
    assert [t.label for t in app.tabs] == ["月份盘面榨利", "历史利润", "当日计算", "自算记录"]
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


def test_monthly_route_without_history_and_refresh_once_per_cnf_change(tmp_path, monkeypatch):
    import json
    from datetime import date
    from agri_research_agent.sugar_import_profit.model import QUOTE_MODES
    from agri_research_agent.sugar_import_profit import runtime
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / '05_apps'))
    import sugar_import_profit_page
    db = tmp_path / 'new.sqlite3'
    monkeypatch.setattr(sugar_import_profit_page, 'storage_context', lambda: (db, tmp_path / 'absent', lambda _: None, True))
    calls = []
    def capture(day, quotes):
        calls.append((day, quotes))
        return None
    monkeypatch.setattr(runtime, 'capture_monthly_market', capture)
    app = AppTest.from_string('from sugar_import_profit_page import render_sugar_import_profit_page\nrender_sugar_import_profit_page()', default_timeout=30).run()
    assert not app.exception and not db.exists() and not calls
    key = 'sugar-month:巴西:' + today()
    app.selectbox(key=key + ':mode').set_value(QUOTE_MODES[1]).run()
    assert not app.exception and not calls
    # Streamlit's data editor exposes edits through session_state, unlike scalar widgets.
    editor_state = dict(edited_rows={'0': {'CNF报价': 0.}}, added_rows=[], deleted_rows=[])
    app.session_state[key + ':editor'] = editor_state
    app.run()
    assert not app.exception and len(calls) == 1
    # AppTest has no editor widget interaction API: re-send its raw editor state
    # with each synthetic browser event, as the actual browser does.
    app.session_state[key + ':editor'] = editor_state
    app.run()
    assert not app.exception and len(calls) == 1
    app.session_state[key + ':editor'] = editor_state
    app.text_area(key=key + ':source').set_value('测试手工报价来源').run()
    app.session_state[key + ':editor'] = editor_state
    app.button(key=key + ':save').click().run()
    assert not app.exception and len(calls) == 1
    saved = Store(db, lambda _: None).monthly_records()
    assert len(saved) == 1 and next(r for r in saved[0]['payload']['quotes'] if r['period'] == today()[:4] + '-11')['quote'] == 0.
    assert saved[0]['payload']['market'] is None
    # Clearing is a true null and does not resurrect the saved quote.
    app.session_state[key + ':editor'] = dict(edited_rows={'0': {'CNF报价': None}}, added_rows=[], deleted_rows=[])
    app.run()
    app.button(key=key + ':save').click().run()
    assert all(r['quote'] is None for r in Store(db, lambda _: None).monthly_records()[0]['payload']['quotes'])
    assert len(calls) == 1
