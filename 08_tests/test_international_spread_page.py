from __future__ import annotations

import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]
FORMAL_ENTRY = ROOT / "05_apps" / "streamlit_app.py"
PAGE_SOURCE = ROOT / "05_apps" / "international_spread_page.py"


def _reference_root() -> str:
    value = os.getenv("SPREAD_REFERENCE_DATA_ROOT", "").strip()
    if not value:
        pytest.skip("SPREAD_REFERENCE_DATA_ROOT is required for page integration")
    return value


def _titles(app: AppTest) -> list[str]:
    return [
        str(item.value).removeprefix("#### ")
        for item in app.markdown
        if str(item.value).startswith("#### ")
    ]


def test_formal_route_renders_7_15_9_charts_and_approved_title_order(
    monkeypatch,
) -> None:
    monkeypatch.setenv("INTERNATIONAL_SPREAD_REFERENCE_DATA_ROOT", _reference_root())
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=35).run(timeout=35)
    app.session_state["selected_workspace_page"] = "国际价差"
    app.run(timeout=35)

    assert not app.exception
    assert len(app.get("plotly_chart")) == 7
    assert any("数据截至：2026-08-12" in str(item.value) for item in app.markdown)
    assert any("数据来源：Reuters / Oil World" in str(item.value) for item in app.markdown)
    assert any("更新方式：人工快照" in str(item.value) for item in app.markdown)
    assert _titles(app) == [
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
    assert len(app.get("plotly_chart")) == 15
    assert _titles(app)[:3] == [
        "国际菜豆",
        "欧洲菜豆",
        "印度国内菜豆：氢化菜油 - 精炼豆油",
    ]
    assert _titles(app)[-3:] == [
        "美国豆油基差",
        "RINS-D4",
        "美豆油盘面 - 阿根廷豆油（30 USD/T freight）",
    ]

    app.button_group[0].set_value("菜油").run(timeout=35)
    assert not app.exception
    assert len(app.get("plotly_chart")) == 9
    assert _titles(app)[-3:] == [
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
    assert "load_international_spread_reference_records" in source
    assert 'st.columns(len(row.metrics), gap="small")' in source


def test_unavailable_reference_degrades_without_internal_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("INTERNATIONAL_SPREAD_REFERENCE_DATA_ROOT", str(tmp_path))
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=20).run(timeout=20)
    app.session_state["selected_workspace_page"] = "国际价差"
    app.run(timeout=20)

    assert not app.exception
    assert len(app.warning) == 1
    assert "只读参考数据暂不可用" in app.warning[0].value
    assert str(tmp_path) not in app.warning[0].value
