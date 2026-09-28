from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]


def load(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = load('08_tests/test_candidate_validation_record.py', 'repair_record_fixtures')
record = fixtures.record
host = load('09_deploy/runtime_identity/host_authorization.py', 'repair_host')
engine = load('04_scripts/runtime/validate_target_runtime.py', 'repair_engine')


def packaging(e):
    b = e['binding']
    grant = dict(commit=b['commit'], tree=b['tree'], image_id=e['image_id'], container_id='a' * 64, runtime_id='target-validation')
    readonly = dict(schema_version='spread-runtime-preflight/1', status='PASS', mode='CANDIDATE_VALIDATION',
        runtime_id='target-validation', git_commit=b['commit'], git_tree=b['tree'], image_id=e['image_id'],
        identity_role='candidate_validation', domestic_spread_rows=2, soybean_release='fixture-release',
        snapshot_status='AVAILABLE', snapshot_release='fixture-pm', snapshot_business_date='2026-08-31',
        capture_executed=False, secret_accessed=False, network_accessed=False)
    return dict(workflow_run_id=None, candidate_commit=b['commit'], candidate_tree=b['tree'],
        image_id=e['image_id'], release_commit=b['commit'], release_tree=b['tree'], oci_revision=b['commit'],
        identity_kind='FIXED_CANDIDATE_VALIDATION_ROOT', role='candidate_validation', production_key_used=False,
        private_key_persisted=None, private_key_visible_to_container=False, public_key_fingerprint=None,
        grant_binding=grant, import_closure=dict(required_module_count=143, manifest_module_count=278,
        dockerfile_module_count=278, missing_from_manifest=[], missing_from_dockerfile=[], missing_from_final_image=[]),
        lifecycle_imports={name:'PASS' for name in ('lifecycle','lifecycle_events','lifecycle_reconciler','lifecycle_store')},
        readonly_initialization=readonly, readonly_exit_code=0, initialize_strict_page='PASS', app_test='PASS',
        probe_stages=copy.deepcopy(e['probes']), timestamp=fixtures.NOW.isoformat(),
        service_credential_mounts={name:'PASS' for name in record._SECRET_PROBES})


def signed_packaging():
    key = Ed25519PrivateKey.generate()
    item = fixtures.envelope(key, validator_version='target-runtime-validator/2')
    e = item['payload']['evidence']
    e['spread_runtime_packaging'] = packaging(e)
    item['payload']['evidence_sha256'] = hashlib.sha256(record.canonical(e)).hexdigest()
    fixtures.resign(item,key)
    return key,item


def test_packaging_is_a_signed_strict_claim_and_legacy_records_remain_valid():
    key,item = signed_packaging()
    assert record.verify_record(fixtures.raw(item),fixtures.trust(key),now=fixtures.NOW) == item['payload']
    for version in ('target-runtime-validator/1','target-runtime-validator/2'):
        old = fixtures.envelope(key,validator_version=version)
        assert record.verify_record(fixtures.raw(old),fixtures.trust(key),now=fixtures.NOW) == old['payload']
    item['payload']['evidence']['spread_runtime_packaging']['timestamp']='2026-09-05T08:01:00+00:00'
    item['payload']['evidence_sha256']=hashlib.sha256(record.canonical(item['payload']['evidence'])).hexdigest()
    with pytest.raises(record.CandidateValidationRecordError,match='signature'):
        record.verify_record(fixtures.raw(item),fixtures.trust(key),now=fixtures.NOW)


@pytest.mark.parametrize('location', ['evidence','packaging','grant_binding','import_closure','lifecycle_imports','readonly_initialization','service_credential_mounts'])
def test_unknown_record_fields_are_rejected_even_after_resigning(location):
    key,item = signed_packaging()
    e=item['payload']['evidence']; p=e['spread_runtime_packaging']
    target=e if location=='evidence' else p if location=='packaging' else p[location]
    target['unknown']=True
    item['payload']['evidence_sha256']=hashlib.sha256(record.canonical(e)).hexdigest()
    fixtures.resign(item,key)
    with pytest.raises(record.CandidateValidationRecordError):
        record.verify_record(fixtures.raw(item),fixtures.trust(key),now=fixtures.NOW)


@pytest.mark.parametrize('field,value', [('candidate_commit','0'*40),('candidate_tree','0'*40),
    ('image_id','sha256:'+'0'*64),('production_key_used',True),('private_key_visible_to_container',True),
    ('readonly_exit_code',False),('initialize_strict_page','FAIL'),('app_test','NOT_EXECUTED'),
    ('timestamp','yesterday'),('workflow_run_id',123),('identity_kind','production'),
    ('readonly_initialization',None),('lifecycle_imports',{}),('service_credential_mounts',{})])
def test_packaging_requires_typed_success_and_bound_identity(field,value):
    key,item=signed_packaging();e=item['payload']['evidence']
    e['spread_runtime_packaging'][field]=value
    item['payload']['evidence_sha256']=hashlib.sha256(record.canonical(e)).hexdigest()
    fixtures.resign(item,key)
    with pytest.raises(record.CandidateValidationRecordError):
        record.verify_record(fixtures.raw(item),fixtures.trust(key),now=fixtures.NOW)


def secret_fixture(tmp_path,monkeypatch):
    manifest=dict(service_id='app',secret_references=['service-credential'],build={'compose_sources':['compose.yml']},
        runtime_roots=[dict(role='identity',container_path='/runtime',access='ro')],
        required_mounts=[dict(role='identity',container_path='/runtime',read_only=True)])
    path=tmp_path/'compose.json';path.write_text('{}')
    secret=tmp_path/'secret.json';secret.write_text('')
    declared={'services':{'app':{'secrets':[dict(source='service-credential',target='/run/secrets/market-data-service.json')]}},
        'secrets':{'service-credential':{'file':'${SOURCE_FILE}'}}}
    rendered=copy.deepcopy(declared);rendered['services']['app']['user']='65532:65532'
    rendered['secrets']['service-credential']['file']=str(secret)
    policy=dict(schema_version='host-runtime-policy/4',role='candidate_validation',service_id='app',
        candidate_host_root=str(tmp_path),compose_project_directory=str(tmp_path),compose_environment_file=str(path),
        compose_sources=[dict(path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest())],
        rendered_compose_sha256=host._digest(rendered),grant_container_directory='/run/market-data-grants')
    monkeypatch.setattr(host,'_protected_path',lambda p,**kw:p)
    # Linux host containment is transported through real Windows test paths.
    monkeypatch.setattr(host,'_within',lambda value,root:Path(value).is_relative_to(Path(root)))
    monkeypatch.setattr(host,'_secret_file_identity',lambda *a,**kw:{})
    monkeypatch.setattr(host,'_run_docker',lambda args:host._canonical(declared if '--no-interpolate' in args else rendered))
    mounts=[dict(source=str(tmp_path),target='/runtime',read_only=True),
            dict(source=str(secret),target='/run/secrets/market-data-service.json',read_only=True)]
    return manifest,policy,declared,rendered,mounts


def test_declared_readonly_service_secret_resolves_from_manifest_and_compose(tmp_path,monkeypatch):
    m,p,_,_,mounts=secret_fixture(tmp_path,monkeypatch)
    host._validate_runtime_mounts(m,mounts,p)


@pytest.mark.parametrize('fault',['undeclared','wrong-target','wrong-declaration-target','writable','missing-reference','duplicate-reference','outside-scope'])
def test_runtime_secret_mounts_fail_closed(tmp_path,monkeypatch,fault):
    m,p,_,r,mounts=secret_fixture(tmp_path,monkeypatch)
    if fault=='undeclared': mounts.append(dict(mounts[-1],target='/run/secrets/unknown.json'))
    elif fault=='wrong-target': mounts[-1]['target']='/run/secrets/wrong.json'
    elif fault=='wrong-declaration-target':
        r['services']['app']['secrets'][0]['target']='/run/secrets/wrong.json'
        mounts[-1]['target']='/run/secrets/wrong.json'
    elif fault=='writable': mounts[-1]['read_only']=False
    elif fault=='missing-reference': r['services']['app']['secrets']=[];r['secrets']={}
    elif fault=='duplicate-reference': r['services']['app']['secrets']*=2
    else: r['secrets']['service-credential']['file']=str(tmp_path.parent/'outside-secret.json')
    p['rendered_compose_sha256']=host._digest(r)
    with pytest.raises(host.HostAuthorizationError):host._validate_runtime_mounts(m,mounts,p)


def test_candidate_secret_bindings_are_files_and_not_generic_secret_binds():
    m=json.loads((ROOT/'02_configs/runtime_contracts/spread-production-runtime.json').read_text())
    m['_secret_declarations']={'market-data-service':'/run/secrets/market-data-service.json'}
    m['_container_user']='65532:65532'
    b=engine._runtime_bindings(m,65532,65532)
    files=[item for item in b if item.get('kind')=='file']
    assert files==[dict(relative_path='service-private/market-data-service.json',target='/run/secrets/market-data-service.json',
        read_only=True,owner_uid=0,owner_gid=65532,kind='file')]
    doc=engine._compose_document(m,'sha256:'+'a'*64,
        [dict(source='/candidate/'+item['relative_path'],target=item['target'],read_only=item['read_only']) for item in b],Path('/grants'),'a'*32)
    assert doc['services']['spread-dashboard']['secrets']==[dict(source='market-data-service',target='/run/secrets/market-data-service.json')]
    assert not any(v['target'].startswith('/run/secrets/') for v in doc['services']['spread-dashboard']['volumes'])


def test_formal_pre_release_producer_signs_and_verifies_packaging(monkeypatch,tmp_path):
    tests=load('08_tests/test_pre_release_runtime.py','repair_formal_producer_tests')
    original=tests._evidence
    def emitted(binding):
        e=original(binding)
        e['spread_runtime_packaging']=packaging(e)
        return e
    monkeypatch.setattr(tests,'_evidence',emitted)
    tests.test_candidate_validation_engine_and_record_fail_closed(monkeypatch,tmp_path,None)


def test_ephemeral_packaging_is_strict_and_does_not_claim_production_trust():
    key,item=signed_packaging();e=item['payload']['evidence'];p=e['spread_runtime_packaging']
    p.update(identity_kind='EPHEMERAL_CI_CANDIDATE_VALIDATION_ROOT',private_key_persisted=False,
             public_key_fingerprint='a'*64,workflow_run_id='12345')
    item['payload']['evidence_sha256']=hashlib.sha256(record.canonical(e)).hexdigest();fixtures.resign(item,key)
    assert record.verify_record(fixtures.raw(item),fixtures.trust(key),now=fixtures.NOW)


def test_hosted_actual_evidence_signing_uses_the_formal_strict_record_contract(tmp_path):
    e=fixtures.evidence('target-runtime-validator/2');e['spread_runtime_packaging']=packaging(e)
    e2e=load('08_tests/shared/runtime_release_record_e2e.py','repair_hosted_record_contract')
    receipt=e2e.verify_actual_engine_record(e,tmp_path/'record')
    assert receipt['CANDIDATE_VALIDATION_RECORD']==receipt['UNKNOWN_RECORD_FIELD_REJECTED']=='PASS'
    assert receipt['production_trust_changed'] is False and receipt['production_approval'] is False


def test_readonly_claim_uses_formal_runtime_mode_not_cli_spelling():
    from agri_research_agent.shared.runtime_context import RuntimeMode
    key,item=signed_packaging()
    claim=item['payload']['evidence']['spread_runtime_packaging']['readonly_initialization']
    assert claim['mode']==RuntimeMode.CANDIDATE_VALIDATION.value
    claim['mode']='candidate-validation'
    item['payload']['evidence_sha256']=hashlib.sha256(record.canonical(item['payload']['evidence'])).hexdigest()
    fixtures.resign(item,key)
    with pytest.raises(record.CandidateValidationRecordError,match='readonly initialization identity'):
        record.verify_record(fixtures.raw(item),fixtures.trust(key),now=fixtures.NOW)


def test_public_hosted_record_artifacts_are_readable_without_exposing_a_private_key(tmp_path):
    import os
    import stat
    e=fixtures.evidence('target-runtime-validator/2');e['spread_runtime_packaging']=packaging(e)
    e2e=load('08_tests/shared/runtime_release_record_e2e.py','repair_readable_public_record')
    output=tmp_path/'record'
    previous=os.umask(0o077)
    try:
        receipt=e2e.verify_actual_engine_record(e,output)
    finally:
        os.umask(previous)
    assert receipt['private_key_persisted'] is False
    assert {p.name for p in output.iterdir()}=={'signed-record.json','fixture-public-trust.json','receipt.json'}
    if os.name=='posix':
        assert stat.S_IMODE(output.stat().st_mode)==0o755
        assert all(stat.S_IMODE(p.stat().st_mode)==0o644 for p in output.iterdir())
