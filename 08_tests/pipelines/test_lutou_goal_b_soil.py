from __future__ import annotations

import inspect
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import agri_research_agent.pipelines.lutou_goal_b_soil as soil_pipeline
from agri_research_agent.data_sources.lutou.live import (
    LutouBatch,
    LutouConnectionProof,
    LutouPlanProof,
)
from agri_research_agent.data_sources.lutou.soil_moisture_live import (
    extract_soil_moisture_live,
    load_soil_moisture_series,
)
from agri_research_agent.pipelines.lutou_goal_b_soil import (
    LutouGoalBSoilError,
    _merge,
    _merge_reference,
    _validate_canonical,
    load_soil_current,
    run_goal_b_soil,
)
from agri_research_agent.shared.file_identity import identify_file
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


DAY = date(2026, 8, 9)


class FakeSoilClient:
    proof = LutouConnectionProof(
        "8.0.37", "MySQL Community Server - GPL", "SYSTEM", "SYSTEM", False, True, 3, ()
    )

    def __init__(
        self,
        value: Decimal = Decimal("0.236"),
        *,
        include_outlier: bool = False,
        include_non_numeric: bool = False,
    ) -> None:
        self.value = value
        self.include_outlier = include_outlier
        self.include_non_numeric = include_non_numeric
        self.non_numeric_emitted = False

    def inspect_query(self, query):  # type: ignore[no-untyped-def]
        return tuple(
            {"COLUMN_NAME": item}
            for item in (query.date_column, *query.value_columns)
        )

    def plan_stream(self, query, start, end):  # type: ignore[no-untyped-def]
        assert start <= DAY <= end
        row = {query.date_column: DAY}
        row.update({item: self.value for item in query.value_columns})
        rows = [row]
        if self.include_non_numeric and not self.non_numeric_emitted:
            sentinel = {query.date_column: date(2026, 8, 8)}
            sentinel.update({item: None for item in query.value_columns})
            sentinel[query.value_columns[0]] = "#N/A"
            rows.append(sentinel)
            self.non_numeric_emitted = True
        if self.include_outlier:
            outlier = {query.date_column: date(2026, 8, 8)}
            outlier.update({item: Decimal("1.2") for item in query.value_columns})
            rows.append(outlier)
        plan = LutouPlanProof(query.sha256, len(rows), query.max_plan_rows)
        return plan, iter(
            (
                LutouBatch(
                    query,
                    plan,
                    tuple(rows),
                    datetime(2026, 8, 19, tzinfo=timezone.utc),
                ),
            )
        )


def _runtime(tmp_path: Path) -> RuntimeContext:
    (tmp_path / ".market-data-runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_id": "goal-b-soil-test",
                "classification": "isolated-dev",
                "module_id": "international-spread",
                "created_at": "2026-08-19T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return RuntimeContext(RuntimeMode.ISOLATED_DEV, "international-spread", tmp_path)


def _catalog() -> Path:
    return Path("02_configs/public_research_data_catalog.candidate.json")


def test_live_soil_scope_and_explicit_fraction_to_percent_conversion() -> None:
    series = load_soil_moisture_series(_catalog())
    result = extract_soil_moisture_live(
        FakeSoilClient(), series, start=DAY, end=DAY
    )

    assert len(series) == len(result.records) == 94
    assert len(result.queries) == 11
    assert {item.source_value for item in result.records} == {Decimal("0.236")}
    assert {item.value_percent for item in result.records} == {Decimal("23.600")}


def test_soil_full_and_incremental_are_source_preserving_and_idempotent(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    first = run_goal_b_soil(
        FakeSoilClient(),
        runtime=runtime,
        run_id="soil-full",
        end_date=DAY,
        full_load=True,
        catalog_path=_catalog(),
    )
    pointer = runtime.runtime_root / "public-market-data" / "lutou-soil-moisture" / "current.json"
    before = identify_file(pointer)
    second = run_goal_b_soil(
        FakeSoilClient(),
        runtime=runtime,
        run_id="soil-incremental",
        end_date=DAY,
        full_load=False,
        catalog_path=_catalog(),
    )
    standard = pq.read_table(first.candidate_directory / "standard.parquet")
    current = load_soil_current(pointer.parent)

    assert first.promoted is True and second.promoted is False
    assert identify_file(pointer) == before
    assert current is not None and current.observations.num_rows == 94
    assert set(standard["source_value"].to_pylist()) == {Decimal("0.236")}
    assert set(standard["value_percent"].to_pylist()) == {Decimal("23.6")}
    assert set(current.observations["unit"].to_pylist()) == {"%"}
    assert set(current.observations["soil_depth"].to_pylist()) == {"0-100cm"}
    assert set(current.observations["conversion_factor"].to_pylist()) == {
        Decimal("100")
    }


def test_soil_outlier_is_retained_in_standard_but_not_canonical(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    result = run_goal_b_soil(
        FakeSoilClient(include_outlier=True),
        runtime=runtime,
        run_id="soil-outlier",
        end_date=DAY,
        full_load=True,
        catalog_path=_catalog(),
    )
    standard = pq.read_table(result.candidate_directory / "standard.parquet")

    assert standard.num_rows == 188
    assert set(standard["quality_status"].to_pylist()) == {"PASS", "OUT_OF_RANGE"}
    assert result.candidate_manifest["quality"]["out_of_range_row_count"] == 94
    assert result.canonical_manifest["row_count"] == 94


def test_soil_non_numeric_sentinel_is_retained_as_qc_evidence(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    result = run_goal_b_soil(
        FakeSoilClient(include_non_numeric=True),
        runtime=runtime,
        run_id="soil-non-numeric",
        end_date=DAY,
        full_load=True,
        catalog_path=_catalog(),
    )
    standard = pq.read_table(result.candidate_directory / "standard.parquet")
    invalid = [
        row for row in standard.to_pylist() if row["quality_status"] == "NON_NUMERIC"
    ]

    assert len(invalid) == 1
    assert invalid[0]["raw_value_text"] == "#N/A"
    assert invalid[0]["source_value"] is None
    assert invalid[0]["value_percent"] is None
    assert invalid[0]["is_usable"] is False
    assert result.candidate_manifest["quality"]["non_numeric_row_count"] == 1
    assert result.canonical_manifest["row_count"] == 94


def _replace_column(table: pa.Table, name: str, values: pa.Array) -> pa.Table:
    return table.set_column(
        table.schema.get_field_index(name), table.schema.field(name), values
    )


def _soil_current_fixture(tmp_path: Path) -> tuple[pa.Table, tuple[object, ...]]:
    result = run_goal_b_soil(
        FakeSoilClient(),
        runtime=_runtime(tmp_path),
        run_id="soil-merge-fixture",
        end_date=DAY,
        full_load=True,
        catalog_path=_catalog(),
    )
    current = pq.read_table(result.current_directory / "observations.parquet")
    return current, load_soil_moisture_series(_catalog())


def test_arrow_native_merge_matches_reference_for_overlap_revision_null_and_dates(
    tmp_path: Path,
) -> None:
    previous, series = _soil_current_fixture(tmp_path)
    historical = _replace_column(
        previous,
        "business_date",
        pa.array([date(2026, 6, 1)] * previous.num_rows, type=pa.date32()),
    )
    unchanged = previous.slice(0, 30)
    revised = previous.slice(30, 30)
    revised = _replace_column(
        revised,
        "value_percent",
        pa.array(
            [Decimal("42.0")] * revised.num_rows,
            type=revised.schema.field("value_percent").type,
        ),
    )
    revised = _replace_column(
        revised,
        "source_group_sha256",
        pa.array(["f" * 64] * revised.num_rows),
    )
    nullable = previous.slice(60, 10)
    nullable = _replace_column(
        nullable,
        "source_value",
        pa.array(
            [None] * nullable.num_rows,
            type=nullable.schema.field("source_value").type,
        ),
    )
    window = pa.concat_tables([unchanged, revised, nullable])
    prior = pa.concat_tables([historical, previous])

    expected = _merge_reference(prior, window, date(2026, 7, 9))
    actual = _merge(prior, window, date(2026, 7, 9))

    assert actual.schema == expected.schema
    assert actual.column_names == expected.column_names
    assert actual.num_rows == expected.num_rows
    assert actual.equals(expected)
    assert actual.equals(
        actual.sort_by(
            [("series_id", "ascending"), ("business_date", "ascending")]
        )
    )
    _validate_canonical(actual, series)


def test_arrow_native_merge_matches_reference_for_no_and_all_overlap(
    tmp_path: Path,
) -> None:
    previous, _ = _soil_current_fixture(tmp_path)
    historical = _replace_column(
        previous,
        "business_date",
        pa.array([date(2026, 6, 1)] * previous.num_rows, type=pa.date32()),
    )

    no_overlap_expected = _merge_reference(historical, previous, date(2026, 7, 9))
    no_overlap_actual = _merge(historical, previous, date(2026, 7, 9))
    all_overlap_expected = _merge_reference(previous, previous, date(2026, 7, 9))
    all_overlap_actual = _merge(previous, previous, date(2026, 7, 9))

    assert no_overlap_actual.equals(no_overlap_expected)
    assert all_overlap_actual.equals(all_overlap_expected)


@pytest.mark.parametrize("duplicate_side", ["previous", "window"])
def test_arrow_native_merge_rejects_duplicate_stable_keys(
    tmp_path: Path, duplicate_side: str
) -> None:
    previous, _ = _soil_current_fixture(tmp_path)
    window = previous.slice(0, 10)
    if duplicate_side == "previous":
        previous = pa.concat_tables([previous, previous.slice(0, 1)])
    else:
        window = pa.concat_tables([window, window.slice(0, 1)])

    with pytest.raises(LutouGoalBSoilError, match="stable-key"):
        _merge(previous, window, date(2026, 7, 9))


def test_soil_merge_hot_path_forbids_whole_history_python_rows() -> None:
    source = inspect.getsource(_merge)
    assert ".to_pylist(" not in source
    assert "from_pylist(" not in source


def test_soil_performance_telemetry_is_observation_only(tmp_path: Path) -> None:
    result = run_goal_b_soil(
        FakeSoilClient(),
        runtime=_runtime(tmp_path),
        run_id="soil-telemetry",
        end_date=DAY,
        full_load=True,
        catalog_path=_catalog(),
    )

    expected_stages = {
        "load_current",
        "extract",
        "standardize",
        "candidate_qc",
        "candidate_build",
        "canonicalize",
        "merge",
        "canonical_qc",
        "canonical_build",
        "promote",
        "total",
    }
    assert expected_stages <= set(result.performance["stages"])
    assert all(
        details["duration_seconds"] >= 0
        for details in result.performance["stages"].values()
    )
    assert result.performance["io"]["current_read"]["rows"] == 0
    assert result.performance["io"]["candidate_write"]["rows"] == 94
    assert result.performance["io"]["canonical_write"]["rows"] == 94
    for manifest in (
        result.candidate_manifest,
        result.canonical_manifest,
        result.current_manifest,
    ):
        assert "performance" not in manifest
        assert "telemetry" not in manifest


def test_telemetry_values_do_not_change_business_artifact_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ticking(step: float):  # type: ignore[no-untyped-def]
        value = 0.0

        def tick() -> float:
            nonlocal value
            value += step
            return value

        return tick

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    monkeypatch.setattr(soil_pipeline, "perf_counter", ticking(1.0))
    first = run_goal_b_soil(
        FakeSoilClient(),
        runtime=_runtime(first_root),
        run_id="soil-identity",
        end_date=DAY,
        full_load=True,
        catalog_path=_catalog(),
    )
    monkeypatch.setattr(soil_pipeline, "perf_counter", ticking(10.0))
    second = run_goal_b_soil(
        FakeSoilClient(),
        runtime=_runtime(second_root),
        run_id="soil-identity",
        end_date=DAY,
        full_load=True,
        catalog_path=_catalog(),
    )

    assert first.performance != second.performance
    assert first.candidate_manifest == second.candidate_manifest
    assert first.canonical_manifest == second.canonical_manifest
    assert first.current_manifest == second.current_manifest
    for filename in ("standard.parquet", "manifest.json"):
        assert identify_file(first.candidate_directory / filename) == identify_file(
            second.candidate_directory / filename
        )
    for filename in ("observations.parquet", "manifest.json"):
        assert identify_file(first.canonical_directory / filename) == identify_file(
            second.canonical_directory / filename
        )
        assert identify_file(first.current_directory / filename) == identify_file(
            second.current_directory / filename
        )
