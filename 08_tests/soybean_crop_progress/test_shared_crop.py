from datetime import date
from pathlib import Path
import copy,os,shutil
import pandas as pd
import pytest
import yaml
from agri_research_agent.summary_engine import crop
from agri_research_agent.pipelines.soybean_crop_comparison import build_dashboard_comparisons,load_display_config
from agri_research_agent.summary_engine.rules import DEFAULT_CONFIG,load_summary_rules

ROOT=Path(__file__).resolve().parents[2]

@pytest.fixture(autouse=True)
def caches():
    crop.clear_shared_crop_cache()
    yield
    crop.clear_shared_crop_cache()

@pytest.fixture
def pair(tmp_path):
    source=ROOT/'08_tests/fixtures/summary/soybean_crop_progress'
    paths=[]
    for name in ['soybeans_crop_progress_weekly_2021_2026.parquet','soybeans_crop_condition_weekly_2021_2026.parquet']:
        target=tmp_path/name;shutil.copyfile(source/name,target);paths.append(target)
    return tuple(paths)

def counted(monkeypatch):
    calls={'read':0,'compare':0,'summary':0}
    for module,name,key in [(pd,'read_parquet','read'),(crop,'build_metric_comparison','compare'),(crop,'build_crop_summary','summary')]:
        original=getattr(module,name)
        def call(*args,_original=original,_key=key,**kwargs):
            calls[_key]+=1;return _original(*args,**kwargs)
        monkeypatch.setattr(module,name,call)
    return calls

def test_both_pages_share_reads_comparisons_and_summary_with_exact_original_results(monkeypatch,pair):
    progress,condition=[pd.read_parquet(p) for p in pair];config=load_display_config(crop.DEFAULT_DISPLAY_CONFIG)
    identity=crop._crop_pair_identity(*pair)
    old_tables=build_dashboard_comparisons(progress,condition,config)
    old_summary=crop.build_crop_summary(progress,condition,source_identity=identity,display_config=config)
    calls=counted(monkeypatch)
    loaded=crop.load_shared_crop_snapshot(*pair)
    pd.testing.assert_frame_equal(loaded[0],progress);pd.testing.assert_frame_equal(loaded[1],condition)
    assert loaded[2]==identity
    tables=crop.load_shared_crop_comparisons(*pair,expected_source_identity=identity)
    summary=crop.load_shared_crop_summary(*pair,expected_source_identity=identity)
    for old,new in zip(old_tables,tables,strict=True):
        assert old.definition==new.definition and old.baseline_week==new.baseline_week
        pd.testing.assert_frame_equal(old.rows,new.rows)
    assert summary.to_dict()==old_summary.to_dict()
    assert crop.load_shared_crop_summary(*pair).to_dict()==old_summary.to_dict()
    assert calls=={'read':2,'compare':10,'summary':1}

def test_aliases_share_numeric_work_but_keep_original_source_identity(monkeypatch,pair):
    calls=counted(monkeypatch);first=crop.load_shared_crop_summary(*pair)
    sub=pair[0].parent/'sub';sub.mkdir()
    aliases=tuple(sub/'..'/p.name for p in pair)
    other=crop.load_shared_crop_summary(*aliases)
    assert first.facts==other.facts and first.source_identity!=other.source_identity
    assert other.source_identity==crop._crop_pair_identity(*aliases)
    assert calls=={'read':2,'compare':10,'summary':2}

def test_preserved_mtime_content_revision_invalidates_without_sampling(monkeypatch,pair):
    first=crop.load_shared_crop_summary(*pair);calls=counted(monkeypatch)
    frame=pd.read_parquet(pair[0]);stat=pair[0].stat()
    index=frame.index[frame['value_pct'].notna()][0];frame.loc[index,'value_pct']=float(frame.loc[index,'value_pct'])+1
    frame.to_parquet(pair[0],index=False);os.utime(pair[0],ns=(stat.st_atime_ns,stat.st_mtime_ns))
    second=crop.load_shared_crop_summary(*pair)
    assert first.source_identity['progress']['sha256']!=second.source_identity['progress']['sha256']
    assert calls['summary']==1 and calls['compare']==10

def test_cache_hit_still_checks_file_presence(pair):
    crop.load_shared_crop_summary(*pair);pair[0].unlink()
    with pytest.raises(FileNotFoundError):crop.load_shared_crop_summary(*pair)

def test_pair_change_during_load_is_not_cached(monkeypatch,pair):
    original=pd.read_parquet
    def read(path,*args,**kwargs):
        result=original(path,*args,**kwargs)
        if str(path)==str(pair[0]):
            stat=pair[1].stat();os.utime(pair[1],ns=(stat.st_atime_ns,stat.st_mtime_ns+1000000))
        return result
    monkeypatch.setattr(pd,'read_parquet',read)
    with pytest.raises(ValueError,match='pair changed'):crop.load_shared_crop_data(*pair)

def test_loaded_page_version_is_required_before_comparison_or_summary(pair):
    *_,identity=crop.load_shared_crop_snapshot(*pair)
    stat=pair[0].stat();os.utime(pair[0],ns=(stat.st_atime_ns,stat.st_mtime_ns+1000000))
    for load in [crop.load_shared_crop_comparisons,crop.load_shared_crop_summary]:
        with pytest.raises(ValueError,match='loaded page version'):load(*pair,expected_source_identity=identity)

def test_display_content_changes_invalidate_tables_and_summary(monkeypatch,tmp_path,pair):
    config=load_display_config(crop.DEFAULT_DISPLAY_CONFIG);path=tmp_path/'display.yaml'
    path.write_text(yaml.safe_dump(config,allow_unicode=True),encoding='utf-8');stat=path.stat()
    first=crop.load_shared_crop_comparisons(*pair,path)
    crop.load_shared_crop_summary(*pair,display_config_path=path)
    config['national']['display_name']='全国测试'
    path.write_text(yaml.safe_dump(config,allow_unicode=True),encoding='utf-8');os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    second=crop.load_shared_crop_comparisons(*pair,path)
    assert first[0].rows.iloc[0]['display_region']!=second[0].rows.iloc[0]['display_region']
    assert second[0].rows.iloc[0]['display_region']=='全国测试'

def test_rule_content_and_utc_day_invalidate_summary_only(monkeypatch,tmp_path,pair):
    path=tmp_path/'rules.yaml';rules=copy.deepcopy(load_summary_rules())
    path.write_text(yaml.safe_dump(rules,allow_unicode=True),encoding='utf-8');stat=path.stat()
    calls=counted(monkeypatch)
    first=crop.load_shared_crop_summary(*pair,rules_path=path,today=date(2026,7,13))
    crop.load_shared_crop_summary(*pair,rules_path=path,today=date(2026,7,14))
    rules['rule_version']='revision-test'
    path.write_text(yaml.safe_dump(rules,allow_unicode=True),encoding='utf-8');os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    revised=crop.load_shared_crop_summary(*pair,rules_path=path,today=date(2026,7,14))
    assert revised.rule_version!=first.rule_version and calls=={'read':2,'compare':10,'summary':3}
    path.write_text('schema_version: 99\nrule_version: bad\n',encoding='utf-8')
    with pytest.raises(ValueError,match='规则配置版本无效'):crop.load_shared_crop_summary(*pair,rules_path=path)

def test_returned_frames_rows_and_facts_are_isolated(pair):
    progress,condition=crop.load_shared_crop_data(*pair)
    original=progress.copy(deep=True);progress.loc[0,'value_pct']=9999
    pd.testing.assert_frame_equal(crop.load_shared_crop_data(*pair)[0],original)
    table=crop.load_shared_crop_comparisons(*pair);original_rows=table[0].rows.copy(deep=True)
    table[0].rows.loc[0,'display_region']='caller-edit'
    pd.testing.assert_frame_equal(crop.load_shared_crop_comparisons(*pair)[0].rows,original_rows)
    summary=crop.load_shared_crop_summary(*pair);summary.facts['metrics'].clear()
    assert crop.load_shared_crop_summary(*pair).facts['metrics']


def test_warm_operation_detects_input_change_after_cached_result(monkeypatch,pair):
    crop.load_shared_crop_summary(*pair)
    original=crop._crop_pair_identity;calls=[]
    def identity(*paths):
        result=original(*paths);calls.append(1)
        if len(calls)==2:result['condition']['sha256']='changed'
        return result
    monkeypatch.setattr(crop,'_crop_pair_identity',identity)
    with pytest.raises(ValueError,match='pair changed'):crop.load_shared_crop_summary(*pair)


def test_real_crop_page_and_overview_share_service(monkeypatch,tmp_path):
    import sys
    from streamlit.testing.v1 import AppTest
    sys.path.insert(0,str(ROOT/'05_apps'))
    import soybean_crop_progress_page as page
    import research_overview_page as overview
    root=tmp_path/'01_data/processed/soybean_crop_progress';root.mkdir(parents=True)
    fixture=ROOT/'08_tests/fixtures/summary/soybean_crop_progress'
    for role in ['progress','condition']:
        shutil.copyfile(fixture/f'soybeans_crop_{role}_weekly_2021_2026.parquet',root/f'soybeans_crop_{role}_weekly.parquet')
    resolver=page.resolve_processed_crop_paths
    monkeypatch.setattr(page,'resolve_processed_crop_paths',lambda config:resolver(config,processed_dir=root))
    monkeypatch.setattr(overview,'PROJECT_ROOT',tmp_path)
    calls=counted(monkeypatch)
    app=AppTest.from_string('from soybean_crop_progress_page import render_soybean_crop_progress_page\nrender_soybean_crop_progress_page()',default_timeout=40).run()
    assert not app.exception and len(app.tabs)==6
    summary=overview.load_crop_overview_summary()
    assert summary.module=='soybean_crop' and calls=={'read':2,'compare':10,'summary':1}
    app.run();assert not app.exception and calls=={'read':2,'compare':10,'summary':1}
