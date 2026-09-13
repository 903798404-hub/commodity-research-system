"""Release orchestration with inert Docker transports, never a production host."""
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('routine_release', ROOT/'04_scripts/runtime/routine_release.py')
routine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routine)
IMAGE = dict(commit='a'*40, tree='b'*40, image_id='sha256:'+'c'*64,
             repo_digests=[], release_id='service-release', oci_revision='a'*40, source_revision='validator')
PREVIOUS = dict(commit='d'*40, tree='e'*40, image_id='sha256:'+'f'*64, container_id='previous')


class Backend:
    project_id = 'service'
    def __init__(self, failure=None): self.calls=[]; self.failure=failure
    def hit(self, name):
        self.calls.append(name)
        if name == self.failure: raise routine.RoutineError(name)
        return 'PASS'
    def create(self, image, role): self.hit('create:'+role); self.image=copy.deepcopy(image)
    def grant_and_start(self, image, role): self.hit('fresh-grant:'+role); self.hit('start:'+role)
    def preflight(self): return self.hit('preflight')
    def health(self): return self.hit('health')
    def smoke(self): return self.hit('smoke')
    def assert_image(self, image): self.hit('image')
    def assert_data_readonly(self): self.hit('data-readonly')
    def cleanup(self): self.hit('cleanup')
    def verify_previous(self, previous): self.hit('previous')
    def rollback(self, previous): self.hit('rollback'); self.hit('fresh-grant:rollback')


def acceptance():
    return routine.candidate_acceptance(Backend(), IMAGE, base_commit='d'*40, ci_run='123')


def test_exact_candidate_to_deploy_without_build_or_old_grant():
    candidate = Backend()
    record = routine.candidate_acceptance(candidate, IMAGE, base_commit='d'*40, ci_run='123')
    production = Backend()
    result = routine.deploy_same_image(production, IMAGE, record, PREVIOUS, ci_run='123')
    assert record['result'] == result['result'] == 'PASS'
    assert candidate.image == production.image == IMAGE
    assert candidate.calls.index('fresh-grant:candidate_validation') < candidate.calls.index('start:candidate_validation')
    assert production.calls.index('fresh-grant:production') < production.calls.index('start:production')
    assert not any('build' in c or 'old-grant' in c for c in candidate.calls+production.calls)


@pytest.mark.parametrize('failure',['fresh-grant:candidate_validation','preflight','health','smoke','data-readonly','image','cleanup'])
def test_candidate_failure_never_authorizes_switch(failure):
    record = routine.candidate_acceptance(Backend(failure), IMAGE, base_commit='d'*40, ci_run='123')
    assert record['result'] == 'FAIL'
    production=Backend()
    with pytest.raises(routine.RoutineError):routine.deploy_same_image(production, IMAGE, record, PREVIOUS, ci_run='123')
    assert production.calls == []


@pytest.mark.parametrize('failure',['create:production','fresh-grant:production','preflight','health','smoke','data-readonly'])
def test_production_failure_records_failure_and_recovers(failure):
    b=Backend(failure)
    result=routine.deploy_same_image(b, IMAGE, acceptance(), PREVIOUS, ci_run='123')
    assert result['result']=='FAIL' and result['rollback']=='PASS'
    assert 'fresh-grant:rollback' in b.calls


@pytest.mark.parametrize('field',['commit','tree','image_id','release_id'])
def test_changed_target_identity_cannot_deploy(field):
    image={**IMAGE,field:'wrong'}
    with pytest.raises(routine.RoutineError):routine.deploy_same_image(Backend(),image,acceptance(),PREVIOUS,ci_run='123')


def test_high_risk_cannot_use_unsigned_routine_record(monkeypatch):
    classifier=SimpleNamespace(classify_release=lambda *a:{'RELEASE_RISK_CLASS':'STATEFUL_OR_INFRA'})
    monkeypatch.setattr(routine,'load',lambda *a:classifier)
    with pytest.raises(routine.RoutineError,match='HIGH_RISK'):
        routine.verify_routine_record(acceptance(),dict(approved_commit=IMAGE['commit'],approved_tree=IMAGE['tree'],
            image_id=IMAGE['image_id'],project_id='service'),ROOT)


def tables():
    return [dict(heading=s+' 盘面榨利', rows=[[f'2026-{m:02d}']+['—']*13 for m in range(1,13)]) for s in ('AM','PM')]


def test_real_page_html_shape_and_zero_not_null():
    assert routine.check_dom_tables(tables(),('AM','PM'))=='PASS'
    value=tables(); value[0]['rows'][0][1]='0.00'
    assert routine.check_dom_tables(value,('PM',))=='PASS'
    with pytest.raises(routine.RoutineError,match='FAKE_VALUE'):routine.check_dom_tables(value,('AM','PM'))


@pytest.mark.parametrize('mutation',['empty','eleven','duplicate','wrong-month','missing-pm'])
def test_dom_missing_months_fail(mutation):
    t=tables()
    if mutation=='empty':t[0]['rows']=[]
    elif mutation=='eleven':t[1]['rows'].pop()
    elif mutation=='duplicate':t.append(copy.deepcopy(t[0]))
    elif mutation=='wrong-month':t[0]['rows'][0][0]='2026-02'
    else:t.pop()
    with pytest.raises(routine.RoutineError):routine.check_dom_tables(t,('AM','PM'))


def test_browser_smoke_reads_real_locator_transport(monkeypatch):
    import sys
    events=[]
    class Page:
        def goto(self,url,**kwargs):events.append(url)
        def locator(self,selector):events.append(selector);return self
        def nth(self,n):return self
        def wait_for(self,**kwargs):pass
        def evaluate_all(self,script):assert 'querySelectorAll' in script;return tables()
    browser=SimpleNamespace(new_page=Page,close=lambda:events.append('close'))
    class Playwright:
        def __enter__(self):return SimpleNamespace(chromium=SimpleNamespace(launch=lambda **k:browser))
        def __exit__(self,*a):pass
    monkeypatch.setitem(sys.modules,'playwright.sync_api',SimpleNamespace(sync_playwright=Playwright))
    assert routine.browser_smoke('http://127.0.0.1:1234?workspace_page=import_profit',['AM','PM'])=='PASS'
    assert events[-1]=='close'


@pytest.mark.parametrize('mutation',['image','build','hook','data-write'])
def test_concrete_docker_adapter_rejects_before_create(tmp_path,mutation):
    service=dict(image=IMAGE['image_id'],entrypoint=['app'],read_only=True,volumes=[])
    if mutation=='image':service['image']='mutable:latest'
    elif mutation=='build':service['build']={'context':'.'}
    elif mutation=='hook':service['post_start']=[{'command':'refresh'}]
    else:service['volumes']=[dict(type='bind',source=str(tmp_path),target='/app/data',read_only=False)]
    host=SimpleNamespace(_protected_path=lambda p,**k:p,_json=lambda raw:raw)
    request={'production':dict(compose='compose',environment='env',policy_template='policy',project_directory=str(tmp_path),writable_root=str(tmp_path))}
    b=routine.DockerSession(request,None,host,{},dict(project_id='service',service_id='service',entrypoint=['app'],runtime_roots=[]))
    calls=[]
    def compose(spec,*args):calls.append(args);return SimpleNamespace(stdout={'services':{'service':service}})
    b.compose=compose
    with pytest.raises(routine.RoutineError):b.create(IMAGE,'production')
    assert calls==[('config','--format','json')]


def test_cli_build_once_and_existing_image_no_rebuild(tmp_path,monkeypatch):
    import json
    request=dict(source_root=str(tmp_path),project_id='service',target_commit='a'*40,target_tree='b'*40,
        base_commit='d'*40,ci_record=str(tmp_path/'ci.json'),production_data_mutation=False,rebuild=False,
        build_authorized=True,build_directory=str(tmp_path/'attempt'))
    req=tmp_path/'request.json'; req.write_text(json.dumps(request))
    ci=dict(candidate=dict(commit='a'*40,tree='b'*40),final_result='PASS',checks=dict(TECHNICAL_VALIDATION='PASS',workflow_run_id='123'))
    (tmp_path/'ci.json').write_text(json.dumps(ci))
    calls=[]
    host=SimpleNamespace(_require_linux_root=lambda:None,require_protected_authority_source=lambda:None,
        _protected_path=lambda p,**k:p,_json=json.loads)
    engine=SimpleNamespace(_project=lambda *a:dict(project_id='service',runtime_contract='manifest'),
        source_contract=lambda *a:({},dict(project_id='service',service_id='service'),dict(commit='a'*40,tree='b'*40)),
        require_builder=lambda:None,create_archive_context=lambda *a:None,_exclude_candidate_inputs=lambda *a:None,
        build_image=lambda *a:calls.append('build') or IMAGE['image_id'],_canonical=lambda r:json.dumps(r).encode(),
        _write_new=lambda p,raw:p.write_bytes(raw))
    risk=SimpleNamespace(require_source=lambda *a,**k:None)
    monkeypatch.setattr(routine,'load',lambda path,name:host if 'host_authorization' in path else engine if 'validate_target' in path else risk)
    monkeypatch.setattr(routine,'require_routine',lambda *a:{})
    monkeypatch.setattr(routine,'image_identity',lambda *a:IMAGE)
    assert routine.main(['build','--request',str(req),'--output',str(tmp_path/'build.json')])==0
    with pytest.raises(FileExistsError):routine.main(['build','--request',str(req),'--output',str(tmp_path/'build-again.json')])
    assert calls==['build']


def test_expired_old_grant_not_read_in_routine():
    previous={**PREVIOUS,'historical_grant':{'expires_at':'2000-01-01T00:00:00Z'}}
    assert routine.deploy_same_image(Backend(),IMAGE,acceptance(),previous,ci_run='123')['result']=='PASS'


def test_even_declared_writable_business_root_is_not_routine_write_permission(tmp_path):
    contract=dict(project_id='service',runtime_roots=[dict(role='data',access='rw',container_path='/data')])
    b=routine.DockerSession({},None,None,{},contract)
    b.spec={'writable_root':str(tmp_path)}
    with pytest.raises(routine.RoutineError,match='DATA_WRITE'):
        b._check_mounts([dict(type='bind',source=str(tmp_path/'data'),target='/data',read_only=False)])


def test_smoke_cannot_target_another_server():
    b=routine.DockerSession({'application_smoke':dict(kind='dom',path='//other-server',selectors={'h1':1})},None,None,{},dict(project_id='service'))
    with pytest.raises(routine.RoutineError,match='ACTUAL_CONTAINER'):b.smoke()


def test_smoke_contract_matches_current_soybean_rendered_html(monkeypatch):
    import re,html
    from datetime import date
    monkeypatch.syspath_prepend(str(ROOT/'05_apps'))
    import import_profit_intraday_page as page
    from agri_research_agent.market_data.intraday import MarketSession
    fragments=[]
    monkeypatch.setattr(page.st,'html',fragments.append)
    params=SimpleNamespace(tariff_rate=0.03,vat_rate=0.09)
    config=SimpleNamespace(resolve_parameters=lambda origin:params)
    for session,heading in [(MarketSession.AM,'大豆早间榨利'),(MarketSession.PM,'大豆下午榨利')]:
        rows=page._result_profit_rows(None,business_date=date(2026,9,13),session=session,origin='brazil',config=config)
        page._render_preview_session_table(heading,rows,short_meta=session.value+' · —')
    tables=[]
    for fragment in fragments:
        meta=re.search(r'class="[^"]*profit-card-time[^"]*">([^<]*)',fragment)[1]
        body=re.search(r'<tbody>(.*?)</tbody>',fragment)[1]
        rows=[[html.unescape(re.sub('<[^>]+>','',c)) for c in re.findall(r'<td[^>]*>(.*?)</td>',r)]
              for r in re.findall(r'<tr>(.*?)</tr>',body)]
        tables.append(dict(heading=meta,rows=rows))
    assert routine.check_dom_tables(tables,('AM','PM'))=='PASS'


@pytest.mark.parametrize('risk_class',['ROUTINE_STATELESS','STATEFUL_OR_INFRA'])
def test_host_consumes_plain_acceptance_only_for_machine_routine(tmp_path,monkeypatch,risk_class):
    import json,hashlib
    host=routine.load('09_deploy/runtime_identity/host_authorization.py','_routine_host_test')
    record=acceptance();path=tmp_path/'accepted.json';path.write_text(json.dumps(record))
    policy=dict(approved_source_root=str(tmp_path),approved_commit=IMAGE['commit'],approved_tree=IMAGE['tree'],
        image_id=IMAGE['image_id'],project_id='service',schema_version='host-runtime-policy/5',
        runtime_manifest_sha256='f'*64,runtime_manifest_path='/app/manifest.json',source_root='/app',
        candidate_record=dict(path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    manifest={'schema_version':'runtime-manifest/3'}
    engine=SimpleNamespace(_project=lambda *a:dict(runtime_contract='manifest.json'),
        source_contract=lambda *a:({},manifest,{'source_sha256':{'manifest.json':'f'*64}}),
        validate_source_compose=lambda *a:None)
    entry=SimpleNamespace(require_source=lambda *a,**k:(IMAGE['commit'],IMAGE['tree']))
    monkeypatch.setattr(host,'_protected_path',lambda p,**k:p)
    monkeypatch.setattr(host,'_contract_module',lambda p,n:routine if p.name=='routine_release.py' else entry if p.name=='pre_release_runtime.py' else engine)
    monkeypatch.setattr(routine,'load',lambda *a:SimpleNamespace(classify_release=lambda *a:{'RELEASE_RISK_CLASS':risk_class}))
    if risk_class=='ROUTINE_STATELESS':assert host._validated_candidate_record(policy)==(record,manifest)
    else:
        with pytest.raises(routine.RoutineError,match='HIGH_RISK'):host._validated_candidate_record(policy)


def test_rollback_record_cannot_authorize_high_risk_forward_release(monkeypatch):
    record=dict(schema_version='routine-rollback-assets/1',**PREVIOUS,health='PASS',project_id='service',forward_target=IMAGE['commit'])
    policy=dict(approved_commit=PREVIOUS['commit'],approved_tree=PREVIOUS['tree'],image_id=PREVIOUS['image_id'],project_id='service')
    monkeypatch.setattr(routine,'load',lambda *a:SimpleNamespace(classify_release=lambda *a:{'RELEASE_RISK_CLASS':'STATEFUL_OR_INFRA'}))
    with pytest.raises(routine.RoutineError,match='HIGH_RISK'):routine.verify_routine_record(record,policy,ROOT)
