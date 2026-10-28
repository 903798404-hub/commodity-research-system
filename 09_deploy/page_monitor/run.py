"""Pinned host monitor. Only metadata is written; retries reuse existing services."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '03_src'))
from agri_research_agent.monitoring.catalog import JOBS, RULES
from agri_research_agent.monitoring.freshness import SCHEMA, reconcile, reserve_retry, retry_plan
from agri_research_agent.monitoring.task_ledger import (
    SERVICE_FIELDS, TIMER_FIELDS, TASKS, parse_inventory, reconcile_tasks,
)

STATE_ROOT = Path('/var/lib/market-data/production-data-automation/page-monitor')
CONFIG_ROOT = Path('/etc/market-data/page-monitor')
ALLOCATION_ROOT = Path('/var/lib/market-data/production-runtime')


def command(args, *, timeout=30, diagnostic=None):
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    if diagnostic is not None:
        with Path(diagnostic).open('wb') as file:
            file.write(result.stderr[-1024**2:])
    result.check_returncode()
    return result.stdout


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def protected(path, parent=None):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError('Protected path identity differs')
    if parent is not None and not path.is_relative_to(parent):
        raise ValueError('Protected path outside approved root')
    for item in (path, *path.parents):
        stat = item.stat()
        if stat.st_uid != 0 or stat.st_mode & 0o022:
            raise ValueError('Protected path owner or permissions invalid')
    return path


def read_json(path):
    if Path(path).stat().st_size > 16 * 1024**2:
        raise ValueError('Monitor JSON too large')
    return json.loads(Path(path).read_text(encoding='utf-8'))


def atomic(path, value):
    """Same-directory fsync + rename; never truncate a live report on failure."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
    descriptor, temporary = tempfile.mkstemp(prefix='.page-monitor-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        if hasattr(os, 'O_DIRECTORY'):
            directory = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_config(path):
    config = read_json(protected(path, CONFIG_ROOT))
    fields = {'schema_version', 'producer', 'image_id', 'reader_id', 'data_disk_uuid', 'inputs', 'report', 'jobs'}
    if config.get('schema_version') in {'page-monitor-config/2','page-monitor-config/3'}:
        fields.add('task_ledger_enabled')
        if config.get('task_ledger_enabled') is not True:
            raise ValueError('Task ledger must be explicitly enabled in config/2')
        if config['schema_version']=='page-monitor-config/3':
            fields.add('dispatcher')
            binding=config.get('dispatcher')
            if not isinstance(binding,dict) or set(binding)!={'config','sha256'}:
                raise ValueError('Dispatcher binding invalid')
            path=protected(binding['config'],Path('/etc/market-data/task-dispatcher'))
            if digest(path.read_bytes())!=binding['sha256']:raise ValueError('Dispatcher configuration changed')
    elif config.get('schema_version') != 'page-monitor-config/1':
        raise ValueError('Monitor configuration schema invalid')
    if set(config) != fields:
        raise ValueError('Monitor configuration schema invalid')
    expected = config['producer']
    if set(expected) != {'commit', 'tree', 'origin'} or expected['origin'] not in {'https://github.com/903798404-hub/commodity-research-system.git','git@github.com:903798404-hub/commodity-research-system.git'}:
        raise ValueError('Monitor source origin invalid')
    for field, args in [('commit', ['rev-parse','HEAD']), ('tree',['rev-parse','HEAD^{tree}']), ('origin',['remote','get-url','origin'])]:
        if command(['git','-C',str(protected(ROOT)),*args]).decode().strip() != expected[field]:
            raise ValueError('Monitor source identity differs')
    if command(['git','-C',str(ROOT),'status','--porcelain','--untracked-files=all']).strip():
        raise ValueError('Monitor source is dirty')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', config['image_id']) or not re.fullmatch(r'[0-9a-f]{64}', config['reader_id']):
        raise ValueError('Monitor image/reader identity invalid')
    image = json.loads(command(['docker','image','inspect',config['image_id']]))[0]
    labels = image['Config']['Labels'] or {}
    if labels.get('org.opencontainers.image.revision') != expected['commit'] or labels.get('market-data.git.tree') != expected['tree']:
        raise ValueError('Monitor image labels differ')
    if set(config['inputs']) != {'data','manual','usda','oil_world','guangxi','yunnan'}:
        raise ValueError('Monitor input roles differ')
    allocation = Path(config['inputs']['data']).parent
    protected(allocation, ALLOCATION_ROOT)
    if not re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}',config['data_disk_uuid']):
        raise ValueError('Monitor data disk UUID invalid')
    if command(['findmnt','-n','-o','UUID','-T',str(allocation)]).decode().strip()!=config['data_disk_uuid']:
        raise ValueError('Monitor data disk identity differs')
    report = Path(config['report'])
    if report != allocation / '01_data/update_status/page_monitor.json' or report.parent.is_symlink():
        raise ValueError('Monitor report is outside dedicated metadata path')
    protected(report.parent)
    # Bind to the actual website reader and its original mount selection.
    reader = json.loads(command(['docker','inspect',config['reader_id']]))[0]
    if not reader['State']['Running']:
        raise ValueError('Website reader is not running')
    mounts = {v['Destination']:v['Source'] for v in reader['Mounts']}
    if mounts.get('/runtime/01_data') != config['inputs']['data'] or mounts.get('/runtime/import-profit/operational/cnf') != config['inputs']['manual']:
        raise ValueError('Website data-reader mount binding differs')
    config['_reader_errors']=check_external_readers(config['inputs'])
    for role, value in config['inputs'].items():
        path = Path(value)
        if not path.is_absolute() or not path.is_dir() or ',' in value or '\n' in value:
            raise ValueError('Monitor input root invalid')
        # Runtime producers can legitimately own files; never broaden their access.
        if path.resolve() != path:
            raise ValueError('Monitor input root alias invalid')
    if set(config['jobs']) != set(JOBS):
        raise ValueError('Monitor jobs differ from fixed whitelist')
    blocked={}
    for job,binding in config['jobs'].items():
        try:check_job(job,binding)
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
            if config['schema_version']!='page-monitor-config/3':raise
            blocked[job]='原服务或配置身份未核实，暂停此任务'
    if config['schema_version']=='page-monitor-config/3':config['_blocked_jobs']=blocked
    return config


def check_external_readers(inputs):
    # Independent weather previews legitimately replace their container each
    # morning. Bind the current named reader's actual data mount, not its old
    # container ID or an identically named loose directory.
    readers=[('usda','usda-dashboard','/runtime/usda',''),
        ('oil_world','oil-world-dashboard','/usr/share/nginx/html/oil-world/data/oil_world',''),
        ('guangxi','guangxi-weather-preview','/workspace','live'),
        ('yunnan','yunnan-weather-preview','/workspace','live')]
    errors={}
    for role,name,destination,child in readers:
        try:
            observed=json.loads(command(['docker','inspect',name]))
            if len(observed)!=1:
                raise ValueError('Independent reader inventory differs')
            container=observed[0]
            mounts={m['Destination']:m['Source'] for m in container['Mounts']}
            if container['Name']!='/'+name or not container['State']['Running'] or destination not in mounts:
                raise ValueError('Independent reader unavailable')
            actual=Path(mounts[destination])
            if child:
                actual=actual/child
            if str(actual)!=inputs[role]:
                raise ValueError('Independent reader mount binding differs')
        except (subprocess.SubprocessError,ValueError,KeyError,TypeError):
            errors[role]='独立应用不可读或实际数据挂载不匹配；已停止该项核验和额外重试'
    return errors


def check_job(job,binding):
    if job not in JOBS or set(binding) != {'unit_sha256','files'} or not re.fullmatch('[0-9a-f]{64}', binding['unit_sha256']):
        raise ValueError('Service binding invalid')
    if digest(command(['systemctl','cat',JOBS[job]])) != binding['unit_sha256']:
        raise ValueError('Existing producer service changed; retry must be rebound')
    for path,sha in binding['files'].items():
        target = producer_file(path)
        if digest(target.read_bytes()) != sha:
            raise ValueError('Existing producer configuration changed')
    # Thai's approved service contains a complete immutable image ID and all
    # arguments; its configuration is inside that image, not a mutable host file.
    if not binding['files'] and job!='thailand':
        raise ValueError('Producer configuration pin missing')


def producer_file(path):
    target = Path(path)
    weather_anchors = [Path('/home/ubuntu/market-data-runtime')/(country+'-weather/releases') for country in ['guangxi','yunnan']]
    weather_root = next((root for root in weather_anchors if target.is_relative_to(root)),None)
    if weather_root is None:
        if not (target.is_relative_to('/etc/market-data') or target.is_relative_to('/opt/market-data-releases')):
            raise ValueError('Unexpected producer binding file')
        return protected(target)
    relative=target.relative_to(weather_root)
    if len(relative.parts)<2 or not re.fullmatch('[A-Za-z0-9._-]{1,128}',relative.parts[0]):
        raise ValueError('Weather release version invalid')
    anchor=weather_root / relative.parts[0]
    # Reuse the already-approved independent weather release boundary. Do not
    # change ownership/permissions on the user's home or runtime parent folders.
    if not target.is_absolute() or target.resolve(strict=True)!=target:
        raise ValueError('Weather producer binding alias invalid')
    for item in (target,*target.parents):
        stat=item.stat()
        if stat.st_uid!=0 or stat.st_mode & 0o022:
            raise ValueError('Existing weather release protection differs')
        if item==anchor:
            return target
    raise ValueError('Weather release boundary missing')


def observe(config):
    args = ['docker','run','--rm','--network','none','--read-only','--user','65532:65532',
        '--cap-drop','ALL','--security-opt','no-new-privileges','--memory','512m','--memory-swap','512m','--cpus','0.75',
        '--pids-limit','64','--tmpfs','/tmp:rw,noexec,nosuid,size=32m',
        '-e','PYTHONPATH=/app/03_src','-e','PYTHONDONTWRITEBYTECODE=1']
    args += ['-e','PAGE_MONITOR_BLOCKED_INPUTS='+json.dumps(config.get('_reader_errors',{}),ensure_ascii=False)]
    for role, source in config['inputs'].items():
        args += ['--mount', f'type=bind,source={source},target=/inputs/{role},readonly']
    args += [config['image_id'],'timeout','600','python','-B','04_scripts/monitoring/observe_pages.py']
    rows = json.loads(command(args, timeout=650, diagnostic=STATE_ROOT / 'last_observer.log'))
    if not isinstance(rows,list) or {r['identity'] for r in rows} != {r.identity for r in RULES} or len(rows) != len(RULES):
        raise ValueError('Monitor observer coverage differs')
    for row in rows:
        rule = next(r for r in RULES if r.identity==row['identity'])
        if row.get('job') != rule.job or row.get('manual')!=rule.manual or row.get('pages')!=list(rule.pages):
            raise ValueError('Observer requested unapproved job')
    return rows


def producer_busy():
    fields={'Id','ActiveState','SubState','MainPID','ControlPID','ControlGroup'}
    raw=command(['systemctl','show',*JOBS.values(),*['--property='+key for key in sorted(fields)]]).decode()
    observed={}
    for block in raw.strip().split('\n\n'):
        row={}
        for line in block.splitlines():
            key,separator,value=line.partition('=')
            if not separator or key in row:
                raise ValueError('Cannot determine producer activity')
            row[key]=value
        if set(row)!=fields or row['Id'] in observed:
            raise ValueError('Cannot determine producer activity')
        observed[row['Id']]=row
    if set(observed)!=set(JOBS.values()) or any(
        row['ActiveState'] not in {'active','activating','inactive','failed','deactivating','reloading'}
        or not re.fullmatch(r'[0-9]+',row['MainPID']) or not re.fullmatch(r'[0-9]+',row['ControlPID'])
        for row in observed.values()):
        raise ValueError('Cannot determine producer activity')
    for row in observed.values():
        idle=row['ActiveState'] in {'inactive','failed'}
        restart_wait=row['ActiveState']=='activating' and row['SubState']=='auto-restart'
        # systemd reports restart backoff as activating even after all workers
        # and the unit cgroup have gone. Do not let that wait starve other jobs.
        if (idle or restart_wait) and int(row['MainPID'])==0 and int(row['ControlPID'])==0 and not row['ControlGroup']:
            continue
        return True
    return False


def start_retry(job, dispatcher=None):
    if dispatcher is not None:
        if digest(protected(dispatcher['config'],Path('/etc/market-data/task-dispatcher')).read_bytes())!=dispatcher['sha256']:
            raise ValueError('Dispatcher configuration changed before enqueue')
        import importlib.util
        spec=importlib.util.spec_from_file_location('_monitor_task_dispatcher',ROOT/'09_deploy/page_monitor/dispatcher.py')
        queue=importlib.util.module_from_spec(spec);spec.loader.exec_module(queue)
        return queue.locked_run(dispatcher['config'],job=job,reason='retry')['status']
    unit = JOBS[job]
    # systemd coalesces a racing scheduled start into this same unit. Never
    # spawn a second collector, change its arguments, or bypass its CAS gates.
    try:
        # Synchronous start waits for the actual systemd job, including fast
        # no-op runs. Polling a previous inactive/success state can falsely
        # report completion before a queued job has started.
        command(['systemctl','start',unit],timeout=600)
        return 'COMPLETED'
    except subprocess.TimeoutExpired:
        return 'STILL_RUNNING'  # leave original producer running; do not duplicate/stop
    except subprocess.CalledProcessError:
        return 'FAILED'


def observe_tasks(state, rows, now):
    # Fixed read-only unit inventories; never execute a caller-provided name.
    def inventory(names, fields, *, calendars=False):
        raw = command(['systemctl', 'show', '--all', *sorted(names),
            *['--property='+field for field in sorted(fields)]]).decode('utf-8', 'strict')
        return parse_inventory(raw, set(names), fields, calendars=calendars)
    services = inventory(set(JOBS.values()), SERVICE_FIELDS)
    timers = inventory({task.timer for task in TASKS}, TIMER_FIELDS, calendars=True)
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
    return reconcile_tasks(rows, services, timers, state.get('task_ledger', {}), now, boot_id)


def publish(config, state, rows, *, failure=None):
    now = datetime.now(timezone.utc)
    state.update(reconcile(rows, state, now))
    state.update(schema_version=SCHEMA, checked_at=now.isoformat(), rows=rows,
        monitor_error=failure, producer=config['producer'], retry_policy={'max_extra_attempts_per_job_per_day':3,'cooldown_minutes':60,'max_jobs_per_pass':1})
    if config.get('task_ledger_enabled'):
        try:
            state['task_ledger'] = observe_tasks(state, rows, now)
            unverified = any(task['execution_status'] in {'UNKNOWN', 'UNAVAILABLE'}
                or task['timer_observation']['LoadState'] != 'loaded'
                for task in state['task_ledger']['tasks'])
            state['task_ledger_error'] = ('部分原服务或定时器状态未核实；暂停额外补试，原定时任务不变'
                if unverified else None)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            # Preserve the last ledger and actual-date report. Extra retries
            # stop until lifecycle evidence is verifiable; original timers run.
            state['task_ledger_error'] = '任务台账核验失败；保留上次执行证据，暂停额外补试，原定时任务不变'
    if config.get('dispatcher'):
        try:
            queue=read_json(protected('/var/lib/market-data/production-data-automation/task-dispatcher/state.json'))
            if queue.get('schema_version')!='task-dispatcher-state/1' or not set(JOBS)<=set(queue.get('jobs',{})):
                raise ValueError('Dispatcher state invalid')
            state['task_dispatcher']=queue;state['task_dispatcher_error']=None
        except (OSError,ValueError,KeyError,TypeError):
            state['task_dispatcher_error']='后台调度状态未核实；保留上次证据，不认定数据已补更新'
    # Persist the event itself before archiving. A crash can replay that exact
    # event without producing a second reminder or losing the recovery receipt.
    atomic(STATE_ROOT / 'state.json', state)
    archive_events(state.get('recent_events',[]))
    if config.get('task_ledger_enabled'):
        archive_task_events(state.get('task_ledger', {}).get('recent_observations', []))
    atomic(Path(config['report']), state)
    return state


def archive_events(events):
    archive_records(STATE_ROOT / 'events', events)


def archive_task_events(events):
    archive_records(STATE_ROOT / 'task-events', events)


def archive_records(directory, events):
    if not events:
        return
    directory.mkdir(exist_ok=True)
    for event in events:
        raw=json.dumps(event,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')
        path=directory / (digest(raw)+'.json')
        if path.exists():
            if path.read_bytes()!=raw:
                raise ValueError('Monitor immutable event differs')
            continue
        # The controller holds the host lock. Atomic replacement of a fresh
        # pathname prevents a killed writer from leaving a partial event file.
        atomic(path,event)
        path.chmod(0o444)


def run(config, *, retry):
    state_path = STATE_ROOT / 'state.json'
    state = read_json(state_path) if state_path.exists() else {}
    archive_events(state.get('recent_events',[]))
    if config.get('task_ledger_enabled'):
        archive_task_events(state.get('task_ledger', {}).get('recent_observations', []))
    rows = observe(config)
    state = publish(config,state,rows)
    controlled=bool(config.get('dispatcher'))
    ledger_blocked = not controlled and config.get('task_ledger_enabled') and state.get('task_ledger_error')
    eligible=[row for row in rows if row.get('job') not in config.get('_blocked_jobs',{})]
    plan = retry_plan(eligible,state,datetime.now(timezone.utc),busy=False if controlled else producer_busy()) if retry and not ledger_blocked else []
    for job in plan:
        if not controlled:check_job(job,config['jobs'][job])
        if not controlled and producer_busy():
            break
        state = reserve_retry(state,job,datetime.now(timezone.utc))
        atomic(state_path,state)
        atomic(Path(config['report']),state)
        try:
            state['retries'][job]['outcome'] = (start_retry(job,config['dispatcher']) if config.get('dispatcher') else start_retry(job))
            state['retries'][job]['finished_at'] = datetime.now(timezone.utc).isoformat()
            rows = observe(config)  # actual business dates, not a successful exit
            state = publish(config,state,rows)
        except Exception:
            state['retries'][job]['outcome'] = 'CHECK_FAILED'
            publish(config,state,rows,failure='重试后核验失败；保留上次数据日期，待下一轮重新核验')
            raise
    return state


def main():
    import fcntl
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--retry', action='store_true')
    args = parser.parse_args()
    config = load_config(args.config)
    protected(STATE_ROOT)
    with (STATE_ROOT / 'monitor.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        run(config,retry=args.retry)


if __name__ == '__main__':
    main()
