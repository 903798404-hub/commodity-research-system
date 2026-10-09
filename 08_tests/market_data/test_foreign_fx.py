from datetime import date
import json

import pandas as pd
import pytest

from agri_research_agent.market_data.foreign_fx import (
    SCHEMA_VERSION, FxDataError, currency_return, overview, parse_bcb, parse_ecb,
    strength_comparison, validate_snapshot, seasonality,
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


def test_seasonal_alignment_preserves_leap_day_and_excludes_current_from_mean():
    frame = validate_snapshot(snapshot([
        quote('BRL','2024-02-29',4), quote('BRL','2024-03-01',4),
        quote('BRL','2025-03-01',6), quote('BRL','2026-03-01',90),
    ]))
    original = frame.copy(deep=True)
    rows, mean = seasonality(frame,'BRL',[2024,2025,2026],current_year=2026)
    assert rows.loc[rows['year']==2024,'season_date'].tolist()==[date(2000,2,29),date(2000,3,1)]
    march = mean.loc[mean['season_date']==date(2000,3,15)].iloc[0]
    assert march['value']==5 and march['year_count']==2
    assert pd.isna(mean.loc[mean['season_date']==date(2000,2,15),'value'].iloc[0])
    assert len(rows)==len(frame)
    pd.testing.assert_frame_equal(frame,original)


def test_seasonal_index_uses_each_year_first_quote_and_usd_direction():
    frame = validate_snapshot(snapshot([
        quote('BRL','2024-01-02',5),quote('BRL','2024-01-04',6),
        quote('BRL','2025-01-03',10),quote('BRL','2025-01-04',8),
        quote('CAD','2024-01-01',1.2),
    ]))
    rows, mean = seasonality(frame,'BRL',[2024,2025],current_year=2026,normalize=True)
    assert rows['value'].tolist()==[100,120,100,80]
    assert rows['currency'].unique().tolist()==['BRL']
    assert mean.loc[mean['season_date']==date(2000,1,15),'value'].iloc[0]==100
    assert not (rows['season_date']==date(2000,1,1)).any()


def test_seasonal_selected_years_control_mean_and_one_year_has_no_mean():
    frame = validate_snapshot(snapshot([quote('BRL',f'{y}-01-02',v) for y,v in [(2023,3),(2024,4),(2025,5),(2026,60)]]))
    rows, mean = seasonality(frame,'BRL',[2024,2025,2026],current_year=2026)
    assert mean['value'].tolist()==[4.5] and set(rows['year'])=={2024,2025,2026}
    _, mean = seasonality(frame,'BRL',[2025,2026],current_year=2026)
    assert mean['value'].isna().all()


def test_seasonal_monthly_mean_weights_years_equally_despite_missing_quotes():
    frame = validate_snapshot(snapshot([
        quote('BRL','2024-01-02',2),quote('BRL','2024-01-03',4),
        quote('BRL','2025-01-02',10),quote('BRL','2026-01-02',90),
    ]))
    _, mean = seasonality(frame,'BRL',[2024,2025,2026],current_year=2026)
    assert mean['value'].tolist()==[6.5]
    assert mean['year_count'].tolist()==[2]
