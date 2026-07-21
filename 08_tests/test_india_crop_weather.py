from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for directory in (PROJECT_ROOT / "03_src", PROJECT_ROOT / "04_scripts", PROJECT_ROOT / "05_apps"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import crop_weather_page
import import_crop_weather_snapshot as snapshot_import
from agri_research_agent.data_sources.weather_adapter import validate_weather_records
from agri_research_agent.weather.crop_weather import add_season_columns, load_weather_config


FORMAL_ENTRY = PROJECT_ROOT / "05_apps" / "streamlit_app.py"
CONFIGS = {
    "IND_COTTON": PROJECT_ROOT / "02_configs" / "cotton_weather_ind.yaml",
    "IND_SUGARCANE": PROJECT_ROOT / "02_configs" / "sugarcane_weather_ind.yaml",
}


def _importance_total(config: dict[str, object]) -> float:
    return sum(float(str(region["importance_label"]).removesuffix("%")) for region in config["regions"])


@pytest.mark.parametrize(
    ("route_key", "crop", "expected_keys", "expected_total", "expected_start", "expected_soil_start", "title"),
    [
        ("IND_COTTON", "cotton", ["maharashtra", "gujarat", "rajasthan", "karnataka", "andhra_pradesh", "haryana"], 73.0, "05-01", "04-01", "印度_马哈拉施特拉邦（Maharashtra）- 29%"),
        ("IND_SUGARCANE", "sugarcane", ["uttar_pradesh", "maharashtra", "karnataka"], 83.33, "02-01", "02-01", "印度_北方邦（Uttar Pradesh）- 48.57%"),
    ],
)
def test_india_crop_configs_use_label_only_regions_and_approved_cross_year_windows(
    route_key: str,
    crop: str,
    expected_keys: list[str],
    expected_total: float,
    expected_start: str,
    expected_soil_start: str,
    title: str,
) -> None:
    config = load_weather_config(CONFIGS[route_key])

    assert config["crop"] == crop
    assert config["country"] == config["country_code"] == "IND"
    assert config["route_key"] == route_key
    assert [region["key"] for region in config["regions"]] == expected_keys
    assert _importance_total(config) == pytest.approx(expected_total)
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
    assert config["chart_windows"]["cumulative_rain"]["start"] == expected_start
    assert config["chart_windows"]["temperature"]["start"] == expected_start
    assert config["chart_windows"]["soil"]["start"] == expected_soil_start
    assert config["season_end_month_day"] == "01-31"
    assert crop_weather_page._region_label(crop_weather_page.region_weights(config).iloc[0], config) == title


@pytest.mark.parametrize("route_key", ["IND_COTTON", "IND_SUGARCANE"])
def test_india_crop_import_reads_only_seven_approved_stable_tables(route_key: str) -> None:
    config = load_weather_config(CONFIGS[route_key])
    tables = snapshot_import.target_tables(config)

    assert len(tables) == 7
    assert set(spec.metric for spec in tables.values()) == {"precipitation", "temperature_max", "soil_moisture"}
    assert not any("最低气温" in table for table in tables)
    assert all(table.startswith("印度_") for table in tables)


def test_crop_year_labels_and_reference_axes_follow_each_module_window() -> None:
    cotton = load_weather_config(CONFIGS["IND_COTTON"])
    sugar = load_weather_config(CONFIGS["IND_SUGARCANE"])
    cotton_dates = pd.DataFrame({"date": pd.to_datetime(["2026-05-01", "2027-01-31", "2026-04-01"])})
    sugar_dates = pd.DataFrame({"date": pd.to_datetime(["2026-02-01", "2027-01-31"])})

    assert add_season_columns(cotton_dates, cotton).loc[:1, "season"].tolist() == ["2026/2027", "2026/2027"]
    assert add_season_columns(cotton_dates.iloc[[2]], cotton, season_start_month_day="04-01")["season"].tolist() == ["2026/2027"]
    assert add_season_columns(sugar_dates, sugar)["season"].tolist() == ["2026/2027", "2026/2027"]
    assert tuple(str(value.date()) for value in crop_weather_page._reference_window("05-01", "01-31")) == ("2000-05-01", "2001-01-31")
    assert tuple(str(value.date()) for value in crop_weather_page._reference_window("04-01", "01-31")) == ("2000-04-01", "2001-01-31")
    assert tuple(str(value.date()) for value in crop_weather_page._reference_window("02-01", "01-31")) == ("2000-02-01", "2001-01-31")


def test_route_keys_and_cache_identity_are_isolated() -> None:
    cotton = crop_weather_page._country_files("IND_COTTON")
    sugar = crop_weather_page._country_files("IND_SUGARCANE")

    assert cotton["config"] != sugar["config"]
    assert cotton["data"] != sugar["data"]
    assert cotton["status"] != sugar["status"]
    parameters = inspect.signature(crop_weather_page._load_selected_records.__wrapped__).parameters
    assert {"data_path", "data_mtime_ns", "data_size", "crop", "country", "metric", "route_key"} <= set(parameters)


def _india_weather_records() -> pd.DataFrame:
    """Small non-sensitive fixture exercising the approved no-fill daily window."""

    rows: list[dict[str, object]] = []
    for year in range(2020, 2027):
        for month_day, value in (("05-01", 3.0), ("06-16", 4.0)):
            date = pd.Timestamp(f"{year}-{month_day}")
            rows.append(
                {
                    "date": date, "crop": "cotton", "country": "IND", "region": "maharashtra",
                    "metric": "precipitation", "data_type": "observed", "model": "observed", "value": value,
                    "unit": "mm", "source_updated_at": "2026-06-22T00:00:00Z",
                }
            )
    for model, value in (("ECMWF", 8.0), ("GFS", 9.0)):
        rows.append(
            {
                "date": pd.Timestamp("2026-06-19"), "crop": "cotton", "country": "IND", "region": "maharashtra",
                "metric": "precipitation", "data_type": "forecast", "model": model, "value": value,
                "unit": "mm", "source_updated_at": "2026-06-22T00:00:00Z",
            }
        )
    records = pd.DataFrame(rows)
    records["forecast_run_at"] = pd.Timestamp("2026-06-18")
    return records


def test_india_daily_rain_uses_approved_28_day_window_without_filling_gaps() -> None:
    config = load_weather_config(CONFIGS["IND_COTTON"])
    records = _india_weather_records()
    region = crop_weather_page.region_weights(config).iloc[0]

    start, end = crop_weather_page._daily_rain_default_window(records)  # type: ignore[misc]
    assert (start, end) == (pd.Timestamp("2026-06-03"), pd.Timestamp("2026-06-30"))
    assert (end - start).days + 1 == 28

    figure = crop_weather_page._daily_rain_figure(records, region, (start, end), config)
    assert figure.layout.xaxis.tickformat == "%m-%d"
    assert figure.layout.xaxis.rangeslider.thickness == pytest.approx(0.05)
    history = next(trace for trace in figure.data if trace.name == "历史降雨")
    ec = next(trace for trace in figure.data if trace.name == "EC预测")
    assert pd.Timestamp(max(history.x)) == pd.Timestamp("2000-06-16")
    assert list(ec.x) == [pd.Timestamp("2000-06-19")]
    assert pd.Timestamp("2000-06-17") not in set(history.x) | set(ec.x)


def test_india_history_figure_keeps_cross_year_reference_axis_and_forecast_continuation() -> None:
    config = load_weather_config(CONFIGS["IND_COTTON"])
    records = _india_weather_records()
    region = crop_weather_page.region_weights(config).iloc[0]

    figure = crop_weather_page._region_line_figure(records, config, region, kind="cumulative_rain")
    traces = {trace.name: trace for trace in figure.data}
    assert figure.layout.xaxis.tickformat == "%m-%d"
    assert traces["2026/2027"].line.color == crop_weather_page.HISTORY_SERIES_COLORS["current"]
    assert traces["2025/2026"].line.color == crop_weather_page.HISTORY_SERIES_COLORS["previous"]
    assert traces["5年均值"].line.dash == "dash"
    for model_name in ("GFS预测", "EC预测"):
        assert traces[model_name].x[0] == traces["2026/2027"].x[-1]
        assert traces[model_name].y[0] == traces["2026/2027"].y[-1]


@pytest.mark.parametrize("crop", ["cotton", "sugarcane"])
def test_adapter_accepts_india_crop_records(crop: str) -> None:
    record = pd.DataFrame(
        {
            "date": ["2026-06-16"], "crop": [crop], "country": ["IND"], "region": ["sample"],
            "metric": ["precipitation"], "data_type": ["observed"], "model": ["observed"], "value": [1.0],
            "unit": ["mm"], "source_updated_at": ["2026-06-22T00:00:00Z"],
        }
    )
    assert len(validate_weather_records(record)) == 1


def test_india_crop_weather_routes_render_from_the_formal_workspace_entry() -> None:
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=30).run()
    app.session_state["selected_workspace_page"] = "印度作物天气"
    app.run(timeout=30)
    assert not app.exception
    country_radio = next(item for item in app.radio if item.label == "国家/地区")
    assert country_radio.options == ["印度棉花", "印度甘蔗"]
    assert country_radio.value == "印度棉花"
    assert any(item.value == "印度棉花天气研究" for item in app.title)
    section_radio = next(item for item in app.radio if item.label == "页面章节")
    assert section_radio.options == ["单日降雨", "累计降雨", "最高气温", "土壤墒情"]

    country_radio.set_value("印度甘蔗").run(timeout=30)
    assert not app.exception
    assert any(item.value == "印度甘蔗天气研究" for item in app.title)
    section_radio = next(item for item in app.radio if item.label == "页面章节")
    assert section_radio.options == ["单日降雨", "累计降雨", "最高气温", "土壤墒情"]
