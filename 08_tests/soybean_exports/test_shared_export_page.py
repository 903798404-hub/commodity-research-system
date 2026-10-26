from pathlib import Path
import copy,json,os,sys
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest
from agri_research_agent.soybean_exports import research as shared
from agri_research_agent.soybean_exports.fas import fas_paths
from agri_research_agent.soybean_exports.fgis import fgis_paths
from test_research_and_isolation import make_research_frames,make_usda

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'05_apps'))
import soybean_exports_page as page
import research_overview_page as overview

@pytest.fixture(autouse=True)
def caches():
    shared.clear_shared_export_cache();page._shared_export_figure_service().clear()
    yield
    shared.clear_shared_export_cache();page._shared_export_figure_service().clear()

@pytest.fixture
def inputs(tmp_path,monkeypatch):
    make_research_frames(tmp_path);usda=make_usda(tmp_path);runtime=tmp_path/'runtime'
    monkeypatch.setenv(page.RUNTIME_ROOT_ENV,str(runtime))
    monkeypatch.setattr(page,'USDA_PROJECT_ROOT',usda)
    return runtime,usda

def load(inputs):
    return shared.load_shared_export_page_snapshot(inputs[0],usda_project_root=inputs[1])

def reference(inputs):
    return shared.load_soybean_export_page_payload(inputs[0],usda_project_root=inputs[1])

def test_complete_payload_and_13_figures_match_original_and_are_reused(monkeypatch,inputs):
    expected=reference(inputs);counts={'read':0,'payload':0,'fgis':0,'fas':0}
    for module,name,key in [(pd,'read_parquet','read'),(shared,'load_soybean_export_page_payload','payload'),(page,'build_fgis_figures','fgis'),(page,'build_fas_figures','fas')]:
        original=getattr(module,name)
        def call(*a,_original=original,_key=key,**kw):
            counts[_key]+=1;return _original(*a,**kw)
        monkeypatch.setattr(module,name,call)
    for _ in range(2):
        payload,identity=load(inputs);assert payload==expected and identity is not None
        fgis=page._shared_export_figure_service()(identity['fgis'],'fgis',_section=payload['fgis'])
        fas=page._shared_export_figure_service()((identity['fas'],identity['psd']),'fas',_section=payload['fas'])
        assert len(fgis)==4 and len(fas[0])+len(fas[1])+1==9
    original_fgis=page.build_fgis_figures(expected['fgis']);original_fas=page.build_fas_figures(expected['fas'])
    for old,new in zip(original_fgis+original_fas[0]+original_fas[1]+[original_fas[2]],fgis+fas[0]+fas[1]+[fas[2]],strict=True):
        assert json.loads(old.to_json())==json.loads(new.to_json())
    assert counts=={'read':2,'payload':1,'fgis':2,'fas':2}

def test_current_page_and_overview_entry_share_same_payload(inputs,monkeypatch):
    calls=[];original=shared.load_soybean_export_page_payload
    def read(*a,**kw):calls.append(1);return original(*a,**kw)
    monkeypatch.setattr(shared,'load_soybean_export_page_payload',read)
    assert overview.load_current_export_page_payload is page.load_current_export_page_payload
    assert page.load_current_export_page_payload()==overview.load_current_export_page_payload()==reference(inputs)
    assert len(calls)==2  # One service load and the uncached reference above.

def test_preserved_mtime_and_size_status_revision_invalidates(inputs):
    path=fas_paths(inputs[0],'test')['status'];body=json.loads(path.read_bytes());body['test_revision']='one'
    path.write_text(json.dumps(body),encoding='utf-8');stat=path.stat();first,_=load(inputs)
    body['test_revision']='two';path.write_text(json.dumps(body),encoding='utf-8');os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    assert path.stat().st_size==stat.st_size
    second,_=load(inputs);assert second==reference(inputs)
    assert first['fas']['status']['test_revision']=='one' and second['fas']['status']['test_revision']=='two'

def test_psd_current_and_snapshot_revision_without_version_mtime_invalidates(inputs):
    first,_=load(inputs);root=inputs[1]/'public/data'
    for path in [root/'matrix/2222000_US.json',root/'snapshots/usda_psd/2026-07/matrix/2222000_US.json']:
        stat=path.stat();path.write_text(path.read_text(encoding='utf-8').replace('4136.8','4236.8'),encoding='utf-8')
        os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns));assert path.stat().st_size==stat.st_size
    second,_=load(inputs);assert second==reference(inputs)
    assert first['kpis']['current_my_sales_progress_pct']!=second['kpis']['current_my_sales_progress_pct']

@pytest.mark.parametrize('role',['fgis','fas'])
def test_warm_missing_source_degrades_independently_and_recovers(inputs,role):
    original,_=load(inputs);path=(fgis_paths if role=='fgis' else fas_paths)(inputs[0],'test')['stable']
    body=path.read_bytes();stat=path.stat();path.unlink()
    missing,_=load(inputs);assert missing==reference(inputs) and missing[role] is None
    peer='fas' if role=='fgis' else 'fgis';assert missing[peer] is not None
    path.write_bytes(body);os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    assert load(inputs)[0]==original

def test_warm_corrupt_manifest_and_psd_do_not_hide_peer(inputs):
    load(inputs);manifest=fas_paths(inputs[0],'test')['stable_manifest'];record=json.loads(manifest.read_bytes());record['sha256']='f'*64
    manifest.write_text(json.dumps(record),encoding='utf-8')
    result,_=load(inputs);assert result==reference(inputs) and result['fgis'] and result['fas'] is None
    record['sha256']=shared.sha256_file(fas_paths(inputs[0],'test')['stable']);manifest.write_text(json.dumps(record),encoding='utf-8')
    (inputs[1]/'public/data/matrix/2222000_US.json').unlink()
    result,_=load(inputs);assert result==reference(inputs) and result['fgis'] and result['fas']
    assert result['kpis']['current_my_sales_progress_pct'] is None

def test_continuously_changing_fas_keeps_fgis_and_never_caches_transient_failure(inputs,monkeypatch):
    original=shared._load_published_source;path=fas_paths(inputs[0],'test')['status']
    def read(paths,validator):
        result=original(paths,validator)
        if paths['status']==path:
            stat=path.stat();os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns+1000000))
        return result
    monkeypatch.setattr(shared,'_load_published_source',read)
    payload,identity=load(inputs)
    assert identity is None and payload['fgis'] and payload['fas'] is None
    assert 'changed during read' in payload['errors']['fas']
    monkeypatch.setattr(shared,'_load_published_source',original)
    recovered,identity=load(inputs);assert identity is not None and recovered==reference(inputs)

def test_psd_change_during_fallback_leaves_only_denominator_unavailable(inputs,monkeypatch):
    def changed(*a,**kw):raise shared._ExportInputChanged('force bounded fallback')
    monkeypatch.setattr(shared,'_shared_export_payload_service',lambda:changed)
    original=shared.read_usda_psd_soybean_exports_mt
    def read(*a,**kw):
        result=original(*a,**kw);path=inputs[1]/'public/data/report_version.json';stat=path.stat()
        os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns+1000000));return result
    monkeypatch.setattr(shared,'read_usda_psd_soybean_exports_mt',read)
    payload,identity=load(inputs);assert identity is None and payload['fgis'] and payload['fas']
    assert payload['kpis']['current_my_sales_progress_pct'] is None
    assert payload['fas']['current_summary']['sales_progress_denominator'] is None

def test_caller_mutation_cannot_change_cached_facts_or_figures(inputs):
    payload,identity=load(inputs);original=copy.deepcopy(payload)
    payload['fgis']['series'].clear();assert load(inputs)[0]==original
    figure=page._shared_export_figure_service()(identity['fgis'],'fgis',_section=original['fgis'])
    expected=json.loads(figure[0].to_json());figure[0].update_layout(title='caller edit')
    assert json.loads(page._shared_export_figure_service()(identity['fgis'],'fgis',_section=original['fgis'])[0].to_json())==expected

def test_real_page_rerun_reuses_all_charts_and_overview_payload(inputs,monkeypatch):
    counts={'fgis':0,'fas':0}
    for name,role in [('build_fgis_figures','fgis'),('build_fas_figures','fas')]:
        original=getattr(page,name)
        def build(*a,_original=original,_role=role,**kw):counts[_role]+=1;return _original(*a,**kw)
        monkeypatch.setattr(page,name,build)
    app=AppTest.from_string('from soybean_exports_page import render_soybean_exports_page\nrender_soybean_exports_page()',default_timeout=30).run()
    assert not app.exception and len(app.get('plotly_chart'))==13
    assert overview.load_current_export_page_payload()==reference(inputs)
    app.run();assert not app.exception and len(app.get('plotly_chart'))==13
    assert counts=={'fgis':1,'fas':1}
