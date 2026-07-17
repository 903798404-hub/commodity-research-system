from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.pipelines.soybean_crop_comparison import (
    MATCH_METHOD_EXACT,
    MATCH_METHOD_MISSING,
    MATCH_METHOD_PREVIOUS,
    MetricDefinition,
    build_dashboard_comparisons,
    build_metric_comparison,
    comparison_display_table,
    delta_cell_style,
    load_display_config,
    metric_definitions,
    round_half_up,
    style_comparison_table,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DISPLAY_CONFIG_FILE = (
    PROJECT_ROOT / "02_configs" / "soybean_crop_progress_display.yaml"
)
PROCESSED_DIR = (
    PROJECT_ROOT / "01_data" / "processed" / "soybean_crop_progress"
)
PROGRESS_FILE = PROCESSED_DIR / "soybeans_crop_progress_weekly_2021_2026.parquet"
CONDITION_FILE = PROCESSED_DIR / "soybeans_crop_condition_weekly_2021_2026.parquet"


def _small_config() -> dict[str, object]:
    return {
        "national": {"region_name": "US TOTAL", "display_name": "美国全国"},
        "states": [
            {
                "region_name": "ILLINOIS",
                "display_name": "伊利诺伊州",
                "display_weight_pct": 14.2,
            },
            {
                "region_name": "IOWA",
                "display_name": "艾奥瓦州",
                "display_weight_pct": 13.8,
            },
        ],
    }


def _row(
    metric: str,
    geography_level: str,
    region_name: str,
    week: str | pd.Timestamp,
    value: float | None,
) -> dict[str, object]:
    timestamp = pd.Timestamp(week)
    return {
        "metric": metric,
        "geography_level": geography_level,
        "region_name": region_name,
        "calendar_year": timestamp.year,
        "week_ending": timestamp,
        "value_pct": value,
    }


def _full_history_rows(
    metric: str = "PLANTED",
    baseline: str = "2026-07-12",
    geography_level: str = "US",
    region_name: str = "US TOTAL",
    current_value: float = 70.0,
    historical_values: tuple[float, ...] = (60.0, 50.0, 40.0, 30.0, 20.0),
) -> list[dict[str, object]]:
    baseline_week = pd.Timestamp(baseline)
    rows = [
        _row(metric, geography_level, region_name, baseline_week, current_value)
    ]
    rows.extend(
        _row(
            metric,
            geography_level,
            region_name,
            baseline_week - pd.Timedelta(weeks=offset),
            value,
        )
        for offset, value in zip((52, 104, 156, 208, 260), historical_values)
    )
    return rows


def _definition(metric: str = "PLANTED") -> MetricDefinition:
    return MetricDefinition(metric, "progress", "播种率", "美豆播种率")


def test_config_contains_six_chinese_metrics_and_display_only_state_order() -> None:
    config = load_display_config(DISPLAY_CONFIG_FILE)
    definitions = metric_definitions(config)

    assert [(item.metric, item.tab_label, item.display_name) for item in definitions] == [
        ("PLANTED", "播种率", "美豆播种率"),
        ("EMERGED", "出苗率", "美豆出苗率"),
        ("BLOOMING", "开花率", "美豆开花率"),
        ("SETTING_PODS", "结荚率", "美豆结荚率"),
        ("HARVESTED", "收割率", "美豆收割率"),
        ("GOOD_EXCELLENT", "优良率", "美豆优良率"),
    ]
    assert config["weight_role"] == "display_only"
    assert config["weight_source"] == "user_provided"
    assert config["weight_vintage"] == "unknown"
    assert len(config["states"]) == 18
    assert [state["region_name"] for state in config["states"][:3]] == [
        "ILLINOIS",
        "IOWA",
        "MINNESOTA",
    ]
    assert [state["display_name"] for state in config["states"][:3]] == [
        "伊利诺伊州",
        "艾奥瓦州",
        "明尼苏达州",
    ]
    assert [state["display_weight_pct"] for state in config["states"]] == sorted(
        [state["display_weight_pct"] for state in config["states"]], reverse=True
    )


def test_exact_date_is_preferred_over_previous_within_seven_days() -> None:
    rows = _full_history_rows()
    baseline = pd.Timestamp("2026-07-12")
    last_year_week = baseline - pd.Timedelta(weeks=52)
    rows.extend(
        [
            _row("PLANTED", "US", "US TOTAL", last_year_week - pd.Timedelta(days=1), 99),
            *_full_history_rows(
                geography_level="STATE", region_name="ILLINOIS", current_value=80
            ),
            _row("PLANTED", "STATE", "IOWA", baseline, 77),
            _row("PLANTED", "STATE", "IOWA", last_year_week - pd.Timedelta(days=1), 88),
        ]
    )

    comparison = build_metric_comparison(
        pd.DataFrame(rows), _definition(), _small_config(), 2026
    )
    national = comparison.rows.iloc[0]
    iowa = comparison.rows.loc[comparison.rows["region_name"].eq("IOWA")].iloc[0]

    assert comparison.baseline_week == baseline
    assert national["previous_value_pct"] == 60
    assert national["five_year_mean_pct"] == 40
    assert national["historical_match_count"] == 5
    assert national["last_year_match_method"] == MATCH_METHOD_EXACT
    assert national["five_year_match_methods"] == (MATCH_METHOD_EXACT,) * 5
    assert national["five_year_fallback_count"] == 0
    assert iowa["previous_value_pct"] == 88
    assert iowa["last_year_match_method"] == MATCH_METHOD_PREVIOUS
    assert pd.isna(iowa["five_year_mean_pct"])
    assert iowa["historical_match_count"] == 1


def test_nearest_previous_record_within_seven_days_completes_five_year_mean() -> None:
    rows = _full_history_rows()
    baseline = pd.Timestamp("2026-07-12")
    last_year_week = baseline - pd.Timedelta(weeks=52)
    rows = [
        row
        for row in rows
        if pd.Timestamp(row["week_ending"]) != last_year_week
    ]
    rows.extend(
        [
            _row("PLANTED", "US", "US TOTAL", last_year_week - pd.Timedelta(days=5), 55),
            _row("PLANTED", "US", "US TOTAL", last_year_week - pd.Timedelta(days=1), 59),
        ]
    )

    comparison = build_metric_comparison(
        pd.DataFrame(rows), _definition(), _small_config(), 2026
    )
    national = comparison.rows.iloc[0]

    assert national["previous_value_pct"] == 59
    assert national["last_year_match_method"] == MATCH_METHOD_PREVIOUS
    assert national["five_year_match_methods"] == (
        MATCH_METHOD_PREVIOUS,
        MATCH_METHOD_EXACT,
        MATCH_METHOD_EXACT,
        MATCH_METHOD_EXACT,
        MATCH_METHOD_EXACT,
    )
    assert national["five_year_fallback_count"] == 1
    assert national["historical_match_count"] == 5
    assert national["five_year_mean_pct"] == pytest.approx(39.8)


def test_future_and_records_older_than_seven_days_remain_missing() -> None:
    rows = _full_history_rows()
    baseline = pd.Timestamp("2026-07-12")
    last_year_week = baseline - pd.Timedelta(weeks=52)
    rows = [
        row
        for row in rows
        if pd.Timestamp(row["week_ending"]) != last_year_week
    ]
    rows.extend(
        [
            _row("PLANTED", "US", "US TOTAL", last_year_week + pd.Timedelta(days=1), 91),
            _row("PLANTED", "US", "US TOTAL", last_year_week - pd.Timedelta(days=8), 89),
        ]
    )

    comparison = build_metric_comparison(
        pd.DataFrame(rows), _definition(), _small_config(), 2026
    )
    national = comparison.rows.iloc[0]

    assert pd.isna(national["previous_value_pct"])
    assert national["last_year_match_method"] == MATCH_METHOD_MISSING
    assert national["five_year_fallback_count"] == 0
    assert national["historical_match_count"] == 4
    assert pd.isna(national["five_year_mean_pct"])


def test_good_excellent_uses_exact_matches_only() -> None:
    baseline = pd.Timestamp("2026-07-12")
    last_year_week = baseline - pd.Timedelta(weeks=52)
    rows = _full_history_rows(metric="GOOD_EXCELLENT")
    rows = [
        row
        for row in rows
        if pd.Timestamp(row["week_ending"]) != last_year_week
    ]
    rows.append(
        _row(
            "GOOD_EXCELLENT",
            "US",
            "US TOTAL",
            last_year_week - pd.Timedelta(days=1),
            66,
        )
    )
    definition = MetricDefinition(
        "GOOD_EXCELLENT", "condition", "优良率", "美豆优良率"
    )

    comparison = build_metric_comparison(
        pd.DataFrame(rows), definition, _small_config(), 2026
    )
    national = comparison.rows.iloc[0]

    assert pd.isna(national["previous_value_pct"])
    assert national["last_year_match_method"] == MATCH_METHOD_MISSING
    assert national["five_year_fallback_count"] == 0
    assert pd.isna(national["five_year_mean_pct"])


def test_fallback_never_uses_another_region_or_metric() -> None:
    rows = _full_history_rows()
    baseline = pd.Timestamp("2026-07-12")
    last_year_week = baseline - pd.Timedelta(weeks=52)
    rows = [
        row
        for row in rows
        if pd.Timestamp(row["week_ending"]) != last_year_week
    ]
    rows.extend(
        [
            _row("PLANTED", "STATE", "ILLINOIS", last_year_week - pd.Timedelta(days=1), 88),
            _row("EMERGED", "US", "US TOTAL", last_year_week - pd.Timedelta(days=1), 77),
        ]
    )

    comparison = build_metric_comparison(
        pd.DataFrame(rows), _definition(), _small_config(), 2026
    )
    national = comparison.rows.iloc[0]

    assert pd.isna(national["previous_value_pct"])
    assert national["last_year_match_method"] == MATCH_METHOD_MISSING
    assert pd.isna(national["five_year_mean_pct"])


def test_five_year_mean_is_missing_when_only_four_historical_years_match() -> None:
    rows = _full_history_rows()
    rows.pop()
    comparison = build_metric_comparison(
        pd.DataFrame(rows), _definition(), _small_config(), 2026
    )

    national = comparison.rows.iloc[0]
    assert national["previous_value_pct"] == 60
    assert national["historical_match_count"] == 4
    assert national["five_year_match_methods"][-1] == MATCH_METHOD_MISSING
    assert pd.isna(national["five_year_mean_pct"])
    assert pd.isna(national["change_vs_five_year_display"])


def test_display_rounding_is_half_up_and_deltas_use_displayed_integers() -> None:
    rows = _full_history_rows(
        current_value=62.5,
        historical_values=(60.4, 61.4, 62.4, 63.4, 64.9),
    )
    comparison = build_metric_comparison(
        pd.DataFrame(rows), _definition(), _small_config(), 2026
    )
    national = comparison.rows.iloc[0]

    assert national["five_year_mean_pct"] == pytest.approx(62.5)
    assert round_half_up(62.5) == 63
    assert national["current_display"] == 63
    assert national["previous_display"] == 60
    assert national["five_year_display"] == 63
    assert national["change_vs_previous_display"] == 3
    assert national["change_vs_five_year_display"] == 0


def test_national_is_direct_us_total_and_states_cannot_generate_or_overwrite_it() -> None:
    rows = _full_history_rows(current_value=55)
    rows.extend(
        _full_history_rows(
            geography_level="STATE", region_name="ILLINOIS", current_value=99
        )
    )
    rows.extend(
        _full_history_rows(
            geography_level="STATE", region_name="IOWA", current_value=1
        )
    )
    comparison = build_metric_comparison(
        pd.DataFrame(rows), _definition(), _small_config(), 2026
    )

    national = comparison.rows.iloc[0]
    assert national["geography_level"] == "US"
    assert national["region_name"] == "US TOTAL"
    assert national["display_region"] == "美国全国"
    assert national["current_value_pct"] == 55
    assert comparison.rows.iloc[1]["display_region"] == "伊利诺伊州（14.2%）"


def test_state_current_value_uses_the_national_baseline_week() -> None:
    baseline = pd.Timestamp("2026-07-12")
    rows = _full_history_rows()
    rows.extend(
        _full_history_rows(
            geography_level="STATE", region_name="ILLINOIS", current_value=82
        )
    )
    rows.extend(
        _full_history_rows(
            baseline="2026-07-05",
            geography_level="STATE",
            region_name="IOWA",
            current_value=91,
        )
    )
    comparison = build_metric_comparison(
        pd.DataFrame(rows), _definition(), _small_config(), 2026
    )
    illinois = comparison.rows.loc[
        comparison.rows["region_name"].eq("ILLINOIS")
    ].iloc[0]
    iowa = comparison.rows.loc[comparison.rows["region_name"].eq("IOWA")].iloc[0]

    assert illinois["baseline_week"] == baseline
    assert illinois["current_value_pct"] == 82
    assert pd.isna(iowa["current_value_pct"])


def test_weight_changes_only_order_and_label_not_values() -> None:
    rows = _full_history_rows()
    rows.extend(
        _full_history_rows(
            geography_level="STATE", region_name="ILLINOIS", current_value=75
        )
    )
    rows.extend(
        _full_history_rows(
            geography_level="STATE", region_name="IOWA", current_value=65
        )
    )
    original = _small_config()
    reversed_config = _small_config()
    reversed_config["states"] = list(reversed(reversed_config["states"]))

    first = build_metric_comparison(pd.DataFrame(rows), _definition(), original, 2026)
    second = build_metric_comparison(
        pd.DataFrame(rows), _definition(), reversed_config, 2026
    )

    assert first.rows.iloc[0]["current_value_pct"] == second.rows.iloc[0]["current_value_pct"]
    assert first.rows.iloc[1]["region_name"] == "ILLINOIS"
    assert second.rows.iloc[1]["region_name"] == "IOWA"
    assert first.rows.set_index("region_name").loc["ILLINOIS", "current_value_pct"] == 75
    assert second.rows.set_index("region_name").loc["ILLINOIS", "current_value_pct"] == 75


def test_delta_styles_cover_positive_negative_zero_and_missing() -> None:
    assert "#dcfce7" in delta_cell_style(1)
    assert "#166534" in delta_cell_style(1)
    assert "#fee2e2" in delta_cell_style(-1)
    assert "#991b1b" in delta_cell_style(-1)
    assert "#ffffff" in delta_cell_style(0)
    assert "#111827" in delta_cell_style(0)
    assert "#f8fafc" in delta_cell_style(pd.NA)

    comparison = build_metric_comparison(
        pd.DataFrame(_full_history_rows()), _definition(), _small_config(), 2026
    )
    html = style_comparison_table(comparison).to_html()
    assert "background-color: #dbeafe" in html
    assert "font-weight: 700" in html
    assert "—" in html


def test_real_processed_data_uses_each_metrics_own_latest_national_week() -> None:
    progress = pd.read_parquet(PROGRESS_FILE)
    condition = pd.read_parquet(CONDITION_FILE)
    config = load_display_config(DISPLAY_CONFIG_FILE)
    comparisons = build_dashboard_comparisons(progress, condition, config)
    by_metric = {comparison.definition.metric: comparison for comparison in comparisons}

    assert {metric: result.baseline_week for metric, result in by_metric.items()} == {
        "PLANTED": pd.Timestamp("2026-06-14"),
        "EMERGED": pd.Timestamp("2026-06-28"),
        "BLOOMING": pd.Timestamp("2026-07-12"),
        "SETTING_PODS": pd.Timestamp("2026-07-12"),
        "HARVESTED": None,
        "GOOD_EXCELLENT": pd.Timestamp("2026-07-12"),
    }
    for metric in ("PLANTED", "EMERGED", "BLOOMING", "SETTING_PODS", "GOOD_EXCELLENT"):
        result = by_metric[metric]
        assert len(result.rows) == 19
        assert result.rows.iloc[0]["region_name"] == "US TOTAL"
        assert comparison_display_table(result).columns.tolist() == [
            "地区",
            "最新进度（%）",
            "去年同期（%）",
            "五年均值（%）",
            "较去年同期（百分点）",
            "较五年均值（百分点）",
        ]

    planted = by_metric["PLANTED"]
    emerged = by_metric["EMERGED"]
    planted_national = planted.rows.iloc[0]
    emerged_national = emerged.rows.iloc[0]
    assert planted.five_year_missing_count == 0
    assert emerged.five_year_missing_count == 0
    assert int(planted.rows["five_year_fallback_count"].sum()) == 6
    assert int(emerged.rows["five_year_fallback_count"].sum()) == 19
    assert planted_national["five_year_mean_pct"] == pytest.approx(94.6)
    assert emerged_national["five_year_mean_pct"] == pytest.approx(95.4)
    assert planted_national["five_year_fallback_count"] == 1
    assert emerged_national["five_year_fallback_count"] == 2


def test_harvested_empty_state_and_future_week_auto_activation() -> None:
    progress = pd.read_parquet(PROGRESS_FILE)
    condition = pd.read_parquet(CONDITION_FILE)
    config = load_display_config(DISPLAY_CONFIG_FILE)
    harvested = {
        comparison.definition.metric: comparison
        for comparison in build_dashboard_comparisons(progress, condition, config)
    }["HARVESTED"]

    assert harvested.current_year == 2026
    assert harvested.baseline_week is None
    assert harvested.latest_historical_week == pd.Timestamp("2025-11-16")
    assert harvested.rows.empty

    new_week = pd.Timestamp("2026-09-20")
    template = progress.loc[
        progress["metric"].eq("HARVESTED")
        & progress["geography_level"].eq("US")
        & progress["region_name"].eq("US TOTAL")
    ].iloc[-1].copy()
    template["calendar_year"] = 2026
    template["week_ending"] = new_week
    template["value_pct"] = 7.0
    updated = pd.concat([progress, pd.DataFrame([template])], ignore_index=True)
    future = {
        comparison.definition.metric: comparison
        for comparison in build_dashboard_comparisons(updated, condition, config)
    }["HARVESTED"]

    assert future.baseline_week == new_week
    assert future.rows.iloc[0]["current_value_pct"] == 7
