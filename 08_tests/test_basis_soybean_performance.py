from __future__ import annotations

import copy
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import sys

import matplotlib.pyplot as plt
import pandas as pd
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "05_apps"))
import basis_page
import soybean_crop_progress_page as crop_page
import soybean_weekly_page as weekly_page
from test_basis_page_formal import _formal_public_current_rows


def test_gap_lookup_matches_previous_mask_for_irregular_and_duplicate_calendar() -> None:
    calendar = pd.DatetimeIndex(pd.to_datetime([
        "2026-01-02", "2026-01-05", "2026-01-05", "2026-01-09", "2026-01-12",
    ]))
    for previous in pd.date_range("2026-01-02", "2026-01-11"):
        for current in pd.date_range(previous + pd.Timedelta(days=1), "2026-01-12"):
            expected = int(((calendar > previous) & (calendar < current)).sum())
            assert basis_page.missing_trading_days_between(previous, current, trading_days=calendar) == expected


def test_cached_png_matches_existing_export_and_rebuilds_after_quote_change(monkeypatch) -> None:
    figures_before = set(plt.get_fignums())
    data = pd.DataFrame({"date": pd.to_datetime(["2026-07-01", "2026-07-10"]), "basis": [0.0, -100.0]})
    options = dict(value_col="basis", title="基差", years=(2026,), zero_line=True,
                   trading_calendar_bytes=basis_page.china_futures_trading_days().asi8.tobytes())
    original = basis_page.build_seasonal_report_figure
    figure = original(data, value_col="basis", title="基差", years=[2026], zero_line=True)
    output = BytesIO()
    figure.savefig(output, bbox_inches="tight", dpi=200, format="png")
    plt.close(figure)
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(basis_page, "build_seasonal_report_figure", counted)
    basis_page.cached_seasonal_report_image.clear()
    try:
        first = basis_page.cached_seasonal_report_image(data, **options)
        assert first == output.getvalue()
        assert basis_page.cached_seasonal_report_image(data.copy(), **options) == first
        assert len(calls) == 1
        changed = data.copy()
        changed.loc[0, "basis"] = 10
        assert basis_page.cached_seasonal_report_image(changed, **options) != first
        assert len(calls) == 2
        assert set(plt.get_fignums()) == figures_before
    finally:
        basis_page.cached_seasonal_report_image.clear()


def test_basis_switch_executes_selected_tab_only_and_retains_filters(monkeypatch) -> None:
    rows = _formal_public_current_rows()
    monkeypatch.setattr(basis_page, "load_basis_page_data", lambda root: (
        rows.copy(), {"release_id": "performance-test", "manifest_sha256": "a" * 64},
    ))
    calls = []
    for name, label in (("_render_basis_tab", "基差"), ("_render_cash_price_tab", "一口价"), ("_render_wholesale_spread_tab", "批发价差")):
        original = getattr(basis_page, name)
        def counted(*args, _original=original, _label=label, **kwargs):
            calls.append(_label)
            return _original(*args, **kwargs)
        monkeypatch.setattr(basis_page, name, counted)
    app = AppTest.from_string("from pathlib import Path\nfrom basis_page import render_basis_page\nrender_basis_page(Path('test-current'))", default_timeout=30).run()
    assert not app.exception and calls == ["基差"]
    app.selectbox(key="basis_matrix_commodity").set_value("豆油").run()
    assert not app.exception
    calls.clear()
    app.session_state["basis_research_tab"] = "一口价"
    app.run()
    assert not app.exception and calls == ["一口价"]
    assert not any(item.key.startswith("basis_matrix_") for item in app.selectbox)
    calls.clear()
    app.session_state["basis_research_tab"] = "基差"
    app.run()
    assert not app.exception and calls == ["基差"]
    assert app.selectbox(key="basis_matrix_commodity").value == "豆油"
    assert app.selectbox(key="basis_matrix_commodity").proto.default == app.selectbox(key="basis_matrix_commodity").options.index("豆油")
    app.session_state["basis_research_tab"] = "批发价差"
    app.run()
    assert not app.exception and calls[-1] == "批发价差"


def test_weekly_switch_does_not_execute_hidden_page(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(weekly_page, "render_soybean_crop_progress_page", lambda: calls.append("crop"))
    monkeypatch.setattr(weekly_page, "render_soybean_exports_page", lambda: calls.append("exports"))
    app = AppTest.from_string("from soybean_weekly_page import render_soybean_weekly_page\nrender_soybean_weekly_page()", default_timeout=10).run()
    assert not app.exception and calls == ["crop"]
    app.session_state["soybean_weekly_tab"] = "出口销售与装船"
    app.run()
    assert not app.exception and calls == ["crop", "exports"]


def test_basis_default_does_not_load_hidden_sugar_but_fallback_still_does(monkeypatch) -> None:
    rows = _formal_public_current_rows()
    monkeypatch.setattr(basis_page, "load_basis_page_data", lambda root: (
        rows.copy(), {"release_id": "performance-test", "manifest_sha256": "a" * 64},
    ))
    calls = []
    monkeypatch.setattr(basis_page, "_optional_sugar_snapshot", lambda root: (calls.append(root) or None, None))
    app = AppTest.from_string("from pathlib import Path\nfrom basis_page import render_basis_page\nrender_basis_page(Path('test-current'))", default_timeout=30).run()
    assert not app.exception and not calls
    app.session_state["basis_research_tab"] = "白糖（柳州）"
    app.run()
    assert not app.exception and len(calls) == 1
    assert any("柳州历史快照尚未配置" in info.value for info in app.info)
    # Use the real typed exception raised by the Current reader.
    from agri_research_agent.market_data.public_basis_current import PublicBasisCurrentErrorCode
    def unavailable(root):
        raise basis_page.PublicBasisCurrentError(PublicBasisCurrentErrorCode.PUBLIC_CURRENT_UNAVAILABLE, "test unavailable")
    monkeypatch.setattr(basis_page, "load_basis_page_data", unavailable)
    app.run()
    assert not app.exception and len(calls) == 2
    assert any("Formal Public Basis Current 暂不可用" in warning.value for warning in app.warning)


def test_crop_cache_preserves_results_and_invalidates_input_rules_config_and_day(monkeypatch) -> None:
    fixture = ROOT / "08_tests/fixtures/summary/soybean_crop_progress"
    progress_path = fixture / "soybeans_crop_progress_weekly_2021_2026.parquet"
    condition_path = fixture / "soybeans_crop_condition_weekly_2021_2026.parquet"
    progress, condition = pd.read_parquet(progress_path), pd.read_parquet(condition_path)
    config = crop_page.load_display_config(crop_page.DISPLAY_CONFIG_FILE)
    identity = {"progress": crop_page.file_identity(progress_path), "condition": crop_page.file_identity(condition_path)}
    rules = crop_page.file_identity(crop_page.SUMMARY_RULES_FILE)
    day = datetime.now(timezone.utc).date().isoformat()
    original_comparisons, original_summary = crop_page.build_dashboard_comparisons, crop_page.build_crop_summary
    expected = original_comparisons(progress, condition, config)
    expected_summary = original_summary(progress, condition, source_identity=identity, display_config=config)
    calls = {"comparisons": 0, "summary": 0}
    def comparisons(*args, **kwargs):
        calls["comparisons"] += 1
        return original_comparisons(*args, **kwargs)
    def summary(*args, **kwargs):
        calls["summary"] += 1
        return original_summary(*args, **kwargs)
    monkeypatch.setattr(crop_page, "build_dashboard_comparisons", comparisons)
    monkeypatch.setattr(crop_page, "build_crop_summary", summary)
    crop_page.cached_crop_comparisons.clear()
    crop_page.cached_crop_summary.clear()
    try:
        actual = crop_page.cached_crop_comparisons(progress, condition, config, identity)
        for old, new in zip(expected, actual, strict=True):
            assert old.definition == new.definition and old.baseline_week == new.baseline_week
            pd.testing.assert_frame_equal(old.rows, new.rows)
        got = crop_page.cached_crop_summary(progress, condition, config, identity, rules, day)
        for field in ("facts", "detail_text", "short_text", "classifications", "source_identity", "comparison_identity", "freshness_status"):
            assert getattr(got, field) == getattr(expected_summary, field)
        # Returned objects are isolated; a consumer cannot poison another session.
        actual[0].rows.loc[0, "display_region"] = "changed"
        again = crop_page.cached_crop_comparisons(progress, condition, config, identity)
        pd.testing.assert_frame_equal(again[0].rows, expected[0].rows)
        crop_page.cached_crop_summary(progress, condition, config, identity, rules, day)
        assert calls == {"comparisons": 1, "summary": 1}
        revised = copy.deepcopy(identity)
        revised["progress"]["mtime_ns"] += 1
        crop_page.cached_crop_comparisons(progress, condition, config, revised)
        crop_page.cached_crop_summary(progress, condition, config, revised, rules, day)
        revised_rules = dict(rules, mtime_ns=rules["mtime_ns"] + 1)
        crop_page.cached_crop_summary(progress, condition, config, identity, revised_rules, day)
        crop_page.cached_crop_summary(progress, condition, config, identity, rules, "2099-01-01")
        changed_config = copy.deepcopy(config)
        changed_config["national"]["display_name"] = "全国测试"
        updated = crop_page.cached_crop_comparisons(progress, condition, changed_config, identity)
        assert updated[0].rows.iloc[0]["display_region"] == "全国测试"
        assert calls == {"comparisons": 3, "summary": 4}
    finally:
        crop_page.cached_crop_comparisons.clear()
        crop_page.cached_crop_summary.clear()
