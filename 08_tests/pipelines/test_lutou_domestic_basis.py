from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest
import yaml

from agri_research_agent.data_sources.lutou.domestic_basis import (
    DomesticBasisEvidenceType,
    DomesticBasisExtraction,
    DomesticBasisSourceRow,
    load_domestic_basis_catalog,
)
from agri_research_agent.pipelines.lutou_domestic_basis import (
    CANONICAL_STABLE_KEY,
    DomesticBasisPipelineError,
    build_standard_table,
    compare_legacy_parity,
    load_domestic_basis_current,
    promote_domestic_basis,
    run_domestic_basis_live,
    run_domestic_basis_offline_fixture,
    simulate_canonical_policy,
    validate_candidate,
)
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


DAY = date(2026, 8, 4)


@pytest.fixture
def runtime(tmp_path: Path) -> RuntimeContext:
    (tmp_path / ".market-data-runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_id": "goal-d3a-fixture",
                "classification": "isolated-dev",
                "module_id": "international-spread",
                "created_at": "2026-08-20T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return RuntimeContext(RuntimeMode.ISOLATED_DEV, "international-spread", tmp_path)


def _source_row(
    source_product: str,
    region: str,
    index: int,
    *,
    value: Decimal = Decimal("100"),
    contract: str = "2609",
    identity: str | None = None,
) -> DomesticBasisSourceRow:
    return DomesticBasisSourceRow(
        business_date=DAY,
        source_product=source_product,
        region=region,
        source_quote_type="现货基差",
        source_contract_code=contract,
        basis_value=value,
        source_row_identity=identity or f"fixture-row-{index}",
        factory=f"factory-{index}",
        article_id=f"article-{index}",
        delivery_month_native="8月",
    )


def _extraction(*, extra: tuple[DomesticBasisSourceRow, ...] = ()) -> DomesticBasisExtraction:
    catalog = load_domestic_basis_catalog()
    rows = tuple(
        _source_row(item.source_product, item.region, index)
        for index, item in enumerate(catalog.series)
    )
    return DomesticBasisExtraction(
        records=(*rows, *extra),
        evidence_type=DomesticBasisEvidenceType.DETERMINISTIC_TEST_FIXTURE,
        query_identity="deterministic-fixture-query-v1",
        snapshot_identity="deterministic-fixture-snapshot-v1",
        extracted_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )


def _live_mapping(tmp_path: Path) -> Path:
    payload = yaml.safe_load(Path("02_configs/lutou_domestic_basis.yaml").read_text(encoding="utf-8"))
    payload["mapping_version"] = "domestic-basis-live-confirmed-test/1"
    payload["source_contract"]["database_schema"] = "油脂油料价格"
    payload["source_contract"]["table_status"] = "LIVE_CONFIRMED"
    payload["source_contract"]["query_identity_status"] = "LIVE_CONFIRMED"
    payload["source_contract"]["snapshot_identity_status"] = "LIVE_CONFIRMED"
    for item in payload["series"]:
        item["live_status"] = "LIVE_CONFIRMED"
        item["expected_source_status"] = "LIVE_CONFIRMED"
    path = tmp_path / "mapping.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _pending_mapping(tmp_path: Path) -> Path:
    payload = yaml.safe_load(Path("02_configs/lutou_domestic_basis.yaml").read_text(encoding="utf-8"))
    payload["mapping_version"] = "domestic-basis-pending-test/1"
    payload["source_contract"]["database_schema"] = None
    payload["source_contract"]["table_status"] = "EXPECTED_FROM_LEGACY_CONTRACT"
    payload["source_contract"]["query_identity_status"] = "LIVE_CONFIRMATION_PENDING"
    payload["source_contract"]["snapshot_identity_status"] = "LIVE_CONFIRMATION_PENDING"
    for item in payload["series"]:
        item["live_status"] = "LIVE_CONFIRMATION_PENDING"
        item["expected_source_status"] = "EXPECTED_FROM_LEGACY_CONTRACT"
    path = tmp_path / "pending-mapping.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


class FakeLiveAdapter:
    def __init__(self, records: tuple[DomesticBasisSourceRow, ...]) -> None:
        self.records = records
        self.windows: list[tuple[date, date]] = []

    def date_bounds(self) -> tuple[date, date]:
        return date(2026, 7, 1), date(2026, 8, 19)

    def extract(self, *, catalog, start_date: date, end_date: date) -> DomesticBasisExtraction:
        self.windows.append((start_date, end_date))
        return DomesticBasisExtraction(
            records=tuple(row for row in self.records if start_date <= row.business_date <= end_date),
            evidence_type=DomesticBasisEvidenceType.LIVE_DATABASE,
            query_identity="live-query-sha", snapshot_identity="live-snapshot-sha",
            extracted_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
            source_min_date=start_date, source_max_date=end_date,
            plan_estimated_rows=len(self.records),
            connection_proof={"transaction_read_only": True, "write_privileges": []},
            schema_proof={"column_count": 19, "index_entry_count": 1, "date_indexed": True},
        )
def test_standard_candidate_is_source_preserving_complete_and_has_no_fill() -> None:
    catalog = load_domestic_basis_catalog()
    extraction = _extraction(
        extra=(
            _source_row("大豆油", "华东", 100, value=Decimal("-20")),
            _source_row("豆粕", "华南", 101, value=Decimal("0")),
        )
    )
    standard = build_standard_table(extraction, catalog)
    quality = validate_candidate(standard, catalog)
    assert quality["quality_status"] == "PASS"
    assert quality["series_count"] == quality["expected_series_count"] == 21
    assert set(standard["evidence_type"].to_pylist()) == {"DETERMINISTIC_TEST_FIXTURE"}
    assert set(standard["live_status"].to_pylist()) == {"LIVE_CONFIRMED"}
    assert Decimal("-20.0000") in standard["basis_value"].to_pylist()
    assert Decimal("0.0000") in standard["basis_value"].to_pylist()
    assert set(standard["business_date"].to_pylist()) == {DAY}
    assert set(standard["currency"].to_pylist()) == {"CNY"}
    assert set(standard["unit"].to_pylist()) == {"CNY/metric_tonne"}


def test_canonical_policy_selects_nearest_contract_and_median_without_collision() -> None:
    catalog = load_domestic_basis_catalog()
    first = catalog.series[0]
    extraction = _extraction(
        extra=(
            _source_row(first.source_product, first.region, 100, value=Decimal("0")),
            _source_row(first.source_product, first.region, 101, value=Decimal("200")),
            _source_row(first.source_product, first.region, 102, value=Decimal("999"), contract="2701"),
        )
    )
    standard = build_standard_table(extraction, catalog)
    canonical, report = simulate_canonical_policy(standard, catalog)
    selected = next(row for row in canonical.to_pylist() if row["series_id"] == first.series_id)
    assert selected["value"] == Decimal("100.0000")
    assert selected["underlying_futures_reference"] == "DCE:P:2026-09"
    assert selected["source_row_count"] == 3
    assert selected["far_contract_row_count"] == 1
    assert selected["aggregation_method"] == "median_of_source_quotes_at_nearest_nonexpired_contract"
    assert report["quality_status"] == "PASS"
    assert report["policy_mode"] == "LIVE"
    assert report["production_authorized"] is False
    assert report["stable_key_duplicate_count"] == 0
    assert report["logical_collision_count"] == 0
    assert report["far_contract_row_count"] == 1
    assert len({tuple(row[key] for key in CANONICAL_STABLE_KEY) for row in canonical.to_pylist()}) == canonical.num_rows


def test_candidate_rejects_incomplete_coverage_and_source_identity_collision() -> None:
    catalog = load_domestic_basis_catalog()
    incomplete = DomesticBasisExtraction(
        records=_extraction().records[:-1],
        evidence_type=DomesticBasisEvidenceType.DETERMINISTIC_TEST_FIXTURE,
        query_identity="query",
        snapshot_identity="snapshot",
        extracted_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    with pytest.raises(DomesticBasisPipelineError, match="coverage"):
        validate_candidate(build_standard_table(incomplete, catalog), catalog)

    first = catalog.series[0]
    duplicate = _source_row(first.source_product, first.region, 999, identity="fixture-row-0")
    with pytest.raises(DomesticBasisPipelineError, match="stable key"):
        validate_candidate(build_standard_table(_extraction(extra=(duplicate,)), catalog), catalog)

    identity_sha = "a" * 64
    content_collision = DomesticBasisSourceRow(
        DAY, first.source_product, first.region, "现货基差", "2609", Decimal("100"),
        "same-identity:2", source_identity_sha256=identity_sha, source_row_sha256="b" * 64,
    )
    original = DomesticBasisSourceRow(
        DAY, first.source_product, first.region, "现货基差", "2609", Decimal("100"),
        "same-identity:1", source_identity_sha256=identity_sha, source_row_sha256="c" * 64,
    )
    extraction = _extraction(extra=(original, content_collision))
    with pytest.raises(DomesticBasisPipelineError, match="identity collision"):
        validate_candidate(build_standard_table(extraction, catalog), catalog)


def test_expired_underlying_contract_is_retained_as_candidate_exception() -> None:
    catalog = load_domestic_basis_catalog()
    first = catalog.series[0]
    extraction = DomesticBasisExtraction(
        records=(_source_row(first.source_product, first.region, 1, contract="2607"),),
        evidence_type=DomesticBasisEvidenceType.DETERMINISTIC_TEST_FIXTURE,
        query_identity="query",
        snapshot_identity="snapshot",
        extracted_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    standard = build_standard_table(extraction, catalog)
    assert standard.num_rows == 1
    row = standard.to_pylist()[0]
    assert row["is_usable"] is False
    assert row["quality_status"] == "RETAINED_EXCEPTION"
    assert row["exception_reason"] == "CONTRACT_EXPIRED"
    assert row["canonical_selection_status"] == "NOT_ELIGIBLE_FOR_CANONICAL"


def test_candidate_retains_null_and_far_rows_and_preserves_source_prices() -> None:
    catalog = load_domestic_basis_catalog()
    first = catalog.series[0]
    extra = (
        DomesticBasisSourceRow(
            DAY, first.source_product, first.region, "现货基差", "2609", None,
            "null-row", raw_basis_value=None, cash_price=Decimal("8123"),
            futures_price=Decimal("7999"),
        ),
        _source_row(first.source_product, first.region, 202, value=Decimal("333"), contract="2701"),
    )
    standard = build_standard_table(_extraction(extra=extra), catalog)
    gate = validate_candidate(standard, catalog)
    null_row = next(row for row in standard.to_pylist() if row["source_row_identity"] == "null-row")
    assert null_row["basis_value"] is None
    assert null_row["cash_price"] == Decimal("8123.0000")
    assert null_row["futures_price"] == Decimal("7999.0000")
    assert null_row["exception_reason"] == "BASIS_NULL_OR_NONNUMERIC"
    assert gate["retained_exception_count"] == 1
    assert gate["far_contract_row_count"] == 1
    canonical, report = simulate_canonical_policy(standard, catalog)
    selected = next(row for row in canonical.to_pylist() if row["series_id"] == first.series_id)
    assert selected["cash_price"] is None
    assert selected["futures_price"] is None
    assert report["cash_futures_policy"] == "legacy_compatible_null"


def test_live_full_then_31_day_incremental_is_idempotent(runtime: RuntimeContext, tmp_path: Path) -> None:
    mapping = _live_mapping(tmp_path)
    catalog = load_domestic_basis_catalog(mapping)
    records = tuple(_source_row(item.source_product, item.region, index) for index, item in enumerate(catalog.series))
    adapter = FakeLiveAdapter(records)
    full = run_domestic_basis_live(
        runtime=runtime, run_id="d3a-live-full", adapter=adapter,
        mapping_path=mapping, mode="full",
    )
    pointer = (runtime.runtime_root / "public-market-data" / "lutou-domestic-basis" / "current.json").read_bytes()
    repeat = run_domestic_basis_live(
        runtime=runtime, run_id="d3a-live-repeat", adapter=adapter,
        mapping_path=mapping, mode="incremental",
    )
    assert full.promoted is True
    assert repeat.promoted is False
    assert repeat.current.release_id == full.current.release_id
    assert adapter.windows == [
        (date(2026, 7, 1), date(2026, 8, 19)),
        (date(2026, 7, 19), date(2026, 8, 19)),
    ]
    assert (runtime.runtime_root / "public-market-data" / "lutou-domestic-basis" / "current.json").read_bytes() == pointer


def test_interrupted_promotion_preserves_previous_current(runtime: RuntimeContext, tmp_path: Path) -> None:
    mapping = _live_mapping(tmp_path)
    catalog = load_domestic_basis_catalog(mapping)
    original = tuple(_source_row(item.source_product, item.region, index) for index, item in enumerate(catalog.series))
    run_domestic_basis_live(runtime=runtime, run_id="seed", adapter=FakeLiveAdapter(original), mapping_path=mapping)
    pointer_path = runtime.runtime_root / "public-market-data" / "lutou-domestic-basis" / "current.json"
    before = pointer_path.read_bytes()
    revised = list(original)
    revised[0] = _source_row(catalog.series[0].source_product, catalog.series[0].region, 0, value=Decimal("101"))
    def fail(stage: str) -> None:
        if stage == "release_sealed_before_pointer":
            raise RuntimeError("injected interruption")
    with pytest.raises(RuntimeError, match="injected"):
        run_domestic_basis_live(
            runtime=runtime, run_id="interrupted", adapter=FakeLiveAdapter(tuple(revised)),
            mapping_path=mapping, failure_hook=fail,
        )
    assert pointer_path.read_bytes() == before


def test_incremental_query_failure_preserves_previous_current(runtime: RuntimeContext, tmp_path: Path) -> None:
    mapping = _live_mapping(tmp_path)
    catalog = load_domestic_basis_catalog(mapping)
    records = tuple(_source_row(item.source_product, item.region, index) for index, item in enumerate(catalog.series))
    run_domestic_basis_live(runtime=runtime, run_id="query-seed", adapter=FakeLiveAdapter(records), mapping_path=mapping)
    pointer_path = runtime.runtime_root / "public-market-data" / "lutou-domestic-basis" / "current.json"
    before = pointer_path.read_bytes()

    class FailingAdapter(FakeLiveAdapter):
        def extract(self, **kwargs):
            raise RuntimeError("injected query failure")

    with pytest.raises(RuntimeError, match="query failure"):
        run_domestic_basis_live(
            runtime=runtime, run_id="query-failed", adapter=FailingAdapter(records),
            mapping_path=mapping, mode="incremental",
        )
    assert pointer_path.read_bytes() == before


def test_missing_live_read_only_proof_blocks_before_current(runtime: RuntimeContext, tmp_path: Path) -> None:
    mapping = _live_mapping(tmp_path)
    catalog = load_domestic_basis_catalog(mapping)
    records = tuple(_source_row(item.source_product, item.region, index) for index, item in enumerate(catalog.series))

    class UnprovenAdapter(FakeLiveAdapter):
        def extract(self, *, catalog, start_date, end_date):
            extraction = super().extract(catalog=catalog, start_date=start_date, end_date=end_date)
            return DomesticBasisExtraction(
                records=extraction.records, evidence_type=extraction.evidence_type,
                query_identity=extraction.query_identity, snapshot_identity=extraction.snapshot_identity,
                extracted_at=extraction.extracted_at, source_min_date=start_date,
                source_max_date=end_date, plan_estimated_rows=extraction.plan_estimated_rows,
                connection_proof=None, schema_proof=extraction.schema_proof,
            )

    with pytest.raises(DomesticBasisPipelineError, match="read-only proof"):
        run_domestic_basis_live(
            runtime=runtime, run_id="unproven", adapter=UnprovenAdapter(records),
            mapping_path=mapping,
        )
    assert not (runtime.runtime_root / "public-market-data" / "lutou-domestic-basis" / "current.json").exists()


def test_invalid_canonical_manifest_file_identity_preserves_current(runtime: RuntimeContext, tmp_path: Path) -> None:
    mapping = _live_mapping(tmp_path)
    catalog = load_domestic_basis_catalog(mapping)
    records = tuple(_source_row(item.source_product, item.region, index) for index, item in enumerate(catalog.series))
    result = run_domestic_basis_live(runtime=runtime, run_id="manifest-seed", adapter=FakeLiveAdapter(records), mapping_path=mapping)
    assert result.canonical_directory is not None
    pointer_path = runtime.runtime_root / "public-market-data" / "lutou-domestic-basis" / "current.json"
    before = pointer_path.read_bytes()
    data_path = result.canonical_directory / "observations.parquet"
    data_path.write_bytes(data_path.read_bytes() + b"tampered")
    with pytest.raises(DomesticBasisPipelineError, match="file identity"):
        promote_domestic_basis(
            runtime=runtime, canonical_directory=result.canonical_directory,
            mapping_path=mapping,
        )
    assert pointer_path.read_bytes() == before


def test_legacy_parity_reports_all_21_series_without_value_differences() -> None:
    catalog = load_domestic_basis_catalog()
    canonical, _ = simulate_canonical_policy(build_standard_table(_extraction(), catalog), catalog)
    legacy = []
    for row in canonical.to_pylist():
        contract = row["underlying_futures_reference"].split(":")[-1].replace("-", "")[2:]
        legacy.append({
            "date": row["business_date"], "commodity": row["consumer_product"],
            "region": row["location"], "futures_contract": contract,
            "basis": row["value"], "cash_price": None, "futures_price": None,
        })
    report = compare_legacy_parity(canonical, legacy, catalog)
    assert len(report) == 21
    assert sum(item["basis_differences"] for item in report) == 0
    assert sum(item["cash_price_differences"] for item in report) == 0
    assert sum(item["futures_price_differences"] for item in report) == 0
    assert sum(item["legacy_only"] + item["public_only"] for item in report) == 0


def test_offline_fixture_seals_candidate_and_simulation_but_never_current(
    runtime: RuntimeContext, tmp_path: Path,
) -> None:
    mapping = _pending_mapping(tmp_path)
    result = run_domestic_basis_offline_fixture(
        runtime=runtime,
        run_id="d3a-fixture",
        extraction=_extraction(),
        mapping_path=mapping,
    )
    assert result.promoted is False
    assert result.candidate_manifest["evidence_type"] == "DETERMINISTIC_TEST_FIXTURE"
    assert result.candidate_manifest["promotion_authorized"] is False
    assert result.candidate_manifest["live_verification_status"] == "LIVE_CONFIRMATION_PENDING"
    assert result.canonical_simulation_manifest["production_authorized"] is False
    assert result.canonical_simulation_manifest["series_count"] == 21
    assert pq.read_table(result.candidate_directory / "standard.parquet").num_rows == 21
    public_root = runtime.runtime_root / "public-market-data" / "lutou-domestic-basis"
    assert not (public_root / "current.json").exists()
    assert load_domestic_basis_current(public_root) is None


def test_pending_mapping_blocks_atomic_promotion_and_preserves_current_pointer(
    runtime: RuntimeContext, tmp_path: Path,
) -> None:
    mapping = _pending_mapping(tmp_path)
    public_root = runtime.runtime_root / "public-market-data" / "lutou-domestic-basis"
    public_root.mkdir(parents=True)
    pointer = public_root / "current.json"
    pointer.write_bytes(b'{"sentinel":"unchanged"}\n')
    before = pointer.read_bytes()
    with pytest.raises(DomesticBasisPipelineError, match="LIVE_CONFIRMED"):
        promote_domestic_basis(
            runtime=runtime,
            canonical_directory=runtime.runtime_root / "does-not-exist",
            mapping_path=mapping,
        )
    assert pointer.read_bytes() == before
    assert not (public_root / "releases").exists()
