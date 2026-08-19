from __future__ import annotations

from datetime import date
import os
from types import SimpleNamespace

import pandas as pd

from agri_research_agent.summary_engine import weather_cache
def test_formal_weather_summary_cache_reuses_current_identity_and_preserves_content(
    monkeypatch,
) -> None:
    records = pd.DataFrame({"value": [11.0, 22.0]})
    normals = pd.DataFrame({"normal_value": [16.5]})
    identity = SimpleNamespace(
        release_id="release-a",
        manifest_sha256="manifest-a",
        content_sha256="content-a",
        schema_version="lutou-public-weather-current/1",
        observation_source_max=date(2026, 8, 18),
        forecast_valid_max=date(2026, 9, 2),
    )
    snapshot = SimpleNamespace(records=records, normals=normals, identity=identity)
    calls = 0

    monkeypatch.setattr(
        weather_cache,
        "load_weather_config",
        lambda path: {
            "crop": "rapeseed",
            "country": "CAN",
            "regions": [{"key": "Ontario"}],
        },
    )

    def load_current(*args, **kwargs):
        nonlocal calls
        calls += 1
        return snapshot

    monkeypatch.setattr(weather_cache, "load_public_weather_current", load_current)
    monkeypatch.setattr(
        weather_cache,
        "build_weather_summary",
        lambda selected, selected_normals, config, **kwargs: {
            "record_values": selected["value"].tolist(),
            "normal_values": selected_normals["normal_value"].tolist(),
            "country": config["country"],
            "source_identity": kwargs["source_identity"],
        },
    )

    weather_cache.clear_weather_summary_cache()
    load = weather_cache._load_weather_current_summary_versioned
    args = (
        "current",
        identity.release_id,
        identity.manifest_sha256,
        identity.observation_source_max.isoformat(),
        ("config.yaml", 10, 1_700_000_000_000_000_000),
        ("rules.yaml", 10, 1_700_000_000_000_000_000),
        "rules-v1",
        "calc-v1",
    )
    first = load(*args)
    second = load(*args)

    assert first == second
    assert calls == 1
    assert first["record_values"] == [11.0, 22.0]
    assert first["normal_values"] == [16.5]
    assert first["source_identity"]["weather_current"] == {
        "release_id": "release-a",
        "manifest_sha256": "manifest-a",
        "content_sha256": "content-a",
        "schema_version": "lutou-public-weather-current/1",
        "observation_source_max": "2026-08-18",
        "forecast_valid_max": "2026-09-02",
    }


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


def test_detail_and_overview_import_the_same_public_current_cache() -> None:
    import crop_weather_page
    import research_overview_page

    assert crop_weather_page.load_weather_current_summary_cached is weather_cache.load_weather_current_summary_cached
    assert research_overview_page.load_weather_current_summary_cached is weather_cache.load_weather_current_summary_cached


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
