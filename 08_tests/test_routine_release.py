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
    host=SimpleNamespace(_protected_path=lambda p,**k:p,_json=lambda raw:raw,
                         _validate_runtime_mounts=lambda *a: (_ for _ in ()).throw(
                             routine.RoutineError('UNDECLARED_RUNTIME_MOUNT')))
    request={'production':dict(compose='compose',environment='env',policy_template='policy',project_directory=str(tmp_path),writable_root=str(tmp_path))}
    b=routine.DockerSession(request,None,host,{},dict(project_id='service',service_id='service',entrypoint=['app'],runtime_roots=[]))
    b.protected_json=lambda _:dict(mounts=[])
    calls=[]
    def compose(spec,*args):calls.append(args);return SimpleNamespace(stdout={'services':{'service':service}})
    b.compose=compose
    with pytest.raises(routine.RoutineError):b.create(IMAGE,'production')
    assert calls==[('config','--format','json')]


@pytest.mark.parametrize('fault', [None, 'wrong-image', 'started', 'extra-service'])
def test_compose_227_create_only_lifecycle(tmp_path, monkeypatch, fault):
    """Exercise the real adapter with a stateful, inert Compose 2.27 transport.

    No release candidate or dependency is created on the production server.
    The CLI option set is checked against that server's actual up --help.
    """
    import json
    events = []
    containers = {'production': {'Image': PREVIOUS['image_id'], 'State': {'Status': 'running'}}}
    production_before = copy.deepcopy(containers['production'])
    service = dict(image=IMAGE['image_id'], entrypoint=['app'], read_only=True, volumes=[],
                   restart='no', ports=[dict(host_ip='127.0.0.1')])
    document = dict(name='routine-candidate-test', services={'service': service},
                    networks={'default': {'driver': 'bridge', 'internal': False}})
    if fault == 'extra-service':
        document['services']['dependency'] = {'image': 'dependency-image'}
    # The transport knows a dependency; --no-deps must leave it absent.
    def docker(*args):
        if args[0] == 'compose':
            command = args[args.index('-f') + 2:]
            events.append(command)
            if command[0] == 'config':
                return SimpleNamespace(stdout=json.dumps(document).encode())
            if command[0] == 'ps':
                return SimpleNamespace(stdout=b'candidate' if 'candidate' in containers else b'')
            assert command[0] == 'up', 'Compose create does not support --no-deps'
            supported = {'--no-deps', '--no-start', '--no-build', '--pull', '--force-recreate'}
            assert all(x in supported for x in command if x.startswith('--'))
            assert command[-1] == 'service' and command[command.index('--pull') + 1] == 'never'
            assert '--no-build' in command  # No build transport is allowed.
            if '--no-deps' not in command:
                containers['dependency'] = {'State': {'Status': 'running'}}
            state = 'created' if '--no-start' in command else 'running'
            containers['candidate'] = {'Image': 'wrong' if fault == 'wrong-image' else IMAGE['image_id'],
                'State': {'Status': 'running' if fault == 'started' else state}}
            return SimpleNamespace(stdout=b'')
        assert args == ('start', 'candidate')
        assert events[-1] == 'fresh-grant'
        containers['candidate']['State']['Status'] = 'running'
        events.append('start')
    engine = SimpleNamespace(_docker=docker, inspect_one=lambda kind, identity: containers[identity])
    host = SimpleNamespace(_protected_path=lambda p, **k: p, _json=json.loads)
    spec = dict(project_directory=str(tmp_path), environment='env', compose='compose', policy_template='policy')
    b = routine.DockerSession({'candidate': spec}, engine, host, {},
        dict(project_id='service', service_id='service', entrypoint=['app']))
    # Mount and image-metadata validation have independent regression coverage;
    # retain the adapter's real observed container-image and created-state checks.
    monkeypatch.setattr(routine, 'image_identity', lambda *a: copy.deepcopy(IMAGE))
    monkeypatch.setattr(b, '_check_mounts', lambda mounts: None)
    monkeypatch.setattr(b, 'assert_data_readonly', lambda: None)
    if fault:
        with pytest.raises(routine.RoutineError, match={
            'wrong-image': 'CONTAINER_IMAGE_CHANGED', 'started': 'FRESH_INSTANCE_REQUIRED',
            'extra-service': 'ONLY_TARGET_SERVICE_ALLOWED'}[fault]):
            b.create(IMAGE, 'candidate_validation')
    else:
        b.create(IMAGE, 'candidate_validation')
        assert containers['candidate']['State']['Status'] == 'created'
        assert containers['candidate']['Image'] == IMAGE['image_id']
        assert 'start' not in events and 'fresh-grant' not in events
        policy = dict(approved_commit=IMAGE['commit'], approved_tree=IMAGE['tree'],
                      image_id=IMAGE['image_id'], role='candidate_validation')
        template = tmp_path/'policy.json'; template.write_text(json.dumps(policy))
        spec.update(policy_template=str(template), policy_output=str(tmp_path/'issued.json'),
                    grant_directory=str(tmp_path), key_path='inert-key')
        engine.inspect_one = lambda kind, identity: {} if kind == 'image' else containers[identity]
        engine._canonical = lambda obj: json.dumps(obj).encode()
        engine._write_new = lambda path, raw: path.write_bytes(raw)
        host.copy_container_json = lambda cid: {'release_id': IMAGE['release_id']}
        host.normalize_observation = lambda *a: {'actual_config_sha256': '0'*64}
        def issue(cid, **kwargs):
            from datetime import datetime, timedelta, timezone
            assert cid == 'candidate' and containers[cid]['State']['Status'] == 'created'
            assert Path(spec['policy_output']).is_file()
            events.append('fresh-grant')
            kwargs['grant_path'].write_text('{}')
            return {'payload': dict(container_id=cid, role='candidate_validation', image_id=IMAGE['image_id'],
                issued_at=datetime.now(timezone.utc).isoformat(),
                expires_at=(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat())}
        host.issue_execution_grant = issue
        b.grant_and_start(IMAGE, 'candidate_validation')
        assert events[-2:] == ['fresh-grant', 'start']
    assert 'dependency' not in containers
    assert containers['production'] == production_before
    assert not any(isinstance(c, tuple) and c[0] == 'build' for c in events)


@pytest.mark.parametrize('fault', [None, 'internal', 'external', 'production-name', 'host-mode', 'extra-network', 'driver'])
def test_routine_candidate_network_generation_and_isolation(tmp_path, fault):
    engine = routine.load('04_scripts/runtime/validate_target_runtime.py', '_network_engine')
    import json
    contract = json.loads((ROOT/'02_configs/runtime_contracts/spread-production-runtime.json').read_text(encoding='utf-8'))
    contract['_container_user'] = '10001:10001'
    mounts = [dict(source=str(tmp_path/'isolated'), target='/runtime/capture-snapshots', read_only=False),
              dict(source='/production/input', target='/runtime/import-profit/snapshots', read_only=True)]
    original = copy.deepcopy(mounts)
    doc = routine.candidate_compose_document(engine, contract, IMAGE['image_id'], mounts,
        tmp_path/'grants', 'a'*32, 'routine-candidate-network-test', 18502, 8501)
    service = doc['services']['spread-dashboard']; network = doc['networks']['default']
    assert set(doc['services']) == {'spread-dashboard'}
    assert service['image'] == IMAGE['image_id'] and 'build' not in service
    assert service['ports'] == [dict(target=8501, published='18502', host_ip='127.0.0.1', protocol='tcp')]
    assert service['entrypoint'] == contract['entrypoint'] and service['restart'] == 'no'
    assert mounts == original
    assert [(v['source'],v['target'],v['read_only']) for v in service['volumes'][:2]] == [(v['source'],v['target'],v['read_only']) for v in mounts]
    if fault == 'internal': network['internal'] = True
    elif fault == 'external': network['external'] = True
    elif fault == 'production-name': network['name'] = 'production_default'
    elif fault == 'host-mode': service['network_mode'] = 'host'
    elif fault == 'extra-network': service['networks'] = {'default': None, 'production': None}
    elif fault == 'driver': network['driver'] = 'macvlan'
    if fault:
        with pytest.raises(routine.RoutineError, match='INDEPENDENT_NON_INTERNAL_BRIDGE'):
            routine.check_candidate_network(doc, service)
    else:
        routine.check_candidate_network(doc, service)
    # The high-risk validator retains its original network-disabled primitive.
    assert engine._compose_document(contract, IMAGE['image_id'], mounts, tmp_path/'grants', 'a'*32)['services']['spread-dashboard']['network_mode'] == 'none'


@pytest.mark.parametrize('published', [False, True])
def test_candidate_url_requires_observed_port_publication(published):
    ports = {'8501/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '18502'}]} if published else {}
    engine = SimpleNamespace(inspect_one=lambda kind, cid: {'NetworkSettings': {'Ports': ports}})
    backend = routine.DockerSession({}, engine, None, {}, {'project_id': 'test', 'service_id': 'service'})
    backend.container_id = 'exact-candidate'; backend.spec = {'container_port': 8501}
    backend.role = 'candidate_validation'
    backend.compose = lambda *args: SimpleNamespace(stdout=b'{"services":{"service":{"ports":[{"target":8501,"published":"18502","host_ip":"127.0.0.1","protocol":"tcp"}]}}}')
    if published:
        assert backend.url() == 'http://127.0.0.1:18502'
    else:
        with pytest.raises(routine.RoutineError, match='HTTP_PORT_NOT_BOUND'):
            backend.url()


def test_real_production_dual_stack_port_fixture_replay():
    # Read-only fixture from the healthy previous Production instance.
    raw = {'8501/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '8501'},
                        {'HostIp': '::', 'HostPort': '8501'}]}
    configured = [dict(target=8501, published='8501', protocol='tcp')]
    assert len(raw['8501/tcp']) == 2
    assert routine.semantic_http_port(raw, configured, 8501, 'production') == '8501'
    assert len({(8501, item['HostPort']) for item in raw['8501/tcp']}) == 1
    engine = SimpleNamespace(inspect_one=lambda kind, cid: {'NetworkSettings': {'Ports': raw}})
    backend = routine.DockerSession({}, engine, None, {}, {'project_id': 'test', 'service_id': 'service'})
    backend.container_id = 'previous-production'
    backend.spec = {'container_port': 8501}
    backend.role = 'production'
    backend.compose = lambda *args: SimpleNamespace(stdout=b'{"services":{"service":{"ports":[{"target":8501,"published":"8501","protocol":"tcp"}]}}}')
    assert backend.url() == 'http://127.0.0.1:8501'


@pytest.mark.parametrize(('role', 'host_ip', 'bindings', 'valid'), [
    ('candidate_validation', '127.0.0.1', [('127.0.0.1', '18502')], True),
    ('candidate_validation', '127.0.0.1', [('127.0.0.1', '18502'), ('::1', '18502')], True),
    ('candidate_validation', '127.0.0.1', [('0.0.0.0', '18502')], False),
    ('candidate_validation', '127.0.0.1', [('127.0.0.1', '18502'), ('::', '18502')], False),
    ('production', None, [('0.0.0.0', '18502')], True),
    ('production', None, [('0.0.0.0', '18502'), ('::', '18502')], True),
    ('production', None, [('127.0.0.1', '18502')], False),
    ('production', None, [('0.0.0.0', '18502'), ('::', '18503')], False),
    ('production', None, [('0.0.0.0', '18502'), ('0.0.0.0', '18502')], False),
    ('production', None, [('0.0.0.0', '18502'), ('::', '18502'), ('::1', '18502')], False),
    ('production', None, [], False),
    ('production', None, [('0.0.0.0', '')], False),
    ('production', None, [('unapproved', '18502')], False),
])
def test_semantic_port_exposure_policy(role, host_ip, bindings, valid):
    raw = {'8501/tcp': [dict(HostIp=ip, HostPort=port) for ip, port in bindings]}
    configured = [dict(target=8501, published='18502', protocol='tcp', host_ip=host_ip)]
    if valid:
        assert routine.semantic_http_port(raw, configured, 8501, role) == '18502'
    else:
        with pytest.raises(routine.RoutineError, match='HTTP_PORT_NOT_BOUND'):
            routine.semantic_http_port(raw, configured, 8501, role)


@pytest.mark.parametrize('mutation', ['different-container-port', 'extra-published-port',
                                      'extra-compose-port', 'wrong-policy-host', 'malformed'])
def test_semantic_port_rejects_extra_or_malformed_exposure(mutation):
    raw = {'8501/tcp': [dict(HostIp='0.0.0.0', HostPort='8501')]}
    configured = [dict(target=8501, published='8501', protocol='tcp')]
    if mutation == 'different-container-port':
        configured[0]['target'] = 8502
    elif mutation == 'extra-published-port':
        raw['8502/tcp'] = [dict(HostIp='0.0.0.0', HostPort='8502')]
    elif mutation == 'extra-compose-port':
        configured.append(dict(target=8502, published='8502', protocol='tcp'))
    elif mutation == 'wrong-policy-host':
        configured[0]['host_ip'] = '192.0.2.10'
    else:
        raw['8501/tcp'] = [{'HostIp': '0.0.0.0'}]
    with pytest.raises(routine.RoutineError, match='HTTP_PORT_NOT_BOUND'):
        routine.semantic_http_port(raw, configured, 8501, 'production')


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


def production_mount_fixture(tmp_path):
    import json
    host=routine.load('09_deploy/runtime_identity/host_authorization.py','_routine_mount_host_test')
    contract=json.loads((ROOT/'02_configs/runtime_contracts/spread-production-runtime.json').read_text(encoding='utf-8'))
    writable_root=tmp_path/'instance';writable_root.mkdir()
    readonly_root=tmp_path/'sealed';readonly_root.mkdir()
    mounts=[]
    for item in contract['required_mounts']:
        role=item['role']
        # Reproduce the failed release: the RO consumer and RW capture alias
        # point at one approved production snapshot directory.
        source=(writable_root/'snapshots' if role in ('snapshots','capture-snapshots')
                else (readonly_root if item['read_only'] else writable_root)/role)
        source.mkdir(exist_ok=True)
        mounts.append(dict(source=str(source),target=item['container_path'],read_only=item['read_only']))
    policy=dict(schema_version='host-runtime-policy/5',grant_container_directory='/run/market-data-grants',mounts=copy.deepcopy(mounts))
    session=routine.DockerSession({},None,host,{},contract)
    session.role='production'
    session.spec={'writable_root':str(writable_root),'policy_template':'protected-policy'}
    session.protected_json=lambda _:policy
    return session,mounts,policy


def test_declared_capture_write_uses_same_manifest_contract_for_deploy_and_rollback(tmp_path):
    session,mounts,policy=production_mount_fixture(tmp_path)
    assert next(m for m in mounts if m['target']=='/runtime/capture-snapshots')['read_only'] is False
    session._check_mounts([dict(type='bind',**m) for m in mounts])
    # Rollback creates a new DockerSession, but calls this same mount validator.
    rollback=routine.DockerSession({},None,session.host,{},session.contract)
    rollback.role='production';rollback.spec=session.spec;rollback.protected_json=lambda _:policy
    rollback._check_mounts([dict(type='bind',**m) for m in mounts])


def test_even_declared_writable_business_root_is_not_routine_write_permission(tmp_path):
    session,mounts,_=production_mount_fixture(tmp_path)
    # Changing a business root to RW in a proposed manifest cannot override
    # the sealed policy for the running release.
    next(r for r in session.contract['runtime_roots'] if r['role']=='data')['access']='rw'
    next(r for r in session.contract['required_mounts'] if r['role']=='data')['read_only']=False
    next(m for m in mounts if m['target']=='/runtime/01_data')['read_only']=False
    with pytest.raises(routine.RoutineError,match='PRODUCTION_MOUNT_POLICY_MISMATCH'):
        session._check_mounts([dict(type='bind',**m) for m in mounts])


@pytest.mark.parametrize('fault',['unknown-rw','history-rw','data-rw','wrong-source','wrong-mode','outside-instance'])
def test_production_mount_contract_remains_strict(tmp_path,fault):
    session,mounts,_=production_mount_fixture(tmp_path)
    if fault=='unknown-rw':
        source=tmp_path/'instance'/'unknown';source.mkdir()
        mounts.append(dict(source=str(source),target='/runtime/unknown',read_only=False))
    else:
        target={'history-rw':'/runtime/import-profit/history','data-rw':'/runtime/01_data'}.get(
            fault,'/runtime/capture-snapshots')
        mount=next(m for m in mounts if m['target']==target)
        if fault in ('history-rw','data-rw','wrong-mode'):
            mount['read_only']=not mount['read_only']
        else:
            source=(tmp_path/'instance'/'other') if fault=='wrong-source' else (tmp_path/'outside')
            source.mkdir()
            mount['source']=str(source)
    with pytest.raises((routine.RoutineError,session.host.HostAuthorizationError)):
        session._check_mounts([dict(type='bind',**m) for m in mounts])


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

class ManualBackend(Backend):
    def http(self): return self.hit('http')
    def checkpoint(self): return dict(container_id='exact-instance', url='http://127.0.0.1:1234')
    def resume(self, instance, stage):
        self.hit('resume')
        assert instance['container_id'] == 'exact-instance'


def waiting():
    return routine.candidate_acceptance(ManualBackend(), IMAGE, base_commit='d'*40, ci_run='123', mode='MANUAL')


def manual_candidate():
    return routine.finish_manual(ManualBackend(), IMAGE, waiting(), stage='candidate', decision='PASS', operator='Maintainer', ci_run='123')


def test_manual_browser_dependency_is_not_imported(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', None)
    routine.check_browser('MANUAL')
    with pytest.raises(ModuleNotFoundError): routine.check_browser('AUTOMATED')


def test_automated_browser_binary_missing_fails(monkeypatch, tmp_path):
    import sys
    class P:
        def __enter__(self): return SimpleNamespace(chromium=SimpleNamespace(executable_path=str(tmp_path/'missing')))
        def __exit__(self, *args): pass
    monkeypatch.setitem(sys.modules,'playwright.sync_api',SimpleNamespace(sync_playwright=P))
    with pytest.raises(routine.RoutineError, match='CHROMIUM_NOT_AVAILABLE'): routine.check_browser('AUTOMATED')


def test_candidate_waiting_retains_instance_and_does_not_authorize_deploy():
    b=ManualBackend(); r=routine.candidate_acceptance(b,IMAGE,base_commit='d'*40,ci_run='123',mode='MANUAL')
    assert r['result']==routine.WAITING and r['instance']['url']
    assert 'cleanup' not in b.calls and 'smoke' not in b.calls
    p=ManualBackend()
    with pytest.raises(routine.RoutineError): routine.deploy_same_image(p,IMAGE,r,PREVIOUS,ci_run='123')
    assert p.calls==[]


@pytest.mark.parametrize('failure',['preflight','health','http','image','data-readonly'])
def test_manual_candidate_machine_failure_cannot_be_approved(failure):
    r=routine.candidate_acceptance(ManualBackend(failure),IMAGE,base_commit='d'*40,ci_run='123',mode='MANUAL')
    assert r['result']=='FAIL' and r['candidate_cleanup']=='PASS'
    with pytest.raises(routine.RoutineError): routine.finish_manual(ManualBackend(),IMAGE,r,stage='candidate',decision='PASS',operator='admin',ci_run='123')


@pytest.mark.parametrize('decision',['FAIL','CANCEL'])
def test_candidate_manual_rejection_cleans_without_switch(decision):
    b=ManualBackend()
    r=routine.finish_manual(b,IMAGE,waiting(),stage='candidate',decision=decision,operator='admin',ci_run='123')
    assert r['result']=='FAIL' and r['candidate_cleanup']=='PASS'
    with pytest.raises(routine.RoutineError): routine.deploy_same_image(ManualBackend(),IMAGE,r,PREVIOUS,ci_run='123')


def test_manual_two_checkpoints_same_image_and_final_acceptance():
    accepted=manual_candidate(); b=ManualBackend()
    r=routine.deploy_same_image(b,IMAGE,accepted,PREVIOUS,ci_run='123')
    assert r['result']==routine.WAITING and b.image==IMAGE
    assert 'fresh-grant:production' in b.calls and 'smoke' not in b.calls
    final=routine.finish_manual(b,IMAGE,r,stage='production',decision='PASS',operator='admin',ci_run='123')
    assert final['result']=='PASS' and final['manual_ui']['recorded_at']
    assert final['rollback']=='NOT_NEEDED'


@pytest.mark.parametrize('decision',['FAIL','CANCEL'])
def test_production_manual_rejection_rolls_back_with_fresh_grant(decision):
    b=ManualBackend(); r=routine.deploy_same_image(b,IMAGE,manual_candidate(),PREVIOUS,ci_run='123')
    final=routine.finish_manual(b,IMAGE,r,stage='production',decision=decision,operator='admin',ci_run='123')
    assert final['result']=='FAIL' and final['rollback']=='PASS'
    assert 'fresh-grant:rollback' in b.calls


@pytest.mark.parametrize('failure',['preflight','health','http','image','data-readonly'])
def test_production_machine_failure_cannot_use_manual_override(failure):
    b=ManualBackend(failure); r=routine.deploy_same_image(b,IMAGE,manual_candidate(),PREVIOUS,ci_run='123')
    assert r['result']=='FAIL'
    with pytest.raises(routine.RoutineError): routine.finish_manual(b,IMAGE,r,stage='production',decision='PASS',operator='admin',ci_run='123')


@pytest.mark.parametrize('stage',['candidate','production'])
@pytest.mark.parametrize('failure',['health','http','data-readonly','image'])
def test_new_machine_failure_during_manual_wait_cannot_be_overridden(stage,failure):
    r=waiting() if stage=='candidate' else routine.deploy_same_image(ManualBackend(),IMAGE,manual_candidate(),PREVIOUS,ci_run='123')
    b=ManualBackend(failure)
    final=routine.finish_manual(b,IMAGE,r,stage=stage,decision='PASS',operator='admin',ci_run='123')
    assert final['result']=='FAIL'
    assert final['candidate_cleanup']=='PASS' if stage=='candidate' else final['rollback']=='PASS'


@pytest.mark.parametrize('field',['commit','tree','image_id','release_id','ci_run'])
def test_manual_checkpoint_identity_changes_rejected_before_attach(field):
    r=waiting();r[field]='changed';b=ManualBackend()
    with pytest.raises(routine.RoutineError):routine.finish_manual(b,IMAGE,r,stage='candidate',decision='PASS',operator='admin',ci_run='123')
    assert b.calls==[]


def test_manual_high_risk_still_rejected_by_grant_issuer(monkeypatch):
    monkeypatch.setattr(routine,'load',lambda *a:SimpleNamespace(classify_release=lambda *a:{'RELEASE_RISK_CLASS':'STATEFUL_OR_INFRA'}))
    with pytest.raises(routine.RoutineError,match='HIGH_RISK'):
        routine.verify_routine_record(manual_candidate(),dict(approved_commit=IMAGE['commit'],approved_tree=IMAGE['tree'],image_id=IMAGE['image_id'],project_id='service'),ROOT)


def test_manual_contract_documented():
    for name in ['AGENTS.md','07_docs/03_标准开发与生产发布规范.md','07_docs/04_开发与发布检查清单.md']:
        doc=(ROOT/name).read_text(encoding='utf-8')
        assert 'MANUAL' in doc and 'AUTOMATED' in doc and 'WAITING_FOR_MAINTAINER_UI_ACCEPTANCE' in doc

@pytest.mark.parametrize('mutation',['none','restart','started','namespace','url','spec'])
def test_resume_requires_same_continuously_running_container(mutation):
    spec={'container_port':8501}
    state={'Running':True,'StartedAt':'start'}
    container={'Id':'id','State':state,'RestartCount':0,'NetworkSettings':{'Ports':{'8501/tcp':[{'HostIp':'127.0.0.1','HostPort':'1234'}]}}}
    b=routine.DockerSession({'candidate':spec,'application_smoke':{'path':'?page=profit'}},SimpleNamespace(inspect_one=lambda *a:container),None,{},dict(project_id='p',service_id='s'))
    b.spec=spec;b.container_id='id';b.role='candidate_validation'
    def compose(_spec, *args):
        if args[0] == 'config':
            return SimpleNamespace(stdout=b'{"services":{"s":{"ports":[{"target":8501,"published":"1234","host_ip":"127.0.0.1","protocol":"tcp"}]}}}')
        return SimpleNamespace(stdout=b'id\n' if mutation!='namespace' else b'other\n')
    b.compose=compose
    checkpoint=b.checkpoint();b.container_id=None
    if mutation=='restart':container['RestartCount']=1
    if mutation=='started':state['StartedAt']='later'
    if mutation=='url':checkpoint['url']='http://127.0.0.1:9999'
    if mutation=='spec':checkpoint['spec']={'container_port':9999}
    if mutation=='none':b.resume(checkpoint,'candidate');assert b.container_id=='id'
    else:
        with pytest.raises(routine.RoutineError):b.resume(checkpoint,'candidate')


@pytest.mark.parametrize('phase',['validate','candidate-ui','deploy','production-ui'])
def test_manual_cli_never_imports_browser_and_preserves_checkpoints(tmp_path,monkeypatch,phase):
    import json,sys
    monkeypatch.setitem(sys.modules,'playwright.sync_api',None)
    binding=dict(commit=IMAGE['commit'],tree=IMAGE['tree'])
    request=dict(source_root=str(tmp_path),project_id='service',target_commit=IMAGE['commit'],target_tree=IMAGE['tree'],
        base_commit=PREVIOUS['commit'],ci_record=str(tmp_path/'ci.json'),production_data_mutation=False,rebuild=False,
        ui_acceptance_mode='MANUAL',candidate_authorized=True,production_authorized=True,image_id=IMAGE['image_id'],
        previous_release=PREVIOUS,acceptance_record='accepted',checkpoint_record='checkpoint',manual_ui={'decision':'PASS','operator':'admin'})
    req=tmp_path/'request.json';req.write_text(json.dumps(request))
    (tmp_path/'ci.json').write_text(json.dumps(dict(candidate=binding,final_result='PASS',checks=dict(TECHNICAL_VALIDATION='PASS',workflow_run_id='123'))))
    host=SimpleNamespace(_require_linux_root=lambda:None,require_protected_authority_source=lambda:None,_protected_path=lambda p,**k:p,_json=json.loads)
    engine=SimpleNamespace(_project=lambda *a:dict(project_id='service',runtime_contract='manifest'),source_contract=lambda *a:({},dict(project_id='service',service_id='service'),binding),_canonical=lambda r:json.dumps(r).encode(),_write_new=lambda p,raw:p.write_bytes(raw))
    source=SimpleNamespace(require_source=lambda *a,**k:None)
    b=ManualBackend();accepted=manual_candidate()
    checkpoint=waiting() if phase=='candidate-ui' else routine.deploy_same_image(ManualBackend(),IMAGE,accepted,PREVIOUS,ci_run='123')
    b.protected_json=lambda path:accepted if path=='accepted' else checkpoint
    monkeypatch.setattr(routine,'load',lambda path,name:host if 'host_authorization' in path else engine if 'validate_target' in path else source)
    monkeypatch.setattr(routine,'require_routine',lambda *a:{})
    monkeypatch.setattr(routine,'image_identity',lambda *a:IMAGE)
    monkeypatch.setattr(routine,'DockerSession',lambda *a:b)
    out=tmp_path/'result.json'
    exitcode=routine.main([phase,'--request',str(req),'--output',str(out)])
    result=json.loads(out.read_text())
    assert exitcode==(2 if phase in ('validate','deploy') else 0)
    assert result['result']==(routine.WAITING if exitcode==2 else 'PASS')
    assert 'smoke' not in b.calls


def test_unproven_waiting_instance_saves_failure_without_touching_replacement():
    b=ManualBackend('resume')
    result=routine.finish_manual(b,IMAGE,waiting(),stage='candidate',decision='PASS',operator='admin',ci_run='123')
    assert result['result']=='FAIL' and result['recovery']=='STOP_INSTANCE_IDENTITY_UNPROVEN'
    assert b.calls==['resume']


def test_manual_rejection_collects_bounded_redacted_logs_before_cleanup():
    b=ManualBackend()
    helper=routine.load('09_deploy/spread_release/wait_for_service_ready.py','_manual_log_test')
    def collect():
        b.hit('diagnostics')
        return helper.build_log_summary('password=secret-value\n'+'warning safe\n'*220,collected_at_utc=routine.now())
    b.diagnostics=collect
    r=routine.finish_manual(b,IMAGE,waiting(),stage='candidate',decision='FAIL',operator='admin',ci_run='123')
    assert r['diagnostics']['stored_bytes']<=65536 and r['diagnostics']['tail_line_count']<=200
    assert 'secret-value' not in r['diagnostics']['tail_text']
    assert b.calls.index('diagnostics')<b.calls.index('cleanup')
