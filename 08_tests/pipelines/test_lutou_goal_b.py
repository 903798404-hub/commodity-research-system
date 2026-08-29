from __future__ import annotations

import inspect
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import agri_research_agent.pipelines.lutou_goal_b as three_oil_pipeline
import agri_research_agent.shared.arrow_window_upsert as arrow_upsert
from agri_research_agent.data_sources.lutou.live import (
    LutouBatch,
    LutouConnectionProof,
    LutouPlanProof,
)
from agri_research_agent.pipelines.lutou_goal_b import (
    CANONICAL_SCHEMA,
    STABLE_KEY,
    LutouGoalBError,
    _merge_current,
    _merge_current_reference,
    _upgrade_legacy_current,
    load_current,
    run_goal_b,
)
from agri_research_agent.shared.file_identity import identify_file
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


DAY = date(2026, 8, 18)


class FakeLiveClient:
    proof = LutouConnectionProof(
        engine_version="8.0.37",
        engine_comment="MySQL Community Server - GPL",
        global_timezone="SYSTEM",
        session_timezone="SYSTEM",
        global_read_only=False,
        transaction_read_only=True,
        grant_count=2,
        write_privileges=(),
    )

    def __init__(
        self,
        *,
        price_shift: Decimal = Decimal("0"),
        duplicate: bool = False,
        conflict: bool = False,
    ) -> None:
        self.price_shift = price_shift
        self.duplicate = duplicate
        self.conflict = conflict

    def inspect_query(self, query):  # type: ignore[no-untyped-def]
        return tuple(
            {"COLUMN_NAME": item}
            for item in (query.date_column, *query.value_columns)
        )

    def plan_stream(self, query, start, end):  # type: ignore[no-untyped-def]
        assert start <= DAY <= end
        base = {query.date_column: DAY}
        base.update(
            {
                item: Decimal("1000") + self.price_shift
                for item in query.value_columns
            }
        )
        rows = [base]
        if self.duplicate or self.conflict:
            second = dict(base)
            if self.conflict:
                second[query.value_columns[0]] += Decimal("1")
            rows.append(second)
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


@pytest.fixture
def runtime(tmp_path: Path) -> RuntimeContext:
    marker = {
        "schema_version": 1,
        "runtime_id": "isolated-dev-international-spread-goal-b-test",
        "classification": "isolated-dev",
        "module_id": "international-spread",
        "created_at": "2026-08-19T00:00:00Z",
    }
    (tmp_path / ".market-data-runtime.json").write_text(
        json.dumps(marker), encoding="utf-8"
    )
    return RuntimeContext(RuntimeMode.ISOLATED_DEV, "international-spread", tmp_path)


def apply_run(
    runtime: RuntimeContext,
    run_id: str,
    *,
    full: bool,
    client: FakeLiveClient | None = None,
    failure_hook: str | None = None,
):
    return run_goal_b(
        client or FakeLiveClient(),
        runtime=runtime,
        run_id=run_id,
        end_date=DAY,
        full_load=full,
        failure_hook=failure_hook,
    )


def test_full_then_incremental_is_idempotent_and_source_preserving(
    runtime: RuntimeContext,
) -> None:
    first = apply_run(runtime, "full", full=True, client=FakeLiveClient(duplicate=True))
    pointer = runtime.runtime_root / "public-market-data" / "lutou-three-oil" / "current.json"
    before = identify_file(pointer)
    second = apply_run(runtime, "incremental", full=False, client=FakeLiveClient(duplicate=True))

    assert first.promoted is True and second.promoted is False
    assert identify_file(pointer) == before
    standard = pq.read_table(first.candidate_directory / "standard.parquet")
    current = load_current(pointer.parent)
    assert current is not None and current.release_id == "full"
    assert standard.num_rows == 40
    assert current.observations.num_rows == 20
    assert set(standard["acquisition_channel"].to_pylist()) == {"direct_database"}
    assert set(standard["provider"].to_pylist()) == {"Reuters", "Oil World"}
    assert set(current.observations["provider"].to_pylist()) == {"Reuters", "Oil World"}
    assert set(current.observations["source_duplicate_count"].to_pylist()) == {1}
    assert len(set(zip(current.observations["series_id"].to_pylist(), current.observations["business_date"].to_pylist()))) == 20


def test_incremental_revision_replaces_values_without_duplicate(
    runtime: RuntimeContext,
) -> None:
    first = apply_run(runtime, "full", full=True)
    revised = apply_run(
        runtime,
        "revision",
        full=False,
        client=FakeLiveClient(price_shift=Decimal("1")),
    )
    assert first.promoted is True and revised.promoted is True
    assert revised.current_manifest["release_id"] == "revision"
    current = load_current(
        runtime.runtime_root / "public-market-data" / "lutou-three-oil"
    )
    assert current is not None and current.observations.num_rows == 20
    assert set(current.observations["value"].to_pylist()) != {Decimal("1000")}


def test_source_precision_beyond_ten_places_is_preserved(
    runtime: RuntimeContext,
) -> None:
    source_value = Decimal("1000.1234567890123")
    result = apply_run(
        runtime,
        "high-precision",
        full=True,
        client=FakeLiveClient(price_shift=source_value - Decimal("1000")),
    )
    standard = pq.read_table(result.candidate_directory / "standard.parquet")

    assert set(standard["raw_price"].to_pylist()) == {source_value}


def test_current_loader_projects_columns_after_full_schema_validation(
    runtime: RuntimeContext,
) -> None:
    result = apply_run(runtime, "projected", full=True)

    current = load_current(
        result.current_directory.parent.parent,
        columns=("series_id", "business_date", "value"),
    )

    assert current is not None
    assert current.observations.column_names == [
        "series_id", "business_date", "value"
    ]
    assert current.observations.num_rows == 20


def test_current_loader_rejects_unknown_or_duplicate_projection(
    runtime: RuntimeContext,
) -> None:
    result = apply_run(runtime, "projection-error", full=True)
    root = result.current_directory.parent.parent

    with pytest.raises(LutouGoalBError, match="projection is invalid"):
        load_current(root, columns=("not_a_column",))
    with pytest.raises(LutouGoalBError, match="projection contains duplicates"):
        load_current(root, columns=("series_id", "series_id"))


def test_projected_current_loader_scans_all_physical_columns(
    runtime: RuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = apply_run(runtime, "projected-readability", full=True)
    original = pq.ParquetFile
    scans: list[object] = []

    class SpyParquetFile:
        def __init__(self, path: Path) -> None:
            self._inner = original(path)
            self.metadata = self._inner.metadata

        def scan_contents(self, *, columns=None):  # type: ignore[no-untyped-def]
            scans.append(columns)
            return self._inner.scan_contents(columns=columns)

    monkeypatch.setattr(
        "agri_research_agent.pipelines.lutou_goal_b.pq.ParquetFile",
        SpyParquetFile,
    )

    current = load_current(
        result.current_directory.parent.parent,
        columns=("series_id", "business_date", "value"),
    )

    assert current is not None
    assert scans == [None]


def test_projected_current_loader_fails_closed_on_nonprojected_readability(
    runtime: RuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = apply_run(runtime, "projected-unreadable", full=True)

    class UnreadableParquetFile:
        def __init__(self, _path: Path) -> None:
            self.metadata = object()

        def scan_contents(self, *, columns=None):  # type: ignore[no-untyped-def]
            assert columns is None
            raise OSError("synthetic nonprojected column failure")

    monkeypatch.setattr(
        "agri_research_agent.pipelines.lutou_goal_b.pq.ParquetFile",
        UnreadableParquetFile,
    )

    with pytest.raises(LutouGoalBError, match="readability is invalid"):
        load_current(
            result.current_directory.parent.parent,
            columns=("series_id", "business_date", "value"),
        )


def test_legacy_current_provider_is_reconciled_without_changing_stable_keys(
    runtime: RuntimeContext,
) -> None:
    result = apply_run(runtime, "full", full=True)
    current = load_current(result.current_directory.parent.parent)
    assert current is not None
    legacy = current.observations.select(
        [
            field.name
            for field in CANONICAL_SCHEMA
            if field.name
            not in {
                "provider",
                "metadata_source_type",
                "metadata_status",
                "source_quote_unit",
            }
        ]
    )

    upgraded = _upgrade_legacy_current(legacy)

    assert upgraded.schema == CANONICAL_SCHEMA
    assert set(upgraded["provider"].to_pylist()) == {"Reuters", "Oil World"}
    assert upgraded.select(["series_id", "business_date"]).equals(
        current.observations.select(["series_id", "business_date"])
    )
    provenance = result.current_manifest["metadata_provenance"]
    assert provenance["scope_rule"] == "exact official-series identity matches only"
    assert provenance["official_provider_series"] == [
        {
            "provider": "Oil World",
            "metadata_source_type": "official_provider_website",
            "metadata_status": "proven",
            "currency": "USD",
            "source_quote_unit": "US-$/T",
            "business_quote_unit": "USD/T",
            "provider_series_id": "lutou:oils:oil_world_prices:Soybean oil,Dutch, fob ex-mill",
        }
    ]


def test_conflicting_live_duplicate_fails_before_candidate(runtime: RuntimeContext) -> None:
    with pytest.raises(LutouGoalBError, match="quality gate"):
        apply_run(runtime, "conflict", full=True, client=FakeLiveClient(conflict=True))
    public_root = runtime.runtime_root / "public-market-data" / "lutou-three-oil"
    assert not (public_root / "current.json").exists()


@pytest.mark.parametrize(
    "hook",
    ["connection", "candidate_qc", "canonical_collision", "manifest_missing", "promote_before_pointer"],
)
def test_failures_preserve_existing_current(
    runtime: RuntimeContext, hook: str
) -> None:
    apply_run(runtime, "full", full=True)
    pointer = runtime.runtime_root / "public-market-data" / "lutou-three-oil" / "current.json"
    before_pointer = identify_file(pointer)
    before = load_current(pointer.parent)
    assert before is not None
    before_data = identify_file(before.directory / "observations.parquet")

    with pytest.raises(LutouGoalBError):
        apply_run(
            runtime,
            f"failed-{hook}",
            full=False,
            client=FakeLiveClient(price_shift=Decimal("1")),
            failure_hook=hook,
        )

    after = load_current(pointer.parent)
    assert after is not None
    assert identify_file(pointer) == before_pointer
    assert after.release_id == before.release_id
    assert identify_file(after.directory / "observations.parquet") == before_data


def test_manifests_exclude_credentials_and_absolute_paths(runtime: RuntimeContext) -> None:
    result = apply_run(runtime, "full", full=True)
    encoded = json.dumps(
        [result.candidate_manifest, result.canonical_manifest, result.current_manifest],
        ensure_ascii=False,
    ).lower()
    for forbidden in (
        "password",
        "passwd",
        "username",
        "private_key",
        "mysql://",
        "mariadb://",
        str(runtime.runtime_root).lower(),
    ):
        assert forbidden not in encoded


def _replace_column(table: pa.Table, name: str, values: pa.Array) -> pa.Table:
    return table.set_column(
        table.schema.get_field_index(name), table.schema.field(name), values
    )


def test_three_oil_arrow_merge_matches_reference_for_revision_and_boundaries(
    runtime: RuntimeContext,
) -> None:
    result = apply_run(runtime, "three-oil-arrow-golden", full=True)
    current = pq.read_table(result.current_directory / "observations.parquet")
    historical = _replace_column(
        current,
        "business_date",
        pa.array([date(2026, 6, 1)] * current.num_rows, type=pa.date32()),
    )
    revised = _replace_column(
        current,
        "source_group_sha256",
        pa.array(["f" * 64, *current["source_group_sha256"].to_pylist()[1:]]),
    )
    previous = pa.concat_tables([historical, current])
    start = date(2026, 7, 18)

    expected = _merge_current_reference(previous, revised, start)
    actual = _merge_current(previous, revised, start)

    assert actual.schema == expected.schema
    assert actual.column_names == expected.column_names
    assert actual.num_rows == expected.num_rows
    assert actual.equals(expected)
    assert actual.equals(
        actual.sort_by(
            [("series_id", "ascending"), ("business_date", "ascending")]
        )
    )
    assert all(column.null_count == 0 for column in actual.columns)


def test_three_oil_merge_handles_no_overlap_full_overlap_and_empty_tables(
    runtime: RuntimeContext,
) -> None:
    result = apply_run(runtime, "three-oil-arrow-boundaries", full=True)
    current = pq.read_table(result.current_directory / "observations.parquet")
    historical = _replace_column(
        current,
        "business_date",
        pa.array([date(2026, 6, 1)] * current.num_rows, type=pa.date32()),
    )
    empty = current.slice(0, 0)
    start = date(2026, 7, 18)

    assert _merge_current(historical, current, start).equals(
        _merge_current_reference(historical, current, start)
    )
    assert _merge_current(current, current, start).equals(current)
    assert _merge_current(empty, current, start).equals(current)
    assert _merge_current(current, empty, start).equals(
        _merge_current_reference(current, empty, start)
    )


@pytest.mark.parametrize("duplicate_side", ["previous", "window"])
def test_three_oil_merge_rejects_duplicate_stable_keys(
    runtime: RuntimeContext, duplicate_side: str
) -> None:
    result = apply_run(runtime, f"three-oil-duplicate-{duplicate_side}", full=True)
    current = pq.read_table(result.current_directory / "observations.parquet")
    previous = current
    window = current
    if duplicate_side == "previous":
        previous = pa.concat_tables([previous, previous.slice(0, 1)])
    else:
        window = pa.concat_tables([window, window.slice(0, 1)])

    with pytest.raises(LutouGoalBError, match="stable key"):
        _merge_current(previous, window, date(2026, 7, 18))


def test_three_oil_merge_hot_path_forbids_whole_history_python_rows() -> None:
    for callable_ in (_merge_current, arrow_upsert.merge_authoritative_window):
        source = inspect.getsource(callable_)
        assert ".to_pylist(" not in source
        assert "from_pylist(" not in source


def test_three_oil_performance_telemetry_is_observation_only(
    runtime: RuntimeContext,
) -> None:
    result = apply_run(runtime, "three-oil-telemetry", full=True)
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
    assert result.performance["io"]["candidate_write"]["rows"] == 20
    for manifest in (
        result.candidate_manifest,
        result.canonical_manifest,
        result.current_manifest,
    ):
        assert "performance" not in manifest
        assert "telemetry" not in manifest


def test_three_oil_timing_values_do_not_change_artifact_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ticking(step: float):  # type: ignore[no-untyped-def]
        value = 0.0

        def tick() -> float:
            nonlocal value
            value += step
            return value

        return tick

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return cls(2026, 8, 19, tzinfo=tz)

    roots = (tmp_path / "first", tmp_path / "second")
    for root in roots:
        root.mkdir()
        (root / ".market-data-runtime.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "runtime_id": f"three-oil-identity-{root.name}",
                    "classification": "isolated-dev",
                    "module_id": "international-spread",
                    "created_at": "2026-08-19T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
    runtimes = tuple(
        RuntimeContext(RuntimeMode.ISOLATED_DEV, "international-spread", root)
        for root in roots
    )
    monkeypatch.setattr(three_oil_pipeline, "datetime", FixedDateTime)
    monkeypatch.setattr(three_oil_pipeline, "perf_counter", ticking(1.0))
    first = apply_run(runtimes[0], "three-oil-identity", full=True)
    monkeypatch.setattr(three_oil_pipeline, "perf_counter", ticking(10.0))
    second = apply_run(runtimes[1], "three-oil-identity", full=True)

    assert first.performance != second.performance
    assert first.candidate_manifest == second.candidate_manifest
    assert first.canonical_manifest == second.canonical_manifest
    assert first.current_manifest == second.current_manifest
    for filename in ("observations.parquet", "manifest.json"):
        assert identify_file(first.current_directory / filename) == identify_file(
            second.current_directory / filename
        )
