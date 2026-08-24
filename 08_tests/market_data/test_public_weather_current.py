from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.market_data import public_weather_current as reader
from agri_research_agent.shared.file_identity import identify_file


def _identity() -> reader.PublicWeatherCurrentIdentity:
    return reader.PublicWeatherCurrentIdentity(
        "weather-release", "a" * 64, "lutou-public-weather-current/1",
        date(2026, 8, 18), date(2026, 9, 2), "b" * 64,
    )


def _resolved(tmp_path: Path, *, bindings=()) -> object:
    current = SimpleNamespace(
        observations_path=tmp_path / "observations.parquet",
        soil_observations_path=tmp_path / "soil.parquet",
        normals_path=tmp_path / "normals.parquet",
    )
    return SimpleNamespace(current=current, identity=_identity(), soil_bindings=bindings)


def _weather_rows(*, unit: str = "mm", metric: str = "precipitation") -> pd.DataFrame:
    common = {
        "provider_series_id": "provider-series", "source_locator": "source",
        "source_table": "table", "source_column": "Illinois", "crop": "soybean",
        "country": "USA", "region": "illinois", "region_label": "Illinois",
        "region_type": "state", "metric": metric, "unit": unit,
        "observation_interval": "daily", "aggregation": "source_value",
        "soil_depth": None, "extracted_at": pd.Timestamp("2026-08-19", tz="UTC"),
        "source_row_sha256": "c" * 64,
    }
    return pd.DataFrame(
        [
            common | {
                "series_id": "observed-series", "data_family": "observation",
                "forecast_model": "OBSERVED", "forecast_issue_date": None,
                "forecast_issue_status": "NOT_APPLICABLE",
                "forecast_run_id": "NOT_APPLICABLE", "valid_date": date(2026, 8, 18),
                "value": 12.5,
            },
            common | {
                "series_id": "ec-series", "data_family": "forecast",
                "forecast_model": "ECMWF", "forecast_issue_date": None,
                "forecast_issue_status": "SOURCE_NOT_PROVIDED",
                "forecast_run_id": "ec-run", "valid_date": date(2026, 8, 19),
                "value": 8.0,
            },
            common | {
                "series_id": "gfs-series", "data_family": "forecast",
                "forecast_model": "GFS", "forecast_issue_date": None,
                "forecast_issue_status": "SOURCE_NOT_PROVIDED",
                "forecast_run_id": "gfs-run", "valid_date": date(2026, 8, 19),
                "value": 9.0,
            },
        ]
    )


def test_reader_uses_parquet_predicates_and_preserves_ec_gfs_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], list[tuple[str, str, object]]]] = []
    monkeypatch.setattr(reader, "_resolve_current_versioned", lambda *_args: _resolved(tmp_path))

    def fake_read(_path, *, columns, filters):  # type: ignore[no-untyped-def]
        calls.append((columns, filters))
        return pa.Table.from_pandas(_weather_rows(), preserve_index=False)

    monkeypatch.setattr(reader.pq, "read_table", fake_read)
    result = reader._read_metric_versioned.__wrapped__(
        str(tmp_path), "weather-release", "a" * 64, "soybean", "USA",
        "precipitation", ("illinois",), date(2026, 8, 1), date(2026, 9, 3),
    )

    assert set(result.loc[result["data_type"].eq("forecast"), "model"]) == {"ECMWF", "GFS"}
    assert set(result["series_id"]) == {"observed-series", "ec-series", "gfs-series"}
    assert set(result["current_manifest_sha256"]) == {"a" * 64}
    assert calls[0][1] == [
        ("crop", "=", "soybean"), ("country", "=", "USA"),
        ("metric", "=", "precipitation"), ("region", "in", ["illinois"]),
        ("valid_date", ">=", date(2026, 8, 1)),
        ("valid_date", "<=", date(2026, 9, 3)),
    ]


def test_reader_selects_latest_source_content_run_without_fabricating_issue_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows = _weather_rows()
    old = rows.loc[rows["data_family"].eq("forecast")].copy()
    old["forecast_run_id"] = old["forecast_run_id"].map(lambda value: f"old-{value}")
    old["value"] = old["value"] + 100
    old["extracted_at"] = pd.Timestamp("2026-08-18", tz="UTC")
    rows = pd.concat([old, rows], ignore_index=True)
    monkeypatch.setattr(reader, "_resolve_current_versioned", lambda *_args: _resolved(tmp_path))
    monkeypatch.setattr(
        reader.pq, "read_table",
        lambda *_args, **_kwargs: pa.Table.from_pandas(rows, preserve_index=False),
    )

    result = reader._read_metric_versioned.__wrapped__(
        str(tmp_path), "weather-release", "a" * 64, "soybean", "USA",
        "precipitation", ("illinois",), None, None,
    )

    forecast = result[result["data_type"].eq("forecast")]
    assert set(forecast["forecast_run_id"]) == {"ec-run", "gfs-run"}
    assert set(forecast["value"]) == {8.0, 9.0}
    assert forecast["forecast_run_at"].isna().all()


def test_reader_rejects_ambiguous_latest_forecast_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows = _weather_rows()
    duplicate = rows.loc[rows["forecast_run_id"].eq("ec-run")].copy()
    duplicate["forecast_run_id"] = "another-ec-run"
    rows = pd.concat([rows, duplicate], ignore_index=True)
    monkeypatch.setattr(reader, "_resolve_current_versioned", lambda *_args: _resolved(tmp_path))
    monkeypatch.setattr(
        reader.pq, "read_table",
        lambda *_args, **_kwargs: pa.Table.from_pandas(rows, preserve_index=False),
    )

    with pytest.raises(reader.PublicWeatherCurrentError) as exc:
        reader._read_metric_versioned.__wrapped__(
            str(tmp_path), "weather-release", "a" * 64, "soybean", "USA",
            "precipitation", ("illinois",), None, None,
        )
    assert exc.value.code == reader.PublicWeatherCurrentErrorCode.SERIES_METADATA_MISMATCH


@pytest.mark.parametrize(
    ("rows", "code"),
    [
        (_weather_rows(unit="degC"), reader.PublicWeatherCurrentErrorCode.UNIT_MISMATCH),
        (_weather_rows(metric="temperature_max"), reader.PublicWeatherCurrentErrorCode.METRIC_MISMATCH),
        (_weather_rows().iloc[0:0], reader.PublicWeatherCurrentErrorCode.SERIES_NOT_FOUND),
    ],
)
def test_reader_fails_closed_for_wrong_unit_metric_or_missing_series(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rows: pd.DataFrame,
    code: reader.PublicWeatherCurrentErrorCode,
) -> None:
    monkeypatch.setattr(reader, "_resolve_current_versioned", lambda *_args: _resolved(tmp_path))
    monkeypatch.setattr(
        reader.pq, "read_table",
        lambda *_args, **_kwargs: pa.Table.from_pandas(rows, preserve_index=False),
    )
    with pytest.raises(reader.PublicWeatherCurrentError) as exc:
        reader._read_metric_versioned.__wrapped__(
            str(tmp_path), "weather-release", "a" * 64, "soybean", "USA",
            "precipitation", ("illinois",), None, None,
        )
    assert exc.value.code == code


def test_soil_percent_and_depth_are_consumed_without_second_conversion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bindings = ({
        "series_id": "soil-series", "crop": "soybean", "country": "USA",
        "region": "illinois", "region_label": "Illinois",
        "metric": "soil_moisture", "unit": "%", "soil_depth": "0-100cm",
    },)
    rows = pd.DataFrame([{
        "series_id": "soil-series", "provider_series_id": "provider-soil",
        "source_locator": "soil-source", "source_table": "soil-table",
        "source_column": "Illinois", "business_date": date(2026, 8, 15),
        "value_percent": 29.627, "unit": "%", "metric": "soil_moisture",
        "soil_depth": "0-100cm", "extracted_at": pd.Timestamp("2026-08-19", tz="UTC"),
        "source_row_sha256": "d" * 64,
    }])
    monkeypatch.setattr(
        reader.pq, "read_table",
        lambda *_args, **_kwargs: pa.Table.from_pandas(rows, preserve_index=False),
    )
    result = reader._read_soil_metric(
        _resolved(tmp_path, bindings=bindings), "soybean", "USA", ("illinois",),
        None, None,
    )
    assert result.iloc[0]["value"] == pytest.approx(29.627)
    assert result.iloc[0]["unit"] == "%"
    assert result.iloc[0]["soil_depth"] == "0-100cm"
    assert result.iloc[0]["value_semantics"] == "canonical"


def _manifest(tmp_path: Path, *, complete_series_count: int = 842) -> tuple[Path, dict[str, object]]:
    directory = tmp_path / "releases" / "weather-release"
    directory.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "release_id": "weather-release", "schema_version": "lutou-public-weather-current/1",
        "scope": "current-weather-consumers", "quality_status": "PASS",
        "contract_sha256": reader._CURRENT_CONTRACT_SHA256,
        "complete_row_count": 30, "complete_series_count": complete_series_count,
        "row_count": 10, "series_count": 686, "soil_row_count": 10,
        "soil_series_count": 94, "normal_row_count": 10, "normal_series_count": 62,
        "stable_key_duplicate_count": 0, "normal_stable_key_duplicate_count": 0,
        "forecast_run_count": 2, "source_max_dates": {
            "observation": "2026-08-18", "forecast_valid": "2026-09-02",
        }, "content_sha256": "b" * 64,
    }
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "soil_bindings.json").write_text(
        json.dumps({"binding_count": 102, "bindings": [{} for _ in range(102)]}),
        encoding="utf-8",
    )
    pq.write_table(
        pa.table({
            "data_family": ["forecast", "forecast", "observation"],
            "forecast_run_id": ["ec-run", "gfs-run", "NOT_APPLICABLE"],
        }),
        directory / "observations.parquet",
    )
    return directory, manifest


def test_842_series_manifest_contract_is_enforced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory, manifest = _manifest(tmp_path)
    fake = SimpleNamespace(
        release_id="weather-release", directory=directory, manifest=manifest,
        observations_path=directory / "observations.parquet",
        soil_observations_path=directory / "soil_observations.parquet",
        normals_path=directory / "normals.parquet",
    )
    monkeypatch.setattr(reader, "load_weather_current", lambda _root: fake)
    sha = identify_file(directory / "manifest.json").sha256
    reader._resolve_current_versioned.cache_clear()
    assert reader._resolve_current_versioned(str(tmp_path), "weather-release", sha).identity.release_id == "weather-release"

    directory, manifest = _manifest(tmp_path, complete_series_count=841)
    fake.manifest = manifest
    sha = identify_file(directory / "manifest.json").sha256
    reader._resolve_current_versioned.cache_clear()
    with pytest.raises(reader.PublicWeatherCurrentError) as exc:
        reader._resolve_current_versioned(str(tmp_path), "weather-release", sha)
    assert exc.value.code == reader.PublicWeatherCurrentErrorCode.INVALID_CURRENT_MANIFEST


def test_sealed_series_identity_contract_sha_is_enforced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory, manifest = _manifest(tmp_path)
    manifest["contract_sha256"] = "e" * 64
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    fake = SimpleNamespace(
        release_id="weather-release", directory=directory, manifest=manifest,
        observations_path=directory / "observations.parquet",
        soil_observations_path=directory / "soil_observations.parquet",
        normals_path=directory / "normals.parquet",
    )
    monkeypatch.setattr(reader, "load_weather_current", lambda _root: fake)
    sha = identify_file(directory / "manifest.json").sha256
    reader._resolve_current_versioned.cache_clear()
    with pytest.raises(reader.PublicWeatherCurrentError) as exc:
        reader._resolve_current_versioned(str(tmp_path), "weather-release", sha)
    assert exc.value.code == reader.PublicWeatherCurrentErrorCode.INVALID_CURRENT_MANIFEST


@pytest.mark.parametrize("invalid_value", [None, "2", 0, 1, 3])
def test_forecast_run_count_is_required_and_matches_parquet_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, invalid_value: object
) -> None:
    directory, manifest = _manifest(tmp_path)
    if invalid_value is None:
        del manifest["forecast_run_count"]
    else:
        manifest["forecast_run_count"] = invalid_value
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    fake = SimpleNamespace(
        release_id="weather-release", directory=directory, manifest=manifest,
        observations_path=directory / "observations.parquet",
        soil_observations_path=directory / "soil_observations.parquet",
        normals_path=directory / "normals.parquet",
    )
    monkeypatch.setattr(reader, "load_weather_current", lambda _root: fake)
    sha = identify_file(directory / "manifest.json").sha256
    reader._resolve_current_versioned.cache_clear()
    with pytest.raises(reader.PublicWeatherCurrentError) as exc:
        reader._resolve_current_versioned(str(tmp_path), "weather-release", sha)
    assert exc.value.code == reader.PublicWeatherCurrentErrorCode.INVALID_CURRENT_MANIFEST


def test_missing_or_invalid_current_pointer_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(reader.PublicWeatherCurrentError) as missing:
        reader.resolve_weather_current_identity(tmp_path)
    assert missing.value.code == reader.PublicWeatherCurrentErrorCode.PUBLIC_CURRENT_UNAVAILABLE
    (tmp_path / "current.json").write_text("{}", encoding="utf-8")
    with pytest.raises(reader.PublicWeatherCurrentError) as invalid:
        reader.resolve_weather_current_identity(tmp_path)
    assert invalid.value.code == reader.PublicWeatherCurrentErrorCode.INVALID_CURRENT_MANIFEST


def test_pointer_manifest_sha_mismatch_fails_closed(tmp_path: Path) -> None:
    directory, _manifest_value = _manifest(tmp_path)
    (tmp_path / "current.json").write_text(
        json.dumps({
            "schema_version": 1,
            "release_id": directory.name,
            "manifest_sha256": "a" * 64,
        }),
        encoding="utf-8",
    )

    with pytest.raises(reader.PublicWeatherCurrentError) as exc:
        reader.resolve_weather_current_identity(tmp_path)
    assert exc.value.code == reader.PublicWeatherCurrentErrorCode.INVALID_CURRENT_MANIFEST


def test_release_identity_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory, manifest = _manifest(tmp_path)
    manifest["release_id"] = "another-weather-release"
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    fake = SimpleNamespace(
        release_id="weather-release", directory=directory, manifest=manifest,
        observations_path=directory / "observations.parquet",
        soil_observations_path=directory / "soil_observations.parquet",
        normals_path=directory / "normals.parquet",
    )
    monkeypatch.setattr(reader, "load_weather_current", lambda _root: fake)
    sha = identify_file(directory / "manifest.json").sha256
    reader._resolve_current_versioned.cache_clear()

    with pytest.raises(reader.PublicWeatherCurrentError) as exc:
        reader._resolve_current_versioned(str(tmp_path), "weather-release", sha)
    assert exc.value.code == reader.PublicWeatherCurrentErrorCode.INVALID_CURRENT_MANIFEST
