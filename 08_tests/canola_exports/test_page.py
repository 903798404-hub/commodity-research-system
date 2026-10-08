from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

from agri_research_agent.canola_exports import data
from test_exports import bundle, source

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "05_apps"))
page = importlib.import_module("canola_exports_page")


def test_three_seasonal_figures_have_real_dates_gaps_and_styles():
    value = bundle(("2026-2027", source(weeks=(1, 2, 4, 5))), ("2025-2026", source("2025-2026")))
    payload = data.page_payload(value)
    figures = page.build_figures(payload)
    assert len(figures) == 3
    for figure in figures:
        current = figure.data[-1]
        assert current.name == "2026/2027"
        assert current.line.width > figure.data[0].line.width
        assert figure.data[0].line.dash == "dash"
        assert not current.connectgaps
        assert current.y[2] is None
        assert len(current.x) == 5
        assert current.customdata[-1] == "2026-09-05"
    assert figures[0].data[-1].y[0] == 0.7
    assert "2" in figures[1].layout.annotations[0].text


def test_observation_never_invents_sales_or_missing_comparisons():
    text = page.observation(data.page_payload(bundle(("2026-2027", source(weeks=(1, 2))))))
    assert "累计" in text
    assert "同比" not in text
    assert "价格" not in text


def test_dashboard_four_metrics_three_charts_and_failure_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOLA_EXPORT_RUNTIME_ROOT", str(tmp_path))
    stable = tmp_path / data.STABLE
    stable.parent.mkdir(parents=True)
    stable.write_text(json.dumps(bundle(("2026-2027", source()), ("2025-2026", source("2025-2026")))), encoding="utf-8")
    status = tmp_path / data.STATUS
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"status": "FAILED", "checked_at": "2026-10-08"}))
    app = AppTest.from_file(str(ROOT / "05_apps/canola_exports_page.py"), default_timeout=15).run()
    assert not app.exception
    assert len(app.metric) == 4
    assert len(app.get("plotly_chart")) == 3
    assert len(app.warning) == 1
    assert app.metric[0].value == "0.70 万吨"
    app.multiselect[0].set_value([]).run()
    assert not app.exception


def test_unpublished_export_does_not_throw_or_fetch(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOLA_EXPORT_RUNTIME_ROOT", str(tmp_path))
    app = AppTest.from_file(str(ROOT / "05_apps/canola_exports_page.py")).run()
    assert not app.exception
    assert len(app.info) == 1
    assert len(app.metric) == 0


def test_canada_tabs_preserve_old_route_and_independent_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOLA_EXPORT_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", str(tmp_path))
    app = AppTest.from_string("from canada_canola_weekly_page import render_canada_canola_weekly_page\nrender_canada_canola_weekly_page()").run()
    assert not app.exception
    assert [tab.label for tab in app.tabs] == ["种植生长", "周度出口"]
    navigation = importlib.import_module("navigation")
    item = next(i for g in navigation.NAVIGATION_GROUPS for i in g.items if i.key == "canada_canola")
    assert item.target == navigation.CANADA_CANOLA_PAGE_TITLE
