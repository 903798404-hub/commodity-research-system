"""Protected retained-deployment facts, not authorization to start an instance.

Only the HIGH_RISK host adapter constructs this consumer. Every use rereads the
current request/Review and the original hash-bound deployment chain. No policy,
grant or CLI field enables historical validation.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


class HistoricalContextNotProven(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise HistoricalContextNotProven(reason)


class PrimaryRollbackContext:
    def __init__(self, backend, plan):
        self.backend = backend
        self.plan = copy.deepcopy(plan)

    def verify_current(self, release, deployment, baseline, asset):
        """Separate historical A from a witnessed, freshly authorized B.

        A retained release may add a hash-bound reference to existing sealed
        execution and recovery observations. No historical record, signature,
        expiry policy or authorization is rewritten by this association.
        """
        b, p = self.backend, self.plan
        original = deployment['instance']
        if (original.get('container_id') == p['source_container_id'] and
                original.get('spec', {}).get('policy_output') == p['source_policy']['path']):
            require('current_deployment' not in release, 'UNNEEDED_REPLACEMENT_REFERENCE')
            return
        require(type(release.get('current_deployment')) is dict, 'HISTORICAL_DEPLOYED_INSTANCE_BINDING')
        association = b.read(release['current_deployment'])
        b.pre._risk_fields(association, ('execution_plan', 'execution_result', 'observation', 'policy'),
                           'current retained deployment')
        previous = b.verify_execution_plan(association['execution_plan'])
        result = b.read(association['execution_result'])
        require(result.get('plan_sha256') == association['execution_plan']['sha256'],
                'REPLACEMENT_EXECUTION_PLAN_BINDING')
        require(previous['primary_rollback'] == asset and previous['source'] == asset and
                previous['source_container_id'] != p['source_container_id'], 'REPLACEMENT_PLAN_ASSET_BINDING')
        # The protected original executor must have reached a real fresh rollback.
        # Its failure/timeout is not changed to success by subsequent acceptance.
        required = ('preconditions', 'recovery_evidence_before_stop', 'rollback_assets',
                    'record_window_before_stop', 'rollback_create', 'rollback_authorize', 'rollback_start')
        timeline = {step['step']: step for step in result.get('timeline', [])}
        require(all(timeline.get(name, {}).get('status') == 'PASS' for name in required) and
                result.get('result') == 'FAIL' and result.get('rollback') in ('PASS', 'FAIL'),
                'REPLACEMENT_EXECUTION_NOT_PROVEN')
        previous_request = b.read(previous['release_request'])
        previous_release = b.read(previous_request['current']['release'])
        require(previous_release.get('accepted_deployment') == release['accepted_deployment'],
                'REPLACEMENT_HISTORICAL_ASSET_CHANGED')
        instance = result.get('instances', {}).get('primary_rollback', {})
        require(instance.get('container_id') == p['source_container_id'] and
                instance.get('application_commit') == asset['commit'] and instance.get('image_id') == asset['image_id'] and
                instance.get('policy_path') == association['policy']['path'], 'REPLACEMENT_EXECUTOR_INSTANCE_BINDING')
        observed = b.read(association['observation'])
        b.pre.verify_recovery_observation(observed, b.host)
        require(observed.get('schema_version') == 'production-recovery-observation/1' and
                observed.get('result') == 'PASS' and observed.get('method') == 'fresh-recovery' and
                'recovery_policy' not in observed and observed['base'] ==
                {'commit': asset['commit'], 'tree': asset['tree']} and
                observed['target'] == {'commit': previous['target']['commit'], 'tree': previous['target']['tree']} and
                observed['old_image_id'] == asset['image_id'], 'REPLACEMENT_ACCEPTANCE_ASSET_BINDING')
        witnessed = b.read(observed['evidence']['instance'])
        issued_policy = b.read(association['policy'])
        envelope = b.read(observed['evidence']['grant'])
        grant = envelope['payload']
        previous_spec = b.read(previous['instances']['primary_rollback'])
        require(previous_spec['transport']['policy_output'] == association['policy']['path'] and
                issued_policy == baseline and grant['grant_id'] == instance.get('grant_id') and
                witnessed['container_id'] == p['source_container_id'], 'REPLACEMENT_POLICY_OR_GRANT_CHANGED')
        require(issued_policy['candidate_record'] == b.read(previous['source_policy'])['candidate_record'],
                'REPLACEMENT_ACCEPTANCE_REFERENCE_CHANGED')
        if result['rollback'] == 'FAIL':
            failed = timeline.get('rollback_accept', {})
            require(failed.get('status') == 'FAIL' and b.pre._docker_timestamp_nanoseconds(
                observed['observed_at'], 'supplemental acceptance') > b.pre._docker_timestamp_nanoseconds(
                failed['finished_at'], 'original rollback failure'), 'INDEPENDENT_POST_INCIDENT_ACCEPTANCE_REQUIRED')
        # No old container has to remain live; B itself must be observed now.
        b.verify_current_instance(p, baseline, witnessed, envelope)
        require(all(b.read(association[k]) == value for k, value in (
            ('execution_result', result), ('observation', observed), ('policy', issued_policy))) and
            b.read(release['current_deployment']) == association, 'REPLACEMENT_PROVENANCE_CHANGED')

    def verify(self, record, policy, source):
        b, p = self.backend, self.plan
        c = b.contract_module
        c.validate_against_schema(p, c.load_schema(Path(c.__file__).with_name('deployment_plan.schema.json')))
        b.validate_plan_intent(p)
        require(b.pre.require_source(b.host, b.engine) == (p['tool']['commit'], p['tool']['tree']),
                'HISTORICAL_CONTEXT_TOOL_CHANGED')
        request = b.read(p['release_request'])
        asset = p['primary_rollback']
        require(request['project_id'] == p['project_id'] == policy['project_id'] and
                request['target_commit'] == p['target']['commit'] and
                all(request['current'][k] == asset[k] for k in ('commit', 'tree', 'image_id')),
                'HISTORICAL_CONTEXT_NOT_CURRENT_PLANNED_ASSET')
        risk = b.pre.classify_release(b.tool_root, request['current']['commit'],
                                      request['target_commit'], p['project_id'])
        risk, state, main = b.pre.reviewed_release_state(request, risk, b.host)
        require(main is not None and risk.get('FINAL_RELEASE_TREATMENT') == 'HIGH_RISK' and
                state['compatible'] is True, 'CURRENT_HIGH_RISK_REVIEW_REQUIRED')
        require(b.pre.current_main_identity(b.tool_root) == main, 'CURRENT_MAIN_CHANGED_DURING_REVIEW')
        retained = b.pre.verify_rollback_assets(b.tool_root, request['current'], b.host)
        require(all(retained[k] == asset[k] for k in ('commit', 'tree', 'image_id')),
                'HISTORICAL_RETAINED_ASSET_CHANGED')
        baseline = b.read(p['source_policy'])
        b.host.validate_policy(baseline, 'production')
        require('recovery' not in baseline and 'recovery' not in policy and
                policy['role'] == baseline['role'] == 'production' and
                all(policy[k] == baseline[k] == asset[a] for k, a in (
                    ('approved_commit', 'commit'), ('approved_tree', 'tree'), ('image_id', 'image_id'))) and
                policy['candidate_record'] == baseline['candidate_record'],
                'HISTORICAL_SOURCE_POLICY_BINDING')
        original = b.read(baseline['candidate_record'])
        require(record == original and record.get('schema_version') == 'routine-candidate-acceptance/1',
                'HISTORICAL_ROUTINE_FACT_REQUIRED')
        release = b.read(request['current']['release'])
        require(type(release.get('accepted_deployment')) is dict, 'HISTORICAL_CONTEXT_NOT_PROVEN')
        deployment = b.read(release['accepted_deployment'])
        require(deployment.get('schema_version') == 'routine-deployment-result/1' and
                deployment.get('result') == 'PASS' and deployment.get('candidate_acceptance') == 'PASS' and
                all(deployment.get(k) == 'PASS' for k in ('runtime_preflight', 'production_health', 'production_smoke')),
                'HISTORICAL_DEPLOYMENT_NOT_ACCEPTED')
        if deployment.get('ui_acceptance_mode') == 'MANUAL':
            require(deployment.get('stage') == 'production' and deployment.get('http') == 'PASS' and
                    deployment.get('manual_ui', {}).get('decision') == 'PASS' and
                    bool(deployment['manual_ui'].get('operator')), 'HISTORICAL_MANUAL_ACCEPTANCE_MISSING')
        require(all(record.get(k) == deployment.get(k) == asset[k] for k in ('commit', 'tree', 'image_id')) and
                record.get('release_id') == deployment.get('release_id') == release['release_id'] and
                record.get('ci_run') == deployment.get('ci_run') and
                deployment.get('previous_release', {}).get('commit') == record.get('base_commit'),
                'HISTORICAL_DEPLOYMENT_ACCEPTANCE_BINDING')
        instance = deployment.get('instance', {})
        # Historical container/policy binding remains the original deployment's
        # own fact. A different current instance needs a separate formal chain.
        require(type(instance.get('container_id')) is str and re.fullmatch('[0-9a-f]{64}', instance['container_id']) and
                isinstance(instance.get('spec', {}).get('policy_output'), str), 'HISTORICAL_DEPLOYED_INSTANCE_BINDING')
        self.verify_current(release, deployment, baseline, asset)
        routine = b.load_application(source, '04_scripts/runtime/routine_release.py', '_historical_routine_fact')
        routine.validate_acceptance(record, commit=asset['commit'], tree=asset['tree'], image_id=asset['image_id'])
        event = b.pre._docker_timestamp_nanoseconds(record['validated_at'], 'historical acceptance')
        deployed = b.pre._docker_timestamp_nanoseconds(deployment['deployed_at'], 'historical deployment')
        started = b.pre._docker_timestamp_nanoseconds(instance['started_at'], 'historical start')
        finished = b.pre._docker_timestamp_nanoseconds(deployment.get('finalized_at', deployment['deployed_at']),
                                                       'historical completion')
        require(event <= deployed <= started <= finished <= b.pre._datetime_nanoseconds(datetime.now(timezone.utc)),
                'HISTORICAL_EVENT_ORDER_NOT_PROVEN')
        original_pre = b.load_application(source, '04_scripts/runtime/pre_release_runtime.py', '_historical_risk')
        original_risk = original_pre.classify_release(source, record['base_commit'], record['commit'], policy['project_id'])
        review_policy = b.load_application(source, '04_scripts/runtime/release_reversibility.py', '_historical_review')
        review = record.get('maintainer_risk_review')
        if review is not None:
            reviewed = b.pre._docker_timestamp_nanoseconds(review['timestamp'], 'historical Review')
            require(event - 24 * 3600 * 1_000_000_000 <= reviewed <= event,
                    'INVALID_RISK_REVIEW_TIME')
        # Historical main is corroborated by the accepted deployment's exact
        # target, not by a caller-provided historical=true or an arbitrary now.
        resolved = review_policy.apply_review(original_risk, review,
            {'commit': asset['commit'], 'tree': asset['tree']},
            now=datetime.fromisoformat(record['validated_at'].replace('Z', '+00:00')))
        require(resolved.get('FINAL_RELEASE_TREATMENT') == 'ROUTINE_STATELESS' and
                resolved.get('ROUTINE_RELEASE_ELIGIBLE') is True, 'HISTORICAL_REVIEW_NOT_ROUTINE')
        # TOCTOU: all protected references remain the identical original facts.
        require(b.read(baseline['candidate_record']) == original and
                b.read(release['accepted_deployment']) == deployment and b.read(p['release_request']) == request and
                b.read(p['source_policy']) == baseline, 'HISTORICAL_PROVENANCE_CHANGED')


def issuer_for(context):
    """Explicit current-tool issuer transport; never mutate module globals.

    Source/manifest/image and fresh grant validation remain in the existing
    signer. Only acceptance-consuming calls carry this protected plan context.
    """
    host = context.backend.host
    adapter = SimpleNamespace(**vars(host))
    for name in ('_validated_candidate_record', '_render_actual_compose',
                 'revalidate_production', 'issue_execution_grant'):
        original = getattr(host, name)
        def call(*args, _original=original, **kwargs):
            return _original(*args, primary_rollback_context=context, **kwargs)
        setattr(adapter, name, call)
    return adapter
