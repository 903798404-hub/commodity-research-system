from datetime import datetime, timezone
from copy import deepcopy

import pandas as pd
import pytest

from agri_research_agent.soybean_exports import fas
from agri_research_agent.soybean_exports.common import PipelineError
from test_fas_data_layer import StaticAdapter, fas_row, normalize, GIT_HEAD


class WindowAdapter(StaticAdapter):
    def __init__(self, rows):
        super().__init__(rows); self.requests = []
    def fetch_export_sales(self, years=None, *, recent_years=7, retained_years=()):
        self.requests.append(recent_years)
        selected = [r for r in self.rows if r["_report_market_year_end"] >= 2026 - recent_years + 1 or r["_report_market_year_end"] in retained_years]
        return StaticAdapter(selected).fetch_export_sales(years)


def historical():
    return [fas_row(report_year=2024, week="2024-07-25T00:00:00"),
            fas_row(report_year=2025, week="2025-07-31T00:00:00"), fas_row()]


def test_daily_queries_two_years_and_monthly_rechecks_seven_without_losing_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(fas, "utc_now", lambda: datetime(2026, 8, 7, tzinfo=timezone.utc))
    first = WindowAdapter(historical())
    initial = fas.run_fas_pipeline(runtime_root=tmp_path, adapter=first, git_head=GIT_HEAD, batch_id="initial", incremental=True)
    stable = pd.read_parquet(initial["stable_path"])
    oldest = stable[stable.report_market_year_end.eq(2024)].reset_index(drop=True)
    rows = historical() + [fas_row(week="2026-08-06T00:00:00")]
    daily = WindowAdapter(rows)
    update = fas.run_fas_pipeline(runtime_root=tmp_path, adapter=daily, git_head=GIT_HEAD, batch_id="daily", incremental=True)
    actual = pd.read_parquet(update["stable_path"])
    assert first.requests == [7] and daily.requests == [2]
    pd.testing.assert_frame_equal(oldest, actual[actual.report_market_year_end.eq(2024)].reset_index(drop=True))
    assert len(actual) == 4
    monkeypatch.setattr(fas, "utc_now", lambda: datetime(2026, 9, 5, tzinfo=timezone.utc))
    revised = deepcopy(rows); revised[0]["currentMYNetSales"] = 12
    monthly = WindowAdapter(revised)
    result = fas.run_fas_pipeline(runtime_root=tmp_path, adapter=monthly, git_head=GIT_HEAD, batch_id="monthly", incremental=True)
    assert monthly.requests == [7] and result["revision_count"] == 1


def test_increment_failure_and_provider_row_deletion_preserve_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(fas, "utc_now", lambda: datetime(2026, 8, 7, tzinfo=timezone.utc))
    fas.run_fas_pipeline(runtime_root=tmp_path, adapter=WindowAdapter(historical() + [fas_row(week="2026-07-23T00:00:00")]), git_head=GIT_HEAD, batch_id="initial", incremental=True)
    path = fas.fas_paths(tmp_path, "unused")["stable"]; before = path.read_bytes()
    with pytest.raises(PipelineError, match="removes published"):
        fas.run_fas_pipeline(runtime_root=tmp_path, adapter=WindowAdapter(historical()), git_head=GIT_HEAD, batch_id="missing", incremental=True)
    assert path.read_bytes() == before
    class Failed(WindowAdapter):
        def fetch_export_sales(self, *a, **k): raise TimeoutError("fixture failed")
    with pytest.raises(TimeoutError):
        fas.run_fas_pipeline(runtime_root=tmp_path, adapter=Failed([]), git_head=GIT_HEAD, batch_id="failed", incremental=True)
    assert path.read_bytes() == before


def test_offline_gate_refuses_unqueried_history_edits_and_provenance_forgery():
    old = normalize(historical(), "old")
    new = normalize(historical()[1:], "new")
    combined = fas.merge_fas_years(old, new, [2025, 2026])
    metadata = dict(batch_id="new", raw_snapshot_sha256="d" * 64, fetch_scope=dict(report_market_years=[2025, 2026]))
    fas.validate_fas_increment(combined, old, metadata)
    forged = combined.copy(); forged.loc[forged.report_market_year_end.eq(2024), "batch_id"] = "new"
    with pytest.raises(PipelineError, match="untouched history"):
        fas.validate_fas_increment(forged, old, metadata)
    bad = combined.copy(); bad.loc[bad.report_market_year_end.eq(2026), "raw_snapshot_sha256"] = "e" * 64
    with pytest.raises(PipelineError, match="provenance"):
        fas.validate_fas_increment(bad, old, metadata)


def test_monthly_reconciliation_includes_published_years_older_than_recent_seven(tmp_path, monkeypatch):
    monkeypatch.setattr(fas, "utc_now", lambda: datetime(2026, 8, 7, tzinfo=timezone.utc))
    old_rows = historical() + [fas_row(report_year=2010, week="2010-07-29T00:00:00")]
    fas.run_fas_pipeline(runtime_root=tmp_path, adapter=StaticAdapter(old_rows), git_head=GIT_HEAD, batch_id="legacy")
    monkeypatch.setattr(fas, "utc_now", lambda: datetime(2026, 9, 5, tzinfo=timezone.utc))
    changed = deepcopy(old_rows); changed[-1]["currentMYNetSales"] = 12
    result = fas.run_fas_pipeline(runtime_root=tmp_path, adapter=WindowAdapter(changed), git_head=GIT_HEAD,
        batch_id="reconcile", incremental=True)
    assert 2010 in result["collection_metrics"]["queried_years"] and result["revision_count"] == 1
