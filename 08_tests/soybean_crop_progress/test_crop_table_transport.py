"""Synthetic API fixtures; no official network requests or live publication."""
from datetime import datetime, timezone
import json

import pandas as pd
import pyarrow as pa
import pytest

from agri_research_agent.pipelines import soybean_crop_weekly_update as crop
from agri_research_agent.shared import table_increment as codec
from test_soybean_crop_weekly_update import (
    CurrentYearTransport, FAKE_SECRET, JULY_2026, TEST_GIT_HEAD, _prepare_root, _run,
)


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_sparse_crop_candidate_reads_baseline_in_place_and_preserves_complete_pair(
        tmp_path, monkeypatch, legacy, changed):
    baseline, output = tmp_path / "baseline", tmp_path / "candidate"
    paths = _prepare_root(baseline)
    if not legacy:
        _run(baseline, CurrentYearTransport())
    before = snapshot(baseline)
    candidate_paths = crop.soybean_weekly_update_paths(output)
    candidate_paths["display_config"].parent.mkdir(parents=True)
    candidate_paths["display_config"].write_bytes(paths["display_config"].read_bytes())
    monkeypatch.setattr(crop, "_write_and_verify_candidate", lambda *a, **k: pytest.fail("full Parquet written"))
    audit = crop.run_soybean_crop_weekly_update(project_root=output, baseline_root=baseline,
        incremental_transport=True, dry_run=True,
        transport=CurrentYearTransport(planted_value=99 if changed else 100),
        now=datetime(2026, 7, 18, 12, tzinfo=timezone.utc), retries=0,
        sleep=lambda _: None, env={"NASS_API_KEY": FAKE_SECRET}, git_head=TEST_GIT_HEAD)
    assert not audit["published"] and audit["candidate_validation"]["passed"]
    assert not list(output.rglob("*.parquet"))
    assert snapshot(baseline) == before
    for family in ("progress", "condition"):
        key = f"{'legacy' if legacy else 'stable'}_{family}"
        payload_path = output / audit[f"candidate_{family}_path"]
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        old = pd.read_parquet(paths[key])
        restored = codec.resolve(payload, old, baseline_sha256=crop._sha256(paths[key]),
            key_columns=crop.CROP_STABLE_KEY, scope_column="calendar_year", scope_values=[2026],
            refresh_columns=("retrieved_at_utc", "raw_snapshot"))
        expected = old.loc[old.calendar_year.ne(2026)].sort_values(crop.CROP_STABLE_KEY).reset_index(drop=True)
        actual = restored.loc[restored.calendar_year.ne(2026)].sort_values(crop.CROP_STABLE_KEY).reset_index(drop=True)
        # Complete candidates can make a formerly non-null int column nullable,
        # or merge object/extension string columns, as the legacy writer does.
        # Require identical stored types, values and null positions after a
        # safe Arrow cast; original baseline file bytes are checked separately.
        expected_table = pa.Table.from_pandas(expected, preserve_index=False)
        actual_table = pa.Table.from_pandas(actual, preserve_index=False)
        assert actual_table.cast(expected_table.schema, safe=True).equals(expected_table)
        assert crop._sha256(payload_path) == audit["candidate_sha256"][family]
    assert (output / audit["raw_path"]).is_file()
    assert (output / audit["manifest_path"]).is_file()
    assert audit["status"] == ("initialized" if legacy else "updated" if changed else "no_change")


@pytest.mark.parametrize("mode", ["publish", "same", "child", "parent"])
def test_crop_unsafe_baseline_mode_rejects_before_api_or_writes(tmp_path, mode):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    output = tmp_path / "output"
    dry_run = mode != "publish"
    if mode == "same": output = baseline
    elif mode == "child": output = baseline / "candidate"
    elif mode == "parent": output = tmp_path
    before = snapshot(tmp_path)
    transport = CurrentYearTransport()
    with pytest.raises(crop.SoybeanWeeklyUpdateError):
        crop.run_soybean_crop_weekly_update(project_root=output, baseline_root=baseline,
            incremental_transport=True, dry_run=dry_run, transport=transport,
            now=JULY_2026, env={"NASS_API_KEY": FAKE_SECRET}, git_head=TEST_GIT_HEAD)
    assert not transport.queries and snapshot(tmp_path) == before
