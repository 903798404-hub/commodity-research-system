"""Synthetic clock/API fixtures; host receipt attestation is a separate gate."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from agri_research_agent.soybean_exports import fas, reconciliation as check
from agri_research_agent.soybean_exports.common import sha256_file
from test_fas_data_layer import GIT_HEAD, StaticAdapter, fas_row

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


def checkpoint(**changes):
    value = {"schema_version": check.SCHEMA, "policy_id": "fixture-policy-001", "policy_sha256": "a" * 64,
        "delta_id": "fixture-month-check-001", "baseline_sha256": "b" * 64, "manifest_sha256": "c" * 64,
        "checked_at_utc": "2026-09-05T01:00:00Z", "reconciliation_at_utc": "2026-09-05T00:00:00Z",
        "report_market_years": list(range(2020, 2027)), "retained_report_years": list(range(2020, 2027)),
        "validation_report_sha256": "d" * 64, "publication_receipt_sha256": "e" * 64}
    value.update(changes)
    return value


def test_prior_check_is_bound_to_exact_source_baseline_and_complete_years():
    value = checkpoint()
    assert check.validate(value, now=NOW) == value
    assert check.select("2026-08-07T00:00:00Z", value, baseline_sha256="b" * 64,
        manifest_sha256="c" * 64, retained_years=list(range(2020, 2027)), now=NOW) == value["reconciliation_at_utc"]
    assert check.select("2026-09-08T00:00:00Z", value, baseline_sha256="b" * 64,
        manifest_sha256="c" * 64, retained_years=list(range(2020, 2027)), now=NOW) == "2026-09-08T00:00:00Z"


@pytest.mark.parametrize("mutation", ["extra", "future", "reversed", "naive", "six_years", "old_year_omitted",
    "duplicate_year", "year_order", "year_type", "policy_path", "bad_sha"])
def test_forged_or_incomplete_check_contract_is_rejected(mutation):
    value = deepcopy(checkpoint())
    if mutation == "extra": value["claimed_success"] = True
    elif mutation == "future": value["checked_at_utc"] = "2026-10-01T00:00:00Z"
    elif mutation == "reversed": value["reconciliation_at_utc"] = "2026-09-06T00:00:00Z"
    elif mutation == "naive": value["checked_at_utc"] = "2026-09-05T01:00:00"
    elif mutation == "six_years": value.update(report_market_years=list(range(2021, 2027)), retained_report_years=[2026])
    elif mutation == "old_year_omitted": value["retained_report_years"] = [2010, 2026]
    elif mutation == "duplicate_year": value["report_market_years"] += [2026]
    elif mutation == "year_order": value["report_market_years"].reverse()
    elif mutation == "year_type": value["report_market_years"][0] = "2020"
    elif mutation == "policy_path": value["policy_id"] = "../outside"
    else: value["publication_receipt_sha256"] = "A" * 64
    with pytest.raises(ValueError): check.validate(value, now=NOW)


@pytest.mark.parametrize("binding", ["baseline_sha256", "manifest_sha256", "retained_years"])
def test_old_checkpoint_cannot_be_used_for_changed_business_snapshot(binding):
    options = dict(baseline_sha256="b" * 64, manifest_sha256="c" * 64,
                   retained_years=list(range(2020, 2027)), now=NOW)
    options[binding] = [2026] if binding == "retained_years" else "f" * 64
    with pytest.raises(ValueError): check.select("2026-08-07T00:00:00Z", checkpoint(), **options)


def test_new_month_nochange_then_attested_checkpoint_queries_only_recent_years(tmp_path, monkeypatch):
    clock = [datetime(2026, 8, 7, tzinfo=timezone.utc)]
    monkeypatch.setattr(fas, "utc_now", lambda: clock[0])
    rows = [fas_row(report_year=year, week=f"{year}-07-30T00:00:00") for year in range(2020, 2027)]
    class Adapter(StaticAdapter):
        def __init__(self): super().__init__(rows); self.queries = []
        def fetch_export_sales(self, years=None, *, recent_years=7, retained_years=()):
            self.queries.append(recent_years)
            selected = [row for row in rows if row["_report_market_year_end"] >= 2026-recent_years+1
                        or row["_report_market_year_end"] in retained_years]
            return replace(StaticAdapter(selected).fetch_export_sales(years), fetch_time_utc=fas.iso_utc(clock[0]))
    baseline = tmp_path / "baseline"
    fas.run_fas_pipeline(runtime_root=baseline, adapter=Adapter(), git_head=GIT_HEAD,
                         batch_id="seed", incremental=True)
    paths = fas.fas_paths(baseline, "unused")
    before = {key: paths[key].read_bytes() for key in ("stable", "stable_manifest", "status")}
    clock[0] = datetime(2026, 9, 5, tzinfo=timezone.utc)
    first = Adapter()
    a = fas.run_fas_pipeline(runtime_root=tmp_path / "first", baseline_root=baseline, adapter=first,
        git_head=GIT_HEAD, batch_id="monthly", incremental=True, incremental_transport=True, publish=False)
    m = json.loads((Path(a["candidate_path"]).parent / "manifest.json").read_text(encoding="utf-8"))
    # This models the value a host may supply only after actual independent
    # validation and NO_CHANGE publication. Arbitrary hashes are unit fixtures.
    attested = checkpoint(baseline_sha256=sha256_file(paths["stable"]).lower(),
        manifest_sha256=sha256_file(paths["stable_manifest"]).lower(),
        reconciliation_at_utc=m["fetch_scope"]["reconciliation_at"])
    clock[0] = NOW
    second = Adapter()
    b = fas.run_fas_pipeline(runtime_root=tmp_path / "second", baseline_root=baseline, adapter=second,
        git_head=GIT_HEAD, batch_id="weekly", incremental=True, incremental_transport=True, publish=False,
        reconciliation_check=attested)
    assert first.queries == [7] and second.queries == [2]
    assert b["collection_metrics"]["queried_years"] == [2025, 2026]
    assert not a["revision_count"] and not b["revision_count"]
    assert {key: paths[key].read_bytes() for key in before} == before
    assert not list((tmp_path / "first").rglob("*.parquet"))
    assert not list((tmp_path / "second").rglob("*.parquet"))
