from __future__ import annotations

import json
import shutil
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.data_sources.usda_client import (
    build_soybean_crop_weekly_current_year_query_grid,
)
from agri_research_agent.pipelines.soybean_crop_comparison import (
    build_dashboard_comparisons,
    load_display_config,
)
from agri_research_agent.pipelines.soybean_crop_progress import (
    CONDITION_UNIT_TO_METRIC,
    CROP_WEEKLY_COLUMNS,
    PROGRESS_UNIT_TO_METRIC,
)
from agri_research_agent.pipelines.soybean_crop_weekly_update import (
    SoybeanWeeklyUpdateError,
    compare_business_records,
    is_soybean_reporting_season,
    merge_current_year,
    replace_processed_pair,
    run_soybean_crop_weekly_update,
    soybean_weekly_update_paths,
    validate_candidate_pair,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FAKE_SECRET = "weekly-update-fixture-secret"
JULY_2026 = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


def _row(
    year: int,
    level: str,
    family: str,
    unit_desc: str,
    *,
    value: float,
    week_ending: str | None = None,
) -> dict[str, object]:
    national = level == "NATIONAL"
    return {
        "source_desc": "SURVEY",
        "sector_desc": "CROPS",
        "group_desc": "FIELD CROPS",
        "commodity_desc": "SOYBEANS",
        "freq_desc": "WEEKLY",
        "statisticcat_desc": family,
        "unit_desc": unit_desc,
        "short_desc": f"SOYBEANS - {family}, MEASURED IN {unit_desc}",
        "reference_period_desc": "WEEK #29",
        "agg_level_desc": level,
        "location_desc": "US TOTAL" if national else "IOWA",
        "state_alpha": "" if national else "IA",
        "state_fips_code": "" if national else "19",
        "state_name": "" if national else "IOWA",
        "year": year,
        "week_ending": week_ending or f"{year}-07-19",
        "Value": value,
        "load_time": f"{year}-07-20 16:00:00.000",
    }


class CurrentYearTransport:
    def __init__(
        self,
        *,
        planted_value: float = 100,
        empty: bool = False,
        fail: bool = False,
        decline: bool = False,
    ) -> None:
        self.planted_value = planted_value
        self.empty = empty
        self.fail = fail
        self.decline = decline
        self.queries: list[dict[str, list[str]]] = []

    def __call__(self, url: str, timeout: int) -> tuple[int, bytes]:
        assert timeout > 0
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.queries.append(query)
        if self.fail:
            raise OSError(f"fixture failure URL={url}")
        if self.empty:
            return 200, b'{"data": []}'
        year = int(query["year"][0])
        level = query["agg_level_desc"][0]
        family = query["statisticcat_desc"][0]
        if family == "PROGRESS":
            rows = [
                _row(
                    year,
                    level,
                    family,
                    unit,
                    value=(
                        self.planted_value
                        if metric == "PLANTED"
                        else 100
                    ),
                )
                for unit, metric in PROGRESS_UNIT_TO_METRIC.items()
            ]
            if self.decline:
                rows.extend(
                    [
                        _row(
                            year,
                            level,
                            family,
                            "PCT PLANTED",
                            value=100,
                            week_ending=f"{year}-07-12",
                        ),
                        _row(
                            year,
                            level,
                            family,
                            "PCT PLANTED",
                            value=90,
                            week_ending=f"{year}-07-26",
                        ),
                    ]
                )
        else:
            values = {
                "PCT VERY POOR": 5,
                "PCT POOR": 10,
                "PCT FAIR": 25,
                "PCT GOOD": 40,
                "PCT EXCELLENT": 20,
            }
            rows = [
                _row(year, level, family, unit, value=value)
                for unit, value in values.items()
            ]
        return 200, json.dumps({"data": rows}).encode("utf-8")


def _prepare_root(tmp_path: Path) -> dict[str, Path]:
    paths = soybean_weekly_update_paths(tmp_path)
    paths["legacy_progress"].parent.mkdir(parents=True, exist_ok=True)
    source = soybean_weekly_update_paths(PROJECT_ROOT)
    progress_rows: list[dict[str, object]] = []
    condition_rows: list[dict[str, object]] = []

    def processed_row(
        year: int,
        level: str,
        family: str,
        metric: str,
        unit_desc: str,
        value: float,
        *,
        derived: bool = False,
    ) -> dict[str, object]:
        national = level == "US"
        row = {
            "system": "USDA_NASS_QUICKSTATS",
            "dataset": "CROP_PROGRESS",
            "metric_family": family,
            "commodity": "SOYBEANS",
            "metric": metric,
            "geography_level": level,
            "region_code": "US" if national else "IA",
            "region_name": "US TOTAL" if national else "IOWA",
            "calendar_year": year,
            "week_ending": pd.Timestamp(f"{year}-07-12"),
            "reference_period_desc": "WEEK #28",
            "value_pct": value,
            "unit": "PCT",
            "source_statisticcat_desc": family,
            "source_unit_desc": unit_desc,
            "source_short_desc": (
                "DERIVED: GOOD + EXCELLENT"
                if derived
                else f"SOYBEANS - {family}, MEASURED IN {unit_desc}"
            ),
            "source_location_desc": "US TOTAL" if national else "IOWA",
            "source_load_time": f"{year}-07-13 16:00:00.000",
            "retrieved_at_utc": "2026-07-16T00:00:00+00:00",
            "raw_value": pd.NA if derived else str(value),
            "suppression_code": pd.NA,
            "raw_snapshot": "legacy-fixture.json",
            "is_derived": derived,
            "derivation_formula": "GOOD + EXCELLENT" if derived else pd.NA,
            "derivation_good_value_pct": 40.0 if derived else pd.NA,
            "derivation_excellent_value_pct": 20.0 if derived else pd.NA,
            "derivation_source_records": (
                '{"EXCELLENT":"fixture","GOOD":"fixture"}'
                if derived
                else pd.NA
            ),
        }
        return row

    condition_values = {
        "VERY_POOR": ("PCT VERY POOR", 5.0),
        "POOR": ("PCT POOR", 10.0),
        "FAIR": ("PCT FAIR", 25.0),
        "GOOD": ("PCT GOOD", 40.0),
        "EXCELLENT": ("PCT EXCELLENT", 20.0),
    }
    for year in range(2021, 2027):
        for level in ("US", "STATE"):
            progress_rows.extend(
                processed_row(
                    year,
                    level,
                    "PROGRESS",
                    metric,
                    unit,
                    50.0,
                )
                for unit, metric in PROGRESS_UNIT_TO_METRIC.items()
            )
            condition_rows.extend(
                processed_row(
                    year,
                    level,
                    "CONDITION",
                    metric,
                    unit,
                    value,
                )
                for metric, (unit, value) in condition_values.items()
            )
            condition_rows.append(
                processed_row(
                    year,
                    level,
                    "CONDITION",
                    "GOOD_EXCELLENT",
                    "PCT GOOD + PCT EXCELLENT",
                    60.0,
                    derived=True,
                )
            )
    pd.DataFrame(progress_rows, columns=CROP_WEEKLY_COLUMNS).to_parquet(
        paths["legacy_progress"], index=False
    )
    pd.DataFrame(condition_rows, columns=CROP_WEEKLY_COLUMNS).to_parquet(
        paths["legacy_condition"], index=False
    )
    paths["display_config"].parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source["display_config"], paths["display_config"])
    return paths


def _run(
    tmp_path: Path,
    transport: CurrentYearTransport,
    *,
    now: datetime = JULY_2026,
    dry_run: bool = False,
) -> dict[str, object]:
    return run_soybean_crop_weekly_update(
        project_root=tmp_path,
        dry_run=dry_run,
        transport=transport,
        retries=0,
        now=now,
        sleep=lambda _: None,
        env={"NASS_API_KEY": FAKE_SECRET},
    )


def test_current_year_query_grid_has_only_four_secret_free_requests() -> None:
    queries = build_soybean_crop_weekly_current_year_query_grid(2027)

    assert [
        (item["statisticcat_desc"], item["agg_level_desc"])
        for item in queries
    ] == [
        ("PROGRESS", "NATIONAL"),
        ("PROGRESS", "STATE"),
        ("CONDITION", "NATIONAL"),
        ("CONDITION", "STATE"),
    ]
    assert {item["year"] for item in queries} == {"2027"}
    assert all(item["unit_desc__LIKE"] == "PCT" for item in queries)
    assert all("key" not in item for item in queries)


def test_reporting_season_runs_from_april_through_november() -> None:
    assert not is_soybean_reporting_season(date(2026, 3, 31))
    assert is_soybean_reporting_season(date(2026, 4, 1))
    assert is_soybean_reporting_season(date(2026, 11, 30))
    assert not is_soybean_reporting_season(date(2026, 12, 1))


def test_initialization_replaces_current_year_and_preserves_prior_years(
    tmp_path: Path,
) -> None:
    paths = _prepare_root(tmp_path)
    old_progress = pd.read_parquet(paths["legacy_progress"])
    old_condition = pd.read_parquet(paths["legacy_condition"])
    transport = CurrentYearTransport()

    audit = _run(tmp_path, transport)
    progress = pd.read_parquet(paths["stable_progress"])
    condition = pd.read_parquet(paths["stable_condition"])

    assert audit["status"] == "initialized"
    assert audit["published"] is True
    assert len(transport.queries) == 4
    assert {query["year"][0] for query in transport.queries} == {"2026"}
    assert len(progress.loc[progress["calendar_year"].eq(2026)]) == 18
    assert len(condition.loc[condition["calendar_year"].eq(2026)]) == 12
    assert condition.loc[
        condition["metric"].eq("GOOD_EXCELLENT")
        & condition["calendar_year"].eq(2026),
        "value_pct",
    ].eq(60).all()
    sort_columns = [
        "metric",
        "calendar_year",
        "geography_level",
        "region_code",
        "week_ending",
    ]
    pd.testing.assert_frame_equal(
        old_progress.loc[old_progress["calendar_year"].lt(2026)]
        .sort_values(sort_columns)
        .reset_index(drop=True),
        progress.loc[progress["calendar_year"].lt(2026)]
        .sort_values(sort_columns)
        .reset_index(drop=True),
        check_dtype=False,
    )
    old_condition_prior = (
        old_condition.loc[old_condition["calendar_year"].lt(2026)]
        .sort_values(sort_columns)
        .reset_index(drop=True)
        .astype("string")
        .fillna("<NA>")
    )
    new_condition_prior = (
        condition.loc[condition["calendar_year"].lt(2026)]
        .sort_values(sort_columns)
        .reset_index(drop=True)
        .astype("string")
        .fillna("<NA>")
    )
    pd.testing.assert_frame_equal(old_condition_prior, new_condition_prior)
    assert paths["legacy_progress"].is_file()
    assert paths["legacy_condition"].is_file()
    assert audit["candidate_validation"]["passed"] is True


def test_historical_revision_and_new_week_are_business_changes(
    tmp_path: Path,
) -> None:
    paths = _prepare_root(tmp_path)
    _run(tmp_path, CurrentYearTransport())
    initial = pd.read_parquet(paths["stable_progress"])
    revised = initial.copy()
    planted = revised["metric"].eq("PLANTED") & revised["calendar_year"].eq(2026)
    revised.loc[planted, "value_pct"] = 99
    template = revised.loc[planted].iloc[0].copy()
    template["week_ending"] = pd.Timestamp("2026-07-26")
    revised = pd.concat([revised, pd.DataFrame([template])], ignore_index=True)

    changes = compare_business_records(initial, revised)

    assert changes["added"] == 1
    assert changes["corrected"] == 2
    assert changes["deleted"] == 0
    assert changes["business_changed"] is True


def test_no_change_does_not_rewrite_stable_processed(tmp_path: Path) -> None:
    paths = _prepare_root(tmp_path)
    first = _run(tmp_path, CurrentYearTransport())
    progress_stat = paths["stable_progress"].stat()
    condition_stat = paths["stable_condition"].stat()
    progress_bytes = paths["stable_progress"].read_bytes()
    condition_bytes = paths["stable_condition"].read_bytes()

    second = _run(
        tmp_path,
        CurrentYearTransport(),
        now=datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
    )

    assert first["status"] == "initialized"
    assert second["status"] == "no_change"
    assert second["published"] is False
    assert paths["stable_progress"].stat().st_mtime_ns == progress_stat.st_mtime_ns
    assert paths["stable_condition"].stat().st_mtime_ns == condition_stat.st_mtime_ns
    assert paths["stable_progress"].read_bytes() == progress_bytes
    assert paths["stable_condition"].read_bytes() == condition_bytes


def test_update_creates_persistent_pair_backup_before_switch(
    tmp_path: Path,
) -> None:
    paths = _prepare_root(tmp_path)
    _run(tmp_path, CurrentYearTransport())
    old_progress = paths["stable_progress"].read_bytes()
    old_condition = paths["stable_condition"].read_bytes()

    audit = _run(
        tmp_path,
        CurrentYearTransport(planted_value=99),
        now=datetime(2026, 7, 18, 11, 0, tzinfo=timezone.utc),
    )

    assert audit["status"] == "updated"
    assert audit["corrected_records"] == 2
    assert audit["published"] is True
    backup_dir = Path(audit["publish"]["backup_dir"])
    assert backup_dir.is_dir()
    assert (
        backup_dir / paths["stable_progress"].name
    ).read_bytes() == old_progress
    assert (
        backup_dir / paths["stable_condition"].name
    ).read_bytes() == old_condition


def test_dry_run_writes_candidates_but_not_processed_or_status(
    tmp_path: Path,
) -> None:
    paths = _prepare_root(tmp_path)
    _run(tmp_path, CurrentYearTransport())
    old_progress = paths["stable_progress"].read_bytes()
    old_condition = paths["stable_condition"].read_bytes()
    old_status = paths["status"].read_bytes()

    audit = _run(
        tmp_path,
        CurrentYearTransport(planted_value=99),
        now=datetime(2026, 7, 18, 13, 0, tzinfo=timezone.utc),
        dry_run=True,
    )

    assert audit["run_mode"] == "dry_run"
    assert audit["published"] is False
    assert audit["recommended_to_publish"] is True
    assert paths["stable_progress"].read_bytes() == old_progress
    assert paths["stable_condition"].read_bytes() == old_condition
    assert paths["status"].read_bytes() == old_status
    assert (tmp_path / str(audit["candidate_progress_path"])).is_file()
    assert (tmp_path / str(audit["candidate_condition_path"])).is_file()
    assert "dry_runs" in str(audit["audit_json_path"])


def test_api_failure_and_candidate_failure_preserve_stable_pair(
    tmp_path: Path,
) -> None:
    paths = _prepare_root(tmp_path)
    _run(tmp_path, CurrentYearTransport())
    old_progress = paths["stable_progress"].read_bytes()
    old_condition = paths["stable_condition"].read_bytes()

    with pytest.raises(SoybeanWeeklyUpdateError):
        _run(
            tmp_path,
            CurrentYearTransport(fail=True),
            now=datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc),
        )
    assert paths["stable_progress"].read_bytes() == old_progress
    assert paths["stable_condition"].read_bytes() == old_condition
    assert json.loads(paths["status"].read_text(encoding="utf-8"))["status"] == "failed"

    with pytest.raises(SoybeanWeeklyUpdateError):
        _run(
            tmp_path,
            CurrentYearTransport(decline=True),
            now=datetime(2026, 7, 18, 15, 0, tzinfo=timezone.utc),
        )
    assert paths["stable_progress"].read_bytes() == old_progress
    assert paths["stable_condition"].read_bytes() == old_condition
    assert json.loads(paths["status"].read_text(encoding="utf-8"))["status"] == "failed"


def test_pair_switch_failure_restores_both_old_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agri_research_agent.pipelines.soybean_crop_weekly_update as update_module

    target_progress = tmp_path / "formal" / "progress.parquet"
    target_condition = tmp_path / "formal" / "condition.parquet"
    candidate_progress = tmp_path / "candidate" / "progress.parquet"
    candidate_condition = tmp_path / "candidate" / "condition.parquet"
    target_progress.parent.mkdir(parents=True)
    candidate_progress.parent.mkdir(parents=True)
    pd.DataFrame({"value": [1]}).to_parquet(target_progress, index=False)
    pd.DataFrame({"value": [2]}).to_parquet(target_condition, index=False)
    pd.DataFrame({"value": [10]}).to_parquet(candidate_progress, index=False)
    pd.DataFrame({"value": [20]}).to_parquet(candidate_condition, index=False)
    old_progress = target_progress.read_bytes()
    old_condition = target_condition.read_bytes()
    original_replace = update_module.os.replace
    failed = False

    def fail_second_stage(source: object, target: object) -> None:
        nonlocal failed
        if (
            not failed
            and Path(target) == target_condition
            and ".tmp.parquet" in str(source)
        ):
            failed = True
            raise OSError("fixture second replacement failure")
        original_replace(source, target)

    monkeypatch.setattr(update_module.os, "replace", fail_second_stage)
    with pytest.raises(OSError, match="second replacement"):
        replace_processed_pair(
            candidate_progress=candidate_progress,
            candidate_condition=candidate_condition,
            target_progress=target_progress,
            target_condition=target_condition,
            backup_dir=tmp_path / "backup",
        )

    assert target_progress.read_bytes() == old_progress
    assert target_condition.read_bytes() == old_condition


def test_out_of_season_no_data_and_missing_key_statuses(tmp_path: Path) -> None:
    outside = CurrentYearTransport()
    audit = run_soybean_crop_weekly_update(
        project_root=tmp_path,
        now=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
        transport=outside,
        env={},
    )
    paths = soybean_weekly_update_paths(tmp_path)
    assert audit["status"] == "out_of_season"
    assert outside.queries == []
    assert json.loads(paths["status"].read_text(encoding="utf-8"))[
        "status"
    ] == "out_of_season"

    with pytest.raises(SoybeanWeeklyUpdateError, match="NASS_API_KEY"):
        run_soybean_crop_weekly_update(
            project_root=tmp_path,
            now=datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc),
            env={},
        )
    assert json.loads(paths["status"].read_text(encoding="utf-8"))[
        "status"
    ] == "failed"

    empty_root = tmp_path / "empty"
    no_data = _run(empty_root, CurrentYearTransport(empty=True))
    empty_paths = soybean_weekly_update_paths(empty_root)
    assert no_data["status"] == "no_data"
    assert not empty_paths["stable_progress"].exists()
    assert not empty_paths["stable_condition"].exists()


def test_raw_manifest_and_all_outputs_are_secret_safe(tmp_path: Path) -> None:
    _prepare_root(tmp_path)
    audit = _run(tmp_path, CurrentYearTransport())
    manifest = json.loads(
        (tmp_path / str(audit["manifest_path"])).read_text(encoding="utf-8")
    )

    assert len(manifest["requests"]) == 4
    assert all(item["http_status"] == 200 for item in manifest["requests"])
    assert all(item["retry_count"] == 0 for item in manifest["requests"])
    assert all(item["response_sha256"] for item in manifest["requests"])
    assert manifest["git_head"]
    assert manifest["published"] is True
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert FAKE_SECRET.encode() not in path.read_bytes()


def test_2027_is_appended_and_page_comparisons_activate_without_code_change(
    tmp_path: Path,
) -> None:
    paths = _prepare_root(tmp_path)
    audit = _run(
        tmp_path,
        CurrentYearTransport(),
        now=datetime(2027, 7, 17, 12, 0, tzinfo=timezone.utc),
    )
    progress = pd.read_parquet(paths["stable_progress"])
    condition = pd.read_parquet(paths["stable_condition"])
    config = load_display_config(paths["display_config"])
    comparisons = build_dashboard_comparisons(progress, condition, config)

    assert audit["current_year"] == 2027
    assert progress["calendar_year"].max() == 2027
    assert condition["calendar_year"].max() == 2027
    assert {comparison.current_year for comparison in comparisons} == {2027}
    assert all(comparison.baseline_week is not None for comparison in comparisons)


def test_validation_rejects_prior_year_change_and_latest_week_retreat(
    tmp_path: Path,
) -> None:
    paths = _prepare_root(tmp_path)
    baseline_progress = pd.read_parquet(paths["legacy_progress"])
    baseline_condition = pd.read_parquet(paths["legacy_condition"])
    bad_prior = baseline_progress.copy()
    bad_prior.loc[0, "value_pct"] = float(bad_prior.loc[0, "value_pct"]) + 1

    with pytest.raises(ValueError, match="before 2026 changed"):
        validate_candidate_pair(
            baseline_progress=baseline_progress,
            baseline_condition=baseline_condition,
            candidate_progress=bad_prior,
            candidate_condition=baseline_condition,
            current_year=2026,
            retrieved_at_utc="2026-07-17T12:00:00+00:00",
            raw_snapshot="raw.json",
            duplicate_counts={"PROGRESS": 0, "CONDITION": 0},
            display_config=paths["display_config"],
        )

    progress_history = merge_current_year(
        baseline_progress,
        baseline_progress.iloc[0:0],
        2026,
    )
    with pytest.raises(ValueError, match="latest week retreated"):
        validate_candidate_pair(
            baseline_progress=baseline_progress,
            baseline_condition=baseline_condition,
            candidate_progress=progress_history,
            candidate_condition=baseline_condition,
            current_year=2026,
            retrieved_at_utc="2026-07-17T12:00:00+00:00",
            raw_snapshot="raw.json",
            duplicate_counts={"PROGRESS": 0, "CONDITION": 0},
            display_config=paths["display_config"],
        )
