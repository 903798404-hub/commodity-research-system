from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('dispatcher_test',ROOT/'09_deploy/page_monitor/dispatcher.py')
queue=importlib.util.module_from_spec(spec);spec.loader.exec_module(queue)
NOW=datetime(2026,10,28,1,tzinfo=timezone.utc)


def observation(job,**changes):
    row=dict(Id=queue.SERVICES[job],LoadState='loaded',ActiveState='inactive',SubState='dead',
        MainPID='0',ControlPID='0',ControlGroup='',InvocationID='',Result='success',ExecMainCode='0',
        ExecMainStatus='0',ExecMainStartTimestampMonotonic='0',ExecMainExitTimestampMonotonic='0',
        ExecMainStartTimestamp='',ExecMainExitTimestamp='',TriggeredBy='',Job='')
    row.update(changes);return row


def observations():return {job:observation(job) for job in queue.SERVICES}


def completed(job,success=True,invocation='a'*32):
    return observation(job,InvocationID=invocation,ExecMainCode='1',ExecMainStatus='0' if success else '1',
        Result='success' if success else 'exit-code',ActiveState='inactive' if success else 'failed',
        ExecMainStartTimestampMonotonic='100',ExecMainExitTimestampMonotonic='200',
        ExecMainStartTimestamp='actual start',ExecMainExitTimestamp='actual exit')


@pytest.mark.parametrize('job',list(queue.JOBS))
def test_each_original_job_queues_without_changing_its_entry(job):
    state=queue.empty_state(queue.SERVICES);before=deepcopy(state)
    state,status=queue.enqueue(state,job,'scheduled',NOW)
    assert status=='QUEUED' and before==queue.empty_state(queue.SERVICES)
    result,starts=queue.advance(state,observations(),'boot',NOW)
    assert starts==[job] and result['jobs'][job]['active']['reason']=='scheduled'
    assert set(queue.JOBS)==set(state['jobs'])-{'precompute'}


def test_excluded_weather_never_enters_queue():
    for job in ['weather_USA','weather_CAN','weather_BRA','weather_IDN','weather_MYS']:
        with pytest.raises(ValueError):queue.enqueue(queue.empty_state(queue.SERVICES),job,'scheduled',NOW)


def test_coalesces_requests_and_separates_weather_from_one_standard_writer():
    state=queue.empty_state(queue.SERVICES)
    for job in ['precompute','positions_foreign','positions_domestic','guangxi','yunnan']:
        state,_=queue.enqueue(state,job,'scheduled',NOW)
    again,status=queue.enqueue(state,'guangxi','retry',NOW)
    assert status=='COALESCED' and again==state
    state,starts=queue.advance(state,observations(),'boot',NOW)
    assert len(starts)==2 and 'guangxi' in starts and 'precompute' not in starts
    assert sum(queue.lane(job)=='standard' for job in starts)==1


def test_old_success_and_crash_after_reservation_never_claim_completion():
    state=queue.empty_state(queue.SERVICES);obs=observations();obs['fgis']=completed('fgis')
    state,_=queue.enqueue(state,'fgis','scheduled',NOW)
    state,starts=queue.advance(state,obs,'boot',NOW)
    assert starts==['fgis']
    state,starts=queue.advance(state,obs,'boot',NOW+timedelta(seconds=30))
    assert starts==[] and state['jobs']['fgis']['last'] is None
    state,starts=queue.advance(state,obs,'boot',NOW+timedelta(minutes=3))
    assert starts==[] and state['jobs']['fgis']['last']['status']=='START_UNCONFIRMED'
    assert state['jobs']['fgis']['last']['invocation_id'] is None


def test_actual_new_invocation_is_required_for_success():
    state=queue.empty_state(queue.SERVICES);state,_=queue.enqueue(state,'brazil','scheduled',NOW)
    state,_=queue.advance(state,observations(),'boot',NOW)
    obs=observations();obs['brazil']=completed('brazil')
    state,starts=queue.advance(state,obs,'boot',NOW+timedelta(minutes=1))
    assert starts==[] and state['jobs']['brazil']['last']['status']=='SUCCEEDED'
    assert state['jobs']['brazil']['last']['invocation_id']=='a'*32


def test_real_failure_retries_three_times_with_hour_cooldown():
    state=queue.empty_state(queue.SERVICES);obs=observations();obs['fgis']=completed('fgis',False)
    state,_=queue.advance(state,obs,'boot',NOW)
    state,starts=queue.advance(state,obs,'boot',NOW+timedelta(minutes=59))
    assert starts==[]
    for attempt in range(1,4):
        moment=NOW+timedelta(hours=attempt,seconds=attempt*2)
        state,starts=queue.advance(state,obs,'boot',moment)
        assert starts==['fgis'] and state['jobs']['fgis']['retries']['attempts']==attempt
        obs['fgis']=completed('fgis',False,invocation=str(attempt)*32)
        state,_=queue.advance(state,obs,'boot',moment+timedelta(seconds=1))
    state,starts=queue.advance(state,obs,'boot',NOW+timedelta(hours=5))
    assert starts==[] and state['jobs']['fgis']['retries']['attempts']==3


def test_external_running_writer_occupies_lane_without_being_stopped():
    state=queue.empty_state(queue.SERVICES);state,_=queue.enqueue(state,'positions_foreign','scheduled',NOW)
    obs=observations();obs['positions_domestic']=observation('positions_domestic',MainPID='123',ActiveState='active',SubState='running',InvocationID='b'*32)
    state,starts=queue.advance(state,obs,'boot',NOW)
    assert not starts and state['jobs']['positions_foreign']['pending'] is not None


def test_boot_change_never_reuses_previous_success():
    state=queue.empty_state(queue.SERVICES);state,_=queue.enqueue(state,'fgis','scheduled',NOW)
    state,_=queue.advance(state,observations(),'old',NOW)
    obs=observations();obs['fgis']=completed('fgis')
    state,starts=queue.advance(state,obs,'new',NOW+timedelta(seconds=30))
    assert not starts and state['jobs']['fgis']['last']['status']=='INTERRUPTED'


def test_unknown_original_service_is_quarantined_without_stopping_other_modules():
    state=queue.empty_state(queue.SERVICES);obs=observations();obs['fgis']['LoadState']='not-found'
    state,_=queue.enqueue(state,'fgis','scheduled',NOW)
    state,_=queue.enqueue(state,'brazil','scheduled',NOW)
    result,starts=queue.advance(state,obs,'boot',NOW)
    assert starts==['brazil'] and 'fgis' in result['blocked_jobs']
    assert result['jobs']['fgis']['pending'] is not None


def test_one_changed_binding_does_not_block_an_independent_weather_job():
    state=queue.empty_state(queue.SERVICES)
    for job in ['fgis','brazil','guangxi']:state,_=queue.enqueue(state,job,'scheduled',NOW)
    result,starts=queue.advance(state,observations(),'boot',NOW,blocked={'fgis':'configuration changed'})
    assert set(starts)=={'brazil','guangxi'} and result['blocked_jobs']=={'fgis':'configuration changed'}


def test_quarantine_and_rebind_events_are_deduplicated_without_claiming_data_recovery():
    state=queue.empty_state(queue.SERVICES);obs=observations()
    state,_=queue.advance(state,obs,'boot',NOW,blocked={'fgis':'changed'})
    count=len(state['events'])
    state,_=queue.advance(state,obs,'boot',NOW+timedelta(seconds=30),blocked={'fgis':'changed'})
    assert len(state['events'])==count
    state,_=queue.advance(state,obs,'boot',NOW+timedelta(seconds=60))
    assert state['events'][-1]['kind']=='TASK_REBOUND' and state['jobs']['fgis']['last'] is None


def test_timer_migration_changes_target_only_without_inventing_a_calendar():
    original='[Unit]\r\nDescription=原更新入口\r\n[Timer]\r\n# 原始日历\r\nOnCalendar=*-*-* 08:30:00 Asia/Shanghai\r\nPersistent=true\r\nAccuracySec=1s\r\n Unit = original.service \r\n[Install]\r\nWantedBy=timers.target\r\n'
    for job in queue.JOBS:
        text=queue.retarget_timer_unit(original,job)
        assert text.replace('market-data-task-enqueue@'+job+'.service','original.service')==original
        assert text.count('Unit = ')==1
    with pytest.raises(ValueError):queue.retarget_timer_unit(original,'weather_USA')


@pytest.mark.parametrize('original',[
    '[Timer]\nOnCalendar=daily\n',
    '[Timer]\nUnit=one.service\nUnit=two.service\n',
    '[Timer]\nUnit=one.service\n[Timer]\nOnCalendar=daily\n',
    '[Service]\nUnit=one.service\n',
    '[Timer]\nUnit=one.service two.service\n',
    '[Timer]\nUnit=../one.service\n',
    '[Timer]\nUnit=one.service\x00\n',
])
def test_timer_migration_rejects_ambiguous_or_invalid_fragments(original):
    with pytest.raises(ValueError):queue.retarget_timer_unit(original,'fgis')


def test_timer_migration_preserves_unrelated_sections_comments_and_final_newline():
    original='[Unit]\n# Unit=comment.service\nDescription=unchanged\n[Timer]\nOnCalendar=Tue..Fri *-*-* 07:15:00 Asia/Shanghai\nPersistent=yes\nUnit=original.service'
    result=queue.retarget_timer_unit(original,'fgis')
    assert result==original.removesuffix('original.service')+'market-data-task-enqueue@fgis.service'


def test_dispatcher_retains_real_service_observations_without_starting_producers():
    units=queue.render_units('/opt/market-data/source','/etc/market-data/task-dispatcher/accepted.json')
    keep=units['market-data-task-observations.target']
    assert keep.splitlines()==['[Unit]','Description=Retain original task execution observations','After='+' '.join(queue.SERVICES.values())]
    assert 'Requires=' not in keep and 'Wants=' not in keep and 'ExecStart=' not in keep
    controller=units['market-data-task-dispatcher.service']
    assert 'Requires=docker.service market-data-task-observations.target\n' in controller
    assert 'After=docker.service market-data-task-observations.target\n' in controller
    assert all(name not in controller for name in queue.SERVICES.values())


def test_daily_retry_budget_resets_on_beijing_day_without_ignoring_cooldown():
    state=queue.empty_state(queue.SERVICES);row=state['jobs']['fgis']
    row['retries']={'day':'2026-10-28','attempts':3,'reserved_at':'2026-10-28T15:30:00+00:00'}
    assert not queue.retry_allowed(row,datetime(2026,10,28,16,0,tzinfo=timezone.utc))
    assert queue.retry_allowed(row,datetime(2026,10,28,16,30,tzinfo=timezone.utc))
