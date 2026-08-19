from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq

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
