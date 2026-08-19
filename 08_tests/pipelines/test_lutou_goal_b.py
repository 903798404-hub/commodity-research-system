from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from agri_research_agent.data_sources.lutou.live import (
    LutouBatch,
    LutouConnectionProof,
    LutouPlanProof,
)
from agri_research_agent.pipelines.lutou_goal_b import (
    CANONICAL_SCHEMA,
    LutouGoalBError,
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
