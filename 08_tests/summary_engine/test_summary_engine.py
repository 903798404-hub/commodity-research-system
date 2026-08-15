from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from agri_research_agent.summary_engine.basis import build_basis_summary
from agri_research_agent.summary_engine.rules import (
    classify_model_consistency,
    classify_rainfall,
    classify_temperature,
    freshness_status,
)
from agri_research_agent.summary_engine.schema import Summary
from agri_research_agent.summary_engine.crop import build_crop_summary
from agri_research_agent.summary_engine.weather import build_weather_summary


NOW = datetime(2026, 8, 13, tzinfo=timezone.utc)


def test_summary_schema_round_trip_and_required_fields() -> None:
    summary = Summary.create(
        module="test", source_dataset="fixture", source_identity={"sha256": "abc"},
        source_date="2026-08-12", comparison_identity=None, generated_at=NOW,
        calculation_version="summary-calc-v1", rule_version="summary-rules-v1",
        freshness_status="fresh", facts={"value": 1}, classifications=["neutral"],
        headline="测试", detail_text="确定性事实。", short_text="测试：1。",
    )
    assert Summary.from_dict(summary.to_dict()) == summary
    assert summary.summary_id == Summary.from_dict(summary.to_dict()).summary_id


def test_summary_generation_is_stable_for_same_file_identity() -> None:
    kwargs = dict(module="test", source_dataset="fixture", source_identity={"mtime_ns": 1_700_000_000_000_000_000}, source_date="2026-08-12", comparison_identity=None, generated_at=None, calculation_version="v1", rule_version="v1", freshness_status="fresh", facts={}, classifications=[], headline="h", detail_text="d", short_text="s")
    assert Summary.create(**kwargs).to_dict() == Summary.create(**kwargs).to_dict()


def test_freshness_uses_versioned_module_thresholds() -> None:
    assert freshness_status("basis", "2026-08-12", today=pd.Timestamp("2026-08-13").date()) == "fresh"
    assert freshness_status("weather", "2026-06-16", today=pd.Timestamp("2026-08-13").date()) == "stale"


def test_rainfall_boundaries_and_five_mm_guard() -> None:
    assert classify_rainfall(-20, -8) == "rainfall_near_normal"
    assert classify_rainfall(-20.1, -8) == "rainfall_below_normal"
    assert classify_rainfall(-40, -8) == "rainfall_below_normal"
    assert classify_rainfall(-60, -8) == "rainfall_significantly_below_normal"
    assert classify_rainfall(91, 20) == "rainfall_significantly_above_normal"
    assert classify_rainfall(100, 4.9) == "rainfall_near_normal_absolute_guard"


def test_temperature_uses_celsius_and_models_compare_direction_first() -> None:
    assert classify_temperature(0.9) == "temperature_near_normal"
    assert classify_temperature(2.5) == "temperature_clearly_above_normal"
    assert classify_temperature(-3) == "temperature_significantly_below_normal"
    assert classify_model_consistency(91, 36) == "models_same_direction_different_intensity"
    assert classify_model_consistency(5, -55) == "one_neutral_one_directional"
    assert classify_model_consistency(35, -42) == "models_opposite_direction"


def test_basis_uses_full_key_and_never_invents_zero() -> None:
    rows = [
        ("2026-08-10", "豆粕", "华东", "基差报价", "现货", "2609", -70),
        ("2026-08-12", "豆粕", "华东", "基差报价", "现货", "2609", -65),
        ("2026-08-12", "豆粕", "华东", "基差报价", "现货", "2701", -30),
    ]
    frame = pd.DataFrame(rows, columns=["date", "commodity", "region", "quote_type", "delivery_month", "futures_contract", "basis"])
    summary = build_basis_summary(frame, source_identity={"fixture": 1}, generated_at=NOW)
    facts = summary.facts["quotes"]
    same_contract = next(item for item in facts if item["futures_contract"] == "2609")
    rolled = next(item for item in facts if item["futures_contract"] == "2701")
    assert same_contract["previous_date"] == "2026-08-10"
    assert same_contract["change"] == 5
    assert rolled["change"] is None and rolled["missing_reason"] == "换月"
    assert same_contract["cash_price"] is None
    assert summary.source_identity == {"fixture": 1}
    assert summary.comparison_identity["key"] == ["commodity", "region", "quote_type", "delivery_month", "futures_contract"]
    assert summary.rule_version and summary.calculation_version


def test_basis_optional_quote_point_is_part_of_comparison_key() -> None:
    frame = pd.DataFrame([
        ("2026-08-10", "一豆", "华东", "A厂", "基差报价", "现货", "2609", 90),
        ("2026-08-12", "一豆", "华东", "A厂", "基差报价", "现货", "2609", 100),
        ("2026-08-12", "一豆", "华东", "B厂", "基差报价", "现货", "2609", 80),
    ], columns=["date", "commodity", "region", "quote_point", "quote_type", "delivery_month", "futures_contract", "basis"])
    summary = build_basis_summary(frame, source_identity={"fixture": 4}, generated_at=NOW)
    quotes = {item["quote_point"]: item for item in summary.facts["quotes"]}
    assert quotes["A厂"]["change"] == 10
    assert quotes["B厂"]["change"] is None
    assert summary.comparison_identity["key"][-1] == "quote_point"


def test_basis_zero_change_renders_without_false_direction() -> None:
    frame = pd.DataFrame([
        ("2026-08-10", "豆粕", "华南", "基差报价", "现货", "2609", 50),
        ("2026-08-12", "豆粕", "华南", "基差报价", "现货", "2609", 50),
    ], columns=["date", "commodity", "region", "quote_type", "delivery_month", "futures_contract", "basis"])
    summary = build_basis_summary(frame, source_identity={"fixture": 5}, generated_at=NOW)
    assert "50（0）" in summary.detail_text


def test_crop_uses_each_metric_latest_week_and_hides_previous_season() -> None:
    rows = []
    for year in range(2021, 2027):
        for metric, date, value in (("PLANTED", f"{year}-06-14", 90 + (year == 2026) * 5), ("GOOD_EXCELLENT", f"{year}-07-12", 70 - (year == 2026) * 5)):
            rows.append({"geography_level": "US", "region_name": "US TOTAL", "calendar_year": year,
                "metric": metric, "week_ending": pd.Timestamp(date), "value_pct": value})
    rows.append({"geography_level": "US", "region_name": "US TOTAL", "calendar_year": 2025,
        "metric": "HARVESTED", "week_ending": pd.Timestamp("2025-11-16"), "value_pct": 95})
    frame = pd.DataFrame(rows)
    summary = build_crop_summary(frame[frame.metric != "GOOD_EXCELLENT"], frame[frame.metric == "GOOD_EXCELLENT"], source_identity={"fixture": 1}, generated_at=NOW)
    facts = {item["metric"]: item for item in summary.facts["metrics"]}
    assert facts["PLANTED"]["week_ending"] == "2026-06-14"
    assert facts["GOOD_EXCELLENT"]["week_ending"] == "2026-07-12"
    assert "HARVESTED" not in facts
    assert summary.facts["state_weights_role"] == "display_only"


def test_weather_soil_contract_and_nonweighted_regions() -> None:
    dates = pd.date_range("2021-06-10", "2026-06-20")
    rows = []
    for date in dates:
        if date.year < 2026 and date.strftime("%m-%d") != "06-16":
            continue
        for region in ("a", "b"):
            rows.extend([
                {"date": date, "region": region, "metric": "precipitation", "data_type": "observed", "model": "observed", "value": 1.0},
                {"date": date, "region": region, "metric": "temperature_max", "data_type": "observed", "model": "observed", "value": 25.0},
                {"date": date, "region": region, "metric": "soil_moisture", "data_type": "observed", "model": "observed", "value": 0.22},
            ])
    for date in pd.date_range("2026-06-21", periods=14):
        for region in ("a", "b"):
            for model in ("ECMWF", "GFS"):
                rows.extend([
                    {"date": date, "region": region, "metric": "precipitation", "data_type": "forecast", "model": model, "value": 2.0, "forecast_run_at": pd.Timestamp("2026-06-20", tz="UTC")},
                    {"date": date, "region": region, "metric": "temperature_max", "data_type": "forecast", "model": model, "value": 27.0, "forecast_run_at": pd.Timestamp("2026-06-20", tz="UTC")},
                ])
    records = pd.DataFrame(rows); records["source_updated_at"] = pd.Timestamp("2026-06-20", tz="UTC"); records["forecast_run_at"] = records.get("forecast_run_at")
    normals = pd.DataFrame([{"month_day": d.strftime("%m-%d"), "region": region, "metric": metric, "normal_value": value}
        for d in pd.date_range("2026-06-01", "2026-07-10") for region in ("a", "b") for metric, value in (("precipitation", 1.0), ("temperature_max", 25.0))])
    config = {"crop": "soybean", "summary_subject": "美豆", "page_title": "测试天气", "season_start_month_day": "01-01", "weighted_aggregation": True, "weighted_coverage_percent": 100.0,
        "regions": [{"key": "a", "display_name": "A", "display_order": 1, "weight": 50}, {"key": "b", "display_name": "B", "display_order": 2, "weight": 50}]}
    summary = build_weather_summary(records, normals, config, source_identity={"fixture": 1}, generated_at=NOW)
    assert summary.facts["soil_moisture"]["unit"] == "%"
    assert summary.facts["soil_moisture"]["depth"] == "0-100cm"
    assert summary.facts["soil_moisture"]["classification"] == "classification_pending"
    config["weighted_aggregation"] = False; config.pop("weighted_coverage_percent")
    regional = build_weather_summary(records, normals.iloc[0:0], config, source_identity={"fixture": 2}, generated_at=NOW)
    assert regional.facts["aggregation_mode"] == "regional"
    assert len(regional.facts["regions"]) == 2
    assert "不生成未经批准的全国加权结果" in regional.detail_text


def test_weather_normalizes_object_timestamp_dates_at_adapter_boundary() -> None:
    records = pd.DataFrame([
        {"date": pd.Timestamp("2026-06-19").to_pydatetime().date(), "region": "a", "metric": "precipitation", "data_type": "observed", "model": "observed", "value": 1.0},
        {"date": pd.Timestamp("2026-06-20"), "region": "a", "metric": "precipitation", "data_type": "observed", "model": "observed", "value": 2.0},
    ], dtype=object)
    normals = pd.DataFrame([
        {"month_day": "06-19", "region": "a", "metric": "precipitation", "normal_value": 1.0},
        {"month_day": "06-20", "region": "a", "metric": "precipitation", "normal_value": 1.0},
    ])
    config = {
        "crop": "soybean", "page_title": "天气", "weighted_aggregation": True,
        "weighted_coverage_percent": 100.0,
        "regions": [{"key": "a", "display_name": "A", "display_order": 1, "weight": 100}],
    }
    summary = build_weather_summary(records, normals, config, source_identity={"fixture": 3}, generated_at=NOW)
    assert summary.source_date == "2026-06-20"
    assert summary.facts["observed_7d"]["precipitation_mm"] == 3.0
