from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "05_apps"))
from basis_page import missing_trading_days_between
from domestic_sugar_regional_basis import REGION_COLORS, build_regional_basis_figure
from agri_research_agent.data_sources.domestic_sugar_regional_history import REGIONS, load_regional_history

pytest_plugins = ["data_sources.test_domestic_sugar_regional_history"]


def test_chart_has_three_stable_regions_real_dates_and_zero_line(regional_root):
    daily = load_regional_history(regional_root).daily
    fig = build_regional_basis_figure(daily, missing_trading_days=missing_trading_days_between)
    axis = fig.axes[0]
    lines = [line for line in axis.lines if line.get_label() in REGIONS]
    assert [line.get_label() for line in lines] == list(REGIONS)
    assert [line.get_color() for line in lines] == [REGION_COLORS[region] for region in REGIONS]
    assert all(pd.Timestamp(line.get_xdata()[0]).year == 2023 for line in lines)
    assert all(pd.isna(line.get_ydata()[-1]) for line in lines)  # Future pairing absent, not carried forward.
    assert any(list(line.get_ydata()) == [0, 0] for line in axis.lines if line.get_label() not in REGIONS)
    fig.canvas.draw(); plt.close(fig)


def test_existing_liuzhou_metrics_and_year_filter_are_unchanged(regional_root, prepared_root, monkeypatch, tmp_path):
    monkeypatch.setenv("DOMESTIC_SUGAR_HISTORY_ROOT", str(prepared_root))
    monkeypatch.setenv("DOMESTIC_SUGAR_REGIONAL_HISTORY_ROOT", str(regional_root))
    script = f"from pathlib import Path\nfrom basis_page import render_basis_page\nrender_basis_page(Path({str(tmp_path/'missing-oil')!r}))"
    app = AppTest.from_string(script, default_timeout=40).run()
    assert not app.exception
    assert [(m.label, m.value) for m in app.metric] == [("柳州现货（元/吨）", "5,330"), ("SR2701 基差", "-211"), ("SR2705 基差", "-288"), ("SR2709 基差", "-370")]
    assert any("三地白糖主力基差" in h.value for h in app.subheader)
    assert app.date_input[0].value.isoformat() == "2025-01-01"
    assert list(app.multiselect[0].options) == ["2023", "2024", "2026"]
    app.multiselect[0].set_value([]).run()
    assert not app.exception and any("三地白糖主力基差" in h.value for h in app.subheader)
    app.date_input[0].set_value(app.date_input[1].value).run()
    assert not app.exception
    assert any("没有有效" in item.value for item in app.info)


def test_corrupt_regional_dataset_does_not_break_old_liuzhou(regional_root, prepared_root, monkeypatch):
    monkeypatch.setenv("DOMESTIC_SUGAR_HISTORY_ROOT", str(prepared_root))
    monkeypatch.setenv("DOMESTIC_SUGAR_REGIONAL_HISTORY_ROOT", str(regional_root))
    (regional_root / "current.json").write_text("{}", encoding="utf-8")
    app = AppTest.from_string("from pathlib import Path\nfrom basis_page import render_basis_page\nrender_basis_page(Path('missing-oil'))", default_timeout=40).run()
    assert not app.exception and app.metric[0].value == "5,330"
    assert any("三地白糖历史快照校验失败" in warning.value for warning in app.warning)
