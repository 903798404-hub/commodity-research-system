from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from agri_research_agent.pipelines.soybean_crop_comparison import load_display_config
from agri_research_agent.summary_engine.crop import build_crop_summary
from summary_panel import _crop_comprehensive_markdown, _crop_short_markdown


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "01_data" / "processed" / "soybean_crop_progress"
DISPLAY_CONFIG = load_display_config(
    PROJECT_ROOT / "02_configs" / "soybean_crop_progress_display.yaml"
)
NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)
REQUIRED_COLUMNS = [
    "metric",
    "geography_level",
    "region_name",
    "calendar_year",
    "week_ending",
    "value_pct",
]


def _real_summary():
    progress = pd.read_parquet(
        DATA_DIR / "soybeans_crop_progress_weekly_2021_2026.parquet"
    )
    condition = pd.read_parquet(
        DATA_DIR / "soybeans_crop_condition_weekly_2021_2026.parquet"
    )
    return build_crop_summary(
        progress,
        condition,
        source_identity={"fixture": "local-stable"},
        generated_at=NOW,
        display_config=DISPLAY_CONFIG,
    )


def _row(metric: str, week: str | pd.Timestamp, value: float) -> dict[str, object]:
    timestamp = pd.Timestamp(week)
    return {
        "metric": metric,
        "geography_level": "US",
        "region_name": "US TOTAL",
        "calendar_year": timestamp.year,
        "week_ending": timestamp,
        "value_pct": value,
    }


def _metric_history(
    metric: str,
    *,
    baseline: str,
    current: float,
    previous: float,
    historical: tuple[float, ...],
) -> list[dict[str, object]]:
    week = pd.Timestamp(baseline)
    rows = [
        _row(metric, week - pd.Timedelta(days=7), previous),
        _row(metric, week, current),
    ]
    rows.extend(
        _row(metric, week - pd.Timedelta(weeks=offset), value)
        for offset, value in zip((52, 104, 156, 208, 260), historical)
    )
    return rows


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=REQUIRED_COLUMNS)


def test_real_summary_uses_chinese_structure_and_each_metrics_own_week() -> None:
    summary = _real_summary()
    metrics = summary.facts["metrics"]

    assert [item["display_name"] for item in metrics] == [
        "播种率",
        "出苗率",
        "开花率",
        "结荚率",
        "优良率",
    ]
    assert [item["week_ending"] for item in metrics] == [
        "2026-06-14",
        "2026-06-28",
        "2026-07-12",
        "2026-07-12",
        "2026-07-12",
    ]
    assert "各指标最新有效周" in summary.facts["data_time_label"]
    assert summary.detail_text.index("### 综合") < summary.detail_text.index(
        "### 重点变化"
    ) < summary.detail_text.index("### 当前有效指标")
    assert "收割率" not in [item["display_name"] for item in metrics]
    for technical in (
        "PLANTED",
        "EMERGED",
        "BLOOMING",
        "SETTING_PODS",
        "GOOD_EXCELLENT",
        "summary-rules-v1",
        "fallback_used",
    ):
        assert technical not in summary.detail_text


def test_reproductive_stage_prioritizes_condition_pods_and_blooming() -> None:
    summary = _real_summary()

    assert summary.facts["current_stage"] == "reproductive"
    assert [item["display_name"] for item in summary.facts["focus_metrics"]] == [
        "优良率",
        "结荚率",
        "开花率",
    ]
    assert len(summary.facts["focus_metrics"]) <= 4
    assert "播种率" not in [
        item["display_name"] for item in summary.facts["focus_metrics"]
    ]


def test_good_excellent_expresses_week_last_year_and_five_year_layers() -> None:
    summary = _real_summary()
    good = next(
        item for item in summary.facts["metrics"] if item["metric"] == "GOOD_EXCELLENT"
    )

    assert summary.facts["good_excellent_definition"] == "GOOD + EXCELLENT"
    assert good["current_pct"] == 65
    assert good["week_change_pct_points"] == 1
    assert good["last_year_pct"] == 70
    assert good["difference_vs_last_year_pct_points"] == -5
    assert good["five_year_mean_pct"] == 63
    assert good["difference_vs_five_year_pct_points"] == 2
    assert "优良率较上周**回升** 1个百分点" in summary.detail_text
    assert "**低于** 去年同期5个百分点" in summary.detail_text
    assert "**高于** 5年平均2个百分点" in summary.detail_text
    assert "作物状况恶化" not in summary.detail_text


def test_real_progress_uses_percentage_points_and_lead_lag_rules() -> None:
    summary = _real_summary()
    metrics = {item["metric"]: item for item in summary.facts["metrics"]}

    assert metrics["BLOOMING"]["direction_vs_last_year"] == "领先"
    assert metrics["BLOOMING"]["direction_vs_five_year"] == "落后"
    assert metrics["SETTING_PODS"]["direction_vs_last_year"] == "领先"
    assert metrics["SETTING_PODS"]["direction_vs_five_year"] == "接近"
    assert "开花率较去年同期 **领先**、较5年平均 **落后**" in summary.detail_text
    assert "结荚率较去年同期 **领先**、较5年平均 **接近**" in summary.detail_text
    assert "个百分点" in summary.detail_text
    assert "增长6.4%" not in summary.detail_text
    assert "pct" not in summary.detail_text.lower()


def test_progress_fallback_is_retained_in_facts_without_changing_matching_rule() -> None:
    baseline = pd.Timestamp("2026-06-14")
    rows = _metric_history(
        "PLANTED",
        baseline=str(baseline.date()),
        current=95,
        previous=92,
        historical=(93, 91, 90, 89, 88),
    )
    target = baseline - pd.Timedelta(weeks=52)
    rows = [row for row in rows if pd.Timestamp(row["week_ending"]) != target]
    rows.append(_row("PLANTED", target - pd.Timedelta(days=1), 93))
    summary = build_crop_summary(
        pd.DataFrame(rows),
        _empty_frame(),
        source_identity={"fixture": "fallback"},
        generated_at=NOW,
        display_config=DISPLAY_CONFIG,
    )
    fact = summary.facts["metrics"][0]

    assert fact["comparison_week"] == target.date().isoformat()
    assert fact["fallback_used"] is True
    assert fact["last_year_pct"] == 93


def test_five_year_mean_requires_all_five_values_and_never_fills_zero() -> None:
    rows = _metric_history(
        "PLANTED",
        baseline="2026-06-14",
        current=95,
        previous=92,
        historical=(93, 91, 90, 89, 88),
    )
    rows.pop()
    summary = build_crop_summary(
        pd.DataFrame(rows),
        _empty_frame(),
        source_identity={"fixture": "four-years"},
        generated_at=NOW,
        display_config=DISPLAY_CONFIG,
    )
    fact = summary.facts["metrics"][0]

    assert fact["five_year_sample_count"] == 4
    assert fact["five_year_mean_pct"] is None
    assert "历史样本不足" in summary.detail_text
    assert "5年均值 | 0%" not in summary.detail_text


def test_late_season_configuration_activates_late_metrics_without_month_logic() -> None:
    progress_rows = []
    progress_rows.extend(
        _metric_history(
            "MATURE",
            baseline="2026-10-04",
            current=70,
            previous=55,
            historical=(60, 58, 57, 56, 55),
        )
    )
    progress_rows.extend(
        _metric_history(
            "HARVESTED",
            baseline="2026-10-04",
            current=40,
            previous=25,
            historical=(45, 46, 44, 43, 42),
        )
    )
    condition_rows = _metric_history(
        "GOOD_EXCELLENT",
        baseline="2026-10-04",
        current=62,
        previous=61,
        historical=(65, 64, 63, 62, 61),
    )
    summary = build_crop_summary(
        pd.DataFrame(progress_rows),
        pd.DataFrame(condition_rows),
        source_identity={"fixture": "late-season"},
        generated_at=NOW,
        display_config=DISPLAY_CONFIG,
    )

    assert summary.facts["current_stage"] == "late_season"
    assert [item["display_name"] for item in summary.facts["focus_metrics"]] == [
        "优良率",
        "收割率",
        "成熟率",
    ]


def test_state_anomalies_are_bounded_facts_without_contribution_attribution() -> None:
    summary = _real_summary()
    states = summary.facts["state_anomalies"]

    assert 0 < len(states) <= 5
    assert all(abs(item["week_change_pct_points"]) >= 3 for item in states)
    assert "州级变化" in summary.detail_text
    assert "以下仅列州级优良率变化事实" in summary.detail_text
    for forbidden in ("贡献全国", "导致全国", "拖累全国"):
        assert forbidden not in summary.detail_text
        assert forbidden not in summary.short_text


def test_research_overview_short_text_reuses_summary_without_tables_or_technical_fields() -> None:
    summary = _real_summary()
    app = AppTest.from_string(
        f"""
from summary_panel import render_summary_panel
payload = {summary.to_dict()!r}
render_summary_panel(payload, compact=True)
""",
        default_timeout=20,
    )
    app.run()
    text = "\n".join(str(item.value) for item in app.markdown)

    assert not app.exception
    assert _crop_short_markdown(summary.short_text) in text
    assert "| 指标 |" not in text
    assert "PLANTED" not in text and "GOOD_EXCELLENT" not in text
    assert "summary-rules-v1" not in text
    assert "**低于**" not in text and "**高于**" not in text


def test_full_panel_shows_only_comprehensive_and_hides_structured_sections() -> None:
    summary = _real_summary()
    app = AppTest.from_string(
        f"""
from summary_panel import render_summary_panel
payload = {summary.to_dict()!r}
render_summary_panel(payload)
""",
        default_timeout=20,
    )
    app.run()
    text = "\n".join(str(item.value) for item in [*app.markdown, *app.caption])

    assert not app.exception
    assert "各指标最新有效周" in text
    assert "详见“当前有效指标”" not in text
    assert "### 综合" in text
    assert "### 重点变化" not in text
    assert "### 当前有效指标" not in text
    assert "### 州级变化" not in text
    assert "规则 summary-rules-v1" not in text
    assert "source identity" not in text.lower()
    assert summary.facts["focus_metrics"]
    assert summary.facts["metrics"]
    assert summary.facts["state_anomalies"]


def test_crop_ui_comprehensive_uses_existing_facts_and_only_bolds_directions() -> None:
    summary = _real_summary()
    rendered = _crop_comprehensive_markdown(summary.to_dict())
    emphasized = re.findall(r"\*\*([^*]+)\*\*", rendered)

    assert "开花率50%" in rendered
    assert "较去年同期 **领先** 3个百分点" in rendered
    assert "较5年平均 **落后** 3个百分点" in rendered
    assert "结荚率19%" in rendered
    assert "优良率65%" in rendered
    assert "较上周 **回升** 1个百分点" in rendered
    assert "较去年同期 **低** 5个百分点" in rendered
    assert "较5年平均 **高** 2个百分点" in rendered
    assert "整体来看" in rendered
    assert "肯塔基州" not in rendered and "北卡罗来纳州" not in rendered
    assert emphasized
    assert set(emphasized).issubset({"领先", "落后", "接近", "回升", "下降", "上升", "高", "低"})
    assert not [value for value in emphasized if re.search(r"\d|%|百分点", value)]


def test_markdown_bold_contains_semantics_not_numbers_dates_or_units() -> None:
    summary = _real_summary()
    emphasized = re.findall(r"\*\*([^*]+)\*\*", summary.detail_text + summary.short_text)

    assert emphasized
    assert not [value for value in emphasized if re.search(r"\d|%|百分点|2026", value)]
    assert set(emphasized).issubset(
        {
            "领先",
            "落后",
            "接近",
            "上升",
            "下降",
            "回升",
            "高于",
            "低于",
            "领先历史",
            "落后历史",
            "高于历史",
            "低于历史",
            "偏快",
            "偏慢",
        }
    )


def test_summary_never_emits_market_or_yield_judgements() -> None:
    summary = _real_summary()
    for forbidden in ("利多", "利空", "单产下降", "减产", "丰产"):
        assert forbidden not in summary.detail_text
        assert forbidden not in summary.short_text
