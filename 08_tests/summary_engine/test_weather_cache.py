from __future__ import annotations

from pathlib import Path
import os

import pandas as pd

from agri_research_agent.summary_engine import weather_cache
from agri_research_agent.summary_engine.weather import build_weather_summary
from agri_research_agent.weather.crop_weather import load_weather_config


ROOT = Path(__file__).resolve().parents[2]
WEATHER_ROOT = ROOT / "01_data" / "processed" / "weather"


def test_formal_weather_summary_cache_reuses_identity_and_preserves_content() -> None:
    data = WEATHER_ROOT / "rapeseed/can/rapeseed_weather_can.parquet"
    normal = WEATHER_ROOT / "rapeseed/can/rapeseed_weather_can_30y_normal.parquet"
    config_path = ROOT / "02_configs/rapeseed_weather_can.yaml"
    config = load_weather_config(config_path)
    direct = build_weather_summary(
        pd.read_parquet(data),
        pd.read_parquet(normal),
        config,
        source_identity={"comparison": "CAN"},
    )

    weather_cache.clear_weather_summary_cache()
    first = weather_cache.load_weather_summary_cached(data, config_path, normal)
    second = weather_cache.load_weather_summary_cached(data, config_path, normal)

    assert first.summary_id == second.summary_id
    assert first.generated_at == second.generated_at
    assert first.facts == direct.facts
    assert first.detail_text == direct.detail_text
    assert first.short_text == direct.short_text


def test_every_identity_component_invalidates_without_cross_country_leakage(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        weather_cache,
        "load_weather_config",
        lambda path: {
            "crop": "soybean",
            "country": "USA" if "usa" in path else "BRA",
        },
    )
    monkeypatch.setattr(
        weather_cache,
        "_read_weather",
        lambda path: pd.DataFrame(
            {
                "crop": ["soybean", "soybean"],
                "country": ["USA", "BRA"],
                "metric": ["precipitation", "precipitation"],
            }
        ),
    )
    monkeypatch.setattr(weather_cache, "_read_normals", lambda path: pd.DataFrame())

    def fake_builder(records, normals, config, **kwargs):
        del normals, kwargs
        calls.append((str(config["country"]), str(records.iloc[0]["country"])))
        return calls[-1]

    monkeypatch.setattr(weather_cache, "build_weather_summary", fake_builder)
    weather_cache.clear_weather_summary_cache()

    data = ("weather.parquet", 100, 1_700_000_000_000_000_000)
    normal = ("normal.parquet", 50, 1_700_000_000_000_000_000)
    config_usa = ("usa.yaml", 10, 1_700_000_000_000_000_000)
    rules = ("rules.yaml", 10, 1_700_000_000_000_000_000)

    load = weather_cache._load_weather_summary_versioned
    base = load(data, config_usa, normal, rules, "rules-v1", "calc-v1")
    assert load(data, config_usa, normal, rules, "rules-v1", "calc-v1") == base
    assert len(calls) == 1

    assert load((data[0], data[1] + 1, data[2]), config_usa, normal, rules, "rules-v1", "calc-v1")
    assert load(data, config_usa, (normal[0], normal[1] + 1, normal[2]), rules, "rules-v1", "calc-v1")
    assert load(data, config_usa, normal, (rules[0], rules[1] + 1, rules[2]), "rules-v1", "calc-v1")
    assert load(data, config_usa, normal, rules, "rules-v2", "calc-v1")
    assert load(data, config_usa, normal, rules, "rules-v1", "calc-v2")
    brazil = load(data, ("bra.yaml", 10, config_usa[2]), normal, rules, "rules-v1", "calc-v1")

    assert len(calls) == 7
    assert base == ("USA", "USA")
    assert brazil == ("BRA", "BRA")


def test_atomic_file_replacement_changes_lightweight_identity(tmp_path) -> None:
    current = tmp_path / "current.parquet"
    replacement = tmp_path / "replacement.parquet"
    current.write_bytes(b"old")
    before = weather_cache.file_stat_identity(current)
    replacement.write_bytes(b"new-version")
    os.replace(replacement, current)
    after = weather_cache.file_stat_identity(current)

    assert before != after
    assert before[0] == after[0]


def test_detail_and_overview_import_the_same_shared_cache() -> None:
    import crop_weather_page
    import research_overview_page

    assert crop_weather_page.load_weather_summary_cached is weather_cache.load_weather_summary_cached
    assert research_overview_page.load_weather_summary_cached is weather_cache.load_weather_summary_cached


def test_missing_object_exception_is_not_cached_or_shared(monkeypatch) -> None:
    monkeypatch.setattr(
        weather_cache,
        "load_weather_config",
        lambda path: {"crop": "soybean", "country": "USA"},
    )
    attempts = 0

    def read(path):
        nonlocal attempts
        attempts += 1
        if path == "missing.parquet":
            raise FileNotFoundError(path)
        return pd.DataFrame(
            {"crop": ["soybean"], "country": ["USA"], "metric": ["precipitation"]}
        )

    monkeypatch.setattr(weather_cache, "_read_weather", read)
    monkeypatch.setattr(weather_cache, "_read_normals", lambda path: pd.DataFrame())
    monkeypatch.setattr(weather_cache, "build_weather_summary", lambda *args, **kwargs: "ok")
    weather_cache.clear_weather_summary_cache()
    identity_tail = ("config.yaml", 1, 1_700_000_000_000_000_000)
    rules = ("rules.yaml", 1, 1_700_000_000_000_000_000)

    try:
        weather_cache._load_weather_summary_versioned(
            ("missing.parquet", 1, identity_tail[2]), identity_tail, None,
            rules, "rules-v1", "calc-v1",
        )
    except FileNotFoundError:
        pass
    assert weather_cache._load_weather_summary_versioned(
        ("usa.parquet", 1, identity_tail[2]), identity_tail, None,
        rules, "rules-v1", "calc-v1",
    ) == "ok"
    assert attempts == 2
