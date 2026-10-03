"""Hosted synthetic-trust exercise of the SAME HIGH_RISK execution entry.

Historical/current application sources get ONLY test public trust and a test
localhost port/project. The tool gets ONLY test public trust. Those disposable
Git identities are reported separately from the admitted candidate, never as
production approval or acceptance. No production key/data is available.
"""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import importlib.machinery
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
import yaml

ROOT = Path(__file__).resolve().parents[2]
PROJECT = 'spread-production-runtime-wiring'
CONTRACT = '02_configs/runtime_contracts/spread-production-runtime.json'
OLD = '88df880127bea4308ee752a37a59884b9198b2ce'
TARGET = '0d7b86ddafbed0e7b063ae1097d7e07ee36e9f00'
REVIEW_CLOCK_ADVANCE = timedelta(0)


class ReviewClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + REVIEW_CLOCK_ADVANCE


class ReviewClockLoader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        super().exec_module(module)
        # Test clock only: the ORIGINAL Review parser and every check still
        # execute. No issuer, signer, grant, Docker or system clock is patched.
        module.datetime = ReviewClock


def install_review_clock():
    original = importlib.util.spec_from_file_location
    def selected(name, location, *args, **kwargs):
        if location is not None and Path(location).name == 'release_reversibility.py':
            kwargs['loader'] = ReviewClockLoader(name, str(location))
        return original(name, location, *args, **kwargs)
    importlib.util.spec_from_file_location = selected
    return original


def load(root, relative, name):
    spec = importlib.util.spec_from_file_location(name, root / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


helpers = load(ROOT, '08_tests/shared/release_canonicalization_docker_e2e.py', '_execution_fixture_helpers')
run, write, directory = helpers.run, helpers.write, helpers.directory


def identity(root):
    return dict(commit=run('git', '-C', str(root), 'rev-parse', 'HEAD'),
                tree=run('git', '-C', str(root), 'rev-parse', 'HEAD^{tree}'))


def ref(path):
    return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def save(root, name, value):
    path = root / name
    write(path, json.dumps(value, sort_keys=True, separators=(',', ':')).encode())
    return path


def clone(work, name, commit, trust, project=None):
    source = work / name
    run('git', 'clone', '--local', '--no-hardlinks', '--no-checkout', str(ROOT), str(source))
    run('git', '-C', str(source), 'checkout', '--detach', commit)
    (source / '02_configs/production_runtime_trust.json').write_text(json.dumps(trust) + '\n', encoding='utf-8')
    changed = ['02_configs/production_runtime_trust.json']
    if project is not None:
        path = source / '09_deploy/spread_runtime/compose.yml'
        compose = yaml.safe_load(path.read_text(encoding='utf-8'))
        compose['name'] = project
        compose['services']['spread-dashboard']['ports'] = ['127.0.0.1:18571:8501']
        path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding='utf-8')
        changed.append('09_deploy/spread_runtime/compose.yml')
    run('git', '-C', str(source), 'add', *changed)
    run('git', '-C', str(source), '-c', 'user.name=Hosted synthetic fixture',
        '-c', 'user.email=fixture@invalid', 'commit', '-m', 'Synthetic trust and isolated port only; NOT release approval')
    assert sorted(run('git', '-C', str(source), 'diff', '--name-only', commit, 'HEAD').splitlines()) == sorted(changed)
    return source


def validate(source, work, key_path, trust, *, ttl_seconds=86400):
    # Formal build + validate + sign producer, never hand-sign copied evidence.
    producer = load(source, '04_scripts/runtime/pre_release_runtime.py', '_initial_producer_' + source.name)
    path = work / (source.name + '-signed-record.json')
    payload = producer.validate_candidate(PROJECT, path, key_path, ttl_seconds=ttl_seconds)
    evidence = payload['evidence']
    parser = load(source, '09_deploy/runtime_identity/candidate_validation_record.py', '_execution_record_' + source.name)
    # The deliberately short-lived target may already have expired while the
    # producer completed its final source-identity checks. Verify its authentic
    # issuance here at the signed issuance instant (test clock only). main()
    # separately requires the ORIGINAL verifier to reject it at real UTC now
    # before preparation. No host clock, signed payload or runtime Gate changes.
    issued = datetime.fromisoformat(payload['issued_at'])
    assert parser.verify_record(path.read_bytes(), trust, now=issued)['evidence'] == evidence
    return evidence, path


def collect_routine_acceptance(source, tool, work, evidence, key_path, base_source):
    """Collect the legacy family through real candidate IO, never copied PASS.

    The original c8 -> 88 delta uses the existing reviewed-Routine contract.
    Approval is explicitly synthetic, never a real Maintainer authorization.
    """
    # A historical acceptance is collected by that application's matching
    # formal collector/validator/issuer, then consumed by the NEW tool below.
    # Mixing the current collector with the historical candidate-only Compose
    # incorrectly applies today's production-secret declaration adapter.
    routine = load(source, '04_scripts/runtime/routine_release.py', '_mixed_routine_collector')
    engine = load(source, '04_scripts/runtime/validate_target_runtime.py', '_mixed_old_engine')
    host = load(source, '09_deploy/runtime_identity/host_authorization.py', '_mixed_old_host')
    _, manifest, binding = engine.source_contract(source, PROJECT, CONTRACT)
    base = identity(base_source)
    run('git', '-C', str(source), 'fetch', str(base_source), base['commit'])
    remote = work / 'original-main.git'
    run('git', 'init', '--bare', str(remote))
    run('git', '-C', str(source), 'remote', 'set-url', 'origin', str(remote))
    run('git', '-C', str(source), 'push', 'origin', 'HEAD:refs/heads/main')
    pre = load(source, '04_scripts/runtime/pre_release_runtime.py', '_original_review_pre')
    review_policy = load(source, '04_scripts/runtime/release_reversibility.py', '_original_review_policy')
    risk = pre.classify_release(source, base['commit'], binding['commit'], PROJECT)
    review = dict(reviewer='SYNTHETIC_TEST_MAINTAINER_NOT_PRODUCTION_APPROVAL',
        timestamp=datetime.now(timezone.utc).isoformat(), base=risk['base'], target=risk['target'],
        authoritative_main=identity(source), machine_findings=review_policy.machine_findings(risk),
        machine_classification=review_policy.machine_classification(risk),
        machine_destructive_evidence=risk['MACHINE_DESTRUCTIVE_EVIDENCE'],
        reviewed_diff_identity=review_policy.reviewed_diff_identity(risk),
        semantic_delta={name: 'NO' for name in review_policy.ROUTINE_SEMANTIC_FACTS},
        maintainer_classification='ROUTINE_STATELESS', reason='Synthetic reproduction of original reviewed Routine delta')
    resolved = routine.require_routine(source, base['commit'], binding['commit'], PROJECT, review)
    image_id = evidence['image_id']
    image = engine.inspect_one('image', image_id)
    uid, gid = engine._numeric_user(image)
    manifest.update(_numeric_uid=uid, _container_user=image['Config']['User'], _runtime_contract=CONTRACT)
    scope = host.create_candidate_scope(engine._runtime_bindings(manifest, uid, gid))
    grants = directory(work / 'grants', 0o755)
    manifest['_grant_dir'] = grants
    session = None
    extraction = None
    namespace = 'routine-candidate-' + uuid.uuid4().hex
    try:
        identity_root = next(item['container_path'] for item in manifest['runtime_roots']
                             if item['role'] == manifest['identity_root_role'])
        identity_source = next(Path(item['source']) for item in scope['mounts'] if item['target'] == identity_root)
        marker = save(identity_source, '.market-data-runtime.json', dict(schema_version=1,
            runtime_id='target-validation', module_id=manifest['module_id'],
            classification='candidate-validation', created_at=datetime.now(timezone.utc).isoformat()))
        os.chmod(marker, 0o444)
        engine._seed_candidate_runtime_inputs(source, manifest, binding, scope, host)
        document = routine.candidate_compose_document(engine, manifest, image_id,
            scope['mounts'], grants, uuid.uuid4().hex, namespace, 18573, 8501)
        compose = save(work, 'compose.json', document)
        env = work / 'compose.env'
        write(env, b'')
        argv = ['docker', 'compose', '--project-directory', str(work), '--env-file', str(env), '-f', str(compose)]
        rendered = host._json(run(*argv, 'config', '--format', 'json').encode())
        # Unstarted throwaway instance supplies immutable image bytes and a
        # policy template. The collector creates and authorizes a NEW instance.
        run(*argv, 'create', '--no-build')
        extraction = run(*argv, 'ps', '-q', '--all', manifest['service_id'])
        container = engine.inspect_one('container', extraction)
        release = host.copy_container_bytes(extraction, '/app/RELEASE.json')
        policy = engine._policy(manifest, binding, image_id, image, container, scope, compose,
            env, host._digest(rendered), hashlib.sha256((source / CONTRACT).read_bytes()).hexdigest(),
            hashlib.sha256(marker.read_bytes()).hexdigest(), hashlib.sha256(release).hexdigest(), host)
        template = save(work, 'policy-template.json', policy)
        run('docker', 'rm', extraction)
        extraction = None
        request = dict(candidate=dict(compose=str(compose), environment=str(env), project_directory=str(work),
            policy_template=str(template), policy_output=str(work/'instance-policy.json'),
            writable_root=scope['candidate_host_root'], grant_directory=str(grants),
            key_path=str(key_path), container_port=8501),
            application_smoke=dict(kind='dom', path='/', selectors={'[data-testid="stAppViewContainer"]': 1}))
        session = routine.DockerSession(request, engine, host, binding, manifest)
        exact = routine.image_identity(engine, image_id, binding, manifest['service_id'])
        record = routine.candidate_acceptance(session, exact, base_commit=base['commit'],
            ci_run=os.environ['GITHUB_RUN_ID'], risk_resolution=resolved, maintainer_risk_review=review)
        path = save(work, 'routine-acceptance.json', record)
        routine.validate_acceptance(record, commit=binding['commit'], tree=binding['tree'], image_id=image_id)
        assert record['result'] == 'PASS' and session.container_id is None
        save(work, 'collector-identity.json', dict(collector=str(source/'04_scripts/runtime/routine_release.py'),
            consumer_tool=identity(tool),
            application=identity(source), image_id=image_id, base_commit=base['commit'],
            evidence_class='HOSTED_SYNTHETIC_ROUTINE_COLLECTOR', record=ref(path)))
        return path
    finally:
        if extraction:
            run('docker', 'rm', '-f', extraction)
        if session is not None and session.container_id:
            session.cleanup()
        if engine._docker('network', 'inspect', namespace+'_default', check=False).returncode == 0:
            run('docker', 'network', 'rm', namespace+'_default')
        candidate_root = Path(scope['candidate_host_root'])
        assert (candidate_root.is_absolute() and not candidate_root.is_symlink()
                and candidate_root.parent.resolve() == Path(host.CANDIDATE_SCOPE_PARENT).resolve()
                and candidate_root.name.startswith('candidate-'+scope['candidate_scope']['scope_id']+'-'))
        shutil.rmtree(candidate_root)
        descriptor = Path(scope['candidate_scope']['descriptor_path'])
        for path in (descriptor, descriptor.with_name(descriptor.name+'.consumed'), grants/'grant.json'):
            path.unlink(missing_ok=True)


def seed_formal_runtime_inputs(manifest, sources, target_source):
    """Keep the existing FORMAL pre-capture state; never retag sealed fixtures.

    Candidate-validation has its own sealed TEST_ISOLATED snapshot. Production
    role fixtures instead exercise the already supported empty snapshot store;
    real capture is not a release prerequisite and is never invoked here.
    """
    prepared = []
    for item in manifest['candidate_runtime_inputs']:
        if item['role'] in {'snapshots', 'capture-snapshots'}:
            continue
        raw = (target_source / item['source_path']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == item['sha256'], item['source_path']
        path = sources[item['role']] / item['relative_path']
        path.parent.mkdir(parents=True, exist_ok=True)
        write(path, raw, 0o444)
        prepared.append(dict(role=item['role'], relative_path=item['relative_path'],
            source_path=item['source_path'], sha256=item['sha256']))
    assert sources['snapshots'] == sources['capture-snapshots']
    assert not any(sources['snapshots'].iterdir())
    return dict(snapshot_state='FORMAL_PRE_CAPTURE_EMPTY', capture_executed=False,
        sealed_candidate_snapshot_retagged=False, inputs=prepared)


def runtime_inputs(work, old_source, target_source, host):
    allocation = Path('/var/lib/market-data/production-runtime') / ('hosted-execution-' + uuid.uuid4().hex)
    allocation.parent.mkdir(parents=True, exist_ok=True)
    directory(allocation, 0o700)
    manifest = json.loads((target_source / CONTRACT).read_bytes())
    sources = {}
    for item in manifest['runtime_roots']:
        role = item['role']
        if role == 'capture-snapshots':
            sources[role] = sources['snapshots']
        else:
            sources[role] = directory(allocation / role, 0o755,
                65532 if item['access'] == 'rw' or role == 'snapshots' else 0)
    helpers.prepare_nested_mountpoints(manifest, sources)
    marker = dict(schema_version=1, runtime_id='target-validation', module_id=manifest['module_id'],
        classification='formal', created_at=datetime.now(timezone.utc).isoformat())
    write(sources['identity'] / '.market-data-runtime.json', host._canonical(marker), 0o444)
    input_evidence = seed_formal_runtime_inputs(manifest, sources, target_source)
    save(work, 'formal-runtime-inputs.json', input_evidence)
    secrets = directory(allocation / 'secrets', 0o700)
    tankan = secrets / 'tankan.env'
    write(tankan, b'HOSTED_TEST_ONLY=1\n', 0o440)
    os.chown(tankan, 0, 65532)
    mapping = {'identity': 'SPREAD_IDENTITY_ROOT', 'data': 'SPREAD_DATA_ROOT', 'weather': 'WEATHER_RUNTIME_CURRENT_DIR',
        'history': 'SPREAD_HISTORY_ROOT', 'snapshots': 'SPREAD_SNAPSHOT_ROOT', 'results': 'SPREAD_RESULT_ROOT',
        'cnf': 'SPREAD_CNF_ROOT', 'outputs': 'SPREAD_OUTPUT_ROOT', 'logs': 'SPREAD_LOG_ROOT',
        'manual-cnf': 'SPREAD_MANUAL_CNF_ROOT', 'am-results': 'SPREAD_AM_RESULT_ROOT'}
    values = {variable: str(sources[role]) for role, variable in mapping.items()}
    values.update(TANKAN_SECRET_FILE=str(tankan), USDA_DASHBOARD_URL='https://hosted-test.invalid/',
                  OIL_WORLD_DASHBOARD_URL='https://hosted-test.invalid/')
    return allocation, sources, values


def instance(work, name, source, evidence, record, host, engine, allocation, sources, values, key_id, key_path):
    manifest = json.loads((source / CONTRACT).read_bytes())
    role_dir = directory(work / name, 0o700)
    grants = directory(allocation / (name + '-grants'))
    env_values = dict(values, SPREAD_IMAGE=evidence['image_id'], SPREAD_GRANT_ROOT=str(grants))
    application_service = 'market-data-service' in manifest['secret_references']
    if application_service:
        credential = allocation / 'secrets' / (name + '-service.json')
        write(credential, b'', 0o440)
        os.chown(credential, 0, 65532)
        env_values['SPREAD_SERVICE_CREDENTIAL_FILE'] = str(credential)
    env = role_dir / 'runtime.env'
    write(env, ''.join(f'{k}={v}\n' for k, v in env_values.items()).encode())
    command = ['docker', 'compose', '--project-directory', str(source), '--env-file', str(env)]
    for relative in manifest['build']['compose_sources']:
        command += ['-f', str(source / relative)]
    rendered = host._json(run(*command, 'config', '--format', 'json').encode())
    rendered['services'][manifest['service_id']].pop('build', None)
    rendered['services'][manifest['service_id']]['hostname'] = uuid.uuid4().hex
    compose = save(role_dir, 'compose.json', rendered)
    compose_cmd = ['docker', 'compose', '--project-directory', str(source), '--env-file', str(env), '-f', str(compose)]
    # Controlled offline extraction proves immutable RELEASE bytes. This does
    # not stand in for the real non-root application acceptance below.
    extraction = run('docker', 'create', '--entrypoint', '/bin/true', evidence['image_id'])
    try:
        release_raw = host.copy_container_bytes(extraction, '/app/RELEASE.json')
    finally:
        run('docker', 'rm', extraction)
    mounts = host._observe('compose_mounts', rendered['services'][manifest['service_id']]['volumes'])
    for item in rendered['services'][manifest['service_id']].get('secrets', []):
        mounts.append(dict(source=rendered['secrets'][item['source']]['file'], target=item['target'], read_only=True))
    mounts.sort(key=lambda item: item['target'])
    policy = dict(schema_version='host-runtime-policy/5', role='production', key_id=key_id,
        project_id=PROJECT, module_id=manifest['module_id'], service_id=manifest['service_id'], runtime_id='target-validation',
        approved_commit=evidence['binding']['commit'], approved_tree=evidence['binding']['tree'], image_id=evidence['image_id'],
        artifact_service=manifest['service_id'], release_application=host._json(release_raw)['application'], source_root='/app',
        runtime_root='/runtime', runtime_manifest_path='/app/' + CONTRACT,
        runtime_manifest_sha256=hashlib.sha256((source / CONTRACT).read_bytes()).hexdigest(),
        runtime_marker_sha256=hashlib.sha256((sources['identity'] / '.market-data-runtime.json').read_bytes()).hexdigest(),
        release_sha256=hashlib.sha256(release_raw).hexdigest(), actual_config_sha256='0' * 64,
        mounts=mounts, compose_sources=[ref(compose)], compose_project_directory=str(source), compose_environment_file=str(env),
        rendered_compose_sha256=host._digest(host._json(run(*compose_cmd, 'config', '--format', 'json').encode())),
        grant_container_directory='/run/market-data-grants', candidate_host_root=None, candidate_scope=None,
        approved_source_root=str(source), production_storage_root=str(allocation), candidate_record=ref(record))
    if application_service:
        policy['application_service'] = dict(service_id=manifest['service_id'], runtime_id='target-validation',
            allowed_writable_roots=[item['container_path'] for item in manifest['runtime_roots'] if item['access'] == 'rw'])
    template = save(role_dir, 'policy-template.json', policy)
    transport = dict(compose=str(compose), environment=str(env), project_directory=str(source), policy_template=str(template),
        policy_output=str(role_dir / 'issued-policy.json'), writable_root=str(allocation), grant_directory=str(grants),
        key_path=str(key_path), container_port=8501)
    document = dict(source_root=str(source), policy=ref(template), transport=transport,
                    application_smoke=dict(kind='dom', path='/', selectors={'[data-testid="stAppViewContainer"]': 1}))
    return save(role_dir, 'instance.json', document), policy


def initialize_baseline_font_cache(work, backend, cid):
    """Finish the source's real lazy cache before recording preservation.

    This executes in the authorized non-root source, not the host or sandbox.
    No cache bytes or later preservation observation are fabricated.
    """
    code = ("import hashlib,json,os,pathlib; import matplotlib.font_manager; "
        "import matplotlib; p=pathlib.Path(matplotlib.get_cachedir()); "
        "print(json.dumps(dict(uid=os.geteuid(),gid=os.getegid(),cache=str(p),"
        "files={f.name:hashlib.sha256(f.read_bytes()).hexdigest() "
        "for f in sorted(p.glob('fontlist-*.json'))})))")
    argv = ['docker', 'exec', cid, 'python', '-B', '-c', code]
    observed = backend.host._json(run(*argv, timeout=120).encode())
    assert observed['uid'] == observed['gid'] == 65532
    assert observed['cache'] == '/runtime/10_logs/matplotlib' and observed['files']
    return save(work, 'baseline-font-cache-initialization.json', dict(
        container_id=cid, argv=argv, observation=observed,
        phase='BEFORE_SOURCE_ACCEPTANCE_AND_PRESERVATION', exit_code=0))


def rehearsal(work, backend, baseline_policy_path, cid, sources, values, production_key):
    host, engine, pre = backend.host, backend.engine, backend.pre
    module = load(backend.host.SOURCE_ROOT, '09_deploy/runtime_identity/recovery_namespace.py', '_execution_rehearsal')
    old = host._load_policy(baseline_policy_path)
    assert 'application_service' not in old  # Historical old image uses its existing path.
    nonce = uuid.uuid4().hex
    root = module.SANDBOX_PARENT / nonce
    module.SANDBOX_PARENT.mkdir(parents=True, exist_ok=True)
    grants = directory(Path(old['production_storage_root']) / ('rehearsal-grants-' + nonce))
    policy = copy.deepcopy(old)
    writable = sorted({m['source'] for m in old['mounts'] if not m['read_only']})
    policy['recovery'] = dict(purpose='recovery-validation', baseline_policy=ref(baseline_policy_path),
        production_container_id=cid, production_observation_sha256=module.production_identity(host, host.docker_inspect(cid)),
        nonce=nonce, project='spread-recovery-' + nonce, container=old['service_id'] + '-recovery-' + nonce,
        host_port=18572, network='spread-recovery-' + nonce + '-net', preserved_store_sources=writable,
        sandbox=dict(mode='isolated-rehearsal', root=str(root), roots=[dict(production_source=s,
            sandbox_source=str(root / ('root-' + str(i))), initialization='COPY_CURRENT_STATE_REQUIRED', uid=65532, gid=65532,
            mode=0o755) for i, s in enumerate(writable)]))
    policy['mounts'] = [dict(m, source=str(grants)) if m['target'] == '/run/market-data-grants' else m
        for m in module.sandbox_mounts(host, policy, old, check_files=False)]
    built = module.build_sandbox(host, policy)
    replacement = {i['production_source']: i['sandbox_source'] for i in policy['recovery']['sandbox']['roots']}
    replacement[next(m['source'] for m in old['mounts'] if m['target'] == '/run/market-data-grants')] = str(grants)
    env_values = {k: replacement.get(v, v) for k, v in values.items()}
    env_values['SPREAD_IMAGE'] = old['image_id']
    env_values['SPREAD_GRANT_ROOT'] = str(grants)
    env = work / 'rehearsal.env'
    write(env, ''.join(f'{k}={v}\n' for k, v in env_values.items()).encode())
    policy['compose_environment_file'] = str(env)
    source = Path(old['approved_source_root'])
    manifest = json.loads((source / CONTRACT).read_bytes())
    command = ['docker', 'compose', '--project-name', policy['recovery']['project'], '--project-directory', str(source), '--env-file', str(env)]
    for relative in manifest['build']['compose_sources']: command += ['-f', str(source / relative)]
    document = module.project_compose(host, host._json(run(*command, 'config', '--format', 'json').encode()), policy)
    document['services'][old['service_id']].pop('build', None)
    document['services'][old['service_id']]['hostname'] = nonce
    compose = save(work, 'rehearsal-compose.json', document)
    command = ['docker', 'compose', '--project-name', policy['recovery']['project'], '--project-directory', str(source), '--env-file', str(env), '-f', str(compose)]
    run(*command, 'create', '--no-build')
    fresh = run(*command, 'ps', '-q', '--all', old['service_id'])
    network = policy['recovery']['network']
    policy['recovery']['expected_network_id'] = backend.network_observation(network)['Id']
    observed = host.normalize_observation(host.docker_inspect(fresh), host.docker_image_inspect(old['image_id']), host.copy_container_json(fresh))
    policy.update(actual_config_sha256=observed['actual_config_sha256'], compose_sources=[ref(compose)],
        compose_project_directory=str(source), rendered_compose_sha256=host._digest(host._json(run(*command, 'config', '--format', 'json').encode())))
    path = save(work, 'rehearsal-policy.json', policy)
    pre.revalidate_production(fresh, path, work / 'rehearsal-pre-release.json')
    envelope = host.issue_execution_grant(fresh, expected_policy_path=path, key_path=production_key,
        grant_path=grants / 'grant.json', grant_dir=grants, role='production')
    grant = save(work, 'rehearsal-grant-evidence.json', envelope)
    run('docker', 'start', fresh)
    host.validate_recovery_post_start(fresh, expected_policy_path=path, grant_path=grants / 'grant.json', key_path=production_key)
    c = host.docker_inspect(fresh)
    instance = save(work, 'rehearsal-instance.json', dict(container_id=fresh, image_id=c['Image'], hostname=c['Config']['Hostname'], created_at=c['Created'], started_at=c['State']['StartedAt']))
    # Execute formal readonly consumers, real health and real HTTP before sealing.
    raw_pre = run('docker', 'exec', fresh, 'python', '-B', '/app/04_scripts/runtime/spread_runtime_preflight.py', '--identity-kind', 'oci_container', timeout=120)
    ready = load(backend.host.SOURCE_ROOT, '09_deploy/spread_release/wait_for_service_ready.py', '_execution_rehearsal_ready')
    raw_health = ready.wait_for_service_ready(runtime=ready.DockerCurlRuntime(), container=fresh,
        health_url='http://127.0.0.1:18572/_stcore/health', expected_image_id=c['Image'], initial_restart_count=0,
        policy=ready.DEFAULT_READINESS_POLICY)
    raw_http = run('curl', '--fail', '--silent', '--show-error', 'http://127.0.0.1:18572/')
    raw_data = dict(sandbox_preservation=dict(production_before=built['production_before'],
        production_after={s: module.tree_identity(host, s) for s in writable}, sandbox_before=built['sandbox_initial'],
        sandbox_after={i['sandbox_source']: module.tree_identity(host, i['sandbox_source']) for i in policy['recovery']['sandbox']['roots']}))
    probes = {}
    for name, value in dict(preflight=raw_pre, health=raw_health, consumer=raw_http, data_unchanged=raw_data).items():
        raw = save(work, name + '-raw.json', value)
        probe = dict(container_id=fresh, commit=old['approved_commit'], tree=old['approved_tree'], image_id=c['Image'], exit_code=0, status='PASS', raw=ref(raw))
        probes[name] = ref(save(work, name + '-probe.json', probe))
    return dict(instance=ref(instance), grant=ref(grant), **probes), ref(path), fresh, root, network


def case(work, tool, old_source, target_source, old_evidence, target_evidence, old_record, target_record,
         production_key_id, production_key, *, injection='NONE', candidate_key_path=None):
    global REVIEW_CLOCK_ADVANCE
    REVIEW_CLOCK_ADVANCE = timedelta(0)
    execution = load(tool, '09_deploy/spread_release/high_risk_execution.py', '_execution_entry_' + work.name)
    backend = execution.HostBackend(directory(work / 'baseline-receipts', 0o700))
    allocation, sources, values = runtime_inputs(work, old_source, target_source, backend.host)
    asset_old = dict(identity(old_source), image_id=old_evidence['image_id'])
    asset_target = dict(identity(target_source), image_id=target_evidence['image_id'])
    baseline_spec, _ = instance(work, 'baseline', old_source, old_evidence, old_record, backend.host, backend.engine,
        allocation, sources, values, production_key_id, production_key)
    seed = dict(primary_rollback=asset_old, instances=dict(primary_rollback=ref(baseline_spec)), project_id=PROJECT,
                policy=dict(start_timeout_seconds=60, readiness=execution.contract().DEFAULT_READINESS_POLICY))
    backend._prepare_session(seed, 'primary_rollback')
    backend.create(seed, 'primary_rollback')
    backend.authorize(seed, 'primary_rollback')
    backend.start(seed, 'primary_rollback')
    initialize_baseline_font_cache(work, backend, backend.sessions['primary_rollback'].container_id)
    backend.accept(seed, 'primary_rollback')
    baseline = backend.sessions['primary_rollback']
    cid = baseline.container_id
    running = backend.host.docker_inspect(cid)
    baseline_policy = copy.deepcopy(baseline.current_policy)
    baseline_policy['actual_config_sha256'] = backend.host.normalize_observation(running,
        backend.host.docker_image_inspect(running['Image']), backend.host.copy_container_json(cid))['actual_config_sha256']
    baseline_path = save(work, 'baseline-running-policy.json', baseline_policy)
    deployment_path = None
    aged_now = datetime.now(timezone.utc) + timedelta(hours=48)
    if json.loads(old_record.read_bytes())['schema_version'] == 'routine-candidate-acceptance/1':
        # The running baseline was created/authorized/started/accepted above by
        # the formal executor, with a fresh production-domain test grant. Use
        # the existing manual finalizer to collect that exact actual instance.
        old_routine = load(old_source, '04_scripts/runtime/routine_release.py', '_original_deployment_collector')
        baseline.spec['policy_output'] = str(baseline_path)
        baseline.request['production'] = baseline.spec
        record = json.loads(old_record.read_bytes())
        image = old_routine.image_identity(backend.engine, asset_old['image_id'], baseline.binding,
                                          baseline.contract['service_id'])
        checkpoint = dict(schema_version='routine-deployment-result/1', **image, ci_run=record['ci_run'],
            ui_acceptance_mode='MANUAL', stage='production', result=old_routine.WAITING,
            candidate_acceptance='PASS', deployed_at=running['Created'],
            previous_release=dict(commit=record['base_commit']), instance=baseline.checkpoint(),
            runtime_preflight=baseline.preflight(), production_health=baseline.health(), http=baseline.http())
        deployed = old_routine.finish_manual(baseline, image, checkpoint, stage='production',
            decision='PASS', operator='SYNTHETIC_TEST_MAINTAINER_NOT_PRODUCTION_APPROVAL', ci_run=record['ci_run'])
        assert deployed['result'] == 'PASS' and deployed['production_smoke'] == 'PASS'
        deployment_path = save(work, 'original-accepted-deployment.json', deployed)
        # Control ONLY current Review evaluation. Original timestamps, Docker
        # clocks and fresh execution-grant issuance/expiry use real UTC time.
        original_policy = load(old_source, '04_scripts/runtime/release_reversibility.py', '_aged_original_review_policy')
        original_pre = load(old_source, '04_scripts/runtime/pre_release_runtime.py', '_aged_original_pre')
        original_risk = original_pre.classify_release(old_source, record['base_commit'], record['commit'], PROJECT)
        try:
            original_policy.apply_review(original_risk, record['maintainer_risk_review'], identity(old_source), now=aged_now)
        except ValueError as exc:
            assert str(exc) == 'INVALID_RISK_REVIEW_TIME'
        else:
            raise AssertionError('Old Review unexpectedly fresh at the controlled current clock')

    evidence, recovery_policy, fresh, sandbox, network = rehearsal(work, backend, baseline_path, cid, sources, values, production_key)
    REVIEW_CLOCK_ADVANCE = timedelta(hours=48) if deployment_path else timedelta(0)
    aged_now = ReviewClock.now(timezone.utc)
    if deployment_path:
        save(work, 'review-clock.json', dict(scope='ALL_ORIGINAL_AND_CURRENT_REVIEW_PARSERS_ONLY',
            advance_hours=48, original_review=record['maintainer_risk_review']['timestamp'],
            original_acceptance=record['validated_at'], current_review_clock=aged_now.isoformat(),
            execution_grant_clock='REAL_UTC_UNCHANGED', docker_clock='REAL_UTC_UNCHANGED'))
    target_spec, _ = instance(work, 'target', target_source, target_evidence, target_record, backend.host, backend.engine,
        allocation, sources, values, production_key_id, production_key)
    rollback_spec, _ = instance(work, 'rollback', old_source, old_evidence, old_record, backend.host, backend.engine,
        allocation, sources, values, production_key_id, production_key)
    artifact = dict(commit=asset_old['commit'], tree=asset_old['tree'], image_id=asset_old['image_id'])
    old_asset = dict(artifact, image_location='LOCAL_IMAGE_ONLY', registry_digest=None,
        release=ref(save(work, 'source-release.json', dict(git_commit=asset_old['commit'], git_tree=asset_old['tree'],
            image_id=asset_old['image_id'], release_id=backend.host.copy_container_json(cid)['release_id'],
            **({'accepted_deployment': ref(deployment_path)} if deployment_path else {})))),
        runtime_config=ref(save(work, 'source-config.json', dict(artifact=artifact,
            namespace=dict(compose_project=running['Config']['Labels']['com.docker.compose.project'], container_name='spread-dashboard', host_ports=[18571]),
            compose=ref(Path(baseline.spec['compose'])), environment=ref(Path(baseline.spec['environment']))))),
        data_schema=ref(save(work, 'source-schema.json', dict(schema_identity='HOSTED_SYNTHETIC_DATA_ONLY', assets=[]))),
        procedure=ref(save(work, 'source-procedure.json', dict(artifact=artifact, fresh_instance_required=True,
            fresh_grant_required=True, steps=['same verified-plan executor', 'fresh old production instance', 'accept restored UI']))))
    base, target = identity(old_source), identity(target_source)
    recovery_record = dict(schema_version='production-recovery-observation/1', base=base, target=target,
        old_image_id=asset_old['image_id'], runtime_config_sha256=old_asset['runtime_config']['sha256'],
        data_schema_sha256=old_asset['data_schema']['sha256'], method='fresh-recovery',
        observed_at=datetime.now(timezone.utc).isoformat(), result='PASS', evidence=evidence, recovery_policy=recovery_policy)
    recovery_path = save(work, 'recovery-observation.json', recovery_record)
    risk = backend.pre.classify_release(tool, base['commit'], target['commit'], PROJECT)
    review_module = load(tool, '04_scripts/runtime/release_reversibility.py', '_execution_fixture_review')
    review = dict(reviewer='SYNTHETIC_TEST_MAINTAINER_NOT_PRODUCTION_APPROVAL',
        timestamp=(aged_now if deployment_path else datetime.now(timezone.utc)).isoformat(), base=base, target=target,
        authoritative_main=identity(tool), machine_findings=review_module.machine_findings(risk),
        maintainer_classification='HIGH_RISK', reason='Isolated Hosted fixture only; no human/production approval claimed')
    request = dict(schema_version='production-release-request/1', project_id=PROJECT, current=old_asset, previous=old_asset,
        target_commit=asset_target['commit'], target_source_root=str(target_source), candidate_record=ref(target_record),
        state_plan=ref(save(work, 'state-plan.json', dict(base=base, target=target,
            data_schema_sha256=old_asset['data_schema']['sha256'], irreversible='NO', compatible=True,
            database_migration=False, production_data_mutation=False, storage_format_change=False))),
        acceptance_plan=ref(save(work, 'acceptance-plan.json', dict(target=target, image_id=asset_target['image_id'],
            checks=['formal readonly preflight', 'health', 'HTTP', 'DOM', 'sealed observation']))),
        recovery_evidence=ref(recovery_path), maintainer_risk_review=review)
    request_path = save(work, 'release-request.json', request)
    plan = dict(schema_version='1.8.0', release_id='spread-20260930-' + asset_target['commit'][:12] + '-b01',
        git_commit=asset_target['commit'], git_tree=asset_target['tree'], candidate_image_id=asset_target['image_id'],
        formal_containers={}, tool=identity(tool), source=asset_old, target=asset_target, primary_rollback=asset_old,
        project_id=PROJECT, source_container_id=cid, source_policy=ref(baseline_path), release_request=ref(request_path),
        instances=dict(target=ref(target_spec), primary_rollback=ref(rollback_spec)),
        lock_file='/var/lib/market-data/production-runtime/spread-release.lock', policy=dict(readiness=copy.deepcopy(execution.contract().DEFAULT_READINESS_POLICY),
            observation_seconds=2, poll_interval_seconds=1, consecutive_failures=2, stop_timeout_seconds=10,
            start_timeout_seconds=60, rollback_timeout_seconds=180, rollback_on=['create_failure', 'authorization_failure',
                'start_failure', 'acceptance_failure', 'runtime_identity_failure', 'health_failure_threshold', 'observation_ended_unhealthy']))
    plan_path = work / 'deployment_plan.json'
    preparation = execution.HostBackend(directory(work / 'preparation-receipts', 0o700))
    # SAME safe preparation entry used by create_deployment_plan --high-risk-input.
    # Expired target input is revalidated against the existing image before any
    # stop; later cases consume the fresh record without repeating its build.
    original_record_bytes = target_record.read_bytes()
    original_rollback_bytes = old_record.read_bytes()
    plan = execution.prepare_plan(plan, plan_path, preparation, candidate_key=candidate_key_path)
    assert target_record.read_bytes() == original_record_bytes
    assert old_record.read_bytes() == original_rollback_bytes
    prepared_request = preparation.read(plan['release_request'])
    refreshed_record = prepared_request['candidate_record']
    assert preparation.host.docker_image_inspect(asset_target['image_id'])['Id'] == asset_target['image_id']
    refresh_outcomes = json.loads((preparation.output / 'candidate-refresh-result.json').read_bytes())
    if json.loads(original_rollback_bytes)['schema_version'] == 'routine-candidate-acceptance/1':
        assert refresh_outcomes['primary_rollback'] == dict(reference=ref(old_record), action='REUSED',
            family='routine-candidate-acceptance/1', trust_source='protected-file',
            signature_verified=False, validation_attempts=0)
        rollback_policy = preparation.read(preparation.read(plan['instances']['primary_rollback'])['policy'])
        assert rollback_policy['candidate_record'] == ref(old_record)
    audits = list(preparation.output.rglob('image-validation-execution.json'))
    if refresh_outcomes['target']['action'] == 'REVALIDATED':
        assert len(audits) == 1
        audit = json.loads(audits[0].read_bytes())
        assert audit == dict(mode='VALIDATE_EXISTING_IMAGE', requested_image_id=asset_target['image_id'],
                             docker_build_invocations=0, status='PASS')
        fresh_payload = json.loads(Path(refreshed_record['path']).read_bytes())['payload']
        assert fresh_payload['record_id'] != json.loads(original_record_bytes)['payload']['record_id']
    else:
        assert refresh_outcomes['target']['action'] == 'REUSED' and not audits
    negatives = {}
    if injection == 'NONE':
        for name in ('missing', 'tamper', 'missing_observation', 'wrong_image'):
            scope = directory(work / ('negative-' + name), 0o700)
            other_path = scope / 'deployment_plan.json'
            if name == 'tamper':
                execution.seal_plan(plan, other_path)
                other_path.write_bytes(other_path.read_bytes() + b' ')
            elif name == 'missing_observation':
                bad = copy.deepcopy(plan)
                del bad['policy']['observation_seconds']
                try:
                    execution.seal_plan(bad, other_path)
                except (execution.ExecutionError, execution.contract().ContractError):
                    negatives[name] = 'PASS'
                    assert backend.host.docker_inspect(cid)['State']['Running']
                    continue
                raise AssertionError('Missing observation parameters were accepted')
            elif name == 'wrong_image':
                bad = copy.deepcopy(plan)
                bad['target']['image_id'] = bad['candidate_image_id'] = 'sha256:' + '0' * 64
                execution.seal_plan(bad, other_path)
            try:
                negative_backend = execution.HostBackend(directory(scope / 'receipt', 0o700))
                rejected = execution.execute_verified_plan(other_path, negative_backend)
                assert name == 'wrong_image' and rejected['result'] == 'FAIL'
                assert rejected['source'] == 'PRESERVED_NOT_STOPPED'
            except execution.ExecutionError:
                assert name in {'missing', 'tamper'}
            assert backend.host.docker_inspect(cid)['State']['Running']
            negatives[name] = 'PASS'
    # This subclass ONLY injects a real Docker stop after the target started.
    # The actual grant, startup, verifier, health and rollback implementations
    # are untouched; no helper PASS or mock transport supplies acceptance.
    class InjectedFailure(execution.HostBackend):
        def accept(self, current_plan, role):
            if role == 'target':
                c = self.host.docker_inspect(self.sessions[role].container_id)
                assert c['State']['Running'] and self.envelopes[role]['payload']['container_id'] == c['Id']
                self.engine._docker('stop', self.sessions[role].container_id)
            return super().accept(current_plan, role)
    class AuthorizationFailure(execution.HostBackend):
        def authorize(self, current_plan, role):
            if role != 'target':
                return super().authorize(current_plan, role)
            session = self.sessions[role]
            original_key = session.spec['key_path']
            session.spec['key_path'] = str(candidate_key_path)
            try:
                return super().authorize(current_plan, role)
            except Exception:
                actual = self.host.docker_inspect(session.container_id)
                assert actual['State']['Status'] == 'created' and not actual['State']['Running']
                save(work, 'unauthorized-instance-state.json', dict(container_id=actual['Id'],
                    image_id=actual['Image'], state=actual['State'], started=False))
                raise
            finally:
                session.spec['key_path'] = original_key
    backend_type = {'NONE': execution.HostBackend, 'POST_START': InjectedFailure, 'AUTHORIZATION': AuthorizationFailure}[injection]
    actual_backend = backend_type(directory(work / 'execution-receipts', 0o700))
    try:
        result = execution.execute_verified_plan(plan_path, actual_backend)
        if injection != 'NONE':
            assert result['result'] == 'FAIL' and result['rollback'] == 'PASS'
            assert actual_backend.sessions['primary_rollback'].container_id != cid
            assert (actual_backend.envelopes['primary_rollback']['payload']['grant_id'] !=
                    backend.envelopes['primary_rollback']['payload']['grant_id'])
            assert (actual_backend.envelopes['primary_rollback']['payload']['grant_id'] !=
                    json.loads(Path(evidence['grant']['path']).read_bytes())['payload']['grant_id'])
        else:
            assert result['result'] == 'SUCCESS' and result['target'] == 'PASS'
        if injection == 'AUTHORIZATION':
            assert 'target_start' not in result['steps']
            assert json.loads((work / 'unauthorized-instance-state.json').read_bytes())['started'] is False
        assert not sandbox.exists()
        return dict(status='PASS', path=injection,
                    tool=identity(tool), old=asset_old, target=asset_target,
                    plan=ref(plan_path), result=ref(actual_backend.output / 'execution-result.json'),
                    candidate_record=refreshed_record,
                    refresh_outcomes=ref(preparation.output / 'candidate-refresh-result.json'),
                    image_validation_execution=[ref(path) for path in audits],
                    negative_probes=negatives, temporary_resource_cleanup='PASS', production_acceptance='NOT_EXECUTED')
    finally:
        # Preserve evidence first. Cleanup names only instances created here.
        for candidate in {cid, fresh, *(s.container_id for s in actual_backend.sessions.values())} - {None}:
            present = backend.engine._docker('inspect', candidate, check=False)
            if present.returncode == 0:
                c = backend.host.docker_inspect(candidate)
                assert c['Image'] in {asset_old['image_id'], asset_target['image_id']}
                run('docker', 'rm', '-f', candidate)
        for candidate in (network,):
            if backend.engine._docker('network', 'inspect', candidate, check=False).returncode == 0:
                run('docker', 'network', 'rm', candidate)
        # Never remove application images. Fixture service credentials are
        # temporary and not evidence; retain all nonsecret formal receipts.
        for credential in (allocation / 'secrets').glob('*-service.json'):
            credential.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert sys.platform == 'linux' and os.geteuid() == 0 and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
    work = directory(Path('/root') / ('hosted-high-risk-execution-' + uuid.uuid4().hex), 0o700)
    candidate = identity(ROOT)
    keys, public = {}, []
    key_root = Path('/etc/market-data/runtime-identity')
    key_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = []
    receipt = dict(status='STARTED', candidate=candidate, evidence_class='HOSTED_SYNTHETIC_TRUST_NOT_PRODUCTION_CUTOVER',
                   production_acceptance='NOT_EXECUTED', application_target_rebuilt=False)
    original_review_loader = install_review_clock()
    try:
        for role in ('production', 'candidate_validation'):
            key = Ed25519PrivateKey.generate()
            key_id = 'hosted-execution-' + role.replace('_', '-') + '-' + uuid.uuid4().hex
            path = key_root / (key_id + '.pem')
            write(path, key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
            paths.append(path)
            keys[role] = key_id, key, path
            public.append(dict(key_id=key_id, domain=role, algorithm='ed25519',
                public_key_base64=base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode('ascii')))
        trust = dict(schema_version='production-runtime-trust/1', keys=public, revoked_key_ids=[], revoked_grant_ids=[])
        project = 'hosted-high-risk-' + uuid.uuid4().hex
        old_source = clone(work, 'old-source', OLD, trust, project)
        original_base = clone(work, 'original-base', 'c8d83d60c5b9a1e99148beecbaa13b68045d65fa', trust, project)
        target_source = clone(work, 'target-source', TARGET, trust, project)
        tool = clone(work, 'tool-source', candidate['commit'], trust)
        for source in (old_source, target_source):
            run('git', '-C', str(tool), 'fetch', str(source), identity(source)['commit'])
        # Current-main freshness is checked against a protected disposable local
        # remote; no actual repository main/ref or human approval is changed.
        remote = work / 'fixture-main.git'
        run('git', 'init', '--bare', str(remote))
        run('git', '-C', str(tool), 'remote', 'set-url', 'origin', str(remote))
        run('git', '-C', str(tool), 'push', 'origin', 'HEAD:refs/heads/main')
        candidate_key_path = keys['candidate_validation'][2]
        old_evidence, old_record = validate(old_source, work, candidate_key_path, trust)
        routine_record = collect_routine_acceptance(old_source, tool,
            directory(work/'routine-collector', 0o700), old_evidence, candidate_key_path, original_base)
        target_evidence, target_record = validate(target_source, work, candidate_key_path, trust, ttl_seconds=1)
        expired_bytes = target_record.read_bytes()
        expired = json.loads(expired_bytes)['payload']
        time.sleep(max(0, (datetime.fromisoformat(expired['expires_at']) - datetime.now(timezone.utc)).total_seconds()) + 0.1)
        old_parser = load(target_source, '09_deploy/runtime_identity/candidate_validation_record.py', '_old_expiry_contract')
        try:
            old_parser.verify_record(expired_bytes, trust)
        except old_parser.CandidateValidationRecordError as exc:
            assert str(exc) == 'candidate validation record is not currently valid'
        else:
            raise AssertionError('Original expired record unexpectedly accepted')
        receipt['expired_record'] = dict(reference=ref(target_record), record_id=expired['record_id'],
            issued_at=expired['issued_at'], expires_at=expired['expires_at'], original_verifier_rejected=True)
        receipt['synthetic_assets'] = dict(tool=identity(tool), old=identity(old_source), target=identity(target_source),
            old_image=old_evidence['image_id'], target_image=target_evidence['image_id'])
        receipt['paths'] = []
        for name, injected in (('success', 'NONE'), ('failure', 'POST_START'), ('authorization', 'AUTHORIZATION')):
            scope = directory(work / name, 0o700)
            key_id, _, key_path = keys['production']
            receipt['paths'].append(case(scope, tool, old_source, target_source, old_evidence, target_evidence,
                routine_record if injected != 'AUTHORIZATION' else old_record,
                target_record, key_id, key_path, injection=injected, candidate_key_path=keys['candidate_validation'][2]))
            target_record = Path(receipt['paths'][-1]['candidate_record']['path'])
        assert (work / 'target-source-signed-record.json').read_bytes() == expired_bytes
        receipt['status'] = 'PASS'
    except Exception as exc:
        receipt.update(status='FAIL', failure_type=type(exc).__name__, failure=str(exc)[-1800:])
        raise
    finally:
        importlib.util.spec_from_file_location = original_review_loader
        for path in paths:
            path.unlink()
        receipt['temporary_private_keys_removed'] = all(not path.exists() for path in paths)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        public_evidence = args.output.with_suffix('')
        public_evidence.mkdir(mode=0o755)
        receipt['evidence_index'] = []
        for scope_name in ('success', 'failure', 'authorization', 'routine-collector'):
            scope = work / scope_name
            if not scope.exists():
                continue
            for path in sorted(p for p in scope.rglob('*') if p.is_file() and
                               (p.suffix == '.json' or p.name in {'engine.stdout', 'engine.stderr'})):
                assert not path.is_symlink()
                relative = path.relative_to(work)
                destination = public_evidence / relative
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                raw = path.read_bytes()
                assert b'-----BEGIN PRIVATE KEY-----' not in raw
                write(destination, raw, 0o644)
                receipt['evidence_index'].append(dict(original_path=str(path), artifact_path=str(relative),
                    sha256=hashlib.sha256(raw).hexdigest()))
        for path in (work / 'old-source-signed-record.json', work / 'target-source-signed-record.json'):
            if path.exists():
                raw = path.read_bytes()
                write(public_evidence / path.name, raw, 0o644)
                receipt['evidence_index'].append(dict(original_path=str(path), artifact_path=path.name,
                    sha256=hashlib.sha256(raw).hexdigest()))
        write(args.output, (json.dumps(receipt, sort_keys=True) + '\n').encode(), 0o644)


if __name__ == '__main__':
    main()
