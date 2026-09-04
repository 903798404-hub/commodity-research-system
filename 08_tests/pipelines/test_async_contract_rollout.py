from copy import deepcopy
import json
import importlib.util
import sys
from pathlib import Path
from datetime import date
from types import SimpleNamespace

import pyarrow as pa
import pytest

from agri_research_agent.pipelines.async_contract_rollout import (
    DOMAINS, account_table, validate_report, collect_reports, render_reports, seal_reports,
)
from agri_research_agent.shared.async_update import FreshnessPolicy


def table(rows):
    return pa.Table.from_pylist(rows, schema=pa.schema([
        ("series_id", pa.string()), ("day", pa.date32()), ("run", pa.string()), ("value", pa.float64()),
    ]))


def row(series="a", day=1, value=1., run="r1"):
    return {"series_id": series, "day": date(2026, 9, day), "run": run, "value": value}


def account(domain, old, source, following, **kwargs):
    return account_table(dataset_id=domain, previous=table(old), source=table(source), following=table(following),
        keys=("series_id", "day", "run"), identity=("series_id",), date_column="day", values=("value",),
        required={"a", "b"}, as_of_date=date(2026, 9, 4), forecast=domain == "weather_forecast", **kwargs)


@pytest.mark.parametrize("domain", DOMAINS)
def test_each_domain_updated_no_change_dates_summary(domain):
    previous = [row(), row("b")]
    current = [*previous, row(day=3)]
    report = account(domain, previous, current, current)
    assert report["promotion_allowed"]
    assert report["summary"]["coverage"] == {"PRESENT": 2, "MISSING": 0, "ERROR": 0}
    assert report["summary"]["updates"] == {"UPDATED": 1, "NO_CHANGE": 1, "ERROR": 0}
    assert report["summary"]["freshness"] == {"FRESH": 0, "STALE": 0, "UNASSESSED": 2}
    b = report["series"][1]
    assert b["previous_latest_date"] == b["source_latest_date"] == b["next_latest_date"] == "2026-09-01"
    assert b["new_rows"] == 0 and b["threshold"] is None
    unchanged = account(domain, previous, previous, previous)
    assert unchanged["dataset_status"] == "NO_CHANGE"


@pytest.mark.parametrize("domain", DOMAINS)
def test_each_domain_missing_and_source_row_dropped_are_blocking(domain):
    previous = [row(), row("b")]
    missing = account(domain, previous, [row()], [row()])
    assert missing["summary"]["coverage"]["MISSING"] == 1
    assert not missing["promotion_allowed"]
    dropped = account(domain, previous, [*previous, row(day=3)], previous)
    assert dropped["summary"]["updates"]["ERROR"] == 1
    assert not dropped["promotion_allowed"]


@pytest.mark.parametrize("domain", DOMAINS)
def test_each_domain_corruption_duplicates_summary_and_freshness(domain):
    old = [row(), row("b")]
    corrupt = account(domain, old, old, [row(value=99), row("b")])
    assert not corrupt["promotion_allowed"]
    duplicate = account(domain, old, old, [*old, row()])
    assert not duplicate["promotion_allowed"]
    report = account(domain, old, old, old, policy=FreshnessPolicy("fixture-approved", 1, False, True))
    assert report["summary"]["updates"]["NO_CHANGE"] == 2
    assert report["summary"]["freshness"]["STALE"] == 2
    assert report["dataset_status"] == "WARNING" and report["promotion_allowed"]
    report["summary"]["updates"]["UPDATED"] = 5
    with pytest.raises(ValueError, match="SUMMARY"):
        validate_report(report)


def test_forecast_same_valid_through_new_run_is_update_not_future_error():
    old = [row(day=18), row("b", day=18)]
    new = [*old, row(day=18, run="r2")]
    report = account("weather_forecast", old, new, new)
    assert report["promotion_allowed"] and report["summary"]["updates"]["UPDATED"] == 1
    assert report["series"][0]["previous_valid_through"] == report["series"][0]["next_valid_through"]
    assert report["series"][0]["horizon_days"] == 14
    assert report["series"][0]["freshness_status"] == "UNASSESSED"
    assert not account("weather_observation", old, new, new)["promotion_allowed"]


def test_no_source_window_record_does_not_fabricate_source_inventory_date():
    old = [row(), row("b")]
    report = account("fx", old, [row()], old)
    assert report["promotion_allowed"]
    assert report["series"][1]["source_latest_date"] is None
    assert report["series"][1]["next_latest_date"] == "2026-09-01"


def test_schema_and_mapping_identity_corruption_block():
    old = [row(), row("b")]
    assert not account("fx", old, old, [row("unmapped"), row("b")])["promotion_allowed"]
    with pytest.raises(ValueError, match="SCHEMA"):
        account_table(dataset_id="fx", previous=table(old), source=table(old), following=table(old).drop(["value"]),
            keys=("series_id", "day"), identity=("series_id",), date_column="day", values=("value",), as_of_date=date(2026, 9, 4))


def test_seal_blocking_before_promotion_and_collect_separate_datasets(tmp_path):
    from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode
    (tmp_path / ".market-data-runtime.json").write_text('{"schema_version":1,"runtime_id":"test","classification":"isolated-dev","module_id":"async-rollout","created_at":"2026-09-04T00:00:00Z"}')
    runtime = RuntimeContext(RuntimeMode.ISOLATED_DEV, "async-rollout", tmp_path)
    reports = {name: account(name, [row(), row("b")], [row(), row("b")], [row(), row("b")]) for name in DOMAINS}
    outcomes = [SimpleNamespace(performance={"provider_details": {"async_updates": reports}})]
    assert len(collect_reports(outcomes)) == 6 and len(render_reports(reports)) == 6
    bad = deepcopy(reports)
    bad["fx"] = account("fx", [row(), row("b")], [row()], [row()])
    with pytest.raises(RuntimeError):
        seal_reports(runtime, "failed", bad, RuntimeError)
    assert (tmp_path / "async-contract-reports/failed/manifest.json").is_file()
    assert not (tmp_path / "public-market-data").exists()


def runtime_fixture(tmp_path):
    from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode
    (tmp_path / ".market-data-runtime.json").write_text(json.dumps({
        "schema_version": 1, "runtime_id": "rollout", "classification": "isolated-dev",
        "module_id": "async-rollout", "created_at": "2026-09-04T00:00:00Z",
    }), encoding="utf-8")
    return RuntimeContext(RuntimeMode.ISOLATED_DEV, "async-rollout", tmp_path)


def test_async_summary_corruption_uses_existing_full_transaction_rollback(tmp_path):
    from agri_research_agent.pipelines.public_data_refresh import CurrentIdentity, RefreshResult, run_unified_refresh
    runtime = runtime_fixture(tmp_path)
    good = account("fx", [row(), row("b")], [row(), row("b")], [row(), row("b")])
    bad = deepcopy(good)
    bad["summary"]["updates"]["UPDATED"] = 77
    class Adapter:
        def __init__(self, name, report):
            self.name, self.report = name, report
            self.pointer = tmp_path / "public-market-data" / name / "current.json"
            self.pointer.parent.mkdir(parents=True)
            self.pointer.write_text('{"release_id":"r1","manifest_sha256":"old"}')
        def current_identity(self):
            identity = json.loads(self.pointer.read_text())
            return CurrentIdentity(identity["release_id"], identity["manifest_sha256"], {})
        def preflight(self):
            return {"read_only": True}
        def refresh(self):
            self.pointer.write_text('{"release_id":"r2","manifest_sha256":"new"}')
            return RefreshResult(True, {}, performance={"async_updates": {"fx": self.report}})
    adapters = [Adapter("first", good), Adapter("second", bad)]
    originals = [item.pointer.read_bytes() for item in adapters]
    result = run_unified_refresh(runtime=runtime, run_id="rollback", adapters=adapters, require_all_sources=True)
    assert result.manifest["aggregate_status"] == "FAILED"
    assert result.transaction["rollback"] == "PASS"
    assert [item.pointer.read_bytes() for item in adapters] == originals


def test_full_daily_exposes_seven_separate_summaries(tmp_path):
    from agri_research_agent.pipelines.public_data_refresh import CurrentIdentity, RefreshResult, run_unified_refresh
    from agri_research_agent.pipelines.public_data_daily import run_daily_update
    runtime = runtime_fixture(tmp_path)
    old = [row(), row("b")]
    reports = {name: account(name, old, old, old) for name in (*DOMAINS, "domestic_basis")}
    class Adapter:
        name = "fixture"
        def current_identity(self):
            return CurrentIdentity("r1", "sha", {})
        def preflight(self):
            return {"read_only": True}
        def refresh(self):
            return RefreshResult(False, {}, performance={"async_updates": reports})
    refreshed = run_unified_refresh(runtime=runtime, run_id="all-domains", adapters=[Adapter()])
    daily = run_daily_update(runtime=runtime, run_id="summary", refresh_runner=lambda: refreshed,
        public_current_root=tmp_path / "public", packages_root=tmp_path / "packages")
    assert daily.succeeded and daily.manifest["async_summary_complete"]
    assert len(daily.manifest["dataset_update_summary"]) == 7
    assert "weather_observation |" in daily.manifest["summary"]
    assert "weather_forecast |" in daily.manifest["summary"]


@pytest.mark.parametrize("key,value", [("dataset_status", "UPDATED"), ("promotion_allowed", False)])
def test_dataset_status_cannot_disagree_with_series(key, value):
    report = account("fx", [row(), row("b")], [row(), row("b")], [row(), row("b")])
    report[key] = value
    with pytest.raises(ValueError):
        validate_report(report)


@pytest.mark.parametrize("producer", ["tankan_goal_a", "lutou_goal_b", "lutou_goal_b_soil", "lutou_weather"])
def test_existing_producer_incremental_outputs_are_actually_accounted(tmp_path, producer):
    # Reuse established offline client fixtures; no provider or production access.
    path = Path(__file__).with_name("test_" + producer + ".py")
    spec = importlib.util.spec_from_file_location("rollout_fixture_" + producer, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if producer in {"tankan_goal_a", "lutou_goal_b"}:
        runtime = module.runtime.__wrapped__(tmp_path)
        seed = module.apply_run(runtime, "seed", full=True)
        result = module.apply_run(runtime, "increment", full=False)
    elif producer == "lutou_goal_b_soil":
        runtime = module._runtime(tmp_path)
        args = dict(runtime=runtime, end_date=module.DAY, catalog_path=module._catalog())
        seed = module.run_goal_b_soil(module.FakeSoilClient(), run_id="seed", full_load=True, **args)
        result = module.run_goal_b_soil(module.FakeSoilClient(), run_id="increment", full_load=False, **args)
    else:
        runtime = module._runtime(tmp_path / "runtime")
        module._seed_soil(runtime)
        seed = module._run(runtime, "seed", full=True)
        result = module._run(runtime, "increment", full=False)
    assert not seed.async_reports  # bootstrap policy deliberately not migrated
    expected = {"tankan_goal_a": {"tankan_market", "fx"}, "lutou_goal_b": {"three_oil"},
                "lutou_goal_b_soil": {"soil_moisture"}, "lutou_weather": {"weather_observation", "weather_forecast"}}[producer]
    assert set(result.async_reports) == expected
    assert not result.promoted
    for report in result.async_reports.values():
        validate_report(report)
        assert report["dataset_status"] == "NO_CHANGE"
        assert report["summary"]["updates"]["ERROR"] == 0
        assert report["summary"]["freshness"]["UNASSESSED"] == report["summary"]["TOTAL_REQUIRED"]
