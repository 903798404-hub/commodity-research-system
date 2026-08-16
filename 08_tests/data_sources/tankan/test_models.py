from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agri_research_agent.data_sources.tankan.models import QueryPlanProof, SourceBatch
from agri_research_agent.data_sources.tankan.queries import (
    FX_WINDOW_QUERY,
    MARKET_WINDOW_QUERY,
    require_approved_query,
)
from agri_research_agent.research_data import DataAssetCatalog


ROOT = Path(__file__).resolve().parents[3]


def test_query_spec_is_bounded_versioned_and_provider_identified() -> None:
    query = MARKET_WINDOW_QUERY
    assert query.version == "2"
    assert query.parameter_count == 2
    assert query.max_window_days == 370
    assert len(query.sha256) == 64
    assert query.identity() == {
        "query_name": "market.foreign_futures_price_raw.window",
        "query_version": "2",
        "query_sha256": query.sha256,
        "dataset_id": "tankan.market.foreign_futures_price_raw",
        "provider_dataset_id": "tankan:market.foreign_futures_price_raw",
        "origin_system": "tankan",
        "acquisition_channel": "direct_database",
        "source_locator": "database:quanyong/schema:market/relation:foreign_futures_price_raw",
    }


def test_query_provider_identities_match_gate_a_approved_catalog() -> None:
    catalog = DataAssetCatalog.load_gate_a_approved(
        ROOT / "02_configs" / "public_research_data_catalog.candidate.json",
        ROOT / "02_configs" / "public_research_data_catalog.approval.json",
    )
    for query in (MARKET_WINDOW_QUERY, FX_WINDOW_QUERY):
        approved = catalog.get(query.provider.dataset.dataset_id)
        assert query.provider.dataset == approved.dataset
        assert query.provider.acquisition_channel == approved.acquisition_channel
        assert query.provider.provider_dataset_id == approved.provider_dataset_id
        assert query.provider.source_locator == approved.source_locator


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE market.x SET value = 1",
        "SELECT * FROM market.x; DELETE FROM market.x",
        "SELECT * FROM market.x -- hidden mutation",
        "SELECT * FROM market.x /* comment */",
        "COPY market.x TO '/tmp/x'",
        "CREATE TEMP TABLE x(a int)",
        "EXPLAIN ANALYZE SELECT * FROM market.x",
    ],
)
def test_query_spec_rejects_non_select_or_ambiguous_sql(sql: str) -> None:
    with pytest.raises(ValueError):
        replace(MARKET_WINDOW_QUERY, sql=sql)


def test_query_spec_rejects_unbounded_or_mismatched_parameters() -> None:
    with pytest.raises(ValueError, match="positive"):
        replace(MARKET_WINDOW_QUERY, parameter_count=0, sql="SELECT 1")
    with pytest.raises(ValueError, match="placeholders"):
        replace(MARKET_WINDOW_QUERY, parameter_count=1)
    with pytest.raises(ValueError, match="max_window_days"):
        replace(MARKET_WINDOW_QUERY, max_window_days=0)


def test_runtime_query_variants_are_not_implicitly_approved() -> None:
    assert require_approved_query(MARKET_WINDOW_QUERY) is MARKET_WINDOW_QUERY
    with pytest.raises(ValueError, match="approved Tankan registry"):
        require_approved_query(replace(MARKET_WINDOW_QUERY, max_plan_rows=1))


def test_source_batch_requires_matching_plan_identity() -> None:
    plan = QueryPlanProof(
        query_sha256="0" * 64,
        estimated_rows=1,
        total_cost=1.0,
        max_plan_rows=2,
        max_total_cost=2.0,
    )
    with pytest.raises(ValueError, match="identity"):
        SourceBatch(MARKET_WINDOW_QUERY, plan, (), datetime.now(timezone.utc))
