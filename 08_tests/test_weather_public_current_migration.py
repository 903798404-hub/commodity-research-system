from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import crop_weather_page
import research_overview_page
from agri_research_agent.market_data.public_weather_current import (
    PublicWeatherCurrentIdentity,
)
from agri_research_agent.summary_engine import weather_cache
from agri_research_agent.summary_engine.weather import _soil_percent
from agri_research_agent.weather.crop_weather import (
    load_weather_config,
    select_latest_forecasts,
)


ROOT = Path(__file__).resolve().parents[1]


def _current_forecasts() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "series_id": f"{model}-{region}", "date": pd.Timestamp("2026-08-20"),
                "data_type": "forecast", "model": model, "region": region,
                "value": value, "forecast_run_id": f"run-{model}-{region}",
                "forecast_run_at": pd.NaT,
                "source_updated_at": pd.Timestamp(extracted, tz="UTC"),
                "current_manifest_sha256": "a" * 64,
            }
            for model, region, value, extracted in (
                ("ECMWF", "illinois", 1.0, "2026-08-19T00:00:01"),
                ("ECMWF", "iowa", 2.0, "2026-08-19T00:00:02"),
                ("GFS", "illinois", 3.0, "2026-08-19T00:00:03"),
                ("GFS", "iowa", 4.0, "2026-08-19T00:00:04"),
            )
        ]
    )


def test_current_forecasts_keep_ec_gfs_and_all_regions_despite_extract_times() -> None:
    selected = select_latest_forecasts(_current_forecasts(), pd.Timestamp("2026-08-18"))
    assert len(selected) == 4
    assert set(selected["model"]) == {"ECMWF", "GFS"}
    assert set(selected["region"]) == {"illinois", "iowa"}


def test_current_forecast_run_collision_fails_closed() -> None:
    rows = pd.concat(
        [
            _current_forecasts(),
            _current_forecasts().iloc[[0]].assign(forecast_run_id="unexpected-second-run"),
        ],
        ignore_index=True,
    )
    with pytest.raises(ValueError, match="multiple forecast runs"):
        select_latest_forecasts(rows, pd.Timestamp("2026-08-18"))


def test_canonical_soil_percent_is_not_treated_as_a_fraction() -> None:
    assert _soil_percent(29.627, canonical_percent=True) == pytest.approx(29.627)
    assert _soil_percent(0.8, canonical_percent=True) == pytest.approx(0.8)
    assert _soil_percent(0.8) == pytest.approx(80.0)  # explicit legacy helper semantics


def test_summary_cache_identity_includes_release_and_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = iter(
        (
            PublicWeatherCurrentIdentity(
                "release-a", "a" * 64, "schema", date(2026, 8, 18),
                date(2026, 9, 2), "c" * 64,
            ),
            PublicWeatherCurrentIdentity(
                "release-b", "b" * 64, "schema", date(2026, 8, 19),
                date(2026, 9, 3), "d" * 64,
            ),
        )
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(weather_cache, "resolve_weather_current_identity", lambda _root: next(identities))
    monkeypatch.setattr(weather_cache, "file_stat_identity", lambda path: (str(path), 1, 2))
    monkeypatch.setattr(
        weather_cache, "load_summary_rules",
        lambda _path: {"rule_version": "rules", "calculation_version": "calc"},
    )
    monkeypatch.setattr(
        weather_cache, "_load_weather_current_summary_versioned",
        lambda _root, release, manifest, *_args: calls.append((release, manifest)) or release,
    )
    assert weather_cache.load_weather_current_summary_cached("current", "config") == "release-a"
    assert weather_cache.load_weather_current_summary_cached("current", "config") == "release-b"
    assert calls == [("release-a", "a" * 64), ("release-b", "b" * 64)]


def test_detail_and_overview_share_one_public_current_summary_boundary() -> None:
    assert (
        crop_weather_page.load_weather_current_summary_cached
        is weather_cache.load_weather_current_summary_cached
    )
    assert (
        research_overview_page.load_weather_current_summary_cached
        is weather_cache.load_weather_current_summary_cached
    )


def test_formal_weather_runtime_has_no_legacy_sql_or_live_fallback() -> None:
    formal_files = (
        ROOT / "05_apps/crop_weather_page.py",
        ROOT / "05_apps/research_overview_page.py",
        ROOT / "03_src/agri_research_agent/summary_engine/weather_cache.py",
        ROOT / "03_src/agri_research_agent/market_data/public_weather_current.py",
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in formal_files)
    for forbidden in (
        "01_data/processed/weather", "WEATHER_DATA_DIR", "pymysql.connect",
        "fallback SQL", "SELECT * FROM",
    ):
        assert forbidden not in source
    render_source = inspect.getsource(crop_weather_page.render_weather_page)
    assert "resolve_weather_current_identity" in render_source
    assert "未加载任何 legacy 或数据库回退" in render_source


def test_public_runtime_root_is_the_only_formal_weather_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ROOT / "isolated-runtime"
    monkeypatch.setenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", str(runtime))
    expected = runtime / "public-market-data" / "lutou-weather"
    assert crop_weather_page._public_weather_current_root() == expected
    assert research_overview_page._public_weather_current_root() == expected


def test_canada_consumer_keeps_weighted_and_agricultural_identities_distinct() -> None:
    config = load_weather_config(ROOT / "02_configs/rapeseed_weather_can.yaml")
    displayed = {item["key"] for item in config["regions"]}
    source = {item["key"] for item in config["source_regions"]}
    assert displayed == {
        "saskatchewan_weighted", "alberta_weighted", "manitoba_weighted",
    }
    assert {"sk9a_raw", "ab6_raw", "ab7_raw", "mb12_raw"}.issubset(source)
    assert displayed.isdisjoint({"sk9a_raw", "ab6_raw", "ab7_raw", "mb12_raw"})


def test_temperature_max_and_min_remain_separate_page_metrics() -> None:
    assert crop_weather_page.MODULES["maximum_temperature"][2] == "temperature_max"
    assert crop_weather_page.MODULES["minimum_temperature"][2] == "temperature_min"
    config = load_weather_config(ROOT / "02_configs/rapeseed_weather_can.yaml")
    assert config["metrics"]["temperature_max"]["unit"] == "degC"
    assert config["metrics"]["temperature_min"]["unit"] == "degC"


def test_soil_chart_uses_public_current_percent_semantics() -> None:
    source = inspect.getsource(crop_weather_page._region_line_figure)
    assert 'metric == "soil_moisture"' in source
    assert 'unit = f"{display_name}（%）"' in source


def test_consumer_pushes_required_history_windows_into_parquet_reader() -> None:
    summary_source = inspect.getsource(weather_cache._load_weather_current_summary_versioned)
    page_source = inspect.getsource(crop_weather_page.render_weather_page)
    assert "pd.DateOffset(years=6)" in summary_source
    assert "start_date=history_start" in summary_source
    assert "pd.DateOffset(years=13)" in page_source
