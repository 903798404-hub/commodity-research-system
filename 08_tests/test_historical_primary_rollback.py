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
        grant = execution.load(execution.ROOT, '09_deploy/runtime_identity/host_authorization.py', '_unclocked_signer')
        assert grant.datetime is datetime
    finally:
        fixture.importlib.util.spec_from_file_location = original
