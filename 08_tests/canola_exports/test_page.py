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
    assert figures[0].data[-1].x[0] == "1999-08-08"
    assert figures[0].data[-1].x[2] is None
    assert figures[0].layout.xaxis.tickformat == "%m/%d"
    assert "第" not in figures[1].layout.annotations[0].text


def test_date_axis_preserves_leap_days_and_gaps_without_invented_dates():
    assert page.seasonal_date("2024-02-29") == "2000-02-29"
    assert page.seasonal_date("2026-09-27") == "1999-09-27"
    assert page.seasonal_date(None) is None
    value = bundle(("2026-2027", source()))
    value["records"][2]["date_quality"] = "non_monotonic_source_date"
    figures = page.build_figures(data.page_payload(value))
    for figure in figures:
        assert figure.data[0].x[2] is None
        assert figure.data[0].y[2] is None
        assert not figure.data[0].connectgaps


def test_observation_never_invents_sales_or_missing_comparisons():
    text = page.observation(data.page_payload(bundle(("2026-2027", source(weeks=(1, 2))))))
    assert "累计" in text
    assert "同比" not in text
    assert "价格" not in text
    assert "第" not in text
    assert "2026-08-15" in text


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
    assert any("数据截至" in caption.value for caption in app.caption)
    assert all("第 5 周" not in caption.value for caption in app.caption)
    app.multiselect[0].set_value([]).run()
    assert not app.exception


def test_unpublished_export_does_not_throw_or_fetch(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOLA_EXPORT_RUNTIME_ROOT", str(tmp_path))
    app = AppTest.from_file(str(ROOT / "05_apps/canola_exports_page.py")).run()
    assert not app.exception
    assert len(app.info) == 1
    assert len(app.metric) == 0


def test_read_cache_is_bounded_and_new_identity_reads_new_data(tmp_path):
    page.read_data.clear()
    stable = tmp_path / "weekly.json"
    first = bundle(("2026-2027", source()))
    stable.write_text(json.dumps(first), encoding="utf-8")
    first_identity = data.digest(stable.read_bytes())
    assert page.read_data(str(stable), first_identity)["records"][0]["weekly_mt"] == 7000
    second = bundle(("2026-2027", source(weekly="2")))
    stable.write_text(json.dumps(second), encoding="utf-8")
    assert page.read_data(str(stable), data.digest(stable.read_bytes()))["records"][0]["weekly_mt"] == 14000
    assert page.read_data._info.max_entries == 2
    page.read_data.clear()


def test_canada_tabs_preserve_old_route_and_independent_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOLA_EXPORT_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", str(tmp_path))
    app = AppTest.from_string("from canada_canola_weekly_page import render_canada_canola_weekly_page\nrender_canada_canola_weekly_page()").run()
    assert not app.exception
    assert [tab.label for tab in app.tabs] == ["种植生长", "周度出口"]
    navigation = importlib.import_module("navigation")
    item = next(i for g in navigation.NAVIGATION_GROUPS for i in g.items if i.key == "canada_canola")
    assert item.target == navigation.CANADA_CANOLA_PAGE_TITLE
