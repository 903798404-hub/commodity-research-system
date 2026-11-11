from __future__ import annotations

import sys
import json
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.text import Annotation
import pandas as pd
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "05_apps"))
from basis_page import missing_trading_days_between
from domestic_sugar_regional_basis import REGION_COLORS, build_regional_basis_figure, build_regional_basis_interactive
from agri_research_agent.data_sources.domestic_sugar_regional_history import REGIONS, load_regional_history
from agri_research_agent.data_sources.domestic_sugar_daily import validate_daily
from agri_research_agent.data_sources.domestic_sugar_history import load_sugar_history
from domestic_sugar_basis import reconcile_daily_guangxi

pytest_plugins = ["data_sources.test_domestic_sugar_regional_history"]


def test_guangxi_appends_exact_fixed_closes_and_preserves_history(prepared_root, tmp_path):
    old = load_sugar_history(prepared_root)
    day = "2026-11-10"
    payload = dict(schema_version="domestic-sugar-daily/1", workbook_sha256="a" * 64,
        mapping_sha256="b" * 64, daily_sha256="c" * 64,
        records=[dict(date=day, region=region, components=components, contract="SR2701.ZCE", close=5195)
                 for region, components in [("广西糖厂", [dict(name="东糖", price=5150, cell="J2701"),
                                                          dict(name="凤糖", price=5160, cell="K2701")]),
                                             ("昆明", []), ("乌鲁木齐", [])]],
        fixed_closes=[dict(date=day, contract="SR2701.ZCE", close=5195),
                      dict(date=day, contract="SR2705.ZCE", close=5261)])
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    combined = reconcile_daily_guangxi(old, feed)
    assert combined.spot.iloc[-1].cash_price == 5155
    latest = combined.nearest_basis.loc[combined.nearest_basis.business_date.eq(day)].set_index("delivery_month")
    assert latest.loc[1, "basis"] == -40 and latest.loc[5, "basis"] == -106
    assert pd.isna(latest.loc[9, "basis"])
    before = combined.spot.loc[combined.spot.business_date.isin(old.spot.business_date)].reset_index(drop=True)
    pd.testing.assert_frame_equal(before[old.spot.columns], old.spot.reset_index(drop=True), check_dtype=False)


def test_fixed_closes_reject_mismatch_with_actual_main(prepared_root, tmp_path):
    from agri_research_agent.data_sources.domestic_sugar_history import SugarHistoryError
    import pytest
    payload = dict(schema_version="domestic-sugar-daily/1", workbook_sha256="a" * 64,
        mapping_sha256="b" * 64, daily_sha256="c" * 64,
        records=[dict(date="2026-11-10", region=r, components=[], contract="SR2701.ZCE", close=5195)
                 for r in ("广西糖厂", "昆明", "乌鲁木齐")],
        fixed_closes=[dict(date="2026-11-10", contract="SR2701.ZCE", close=5196)])
    feed = tmp_path / "feed.json"; feed.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SugarHistoryError, match="冲突"):
        reconcile_daily_guangxi(load_sugar_history(prepared_root), feed)


def test_page_shows_guangxi_and_all_three_actual_contract_bases(prepared_root, regional_root, monkeypatch, tmp_path):
    day = "2026-11-10"
    payload = dict(schema_version="domestic-sugar-daily/1", workbook_sha256="a" * 64,
        mapping_sha256="b" * 64, daily_sha256="c" * 64,
        records=[dict(date=day, region=region, components=components, contract="SR2701.ZCE", close=5195)
                 for region, components in [("广西糖厂", [dict(name="东糖", price=5150, cell="J2701"),
                                                          dict(name="凤糖", price=5160, cell="K2701")]),
                                             ("昆明", []), ("乌鲁木齐", [])]],
        fixed_closes=[dict(date=day, contract=code, close=close) for code, close in
                      [("SR2701.ZCE", 5195), ("SR2705.ZCE", 5261), ("SR2709.ZCE", 5333)]])
    feed=tmp_path / "feed.json"; feed.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("DOMESTIC_SUGAR_DAILY_FILE", str(feed))
    monkeypatch.setenv("DOMESTIC_SUGAR_HISTORY_ROOT", str(prepared_root))
    monkeypatch.setenv("DOMESTIC_SUGAR_REGIONAL_HISTORY_ROOT", str(regional_root))
    app=AppTest.from_string("from domestic_sugar_basis import render_sugar_basis_page\nrender_sugar_basis_page()", default_timeout=40).run()
    assert not app.exception
    assert [(m.label, m.value) for m in app.metric] == [("广西糖厂现货（元/吨）", "5,155"),
        ("SR2701 基差", "-40"), ("SR2705 基差", "-106"), ("SR2709 基差", "-178")]
    assert len(app.get("plotly_chart")) == 6


def test_manual_feed_adds_guangxi_without_changing_historic_liuzhou(regional_root):
    import pandas as pd
    historical = load_regional_history(regional_root).daily
    extra = validate_daily(dict(schema_version="domestic-sugar-daily/1", workbook_sha256="a" * 64,
        mapping_sha256="b" * 64, daily_sha256="c" * 64, records=[dict(
            date="2026-11-10", region="广西糖厂",
            components=[dict(name="东糖", price=5150, cell="J2701"),
                        dict(name="凤糖", price=5160, cell="K2701")],
            contract="SR2701.ZCE", close=5195),
            dict(date="2026-11-10", region="昆明", components=[], contract="SR2701.ZCE", close=5195),
            dict(date="2026-11-10", region="乌鲁木齐", components=[], contract="SR2701.ZCE", close=5195)]))
    combined = pd.concat([historical, extra], ignore_index=True)
    figure = build_regional_basis_interactive(combined, missing_trading_days=missing_trading_days_between)
    legacy = next(t for t in figure.data if t.name == "柳州")
    new = next(t for t in figure.data if t.name == "广西糖厂")
    assert len(legacy.y) and list(new.y) == [-40.0]
    assert new.customdata[0][0] == "SR2701.ZCE"


def test_interactive_regions_preserve_exact_dates_values_contracts_and_missingness(regional_root):
    daily = load_regional_history(regional_root).daily
    figure = build_regional_basis_interactive(daily, missing_trading_days=missing_trading_days_between)
    assert [trace.name for trace in figure.data] == list(REGIONS)
    for trace in figure.data:
        assert trace.line.color == REGION_COLORS[trace.name]
        assert trace.connectgaps is False and trace.y[-1] is None
        index = list(trace.x).index(pd.Timestamp("2026-10-09"))
        assert trace.customdata[index][0] == "SR2701.ZCE"
    assert figure.layout.xaxis.rangeslider.visible
    assert len(figure.layout.annotations) == 3


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


def test_endpoint_labels_use_latest_valid_values_and_separate_close_labels(regional_root):
    daily = load_regional_history(regional_root).daily
    # Missing latest prices must not be labelled as today's zero or carried forward.
    daily.loc[daily.region.eq("乌鲁木齐") & daily.business_date.eq("2026-10-09"), "basis"] = float("nan")
    daily.loc[daily.region.eq("昆明") & daily.business_date.eq("2026-10-09"), "basis"] = -210
    fig = build_regional_basis_figure(daily, missing_trading_days=missing_trading_days_between)
    labels = [item for item in fig.axes[0].texts if isinstance(item, Annotation)]
    assert len(labels) == 3
    positions = sorted(item.get_position()[1] for item in labels)
    assert min(b - a for a, b in zip(positions, positions[1:])) >= .099
    for region in REGIONS:
        latest = daily.loc[daily.region.eq(region) & daily.basis.notna()].iloc[-1]
        label = next(item for item in labels if item.get_text().startswith(region))
        assert label.xy == (latest.business_date, latest.basis)
        assert label.get_text().startswith(f"{region} {latest.basis:+,.0f}")
        if region == "乌鲁木齐":
            assert latest.business_date.strftime("%Y年%m月%d日") in label.get_text()
    fig.canvas.draw(); plt.close(fig)


def test_existing_liuzhou_metrics_and_year_filter_are_unchanged(regional_root, prepared_root, monkeypatch, tmp_path):
    monkeypatch.setenv("DOMESTIC_SUGAR_HISTORY_ROOT", str(prepared_root))
    monkeypatch.setenv("DOMESTIC_SUGAR_REGIONAL_HISTORY_ROOT", str(regional_root))
    script = f"from pathlib import Path\nfrom domestic_sugar_basis import render_sugar_basis_page\nrender_sugar_basis_page()"
    app = AppTest.from_string(script, default_timeout=40).run()
    assert not app.exception
    assert len(app.get("plotly_chart")) == 6
    assert [(m.label, m.value) for m in app.metric] == [("柳州现货（元/吨）", "5,330"), ("SR2701 基差", "—"), ("SR2705 基差", "—"), ("SR2709 基差", "—")]
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
    app = AppTest.from_string("from pathlib import Path\nfrom domestic_sugar_basis import render_sugar_basis_page\nrender_sugar_basis_page()", default_timeout=40).run()
    assert not app.exception and app.metric[0].value == "5,330"
    assert any("三地白糖历史快照校验失败" in warning.value for warning in app.warning)
