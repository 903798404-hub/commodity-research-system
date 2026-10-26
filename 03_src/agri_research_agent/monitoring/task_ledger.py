"""Unified inventory and observed task lifecycle; never schedules or writes data."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
import re

from .catalog import JOBS, RULES
from .freshness import QUIET, aware

SCHEMA = 'observed-task-ledger/1'
SERVICE_FIELDS = frozenset({
    'Id', 'LoadState', 'ActiveState', 'SubState', 'MainPID', 'ControlPID',
    'ControlGroup', 'InvocationID', 'Result', 'ExecMainCode', 'ExecMainStatus',
    'ExecMainStartTimestampMonotonic', 'ExecMainExitTimestampMonotonic',
    'ExecMainStartTimestamp', 'ExecMainExitTimestamp', 'TriggeredBy', 'Job',
})
TIMER_FIELDS = frozenset({'Id', 'LoadState', 'ActiveState', 'TimersCalendar',
    'NextElapseUSecRealtime', 'NextElapseUSecMonotonic'})
EXECUTION_LABELS = {
    'NOT_OBSERVED': '尚未观察到执行', 'QUEUED': '原服务排队中',
    'RUNNING': '原服务运行中', 'STOPPING': '原服务停止或重载中',
    'RESTART_WAIT': '等待原服务重启', 'SUCCEEDED': '原服务执行完成',
    'FAILED': '原服务执行失败', 'UNKNOWN': '执行状态未核实',
    'UNAVAILABLE': '原服务未加载',
}
TASK_NAMES = {
    'foreign_fx': '外盘汇率', 'spreads': '国内月差及跨品种价差',
    'fgis': '美豆出口检验', 'fas': '美豆出口销售', 'crop': '美豆种植与生长',
    'brazil': '巴西大豆种植与生长', 'canola_exports': '加拿大菜籽出口',
    'warehouse': '交易所仓单', 'positions_domestic': '国内持仓',
    'positions_foreign': '外盘持仓', 'guangxi': '广西甘蔗天气',
    'yunnan': '云南甘蔗天气', 'thailand': '泰国甘蔗天气',
}


@dataclass(frozen=True)
class Task:
    identity: str
    label: str
    service: str
    timer: str
    datasets: tuple[str, ...]
    pages: tuple[str, ...]


def task_catalog():
    """One source of dataset policy; business check deadlines are not timers."""
    if set(TASK_NAMES) != set(JOBS):
        raise ValueError('Task inventory differs from approved producer whitelist')
    tasks = []
    for job, label in TASK_NAMES.items():
        rules = [rule for rule in RULES if rule.job == job]
        if not rules or any(rule.excluded or rule.manual for rule in rules):
            raise ValueError('Automatic task has excluded/manual dependencies')
        tasks.append(Task(job, label, JOBS[job], JOBS[job].removesuffix('.service') + '.timer',
            tuple(rule.identity for rule in rules), tuple(sorted({p for rule in rules for p in rule.pages}))))
    return tuple(tasks)


TASKS = task_catalog()
BY_TASK = {task.identity: task for task in TASKS}


def dataset_catalog():
    records = []
    for rule in RULES:
        mode = ('EXCLUDED' if rule.excluded else 'STATIC' if rule.cadence == 'static'
            else 'MANUAL' if rule.manual else 'AUTOMATIC' if rule.job else 'NO_UPDATER')
        # Preserve the exact original policy, including season and period grain.
        records.append(dict(**asdict(rule), mode=mode, task_id=rule.job))
    return records


def parse_inventory(raw: str, expected: set[str], fields: frozenset[str], *, calendars=False):
    """Bounded structured systemctl output; only TimersCalendar can repeat."""
    if len(raw.encode('utf-8')) > 256 * 1024:
        raise ValueError('Task observation exceeds bound')
    result = {}
    for block in raw.strip().split('\n\n'):
        row = {}
        for line in block.splitlines():
            key, separator, value = line.partition('=')
            if not separator or key not in fields:
                raise ValueError('Unknown task observation field')
            if calendars and key == 'TimersCalendar':
                row.setdefault(key, []).append(value)
            elif key in row:
                raise ValueError('Duplicate task observation field')
            else:
                row[key] = value
        # systemctl omits an empty array; an absent calendar is not a schedule.
        if calendars:
            row.setdefault('TimersCalendar', [])
        if set(row) != fields or row.get('Id') in result:
            raise ValueError('Incomplete or duplicate task observation')
        result[row['Id']] = row
    if set(result) != expected:
        raise ValueError('Task observation inventory differs')
    return result


def execution_state(service):
    if set(service) != SERVICE_FIELDS:
        raise ValueError('Incomplete service lifecycle')
    integers = ['MainPID', 'ControlPID', 'ExecMainCode', 'ExecMainStatus',
        'ExecMainStartTimestampMonotonic', 'ExecMainExitTimestampMonotonic']
    if any(not re.fullmatch(r'[0-9]+', service[key]) for key in integers):
        raise ValueError('Invalid service lifecycle number')
    if service['Job'] and not re.fullmatch(r'[1-9][0-9]*', service['Job']):
        raise ValueError('Invalid systemd job identity')
    invocation = service['InvocationID']
    if invocation and (not re.fullmatch(r'[0-9a-f]{32}', invocation) or int(invocation, 16) == 0):
        raise ValueError('Invalid service invocation identity')
    if service['LoadState'] != 'loaded':
        return 'UNAVAILABLE'
    active, sub = service['ActiveState'], service['SubState']
    if active not in {'active', 'activating', 'inactive', 'failed', 'deactivating', 'reloading'}:
        return 'UNKNOWN'
    workers = any(int(service[key]) for key in ['MainPID', 'ControlPID']) or bool(service['ControlGroup'])
    if active in {'deactivating', 'reloading'}:
        return 'STOPPING'
    if active == 'activating' and sub == 'auto-restart' and not workers:
        return 'RESTART_WAIT'
    if workers or active in {'active', 'activating'}:
        return 'RUNNING'
    if service['Job']:
        return 'QUEUED'
    start = int(service['ExecMainStartTimestampMonotonic'])
    end = int(service['ExecMainExitTimestampMonotonic'])
    if active == 'failed':
        return 'FAILED'
    if not invocation and start == end == 0:
        return 'NOT_OBSERVED'  # systemd's default Result=success is not an execution
    if not invocation or not start or end < start:
        return 'UNKNOWN'
    if service['Result'] == 'success' and service['ExecMainCode'] == '1' and service['ExecMainStatus'] == '0':
        return 'SUCCEEDED'
    if service['Result'] and service['Result'] != 'success':
        return 'FAILED'
    return 'UNKNOWN'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')


def reconcile_tasks(rows, services, timers, previous, now: datetime, boot_id: str):
    """Capture observed transitions. Execution success never changes data status."""
    stamp = aware(now).isoformat()
    if not re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', boot_id):
        raise ValueError('Invalid task observation boot identity')
    if set(services) != set(JOBS.values()) or set(timers) != {task.timer for task in TASKS}:
        raise ValueError('Incomplete task/timer coverage')
    by_data = {row['identity']: row for row in rows}
    if len(by_data) != len(rows) or set(by_data) != {rule.identity for rule in RULES}:
        raise ValueError('Task ledger data coverage differs')
    if previous and previous.get('schema_version') != SCHEMA:
        raise ValueError('Task ledger schema differs')
    prior = {row['identity']: row for row in previous.get('tasks', [])}
    events = list(previous.get('recent_observations', []))
    tasks = []
    for task in TASKS:
        service, timer = services[task.service], timers[task.timer]
        if service['Id'] != task.service or timer['Id'] != task.timer or set(timer) != TIMER_FIELDS:
            raise ValueError('Task service/timer binding differs')
        if set(service['TriggeredBy'].split()) - {task.timer}:
            raise ValueError('Unexpected task trigger')
        execution = execution_state(service)
        invocation = service['InvocationID']
        run_id = (f'{boot_id}:{task.identity}:{invocation}'
            if invocation and execution not in {'QUEUED', 'NOT_OBSERVED', 'UNAVAILABLE'} else None)
        # Raw process evidence only. No command, environment, URL, secret or log body.
        evidence = {key: service[key] for key in sorted(SERVICE_FIELDS)}
        fingerprint = sha256(canonical(dict(boot_id=boot_id, service=evidence, status=execution))).hexdigest()
        old = prior.get(task.identity, {})
        changed = fingerprint != old.get('fingerprint')
        first_seen = stamp if changed else old['state_first_seen_at']
        statuses = {}
        for identity in task.datasets:
            state = by_data[identity]['status']
            statuses[state] = statuses.get(state, 0) + 1
        pending = sum(count for state, count in statuses.items() if state not in QUIET)
        record = dict(**asdict(task), execution_status=execution, run_id=run_id,
            observed_at=stamp, state_first_seen_at=first_seen, fingerprint=fingerprint,
            evidence=evidence, timer_observation=timer, data_status_counts=statuses, pending_datasets=pending,
            dependency_policy='原服务内部依赖保持原样；跨服务依赖尚未迁移到统一调度')
        tasks.append(record)
        if changed:
            events.append(dict(kind='TASK_STATE_OBSERVED', task_id=task.identity, label=task.label,
                run_id=run_id, boot_id=boot_id, execution_status=execution, at=stamp, evidence=evidence,
                fingerprint=fingerprint, observation_source='systemd'))
    return dict(schema_version=SCHEMA, checked_at=stamp, boot_id=boot_id,
        datasets=dataset_catalog(), tasks=tasks, recent_observations=events[-100:],
        history_scope='接入后巡检观察到的执行状态；未观察到的历史运行不补造',
        execution_control='原定时器与原受保护更新器；本阶段只统一台账和状态')


def validate_ledger(ledger, now):
    """Validate the website projection without executing or inspecting a task."""
    fields={'schema_version','checked_at','boot_id','datasets','tasks','recent_observations','history_scope','execution_control'}
    if not isinstance(ledger,dict) or set(ledger)!=fields or not re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}',ledger['boot_id']):
        raise ValueError('Task ledger structure differs')
    if ledger.get('schema_version') != SCHEMA or aware(ledger['checked_at']) > aware(now)+timedelta(minutes=5):
        raise ValueError('Task ledger identity/time invalid')
    tasks = ledger['tasks']
    if len(tasks) != len(TASKS) or {task['identity'] for task in tasks} != set(BY_TASK):
        raise ValueError('Task ledger coverage differs')
    expected = {row['identity']: row for row in dataset_catalog()}
    actual = ledger['datasets']
    if len(actual) != len(expected) or {row['identity'] for row in actual} != set(expected):
        raise ValueError('Dataset registry coverage differs')
    # JSON transports tuples as lists; compare their canonical representations.
    if canonical(sorted(actual, key=lambda x: x['identity'])) != canonical(sorted(expected.values(), key=lambda x: x['identity'])):
        raise ValueError('Dataset registry policy differs')
    for task in tasks:
        definition = BY_TASK[task['identity']]
        fields=set(asdict(definition))|{'execution_status','run_id','observed_at','state_first_seen_at','fingerprint',
            'evidence','timer_observation','data_status_counts','pending_datasets','dependency_policy'}
        if set(task)!=fields:
            raise ValueError('Task projection fields differ')
        if task['service'] != definition.service or task['label'] != definition.label or task['timer'] != definition.timer or tuple(task['datasets']) != definition.datasets or tuple(task['pages']) != definition.pages:
            raise ValueError('Task registry binding differs')
        if task['execution_status'] not in EXECUTION_LABELS or execution_state(task['evidence']) != task['execution_status']:
            raise ValueError('Task lifecycle evidence differs')
        if aware(task['observed_at']) != aware(ledger['checked_at']):
            raise ValueError('Task observation time differs')
        if aware(task['state_first_seen_at']) > aware(task['observed_at']):
            raise ValueError('Task first observation is in the future')
        timer = task['timer_observation']
        if set(timer) != TIMER_FIELDS or timer['Id'] != definition.timer or not isinstance(timer['TimersCalendar'], list):
            raise ValueError('Task timer evidence differs')
        invocation = task['evidence']['InvocationID']
        expected_id = (f"{ledger['boot_id']}:{task['identity']}:{invocation}"
            if invocation and task['execution_status'] not in {'QUEUED','NOT_OBSERVED','UNAVAILABLE'} else None)
        if task['run_id'] != expected_id:
            raise ValueError('Task execution identity differs')
        if task['fingerprint'] != sha256(canonical(dict(boot_id=ledger['boot_id'],service=task['evidence'],status=task['execution_status']))).hexdigest():
            raise ValueError('Task evidence fingerprint differs')
        counts=task['data_status_counts']
        states=QUIET|{'MISSED','CHECK_DUE','WAITING_FOR_SOURCE','SOURCE_UNVERIFIED','WAITING_MANUAL','MISSING','ERROR'}
        if not set(counts)<=states or any(type(value) is not int or value<0 for value in counts.values()) or sum(counts.values())!=len(definition.datasets):
            raise ValueError('Task data status counts differ')
        if task['pending_datasets']!=sum(value for key,value in counts.items() if key not in QUIET):
            raise ValueError('Task pending dataset count differs')
    if len(ledger['recent_observations']) > 100:
        raise ValueError('Task history exceeds bound')
    for event in ledger['recent_observations']:
        if event['task_id'] not in BY_TASK or event['kind'] != 'TASK_STATE_OBSERVED' or event['observation_source'] != 'systemd':
            raise ValueError('Unknown task observation')
        if execution_state(event['evidence']) != event['execution_status'] or event['evidence']['Id'] != BY_TASK[event['task_id']].service:
            raise ValueError('Task history evidence differs')
        if aware(event['at']) > aware(ledger['checked_at']):
            raise ValueError('Task history is in the future')
        if not re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}',event['boot_id']):
            raise ValueError('Task history boot identity differs')
        if event['fingerprint']!=sha256(canonical(dict(boot_id=event['boot_id'],service=event['evidence'],status=event['execution_status']))).hexdigest():
            raise ValueError('Task history fingerprint differs')
    return ledger
