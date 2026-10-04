from datetime import date
import pandas as pd
import pytest

from agri_research_agent.soybean_margin.public_inputs import apply_public_inputs


def inputs():
    day = date(2026,6,25)
    history = pd.DataFrame([dict(business_date=day, origin='brazil', shipment_year=2026,
        shipment_month=9, cnf_cents_per_bushel=0)])
    market = pd.DataFrame([dict(business_date=day, exchange='CBOT', product='SOYBEAN',
        contract_code='2609', price=1000., currency='USD', price_unit='US_cents/bushel', is_usable=True)])
    fx = pd.DataFrame([dict(quote_date=day, tenor_months=3, rate=7., base_currency='USD',
        quote_currency='CNH', rate_unit='CNH_per_USD', is_usable=True)])
    domestic = pd.DataFrame([dict(date=day, status='success', season='2026/2027',
        leg1_instrument='M', leg1_month=1, leg1_price=3000.,
        leg2_instrument='Y', leg2_month=1, leg2_price=8000.)])
    return history, market, fx, domestic


def test_full_update_prices_are_read_by_exact_date_contract_and_tenor():
    history, market, fx, domestic = inputs()
    data, latest = apply_public_inputs(history, market, fx, domestic, date(2026,6,26))
    row = data.loc[(data.origin=='brazil') & (data.business_date==date(2026,6,25)) & (data.shipment_month==9)].iloc[0]
    assert row.cbot_price_cents_per_bushel == 1000
    assert row.fx_value == 7
    assert row.soymeal_price_cny_per_tonne == 3000
    assert row.soyoil_price_cny_per_tonne == 8000
    assert row.soymeal_contract_code == 'M2701'
    assert latest['CBOT'] == date(2026,6,25)
    tomorrow = data.loc[data.business_date==date(2026,6,26)]
    assert len(tomorrow)==48
    assert tomorrow.cbot_price_cents_per_bushel.isna().all()
    assert tomorrow.fx_value.isna().all()
    assert tomorrow.soymeal_price_cny_per_tonne.isna().all()
    market.loc[0,'price'] = 1200
    refreshed,_ = apply_public_inputs(history,market,fx,domestic,date(2026,6,26))
    assert refreshed.loc[(refreshed.business_date==date(2026,6,25)) & (refreshed.shipment_month==9),'cbot_price_cents_per_bushel'].eq(1200).all()


def test_public_prices_reject_wrong_units_and_ambiguous_contracts():
    history,market,fx,domestic=inputs()
    market.loc[0,'price_unit']='USD/tonne'
    with pytest.raises(ValueError,match='单位'):
        apply_public_inputs(history,market,fx,domestic,date(2026,6,25))
    history,market,fx,domestic=inputs()
    market=pd.concat([market,market.assign(price=1001)],ignore_index=True)
    with pytest.raises(ValueError,match='不同数值'):
        apply_public_inputs(history,market,fx,domestic,date(2026,6,25))


def test_another_contract_never_substitutes_missing_target():
    history,market,fx,domestic=inputs()
    market.loc[0,'contract_code']='2709'
    data,_=apply_public_inputs(history,market,fx,domestic,date(2026,6,25))
    assert data.loc[data.shipment_month.eq(9),'cbot_price_cents_per_bushel'].isna().all()


def test_fx_uses_same_day_adjacent_tenors_without_extrapolation():
    history,market,fx,domestic=inputs()
    fx=pd.concat([fx.assign(tenor_months=1,rate=7.),fx.assign(tenor_months=6,rate=7.5)],ignore_index=True)
    data,_=apply_public_inputs(history,market,fx,domestic,date(2026,6,25))
    row=data.loc[data.shipment_month.eq(9)].iloc[0]
    assert row.fx_value == pytest.approx(7.2)
    assert bool(row.fx_is_interpolated)
    assert row.fx_lower_tenor == 1 and row.fx_upper_tenor == 6
    assert data.loc[data.shipment_month.eq(6),'fx_value'].isna().all()
