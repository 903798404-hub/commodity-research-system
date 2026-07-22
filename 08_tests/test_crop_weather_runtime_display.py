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
from agri_research_agent.weather.crop_weather import load_weather_config, region_weights


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
