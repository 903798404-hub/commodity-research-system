from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for directory in (PROJECT_ROOT / "03_src", PROJECT_ROOT / "04_scripts", PROJECT_ROOT / "05_apps"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import import_crop_weather_snapshot as snapshot_import
import crop_weather_page
from agri_research_agent.data_sources.weather_adapter import validate_weather_records
from agri_research_agent.weather.crop_weather import load_weather_config


CONFIGS = {
    "MYS": PROJECT_ROOT / "02_configs" / "palm_oil_weather_mys.yaml",
    "IDN": PROJECT_ROOT / "02_configs" / "palm_oil_weather_idn.yaml",
}
EXPECTED = {
    "MYS": (10, 99.0, "马来_沙巴州（23.6%）"),
    "IDN": (7, 83.0, "印尼_廖内省（20%）"),
}


def _importance_total(config: dict[str, object]) -> float:
    return sum(float(str(region["importance_label"]).removesuffix("%")) for region in config["regions"])


@pytest.mark.parametrize("country", ["MYS", "IDN"])
def test_palm_oil_config_is_label_only_and_enables_exactly_four_region_modules(country: str) -> None:
    config = load_weather_config(CONFIGS[country])
    count, total, title = EXPECTED[country]

    assert config["crop"] == "palm_oil"
    assert config["country"] == country
    assert len(config["regions"]) == count
    assert _importance_total(config) == pytest.approx(total)
    assert config["weighted_aggregation"] is False
    assert all("weight" not in region for region in config["regions"])
    assert config["enabled_sections"] == {
        "rainfall_summary": False,
        "temperature_summary": False,
        "daily_rainfall": True,
        "cumulative_rainfall": True,
        "maximum_temperature": True,
        "minimum_temperature": False,
        "soil_moisture": True,
    }
    assert config["chart_windows"] == {
        "cumulative_rain": {"start": "01-01", "end": "12-31"},
        "temperature": {"start": "01-01", "end": "12-31"},
        "soil": {"start": "01-01", "end": "12-31"},
    }
    region = crop_weather_page.region_weights(config).iloc[0]
    assert crop_weather_page._region_label(region, config) == title


@pytest.mark.parametrize("country", ["MYS", "IDN"])
def test_palm_oil_import_targets_only_approved_seven_static_tables(country: str) -> None:
    config = load_weather_config(CONFIGS[country])
    tables = snapshot_import.target_tables(config)

    assert len(tables) == 7
    assert {spec.metric for spec in tables.values()} == {"precipitation", "temperature_max", "soil_moisture"}
    assert not any("最低气温" in table for table in tables)
    assert all(table.startswith(str(config["source_table_prefix"]) + "_") for table in tables)


def test_daily_rain_window_is_exactly_twenty_eight_days_and_preserves_a_forecast_gap() -> None:
    latest = pd.Timestamp("2026-06-16")
    records = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-06-16", "2026-06-18", "2026-07-02"]),
            "crop": "palm_oil",
            "country": "MYS",
            "region": "sabah",
            "metric": "precipitation",
            "data_type": ["observed", "forecast", "forecast"],
            "model": ["observed", "ECMWF", "GFS"],
            "value": [1.0, 2.0, 3.0],
            "unit": "mm",
            "source_updated_at": pd.to_datetime(["2026-06-22T00:00:00Z"] * 3),
            "forecast_run_at": pd.to_datetime([None, "2026-06-22T00:00:00Z", "2026-06-22T00:00:00Z"], utc=True),
        }
    )
    start, end = crop_weather_page._daily_rain_default_window(records) or (None, None)

    assert (start, end) == (latest - pd.Timedelta(days=13), latest + pd.Timedelta(days=14))
    assert (end - start).days + 1 == 28
    figure = crop_weather_page._daily_rain_figure(
        records,
        type("Region", (), {"key": "sabah", "display_name": "沙巴州", "weight": 0})(),
        (start, end),
        load_weather_config(CONFIGS["MYS"]),
    )
    trace_dates = {pd.Timestamp(value).strftime("%m-%d") for trace in figure.data for value in trace.x}
    assert "06-17" not in trace_dates
    assert "07-02" not in trace_dates
    assert "06-16" in trace_dates and "06-18" in trace_dates
    extended = crop_weather_page._daily_rain_figure(
        records,
        type("Region", (), {"key": "sabah", "display_name": "沙巴州", "weight": 0})(),
        (start, pd.Timestamp("2026-07-02")),
        load_weather_config(CONFIGS["MYS"]),
    )
    assert "07-02" in {pd.Timestamp(value).strftime("%m-%d") for trace in extended.data for value in trace.x}


@pytest.mark.parametrize("country", ["MYS", "IDN"])
def test_adapter_accepts_only_its_selected_palm_oil_country(country: str) -> None:
    record = pd.DataFrame(
        {
            "date": ["2026-06-16"], "crop": ["palm_oil"], "country": [country], "region": ["sample"],
            "metric": ["precipitation"], "data_type": ["observed"], "model": ["observed"], "value": [1.0],
            "unit": ["mm"], "source_updated_at": ["2026-06-22T00:00:00Z"],
        }
    )
    assert len(validate_weather_records(record)) == 1
