from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from agri_research_agent.monitoring.catalog import JOBS, RULES
from agri_research_agent.monitoring.freshness import Observation, evaluate
from agri_research_agent.monitoring.task_ledger import (
    BY_TASK, EXECUTION_LABELS, SERVICE_FIELDS, TASKS, TIMER_FIELDS,
    dataset_catalog, execution_state, parse_inventory, reconcile_tasks, validate_ledger,
)

NOW = datetime(2026, 10, 23, 5, 0, tzinfo=timezone.utc)
BOOT = '109b359d-6e55-4911-a665-c67735dc0a10'
ROOT = Path(__file__).resolve().parents[2]


def service(unit, **changes):
    row = dict(Id=unit, LoadState='loaded', ActiveState='inactive', SubState='dead',
        MainPID='0', ControlPID='0', ControlGroup='', InvocationID='', Result='success',
        ExecMainCode='0', ExecMainStatus='0', ExecMainStartTimestampMonotonic='0',
        ExecMainExitTimestampMonotonic='0', ExecMainStartTimestamp='', ExecMainExitTimestamp='',
        TriggeredBy=unit.removesuffix('.service')+'.timer', Job='')
    row.update(changes)
    return row


def successful(unit, **changes):
    row = service(unit, InvocationID='a'*32, ExecMainCode='1',
        ExecMainStartTimestampMonotonic='100', ExecMainExitTimestampMonotonic='200',
        ExecMainStartTimestamp='Fri 2026-10-23 12:00:00 CST',
        ExecMainExitTimestamp='Fri 2026-10-23 12:00:01 CST')
    row.update(changes)
    return row


def inputs():
    rows=[evaluate(rule, Observation('2026-10' if rule.period_kind=='month' else '2026-10-21'), NOW) for rule in RULES]
    services={task.service:service(task.service) for task in TASKS}
    timers={task.timer:dict(Id=task.timer, LoadState='loaded', ActiveState='active',
        TimersCalendar=['{ OnCalendar=*-*-* 08:30:00 Asia/Shanghai ; next_elapse=Sat 2026-10-24 08:30:00 CST }'],
        NextElapseUSecRealtime='Sat 2026-10-24 08:30:00 CST', NextElapseUSecMonotonic='0') for task in TASKS}
    return rows, services, timers


def ledger(previous=None, **changes):
    rows, services, timers=inputs()
    services[JOBS['foreign_fx']]=successful(JOBS['foreign_fx'], **changes)
    return reconcile_tasks(rows, services, timers, previous or {}, NOW, BOOT)


def host():
    spec=importlib.util.spec_from_file_location('task_ledger_host', ROOT/'09_deploy/page_monitor/run.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_inventory_preserves_all_original_policies_and_excludes_old_weather():
    records=dataset_catalog()
    assert set(BY_TASK)==set(JOBS) and len(TASKS)==13
    assert len(records)==len(RULES)==84
    assert {row['identity'] for row in records if row['mode']=='EXCLUDED'}=={
        'weather_USA','weather_CAN','weather_BRA','weather_IDN','weather_MYS'}
    assert all(row['task_id'] is None for row in records if row['mode'] in {'EXCLUDED','MANUAL','NO_UPDATER','STATIC'})
    by_id={row['identity']:row for row in records}
    for rule in RULES:
        row=by_id[rule.identity]
        assert (row['source'],row['cadence'],row['deadline'],row['active_months'],row['period_kind'])==(
            rule.source,rule.cadence,rule.deadline,rule.active_months,rule.period_kind)


@pytest.mark.parametrize('job', [
    'foreign_fx','spreads','fgis','fas','crop','brazil','canola_exports',
    'warehouse','positions_domestic','positions_foreign','guangxi','yunnan','thailand',
])
def test_each_original_module_migrates_independently_without_changing_data(job):
    task=BY_TASK[job]
    rows,services,timers=inputs();before=deepcopy(rows)
    services[task.service]=successful(task.service)
    result=reconcile_tasks(rows,services,timers,{},NOW,BOOT)
    assert rows==before
    current=next(row for row in result['tasks'] if row['identity']==task.identity)
    assert current['execution_status']=='SUCCEEDED'
    assert current['datasets']==task.datasets and current['service']==JOBS[task.identity]
    assert all(row['execution_status']=='NOT_OBSERVED' for row in result['tasks'] if row['identity']!=task.identity)
    assert validate_ledger(json.loads(json.dumps(result)),NOW)==json.loads(json.dumps(result))


def test_default_success_is_not_an_execution_and_old_data_never_becomes_current():
    rows,services,timers=inputs()
    original=deepcopy(rows)
    result=reconcile_tasks(rows,services,timers,{},NOW,BOOT)
    assert all(task['execution_status']=='NOT_OBSERVED' for task in result['tasks'])
    assert all(task['run_id'] is None for task in result['tasks'])
    services[JOBS['foreign_fx']]=successful(JOBS['foreign_fx'])
    done=reconcile_tasks(rows,services,timers,result,NOW+timedelta(minutes=1),BOOT)
    assert rows==original
    fx=next(task for task in done['tasks'] if task['identity']=='foreign_fx')
    assert fx['execution_status']=='SUCCEEDED' and fx['pending_datasets']>0


@pytest.mark.parametrize('changes,expected',[
    ({},'SUCCEEDED'),
    ({'ActiveState':'activating','SubState':'auto-restart','Result':'exit-code','ExecMainStatus':'1'},'RESTART_WAIT'),
    ({'ActiveState':'activating','SubState':'auto-restart','MainPID':'123'},'RUNNING'),
    ({'ActiveState':'activating','SubState':'start','MainPID':'123','Job':'456'},'RUNNING'),
    ({'ControlPID':'456'},'RUNNING'),
    ({'ControlGroup':'/system.slice/remaining-worker'},'RUNNING'),
    ({'ActiveState':'deactivating'},'STOPPING'),
    ({'Job':'123'},'QUEUED'),
    ({'ActiveState':'failed','Result':'exit-code','ExecMainStatus':'1'},'FAILED'),
    ({'Result':'timeout','ExecMainStatus':'15'},'FAILED'),
    ({'ExecMainExitTimestampMonotonic':'0'},'UNKNOWN'),
    ({'InvocationID':''},'UNKNOWN'),
    ({'ExecMainCode':'0'},'UNKNOWN'),
    ({'ActiveState':'unexpected'},'UNKNOWN'),
    ({'LoadState':'not-found'},'UNAVAILABLE'),
])
def test_lifecycle_requires_native_execution_evidence(changes,expected):
    assert execution_state(successful(JOBS['foreign_fx'],**changes))==expected


@pytest.mark.parametrize('changes',[
    {'MainPID':'-1'}, {'ExecMainStatus':'NaN'}, {'Job':'not-a-job'},
    {'InvocationID':'0'*32}, {'InvocationID':'bad-invocation'},
])
def test_malformed_native_evidence_is_rejected(changes):
    with pytest.raises(ValueError):execution_state(successful(JOBS['foreign_fx'],**changes))


def test_queue_does_not_reuse_previous_run_identifier_and_boots_are_distinct():
    queued=next(t for t in ledger(Job='123')['tasks'] if t['identity']=='foreign_fx')
    assert queued['execution_status']=='QUEUED' and queued['run_id'] is None
    first=ledger();rows,services,timers=inputs();services[JOBS['foreign_fx']]=successful(JOBS['foreign_fx'])
    reboot=reconcile_tasks(rows,services,timers,first,NOW+timedelta(minutes=1),'209b359d-6e55-4911-a665-c67735dc0a10')
    assert next(t for t in first['tasks'] if t['identity']=='foreign_fx')['run_id']!=next(t for t in reboot['tasks'] if t['identity']=='foreign_fx')['run_id']


def test_observations_deduplicate_and_unobserved_history_is_not_invented():
    first=ledger();rows,services,timers=inputs();services[JOBS['foreign_fx']]=successful(JOBS['foreign_fx'])
    same=reconcile_tasks(rows,services,timers,first,NOW+timedelta(minutes=15),BOOT)
    assert same['recent_observations']==first['recent_observations']
    services[JOBS['foreign_fx']]=successful(JOBS['foreign_fx'],InvocationID='b'*32)
    changed=reconcile_tasks(rows,services,timers,same,NOW+timedelta(minutes=30),BOOT)
    assert len(changed['recent_observations'])==len(first['recent_observations'])+1
    assert changed['recent_observations'][-1]['run_id'].endswith('b'*32)


def test_parser_supports_multiple_calendar_entries_but_rejects_duplicate_identity():
    rows,services,timers=inputs();unit=TASKS[0].timer;timer=timers[unit]
    raw='\n'.join(k+'='+v for k,v in timer.items() if k!='TimersCalendar')+'\nTimersCalendar=A\nTimersCalendar=B\n'
    assert parse_inventory(raw,{unit},TIMER_FIELDS,calendars=True)[unit]['TimersCalendar']==['A','B']
    with pytest.raises(ValueError):parse_inventory(raw+'\n'+raw,{unit},TIMER_FIELDS,calendars=True)
    with pytest.raises(ValueError):parse_inventory(raw.replace('LoadState=loaded\n',''),{unit},TIMER_FIELDS,calendars=True)


def test_malformed_ledger_cannot_claim_a_different_producer_or_data_policy():
    original=ledger()
    for alter in [lambda x:x['tasks'][0].update(service='other.service'),
                  lambda x:x['datasets'][0].update(source='changed source'),
                  lambda x:x['tasks'][1].update(execution_status='SUCCEEDED'),
                  lambda x:x['tasks'].pop()]:
        value=deepcopy(original);alter(value)
        with pytest.raises(ValueError):validate_ledger(value,NOW)


def test_ledger_failure_preserves_last_valid_evidence_and_blocks_extra_retries(tmp_path,monkeypatch):
    module=host();rows,_,_=inputs();state={'task_ledger':ledger()}
    monkeypatch.setattr(module,'STATE_ROOT',tmp_path)
    monkeypatch.setattr(module,'observe_tasks',lambda *args:(_ for _ in ()).throw(ValueError('incomplete unit')))
    config={'report':str(tmp_path/'report.json'),'producer':{},'task_ledger_enabled':True}
    result=module.publish(config,state,rows)
    assert result['task_ledger']==state['task_ledger'] and result['task_ledger_error']
    monkeypatch.setattr(module,'observe',lambda _:rows)
    monkeypatch.setattr(module,'producer_busy',lambda:pytest.fail('unverified lifecycle must block extra retry'))
    module.run(config,retry=True)
    assert len(list((tmp_path/'task-events').glob('*.json')))==13


def test_task_journal_replays_after_crash_before_archive_and_is_immutable(tmp_path,monkeypatch):
    module=host();rows,_,_=inputs();value=ledger()
    monkeypatch.setattr(module,'STATE_ROOT',tmp_path)
    monkeypatch.setattr(module,'observe_tasks',lambda *args:value)
    config={'report':str(tmp_path/'report.json'),'producer':{},'task_ledger_enabled':True}
    archive=module.archive_task_events
    monkeypatch.setattr(module,'archive_task_events',lambda _:(_ for _ in ()).throw(OSError('archive interrupted')))
    with pytest.raises(OSError):module.publish(config,{},rows)
    assert json.loads((tmp_path/'state.json').read_bytes())['task_ledger']==json.loads(json.dumps(value))
    monkeypatch.setattr(module,'archive_task_events',archive)
    monkeypatch.setattr(module,'observe',lambda _:rows)
    module.run(config,retry=False);module.run(config,retry=False)
    files=list((tmp_path/'task-events').glob('*.json'))
    assert len(files)==13 and all(path.stat().st_mode & 0o222==0 for path in files)


def test_disabled_ledger_does_not_change_old_config_retry_behavior(tmp_path,monkeypatch):
    module=host();monkeypatch.setattr(module,'STATE_ROOT',tmp_path)
    row=evaluate(next(rule for rule in RULES if rule.identity=='fx_CNY'),Observation('2026-10-19'),NOW)
    module.atomic(tmp_path/'state.json',{'task_ledger_error':'previous metadata issue','task_ledger':[]})
    monkeypatch.setattr(module,'observe',lambda _:[row])
    monkeypatch.setattr(module,'observe_tasks',lambda *args:pytest.fail('config/1 must not collect task state'))
    monkeypatch.setattr(module,'archive_task_events',lambda *args:pytest.fail('config/1 must ignore task metadata'))
    monkeypatch.setattr(module,'producer_busy',lambda:False)
    monkeypatch.setattr(module,'check_job',lambda *args:None)
    started=[]
    monkeypatch.setattr(module,'start_retry',lambda job:started.append(job) or 'COMPLETED')
    result=module.run({'producer':{},'report':str(tmp_path/'report.json'),'jobs':{'foreign_fx':{}}},retry=True)
    assert started==['foreign_fx'] and result['retries']['foreign_fx']['outcome']=='COMPLETED'


def test_host_observation_uses_only_the_fixed_service_and_timer_whitelists(monkeypatch):
    module=host();rows,services,timers=inputs();calls=[]
    def raw(values):
        blocks=[]
        for row in values.values():
            blocks.append('\n'.join(key+'='+item for key,value in row.items() for item in (value if isinstance(value,list) else [value])))
        return ('\n\n'.join(blocks)+'\n').encode()
    def command(args):
        calls.append(args)
        assert args[:3]==['systemctl','show','--all']
        names={value for value in args[3:] if not value.startswith('--property=')}
        if names==set(services):return raw(services)
        assert names==set(timers)
        return raw(timers)
    monkeypatch.setattr(module,'command',command)
    original=Path.read_text
    monkeypatch.setattr(Path,'read_text',lambda path,*a,**kw:BOOT if str(path).replace('\\','/')=='/proc/sys/kernel/random/boot_id' else original(path,*a,**kw))
    result=module.observe_tasks({},rows,NOW)
    assert len(calls)==2 and len(result['tasks'])==13
    assert all(task['execution_status']=='NOT_OBSERVED' for task in result['tasks'])


def test_task_ui_keeps_success_separate_from_stale_data_and_old_reports_work(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.syspath_prepend(str(ROOT/'05_apps'))
    rows,services,timers=inputs();services[JOBS['foreign_fx']]=successful(JOBS['foreign_fx'])
    now=datetime.now(timezone.utc);value=reconcile_tasks(rows,services,timers,{},now,BOOT)
    folder=tmp_path/'update_status';folder.mkdir()
    from agri_research_agent.monitoring.freshness import SCHEMA
    payload=dict(schema_version=SCHEMA,checked_at=now.isoformat(),rows=rows,task_ledger=value)
    path=folder/'page_monitor.json';path.write_text(json.dumps(payload),encoding='utf-8')
    script='from pathlib import Path\nfrom page_monitor_page import render_page_monitor\nrender_page_monitor(Path('+repr(str(tmp_path))+'))'
    app=AppTest.from_string(script).run();assert not app.exception
    table=app.dataframe[1].value
    fx=table.loc[table['任务']=='外盘汇率'].iloc[0]
    assert fx['执行状态']=='原服务执行完成' and '待处理或核实' in fx['数据核验']
    assert len(table)==13
    registry=next(item.value for item in app.dataframe if '接入方式' in item.value.columns)
    assert '按要求暂缓' in set(registry['接入方式'])
    payload.pop('task_ledger');path.write_text(json.dumps(payload),encoding='utf-8')
    app.run();assert not app.exception
    assert any('任务台账尚未提供' in x.value for x in app.info)
    payload['task_ledger']=[];path.write_text(json.dumps(payload),encoding='utf-8')
    app.run();assert not app.exception
    assert any('任务台账回执校验失败' in x.value for x in app.warning)
    assert '实际数据日期' in app.dataframe[0].value.columns
