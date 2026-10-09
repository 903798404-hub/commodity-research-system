from datetime import date
import importlib
import json
from pathlib import Path
import sys

from streamlit.testing.v1 import AppTest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'05_apps'))
from agri_research_agent.market_data.foreign_fx import CURRENCIES, SCHEMA_VERSION


def test_navigation_route_preserves_canada_and_loads_missing_fx_without_network(monkeypatch,tmp_path):
    monkeypatch.setenv('FOREIGN_FX_SNAPSHOT_FILE',str(tmp_path/'missing.json'))
    app=AppTest.from_file(str(ROOT/'05_apps/streamlit_app.py'),default_timeout=30).run()
    nav=importlib.import_module('navigation')
    assert nav.CANADA_CANOLA_PAGE_TITLE in nav.internal_workspace_pages()
    assert nav.FOREIGN_FX_PAGE_TITLE in nav.internal_workspace_pages()
    app.session_state['selected_workspace_page']=nav.FOREIGN_FX_PAGE_TITLE
    app.run(timeout=30)
    assert not app.exception
    assert '暂无已发布' in app.info[0].value
    assert len(app.dataframe[0].value)==8


def test_main_entry_displays_real_schema_changes_currency_and_comparison(monkeypatch,tmp_path):
    rows=[]
    for i,c in enumerate(CURRENCIES):
        for day,rate in [('2026-10-02',5+i),('2026-10-08',4+i)]:
            rows.append(dict(currency=c.code,date=day,local_per_usd=rate,
                             provider='BCB_SGS1' if c.code=='BRL' else 'ECB_CROSS'))
    payload=dict(schema_version=SCHEMA_VERSION,generated_at='2026-10-09T02:00:00+00:00',
                 sources=[dict(provider=p,url='https://example.test/official',raw_sha256='a'*64)
                          for p in ('BCB_SGS1','ECB_CROSS')],observations=rows)
    path=tmp_path/'daily.json'; path.write_text(json.dumps(payload),encoding='utf-8')
    original=path.read_bytes()
    monkeypatch.setenv('FOREIGN_FX_SNAPSHOT_FILE',str(path))
    app=AppTest.from_file(str(ROOT/'05_apps/streamlit_app.py'),default_timeout=30).run()
    app.session_state['selected_workspace_page']='外盘汇率'; app.run(timeout=30)
    assert not app.exception
    assert app.metric[0].label=='USD/BRL'
    assert app.metric[2].value=='+25.00%'
    app.selectbox[0].set_value('CAD').run(timeout=30)
    assert not app.exception and app.metric[0].label=='USD/CAD'
    app.multiselect[0].set_value([]).run(timeout=30)
    assert any('选择至少一个币种' in x.value for x in app.info)
    assert path.read_bytes()==original


def test_corrupt_snapshot_reports_error_instead_of_zero_quotes(monkeypatch,tmp_path):
    path=tmp_path/'daily.json'; path.write_text('{bad JSON',encoding='utf-8')
    monkeypatch.setenv('FOREIGN_FX_SNAPSHOT_FILE',str(path))
    app=AppTest.from_file(str(ROOT/'05_apps/streamlit_app.py'),default_timeout=30).run()
    app.session_state['selected_workspace_page']='外盘汇率'; app.run(timeout=30)
    assert not app.exception
    assert app.error and not app.metric
