from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.summary_engine.weather import (
    _build_focus_regions,
    _focus_regions_sentence,
    _rain_history_cell,
    _rain_comprehensive_sentence,
    _soil_comprehensive_sentence,
    _temperature_comprehensive_sentence,
    build_weather_summary,
)
from agri_research_agent.weather.crop_weather import load_weather_config


ROOT = Path(__file__).resolve().parents[2]
WEATHER_ROOT = ROOT / "08_tests" / "fixtures" / "summary" / "weather"
CASES = (
    ("USA", "soybean/us/soybean_weather_us.parquet", "soybean_weather_us.yaml", "soybean/us/soybean_weather_us_30y_normal.parquet", "weighted"),
    ("BRA", "soybean/br/soybean_weather_br.parquet", "soybean_weather_br.yaml", "soybean/br/soybean_weather_br_30y_normal.parquet", "weighted"),
    ("ARG", "soybean/ar/soybean_weather_ar.parquet", "soybean_weather_ar.yaml", "soybean/ar/soybean_weather_ar_30y_normal.parquet", "weighted"),
    ("CAN", "rapeseed/can/rapeseed_weather_can.parquet", "rapeseed_weather_can.yaml", "rapeseed/can/rapeseed_weather_can_30y_normal.parquet", "weighted"),
    ("AUS", "rapeseed/aus/rapeseed_weather_aus.parquet", "rapeseed_weather_aus.yaml", None, "regional"),
    ("EU", "rapeseed/eu/rapeseed_weather_eu.parquet", "rapeseed_weather_eu.yaml", None, "regional"),
    ("RUS", "rapeseed/rus/rapeseed_weather_rus.parquet", "rapeseed_weather_rus.yaml", None, "regional"),
    ("UKR", "rapeseed/ukr/rapeseed_weather_ukr.parquet", "rapeseed_weather_ukr.yaml", None, "regional"),
    ("MYS", "palm_oil/mys/palm_oil_weather_mys.parquet", "palm_oil_weather_mys.yaml", None, "regional"),
    ("IDN", "palm_oil/idn/palm_oil_weather_idn.parquet", "palm_oil_weather_idn.yaml", None, "regional"),
    ("IND_COTTON", "cotton/ind/cotton_weather_ind.parquet", "cotton_weather_ind.yaml", None, "regional"),
    ("IND_SUGARCANE", "sugarcane/ind/sugarcane_weather_ind.parquet", "sugarcane_weather_ind.yaml", None, "regional"),
)


def _summary(case: tuple[str, str, str, str | None, str]):
    code, relative, config_name, normal_relative, _ = case
    records = pd.read_parquet(WEATHER_ROOT / relative)
    normals = pd.read_parquet(WEATHER_ROOT / normal_relative) if normal_relative else pd.DataFrame()
    config = load_weather_config(ROOT / "02_configs" / config_name)
    return code, build_weather_summary(records, normals, config, source_identity={"fixture": relative})


@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
def test_every_formal_weather_country_builds_the_approved_template(case) -> None:
    code, summary = _summary(case)
    expected_mode = case[-1]
    assert summary.facts["aggregation_mode"] == expected_mode
    assert summary.source_date == "2026-06-16"
    assert summary.missing_reason is None
    assert summary.detail_text.startswith("### 综合")
    assert "短期EC" in summary.detail_text
    assert "中期GFS" in summary.detail_text
    assert "EC" in summary.detail_text and "GFS" in summary.detail_text
    assert "### 重点关注" in summary.detail_text
    assert "### 完整降水数据" in summary.detail_text
    assert "### 完整最高气温数据" in summary.detail_text
    assert "### 完整土墒数据" in summary.detail_text
    assert summary.detail_text.index("### 综合") < summary.detail_text.index("### 重点关注") < summary.detail_text.index("### 完整降水数据") < summary.detail_text.index("### 完整最高气温数据") < summary.detail_text.index("### 完整土墒数据")
    assert "0—100厘米" in summary.detail_text
    assert "较前7天" in summary.detail_text
    assert "较7日前" in summary.detail_text
    assert "温度百分比" not in summary.detail_text
    assert "summary-rules" not in summary.detail_text
    assert "产量占比" not in summary.detail_text
    if expected_mode == "regional":
        assert "全国加权" not in summary.short_text
        assert "30年" not in summary.detail_text
        assert summary.facts["aggregation_mode"] == "regional"
        assert "| **加权结果" not in summary.detail_text
        assert "较5年同期" in summary.detail_text
    else:
        assert summary.facts["fixed_denominator_pct"] > 0
        assert "30年同期" in summary.detail_text
        assert "加权结果" in summary.detail_text


def test_us_weighted_template_has_complete_windows_temperature_and_soil() -> None:
    _, summary = _summary(CASES[0])
    facts = summary.facts
    assert facts["observed_7d"]["previous_week_mm"] is not None
    assert facts["forecast_week_1"]["models"].keys() == {"ECMWF", "GFS"}
    assert facts["forecast_week_2"]["models"].keys() == {"ECMWF", "GFS"}
    assert "℃" in summary.detail_text
    assert "%" in summary.detail_text
    assert "百分点" in summary.detail_text
    assert facts["soil_moisture"]["unit"] == "%"
    assert facts["soil_moisture"]["depth"] == "0-100cm"
    assert facts["fixed_denominator_pct"] == 88.9
    assert facts["observed_7d"]["coverage_pct"] <= 88.9
    assert facts["previous_7d_end"] < facts["current_7d_start"]
    assert (pd.Timestamp(facts["current_7d_end"]) - pd.Timestamp(facts["current_7d_start"])).days == 6
    assert (pd.Timestamp(facts["previous_7d_end"]) - pd.Timestamp(facts["previous_7d_start"])).days == 6
    assert len(facts["regions"]) == 15
    assert all(row["observed_7d"]["delta_vs_previous_7d_mm"] is not None for row in facts["regions"])
    assert all(row["temperature_7d"]["delta_vs_previous_7d_c"] is not None for row in facts["regions"])


def test_palm_templates_are_dynamic_monthly_regional_summaries() -> None:
    for case in CASES[8:10]:
        _, summary = _summary(case)
        assert "6月累计降雨较5年同期均值" in summary.detail_text
        assert summary.facts["aggregation_mode"] == "regional"
        assert all("month_to_forecast_end" in row for row in summary.facts["regions"])
        assert all(
            row["month_to_forecast_end"]["models"][model]["precipitation_mm"] is not None
            for row in summary.facts["regions"]
            for model in ("ECMWF", "GFS")
        )


def test_missing_country_summary_does_not_change_other_country() -> None:
    _, usa = _summary(CASES[0])
    config = load_weather_config(ROOT / "02_configs" / "soybean_weather_br.yaml")
    missing = build_weather_summary(pd.DataFrame(columns=["date", "data_type"]), pd.DataFrame(), config, source_identity={"missing": True})
    assert missing.freshness_status == "missing"
    assert usa.facts["observed_7d"]["precipitation_mm"] is not None


def _model(relative: float, temperature: float = 0.0) -> dict[str, float]:
    return {
        "relative_anomaly_pct": relative,
        "absolute_anomaly_mm": 10.0 if relative >= 0 else -10.0,
        "temperature_anomaly_c": temperature,
    }


def _week(ec: float, gfs: float, ec_temp: float = 0.0, gfs_temp: float = 0.0) -> dict[str, object]:
    return {"models": {"ECMWF": _model(ec, ec_temp), "GFS": _model(gfs, gfs_temp)}}


def test_classification_words_are_bold_but_numbers_are_not() -> None:
    _, canada = _summary(CASES[3])
    assert "**显著偏多** +75.5%" in canada.detail_text
    assert "**偏多** +34.3%" in canada.detail_text
    # +34.3% is moderate, so two wet models do not both imply significant wetness.
    assert "**方向一致偏湿**" in canada.detail_text
    assert "**偏低**" in canada.detail_text
    assert "**+75.5%**" not in canada.detail_text
    assert "**-2.7℃**" not in canada.detail_text
    assert "见上述分项" not in canada.detail_text


def test_canada_current_facts_generate_research_comprehensive_text() -> None:
    _, canada = _summary(CASES[3])
    comprehensive = canada.facts["weather_render"]["comprehensive"]
    assert "过去一周降雨**接近正常**" in comprehensive
    assert "未来两周EC/GFS均显示降雨**偏多**" in comprehensive
    assert "呈**方向一致偏湿**" in comprehensive
    assert "第二周两模型**一致显著偏湿**" not in comprehensive
    assert "未来两周最高气温整体**偏低**，EC冷信号更强" in comprehensive
    assert "萨斯喀彻温（55%）" in comprehensive
    assert "近7日土墒走弱" in comprehensive
    assert "加拿大菜籽产区呈**偏湿**、**偏凉**特征" in comprehensive


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    (
        (_week(45, 35), _week(65, 70), "未来两周EC/GFS均显示降雨**偏多**"),
        (_week(45, 35), _week(-45, -35), "短期水分条件改善后出现**转干信号**"),
        (_week(-45, -35), _week(-65, -70), "降雨连续偏少"),
        (_week(45, 35), _week(45, -35), "第二周**模型方向分歧**"),
        (_week(45, -35), _week(5, 5), "未来第一周**模型方向分歧**"),
        (_week(45, 35), _week(75.5, 70), "第二周两模型**一致显著偏湿**"),
        (_week(45, 35), _week(75.5, 34.3), "呈**方向一致偏湿**"),
    ),
)
def test_rainfall_scenarios_generate_distinct_comprehensive_text(first, second, expected) -> None:
    observed = {"relative_anomaly_pct": 0.0, "absolute_anomaly_mm": 10.0}
    assert expected in _rain_comprehensive_sentence(observed, first, second)


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    (
        (_week(0, 0, 2.0, 1.5), _week(0, 0, 2.5, 1.2), "整体**偏高**"),
        (_week(0, 0, -2.0, -1.5), _week(0, 0, -2.5, -1.2), "整体**偏低**"),
        (_week(0, 0, 2.0, 1.5), _week(0, 0, 0.5, -0.2), "第一周最高气温**偏高**，第二周趋于**中性**"),
    ),
)
def test_temperature_scenarios_enter_comprehensive_text(first, second, expected) -> None:
    assert expected in _temperature_comprehensive_sentence(first, second)


def test_soil_comprehensive_uses_explicit_fact_only_display_rule() -> None:
    assert "均略高于5年同期" in _soil_comprehensive_sentence([{"difference_pct_points": 0.2}, {"difference_pct_points": 0.8}])
    assert "部分区域0—100cm土壤含水率低于5年同期" in _soil_comprehensive_sentence([{"difference_pct_points": -0.2}, {"difference_pct_points": 0.8}])


def test_weather_comprehensive_never_adds_market_or_damage_judgments() -> None:
    forbidden = ("利多", "利空", "做多", "做空", "单产下降", "产量受损", "严重干旱", "高温胁迫")
    for case in CASES:
        _, summary = _summary(case)
        assert not any(word in summary.detail_text or word in summary.short_text for word in forbidden)


def test_weighted_region_labels_and_rows_use_only_approved_config_weights() -> None:
    for case in CASES[:4]:
        _, summary = _summary(case)
        assert all(row["weight_pct"] is not None and f'（{row["weight_pct"]:g}%）' in row["display_label"] for row in summary.facts["regions"])
        assert "加权结果（" in summary.detail_text
    _, canada = _summary(CASES[3])
    labels = [row["display_label"] for row in canada.facts["regions"]]
    assert labels == ["萨斯喀彻温（55%）", "阿尔伯塔（28%）", "曼尼托巴（16%）"]
    assert "| 产量占比 |" not in canada.detail_text


def test_regional_countries_keep_config_order_and_have_no_weighted_result() -> None:
    for case in CASES[4:]:
        code, summary = _summary(case)
        assert "| **加权结果" not in summary.detail_text
        orders = [row["display_order"] for row in summary.facts["regions"]]
        assert orders == sorted(orders)
        if code not in {"EU"}:
            assert all(row["weight_pct"] is None and "（—）" not in row["display_label"] for row in summary.facts["regions"])


def test_all_regions_have_rain_temperature_and_soil_seven_day_trends() -> None:
    for case in CASES:
        _, summary = _summary(case)
        for row in summary.facts["regions"]:
            rain = row["observed_7d"]
            temperature = row["temperature_7d"]
            soil = row["soil_moisture"]
            assert rain["current_7d_mm"] is not None
            assert rain["previous_7d_mm"] is not None
            assert rain["delta_vs_previous_7d_mm"] == pytest.approx(rain["current_7d_mm"] - rain["previous_7d_mm"])
            assert temperature["current_7d_c"] is not None
            assert temperature["previous_7d_c"] is not None
            assert temperature["delta_vs_previous_7d_c"] == pytest.approx(temperature["current_7d_c"] - temperature["previous_7d_c"])
            assert soil["change_vs_seven_days_ago_pct_points"] == pytest.approx(soil["current_pct"] - soil["seven_days_ago_pct"])
            assert "temperature" not in summary.detail_text.lower() and "温度百分比" not in summary.detail_text


def test_canada_soil_seven_day_comparison_uses_same_weighted_identity() -> None:
    _, canada = _summary(CASES[3])
    identities = {"saskatchewan_weighted", "alberta_weighted", "manitoba_weighted"}
    assert {row["region_identity"] for row in canada.facts["regions"]} == identities
    for row in canada.facts["regions"]:
        soil = row["soil_moisture"]
        assert soil["region_identity"] == row["region_identity"]
        assert soil["seven_days_ago_region_identity"] == row["region_identity"]
        assert soil["comparison_identity_matches"] is True
        assert soil["comparison_tolerance_days"] == 0
        assert (pd.Timestamp(soil["date"]) - pd.Timestamp(soil["seven_days_ago_date"])).days == 7


def test_missing_strict_soil_comparison_is_not_filled_with_zero() -> None:
    case = CASES[3]
    records = pd.read_parquet(WEATHER_ROOT / case[1])
    records["date"] = pd.to_datetime(records["date"])
    mask = records.region.eq("saskatchewan_weighted") & records.metric.eq("soil_moisture") & records.data_type.eq("observed") & records.date.eq(pd.Timestamp("2026-06-08"))
    records = records[~mask]
    normals = pd.read_parquet(WEATHER_ROOT / case[3])
    config = load_weather_config(ROOT / "02_configs" / case[2])
    summary = build_weather_summary(records, normals, config, source_identity={"fixture": "missing-seven-day-soil"})
    row = next(item for item in summary.facts["regions"] if item["region_identity"] == "saskatchewan_weighted")
    assert row["soil_moisture"]["seven_days_ago_pct"] is None
    assert row["soil_moisture"]["change_vs_seven_days_ago_pct_points"] is None
    assert "无7日可比值" in summary.detail_text


def _focus_row(name: str, history: float, trend: float, order: int = 1, weight: float | None = None) -> dict[str, object]:
    return {
        "region_identity": name,
        "display_label": name,
        "display_order": order,
        "weight_pct": weight,
        "soil_moisture": {"difference_pct_points": history, "change_vs_seven_days_ago_pct_points": trend},
        "observed_7d": {"relative_anomaly_pct": 0.0, "absolute_anomaly_mm": 10.0},
        "forecast_weeks": [_week(0, 0), _week(0, 0)],
    }


def test_focus_region_distinguishes_absolute_level_and_recent_trend() -> None:
    rows = [_focus_row("核心区（55%）", 0.7, -4.4, weight=55)]
    focus = _build_focus_regions(rows, weighted=True)[0]
    assert focus["current_state"] == "土墒接近5年同期（+0.7个百分点）"
    assert focus["recent_change"] == "7日下降4.4个百分点"
    assert focus["status"] == "关注"


def test_focus_regions_are_limited_to_three_and_short_text_uses_trend() -> None:
    rows = [_focus_row(f"地区{i}", -1.0, -1.0, order=i) for i in range(1, 6)]
    for row in rows:
        row["forecast_weeks"] = [_week(-40, -40), _week(0, 0)]
    text = _focus_regions_sentence(rows, weighted=False)
    assert sum(f"地区{i}" in text for i in range(1, 6)) == 3
    _, canada = _summary(CASES[3])
    assert "近7日土墒走弱" in canada.short_text


def test_focus_table_is_limited_to_five_and_never_contains_aggregate_row() -> None:
    for case in CASES:
        _, summary = _summary(case)
        focus = summary.facts["focus_regions"]
        assert len(focus) <= 5
        assert all("加权结果" not in row["region"] for row in focus)
        assert all(row["status"] in {"风险", "关注", "平稳"} for row in focus)
        if focus:
            assert focus[0]["region"] in summary.short_text


def test_risk_requires_compound_conditions_and_single_anomaly_is_not_risk() -> None:
    single = _focus_row("单一异常", -2.0, 0.5)
    single["forecast_weeks"] = [_week(-70, -70), _week(0, 0)]
    assert _build_focus_regions([single], weighted=False)[0]["status"] == "关注"
    compound = _focus_row("复合异常", -2.0, -1.0)
    compound["forecast_weeks"] = [_week(-70, -70), _week(0, 0)]
    assert _build_focus_regions([compound], weighted=False)[0]["status"] == "风险"


def test_stable_is_neutral_priority_wording_not_favorable_claim() -> None:
    stable = _focus_row("平稳区", 0.0, 0.0, weight=55)
    focus = _build_focus_regions([stable], weighted=True)[0]
    assert focus["status"] == "平稳"
    assert "有利" not in focus["reason"] and "安全" not in focus["reason"]


def test_low_base_guard_renders_absolute_mm_but_preserves_relative_fact() -> None:
    item = {"relative_anomaly_pct": 679.8, "absolute_anomaly_mm": 3.4}
    assert _rain_history_cell(item) == "**接近正常**（距平 +3.4mm）"
    assert item["relative_anomaly_pct"] == 679.8


def test_brazil_real_low_base_case_replaces_679_percent_with_absolute_mm() -> None:
    _, brazil = _summary(CASES[1])
    rain = brazil.facts["weather_render"]["rain_markdown"]
    assert "接近正常** +679.8%" not in rain
    assert "**接近正常**（距平 +4.4mm）" in rain


def test_regional_all_stable_focus_can_be_empty() -> None:
    assert _build_focus_regions([_focus_row("普通区域", 0.0, 0.0)], weighted=False) == []
