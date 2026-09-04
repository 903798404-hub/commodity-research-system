from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from agri_research_agent.data_sources.lutou.domestic_basis import (
    DomesticBasisEvidenceType,
    DomesticBasisExtraction,
    DomesticBasisSourceRow,
    DomesticBasisSourceInventory,
    load_domestic_basis_catalog,
)
from agri_research_agent.pipelines import lutou_domestic_basis as pipeline
from agri_research_agent.shared.file_identity import identify_file
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


@pytest.fixture
def runtime(tmp_path: Path) -> RuntimeContext:
    (tmp_path / ".market-data-runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_id": "goal-d3a3-fixture",
                "classification": "isolated-dev",
                "module_id": "international-spread",
                "created_at": "2026-08-20T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return RuntimeContext(RuntimeMode.ISOLATED_DEV, "international-spread", tmp_path)


def _historical_source() -> pa.Table:
    commodities = (
        ("一豆", "一豆基差", "基差报价"),
        ("24度", "24度基差", "基差报价"),
        ("三菜", "三菜基差", "基差报价"),
        ("豆粕", "豆粕基差", "基差报价"),
        ("菜粕", "菜粕基差", "基差报价"),
        ("葵粕", "葵粕基差", "基差报价"),
        ("一葵", "一葵价格", "一口价"),
        ("一级玉米油", "一级玉米油价格", "一口价"),
    )
    first_day = date(2022, 3, 17)
    day_count = (date(2026, 5, 30) - first_day).days
    rows = []
    for index in range(16_331):
        commodity, source_sheet, quote_type = commodities[index % len(commodities)]
        sequence = index // len(commodities)
        business_date = first_day + timedelta(days=sequence % day_count)
        delivery_cycle = sequence // day_count
        cash = Decimal(5_000 + index % 101)
        is_basis = quote_type == "基差报价"
        rows.append(
            {
                "date": datetime.combine(business_date, datetime.min.time()),
                "commodity": commodity,
                "region": "华东",
                "quote_type": quote_type,
                "delivery_month": "现货" if delivery_cycle == 0 else f"历史批次{delivery_cycle}",
                "futures_contract": "05月" if is_basis else None,
                "cash_price": float(cash),
                "futures_price": float(cash - Decimal("100")) if is_basis else None,
                "basis": 100.0 if is_basis else None,
                "source_sheet": source_sheet,
            }
        )
    return pa.Table.from_pylist(rows)


def _live_records(*, extension: bool = False) -> tuple[DomesticBasisSourceRow, ...]:
    catalog = load_domestic_basis_catalog()
    dated_series: list[tuple[date, object]] = []
    for index in range(690):
        dated_series.append((date(2026, 6, 1) + timedelta(days=index // 21), catalog.series[index % 21]))
    dated_series.extend((date(2026, 8, 14), item) for item in catalog.series)
    if extension:
        dated_series.extend((date(2026, 8, 19), item) for item in catalog.series)
    return tuple(
        DomesticBasisSourceRow(
            business_date=business_date,
            source_product=item.source_product,
            region=item.region,
            source_quote_type="现货基差",
            source_contract_code="2609",
            basis_value=Decimal((-1, 0, 1)[index % 3] * 10),
            source_row_identity=f"formal-live-{business_date.isoformat()}-{index}",
            factory=f"factory-{index}",
            article_id=f"article-{index}",
            delivery_month_native="现货",
        )
        for index, (business_date, item) in enumerate(dated_series)
    )


class FakeLiveAdapter:
    def __init__(self, records: tuple[DomesticBasisSourceRow, ...]) -> None:
        self.records = records
        self.windows: list[tuple[date, date]] = []

    def date_bounds(self) -> tuple[date, date]:
        dates = [row.business_date for row in self.records]
        return min(dates), max(dates)

    def extract(self, *, catalog, start_date: date, end_date: date) -> DomesticBasisExtraction:
        self.windows.append((start_date, end_date))
        rows = tuple(row for row in self.records if start_date <= row.business_date <= end_date)
        return DomesticBasisExtraction(
            records=rows,
            evidence_type=DomesticBasisEvidenceType.LIVE_DATABASE,
            query_identity="formal-live-query",
            snapshot_identity="formal-live-snapshot",
            extracted_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
            source_min_date=start_date,
            source_max_date=end_date,
            plan_estimated_rows=len(rows),
            connection_proof={"transaction_read_only": True, "write_privileges": []},
            schema_proof={"column_count": 19, "index_entry_count": 1, "date_indexed": True},
            source_inventory=tuple(DomesticBasisSourceInventory(
                item.source_product, item.region,
                max((row.business_date for row in self.records if (row.source_product, row.region) == (item.source_product, item.region)), default=None),
                sum((row.source_product, row.region) == (item.source_product, item.region) for row in rows),
            ) for item in catalog.series),
            inventory_query_identity="fixture-independent-inventory", inventory_plan_estimated_rows=len(self.records),
        )


def _legacy_baseline(table: pa.Table) -> pa.Table:
    rows = []
    for row in table.to_pylist():
        rows.append(
            {
                "date": datetime.combine(row["business_date"], datetime.min.time()),
                "commodity": row["consumer_product"],
                "region": row["location"],
                "quote_type": row["quote_type"],
                "delivery_month": row["delivery_month"],
                "futures_contract": row["futures_contract"],
                "cash_price": None if row["cash_price"] is None else float(row["cash_price"]),
                "futures_price": None if row["futures_price"] is None else float(row["futures_price"]),
                "basis": None if row["basis"] is None else float(row["basis"]),
                "source_sheet": row["source_sheet"],
            }
        )
    return pa.Table.from_pylist(rows)


def test_formal_alignment_parity_incremental_and_immutable_seed(
    runtime: RuntimeContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = yaml.safe_load(Path("02_configs/lutou_domestic_basis.yaml").read_text(encoding="utf-8"))
    payload["freshness_policy"] = {
        "policy_version": "test-only-not-business-approved", "threshold_approved": True,
        "freshness_threshold": (date(2026, 8, 20) - date(2026, 8, 14)).days, "stale_is_blocking": False,
    }
    mapping = tmp_path / "fixture-mapping.yaml"
    mapping.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    catalog = load_domestic_basis_catalog(mapping)
    adapter = FakeLiveAdapter(_live_records())
    initial = pipeline.run_domestic_basis_live(
        runtime=runtime,
        run_id="goal-d3a-live-seed",
        adapter=adapter,
        mapping_path=mapping,
        mode="full",
    )
    assert initial.current.observations.num_rows == 711

    source_path = tmp_path / "sealed-history.parquet"
    pq.write_table(_historical_source(), source_path)
    monkeypatch.setattr(pipeline, "HISTORICAL_SOURCE_SHA256", identify_file(source_path).sha256)
    monkeypatch.setattr(pipeline, "HISTORICAL_SEED_ID", "goal-d3a3-test-history")
    seed = pipeline.seal_historical_basis_seed(
        runtime=runtime, source_path=source_path, mapping_path=mapping,
    )
    composite, quality = pipeline.compose_formal_basis_current(
        seed, initial.current.observations, catalog,
    )
    assert quality["historical_row_count"] == 16_331
    assert quality["live_row_count"] == 711
    assert quality["historical_cash_non_null_count"] == 16_331
    assert quality["live_cash_non_null_count"] == 0
    assert quality["live_futures_non_null_count"] == 0

    baseline_path = tmp_path / "formal-baseline.parquet"
    pq.write_table(_legacy_baseline(composite), baseline_path)
    monkeypatch.setattr(pipeline, "FORMAL_BASELINE_SHA256", identify_file(baseline_path).sha256)
    aligned = pipeline.align_formal_domestic_basis_current(
        runtime=runtime,
        run_id="goal-d3a3-formal-alignment",
        mapping_path=mapping,
        historical_source_path=source_path,
        formal_baseline_path=baseline_path,
    )
    assert aligned.current.observations.num_rows == 17_042
    assert aligned.parity["common_rows"] == 17_042
    assert aligned.parity["baseline_only_rows"] == 0
    assert sum(aligned.parity["field_differences"].values()) == 0
    assert set(aligned.current.observations["consumer_product"].to_pylist()) >= {
        "一豆", "24度", "三菜", "豆粕", "菜粕", "一葵", "一级玉米油", "葵粕",
    }
    live = [row for row in aligned.current.observations.to_pylist() if row["segment"] == "LIVE_LUTOU"]
    assert {row["basis"] for row in live} >= {Decimal("-10.0000"), Decimal("0.0000"), Decimal("10.0000")}
    assert all(row["cash_price"] is None and row["futures_price"] is None for row in live)

    root = runtime.runtime_root / "public-market-data" / "lutou-domestic-basis"
    seed_pointer_before = (root / "historical-seed.json").read_bytes()
    seed_manifest_before = (seed.directory / "manifest.json").read_bytes()
    current_pointer_before = (root / "current.json").read_bytes()
    repeat_adapter = FakeLiveAdapter(_live_records())
    repeat = pipeline.run_domestic_basis_live(
        runtime=runtime,
        run_id="goal-d3a3-repeat",
        adapter=repeat_adapter,
        mapping_path=mapping,
        require_formal_current=True,
    )
    assert repeat.promoted is False
    assert (root / "current.json").read_bytes() == current_pointer_before
    assert (root / "historical-seed.json").read_bytes() == seed_pointer_before
    assert (seed.directory / "manifest.json").read_bytes() == seed_manifest_before
    assert repeat_adapter.windows == [(date(2026, 7, 14), date(2026, 8, 14))]

    def interrupt(stage: str) -> None:
        if stage == "release_sealed_before_pointer":
            raise RuntimeError("injected formal promotion interruption")

    with pytest.raises(RuntimeError, match="formal promotion interruption"):
        pipeline.run_domestic_basis_live(
            runtime=runtime,
            run_id="goal-d3a3-interrupted",
            adapter=FakeLiveAdapter(_live_records(extension=True)),
            mapping_path=mapping,
            require_formal_current=True,
            failure_hook=interrupt,
        )
    assert (root / "current.json").read_bytes() == current_pointer_before
    assert (root / "historical-seed.json").read_bytes() == seed_pointer_before

    extended = pipeline.run_domestic_basis_live(
        runtime=runtime,
        run_id="goal-d3a3-extension",
        adapter=FakeLiveAdapter(_live_records(extension=True)),
        mapping_path=mapping,
        require_formal_current=True,
    )
    assert extended.promoted is True
    assert extended.current.observations.num_rows == 17_063
    assert extended.current.manifest["historical_row_count"] == 16_331
    assert extended.current.manifest["live_row_count"] == 732
    assert extended.current.manifest["source_max_date"] == "2026-08-19"
    assert (root / "historical-seed.json").read_bytes() == seed_pointer_before
    assert (seed.directory / "manifest.json").read_bytes() == seed_manifest_before


def test_formal_current_rejects_cutover_overlap() -> None:
    catalog = load_domestic_basis_catalog()
    source = _historical_source()
    rows = source.to_pylist()
    rows[0]["date"] = datetime(2026, 6, 1)
    with pytest.raises(pipeline.DomesticBasisPipelineError, match="cutover"):
        pipeline.build_historical_seed_table(pa.Table.from_pylist(rows), catalog)
