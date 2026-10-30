from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"05_apps"))
from basis_page import YEAR_COLORS,FALLBACK_COLORS,missing_trading_days_between
from domestic_sugar_basis import build_sugar_seasonal_figure,sugar_chart_rows
from basis_interactive_charts import seasonal_chart
from agri_research_agent.data_sources.domestic_sugar_history import load_sugar_history
from agri_research_agent.pipelines.domestic_sugar_history import prepare_sugar_history
from test_basis_page_formal import mock_public_basis_current

pytest_plugins=["data_sources.test_domestic_sugar_history"]


@pytest.mark.parametrize("kind",["spot","01","05","09","main"])
def test_chart_colors_match_existing_domestic_basis_template(prepared_root,kind):
    snapshot=load_sugar_history(prepared_root)
    rows=sugar_chart_rows(snapshot,kind)
    assert not rows.empty
    figure=build_sugar_seasonal_figure(rows,kind=kind,title="柳州白糖",years=[2024,2025,2026],
        year_colors=YEAR_COLORS,fallback_colors=FALLBACK_COLORS,missing_trading_days=missing_trading_days_between)
    for line in figure.axes[0].lines:
        label=line.get_label()
        if not label.startswith("_"):
            year=int(label.split("/")[0]);assert line.get_color()==YEAR_COLORS[year]
    figure.canvas.draw();plt.close(figure)


def test_fixed_window_uses_one_delivery_year_and_preserves_feb29(prepared_root):
    snapshot=load_sugar_history(prepared_root)
    rows=sugar_chart_rows(snapshot,"01")
    feb=rows.loc[rows.business_date.eq("2024-02-29")].iloc[0]
    assert feb.chart_year==2024 and feb.delivery_year==2025 and feb.ts_code=="SR2501.ZCE"
    figure=build_sugar_seasonal_figure(rows,kind="01",title="柳州SR01",years=[2024],
        year_colors=YEAR_COLORS,fallback_colors=FALLBACK_COLORS,missing_trading_days=missing_trading_days_between)
    line=figure.axes[0].lines[0]
    assert line.get_label()=="2024/25" and line.get_ydata()[0]==1000
    assert pd.isna(line.get_ydata()[1])  # Explicit missing cash quote stays a gap.
    plt.close(figure)


def test_older_year_color_does_not_change_when_selected_with_other_years(prepared_root):
    data=sugar_chart_rows(load_sugar_history(prepared_root),"spot")
    colors=[]
    for years in ([2023],[2023,2024,2026]):
        figure=build_sugar_seasonal_figure(data,kind="spot",title="现货",years=list(years),year_colors=YEAR_COLORS,
            fallback_colors=FALLBACK_COLORS,missing_trading_days=missing_trading_days_between)
        colors.append(next(line.get_color() for line in figure.axes[0].lines if line.get_label()=="2023"))
        plt.close(figure)
    assert colors[0]==colors[1]


def test_interactive_seasonality_keeps_leap_day_null_gaps_and_exact_contract(prepared_root):
    rows = sugar_chart_rows(load_sugar_history(prepared_root), "01")
    figure = seasonal_chart(rows, title="SR01", years=[2024], year_colors=YEAR_COLORS,
        fallback_colors=FALLBACK_COLORS, unit="元/吨", zero_line=True,
        missing_trading_days=missing_trading_days_between, start_month=2, cycle_labels=True)
    trace = figure.data[0]
    assert trace.name == "2024/25" and trace.line.color == YEAR_COLORS[2024]
    assert list(trace.x)[:2] == [29, 30] and list(trace.y)[:2] == [1000, None]
    assert trace.customdata[0] == ["2024-02-29", "SR2501.ZCE"]
    assert trace.connectgaps is False and figure.layout.dragmode == "zoom"


def test_sugar_renders_when_other_formal_current_is_missing(prepared_root, monkeypatch, tmp_path):
    monkeypatch.setenv("DOMESTIC_SUGAR_HISTORY_ROOT", str(prepared_root))
    monkeypatch.setenv("DOMESTIC_SUGAR_REGIONAL_HISTORY_ROOT", str(tmp_path / "missing"))
    app = AppTest.from_string("from domestic_sugar_basis import render_sugar_basis_page\nrender_sugar_basis_page()", default_timeout=35).run()
    assert not app.exception
    assert app.title[0].value == "白糖基差"
    assert [(m.label, m.value) for m in app.metric] == [("柳州现货（元/吨）", "5,330"), ("SR2701 基差", "—"), ("SR2705 基差", "—"), ("SR2709 基差", "—")]
    assert len(app.get("plotly_chart")) == 5
    assert any("2026-10-10 缺少同日收盘价" in item.value for item in app.caption)
    app.multiselect[0].set_value([]).run()
    assert not app.exception and any("至少一个" in item.value for item in app.info)


def test_actual_basis_page_adds_sugar_tab_without_changing_existing_oil_rows(prepared_root, monkeypatch, mock_public_basis_current):
    """Retain the regression ID while verifying the requested independent route."""
    monkeypatch.setenv("DOMESTIC_SUGAR_HISTORY_ROOT", str(prepared_root))
    oil = pd.DataFrame([dict(date=pd.Timestamp("2026-08-19"),commodity="豆粕",region="华东",quote_type="基差报价",delivery_month="现货",futures_contract="2609",cash_price=8000,futures_price=7900,basis=110,source_sheet="formal")])
    mock_public_basis_current(oil, dict(release_id="test-oil",manifest_sha256="a"*64,schema_version="domestic-basis-current/4"))
    app = AppTest.from_string("from pathlib import Path\nfrom basis_page import render_basis_page\nrender_basis_page(Path('formal-current'))", default_timeout=35).run()
    assert not app.exception
    assert [t.label for t in app.tabs] == ["基差", "一口价", "批发价差"]
    assert list(app.dataframe[0].value["品种"]) == ["豆粕"]
    assert not app.metric and len(app.get("plotly_chart")) > 0
    app.session_state["basis_research_tab"] = "白糖（柳州）"
    app.run()
    assert not app.exception and app.session_state["basis_research_tab"] == "基差"
    assert list(app.dataframe[0].value["品种"]) == ["豆粕"]


def test_invalid_sugar_does_not_break_existing_oil_current(prepared_root, monkeypatch, tmp_path, mock_public_basis_current):
    monkeypatch.setenv("DOMESTIC_SUGAR_HISTORY_ROOT", str(prepared_root))
    monkeypatch.setenv("DOMESTIC_SUGAR_REGIONAL_HISTORY_ROOT", str(tmp_path / "missing"))
    (prepared_root / "current.json").write_text('{}', encoding="utf-8")
    app = AppTest.from_string("from domestic_sugar_basis import render_sugar_basis_page\nrender_sugar_basis_page()", default_timeout=35).run()
    assert not app.exception and not app.metric
    assert any("白糖图表暂停" in item.value for item in app.warning)
    oil = pd.DataFrame([dict(date=pd.Timestamp("2026-08-19"),commodity="豆粕",region="华东",quote_type="基差报价",delivery_month="现货",futures_contract="2609",cash_price=8000,futures_price=7900,basis=110,source_sheet="formal")])
    mock_public_basis_current(oil, dict(release_id="test-oil", manifest_sha256="a"*64, schema_version="domestic-basis-current/4"))
    oil_app = AppTest.from_string("from pathlib import Path\nfrom basis_page import render_basis_page\nrender_basis_page(Path('formal-current'))", default_timeout=35).run()
    assert not oil_app.exception and not oil_app.warning
    assert list(oil_app.dataframe[0].value["品种"]) == ["豆粕"]


def test_year_filter_survives_leaving_the_independent_sugar_route(prepared_root, monkeypatch, tmp_path):
    monkeypatch.setenv("DOMESTIC_SUGAR_HISTORY_ROOT", str(prepared_root))
    monkeypatch.setenv("DOMESTIC_SUGAR_REGIONAL_HISTORY_ROOT", str(tmp_path / "missing"))
    app = AppTest.from_file(str(ROOT / "05_apps/streamlit_app.py"), default_timeout=35)
    app.session_state["selected_workspace_page"] = "白糖基差"
    app.run()
    assert not app.exception
    for years in ([2023, 2026], []):
        app.multiselect[0].set_value(years).run()
        app.session_state["selected_workspace_page"] = "首页"
        app.run()
        assert not app.exception and not app.metric
        app.session_state["selected_workspace_page"] = "白糖基差"
        app.run()
        assert not app.exception and app.multiselect[0].value == years
        assert [app.multiselect[0].options[i] for i in app.multiselect[0].proto.default] == [str(y) for y in years]
