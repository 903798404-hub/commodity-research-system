from datetime import date
import json

import pandas as pd
import pytest

from agri_research_agent.market_data.foreign_fx import (
    SCHEMA_VERSION, FxDataError, currency_return, overview, parse_bcb, parse_ecb,
    strength_comparison, validate_snapshot,
)


def snapshot(rows):
    return dict(schema_version=SCHEMA_VERSION, generated_at="2026-10-09T02:00:00+00:00",
                sources=[dict(provider=p, url="https://example.test/official", raw_sha256="a"*64)
                         for p in ("BCB_SGS1", "ECB_CROSS")], observations=rows)


def quote(code, day, rate):
    return dict(currency=code, date=day, local_per_usd=rate,
                provider="BCB_SGS1" if code=="BRL" else "ECB_CROSS")


def test_real_inverse_return_is_not_the_negative_usd_return():
    assert currency_return(5.0,5.2)==pytest.approx(0.04)
    assert currency_return(5.0,5.2)!=pytest.approx(-(5/5.2-1))


def test_ecb_crosses_only_same_day_usd_and_preserves_missing_dates():
    raw=b'<Envelope><Cube><Cube time="2026-10-06"><Cube currency="USD" rate="1.25"/><Cube currency="CAD" rate="1.50"/></Cube><Cube time="2026-10-07"><Cube currency="CAD" rate="1.51"/></Cube><Cube time="2026-10-08"><Cube currency="USD" rate="1.20"/><Cube currency="CAD" rate="1.44"/><Cube currency="BRL" rate="6.0"/></Cube></Cube></Envelope>'
    rows=parse_ecb(raw,date(2026,10,6),date(2026,10,8))
    assert [(r['date'],r['local_per_usd']) for r in rows]==[('2026-10-06',1.2),('2026-10-08',1.2)]
    assert {r['currency'] for r in rows}=={'CAD'}


def test_bcb_reads_day_month_order_and_filters_request_window():
    raw=json.dumps([dict(data='08/10/2026',valor='5.0119'),dict(data='09/10/2026',valor='5.1')]).encode()
    assert parse_bcb(raw,date(2026,10,1),date(2026,10,8))==[quote('BRL','2026-10-08',5.0119)]


@pytest.mark.parametrize('rate',[0,-1,float('nan'),float('inf'),True,None])
def test_invalid_rate_fails_closed(rate):
    with pytest.raises(FxDataError):
        validate_snapshot(snapshot([quote('BRL','2026-10-08',rate)]))


def test_duplicate_and_mismatched_source_are_rejected():
    row=quote('BRL','2026-10-08',5)
    with pytest.raises(FxDataError,match='重复'):
        validate_snapshot(snapshot([row,row]))
    with pytest.raises(FxDataError,match='来源口径'):
        validate_snapshot(snapshot([{**row,'provider':'ECB_CROSS'}]))


def test_missing_history_and_currency_remain_unavailable():
    frame=validate_snapshot(snapshot([quote('BRL','2026-10-08',5)]))
    result=overview(frame).set_index('currency')
    assert pd.isna(result.loc['BRL','return_1'])
    assert pd.isna(result.loc['CAD','rate'])
    assert pd.isna(result.loc['CAD','latest_date'])


def test_period_returns_use_own_observed_days_not_calendar_or_filled_rows():
    dates=['2026-10-01','2026-10-02','2026-10-05','2026-10-06','2026-10-07','2026-10-08']
    frame=validate_snapshot(snapshot([quote('BRL',d,5.5-i/10) for i,d in enumerate(dates)]))
    row=overview(frame).set_index('currency').loc['BRL']
    assert row['return_5']==pytest.approx(0.1)
    assert row['return_1']==pytest.approx(5.1/5.0-1)
    assert pd.isna(row['return_20'])


def test_comparison_has_one_common_base_and_does_not_fill_gaps():
    frame=validate_snapshot(snapshot([
        quote('BRL','2026-10-01',6),quote('BRL','2026-10-02',5),quote('BRL','2026-10-05',4),
        quote('CAD','2026-10-02',1.5),quote('CAD','2026-10-06',1.25),
    ]))
    result,base=strength_comparison(frame,['BRL','CAD'],date(2026,10,1))
    assert base==date(2026,10,2)
    assert result.loc[result['date']==base,'strength'].tolist()==[100,100]
    assert result.loc[(result['currency']=='CAD') & (result['date']==date(2026,10,6)),'strength'].iloc[0]==pytest.approx(120)
    assert len(result)==4
    assert not ((result['currency']=='CAD') & (result['date']==date(2026,10,5))).any()


def test_disjoint_reference_calendars_cannot_be_compared():
    frame=validate_snapshot(snapshot([quote('BRL','2026-10-01',5),quote('CAD','2026-10-02',1.4)]))
    result,base=strength_comparison(frame,['BRL','CAD'],date(2026,10,1))
    assert result.empty and base is None
