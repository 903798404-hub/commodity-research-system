from datetime import date
import importlib
import json
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'05_apps'))
import computation_cache as cache


@pytest.fixture(autouse=True)
def empty_cache():
    cache.clear_computation_cache()
    yield
    cache.clear_computation_cache()


def test_complete_large_frame_revision_and_mutable_result_are_isolated():
    calls=[]
    @cache.cache_computation
    def calculate(frame, *, mode):
        calls.append(mode)
        return {'frame':frame.copy(), 'sum':frame.value.sum(), 'mode':mode}
    data=pd.DataFrame({'value':range(60_000)})
    first=calculate(data,mode='sum')
    first['frame'].iloc[0,0]=-99
    again=calculate(data,mode='sum')
    pd.testing.assert_frame_equal(again['frame'],data)
    assert calls==['sum']
    revised=data.copy(); revised.iloc[59_997,0]+=1
    assert calculate(revised,mode='sum')['sum']==again['sum']+1
    assert len(calls)==2


@pytest.mark.parametrize('revision', ['index','dtype','columns','null','zero','attrs','order'])
def test_full_frame_identity_includes_schema_missingness_and_metadata(revision):
    calls=[]
    @cache.cache_computation
    def calculate(frame):
        calls.append(1)
        return frame
    frame=pd.DataFrame({'a':pd.Series([1,2],dtype='Int64')})
    calculate(frame); calculate(frame)
    revised=frame.copy()
    if revision=='index':revised.index=[2,3]
    elif revision=='dtype':revised=revised.astype('float64')
    elif revision=='columns':revised.columns=['b']
    elif revision=='null':revised.iloc[0,0]=pd.NA
    elif revision=='zero':revised.iloc[0,0]=0
    elif revision=='attrs':revised.attrs['source']='revised'
    elif revision=='order':revised=revised.iloc[::-1]
    pd.testing.assert_frame_equal(calculate(revised),revised)
    assert len(calls)==2


def test_same_callable_shared_between_consumers_and_distinct_closures_isolated():
    calls=[]
    def original(value):calls.append(value);return [value]
    page=cache.cache_computation(original)
    overview=cache.cache_computation(original)
    assert page(0)==overview(0)==[0] and calls==[0]
    def factory(offset):
        @cache.cache_computation
        def calculate(value):return value+offset
        return calculate
    assert factory(1)(4)==5 and factory(2)(4)==6


def test_main_script_reexecution_reuses_the_same_pure_computation():
    scope={'__name__':'streamlit_script','calls':[]}
    code=compile('def calculate(value):\n    calls.append(value)\n    return [value]\n','streamlit_script.py','exec')
    exec(code,scope)
    first=cache.cache_computation(scope['calculate'])
    assert first(3)==[3]
    exec(code,scope)
    second=cache.cache_computation(scope['calculate'])
    assert second(3)==[3] and scope['calls']==[3]


def test_dates_options_zero_clear_and_dataclass_inputs_do_not_collide():
    calls=[]
    @cache.cache_computation
    def calculate(day, cnf, *, quota):
        calls.append((day,cnf,quota))
        return dict(day=day,cnf=cnf,quota=quota)
    for day,cnf,quota in [(date(2026,10,27),0,'in'),(date(2026,10,27),None,'in'),
                         (date(2026,10,28),0,'in'),(date(2026,10,27),0,'out')]:
        expected=dict(day=day,cnf=cnf,quota=quota)
        assert calculate(day,cnf,quota=quota)==calculate(day,cnf,quota=quota)==expected
    assert len(calls)==4


def test_failed_calculation_is_retried_without_stale_fallback():
    calls=[]
    @cache.cache_computation
    def calculate(value):
        calls.append(value)
        if len(calls)<2:raise ValueError('unavailable')
        return value
    with pytest.raises(ValueError,match='unavailable'):calculate(7)
    assert calculate(7)==calculate(7)==7 and calls==[7,7]


def test_large_or_unserializable_values_remain_functional_and_uncached(monkeypatch):
    calls=[]
    @cache.cache_computation
    def calculate(value):calls.append(1);return value
    monkeypatch.setattr(cache,'MAX_INPUT_BYTES',16)
    value='x'*100
    assert calculate(value)==calculate(value)==value and len(calls)==2
    monkeypatch.setattr(cache,'MAX_INPUT_BYTES',16*1024*1024)
    callback=lambda:5
    assert calculate(callback) is callback
    monkeypatch.setattr(cache,'MAX_RESULT_BYTES',16)
    assert calculate(value)==calculate(value)==value and len(calls)==5


def test_payload_byte_budget_evicts_individual_lru_results(monkeypatch):
    monkeypatch.setattr(cache,'MAX_CACHE_BYTES',280)
    calls=[]
    @cache.cache_computation
    def calculate(key):calls.append(key);return key+'x'*100
    calculate('a');calculate('b');calculate('b');calculate('c')
    assert calls==['a','b','c']
    assert cache._budget_bytes<=280
    calculate('b')
    assert calls==['a','b','c']
    calculate('a')
    assert calls==['a','b','c','a']


def test_entry_growth_remains_bounded_even_when_native_storage_evicts_first():
    calls=[]
    @cache.cache_computation
    def calculate(value):calls.append(value);return value
    for value in range(cache.MAX_ENTRIES+10):assert calculate(value)==value
    assert len(cache._budget_entries)<=cache.MAX_ENTRIES
    assert cache._budget_bytes<=cache.MAX_CACHE_BYTES
    before=len(calls)
    assert calculate(0)==0 and len(calls)==before+1


def test_same_input_concurrent_requests_compute_once_without_blocking_other_keys():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered=Event();release=Event();calls=[]
    @cache.cache_computation
    def calculate(value):
        calls.append(value)
        if value=='slow':entered.set();assert release.wait(timeout=5)
        return [value]
    with ThreadPoolExecutor(max_workers=3) as pool:
        first=pool.submit(calculate,'slow')
        assert entered.wait(timeout=3)
        second=pool.submit(calculate,'slow')
        other=pool.submit(calculate,'other')
        try:assert other.result(timeout=3)==['other']
        finally:release.set()
        left,right=first.result(timeout=3),second.result(timeout=3)
    assert left==right==['slow'] and left is not right
    assert calls.count('slow')==1 and calls.count('other')==1


def test_pure_import_does_not_initialize_streamlit():
    root=Path(__file__).resolve().parents[1]
    code="import sys; sys.path.insert(0,sys.argv[1]); import computation_cache; assert 'streamlit' not in sys.modules"
    subprocess.run([sys.executable,'-I','-B','-c',code,str(root/'05_apps')],check=True,timeout=30)


def test_plotly_array_properties_subplots_and_mutable_metadata_survive_cache():
    import numpy as np
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    @cache.cache_computation
    def build():
        figure=make_subplots(rows=1,cols=2)
        figure.add_trace(go.Scatter(x=np.array([1,2]),y=np.array([0.,np.nan]),customdata=np.array([[1,2],[3,4]])),row=1,col=1)
        figure.update_layout(meta={'source':'original'})
        return figure
    first=build()
    assert isinstance(first.data[0].y,np.ndarray) and first.data[0].y[0]==0
    assert np.isnan(first.data[0].y[1]) and first.data[0].customdata[1][1]==4
    first.update_layout(meta={'source':'caller'})
    second=build()
    assert second.layout.meta=={'source':'original'}
    second.add_trace(go.Scatter(y=[3]),row=1,col=2)
    assert len(build().data)==1


def test_real_fx_chart_reuses_work_and_preserves_full_figure(monkeypatch):
    apps=Path(__file__).resolve().parents[1]/'05_apps'
    monkeypatch.syspath_prepend(str(apps))
    page=importlib.import_module('foreign_fx_page')
    frame=pd.DataFrame({'currency':['BRL']*4,'date':[date(2024,2,29),date(2024,3,1),date(2026,3,1),date(2026,3,3)],'local_per_usd':[5.,5.1,5.2,5.0]})
    original=page.build_seasonality_figure.__wrapped__
    expected=json.loads(original(frame,'BRL',[2024,2026],current_year=2026).to_json())
    calls=[]
    source=page.seasonality
    def observed(*args,**kwargs):calls.append(1);return source(*args,**kwargs)
    monkeypatch.setattr(page,'seasonality',observed)
    first=page.build_seasonality_figure(frame,'BRL',[2024,2026],current_year=2026)
    first.update_layout(title='caller edit')
    assert json.loads(page.build_seasonality_figure(frame,'BRL',[2024,2026],current_year=2026).to_json())==expected
    assert len(calls)==1
    page.build_seasonality_figure(frame,'BRL',[2024,2026],current_year=2026,normalize=True)
    assert len(calls)==2


def test_sugar_quotas_months_and_historical_gaps_are_preserved(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'05_apps'))
    page=importlib.import_module('sugar_import_profit_page')
    rows=[dict(period='202701',date='2026-10-01',margin_in=0,margin_out=None),
          dict(period='202701',date='2026-10-15',margin_in=5,margin_out=-2)]
    for month in range(1,13):
        for quota in ['配额内','配额外']:
            figure=page.build_sugar_seasonal_figure(rows,month,quota)
            assert json.loads(figure.to_json())==json.loads(page.build_sugar_seasonal_figure.__wrapped__(rows,month,quota).to_json())
            if month==1:
                assert list(figure.data[0].y)==([0,None,5] if quota=='配额内' else [None,None,-2])
            else:assert not figure.data


def test_brazil_tab_roundtrip_reuses_already_built_figures(monkeypatch,tmp_path):
    from streamlit.testing.v1 import AppTest
    from agri_research_agent.pipelines import brazil_soy as domain
    import brazil_soy_page as page
    stamp='2026-10-07T00:00:00+00:00'
    records=[]
    for year in range(2021,2027):
        season=f'{year}/{year+1}'
        for metric in domain.METRICS:
            for region in domain.REGIONS:
                records.append(domain.observation(season,region,metric,date(year,10,2),10.,source_url=domain.SOURCE_URL,
                    source_sha256='a'*64,locator='synthetic cache fixture',retrieved_at=stamp))
        for metric in domain.STAGES:
            records.append(domain.observation(season,'BR',metric,date(year,10,2),10.,source_url=domain.SOURCE_URL,
                source_sha256='a'*64,locator='synthetic cache fixture',retrieved_at=stamp))
    path=tmp_path/domain.STABLE_RELATIVE_PATH;path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(schema_version=domain.SCHEMA,generated_at=stamp,records=records,import_notes=[])),encoding='utf-8')
    monkeypatch.setenv('PUBLIC_MARKET_DATA_RUNTIME_ROOT',str(tmp_path))
    counts=[]
    original=page.seasonal_figure.__wrapped__
    def observed(*args,**kwargs):counts.append((args[1],args[2]));return original(*args,**kwargs)
    monkeypatch.setattr(page,'seasonal_figure',cache.cache_computation(observed))
    app=AppTest.from_string('from brazil_soy_page import render_brazil_soy_page\nrender_brazil_soy_page()',default_timeout=90).run()
    assert not app.exception and len(counts)==13
    for tab in ['收割进度','生长进度']:
        app.session_state['brazil-metric-tabs']=tab;app.run(timeout=90)
        assert not app.exception
    before=len(counts)
    app.session_state['brazil-metric-tabs']='播种进度';app.run(timeout=90)
    assert not app.exception and len(app.get('plotly_chart'))==13
    assert len(counts)==before,'Returning to unchanged planting view rebuilt its figures'
