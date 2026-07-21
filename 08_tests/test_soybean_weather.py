from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from agri_research_agent.data_sources.weather_adapter import validate_weather_records
from agri_research_agent.weather.soybean_weather import (
    absolute_and_relative_anomaly,
    aggregate_weighted_daily,
    five_year_mean,
    latest_observation_date,
    load_weather_config,
    region_weights,
    select_latest_forecasts,
    weekly_historical_baseline,
    weekly_metric_summary,
    weighted_values,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = PROJECT_ROOT / "02_configs" / "soybean_weather_us.yaml"
FORMAL_ENTRY = PROJECT_ROOT / "05_apps" / "streamlit_app.py"


def _weather_records() -> pd.DataFrame:
    """Non-sensitive fixture encoding the approved legacy calculation semantics."""

    config = load_weather_config(CONFIG_FILE)
    rows: list[dict[str, object]] = []
    for season_offset, start_year in enumerate(range(2020, 2026)):
        for day_offset in range(7):
            date = pd.Timestamp(year=start_year, month=11, day=1) + pd.Timedelta(days=day_offset)
            for region_index, region in enumerate(config["regions"]):
                base = float(region_index + 1 + season_offset)
                for metric, value, unit in (
                    ("precipitation", base + day_offset, "mm"),
                    ("temperature_max", 20 + base / 10 + day_offset / 5, "degC"),
                    ("temperature_min", 10 + base / 10 + day_offset / 5, "degC"),
                    ("soil_moisture", 40 + base / 10 + day_offset / 10, "原始值，单位待确认"),
                ):
                    rows.append(
                        {
                            "date": date,
                            "crop": "soybean",
                            "country": "USA",
                            "region": region["key"],
                            "metric": metric,
                            "data_type": "observed",
                            "model": "observed",
                            "value": value,
                            "unit": unit,
                            "source_updated_at": "2025-11-07T00:00:00Z",
                            "forecast_run_at": pd.NaT,
                        }
                    )
    for model, bump in (("ECMWF", 0.0), ("GFS", 1.0)):
        for day_offset in range(7, 21):
            date = pd.Timestamp("2025-11-01") + pd.Timedelta(days=day_offset)
            for region_index, region in enumerate(config["regions"]):
                rows.append(
                    {
                        "date": date,
                        "crop": "soybean",
                            "country": "USA",
                        "region": region["key"],
                        "metric": "precipitation",
                        "data_type": "forecast",
                        "model": model,
                        "value": float(region_index + 2 + bump),
                        "unit": "mm",
                        "source_updated_at": "2025-11-07T12:00:00Z",
                        "forecast_run_at": "2025-11-07T12:00:00Z",
                    }
                )
    return validate_weather_records(pd.DataFrame(rows))


def _weather_normals() -> pd.DataFrame:
    """Non-sensitive daily 30-year-normal fixture for page-level calculations."""

    config = load_weather_config(CONFIG_FILE)
    rows: list[dict[str, object]] = []
    for date in pd.date_range("2025-01-01", "2025-12-31", freq="D"):
        for region_index, region in enumerate(config["regions"]):
            for metric, value, unit in (
                ("precipitation", float(region_index + 1), "mm"),
                ("temperature_max", float(20 + region_index / 10), "degC"),
            ):
                rows.append(
                    {
                        "month_day": date.strftime("%m-%d"),
                        "country": "USA",
                        "region": region["key"],
                        "metric": metric,
                        "normal_value": value,
                        "unit": unit,
                        "baseline_label": "沿用旧项目30年历史同期基准",
                        "source_workbook_sha256": "a" * 64,
                        "source_sheet": "fixture",
                    }
                )
    return pd.DataFrame(rows)


def test_approved_weights_are_single_source_and_sum_to_88_9() -> None:
    config = load_weather_config(CONFIG_FILE)
    weights = region_weights(config)

    assert weights["key"].tolist() == [
        "illinois", "iowa", "minnesota", "indiana", "nebraska", "missouri", "ohio",
        "south_dakota", "north_dakota", "kansas", "wisconsin", "michigan", "kentucky",
        "tennessee", "north_carolina",
    ]
    assert weights["weight"].sum() == pytest.approx(88.9)
    assert float(config["weighted_coverage_percent"]) == 88.9
    assert "88.856472" not in CONFIG_FILE.read_text(encoding="utf-8")


def test_fixed_88_9_weighted_value_does_not_renormalize_missing_states() -> None:
    config = load_weather_config(CONFIG_FILE)
    values = pd.Series({region["key"]: index + 1 for index, region in enumerate(config["regions"])})
    weighted, coverage = weighted_values(values, config)
    expected = sum((index + 1) * float(region["weight"]) for index, region in enumerate(config["regions"])) / 88.9
    assert weighted == pytest.approx(expected)
    assert coverage == pytest.approx(88.9)

    partial_weighted, partial_coverage = weighted_values(values.drop("illinois"), config)
    assert partial_coverage == pytest.approx(73.5)
    assert partial_weighted == pytest.approx((expected * 88.9 - 15.4) / 88.9)


def test_authorized_30_year_daily_normals_drive_weekly_values_and_anomalies() -> None:
    config = load_weather_config(CONFIG_FILE)
    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)
    import soybean_weather_page

    assert list(inspect.signature(soybean_weather_page._load_normals).parameters) == [
        "normal_path", "normal_mtime_ns", "normal_size"
    ]

    normals = _weather_normals()
    window = (pd.Timestamp("2025-11-01"), pd.Timestamp("2025-11-07"))
    rain_normal = soybean_weather_page._normal_summary(normals, config, metric="precipitation", window=window)
    high_normal = soybean_weather_page._normal_summary(normals, config, metric="temperature_max", window=window)

    assert rain_normal.loc["illinois", "value"] == pytest.approx(7.0)  # 7 daily values summed.
    assert high_normal.loc["illinois", "value"] == pytest.approx(20.0)  # 7 daily values averaged.
    expected_weighted = sum((index + 1) * float(region["weight"]) for index, region in enumerate(config["regions"])) / 88.9
    assert rain_normal.loc["weighted", "value"] == pytest.approx(expected_weighted * 7)
    assert rain_normal.loc["weighted", "coverage_percent"] == pytest.approx(88.9)

    records = _weather_records()
    current_rain = weekly_metric_summary(
        records, config, metric="precipitation", start=window[0], end=window[1], data_type="observed"
    ).set_index("region")
    current_high = weekly_metric_summary(
        records, config, metric="temperature_max", start=window[0], end=window[1], data_type="observed"
    ).set_index("region")
    assert soybean_weather_page._anomaly(current_rain.loc["illinois", "value"], rain_normal.loc["illinois", "value"], percent=False) == pytest.approx(56.0)
    assert soybean_weather_page._anomaly(current_high.loc["illinois", "value"], high_normal.loc["illinois", "value"], percent=False) == pytest.approx(1.2)
    assert soybean_weather_page._anomaly(current_high.loc["illinois", "value"], high_normal.loc["illinois", "value"], percent=True) == pytest.approx(6.0)


def test_weekly_anomaly_percentage_display_and_threshold_colors() -> None:
    config = load_weather_config(CONFIG_FILE)
    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)
    import soybean_weather_page

    assert soybean_weather_page._anomaly_ratio(160.5, 100.0) == pytest.approx(0.605)
    assert soybean_weather_page._anomaly_pct(160.5, 100.0) == pytest.approx(60.5)
    assert soybean_weather_page._format_value(soybean_weather_page._anomaly_pct(160.5, 100.0), percent=True) == "+60.5%"
    assert soybean_weather_page._format_value(soybean_weather_page._anomaly_pct(69.6, 100.0), percent=True) == "-30.4%"
    assert soybean_weather_page._anomaly_pct(44.0, 27.4) == pytest.approx((44.0 - 27.4) / 27.4 * 100)

    thresholds = {
        -50.0: "anomaly-deep-negative",
        -20.0: "anomaly-medium-negative",
        -0.1: "anomaly-light-negative",
        0.0: "anomaly-light-positive",
        20.0: "anomaly-medium-positive",
        50.0: "anomaly-deep-positive",
        None: "anomaly-unavailable",
    }
    assert {value: soybean_weather_page._anomaly_class(value) for value in thresholds} == thresholds
    styles = inspect.getsource(soybean_weather_page._inject_weather_styles)
    assert ".anomaly-deep-negative {background:#C62828;color:#fff;font-weight:800;}" in styles
    assert ".anomaly-deep-positive {background:#2E7D32;color:#fff;font-weight:800;}" in styles

    regions = region_weights(config).head(1)
    normal = pd.DataFrame({"value": [100.0, 100.0]}, index=["weighted", "illinois"])
    observed = pd.DataFrame({"value": [160.5, 273.9]}, index=["weighted", "illinois"])
    absolute_html = soybean_weather_page._wide_row(
        (pd.Timestamp("2026-06-08"), pd.Timestamp("2026-06-14")),
        regions,
        {"observed": observed},
        normal,
        forecast=False,
        measure="absolute",
        metric="precipitation",
    )
    assert "+60.5" in absolute_html
    assert "+173.9" in absolute_html
    assert absolute_html.count("anomaly-deep-positive") == 2

    ec = pd.DataFrame({"value": [160.5, 160.5]}, index=["weighted", "illinois"])
    gfs = pd.DataFrame({"value": [69.6, 69.6]}, index=["weighted", "illinois"])
    forecast_html = soybean_weather_page._wide_row(
        (pd.Timestamp("2026-06-15"), pd.Timestamp("2026-06-21")),
        regions,
        {"ECMWF": ec, "GFS": gfs},
        normal,
        forecast=True,
        measure="percent",
        metric="precipitation",
    )
    assert forecast_html.count("anomaly-deep-positive") == 2
    assert forecast_html.count("anomaly-medium-negative") == 2

    unavailable_normal = pd.DataFrame({"value": [0.0, float("nan")]}, index=["weighted", "illinois"])
    unavailable_html = soybean_weather_page._wide_row(
        (pd.Timestamp("2026-06-15"), pd.Timestamp("2026-06-21")),
        regions,
        {"observed": observed},
        unavailable_normal,
        forecast=False,
        measure="percent",
        metric="precipitation",
    )
    assert unavailable_html.count("anomaly-unavailable") == 2
    assert unavailable_html.count("—") == 3


def test_legacy_reference_regression_for_weekly_values_baseline_and_forecast_boundary() -> None:
    config = load_weather_config(CONFIG_FILE)
    records = _weather_records()
    latest = latest_observation_date(records)
    assert latest == pd.Timestamp("2025-11-07")
    start = latest - pd.Timedelta(days=6)

    weekly = weekly_metric_summary(
        records, config, metric="precipitation", start=start, end=latest, data_type="observed"
    ).set_index("region")
    illinois_week = weekly.loc["illinois", "value"]
    assert illinois_week == pytest.approx(63.0)  # 7日降雨合计：6 至 12 mm
    expected_weighted = sum((63 + 7 * index) * float(region["weight"]) for index, region in enumerate(config["regions"])) / 88.9
    assert weekly.loc["weighted", "value"] == pytest.approx(expected_weighted)

    baseline = weekly_historical_baseline(
        records, config, metric="precipitation", start=start, end=latest
    ).set_index("region")
    assert baseline.loc["illinois", "baseline_value"] == pytest.approx(42.0)
    assert weekly.loc["illinois", "value"] - baseline.loc["illinois", "baseline_value"] == pytest.approx(21.0)
    absolute, relative = absolute_and_relative_anomaly(
        weekly.loc["illinois", "value"], baseline.loc["illinois", "baseline_value"]
    )
    assert absolute == pytest.approx(21.0)
    assert relative == pytest.approx(50.0)

    current_daily = aggregate_weighted_daily(
        records[
            (records["metric"] == "precipitation")
            & (records["data_type"] == "observed")
            & (records["date"] >= start)
        ],
        config,
    )
    assert current_daily["value"].cumsum().iloc[-1] == pytest.approx(expected_weighted)

    forecasts = select_latest_forecasts(records, latest)
    assert set(forecasts["model"]) == {"ECMWF", "GFS"}
    assert forecasts["date"].min() == pd.Timestamp("2025-11-08")
    assert (forecasts["date"] > latest).all()


def test_weekly_temperature_means_and_five_year_mean_follow_legacy_semantics() -> None:
    config = load_weather_config(CONFIG_FILE)
    records = _weather_records()
    latest = latest_observation_date(records)
    start = latest - pd.Timedelta(days=6)
    high = weekly_metric_summary(
        records, config, metric="temperature_max", start=start, end=latest, data_type="observed"
    ).set_index("region")
    low = weekly_metric_summary(
        records, config, metric="temperature_min", start=start, end=latest, data_type="observed"
    ).set_index("region")
    assert high.loc["illinois", "value"] == pytest.approx(21.2)
    assert low.loc["illinois", "value"] == pytest.approx(11.2)

    precipitation = records[(records["metric"] == "precipitation") & (records["data_type"] == "observed")]
    baseline = five_year_mean(aggregate_weighted_daily(precipitation, config))
    assert not baseline.empty
    assert baseline["value"].notna().all()


def test_daily_rain_default_window_is_28_calendar_days_without_filling_missing_values() -> None:
    config = load_weather_config(CONFIG_FILE)
    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)
    import soybean_weather_page

    precipitation = _weather_records()[lambda frame: frame["metric"] == "precipitation"]
    latest = latest_observation_date(precipitation)
    assert latest == pd.Timestamp("2025-11-07")
    default_window = soybean_weather_page._daily_rain_default_window(precipitation)
    assert default_window == (pd.Timestamp("2025-10-25"), pd.Timestamp("2025-11-21"))
    assert (default_window[1] - default_window[0]).days + 1 == 28

    region = region_weights(config).iloc[0]
    figure = soybean_weather_page._daily_rain_figure(precipitation, region)
    assert tuple(pd.Timestamp(value) for value in figure.layout.xaxis.range) == (
        pd.Timestamp("2000-10-25"), pd.Timestamp("2000-11-21")
    )
    observed, ec, gfs = figure.data
    assert all(pd.Timestamp(value) <= pd.Timestamp("2000-11-07") for value in observed.x)
    assert len(observed.x) == 7  # The missing Oct. days remain absent rather than being zero-filled.
    assert all(float(value) != 0 for value in observed.y)
    for trace in (ec, gfs):
        assert all(pd.Timestamp("2000-11-08") <= pd.Timestamp(value) <= pd.Timestamp("2000-11-21") for value in trace.x)
        assert len(trace.x) == 14
    assert figure.layout.hovermode == "closest"

    manual_window = (pd.Timestamp("2025-11-04"), pd.Timestamp("2025-11-09"))
    manual_figure = soybean_weather_page._daily_rain_figure(precipitation, region, manual_window)
    assert tuple(pd.Timestamp(value) for value in manual_figure.layout.xaxis.range) == (
        pd.Timestamp("2000-11-04"), pd.Timestamp("2000-11-09")
    )


def test_conflicting_duplicate_dates_are_rejected() -> None:
    records = _weather_records()
    duplicate = records.iloc[[0]].copy()
    duplicate.loc[:, "value"] = float(duplicate.iloc[0]["value"]) + 1
    with pytest.raises(ValueError, match="冲突"):
        validate_weather_records(pd.concat([records, duplicate], ignore_index=True))


def test_styles_and_page_route_preserve_approved_series_semantics(monkeypatch, tmp_path: Path) -> None:
    config = load_weather_config(CONFIG_FILE)
    styles = config["series_styles"]
    assert styles["current_season"]["color"] == "#bd2c25"
    assert styles["five_year_mean"]["dash"] == "dash"
    assert styles["ec"]["label"] == "EC"
    assert styles["gfs"]["label"] == "GFS"

    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)
    import soybean_weather_page

    precipitation = _weather_records()[lambda frame: frame["metric"] == "precipitation"]
    region = region_weights(config).iloc[0]
    figure = soybean_weather_page._region_line_figure(precipitation, config, region, kind="cumulative_rain")
    styles_by_name = {trace.name: trace.line for trace in figure.data}
    assert styles_by_name["5年均值"].color == "#15803d"
    assert styles_by_name["5年均值"].dash == "dash"
    assert styles_by_name["5年均值"].width == pytest.approx(2.5)
    assert styles_by_name["EC预测"].color == "#ff8a22"
    assert styles_by_name["EC预测"].dash == "dash"
    assert styles_by_name["EC预测"].width == pytest.approx(2.3)
    assert styles_by_name["GFS预测"].color == "#1688e8"
    assert styles_by_name["GFS预测"].dash == "dash"
    assert styles_by_name["GFS预测"].width == pytest.approx(2.3)
    assert styles_by_name["2024"].color == "#172033"
    assert styles_by_name["2024"].width == pytest.approx(2.8)
    assert styles_by_name["2025"].color == "#e53935"
    assert styles_by_name["2025"].width == pytest.approx(3.5)
    assert figure.layout.title.text == ""
    assert figure.layout.hovermode == "x unified"
    assert figure.layout.xaxis.hoverformat == "%m-%d"
    assert figure.layout.xaxis.dtick == 20 * 24 * 60 * 60 * 1000
    assert figure.layout.xaxis.griddash == "dot"
    assert figure.layout.yaxis.griddash == "dot"
    assert figure.layout.height == 350
    assert figure.data[-1].name == "2025"
    background_traces = [trace for trace in figure.data if trace.name in {"2020", "2021", "2022", "2023"}]
    assert all(trace.hoverinfo == "skip" for trace in background_traces)
    assert all(trace.opacity == pytest.approx(0.58) for trace in background_traces)
    assert [trace.line.color for trace in background_traces] == list(soybean_weather_page.HISTORY_SERIES_PALETTE[:4])
    assert [trace.legendrank for trace in figure.data if trace.name in {"5年均值", "2024", "2025", "GFS预测", "EC预测"}] == [100, 110, 130, 140, 120]
    heading = soybean_weather_page._history_card_heading(region)
    assert "伊利诺伊州（15.4%）" in heading
    assert "美国大豆主产区" not in heading
    assert figure.layout.xaxis.tickformat == "%m-%d"
    assert tuple(pd.Timestamp(value) for value in figure.layout.xaxis.range) == (
        pd.Timestamp("2000-04-15"), pd.Timestamp("2000-11-20")
    )
    assert all(pd.Timestamp(value).year == 2000 for trace in figure.data for value in trace.x)
    current_trace = next(trace for trace in figure.data if trace.name == "2025")
    for forecast_name in ("EC预测", "GFS预测"):
        forecast_trace = next(trace for trace in figure.data if trace.name == forecast_name)
        assert pd.Timestamp(forecast_trace.x[0]) == pd.Timestamp(current_trace.x[-1])

    soil_without_forecast = _weather_records()[lambda frame: frame["metric"] == "soil_moisture"]
    soil_figure = soybean_weather_page._region_line_figure(soil_without_forecast, config, region, kind="soil")
    assert len(soil_figure.data) > 0
    assert all(trace.name not in {"EC", "GFS"} for trace in soil_figure.data)
    assert soil_figure.layout.xaxis.tickformat == "%m-%d"
    assert tuple(pd.Timestamp(value) for value in soil_figure.layout.xaxis.range) == (
        pd.Timestamp("2000-03-01"), pd.Timestamp("2000-12-26")
    )
    assert soil_figure.layout.xaxis.dtick == 20 * 24 * 60 * 60 * 1000
    assert all(pd.Timestamp(value).year == 2000 for trace in soil_figure.data for value in trace.x)

    temperature = _weather_records()[lambda frame: frame["metric"] == "temperature_max"]
    temperature_figure = soybean_weather_page._region_line_figure(temperature, config, region, kind="temperature")
    assert tuple(pd.Timestamp(value) for value in temperature_figure.layout.xaxis.range) == (
        pd.Timestamp("2000-03-01"), pd.Timestamp("2000-11-20")
    )
    assert temperature_figure.layout.xaxis.dtick == 20 * 24 * 60 * 60 * 1000
    assert all(pd.Timestamp(value).year == 2000 for trace in temperature_figure.data for value in trace.x)
    assert soybean_weather_page._reference_date(pd.Timestamp("2015-06-01")) == soybean_weather_page._reference_date(pd.Timestamp("2026-06-01"))
    assert soybean_weather_page._reference_date(pd.Timestamp("2016-02-29")) == pd.Timestamp("2000-02-29")

    daily_figure = soybean_weather_page._daily_rain_figure(precipitation, region)
    assert daily_figure.layout.xaxis.tickformat == "%m-%d"
    assert daily_figure.layout.xaxis.dtick == 24 * 60 * 60 * 1000
    assert daily_figure.layout.xaxis.tickangle == -55
    assert daily_figure.layout.xaxis.griddash == "dot"
    assert daily_figure.layout.xaxis.rangeslider.visible is True
    assert daily_figure.layout.xaxis.rangeslider.thickness == pytest.approx(0.05)
    assert daily_figure.layout.xaxis.rangeslider.bgcolor == "#fcfdfe"
    assert daily_figure.layout.xaxis.rangeslider.bordercolor == "#edf1f5"
    assert daily_figure.layout.xaxis.rangeslider.borderwidth == 0
    assert daily_figure.layout.height == 400
    assert daily_figure.layout.legend.y == pytest.approx(0.93)
    assert daily_figure.layout.barmode == "group"
    assert daily_figure.layout.bargap == pytest.approx(0.18)
    assert daily_figure.layout.bargroupgap == pytest.approx(0.03)
    assert [trace.name for trace in daily_figure.data] == ["历史降雨", "EC预测", "GFS预测"]
    assert all(trace.width == pytest.approx(soybean_weather_page.DAILY_BAR_WIDTH_MS) for trace in daily_figure.data)
    assert all(pd.Timestamp(value).year == 2000 for trace in daily_figure.data for value in trace.x)

    latest = soybean_weather_page.latest_observation_date(precipitation)
    normals = _weather_normals()
    table = soybean_weather_page._build_weekly_wide_table(
        precipitation, normals, config, metric="precipitation", latest=latest, snapshot_date="2026-06-22", show_all_regions=False
    )
    assert "美豆主产区降水" in table
    assert "预测：未来2周" in table
    assert "沿用旧项目30年历史同期基准" in table
    assert "30年历史基准待接入" not in table
    assert "预测偏差：未来2周" in table
    assert "预测偏差幅度：未来2周" in table
    assert "15州 / 88.9%" in table
    assert table.find("15.4%") < table.find("伊利诺伊州")
    assert table.count("forecast-ec") >= 10
    assert "EC<br><strong>" not in table
    assert "北卡罗来纳州" not in table

    calls: list[str] = []
    with monkeypatch.context() as lazy_loader:
        lazy_loader.setattr(
            soybean_weather_page,
            "load_weather_records",
            lambda _path, **kwargs: calls.append(str(kwargs["metric"])) or _weather_records().iloc[0:0],
        )
        soybean_weather_page._load_selected_records.clear()
        soybean_weather_page._load_selected_records("fixture.csv", 1, "soybean", "USA", "precipitation")
        soybean_weather_page._load_selected_records("fixture.csv", 2, "soybean", "USA", "precipitation")
    assert calls == ["precipitation", "precipitation"]

    fixture = tmp_path / "soybean_weather_fixture.csv"
    _weather_records().to_csv(fixture, index=False)
    normal_fixture = tmp_path / "soybean_weather_normal_fixture.parquet"
    _weather_normals().to_parquet(normal_fixture, index=False)
    monkeypatch.setenv("SOYBEAN_WEATHER_FIXTURE_PATH", str(fixture))
    monkeypatch.setenv("SOYBEAN_WEATHER_NORMAL_FIXTURE_PATH", str(normal_fixture))
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=20).run()
    app.session_state["selected_workspace_page"] = "大豆天气"
    app.run(timeout=20)
    assert not app.exception
    assert any(item.label == "页面章节" for item in app.radio)
    assert any(item.label == "展开全部15州" for item in app.checkbox)
    assert any("15州覆盖权重88.9%" in item.value for item in app.caption)


def test_weather_page_degrades_without_stable_data_or_fixture(monkeypatch) -> None:
    monkeypatch.delenv("SOYBEAN_WEATHER_FIXTURE_PATH", raising=False)
    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)
    import soybean_weather_page

    usa_files = dict(soybean_weather_page.WEATHER_COUNTRY_FILES["USA"])
    monkeypatch.setitem(usa_files, "data", PROJECT_ROOT / "01_data" / "processed" / "weather" / "soybean" / "us" / "missing.parquet")
    monkeypatch.setitem(soybean_weather_page.WEATHER_COUNTRY_FILES, "USA", usa_files)
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=20).run()
    app.session_state["selected_workspace_page"] = "大豆天气"
    app.run(timeout=20)
    assert not app.exception
    assert any("稳定天气数据未接入" in item.value for item in app.error)
