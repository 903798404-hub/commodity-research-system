from datetime import date
import importlib
import json
from pathlib import Path
import sys

from streamlit.testing.v1 import AppTest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'05_apps'))
from agri_research_agent.market_data.foreign_fx import CURRENCIES, SCHEMA_VERSION


def test_seasonal_chart_month_axis_and_current_year_highlight():
    import pandas as pd
    from foreign_fx_page import build_seasonality_figure
    from agri_research_agent.shared.chart_style import CURRENT_YEAR_COLOR, CURRENT_LINE_WIDTH
    frame = pd.DataFrame([
        dict(currency='BRL',date=date(y,3,1),local_per_usd=v)
        for y,v in [(2024,4),(2025,5),(2026,6)]
    ])
    figure = build_seasonality_figure(frame,'BRL',[2024,2025,2026],current_year=2026)
    current = next(t for t in figure.data if '今年' in t.name)
    assert current.line.color == CURRENT_YEAR_COLOR and current.line.width == CURRENT_LINE_WIDTH
    assert current.x[0] == date(2000,3,1) and current.customdata[0] == '2026-03-01'
    assert list(figure.layout.xaxis.ticktext) == [f'{m}月' for m in range(1,13)]
    assert next(t for t in figure.data if '均值' in t.name).y[0] == 4.5


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
    app.multiselect(key='fx_season_years_CAD').set_value([]).run(timeout=30)
    assert any('选择至少一个年份' in x.value for x in app.info)
    app.multiselect(key='fx_season_years_CAD').set_value([2026]).run(timeout=30)
    app.radio(key='fx_season_mode').set_value('年初＝100').run(timeout=30)
    assert not app.exception
    app.multiselect(key='fx_compare_currencies').set_value([]).run(timeout=30)
    assert any('选择至少一个币种' in x.value for x in app.info)
    assert path.read_bytes()==original


def test_corrupt_snapshot_reports_error_instead_of_zero_quotes(monkeypatch,tmp_path):
    path=tmp_path/'daily.json'; path.write_text('{bad JSON',encoding='utf-8')
    monkeypatch.setenv('FOREIGN_FX_SNAPSHOT_FILE',str(path))
    app=AppTest.from_file(str(ROOT/'05_apps/streamlit_app.py'),default_timeout=30).run()
    app.session_state['selected_workspace_page']='外盘汇率'; app.run(timeout=30)
    assert not app.exception
    assert app.error and not app.metric


def test_page_reports_failed_check_without_rewriting_stable_data(monkeypatch, tmp_path):
    from agri_research_agent.market_data.foreign_fx_update import run_update
    sys.path.insert(0, str(ROOT/'08_tests/market_data'))
    from test_foreign_fx_update import official_sources, START, END
    run_update(tmp_path, START, END, fetcher=official_sources())
    def failed(url):
        raise TimeoutError('source unavailable')
    try:
        run_update(tmp_path, START, END, fetcher=failed)
    except TimeoutError:
        pass
    path = tmp_path/'daily.json'; original = path.read_bytes()
    monkeypatch.setenv('FOREIGN_FX_SNAPSHOT_FILE', str(path))
    app = AppTest.from_file(str(ROOT/'05_apps/streamlit_app.py'), default_timeout=30).run()
    app.session_state['selected_workspace_page'] = '外盘汇率'
    app.run(timeout=30)
    assert not app.exception
    assert any('采集失败' in warning.value for warning in app.warning)
    assert any('最近检查' in caption.value and '北京时间' in caption.value for caption in app.caption)
    assert app.metric[1].value == '2024-01-03'
    assert path.read_bytes() == original
