from __future__ import annotations

import os
import re
import importlib.util
from datetime import date
from decimal import Decimal
from html import unescape
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from agri_research_agent.application.international_spreads import (
    MetricPayload,
    MetricStatus,
    SeasonalityObservation,
)


ROOT = Path(__file__).resolve().parents[1]
FORMAL_ENTRY = ROOT / "05_apps" / "streamlit_app.py"
PAGE_SOURCE = ROOT / "05_apps" / "international_spread_page.py"


def _reference_root() -> str:
    value = os.getenv("SPREAD_REFERENCE_DATA_ROOT", "").strip()
    if not value:
        pytest.skip("SPREAD_REFERENCE_DATA_ROOT is required for page integration")
    return value


def _titles(app: AppTest) -> list[str]:
    titles = []
    for item in app.markdown:
        match = re.search(r'data-title="([^"]+)"', str(item.value))
        if match:
            titles.append(unescape(match.group(1)))
    return titles


def _base_titles(app: AppTest) -> list[str]:
    return [item.split("｜", maxsplit=1)[0] for item in _titles(app)]


def _page_module():
    spec = importlib.util.spec_from_file_location(
        "international_spread_page_style_test", PAGE_SOURCE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seasonality_lines_reuse_month_spread_colorway_and_emphasize_ytd() -> None:
    page = _page_module()
    observations = tuple(
        SeasonalityObservation(
            date(year, 8, 1),
            year,
            "2026 YTD" if year == 2026 else str(year),
            "08-01",
            Decimal(year),
        )
        for year in range(2021, 2027)
    )
    metric = MetricPayload(
        "spread",
        "spread.test",
        "测试价差",
        "USD/T",
        1,
        1,
        observations,
        tuple(range(2021, 2027)),
        date(2026, 8, 1),
        Decimal("2026"),
        date(2026, 8, 1),
        "Reuters",
        "A - B",
        (),
        (),
        "USER_APPROVED_BUSINESS_DEFINITION",
        MetricStatus.READY,
        "exact business-date",
    )

    figure = page.build_seasonality_figure(metric)

    assert [trace.name for trace in figure.data] == [
        "2021",
        "2022",
        "2023",
        "2024",
        "2025",
        "2026 YTD",
    ]
    assert all(trace.line.color is None for trace in figure.data[:-1])
    assert all(trace.line.width == 2.0 for trace in figure.data[:-1])
    assert all(trace.opacity == 1 for trace in figure.data)
    assert figure.data[-1].line.color == page.CURRENT_YEAR_COLOR
    assert figure.data[-1].line.width == 3.4


def test_formal_route_renders_7_15_9_charts_and_approved_title_order(
    monkeypatch,
) -> None:
    monkeypatch.setenv("INTERNATIONAL_SPREAD_REFERENCE_DATA_ROOT", _reference_root())
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=35).run(timeout=35)
    app.session_state["selected_workspace_page"] = "国际价差"
    app.run(timeout=35)

    assert not app.exception
    assert len(app.get("plotly_chart")) == 7
    assert any(
        "数据截至 2026-08-12 ｜ Reuters / Oil World ｜ 人工快照"
        in str(item.value)
        for item in app.markdown
    )
    assert any(
        'class="international-spread-freshness-note"' in str(item.value)
        for item in app.markdown
    )
    assert re.fullmatch(r"国际豆棕｜-?[\d,.]+ USD/T", _titles(app)[0])
    assert any("截至 2026-08-10" in str(item.value) for item in app.markdown)
    assert _base_titles(app) == [
        "国际豆棕",
        "国际菜棕",
        "国际葵棕",
        "欧洲豆棕",
        "欧洲菜棕",
        "欧洲葵棕",
        "POGO：印尼毛棕 - ICE柴油",
    ]

    app.button_group[0].set_value("豆油").run(timeout=35)
    assert not app.exception
    assert len(app.get("plotly_chart")) == 14
    assert _base_titles(app)[:3] == [
        "国际菜豆",
        "欧洲菜豆",
        "印度国内菜豆：氢化菜油 - 精炼豆油",
    ]
    assert _base_titles(app)[-3:] == [
        "美国豆油基差",
        "RINS-D4",
        "美豆油盘面 - 阿根廷豆油（30 USD/T freight）",
    ]

    app.button_group[0].set_value("菜油").run(timeout=35)
    assert not app.exception
    assert len(app.get("plotly_chart")) == 7
    assert _base_titles(app)[-3:] == [
        "欧洲 RME 生物柴油现货",
        "欧洲葵菜价差",
        "RME 生柴溢价",
    ]


def test_page_source_has_no_business_formula_or_provider_coupling() -> None:
    source = PAGE_SOURCE.read_text(encoding="utf-8")

    assert "22.0462" not in source
    assert "raw_value / 100" not in source
    assert "30 USD/T" not in source
    assert "oil_world_prices" not in source
    assert "provider_series_id" not in source
    assert "parse_insert_values" not in source
    assert "data_sources" not in source
    assert "connectgaps=False" in source
    assert "2026 YTD" in source
    assert "st.expander" not in source
    assert "st.popover" in source
    assert "SOURCE_DATA_UNDER_REVIEW" in source
    assert "load_international_spread_reference_records" in source
    assert 'st.columns(GRID_COLUMN_COUNT, gap="small")' in source
    assert "GRID_COLUMN_COUNT = 3" in source
    assert "st.columns(len(row.metrics)" not in source
    assert ".international-spread-freshness-note" in source
    assert ".international-spread-freshness {" in source
    assert "international-spread-freshness-note\">" in source
    assert "pogo" not in source.lower()
    assert "column_span" not in source

    for selector in (
        ".international-spread-freshness",
        ".international-spread-freshness-note",
    ):
        rule = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)\}}", source)
        assert rule is not None
        declarations = rule.group(1)
        assert "position: absolute" not in declarations
        assert "transform:" not in declarations
        assert not re.search(r"margin(?:-[a-z]+)?:\s*[^;]*-\d", declarations)

    assert '[data-testid="stPlotlyChart"] {\n  margin-top: -' not in source


def test_unavailable_reference_degrades_without_internal_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("INTERNATIONAL_SPREAD_REFERENCE_DATA_ROOT", str(tmp_path))
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=20).run(timeout=20)
    app.session_state["selected_workspace_page"] = "国际价差"
    app.run(timeout=20)

    assert not app.exception
    assert len(app.warning) == 1
    assert "只读参考数据暂不可用" in app.warning[0].value
    assert str(tmp_path) not in app.warning[0].value
