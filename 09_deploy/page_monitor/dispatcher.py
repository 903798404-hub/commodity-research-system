"""Durable admission queue for the existing fixed, protected systemd writers.

The queue never collects or publishes data. Original updater arguments, source
checks, locks, validation and CAS publication remain authoritative.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'03_src'))
from agri_research_agent.monitoring.catalog import JOBS
from agri_research_agent.monitoring.task_ledger import SERVICE_FIELDS, execution_state, parse_inventory

SERVICES={**JOBS,'precompute':'market-data-page-precompute.service'}
STATE_ROOT=Path('/var/lib/market-data/production-data-automation/task-dispatcher')
CONFIG_ROOT=Path('/etc/market-data/task-dispatcher')
SCHEMA='task-dispatcher-state/1'
BUSY={'RUNNING','QUEUED','STOPPING','RESTART_WAIT'}


def stamp(value):
    result=datetime.fromisoformat(value) if isinstance(value,str) else value
    if result.tzinfo is None or result.utcoffset() is None:raise ValueError('Aware queue clock required')
    return result.astimezone(timezone.utc)


def lane(job):return 'weather' if job in {'guangxi','yunnan','thailand'} else 'standard'


def render_units(source,config):
    if any(not re.fullmatch(r'/[A-Za-z0-9_./-]+',p) or '..' in p.split('/') for p in [source,config]):
        raise ValueError('Dispatcher unit path invalid')
    command=f'/usr/bin/python3 -I -B {source}/09_deploy/page_monitor/dispatcher.py --config {config}'
    service='[Unit]\nDescription=Bounded admission to original market-data writers\nAfter=docker.service\nRequires=docker.service\n\n[Service]\nType=oneshot\nExecStart='+command+'\nTimeoutStartSec=90\nNice=10\nNoNewPrivileges=true\nPrivateTmp=true\nUMask=0022\n'
    enqueue='[Unit]\nDescription=Queue original market-data task %i\n\n[Service]\nType=oneshot\nExecStart='+command+' --enqueue %i\nTimeoutStartSec=90\nNoNewPrivileges=true\nPrivateTmp=true\nUMask=0022\n'
    timer='[Unit]\nDescription=Reconcile persisted market-data queue\n\n[Timer]\nOnBootSec=30s\nOnUnitActiveSec=30s\nAccuracySec=1s\nUnit=market-data-task-dispatcher.service\n\n[Install]\nWantedBy=timers.target\n'
    return {'market-data-task-dispatcher.service':service,'market-data-task-dispatcher.timer':timer,'market-data-task-enqueue@.service':enqueue}


def timer_dropin(job):
    if job not in SERVICES:raise ValueError('Unknown queue task')
    return '[Timer]\nUnit=market-data-task-enqueue@'+job+'.service\n'


def empty_state(jobs):
    if not set(jobs)<=set(SERVICES) or not set(JOBS)<=set(jobs):raise ValueError('Unknown or missing queue task')
    return {'schema_version':SCHEMA,'jobs':{job:{'pending':None,'active':None,'last':None,'retries':{}} for job in jobs},'events':[]}


def event(state,job,kind,now,**details):
    row={'id':uuid4().hex,'job':job,'kind':kind,'at':stamp(now).isoformat(),**details}
    state['events']=(state['events']+[row])[-64:]


def retry_allowed(row,now):
    now=stamp(now);day=now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
    retry=row['retries']
    if retry.get('day')==day and retry.get('attempts',0)>=3:return False
    latest=retry.get('reserved_at')
    if latest and now-stamp(latest)<timedelta(hours=1):return False
    finished=(row.get('last') or {}).get('finished_at')
    return not finished or now-stamp(finished)>=timedelta(hours=1)


def enqueue(state,job,reason,now):
    if job not in state['jobs'] or reason not in {'scheduled','retry','manual'}:
        raise ValueError('Unknown queue task or reason')
    state=deepcopy(state);row=state['jobs'][job]
    if row['pending'] is not None:return state,'COALESCED'
    if row['active'] is not None:return state,'ALREADY_RUNNING'
    if reason=='retry' and not retry_allowed(row,now):return state,'RETRY_LIMIT_OR_COOLDOWN'
    row['pending']={'id':uuid4().hex,'reason':reason,'enqueued_at':stamp(now).isoformat()}
    event(state,job,'ENQUEUED',now,reason=reason)
    return state,'QUEUED'


def advance(state,observations,boot_id,now,*,blocked=None):
    """Plan starts after reconciling actual invocation evidence, without IO.

    Caller must persist every claim before asking systemd to start anything.
    One standard writer plus one weather writer keeps independent weather from
    waiting behind a long export download. Same-domain writers share the lane.
    """
    state=deepcopy(state);now=stamp(now);requests=[]
    if state.get('schema_version')!=SCHEMA or set(observations)!=set(state['jobs']):
        raise ValueError('Queue observation scope differs')
    statuses={job:execution_state(value) for job,value in observations.items()}
    blocked=dict(blocked or {})
    if not set(blocked)<=set(state['jobs']):raise ValueError('Unknown quarantined task')
    blocked.update({job:'原服务执行状态未核实' for job,status in statuses.items() if status in {'UNKNOWN','UNAVAILABLE'}})
    state['blocked_jobs']=blocked
    for job,row in state['jobs'].items():
        if job in blocked:continue
        observed=observations[job];active=row['active'];status=statuses[job]
        if active is not None:
            changed=bool(observed['InvocationID']) and observed['InvocationID']!=active['baseline_invocation']
            outcome=None
            if active['boot_id']!=boot_id:outcome='INTERRUPTED'
            elif changed and status in {'SUCCEEDED','FAILED'}:outcome=status
            elif not changed and status not in BUSY and now-stamp(active['requested_at'])>timedelta(minutes=2):
                outcome='START_UNCONFIRMED'
            if outcome:
                row['last']={**active,'status':outcome,'invocation_id':observed['InvocationID'] if changed else None,
                    'finished_at':now.isoformat(),'actual_start':observed['ExecMainStartTimestamp'] if changed else '',
                    'actual_exit':observed['ExecMainExitTimestamp'] if changed else ''}
                row['active']=None;event(state,job,outcome,now,request_id=active['id'])
        elif row['last'] is None and status=='FAILED':
            # Adopt only an actually failed original invocation, never the
            # default inactive/success fields. A new hour cooldown starts here.
            row['last']={'status':'FAILED','invocation_id':observed['InvocationID'],
                'finished_at':now.isoformat(),'adopted_existing_failure':True}
            event(state,job,'EXISTING_FAILURE_OBSERVED',now,invocation_id=observed['InvocationID'])
        last=row['last'] or {}
        if last.get('status') in {'FAILED','INTERRUPTED','START_UNCONFIRMED'} and not row['active'] and not row['pending'] and retry_allowed(row,now):
            row['pending']={'id':uuid4().hex,'reason':'retry','enqueued_at':now.isoformat()}
            event(state,job,'ENQUEUED',now,reason='retry')
    occupied={lane(job) for job,status in statuses.items() if status in BUSY}
    occupied.update(lane(job) for job,value in observations.items() if job in blocked and
        (value['MainPID']!='0' or value['ControlPID']!='0' or value['ControlGroup'] or value['Job']))
    occupied.update(lane(job) for job,row in state['jobs'].items() if row['active'])
    pending=sorted((job for job,row in state['jobs'].items() if row['pending']),
        key=lambda job:(job=='precompute',lane(job)!='weather',state['jobs'][job]['pending']['enqueued_at'],job))
    for job in pending:
        if job in blocked:continue
        row=state['jobs'][job];category=lane(job)
        if category in occupied or statuses[job] in BUSY:continue
        request=row['pending']
        if request['reason']=='retry':
            if not retry_allowed(row,now):continue
            day=now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat();old=row['retries']
            row['retries']={'day':day,'attempts':(old.get('attempts',0) if old.get('day')==day else 0)+1,'reserved_at':now.isoformat()}
        row['active']={**request,'requested_at':now.isoformat(),'boot_id':boot_id,
            'baseline_invocation':observations[job]['InvocationID']}
        row['pending']=None;occupied.add(category);requests.append(job)
        event(state,job,'START_REQUESTED',now,request_id=request['id'],reason=request['reason'])
    state['checked_at']=now.isoformat();state['boot_id']=boot_id
    state['limits']={'standard_writers':1,'weather_writers':1,'max_extra_retries_per_day':3,'retry_cooldown_minutes':60}
    return state,requests


def monitor_module():
    spec=importlib.util.spec_from_file_location('_dispatcher_monitor_host',ROOT/'09_deploy/page_monitor/run.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def load_config(path):
    host=monitor_module();config=host.read_json(host.protected(path,CONFIG_ROOT))
    if set(config)!={'schema_version','producer','data_disk_uuid','jobs','precompute_enabled'} or config['schema_version']!='task-dispatcher-config/1':
        raise ValueError('Invalid dispatcher config')
    if type(config['precompute_enabled']) is not bool:raise ValueError('Explicit precompute boolean required')
    enabled={**JOBS,**({'precompute':SERVICES['precompute']} if config['precompute_enabled'] else {})}
    if set(config['jobs'])!=set(enabled):raise ValueError('Dispatcher must bind the fixed task inventory')
    producer=config['producer']
    if set(producer)!={'commit','tree','origin'}:raise ValueError('Dispatcher producer identity invalid')
    if producer['origin'] not in {'https://github.com/903798404-hub/commodity-research-system.git','git@github.com:903798404-hub/commodity-research-system.git'}:
        raise ValueError('Dispatcher origin invalid')
    for key,args in [('commit',['rev-parse','HEAD']),('tree',['rev-parse','HEAD^{tree}']),('origin',['remote','get-url','origin'])]:
        if host.command(['git','-C',str(host.protected(ROOT)),*args]).decode().strip()!=producer[key]:raise ValueError('Dispatcher source differs')
    if host.command(['git','-C',str(ROOT),'status','--porcelain','--untracked-files=all']).strip():raise ValueError('Dispatcher source dirty')
    host.protected(STATE_ROOT)
    if host.command(['findmnt','-n','-o','UUID','-T',str(STATE_ROOT)]).decode().strip()!=config['data_disk_uuid']:
        raise ValueError('Dispatcher data disk identity differs')
    blocked={}
    for job,binding in config['jobs'].items():
        try:
            if job in JOBS:host.check_job(job,binding)
            else:
                if set(binding)!={'unit_sha256','files'} or not binding['files']:raise ValueError('Precompute binding invalid')
                if host.digest(host.command(['systemctl','cat',SERVICES[job]]))!=binding['unit_sha256']:raise ValueError('Precompute service changed')
                for filename,digest in binding['files'].items():
                    if host.digest(host.protected(filename,Path('/etc/market-data')).read_bytes())!=digest:raise ValueError('Precompute config changed')
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
            blocked[job]='原服务或配置身份未核实，暂停此任务'
    config['_blocked_jobs']=blocked
    return host,config


def locked_run(config_path,*,job=None,reason='scheduled'):
    import fcntl
    host,config=load_config(config_path)
    with (STATE_ROOT/'queue.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=STATE_ROOT/'state.json'
        state=host.read_json(host.protected(path)) if path.exists() else empty_state(config['jobs'])
        if state.get('schema_version')!=SCHEMA or set(state['jobs'])!=set(config['jobs']):raise ValueError('Dispatcher state inventory differs')
        now=datetime.now(timezone.utc)
        if job is not None:
            state,outcome=enqueue(state,job,reason,now);host.atomic(path,state)
            archive_events(host,state);return {'status':outcome,'job':job}
        units={SERVICES[task] for task in config['jobs']}
        raw=host.command(['systemctl','show','--all',*sorted(units),*['--property='+p for p in sorted(SERVICE_FIELDS)]]).decode('utf-8','strict')
        inventory=parse_inventory(raw,units,SERVICE_FIELDS)
        observations={task:inventory[SERVICES[task]] for task in config['jobs']}
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        state,requests=advance(state,observations,boot,now,blocked=config['_blocked_jobs'])
        host.atomic(path,state)  # crash-safe reservation before any start
        archive_events(host,state)
        for task in requests:
            try:host.command(['systemctl','start','--no-block',SERVICES[task]])
            except subprocess.SubprocessError:
                event(state,task,'START_COMMAND_FAILED',datetime.now(timezone.utc))
        host.atomic(path,state);archive_events(host,state)
        return {'status':'CHECKED','requested_starts':requests,'checked_at':now.isoformat()}


def archive_events(host,state):
    directory=STATE_ROOT/'events';directory.mkdir(exist_ok=True)
    host.archive_records(directory,state['events'])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True)
    parser.add_argument('--enqueue',choices=tuple(SERVICES));parser.add_argument('--reason',choices=['scheduled','retry','manual'],default='scheduled')
    args=parser.parse_args();print(json.dumps(locked_run(args.config,job=args.enqueue,reason=args.reason)))


if __name__=='__main__':main()
