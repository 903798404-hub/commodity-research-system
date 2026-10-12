"""Protected Linux FX schedule: direct collection, offline validation, host CAS publication.

Only stdlib runs on the host. Dependencies stay in the pinned existing image.
No SSH, desktop files, dependency installation, image build/pull or app restart.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path('/var/lib/market-data/production-data-automation/foreign-fx')
CONFIG_ROOT = Path('/etc/market-data/foreign-fx-server')
BEIJING = timezone(timedelta(hours=8))
SCHEMA = 'foreign-fx-server-config/1'


def load_host():
    spec = importlib.util.spec_from_file_location('foreign_fx_server_publisher',
        ROOT / '09_deploy/production_data_delivery/activate_production_data_delta.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_config(host, value):
    config = host._exact(value, {'schema_version', 'policy', 'policy_sha256', 'image_id', 'data_disk_uuid'}, 'server FX config')
    host.require(config['schema_version'] == SCHEMA, 'server FX config schema invalid')
    host.require(type(config['policy_sha256']) is str and host.HEX64.fullmatch(config['policy_sha256']), 'FX policy SHA invalid')
    host.require(type(config['image_id']) is str and host.IMAGE_ID.fullmatch(config['image_id']), 'FX image ID invalid')
    host.require(type(config['data_disk_uuid']) is str and
        re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', config['data_disk_uuid']), 'data disk UUID invalid')
    host._under(config['policy'], host.POLICY_ROOT)
    return config


def error_details(exc, stage):
    message = re.sub(r'(?i)(token|password|api[_-]?key|authorization)(\s*[=:]\s*)[^\s&]+',
                     r'\1\2[REDACTED]', str(exc))
    message = re.sub(r'https?://[^\s/@]+:[^\s/@]+@', 'https://[REDACTED]@', message)
    return dict(stage=stage, error_type=type(exc).__name__, message=message[:1000])


def retry_needed(state, producer, now):
    if type(state) is not dict or state.get('producer') != producer:
        return True
    try:
        checked = datetime.fromisoformat(state['checked_at'])
        if checked.tzinfo is None or checked > now or checked.astimezone(BEIJING).date() != now.astimezone(BEIJING).date():
            return True
    except (KeyError, TypeError, ValueError):
        return True
    return state.get('status') not in {'PUBLISHED', 'NO_CHANGE'}


def check_mount(host, config):
    value = subprocess.check_output(['findmnt', '-n', '-o', 'UUID,TARGET,OPTIONS', '--target', '/var/lib/market-data'],
                                    text=True, timeout=15).split()
    host.require(len(value) == 3 and value[0] == config['data_disk_uuid'] and value[1] == '/var/lib/market-data'
                 and 'rw' in value[2].split(','), 'FX data disk identity or mount invalid')
    disk = os.statvfs('/var/lib/market-data')
    host.require(disk.f_bavail * disk.f_frsize > 256 * 1024**2 and disk.f_favail > 1000, 'FX data disk capacity insufficient')


def volume_usage(host, config):
    """Observe the verified data volume; unavailable measurements stay explicit."""
    try:
        check_mount(host, config)
        disk = os.statvfs('/var/lib/market-data')
        return {'status': 'OBSERVED', 'used_bytes': (disk.f_blocks - disk.f_bfree) * disk.f_frsize,
                'available_bytes': disk.f_bavail * disk.f_frsize}
    except Exception as exc:
        return {'status': 'UNAVAILABLE', 'used_bytes': None, 'available_bytes': None,
                'error_type': type(exc).__name__}


def volume_change(host, config, before):
    after = volume_usage(host, config)
    observed = before['status'] == after['status'] == 'OBSERVED'
    return {'scope': 'shared_data_volume', 'before': before, 'after': after,
            'net_used_bytes': after['used_bytes'] - before['used_bytes'] if observed else None}


def pinned_policy(host, config):
    host.require(host.sha256_file(host._protected(config['policy'])) == config['policy_sha256'], 'FX policy pin differs')
    policy, digest = host._load_policy(config['policy'])
    host.require(digest == config['policy_sha256'] and policy['domain'] == 'foreign_fx', 'FX policy domain or identity differs')
    host.require(policy['validation_image']['image_id'] == config['image_id'] and
        policy['validation_image']['commit'] == policy['approved_producer']['commit'] and
        policy['validation_image']['tree'] == policy['approved_producer']['tree'], 'FX image binding differs')
    host._inspect_image(policy['validation_image'])
    return policy


def write_state(host, path, value):
    host._no_links(path)
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    host._write_json_exclusive(temporary, value)
    os.replace(temporary, path)


def copy_baseline(host, snapshot, root, request):
    expected = set(host.DOMAIN_CONTRACTS['foreign_fx']['baseline'])
    host._exact(snapshot['files'], expected, 'FX baseline files')
    host._exact(snapshot['contents'], expected, 'FX baseline contents')
    host.require(len({v is None for v in snapshot['files'].values()}) == 1, 'FX baseline incomplete')
    root.mkdir(mode=0o755)
    for relative, identity in snapshot['files'].items():
        host._file_identity(identity, nullable=True)
        encoded = snapshot['contents'][relative]
        if identity is None:
            host.require(encoded is None, 'absent FX baseline has content')
            continue
        host.require(type(encoded) is str and len(encoded) <= 96 * 1024**2, 'FX baseline exceeds bound')
        raw = base64.b64decode(encoded, validate=True)
        host.require(dict(sha256=host.sha256_bytes(raw), size_bytes=len(raw)) == identity, 'FX baseline copy identity differs')
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        host._write_exclusive(path, raw)
    host._write_json_exclusive(root / 'request.json', request)
    host._make_tree_readonly(root, public_read=True)


def collector_command(config, work, run_id):
    return ['docker', 'run', '--rm', '--name', run_id, '--pull', 'never', '--network', 'bridge',
            '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', '65532:65532',
            '--memory', '512m', '--cpus', '1', '--pids-limit', '64',
            '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=64m',
            '--mount', f'type=bind,src={work / "baseline"},dst=/baseline,readonly',
            '--mount', f'type=bind,src={work / "collection"},dst=/output',
            '--mount', f'type=bind,src={ROOT / "09_deploy/foreign_fx_server"},dst=/opt/fx,readonly',
            '--entrypoint', 'python', config['image_id'], '-I', '-B', '/opt/fx/collect.py']


def collect(host, config, work, run_id):
    output = work / 'collection'
    output.mkdir(mode=0o700)
    os.chown(output, 65532, 65532)
    try:
        completed = subprocess.run(collector_command(config, work, run_id), capture_output=True, timeout=300)
        for name, raw in [('collector.stdout', completed.stdout), ('collector.stderr', completed.stderr)]:
            # Collector emits only bounded diagnostics, never raw response bodies.
            host._write_exclusive(work / name, raw[-65536:])
        result_path = host._no_links(output / 'collection-result.json')
        host.require(result_path.is_file() and result_path.stat().st_size <= 65536, 'FX collector result missing or oversized')
        result = host.read_json(result_path)
        if completed.returncode or result.get('status') != 'PASS':
            error = RuntimeError('FX official collector failed')
            error.collection_error = result.get('error')
            raise error
        return output / 'package', result['observations']
    except subprocess.TimeoutExpired:
        # Kill only this owned transient collector; never leave a detached writer.
        cleanup = subprocess.run(['docker', 'rm', '-f', run_id], capture_output=True, timeout=30)
        host._write_json_exclusive(work / 'timeout-cleanup.json', {'exit_code': cleanup.returncode})
        raise


def seal_package(host, config, policy, package, run_id):
    delta = host.validate_delta_document(host.read_json(package / host.MANIFEST_NAME))
    host.require(delta['delta_id'] == run_id and delta['domain'] == 'foreign_fx' and
        delta['producer'] == policy['approved_producer'], 'FX collected package identity differs')
    expected = {host.MANIFEST_NAME, *delta['payloads']}
    before = host._file_set(package)
    host.require(set(before) == expected, 'FX package contains unexpected files')
    upload = host.UPLOAD_ROOT / run_id
    host._no_links(upload)
    upload.mkdir(mode=0o700)
    for name in sorted(expected):
        host._write_exclusive(upload / name, (package / name).read_bytes())
    host.require(host._file_set(upload) == before and host._file_set(package) == before, 'FX package changed during sealing')
    digest = host.sha256_file(upload / host.MANIFEST_NAME)
    staged = host.stage_upload(config['policy'], upload, digest)
    host.receive(config['policy'], staged['incoming_path'], digest)
    return host.validate(config['policy'], run_id)


def execute(host, config, policy, work, *, publish):
    producer = policy['approved_producer']
    now = datetime.now(timezone.utc)
    run_id = work.name
    result = dict(schema_version='foreign-fx-server-result/1', run_id=run_id, producer=producer,
                  status='FAIL', published=False, checked_at=now.isoformat(), execution_host='server')
    snapshot = None
    volume_before = volume_usage(host, config)
    stage = 'baseline'
    try:
        snapshot = host.snapshot_fx_baseline(config['policy'])
        host.require(snapshot['producer'] == producer and snapshot['image_id'] == config['image_id'] and
            snapshot['policy_sha256'] == config['policy_sha256'] and snapshot['allocation_root'] == policy['allocation_root'],
            'FX fresh baseline identity differs')
        request = dict(run_id=run_id, checked_at=now.isoformat(), producer=producer, baseline_files=snapshot['files'])
        copy_baseline(host, snapshot, work / 'baseline', request)
        stage = 'collect_official_sources'
        package, observations = collect(host, config, work, run_id)
        stage = 'offline_validation'
        report = seal_package(host, config, policy, package, run_id)
        result.update(status='CANDIDATE', observations=observations, validation_report=report['report_path'],
                      validation_sha256=report['report_sha256'])
        if publish:
            stage = 'publish'
            pinned_policy(host, config)
            check_mount(host, config)
            receipt = host.publish(config['policy'], run_id, report['report_path'], report['report_sha256'])
            result.update(status=receipt['status'], published=receipt['status'] == 'PUBLISHED', receipt=receipt)
    except Exception as exc:
        result.update(status='FAIL', error=error_details(exc, stage))
        detail = getattr(exc, 'collection_error', None)
        if type(detail) is dict:
            result['collection_error'] = detail
        if publish and snapshot and snapshot['files'].get(host.FX_STABLE):
            try:
                pinned_policy(host, config)
                check_mount(host, config)
                host.record_fx_failure(config['policy'], run_id, snapshot['files'][host.FX_STABLE]['sha256'], type(exc).__name__)
                result['failure_status_recorded'] = True
            except Exception as log_error:
                result.update(failure_status_recorded=False, failure_log_error=error_details(log_error, 'record_failure_status'))
        raise
    finally:
        result['checked_at'] = datetime.now(timezone.utc).isoformat()
        result['volume_metrics'] = volume_change(host, config, volume_before)
        host._write_json_exclusive(work / 'result.json', result)
        write_state(host, RUNTIME / 'foreign-fx-last-run.json', result)
    return result


def run(config_path, *, publish=False, retry_if_needed=False):
    host = load_host()
    host.require(sys.platform == 'linux' and os.geteuid() == 0 and sys.flags.isolated and sys.dont_write_bytecode,
                 'FX server entry requires protected Linux Python -I -B')
    path = host._protected(host._under(config_path, CONFIG_ROOT))
    config = validate_config(host, host.read_json(path))
    digest = host.sha256_file(path)
    check_mount(host, config)
    policy = pinned_policy(host, config)
    host._protected(RUNTIME)
    host.require(not retry_if_needed or publish, 'FX conditional retry requires publication')
    import fcntl
    lock = host._no_links(RUNTIME / 'update.lock')
    with lock.open('a+b') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(status='SKIPPED', reason='FX server update already running', execution_host='server')
        state_path = host._no_links(RUNTIME / 'foreign-fx-last-run.json')
        state = host.read_json(state_path) if state_path.exists() else None
        now = datetime.now(timezone.utc)
        if retry_if_needed and not retry_needed(state, policy['approved_producer'], now):
            return dict(status='SKIPPED', reason='successful FX check already completed today', execution_host='server')
        host.require(host.sha256_file(path) == digest, 'FX server config changed')
        run_id = 'foreign-fx-server-' + now.strftime('%Y%m%dT%H%M%S').lower() + '-' + uuid.uuid4().hex[:12]
        work = RUNTIME / run_id
        work.mkdir(mode=0o755)
        return execute(host, config, policy, work, publish=publish)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--retry-if-needed', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = run(args.config, publish=args.publish, retry_if_needed=args.retry_if_needed)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps(dict(status='FAIL', error=error_details(exc, 'server_entry')), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
