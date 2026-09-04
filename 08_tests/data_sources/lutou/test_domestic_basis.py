from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from agri_research_agent.data_sources.basis_sql_loader import (
    EXPECTED_SOURCE_COLUMNS,
    PRODUCT_MAP,
)
from agri_research_agent.data_sources.lutou.domestic_basis import (
    DomesticBasisSourceRow,
    LUTOU_DOMESTIC_BASIS_NOT_NULL,
    LUTOU_DOMESTIC_BASIS_TYPES,
    LutouDomesticBasisLiveAdapter,
    load_domestic_basis_catalog,
)
from agri_research_agent.data_sources.lutou.live import LutouBatch, LutouPlanProof


EXPECTED_MATRIX = {
    "棕榈油": {"华东", "华北", "华南"},
    "大豆油": {"东北", "华东", "华中", "华北", "华南", "西北", "西南"},
    "菜籽油": {"华东", "华南"},
    "菜粕": {"华东", "华南"},
    "豆粕": {"东北", "华东", "华中", "华北", "华南", "西北", "西南"},
}


def test_mapping_matches_complete_live_confirmed_consumer_matrix() -> None:
    catalog = load_domestic_basis_catalog()
    actual = {
        product: {item.region for item in catalog.series if item.source_product == product}
        for product in {item.source_product for item in catalog.series}
    }
    assert len(catalog.series) == 21
    assert actual == EXPECTED_MATRIX
    assert set(actual) == set(PRODUCT_MAP)
    assert {item.consumer_product for item in catalog.series} == set(PRODUCT_MAP.values())
    assert {item.legacy_status for item in catalog.series} == {"LEGACY_IDENTIFIED"}
    assert {item.live_status for item in catalog.series} == {"LIVE_CONFIRMED"}
    assert {item.expected_source_status for item in catalog.series} == {"LIVE_CONFIRMED"}
    assert catalog.source_contract["database_schema"] == "油脂油料价格"
    assert catalog.source_contract["table"] == "basis_price"
    assert catalog.source_contract["table_status"] == "LIVE_CONFIRMED"
    assert {
        catalog.source_contract["date_column"],
        catalog.source_contract["product_column"],
        catalog.source_contract["region_column"],
        catalog.source_contract["quote_type_column"],
        catalog.source_contract["contract_column"],
        catalog.source_contract["value_column"],
        *catalog.source_contract["source_row_identity_fields"],
    } <= set(EXPECTED_SOURCE_COLUMNS)
    assert catalog.source_contract["query_identity_status"] == "LIVE_CONFIRMED"
    assert catalog.source_contract["snapshot_identity_status"] == "LIVE_CONFIRMED"
    assert catalog.live_verified is True


def test_source_mapping_and_provider_identity_are_unique_and_source_preserving() -> None:
    catalog = load_domestic_basis_catalog()
    selected = catalog.match("大豆油", "华东")
    assert selected.consumer_product == "一豆"
    assert selected.provider_dataset_id == "lutou:油脂油料价格:basis_price"
    assert selected.provider_series_id.endswith("大豆油:华东:现货基差")
    assert selected.source_series_id == selected.provider_series_id
    assert "schema:油脂油料价格/relation:basis_price" in selected.source_locator
    assert len({item.series_id for item in catalog.series}) == 21
    assert len({item.provider_series_id for item in catalog.series}) == 21


@pytest.mark.parametrize("value", [Decimal("-1"), Decimal("0"), Decimal("1")])
def test_source_row_preserves_signed_basis_without_fill_or_conversion(value: Decimal) -> None:
    row = DomesticBasisSourceRow(
        business_date=date(2026, 8, 4),
        source_product="大豆油",
        region="华东",
        source_quote_type="现货基差",
        source_contract_code="2609",
        basis_value=value,
        source_row_identity=f"row-{value}",
    )
    assert row.basis_value == value


def test_source_row_retains_nonspot_invalid_contract_and_null_basis_evidence() -> None:
    row = DomesticBasisSourceRow(
        business_date=date(2026, 8, 4), source_product="大豆油", region="华东",
        source_quote_type="远月基差", source_contract_code="bad", basis_value=None,
        raw_basis_value=None, source_row_identity="row-retained",
    )
    assert row.source_quote_type == "远月基差"
    assert row.contract_is_parseable is False
    assert row.basis_value is None


@pytest.mark.parametrize("overrides", [{"basis_value": Decimal("NaN")}, {"region": ""}])
def test_source_row_rejects_invalid_semantics(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "business_date": date(2026, 8, 4),
        "source_product": "大豆油",
        "region": "华东",
        "source_quote_type": "现货基差",
        "source_contract_code": "2609",
        "basis_value": Decimal("10"),
        "source_row_identity": "row-1",
    }
    values.update(overrides)
    with pytest.raises((TypeError, ValueError)):
        DomesticBasisSourceRow(**values)  # type: ignore[arg-type]


class FakeClient:
    class Proof:
        def safe_manifest_fields(self):
            return {"transaction_read_only": True, "write_privileges": []}

    proof = Proof()

    def __init__(self) -> None:
        self.windows: list[tuple[date, date]] = []

    def inspect_query(self, query):
        return ()

    def series_inventory(self, query, groups, start, end):
        assert groups == ("品种", "地区")
        latest = date(2026, 8, 19)
        return {"rows": ({"品种": "大豆油", "地区": "华东", "source_latest_date": latest,
                          "window_row_count": int(start <= latest <= end)},),
                "query_identity": "fixture-independent-inventory", "plan_estimated_rows": 2}

    def date_bounds(self, query):
        return date(2022, 6, 15), date(2026, 8, 19)

    def inspect_relation(self, schema, table):
        return tuple(
            {"COLUMN_NAME": name, "DATA_TYPE": data_type,
             "IS_NULLABLE": "NO" if name in LUTOU_DOMESTIC_BASIS_NOT_NULL else "YES"}
            for name, data_type in LUTOU_DOMESTIC_BASIS_TYPES.items()
        )

    def inspect_relation_indexes(self, schema, table):
        return ({"INDEX_NAME": "idx_date", "COLUMN_NAME": "日期"},)

    def plan_stream(self, query, start, end, *, batch_size):
        self.windows.append((start, end))
        plan = LutouPlanProof(query.sha256, 2, query.max_plan_rows)
        base = {
            "日期": date(2026, 8, 19), "品种": "大豆油", "文章ID": "a-1",
            "文章标题": "title", "行类型": "报价", "地区": "华东", "省份": "江苏",
            "工厂": "factory", "合同情况": "contract", "基差合同年": "2026",
            "基差合同月": "9月", "价格原始": "+100", "期货合约": "2609",
            "基差": Decimal("100"), "现货价": Decimal("8200"), "成交量": None,
            "created_at": datetime(2026, 8, 19, 12), "报价类别": "现货基差",
            "期货收盘价": Decimal("8100"),
        }
        outside = dict(base, 品种="玉米", 地区="华北", 文章ID="outside")
        rows = (base, outside) if start <= base["日期"] <= end else ()
        batch = LutouBatch(query, plan, rows, datetime.now(timezone.utc))
        return plan, iter((batch,))


def test_live_adapter_uses_direct_query_and_preserves_raw_prices() -> None:
    catalog = load_domestic_basis_catalog()
    adapter = LutouDomesticBasisLiveAdapter(FakeClient(), partition_days=92)  # type: ignore[arg-type]
    assert adapter.verify_schema() == {"column_count": 19, "index_entry_count": 1, "date_indexed": True}
    assert adapter.date_bounds() == (date(2022, 6, 15), date(2026, 8, 19))
    extraction = adapter.extract(
        catalog=catalog, start_date=date(2026, 8, 1), end_date=date(2026, 8, 19)
    )
    assert len(extraction.records) == 1
    row = extraction.records[0]
    assert row.basis_value == Decimal("100")
    assert row.cash_price == Decimal("8200")
    assert row.futures_price == Decimal("8100")
    assert row.source_row_sha256 and len(row.source_row_sha256) == 64
    assert extraction.query_identity == adapter.query.sha256
    assert extraction.connection_proof == {
        "transaction_read_only": True, "write_privileges": []
    }
    assert extraction.schema_proof == {
        "column_count": 19, "index_entry_count": 1, "date_indexed": True
    }


def test_live_adapter_uses_daily_physical_partitions_for_incremental_window() -> None:
    catalog = load_domestic_basis_catalog()
    client = FakeClient()
    adapter = LutouDomesticBasisLiveAdapter(client)  # type: ignore[arg-type]
    extraction = adapter.extract(
        catalog=catalog, start_date=date(2026, 8, 17), end_date=date(2026, 8, 19)
    )
    assert client.windows == [
        (date(2026, 8, 17), date(2026, 8, 17)),
        (date(2026, 8, 18), date(2026, 8, 18)),
        (date(2026, 8, 19), date(2026, 8, 19)),
    ]
    assert extraction.partition_count == 3


def test_live_adapter_rejects_independent_count_mismatch():
    class DroppedClient(FakeClient):
        def plan_stream(self, query, start, end, *, batch_size):
            plan, _ = super().plan_stream(query, start, end, batch_size=batch_size)
            return plan, iter(())
    with pytest.raises(ValueError, match="inventory count mismatch"):
        LutouDomesticBasisLiveAdapter(DroppedClient()).extract(
            catalog=load_domestic_basis_catalog(), start_date=date(2026, 8, 19), end_date=date(2026, 8, 19),
        )


def test_live_adapter_empty_window_keeps_source_latest_evidence():
    result = LutouDomesticBasisLiveAdapter(FakeClient()).extract(
        catalog=load_domestic_basis_catalog(), start_date=date(2026, 8, 20), end_date=date(2026, 8, 20),
    )
    assert result.records == ()
    item = next(item for item in result.source_inventory if item.source_product == "大豆油" and item.region == "华东")
    assert item.source_latest_date == date(2026, 8, 19) and item.window_row_count == 0
