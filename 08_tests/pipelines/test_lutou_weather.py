from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from agri_research_agent.data_sources.lutou.live import (
    LutouBatch,
    LutouConnectionProof,
    LutouPlanProof,
    LutouQuery,
)
from agri_research_agent.data_sources.lutou.weather_live import (
    WeatherSeries,
    WeatherSourceCatalog,
    WeatherTable,
    load_weather_config,
)
from agri_research_agent.pipelines.lutou_goal_b_soil import run_goal_b_soil
from agri_research_agent.pipelines.lutou_weather import (
    LutouWeatherError,
    _table_query_windows,
    load_weather_current,
    run_lutou_weather,
)
from agri_research_agent.shared.file_identity import identify_file
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode

OBS_DAY = date(2026, 8, 18)
VALID_DAY = date(2026, 8, 20)


class FakeSoilClient:
    proof = LutouConnectionProof(
        "8.0.37", "MySQL", "SYSTEM", "SYSTEM", False, True, 3, ()
    )

    def inspect_query(self, query):  # type: ignore[no-untyped-def]
        return ()

    def plan_stream(self, query, start, end):  # type: ignore[no-untyped-def]
        day = date(2026, 8, 9)
        row = {query.date_column: day} | {
            column: Decimal("0.236") for column in query.value_columns
        }
        plan = LutouPlanProof(query.sha256, 1, query.max_plan_rows)
        batch = LutouBatch(query, plan, (row,), datetime(2026, 8, 19, tzinfo=UTC))
        return plan, iter((batch,))


class FakeWeatherClient:
    proof = LutouConnectionProof(
        "8.0.37", "MySQL", "SYSTEM", "SYSTEM", False, True, 3, ()
    )

    def __init__(self, rain: Decimal = Decimal("12.5")) -> None:
        self.rain = rain
        self.windows: list[tuple[str, date, date]] = []

    def plan_stream(self, query, start, end):  # type: ignore[no-untyped-def]
        self.windows.append((query.table, start, end))
        forecast = "forecast" in query.table
        day = VALID_DAY if forecast else OBS_DAY
        values = {}
        for column in query.value_columns:
            if "rain" in query.table:
                values[column] = self.rain
            elif "temp" in query.table:
                values[column] = Decimal("31.25")
            else:
                values[column] = Decimal(1)
        row = {query.date_column: day} | values
        plan = LutouPlanProof(query.sha256, 1, query.max_plan_rows)
        batch = LutouBatch(query, plan, (row,), datetime(2026, 8, 19, tzinfo=UTC))
        return plan, iter((batch,))


class FailOnceWeatherClient(FakeWeatherClient):
    def __init__(self, *, fail_on_call: int) -> None:
        super().__init__()
        self.fail_on_call = fail_on_call
        self.plan_calls = 0
        self.failed = False

    def plan_stream(self, query, start, end):  # type: ignore[no-untyped-def]
        self.plan_calls += 1
        if self.plan_calls == self.fail_on_call and not self.failed:
            self.failed = True
            raise RuntimeError("injected one-time source partition failure")
        return super().plan_stream(query, start, end)


def _runtime(path: Path) -> RuntimeContext:
    path.mkdir(parents=True)
    (path / ".market-data-runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_id": "goal-d2a-test",
                "classification": "isolated-dev",
                "module_id": "international-spread",
                "created_at": "2026-08-19T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return RuntimeContext(RuntimeMode.ISOLATED_DEV, "international-spread", path)


def _seed_soil(runtime: RuntimeContext) -> None:
    run_goal_b_soil(
        FakeSoilClient(),
        runtime=runtime,
        run_id="soil-current",
        end_date=date(2026, 8, 9),
        full_load=True,
        catalog_path=Path("02_configs/public_research_data_catalog.candidate.json"),
    )


def _seed_normal_baselines(root: Path) -> None:
    if root.exists():
        return
    sources = (
        (
            "soybean_weather_us.yaml",
            "soybean/us/soybean_weather_us_30y_normal.parquet",
        ),
        (
            "soybean_weather_br.yaml",
            "soybean/br/soybean_weather_br_30y_normal.parquet",
        ),
        (
            "soybean_weather_ar.yaml",
            "soybean/ar/soybean_weather_ar_30y_normal.parquet",
        ),
        (
            "rapeseed_weather_can.yaml",
            "rapeseed/can/rapeseed_weather_can_30y_normal.parquet",
        ),
    )
    for config_name, relative in sources:
        config = load_weather_config(Path("02_configs") / config_name)
        rows = []
        for metric, unit in (("precipitation", "mm"), ("temperature_max", "degC")):
            configured = config.get("normal_sheets", {})
            sheet = str(
                configured.get(
                    metric,
                    {
                        "precipitation": "美国历史30年平均降雨",
                        "temperature_max": "美国历史30年平均最高气温",
                    }[metric],
                )
            )
            for offset in range(365):
                month_day = (date(2001, 1, 1) + timedelta(days=offset)).strftime(
                    "%m-%d"
                )
                for region in config["regions"]:
                    rows.append(
                        {
                            "month_day": month_day,
                            "country": str(config["country"]),
                            "region": str(region["key"]),
                            "metric": metric,
                            "normal_value": 1.0,
                            "unit": unit,
                            "baseline_label": "沿用旧项目30年历史同期基准",
                            "source_workbook_sha256": "a" * 64,
                            "source_sheet": sheet,
                        }
                    )
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows), destination)


def _series(
    table: str, metric: str, family: str, model: str, unit: str
) -> WeatherSeries:
    return WeatherSeries(
        series_id=f"weather.{metric}.test.usa.region.{family}.{model.lower()}",
        provider_dataset_id=f"lutou:天气2.0:{table}",
        provider_series_id=f"lutou:天气2.0:{table}:value",
        crop="test",
        country="USA",
        region="region",
        region_label="Region",
        region_type="consumer_display_region",
        metric=metric,
        data_family=family,
        model=model,
        source_table=table,
        source_column="value",
        date_column="date",
        source_unit=unit,
        unit=unit,
        interval="P1D",
        aggregation=("daily_total" if metric == "precipitation" else "daily_maximum"),
        transformation_id="weather.identity/1",
    )


def _catalog() -> WeatherSourceCatalog:
    series = (
        _series("rain-observation", "precipitation", "observation", "OBSERVED", "mm"),
        _series(
            "temp-observation", "temperature_max", "observation", "OBSERVED", "degC"
        ),
        _series("rain-forecast-ec", "precipitation", "forecast", "ECMWF", "mm"),
        _series("rain-forecast-gfs", "precipitation", "forecast", "GFS", "mm"),
    )
    tables = []
    for item in series:
        query = LutouQuery("天气2.0", item.source_table, "date", ("value",))
        forecast = item.data_family == "forecast"
        tables.append(
            WeatherTable(
                item.source_table,
                "date",
                item.data_family,
                item.model,
                (item,),
                query,
                VALID_DAY if forecast else OBS_DAY,
                VALID_DAY if forecast else OBS_DAY,
                1,
            )
        )
    inventory = tuple(
        {
            "TABLE_NAME": item.source_table,
            "TABLE_TYPE": "BASE TABLE",
            "TABLE_ROWS": 1,
            "TABLE_COMMENT": "",
            "UPDATE_TIME": None,
        }
        for item in series
    ) + tuple(
        {
            "TABLE_NAME": f"other-{index}",
            "TABLE_TYPE": "BASE TABLE",
            "TABLE_ROWS": 0,
            "TABLE_COMMENT": "",
            "UPDATE_TIME": None,
        }
        for index in range(561)
    )
    return WeatherSourceCatalog(
        schema_version="lutou-public-weather-current/1",
        source_schema="天气2.0",
        observation_lookback_days=31,
        forecast_horizon_days=15,
        tables=tuple(tables),
        series=series,
        schema_inventory=inventory,
        audited_inventory_table_count=565,
        inventory_audited_at="2026-08-19",
        consumer_soil_table_count=11,
        relation_audit_performed=True,
        derived_contracts=({"identity": "derived", "formula": "sum(daily)"},),
        canada_region_policy={
            "source_weighted_derivation_status": "SOURCE_NOT_PROVIDED"
        },
    )


def _run(
    runtime: RuntimeContext,
    run_id: str,
    *,
    full: bool,
    client: FakeWeatherClient | None = None,
    failure_hook: str | None = None,
):
    baseline_root = runtime.runtime_root / "approved-normal-baselines"
    _seed_normal_baselines(baseline_root)
    return run_lutou_weather(
        client or FakeWeatherClient(),
        runtime=runtime,
        run_id=run_id,
        as_of_date=date(2026, 8, 19),
        full_load=full,
        policy_path=Path("02_configs/lutou_weather_current.yaml"),
        baseline_root=baseline_root,
        source_catalog=_catalog(),
        failure_hook=failure_hook,
    )


def test_complete_weather_current_preserves_identity_and_is_idempotent(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "runtime")
    _seed_soil(runtime)
    client = FakeWeatherClient()
    first = _run(runtime, "weather-full", full=True, client=client)
    pointer = runtime.runtime_root / "public-market-data/lutou-weather/current.json"
    before = identify_file(pointer)
    second = _run(runtime, "weather-repeat", full=False, client=client)
    current = load_weather_current(pointer.parent)
    assert current is not None

    rows = pq.read_table(current.observations_path).to_pylist()
    soil_source = (
        runtime.runtime_root
        / "public-market-data/lutou-soil-moisture/releases/soil-current"
        / "observations.parquet"
    )
    assert first.promoted is True and second.promoted is False
    assert first.mode == "full"
    assert second.mode == "incremental-31-day-observation-plus-full-forecast"
    second_windows = client.windows[-4:]
    assert {
        (start, end)
        for table, start, end in second_windows
        if "forecast" not in table
    } == {(date(2026, 7, 18), date(2026, 8, 19))}
    assert {
        (start, end)
        for table, start, end in second_windows
        if "forecast" in table
    } == {(date(2026, 8, 19), date(2026, 9, 2))}
    performance = second.candidate_manifest["quality"]["table_query_performance"]
    assert len(performance) == 4
    assert {item["data_family"] for item in performance} == {
        "observation",
        "forecast",
    }
    assert all(item["duration_seconds"] >= 0 for item in performance)
    assert identify_file(pointer) == before
    assert len(rows) == 4
    assert len({_key(row) for row in rows}) == 4
    assert {row["forecast_model"] for row in rows} == {"OBSERVED", "ECMWF", "GFS"}
    forecast = [row for row in rows if row["data_family"] == "forecast"]
    assert all(row["forecast_issue_date"] is None for row in forecast)
    assert {row["forecast_issue_status"] for row in forecast} == {"SOURCE_NOT_PROVIDED"}
    assert len({row["forecast_run_id"] for row in forecast}) == 2
    assert (
        identify_file(current.soil_observations_path).sha256
        == identify_file(soil_source).sha256
    )
    normals = pq.read_table(current.normals_path)
    assert normals.num_rows == 22_630
    assert len(set(normals["series_id"].to_pylist())) == 62
    assert set(normals["data_family"].to_pylist()) == {"historical_climatology"}
    bindings = json.loads(
        (current.directory / "soil_bindings.json").read_text(encoding="utf-8")
    )
    assert bindings["binding_count"] == 102
    assert len({item["series_id"] for item in bindings["bindings"]}) == 94
    assert (
        sum(
            item["consumer_status"] == "MAPPED_DISPLAY" for item in bindings["bindings"]
        )
        == 85
    )
    assert current.manifest["normal_series_count"] == 62
    assert current.manifest["normal_row_count"] == 22_630
    assert current.manifest["complete_series_count"] == 160
    assert current.manifest["complete_row_count"] == 22_728
    assert current.manifest["stable_key_duplicate_count"] == 0
    assert current.manifest["normal_stable_key_duplicate_count"] == 0


def test_weather_uses_self_contained_soil_release_without_candidate_history(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "runtime")
    _seed_soil(runtime)
    soil_root = runtime.runtime_root / "public-market-data/lutou-soil-moisture"
    shutil.rmtree(soil_root / "candidates" / "soil-current")

    result = _run(runtime, "weather-without-soil-candidate", full=True)

    assert result.promoted is True
    assert result.current_manifest["soil_current"]["release_id"] == "soil-current"
    assert not (soil_root / "candidates" / "soil-current").exists()


def test_weather_accepts_legacy_soil_release_after_no_change_without_candidate(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "runtime")
    _seed_soil(runtime)
    unchanged = run_goal_b_soil(
        FakeSoilClient(), runtime=runtime, run_id="soil-no-change",
        end_date=date(2026, 8, 9), full_load=False,
        catalog_path=Path("02_configs/public_research_data_catalog.candidate.json"),
    )
    assert unchanged.promoted is False
    soil_root = runtime.runtime_root / "public-market-data/lutou-soil-moisture"
    release_manifest = soil_root / "releases/soil-current/manifest.json"
    legacy = json.loads(release_manifest.read_text(encoding="utf-8"))
    legacy.pop("weather_evidence")
    release_manifest.write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    pointer_path = soil_root / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["manifest_sha256"] = identify_file(release_manifest).sha256
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    shutil.rmtree(soil_root / "candidates" / "soil-current")

    weather = _run(runtime, "weather-from-legacy-soil", full=True)

    evidence = weather.current_manifest["soil_current"]
    assert weather.promoted is True
    assert evidence["retained_exception_count"] is None
    assert evidence["retained_exception_count_status"] == "NOT_RECORDED_LEGACY_RELEASE"


def test_weather_uses_updated_soil_release_without_candidate_history(
    tmp_path: Path,
) -> None:
    class UpdatedSoilClient(FakeSoilClient):
        def plan_stream(self, query, start, end):  # type: ignore[no-untyped-def]
            plan, batches = super().plan_stream(query, start, end)
            batch = next(batches)
            row = dict(batch.rows[0])
            for column in query.value_columns:
                row[column] = Decimal("0.500")
            return plan, iter((LutouBatch(query, plan, (row,), batch.extracted_at),))

    runtime = _runtime(tmp_path / "runtime")
    _seed_soil(runtime)
    updated = run_goal_b_soil(
        UpdatedSoilClient(), runtime=runtime, run_id="soil-updated",
        end_date=date(2026, 8, 9), full_load=False,
        catalog_path=Path("02_configs/public_research_data_catalog.candidate.json"),
    )
    assert updated.promoted is True
    soil_root = runtime.runtime_root / "public-market-data/lutou-soil-moisture"
    shutil.rmtree(soil_root / "candidates" / "soil-updated")

    weather = _run(runtime, "weather-from-updated-soil", full=True)

    assert weather.promoted is True
    assert weather.current_manifest["soil_current"]["release_id"] == "soil-updated"


def test_approved_normal_revision_promotes_without_changing_series_count(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "runtime")
    _seed_soil(runtime)
    first = _run(runtime, "weather-full", full=True)
    baseline = (
        runtime.runtime_root
        / "approved-normal-baselines/soybean/us/soybean_weather_us_30y_normal.parquet"
    )
    rows = pq.read_table(baseline).to_pylist()
    rows[0]["normal_value"] = 2.0
    pq.write_table(pa.Table.from_pylist(rows), baseline)

    second = _run(runtime, "weather-normal-revision", full=False)

    assert first.promoted is True and second.promoted is True
    assert second.current_manifest["normal_series_count"] == 62
    assert (
        second.current_manifest["normal_content_sha256"]
        != first.current_manifest["normal_content_sha256"]
    )


def test_incremental_upgrades_validated_legacy_current_without_normals(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "runtime")
    _seed_soil(runtime)
    first = _run(runtime, "weather-legacy-seed", full=True)
    release = first.current_directory
    (release / "normals.parquet").unlink()
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["files"]["normals.parquet"]
    for field in (
        "normal_row_count",
        "normal_series_count",
        "normal_stable_key_duplicate_count",
        "normal_content_sha256",
    ):
        manifest.pop(field, None)
    manifest["complete_row_count"] -= 22_630
    manifest["complete_series_count"] -= 62
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    pointer_path = release.parent.parent / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["manifest_sha256"] = identify_file(manifest_path).sha256
    pointer_path.write_text(
        json.dumps(pointer, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(LutouWeatherError, match="normal Current is missing"):
        load_weather_current(pointer_path.parent)

    upgraded = _run(runtime, "weather-baseline-upgrade", full=False)
    current = load_weather_current(pointer_path.parent)

    assert upgraded.promoted is True
    assert current is not None
    assert current.normals_path.exists()
    assert current.manifest["normal_row_count"] == 22_630
    assert current.manifest["historical_base"] == {
        "release_id": "weather-legacy-seed",
        "manifest_sha256": identify_file(manifest_path).sha256,
        "row_count": 4,
        "normal_upgrade_required": True,
    }


def test_full_seed_resumes_completed_immutable_source_partitions(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "runtime")
    _seed_soil(runtime)
    client = FailOnceWeatherClient(fail_on_call=3)

    with pytest.raises(RuntimeError, match="one-time source partition failure"):
        _run(runtime, "weather-seed-failed", full=True, client=client)

    seed_root = (
        runtime.runtime_root
        / "public-market-data"
        / "lutou-weather-seed-partitions"
    )
    completed_before_retry = list(seed_root.glob("seed-*/table-*/manifest.json"))
    assert len(completed_before_retry) == 2
    assert all(
        json.loads(path.read_text(encoding="utf-8"))["status"] == "COMPLETE"
        for path in completed_before_retry
    )

    result = _run(runtime, "weather-seed-resumed", full=True, client=client)

    assert result.promoted is True
    assert client.plan_calls == 5
    assert result.candidate_manifest["quality"]["required_partition_count"] == 4
    assert result.candidate_manifest["quality"]["completed_partition_count"] == 4
    assert result.candidate_manifest["quality"]["resumed_partition_count"] == 2
    assert result.candidate_manifest["quality"]["resumable_seed_enabled"] is True


def test_full_seed_chunks_historical_years_and_reuses_completed_prefix() -> None:
    catalog = _catalog()
    observation = next(
        item for item in catalog.tables if item.data_family == "observation"
    )
    forecast = next(item for item in catalog.tables if item.data_family == "forecast")

    assert _table_query_windows(
        observation,
        date(2024, 7, 1),
        date(2026, 8, 19),
        resumable_seed=True,
    ) == (
        (date(2024, 7, 1), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 8, 19)),
    )
    assert _table_query_windows(
        observation,
        date(2024, 7, 1),
        date(2031, 8, 19),
        resumable_seed=True,
        completed_windows=(
            (date(2024, 7, 1), date(2024, 12, 31)),
            (date(2025, 1, 1), date(2025, 12, 31)),
        ),
    ) == (
        (date(2024, 7, 1), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 12, 31)),
        (date(2027, 1, 1), date(2027, 12, 31)),
        (date(2028, 1, 1), date(2028, 12, 31)),
        (date(2029, 1, 1), date(2029, 12, 31)),
        (date(2030, 1, 1), date(2030, 12, 31)),
        (date(2031, 1, 1), date(2031, 8, 19)),
    )
    assert _table_query_windows(
        forecast,
        date(2026, 8, 19),
        date(2026, 9, 2),
        resumable_seed=True,
    ) == ((date(2026, 8, 19), date(2026, 9, 2)),)


def test_invalid_rainfall_is_retained_in_candidate_and_excluded_from_current(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "runtime")
    _seed_soil(runtime)
    result = _run(
        runtime,
        "weather-negative-rain",
        full=True,
        client=FakeWeatherClient(Decimal(-1)),
    )
    standard = pq.read_table(result.candidate_directory / "standard.parquet")
    assert result.candidate_manifest["quality"]["negative_rainfall_count"] == 3
    assert "NEGATIVE_RAINFALL" in set(standard["quality_status"].to_pylist())
    current = load_weather_current(result.current_directory.parent.parent)
    assert current is not None
    rows = pq.read_table(current.observations_path).to_pylist()
    assert {row["metric"] for row in rows} == {"temperature_max"}


def test_missing_approved_normal_preserves_previous_weather_current(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "runtime")
    _seed_soil(runtime)
    _run(runtime, "weather-good", full=True)
    pointer = runtime.runtime_root / "public-market-data/lutou-weather/current.json"
    before = identify_file(pointer)
    missing = (
        runtime.runtime_root
        / "approved-normal-baselines/soybean/us/soybean_weather_us_30y_normal.parquet"
    )
    missing.unlink()

    with pytest.raises(LutouWeatherError, match="normal baseline is missing"):
        _run(runtime, "weather-missing-normal", full=False)

    assert identify_file(pointer) == before
    assert load_weather_current(pointer.parent).release_id == "weather-good"


@pytest.mark.parametrize(
    "hook",
    [
        "rainfall_extraction",
        "temperature_extraction",
        "forecast_extraction",
        "candidate_qc",
        "forecast_collision",
        "canonical_collision",
        "invalid_manifest",
        "promotion",
    ],
)
def test_failures_preserve_previous_weather_current(tmp_path: Path, hook: str) -> None:
    runtime = _runtime(tmp_path / hook)
    _seed_soil(runtime)
    _run(runtime, "weather-good", full=True)
    pointer = runtime.runtime_root / "public-market-data/lutou-weather/current.json"
    before = identify_file(pointer)
    with pytest.raises(LutouWeatherError):
        _run(
            runtime,
            f"weather-fail-{hook}",
            full=False,
            client=(
                FakeWeatherClient(Decimal(13))
                if hook in {"invalid_manifest", "promotion"}
                else None
            ),
            failure_hook=hook,
        )
    assert identify_file(pointer) == before
    assert load_weather_current(pointer.parent).release_id == "weather-good"


def _key(row):  # type: ignore[no-untyped-def]
    return row["series_id"], row["valid_date"], row["forecast_run_id"]
