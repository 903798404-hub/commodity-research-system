from __future__ import annotations

import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


def reference_root() -> Path:
    value = os.environ.get("SPREAD_REFERENCE_DATA_ROOT", "")
    if not value:
        pytest.skip("SPREAD_REFERENCE_DATA_ROOT is required for page reference-data test")
    return Path(value).resolve(strict=True)


def test_spread_page_keeps_default_controls_layout_and_chart_contract() -> None:
    root = reference_root()
    feature_page = Path(__file__).resolve().parents[3] / "05_apps" / "streamlit_app.py"
    script = f"""
import importlib.util
from pathlib import Path

reference_root = Path({str(root)!r})
feature_page = Path({str(feature_page)!r})
spec = importlib.util.spec_from_file_location("spread_page_contract_app", feature_page)
assert spec is not None and spec.loader is not None
page = importlib.util.module_from_spec(spec)
spec.loader.exec_module(page)
page.DATABASE_PARQUET_FILE = reference_root / "01_data" / "historical_spread_database.parquet"
page.DATABASE_XLSX_FILE = reference_root / "01_data" / "historical_spread_database.xlsx"
page.SPREAD_CONFIG_FILE = reference_root / "02_configs" / "historical_spread_config.xlsx"
page.UPDATE_STATUS_FILE = reference_root / "01_data" / "update_status.json"
page.render_spread_dashboard()
"""
    app = AppTest.from_string(script, default_timeout=30).run()

    assert not app.exception
    board = next(item for item in app.radio if item.label == "板块")
    assert board.options == ["豆系月差", "棕榈油与菜系月差", "品种间套利"]
    assert board.value == "豆系月差"
    method = next(item for item in app.selectbox if item.label == "计算方法")
    assert method.options == ["绝对价差 A-B", "商品比值 A/B"]
    assert method.value == "绝对价差 A-B"
    years = next(item for item in app.number_input if item.label == "显示年份数量")
    assert years.value == 5
    assert [item.value for item in app.title] == ["豆系月差"]
    charts = app.get("plotly_chart")
    assert len(charts) == 6
