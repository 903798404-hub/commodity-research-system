from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRODUCER = dict(commit='a' * 40, tree='b' * 40,
                origin='https://github.com/903798404-hub/commodity-research-system.git')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner():
    return load('fx_server_test', '09_deploy/foreign_fx_server/run.py')


@pytest.fixture
def host(runner):
    return runner.load_host()


def configuration(tmp_path, host):
    return dict(schema_version='foreign-fx-server-config/1',
        policy=str(host.POLICY_ROOT / 'fx-server-test.json'), policy_sha256='c' * 64,
        image_id='sha256:' + 'd' * 64, data_disk_uuid='109b359d-6e55-4911-a665-c67735dc0a10')


def test_volume_metrics_record_growth_and_keep_unavailable_measurements_unknown(runner, host, monkeypatch):
    monkeypatch.setattr(runner, 'check_mount', lambda *args: None)
    disks = iter([SimpleNamespace(f_blocks=1000, f_bfree=400, f_bavail=350, f_frsize=4096),
                  SimpleNamespace(f_blocks=1000, f_bfree=370, f_bavail=320, f_frsize=4096)])
    monkeypatch.setattr(runner.os, 'statvfs', lambda path: next(disks), raising=False)
    before = runner.volume_usage(host, {})
    result = runner.volume_change(host, {}, before)
    assert result['scope'] == 'shared_data_volume' and result['net_used_bytes'] == 30 * 4096
    def reject(*args):
        raise ValueError('data volume identity differs')
    monkeypatch.setattr(runner, 'check_mount', reject)
    result = runner.volume_change(host, {}, before)
    assert result['net_used_bytes'] is None and result['after']['status'] == 'UNAVAILABLE'
    assert result['after']['used_bytes'] is None


@pytest.mark.parametrize('status,needed', [('PUBLISHED', False), ('NO_CHANGE', False),
    ('FAIL', True), ('CANDIDATE', True), ('SKIPPED', True)])
def test_conditional_windows_use_success_only_on_beijing_day(runner, status, needed):
    now = datetime(2026, 10, 11, 23, 30, tzinfo=timezone.utc)
    state = dict(producer=PRODUCER, checked_at='2026-10-11T23:01:00+00:00', status=status)
    assert runner.retry_needed(state, PRODUCER, now) is needed
    state['checked_at'] = '2026-10-11T15:59:00+00:00'
    assert runner.retry_needed(state, PRODUCER, now)
    state['checked_at'] = '2026-10-12T23:01:00+00:00'
    assert runner.retry_needed(state, PRODUCER, now)


@pytest.mark.parametrize('state', [None, {}, {'producer': PRODUCER, 'checked_at': 'bad'},
    {'producer': PRODUCER, 'checked_at': '2026-10-11T07:00:00', 'status': 'NO_CHANGE'}])
def test_corrupt_or_unbound_state_never_suppresses_update(runner, state):
    assert runner.retry_needed(state, PRODUCER, datetime.now(timezone.utc))


def test_configuration_closed_to_one_policy_image_and_disk(runner, host, tmp_path, monkeypatch):
    monkeypatch.setattr(host, 'POLICY_ROOT', tmp_path / 'policies')
    config = configuration(tmp_path, host)
    assert runner.validate_config(host, config) == config
    for change in [{'ssh_target': 'desktop'}, {'policy': str(tmp_path / 'bad.json')},
                   {'policy_sha256': 'bad'}, {'image_id': 'latest'}, {'data_disk_uuid': 'vdb'}]:
        with pytest.raises(host.DeltaError): runner.validate_config(host, {**config, **change})


def test_collector_cannot_write_production_or_use_desktop(runner, host, tmp_path):
    command = runner.collector_command(configuration(tmp_path, host), tmp_path, 'fx-test-run')
    assert '--pull' in command and command[command.index('--pull') + 1] == 'never'
    assert '--read-only' in command and '--user' in command and '65532:65532' in command
    assert '--cap-drop' in command and 'ALL' in command and '--memory' in command
    mounts = [command[i + 1] for i, value in enumerate(command) if value == '--mount']
    assert len(mounts) == 3
    assert sum(mount.endswith(',readonly') for mount in mounts) == 2
    assert next(m for m in mounts if not m.endswith(',readonly')).endswith('dst=/output')
    assert not any('production-runtime' in arg or 'ssh' == arg or '.env' in arg for arg in command)


def test_timeout_removes_only_owned_transient_collector(runner, host, tmp_path, monkeypatch):
    import subprocess
    calls = []
    monkeypatch.setattr(runner.os, 'chown', lambda *a: None, raising=False)
    def execute(command, **kwargs):
        calls.append(command)
        if command[1] == 'run': raise subprocess.TimeoutExpired(command, 300)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner.subprocess, 'run', execute)
    with pytest.raises(subprocess.TimeoutExpired):
        runner.collect(host, configuration(tmp_path, host), tmp_path, 'foreign-fx-server-owned-run')
    assert calls[-1] == ['docker', 'rm', '-f', 'foreign-fx-server-owned-run']
    assert host.read_json(tmp_path / 'timeout-cleanup.json') == {'exit_code': 0}


def test_pinned_policy_rejects_other_domains_or_images(runner, host, tmp_path, monkeypatch):
    config = configuration(tmp_path, host)
    policy = dict(domain='foreign_fx', approved_producer=PRODUCER,
                  validation_image=dict(image_id=config['image_id'], commit=PRODUCER['commit'], tree=PRODUCER['tree']))
    monkeypatch.setattr(host, '_protected', lambda p: Path(p))
    monkeypatch.setattr(host, 'sha256_file', lambda p: config['policy_sha256'])
    monkeypatch.setattr(host, '_load_policy', lambda p: (policy, config['policy_sha256']))
    monkeypatch.setattr(host, '_inspect_image', lambda p: p)
    assert runner.pinned_policy(host, config) == policy
    policy['domain'] = 'soybean_export_sales'
    with pytest.raises(host.DeltaError): runner.pinned_policy(host, config)
    policy['domain'] = 'foreign_fx'
    policy['validation_image']['commit'] = 'e' * 40
    with pytest.raises(host.DeltaError): runner.pinned_policy(host, config)


def test_missing_or_wrong_data_disk_fails_closed(runner, host, tmp_path, monkeypatch):
    config = configuration(tmp_path, host)
    monkeypatch.setattr(runner.subprocess, 'check_output', lambda *a, **k: 'wrong /var/lib/market-data rw\n')
    with pytest.raises(host.DeltaError, match='disk identity'): runner.check_mount(host, config)
    monkeypatch.setattr(runner.subprocess, 'check_output', lambda *a, **k:
        config['data_disk_uuid'] + ' /var/lib/market-data ro\n')
    with pytest.raises(host.DeltaError): runner.check_mount(host, config)


def snapshot(host, config):
    raw = b'{}\n'
    files = {name: dict(sha256=host.sha256_bytes(raw), size_bytes=len(raw))
             for name in host.DOMAIN_CONTRACTS['foreign_fx']['baseline']}
    return dict(producer=PRODUCER, image_id=config['image_id'], policy_sha256=config['policy_sha256'],
                allocation_root='/var/lib/market-data/production-runtime/test', files=files,
                contents={name: base64.b64encode(raw).decode() for name in files})


def test_fresh_baseline_bytes_and_partial_initialization_rejected(runner, host, tmp_path):
    config = configuration(tmp_path, host)
    value = snapshot(host, config)
    runner.copy_baseline(host, value, tmp_path / 'baseline', {'run_id': 'test'})
    assert (tmp_path / 'baseline' / host.FX_STABLE).read_bytes() == b'{}\n'
    corrupted = deepcopy(value)
    corrupted['contents'][host.FX_STABLE] = base64.b64encode(b'corrupt').decode()
    with pytest.raises(host.DeltaError): runner.copy_baseline(host, corrupted, tmp_path / 'bad', {})
    value['files'][host.FX_STABLE] = None
    with pytest.raises(host.DeltaError): runner.copy_baseline(host, value, tmp_path / 'partial', {})


def execution_fixture(runner, host, tmp_path, monkeypatch):
    config = configuration(tmp_path, host)
    value = snapshot(host, config)
    policy = dict(approved_producer=PRODUCER, allocation_root=value['allocation_root'])
    work = tmp_path / 'foreign-fx-server-test-run'
    work.mkdir()
    monkeypatch.setattr(runner, 'RUNTIME', tmp_path)
    monkeypatch.setattr(host, 'snapshot_fx_baseline', lambda p: value)
    monkeypatch.setattr(runner, 'copy_baseline', lambda *a: None)
    monkeypatch.setattr(runner, 'collect', lambda *a: (tmp_path / 'package', dict(added=0, revised=0)))
    report = dict(report_path=str(tmp_path / 'report.json'), report_sha256='e' * 64)
    monkeypatch.setattr(runner, 'seal_package', lambda *a: report)
    monkeypatch.setattr(runner, 'pinned_policy', lambda *a: policy)
    monkeypatch.setattr(runner, 'check_mount', lambda *a: None)
    return config, policy, work


def test_candidate_never_publishes_and_success_records_actual_receipt(runner, host, tmp_path, monkeypatch):
    config, policy, work = execution_fixture(runner, host, tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(host, 'publish', lambda *a: calls.append(a) or dict(status='NO_CHANGE', stable_sha256='f' * 64))
    result = runner.execute(host, config, policy, work, publish=False)
    assert result['status'] == 'CANDIDATE' and calls == []
    second = tmp_path / 'foreign-fx-server-second-run'; second.mkdir()
    result = runner.execute(host, config, policy, second, publish=True)
    assert result['status'] == 'NO_CHANGE' and result['execution_host'] == 'server' and len(calls) == 1
    assert host.read_json(tmp_path / 'foreign-fx-last-run.json')['receipt']['stable_sha256'] == 'f' * 64


@pytest.mark.parametrize('failure_stage', ['collect', 'seal_package', 'publish'])
def test_failures_preserve_quotes_and_record_diagnostics(runner, host, tmp_path, monkeypatch, failure_stage):
    config, policy, work = execution_fixture(runner, host, tmp_path, monkeypatch)
    def fail(*a): raise RuntimeError('timeout password=hidden')
    if failure_stage == 'publish': monkeypatch.setattr(host, 'publish', fail)
    else: monkeypatch.setattr(runner, failure_stage, fail)
    calls = []
    monkeypatch.setattr(host, 'record_fx_failure', lambda *a: calls.append(a))
    with pytest.raises(RuntimeError): runner.execute(host, config, policy, work, publish=True)
    result = host.read_json(work / 'result.json')
    assert result['status'] == 'FAIL' and result['failure_status_recorded'] is True
    assert 'hidden' not in result['error']['message']
    assert calls[0][2] == host.sha256_bytes(b'{}\n')
    assert not (tmp_path / host.FX_STABLE).exists()


def test_failure_logger_cannot_hide_primary_error(runner, host, tmp_path, monkeypatch):
    config, policy, work = execution_fixture(runner, host, tmp_path, monkeypatch)
    def fail(*a): raise RuntimeError('official timeout')
    monkeypatch.setattr(runner, 'collect', fail)
    monkeypatch.setattr(host, 'record_fx_failure', lambda *a: (_ for _ in ()).throw(ValueError('stale baseline')))
    with pytest.raises(RuntimeError, match='official timeout'): runner.execute(host, config, policy, work, publish=True)
    result = host.read_json(work / 'result.json')
    assert result['failure_status_recorded'] is False
    assert result['error']['error_type'] == 'RuntimeError' and result['failure_log_error']['error_type'] == 'ValueError'


def test_collector_replays_real_fx_semantics_and_keeps_all_history(tmp_path):
    collector = load('fx_collector_test', '09_deploy/foreign_fx_server/collect.py')
    fixtures = load('fx_delivery_fixtures', '08_tests/market_data/test_foreign_fx_delivery.py')
    from agri_research_agent.market_data import foreign_fx_delivery as fx
    baseline, evidence = fixtures.case(tmp_path / 'fixture')
    root = tmp_path / 'baseline'; root.mkdir()
    files = {}
    for name, value in [('daily.json', baseline), ('source_evidence.json', evidence), ('status.json', {})]:
        relative = '01_data/processed/foreign_fx/' + name
        path = root / relative; path.parent.mkdir(parents=True, exist_ok=True)
        raw = fx.canonical(value); path.write_bytes(raw)
        files[relative] = dict(sha256=collector.hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    request = dict(run_id='foreign-fx-server-test', checked_at='2024-01-10T00:00:00+00:00',
                   producer=PRODUCER, baseline_files=files)
    output = tmp_path / 'output'; output.mkdir()
    result = collector.prepare(request, root, output, fetcher=fixtures.official_sources())
    assert result['record_count'] == 16 and result['added'] == result['revised'] == 0
    host = load('fx_collector_host', '09_deploy/production_data_delivery/activate_production_data_delta.py')
    delta = host.validate_delta_document(host.read_json(output / 'package/delta_contract.json'))
    assert delta['run']['business_status'] == 'no_change'
    bad = tmp_path / 'incomplete-output'; bad.mkdir()
    with pytest.raises(fx.FxDataError):
        collector.prepare(request, root, bad, fetcher=fixtures.official_sources(partial=True))
    assert not (bad / 'package').exists()


def test_systemd_preserves_exact_three_windows_without_app_commands():
    path = ROOT / '09_deploy/foreign_fx_server'
    timer = (path / 'market-data-foreign-fx.timer.template').read_text(encoding='utf-8')
    service = (path / 'market-data-foreign-fx.service.template').read_text(encoding='utf-8')
    assert [line for line in timer.splitlines() if line.startswith('OnCalendar=')] == [
        'OnCalendar=*-*-* 07:00:00 Asia/Shanghai', 'OnCalendar=*-*-* 07:30:00 Asia/Shanghai',
        'OnCalendar=*-*-* 08:00:00 Asia/Shanghai']
    assert 'Persistent=true' in timer and 'RandomizedDelaySec=0' in timer
    assert 'RequiresMountsFor=/var/lib/market-data' in service and 'TimeoutStartSec=25min' in service
    assert '--publish --retry-if-needed' in service and '-I -B' in service
    assert not any(command in service + timer for command in ('powershell', 'ssh ', 'compose', 'restart ', 'pull '))
