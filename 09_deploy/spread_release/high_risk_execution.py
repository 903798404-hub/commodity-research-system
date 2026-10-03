"""Bounded HIGH_RISK deployment adapter, not an authorization protocol.

The existing protected plan/manifest, assessment, application-bound issuer and
DockerSession remain authoritative. Nothing accepts a caller's PASS override.
"""
from __future__ import annotations

import argparse
import base64
import copy
import contextlib
import hashlib
import importlib.util
import inspect
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
VERSION = '1.8.0'


def load(root, relative, name):
    spec = importlib.util.spec_from_file_location(name, root / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def contract():
    # The legacy release module uses its sibling readiness import.
    if 'wait_for_service_ready' not in sys.modules:
        load(ROOT, '09_deploy/spread_release/wait_for_service_ready.py', 'wait_for_service_ready')
    return load(ROOT, '09_deploy/spread_release/release_contract.py', '_high_risk_plan_contract')


class ExecutionError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ExecutionError(message)


def validate_issuer_api(issuer, *, application_service=False):
    """Check the existing app-bound ABI before stop; never substitute a signer.

    _observe is deliberately not required: pure Compose interpretation is owned
    by the current transport. These are the actual issuer calls of this path.
    """
    calls = {
        'require_protected_authority_source': ((), {}),
        '_protected_path': ((Path('/unused'),), {'private': True}),
        '_json': ((b'{}',), {}), '_digest': (({},), {}),
        '_load_private_key': (('/unused',), {}), 'validate_policy': (({}, 'production'), {}),
        '_validated_candidate_record': (({},), {}),
        '_production_compose_bridge': (({}, {}, {}), {}),
        '_validate_runtime_mounts': (({}, [], {}), {}), '_mounts': (({},), {}),
        'copy_container_json': (('unused',), {}), 'normalize_observation': (({}, {}, {}), {}),
        '_render_actual_compose': (({}, {}, {}), {}),
        'revalidate_production': (('unused',), {'expected_policy_path': Path('/unused')}),
        'issue_execution_grant': (('unused',), dict(expected_policy_path=Path('/unused'),
            key_path='/unused', grant_path='/unused', grant_dir='/unused', role='production', ttl_seconds=900)),
    }
    if application_service:
        calls['issue_application_service_credential'] = (('unused',), dict(
            expected_policy_path=Path('/unused'), credential_path='/unused', role='production'))
    for name, (args, kwargs) in calls.items():
        method = vars(issuer).get(name)
        require(callable(method), 'UNSUPPORTED_APPLICATION_ISSUER_API: ' + name)
        try:
            inspect.signature(method).bind(*args, **kwargs)
        except (TypeError, ValueError) as exc:
            raise ExecutionError('UNSUPPORTED_APPLICATION_ISSUER_API: ' + name) from exc


def verify_plan(path):
    c = contract()
    try:
        plan, _ = c.load_verified_json_artifact(path, artifact_type='deployment_plan')
        c.validate_against_schema(plan, c.load_schema(Path(__file__).with_name('deployment_plan.schema.json')))
    except c.ContractError as exc:
        raise ExecutionError(str(exc)) from exc
    validate_intent(plan, c)
    return plan


def validate_intent(plan, c):
    require(plan['schema_version'] == VERSION, 'HIGH_RISK_REQUIRES_PLAN_1_8')
    require(plan['formal_containers'] == {}, 'FUTURE_INSTANCE_ID_FORBIDDEN')
    require(plan['git_commit'] == plan['target']['commit'] and plan['git_tree'] == plan['target']['tree']
            and plan['candidate_image_id'] == plan['target']['image_id'], 'PLAN_TARGET_BINDING')
    require(plan['source'] == plan['primary_rollback'], 'PRIMARY_ROLLBACK_MUST_BE_CURRENT_SOURCE')
    c.validate_readiness_policy(plan['policy']['readiness'])
    require(plan['policy']['rollback_timeout_seconds'] >= plan['policy']['readiness']['total_timeout_seconds'],
            'ROLLBACK_TIMEOUT_TOO_SHORT')
    require(plan['policy']['observation_seconds'] >= plan['policy']['poll_interval_seconds']
            * plan['policy']['consecutive_failures'], 'OBSERVATION_TOO_SHORT')


def seal_plan(plan, path):
    """Existing deployment_plan producer seals intent, never an execution grant."""
    c = contract()
    try:
        c.validate_against_schema(plan, c.load_schema(Path(__file__).with_name('deployment_plan.schema.json')))
    except c.ContractError as exc:
        raise ExecutionError(str(exc)) from exc
    validate_intent(plan, c)
    c.write_deployment_plan(plan, path)
    verify_plan(path)


def record_consumption_windows(plan):
    """Bound only phases preceding each record's last authorization consumer.

    Startup covers create/authorize/start as one bounded operation. Acceptance
    covers the complete readonly acceptance, not only its HTTP polling loop.
    Rollback can be needed after observation, so its record covers that path.
    No candidate record is needed after its instance authorization completes.
    """
    p = plan['policy']
    bootstrap = p['start_timeout_seconds']
    acceptance = p['readiness']['total_timeout_seconds']
    observation = p['observation_seconds'] + p['readiness']['request_timeout_seconds'] + p['poll_interval_seconds']
    target = p['stop_timeout_seconds'] + 10 + bootstrap
    return dict(target=target,
                primary_rollback=target + acceptance + observation + p['rollback_timeout_seconds'])


def prepare_plan(plan, path, backend, *, candidate_key, revalidate=False):
    """Existing HIGH_RISK preparation, explicitly never the execution entry.

    Revalidation is bounded once per role. Assessment and every live recovery
    dependency consume the refreshed references before the plan is sealed.
    No cleanup here: the executor still needs those live dependencies later.
    """
    require(path.name == 'deployment_plan.json', 'FORMAL_PLAN_FILENAME_REQUIRED')
    require(not path.exists() and not path.with_name('deployment_plan.manifest.json').exists(),
            'NEW_PREPARATION_OUTPUT_REQUIRED')
    with backend.lock(plan):
        updated = backend.refresh_plan_inputs(plan, candidate_key=candidate_key,
            remaining_seconds=record_consumption_windows(plan), revalidate=revalidate)
        backend.preconditions(updated)
        backend.consume_recovery(updated)
        backend.rollback_assets(updated)
        backend.before_stop(updated)
        seal_plan(updated, path)
        require(verify_plan(path) == updated, 'PREPARED_PLAN_CHANGED')
    return updated


class EvidenceLease:
    """Do not destroy live verifier dependencies before the last consumer.

    No signed record is rewritten and no offline PASS replaces a live verifier.
    An interrupted execution intentionally retains resources for operator review.
    """
    def __init__(self):
        self.consumed = False
        self.terminal = False

    def consume(self, consumer):
        consumer()
        self.consumed = True

    def release(self, cleanup):
        require(self.consumed and self.terminal, 'EVIDENCE_STILL_IN_USE')
        cleanup()


def execute_verified_plan(path, backend, *, monotonic=time.monotonic, sleep=time.sleep):
    """The one execution entry for real operations and Hosted Docker exercises.

    Backend is an internal transport seam, never imported from a CLI argument.
    All pre-stop validation completes under the same nonblocking release lock.
    """
    plan = verify_plan(path)
    lease = EvidenceLease()
    result = dict(result='FAIL', target='NOT_EXECUTED', rollback='NOT_EXECUTED',
                  plan_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), steps=[])
    stopped = False
    def step(name, call):
        result['steps'].append(name)
        return call()
    with backend.lock(plan):
        try:
            step('preconditions', lambda: backend.preconditions(plan))
            lease.consume(lambda: step('recovery_evidence_before_stop', lambda: backend.consume_recovery(plan)))
            step('rollback_assets', lambda: backend.rollback_assets(plan))
            # Detect any sealed input drift immediately before a mutating action.
            require(verify_plan(path) == plan, 'PLAN_CHANGED_BEFORE_STOP')
            step('record_window_before_stop', lambda: backend.before_stop(plan))
            stopped = True  # Stop can fail after stopping; recover conservatively.
            step('stop_source', lambda: backend.stop_source(plan))
            with backend.phase_window(plan['policy']['start_timeout_seconds'], 'TARGET_STARTUP_TIMEOUT'):
                step('target_create', lambda: backend.create(plan, 'target'))
                step('target_authorize', lambda: backend.authorize(plan, 'target'))
                step('target_start', lambda: backend.start(plan, 'target'))
            with backend.phase_window(plan['policy']['readiness']['total_timeout_seconds'], 'TARGET_ACCEPTANCE_TIMEOUT'):
                step('target_accept', lambda: backend.accept(plan, 'target'))
            policy = plan['policy']
            deadline = monotonic() + policy['observation_seconds']
            failures = 0
            observation_window = (policy['observation_seconds'] + policy['readiness']['request_timeout_seconds']
                                  + policy['poll_interval_seconds'])
            with backend.phase_window(observation_window, 'TARGET_OBSERVATION_TIMEOUT'):
                while True:
                    verdict = backend.observe(plan)
                    require(verdict in {'PASS', 'TRANSIENT', 'FATAL'}, 'UNKNOWN_OBSERVATION_RESULT')
                    require(verdict != 'FATAL', 'FATAL_OBSERVATION')
                    failures = failures + 1 if verdict == 'TRANSIENT' else 0
                    require(failures < policy['consecutive_failures'], 'OBSERVATION_FAILURE_THRESHOLD')
                    # An observation ending on a transient failure is not success.
                    if monotonic() >= deadline:
                        require(verdict == 'PASS', 'OBSERVATION_ENDED_UNHEALTHY')
                        break
                    sleep(min(policy['poll_interval_seconds'], max(0, deadline - monotonic())))
            result.update(result='SUCCESS', target='PASS')
            lease.terminal = True
        except Exception as exc:
            # Never report successful target deployment after a rollback.
            result['failure'] = type(exc).__name__ + ': ' + str(exc)
            if any(name.startswith('target_') for name in result['steps']):
                result['target'] = 'FAIL'
            if stopped:
                rollback_started = monotonic()
                try:
                    with backend.rollback_window(plan['policy']['rollback_timeout_seconds']):
                        step('stop_failed_target', lambda: backend.stop_target(plan))
                        step('rollback_create', lambda: backend.create(plan, 'primary_rollback'))
                        step('rollback_authorize', lambda: backend.authorize(plan, 'primary_rollback'))
                        step('rollback_start', lambda: backend.start(plan, 'primary_rollback'))
                        step('rollback_accept', lambda: backend.accept(plan, 'primary_rollback'))
                    require(monotonic() - rollback_started <= plan['policy']['rollback_timeout_seconds'],
                            'ROLLBACK_TIMEOUT')
                    result['rollback'] = 'PASS'
                    lease.terminal = True
                except Exception as recovery_error:
                    result['rollback'] = 'FAIL'
                    result['rollback_failure'] = type(recovery_error).__name__ + ': ' + str(recovery_error)
            else:
                result['source'] = 'PRESERVED_NOT_STOPPED'
                lease.terminal = lease.consumed
        finally:
            if lease.terminal:
                try:
                    lease.release(lambda: backend.cleanup_temporary(plan))
                    result['temporary_cleanup'] = 'PASS'
                except Exception as cleanup_error:
                    result.update(result='FAIL', temporary_cleanup='FAIL',
                                  cleanup_failure=type(cleanup_error).__name__ + ': ' + str(cleanup_error))
            else:
                result['resources'] = 'RETAINED_FOR_RECOVERY'
            backend.record(result)
    return result


class HostBackend:
    """Root-only adapter reusing existing app issuer and Compose create transport."""
    def __init__(self, output):
        self.output = output
        self.host = load(ROOT, '09_deploy/runtime_identity/host_authorization.py', '_high_risk_host')
        self.engine = load(ROOT, '04_scripts/runtime/validate_target_runtime.py', '_high_risk_engine')
        self.pre = load(ROOT, '04_scripts/runtime/pre_release_runtime.py', '_high_risk_pre')
        self.routine = load(ROOT, '04_scripts/runtime/routine_release.py', '_high_risk_transport')
        self.contract_module = contract()
        self.tool_root = ROOT
        self.load_application = load
        self.validate_plan_intent = lambda plan: validate_intent(plan, self.contract_module)
        self.sessions = {}
        self.envelopes = {}
        self.cleanup_scope = None

    def read(self, ref):
        raw = self.host._protected_path(Path(ref['path']), private=True).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == ref['sha256'], 'REFERENCED_INPUT_CHANGED')
        return self.host._json(raw)

    def historical_consumer(self, plan, role, policy):
        if (role == 'primary_rollback' and plan is not None and plan.get('schema_version') == VERSION and
                self.read(policy['candidate_record']).get('schema_version') == 'routine-candidate-acceptance/1'):
            module = load(ROOT, '04_scripts/runtime/historical_primary_rollback.py', '_primary_rollback_consumer')
            return module.issuer_for(module.PrimaryRollbackContext(self, plan))
        return None

    def resolve_acceptance(self, policy, role, asset, *, allow_expired=False, plan=None):
        """Verify the original protected reference by role and record family.

        This is a read-only projection, not a replacement record or grant.
        Only preparation may inspect an authenticated expired signed payload;
        the existing refresh producer still owns any actual revalidation.
        """
        require(role in {'target', 'primary_rollback'}, 'UNKNOWN_ACCEPTANCE_ROLE')
        self.host.validate_policy(policy, 'production')
        require('recovery' not in policy, 'REHEARSAL_POLICY_CANNOT_DEPLOY_OR_RESTORE')
        require((policy['approved_commit'], policy['approved_tree'], policy['image_id']) ==
                (asset['commit'], asset['tree'], asset['image_id']), 'INSTANCE_POLICY_IDENTITY')
        ref = policy['candidate_record']
        raw = self.pre._risk_file(ref, self.host)
        document = self.host._json(raw)
        require(type(document) is dict, 'ACCEPTANCE_OBJECT_REQUIRED')
        family = document.get('schema_version')
        require(family in {'candidate-validation-record/1', 'routine-candidate-acceptance/1'},
                'UNSUPPORTED_ACCEPTANCE_FAMILY')
        if family == 'routine-candidate-acceptance/1':
            require(role == 'primary_rollback', 'SIGNED_TARGET_REQUIRED')
            require(not ({'payload', 'signature', 'algorithm', 'key_id'} & document.keys()),
                    'CONFLICTING_ACCEPTANCE_FAMILY')
            # Reuse the existing recovery-first host dispatcher. Recovery is
            # explicitly forbidden above; it must take its Routine branch,
            # including protected source, risk, identity and manifest checks.
            consumer = self.historical_consumer(plan, role, policy) or self.host
            verified, _ = consumer._validated_candidate_record(policy)
            require(verified == document, 'ACCEPTANCE_CHANGED_DURING_VALIDATION')
            result = dict(family=family, reference=ref, signature_verified=False,
                          trust_source='protected-file', expires_at=None)
        else:
            parser = self.pre._load(self.pre.RECORD, '_plan_acceptance_record')
            trust = self.host._json((ROOT / self.pre.TRUST).read_bytes())
            try:
                payload = parser.verify_record(raw, trust)
            except parser.CandidateValidationRecordExpired as exc:
                if not allow_expired:
                    raise
                payload = exc.payload
            source = self.host._protected_path(Path(policy['approved_source_root']), directory=True)
            require(self.pre.require_source(self.host, self.engine, source_root=source) ==
                    (asset['commit'], asset['tree']), 'APPLICATION_SOURCE_CHANGED')
            project = self.engine._project(source, policy['project_id'])
            _, _, binding = self.engine.source_contract(source, policy['project_id'], project['runtime_contract'])
            require(payload['evidence']['binding'] == binding and
                    payload['evidence']['image_id'] == asset['image_id'], 'ACCEPTANCE_FIXED_ASSET_MISMATCH')
            result = dict(family=family, reference=ref, signature_verified=True,
                          trust_source='candidate-validation-signature',
                          expires_at=parser._time(payload['expires_at'], 'record expiry time'))
        require(self.pre._risk_file(ref, self.host) == raw, 'ACCEPTANCE_CHANGED_DURING_VALIDATION')
        return result

    def require_historical_reference(self, plan, acceptance):
        """Legacy acceptance must be the current source policy's exact fact."""
        if acceptance['family'] == 'routine-candidate-acceptance/1':
            baseline = self.read(plan['source_policy'])
            self.host.validate_policy(baseline, 'production')
            asset = plan['source']
            require('recovery' not in baseline and
                    (baseline['approved_commit'], baseline['approved_tree'], baseline['image_id']) ==
                    (asset['commit'], asset['tree'], asset['image_id']), 'CURRENT_POLICY_IDENTITY_CHANGED')
            require(baseline['candidate_record'] == acceptance['reference'],
                    'HISTORICAL_ACCEPTANCE_REFERENCE_CHANGED')

    def refresh_plan_inputs(self, plan, *, candidate_key, remaining_seconds, revalidate=False):
        """Preparation only: produce new immutable references, never a grant.

        The caller owns the release lock and time budget. Final assessment and
        plan sealing still follow this operation; no active pointer is changed.
        A failure leaves diagnostic artifacts only, not a deployable plan.
        """
        c = contract()
        schema = c.load_schema(Path(__file__).with_name('deployment_plan.schema.json'))
        c.validate_against_schema(plan, schema)
        validate_intent(plan, c)
        require(self.pre.require_source(self.host, self.engine) ==
                (plan['tool']['commit'], plan['tool']['tree']), 'TOOL_IDENTITY_CHANGED')
        require(set(remaining_seconds) == {'target', 'primary_rollback'}, 'RECORD_BUDGET_ROLES')
        request = self.read(plan['release_request'])
        require(request['project_id'] == plan['project_id'] and
                request['target_commit'] == plan['target']['commit'] and
                request['current']['commit'] == plan['source']['commit'], 'REFRESH_REQUEST_IDENTITY')
        risk = self.pre.classify_release(ROOT, request['current']['commit'],
                                         request['target_commit'], request['project_id'])
        # Exact existing Review parser/freshness/findings rules. Do this before
        # expensive probes; never generate or revise a human Review here.
        risk, _, _ = self.pre.reviewed_release_state(request, risk, self.host)
        require(risk.get('STATE_CHANGE_CLASS') != 'NEEDS_MAINTAINER_RISK_REVIEW',
                'HUMAN_RISK_REVIEW_REQUIRED_BEFORE_REVALIDATION')
        specs = {}
        policies = {}
        acceptances = {}
        for role in ('target', 'primary_rollback'):
            spec = self.read(plan['instances'][role])
            c.validate_against_schema(spec, schema['$defs']['highRiskInstanceSpec'], root_schema=schema)
            policy = self.read(spec['policy'])
            self.host.validate_policy(policy, 'production')
            asset = plan[role]
            require('recovery' not in policy and
                    (policy['approved_commit'], policy['approved_tree'], policy['image_id']) ==
                    (asset['commit'], asset['tree'], asset['image_id']), 'INSTANCE_POLICY_IDENTITY')
            require(policy['approved_source_root'] == spec['source_root'] and
                    spec['transport']['policy_template'] == spec['policy']['path'], 'POLICY_TEMPLATE_DIFFERS')
            if role == 'target':
                require(request['target_source_root'] == spec['source_root'] and
                        request['candidate_record'] == policy['candidate_record'], 'TARGET_RECORD_REFERENCE_DIFFERS')
            specs[role], policies[role] = spec, policy
            # Validate BOTH supplied inputs before either role can invoke an
            # expensive image validator. Expiry alone is handled later.
            acceptances[role] = self.resolve_acceptance(policy, role, asset, allow_expired=True, plan=plan)
            self.require_historical_reference(plan, acceptances[role])

        def save(name, value):
            path = self.output / name
            raw = self.engine._canonical(value)
            self.pre._write_new(self.host, path, raw)
            return dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())

        updated = copy.deepcopy(plan)
        changed_request = copy.deepcopy(request)
        outcomes = {}
        # Prepare rollback first, reserving the actual bounded engine time for
        # the subsequent target validation. Target is checked after that work,
        # not against a stale pre-validation clock. Each role still gets at
        # most one revalidation; final consumers recheck without retrying.
        for role in ('primary_rollback', 'target'):
            spec, policy = specs[role], policies[role]
            acceptance = acceptances[role]
            if acceptance['family'] == 'routine-candidate-acceptance/1':
                outcomes[role] = dict(reference=acceptance['reference'], action='REUSED',
                    family=acceptance['family'], trust_source=acceptance['trust_source'],
                    signature_verified=False, validation_attempts=0)
                continue
            budget = remaining_seconds[role]
            if role == 'primary_rollback':
                budget += self.pre.VALIDATION_TIMEOUT_SECONDS
            outcome = self.pre.ensure_candidate_record(plan['project_id'], policy['candidate_record'],
                application_source_root=Path(spec['source_root']), image_id=plan[role]['image_id'],
                destination=self.output / (role + '-candidate-record.json'), key_path=candidate_key,
                minimum_remaining_seconds=budget, revalidate=revalidate)
            outcomes[role] = outcome
            if outcome['reference'] != policy['candidate_record']:
                policy = copy.deepcopy(policy)
                policy['candidate_record'] = outcome['reference']
                self.host.validate_policy(policy, 'production')
                new_policy = save(role + '-policy-template.json', policy)
                spec = copy.deepcopy(spec)
                spec['policy'] = new_policy
                spec['transport']['policy_template'] = new_policy['path']
                c.validate_against_schema(spec, schema['$defs']['highRiskInstanceSpec'], root_schema=schema)
                updated['instances'][role] = save(role + '-instance-spec.json', spec)
                if role == 'target':
                    changed_request['candidate_record'] = outcome['reference']
        if changed_request != request:
            updated['release_request'] = save('refreshed-release-request.json', changed_request)
        save('candidate-refresh-result.json', outcomes)
        # Assessment, recovery and runtime checks must consume these exact new
        # references before the existing producer can seal the final plan.
        return updated

    def rollback_window(self, seconds):
        return self.phase_window(seconds, 'ROLLBACK_TIMEOUT')

    @contextlib.contextmanager
    def phase_window(self, seconds, failure):
        # Linux host tools run on the main thread. Bound the complete recovery,
        # including legacy transports whose own per-command limits are larger.
        import signal
        require(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0), 'NESTED_ROLLBACK_TIMER_FORBIDDEN')
        previous = signal.getsignal(signal.SIGALRM)
        def expired(signum, frame):
            raise ExecutionError(failure)
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def before_stop(self, plan):
        """Read-only recheck after any human wait; never refresh after stop."""
        expirations = []
        for role, seconds in record_consumption_windows(plan).items():
            spec = self.read(plan['instances'][role])
            policy = self.read(spec['policy'])
            acceptance = self.resolve_acceptance(policy, role, plan[role], plan=plan)
            self.require_historical_reference(plan, acceptance)
            if acceptance['expires_at'] is not None:
                expirations.append((role, seconds, acceptance['expires_at']))
        # Measure after all signature/file reads; their cost is not free time.
        now = datetime.now(timezone.utc)
        for role, seconds, expires_at in expirations:
            require(expires_at > now + timedelta(seconds=seconds),
                    'CANDIDATE_RECORD_WINDOW_INSUFFICIENT_REPREPARE_BEFORE_STOP: ' + role)

    def lock(self, plan):
        import contextlib
        import fcntl
        @contextlib.contextmanager
        def locked():
            self.host._require_linux_root()
            self.host.require_protected_authority_source()
            self.engine.require_builder()
            self.host._protected_path(Path(plan['lock_file']).parent, directory=True)
            descriptor = os.open(plan['lock_file'], os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(descriptor, 'a') as handle:
                self.host._protected_path(Path(plan['lock_file']), private=True)
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield
        return locked()

    def preconditions(self, plan):
        require(self.pre.require_source(self.host, self.engine) ==
                (plan['tool']['commit'], plan['tool']['tree']), 'TOOL_IDENTITY_CHANGED')
        self.host._protected_path(self.output, directory=True)
        # Execute the existing assessment, including independent Review parser,
        # current-main freshness, signed record and live recovery verification.
        self.read(plan['release_request'])
        report = self.pre.assess_release(Path(plan['release_request']['path']), self.output / 'pre-stop-assessment.json')
        require(report['candidate_image_id'] == plan['target']['image_id'], 'ASSESSMENT_TARGET_CHANGED')
        require(report['production_authorized'] is False, 'ASSESSMENT_IS_NOT_AUTHORIZATION')
        require(report['RELEASE_RISK_CLASS'] == 'STATEFUL_OR_INFRA', 'NOT_HIGH_RISK_PATH')
        require(report['PRODUCTION_RELEASE_PREFLIGHT'] == 'PASS', 'RELEASE_NOT_READY')
        current = self.host.docker_inspect(plan['source_container_id'])
        require(current['Image'] == plan['source']['image_id'] and current['State']['Running'], 'SOURCE_CHANGED')
        source_policy = self.read(plan['source_policy'])
        self.host.validate_policy(source_policy, 'production')
        require('recovery' not in source_policy and (source_policy['approved_commit'], source_policy['approved_tree'],
            source_policy['image_id']) == (plan['source']['commit'], plan['source']['tree'], plan['source']['image_id']),
            'CURRENT_POLICY_IDENTITY_CHANGED')
        current_release = self.host.copy_container_bytes(current['Id'], '/app/RELEASE.json')
        current_observed = self.host.normalize_observation(current,
            self.host.docker_image_inspect(current['Image']), self.host._json(current_release))
        require(current_observed['mounts'] == source_policy['mounts'] and
            hashlib.sha256(current_release).hexdigest() == source_policy['release_sha256'], 'CURRENT_RUNTIME_CHANGED')
        self.host.compare_observed_config(current_observed, source_policy['actual_config_sha256'])
        for role in ('target', 'primary_rollback'):
            self._prepare_session(plan, role)

    def _prepare_session(self, plan, role):
        asset = plan[role]
        spec = self.read(plan['instances'][role])
        schema = contract().load_schema(Path(__file__).with_name('deployment_plan.schema.json'))
        contract().validate_against_schema(spec, schema['$defs']['highRiskInstanceSpec'], root_schema=schema)
        source = self.host._protected_path(Path(spec['source_root']), directory=True)
        require(self.pre.require_source(self.host, self.engine, source_root=source) ==
                (asset['commit'], asset['tree']), 'APPLICATION_SOURCE_CHANGED')
        spec_policy = self.read(spec['policy'])
        historical = self.historical_consumer(plan, role, spec_policy)
        issuer = historical or load(source, '09_deploy/runtime_identity/host_authorization.py', '_high_risk_issuer_' + role)
        self.engine._write_new(self.output / ('issuer-loading-' + role + '.json'), self.engine._canonical(dict(
            tool_root=str(ROOT), issuer_kind='module', issuer_module=issuer.__name__,
            issuer_file=issuer.__file__, issuer_source_commit=plan['tool']['commit'] if historical else asset['commit'],
            issuer_source_tree=plan['tool']['tree'] if historical else asset['tree'],
            application_commit=asset['commit'], application_tree=asset['tree'],
            acceptance_consumption='PROTECTED_HISTORICAL_PRIMARY_ROLLBACK' if historical else 'CURRENT_APPLICATION_BOUND',
            transport_file=self.routine.__file__, observation_file=str(ROOT / '09_deploy/runtime_identity/runtime_observation.py'))))
        require(Path(issuer.__file__).resolve() == (ROOT if historical else source) / '09_deploy/runtime_identity/host_authorization.py',
                'APPLICATION_ISSUER_LOADING_SOURCE_CHANGED')
        validate_issuer_api(issuer)
        issuer.require_protected_authority_source()
        require(issuer.TRUST_CONFIG_PATH.read_bytes() == self.host.TRUST_CONFIG_PATH.read_bytes(), 'TRUST_DOMAIN_DIFFERS')
        project = self.engine._project(source, plan['project_id'])
        _, manifest, binding = self.engine.source_contract(source, plan['project_id'], project['runtime_contract'])
        policy = self.read(spec['policy'])
        validate_issuer_api(issuer, application_service='application_service' in policy)
        require('recovery' not in policy, 'REHEARSAL_POLICY_CANNOT_DEPLOY_OR_RESTORE')
        issuer.validate_policy(policy, 'production')
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        key = issuer._load_private_key(spec['transport']['key_path'])
        public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode('ascii')
        trust = issuer._json(issuer.TRUST_CONFIG_PATH.read_bytes())
        require(policy['key_id'] not in trust['revoked_key_ids'] and sum(
            k['key_id'] == policy['key_id'] and k['domain'] == 'production' and k['public_key_base64'] == public
            for k in trust['keys']) == 1, 'PRODUCTION_SIGNER_NOT_AVAILABLE')
        require((policy['approved_commit'], policy['approved_tree'], policy['image_id']) ==
                (asset['commit'], asset['tree'], asset['image_id']), 'INSTANCE_POLICY_IDENTITY')
        require(policy['approved_source_root'] == str(source), 'ISSUER_SOURCE_BINDING')
        self.resolve_acceptance(policy, role, asset, plan=plan)
        # This is the existing production acceptance consumer, not a new
        # Routine rollback-assets record bound to a HIGH_RISK forward delta.
        issuer._validated_candidate_record(policy)
        record = self.read(policy['candidate_record'])
        require(record.get('schema_version') != 'routine-rollback-assets/1', 'HIGH_RISK_ROUTINE_ROLLBACK_FORBIDDEN')
        self.routine.image_identity(self.engine, asset['image_id'], binding, manifest['service_id'])
        request = dict(production=spec['transport'], acceptance_record=policy['candidate_record']['path'],
                       application_smoke=spec['application_smoke'], empty_sessions=[])
        require(spec['transport']['policy_template'] == spec['policy']['path'], 'POLICY_TEMPLATE_DIFFERS')
        for ref in policy['compose_sources']:
            require(hashlib.sha256(issuer._protected_path(Path(ref['path'])).read_bytes()).hexdigest() == ref['sha256'],
                    'COMPOSE_SOURCE_CHANGED')
        require(policy['compose_environment_file'] == spec['transport']['environment'] and
                policy['compose_project_directory'] == spec['transport']['project_directory'], 'TRANSPORT_CONFIG_CHANGED')
        session = self.routine.DockerSession(request, self.engine, issuer, binding, manifest)
        session.role, session.spec = 'production', spec['transport']
        rendered = issuer._json(session.compose(session.spec, 'config', '--format', 'json').stdout)
        require(set(rendered['services']) == {manifest['service_id']}, 'NON_TARGET_SERVICE_FORBIDDEN')
        service = rendered['services'][manifest['service_id']]
        require(service['image'] == asset['image_id'] and 'build' not in service, 'IMMUTABLE_IMAGE_REQUIRED')
        require(issuer._digest(rendered) == policy['rendered_compose_sha256'], 'RENDERED_CONFIG_CHANGED')
        issuer._production_compose_bridge(rendered, policy, manifest)
        session._check_mounts(session.resolved_compose_mounts(rendered))
        require(not (Path(session.spec['grant_directory']) / 'grant.json').exists(), 'FRESH_GRANT_DIRECTORY_REQUIRED')
        require(not Path(session.spec['policy_output']).exists(), 'FRESH_POLICY_OUTPUT_REQUIRED')
        self.sessions[role] = session

    def consume_recovery(self, plan):
        request = self.read(plan['release_request'])
        require(request['recovery_evidence'] is not None, 'RECOVERY_EVIDENCE_MISSING')
        recovery = self.read(request['recovery_evidence'])
        self.pre.verify_recovery_observation(recovery, self.host)
        if 'recovery_policy' in recovery:
            policy = self.read(recovery['recovery_policy'])
            require('sandbox' in policy['recovery'], 'ISOLATED_REHEARSAL_REQUIRED')
            instance = self.read(recovery['evidence']['instance'])
            self.cleanup_scope = dict(policy=policy, container_id=instance['container_id'])

    def rollback_assets(self, plan):
        request = self.read(plan['release_request'])
        asset = self.pre.verify_rollback_assets(ROOT, request['current'], self.host)
        require(all(asset[k] == plan['primary_rollback'][k] for k in ('commit', 'tree', 'image_id')), 'ROLLBACK_ASSETS_CHANGED')
        a, b = self.sessions['target'].spec, self.sessions['primary_rollback'].spec
        require(a['grant_directory'] != b['grant_directory'] and a['policy_output'] != b['policy_output'],
                'INSTANCE_AUTHORIZATION_REUSE_FORBIDDEN')
        original = self.read(plan['source_policy'])
        rollback = self.sessions['primary_rollback'].protected_json(b['policy_template'])
        instance_only = {'/run/market-data-grants', '/run/secrets/market-data-service.json'}
        require([m for m in original['mounts'] if m['target'] not in instance_only] ==
                [m for m in rollback['mounts'] if m['target'] not in instance_only],
                'PRIMARY_ROLLBACK_DATA_MAPPING_CHANGED')
        current = self.host.docker_inspect(plan['source_container_id'])
        desired = self.host._json(self.sessions['primary_rollback'].compose(b, 'config', '--format', 'json').stdout)
        service = desired['services'][rollback['service_id']]
        require(service.get('container_name') == current['Name'].lstrip('/') and
                desired.get('name') == current['Config']['Labels'].get('com.docker.compose.project'),
                'PRIMARY_ROLLBACK_NAMESPACE_CHANGED')

    def stop_source(self, plan):
        self.engine._docker('stop', '--time', str(plan['policy']['stop_timeout_seconds']), plan['source_container_id'],
            timeout=plan['policy']['stop_timeout_seconds'] + 10)

    def create(self, plan, role):
        session = self.sessions[role]
        image = self.routine.image_identity(self.engine, plan[role]['image_id'], session.binding, session.contract['service_id'])
        session.create(image, 'production')
        self._image = image

    def authorize(self, plan, role):
        session = self.sessions[role]
        session.prepare_authorization(self._image, 'production')
        report = session.host.revalidate_production(session.container_id,
            expected_policy_path=Path(session.spec['policy_output']))
        require(report['PRE_RELEASE_VALIDATION'] == 'PASS' and report['container_started'] is False and
            report['production_write_granted'] is False, 'PRE_START_REVALIDATION_FAILED')
        self.engine._write_new(self.output / (role + '-pre-release.json'), self.engine._canonical(report))
        self.envelopes[role] = session.authorize_prepared(self._image, 'production')
        self.engine._write_new(self.output / (role + '-grant-evidence.json'), self.engine._canonical(self.envelopes[role]))

    def start(self, plan, role):
        session = self.sessions[role]
        require(self.envelopes[role]['payload']['container_id'] == session.container_id, 'GRANT_INSTANCE_CHANGED')
        session.assert_data_readonly()
        self.engine._docker('start', session.container_id, timeout=plan['policy']['start_timeout_seconds'])

    def accept(self, plan, role):
        session = self.sessions[role]
        self._post_start(plan, role)
        require(session.preflight() == 'PASS', 'RUNTIME_PREFLIGHT_FAILED')
        ready = load(ROOT, '09_deploy/spread_release/wait_for_service_ready.py', '_high_risk_readiness')
        ready.wait_for_service_ready(runtime=ready.DockerCurlRuntime(), container=session.container_id,
            health_url=session.url() + plan['policy']['readiness']['health_endpoint_path'],
            expected_image_id=plan[role]['image_id'], initial_restart_count=0,
            policy=plan['policy']['readiness'])
        self._application_preflight(plan, role, ready)
        if 'application_service' in session.current_policy:
            code = ("from agri_research_agent.shared.runtime_context import establish_application_service_context; "
                f"establish_application_service_context(service_id={session.contract['service_id']!r},"
                f"module_id={session.contract['module_id']!r},runtime_root='/runtime'); "
                "print('APPLICATION_SERVICE_CONTEXT=PASS')")
            self.engine._docker('exec', session.container_id, 'python', '-B', '-c', code)
        require(session.http() == session.smoke() == 'PASS', 'APPLICATION_ACCEPTANCE_FAILED')

    def _application_preflight(self, plan, role, ready):
        session = self.sessions[role]
        arguments = ['exec', session.container_id, 'python', '-B',
            '/app/04_scripts/runtime/spread_runtime_preflight.py', '--identity-kind', 'oci_container']
        probe = self.engine._docker(*arguments, check=False)
        # This official CLI emits its structured failure on stdout. Preserve
        # both streams before rejecting; an empty stderr is not the root cause.
        evidence = self.output / (role + '-application-preflight.json')
        self.engine._write_new(evidence, self.engine._canonical(dict(
            argv=['docker', *arguments], container_id=session.container_id,
            image_id=plan[role]['image_id'], exit_code=probe.returncode,
            stdout_sha256=hashlib.sha256(probe.stdout).hexdigest(),
            stderr_sha256=hashlib.sha256(probe.stderr).hexdigest(),
            stdout=ready._redact_log_text(probe.stdout.decode('utf-8')),
            stderr=ready._redact_log_text(probe.stderr.decode('utf-8')))))
        if probe.returncode:
            # The historical main intentionally reports only error_type. Call
            # its SAME readonly function to retain the traceback for diagnosis;
            # never treat a diagnostic success as acceptance of the failed CLI.
            code = ("import runpy; m=runpy.run_path('/app/04_scripts/runtime/spread_runtime_preflight.py'); "
                "m['readonly_preflight'](m['parser']().parse_args(['--identity-kind','oci_container']))")
            diagnostic_args = ['exec', session.container_id, 'python', '-B', '-c', code]
            diagnostic = self.engine._docker(*diagnostic_args, check=False)
            self.engine._write_new(self.output / (role + '-application-preflight-diagnostic.json'),
                self.engine._canonical(dict(argv=['docker', *diagnostic_args],
                    container_id=session.container_id, image_id=plan[role]['image_id'],
                    exit_code=diagnostic.returncode, purpose='READONLY_DIAGNOSIS_NOT_ACCEPTANCE',
                    stdout_sha256=hashlib.sha256(diagnostic.stdout).hexdigest(),
                    stderr_sha256=hashlib.sha256(diagnostic.stderr).hexdigest(),
                    stdout=ready._redact_log_text(diagnostic.stdout.decode('utf-8')),
                    stderr=ready._redact_log_text(diagnostic.stderr.decode('utf-8')))))
        require(probe.returncode == 0, 'APPLICATION_RUNTIME_PREFLIGHT_FAILED: ' + str(evidence))

    def _post_start(self, plan, role):
        session = self.sessions[role]
        c = self.host.docker_inspect(session.container_id)
        require(c['State']['Running'] and c['Image'] == plan[role]['image_id'] and c['RestartCount'] == 0,
                'ACTUAL_POST_START_IDENTITY')
        raw = self.host.copy_container_bytes(session.container_id, '/app/RELEASE.json')
        observed = self.host.normalize_observation(c, self.host.docker_image_inspect(c['Image']), self.host._json(raw))
        grant = self.envelopes[role]['payload']
        require(grant['container_id'] == c['Id'] and grant['image_id'] == c['Image'] and
                grant['hostname_nonce'] == c['Config']['Hostname'] and
                grant['release_sha256'] == hashlib.sha256(raw).hexdigest() and
                grant['mount_contract_sha256'] == self.host._digest(observed['mounts']) and
                datetime.fromisoformat(grant['issued_at']) <= datetime.now(timezone.utc) <
                datetime.fromisoformat(grant['expires_at']), 'POST_START_GRANT_BINDING')
        # The existing shared comparator owns the witnessed Docker compatibility
        # rule, including created/running OOM equivalence; raw hashes stay intact.
        self.host.compare_observed_config(observed, grant['actual_config_sha256'])
        require(session.host._render_actual_compose(c, session.current_policy,
            self.host.docker_image_inspect(c['Image'])) == grant['rendered_compose_sha256'], 'POST_START_COMPOSE_CHANGED')
        session.assert_data_readonly()

    def observe(self, plan):
        session = self.sessions['target']
        self._post_start(plan, 'target')
        c = self.host.docker_inspect(session.container_id)
        if c['Image'] != plan['target']['image_id'] or not c['State']['Running'] or c['RestartCount'] != 0:
            return 'FATAL'
        try:
            policy = plan['policy']['readiness']
            with urllib.request.urlopen(session.url() + policy['health_endpoint_path'],
                                        timeout=policy['request_timeout_seconds']) as response:
                return 'PASS' if response.status == policy['success_http_status'] and response.read().decode().strip() == policy['success_body_exact'] else 'TRANSIENT'
        except (OSError, TimeoutError):
            return 'TRANSIENT'

    def stop_target(self, plan):
        if self.sessions['target'].container_id:
            self.engine._docker('stop', '--time', str(plan['policy']['stop_timeout_seconds']), self.sessions['target'].container_id,
                timeout=plan['policy']['stop_timeout_seconds'] + 10)

    def record(self, result):
        result['instances'] = {role: dict(container_id=session.container_id,
            application_commit=session.binding['commit'], image_id=session.current_policy['image_id']
            if session.current_policy else None,
            grant_id=self.envelopes.get(role, {}).get('payload', {}).get('grant_id'),
            policy_path=session.spec['policy_output'],
            issuer_module=session.host.__name__, issuer_file=session.host.__file__,
            issuer_source_commit=session.binding['commit'],
            transport_file=self.routine.__file__, observation_source_root=str(ROOT))
            for role, session in self.sessions.items()}
        self.engine._write_new(self.output / 'execution-result.json', self.engine._canonical(result))

    def network_observation(self, network_name, *, expected_id=None):
        return self.engine._interpret('inspect_object',
            self.engine._docker('network', 'inspect', network_name).stdout,
            'Docker network inspect', expected_id=expected_id)

    def cleanup_temporary(self, plan):
        # The exact rehearsal scope was successfully consumed while the source
        # was still running. Do not call retained() after the switch and pretend
        # that its live-old-instance predicate remains true.
        if self.cleanup_scope is None:
            return
        scope = self.cleanup_scope
        policy = scope['policy']
        module = load(ROOT, '09_deploy/runtime_identity/recovery_namespace.py', '_high_risk_cleanup_scope')
        module.validate_sandbox_declaration(self.host, policy['recovery'])
        root = Path(policy['recovery']['sandbox']['root'])
        self.host._protected_path(root, directory=True)
        require(root.resolve(strict=True) == root and root.parent == module.SANDBOX_PARENT,
                'CLEANUP_OUTSIDE_EXACT_REHEARSAL_ALLOCATION')
        rehearsal = scope['container_id']
        require(rehearsal != plan['source_container_id'] and
                rehearsal not in {s.container_id for s in self.sessions.values()}, 'CLEANUP_REFERENCES_PRODUCTION_INSTANCE')
        grant_directory = Path(next(m['source'] for m in policy['mounts']
            if m['target'] == policy['grant_container_directory']))
        require(grant_directory.parent == Path(policy['production_storage_root']) and
                grant_directory not in {Path(s.spec['grant_directory']) for s in self.sessions.values()},
                'TEMPORARY_GRANT_CLEANUP_SCOPE')
        self.host._protected_path(grant_directory, directory=True)
        require({p.name for p in grant_directory.iterdir()} == {'grant.json'}, 'UNDECLARED_TEMPORARY_GRANT_CONTENT')
        network_name = policy['recovery']['network']
        network = self.network_observation(network_name,
            expected_id=policy['recovery']['expected_network_id'])
        require(network['Id'] == policy['recovery']['expected_network_id'] and
                set(network.get('Containers', {})) <= {rehearsal}, 'TEMPORARY_NETWORK_CHANGED_OR_SHARED')
        ids = self.engine._docker('ps', '-aq', '--no-trunc').stdout.decode().split()
        if rehearsal in ids:
            c = self.host.docker_inspect(rehearsal)
            require(c['Image'] == plan['source']['image_id'] and
                    c['Name'] == '/' + policy['recovery']['container'], 'REHEARSAL_INSTANCE_CHANGED')
            self.engine._docker('rm', '-f', rehearsal)
        self.engine._docker('network', 'rm', network_name)
        # Refuse deletion if any other instance still references the allocation.
        for cid in self.engine._docker('ps', '-aq', '--no-trunc').stdout.decode().split():
            c = self.host.docker_inspect(cid)
            require(not any(self.host._within(m['Source'], str(root)) or self.host._within(str(root), m['Source'])
                    for m in c.get('Mounts', [])), 'REHEARSAL_ALLOCATION_STILL_MOUNTED')
        shutil.rmtree(root)
        (grant_directory / 'grant.json').unlink()
        grant_directory.rmdir()
        if 'primary_rollback' in self.envelopes:
            target = self.sessions.get('target')
            if target and target.current_policy and 'application_service' in target.current_policy:
                credential = Path(next(m['source'] for m in target.current_policy['mounts']
                    if m['target'] == '/run/secrets/market-data-service.json'))
                require(credential.resolve(strict=True) == credential and
                        credential.is_relative_to(Path(target.current_policy['production_storage_root'])),
                        'FAILED_TARGET_CREDENTIAL_CLEANUP_SCOPE')
                require(all(credential != Path(m['source']) for m in self.sessions['primary_rollback'].current_policy['mounts']),
                        'FAILED_TARGET_CREDENTIAL_IS_CURRENT_ROLLBACK_INPUT')
                credential.unlink()
        # Evidence copies outside this allocation and current production grants
        # and service credentials are deliberately retained, never overwritten.


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    result = execute_verified_plan(args.plan, HostBackend(args.output))
    print(json.dumps(result, sort_keys=True))
    return 0 if result['result'] == 'SUCCESS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
