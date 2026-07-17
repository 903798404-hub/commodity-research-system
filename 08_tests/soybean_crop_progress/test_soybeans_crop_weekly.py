from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.data_sources.usda_client import (
    build_soybean_crop_weekly_query_grid,
)
from agri_research_agent.pipelines.soybean_crop_progress import (
    CONDITION_UNIT_TO_METRIC,
    CROP_STABLE_KEY,
    PROGRESS_UNIT_TO_METRIC,
    CropMetricMappingError,
    analyze_crop_weekly_quality,
    derive_good_excellent,
    identify_crop_metric_mappings,
    normalize_crop_weekly_rows,
    run_soybean_crop_weekly_pipeline,
    run_soybean_planted_pipeline,
    soybean_crop_weekly_paths,
)


FAKE_SECRET = "crop-weekly-fixture-secret"


def _row(
    year: int,
    level: str,
    family: str,
    unit_desc: str,
    *,
    value: object,
    week_ending: str = "2026-05-31",
    short_desc: str | None = None,
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
        "short_desc": short_desc
        or f"SOYBEANS - {family}, MEASURED IN {unit_desc}",
        "reference_period_desc": "WEEK #22",
        "agg_level_desc": level,
        "location_desc": "US TOTAL" if national else "IOWA",
        "state_alpha": "" if national else "IA",
        "state_fips_code": "" if national else "19",
        "state_name": "" if national else "IOWA",
        "year": year,
        "week_ending": week_ending.replace("2026", str(year)),
        "Value": value,
        "load_time": f"{year}-06-01 16:00:00.000",
    }


def _family_rows(year: int, level: str, family: str) -> list[dict[str, object]]:
    if family == "PROGRESS":
        return [
            _row(
                year,
                level,
                family,
                unit,
                value=index * 10,
            )
            for index, unit in enumerate(PROGRESS_UNIT_TO_METRIC, start=1)
        ]
    condition_values = {
        "PCT VERY POOR": 5,
        "PCT POOR": 10,
        "PCT FAIR": 25,
        "PCT GOOD": 40,
        "PCT EXCELLENT": 20,
    }
    return [
        _row(year, level, family, unit, value=value)
        for unit, value in condition_values.items()
    ]


def _request_item(
    year: int, level: str, family: str, rows: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "query_params": {
            "year": str(year),
            "agg_level_desc": level,
            "statisticcat_desc": family,
        },
        "response": {"data": rows},
        "record_count": len(rows),
    }


class FakeCropTransport:
    def __init__(self, *, unexpected: bool = False) -> None:
        self.queries: list[dict[str, list[str]]] = []
        self.unexpected = unexpected

    def __call__(self, url: str, timeout: int) -> tuple[int, bytes]:
        assert timeout > 0
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.queries.append(query)
        year = int(query["year"][0])
        level = query["agg_level_desc"][0]
        family = query["statisticcat_desc"][0]
        rows = _family_rows(year, level, family)
        if self.unexpected and family == "PROGRESS":
            rows.append(
                _row(
                    year,
                    level,
                    family,
                    "PCT UNKNOWN",
                    value=1,
                )
            )
        return 200, json.dumps({"data": rows}).encode("utf-8")


def _one_year_raw() -> list[dict[str, object]]:
    return [
        _request_item(2026, "NATIONAL", family, _family_rows(2026, "NATIONAL", family))
        for family in ("PROGRESS", "CONDITION")
    ]


def test_unified_query_grid_has_24_secret_free_requests() -> None:
    queries = build_soybean_crop_weekly_query_grid()

    assert len(queries) == 24
    assert {
        (query["year"], query["statisticcat_desc"], query["agg_level_desc"])
        for query in queries
    } == {
        (str(year), family, level)
        for year in range(2021, 2027)
        for family in ("PROGRESS", "CONDITION")
        for level in ("NATIONAL", "STATE")
    }
    assert all(query["unit_desc__LIKE"] == "PCT" for query in queries)
    assert all("key" not in query for query in queries)


def test_all_official_metrics_map_by_three_fields_without_family_confusion() -> None:
    audit = identify_crop_metric_mappings(_one_year_raw())
    mapping = {
        (item["metric_family"], item["unit_desc"]): item["metric"]
        for item in audit["final_metric_mapping"]
    }

    assert len(mapping) == 14
    assert {
        unit: mapping[("PROGRESS", unit)] for unit in PROGRESS_UNIT_TO_METRIC
    } == PROGRESS_UNIT_TO_METRIC
    assert {
        unit: mapping[("CONDITION", unit)] for unit in CONDITION_UNIT_TO_METRIC
    } == CONDITION_UNIT_TO_METRIC
    assert audit["mapping_complete"] is True


def test_good_excellent_requires_both_components_and_is_traceable() -> None:
    raw = _one_year_raw()
    mapping = identify_crop_metric_mappings(raw)
    _, direct, _ = normalize_crop_weekly_rows(
        raw,
        mapping_audit=mapping,
        retrieved_at_utc="2026-07-16T00:00:00+00:00",
        raw_snapshot="raw.json",
    )

    condition, derivation = derive_good_excellent(direct)
    derived = condition.loc[condition["metric"].eq("GOOD_EXCELLENT")].iloc[0]
    assert derived["value_pct"] == 60
    assert derived["is_derived"]
    assert derived["derivation_formula"] == "GOOD + EXCELLENT"
    assert derived["derivation_good_value_pct"] == 40
    assert derived["derivation_excellent_value_pct"] == 20
    assert derivation["derived_count"] == 1

    missing_excellent = direct.loc[~direct["metric"].eq("EXCELLENT")]
    without_derived, missing_audit = derive_good_excellent(missing_excellent)
    assert not without_derived["metric"].eq("GOOD_EXCELLENT").any()
    assert missing_audit["skipped_missing_component_count"] == 1
    assert missing_audit["missing_components_treated_as_zero"] is False


def test_family_specific_quality_checks_find_sum_decline_duplicate_and_conflict() -> None:
    progress_rows = _family_rows(2026, "NATIONAL", "PROGRESS")
    planted = progress_rows[0]
    progress_rows.extend(
        [
            dict(planted),
            {**planted, "Value": 15},
            {
                **planted,
                "week_ending": "2026-06-07",
                "reference_period_desc": "WEEK #23",
                "Value": 5,
            },
        ]
    )
    condition_rows = _family_rows(2026, "NATIONAL", "CONDITION")
    for row in condition_rows:
        if row["unit_desc"] == "PCT EXCELLENT":
            row["Value"] = 0
    raw = [
        _request_item(2026, "NATIONAL", "PROGRESS", progress_rows),
        _request_item(2026, "NATIONAL", "CONDITION", condition_rows),
    ]
    mapping = identify_crop_metric_mappings(raw)
    progress, direct, duplicates = normalize_crop_weekly_rows(
        raw,
        mapping_audit=mapping,
        retrieved_at_utc="2026-07-16T00:00:00+00:00",
        raw_snapshot="raw.json",
    )
    condition, _ = derive_good_excellent(direct)
    quality = analyze_crop_weekly_quality(
        progress, condition, exact_duplicate_counts=duplicates
    )

    assert quality["complete_duplicate_count"] == 1
    assert quality["duplicate_stable_key_count"] == 1
    assert quality["stable_key_conflict_count"] == 1
    assert quality["progress_decline_count"] == 1
    assert quality["condition_sum_anomaly_count"] == 1
    assert quality["condition_monotonicity_check_applied"] is False
    assert quality["progress_monotonicity_check_applied"] is True
    assert quality["range_anomaly_count"] == 0
    combined = pd.concat([progress, condition], ignore_index=True)
    assert not combined.duplicated(CROP_STABLE_KEY).all()


def test_unified_pipeline_preserves_us_total_and_matches_first_step(
    tmp_path: Path,
) -> None:
    transport = FakeCropTransport()
    run_soybean_planted_pipeline(
        project_root=tmp_path,
        api_key=FAKE_SECRET,
        transport=transport,
        retries=0,
        now=datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc),
        sleep=lambda _: None,
    )
    audit = run_soybean_crop_weekly_pipeline(
        project_root=tmp_path,
        api_key=FAKE_SECRET,
        transport=transport,
        retries=0,
        now=datetime(2026, 7, 16, 11, 0, tzinfo=timezone.utc),
        sleep=lambda _: None,
    )
    paths = soybean_crop_weekly_paths(tmp_path)
    progress = pd.read_parquet(paths["progress"])
    condition = pd.read_parquet(paths["condition"])

    assert len(transport.queries) == 36
    assert len(progress) == 108
    assert len(condition) == 72
    assert condition["metric"].eq("GOOD_EXCELLENT").sum() == 12
    assert audit["planted_compatibility"]["business_fields_exact_match"] is True
    assert audit["state_data_used_for_us_total"] is False
    assert audit["incomplete_2026"]["future_weeks_missing_is_error"] is False
    assert audit["ready_for_next_stage"] is True
    us = pd.concat([progress, condition]).loc[
        lambda data: data["geography_level"].eq("US")
    ]
    assert us["region_name"].eq("US TOTAL").all()
    assert us["source_location_desc"].eq("US TOTAL").all()
    assert set(progress["metric"]) == set(PROGRESS_UNIT_TO_METRIC.values())
    assert set(condition["metric"]) == {
        *CONDITION_UNIT_TO_METRIC.values(),
        "GOOD_EXCELLENT",
    }
    assert json.loads(paths["audit_json"].read_text(encoding="utf-8"))[
        "processing_success"
    ]
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert FAKE_SECRET.encode() not in path.read_bytes()


def test_mapping_failure_preserves_existing_unified_files(tmp_path: Path) -> None:
    paths = soybean_crop_weekly_paths(tmp_path)
    paths["progress"].parent.mkdir(parents=True, exist_ok=True)
    paths["progress"].write_bytes(b"existing-progress")
    paths["condition"].write_bytes(b"existing-condition")

    with pytest.raises(CropMetricMappingError):
        run_soybean_crop_weekly_pipeline(
            project_root=tmp_path,
            api_key=FAKE_SECRET,
            transport=FakeCropTransport(unexpected=True),
            retries=0,
            now=datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
            sleep=lambda _: None,
        )

    assert paths["progress"].read_bytes() == b"existing-progress"
    assert paths["condition"].read_bytes() == b"existing-condition"
    failure = json.loads(paths["audit_json"].read_text(encoding="utf-8"))
    assert failure["progress_file_preserved"] is True
    assert failure["condition_file_preserved"] is True
