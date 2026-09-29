"""Permanent Hosted release boundary exercise with a disposable test-domain repo.

The synthetic repo differs ONLY in public test trust, committed before build.
All formal source, signing, Compose, mount and post-start validators run intact.
No production key/data/approval is available. Its synthetic SHA is NOT candidate
admission identity; both are recorded, and normal candidate validation is separate.
"""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

ROOT = Path(__file__).resolve().parents[2]
PROJECT = 'spread-production-runtime-wiring'
CONTRACT = '02_configs/runtime_contracts/spread-production-runtime.json'
UID = GID = 65532


def run(*args, timeout=120):
    result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        # CLI outputs may include configuration; publish only bounded stderr.
        raise RuntimeError(f'{args[:3]} failed ({result.returncode}): ' + result.stderr.decode('utf-8', 'replace')[-1000:])
    return result.stdout.decode('utf-8', 'strict').strip()


def load(root, relative, name):
    spec = importlib.util.spec_from_file_location(name, root / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write(path, raw, mode=0o600):
    with path.open('xb') as stream:
        stream.write(raw)
    path.chmod(mode)


def directory(path, mode=0o755, uid=0):
    path.mkdir(mode=mode)
    os.chown(path, uid, GID if uid else 0)
    path.chmod(mode)
    return path


def exercise(work, receipt):
    source = work / 'test-source'
    candidate = run('git', '-C', str(ROOT), 'rev-parse', 'HEAD')
    run('git', 'clone', '--local', '--no-hardlinks', '--no-checkout', str(ROOT), str(source))
    run('git', '-C', str(source), 'checkout', '--detach', candidate)
    keys, public = {}, []
    for role in ('production', 'candidate_validation'):
        key = Ed25519PrivateKey.generate()
        key_id = 'hosted-test-' + role.replace('_', '-')
        path = work / (key_id + '.pem')
        write(path, key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        keys[role] = (key_id, key, path)
        public.append(dict(key_id=key_id, domain=role, algorithm='ed25519',
            public_key_base64=base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode('ascii')))
    trust = dict(schema_version='production-runtime-trust/1', keys=public,
                 revoked_key_ids=[], revoked_grant_ids=[])
    trust_path = source / '02_configs/production_runtime_trust.json'
    # This is a disposable test repository, never the candidate/production trust.
    trust_path.write_text(json.dumps(trust, sort_keys=True) + '\n', encoding='utf-8')
    run('git', '-C', str(source), 'add', '02_configs/production_runtime_trust.json')
    run('git', '-C', str(source), '-c', 'user.name=Hosted fixture', '-c', 'user.email=fixture@invalid',
        'commit', '-m', 'Synthetic test public trust only; not candidate or production authority')
    assert run('git', '-C', str(source), 'diff', '--name-only', candidate, 'HEAD').splitlines() == ['02_configs/production_runtime_trust.json']
    receipt.update(candidate_commit=candidate, synthetic_commit=run('git', '-C', str(source), 'rev-parse', 'HEAD'),
        synthetic_tree=run('git', '-C', str(source), 'rev-parse', 'HEAD^{tree}'),
        evidence_class='HOSTED_SYNTHETIC_TRUST_NOT_PRODUCTION_REHEARSAL')
    evidence_path = work / 'target-evidence.json'
    run(sys.executable, '-I', '-B', str(source / '04_scripts/runtime/validate_target_runtime.py'),
        '--project', PROJECT, '--runtime-contract', CONTRACT, '--ephemeral-candidate-trust',
        '--evidence-output', str(evidence_path), timeout=3900)
    evidence = json.loads(evidence_path.read_bytes())
    assert evidence['TARGET_RUNTIME_CONTAINER_VALIDATION'] == 'PASS'
    record = load(source, '09_deploy/runtime_identity/candidate_validation_record.py', '_canonicalization_record')
    now = datetime.now(timezone.utc)
    payload = dict(record_id=uuid.uuid4().hex, purpose='target-runtime-validation',
        authorization_role='candidate_validation', issued_at=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(), evidence=evidence,
        evidence_sha256=hashlib.sha256(record.canonical(evidence)).hexdigest())
    record.validate_payload(payload)
    envelope = dict(schema_version='candidate-validation-record/1', algorithm='ed25519',
        key_id=keys['candidate_validation'][0], payload=payload)
    envelope['signature'] = base64.b64encode(keys['candidate_validation'][1].sign(record.canonical(envelope))).decode('ascii')
    record_raw = record.canonical(envelope)
    assert record.verify_record(record_raw, trust) == payload
    record_path = work / 'signed-candidate-record.json'
    write(record_path, record_raw)
    host = load(source, '09_deploy/runtime_identity/host_authorization.py', '_canonicalization_host')
    engine = load(source, '04_scripts/runtime/validate_target_runtime.py', '_canonicalization_engine')
    recovery = load(source, '09_deploy/runtime_identity/recovery_namespace.py', '_canonicalization_recovery')
    manifest = json.loads((source / CONTRACT).read_bytes())
    image_id = evidence['image_id']
    image = host.docker_image_inspect(image_id)  # real strict adapter, not a mock
    receipt['image_adapter'] = dict(status='PASS', image_id=image['Id'])
    allocation = Path('/var/lib/market-data/production-runtime') / ('hosted-canonicalization-' + uuid.uuid4().hex)
    allocation.parent.mkdir(parents=True, exist_ok=True)
    directory(allocation, 0o700)
    sources = {}
    for item in manifest['runtime_roots']:
        role = item['role']
        if role == 'capture-snapshots':
            sources[role] = sources['snapshots']
        else:
            sources[role] = directory(allocation / role, 0o755, UID if item['access'] == 'rw' or role == 'snapshots' else 0)
    marker = dict(schema_version=1, runtime_id='target-validation', module_id=manifest['module_id'],
        classification='formal', created_at=now.isoformat())
    write(sources['identity'] / '.market-data-runtime.json', host._canonical(marker), 0o444)
    for item in manifest['candidate_runtime_inputs']:
        path = sources[item['role']] / item['relative_path']
        path.parent.mkdir(parents=True, exist_ok=True)
        write(path, (source / item['source_path']).read_bytes(), 0o444)
    secret_dir = directory(allocation / 'secrets', 0o700)
    secrets = {}
    for name in ('tankan-reader', 'market-data-service'):
        path = secret_dir / name
        write(path, b'HOSTED_TEST_ONLY=1\n' if name == 'tankan-reader' else b'', 0o440)
        os.chown(path, 0, GID)
        secrets[name] = path
    baseline_grants = directory(allocation / 'grants')
    values = {'SPREAD_IMAGE': image_id, 'SPREAD_IDENTITY_ROOT': str(sources['identity']),
        'SPREAD_DATA_ROOT': str(sources['data']), 'WEATHER_RUNTIME_CURRENT_DIR': str(sources['weather']),
        'SPREAD_HISTORY_ROOT': str(sources['history']), 'SPREAD_SNAPSHOT_ROOT': str(sources['snapshots']),
        'SPREAD_RESULT_ROOT': str(sources['results']), 'SPREAD_CNF_ROOT': str(sources['cnf']),
        'SPREAD_OUTPUT_ROOT': str(sources['outputs']), 'SPREAD_LOG_ROOT': str(sources['logs']),
        'SPREAD_MANUAL_CNF_ROOT': str(sources['manual-cnf']), 'SPREAD_AM_RESULT_ROOT': str(sources['am-results']),
        'SPREAD_GRANT_ROOT': str(baseline_grants), 'TANKAN_SECRET_FILE': str(secrets['tankan-reader']),
        'SPREAD_SERVICE_CREDENTIAL_FILE': str(secrets['market-data-service']),
        'USDA_DASHBOARD_URL': 'https://hosted-test.invalid/', 'OIL_WORLD_DASHBOARD_URL': 'https://hosted-test.invalid/'}
    env = work / 'runtime.env'
    write(env, ''.join(f'{k}={v}\n' for k, v in values.items()).encode())
    command = ['docker', 'compose', '--project-directory', str(source), '--env-file', str(env)]
    for relative in manifest['build']['compose_sources']:
        command += ['-f', str(source / relative)]
    desired = host._json(run(*command, 'config', '--format', 'json').encode())
    baseline_compose = copy.deepcopy(desired)
    baseline_compose['services'][manifest['service_id']].pop('build', None)
    baseline_compose['services'][manifest['service_id']]['hostname'] = uuid.uuid4().hex
    baseline_file = work / 'baseline-compose.json'
    write(baseline_file, host._canonical(baseline_compose))
    compose_cmd = ['docker', 'compose', '--project-directory', str(work), '--env-file', str(env), '-f', str(baseline_file)]
    cid = fresh = network = None
    try:
        run(*compose_cmd, 'create', '--no-build')
        cid = run(*compose_cmd, 'ps', '-q', '--all', manifest['service_id'])
        assert re.fullmatch(r'[0-9a-f]{64}', cid)
        container = host.docker_inspect(cid)
        release_raw = host.copy_container_bytes(cid, '/app/RELEASE.json')
        observed = host.normalize_observation(container, image, host._json(release_raw))
        rendered = host._json(run(*compose_cmd, 'config', '--format', 'json').encode())
        policy = dict(schema_version='host-runtime-policy/5', role='production', key_id=keys['production'][0],
            project_id=PROJECT, module_id=manifest['module_id'], service_id=manifest['service_id'],
            runtime_id='target-validation', approved_commit=receipt['synthetic_commit'], approved_tree=receipt['synthetic_tree'],
            image_id=image_id, artifact_service=manifest['service_id'], release_application=host._json(release_raw)['application'],
            source_root='/app', runtime_root='/runtime', runtime_manifest_path='/app/' + CONTRACT,
            runtime_manifest_sha256=hashlib.sha256((source / CONTRACT).read_bytes()).hexdigest(),
            runtime_marker_sha256=hashlib.sha256((sources['identity'] / '.market-data-runtime.json').read_bytes()).hexdigest(),
            release_sha256=hashlib.sha256(release_raw).hexdigest(), actual_config_sha256=observed['actual_config_sha256'],
            mounts=observed['mounts'], compose_sources=[dict(path=str(baseline_file), sha256=hashlib.sha256(baseline_file.read_bytes()).hexdigest())],
            compose_project_directory=str(work), compose_environment_file=str(env), rendered_compose_sha256=host._digest(rendered),
            grant_container_directory='/run/market-data-grants', candidate_host_root=None, candidate_scope=None,
            approved_source_root=str(source), production_storage_root=str(allocation),
            candidate_record=dict(path=str(record_path), sha256=hashlib.sha256(record_raw).hexdigest()),
            application_service=dict(service_id=manifest['service_id'], runtime_id='target-validation',
                allowed_writable_roots=[item['container_path'] for item in manifest['runtime_roots'] if item['access'] == 'rw']))
        baseline_path = work / 'baseline-policy.json'
        write(baseline_path, host._canonical(policy))
        host.issue_application_service_credential(cid, expected_policy_path=baseline_path,
            credential_path=secrets['market-data-service'], role='production')
        host.issue_execution_grant(cid, expected_policy_path=baseline_path, key_path=keys['production'][2],
            grant_path=baseline_grants / 'grant.json', grant_dir=baseline_grants, role='production')
        run('docker', 'start', cid)
        # Retain the actually running synthetic baseline, never pretend created
        # raw configuration remained unchanged on an unobserved platform.
        policy['actual_config_sha256'] = host.normalize_observation(host.docker_inspect(cid), image, host._json(release_raw))['actual_config_sha256']
        # Recovery transport does not replace an already-bound application
        # credential. Like the existing sandbox lane, this fixture exercises the
        # execution-grant path separately from application-service continuity.
        policy.pop('application_service')
        baseline_path.write_bytes(host._canonical(policy))
        nonce = uuid.uuid4().hex
        grants = directory(allocation / 'recovery-grants')
        sandbox_root = recovery.SANDBOX_PARENT / nonce
        recovery.SANDBOX_PARENT.mkdir(parents=True, exist_ok=True)
        writable = list(dict.fromkeys(m['source'] for m in policy['mounts'] if not m['read_only']))
        recovered = copy.deepcopy(policy)
        recovered['recovery'] = dict(purpose='recovery-validation',
            baseline_policy=dict(path=str(baseline_path), sha256=hashlib.sha256(baseline_path.read_bytes()).hexdigest()),
            production_container_id=cid, production_observation_sha256=recovery.production_identity(host, host.docker_inspect(cid)),
            nonce=nonce, project='spread-recovery-' + nonce, container=manifest['service_id'] + '-recovery-' + nonce,
            host_port=18541, network='spread-recovery-' + nonce + '-net', preserved_store_sources=writable,
            sandbox=dict(mode='isolated-rehearsal', root=str(sandbox_root), roots=[dict(production_source=s,
                sandbox_source=str(sandbox_root / ('root-' + str(i))), initialization='COPY_CURRENT_STATE_REQUIRED',
                uid=UID, gid=GID, mode=0o755) for i, s in enumerate(writable)]))
        projected = recovery.sandbox_mounts(host, recovered, policy, check_files=False)
        recovered['mounts'] = [dict(m, source=str(grants)) if m['target'] == '/run/market-data-grants' else m for m in projected]
        built = recovery.build_sandbox(host, recovered)
        projected_compose = recovery.project_compose(host, desired, recovered)
        projected_compose['services'][manifest['service_id']].pop('build', None)
        projected_compose['services'][manifest['service_id']]['hostname'] = nonce
        fresh_file = work / 'recovery-compose.json'
        write(fresh_file, host._canonical(projected_compose))
        fresh_cmd = ['docker', 'compose', '--project-directory', str(work), '--env-file', str(env), '-f', str(fresh_file)]
        run(*fresh_cmd, 'create', '--no-build')
        fresh = run(*fresh_cmd, 'ps', '-q', '--all', manifest['service_id'])
        c = host.docker_inspect(fresh)
        network = recovered['recovery']['network']
        network_id = host._observe('inspect_object', host._run_docker(['network', 'inspect', network]), 'network')['Id']
        recovered['recovery']['expected_network_id'] = network_id
        recovered.update(actual_config_sha256=host.normalize_observation(c, image, host._json(release_raw))['actual_config_sha256'],
            compose_sources=[dict(path=str(fresh_file), sha256=hashlib.sha256(fresh_file.read_bytes()).hexdigest())],
            rendered_compose_sha256=host._digest(host._json(run(*fresh_cmd, 'config', '--format', 'json').encode())))
        recovery_path = work / 'recovery-policy.json'
        write(recovery_path, host._canonical(recovered))
        grant_path = grants / 'grant.json'
        host.issue_execution_grant(fresh, expected_policy_path=recovery_path, key_path=keys['production'][2],
            grant_path=grant_path, grant_dir=grants, role='production')
        grant_raw = grant_path.read_bytes()
        run('docker', 'start', fresh)
        post = host.validate_recovery_post_start(fresh, expected_policy_path=recovery_path,
            grant_path=grant_path, key_path=keys['production'][2])
        assert grant_path.read_bytes() == grant_raw
        assert post['POST_START_NETWORK_IDENTITY_VALIDATION'] == 'PASS'
        receipt['formal_post_start'] = post
        receipt['sandbox'] = dict(status='PASS', production_before=built['production_before'], production_after=built['production_after'])
        receipt['signed_record'] = dict(status='PASS', sha256=hashlib.sha256(record_raw).hexdigest(), synthetic_binding=evidence['binding'])
        receipt['synthetic_target_validation'] = 'PASS'
        receipt['raw_grant_preserved'] = True
        assessor = load(source, '04_scripts/runtime/pre_release_runtime.py', '_canonicalization_assessor')
        def sealed(name, value):
            path = work / name
            raw = host._canonical(value)
            write(path, raw)
            return dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())
        identity = dict(commit=receipt['synthetic_commit'], tree=receipt['synthetic_tree'], image_id=image_id)
        asset = dict(identity, image_location='LOCAL_IMAGE_ONLY', registry_digest=None,
            release=sealed('retained-release.json', dict(git_commit=identity['commit'], git_tree=identity['tree'],
                image_id=image_id, release_id=host._json(release_raw)['release_id'])),
            runtime_config=sealed('retained-config.json', dict(artifact=identity,
                namespace=dict(compose_project=desired['name'], container_name=manifest['service_id'], host_ports=[8501]),
                compose=sealed('retained-compose.json', rendered), environment=dict(path=str(env), sha256=hashlib.sha256(env.read_bytes()).hexdigest()))),
            data_schema=sealed('retained-schema.json', dict(schema_identity='HOSTED_TEST_NO_DATABASE', assets=[])),
            procedure=sealed('retained-procedure.json', dict(artifact=identity, fresh_instance_required=True,
                fresh_grant_required=True, steps=['create exact image', 'issue fresh grant', 'accept runtime'])))
        source_identity = {k: identity[k] for k in ('commit', 'tree')}
        # Deliberately withhold a complete release recovery observation: an
        # assessment must emit a HIGH_RISK blocked report, never authorize release
        # based only on a successful post-start transport test.
        request = dict(schema_version='production-release-request/1', project_id=PROJECT,
            current=asset, previous=asset, target_commit=identity['commit'], target_source_root=str(source),
            candidate_record=dict(path=str(record_path), sha256=hashlib.sha256(record_raw).hexdigest()),
            state_plan=sealed('state-plan.json', dict(base=source_identity, target=source_identity,
                data_schema_sha256=asset['data_schema']['sha256'], irreversible='NO', compatible=True,
                database_migration=True, production_data_mutation=False, storage_format_change=False)),
            acceptance_plan=sealed('acceptance-plan.json', dict(target=source_identity,
                image_id=image_id, checks=['HOSTED formal post-start', 'no release approval'])), recovery_evidence=None)
        request_path = work / 'assessment-request.json'
        write(request_path, host._canonical(request))
        assessment = assessor.assess_release(request_path, work / 'high-risk-assessment.json')
        assert assessment['RELEASE_RISK_CLASS'] == 'STATEFUL_OR_INFRA'
        assert assessment['production_authorized'] is False
        assert assessment['PRODUCTION_RELEASE_PREFLIGHT'] == 'FAIL'
        receipt['formal_high_risk_assessment'] = dict(execution='PASS', release_gate='FAIL',
            production_authorized=False, reason='Complete release recovery observation intentionally NOT_PROVEN')
    finally:
        for instance in (fresh, cid):
            if instance:
                run('docker', 'rm', '--force', instance)
        if network:
            run('docker', 'network', 'rm', network)
        # Retained nonsecret receipts remain; fixture private keys are not artifacts.
        for _, _, path in keys.values():
            path.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert sys.platform == 'linux' and os.geteuid() == 0 and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
    work = directory(Path('/root') / ('hosted-release-canonicalization-' + uuid.uuid4().hex), 0o700)
    receipt = dict(status='STARTED', docker_platform=json.loads(run('docker', 'info', '--format', 'json')),
        production_acceptance='NOT_EXECUTED', production_authorized=False)
    # Restrict public platform evidence to the rule's actual inputs.
    receipt['docker_platform'] = {k: receipt['docker_platform'].get(k) for k in ('ServerVersion', 'CgroupVersion', 'CgroupDriver')}
    try:
        exercise(work, receipt)
        receipt['status'] = 'PASS'
    except Exception as exc:
        receipt.update(status='FAIL', failure_type=type(exc).__name__, failure=str(exc)[-1400:])
        raise
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write(args.output, (json.dumps(receipt, sort_keys=True) + '\n').encode(), 0o644)


if __name__ == '__main__':
    main()
