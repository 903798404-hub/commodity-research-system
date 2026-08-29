from __future__ import annotations

import inspect
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

import agri_research_agent.pipelines.tankan_goal_a as tankan_pipeline
import agri_research_agent.shared.arrow_window_upsert as arrow_upsert
from agri_research_agent.data_sources.tankan.models import (
    ConnectionProof,
    QueryPlanProof,
    SourceBatch,
)
from agri_research_agent.pipelines.tankan_goal_a import (
    FX_STABLE_KEY,
    MARKET_STABLE_KEY,
    MARKET_PRODUCTS,
    TankanGoalAError,
    _merge_current,
    _merge_current_reference,
    load_current,
    run_goal_a,
)
from agri_research_agent.shared.file_identity import identify_file
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


DAY = date(2026, 8, 17)
ROOT = Path(__file__).resolve().parents[2]


class FakeReader:
    proof = ConnectionProof(
        database="fixture",
        server_version="18.4",
        source_timezone="Asia/Shanghai",
        default_transaction_read_only="on",
        transaction_read_only="on",
    )

    def __init__(self, *, invalid_market_row: bool = False, price_shift: float = 0.0) -> None:
        self.invalid_market_row = invalid_market_row
        self.price_shift = price_shift

    def plan_stream(self, query, parameters, *, batch_size=10_000):
        plan = QueryPlanProof(query.sha256, 20, 10.0, query.max_plan_rows, query.max_total_cost)
        if "foreign_futures" in query.name:
            rows = [
                market_row("CBOT", "soybean", "2609", 1_025.5 + self.price_shift),
                market_row("CBOT", "soymeal", "2610", 312.4),
                market_row("CBOT", "soyoil", "2612", 51.25),
                market_row("BMD", "palm", "2610", 4_432.0),
            ]
            if self.invalid_market_row:
                rows.append(market_row("CBOT", "soybean", "9912", 900.0))
        else:
            rows = [fx_row()]
        batch = SourceBatch(query, plan, tuple(rows), datetime(2026, 8, 18, tzinfo=timezone.utc))
        return plan, iter([batch])


def market_row(exchange: str, product: str, contract: str, price: float) -> dict[str, object]:
    return {
        "trade_date": DAY,
        "exchange": exchange,
        "product_name": product,
        "contract": contract,
        "close_price": price,
        "updated_at": datetime(2026, 8, 18, 8, 0),
    }


def fx_row() -> dict[str, object]:
    row: dict[str, object] = {
        "trade_date": DAY,
        "spot": 6.7394,
        "updated_at": datetime(2026, 8, 18, 8, 0),
    }
    for month in range(1, 13):
        row[f"fx_{month}m"] = 6.7394 - month / 1000
    return row


@pytest.fixture
def runtime(tmp_path: Path) -> RuntimeContext:
    marker = {
        "schema_version": 1,
        "runtime_id": "isolated-dev-international-spread-goal-a-test",
        "classification": "isolated-dev",
        "module_id": "international-spread",
        "created_at": "2026-08-18T00:00:00Z",
    }
    (tmp_path / ".market-data-runtime.json").write_text(json.dumps(marker), encoding="utf-8")
    return RuntimeContext(RuntimeMode.ISOLATED_DEV, "international-spread", tmp_path)


def apply_run(runtime: RuntimeContext, run_id: str, *, full: bool, reader=None, failure_hook=None):
    return run_goal_a(
        reader or FakeReader(),
        runtime=runtime,
        run_id=run_id,
        market_config_path=ROOT / "02_configs" / "tankan_goal_a_market_price.yaml",
        fx_config_path=ROOT / "02_configs" / "tankan_fx.yaml",
        end_date=DAY,
        full_load=full,
        failure_hook=failure_hook,
        market_full_start=DAY,
        fx_full_start=DAY,
    )


def test_full_then_31_day_incremental_is_idempotent(runtime: RuntimeContext) -> None:
    first = apply_run(runtime, "full", full=True)
    pointer = runtime.runtime_root / "public-market-data" / "tankan" / "current.json"
    before = identify_file(pointer)
    second = apply_run(runtime, "incremental", full=False)
    after = identify_file(pointer)

    assert first.promoted is True
    assert second.promoted is False
    assert before == after
    current = load_current(pointer.parent)
    assert current is not None
    assert current.release_id == "full"
    assert set(current.market["product"].to_pylist()) == MARKET_PRODUCTS
    assert not ({"CANOLA", "RAPESEED"} & set(current.market["product"].to_pylist()))
    assert current.fx.num_rows == 13
    assert len(set(zip(*(current.market[name].to_pylist() for name in ("business_date", "exchange", "product", "instrument_id", "price_type", "session"))))) == current.market.num_rows


@pytest.mark.parametrize(
    "hook",
    ["connection", "candidate_qc", "canonical_collision", "manifest_missing", "promote_before_pointer"],
)
def test_failures_preserve_existing_current(runtime: RuntimeContext, hook: str) -> None:
    apply_run(runtime, "full", full=True)
    pointer = runtime.runtime_root / "public-market-data" / "tankan" / "current.json"
    before_pointer = identify_file(pointer)
    before = load_current(pointer.parent)
    assert before is not None
    before_market = identify_file(before.directory / "market.parquet")
    before_fx = identify_file(before.directory / "fx.parquet")

    with pytest.raises(TankanGoalAError):
        apply_run(
            runtime,
            f"failed-{hook}",
            full=False,
            reader=FakeReader(price_shift=1.0),
            failure_hook=hook,
        )

    after = load_current(pointer.parent)
    assert after is not None
    assert identify_file(pointer) == before_pointer
    assert after.release_id == before.release_id
    assert identify_file(after.directory / "market.parquet") == before_market
    assert identify_file(after.directory / "fx.parquet") == before_fx


def test_exception_is_retained_in_standard_but_not_current(runtime: RuntimeContext) -> None:
    result = apply_run(runtime, "full", full=True, reader=FakeReader(invalid_market_row=True))
    standard = pq.read_table(result.candidate_directory / "market_standard.parquet")
    current = pq.read_table(result.current_directory / "market.parquet")

    assert standard.num_rows == 5
    assert sum(not value for value in standard["is_usable"].to_pylist()) == 1
    assert result.candidate_manifest["quality_status"] == "PASS_WITH_RETAINED_EXCEPTIONS"
    assert current.num_rows == 4
    assert all(current["is_usable"].to_pylist())


def test_incremental_late_revision_replaces_value_without_duplicate(runtime: RuntimeContext) -> None:
    first = apply_run(runtime, "full", full=True)
    first_market = pq.read_table(first.current_directory / "market.parquet")
    revised = apply_run(
        runtime,
        "incremental-revision",
        full=False,
        reader=FakeReader(price_shift=1.0),
    )
    revised_market = pq.read_table(revised.current_directory / "market.parquet")

    assert revised.promoted is True
    assert revised.current_manifest["release_id"] == "incremental-revision"
    assert revised_market.num_rows == first_market.num_rows
    soybean = revised_market.filter(pc.equal(revised_market["product"], "SOYBEAN"))
    assert soybean["price"].to_pylist() == [1_026.5]
    assert len(set(soybean["source_row_sha256"].to_pylist())) == 1


def test_source_identity_and_fx_direction_reach_current(runtime: RuntimeContext) -> None:
    result = apply_run(runtime, "full", full=True)
    market = pq.read_table(result.current_directory / "market.parquet")
    fx = pq.read_table(result.current_directory / "fx.parquet")

    assert all(value.endswith(".en") for value in market["source_series_id"].to_pylist())
    assert market["source_series_id"].to_pylist() == market["provider_series_id"].to_pylist()
    assert set(fx["base_currency"].to_pylist()) == {"USD"}
    assert set(fx["quote_currency"].to_pylist()) == {"CNH"}
    assert set(fx["rate_unit"].to_pylist()) == {"CNH_per_USD"}


def _replace_column(table: pa.Table, name: str, values: pa.Array) -> pa.Table:
    return table.set_column(
        table.schema.get_field_index(name), table.schema.field(name), values
    )


def test_arrow_native_merge_matches_reference_for_both_tankan_domains(
    runtime: RuntimeContext,
) -> None:
    result = apply_run(runtime, "arrow-golden", full=True)
    market = pq.read_table(result.current_directory / "market.parquet")
    fx = pq.read_table(result.current_directory / "fx.parquet")
    historical_market = _replace_column(
        market,
        "business_date",
        pa.array([date(2026, 6, 1)] * market.num_rows, type=pa.date32()),
    )
    historical_fx = _replace_column(
        fx,
        "quote_date",
        pa.array([date(2026, 6, 1)] * fx.num_rows, type=pa.date32()),
    )
    market_window = _replace_column(
        market,
        "instrument_id",
        pa.array([None, *market["instrument_id"].to_pylist()[1:]], type=pa.string()),
    )
    market_window = _replace_column(
        market_window,
        "source_row_sha256",
        pa.array(["f" * 64, *market["source_row_sha256"].to_pylist()[1:]]),
    )
    fx_window = _replace_column(
        fx,
        "value_date",
        pa.array([None] * fx.num_rows, type=pa.date32()),
    )
    fx_window = _replace_column(
        fx_window,
        "source_row_sha256",
        pa.array(["e" * 64] * fx.num_rows),
    )

    for previous, window, keys, date_column in (
        (
            pa.concat_tables([historical_market, market]),
            market_window,
            MARKET_STABLE_KEY,
            "business_date",
        ),
        (
            pa.concat_tables([historical_fx, fx]),
            fx_window,
            FX_STABLE_KEY,
            "quote_date",
        ),
    ):
        expected = _merge_current_reference(previous, window, keys, date_column)
        actual = _merge_current(previous, window, keys, date_column)
        assert actual.schema == expected.schema
        assert actual.column_names == expected.column_names
        assert actual.num_rows == expected.num_rows
        assert actual.equals(expected)
        assert actual.equals(actual.sort_by([(key, "ascending") for key in keys]))


def test_tankan_merge_handles_no_overlap_full_overlap_and_empty_previous(
    runtime: RuntimeContext,
) -> None:
    result = apply_run(runtime, "arrow-boundaries", full=True)
    market = pq.read_table(result.current_directory / "market.parquet")
    historical = _replace_column(
        market,
        "business_date",
        pa.array([date(2026, 6, 1)] * market.num_rows, type=pa.date32()),
    )
    empty = market.slice(0, 0)

    assert _merge_current(
        historical, market, MARKET_STABLE_KEY, "business_date"
    ).equals(
        _merge_current_reference(
            historical, market, MARKET_STABLE_KEY, "business_date"
        )
    )
    assert _merge_current(
        market, market, MARKET_STABLE_KEY, "business_date"
    ).equals(market)
    assert _merge_current(
        empty, market, MARKET_STABLE_KEY, "business_date"
    ).equals(market)
    with pytest.raises(TankanGoalAError, match="window"):
        _merge_current(market, empty, MARKET_STABLE_KEY, "business_date")


@pytest.mark.parametrize("duplicate_side", ["previous", "window"])
def test_tankan_merge_rejects_duplicate_stable_keys(
    runtime: RuntimeContext, duplicate_side: str
) -> None:
    result = apply_run(runtime, f"arrow-duplicate-{duplicate_side}", full=True)
    market = pq.read_table(result.current_directory / "market.parquet")
    previous = market
    window = market
    if duplicate_side == "previous":
        previous = pa.concat_tables([previous, previous.slice(0, 1)])
    else:
        window = pa.concat_tables([window, window.slice(0, 1)])

    with pytest.raises(TankanGoalAError, match="stable key"):
        _merge_current(previous, window, MARKET_STABLE_KEY, "business_date")


def test_tankan_merge_hot_path_forbids_whole_history_python_rows() -> None:
    for callable_ in (_merge_current, arrow_upsert.merge_authoritative_window):
        source = inspect.getsource(callable_)
        assert ".to_pylist(" not in source
        assert "from_pylist(" not in source


def test_tankan_performance_telemetry_is_observation_only(
    runtime: RuntimeContext,
) -> None:
    result = apply_run(runtime, "tankan-telemetry", full=True)
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
    assert result.performance["io"]["candidate_write"]["rows"] == 5
    for manifest in (
        result.candidate_manifest,
        result.canonical_manifest,
        result.current_manifest,
    ):
        assert "performance" not in manifest
        assert "telemetry" not in manifest


def test_tankan_timing_values_do_not_change_artifact_identity(
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
                    "runtime_id": f"tankan-identity-{root.name}",
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
    monkeypatch.setattr(tankan_pipeline, "datetime", FixedDateTime)
    monkeypatch.setattr(tankan_pipeline, "perf_counter", ticking(1.0))
    first = apply_run(runtimes[0], "tankan-identity", full=True)
    monkeypatch.setattr(tankan_pipeline, "perf_counter", ticking(10.0))
    second = apply_run(runtimes[1], "tankan-identity", full=True)

    assert first.performance != second.performance
    assert first.candidate_manifest == second.candidate_manifest
    assert first.canonical_manifest == second.canonical_manifest
    assert first.current_manifest == second.current_manifest
    for filename in ("market.parquet", "fx.parquet", "manifest.json"):
        assert identify_file(first.current_directory / filename) == identify_file(
            second.current_directory / filename
        )
