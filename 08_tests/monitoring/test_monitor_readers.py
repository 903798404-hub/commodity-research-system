from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from types import SimpleNamespace

from agri_research_agent.monitoring.catalog import BY_ID
from agri_research_agent.monitoring.freshness import Observation, evaluate
from agri_research_agent.monitoring.observations import Inputs, Reader, parquet_cutoffs, report_observation

NOW=datetime(2026,10,22,8,tzinfo=timezone.utc)


def inputs(root):
    return Inputs(root,root,root,root,root,root,root)


def test_columnar_date_scan_filters_other_inputs_and_preserves_common_cutoff(tmp_path,monkeypatch):
    path=tmp_path/'prices.parquet'
    frame=pd.DataFrame({'series_id':['A','A','B','C'],'business_date':[
        date(2026,10,20),date(2026,10,21),date(2026,10,19),date(2026,10,22)],
        'country':['ARG','ARG','ARG','USA'],'unused_payload':['x'*10000]*4})
    frame.to_parquet(path,index=False,row_group_size=2)
    def reject(*args,**kwargs):
        raise AssertionError('Full history materialization forbidden')
    monkeypatch.setattr(pd,'read_parquet',reject)
    value=parquet_cutoffs(path,['series_id'],'business_date',select={'country':{'ARG'}})
    assert value.actual=='2026-10-19'
    assert value.extra['series_dates']=={'A':'2026-10-21','B':'2026-10-19'}


def test_null_values_do_not_advance_weather_data_date(tmp_path):
    path=tmp_path/'weather.parquet'
    pd.DataFrame({'region':['A','A'],'valid_date':[date(2026,10,21),date(2026,10,22)],
        'value':[2.0,None]}).to_parquet(path,index=False)
    assert parquet_cutoffs(path,['region'],'valid_date',valid_value='value').actual=='2026-10-21'


def test_weather_window_before_scheduler_deadline_accepts_original_snapshot():
    from agri_research_agent.monitoring.observations import weather_observation
    history=pd.DataFrame({'region':['A'],'date':['2026-10-20']})
    prior=pd.DataFrame({'region':['A']*14,'model':['EC']*14,'date':pd.date_range('2026-10-21',periods=14)})
    before=datetime(2026,10,22,0,tzinfo=timezone.utc)
    assert weather_observation(history,prior,'date',['region','model'],before).error is None
    assert weather_observation(history,prior,'date',['region','model'],NOW).error


def test_usda_monitor_uses_served_report_version_not_update_timestamp(tmp_path):
    release=tmp_path/'releases/release-1/data';release.mkdir(parents=True)
    pointer={'release_id':'release-1','report_month':'2026-08','updated_at':'2026-10-22'}
    (tmp_path/'current.json').write_text(json.dumps(pointer))
    path=release/'report_version.json';path.write_text(json.dumps({'currentReportMonth':'2026-08'}))
    assert report_observation('usda',tmp_path).actual=='2026-08'
    path.write_text(json.dumps({'currentReportMonth':'2026-09'}))
    with pytest.raises(ValueError,match='reader differ'):
        report_observation('usda',tmp_path)


def test_nass_original_preferred_path_and_common_good_excellent_week(tmp_path):
    folder=tmp_path/'processed/soybean_crop_progress';folder.mkdir(parents=True)
    common=dict(geography_level='US',region_name='US TOTAL',calendar_year=2026,value_pct=30)
    rows=[dict(**common,metric='GOOD',week_ending='2026-10-18'),
        dict(**common,metric='EXCELLENT',week_ending='2026-10-11'),
        dict(**common,metric='GOOD_EXCELLENT',week_ending='2026-10-04')]
    pd.DataFrame(rows).to_parquet(folder/'soybeans_crop_condition_weekly.parquet',index=False)
    value=Reader(inputs(tmp_path),NOW).read('nass_GOOD_EXCELLENT')
    assert value.actual=='2026-10-04'


def test_nass_100_percent_completion_is_not_a_daily_update_demand(tmp_path):
    folder=tmp_path/'processed/soybean_crop_progress';folder.mkdir(parents=True)
    pd.DataFrame([dict(geography_level='US',region_name='US TOTAL',calendar_year=2026,metric='PLANTED',value_pct=100,
        week_ending='2026-06-14')]).to_parquet(folder/'soybeans_crop_progress_weekly_2021_2026.parquet',index=False)
    value=Reader(inputs(tmp_path),NOW).read('nass_PLANTED')
    assert value.actual=='2026-06-14' and value.season_complete
    assert evaluate(BY_ID['nass_PLANTED'],value,NOW)['status']=='SEASON_COMPLETE'


def test_unpublished_out_of_season_harvest_stays_blank():
    row=evaluate(BY_ID['brazil_HARVESTED'],Observation(None),NOW)
    assert row['status']=='SEASON_COMPLETE' and row['actual_date'] is None and not row['retry_eligible']


def test_missing_in_season_data_retries_only_its_existing_producer():
    row=evaluate(BY_ID['nass_HARVESTED'],Observation(None),NOW)
    assert row['status']=='MISSING' and row['retry_eligible'] and row['job']=='crop'


def test_original_user_saved_history_has_no_invented_daily_deadline():
    row=evaluate(BY_ID['manual_sugar'],Observation('2026-07-01'),NOW)
    assert row['status']=='REFERENCE' and row['actual_date']=='2026-07-01'
    assert not row['retry_eligible']
    assert evaluate(BY_ID['manual_sugar'],Observation(None),NOW)['status']=='REFERENCE'


def test_manual_cnf_advancing_beyond_saved_market_still_alerts():
    row=evaluate(BY_ID['margin_market_palm'],Observation('2026-10-20',source_actual='2026-10-21'),NOW)
    assert row['status']=='MISSED' and not row['retry_eligible']


def test_euronext_and_ice_reporting_periods_are_independent():
    # After the Saturday CFTC/ICE window, Euronext's next Wednesday release
    # is not due yet. Its legitimate Friday cutoff must remain current.
    saturday=datetime(2026,10,24,5,tzinfo=timezone.utc)
    assert evaluate(BY_ID['foreign_positions_rapeseed'],Observation('2026-10-13'),saturday)['status']=='CHECK_DUE'
    assert evaluate(BY_ID['foreign_positions_euronext_rapeseed'],Observation('2026-10-16'),saturday)['status']=='CURRENT'


def test_morning_fx_check_uses_previous_source_business_day():
    assert evaluate(BY_ID['fx_CNY'],Observation('2026-10-21'),NOW)['status']=='CURRENT'
    monday=datetime(2026,10,26,5,tzinfo=timezone.utc)
    assert evaluate(BY_ID['fx_BRL'],Observation('2026-10-23'),monday)['status']=='CURRENT'


def test_native_export_paths_resolve_mounted_data_root(tmp_path,monkeypatch):
    from agri_research_agent.soybean_exports import research
    seen=[]
    def published(paths,validator):
        seen.append(paths)
        return pd.DataFrame({'week_ending_date':['2026-10-15']}),{'source_latest_week':'2026-10-15'},{},None
    monkeypatch.setattr(research,'_load_published_source',published)
    assert Reader(inputs(tmp_path),NOW).read('fgis').actual=='2026-10-15'
    assert seen[0]['stable'].is_relative_to(tmp_path/'processed/soybean_export_inspections')
    assert seen[0]['status'].is_relative_to(tmp_path/'update_status')


def test_positions_use_current_object_identity_and_ignore_expired_scopes(tmp_path,monkeypatch):
    from agri_research_agent.positions import formal_store as store
    formal=tmp_path/'processed/commodity_positions';pool=formal/store.DIRECTORY/'objects';pool.mkdir(parents=True)
    raw=b'authenticated current snapshot object'
    sha=hashlib.sha256(raw).hexdigest();(pool/sha).write_bytes(raw)
    selected={'index_sha256':'a'*64}
    status={'position_store':selected,'published':True,'latest_dates':{
        'soybean/domestic/M':'2026-10-21','soybean/domestic/M2609':'2026-09-11',
        'soybean/foreign/cbot_soybean/combined':'2026-10-13'}}
    path=store.status_path(formal);path.parent.mkdir(parents=True);path.write_text(json.dumps(status))
    monkeypatch.setattr(store,'pointer',lambda _:selected)
    monkeypatch.setattr(store,'index',lambda *args:{'files':{'soybean/releases/current/snapshot.json':{
        'kind':'object','sha256':sha,'size_bytes':len(raw)}}})
    # Loading/serializing the full archive is expressly forbidden here.
    monkeypatch.setattr(store,'read_snapshot',lambda *_:(_ for _ in ()).throw(AssertionError('full archive read')))
    assert Reader(inputs(tmp_path),NOW).read('positions_M').actual=='2026-10-21'
    assert Reader(inputs(tmp_path),NOW).read('foreign_positions_soybean').actual=='2026-10-13'
    (pool/sha).write_bytes(b'changed current object')
    with pytest.raises(ValueError,match='identity invalid'):
        Reader(inputs(tmp_path),NOW).read('positions_M')


def test_manual_cnf_uses_native_business_key_and_leaves_cleared_quotes_blank(tmp_path):
    from agri_research_agent.import_profit.cnf_store import CnfQuoteRecord,CNF_SCHEMA
    from agri_research_agent.import_profit.models import BusinessKey
    source=tmp_path/'02_configs';source.mkdir()
    (source/'import_profit_soybean.yaml').write_text('origins:\n  - code: brazil\n')
    rows=[]
    for day,value in [(date(2026,10,21),150.0),(date(2026,10,22),None)]:
        key=BusinessKey(day,'soybean','brazil',2026,12,['brazil'],'soybean')
        rows.append(CnfQuoteRecord(key,value,'manual_ui',NOW,'batch-1').as_row())
    pq.write_table(pa.Table.from_pylist(rows,schema=CNF_SCHEMA),tmp_path/'manual_cnf_quotes.parquet')
    assert Reader(inputs(tmp_path),NOW).read('manual_soybean').actual=='2026-10-21'


def test_weather_uses_native_lowercase_crop_and_includes_soil_cutoff(tmp_path):
    config=tmp_path/'02_configs';config.mkdir()
    (config/'cotton_weather_ind.yaml').write_text('cotton_weather:\n  crop: cotton\n  country: IND\n  weighted_aggregation: false\n  regions:\n    - key: state_a\n',encoding='utf-8')
    observations=tmp_path/'observations.parquet'
    pd.DataFrame([dict(country='IND',crop='cotton',region='state_a',metric=metric,
        valid_date=date(2026,10,21),data_family='observation',value=2.0) for metric in ['precipitation','temperature_max']]).to_parquet(observations,index=False)
    soil=tmp_path/'soil.parquet'
    pd.DataFrame([dict(series_id='soil.ind.cotton.a',business_date=date(2026,10,20),value_percent=30.0)]).to_parquet(soil,index=False)
    binding=dict(country='IND',crop='cotton',region='state_a',series_id='soil.ind.cotton.a')
    (tmp_path/'soil_bindings.json').write_text(json.dumps({'bindings':[binding]}))
    reader=Reader(inputs(tmp_path),NOW)
    reader.cache['weather']=SimpleNamespace(directory=tmp_path,observations_path=observations,soil_observations_path=soil)
    value=reader.read('weather_IND_COTTON')
    assert value.actual=='2026-10-20' and not value.error
    assert value.extra['series_dates']['IND/cotton/state_a/precipitation']=='2026-10-21'
