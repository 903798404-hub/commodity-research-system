from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from agri_research_agent.data_sources.lutou.live import (
    LutouBatch,
    LutouPlanProof,
)
from agri_research_agent.data_sources.lutou.three_oil_live import (
    extract_three_oil_live,
)
from agri_research_agent.research_data.three_oil_v1 import load_three_oil_v1


DAY = date(2026, 8, 18)


class FakeClient:
    def __init__(self) -> None:
        self.queries = []

    def inspect_query(self, query):  # type: ignore[no-untyped-def]
        self.queries.append(query)
        return tuple(
            {"COLUMN_NAME": item}
            for item in (query.date_column, *query.value_columns)
        )

    def plan_stream(self, query, start, end):  # type: ignore[no-untyped-def]
        assert start <= DAY <= end
        row = {query.date_column: DAY}
        row.update({item: Decimal("1000") for item in query.value_columns})
        plan = LutouPlanProof(query.sha256, 10, query.max_plan_rows)
        batch = LutouBatch(
            query,
            plan,
            (row,),
            datetime(2026, 8, 19, tzinfo=timezone.utc),
        )
        return plan, iter((batch,))


def test_live_adapter_reads_only_sealed_tables_and_columns() -> None:
    catalog = load_three_oil_v1()
    client = FakeClient()
    result = extract_three_oil_live(client, catalog, start=DAY, end=DAY)

    assert len(result.records) == 20
    assert len(result.queries) == 12
    assert {item.series_id for item in result.records} == {
        item.series_id for item in catalog.series
    }
    assert all(item.source_locator.startswith("database:lutou/schema:") for item in result.records)
    assert all(
        item.provider_dataset_id == f"lutou:oils:{item.source_table}"
        for item in result.records
    )
    assert {item.provider_series_id for item in result.records} == {
        item.provider_series_id for item in catalog.series
    }
    assert {item.provider for item in result.records} == {"Reuters", "Oil World"}
    oil_world = [item for item in result.records if item.provider == "Oil World"]
    assert len(oil_world) == 1
    assert oil_world[0].metadata_source_type == "official_provider_website"
    assert oil_world[0].metadata_status == "proven"
    assert oil_world[0].source_quote_unit == "US-$/T"
    assert all(item.raw_price == Decimal("1000") for item in result.records)
    assert len({item.source_row_sha256 for item in result.records}) == 20


def test_live_adapter_query_columns_match_sealed_native_mapping() -> None:
    catalog = load_three_oil_v1()
    result = extract_three_oil_live(FakeClient(), catalog, start=DAY, end=DAY)
    actual = {
        (query.table, column)
        for query in result.queries
        for column in query.value_columns
    }
    expected = {
        (item.source_native_table, item.source_native_series)
        for item in catalog.series
    }
    assert actual == expected
