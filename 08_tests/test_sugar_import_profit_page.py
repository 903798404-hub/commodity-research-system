from pathlib import Path
from datetime import date
from streamlit.testing.v1 import AppTest
from agri_research_agent.sugar_import_profit.model import today, RESEARCH_VERSION
from agri_research_agent.sugar_import_profit.store import Store


def page(tmp_path, monkeypatch, *, formal=False):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / '05_apps'))
    import sugar_import_profit_page
    db = tmp_path / 'own.sqlite3'
    monkeypatch.setattr(sugar_import_profit_page, 'storage_context', lambda: (db, tmp_path / 'absent', lambda _: None, True))
    if formal:
        app = AppTest.from_file(str(root / '05_apps/streamlit_app.py'), default_timeout=30)
        app.query_params['workspace_page'] = 'sugar_import_profit'
    else:
        app = AppTest.from_string('from sugar_import_profit_page import render_sugar_import_profit_page\nrender_sugar_import_profit_page()', default_timeout=30)
    return app.run(), db


def test_page_without_vendor_history_switches_country_and_keeps_nulls(tmp_path, monkeypatch):
    app, db = page(tmp_path, monkeypatch)
    assert not app.exception and not db.exists()
    assert len(app.get('plotly_chart')) == 12
    assert len(app.tabs) == 0
    assert not any('钢联' in str(e.value) for e in list(app.caption) + list(app.info))
    app.radio(key='sugar-month-country').set_value('泰国').run()
    assert not app.exception and len(app.get('plotly_chart')) == 12 and not db.exists()


def test_formal_route_save_idempotence_stale_cas_and_history_does_not_fetch(tmp_path, monkeypatch):
    app, db = page(tmp_path, monkeypatch, formal=True)
    assert not app.exception and len(app.get('plotly_chart')) == 12
    key = 'sugar-month:巴西:' + today()
    app.button(key=key + ':save').click().run()
    store = Store(db, lambda _: None)
    first = store.monthly_records()[0]
    assert first['payload']['schema_version'] == RESEARCH_VERSION
    app.button(key=key + ':save').click().run()
    assert store.monthly_records()[0] == first
    raw = first['payload']
    raw['parameters']['processing'] = 510.
    store.save_monthly(raw, expected_version=first['version'])
    latest = store.monthly_records()[0]
    app.number_input(key=key + ':processing').set_value(520.).run()
    app.button(key=key + ':save').click().run()
    assert not app.exception and store.monthly_records()[0] == latest
    assert any('另一会话' in w.value for w in app.warning)
    app.date_input(key='sugar-month-day').set_value(date(2025, 1, 2)).run()
    assert not app.exception
    assert app.button(key='sugar-month:巴西:2025-01-02:refresh').disabled


def test_manual_zero_quote_triggers_once_clearing_remains_null(tmp_path, monkeypatch):
    from agri_research_agent.sugar_import_profit import runtime
    calls = []
    monkeypatch.setattr(runtime, 'capture_monthly_market', lambda day, quotes: calls.append((day, quotes)))
    app, db = page(tmp_path, monkeypatch)
    key = 'sugar-month:巴西:' + today()
    state = dict(edited_rows={'0': {'升贴水（美分/磅）': 0., '海运费（美元/吨）': 54.}}, added_rows=[], deleted_rows=[])
    for _ in range(2):
        app.session_state[key + ':editor'] = state
        app.run()
    assert not app.exception and len(calls) == 1
    app.session_state[key + ':editor'] = state
    app.button(key=key + ':save').click().run()
    saved = Store(db, lambda _: None).monthly_records()[0]['payload']
    assert any(r['quote'] == 0. and r['freight'] == 54. for r in saved['quotes'])
    app.session_state[key + ':editor'] = dict(edited_rows={'0': {'升贴水（美分/磅）': None}}, added_rows=[], deleted_rows=[])
    app.run()
    app.button(key=key + ':save').click().run()
    assert not app.exception and len(calls) == 1
    assert all(r['quote'] is None for r in Store(db, lambda _: None).monthly_records()[0]['payload']['quotes'])


def test_seasonal_alignment_retains_leap_day_and_relative_year(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / '05_apps'))
    from sugar_import_profit_page import _seasonal_x
    assert _seasonal_x(date(2024, 2, 29), 2025) == _seasonal_x(date(2028, 2, 29), 2029)
    assert _seasonal_x(date(2024, 3, 1), 2025) - _seasonal_x(date(2024, 2, 29), 2025) == 1
    assert _seasonal_x(date(2023, 12, 31), 2024) + 1 == _seasonal_x(date(2024, 1, 1), 2024)


def _seed_public_freight(tmp_path, value=54.):
    import hashlib, json
    from agri_research_agent.sugar_import_profit.freight import SCHEMA, DEFINITION
    root = tmp_path / 'absent/hisugar_freight'
    root.mkdir(parents=True, exist_ok=True)
    source = dict(article_url='https://www.hisugar.com/home/articleContent?id=2025010308000000000000',
        image_url='https://www.hisugar.com/image/20250103/table.png', image_sha256='a'*64,
        published_at='2025-01-03T08:00:00+08:00')
    row = dict(date='2025-01-02', country='巴西', freight_usd_per_tonne=value, sources=[source])
    raw = json.dumps(dict(schema_version=SCHEMA, provider='HiSugar', unit='USD/tonne',
        definition=DEFINITION, observations=[row]), ensure_ascii=False).encode()
    (root / 'freight.json').write_bytes(raw)
    (root / 'current.json').write_text(json.dumps(dict(schema_version=SCHEMA, file='freight.json',
        sha256=hashlib.sha256(raw).hexdigest(), row_count=1)), encoding='utf-8')
    return row


def test_public_reference_does_not_overwrite_saved_zero_or_clear_and_never_fills_next_day(tmp_path, monkeypatch):
    row = _seed_public_freight(tmp_path)
    app, db = page(tmp_path, monkeypatch)
    assert not app.exception and len(app.get('plotly_chart')) == 13
    app.date_input(key='sugar-month-day').set_value(date(2025, 1, 2)).run()
    key = 'sugar-month:巴西:2025-01-02'
    app.button(key=key + ':save').click().run()
    store = Store(db, lambda _: None)
    saved = store.monthly_records()[0]['payload']
    assert all(r['freight'] == 54. and r['freight_origin']['kind'] == 'hisugar_reference' for r in saved['quotes'])
    assert all(r['cost_in'] is None for r in __import__('agri_research_agent.sugar_import_profit.model', fromlist=['monthly_rows']).monthly_rows(saved))
    app.session_state[key + ':editor'] = dict(edited_rows={'0': {'海运费（美元/吨）':0.}, '1': {'海运费（美元/吨）':None}}, added_rows=[], deleted_rows=[])
    app.button(key=key + ':save').click().run()
    _seed_public_freight(tmp_path, value=56.)
    app, _ = page(tmp_path, monkeypatch)
    app.date_input(key='sugar-month-day').set_value(date(2025, 1, 2)).run()
    app.button(key=key + ':save').click().run()
    saved = store.monthly_records()[0]['payload']
    assert saved['quotes'][0]['freight'] == 0. and saved['quotes'][0]['freight_origin']['kind'] == 'manual'
    assert saved['quotes'][1]['freight'] is None and saved['quotes'][1]['freight_origin']['kind'] == 'missing'
    assert all(r['freight'] == 54. for r in saved['quotes'][2:])
    app.date_input(key='sugar-month-day').set_value(date(2025, 1, 3)).run()
    app.button(key='sugar-month:巴西:2025-01-03:save').click().run()
    missing = next(r['payload'] for r in store.monthly_records() if r['payload']['date'] == '2025-01-03')
    assert all(r['freight'] is None for r in missing['quotes'])
    from sugar_import_profit_page import _freight_grid, _freight_source
    grid = _freight_grid([row])
    assert grid.iloc[0].freight_usd_per_tonne == 54. and grid.iloc[1].isna().freight_usd_per_tonne
    assert _freight_source(saved['quotes'][0])['运费来源'] == '人工'
    assert _freight_source(saved['quotes'][2])['运费来源链接'] == row['sources'][0]['article_url']
