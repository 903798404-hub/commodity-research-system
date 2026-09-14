"""Build once, accept an exact image, then switch with a fresh execution grant.

The CLI consumes administrator-owned deployment inputs. It never accepts a
caller risk override or executes arbitrary shell commands. No signing of smoke
artifacts: execution grants retain their existing host/instance verification.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE_SCHEMA = 'routine-candidate-acceptance/1'
WAITING = 'WAITING_FOR_MAINTAINER_UI_ACCEPTANCE'


def acceptance_mode(request):
    mode = request.get('ui_acceptance_mode', 'AUTOMATED')
    require(mode in ('MANUAL', 'AUTOMATED'), 'UNKNOWN_UI_ACCEPTANCE_MODE')
    return mode


def check_browser(mode):
    if mode == 'AUTOMATED':
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            require(Path(p.chromium.executable_path).is_file(), 'CHROMIUM_NOT_AVAILABLE')


def diagnostics(backend):
    try:
        return backend.diagnostics()
    except Exception:
        # Logs are supplemental; never hide the machine failure or block cleanup.
        return {'collection_status': 'unavailable'}


class RoutineError(ValueError):
    pass


def load(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def now():
    return datetime.now(timezone.utc).isoformat()


def require(condition, message):
    if not condition:
        raise RoutineError(message)


def require_routine(repo, base, target, project):
    risk = load('04_scripts/runtime/pre_release_runtime.py', '_routine_risk').classify_release(
        Path(repo), base, target, project)
    require(risk['RELEASE_RISK_CLASS'] == 'ROUTINE_STATELESS', 'HIGH_RISK_REQUIRES_EXISTING_RELEASE_PATH')
    return risk


def validate_acceptance(record, *, commit, tree, image_id):
    require(record.get('schema_version') == ACCEPTANCE_SCHEMA, 'ACCEPTANCE_SCHEMA')
    require(record.get('commit') == commit and record.get('tree') == tree
            and record.get('image_id') == image_id, 'ACCEPTANCE_IDENTITY_MISMATCH')
    require(record.get('result') == 'PASS' and all(record.get(k) == 'PASS' for k in
            ('runtime_preflight', 'health', 'application_smoke', 'candidate_cleanup')), 'CANDIDATE_NOT_ACCEPTED')
    if acceptance_mode(record) == 'MANUAL':
        require(record.get('http') == 'PASS' and record.get('manual_ui', {}).get('decision') == 'PASS'
                and bool(record['manual_ui'].get('operator')), 'MANUAL_ACCEPTANCE_MISSING')
    require(isinstance(record.get('release_id'), str) and bool(record['release_id']), 'RELEASE_ID_MISSING')
    require(re.fullmatch(r'[0-9a-f]{40}', record.get('base_commit', '')) is not None, 'BASE_MISSING')
    stamp = datetime.fromisoformat(record['validated_at'])
    require(stamp.tzinfo is not None and stamp <= datetime.now(timezone.utc), 'ACCEPTANCE_TIMESTAMP')
    return record


def verify_routine_record(record, policy, source_root):
    """Called inside the protected grant issuer; no unsigned caller PASS override.

    The file is an administrator-protected collector result, not a signature.
    Recompute risk from Git objects; a high-risk delta cannot opt into this path.
    """
    if record.get('schema_version') == 'routine-rollback-assets/1':
        require(record.get('commit') == policy['approved_commit'] and record.get('tree') == policy['approved_tree']
                and record.get('image_id') == policy['image_id'] and record.get('health') == 'PASS'
                and record.get('project_id') == policy['project_id'], 'ROLLBACK_ASSETS_MISMATCH')
        require_routine(source_root, record['commit'], record['forward_target'], policy['project_id'])
        return record
    validate_acceptance(record, commit=policy['approved_commit'], tree=policy['approved_tree'],
                        image_id=policy['image_id'])
    require_routine(source_root, record['base_commit'], record['commit'], policy['project_id'])
    require(record['project_id'] == policy['project_id'], 'ACCEPTANCE_PROJECT_MISMATCH')
    return record


def image_identity(engine, image_id, binding, service):
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', image_id or '') is not None, 'IMMUTABLE_IMAGE_REQUIRED')
    image = engine.inspect_one('image', image_id)
    require(image['Id'] == image_id, 'IMAGE_ID_MISMATCH')
    engine._labels(image, binding, service)
    labels = image['Config']['Labels']
    return dict(commit=binding['commit'], tree=binding['tree'], image_id=image_id,
                repo_digests=image.get('RepoDigests') or [], release_id=labels['market-data.release.id'],
                oci_revision=labels['org.opencontainers.image.revision'],
                source_revision=labels['org.opencontainers.image.source'])


def check_ci(ci, binding):
    require(ci.get('candidate') == {'commit': binding['commit'], 'tree': binding['tree']}
            and ci.get('final_result') == 'PASS'
            and ci.get('checks', {}).get('TECHNICAL_VALIDATION') == 'PASS'
            and bool(ci['checks'].get('workflow_run_id')), 'EXACT_MAIN_CI_PASS_REQUIRED')
    return ci['checks']['workflow_run_id']


def candidate_acceptance(backend, image, *, base_commit, ci_run, mode='AUTOMATED'):
    """The backend performs fresh create/grant/start and actual application IO."""
    result = dict(schema_version=ACCEPTANCE_SCHEMA, **image, base_commit=base_commit,
                  project_id=backend.project_id, ci_run=ci_run, validated_at=now(),
                  runtime_preflight='NOT_RUN', health='NOT_RUN', application_smoke='NOT_RUN',
                  candidate_cleanup='NOT_RUN', result='FAIL', ui_acceptance_mode=mode)
    require(mode in ('MANUAL', 'AUTOMATED'), 'UNKNOWN_UI_ACCEPTANCE_MODE')
    try:
        backend.create(image, 'candidate_validation')
        backend.grant_and_start(image, 'candidate_validation')
        result['runtime_preflight'] = backend.preflight()
        result['health'] = backend.health()
        if mode == 'MANUAL':
            result['http'] = backend.http()
            require(result['runtime_preflight'] == result['health'] == result['http'] == 'PASS', 'CANDIDATE_SMOKE_FAILED')
        else:
            result['application_smoke'] = backend.smoke()
            require(all(result[k] == 'PASS' for k in ('runtime_preflight', 'health', 'application_smoke')), 'CANDIDATE_SMOKE_FAILED')
        backend.assert_image(image)
        backend.assert_data_readonly()
        if mode == 'MANUAL':
            result.update(instance=backend.checkpoint(), stage='candidate', result=WAITING)
        else:
            result['result'] = 'PASS'
    except Exception as exc:
        result['failure'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        if mode == 'MANUAL':
            result['diagnostics'] = diagnostics(backend)
        try:
            if result['result'] != WAITING:
                backend.cleanup()
                result['candidate_cleanup'] = 'PASS'
            else:
                result['candidate_cleanup'] = 'RETAINED_FOR_MANUAL_UI'
        except Exception as exc:
            result.update(result='FAIL', candidate_cleanup='FAIL', cleanup_failure=str(exc))
        result['validated_at'] = now()
    return result


def deploy_same_image(backend, image, acceptance, previous, *, ci_run):
    validate_acceptance(acceptance, commit=image['commit'], tree=image['tree'], image_id=image['image_id'])
    require(acceptance['release_id'] == image['release_id'], 'RELEASE_ID_MISMATCH')
    require(acceptance['ci_run'] == ci_run, 'CI_RUN_MISMATCH')
    require(acceptance['base_commit'] == previous['commit'], 'PRODUCTION_BASE_MOVED')
    # This function has no build operation. Docker create always uses --no-build.
    mode = acceptance_mode(acceptance)
    result = dict(schema_version='routine-deployment-result/1', **image, ci_run=ci_run, ui_acceptance_mode=mode,
                  candidate_acceptance='PASS', deployed_at=now(), previous_release=previous,
                  production_health='NOT_RUN', production_smoke='NOT_RUN', result='FAIL', rollback='NOT_NEEDED')
    switched = False
    try:
        backend.assert_image(image)
        backend.verify_previous(previous)
        switched = True  # A failed create can already have stopped/replaced the old container.
        backend.create(image, 'production')
        backend.grant_and_start(image, 'production')
        result['runtime_preflight'] = backend.preflight()
        require(result['runtime_preflight'] == 'PASS', 'PRODUCTION_PREFLIGHT_FAILED')
        result['production_health'] = backend.health()
        if mode == 'MANUAL':
            result['http'] = backend.http()
            require(result['production_health'] == result['http'] == 'PASS', 'PRODUCTION_SMOKE_FAILED')
        else:
            result['production_smoke'] = backend.smoke()
            require(result['production_health'] == result['production_smoke'] == 'PASS', 'PRODUCTION_SMOKE_FAILED')
        backend.assert_image(image)
        backend.assert_data_readonly()
        if mode == 'MANUAL':
            result.update(instance=backend.checkpoint(), stage='production', result=WAITING)
        else:
            result['result'] = 'PASS'
    except Exception as exc:
        result['failure'] = type(exc).__name__ + ': ' + str(exc)
        if switched:
            try:
                backend.rollback(previous)
                result['rollback'] = 'PASS'
            except Exception as rollback_error:
                result.update(rollback='FAIL', rollback_failure=str(rollback_error))
    return result


def finish_manual(backend, image, checkpoint, *, stage, decision, operator, ci_run):
    """Record an explicit maintainer decision; never manufacture a machine PASS.

    Input/output are ordinary protected deployment records, not approval tokens.
    A resumed process may only use the original, continuously running instance.
    """
    require(stage in ('candidate', 'production'), 'MANUAL_STAGE')
    require(decision in ('PASS', 'FAIL', 'CANCEL') and isinstance(operator, str)
            and bool(operator.strip()), 'EXPLICIT_MANUAL_DECISION_REQUIRED')
    require(checkpoint.get('result') == WAITING and checkpoint.get('stage') == stage
            and acceptance_mode(checkpoint) == 'MANUAL', 'NOT_WAITING_FOR_MANUAL_UI')
    require(all(checkpoint.get(k) == image[k] for k in image)
            and checkpoint.get('ci_run') == ci_run, 'CHECKPOINT_IDENTITY_MISMATCH')
    expected_schema = ACCEPTANCE_SCHEMA if stage == 'candidate' else 'routine-deployment-result/1'
    health_key = 'health' if stage == 'candidate' else 'production_health'
    require(checkpoint.get('schema_version') == expected_schema and all(checkpoint.get(k) == 'PASS'
            for k in ('runtime_preflight', health_key, 'http')), 'MACHINE_FAILURE_CANNOT_BE_OVERRIDDEN')
    # Attach first. If the instance identity changed, do not delete a replacement.
    result = copy.deepcopy(checkpoint)
    result.update(result='FAIL', manual_ui=dict(decision=decision, operator=operator.strip(), recorded_at=now()))
    try:
        backend.resume(checkpoint['instance'], stage)
    except Exception as exc:
        result.update(failure=type(exc).__name__ + ': ' + str(exc),
                      recovery='STOP_INSTANCE_IDENTITY_UNPROVEN', finalized_at=now())
        return result
    try:
        backend.assert_image(image)
        backend.assert_data_readonly()
        result[health_key] = backend.health()
        result['http'] = backend.http()
        require(result[health_key] == result['http'] == 'PASS', 'MACHINE_FAILURE_CANNOT_BE_OVERRIDDEN')
        require(decision == 'PASS', 'MANUAL_UI_' + decision)
        result['application_smoke' if stage == 'candidate' else 'production_smoke'] = 'PASS'
        result['result'] = 'PASS'
    except Exception as exc:
        result['failure'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        result['diagnostics'] = diagnostics(backend)
        if stage == 'candidate':
            try:
                backend.cleanup()
                result['candidate_cleanup'] = 'PASS'
            except Exception as exc:
                result.update(result='FAIL', candidate_cleanup='FAIL', cleanup_failure=str(exc))
            result['validated_at'] = now()
        elif result['result'] != 'PASS':
            try:
                backend.rollback(result['previous_release'])
                result['rollback'] = 'PASS'
            except Exception as exc:
                result.update(rollback='FAIL', rollback_failure=str(exc))
    result['finalized_at'] = now()
    return result


def check_dom_tables(tables, empty_sessions=()):
    """Check rendered HTML, including null cells. Zero is never a null token."""
    seen = {}
    for table in tables:
        heading = table['heading'].upper()
        session = next((s for s in ('AM', 'PM') if re.search(r'\b'+s+r'\b', heading)), None)
        if session is None:
            continue
        require(session not in seen, 'DUPLICATE_SESSION_TABLE')
        rows = table['rows']
        require(len(rows) == 12, 'MONTH_ROW_COUNT')
        months = []
        for cells in rows:
            require(len(cells) == 14, 'TABLE_COLUMN_COUNT')
            match = re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', cells[0].strip())
            require(match is not None, 'MONTH_IDENTITY')
            months.append(int(match[1]))
            if session in empty_sessions:
                # Tariff/tax are configured parameters, not fabricated market values.
                require(all(cells[i].strip() in ('', '—', '--', '暂无') for i in
                            (1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 13)), 'EMPTY_MONTH_HAS_FAKE_VALUE')
        require(months == list(range(1, 13)), 'MONTH_SEQUENCE')
        seen[session] = months
    require(set(seen) == {'AM', 'PM'}, 'AM_PM_TABLES_REQUIRED')
    return 'PASS'


def browser_smoke(url, empty_sessions, selectors=None):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
            if selectors is not None:
                require(bool(selectors), 'DOM_ASSERTIONS_REQUIRED')
                for selector, expected_count in selectors.items():
                    require(isinstance(selector, str) and type(expected_count) is int and expected_count > 0,
                            'INVALID_DOM_ASSERTION')
                    page.locator(selector).first.wait_for(timeout=60000)
                    require(page.locator(selector).count() == expected_count, 'DOM_ASSERTION_FAILED')
                require(page.locator('[data-testid="stException"]').count() == 0, 'APPLICATION_EXCEPTION')
                return 'PASS'
            page.locator('.profit-card').nth(1).wait_for(timeout=60000)
            tables = page.locator('.profit-card').evaluate_all("""cards => cards.map(c => ({
                heading: c.querySelector('.profit-card-time')?.textContent || '',
                rows: Array.from(c.querySelectorAll('tbody tr'), r =>
                  Array.from(r.querySelectorAll('td'), td => td.textContent.trim()))
            }))""")
            return check_dom_tables(tables, empty_sessions)
        finally:
            browser.close()


class DockerSession:
    """Concrete host adapter using prepared, approved Compose/env/policy inputs.

    Mount directories and policy templates are prepared by the operator under
    the existing host contract. No scripts, refresh hooks or shell callbacks.
    """
    def __init__(self, request, engine, host, binding, contract):
        self.request, self.engine, self.host = request, engine, host
        self.binding, self.contract = binding, contract
        self.project_id = contract['project_id']
        self.container_id = None
        self.current_policy = None
        self.spec = None

    def compose(self, spec, *args):
        return self.engine._docker('compose', '--project-directory', spec['project_directory'],
                '--env-file', spec['environment'], '-f', spec['compose'], *args)

    def protected_json(self, path):
        return self.host._json(self.host._protected_path(Path(path), private=True).read_bytes())

    def assert_image(self, image):
        require(image_identity(self.engine, image['image_id'], self.binding, self.contract['service_id']) == image,
                'DEPLOYMENT_IMAGE_CHANGED')
        if self.container_id:
            require(self.engine.inspect_one('container', self.container_id)['Image'] == image['image_id'], 'CONTAINER_IMAGE_CHANGED')

    def create(self, image, role):
        self.spec = self.request['candidate' if role == 'candidate_validation' else 'production']
        spec = self.spec
        for key in ('compose', 'environment', 'policy_template'):
            self.host._protected_path(Path(spec[key]))
        self.host._protected_path(Path(spec['project_directory']), directory=True)
        rendered = self.host._json(self.compose(spec, 'config', '--format', 'json').stdout)
        service_id = self.contract['service_id']
        require(set(rendered.get('services', {})) == {service_id}, 'ONLY_TARGET_SERVICE_ALLOWED')
        service = rendered['services'][service_id]
        require(service.get('image') == image['image_id'], 'DEPLOYMENT_IMAGE_MISMATCH')
        require('build' not in service, 'PRODUCTION_REBUILD_FORBIDDEN')
        require(service.get('entrypoint') == self.contract['entrypoint'], 'ENTRYPOINT_CHANGED')
        require(not service.get('command') and not service.get('post_start') and not service.get('pre_stop'), 'LIFECYCLE_HOOK_FORBIDDEN')
        require(service.get('read_only') is True, 'WRITABLE_ROOTFS_FORBIDDEN')
        if role == 'candidate_validation':
            require(service.get('restart', 'no') == 'no', 'CANDIDATE_RESTART_FORBIDDEN')
            require(str(rendered.get('name', '')).startswith('routine-candidate-'), 'CANDIDATE_NAMESPACE')
            require(all(p.get('host_ip') == '127.0.0.1' for p in service.get('ports', [])), 'CANDIDATE_PORT_EXPOSURE')
        self._check_mounts(service.get('volumes', []))
        # Candidate namespaces must be unused: never recreate a running service.
        if role == 'candidate_validation':
            require(not self.compose(spec, 'ps', '-q', '--all', service_id).stdout.strip(), 'CANDIDATE_NAMESPACE_IN_USE')
        self.compose(spec, 'create', '--no-build', '--pull', 'never', '--no-deps', '--force-recreate', service_id)
        ids = self.compose(spec, 'ps', '-q', '--all', service_id).stdout.decode().split()
        require(len(ids) == 1, 'EXACTLY_ONE_INSTANCE_REQUIRED')
        self.container_id = ids[0]
        container = self.engine.inspect_one('container', self.container_id)
        require(container['State']['Status'] == 'created', 'FRESH_INSTANCE_REQUIRED')
        self.assert_image(image)
        self.assert_data_readonly()

    def _check_mounts(self, mounts):
        writable = {r['container_path'] for r in self.contract['runtime_roots']
                    if r['access'] == 'rw' and r['role'] in {'outputs', 'logs', 'cache', 'temporary'}}
        for mount in mounts:
            require(mount.get('type') == 'bind', 'EXPLICIT_BIND_REQUIRED')
            target = mount.get('target')
            if not mount.get('read_only', False):
                require(target in writable, 'PRODUCTION_DATA_WRITE_FORBIDDEN')
                source = Path(mount['source']).resolve()
                root = Path(self.spec['writable_root']).resolve()
                require(source != root and source.is_relative_to(root), 'WRITE_OUTSIDE_INSTANCE_RUNTIME')

    def assert_data_readonly(self):
        container = self.engine.inspect_one('container', self.container_id)
        mounts = [{'type': m['Type'], 'source': m['Source'], 'target': m['Destination'],
                   'read_only': not m['RW']} for m in container['Mounts']]
        self._check_mounts(mounts)

    def grant_and_start(self, image, role):
        spec = self.spec
        policy = self.protected_json(spec['policy_template'])
        if role == 'production':
            record_path = Path(self.request['acceptance_record'])
            raw = self.host._protected_path(record_path, private=True).read_bytes()
            policy['candidate_record'] = {'path': str(record_path), 'sha256': hashlib.sha256(raw).hexdigest()}
        require(policy['approved_commit'] == image['commit'] and policy['approved_tree'] == image['tree']
                and policy['image_id'] == image['image_id'] and policy['role'] == role, 'POLICY_TARGET_MISMATCH')
        container = self.engine.inspect_one('container', self.container_id)
        inspected_image = self.engine.inspect_one('image', image['image_id'])
        release = self.host.copy_container_json(self.container_id)
        require(release['release_id'] == image['release_id'], 'RUNTIME_RELEASE_ID_MISMATCH')
        observed = self.host.normalize_observation(container, inspected_image, release)
        policy['actual_config_sha256'] = observed['actual_config_sha256']
        self.current_policy = policy
        path = Path(spec['policy_output'])
        self.engine._write_new(path, self.engine._canonical(policy))
        grant_path = Path(spec['grant_directory']) / 'grant.json'
        envelope = self.host.issue_execution_grant(self.container_id, expected_policy_path=path,
            key_path=spec['key_path'], grant_path=grant_path, grant_dir=grant_path.parent, role=role, ttl_seconds=900)
        payload = envelope['payload']
        require(payload['container_id'] == self.container_id and payload['role'] == role
                and payload['image_id'] == image['image_id'] and grant_path.is_file(), 'FRESH_GRANT_MISSING')
        require(datetime.fromisoformat(payload['issued_at']) <= datetime.now(timezone.utc)
                < datetime.fromisoformat(payload['expires_at']), 'FRESH_GRANT_EXPIRED')
        self.assert_data_readonly()
        self.engine._docker('start', self.container_id)

    def preflight(self):
        container = self.engine.inspect_one('container', self.container_id)
        require(container['State']['Running'] is True, 'APPLICATION_NOT_RUNNING')
        self.assert_data_readonly()
        policy = self.current_policy
        code = ("from pathlib import Path; from agri_research_agent.shared.production_identity import OCIExecutionRequest,verify_execution; "
            f"r=Path({policy['runtime_root']!r}); verify_execution(OCIExecutionRequest(Path('/run/market-data-grants/grant.json'),"
            f"Path('/app/RELEASE.json'),Path({policy['runtime_manifest_path']!r}),r,r/'.market-data-runtime.json'),"
            f"expected_role={policy['role']!r},module_id={policy['module_id']!r},runtime_id={policy['runtime_id']!r},"
            f"runtime_root=r,marker_sha256={policy['runtime_marker_sha256']!r})")
        self.engine._docker('exec', self.container_id, 'python', '-B', '-c', code)
        return 'PASS'

    def url(self):
        container = self.engine.inspect_one('container', self.container_id)
        port = self.spec['container_port']
        ports = container['NetworkSettings']['Ports'].get(str(port)+'/tcp') or []
        require(len(ports) == 1 and ports[0]['HostIp'] in ('127.0.0.1', '0.0.0.0'), 'HTTP_PORT_NOT_BOUND')
        return 'http://127.0.0.1:' + ports[0]['HostPort']

    def health(self):
        deadline = time.monotonic() + 60
        url = self.url() + '/_stcore/health'
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    if response.status == 200 and response.read().strip() == b'ok':
                        return 'PASS'
            except (OSError, TimeoutError):
                pass
            time.sleep(1)
        raise RoutineError('HEALTH_FAILED')

    def application_url(self):
        path = self.request['application_smoke']['path']
        require(isinstance(path, str) and path.startswith(('/', '?')) and not path.startswith('//')
                and '\\' not in path, 'SMOKE_MUST_USE_ACTUAL_CONTAINER_PORT')
        return self.url() + path

    def http(self):
        url = self.application_url()
        with urllib.request.urlopen(url, timeout=10) as response:
            require(response.status == 200 and response.geturl() == url
                    and bool(response.read(4096)), 'APPLICATION_HTTP_FAILED')
        return 'PASS'

    def checkpoint(self):
        container = self.engine.inspect_one('container', self.container_id)
        require(container['State']['Running'] is True, 'APPLICATION_NOT_RUNNING')
        return dict(container_id=self.container_id, started_at=container['State']['StartedAt'],
                    restart_count=container['RestartCount'], url=self.application_url(), spec=copy.deepcopy(self.spec))

    def diagnostics(self):
        require(bool(self.container_id), 'NO_DIAGNOSTIC_INSTANCE')
        raw = self.engine._docker('logs', '--tail', '200', self.container_id)
        summary = load('09_deploy/spread_release/wait_for_service_ready.py', '_routine_log_summary')
        return summary.build_log_summary((raw.stdout + raw.stderr).decode('utf-8', errors='replace'),
                                         collected_at_utc=now())

    def resume(self, instance, stage):
        spec = self.request['candidate' if stage == 'candidate' else 'production']
        require(spec == instance['spec'], 'INSTANCE_INPUTS_CHANGED')
        container = self.engine.inspect_one('container', instance['container_id'])
        require(container['Id'] == instance['container_id'] and container['State']['Running'] is True
                and container['State']['StartedAt'] == instance['started_at']
                and container['RestartCount'] == instance['restart_count'], 'WAITING_INSTANCE_CHANGED')
        ids = self.compose(spec, 'ps', '-q', '--all', self.contract['service_id']).stdout.decode().split()
        require(ids == [instance['container_id']], 'WAITING_NAMESPACE_CHANGED')
        self.spec, self.container_id = spec, instance['container_id']
        require(self.application_url() == instance['url'], 'WAITING_URL_CHANGED')

    def smoke(self):
        smoke = self.request['application_smoke']
        path = smoke['path']
        require(isinstance(path, str) and path.startswith(('/', '?')) and not path.startswith('//')
                and '\\' not in path, 'SMOKE_MUST_USE_ACTUAL_CONTAINER_PORT')
        require(smoke['kind'] in ('soybean-fixed-months', 'dom'), 'UNKNOWN_APPLICATION_SMOKE')
        return browser_smoke(self.url()+path, self.request.get('empty_sessions', []),
                             smoke['selectors'] if smoke['kind'] == 'dom' else None)

    def cleanup(self):
        if self.container_id:
            self.engine._docker('rm', '-f', self.container_id)
            require(self.engine._docker('inspect', self.container_id, check=False).returncode != 0,
                    'CANDIDATE_CLEANUP_FAILED')
            self.container_id = None

    def verify_previous(self, previous):
        ids = self.compose(self.request['production'], 'ps', '-q', '--all', self.contract['service_id']).stdout.decode().split()
        require(ids == [previous['container_id']], 'PRODUCTION_NAMESPACE_MISMATCH')
        c = self.engine.inspect_one('container', previous['container_id'])
        require(c['State']['Running'] and c['Image'] == previous['image_id'], 'PREVIOUS_RELEASE_DRIFT')
        labels = self.engine.inspect_one('image', previous['image_id'])['Config']['Labels']
        require(labels['org.opencontainers.image.revision'] == previous['commit']
                and labels['market-data.git.tree'] == previous['tree'], 'PREVIOUS_IMAGE_IDENTITY')
        self.host._protected_path(Path(self.request['rollback']['compose']))
        self.host._protected_path(Path(self.request['rollback']['policy_template']))
        source = self.host._protected_path(Path(self.request['rollback']['source_root']), directory=True)
        require(self.engine._git(source, 'rev-parse', 'HEAD') == previous['commit']
                and self.engine._git(source, 'rev-parse', 'HEAD^{tree}') == previous['tree'], 'ROLLBACK_SOURCE_MISMATCH')
        # Observe the old application now. Historical execution grant is not read.
        saved_id, saved_spec = self.container_id, self.spec
        self.container_id, self.spec = previous['container_id'], self.request['rollback']
        try:
            require(self.health() == 'PASS', 'PREVIOUS_RELEASE_UNHEALTHY')
        finally:
            self.container_id, self.spec = saved_id, saved_spec
        assets = dict(schema_version='routine-rollback-assets/1', **previous, project_id=self.project_id,
                      forward_target=self.binding['commit'], health='PASS', observed_at=now())
        self.engine._write_new(Path(self.request['rollback']['assets_output']), self.engine._canonical(assets))

    def rollback(self, previous):
        # Fresh rollback instance and grant; never read or reuse the old grant.
        self.cleanup()
        request = copy.deepcopy(self.request)
        request['production'] = request['rollback']
        request['acceptance_record'] = request['rollback']['assets_output']
        source = Path(request['rollback']['source_root'])
        project = self.engine._project(source, self.project_id)
        _, contract, binding = self.engine.source_contract(source, self.project_id, project['runtime_contract'])
        backend = DockerSession(request, self.engine, self.host, binding, contract)
        image = image_identity(self.engine, previous['image_id'], binding, contract['service_id'])
        backend.create(image, 'production')
        backend.grant_and_start(image, 'production')
        require(backend.preflight() == backend.health() == 'PASS', 'ROLLBACK_HEALTH_FAILED')
        require(backend.http() == 'PASS', 'ROLLBACK_HTTP_FAILED')
        if acceptance_mode(self.request) == 'MANUAL':
            return
        # Previous releases need their own consumer contract, not the new fix's
        # twelve-row invariant. Require the real previous page to load cleanly.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(backend.url()+'?workspace_page=import_profit', wait_until='domcontentloaded')
                page.locator('.profit-card').nth(1).wait_for(timeout=60000)
                require(page.locator('[data-testid="stException"]').count() == 0, 'ROLLBACK_CONSUMER_FAILED')
            finally:
                browser.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['build', 'validate', 'deploy', 'candidate-ui', 'production-ui'])
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    host = load('09_deploy/runtime_identity/host_authorization.py', '_routine_host')
    engine = load('04_scripts/runtime/validate_target_runtime.py', '_routine_engine')
    host._require_linux_root()
    host.require_protected_authority_source()
    request = host._json(host._protected_path(args.request, private=True).read_bytes())
    source = host._protected_path(Path(request['source_root']), directory=True)
    load('04_scripts/runtime/pre_release_runtime.py', '_routine_source').require_source(host, engine, source_root=source)
    project = engine._project(source, request['project_id'])
    _, contract, binding = engine.source_contract(source, project['project_id'], project['runtime_contract'])
    require(binding['commit'] == request['target_commit'] and binding['tree'] == request['target_tree'], 'TARGET_SOURCE_MISMATCH')
    require_routine(source, request['base_commit'], binding['commit'], project['project_id'])
    ci = host._json(host._protected_path(Path(request['ci_record']), private=True).read_bytes())
    ci_run = check_ci(ci, binding)
    require(request.get('production_data_mutation') is False, 'PRODUCTION_DATA_WRITE_FORBIDDEN')
    require(request.get('rebuild') is False, 'PRODUCTION_REBUILD_FORBIDDEN')
    host._protected_path(args.output.parent, directory=True)
    require(not args.output.exists(), 'OUTPUT_ALREADY_EXISTS')
    backend = DockerSession(request, engine, host, binding, contract)
    if args.phase == 'build':
        require(request.get('build_authorized') is True, 'BUILD_NOT_AUTHORIZED')
        # Exclusive attempt directory prevents a resumed invocation rebuilding.
        work = Path(request['build_directory'])
        host._protected_path(work.parent, directory=True)
        work.mkdir(mode=0o700)
        context = work/'context'
        engine.require_builder()
        engine.create_archive_context(source, context, binding)
        engine._exclude_candidate_inputs(context, contract)
        image_id = engine.build_image(source, context, contract, binding)
        result = dict(schema_version='routine-build/1', **image_identity(engine, image_id, binding, contract['service_id']), ci_run=ci_run)
    else:
        # Browser installation is an execution-environment prerequisite. Never
        # install it implicitly during a production switch.
        mode = acceptance_mode(request)
        check_browser(mode)
        image = image_identity(engine, request['image_id'], binding, contract['service_id'])
        if args.phase == 'validate':
            require(request.get('candidate_authorized') is True, 'CANDIDATE_NOT_AUTHORIZED')
            result = candidate_acceptance(backend, image, base_commit=request['base_commit'], ci_run=ci_run, mode=mode)
        elif args.phase in ('candidate-ui', 'production-ui'):
            stage = args.phase.split('-')[0]
            require(mode == 'MANUAL', 'MANUAL_MODE_REQUIRED')
            require(request.get('candidate_authorized' if stage == 'candidate' else 'production_authorized') is True,
                    'MANUAL_STAGE_NOT_AUTHORIZED')
            checkpoint = backend.protected_json(request['checkpoint_record'])
            decision = request['manual_ui']
            result = finish_manual(backend, image, checkpoint, stage=stage, decision=decision['decision'],
                                   operator=decision['operator'], ci_run=ci_run)
        else:
            require(request.get('production_authorized') is True, 'PRODUCTION_NOT_AUTHORIZED')
            acceptance = backend.protected_json(request['acceptance_record'])
            require(acceptance_mode(acceptance) == mode, 'ACCEPTANCE_MODE_CHANGED')
            result = deploy_same_image(backend, image, acceptance, request['previous_release'], ci_run=ci_run)
    engine._write_new(args.output, engine._canonical(result))
    return 0 if result.get('result', 'PASS') == 'PASS' else 2 if result.get('result') == WAITING else 1


if __name__ == '__main__':
    raise SystemExit(main())
