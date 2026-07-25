from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for directory in (PROJECT_ROOT / "03_src", PROJECT_ROOT / "05_apps"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import crop_weather_page
from agri_research_agent.data_sources.weather_adapter import load_weather_records
from agri_research_agent.weather.crop_weather import (
    latest_observation_date,
    load_weather_config,
    region_weights,
    weekly_metric_summary,
)


EU_CONFIG = load_weather_config(PROJECT_ROOT / "02_configs" / "rapeseed_weather_eu.yaml")


def _eu_records(metric: str) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for date in pd.date_range("2026-07-07", "2026-07-20", freq="D"):
        rows.append(
            {
                "date": date,
                "crop": "rapeseed",
                "country": "EU",
                "region": "france",
                "metric": metric,
                "data_type": "observed",
                "model": "observed",
                "value": 1.0,
                "unit": "mm" if metric == "precipitation" else "degC",
                "source_updated_at": pd.Timestamp("2026-07-22T08:17:43Z"),
                "forecast_run_at": pd.NaT,
            }
        )
    for model, end in (("ECMWF", "2026-08-04"), ("GFS", "2026-08-05")):
        for date in pd.date_range("2026-07-23", end, freq="D"):
            rows.append(
                {
                    "date": date,
                    "crop": "rapeseed",
                    "country": "EU",
                    "region": "france",
                    "metric": metric,
                    "data_type": "forecast",
                    "model": model,
                    "value": 2.0,
                    "unit": "mm" if metric == "precipitation" else "degC",
                    "source_updated_at": pd.Timestamp("2026-07-22T08:17:43Z"),
                    "forecast_run_at": pd.Timestamp("2026-07-22T08:17:43Z"),
                }
            )
    return pd.DataFrame(rows)


def test_eu_daily_rain_uses_natural_28_day_axis_and_one_value_per_country_date_series() -> None:
    records = _eu_records("precipitation")
    region = region_weights(EU_CONFIG).iloc[0]
    window = (pd.Timestamp("2026-07-07"), pd.Timestamp("2026-08-03"))

    series = crop_weather_page._prepare_daily_rain_series(records, "france", window)
    assert {name: len(values) for name, values in series.items()} == {"observed": 14, "ECMWF": 12, "GFS": 12}
    assert all(values.groupby(["country", "region", "date", "series"]).size().eq(1).all() for values in series.values())

    figure = crop_weather_page._daily_rain_figure(records, region, window, EU_CONFIG)
    assert len(figure.data) == 3
    assert sum(len(trace.x) for trace in figure.data) == 38
    assert all(len(trace.x) <= 28 for trace in figure.data)
    assert tuple(pd.Timestamp(value) for value in figure.layout.xaxis.range) == window
    assert figure.layout.xaxis.rangeslider.visible is False
    assert pd.Timestamp(min(figure.data[0].x)) == pd.Timestamp("2026-07-07")
    assert pd.Timestamp(max(figure.data[0].x)) == pd.Timestamp("2026-07-20")


def test_daily_rain_refuses_duplicate_country_date_series_instead_of_silently_deduplicating() -> None:
    records = _eu_records("precipitation")
    duplicate = records.iloc[[0]].copy()
    duplicate.loc[:, "value"] = 9.0
    with pytest.raises(ValueError, match="重复"):
        crop_weather_page._prepare_daily_rain_series(
            pd.concat([records, duplicate], ignore_index=True),
            "france",
            (pd.Timestamp("2026-07-07"), pd.Timestamp("2026-08-03")),
        )


@pytest.mark.parametrize("metric", ["temperature_max", "temperature_min"])
def test_eu_temperature_forecasts_break_at_the_07_31_to_08_01_season_boundary(metric: str) -> None:
    records = _eu_records(metric)
    region = region_weights(EU_CONFIG).iloc[0]
    figure = crop_weather_page._region_line_figure(records, EU_CONFIG, region, kind="temperature", metric=metric)
    forecasts = [trace for trace in figure.data if trace.line.dash == "dash"]

    assert len(forecasts) == 2
    for trace in forecasts:
        values = list(trace.x)
        null_index = next(index for index, value in enumerate(values) if value is None or pd.isna(value))
        before = pd.Timestamp(values[null_index - 1])
        after = pd.Timestamp(values[null_index + 1])
        assert before == pd.Timestamp("2001-07-31")
        assert after == pd.Timestamp("2000-08-01")
        assert trace.connectgaps is False
        assert sum(value is not None and not pd.isna(value) for value in values) in {13, 14}


def test_freshness_comes_from_parquet_records_not_legacy_update_status(tmp_path: Path) -> None:
    data_file = tmp_path / "rapeseed_weather_eu.parquet"
    data_file.write_bytes(b"current-runtime-data")
    freshness = crop_weather_page._weather_freshness(_eu_records("precipitation"), data_file)

    assert freshness["observed"] == "2026-07-20"
    assert freshness["ecmwf"] == "2026-08-04"
    assert freshness["gfs"] == "2026-08-05"
    assert "2026-07-" in freshness["refreshed_at"] or freshness["refreshed_at"].endswith(" UTC")


def test_selected_record_cache_is_keyed_by_runtime_path_and_file_identity(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setenv(crop_weather_page.WEATHER_DATA_DIR_ENV, "/runtime-a")
    assert crop_weather_page._weather_data_file("rapeseed/eu.parquet") == Path("/runtime-a/rapeseed/eu.parquet")
    monkeypatch.setenv(crop_weather_page.WEATHER_DATA_DIR_ENV, "/runtime-b")
    assert crop_weather_page._weather_data_file("rapeseed/eu.parquet") == Path("/runtime-b/rapeseed/eu.parquet")
    monkeypatch.setattr(
        crop_weather_page,
        "load_weather_records",
        lambda path, **_kwargs: calls.append(str(path)) or pd.DataFrame(),
    )
    crop_weather_page._load_selected_records.clear()
    try:
        crop_weather_page._load_selected_records("/runtime-a/rapeseed/eu.parquet", 10, "rapeseed", "EU", "precipitation", 100, "EU")
        crop_weather_page._load_selected_records("/runtime-a/rapeseed/eu.parquet", 11, "rapeseed", "EU", "precipitation", 100, "EU")
        crop_weather_page._load_selected_records("/runtime-b/rapeseed/eu.parquet", 11, "rapeseed", "EU", "precipitation", 100, "EU")
    finally:
        crop_weather_page._load_selected_records.clear()

    assert calls == [
        "/runtime-a/rapeseed/eu.parquet",
        "/runtime-a/rapeseed/eu.parquet",
        "/runtime-b/rapeseed/eu.parquet",
    ]


def _forecast_contract_records(
    config: dict[str, object],
    *,
    start: str,
    dates_by_model: dict[str, int],
) -> dict[str, pd.DataFrame]:
    rows: list[dict[str, object]] = []
    for metric, unit in (("precipitation", "mm"), ("temperature_max", "degC")):
        for model, days in dates_by_model.items():
            for date in pd.date_range(start, periods=days, freq="D"):
                for region_index, region in enumerate(config["regions"]):
                    rows.append(
                        {
                            "date": date,
                            "crop": str(config["crop"]),
                            "country": str(config["country"]),
                            "region": region["key"],
                            "metric": metric,
                            "data_type": "forecast",
                            "model": model,
                            "value": 0.0 if metric == "precipitation" and region_index == 0 and date == pd.Timestamp(start) else 1.0,
                            "unit": unit,
                            "source_updated_at": pd.Timestamp("2026-07-22T08:17:43Z"),
                            "forecast_run_at": pd.Timestamp("2026-07-22T08:17:43Z"),
                        }
                    )
    frame = pd.DataFrame(rows)
    return {metric: frame[frame["metric"] == metric].reset_index(drop=True) for metric in ("precipitation", "temperature_max")}


def test_forecast_windows_are_two_fixed_continuous_seven_day_periods() -> None:
    config = load_weather_config(PROJECT_ROOT / "02_configs" / "soybean_weather_us.yaml")
    records_by_metric = _forecast_contract_records(config, start="2026-07-23", dates_by_model={"ECMWF": 14, "GFS": 15})

    plan = crop_weather_page._forecast_window_plan(records_by_metric, config)

    assert plan["windows"] == [
        (pd.Timestamp("2026-07-23"), pd.Timestamp("2026-07-29")),
        (pd.Timestamp("2026-07-30"), pd.Timestamp("2026-08-05")),
    ]
    assert all((end - begin).days + 1 == 7 for begin, end in plan["windows"])
    assert plan["windows"][0][1] + pd.Timedelta(days=1) == plan["windows"][1][0]
    for metric, records in records_by_metric.items():
        for window in plan["windows"]:
            for model in ("ECMWF", "GFS"):
                summary = crop_weather_page._forecast_summary_row(
                    records, config, metric=metric, window=window, model=model
                )
                assert summary["value"].notna().all()
    assert crop_weather_page._format_value(0.0) == "0.0"


@pytest.mark.parametrize(
    ("config_file", "latest"),
    [
        ("soybean_weather_us.yaml", "2026-07-21"),
        ("soybean_weather_br.yaml", "2026-07-18"),
        ("soybean_weather_ar.yaml", "2026-07-20"),
        ("rapeseed_weather_can.yaml", "2026-07-22"),
    ],
)
def test_historical_windows_roll_back_from_each_countrys_actual_latest_observation(
    config_file: str,
    latest: str,
) -> None:
    # The page's shared matrix receives the country-specific latest observation
    # date and must not re-anchor the history to a Monday-to-Sunday calendar.
    config = load_weather_config(PROJECT_ROOT / "02_configs" / config_file)
    latest_date = pd.Timestamp(latest)

    windows = crop_weather_page._historical_windows(latest_date)

    assert len(windows) == 4
    assert windows[-1] == (latest_date - pd.Timedelta(days=6), latest_date)
    assert all((end - start).days + 1 == 7 for start, end in windows)
    assert all(windows[index][1] + pd.Timedelta(days=1) == windows[index + 1][0] for index in range(3))
    assert windows[-1][1] == latest_date
    assert len(config["regions"]) > 0


def test_us_historical_window_values_baselines_and_anomalies_use_the_same_rolling_days() -> None:
    config = load_weather_config(PROJECT_ROOT / "02_configs" / "soybean_weather_us.yaml")
    latest = pd.Timestamp("2026-07-21")
    windows = crop_weather_page._historical_windows(latest)
    assert windows == [
        (pd.Timestamp("2026-06-24"), pd.Timestamp("2026-06-30")),
        (pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-07")),
        (pd.Timestamp("2026-07-08"), pd.Timestamp("2026-07-14")),
        (pd.Timestamp("2026-07-15"), pd.Timestamp("2026-07-21")),
    ]

    records: list[dict[str, object]] = []
    normals: list[dict[str, object]] = []
    for metric, observed_value, normal_value, unit in (
        ("precipitation", 1.0, 2.0, "mm"),
        ("temperature_max", 20.0, 22.0, "degC"),
    ):
        for date in pd.date_range(windows[0][0], latest, freq="D"):
            for region_index, region in enumerate(config["regions"]):
                # A real zero must remain an observed daily rainfall value.
                value = 0.0 if metric == "precipitation" and region_index == 0 else observed_value
                records.append(
                    {
                        "date": date,
                        "crop": config["crop"],
                        "country": config["country"],
                        "region": region["key"],
                        "metric": metric,
                        "data_type": "observed",
                        "model": "observed",
                        "value": value,
                        "unit": unit,
                    }
                )
                normals.append(
                    {
                        "month_day": date.strftime("%m-%d"),
                        "country": config["country"],
                        "region": region["key"],
                        "metric": metric,
                        "normal_value": normal_value,
                        "unit": unit,
                    }
                )
    observed_records = pd.DataFrame(records)
    normal_records = pd.DataFrame(normals)
    final_window = windows[-1]
    first_region = str(config["regions"][0]["key"])

    for metric, expected_value, expected_baseline in (
        ("precipitation", 7.0, 14.0),
        ("temperature_max", 20.0, 22.0),
    ):
        observed = crop_weather_page._weekly_row(
            observed_records, config, metric=metric, window=final_window, data_type="observed"
        )
        baseline = crop_weather_page._normal_summary(normal_records, config, metric=metric, window=final_window)
        assert observed.loc[first_region, "value"] == pytest.approx(0.0 if metric == "precipitation" else expected_value)
        if metric == "precipitation":
            assert observed.loc["weighted", "value"] < expected_value
        else:
            assert observed.loc["weighted", "value"] == pytest.approx(expected_value)
        assert baseline.loc[first_region, "value"] == pytest.approx(expected_baseline)
        assert crop_weather_page._anomaly(
            observed.loc[first_region, "value"], baseline.loc[first_region, "value"], percent=False
        ) == pytest.approx(-expected_baseline if metric == "precipitation" else -2.0)
        assert crop_weather_page._anomaly_pct(
            observed.loc[first_region, "value"], baseline.loc[first_region, "value"]
        ) == pytest.approx(-100.0 if metric == "precipitation" else -100.0 / 11.0)
        history_html = crop_weather_page._wide_row(
            final_window,
            region_weights(config),
            {"observed": observed},
            baseline,
            forecast=False,
            measure="percent",
            metric=metric,
        )
        assert "2026-07-15" in history_html and "2026-07-21" in history_html
        assert "-100.0%" in history_html if metric == "precipitation" else "-9.1%" in history_html


@pytest.mark.parametrize("config_file", [
    "soybean_weather_us.yaml",
    "soybean_weather_br.yaml",
    "soybean_weather_ar.yaml",
    "rapeseed_weather_can.yaml",
])
def test_fixed_second_week_aggregates_each_models_real_non_null_days(config_file: str) -> None:
    config = load_weather_config(PROJECT_ROOT / "02_configs" / config_file)
    records_by_metric = _forecast_contract_records(config, start="2026-07-24", dates_by_model={"ECMWF": 13, "GFS": 14})

    plan = crop_weather_page._forecast_window_plan(records_by_metric, config)

    assert plan["windows"] == [
        (pd.Timestamp("2026-07-24"), pd.Timestamp("2026-07-30")),
        (pd.Timestamp("2026-07-31"), pd.Timestamp("2026-08-06")),
    ]
    assert (plan["windows"][0][1] - plan["windows"][0][0]).days + 1 == 7
    assert (plan["windows"][1][1] - plan["windows"][1][0]).days + 1 == 7
    assert plan["windows"][0][1] + pd.Timedelta(days=1) == plan["windows"][1][0]

    second_window = plan["windows"][1]
    normals = pd.DataFrame(
        [
            {
                "month_day": date.strftime("%m-%d"),
                "country": config["country"],
                "region": region["key"],
                "metric": metric,
                "normal_value": 1.0,
                "unit": "mm" if metric == "precipitation" else "degC",
                "baseline_label": "fixture",
                "source_workbook_sha256": "a" * 64,
                "source_sheet": "fixture",
            }
            for metric in ("precipitation", "temperature_max")
            for date in pd.date_range(*second_window, freq="D")
            for region in config["regions"]
        ]
    )
    for metric, records in records_by_metric.items():
        ec_summary = crop_weather_page._forecast_summary_row(
            records, config, metric=metric, window=second_window, model="ECMWF"
        )
        gfs_summary = crop_weather_page._forecast_summary_row(
            records, config, metric=metric, window=second_window, model="GFS"
        )
        configured_regions = [region["key"] for region in config["regions"]]
        assert configured_regions
        assert ec_summary.loc[configured_regions, "value"].notna().all()
        assert gfs_summary.loc[configured_regions, "value"].notna().all()
        assert ec_summary.loc["weighted", "value"] == pytest.approx(6.0 if metric == "precipitation" else 1.0)
        assert gfs_summary.loc["weighted", "value"] == pytest.approx(7.0 if metric == "precipitation" else 1.0)

        normal = crop_weather_page._normal_summary(normals, config, metric=metric, window=second_window)
        assert normal.loc["weighted", "value"] == pytest.approx(7.0 if metric == "precipitation" else 1.0)
        assert crop_weather_page._anomaly(ec_summary.loc["weighted", "value"], normal.loc["weighted", "value"], percent=False) == pytest.approx(-1.0 if metric == "precipitation" else 0.0)
        assert crop_weather_page._anomaly_pct(gfs_summary.loc["weighted", "value"], normal.loc["weighted", "value"]) == pytest.approx(0.0)

        row = crop_weather_page._wide_row(
            second_window,
            region_weights(config),
            {"ECMWF": ec_summary, "GFS": gfs_summary},
            normal,
            forecast=True,
            measure="value",
            metric=metric,
        )
        assert "2026-07-31" in row and "2026-08-06" in row and "—" in row
        assert "source_missing" not in row and "6/7" not in row

    first_window = plan["windows"][0]
    first_rain = crop_weather_page._forecast_summary_row(
        records_by_metric["precipitation"], config, metric="precipitation", window=first_window, model="ECMWF"
    )
    assert first_rain.loc[config["regions"][0]["key"], "value"] == pytest.approx(6.0)
    assert crop_weather_page._format_value(0.0) == "0.0"


def test_rolling_window_baseline_and_rainfall_sum_follow_the_same_seven_calendar_days() -> None:
    config = load_weather_config(PROJECT_ROOT / "02_configs" / "soybean_weather_us.yaml")
    window = (pd.Timestamp("2026-07-23"), pd.Timestamp("2026-07-29"))
    normals = pd.DataFrame(
        [
            {
                "month_day": date.strftime("%m-%d"),
                "country": "USA",
                "region": region["key"],
                "metric": "precipitation",
                "normal_value": 1.0,
                "unit": "mm",
                "baseline_label": "fixture",
                "source_workbook_sha256": "a" * 64,
                "source_sheet": "fixture",
            }
            for date in pd.date_range(*window, freq="D")
            for region in config["regions"]
        ]
    )
    summary = crop_weather_page._normal_summary(normals, config, metric="precipitation", window=window)
    assert summary.loc["illinois", "value"] == pytest.approx(7.0)
    assert sorted(normals["month_day"].unique()) == ["07-23", "07-24", "07-25", "07-26", "07-27", "07-28", "07-29"]
    assert crop_weather_page._format_value(0.0) == "0.0"


def test_weekly_metric_configuration_preserves_one_shared_table_grammar() -> None:
    assert set(crop_weather_page.WEEKLY_TABLE_METRICS) == {"precipitation", "temperature_max"}
    assert crop_weather_page.WEEKLY_TABLE_METRICS["precipitation"]["weekly_aggregation"] == "sum"
    assert crop_weather_page.WEEKLY_TABLE_METRICS["temperature_max"]["weekly_aggregation"] == "mean"

    source = Path(crop_weather_page.__file__).read_text(encoding="utf-8")
    assert source.count("def _build_weekly_wide_table(") == 1
    assert source.count("table.weather-wide-table") == 1
    assert source.count(".weather-wide-table .table-title") == 1
    assert source.count(".weather-wide-table .forecast-header") == 1
    assert source.count('"row-forecast" if forecast else ""') == 1


@pytest.mark.parametrize("slug", ["us", "br", "ar", "can"])
def test_forecast_anomaly_cells_keep_heatmap_backgrounds_and_readable_text(slug: str) -> None:
    config = load_weather_config(PROJECT_ROOT / "02_configs" / f"soybean_weather_{slug}.yaml") if slug in {"us", "br", "ar"} else load_weather_config(PROJECT_ROOT / "02_configs" / "rapeseed_weather_can.yaml")
    regions = region_weights(config).head(1)
    region = str(regions.iloc[0]["key"])
    normal = pd.DataFrame({"value": [100.0, 100.0]}, index=["weighted", region])
    strong = {
        "ECMWF": pd.DataFrame({"value": [40.0, 40.0]}, index=["weighted", region]),
        "GFS": pd.DataFrame({"value": [160.0, 160.0]}, index=["weighted", region]),
    }
    light = {
        "ECMWF": pd.DataFrame({"value": [95.0, 95.0]}, index=["weighted", region]),
        "GFS": pd.DataFrame({"value": [105.0, 105.0]}, index=["weighted", region]),
    }
    window = (pd.Timestamp("2026-07-31"), pd.Timestamp("2026-08-05"))

    strong_html = crop_weather_page._wide_row(
        window, regions, strong, normal, forecast=True, measure="percent", metric="precipitation"
    )
    light_html = crop_weather_page._wide_row(
        window, regions, light, normal, forecast=True, measure="percent", metric="precipitation"
    )
    historical_html = crop_weather_page._wide_row(
        window, regions, {"observed": strong["ECMWF"]}, normal, forecast=False, measure="percent", metric="precipitation"
    )

    assert "-60.0%" in strong_html and "+60.0%" in strong_html
    assert "anomaly-deep-negative" in strong_html and "anomaly-deep-positive" in strong_html
    assert "-5.0%" in light_html and "+5.0%" in light_html
    assert "anomaly-light-negative" in light_html and "anomaly-light-positive" in light_html
    assert "forecast-ec" not in historical_html and "anomaly-deep-negative" in historical_html

    source = Path(crop_weather_page.__file__).read_text(encoding="utf-8")
    assert ".weather-wide-table .row-forecast td {background:" not in source
    assert ".weather-wide-table .forecast-header td,.weather-wide-table .forecast-sub {background:#fff2cc" in source
    assert ".weather-wide-table .anomaly-deep-negative {background:#C62828;color:#fff" in source
    assert ".weather-wide-table .anomaly-deep-positive {background:#2E7D32;color:#fff" in source
    assert ".weather-wide-table .anomaly-light-negative {background:#FFEBEE;color:#9f1d1d" in source
    assert ".weather-wide-table .anomaly-light-positive {background:#E8F5E9;color:#17613b" in source
