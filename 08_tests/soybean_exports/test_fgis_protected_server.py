from pathlib import Path
import importlib.util
import json
import pytest
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def test_network_worker_never_receives_writable_production():
    host=load('fgis_new_host','09_deploy/fgis_server/run.py')
    config={'allocation_root':'/var/lib/market-data/production-runtime/test','image_id':'sha256:'+'a'*64}
    for replay in [False,True]:
        cmd=host.worker_command(config,Path('/owned/fgis-test'),replay=replay)
        assert cmd[cmd.index('--network')+1]==('none' if replay else 'bridge')
        mounts=[cmd[i+1] for i,v in enumerate(cmd) if v=='--mount']
        assert [v for v in mounts if 'dst=/runtime' in v]==['type=bind,src='+config['allocation_root']+',dst=/runtime,readonly']
        assert '--read-only' in cmd and cmd[cmd.index('--user')+1]=='65532:65532'
        assert sum('readonly' not in v for v in mounts)==1


@pytest.mark.parametrize('origin,commit,expected',[
    ('https://github.com/903798404-hub/commodity-research-system.git','a'*40,True),
    ('git@github.com:903798404-hub/commodity-research-system.git','a'*40,True),
    ('https://github.com/other/repository.git','a'*40,False),
    ('git@github.com:903798404-hub/commodity-research-system.git','c'*40,False)])
def test_approved_deploy_key_origin_and_exact_commit(origin,commit,expected):
    host=load('fgis_source_test','09_deploy/fgis_server/run.py')
    assert host.approved_source({'commit':commit,'tree':'b'*40,'origin':origin},
        {'commit':'a'*40,'tree':'b'*40},host.host_module().CANONICAL_ORIGINS) is expected

@pytest.mark.parametrize('with_history',[False,True])
def test_fgis_preparation_and_offline_source_replay_preserve_history(tmp_path,monkeypatch,with_history):
    fixtures=load('fgis_native_fixtures','08_tests/soybean_exports/test_fgis_yearly_source.py')
    from agri_research_agent.soybean_exports import fgis_server as worker
    from agri_research_agent.soybean_exports.fgis import run_fgis_pipeline,_parse_yearly_source
    class Adapter:
        def __init__(self,rows):self.rows=rows
        def fetch_soybeans(self,**kwargs):
            raw=fixtures.csv_bytes(self.rows)
            return _parse_yearly_source(content=raw,calendar_year=2026,source_file='CY2026.csv',
                source_url='https://fgisonline.ams.usda.gov/exportgrainreport/CY2026.csv',
                fetched_at='2026-10-22T00:00:00Z',http_status=200,content_length=len(raw),etag='test',
                last_modified='Mon, 19 Oct 2026 15:00:00 GMT',delivery='https')
    baseline=tmp_path/'baseline';rows=[fixtures.yearly_row()]
    run_fgis_pipeline(runtime_root=baseline,adapter=Adapter(rows),git_head='a'*40,batch_id='fgis-initial',publish=True)
    old_log=pd.DataFrame()
    if with_history:
        rows=[fixtures.yearly_row(mt='12')]
        run_fgis_pipeline(runtime_root=baseline,adapter=Adapter(rows),git_head='a'*40,batch_id='fgis-old-revision',publish=True)
        old_log=pd.read_parquet(baseline/worker.REVISION)
        assert not old_log.empty
    before={p:worker.identity(baseline/p) for p in worker.BASELINE_FILES}
    request={'run_id':'fgis-second','commit':'b'*40,'tree':'c'*40,'baseline':before}
    rows2=[fixtures.yearly_row(mt='15'),fixtures.yearly_row(thursday='20260806',cert_date='20260806',mt='20')]
    monkeypatch.setattr(worker,'FgisYearlyAdapter',lambda **kwargs:Adapter(rows2))
    candidate=tmp_path/'candidate';candidate.mkdir()
    result=worker.prepare(baseline,candidate,request)
    assert result['status']=='CANDIDATE' and result['validation']['latest_week']=='2026-08-06'
    new_log=pd.read_parquet(candidate/'project'/worker.REVISION)
    if with_history:pd.testing.assert_frame_equal(old_log,new_log.iloc[:len(old_log)].reset_index(drop=True),check_dtype=False,check_exact=True)
    assert len(new_log)>len(old_log)
    verify=tmp_path/'verify';verify.mkdir()
    checked=worker.prepare(baseline,verify,request,replay=candidate)
    assert checked['offline_replay']=='PASS'
    assert checked['revision_history']==result['revision_history']
    assert {p:worker.identity(baseline/p) for p in worker.BASELINE_FILES}==before
    revision_path=candidate/'project'/worker.REVISION;revision_bytes=revision_path.read_bytes()
    for row in sorted({0,len(old_log)}):
        tampered=new_log.copy();tampered.loc[row,'new_value']+=1;tampered.to_parquet(revision_path,index=False)
        bad_log=tmp_path/('bad-log-'+str(row));bad_log.mkdir()
        with pytest.raises(AssertionError):worker.prepare(baseline,bad_log,request,replay=candidate)
    revision_path.write_bytes(revision_bytes)
    source=candidate/result['source_relative_path'];source.write_bytes(source.read_bytes()+b'tampered')
    bad=tmp_path/'bad';bad.mkdir()
    with pytest.raises(ValueError,match='identity'):worker.prepare(baseline,bad,request,replay=candidate)

def test_provenance_paths_only_remap_owned_runtime_prefix():
    host=load('fgis_path_host','09_deploy/fgis_server/run.py')
    assert host.remap({'raw':'/output/project/01_data/raw/new','unrelated':'/safe/project/keep'})=={'raw':'/runtime/01_data/raw/new','unrelated':'/safe/project/keep'}


@pytest.mark.parametrize('with_history,failure',[
    (False,None),(False,'stale'),(False,'revision_stale'),(False,'status'),(False,'unknown'),
    (True,None),(True,'stale'),(True,'revision_stale'),(True,'status'),(True,'unknown'),
    (True,'revision_missing'),(True,'revision_tamper'),(False,'new_revision')])
def test_publication_cas_and_status_failure_restore_both_files(tmp_path,monkeypatch,with_history,failure):
    host=load('fgis_publish_test','09_deploy/fgis_server/run.py')
    allocation=tmp_path/'formal';work=tmp_path/'run';candidate=work/'collection/project'
    def seed(root,content):
        for relative in host.FILES:
            p=root/relative;p.parent.mkdir(parents=True,exist_ok=True)
            if relative==host.STABLE:p.write_bytes(content)
            elif relative==host.MANIFEST:p.write_text(json.dumps({'sha256':host.sha(root/host.STABLE)}))
            else:p.write_text(json.dumps({'last_success':{'source_latest_week':'2026-10-15'},'revision_count':0}))
    seed(allocation,b'previous');seed(candidate,b'new')
    if with_history:
        for root,value in [(allocation,b'prior-audit'),(candidate,b'prior-audit+new-audit')]:
            p=root/host.REVISION;p.parent.mkdir();p.write_bytes(value)
    elif failure=='new_revision':
        p=candidate/host.REVISION;p.parent.mkdir();p.write_bytes(b'first-audit')
    source=candidate/'01_data/raw'/host.DOMAIN/'fgis-run';source.mkdir(parents=True);(source/'source.csv').write_bytes(b'official')
    before={p:(allocation/p).read_bytes() if (allocation/p).exists() else None for p in host.BASELINE_FILES}
    request={'run_id':'fgis-run','baseline':{p:host.optional_identity(allocation/p) for p in host.BASELINE_FILES}}
    result={'files':{p:host.optional_identity(candidate/p) for p in host.BASELINE_FILES},'revision_history':{'baseline_identity':request['baseline'][host.REVISION]}}
    def exchange(left,right,flags):
        temporary=left.with_name('exchange-scratch');left.rename(temporary);right.rename(left);temporary.rename(right)
    native=host.host_module();monkeypatch.setattr(native,'_renameat2',exchange)
    monkeypatch.setattr(host,'host_module',lambda:native);monkeypatch.setattr(host,'seal',lambda path:None)
    if failure in {'stale','revision_stale'}:
        p=allocation/(host.STABLE if failure=='stale' else host.REVISION);p.parent.mkdir(exist_ok=True);p.write_bytes(b'other-publication')
        with pytest.raises(ValueError,match='drift'):host.publish({'allocation_root':str(allocation)},work,request,result)
        assert not (work/'backup').exists()
        return
    if failure=='unknown':
        (allocation/host.STABLE).parent.joinpath('unexpected.json').write_text('{}')
        with pytest.raises(ValueError,match='unexpected'):host.publish({'allocation_root':str(allocation)},work,request,result)
        assert not (work/'backup').exists()
        return
    if failure in {'revision_missing','revision_tamper'}:
        if failure=='revision_missing':(candidate/host.REVISION).unlink()
        else:(candidate/host.REVISION).write_bytes(b'changed-after-validation')
        with pytest.raises(ValueError,match='revision log'):host.publish({'allocation_root':str(allocation)},work,request,result)
        assert not (work/'backup').exists()
        return
    if failure=='status':
        monkeypatch.setattr(host,'atomic',lambda *args:(_ for _ in ()).throw(OSError('injected status failure')))
        with pytest.raises(OSError,match='injected'):host.publish({'allocation_root':str(allocation)},work,request,result)
        assert {p:(allocation/p).read_bytes() if (allocation/p).exists() else None for p in host.BASELINE_FILES}==before
    else:
        receipt=host.publish({'allocation_root':str(allocation)},work,request,result)
        assert receipt['status']=='PUBLISHED' and (allocation/host.STABLE).read_bytes()==b'new'
        assert (work/'backup/processed'/Path(host.STABLE).name).read_bytes()==b'previous'
        if with_history:
            assert (allocation/host.REVISION).read_bytes()==b'prior-audit+new-audit'
            assert (work/'backup/processed/revisions'/Path(host.REVISION).name).read_bytes()==b'prior-audit'
            assert (work/'backup/revision-audit'/Path(host.REVISION).name).read_bytes()==b'prior-audit+new-audit'
        elif failure=='new_revision':
            assert (allocation/host.REVISION).read_bytes()==b'first-audit'
            assert not (work/'backup/processed/revisions').exists()
    assert (allocation/'01_data/raw'/host.DOMAIN/'fgis-run/source.csv').read_bytes()==b'official'
