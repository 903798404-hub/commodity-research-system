from __future__ import annotations

import os
import json
from pathlib import Path

import pandas as pd
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
page.DATA_DIR = reference_root / "01_data"
page.DATABASE_PARQUET_FILE = reference_root / "01_data" / "historical_spread_database.parquet"
page.DATABASE_XLSX_FILE = reference_root / "01_data" / "historical_spread_database.xlsx"
page.SPREAD_CONFIG_FILE = reference_root / "02_configs" / "historical_spread_config.xlsx"
page.render_spread_dashboard()
"""
    app = AppTest.from_string(script, default_timeout=30).run()

    assert not app.exception
    board = next(item for item in app.radio if item.label == "板块")
    assert board.options == ["豆系月差", "棕榈油与菜系月差", "国内白糖价差", "品种间套利"]
    assert len(app.sidebar.radio) == 0
    assert len(app.main.radio) == 1
    assert board.proto.horizontal
    assert board.value == "豆系月差"
    method = next(item for item in app.selectbox if item.label == "计算方法")
    assert method.options == ["绝对价差 A-B", "商品比值 A/B"]
    assert method.value == "绝对价差 A-B"
    years = next(item for item in app.number_input if item.label == "显示年份数量")
    assert years.value == 5
    assert [item.value for item in app.title] == ["豆系月差"]
    charts = app.get("plotly_chart")
    assert len(charts) == 6
    board.set_value("国内白糖价差").run()
    assert not app.exception
    assert len(app.get("plotly_chart")) == 0
    assert "暂无可用白糖月差数据" in app.info[0].value
    assert "白糖 09-01" in app.info[0].value


def test_board_controls_remain_available_without_a_database(tmp_path: Path) -> None:
    feature_page = Path(__file__).resolve().parents[3] / "05_apps" / "streamlit_app.py"
    script = f"""
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("spread_missing_data_app", {str(feature_page)!r})
page = importlib.util.module_from_spec(spec)
spec.loader.exec_module(page)
page.get_database_path = lambda: Path({str(tmp_path / 'missing.parquet')!r})
page.render_spread_dashboard()
"""
    app = AppTest.from_string(script).run()
    assert not app.exception
    assert app.radio[0].options[2] == "国内白糖价差"
    app.radio[0].set_value("国内白糖价差").run()
    assert not app.exception
    assert app.error
    assert app.title[0].value == "国内白糖价差"


def test_sugar_page_renders_all_month_spreads_and_switches_to_ratio(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    rows = []
    for name in ["SR 9-1", "SR 1-5", "SR 5-9"]:
        for year in range(2020, 2026):
            for offset in range(2):
                rows.append({
                    "date": pd.Timestamp(year=year, month=10, day=1 + offset),
                    "season": f"{year}/{year + 1}", "calendar_offset": offset,
                    "month_day": f"10-{1 + offset:02d}", "spread_name": name,
                    "status": "success", "spread_value": 200.0 + offset,
                    "leg1_price": 6100.0 + offset, "leg2_price": 5900.0,
                })
    artifact = tmp_path / "sugar.parquet"
    pd.DataFrame(rows).to_parquet(artifact, index=False)
    script = f"""
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("sugar_populated_app", {str(root / '05_apps/streamlit_app.py')!r})
page = importlib.util.module_from_spec(spec)
spec.loader.exec_module(page)
page.get_database_path = lambda: Path({str(artifact)!r})
page.render_spread_dashboard()
"""
    app = AppTest.from_string(script, default_timeout=15).run()
    app.radio[0].set_value("国内白糖价差").run()
    assert not app.exception
    charts = app.get("plotly_chart")
    assert len(charts) == 3
    specs = [json.loads(chart.proto.spec) for chart in charts]
    assert [spec["layout"]["title"]["text"].split("｜")[0] for spec in specs] == [
        "<b>白糖 09-01", "<b>白糖 01-05", "<b>白糖 05-09",
    ]
    for spec in specs:
        latest = next(trace for trace in spec["data"] if "当前年度" in trace["name"])
        assert latest["line"]["width"] == 3.4
    app.selectbox[0].set_value("商品比值 A/B").run()
    assert not app.exception
    assert len(app.get("plotly_chart")) == 3
    assert "商品比值无单位" in app.caption[0].value
