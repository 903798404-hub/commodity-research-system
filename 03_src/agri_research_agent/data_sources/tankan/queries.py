"""Code-reviewed and bounded Tankan SELECT queries."""

from __future__ import annotations

from agri_research_agent.research_data import (
    AcquisitionChannel,
    DatasetId,
    DatasetRef,
    OriginSystem,
    ProviderDatasetId,
    ProviderIdentity,
    SourceLocator,
)

from .models import QuerySpec


def _provider(dataset_id: str, relation: str) -> ProviderIdentity:
    return ProviderIdentity(
        dataset=DatasetRef(DatasetId(dataset_id), OriginSystem("tankan")),
        acquisition_channel=AcquisitionChannel.DIRECT_DATABASE,
        provider_dataset_id=ProviderDatasetId(f"tankan:market.{relation}"),
        source_locator=SourceLocator(
            f"database:quanyong/schema:market/relation:{relation}"
        ),
    )


MARKET_WINDOW_QUERY = QuerySpec(
    name="market.foreign_futures_price_raw.window",
    version="2",
    provider=_provider(
        "tankan.market.foreign_futures_price_raw", "foreign_futures_price_raw"
    ),
    parameter_count=2,
    max_window_days=370,
    max_plan_rows=2_000_000,
    max_total_cost=2_000_000.0,
    sql="""
SELECT trade_date, exchange, product_name, contract, close_price, updated_at
FROM market.foreign_futures_price_raw
WHERE trade_date >= %s AND trade_date <= %s
ORDER BY trade_date, exchange, product_name, contract
""",
)

FX_WINDOW_QUERY = QuerySpec(
    name="market.exchange_rate.window",
    version="2",
    provider=_provider("tankan.market.exchange_rate", "exchange_rate"),
    parameter_count=2,
    max_window_days=370,
    max_plan_rows=100_000,
    max_total_cost=250_000.0,
    sql="""
SELECT trade_date, spot, fx_1m, fx_2m, fx_3m, fx_4m, fx_5m, fx_6m,
       fx_7m, fx_8m, fx_9m, fx_10m, fx_11m, fx_12m, updated_at
FROM market.exchange_rate
WHERE trade_date >= %s AND trade_date <= %s
ORDER BY trade_date
""",
)


_APPROVED_BY_SHA = {
    query.sha256: query for query in (MARKET_WINDOW_QUERY, FX_WINDOW_QUERY)
}


def require_approved_query(query: QuerySpec) -> QuerySpec:
    """Reject runtime-created variants that have not passed code review."""

    approved = _APPROVED_BY_SHA.get(query.sha256)
    if approved != query:
        raise ValueError("query is not in the approved Tankan registry")
    return approved


__all__ = ["FX_WINDOW_QUERY", "MARKET_WINDOW_QUERY", "require_approved_query"]
