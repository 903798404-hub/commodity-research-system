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


def test_sugar_renders_when_other_formal_current_is_missing(prepared_root,monkeypatch,tmp_path):
    monkeypatch.setenv("DOMESTIC_SUGAR_HISTORY_ROOT",str(prepared_root))
    script=f"from pathlib import Path\nfrom basis_page import render_basis_page\nrender_basis_page(Path({str(tmp_path/'missing-oil-current')!r}))"
    app=AppTest.from_string(script,default_timeout=35).run()
    assert not app.exception
    assert [(m.label,m.value) for m in app.metric]==[("柳州现货（元/吨）","5,330"),("SR2701 基差","-211"),("SR2705 基差","-288"),("SR2709 基差","-370")]
    assert len(app.dataframe)>=3
    assert any("完整日数据" in c.value for c in app.caption)
    app.multiselect[0].set_value([]).run()
    assert not app.exception and any("至少一个" in i.value for i in app.info)


def test_actual_basis_page_adds_sugar_tab_without_changing_existing_oil_rows(prepared_root,monkeypatch,mock_public_basis_current):
    import basis_page
    monkeypatch.setenv("DOMESTIC_SUGAR_HISTORY_ROOT",str(prepared_root))
    oil=pd.DataFrame([dict(date=pd.Timestamp(day),commodity="豆粕",region="华东",quote_type="基差报价",
        delivery_month="现货",futures_contract="2609",cash_price=8000,futures_price=7900,basis=basis,source_sheet="formal") for day,basis in [("2026-08-18",100),("2026-08-19",110)]])
    mock_public_basis_current(oil,dict(release_id="test-oil",manifest_sha256="a"*64,schema_version="domestic-basis-current/4"))
    app=AppTest.from_string("from pathlib import Path\nfrom basis_page import render_basis_page\nrender_basis_page(Path('formal-current'))",default_timeout=35).run()
    assert not app.exception
    assert [t.label for t in app.tabs]==["基差","一口价","批发价差","白糖（柳州）"]
    assert list(app.dataframe[0].value["品种"])==["豆粕"]
    assert not app.metric
    app.session_state["basis_research_tab"] = "白糖（柳州）"
    app.run()
    assert not app.exception
    assert list(app.dataframe[0].value["品种"])==["豆粕"]
    assert any("5,330"==m.value for m in app.metric)
    # Real browser remounts use the declared default, even when the server kept
    # the hidden widget's state. Keep both frontend selection and server value.
    for selected in ([2023,2026],[]):
        app.multiselect[0].set_value(selected).run()
        assert not app.exception
        app.session_state["basis_research_tab"] = "基差"
        app.run()
        app.session_state["basis_research_tab"] = "白糖（柳州）"
        app.run()
        assert not app.exception
        assert app.multiselect[0].value == selected
        assert [app.multiselect[0].options[i] for i in app.multiselect[0].proto.default] == [str(year) for year in selected]


def test_invalid_sugar_does_not_break_existing_oil_current(prepared_root,monkeypatch,mock_public_basis_current):
    import basis_page
    monkeypatch.setenv("DOMESTIC_SUGAR_HISTORY_ROOT",str(prepared_root))
    (prepared_root/"current.json").write_text('{}')
    oil=pd.DataFrame([dict(date=pd.Timestamp("2026-08-19"),commodity="豆粕",region="华东",quote_type="基差报价",delivery_month="现货",futures_contract="2609",cash_price=8000,futures_price=7900,basis=110,source_sheet="formal")])
    mock_public_basis_current(oil,dict(release_id="test-oil",manifest_sha256="a"*64,schema_version="domestic-basis-current/4"))
    app=AppTest.from_string("from pathlib import Path\nfrom basis_page import render_basis_page\nrender_basis_page(Path('formal-current'))",default_timeout=35).run()
    assert not app.exception and len(app.dataframe)>0
    assert not app.warning
    app.session_state["basis_research_tab"] = "白糖（柳州）"
    app.run()
    assert not app.exception and len(app.dataframe)>0
    assert any("白糖图表暂停" in w.value for w in app.warning)
    assert not app.metric
