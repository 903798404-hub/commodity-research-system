"""Bounded SQL and isolated approval fixtures; never production approval."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load('_test_strict_migration', '04_scripts/runtime/strict_migration_gate.py')
pre = load('_test_migration_pre', '04_scripts/runtime/pre_release_runtime.py')
SOURCE = (ROOT / gate.SOURCE).read_bytes()
STORE = (ROOT / gate.STORE).read_bytes()
NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def observation():
    return dict(schema_version='strict-migration-validation/1', profile=gate.PROFILE, status='PASS',
                binding_sha256='a' * 64, checks=dict.fromkeys(gate.CHECKS, 'PASS'), production_authorized=False)


def test_literal_pending_sql_preserves_completed_requests_and_legacy_state():
    proof = gate.probe_sources(SOURCE, STORE, STORE)
    assert proof['checks'] == dict.fromkeys(gate.CHECKS, 'PASS')
    assert proof['source_sha256'][gate.SOURCE] == hashlib.sha256(SOURCE).hexdigest()
    assert proof['probe_scope'] == 'ALLOWLISTED_SQL_IN_MEMORY_NOT_ACTUAL_RUNTIME_REHEARSAL'


@pytest.mark.parametrize('change', ['no_null', 'all_requests', 'other_table', 'other_column',
    'params', 'guard', 'guard_omitted', 'transaction', 'extra_delete', 'extra_schema', 'dynamic_ddl'])
def test_unsafe_or_unknown_sql_profile_cannot_be_approved(change):
    text = SOURCE.decode('utf-8').replace('\r\n', '\n')
    replacements = {
        'no_null': (' AND result_json IS NULL', ''),
        'all_requests': ('WHERE request_id=? AND result_json IS NULL', 'WHERE result_json IS NULL'),
        'other_table': ('UPDATE quote_submissions', 'UPDATE cnf'),
        'other_column': ('SET result_json=?,result_sha256=?', 'SET cnf_json=?,result_sha256=?'),
        'params': ("submission['request_id'])).rowcount", "origin)).rowcount"),
        'guard': ('if changed != 1:', 'if changed == 1:'),
        'guard_omitted': ("if changed != 1:\n            raise ValueError('参考行情请求已完成或不存在')", 'pass'),
        'transaction': ('sqlite3.connect(path, timeout=10)', 'sqlite3.connect(other_path, timeout=10)'),
        'extra_delete': ('return result\n\n\n', "db.execute('DELETE FROM cnf')\n    return result\n\n\n"),
        'extra_schema': ('request_id TEXT PRIMARY KEY', 'request_id TEXT'),
        'dynamic_ddl': ("DDL = '''CREATE", "DDL = factory('''CREATE"),
    }
    old, new = replacements[change]
    assert old in text
    text = text.replace(old, new)
    with pytest.raises((ValueError, SyntaxError)):
        gate.probe_sources(text.encode(), STORE, STORE)


@pytest.mark.parametrize('side', ['base', 'target'])
def test_existing_cnf_schema_change_is_not_a_pending_result_write(side):
    changed = STORE.replace(b'value REAL', b'value TEXT')
    with pytest.raises(ValueError, match='EXISTING_CNF_SCHEMA_CHANGED'):
        gate.probe_sources(SOURCE, changed if side == 'base' else STORE, changed if side == 'target' else STORE)


@pytest.fixture
def verified_inputs(tmp_path):
    def ref(name, value):
        path = tmp_path / name
        raw = gate.canonical(value)
        path.write_bytes(raw)
        return dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())
    identity = dict(commit='a' * 40, tree='b' * 40)
    risk = dict(base=identity, target=dict(commit='c' * 40, tree='d' * 40), findings=[],
        RELEASE_RISK_CLASS='STATEFUL_OR_INFRA', FINAL_RELEASE_TREATMENT='HIGH_RISK',
        STATE_CHANGE_CLASS='IRREVERSIBLE_OR_DESTRUCTIVE', MACHINE_DESTRUCTIVE_EVIDENCE=True,
        MACHINE_STATE_CHANGE_CLASS='IRREVERSIBLE_OR_DESTRUCTIVE', REVERSIBILITY_EVIDENCE={},
        DESTRUCTIVE_FINDINGS=[dict(path=gate.SOURCE, operation='db.execute', reason='EXPLICIT_SCHEMA_OR_DATA_MIGRATION')])
    request = dict(project_id='fixture', current=dict(runtime_config=ref('config', {}), data_schema=ref('data', {})))
    for name in ('state_plan', 'candidate_record', 'recovery_evidence', 'acceptance_plan'):
        request[name] = ref(name, {'fixture': name})
    state = dict(irreversible='NO', compatible=True, database_migration=False,
                 production_data_mutation=False, storage_format_change=False)
    tool = dict(commit='e' * 40, tree='f' * 40)
    image = 'sha256:' + 'a' * 64
    main = copy.deepcopy(tool)
    probe = gate.probe_sources(SOURCE, STORE, STORE)
    binding = gate.prepare_binding(request, risk, tool=tool, main=main, candidate_image_id=image, probe=probe)
    approval = dict(schema_version='strict-migration-approval/1', binding=binding,
        decision='APPROVE_BOUNDED_PENDING_RESULT', reviewer='SYNTHETIC_NOT_PRODUCTION_APPROVAL',
        timestamp=datetime.now(timezone.utc).isoformat(), reason='Inert isolated fixture')
    request['migration_gate'] = ref('independent-approval', approval)
    host = SimpleNamespace(_json=json.loads, _protected_path=lambda path, **kw: path)
    def read(reference, _):
        raw = Path(reference['path']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == reference['sha256']
        return raw
    adapter = SimpleNamespace(ROOT=ROOT, Path=Path, _risk_file=read, current_main_identity=lambda _: main,
        _risk_git=lambda root, command, spec: SOURCE if spec.endswith(':' + gate.SOURCE) else STORE)
    return request, risk, state, host, tool, image, main, approval, adapter


def verify(values):
    request, risk, state, host, tool, image, _, _, adapter = values
    return gate.verify(adapter, request, risk, state, host, tool=tool,
        candidate_image_id=image, rehearsal_validated=True)


def test_independent_approval_exact_bindings_and_full_recovery_are_required(verified_inputs):
    result, main = verify(verified_inputs)
    assert result['status'] == 'PASS' and result['production_authorized'] is False
    assert main == verified_inputs[6]


@pytest.mark.parametrize('name', ['profile', 'project_id', 'base', 'target', 'tool', 'authoritative_main',
    'candidate_image_id', 'release_inputs_sha256', 'machine_findings_sha256', 'state_plan', 'candidate_record',
    'current_runtime_config', 'current_data_schema', 'recovery_evidence', 'acceptance_plan', 'probe'])
def test_approval_cannot_be_reused_after_any_bound_input_moves(verified_inputs, name):
    approval = verified_inputs[7]
    binding = copy.deepcopy(approval['binding'])
    binding[name] = 'changed'
    with pytest.raises(ValueError, match='BINDING_CHANGED'):
        gate.validate_approval(approval, binding)


@pytest.mark.parametrize('change', ['downgrade', 'extra_finding', 'delete', 'irreversible', 'unknown',
    'migration', 'mutation', 'format', 'incompatible', 'missing_recovery', 'shared_approval', 'main_moves'])
def test_unsupported_migration_scope_fails_closed(verified_inputs, change):
    request, risk, state, _, _, _, main, _, _ = verified_inputs
    if change == 'downgrade': risk['FINAL_RELEASE_TREATMENT'] = 'ADDITIVE_REVERSIBLE'
    if change == 'extra_finding': risk['DESTRUCTIVE_FINDINGS'].append(dict(path='other', operation='unlink'))
    if change == 'delete': risk['DESTRUCTIVE_FINDINGS'][0]['operation'] = 'db.executescript'
    if change in ('irreversible', 'unknown'): state['irreversible'] = 'YES' if change == 'irreversible' else 'UNKNOWN'
    if change in ('migration', 'mutation', 'format'):
        state[dict(migration='database_migration', mutation='production_data_mutation', format='storage_format_change')[change]] = True
    if change == 'incompatible': state['compatible'] = False
    if change == 'missing_recovery': request['recovery_evidence'] = None
    if change == 'shared_approval': request['migration_gate'] = request['state_plan']
    if change == 'main_moves': main['commit'] = '9' * 40
    with pytest.raises(ValueError): verify(verified_inputs)


@pytest.mark.parametrize('hours', [-1, 25])
def test_expired_or_future_approval_fails(hours, verified_inputs):
    approval = verified_inputs[7]
    approval['timestamp'] = (NOW - timedelta(hours=hours)).isoformat()
    with pytest.raises(ValueError): gate.validate_approval(approval, approval['binding'], now=NOW)


@pytest.mark.parametrize('change', ['pending', 'fake_status', 'extra', 'missing', 'untyped_time', 'naive_time', 'no_reviewer', 'no_reason'])
def test_approval_is_not_a_boolean_or_copied_pass_label(verified_inputs, change):
    approval = verified_inputs[7]
    if change == 'pending': approval['decision'] = 'PENDING'
    if change == 'fake_status': approval['status'] = 'PASS'
    if change == 'extra': approval['production_authorized'] = True
    if change == 'missing': del approval['reason']
    if change == 'untyped_time': approval['timestamp'] = 0
    if change == 'naive_time': approval['timestamp'] = NOW.replace(tzinfo=None).isoformat()
    if change == 'no_reviewer': approval['reviewer'] = ''
    if change == 'no_reason': approval['reason'] = ''
    with pytest.raises(ValueError): gate.validate_approval(approval, verified_inputs[7]['binding'], now=NOW)


def aggregate(value=None, **changes):
    values = dict(assets_ready=True, candidate_validated=True, irreversible_state_change='NO',
        compatibility=True, acceptance_plan_ready=True, rehearsal_validated=True, old_grant_expired=True,
        migration_validation=value)
    risk = changes.pop('risk', dict(RELEASE_RISK_CLASS='STATEFUL_OR_INFRA', FINAL_RELEASE_TREATMENT='HIGH_RISK',
        STATE_CHANGE_CLASS='IRREVERSIBLE_OR_DESTRUCTIVE', MACHINE_DESTRUCTIVE_EVIDENCE=True))
    values.update(changes)
    return pre.evaluate_release_gate(risk, **values)


def test_admitted_bounded_profile_preserves_destructive_classification_and_all_recovery_requirements():
    assert aggregate()['PRODUCTION_RELEASE_PREFLIGHT'] == 'FAIL'
    result = aggregate(observation())
    assert result['PRODUCTION_RELEASE_PREFLIGHT'] == 'PASS'
    assert result['STATE_CHANGE_CLASS'] == 'IRREVERSIBLE_OR_DESTRUCTIVE'
    assert result['FULL_ROLLBACK_REHEARSAL_REQUIRED'] and result['TARGETED_RECOVERY_VALIDATION_REQUIRED']
    assert result['production_authorized'] is False


@pytest.mark.parametrize('name,value', [('rehearsal_validated', False), ('compatibility', False),
    ('assets_ready', False), ('candidate_validated', False), ('acceptance_plan_ready', False),
    ('irreversible_state_change', 'YES'), ('irreversible_state_change', 'UNKNOWN')])
def test_migration_admission_never_waives_other_release_requirements(name, value):
    assert aggregate(observation(), **{name: value})['PRODUCTION_RELEASE_PREFLIGHT'] == 'FAIL'


@pytest.mark.parametrize('value', [True, {}, dict(status='PASS'), dict(observation(), production_authorized=True),
    dict(observation(), checks={}), dict(observation(), profile='any-migration'), dict(observation(), binding_sha256='z'*64)])
def test_aggregator_rejects_malformed_trusted_observations(value):
    with pytest.raises(ValueError): aggregate(value)


@pytest.mark.parametrize('fault', [None, 'missing', 'pending', 'hash', 'private', 'main_drift', 'no_recovery', 'recovery_fail'])
def test_formal_assessment_consumes_independent_gate_and_rechecks_inputs(verified_inputs, tmp_path, monkeypatch, fault):
    request, risk, state, host, tool, image, main, approval, _ = verified_inputs
    def ref(name, value):
        path = tmp_path / name
        raw = gate.canonical(value)
        path.write_bytes(raw)
        return dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())
    target = risk['target']
    state.update(base=risk['base'], target=target, data_schema_sha256=request['current']['data_schema']['sha256'])
    request['state_plan'] = ref('state', state)
    binding = dict(target, project_id=request['project_id'])
    request['candidate_record'] = ref('candidate', dict(binding=binding, image_id=image))
    request['acceptance_plan'] = ref('acceptance', dict(target=target, image_id=image, checks=['actual UI']))
    request['current'].update(commit=risk['base']['commit'], tree=risk['base']['tree'], image_id='sha256:'+'b'*64)
    request['previous'] = copy.deepcopy(request['current'])
    request.update(schema_version='production-release-request/1', target_commit=target['commit'],
                   target_source_root=str(ROOT), maintainer_risk_review={'fixture': 'HIGH_RISK'})
    recovery = dict(schema_version='production-recovery-observation/1', base=risk['base'], target=target,
        old_image_id=request['current']['image_id'], runtime_config_sha256=request['current']['runtime_config']['sha256'],
        data_schema_sha256=request['current']['data_schema']['sha256'], method='fresh-recovery',
        observed_at=datetime.now(timezone.utc).isoformat(), result='PASS',
        evidence={k: ref(k, {}) for k in ('instance', 'grant', 'preflight', 'health', 'consumer', 'data_unchanged')})
    request['recovery_evidence'] = ref('recovery', recovery) if fault != 'no_recovery' else None
    approval['binding'] = gate.prepare_binding(request, risk, tool=tool, main=main,
        candidate_image_id=image, probe=gate.probe_sources(SOURCE, STORE, STORE))
    if fault == 'pending': approval['decision'] = 'PENDING'
    request['migration_gate'] = ref('approval', approval)
    if fault == 'missing': del request['migration_gate']
    if fault == 'hash': Path(request['migration_gate']['path']).write_bytes(b'{}')
    protected_calls = []
    def protected(path, **kwargs):
        protected_calls.append((path, kwargs))
        if fault == 'private' and path.name == 'approval' and kwargs.get('private'):
            raise ValueError('not root-private')
        return path
    host._protected_path = protected
    host.require_protected_authority_source = lambda: None
    host.docker_image_inspect = lambda value: dict(Id=value, Config=dict(Labels={
        'org.opencontainers.image.revision': target['commit'], 'market-data.git.tree': target['tree']}))
    engine = SimpleNamespace(_project=lambda *a: {'runtime_contract': 'fixture'}, source_contract=lambda *a: (None, {}, binding))
    records = SimpleNamespace(verify_record=lambda raw, trust: dict(evidence=json.loads(raw)))
    original_load = pre._load
    monkeypatch.setattr(pre, '_load', lambda name, alias: {
        pre.HOST: host, pre.ENGINE: engine, pre.RECORD: records,
    }[name] if name in (pre.HOST, pre.ENGINE, pre.RECORD) else original_load(name, alias))
    monkeypatch.setattr(pre, 'require_source', lambda *a, **kw: (target['commit'], target['tree']) if 'source_root' in kw else (tool['commit'], tool['tree']))
    monkeypatch.setattr(pre, 'classify_release', lambda *a: copy.deepcopy(risk))
    monkeypatch.setattr(pre, 'apply_maintainer_review', lambda risk, review, main: risk)
    monkeypatch.setattr(pre, 'verify_rollback_assets', lambda root, asset, h: dict(commit=asset['commit'], image_id=asset['image_id']))
    monkeypatch.setattr(pre, '_risk_git', lambda root, command, spec: SOURCE if spec.endswith(':' + gate.SOURCE) else STORE)
    main_calls = []
    def authoritative(_):
        main_calls.append(True)
        return dict(main, commit='9'*40) if fault == 'main_drift' and len(main_calls) > 1 else main
    monkeypatch.setattr(pre, 'current_main_identity', authoritative)
    recovery_calls = []
    def actual_recovery(*args, **kwargs):
        recovery_calls.append(True)
        if fault == 'recovery_fail': raise pre.PreReleaseError('actual recovery failed')
    monkeypatch.setattr(pre, 'verify_recovery_observation', actual_recovery)
    monkeypatch.setattr(pre, '_write_new', lambda h, path, raw: path.write_bytes(raw))
    path = tmp_path / 'request.json'
    path.write_bytes(gate.canonical(request))
    output = tmp_path / 'result.json'
    if fault not in (None, 'missing'):
        with pytest.raises((ValueError, OSError)): pre.assess_release(path, output)
        assert not output.exists()
    else:
        result = pre.assess_release(path, output)
        assert result['PRODUCTION_RELEASE_PREFLIGHT'] == ('FAIL' if fault == 'missing' else 'PASS')
        assert result['MACHINE_DESTRUCTIVE_EVIDENCE'] is True
        assert result['STATE_CHANGE_CLASS'] == 'IRREVERSIBLE_OR_DESTRUCTIVE'
        assert result['FULL_ROLLBACK_REHEARSAL_REQUIRED'] is True
        assert result['production_authorized'] is False
        assert len(recovery_calls) == 2
        if fault is None:
            assert any(path.name == 'approval' and options.get('private') for path, options in protected_calls)
        else:
            assert result['SEPARATE_MIGRATION_GATE'] is None
            assert result['SEPARATE_MIGRATION_PROPOSAL']['decision'] == 'PENDING'
            assert result['SEPARATE_MIGRATION_PROPOSAL']['reviewer'] == ''


def test_just_a_targeted_probe_never_replaces_full_runtime_recovery(verified_inputs):
    request, risk, state, host, tool, image, _, _, adapter = verified_inputs
    with pytest.raises(ValueError, match='FULL_RECOVERY_REQUIRED'):
        gate.verify(adapter, request, risk, state, host, tool=tool,
            candidate_image_id=image, rehearsal_validated=False)
