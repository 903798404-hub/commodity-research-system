"""Protected retained-deployment facts, not authorization to start an instance.

Only the HIGH_RISK host adapter constructs this consumer. Every use rereads the
current request/Review and the original hash-bound deployment chain. No policy,
grant or CLI field enables historical validation.
"""
from __future__ import annotations

import copy
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
        require(instance.get('container_id') == p['source_container_id'] and
                instance.get('spec', {}).get('policy_output') == p['source_policy']['path'],
                'HISTORICAL_DEPLOYED_INSTANCE_BINDING')
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
