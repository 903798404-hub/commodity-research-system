"""Cross-day protected-consumer tests; real Docker evidence is the owner lane."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_pre_release_runtime import risk_repo, unknown_stateless_risk, routine_review_for, review_for, runtime
from test_high_risk_execution import execution, plan


history = execution.load(execution.ROOT, '04_scripts/runtime/historical_primary_rollback.py', '_test_history')
review_policy = runtime._load('04_scripts/runtime/release_reversibility.py', '_test_history_review')
routine = runtime._load('04_scripts/runtime/routine_release.py', '_test_history_routine')


@pytest.fixture
def chain(risk_repo):
    risk = unknown_stateless_risk(risk_repo)
    risk['project_id'] = 'spread-production-runtime-wiring'
    now = datetime.now(timezone.utc)
    event = now - timedelta(hours=48)
    old_review = routine_review_for(risk)
    old_review['timestamp'] = (event - timedelta(hours=1)).isoformat()
    asset = {**risk['target'], 'image_id': 'sha256:' + 'a' * 64}
    p = plan()
    p.update(source=asset, primary_rollback=asset)
    objects = {}

    def put(name, data):
        raw = json.dumps(data, sort_keys=True).encode()
        objects[name] = raw
        return dict(path=name, sha256=hashlib.sha256(raw).hexdigest())

    def read(ref):
        raw = objects[ref['path']]
        if hashlib.sha256(raw).hexdigest() != ref['sha256']:
            raise ValueError('REFERENCED_INPUT_CHANGED')
        return json.loads(raw)

    record = dict(schema_version='routine-candidate-acceptance/1', **asset, project_id=p['project_id'],
        base_commit=risk['base']['commit'], result='PASS', runtime_preflight='PASS', health='PASS',
        application_smoke='PASS', candidate_cleanup='PASS', release_id='test-only-release', ci_run='test-run',
        validated_at=event.isoformat(), maintainer_risk_review=old_review)
    rref = put('/protected/original.json', record)
    policy = dict(role='production', approved_commit=asset['commit'], approved_tree=asset['tree'],
        image_id=asset['image_id'], project_id=p['project_id'], candidate_record=rref)
    p['source_policy'] = put('/protected/original-policy.json', policy)
    deployment = dict(schema_version='routine-deployment-result/1', **asset, result='PASS',
        candidate_acceptance='PASS', runtime_preflight='PASS', production_health='PASS', production_smoke='PASS',
        previous_release=risk['base'], ci_run=record['ci_run'], release_id=record['release_id'],
        deployed_at=(event+timedelta(minutes=1)).isoformat(), finalized_at=(event+timedelta(minutes=2)).isoformat(),
        instance=dict(container_id=p['source_container_id'], started_at=(event+timedelta(minutes=1, seconds=1)).isoformat(),
                      spec=dict(policy_output=p['source_policy']['path'])))
    dref = put('/protected/deployment.json', deployment)
    release = dict(release_id=record['release_id'], accepted_deployment=dref)
    current_review = review_for(risk, 'HIGH_RISK')
    current_review['authoritative_main'] = p['tool']
    current_review['timestamp'] = now.isoformat()
    request = dict(project_id=p['project_id'], current={**asset, 'release': put('/protected/release.json', release)},
                   target_commit=p['target']['commit'], maintainer_risk_review=current_review)
    p['release_request'] = put('/protected/request.json', request)
    def reviewed(request, risk, host):
        resolved = review_policy.apply_review(risk, request['maintainer_risk_review'], p['tool'])
        return resolved, dict(compatible=True), p['tool']
    backend = SimpleNamespace(contract_module=execution.contract(), tool_root=execution.ROOT,
        validate_plan_intent=lambda value: execution.validate_intent(value, execution.contract()),
        host=SimpleNamespace(validate_policy=lambda *a: None), read=read, engine=None,
        pre=SimpleNamespace(require_source=lambda *a: (p['tool']['commit'], p['tool']['tree']),
            classify_release=lambda *a: risk, reviewed_release_state=reviewed,
            current_main_identity=lambda *a: p['tool'], verify_rollback_assets=lambda *a: asset,
            _docker_timestamp_nanoseconds=runtime._docker_timestamp_nanoseconds,
            _datetime_nanoseconds=runtime._datetime_nanoseconds))
    def load(source, relative, name):
        if relative.endswith('routine_release.py'): return routine
        if relative.endswith('pre_release_runtime.py'): return SimpleNamespace(classify_release=lambda *a: risk)
        return review_policy
    backend.load_application = load
    return SimpleNamespace(plan=p, backend=backend, record=record, policy=policy, objects=objects,
                           put=put, read=read, risk=risk, event=event)


def test_aged_fact_accepted_without_rewriting_old_bytes_and_current_routine_still_rejects(chain):
    original = copy.deepcopy(chain.objects)
    history.PrimaryRollbackContext(chain.backend, chain.plan).verify(chain.record, chain.policy, Path('/source'))
    assert chain.objects == original
    with pytest.raises(ValueError, match='INVALID_RISK_REVIEW_TIME'):
        review_policy.apply_review(chain.risk, chain.record['maintainer_risk_review'], chain.risk['target'])


@pytest.mark.parametrize('fault', ['expired_then', 'expired_one_nanosecond', 'tampered_bytes', 'unproven_event', 'wrong_instance',
    'wrong_policy', 'wrong_image', 'wrong_ci', 'time_reversed', 'unset_start', 'current_review_expired',
    'current_main_changed', 'asset_not_retained', 'recovery_policy', 'signed_family'])
def test_protected_history_never_bypasses_current_authority_or_bad_history(chain, fault):
    p, record, policy = chain.plan, chain.record, chain.policy
    request = chain.read(p['release_request'])
    release = chain.read(request['current']['release'])
    deployment = chain.read(release['accepted_deployment'])
    if fault == 'expired_then':
        record['maintainer_risk_review']['timestamp'] = (chain.event-timedelta(hours=25)).isoformat()
    if fault == 'expired_one_nanosecond':
        record['validated_at'] = chain.event.isoformat(timespec='microseconds').replace('+00:00', '001+00:00')
        record['maintainer_risk_review']['timestamp'] = (chain.event-timedelta(hours=24)).isoformat()
    if fault in ('expired_then', 'expired_one_nanosecond'):
        policy['candidate_record'] = chain.put('/protected/original.json', record)
        p['source_policy'] = chain.put(p['source_policy']['path'], policy)
    if fault == 'tampered_bytes': chain.objects[policy['candidate_record']['path']] += b' '
    if fault == 'unproven_event': del release['accepted_deployment']
    if fault == 'wrong_instance': deployment['instance']['container_id'] = '0' * 64
    if fault == 'wrong_policy': deployment['instance']['spec']['policy_output'] = '/other/policy.json'
    if fault == 'wrong_image': deployment['image_id'] = 'sha256:'+'0'*64
    if fault == 'wrong_ci': deployment['ci_run'] = 'other'
    if fault == 'time_reversed': deployment['deployed_at'] = (chain.event-timedelta(seconds=1)).isoformat()
    if fault == 'unset_start': deployment['instance']['started_at'] = '0001-01-01T00:00:00Z'
    if fault == 'current_review_expired': request['maintainer_risk_review']['timestamp'] = (chain.event).isoformat()
    if fault == 'current_main_changed': request['maintainer_risk_review']['authoritative_main']['commit'] = '0'*40
    if fault == 'asset_not_retained': chain.backend.pre.verify_rollback_assets = lambda *a: {**p['source'], 'image_id': 'sha256:'+'0'*64}
    if fault == 'recovery_policy': policy['recovery'] = {}
    if fault == 'signed_family': record['schema_version'] = 'candidate-validation-record/1'
    if 'accepted_deployment' in release:
        release['accepted_deployment'] = chain.put('/protected/deployment.json', deployment)
    request['current']['release'] = chain.put('/protected/release.json', release)
    p['release_request'] = chain.put('/protected/request.json', request)
    with pytest.raises(ValueError):
        history.PrimaryRollbackContext(chain.backend, p).verify(record, policy, Path('/source'))


def test_current_tool_adapter_threads_context_without_mutating_issuer_globals():
    calls = []
    def consumer(*args, **kwargs):
        calls.append(kwargs)
        return 'original-result'
    host = SimpleNamespace(**{name: consumer for name in ('_validated_candidate_record', '_render_actual_compose',
                                                        'revalidate_production', 'issue_execution_grant')})
    context = SimpleNamespace(backend=SimpleNamespace(host=host))
    adapter = history.issuer_for(context)
    assert adapter.issue_execution_grant('fresh', role='production') == 'original-result'
    assert calls == [{'role': 'production', 'primary_rollback_context': context}]
    assert host.issue_execution_grant is consumer


@pytest.fixture
def replacement(chain):
    p, b = chain.plan, chain.backend
    original_plan = copy.deepcopy(p)
    original_objects = copy.deepcopy(chain.objects)
    request = chain.read(p['release_request'])
    release = chain.read(request['current']['release'])
    prior = chain.put('/protected/first-sealed-plan.json', original_plan)
    b.verify_execution_plan = lambda reference: chain.read(reference)
    p['source_container_id'] = 'd' * 64
    issued = copy.deepcopy(chain.policy)
    policy_ref = chain.put('/protected/restored-issued-policy.json', issued)
    original_plan['instances']['primary_rollback'] = chain.put('/protected/first-spec.json',
        dict(transport=dict(policy_output=policy_ref['path'])))
    prior = chain.put(prior['path'], original_plan)
    now = datetime.now(timezone.utc)
    result = dict(result='FAIL', rollback='PASS', plan_sha256=prior['sha256'], instances=dict(primary_rollback=dict(
        container_id=p['source_container_id'], image_id=p['source']['image_id'], application_commit=p['source']['commit'],
        policy_path=policy_ref['path'], grant_id='actual-new-grant')),
        timeline=[dict(step=s, status='PASS') for s in ('preconditions', 'recovery_evidence_before_stop', 'rollback_assets',
            'record_window_before_stop', 'rollback_create', 'rollback_authorize', 'rollback_start')])
    result_ref = chain.put('/protected/first-failed-result.json', result)
    obs = dict(schema_version='production-recovery-observation/1', result='PASS', method='fresh-recovery',
        observed_at=now.isoformat(), base={k:p['source'][k] for k in ('commit','tree')},
        target={k:p['target'][k] for k in ('commit','tree')}, old_image_id=p['source']['image_id'],
        evidence=dict(instance=chain.put('/protected/current-instance.json', dict(container_id=p['source_container_id'])),
            grant=chain.put('/protected/current-grant.json', dict(payload=dict(grant_id='actual-new-grant')))))
    observation_ref = chain.put('/protected/post-rollback-observation.json', obs)
    association = dict(execution_plan=prior, execution_result=result_ref, observation=observation_ref, policy=policy_ref)
    release['current_deployment'] = chain.put('/protected/current-deployment.json', association)
    request['current']['release'] = chain.put('/protected/new-retained-release.json', release)
    p['release_request'] = chain.put('/protected/second-request.json', request)
    p['source_policy'] = policy_ref
    b.pre._risk_fields = runtime._risk_fields
    b.pre.verify_recovery_observation = lambda *a: None  # Real verifier/crypto covered by owner Docker lane.
    observed_calls = []
    b.verify_current_instance = lambda *a: observed_calls.append(a)
    return SimpleNamespace(chain=chain, original_objects=original_objects, association=association, release=release,
        request=request, prior=original_plan, result=result, obs=obs, observed_calls=observed_calls)


def test_proven_fresh_rollback_associates_B_without_rewriting_A(replacement):
    x, c = replacement, replacement.chain
    before = copy.deepcopy(c.objects)
    history.PrimaryRollbackContext(c.backend, c.plan).verify(c.record, c.policy, Path('/source'))
    assert c.objects == before
    assert len(x.observed_calls) == 1 and x.observed_calls[0][0]['source_container_id'] == 'd'*64
    for name, raw in x.original_objects.items():
        assert c.objects[name] == raw


@pytest.mark.parametrize('fault', ['no_proof', 'extra_field', 'unsealed_plan', 'wrong_previous_asset',
    'unstarted', 'wrong_current_cid', 'wrong_current_policy', 'grant_reused', 'wrong_observed_image',
    'wrong_observed_target', 'wrong_history', 'changed_acceptance_ref', 'sandbox_as_deployment',
    'changed_current_config', 'timeout_without_independent_acceptance', 'timeout_rewritten_as_success',
    'wrong_execution_plan_hash'])
def test_unproven_same_image_replacement_and_bad_links_reject(replacement, fault):
    x, c = replacement, replacement.chain
    if fault == 'no_proof': del x.release['current_deployment']
    if fault == 'extra_field': x.association['current'] = True
    if fault == 'unsealed_plan':
        def rejected(*a): raise ValueError('manifest missing')
        c.backend.verify_execution_plan = rejected
    if fault == 'wrong_execution_plan_hash': x.result['plan_sha256'] = '0'*64
    if fault == 'wrong_previous_asset': x.prior['primary_rollback']['image_id'] = 'sha256:'+'0'*64
    if fault == 'unstarted': x.result['timeline'][-1]['status'] = 'FAIL'
    if fault == 'wrong_current_cid': x.result['instances']['primary_rollback']['container_id'] = '0'*64
    if fault == 'wrong_current_policy': x.result['instances']['primary_rollback']['policy_path'] = '/wrong'
    if fault == 'grant_reused': x.result['instances']['primary_rollback']['grant_id'] = 'old-A-grant'
    if fault == 'wrong_observed_image': x.obs['old_image_id'] = 'sha256:'+'0'*64
    if fault == 'wrong_observed_target': x.obs['target']['commit'] = '0'*40
    if fault == 'wrong_history':
        prior_request = c.read(x.prior['release_request'])
        old_release = c.read(prior_request['current']['release'])
        old_release['accepted_deployment'] = c.put('/protected/other-deployment.json', {})
        prior_request['current']['release'] = c.put('/protected/other-release.json', old_release)
        x.prior['release_request'] = c.put('/protected/other-request.json', prior_request)
    if fault == 'changed_acceptance_ref':
        old_policy = c.read(x.prior['source_policy'])
        old_policy['candidate_record'] = c.put('/protected/other-acceptance.json', {})
        x.prior['source_policy'] = c.put('/protected/wrong-original-policy.json', old_policy)
    if fault == 'sandbox_as_deployment': x.obs['recovery_policy'] = dict(path='/sandbox', sha256='0'*64)
    if fault == 'changed_current_config':
        def rejected(*a): raise ValueError('CURRENT_REPLACEMENT_RUNTIME_CHANGED')
        c.backend.verify_current_instance = rejected
    if fault == 'timeout_without_independent_acceptance':
        x.result['rollback'] = 'FAIL'
        x.result['timeline'].append(dict(step='rollback_accept', status='FAIL', finished_at=x.obs['observed_at']))
    if fault == 'timeout_rewritten_as_success': x.result['result'] = 'SUCCESS'
    x.association['execution_plan'] = c.put(x.association['execution_plan']['path'], x.prior)
    x.association['execution_result'] = c.put(x.association['execution_result']['path'], x.result)
    x.association['observation'] = c.put(x.association['observation']['path'], x.obs)
    if fault != 'no_proof': x.release['current_deployment'] = c.put('/protected/current-deployment.json', x.association)
    x.request['current']['release'] = c.put('/protected/new-retained-release.json', x.release)
    c.plan['release_request'] = c.put('/protected/second-request.json', x.request)
    with pytest.raises(ValueError):
        history.PrimaryRollbackContext(c.backend, c.plan).verify(c.record, c.policy, Path('/source'))


def test_post_incident_new_acceptance_is_not_timeout_rewrite(replacement):
    x, c = replacement, replacement.chain
    x.result['rollback'] = 'FAIL'
    x.result['timeline'].append(dict(step='rollback_accept', status='FAIL', finished_at=(
        datetime.fromisoformat(x.obs['observed_at'])-timedelta(seconds=1)).isoformat()))
    x.association['execution_result'] = c.put('/protected/original-timeout.json', x.result)
    timeout_bytes = c.objects['/protected/original-timeout.json']
    x.release['current_deployment'] = c.put('/protected/separate-current-acceptance.json', x.association)
    x.request['current']['release'] = c.put('/protected/new-retained-release.json', x.release)
    c.plan['release_request'] = c.put('/protected/second-request.json', x.request)
    history.PrimaryRollbackContext(c.backend, c.plan).verify(c.record, c.policy, Path('/source'))
    assert c.objects['/protected/original-timeout.json'] == timeout_bytes
    assert c.read(x.association['execution_result'])['rollback'] == 'FAIL'


def test_execution_receipt_distinguishes_tool_issuer_from_old_application(tmp_path):
    backend = execution.HostBackend.__new__(execution.HostBackend)
    backend.acceptance_timeline = []
    backend.output = tmp_path
    backend.engine = SimpleNamespace(_canonical=lambda value: json.dumps(value).encode(),
        _write_new=lambda path, raw: path.write_bytes(raw))
    backend.routine = SimpleNamespace(__file__='current/transport.py')
    backend.envelopes = {}
    backend.sessions = {'primary_rollback': SimpleNamespace(container_id='fresh-container',
        binding={'commit': 'old-application'}, current_policy={'image_id': 'exact-old-image'},
        spec={'policy_output': 'fresh-policy'},
        host=SimpleNamespace(__name__='current-issuer', __file__='current/host_authorization.py'),
        issuer_source_commit='current-tool')}
    result = {}
    backend.record(result)
    recorded = json.loads((tmp_path / 'execution-result.json').read_bytes())['instances']['primary_rollback']
    assert recorded['application_commit'] == 'old-application'
    assert recorded['issuer_source_commit'] == 'current-tool'


def test_hosted_clock_reaches_original_review_parser_but_not_grant_clock(chain):
    fixture = execution.load(execution.ROOT, '08_tests/shared/high_risk_execution_docker_e2e.py', '_test_uniform_review_clock')
    original = fixture.install_review_clock()
    try:
        fixture.REVIEW_CLOCK_ADVANCE = timedelta(hours=48)
        policy = execution.load(execution.ROOT, '04_scripts/runtime/release_reversibility.py', '_clocked_original_parser')
        now_review = routine_review_for(chain.risk)
        with pytest.raises(ValueError, match='INVALID_RISK_REVIEW_TIME'):
            policy.apply_review(chain.risk, now_review, chain.risk['target'])
        # Historical event-time validation still invokes the identical parser.
        policy.apply_review(chain.risk, now_review, chain.risk['target'],
                            now=datetime.fromisoformat(now_review['timestamp']))
        assert policy.datetime.now(timezone.utc) - datetime.now(timezone.utc) > timedelta(hours=47)
        # Exercise the actual trusted source-byte reader which bypasses
        # loader.exec_module (the original Hosted failure's exact call path).
        pre = execution.load(execution.ROOT, '04_scripts/runtime/pre_release_runtime.py', '_clocked_pre')
        trusted_policy = pre._load('04_scripts/runtime/release_reversibility.py', '_clocked_trusted_parser')
        assert trusted_policy.datetime is fixture.ReviewClock
        current_review = copy.deepcopy(now_review)
        current_review['timestamp'] = fixture.ReviewClock.now(timezone.utc).isoformat()
        pre.apply_maintainer_review(chain.risk, current_review, chain.risk['target'])
        with pytest.raises(ValueError, match='INVALID_RISK_REVIEW_TIME'):
            pre.apply_maintainer_review(chain.risk, now_review, chain.risk['target'])
        grant = execution.load(execution.ROOT, '09_deploy/runtime_identity/host_authorization.py', '_unclocked_signer')
        assert grant.datetime is datetime
    finally:
        original()
