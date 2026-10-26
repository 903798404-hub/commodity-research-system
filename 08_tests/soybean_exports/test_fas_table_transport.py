"""Offline fixtures: these are not observations from the official API."""
from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.shared import table_increment as codec
from agri_research_agent.soybean_exports import fas
from agri_research_agent.soybean_exports.common import PipelineError, sha256_file
from test_fas_data_layer import GIT_HEAD, fas_row
from test_fas_incremental import WindowAdapter, historical


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def seed(root, monkeypatch):
    monkeypatch.setattr(fas, "utc_now", lambda: datetime(2026, 8, 7, tzinfo=timezone.utc))
    fas.run_fas_pipeline(runtime_root=root, adapter=WindowAdapter(historical()),
                         git_head=GIT_HEAD, batch_id="seed", incremental=True)
    return fas.fas_paths(root, "unused")


def resolve(audit, old_path):
    candidate_path = Path(audit["candidate_path"])
    manifest = json.loads((candidate_path.parent / "manifest.json").read_text(encoding="utf-8"))
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    old = pd.read_parquet(old_path) if old_path is not None else None
    restored = codec.resolve(payload, old, baseline_sha256=sha256_file(old_path).lower() if old_path else None,
        key_columns=fas.FAS_KEY, scope_column="report_market_year_end",
        scope_values=manifest["fetch_scope"]["report_market_years"],
        refresh_columns=("source_fetch_id", "batch_id", "raw_snapshot_id", "raw_snapshot_sha256",
                         "fetch_time_utc", "source_release_time_raw", "source_release_timezone"))
    fas.validate_fas_increment(restored, old, manifest)
    return restored, payload


@pytest.mark.parametrize("change", ["none", "add", "revise"])
def test_readonly_baseline_emits_sparse_candidate_without_full_parquet(tmp_path, monkeypatch, change):
    baseline, candidate_root = tmp_path / "baseline", tmp_path / "candidate"
    paths = seed(baseline, monkeypatch)
    before = snapshot(baseline)
    rows = historical()
    if change == "add":
        rows += [fas_row(week="2026-08-06T00:00:00")]
    elif change == "revise":
        rows[-1]["currentMYNetSales"] = 13
    monkeypatch.setattr(fas, "write_parquet_atomic", lambda *a, **k: pytest.fail("full Parquet written"))
    audit = fas.run_fas_pipeline(runtime_root=candidate_root, baseline_root=baseline,
        adapter=WindowAdapter(rows), git_head=GIT_HEAD, batch_id="delta",
        incremental=True, incremental_transport=True, publish=False)
    assert audit["status"] == "candidate_only" and not audit["published"]
    assert not list(candidate_root.rglob("*.parquet"))
    assert snapshot(baseline) == before
    restored, payload = resolve(audit, paths["stable"])
    old = pd.read_parquet(paths["stable"])
    pd.testing.assert_frame_equal(restored.loc[restored.report_market_year_end.eq(2024)].reset_index(drop=True),
                                  old.loc[old.report_market_year_end.eq(2024)].reset_index(drop=True))
    assert len(restored) == (4 if change == "add" else 3)
    if change == "none":
        assert payload["updates"] is None and not payload["removals"]
    else:
        assert codec._decode(payload["updates"]).num_rows == 1
    assert list(candidate_root.rglob("records.jsonl.gz")), "official-format input evidence must be retained"


def test_empty_baseline_initialization_still_passes_existing_domain_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(fas, "utc_now", lambda: datetime(2026, 8, 7, tzinfo=timezone.utc))
    audit = fas.run_fas_pipeline(runtime_root=tmp_path, adapter=WindowAdapter(historical()),
        git_head=GIT_HEAD, batch_id="init", incremental=True, incremental_transport=True, publish=False)
    result, payload = resolve(audit, None)
    assert len(result) == 3 and payload["baseline_sha256"] is None


@pytest.mark.parametrize("mode", ["publish", "nonincremental", "same", "child", "parent"])
def test_unsafe_transport_modes_reject_before_provider_or_writes(tmp_path, mode):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    root = tmp_path / "candidate"
    options = dict(baseline_root=baseline, publish=False, incremental=True, incremental_transport=True)
    if mode == "publish": options["publish"] = True
    elif mode == "nonincremental": options["incremental"] = False
    elif mode == "same": root = baseline
    elif mode == "child": root = baseline / "child"
    elif mode == "parent": root = tmp_path
    class Forbidden:
        def fetch_export_sales(self, *args, **kwargs): pytest.fail("provider invoked")
    before = snapshot(tmp_path)
    with pytest.raises(PipelineError):
        fas.run_fas_pipeline(runtime_root=root, adapter=Forbidden(), git_head=GIT_HEAD, **options)
    assert snapshot(tmp_path) == before


def test_provider_deletion_and_baseline_manifest_drift_preserve_source(tmp_path, monkeypatch):
    baseline, output = tmp_path / "baseline", tmp_path / "output"
    paths = seed(baseline, monkeypatch)
    fas.run_fas_pipeline(runtime_root=baseline, adapter=WindowAdapter(historical() +
        [fas_row(week="2026-07-23T00:00:00")]), git_head=GIT_HEAD,
        batch_id="extra", incremental=True)
    before = snapshot(baseline)
    with pytest.raises(PipelineError, match="removes published"):
        fas.run_fas_pipeline(runtime_root=output, baseline_root=baseline,
            adapter=WindowAdapter(historical()), git_head=GIT_HEAD, batch_id="missing",
            incremental=True, incremental_transport=True, publish=False)
    assert snapshot(baseline) == before
    manifest = json.loads(paths["stable_manifest"].read_text())
    manifest["sha256"] = "0" * 64
    paths["stable_manifest"].write_text(json.dumps(manifest))
    drifted = snapshot(baseline)
    with pytest.raises(PipelineError, match="identity differs"):
        fas.run_fas_pipeline(runtime_root=output, baseline_root=baseline,
            adapter=WindowAdapter(historical()), git_head=GIT_HEAD, batch_id="drift",
            incremental=True, incremental_transport=True, publish=False)
    assert snapshot(baseline) == drifted


def test_legacy_candidate_mode_remains_real_parquet_with_separate_readonly_baseline(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline"
    seed(baseline, monkeypatch)
    before = snapshot(baseline)
    audit = fas.run_fas_pipeline(runtime_root=tmp_path / "output", baseline_root=baseline,
        adapter=WindowAdapter(historical()), git_head=GIT_HEAD, batch_id="legacy",
        incremental=True, publish=False)
    assert len(pd.read_parquet(audit["candidate_path"])) == 3
    assert snapshot(baseline) == before
