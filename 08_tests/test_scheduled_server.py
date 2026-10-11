from copy import deepcopy
import importlib.util
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]


def load(name,relative):
    spec=importlib.util.spec_from_file_location(name,ROOT/relative)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


@pytest.fixture
def runner(): return load('scheduled_runner_test','09_deploy/scheduled_server/run.py')


@pytest.fixture
def linux_host(runner,monkeypatch):
    host=runner.helpers()[1]
    def under(value,parent,**kwargs):
        path=PurePosixPath(str(value).replace('\\','/')); root=PurePosixPath(str(parent).replace('\\','/'))
        if not path.is_absolute() or '..' in path.parts or root not in path.parents:
            raise ValueError('outside protected Linux namespace')
        return path
    monkeypatch.setattr(host,'_under',under)
    return host


def config(runner,job):
    return {'schema_version':'scheduled-server-config/1','job':job,
        'policy':'/etc/market-data/production-data-delivery/test.json','policy_sha256':'c'*64,
        'image_id':'sha256:'+'d'*64,'data_disk_uuid':'109b359d-6e55-4911-a665-c67735dc0a10',
        'secret_file':'/etc/market-data/production-input-secrets/'+job+'.env' if job in runner.KEYS else None,
        'initial_baseline':None,'initial_baseline_sha256':None,
        'allocation_root':'/var/lib/market-data/production-runtime/test',
        'producer':{'commit':'a'*40,'tree':'b'*40,'origin':'https://github.com/903798404-hub/commodity-research-system.git'}}


@pytest.mark.parametrize('job,clocks',[
    ('akshare',['Mon..Fri *-*-* 16:30:00 Asia/Shanghai']),
    ('canola_exports',['*-*-* 08:30:00 Asia/Shanghai']),
    ('positions_domestic',['Mon..Fri *-*-* 16:35:00 Asia/Shanghai','Mon..Fri *-*-* 18:00:00 Asia/Shanghai']),
    ('positions_foreign',['*-*-* 07:45:00 Asia/Shanghai']),
    ('soybean_crop_progress',['Tue..Thu *-*-* 06:30:00 Asia/Shanghai']),
    ('soybean_export_sales',['Sat *-*-* 06:15:00 Asia/Shanghai'])])
def test_actual_schedules_preserved_and_no_success_skip(runner,job,clocks):
    service,timer=runner.render_units(job,'/approved/source','/etc/market-data/scheduled-server/test.json')
    assert [line.split('=',1)[1] for line in timer.splitlines() if line.startswith('OnCalendar=')]==clocks
    assert 'Persistent=true' in timer and 'RandomizedDelaySec=0' in timer
    assert 'retry-if-needed' not in service and '--publish' in service and '-I -B' in service
    assert runner.JOBS['positions_domestic'][0]==runner.JOBS['positions_foreign'][0]


@pytest.mark.parametrize('job',['nutstore_basis','foreign_fx','fgis','weather'])
def test_unrequested_jobs_cannot_be_rendered(runner,job):
    with pytest.raises(ValueError): runner.render_units(job,'/source','/config')


@pytest.mark.parametrize('field,value',[('job','nutstore_basis'),('secret_file','/tmp/key'),
    ('image_id','market-data:latest'),('allocation_root','/tmp/production'),('extra','ignored')])
def test_closed_config_rejects_other_tasks_or_unprotected_inputs(runner,linux_host,field,value):
    host=linux_host; c=config(runner,'soybean_crop_progress'); c[field]=value
    with pytest.raises((ValueError,TypeError)): runner.validate_config(host,c)


def test_public_job_cannot_borrow_delta_policy_or_other_credential(runner,linux_host):
    host=linux_host; c=config(runner,'akshare')
    with pytest.raises(ValueError): runner.validate_config(host,c)
    c.update(policy=None,policy_sha256=None,initial_baseline=str(runner.RUNTIME/'initial-baselines/test'),initial_baseline_sha256='e'*64)
    runner.validate_config(host,c)
    c['secret_file']='/etc/market-data/production-input-secrets/nass.env'
    with pytest.raises(ValueError): runner.validate_config(host,c)


def test_collectors_have_no_formal_writable_mount_or_credentials_in_args(runner,tmp_path):
    for job in runner.JOBS:
        command=runner.collector_command(config(runner,job),tmp_path/'run')
        assert '--read-only' in command and '--user' in command and '65532:65532' in command
        assert '--pull' in command and 'never' in command and '-i' in command
        mounts=[command[i+1] for i,x in enumerate(command) if x=='--mount']
        assert len(mounts)==3 and sum(m.endswith(',readonly') for m in mounts)==2
        assert not any('production-runtime' in m or 'secrets' in m for m in mounts)
        assert not any('API_KEY' in x or 'docker.sock' in x for x in command)


def test_corrupt_or_extra_baseline_content_rejected_before_network(runner,tmp_path):
    host=runner.helpers()[1]
    value={'files':{'a':{'sha256':'a'*64,'size_bytes':3}},'contents':{'a':'YmFk'}}
    with pytest.raises(ValueError): runner.copy_snapshot(host,value,tmp_path/'corrupt',{})
    value['contents']['other']='unexpected'
    with pytest.raises(ValueError): runner.copy_snapshot(host,value,tmp_path/'extra',{})


def test_domain_policy_drift_and_wrong_source_block_before_collection(runner,monkeypatch):
    c=config(runner,'positions_foreign'); _,host=runner.helpers()
    monkeypatch.setattr(host,'_git_identity',lambda p:{**c['producer'],'commit':'f'*40})
    with pytest.raises(ValueError): runner.check_identity(host,c)


def test_public_scope_preserves_basis_and_all_other_datasets():
    public=load('public_server_scope','09_deploy/scheduled_server/public.py')
    old={'files':[{'path':'consumer-artifacts/domestic-spread/a','sha256':'old'},
        {'path':'public-market-data/lutou-domestic-basis/a','sha256':'basis'}],
        'current_identities':{'basis':'identity'},'source_max_dates':{'basis':'2026-10-09'}}
    changed=deepcopy(old); changed['files'][0]['sha256']='new'
    public.assert_scope({'public_manifest':old},changed)
    changed['files'][1]['sha256']='changed basis'
    with pytest.raises(ValueError): public.assert_scope({'public_manifest':old},changed)


def test_local_host_transport_keeps_existing_remote_path_and_requires_linux_root(monkeypatch):
    transport=load('local_transport_test','04_scripts/transfer_public_data_package.py')
    observed=[]
    monkeypatch.setattr(transport,'_run',lambda args:observed.append(args))
    transport._ssh('approved-host',['cat','/file'])
    assert observed[-1][0]=='ssh' and 'approved-host' in observed[-1]
    monkeypatch.setattr(transport.sys,'platform','win32')
    with pytest.raises(RuntimeError): transport._ssh(None,['cat','/file'])


@pytest.mark.parametrize('job',['canola_exports','positions_domestic','positions_foreign','soybean_crop_progress','soybean_export_sales'])
def test_validation_failure_never_calls_formal_publish_and_preserves_failure_state(runner,monkeypatch,tmp_path,job):
    fx,host=runner.helpers(); c=config(runner,job); work=tmp_path/'test-run'; work.mkdir()
    monkeypatch.setattr(runner,'RUNTIME',tmp_path)
    monkeypatch.setattr(runner,'check_identity',lambda *a:None)
    monkeypatch.setattr(host,'snapshot_fx_baseline',lambda p:{'producer':c['producer'],'image_id':c['image_id'],
        'policy_sha256':c['policy_sha256'],'allocation_root':c['allocation_root'],'files':{},'contents':{}})
    monkeypatch.setattr(runner,'copy_snapshot',lambda *a:None)
    monkeypatch.setattr(runner,'collect',lambda *a:{'status':'PASS'})
    def reject(*args): raise ValueError('invalid candidate')
    monkeypatch.setattr(runner,'seal_delta',reject)
    monkeypatch.setattr(host,'publish',lambda *a:pytest.fail('publication must not run'))
    with pytest.raises(ValueError): runner.execute(fx,host,c,work,True)
    result=host.read_json(work/'result.json')
    assert result['status']=='FAIL' and result['published'] is False and result['error']['stage']=='offline_validation'
    assert host.read_json(tmp_path/(job+'-last-run.json'))==result


def test_partial_source_failure_keeps_delivery_status_but_cannot_pass_acceptance(runner,monkeypatch,tmp_path):
    fx,host=runner.helpers(); c=config(runner,'positions_foreign'); work=tmp_path/'test-run'; work.mkdir()
    monkeypatch.setattr(runner,'RUNTIME',tmp_path)
    monkeypatch.setattr(runner,'check_identity',lambda *a:None)
    monkeypatch.setattr(host,'snapshot_fx_baseline',lambda p:{'producer':c['producer'],'image_id':c['image_id'],
        'policy_sha256':c['policy_sha256'],'allocation_root':c['allocation_root'],'files':{},'contents':{}})
    monkeypatch.setattr(runner,'copy_snapshot',lambda *a:None)
    monkeypatch.setattr(runner,'collect',lambda *a:{'status':'PASS','attempts':[{'status':'failed'}]})
    monkeypatch.setattr(runner,'seal_delta',lambda *a:{'report_path':'/report','report_sha256':'e'*64})
    monkeypatch.setattr(fx,'check_mount',lambda *a:None)
    monkeypatch.setattr(host,'publish',lambda *a:{'status':'NO_CHANGE'})
    result=runner.execute(fx,host,c,work,True)
    assert result['status']=='PARTIAL_FAILURE' and result['delivery_status']=='NO_CHANGE'


@pytest.mark.parametrize('domain',['soybean_crop_progress','soybean_export_sales'])
def test_new_snapshot_domains_preserve_fixed_files_and_reject_incomplete_pair(runner,monkeypatch,tmp_path,domain):
    from contextlib import nullcontext
    host=runner.helpers()[1]
    allocation=tmp_path/'allocation'; allocation.mkdir()
    contract=host.DOMAIN_CONTRACTS[domain]
    for relative in contract['baseline']:
        path=allocation/relative; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(b'input')
    policy={'domain':domain,'allocation_root':str(allocation),'approved_producer':{},'validation_image':{'image_id':'pinned'}}
    monkeypatch.setattr(host,'_load_policy',lambda p:(policy,'a'*64))
    monkeypatch.setattr(host,'_protected',lambda p,**k:Path(p))
    monkeypatch.setattr(host,'_under',lambda p,*a,**k:Path(p))
    monkeypatch.setattr(host,'_lock',lambda p:nullcontext())
    monkeypatch.setattr(host,'_policy_unchanged',lambda *a:None)
    value=host.snapshot_fx_baseline('/policy')
    assert set(value['files'])==set(contract['baseline']) and value['domain']==domain
    assert all(identity['size_bytes']==5 for identity in value['files'].values())
    missing=host.CROP_CONDITION if domain=='soybean_crop_progress' else host.FAS_MANIFEST
    (allocation/missing).unlink()
    with pytest.raises(ValueError): host.snapshot_fx_baseline('/policy')
