"""Synthetic regression of the July-28 / July-31 incident; no production data."""
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from agri_research_agent.data_sources.lutou.domestic_basis import (
    DomesticBasisEvidenceType, DomesticBasisExtraction, DomesticBasisSourceInventory,
    DomesticBasisSourceRow, load_domestic_basis_catalog,
)
from agri_research_agent.pipelines import lutou_domestic_basis as basis
from agri_research_agent.shared.async_update import FreshnessPolicy
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


MISSING = "market.basis.domestic.china.soybean_oil.central_china.spot"
OLD = date(2026, 7, 28)
START = date(2026, 7, 31)
PREVIOUS = date(2026, 8, 31)
NEW = date(2026, 9, 3)
AS_OF = date(2026, 9, 4)


def policy(*, stale=False, blocking=False):
    # Boundary test value derived from the synthetic case, NEVER a business default.
    return FreshnessPolicy("synthetic-boundary-only-not-production", (AS_OF - OLD).days - int(stale), blocking, True)


def sources(catalog, *, increment=False, include_all=False):
    return tuple(DomesticBasisSourceRow(
        business_date=NEW if increment else OLD if item.series_id == MISSING else PREVIOUS,
        source_product=item.source_product, region=item.region, source_quote_type="现货基差",
        source_contract_code="2701", basis_value=Decimal("101" if increment else "100"),
        source_row_identity=f"synthetic-{item.series_id}-{'new' if increment else 'old'}",
        source_created_at=datetime(2026, 9, 3) if increment else datetime(2026, 7, 28),
    ) for item in catalog.series if not increment or include_all or item.series_id != MISSING)


def extraction(catalog, rows, all_sources, *, start=START, end=NEW):
    return DomesticBasisExtraction(
        records=tuple(rows), evidence_type=DomesticBasisEvidenceType.DETERMINISTIC_TEST_FIXTURE,
        query_identity="synthetic-window", snapshot_identity="synthetic-snapshot",
        extracted_at=datetime(2026, 9, 4, tzinfo=timezone.utc), source_min_date=start, source_max_date=end,
        source_inventory=tuple(DomesticBasisSourceInventory(
            item.source_product, item.region,
            max((row.business_date for row in all_sources if (row.source_product, row.region) == (item.source_product, item.region)), default=None),
            sum((row.source_product, row.region) == (item.source_product, item.region) and start <= row.business_date <= end for row in all_sources),
        ) for item in catalog.series),
        inventory_query_identity="synthetic-independent-source-inventory", inventory_plan_estimated_rows=len(all_sources),
    )


def fixture(*, include_all=False, stale=False):
    catalog = replace(load_domestic_basis_catalog(), freshness_policy=policy(stale=stale))
    old = sources(catalog)
    seed = extraction(catalog, old, old, start=OLD, end=PREVIOUS)
    current, _ = basis.build_canonical_table(basis.build_standard_table(seed, catalog), catalog)
    new = sources(catalog, increment=True, include_all=include_all)
    # Source inventory includes old rows too. The window contains previous rows
    # for the 20 active series, plus new rows; central/soybean-oil is outside it.
    all_sources = old + new
    rows = tuple(row for row in all_sources if START <= row.business_date <= NEW)
    return catalog, current, extraction(catalog, rows, all_sources)


def assemble(catalog, current, source):
    return basis.assemble_domestic_basis_candidate(current=current, extraction=source, catalog=catalog, as_of_date=AS_OF)


@pytest.mark.parametrize("include_all,stale", [
    (True, False), (False, False), (False, True),
])
def test_incident_fixture_complete_state_and_statuses(tmp_path, include_all, stale):
    catalog, current, source = fixture(include_all=include_all, stale=stale)
    standard = basis.build_standard_table(source, catalog)
    if not include_all:
        with pytest.raises(basis.DomesticBasisPipelineError, match="Candidate coverage is incomplete"):
            basis.validate_candidate(standard, catalog)
    state = assemble(catalog, current, source)
    report = state.update_report
    assert report["summary"] == {
        "TOTAL_REQUIRED": 21,
        "coverage": {"PRESENT": 21, "MISSING": 0, "ERROR": 0},
        "updates": {"UPDATED": 21 if include_all else 20, "NO_CHANGE": 0 if include_all else 1, "ERROR": 0},
        "freshness": {"FRESH": 20 if stale else 21, "STALE": 1 if stale else 0, "UNASSESSED": 0},
    }
    assert report["identity_coverage"] == {"status": "PASS", "expected_count": 21, "actual_required_count": 21, "missing": [], "unexpected": []}
    assert not report["incremental_window_defines_completeness"]
    central = next(row for row in report["series"] if row["identity"] == MISSING)
    if not include_all:
        assert central["previous_latest_date"] == central["source_latest_date"] == central["next_latest_date"] == OLD.isoformat()
        assert central["new_row_count"] == 0
        assert central["coverage_status"] == "PRESENT"
        assert central["update_status"] == "NO_CHANGE"
        assert central["freshness_status"] == ("STALE" if stale else "FRESH")
        assert [row for row in state.next_state.to_pylist() if row["series_id"] == MISSING] == [row for row in current.to_pylist() if row["series_id"] == MISSING]
    assert all(row["next_latest_date"] == NEW.isoformat() for row in report["series"] if row["update_status"] == "UPDATED")
    (tmp_path / "synthetic-candidate-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


def test_missing_valid_source_is_error_not_masked_by_old_current():
    catalog, current, source = fixture(include_all=True)
    dropped = replace(source, records=tuple(row for row in source.records if not (row.source_product == "大豆油" and row.region == "华中")))
    with pytest.raises(basis.DomesticBasisPipelineError) as caught:
        assemble(catalog, current, dropped)
    row = next(item for item in caught.value.update_report["series"] if item["identity"] == MISSING)
    assert row["update_status"] == "ERROR"
    assert "SOURCE_PRESENT_EXTRACTION_DROPPED" in row["reason"]
    assert caught.value.update_report["summary"]["updates"]["NO_CHANGE"] == 0


def test_mapping_removal_is_error_even_if_catalog_itself_shrinks():
    catalog, current, source = fixture()
    catalog = replace(catalog, series=tuple(item for item in catalog.series if item.series_id != MISSING))
    with pytest.raises(basis.DomesticBasisPipelineError) as caught:
        assemble(catalog, current, source)
    assert caught.value.update_report["summary"]["TOTAL_REQUIRED"] == 21
    row = next(item for item in caught.value.update_report["series"] if item["identity"] == MISSING)
    assert row["update_status"] == "ERROR" and "MAPPING" in row["reason"]


@pytest.mark.parametrize("layer", ["normalization_drop", "normalization_invalid", "valid_filter", "canonical_drop", "merge_drop", "timestamp_fabrication", "capture_fabrication"])
def test_faults_fail_closed(monkeypatch, layer):
    catalog, current, source = fixture(include_all=True)
    if layer == "normalization_invalid":
        source = replace(source, records=(replace(source.records[0], basis_value=None), *source.records[1:]))
    elif layer in {"normalization_drop", "valid_filter"}:
        original = basis.build_standard_table
        def broken(*args, **kwargs):
            rows = original(*args, **kwargs).to_pylist()
            if layer == "normalization_drop":
                rows.pop()
            else:
                rows[0]["is_usable"] = False
            return pa.Table.from_pylist(rows, schema=basis.STANDARD_SCHEMA)
        monkeypatch.setattr(basis, "build_standard_table", broken)
    elif layer == "canonical_drop":
        original = basis.build_canonical_table
        def broken(*args, **kwargs):
            table, report = original(*args, **kwargs)
            return table.slice(1), report
        monkeypatch.setattr(basis, "build_canonical_table", broken)
    else:
        original = basis._merge_current
        def broken(*args):
            rows = original(*args).to_pylist()
            if layer == "merge_drop":
                rows = [row for row in rows if row["series_id"] != MISSING]
            elif layer == "capture_fabrication":
                rows[0]["captured_at"] = datetime(2099, 1, 1, tzinfo=timezone.utc)
            else:
                rows[0]["business_date"] = AS_OF
            return pa.Table.from_pylist(rows, schema=basis.CANONICAL_SCHEMA)
        monkeypatch.setattr(basis, "_merge_current", broken)
    with pytest.raises(basis.DomesticBasisPipelineError) as caught:
        assemble(catalog, current, source)
    assert caught.value.update_report["summary"]["updates"]["ERROR"] > 0
    assert not caught.value.update_report["promotion_allowed"]
    if layer == "merge_drop":
        assert caught.value.update_report["identity_coverage"]["actual_required_count"] == 20
        assert next(row for row in caught.value.update_report["series"] if row["identity"] == MISSING)["coverage_status"] == "MISSING"


def test_completely_empty_increment_preserves_exact_current():
    catalog, current, _ = fixture()
    old = sources(catalog)
    empty = extraction(catalog, (), old, start=NEW, end=NEW)
    state = assemble(catalog, current, empty)
    assert state.standard.num_rows == 0
    assert state.next_state.equals(current)
    assert state.update_report["summary"]["updates"]["NO_CHANGE"] == 21


def test_policy_filter_is_audited_not_confused_with_normalization_error():
    catalog, current, source = fixture()
    filtered = replace(source, records=tuple(replace(row, source_quote_type="一口价") if row.business_date == NEW else row for row in source.records))
    state = assemble(catalog, current, filtered)
    assert state.update_report["summary"]["updates"]["NO_CHANGE"] == 21
    assert state.candidate_quality["retained_exceptions_by_reason"]["NON_SPOT_QUOTE_TYPE"] == 20
    assert state.next_state.equals(current)


def test_domestic_basis_unassessed_freshness_is_non_blocking(tmp_path):
    catalog, current, source = fixture()
    catalog = replace(catalog, freshness_policy=load_domestic_basis_catalog().freshness_policy)
    state = assemble(catalog, current, source)
    assert state.update_report["identity_coverage"]["status"] == "PASS"
    report = state.update_report
    assert report["summary"] == {
        "TOTAL_REQUIRED": 21,
        "coverage": {"PRESENT": 21, "MISSING": 0, "ERROR": 0},
        "updates": {"UPDATED": 20, "NO_CHANGE": 1, "ERROR": 0},
        "freshness": {"FRESH": 0, "STALE": 0, "UNASSESSED": 21},
    }
    central = next(row for row in report["series"] if row["identity"] == MISSING)
    assert central["coverage_status"] == "PRESENT" and central["update_status"] == "NO_CHANGE"
    assert central["freshness_status"] == "UNASSESSED" and central["threshold"] is None
    assert central["previous_latest_date"] == central["source_latest_date"] == central["next_latest_date"] == OLD.isoformat()
    assert central["new_rows"] == 0 and central["age_days"] == (AS_OF - OLD).days
    assert report["blocking_reasons"] == [] and report["promotion_allowed"]
    assert report["policy"]["stale_is_blocking"] is False
    (tmp_path / "dimensions-fixture-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


@pytest.mark.parametrize("fault", ["duplicate", "source_unknown", "new_outside_window", "schema", "key_schema", "inventory_missing"])
def test_additional_integrity_guards(fault):
    catalog, current, source = fixture()
    if fault == "duplicate":
        row = source.records[0]
        source = replace(source, records=(*source.records, row), source_inventory=tuple(
            replace(item, window_row_count=item.window_row_count + 1) if (item.source_product, item.region) == (row.source_product, row.region) else item for item in source.source_inventory))
    elif fault in {"source_unknown", "new_outside_window"}:
        source = replace(source, source_inventory=tuple(
            replace(item, source_latest_date=None if fault == "source_unknown" else date(2026, 7, 29))
            if item.source_product == "大豆油" and item.region == "华中" else item for item in source.source_inventory))
    elif fault in {"schema", "key_schema"}:
        current = current.drop(["unit" if fault == "schema" else "business_date"])
    else:
        source = replace(source, inventory_query_identity=None)
    with pytest.raises(basis.DomesticBasisPipelineError) as caught:
        assemble(catalog, current, source)
    assert caught.value.update_report["summary"]["updates"]["ERROR"] > 0
    assert not caught.value.update_report["promotion_allowed"]


@pytest.fixture
def runtime(tmp_path):
    (tmp_path / ".market-data-runtime.json").write_text(json.dumps({
        "schema_version": 1, "runtime_id": "async-fixture", "classification": "isolated-dev",
        "module_id": "international-spread", "created_at": "2026-09-04T00:00:00Z",
    }), encoding="utf-8")
    return RuntimeContext(RuntimeMode.ISOLATED_DEV, "international-spread", tmp_path)


def mapping_file(tmp_path, approved=True):
    payload = yaml.safe_load(Path("02_configs/lutou_domestic_basis.yaml").read_text(encoding="utf-8"))
    if approved:
        payload["freshness_policy"] = asdict(policy())
    path = tmp_path / ("synthetic-approved-mapping.yaml" if approved else "pending-mapping.yaml")
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


class FakeAdapter:
    def __init__(self, rows):
        self.rows = rows

    def date_bounds(self):
        return min(row.business_date for row in self.rows), max(row.business_date for row in self.rows)

    def extract(self, *, catalog, start_date, end_date):
        value = extraction(catalog, tuple(row for row in self.rows if start_date <= row.business_date <= end_date), self.rows, start=start_date, end=end_date)
        return replace(value, evidence_type=DomesticBasisEvidenceType.LIVE_DATABASE, plan_estimated_rows=len(self.rows),
                       connection_proof={"transaction_read_only": True, "write_privileges": []}, schema_proof={"column_count": 19, "date_indexed": True})


@pytest.mark.parametrize("candidate_only", [True, False])
def test_unassessed_policy_allows_fixture_refresh_but_candidate_only_never_promotes(runtime, tmp_path, candidate_only):
    catalog = load_domestic_basis_catalog()
    old, new = sources(catalog), sources(catalog, increment=True)
    mapping = mapping_file(tmp_path)
    basis.run_domestic_basis_live(runtime=runtime, run_id="seed", adapter=FakeAdapter(old), mapping_path=mapping)
    root = runtime.runtime_root / "public-market-data" / "lutou-domestic-basis"
    before = (root / "current.json").read_bytes()
    pending = mapping_file(tmp_path, approved=False)
    args = dict(runtime=runtime, run_id="pending", adapter=FakeAdapter(old + new), mapping_path=pending, candidate_only=candidate_only)
    result = basis.run_domestic_basis_live(**args)
    assert result.promoted is (not candidate_only)
    assert result.update_summary["summary"]["updates"]["UPDATED"] == 20
    assert len(set(pq.read_table(result.candidate_directory / "next-state.parquet")["series_id"].to_pylist())) == 21
    assert ((root / "current.json").read_bytes() == before) is candidate_only
    report = json.loads((root / "async-update-reports/pending/manifest.json").read_text(encoding="utf-8"))
    assert report["freshness_assessment_available"] is False and report["identity_coverage"]["status"] == "PASS"
    assert report["summary"]["freshness"]["UNASSESSED"] == 21
    assert report["promotion_allowed"] is True
    assert (root / "canonical/pending").exists() is (not candidate_only)


def test_real_pilot_error_triggers_existing_multi_provider_pointer_rollback(runtime, tmp_path):
    from agri_research_agent.pipelines.public_data_refresh import CurrentIdentity, RefreshResult, run_unified_refresh
    catalog, current, source = fixture(include_all=True)
    dropped = replace(source, records=source.records[:-1])

    @dataclass
    class Adapter:
        name: str
        fails: bool = False

        @property
        def pointer(self):
            return runtime.runtime_root / "public-market-data" / self.name / "current.json"

        def current_identity(self):
            value = json.loads(self.pointer.read_text())
            return CurrentIdentity(value["release_id"], value["manifest_sha256"], {})

        def preflight(self):
            return {"read_only": True}

        def refresh(self):
            if self.fails:
                assemble(catalog, current, dropped)
            self.pointer.write_text(json.dumps({"release_id": "new", "manifest_sha256": "b" * 64}))
            return RefreshResult(True, {})

    adapters = [Adapter("tankan"), Adapter("lutou-domestic-basis", True)]
    for item in adapters:
        item.pointer.parent.mkdir(parents=True)
        item.pointer.write_text(json.dumps({"release_id": "old", "manifest_sha256": "a" * 64}))
    before = [item.pointer.read_bytes() for item in adapters]
    result = run_unified_refresh(runtime=runtime, run_id="async-rollback", adapters=adapters, require_all_sources=True)
    assert result.manifest["transaction"]["rollback"] == "PASS"
    assert [item.pointer.read_bytes() for item in adapters] == before
    assert result.manifest["root_failure"]["exception_type"] == "DomesticBasisPipelineError"


@pytest.mark.parametrize("assessed", [False, True])
def test_domestic_basis_no_change_with_unassessed_or_stale_never_blocks(runtime, tmp_path, assessed):
    catalog = load_domestic_basis_catalog()
    mapping = mapping_file(tmp_path, approved=False)
    if assessed:
        payload = yaml.safe_load(mapping.read_text(encoding="utf-8"))
        # Test-only boundary: all historic fixture dates are older than as-of.
        payload["freshness_policy"]["freshness_threshold"] = 0
        payload["freshness_policy"]["threshold_approved"] = True
        mapping.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    adapter = FakeAdapter(sources(catalog))
    basis.run_domestic_basis_live(runtime=runtime, run_id="seed-no-change", adapter=adapter, mapping_path=mapping)
    pointer = runtime.runtime_root / "public-market-data/lutou-domestic-basis/current.json"
    before = pointer.read_bytes()
    result = basis.run_domestic_basis_live(runtime=runtime, run_id="repeat-no-change", adapter=adapter, mapping_path=mapping)
    assert not result.promoted and pointer.read_bytes() == before
    assert result.update_summary["summary"]["updates"] == {"UPDATED": 0, "NO_CHANGE": 21, "ERROR": 0}
    assert result.update_summary["summary"]["freshness"]["STALE" if assessed else "UNASSESSED"] == 21
    assert result.update_summary["dataset_status"] == ("WARNING" if assessed else "NO_CHANGE")
    assert result.update_summary["promotion_allowed"]


@pytest.mark.parametrize("broken_mapping", [False, True])
def test_preassembly_error_report_does_not_claim_missing_dataset(runtime, tmp_path, broken_mapping):
    class FailedAdapter:
        def date_bounds(self):
            raise RuntimeError("synthetic-source-failure")
    mapping = mapping_file(tmp_path)
    if broken_mapping:
        mapping.write_text("not-a-catalog", encoding="utf-8")
    with pytest.raises((RuntimeError, ValueError)):
        basis.run_domestic_basis_live(runtime=runtime, run_id="before-candidate", adapter=FailedAdapter(), mapping_path=mapping)
    report = json.loads((runtime.runtime_root / "public-market-data/lutou-domestic-basis/async-update-reports/before-candidate/manifest.json").read_text(encoding="utf-8"))
    assert report["dataset_status"] == "FAILED"
    assert report["identity_coverage"]["status"] == "NOT_EVALUATED"
    assert report["identity_coverage"]["actual_required_count"] is None
    assert report["summary"]["updates"]["ERROR"] == (0 if broken_mapping else 21)
    assert report["summary"]["coverage"]["ERROR"] == (0 if broken_mapping else 21)
    assert all(item["reason"].startswith("REFRESH_FAILED_") for item in report["series"])
    assert not (runtime.runtime_root / "public-market-data/lutou-domestic-basis/current.json").exists()


@pytest.mark.parametrize("boundary", ["assembly", "promotion"])
@pytest.mark.parametrize("dimension,first,second", [
    ("coverage", "PRESENT", "MISSING"),
    ("updates", "UPDATED", "NO_CHANGE"),
    ("freshness", "UNASSESSED", "FRESH"),
])
def test_summary_mismatch_blocks_before_writes(monkeypatch, tmp_path, boundary, dimension, first, second):
    catalog, current, source = fixture()
    catalog = replace(catalog, freshness_policy=load_domestic_basis_catalog().freshness_policy)
    original = basis.evaluate_update

    def corrupt(**kwargs):
        value = original(**kwargs)
        value["summary"][dimension][first] -= 1
        value["summary"][dimension][second] += 1
        return value

    def forbid_write(*args, **kwargs):
        pytest.fail("inconsistent summary reached runtime write")

    monkeypatch.setattr(basis, "assert_runtime_write", forbid_write)
    if boundary == "assembly":
        monkeypatch.setattr(basis, "evaluate_update", corrupt)
        with pytest.raises(basis.DomesticBasisPipelineError, match="ASYNC_SUMMARY_SERIES_MISMATCH"):
            assemble(catalog, current, source)
    else:
        value = assemble(catalog, current, source).update_report
        value["summary"][dimension][first] -= 1
        value["summary"][dimension][second] += 1
        monkeypatch.setattr(basis, "_read_json", lambda _: {"quality": {"async_update": value}})
        with pytest.raises(basis.DomesticBasisPipelineError, match="ASYNC_SUMMARY_SERIES_MISMATCH"):
            basis.promote_domestic_basis(runtime=None, canonical_directory=tmp_path,
                                         mapping_path=Path("02_configs/lutou_domestic_basis.yaml"))
