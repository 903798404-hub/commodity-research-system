from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for directory in (PROJECT_ROOT / "03_src", PROJECT_ROOT / "04_scripts", PROJECT_ROOT / "05_apps"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import import_crop_weather_snapshot as snapshot_import
import crop_weather_page
from agri_research_agent.weather.crop_weather import load_weather_config, select_latest_forecasts


EU_CONFIG = PROJECT_ROOT / "02_configs" / "rapeseed_weather_eu.yaml"
RUS_CONFIG = PROJECT_ROOT / "02_configs" / "rapeseed_weather_rus.yaml"
UKR_CONFIG = PROJECT_ROOT / "02_configs" / "rapeseed_weather_ukr.yaml"


def _forecast_records(config: dict[str, object], table: str) -> pd.DataFrame:
    spec = snapshot_import.target_tables(config)[table]
    columns = snapshot_import.expected_columns(config, spec)
    rows = list(
        snapshot_import._record_rows(
            table,
            columns,
            [["2026-06-19", *range(1, len(columns))]],
            config,
            pd.Timestamp("2026-06-22", tz="UTC"),
            snapshot_import.target_tables(config),
        )
    )
    return pd.DataFrame(rows)


def test_eu_config_preserves_germany_ec_absence_and_cross_year_modules() -> None:
    config = load_weather_config(EU_CONFIG)
    assert config["season_label_style"] == "range"
    assert (config["season_start_month_day"], config["season_end_month_day"]) == ("08-01", "07-31")
    assert [region["display_name"] for region in config["regions"]] == ["法国", "德国", "波兰", "罗马尼亚", "捷克", "匈牙利", "丹麦"]
    assert sum(region["weight"] for region in config["regions"]) == 80
    assert config["weighted_aggregation"] is False
    assert config["enabled_sections"] == {
        "rainfall_summary": False, "temperature_summary": False, "daily_rainfall": True,
        "cumulative_rainfall": True, "maximum_temperature": True, "minimum_temperature": True, "soil_moisture": True,
    }
    ec_rows = _forecast_records(config, "欧盟_降雨_预测_ec")
    assert "germany" not in set(ec_rows["region"])
    assert set(ec_rows["region"]) == {region["key"] for region in config["regions"]} - {"germany"}
    assert snapshot_import.expected_columns(config, snapshot_import.target_tables(config)["欧盟_降雨_预测_ec"]) == [
        "日期", "France_precip_ec", "Poland_precip_ec", "Romania_precip_ec", "Czech Republic_precip_ec", "Hungary_precip_ec", "Denmark_precip_ec"
    ]
    assert crop_weather_page._reference_window("08-01", "07-31") == (pd.Timestamp("2000-08-01"), pd.Timestamp("2001-07-31"))
    assert crop_weather_page._region_label(crop_weather_page.region_weights(config).iloc[0], config) == "欧盟_法国（21.0%）"


def test_russia_is_nine_direct_series_with_label_only_parent_weights() -> None:
    config = load_weather_config(RUS_CONFIG)
    assert len(config["regions"]) == 9
    assert all("weight" not in region for region in config["regions"])
    assert [region["parent_label"] for region in config["regions"][:4]] == ["西伯利亚联邦区（37%）"] * 4
    assert config["weighted_aggregation"] is False
    assert config["enabled_sections"]["minimum_temperature"] is True
    assert config["chart_windows"] == {
        "cumulative_rain": {"start": "04-01", "end": "11-20"},
        "temperature": {"start": "04-01", "end": "11-20"},
        "temperature_min": {"start": "04-01", "end": "11-20"},
        "soil": {"start": "03-01", "end": "12-31"},
    }
    assert crop_weather_page._reference_window("04-01", "11-20") == (pd.Timestamp("2000-04-01"), pd.Timestamp("2000-11-20"))
    regions = crop_weather_page.region_weights(config)
    assert crop_weather_page._region_label(regions.iloc[0], config) == "俄罗斯*西伯利亚联邦区（37%）*鄂木斯克州"
    assert crop_weather_page._region_label(regions.iloc[4], config) == "俄罗斯_中央联邦区（30%）"


def test_ukraine_is_one_unweighted_national_cross_year_series() -> None:
    config = load_weather_config(UKR_CONFIG)
    assert config["regions"] == [{"key": "ukraine", "display_name": "乌克兰（National）", "display_order": 1, "source_column": "乌克兰"}]
    assert config["weighted_aggregation"] is False
    assert config["season_label_style"] == "range"
    assert (config["season_start_month_day"], config["season_end_month_day"]) == ("09-01", "08-31")
    assert config["enabled_sections"]["minimum_temperature"] is True
    assert crop_weather_page._reference_window("09-01", "08-31") == (pd.Timestamp("2000-09-01"), pd.Timestamp("2001-08-31"))
    assert crop_weather_page._region_label(crop_weather_page.region_weights(config).iloc[0], config) == "乌克兰（National）"


def test_soil_axis_is_configuration_driven_and_hides_reference_year() -> None:
    settings = {
        EU_CONFIG: 30,
        RUS_CONFIG: 25,
        UKR_CONFIG: 30,
    }
    for path, interval in settings.items():
        config = load_weather_config(path)
        options = config["chart_axis"]["soil"]
        figure = crop_weather_page._line_layout("原始值，单位待确认", "03-01", "12-31", axis_options=options)
        assert figure.layout.xaxis.tickformat == "%m-%d"
        assert figure.layout.xaxis.dtick == interval * 24 * 60 * 60 * 1000
        assert figure.layout.xaxis.tickangle == -45
        assert figure.layout.xaxis.automargin is True
        assert figure.layout.margin.b == 70


def test_forecast_gaps_and_germany_ec_warning_remain_explicit_without_runtime_data() -> None:
    config = load_weather_config(EU_CONFIG)
    records = pd.DataFrame(
        [
            {"date": "2026-06-16", "data_type": "observed", "model": "observed", "source_updated_at": "2026-06-22"},
            {"date": "2026-06-19", "data_type": "forecast", "model": "GFS", "forecast_run_at": "2026-06-22", "source_updated_at": "2026-06-22"},
        ]
    )
    records["date"] = pd.to_datetime(records["date"])
    records["forecast_run_at"] = pd.to_datetime(records.get("forecast_run_at"), utc=True)
    records["source_updated_at"] = pd.to_datetime(records["source_updated_at"], utc=True)
    forecast = select_latest_forecasts(records, pd.Timestamp("2026-06-16"))
    assert forecast["date"].tolist() == [pd.Timestamp("2026-06-19")]
    assert config["status_warnings"] == ["germany_ec_forecast_unavailable"]
    assert "GFS" in config["warning_messages"]["germany_ec_forecast_unavailable"]
