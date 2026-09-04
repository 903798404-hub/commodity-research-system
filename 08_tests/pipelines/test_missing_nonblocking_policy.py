"""Offline contract tests; optional saved-run replay never invokes a refresh."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.pipelines.async_contract_rollout import (
    account_table, validate_report, weather_reports, render_reports,
)
from agri_research_agent.pipelines.lutou_weather import STANDARD_SCHEMA, CANONICAL_SCHEMA
from agri_research_agent.data_sources.lutou.weather_live import load_weather_source_catalog
from agri_research_agent.shared.async_update import SeriesUpdate, FreshnessPolicy, evaluate_update
from test_async_contract_rollout import account, row, runtime_fixture

ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 9, 4)
GERMANY = {"weather.temperature_" + metric + ".rapeseed.eu.germany.forecast.ecmwf"
           for metric in ("max", "min")}


def test_never_observed_verified_empty_is_nonblocking_without_fabricated_dates():
    report = account("fx", [row()], [], [row()])
    assert report["promotion_allowed"]
    assert report["identity_coverage"]["accounted_for_count"] == 2
    missing = report["series"][1]
    assert missing["coverage_status"] == "MISSING"
    assert missing["update_status"] == "NO_CHANGE"
    assert missing["freshness_status"] == "UNASSESSED"
    assert missing["reason"] == "SOURCE_NO_VALID_OBSERVATION"
    assert not missing["blocking"]
    assert all(missing[key] is None for key in (
        "previous_latest_date", "source_latest_date", "next_latest_date", "age_days", "threshold"))
    assert missing["new_rows"] == 0
    assert report["series"][0]["coverage_status"] == "PRESENT"
    assert report["series"][0]["update_status"] == "NO_CHANGE"
    assert report["series"][0]["next_latest_date"] == "2026-09-01"
    validate_report(report)


def test_pure_contract_does_not_treat_missing_evidence_as_verified_empty():
    report = evaluate_update(dataset_id="future", required={"a"}, series={},
        next_identities=set(), as_of_date=DAY, policy=FreshnessPolicy("v1"),
        verified_empty_source={"a"})
    assert not report["promotion_allowed"]
    assert report["series"][0]["coverage_status"] == "ERROR"


def test_dropped_old_row_with_same_latest_still_blocks():
    old = [row(), row(day=2), row("b")]
    report = account("fx", old, [], [row(day=2), row("b")])
    assert not report["promotion_allowed"]
    assert report["series"][0]["coverage_status"] == "ERROR"
    assert "CURRENT_PRESENT_CANDIDATE_DROPPED" in report["series"][0]["reason"]


def test_source_revision_replaces_key_without_being_misclassified_as_loss():
    report = account("fx", [row(), row("b")], [row(value=3)], [row(value=3), row("b")])
    assert report["promotion_allowed"]
    assert report["series"][0]["revision_row_count"] == 1


def test_mapping_unknown_source_cannot_hide_behind_missing():
    with pytest.raises(ValueError, match="MAPPING"):
        account("fx", [], [row("unmapped")], [])


def test_valid_first_source_observation_dropped_is_error_not_missing():
    report = account("fx", [], [row()], [])
    assert report["series"][0]["coverage_status"] == "ERROR"
    assert report["series"][0]["blocking"]
    assert "SOURCE_PRESENT_CANDIDATE_DROPPED" in report["series"][0]["reason"]
    assert not report["promotion_allowed"]


@pytest.mark.parametrize("change", ["summary", "reason", "blocking", "proof", "policy"])
def test_missing_report_tamper_is_blocking(change):
    report = account("fx", [row()], [], [row()])
    if change == "summary":
        report["summary"]["coverage"]["MISSING"] = 0
    elif change == "reason":
        report["series"][1]["reason"] = "invented"
    elif change == "blocking":
        report["series"][1]["blocking"] = True
    elif change == "proof":
        report["verified_empty_source"] = []
    else:
        report["missing_source_is_blocking"] = True
    with pytest.raises(ValueError):
        validate_report(report)


def weather_fixture(tmp_path, corrupt=False):
    catalog = load_weather_source_catalog(None, ROOT / "02_configs/lutou_weather_current.yaml")
    rows = []
    for item in catalog.series:
        if item.data_family not in {"observation", "forecast"}:
            continue
        missing = item.series_id in GERMANY
        rows.append(dict(series_id=item.series_id, valid_date=DAY, forecast_run_id="fixture",
            data_family=item.data_family, value=None if missing else Decimal("1"),
            unit=item.unit, source_row_sha256="fixture", raw_value_text="" if missing else "1",
            quality_status="MISSING_VALUE" if missing else "PASS", is_usable=not missing,
            metric=item.metric))
    if corrupt:
        target = next(item for item in rows if item["series_id"] in GERMANY)
        target["raw_value_text"] = "12.5"  # valid source misclassified as NULL
    for item in rows:
        for field in STANDARD_SCHEMA:
            if not field.nullable and field.name not in item:
                item[field.name] = (datetime(2026, 9, 4, tzinfo=timezone.utc) if pa.types.is_timestamp(field.type)
                                    else False if pa.types.is_boolean(field.type) else "fixture")
        item["source_policy_version"] = "fixture"
    standard = pa.Table.from_pylist(rows, schema=STANDARD_SCHEMA)
    canonical = pa.Table.from_pylist([item for item in rows if item["is_usable"]], schema=CANONICAL_SCHEMA)
    candidate, following = tmp_path / "input", tmp_path / "next"
    candidate.mkdir()
    following.mkdir()
    pq.write_table(standard, candidate / "standard.parquet")
    pq.write_table(canonical, following / "observations.parquet")
    return catalog, candidate, following


def test_germany_catalog_fixture_455_present_2_missing(tmp_path):
    runtime = runtime_fixture(tmp_path)
    catalog, candidate, following = weather_fixture(tmp_path)
    report = weather_reports(runtime, "fixture", None, candidate, following, catalog, DAY, RuntimeError)["weather_forecast"]
    assert report["summary"]["TOTAL_REQUIRED"] == 457
    assert report["summary"]["coverage"] == {"PRESENT": 455, "MISSING": 2, "ERROR": 0}
    assert report["promotion_allowed"]
    assert set(report["identity_coverage"]["missing"]) == GERMANY
    for item in report["series"]:
        if item["identity"] in GERMANY:
            assert item["reason"] == "SOURCE_NO_VALID_OBSERVATION"
            assert item["freshness_status"] == "UNASSESSED" and not item["blocking"]
    assert all(identity in "\n".join(render_reports({"weather_forecast": report})) for identity in GERMANY)
    from agri_research_agent.pipelines.public_data_refresh import CurrentIdentity, RefreshResult, run_unified_refresh
    from agri_research_agent.pipelines.public_data_daily import run_daily_update
    class Adapter:
        name = "weather"
        def current_identity(self):
            return CurrentIdentity("r1", "sha", {})
        def preflight(self):
            return {"read_only": True}
        def refresh(self):
            return RefreshResult(False, {}, performance={"async_updates": {"weather_forecast": report}})
    result = run_unified_refresh(runtime=runtime, run_id="allowed-missing", adapters=[Adapter()], require_all_sources=True)
    daily = run_daily_update(runtime=runtime, run_id="allowed-missing-summary", refresh_runner=lambda: result,
        public_current_root=tmp_path / "public", packages_root=tmp_path / "packages")
    assert daily.succeeded
    assert all(identity in daily.manifest["summary"] for identity in GERMANY)


def test_normalization_corruption_blocks_but_both_reports_are_preserved(tmp_path):
    runtime = runtime_fixture(tmp_path)
    catalog, candidate, following = weather_fixture(tmp_path, corrupt=True)
    sink = {}
    with pytest.raises(RuntimeError, match="accounting failed"):
        weather_reports(runtime, "corrupt", None, candidate, following, catalog, DAY, RuntimeError, sink)
    assert set(sink) == {"weather_observation", "weather_forecast"}
    assert sink["weather_forecast"]["summary"]["coverage"]["ERROR"] == 1
    assert not sink["weather_forecast"]["promotion_allowed"]
    assert (tmp_path / "async-contract-reports/corrupt/manifest.json").exists()


@pytest.mark.parametrize("blocking_report", [False, True])
def test_weather_failure_reports_reach_daily_without_swallowing_exception(tmp_path, monkeypatch, blocking_report):
    from agri_research_agent.pipelines import public_data_providers as providers
    from agri_research_agent.pipelines.public_data_refresh import CurrentIdentity, ProviderStatus, run_unified_refresh
    from agri_research_agent.pipelines.public_data_daily import run_daily_update
    from agri_research_agent.pipelines.lutou_weather import LutouWeatherStageError
    from agri_research_agent.data_sources.lutou.live import LutouConnectionSettings
    runtime = runtime_fixture(tmp_path)
    old = [row(), row("b")]
    reports = {"weather_observation": account("weather_observation", old, old, old),
               "weather_forecast": account("weather_forecast", old, old, [row()] if blocking_report else old)}
    for function in ("run_goal_b", "run_goal_b_soil"):
        monkeypatch.setattr(providers, function, lambda *a, **k: SimpleNamespace(
            promoted=False, candidate_manifest={"source_max_date": "2026-09-01"}))
    def fail(*args, **kwargs):
        kwargs["async_report_sink"].update(deepcopy(reports))
        raise LutouWeatherStageError("PROMOTION", ValueError("injected publication failure"))
    monkeypatch.setattr(providers, "run_lutou_weather", fail)
    adapter = providers.LutouRefreshAdapter(
        LutouConnectionSettings("fixture.invalid", 3306, "reader", "fixture"),
        runtime, "failed-weather", DAY, Path("catalog.json"), weather_policy_path=Path("weather.yaml"))
    adapter._client = SimpleNamespace(ensure_connected=lambda: None, close=lambda: None)
    monkeypatch.setattr(providers.LutouRefreshAdapter, "preflight", lambda self: {"read_only": True})
    monkeypatch.setattr(providers.LutouRefreshAdapter, "current_identity", lambda self: CurrentIdentity("r1", "sha", {}))
    refreshed = run_unified_refresh(runtime=runtime, run_id="failed-provider", adapters=[adapter], require_all_sources=True)
    assert refreshed.providers[0].status is ProviderStatus.INGESTION_FAILURE
    assert refreshed.root_failure.stage == "PROMOTION"
    assert refreshed.transaction["rollback"] == "PASS"
    daily = run_daily_update(runtime=runtime, run_id="failed-daily", refresh_runner=lambda: refreshed,
        public_current_root=tmp_path / "public", packages_root=tmp_path / "packages")
    assert not daily.succeeded
    assert set(daily.manifest["async_updates"]) == set(reports)
    assert daily.manifest["root_failure"]["stage"] == "PROMOTION"
    assert "weather_observation |" in daily.manifest["summary"]
    assert "weather_forecast |" in daily.manifest["summary"]


def test_saved_real_germany_run_read_only_replay(tmp_path):
    source = os.environ.get("MISSING_POLICY_SAVED_WEATHER_ROOT")
    if not source:
        pytest.skip("optional saved production evidence; no live provider access")
    root = Path(source).resolve(strict=True)
    rid = "full-daily-20260904T064312.933627Z-da3fbbc1-lutou-weather"
    current_id = json.loads((root / "current.json").read_text(encoding="utf-8"))["release_id"]
    previous = root / "releases" / current_id / "observations.parquet"
    candidate = root / "candidates" / rid
    following = root / "canonical-candidates" / rid
    paths = [previous, candidate / "standard.parquet", following / "observations.parquet", root / "current.json"]
    def hashes():
        result = {}
        for path in paths:
            with path.open("rb") as stream:
                result[str(path)] = hashlib.file_digest(stream, "sha256").hexdigest()
        return result
    before = hashes()
    catalog = load_weather_source_catalog(None, ROOT / "02_configs/lutou_weather_current.yaml")
    runtime = runtime_fixture(tmp_path)
    reports = weather_reports(runtime, "saved-evidence-replay", SimpleNamespace(observations_path=previous),
        candidate, following, catalog, DAY, RuntimeError)
    assert hashes() == before
    report = reports["weather_forecast"]
    assert report["summary"]["TOTAL_REQUIRED"] == 457
    assert report["summary"]["coverage"] == {"PRESENT": 455, "MISSING": 2, "ERROR": 0}
    assert set(report["identity_coverage"]["missing"]) == GERMANY
    assert all(item["promotion_allowed"] for item in reports.values())
    print(json.dumps({key: value["summary"] for key, value in reports.items()}))
