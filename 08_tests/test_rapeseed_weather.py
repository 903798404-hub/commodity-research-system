from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for directory in (PROJECT_ROOT / "03_src", PROJECT_ROOT / "04_scripts", PROJECT_ROOT / "05_apps"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import import_soybean_weather_snapshot as snapshot_import
import soybean_weather_page
from agri_research_agent.weather.soybean_weather import load_weather_config, weighted_values


CAN_CONFIG = PROJECT_ROOT / "02_configs" / "rapeseed_weather_can.yaml"
AUS_CONFIG = PROJECT_ROOT / "02_configs" / "rapeseed_weather_aus.yaml"


def test_canada_configuration_enables_all_sections_and_uses_99_denominator() -> None:
    config = load_weather_config(CAN_CONFIG)
    assert all(config["enabled_sections"].values())
    assert [region["key"] for region in config["regions"]] == [
        "saskatchewan_weighted", "alberta_weighted", "manitoba_weighted"
    ]
    value, coverage = weighted_values(
        pd.Series({"saskatchewan_weighted": 10.0, "alberta_weighted": 20.0, "manitoba_weighted": 30.0}), config
    )
    assert coverage == pytest.approx(99.0)
    assert value == pytest.approx((10 * 55 + 20 * 28 + 30 * 16) / 99)
    assert snapshot_import.expected_columns(config, snapshot_import.target_tables(config)["加拿大_土壤墒情"])[-3:] == [
        "萨省_土壤墒情", "阿尔伯塔_土壤墒情", "曼尼托巴_土壤墒情"
    ]
    assert config["chart_windows"]["temperature_min"] == {"start": "04-01", "end": "10-31"}
    region = soybean_weather_page.region_weights(config).iloc[0]
    assert soybean_weather_page._region_label(region, config) == "加拿大_萨斯喀彻温省（55%）"


def test_australia_configuration_disables_summaries_and_never_assigns_state_weight_per_subregion() -> None:
    config = load_weather_config(AUS_CONFIG)
    assert config["weighted_aggregation"] is False
    assert config["enabled_sections"]["rainfall_summary"] is False
    assert config["enabled_sections"]["temperature_summary"] is False
    assert config["enabled_sections"]["minimum_temperature"] is True
    assert config["chart_windows"]["temperature_min"] == {"start": "05-01", "end": "12-31"}
    assert all("weight" not in region for region in config["regions"])
    assert soybean_weather_page.MODULES["rainfall_summary"][0] not in [
        soybean_weather_page.MODULES[key][0] for key, enabled in config["enabled_sections"].items() if enabled
    ]
    assert soybean_weather_page._region_label(type("Region", (), {"display_name": "Midlands", "parent_label": "西澳大利亚州（38%产量）", "weight": 0})(), config) == "西澳大利亚州（38%产量） - Midlands"
    direct_state = soybean_weather_page.region_weights(config).iloc[5]
    assert soybean_weather_page._region_label(direct_state, config) == "新南威尔士州（33%产量）"


def test_australia_non_numeric_snapshot_values_are_explicit_quality_issues_not_zeroes(tmp_path: Path) -> None:
    config = load_weather_config(AUS_CONFIG)
    table = "澳洲_降雨"
    spec = snapshot_import.target_tables(config)[table]
    columns = snapshot_import.expected_columns(config, spec)
    values = [["2026-02-17", *["not-a-number"] * (len(columns) - 1)]]
    issues: list[dict[str, str]] = []
    assert list(snapshot_import._record_rows(table, columns, values, config, pd.Timestamp("2026-07-21", tz="UTC"), snapshot_import.target_tables(config), issues)) == []
    assert len(issues) == len(config["regions"])
    assert {issue["reason"] for issue in issues} == {"non_numeric_source_value"}
