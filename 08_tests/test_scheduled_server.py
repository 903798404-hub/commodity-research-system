from copy import deepcopy
import importlib.util
import json
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
        'public_store_owner':{'uid':1000,'gid':1000} if job=='akshare' else None,
        'allocation_root':'/var/lib/market-data/production-runtime/test',
        'producer':{'commit':'a'*40,'tree':'b'*40,'origin':'https://github.com/903798404-hub/commodity-research-system.git'}}


@pytest.mark.parametrize('job,clocks',[
    ('akshare',['Mon..Fri *-*-* 16:30:00 Asia/Shanghai','Mon..Fri *-*-* 18:30:00 Asia/Shanghai','Mon..Fri *-*-* 22:30:00 Asia/Shanghai']),
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


def test_incremental_public_requires_pinned_protected_pool_and_readonly_base_mount(runner,linux_host,tmp_path):
    c=config(runner,'akshare')
    c.update(policy=None,policy_sha256=None,initial_baseline=str(runner.RUNTIME/'initial-baselines/test'),
             initial_baseline_sha256='e'*64,incremental_public_packages=True,
             public_object_pool='/var/lib/market-data/storage-hygiene/package-retention/objects')
    runner.validate_config(linux_host,c)
    for field,value in [('public_object_pool','/tmp/objects'),('incremental_public_packages','true'),('job','positions_foreign')]:
        with pytest.raises((ValueError,TypeError)):
            runner.validate_config(linux_host,{**c,field:value})
    baseline=tmp_path/'baseline';baseline.mkdir()
    package_id='public-current-'+'a'*24
    (baseline/'request.json').write_text(json.dumps({'public_pointer':{'package_id':package_id}}))
    command=runner.collector_command(c,tmp_path)
    mounts=[command[i+1] for i,v in enumerate(command) if v=='--mount']
    assert any(f'releases{Path("/")}{package_id}' in v and v.endswith(f'dst=/public-baseline/{package_id},readonly') for v in mounts)
    assert command[command.index('--network')+1]=='none'
    assert not any('dst=/public-baseline' in v for v in runner.collector_command(c,tmp_path,public_preflight=True))


def test_stock_api_is_explicit_domestic_only_and_requires_protected_secret(runner,linux_host):
    value=config(runner,'positions_domestic')
    runner.validate_config(linux_host,value)
    value['positions_stock_api']=True
    with pytest.raises((ValueError,TypeError)):runner.validate_config(linux_host,value)
    value['secret_file']='/etc/market-data/production-input-secrets/stock-api.env'
    runner.validate_config(linux_host,value)
    assert runner.credential_key(value)=='STOCK_API_TOKEN'
    for job in ('positions_foreign','akshare'):
        bad={**value,'job':job}
        with pytest.raises(ValueError):runner.validate_config(linux_host,bad)
    value['positions_stock_api']='true'
    with pytest.raises(ValueError):runner.validate_config(linux_host,value)


def test_stock_token_is_stdin_only_and_extra_secret_keys_are_rejected(runner,tmp_path,monkeypatch):
    host=runner.helpers()[1];secret=tmp_path/'stock.env';secret.write_text('STOCK_API_TOKEN=private-test-token\n')
    secret.chmod(0o600)
    # Windows mode bits do not model the production ACL; test the parser with a protected-file proxy.
    class ProtectedFile:
        def stat(self):return SimpleNamespace(st_size=100,st_mode=0o600)
        def read_text(self,**kwargs):return secret.read_text(**kwargs)
    monkeypatch.setattr(host,'_protected',lambda p:ProtectedFile())
    value={**config(runner,'positions_domestic'),'positions_stock_api':True,'secret_file':str(secret)}
    assert json.loads(runner.secret_input(host,value))=={'STOCK_API_TOKEN':'private-test-token'}
    command=runner.collector_command(value,tmp_path/'work')
    assert not any('private-test-token' in arg or 'STOCK_API_TOKEN' in arg or str(secret) in arg for arg in command)
    secret.write_text('STOCK_API_TOKEN=private-test-token\nOTHER_KEY=forbidden\n')
    with pytest.raises(ValueError):runner.secret_input(host,value)


def test_collectors_have_no_formal_writable_mount_or_credentials_in_args(runner,tmp_path):
    baseline=tmp_path/'run/baseline';baseline.mkdir(parents=True)
    host=runner.helpers()[1]
    (baseline/'request.json').write_text(json.dumps({'baseline_files':{relative:None
        for domain in ('canola_exports','soybean_crop_progress','soybean_export_sales')
        for relative in host.DOMAIN_CONTRACTS[domain]['baseline']}}))
    for job in runner.JOBS:
        command=runner.collector_command(config(runner,job),tmp_path/'run')
        assert '--read-only' in command and '--user' in command and '65532:65532' in command
        assert '--pull' in command and 'never' in command and '-i' in command
        mounts=[command[i+1] for i,x in enumerate(command) if x=='--mount']
        extra = int(job.startswith('positions_'))
        assert len(mounts)==3+extra and sum(m.endswith(',readonly') for m in mounts)==2+extra
        if extra:
            assert mounts[-1] == ('type=bind,src='+config(runner,job)['allocation_root']+
                '/01_data/processed/commodity_positions/positions_archive.json,dst=/baseline/'
                '01_data/processed/commodity_positions/positions_archive.json,readonly')
        assert not any('secrets' in m for m in mounts)
        assert not any('production-runtime' in m for m in (mounts[:-1] if extra else mounts))
        assert not any('API_KEY' in x or 'docker.sock' in x for x in command)


def test_corrupt_or_extra_baseline_content_rejected_before_network(runner,tmp_path):
    host=runner.helpers()[1]
    value={'files':{'a':{'sha256':'a'*64,'size_bytes':3}},'contents':{'a':'YmFk'}}
    with pytest.raises(ValueError): runner.copy_snapshot(host,value,tmp_path/'corrupt',{})
    value['contents']['other']='unexpected'
    with pytest.raises(ValueError): runner.copy_snapshot(host,value,tmp_path/'extra',{})


def test_positions_baseline_keeps_only_empty_mountpoint_and_small_status(runner, tmp_path):
    import base64
    host=runner.helpers()[1]
    raw=b'x'*(1024*1024)
    contents={host.POSITIONS_STABLE:raw,host.POSITIONS_STATUS:b'{}'}
    snapshot={'files':{k:{'sha256':host.sha256_bytes(v),'size_bytes':len(v)} for k,v in contents.items()},
              'contents':{k:base64.b64encode(v).decode() for k,v in contents.items()}}
    destination=tmp_path/'baseline'
    runner.copy_snapshot(host,snapshot,destination,{'job':'positions_domestic'})
    assert (destination/host.POSITIONS_STABLE).read_bytes()==b''
    assert (destination/host.POSITIONS_STATUS).read_bytes()==b'{}'
    assert (destination/Path(host.POSITIONS_STABLE).parent/'.position-store').is_dir()
    assert sum(p.stat().st_size for p in destination.rglob('*') if p.is_file())<256


@pytest.mark.parametrize('job',['soybean_crop_progress','soybean_export_sales'])
def test_table_baselines_use_only_empty_mountpoints_and_exact_readonly_binds(runner,tmp_path,job):
    import base64
    host=runner.helpers()[1]
    relative_paths=host.DOMAIN_CONTRACTS[job]['baseline']
    contents={relative:(b'x'*(1024*1024) if relative.endswith('.parquet') else b'{}')
              for relative in relative_paths}
    snapshot={'files':{key:{'sha256':host.sha256_bytes(raw),'size_bytes':len(raw)} for key,raw in contents.items()},
              'contents':{key:base64.b64encode(raw).decode() for key,raw in contents.items()}}
    work=tmp_path/'work';work.mkdir()
    destination=work/'baseline'
    runner.copy_snapshot(host,snapshot,destination,{'job':job,'baseline_files':snapshot['files']})
    assert sum(path.stat().st_size for path in destination.rglob('*') if path.is_file())<16384
    for relative in relative_paths:
        assert (destination/relative).read_bytes()==(b'' if relative.endswith('.parquet') else b'{}')
    configured=config(runner,job)
    command=runner.collector_command(configured,work)
    mounts=[command[index+1] for index,value in enumerate(command) if value=='--mount']
    for relative in relative_paths:
        if relative.endswith('.parquet'):
            assert f'type=bind,src={configured["allocation_root"]}/{relative},dst=/baseline/{relative},readonly' in mounts
    assert [mount for mount in mounts if not mount.endswith(',readonly')]==[
        f'type=bind,src={work/"collection"},dst=/output']


def test_positions_collector_mounts_the_protected_append_store_readonly(runner,tmp_path,monkeypatch):
    c=config(runner,'positions_domestic')
    c['allocation_root']=str(tmp_path/'allocation')
    pool=Path(c['allocation_root'])/'01_data/processed/commodity_positions/.position-store'
    pool.mkdir(parents=True)
    fx,host=runner.helpers()
    checks=[]
    monkeypatch.setattr(host,'_protected_tree',lambda p:checks.append(p) or p)
    monkeypatch.setattr(runner,'helpers',lambda:(fx,host))
    command=runner.collector_command(c,tmp_path/'work')
    mounts=[command[i+1] for i,v in enumerate(command) if v=='--mount']
    assert checks==[pool]
    assert f'type=bind,src={pool},dst=/baseline/{Path("01_data/processed/commodity_positions/.position-store")},readonly' in mounts


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


@pytest.mark.parametrize('job',['positions_domestic','positions_foreign','canola_exports'])
def test_network_worker_builds_domain_package_after_collection(monkeypatch,tmp_path,job):
    from agri_research_agent.automation import production_data_delta as delivery
    from agri_research_agent.automation import positions_scheduled_collection,production_data_delta_exports
    from agri_research_agent.positions import delivery as positions_codec
    from agri_research_agent.canola_exports import delivery as exports_codec
    from agri_research_agent.canola_exports import increment as exports_increment
    collector=load('candidate_collect_package','09_deploy/scheduled_server/collect.py')
    host=delivery._host_contract()
    baseline=tmp_path/'baseline'; baseline.mkdir()
    output=tmp_path/'output'; output.mkdir()
    previous={'baseline':'verified'}
    for relative in (host.POSITIONS_STABLE,host.EXPORTS_STABLE,host.EXPORTS_SOURCES):
        p=baseline/relative; p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(previous))
    candidate=tmp_path/'weekly.json';candidate.write_text(json.dumps({'candidate':'collected'}))
    monkeypatch.setattr(positions_scheduled_collection,'collect_bundle',lambda *a,**k:({'objects':{},'updates':{}},[]))
    monkeypatch.setattr(positions_codec,'resolve_increment',lambda *a:{'candidate':'collected'})
    monkeypatch.setattr(positions_codec,'observations',lambda *a:{'business_changed':False})
    monkeypatch.setattr(production_data_delta_exports,'_collect',lambda *a:(candidate,{'source':'verified'}))
    monkeypatch.setattr(exports_codec,'observations',lambda *a:{'business_changed':False})
    monkeypatch.setattr(exports_increment,'build',lambda *a:({'updates':[]},{'sources':[]}))
    calls=[]
    monkeypatch.setattr(collector,'package',lambda request,output,payloads,semantic,host:calls.append((request['job'],payloads,semantic)))
    request={'job':job,'business_date':'2026-10-11','baseline_files':{
        name:delivery._identity(baseline/name) for name in (host.EXPORTS_STABLE,host.EXPORTS_SOURCES)}
        if job=='canola_exports' else {}}
    result=collector.prepare(request,baseline,output,{})
    assert len(calls)==1 and calls[0][0]==job
    assert set(calls[0][1])==({'positions_archive.json'} if job.startswith('positions_') else {'weekly.json','source_evidence.json'})
    assert result['observations']['business_changed'] is False


def test_crop_worker_stages_approved_display_definition_before_pipeline(monkeypatch,tmp_path):
    from agri_research_agent.pipelines import soybean_crop_weekly_update
    collector=load('candidate_crop_config','09_deploy/scheduled_server/collect.py')
    monkeypatch.setattr(collector,'SOURCE',ROOT)
    output=tmp_path/'output';output.mkdir()
    def check_pipeline(**kwargs):
        actual=kwargs['project_root']/'02_configs/soybean_crop_progress_display.yaml'
        assert actual.read_bytes()==(ROOT/'02_configs/soybean_crop_progress_display.yaml').read_bytes()
        assert kwargs['dry_run'] is True and kwargs['git_head']=='a'*40
        return {'status':'out_of_season'}
    monkeypatch.setattr(soybean_crop_weekly_update,'run_soybean_crop_weekly_update',check_pipeline)
    result=collector.prepare({'job':'soybean_crop_progress','producer':{'commit':'a'*40},'baseline_files':{}},tmp_path,output,{'NASS_API_KEY':'test'})
    assert result=={'out_of_season':True}


@pytest.mark.parametrize('job',['soybean_crop_progress','soybean_export_sales'])
def test_weekly_delivery_receives_full_existing_baseline_without_losing_prefix(monkeypatch,tmp_path,job):
    from agri_research_agent.automation import production_data_delta as delivery
    from agri_research_agent.pipelines import soybean_crop_weekly_update
    from agri_research_agent.soybean_exports import fas
    collector=load('candidate_weekly_baseline','09_deploy/scheduled_server/collect.py')
    monkeypatch.setattr(collector,'SOURCE',ROOT)
    baseline=tmp_path/'baseline';baseline.mkdir()
    output=tmp_path/'output';output.mkdir()
    relative='01_data/processed/'+job+'/existing.parquet'
    path=baseline/relative;path.parent.mkdir(parents=True);path.write_bytes(b'verified existing input')
    identity=delivery._identity(path)
    monkeypatch.setattr(soybean_crop_weekly_update,'run_soybean_crop_weekly_update',lambda **k:{'status':'no_change'})
    monkeypatch.setattr(fas,'run_fas_pipeline',lambda **k:{'status':'candidate_only','validation':{'latest_week':'2026-10-01'}})
    received=[]
    monkeypatch.setattr(delivery,'collect_delta',lambda domain,project,output,producer,baseline,**k:received.append(baseline))
    request={'job':job,'run_id':'verified-run','producer':{'commit':'a'*40},'baseline_files':{relative:identity}}
    collector.prepare(request,baseline,output,{'FAS_EXPORT_SALES_API_KEY':'test'})
    assert received==[{relative.removeprefix('01_data/'):identity}]


def test_public_worker_timeout_removes_only_its_named_container(monkeypatch):
    import subprocess
    public=load('public_cleanup','09_deploy/scheduled_server/public.py')
    calls=[]
    def run(command,**kwargs):
        calls.append(command)
        if command[1]=='run':raise subprocess.TimeoutExpired(command,1200)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(public.subprocess,'run',run)
    with pytest.raises(subprocess.TimeoutExpired):public.invoke(['docker','run','--rm','image','python','--validate-only'])
    name=calls[0][calls[0].index('--name')+1]
    assert name.startswith('scheduled-public-') and calls[1]==['docker','rm','-f',name]


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
    monkeypatch.setattr(runner,'disk_budget',lambda *a:{})
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
    monkeypatch.setattr(runner,'disk_budget',lambda *a:{})
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
    policy={'policy_id':'snapshot-fixture-001','domain':domain,'allocation_root':str(allocation),
            'approved_producer':{},'validation_image':{'image_id':'pinned'}}
    monkeypatch.setattr(host,'EVIDENCE_ROOT',tmp_path/'evidence')
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


def test_disk_budget_accounts_for_peak_copies_and_reserves_other_tasks(runner):
    host=runner.helpers()[1]
    baseline={'files':{'public':{'size_bytes':200*1024**2},'missing':None}}
    exact=(5*200+768)*1024**2
    stats=SimpleNamespace(f_bavail=exact,f_frsize=1,f_favail=1001)
    value=runner.disk_budget(host,config(runner,'akshare'),baseline,stats)
    assert value['required_free_bytes']==exact and value['reserved_bytes']==768*1024**2
    stats.f_bavail-=1
    with pytest.raises(ValueError,match='capacity insufficient'):runner.disk_budget(host,config(runner,'akshare'),baseline,stats)
    stats.f_bavail=exact;stats.f_favail=1000
    with pytest.raises(ValueError,match='capacity insufficient'):runner.disk_budget(host,config(runner,'akshare'),baseline,stats)


@pytest.mark.parametrize('tamper',[False,True])
@pytest.mark.parametrize('no_change',[False,True])
def test_cleanup_only_verified_duplicates_and_keeps_retained_releases(runner,monkeypatch,tmp_path,tamper,no_change):
    public=load('scheduled_public_cleanup_test','09_deploy/scheduled_server/public.py');host=runner.helpers()[1]
    work=tmp_path/'runtime/accepted';store=tmp_path/'store';old='public-current-'+'a'*24;candidate=old if no_change else 'public-current-'+'b'*24
    relative='public-package/'+old;upload=store/'incoming'/(candidate+'.upload-'+work.name)
    copies=[(work/'baseline'/relative/'data',store/'releases'/old/'data'),
            (work/'collection/packages'/candidate/'data',store/'releases'/candidate/'data')]
    if no_change:copies.append((upload/'data',store/'releases'/candidate/'data'))
    for target,reference in copies:
        target.mkdir(parents=True,exist_ok=True);reference.mkdir(parents=True,exist_ok=True)
        (target/'quote').write_bytes(b'preserved quote');(reference/'quote').write_bytes(b'preserved quote')
    if no_change:(upload/'manifest.json').write_text('retained original upload manifest')
    (work/'result.json').write_text('retained result')
    unrelated=store/'incoming/another-task';unrelated.mkdir(parents=True);(unrelated/'data').write_bytes(b'other task')
    monkeypatch.setattr(public,'RUNTIME',tmp_path/'runtime')
    monkeypatch.setattr(host,'_under',lambda p,root,**k:Path(p) if Path(p).is_relative_to(root) else (_ for _ in ()).throw(ValueError('outside')))
    monkeypatch.setattr(public.shutil.rmtree,'avoids_symlink_attacks',True)
    extra={'public_pointer':{'package_id':old},'public_package_relative':relative}
    if tamper:
        (copies[1][0]/'quote').write_bytes(b'unverified')
        with pytest.raises(ValueError,match='duplicate differs'):public.release_duplicates(host,work,store,extra,candidate,upload,no_change)
        assert all(target.exists() for target,_ in copies)
    else:
        proof=public.release_duplicates(host,work,store,extra,candidate,upload,no_change)
        assert proof['status']=='PASS' and proof['removed_directories']==(3 if no_change else 2)
        assert all(not target.exists() for target,_ in copies)
    assert all(reference.exists() for _,reference in copies)
    assert (work/'result.json').read_text()=='retained result' and (unrelated/'data').read_bytes()==b'other task'
    if no_change:assert (upload/'manifest.json').read_text()=='retained original upload manifest'
