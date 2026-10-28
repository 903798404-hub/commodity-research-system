from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'05_apps'))
import computation_cache as cache
spec=importlib.util.spec_from_file_location('precompute_host_test',ROOT/'09_deploy/page_monitor/precompute.py')
host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)


def prepared(tmp_path):
    image={'git_commit':'a'*40,'git_tree':'b'*40};key='c'*64
    directory=tmp_path/'output';directory.mkdir();path=directory/(key+'.json')
    cache._record_derived(path,image,key,b'PNG')
    config={'producer':{'commit':image['git_commit'],'tree':image['git_tree']}}
    result={'status':'PREPARED','image':image,'files':{path.name:sha256(path.read_bytes()).hexdigest()}}
    return directory,path,config,result


def test_exact_staged_bytes_are_required_before_publication(tmp_path):
    directory,path,config,result=prepared(tmp_path)
    assert host.validate_outputs(directory,result,config)==path.stat().st_size
    path.write_text('{}')
    with pytest.raises(ValueError):host.validate_outputs(directory,result,config)


@pytest.mark.parametrize('change',['wrong_image','wrong_key','extra_file','missing_file','wrong_hash','wrong_schema'])
def test_untrusted_or_incomplete_stage_is_not_published(tmp_path,change):
    directory,path,config,result=prepared(tmp_path)
    if change=='wrong_image':result['image']['git_commit']='d'*40
    if change=='wrong_key':
        body=json.loads(path.read_bytes());body['key']='e'*64;path.write_text(json.dumps(body))
        result['files'][path.name]=sha256(path.read_bytes()).hexdigest()
    if change=='extra_file':(directory/'unexpected.json').write_text('{}')
    if change=='missing_file':path.unlink()
    if change=='wrong_hash':result['files'][path.name]='f'*64
    if change=='wrong_schema':
        body=json.loads(path.read_bytes());body['schema_version']='untrusted';path.write_text(json.dumps(body))
        result['files'][path.name]=sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):host.validate_outputs(directory,result,config)


def test_precompute_template_only_queues_derived_work_and_preserves_clock():
    units=host.render_units('/opt/market-data-releases/test/source','/etc/market-data/page-precompute/test.json')
    assert 'OnCalendar=*-*-* *:00,15,30,45:00 Asia/Shanghai' in units['market-data-page-precompute.timer']
    assert 'Unit=market-data-task-enqueue@precompute.service' in units['market-data-page-precompute.timer']
    assert '--publish' in units['market-data-page-precompute.service']
    with pytest.raises(ValueError):host.render_units('/opt/path with spaces','/etc/market-data/config.json')


def test_worker_retains_the_actual_activated_package_selector(monkeypatch,tmp_path):
    from agri_research_agent.market_data import activated_runtime as reader
    env=host.worker_environment({'container_data_root':'/runtime/01_data'},
        {'PUBLIC_DATA_SERVER_STORE_ROOT':'/runtime/01_data/public-data-server-store','TZ':'Asia/Shanghai','UNRELATED_SECRET':'not forwarded'})
    assert env=={'PUBLIC_MARKET_DATA_RUNTIME_ROOT':'/runtime/01_data',
        'PUBLIC_DATA_SERVER_STORE_ROOT':'/runtime/01_data/public-data-server-store','TZ':'Asia/Shanghai'}
    activated=tmp_path/'actual-activated-package'
    def resolve(path):
        assert path=='/runtime/01_data/public-data-server-store'
        return activated
    monkeypatch.setattr(reader,'resolve_server_current_data_root',resolve)
    monkeypatch.setenv('PUBLIC_DATA_SERVER_STORE_ROOT',env['PUBLIC_DATA_SERVER_STORE_ROOT'])
    assert reader.resolve_public_data_root('/loose-directory-must-not-be-used')==activated


def test_unmounted_store_is_rejected_instead_of_substituting_another_source():
    with pytest.raises(ValueError,match='outside'):
        host.worker_environment({'container_data_root':'/runtime/01_data'},
            {'PUBLIC_DATA_SERVER_STORE_ROOT':'/unmounted/other-source'})
