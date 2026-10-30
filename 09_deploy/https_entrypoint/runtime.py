"""Protected host renewal and public operational receipt; no market collection."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import certificates
import configuration
import firewall

CONFIG_ROOT = Path('/etc/market-data/https-entrypoint')
APPS = ('spread-dashboard', 'usda-dashboard', 'oil-world-dashboard',
        'guangxi-weather-preview', 'yunnan-weather-preview', certificates.GATEWAY)
LOG_OPTIONS = {'max-size': '20m', 'max-file': '5'}


def atomic(path, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf8')
    descriptor, temporary = tempfile.mkstemp(prefix='.https-status-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def command(args):
    result = subprocess.run(args, capture_output=True, timeout=30)
    result.check_returncode()
    return result.stdout


def config_value(path):
    path = Path(path)
    if path.parent != CONFIG_ROOT or not re.fullmatch(r'[a-z0-9-]+\.json', path.name):
        raise ValueError('HTTPS configuration path invalid')
    certificates.protected(path)
    if path.stat().st_size > 16384:
        raise ValueError('HTTPS configuration exceeds bound')
    value = json.loads(path.read_bytes())
    if set(value) != {'schema_version', 'producer', 'public_ip', 'report'} or value['schema_version'] != 'public-https-config/1':
        raise ValueError('HTTPS configuration fields invalid')
    producer = value['producer']
    if not isinstance(producer, dict) or set(producer) != {'commit', 'tree', 'origin'}:
        raise ValueError('HTTPS source identity invalid')
    for key in ('commit', 'tree'):
        if not isinstance(producer[key], str) or not re.fullmatch(r'[0-9a-f]{40}', producer[key]):
            raise ValueError('HTTPS source identity invalid')
    certificates.protected(SOURCE)
    spec = importlib.util.spec_from_file_location('_https_native_identity', SOURCE / '09_deploy/production_data_delivery/activate_production_data_delta.py')
    host = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = host
    spec.loader.exec_module(host)
    expected = host._git_identity(SOURCE)
    if expected != producer:
        raise ValueError('HTTPS source changed')
    ip = ipaddress.IPv4Address(value['public_ip'])
    if not ip.is_global or str(ip) != value['public_ip']:
        raise ValueError('HTTPS requires the approved public IPv4 address')
    mounts = json.loads(command(['docker', 'inspect', '--format', '{{json .Mounts}}', 'spread-dashboard']))
    selected = [m for m in mounts if m['Destination'] == '/runtime/01_data' and not m['RW']]
    if len(selected) != 1:
        raise ValueError('HTTPS receipt reader mount invalid')
    parent = Path(selected[0]['Source'])
    if not parent.is_relative_to(Path('/var/lib/market-data/production-runtime')):
        raise ValueError('HTTPS receipt allocation invalid')
    report = parent / 'update_status/https_entrypoint.json'
    if value['report'] != str(report):
        raise ValueError('HTTPS receipt differs from the actual served allocation')
    certificates.protected(report.parent)
    if report.is_symlink():
        raise ValueError('HTTPS receipt cannot be a symlink')
    return value


def observation(value, *, renewal_error=None, firewall_error=None):
    errors = []
    certificate = None
    try:
        _, identity, until = certificates.certificate(value['public_ip'], certificates.ROOT / 'acme')
        certificate = dict(sha256=identity, expires_at=until.isoformat())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        errors.append('证书身份、信任链或有效期核验失败')
    logs = []
    for name in APPS:
        try:
            config = json.loads(command(['docker', 'inspect', '--format', '{{json .HostConfig.LogConfig}}', name]))
            valid = config.get('Type') == 'json-file' and config.get('Config') == LOG_OPTIONS
        except (OSError, ValueError, subprocess.SubprocessError):
            valid = False
        logs.append(dict(service=name, bounded=valid, maximum_megabytes=100))
    if any(not row['bounded'] for row in logs):
        errors.append('部分运行容器的日志轮转尚未通过核验')
    if renewal_error:
        errors.append('证书续期执行失败，保留现有证书并等待服务重试')
    if firewall_error:
        errors.append('HTTPS入口健康或旧端口隔离执行失败，等待服务重试')
    restricted = firewall.confirmed()
    if not restricted:
        errors.append('旧HTTP端口收敛尚未通过核验')
    return dict(schema_version='public-https-status/1', checked_at=datetime.now(timezone.utc).isoformat(),
                status='ATTENTION' if errors else 'CURRENT', public_url='https://' + value['public_ip'] + '/',
                access='PUBLIC_READ_WRITE', certificate=certificate, logs=logs, errors=errors,
                legacy_ports_restricted=restricted)


def render_units(source, config):
    for path in (source, config):
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', path) or '..' in path.split('/'):
            raise ValueError('HTTPS unit path invalid')
    service = ('[Unit]\nDescription=Renew the public IP HTTPS certificate\nAfter=docker.service network-online.target\n'
               'Requires=docker.service\nStartLimitIntervalSec=3600\nStartLimitBurst=3\n\n[Service]\nType=oneshot\n'
               'ExecStart=/usr/bin/python3 -I -B ' + source + '/09_deploy/https_entrypoint/runtime.py --config ' + config + ' --renew\n'
               'TimeoutStartSec=8min\nRestart=on-failure\nRestartSec=15min\nNice=10\nNoNewPrivileges=true\nUMask=0077\n')
    timer = ('[Unit]\nDescription=Six-hourly public HTTPS certificate renewal\n\n[Timer]\n'
             'OnCalendar=*-*-* 00,06,12,18:15:00 Asia/Shanghai\nOnBootSec=3min\nPersistent=true\n'
             'AccuracySec=30s\n\n[Install]\nWantedBy=timers.target\n')
    firewall_service = service.replace('Renew the public IP HTTPS certificate','Verify the HTTPS gateway and legacy entrypoint isolation').replace(' --renew\n',' --firewall\n')
    firewall_timer = ('[Unit]\nDescription=Check the approved HTTPS entrypoint isolation\n\n[Timer]\n'
                      'OnBootSec=2min\nOnUnitActiveSec=15min\nAccuracySec=15s\n\n[Install]\nWantedBy=timers.target\n')
    return {'market-data-https-renew.service': service, 'market-data-https-renew.timer': timer,
            'market-data-https-firewall.service': firewall_service, 'market-data-https-firewall.timer': firewall_timer}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--renew', action='store_true')
    parser.add_argument('--firewall', action='store_true')
    args = parser.parse_args(argv)
    if sys.platform != 'linux' or os.geteuid() != 0 or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise ValueError('Protected Linux Python -I -B is required')
    import fcntl
    value = config_value(args.config)
    if args.firewall:
        if args.renew:
            raise ValueError('Firewall activation and certificate renewal are separate operations')
    lock = certificates.protected(certificates.ROOT) / 'renew.lock'
    if lock.is_symlink():
        raise ValueError('HTTPS lock cannot be a symlink')
    with lock.open('a+b') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if args.firewall:
                raise ValueError('HTTPS maintenance is busy; retry entrypoint validation')
            return 0
        failed = firewall_failed = False
        if args.firewall:
            try:
                firewall.install(value['public_ip'])
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                firewall_failed = True
        if args.renew:
            try:
                certificates.certbot(value['public_ip'], 'renew')
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                failed = True
        result = observation(value, renewal_error=failed, firewall_error=firewall_failed)
        atomic(Path(value['report']), result)
        print(json.dumps({'status': result['status'], 'renewal_failed': failed}))
        return int(failed or firewall_failed or result['status'] != 'CURRENT')


if __name__ == '__main__':
    raise SystemExit(main())
