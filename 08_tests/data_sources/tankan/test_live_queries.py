"""Exact live API contracts using the existing read-only connection fixture."""
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from test_client import FakeConnection, settings
from agri_research_agent.data_sources.tankan.client import (
    TankanClient, TankanPlanRejectedError, TankanSchemaError,
)
from agri_research_agent.data_sources.tankan.queries import (
    CBOT_SOYBEAN_LIVE_QUERY as CBOT, DCE_SOYMEAL_LIVE_QUERY as M,
    DCE_SOYOIL_LIVE_QUERY as Y, USD_CNH_SPOT_LIVE_QUERY as FX,
    MARKET_WINDOW_QUERY, FX_WINDOW_QUERY, DOMESTIC_SPREAD_WINDOW_QUERY,
    require_approved_query, require_approved_live_query,
)

STAMP = datetime(2026, 8, 31, 7, 5, tzinfo=timezone.utc)


def row(query, contract):
    if query == FX:
        return {'tenor': 'spot', 'mid': 6.85, 'update_time': STAMP}
    return {'exchange': 'CBOT', 'product_name': '大豆' if query == CBOT else '豆粕' if query == M else '豆油',
            'contract': contract, 'last': 1234.5, 'ric': 'fixture', 'update_time': STAMP}


def read(query=M, identities=('M2709',), rows=(), **kwargs):
    connection = FakeConnection(batches=(list(rows),), **kwargs)
    with TankanClient(settings(), connector=lambda **_: connection) as client:
        result = client.read_live(query, identities)
    return result, connection


@pytest.mark.parametrize('query,identity,physical', [
    (CBOT, '2709', '2709'), (M, 'M2709', '2709'), (M, 'M2801', 'M2801'),
    (Y, 'Y2709', '2709'), (Y, 'Y2801', 'Y2801'), (FX, 'USD/CNH:SPOT', 'spot'),
])
def test_exact_identity_source_timestamp_and_proofs(query, identity, physical):
    result, connection = read(query, (identity,), (row(query, physical),))
    assert result.requested == (identity,)
    assert result.unavailable == ()
    quote = result.available[0]
    assert quote['requested_identity'] == identity
    assert quote['source_updated_at'] is STAMP
    assert quote['update_time'] is STAMP
    assert result.plan.query_sha256 == query.sha256
    assert result.connection_proof.transaction_read_only == 'on'
    assert result.retrieved_at.utcoffset() is not None
    assert connection.closed
    assert any(statement.startswith('EXPLAIN') for statement, _, _ in connection.statements)


def test_partial_unavailable_is_never_substituted_or_zero():
    result, connection = read(M, ('M2709', 'M2801'), (row(M, '2709'),))
    assert [q['requested_identity'] for q in result.available] == ['M2709']
    assert dict(result.unavailable[0]) == {'requested_identity': 'M2801', 'reason': 'NOT_FOUND', 'price': None}
    statement, parameters, _ = next(v for v in connection.statements if v[2])
    assert 'futures_live' in statement and 'futures_spread' not in statement
    assert set(parameters[0]) == {'2709', 'M2709', '2801', 'M2801'}


@pytest.mark.parametrize('identities', [('09',), ('M主力',), ('M0',), ('M2713',),
                                         ('Y2709',), ('M2709;DROP',), ('M2709', 'M2709'), (), 'M2709'])
def test_invalid_live_parameters_rejected_without_connection(identities):
    client = TankanClient(settings())
    with pytest.raises(ValueError):
        client.read_live(M, identities)


@pytest.mark.parametrize('change,reason', [
    ({'last': 0}, 'INVALID_PRICE'), ({'last': None}, 'INVALID_PRICE'),
    ({'last': float('nan')}, 'INVALID_PRICE'), ({'last': True}, 'INVALID_PRICE'),
    ({'update_time': None}, 'SOURCE_TIMESTAMP_UNAVAILABLE'),
    ({'update_time': datetime(2026, 8, 31, 15)}, 'SOURCE_TIMESTAMP_UNAVAILABLE'),
])
def test_invalid_quote_is_explicit_unavailable(change, reason):
    result, _ = read(rows=({**row(M, '2709'), **change},))
    assert not result.available
    assert result.unavailable[0]['reason'] == reason
    assert result.unavailable[0]['price'] is None


def test_duplicate_source_identity_not_arbitrarily_selected():
    result, _ = read(rows=(row(M, '2709'), row(M, 'M2709')))
    assert not result.available
    assert result.unavailable[0]['reason'] == 'DUPLICATE_SOURCE_IDENTITY'


@pytest.mark.parametrize('bad', [row(M, '2801'), row(Y, '2709'), row(M, '09')])
def test_source_cannot_silently_substitute(bad):
    with pytest.raises(TankanSchemaError):
        read(rows=(bad,))


def test_allowlists_and_daily_parameters_remain_separate():
    for query in (MARKET_WINDOW_QUERY, FX_WINDOW_QUERY, DOMESTIC_SPREAD_WINDOW_QUERY):
        assert require_approved_query(query) is query
        assert TankanClient._validate_parameters(query, (date(2026, 8, 1), date(2026, 8, 2))) == (date(2026, 8, 1), date(2026, 8, 2))
        with pytest.raises(ValueError):
            require_approved_live_query(query)
    for query in (CBOT, M, Y, FX):
        assert require_approved_live_query(query) is query
        with pytest.raises(ValueError):
            require_approved_query(query)
        with pytest.raises(ValueError):
            require_approved_live_query(replace(query, max_plan_rows=1000))
    with pytest.raises(ValueError):
        TankanClient(settings()).read_live(FX, ('USD/CNY:SPOT',))


def test_live_plan_bounds_fail_closed():
    with pytest.raises(TankanPlanRejectedError):
        read(plan_rows=1_000_000)


def test_daily_call_still_queries_daily_table():
    connection = FakeConnection()
    with TankanClient(settings(), connector=lambda **_: connection) as client:
        list(client.stream(MARKET_WINDOW_QUERY, (date(2026, 8, 1), date(2026, 8, 2))))
    assert not any('_live' in statement for statement, _, _ in connection.statements)


def test_source_result_bound():
    with pytest.raises(TankanSchemaError, match='bound'):
        read(rows=tuple(row(M, '2709') for _ in range(129)))
