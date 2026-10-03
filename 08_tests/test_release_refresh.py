"""Entry-level record refresh tests. Docker transport here is unit-only.

Signing, strict record verification, target binding and immutable output are
real; the Hosted owner lane separately proves real container probes.
"""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[1]


def load(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def refresh(monkeypatch, tmp_path):
    pre = load('04_scripts/runtime/pre_release_runtime.py', 'refresh_test_pre')
    record = load('09_deploy/runtime_identity/candidate_validation_record.py', 'refresh_test_record')
    fixture = load('08_tests/test_candidate_validation_record.py', 'refresh_record_fixture')
    key = Ed25519PrivateKey.generate()
    trust = fixture.trust(key)
    source = tmp_path / 'app'
    source.mkdir()
    trust_path = tmp_path / 'trust.json'
    trust_path.write_text(json.dumps(trust), encoding='utf-8')
    evidence = fixture.evidence('target-runtime-validator/2')
    binding = evidence['binding']
    now = datetime.now(timezone.utc)
    payload = dict(record_id='1' * 32, purpose='target-runtime-validation', authorization_role='candidate_validation',
                   issued_at=(now-timedelta(hours=2)).isoformat(), expires_at=(now+timedelta(hours=1)).isoformat(), evidence=evidence)
    host = SimpleNamespace(require_protected_authority_source=lambda: None,
        _protected_path=lambda path, **kw: path, _json=json.loads,
        _load_private_key=lambda path: key, _fsync_directory=lambda path: None)
    project = dict(project_id=binding['project_id'], runtime_contract='runtime.json')
    engine = SimpleNamespace(_project=lambda *a: project, source_contract=lambda *a: (project, {}, binding))
    monkeypatch.setattr(pre, 'ROOT', source)
    monkeypatch.setattr(pre, 'TRUST', str(trust_path))
    monkeypatch.setattr(pre, 'KEY_DIRECTORY', tmp_path)
    monkeypatch.setattr(pre.sys, 'platform', 'linux')
    monkeypatch.setattr(pre.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(pre, '_load', lambda p, n: host if p == pre.HOST else engine if p == pre.ENGINE else record)
    monkeypatch.setattr(pre, 'require_source', lambda *a, **kw: (binding['commit'], binding['tree']))
    calls = []
    def execute(project, output, **kw):
        calls.append(kw)
        assert kw['existing_image_id'] == evidence['image_id']
        assert kw['application_source_root'] == source
        output.write_bytes(record.canonical(evidence))
        return 0
    monkeypatch.setattr(pre, '_execute_validation', execute)
    old = tmp_path / 'old.json'
    def old_record(change=None):
        value = fixture.envelope(key, payload=payload)
        if change:
            change(value)
            fixture.resign(value, key)
        old.write_bytes(record.canonical(value))
        return dict(path=str(old), sha256=hashlib.sha256(old.read_bytes()).hexdigest())
    def ensure(reference, **kwargs):
        options = dict(application_source_root=source, image_id=evidence['image_id'], destination=tmp_path/'new.json',
                       key_path=tmp_path/'candidate.pem', minimum_remaining_seconds=300)
        options.update(kwargs)
        return pre.ensure_candidate_record(binding['project_id'], reference, **options)
    return SimpleNamespace(pre=pre, record=record, trust=trust, payload=payload, old=old,
        old_record=old_record, ensure=ensure, calls=calls, key=key, path=tmp_path, evidence=evidence)


def test_valid_record_reuses_without_validation_or_signing(refresh):
    ref = refresh.old_record()
    original = refresh.old.read_bytes()
    assert refresh.ensure(ref)['action'] == 'REUSED'
    assert refresh.calls == [] and refresh.old.read_bytes() == original
    assert not (refresh.path/'new.json').exists()


def test_hosted_expiry_fixture_verifies_issuance_without_claiming_current_validity(refresh, monkeypatch):
    """Replay the real Hosted failure: source checks outlive a short test TTL."""
    fixture = load('08_tests/shared/high_risk_execution_docker_e2e.py', 'refresh_hosted_expiry_fixture')
    refresh.payload['expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    refresh.old_record()
    original = refresh.old.read_bytes()
    issued = datetime.fromisoformat(refresh.payload['issued_at'])
    payload = refresh.record.verify_record(original, refresh.trust, now=issued)
    calls = []

    def delayed_producer(project, destination, key, *, ttl_seconds):
        calls.append(ttl_seconds)
        destination.write_bytes(original)
        return payload

    producer = SimpleNamespace(validate_candidate=delayed_producer)
    monkeypatch.setattr(fixture, 'load', lambda root, path, name:
        producer if path.endswith('pre_release_runtime.py') else refresh.record)
    evidence, path = fixture.validate(refresh.path/'target-source', refresh.path,
        refresh.path/'test-key.pem', refresh.trust, ttl_seconds=1)
    assert calls == [1] and evidence == payload['evidence']
    assert path.read_bytes() == original
    # Historical signature validity is NOT current admission. No current clock
    # override is supplied to the real verifier at the preparation boundary.
    with pytest.raises(refresh.record.CandidateValidationRecordExpired):
        refresh.record.verify_record(path.read_bytes(), refresh.trust)


def test_hosted_exports_only_nonsecret_engine_receipts_even_after_failure():
    import yaml
    workflow = yaml.safe_load((ROOT/'.github/workflows/trusted-main-admission.yml').read_text(encoding='utf-8'))
    step = next(s for s in workflow['jobs']['linux']['steps']
        if s.get('name') == 'Validate real spread-runtime image with ephemeral candidate trust')
    command = step['run']
    assert 'trap export_validation_receipts EXIT' in command
    assert 'for name in spread-runtime-hosted-evidence.json image-validation-execution.json; do' in command
    assert 'sudo chmod 0644 "$evidence"' in command
    assert command.index('trap export_validation_receipts EXIT') < command.index('sudo GITHUB_RUN_ID=')
    assert 'chmod -R' not in command


@pytest.mark.parametrize('mode', ['expired', 'short', 'explicit', 'missing'])
def test_refresh_uses_formal_producer_and_real_verifier(refresh, mode):
    if mode == 'expired':
        refresh.payload['expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    if mode == 'short':
        refresh.payload['expires_at'] = (datetime.now(timezone.utc)+timedelta(seconds=100)).isoformat()
    ref = refresh.old_record() if mode != 'missing' else None
    original = refresh.old.read_bytes() if ref else None
    if mode == 'expired':
        with pytest.raises(refresh.record.CandidateValidationRecordExpired):
            refresh.record.verify_record(original, refresh.trust)
    answer = refresh.ensure(ref, revalidate=mode == 'explicit')
    assert answer['action'] == 'REVALIDATED' and len(refresh.calls) == 1
    new = refresh.record.verify_record((refresh.path/'new.json').read_bytes(), refresh.trust)
    assert new['record_id'] != refresh.payload['record_id']
    assert new['evidence']['image_id'] == refresh.evidence['image_id']
    assert datetime.fromisoformat(new['expires_at'])-datetime.fromisoformat(new['issued_at']) == timedelta(days=1)
    if ref:
        assert refresh.old.read_bytes() == original


@pytest.mark.parametrize('mutation', ['signature', 'schema', 'revoked', 'unknown_trust', 'target', 'image', 'future'])
def test_security_failure_never_enters_refresh_even_when_expired(refresh, mutation):
    refresh.payload['expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    if mutation == 'future':
        refresh.payload['issued_at'] = (datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat()
        refresh.payload['expires_at'] = (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
    def change(value):
        if mutation == 'schema':value['extra'] = True
    ref = refresh.old_record(change)
    if mutation == 'signature':
        value = json.loads(refresh.old.read_bytes())
        value['signature'] = base64.b64encode(b'0'*64).decode()
        refresh.old.write_bytes(refresh.record.canonical(value))
        ref['sha256'] = hashlib.sha256(refresh.old.read_bytes()).hexdigest()
    if mutation == 'revoked':
        refresh.trust['revoked_key_ids'] = ['candidate-key']
        (refresh.path/'trust.json').write_text(json.dumps(refresh.trust),encoding='utf-8')
    if mutation == 'unknown_trust':
        refresh.trust['keys'] = []
        (refresh.path/'trust.json').write_text(json.dumps(refresh.trust), encoding='utf-8')
    if mutation == 'target':refresh.evidence['binding']['commit'] = 'a'*40
    options = dict(image_id='sha256:'+'0'*64) if mutation == 'image' else {}
    with pytest.raises(ValueError):refresh.ensure(ref, revalidate=True, **options)
    assert refresh.calls == [] and not (refresh.path/'new.json').exists()


def test_probe_failure_never_issues_pass_or_replaces_old(refresh, monkeypatch):
    refresh.payload['expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    ref = refresh.old_record()
    original = refresh.old.read_bytes()
    monkeypatch.setattr(refresh.pre, '_execute_validation', lambda *a, **kw: 2)
    with pytest.raises(refresh.pre.PreReleaseError, match='engine did not complete'):
        refresh.ensure(ref)
    assert refresh.old.read_bytes() == original and not (refresh.path/'new.json').exists()


def test_new_signed_record_survives_later_plan_failure_without_publishing_plan(refresh):
    """Real producer/verifier and preparation entry; Docker remains unit transport."""
    import contextlib
    execution = load('09_deploy/spread_release/high_risk_execution.py', 'refresh_failed_plan_execution')
    plans = load('08_tests/test_high_risk_execution.py', 'refresh_failed_plan_fixture')
    refresh.payload['expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    old_reference = refresh.old_record()
    old_bytes = refresh.old.read_bytes()
    old_folder = refresh.path/'old-plan'
    old_folder.mkdir()
    old_plan = old_folder/'deployment_plan.json'
    execution.seal_plan(plans.plan(), old_plan)
    old_files = {p: p.read_bytes() for p in old_folder.iterdir() if p.is_file()}
    destination = refresh.path/'deployment_plan.json'
    events = []

    class Backend:
        def lock(self, value):
            return contextlib.nullcontext()

        def refresh_plan_inputs(self, value, **kwargs):
            result = refresh.ensure(old_reference)
            events.append('new_signed_record')
            value = dict(value)
            value['release_request'] = result['reference']
            return value

        def prepare_execution_conditions(self, value):
            # Unit transport only; real root-owned preparation is covered by
            # the permanent same-entry Hosted Docker lane, not this verdict.
            events.append('prepared_execution_conditions')

        def preconditions(self, value):
            reference = value['release_request']
            raw = Path(reference['path']).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == reference['sha256']
            payload = refresh.record.verify_record(raw, refresh.trust)
            assert payload['record_id'] != refresh.payload['record_id']
            events.append('verified_new_record_before_assessment_failure')
            raise ValueError('controlled downstream assessment failure')

    with pytest.raises(ValueError, match='controlled downstream assessment failure'):
        execution.prepare_plan(plans.plan(), destination, Backend(),
            candidate_key=refresh.path/'candidate.pem')
    assert events == ['new_signed_record', 'prepared_execution_conditions',
                      'verified_new_record_before_assessment_failure']
    assert len(refresh.calls) == 1
    assert refresh.old.read_bytes() == old_bytes
    assert all(path.read_bytes() == raw for path, raw in old_files.items())
    assert not destination.exists()
    assert not destination.with_name('deployment_plan.manifest.json').exists()
    # Diagnostic signed output is retained, not deleted or promoted to a plan.
    refresh.record.verify_record((refresh.path/'new.json').read_bytes(), refresh.trust)


def test_impossible_budget_stops_before_any_validation(refresh):
    with pytest.raises(refresh.pre.PreReleaseError, match='TIME_BUDGET_UNSATISFIABLE'):
        refresh.ensure(refresh.old_record(), minimum_remaining_seconds=86400)
    assert refresh.calls == []


@pytest.fixture
def plan_acceptance(refresh, monkeypatch):
    execution = load('09_deploy/spread_release/high_risk_execution.py', 'plan_acceptance_execution')
    host = load('09_deploy/runtime_identity/host_authorization.py', 'plan_acceptance_host')
    routine = load('04_scripts/runtime/routine_release.py', 'plan_acceptance_routine')
    backend = execution.HostBackend.__new__(execution.HostBackend)
    backend.pre, backend.host = refresh.pre, host
    backend.engine = refresh.pre._load(refresh.pre.ENGINE, 'engine')
    binding = refresh.evidence['binding']
    asset = dict(commit=binding['commit'], tree=binding['tree'], image_id=refresh.evidence['image_id'])
    ref = refresh.old_record()
    policy = dict(schema_version='host-runtime-policy/5', candidate_record=ref,
        approved_commit=asset['commit'], approved_tree=asset['tree'], image_id=asset['image_id'],
        approved_source_root=str(refresh.pre.ROOT), project_id=binding['project_id'],
        runtime_manifest_sha256='a'*64, runtime_manifest_path='/app/runtime.json', source_root='/app')
    # Host ownership and source checkout are a unit-test boundary; parsing,
    # content hashes, family dispatch and both record verifiers remain real.
    monkeypatch.setattr(host, '_protected_path', lambda path, **kw: path)
    monkeypatch.setattr(host, '_require_linux_root', lambda: None)
    monkeypatch.setattr(host, 'validate_policy', lambda *a: None)
    checks = []
    monkeypatch.setattr(routine, 'require_routine', lambda *a: checks.append(a))
    manifest = dict(schema_version='runtime-manifest/3')
    routine_engine = SimpleNamespace(_project=lambda *a: dict(runtime_contract='runtime.json'),
        source_contract=lambda *a: ({}, manifest, dict(source_sha256={'runtime.json': 'a'*64})),
        validate_source_compose=lambda *a: checks.append('compose'))
    contract_loader = host._contract_module
    monkeypatch.setattr(host, '_contract_module', lambda path, name:
        routine if path.name == 'routine_release.py' else refresh.pre
        if path.name == 'pre_release_runtime.py' else routine_engine
        if path.name == 'validate_target_runtime.py' else contract_loader(path, name))
    def legacy(**changes):
        record = dict(schema_version='routine-candidate-acceptance/1', **asset,
            project_id=binding['project_id'], base_commit='b'*40, release_id='old-release',
            validated_at=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat(),
            result='PASS', runtime_preflight='PASS', health='PASS', application_smoke='PASS',
            candidate_cleanup='PASS', ui_acceptance_mode='AUTOMATED')
        record.update(changes)
        refresh.old.write_text(json.dumps(record), encoding='utf-8')
        policy['candidate_record'] = dict(path=str(refresh.old), sha256=hashlib.sha256(refresh.old.read_bytes()).hexdigest())
        return record
    return SimpleNamespace(backend=backend, policy=policy, asset=asset, legacy=legacy,
                           checks=checks, execution=execution)


def test_plan_routine_rollback_uses_original_host_dispatcher_and_verifier(plan_acceptance, refresh):
    p = plan_acceptance
    p.legacy()
    original = refresh.old.read_bytes()
    result = p.backend.resolve_acceptance(p.policy, 'primary_rollback', p.asset)
    assert result['trust_source'] == 'protected-file' and result['signature_verified'] is False
    assert result['expires_at'] is None and result['reference'] == p.policy['candidate_record']
    assert len(p.checks) == 2 and p.checks[-1] == 'compose'
    assert refresh.old.read_bytes() == original and refresh.calls == []


@pytest.mark.parametrize('role', ['target', 'primary_rollback'])
def test_plan_signed_acceptance_uses_real_signature_verifier(plan_acceptance, role):
    p = plan_acceptance
    assert p.backend.resolve_acceptance(p.policy, role, p.asset)['signature_verified'] is True
    assert p.checks == []


@pytest.mark.parametrize('change', [
    {'schema_version': 'routine-candidate-acceptance/2'},
    {'signature': 'forged'}, {'payload': {}}, {'algorithm': 'ed25519'}, {'key_id': 'forged'},
    {'commit': 'f'*40}, {'tree': 'f'*40}, {'image_id': 'sha256:'+'f'*64},
    {'result': 'FAIL'}, {'candidate_cleanup': 'FAIL'},
])
def test_plan_wrong_family_and_invalid_legacy_rejected(plan_acceptance, change):
    p = plan_acceptance
    p.legacy(**change)
    with pytest.raises(ValueError):
        p.backend.resolve_acceptance(p.policy, 'primary_rollback', p.asset)


def test_plan_routine_target_rejected_before_legacy_verifier(plan_acceptance):
    p = plan_acceptance
    p.legacy()
    with pytest.raises(ValueError, match='SIGNED_TARGET_REQUIRED'):
        p.backend.resolve_acceptance(p.policy, 'target', p.asset)
    assert p.checks == []


def test_plan_tampered_signed_input_does_not_fall_back(plan_acceptance, refresh):
    p = plan_acceptance
    document = json.loads(refresh.old.read_bytes())
    document['payload']['record_id'] = 'f'*32
    refresh.old.write_text(json.dumps(document), encoding='utf-8')
    p.policy['candidate_record']['sha256'] = hashlib.sha256(refresh.old.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        p.backend.resolve_acceptance(p.policy, 'primary_rollback', p.asset, allow_expired=True)
    assert p.checks == []


def test_plan_changed_schema_cannot_disguise_signed_envelope(plan_acceptance, refresh):
    p = plan_acceptance
    document = json.loads(refresh.old.read_bytes())
    document['schema_version'] = 'routine-candidate-acceptance/1'
    refresh.old.write_text(json.dumps(document), encoding='utf-8')
    p.policy['candidate_record']['sha256'] = hashlib.sha256(refresh.old.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='CONFLICTING_ACCEPTANCE_FAMILY'):
        p.backend.resolve_acceptance(p.policy, 'primary_rollback', p.asset)
    assert p.checks == []


@pytest.mark.parametrize('problem', ['hash', 'missing', 'protection'])
def test_plan_requires_original_protected_file_reference(plan_acceptance, refresh, monkeypatch, problem):
    p = plan_acceptance
    p.legacy()
    if problem == 'hash':
        p.policy['candidate_record']['sha256'] = 'f'*64
    elif problem == 'missing':
        p.policy['candidate_record']['path'] = str(refresh.path/'missing.json')
    else:
        def denied(*a, **kw):
            raise PermissionError('unprotected source')
        monkeypatch.setattr(p.backend.host, '_protected_path', denied)
    with pytest.raises((ValueError, OSError)):
        p.backend.resolve_acceptance(p.policy, 'primary_rollback', p.asset)
    assert p.checks == []


def test_remaining_budget_failure_after_one_real_production_attempt_stops(refresh, monkeypatch):
    """Controlled test clock, not changed host time or rewritten signed payload."""
    refresh.payload['expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    ref = refresh.old_record()
    original = refresh.old.read_bytes()
    producer = refresh.pre.validate_candidate

    def delayed_consumption(*args, **kwargs):
        result = producer(*args, **kwargs)
        expires = datetime.fromisoformat(result['expires_at'])

        class ConsumptionClock(datetime):
            @classmethod
            def now(cls, tz=None):
                assert tz is timezone.utc
                return expires - timedelta(seconds=100)

        monkeypatch.setattr(refresh.pre, 'datetime', ConsumptionClock)
        return result

    monkeypatch.setattr(refresh.pre, 'validate_candidate', delayed_consumption)
    with pytest.raises(refresh.pre.PreReleaseError, match='INSUFFICIENT_AFTER_ONE_VALIDATION'):
        refresh.ensure(ref)
    assert len(refresh.calls) == 1
    assert refresh.old.read_bytes() == original
    # Keep the new authentic record for diagnosis; failure does not publish a
    # successful refresh result or silently consume the old expired record.
    refresh.record.verify_record((refresh.path/'new.json').read_bytes(), refresh.trust)


@pytest.mark.parametrize('margin_microseconds', [-1, 0, 1])
def test_prestop_window_uses_real_signed_record_and_exact_boundary(refresh, monkeypatch, margin_microseconds):
    execution = load('09_deploy/spread_release/high_risk_execution.py', 'refresh_window_execution')
    fixed_now = datetime.now(timezone.utc)
    seconds = 240
    refresh.payload['expires_at'] = (fixed_now + timedelta(seconds=seconds,
        microseconds=margin_microseconds)).isoformat()
    ref = refresh.old_record()
    asset = dict(commit=refresh.evidence['binding']['commit'], tree=refresh.evidence['binding']['tree'],
                 image_id=refresh.evidence['image_id'])
    plan = dict(target=asset, primary_rollback=asset,
        instances=dict(target={'kind':'spec'}, primary_rollback={'kind':'spec'}))
    backend = execution.HostBackend.__new__(execution.HostBackend)
    backend.sessions = {}
    capability_checks = []
    # This unit owns only the REAL signed-record time boundary. Actual browser
    # launch and pre-stop order are required by the separate real Docker lane.
    backend._browser_preflight = lambda plan, phase: capability_checks.append(phase)
    backend.pre = refresh.pre
    backend.host = refresh.pre._load(refresh.pre.HOST, 'host')
    backend.engine = refresh.pre._load(refresh.pre.ENGINE, 'engine')
    backend.host.validate_policy = lambda *args: None
    policy = dict(candidate_record=ref, approved_commit=asset['commit'], approved_tree=asset['tree'],
                  image_id=asset['image_id'], approved_source_root=str(refresh.pre.ROOT),
                  project_id=refresh.evidence['binding']['project_id'])
    backend.read = lambda ref_: {'policy': {'kind':'policy'}} if ref_['kind'] == 'spec' else policy
    monkeypatch.setattr(execution, 'record_consumption_windows', lambda plan: dict(target=seconds, primary_rollback=seconds))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            return fixed_now

    monkeypatch.setattr(execution, 'datetime', Clock)
    if margin_microseconds > 0:
        backend.before_stop(plan)
    else:
        with pytest.raises(execution.ExecutionError, match='WINDOW_INSUFFICIENT_REPREPARE_BEFORE_STOP'):
            backend.before_stop(plan)
    assert refresh.calls == []
    assert capability_checks == ['immediately-before-stop']
