"""Code-reviewed and bounded Tankan SELECT queries."""

from __future__ import annotations

from types import MappingProxyType

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
    version="3-goal-a",
    provider=_provider(
        "tankan.market.foreign_futures_price_raw", "foreign_futures_price_raw"
    ),
    parameter_count=2,
    max_window_days=93,
    max_plan_rows=250_000,
    max_total_cost=2_000_000.0,
    sql="""
SELECT trade_date, exchange, product_name, contract, close_price, updated_at
FROM market.foreign_futures_price_raw
WHERE trade_date >= %s AND trade_date <= %s
  AND (
    (exchange = 'CBOT' AND product_name IN ('大豆', 'soybean', '豆粕', 'soymeal', '豆油', 'soyoil'))
    OR (exchange = 'BMD' AND product_name IN ('棕榈油', 'palm'))
  )
ORDER BY trade_date, exchange, product_name, contract
""",
)

FX_WINDOW_QUERY = QuerySpec(
    name="market.exchange_rate.window",
    version="2",
    provider=_provider("tankan.market.exchange_rate", "exchange_rate"),
    parameter_count=2,
    max_window_days=93,
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

DOMESTIC_SPREAD_WINDOW_QUERY = QuerySpec(
    name="market.futures_spread.window",
    version="1-goal-e2",
    provider=_provider("tankan.market.futures_spread", "futures_spread"),
    parameter_count=2,
    max_window_days=93,
    max_plan_rows=100_000,
    max_total_cost=500_000.0,
    sql="""
SELECT trade_date, product_name, contract, close_price, updated_at
FROM market.futures_spread
WHERE trade_date >= %s AND trade_date <= %s
  AND product_name IN ('豆粕', '菜籽粕', '豆油', '菜籽油', '棕榈油')
  AND contract IN ('01', '05', '09')
ORDER BY trade_date, product_name, contract
""",
)


_APPROVED_BY_SHA = {
    query.sha256: query
    for query in (MARKET_WINDOW_QUERY, FX_WINDOW_QUERY, DOMESTIC_SPREAD_WINDOW_QUERY)
}


def require_approved_query(query: QuerySpec) -> QuerySpec:
    """Reject runtime-created variants that have not passed code review."""

    approved = _APPROVED_BY_SHA.get(query.sha256)
    if approved != query:
        raise ValueError("query is not in the approved Tankan registry")
    return approved


def _live_spec(name: str, relation: str, sql: str) -> QuerySpec:
    # QuerySpec validates SQL and bounds. Live parameters have their own client
    # contract; max_window_days is not used by that entrypoint.
    return QuerySpec(name=name, version="1-exact-live", sql=sql,
                     provider=_provider(f"tankan.market.{relation}", relation),
                     parameter_count=1, max_window_days=1,
                     max_plan_rows=128, max_total_cost=10_000.0)


CBOT_SOYBEAN_LIVE_QUERY = _live_spec(
    "market.foreign_futures_live.cbot.exact", "foreign_futures_live", """
SELECT exchange, product_name, contract, last, ric, update_time
FROM market.foreign_futures_live
WHERE exchange = 'CBOT' AND product_name = '大豆' AND contract = ANY(%s)
ORDER BY contract
""")
DCE_SOYMEAL_LIVE_QUERY = _live_spec(
    "market.futures_live.m.exact", "futures_live", """
SELECT product_name, contract, bid, ask, last, volume, open_interest, update_time
FROM market.futures_live
WHERE product_name = '豆粕' AND contract = ANY(%s)
ORDER BY contract
""")
DCE_SOYOIL_LIVE_QUERY = _live_spec(
    "market.futures_live.y.exact", "futures_live", """
SELECT product_name, contract, bid, ask, last, volume, open_interest, update_time
FROM market.futures_live
WHERE product_name = '豆油' AND contract = ANY(%s)
ORDER BY contract
""")
USD_CNH_SPOT_LIVE_QUERY = _live_spec(
    "market.exchange_rate_live.usdcnh.spot", "exchange_rate_live", """
SELECT tenor, bid, ask, mid, value_date, update_time
FROM market.exchange_rate_live
WHERE tenor = %s
""")
_APPROVED_LIVE = MappingProxyType({q.sha256: q for q in (
    CBOT_SOYBEAN_LIVE_QUERY, DCE_SOYMEAL_LIVE_QUERY,
    DCE_SOYOIL_LIVE_QUERY, USD_CNH_SPOT_LIVE_QUERY)})


def require_approved_live_query(query: QuerySpec) -> QuerySpec:
    approved = _APPROVED_LIVE.get(query.sha256)
    if type(query) is not QuerySpec or approved != query:
        raise ValueError("query is not in the approved Tankan live registry")
    return approved


__all__ = [
    "DOMESTIC_SPREAD_WINDOW_QUERY", "FX_WINDOW_QUERY", "MARKET_WINDOW_QUERY",
    "require_approved_query",
    "CBOT_SOYBEAN_LIVE_QUERY", "DCE_SOYMEAL_LIVE_QUERY", "DCE_SOYOIL_LIVE_QUERY",
    "USD_CNH_SPOT_LIVE_QUERY", "require_approved_live_query",
]
