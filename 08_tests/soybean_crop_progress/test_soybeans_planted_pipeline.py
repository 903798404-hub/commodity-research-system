from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.data_sources.usda_client import (
    build_soybean_progress_query_grid,
)
from agri_research_agent.pipelines.soybean_crop_progress import (
    MISSING_API_KEY_MESSAGE,
    MissingNassApiKeyError,
    PlantedCandidateError,
    analyze_quality,
    identify_planted_candidate,
    normalize_planted_rows,
    run_soybean_planted_pipeline,
    soybean_progress_paths,
)


FAKE_SECRET = "fixture-secret-must-not-leak"
PLANTED_SHORT_DESC = "SOYBEANS - PROGRESS, MEASURED IN PCT PLANTED"


def _row(
    year: int,
    level: str,
    *,
    unit: str = "PCT PLANTED",
    short_desc: str | None = None,
    week_ending: str = "2026-04-12",
    reference_period_desc: str = "WEEK #15",
    value: object = "4",
    state_alpha: str = "IA",
    state_name: str = "IOWA",
) -> dict[str, object]:
    national = level == "NATIONAL"
    return {
        "source_desc": "SURVEY",
        "sector_desc": "CROPS",
        "group_desc": "FIELD CROPS",
        "commodity_desc": "SOYBEANS",
        "freq_desc": "WEEKLY",
        "statisticcat_desc": "PROGRESS",
        "unit_desc": unit,
        "short_desc": short_desc
        or f"SOYBEANS - PROGRESS, MEASURED IN {unit}",
        "reference_period_desc": reference_period_desc,
        "agg_level_desc": level,
        "location_desc": "US TOTAL" if national else state_name,
        "state_alpha": "" if national else state_alpha,
        "state_fips_code": "" if national else "19",
        "state_name": "" if national else state_name,
        "year": year,
        "week_ending": week_ending.replace("2026", str(year)),
        "Value": value,
        "load_time": f"{year}-04-13 16:00:00.000",
    }


def _request_item(
    year: int, level: str, rows: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "query_params": {
            "year": str(year),
            "agg_level_desc": level,
        },
        "response": {"data": rows},
        "record_count": len(rows),
    }


class FakeTransport:
    def __init__(self, *, ambiguous: bool = False) -> None:
        self.queries: list[dict[str, list[str]]] = []
        self.ambiguous = ambiguous

    def __call__(self, url: str, timeout: int) -> tuple[int, bytes]:
        assert timeout > 0
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.queries.append(query)
        year = int(query["year"][0])
        level = query["agg_level_desc"][0]
        planted = _row(year, level, value="0" if year == 2021 else str(year - 2000))
        emerged = _row(year, level, unit="PCT EMERGED", value="2")
        rows = [planted, emerged]
        if self.ambiguous:
            rows.append(
                _row(
                    year,
                    level,
                    short_desc=(
                        "SOYBEANS - PLANTED PROGRESS, MEASURED IN PCT PLANTED"
                    ),
                )
            )
        return 200, json.dumps({"data": rows}).encode("utf-8")


def test_missing_api_key_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NASS_API_KEY", raising=False)

    with pytest.raises(MissingNassApiKeyError, match="缺少 NASS_API_KEY") as exc:
        run_soybean_planted_pipeline(project_root=tmp_path)

    assert str(exc.value) == MISSING_API_KEY_MESSAGE
    assert list(tmp_path.rglob("*")) == []


def test_query_grid_is_split_and_uses_required_filters() -> None:
    queries = build_soybean_progress_query_grid()

    assert len(queries) == 12
    assert {
        (query["year"], query["agg_level_desc"]) for query in queries
    } == {
        (str(year), level)
        for year in range(2021, 2027)
        for level in ("NATIONAL", "STATE")
    }
    for query in queries:
        assert query["commodity_desc"] == "SOYBEANS"
        assert query["freq_desc"] == "WEEKLY"
        assert query["statisticcat_desc"] == "PROGRESS"
        assert query["unit_desc__LIKE"] == "PCT"
        assert "key" not in query


def test_candidate_selection_records_all_exclusions() -> None:
    raw = [
        _request_item(
            2026,
            "NATIONAL",
            [
                _row(2026, "NATIONAL"),
                _row(2026, "NATIONAL", unit="PCT EMERGED"),
                _row(2026, "NATIONAL", unit="PCT BLOOMING"),
            ],
        )
    ]

    audit = identify_planted_candidate(raw)

    assert audit["selected_short_desc"] == PLANTED_SHORT_DESC
    assert {item["unit_desc"] for item in audit["all_candidates"]} == {
        "PCT PLANTED",
        "PCT EMERGED",
        "PCT BLOOMING",
    }
    assert len(audit["excluded_candidates"]) == 2


def test_zero_special_range_duplicate_conflict_and_decline_are_preserved() -> None:
    rows = [
        _row(2026, "STATE", week_ending="2026-04-12", value="0"),
        _row(2026, "STATE", week_ending="2026-04-12", value="0"),
        _row(2026, "STATE", week_ending="2026-04-12", value="5"),
        _row(
            2026,
            "STATE",
            week_ending="2026-04-19",
            reference_period_desc="WEEK #16",
            value="(D)",
        ),
        _row(
            2026,
            "STATE",
            week_ending="2026-04-26",
            reference_period_desc="WEEK #17",
            value="101",
        ),
        _row(
            2026,
            "STATE",
            week_ending="2026-05-03",
            reference_period_desc="WEEK #18",
            value="90",
        ),
    ]
    frame, exact_duplicates = normalize_planted_rows(
        [_request_item(2026, "STATE", rows)],
        selected_short_desc=PLANTED_SHORT_DESC,
        retrieved_at_utc="2026-07-16T00:00:00+00:00",
        raw_snapshot="raw.json",
    )
    quality = analyze_quality(frame, exact_duplicate_count=exact_duplicates)

    assert exact_duplicates == 1
    assert frame.loc[frame["raw_value"].eq("0"), "value_pct"].iloc[0] == 0
    special = frame.loc[frame["raw_value"].eq("(D)")].iloc[0]
    assert pd.isna(special["value_pct"])
    assert special["suppression_code"] == "(D)"
    assert quality["range_anomaly_count"] == 1
    assert quality["duplicate_week_count"] == 1
    assert quality["stable_key_conflict_count"] == 1
    assert quality["planting_progress_decline_count"] == 1
    assert quality["interpolation_or_fill_applied"] is False


def test_pipeline_publishes_readable_secret_safe_outputs(tmp_path: Path) -> None:
    transport = FakeTransport()
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)

    audit = run_soybean_planted_pipeline(
        project_root=tmp_path,
        api_key=FAKE_SECRET,
        transport=transport,
        retries=0,
        now=now,
        sleep=lambda _: None,
    )

    assert len(transport.queries) == 12
    assert all(query["key"] == [FAKE_SECRET] for query in transport.queries)
    paths = soybean_progress_paths(tmp_path)
    data = pd.read_parquet(paths["processed"])
    reloaded_audit = json.loads(paths["audit_json"].read_text(encoding="utf-8"))
    manifests = list(paths["raw_dir"].glob("*_manifest.json"))
    raw_files = [
        path
        for path in paths["raw_dir"].glob("*.json")
        if not path.name.endswith("_manifest.json")
    ]
    assert len(manifests) == len(raw_files) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    raw = json.loads(raw_files[0].read_text(encoding="utf-8"))

    assert len(data) == 12
    assert len(data.loc[data["geography_level"].eq("US")]) == 6
    assert data.loc[data["geography_level"].eq("US"), "region_name"].eq(
        "US TOTAL"
    ).all()
    assert data.loc[data["geography_level"].eq("US"), "region_code"].eq(
        "US"
    ).all()
    assert audit["state_data_used_for_us_total"] is False
    assert audit["incomplete_2026"]["is_incomplete"] is True
    assert audit["incomplete_2026"]["future_weeks_missing_is_error"] is False
    assert audit["ready_for_next_stage"] is True
    assert reloaded_audit["processed_total_records"] == 12
    assert manifest["overall_success"] is True
    assert len(manifest["requests"]) == 12
    assert sum(
        len(item["response"]["data"]) for item in raw["requests"]
    ) == 24
    assert paths["audit_markdown"].read_text(encoding="utf-8").count(
        "### 202"
    ) == 6
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert FAKE_SECRET.encode("utf-8") not in path.read_bytes()


def test_ambiguous_candidate_preserves_existing_processed(tmp_path: Path) -> None:
    paths = soybean_progress_paths(tmp_path)
    paths["processed"].parent.mkdir(parents=True, exist_ok=True)
    paths["processed"].write_bytes(b"existing-processed")

    with pytest.raises(PlantedCandidateError):
        run_soybean_planted_pipeline(
            project_root=tmp_path,
            api_key=FAKE_SECRET,
            transport=FakeTransport(ambiguous=True),
            retries=0,
            now=datetime(2026, 7, 16, 13, 0, tzinfo=timezone.utc),
            sleep=lambda _: None,
        )

    assert paths["processed"].read_bytes() == b"existing-processed"
    failure = json.loads(paths["audit_json"].read_text(encoding="utf-8"))
    assert failure["processed_file_preserved"] is True
    assert len(failure["candidates"]["all_candidates"]) == 3


def test_parquet_failure_preserves_existing_processed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = soybean_progress_paths(tmp_path)
    paths["processed"].parent.mkdir(parents=True, exist_ok=True)
    paths["processed"].write_bytes(b"existing-processed")

    def fail_parquet(*_: object, **__: object) -> None:
        raise RuntimeError("simulated parquet failure")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", fail_parquet)
    with pytest.raises(RuntimeError, match="simulated parquet failure"):
        run_soybean_planted_pipeline(
            project_root=tmp_path,
            api_key=FAKE_SECRET,
            transport=FakeTransport(),
            retries=0,
            now=datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc),
            sleep=lambda _: None,
        )

    assert paths["processed"].read_bytes() == b"existing-processed"
    assert not list(tmp_path.rglob("*.tmp*"))
