from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'05_apps'))
import computation_cache as cache

IMAGE={'git_commit':'a'*40,'git_tree':'b'*40}


def pure_png(frame,*,mode='original'):
    return b'PNG'+str(frame.value.sum()).encode()+mode.encode()


def test_background_record_roundtrip_is_safe_json_not_pickle(tmp_path):
    frame=pd.DataFrame({'value':[None,0,1.]})
    key=cache._derived_key(pure_png,(frame,),{'mode':'original'})
    path=tmp_path/(key+'.json');result=pure_png(frame)
    cache._record_derived(path,IMAGE,key,result)
    raw=path.read_bytes();payload=json.loads(raw)
    assert payload['result']['kind']=='bytes'
    assert cache._parse_derived(raw,IMAGE,key)==result
    assert cache._parse_derived(raw,{**IMAGE,'git_commit':'c'*40},key) is None
    assert cache._parse_derived(raw,IMAGE,'d'*64) is None


def test_complete_input_revision_options_types_and_missingness_change_keys():
    frame=pd.DataFrame({'value':np.arange(60000,dtype=float)})
    key=cache._derived_key(pure_png,(frame,),{'mode':'original'})
    frame.iloc[-1,0]+=1
    assert cache._derived_key(pure_png,(frame,),{'mode':'original'})!=key
    assert cache._derived_key(pure_png,(frame,),{'mode':'changed'})!=key
    assert cache._derived_key(pure_png,(pd.DataFrame({'value':[None]}),),{})!=cache._derived_key(pure_png,(pd.DataFrame({'value':[0.]}),),{})


@pytest.mark.parametrize('change',['result','hash','schema','key','image'])
def test_corrupt_or_wrong_version_never_becomes_a_hit(tmp_path,change):
    key='c'*64;path=tmp_path/(key+'.json');cache._record_derived(path,IMAGE,key,b'original')
    payload=json.loads(path.read_bytes())
    if change=='result':payload['result']['body']='bm90IG9yaWdpbmFs'
    if change=='hash':payload['result_sha256']='f'*64
    if change=='schema':payload['schema_version']='unknown'
    if change=='key':payload['key']='d'*64
    if change=='image':payload['image']['git_tree']='e'*40
    assert cache._parse_derived(json.dumps(payload).encode(),IMAGE,key) is None


def test_plotly_rendered_json_preserves_all_values_dates_labels_and_nulls():
    import plotly.graph_objects as go
    figure=go.Figure(go.Scatter(x=[datetime(2026,10,2),datetime(2026,10,3)],y=[0,None],name='原口径'))
    figure.update_layout(xaxis_title='日期',yaxis_title='%')
    encoded=cache._encode_derived(figure);restored=cache._decode_derived(encoded)
    assert json.loads(figure.to_json())==json.loads(restored.to_json())
    restored.data[0].name='caller modification'
    assert cache._decode_derived(encoded).data[0].name=='原口径'


def test_missing_or_untrusted_cache_uses_original_body(monkeypatch):
    monkeypatch.setattr(cache,'_derived_release',lambda:IMAGE)
    monkeypatch.setattr(cache,'_derived_path',lambda *args,**kwargs:None)
    frame=pd.DataFrame({'value':[0,5]})
    assert cache.precomputed_chart(pure_png)(frame)==pure_png(frame)


def test_website_consumer_never_records(monkeypatch):
    monkeypatch.delenv('MARKET_DATA_PRECOMPUTE_MODE',raising=False)
    monkeypatch.setattr(cache,'_derived_release',lambda:IMAGE)
    monkeypatch.setattr(cache,'_derived_path',lambda *args,**kwargs:None)
    monkeypatch.setattr(cache,'_record_derived',lambda *args:pytest.fail('website wrote derived state'))
    assert cache.precomputed_chart(pure_png)(pd.DataFrame({'value':[0]})).startswith(b'PNG')


def test_corrupt_recursive_json_falls_back(monkeypatch):
    monkeypatch.setattr(cache,'_derived_release',lambda:IMAGE)
    monkeypatch.setattr(cache,'_derived_path',lambda *args,**kwargs:Path('/unused'))
    def broken(*args):raise RecursionError('broken derived JSON')
    monkeypatch.setattr(cache,'_read_derived',broken)
    frame=pd.DataFrame({'value':[1]})
    assert cache.precomputed_chart(pure_png)(frame)==pure_png(frame)


def test_oversized_record_is_not_retained(tmp_path,monkeypatch):
    monkeypatch.setattr(cache,'MAX_RESULT_BYTES',200)
    path=tmp_path/('c'*64+'.json');cache._record_derived(path,IMAGE,'c'*64,b'x'*1000)
    assert not path.exists()
