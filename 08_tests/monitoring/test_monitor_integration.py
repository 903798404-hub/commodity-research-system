from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys

import pandas as pd
import pytest

from agri_research_agent.monitoring import observations
from agri_research_agent.monitoring.catalog import RULES, JOBS
from agri_research_agent.monitoring.freshness import Observation, SCHEMA, reconcile, evaluate
from agri_research_agent.monitoring.observations import Inputs, Reader, collect, grouped_dates, last_date, weather_observation

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026,10,22,8,tzinfo=timezone.utc)


def load(name, path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module


def host():
    return load('monitor_host_test',ROOT/'09_deploy/page_monitor/run.py')


def inputs(tmp_path):
    return Inputs(*([tmp_path]*7))


def test_navigation_has_explicit_dependency_coverage():
    navigation=load('monitor_navigation_test',ROOT/'05_apps/navigation.py')
    keys={item.key for group in navigation.NAVIGATION_GROUPS for item in group.items}
    assert {page for rule in RULES for page in rule.pages} == keys
    assert all(rule.job is None or rule.job in JOBS for rule in RULES)


def test_excluded_weather_never_calls_observer_and_failed_domain_isolated(tmp_path,monkeypatch):
    called=[]
    def read(self,identity):
        called.append(identity)
        if identity=='fx_AUD':
            raise ValueError('corrupt native input')
        return Observation('2026-10' if identity in {'usda','oil_world'} else '2026-10-21')
    monkeypatch.setattr(Reader,'read',read)
    rows=collect(inputs(tmp_path),NOW)
    assert len(rows)==len(RULES)
    excluded={'weather_USA','weather_BRA','weather_CAN','weather_IDN','weather_MYS'}
    assert not excluded.intersection(called)
    assert {r['identity'] for r in rows if r['status']=='EXCLUDED'}==excluded
    assert next(r for r in rows if r['identity']=='fx_AUD')['status']=='ERROR'
    assert not next(r for r in rows if r['identity']=='fx_AUD')['retry_eligible']
    assert next(r for r in rows if r['identity']=='fx_CAD')['actual_date']=='2026-10-21'


def test_shared_source_reads_once_and_rethrows_original_failure(tmp_path):
    reader=Reader(inputs(tmp_path),NOW);calls=[]
    def fail():
        calls.append(1)
        raise ValueError('failed identity')
    for _ in range(2):
        with pytest.raises(ValueError,match='failed identity'):
            reader.cached('shared',fail)
    assert calls==[1]


def test_independent_series_cutoff_does_not_hide_stale_region():
    frame=pd.DataFrame({'region':['A','A','B'],'date':['2026-10-20','2026-10-21','2026-10-18']})
    value=grouped_dates(frame,['region'],'date')
    assert value.actual=='2026-10-18'
    assert sorted(value.extra['series_dates'].values())==['2026-10-18','2026-10-21']
    assert grouped_dates(frame.iloc[:0],['region'],'date').actual is None


def test_invalid_data_dates_fail_closed():
    with pytest.raises(ValueError):
        last_date(['2026-10-21','not a date'])


def test_forecast_end_in_future_is_not_proof_of_complete_14_days():
    history=pd.DataFrame({'region':['A'],'date':['2026-10-21']})
    forecast=pd.DataFrame({'region':['A']*14,'model':['EC']*14,
        'date':pd.date_range('2026-10-22',periods=14)})
    ok=weather_observation(history,forecast,'date',['region','model'],NOW)
    assert ok.actual=='2026-10-21' and ok.error is None
    broken=weather_observation(history,forecast.drop(index=3),'date',['region','model'],NOW)
    assert broken.actual=='2026-10-21' and broken.error


def test_reminders_resolve_only_after_actual_data_advances():
    rule=next(r for r in RULES if r.identity=='fx_CNY')
    old=evaluate(rule,Observation('2026-10-19'),NOW)
    first=reconcile([old],{},NOW)
    # Exit success without advancing date remains the same incident.
    same=reconcile([old],first,NOW+timedelta(minutes=15))
    assert not same['events'] and len(same['incidents'])==1
    current=evaluate(rule,Observation('2026-10-22'),NOW)
    recovered=reconcile([current],same,NOW+timedelta(minutes=30))
    assert recovered['events'][0]['kind']=='RECOVERED'


def test_atomic_failure_keeps_last_report_and_cleans_temp(tmp_path,monkeypatch):
    module=host();path=tmp_path/'report.json';path.write_bytes(b'{"old":true}')
    def reject(*args):
        raise OSError('replace failed')
    monkeypatch.setattr(module.os,'replace',reject)
    with pytest.raises(OSError):
        module.atomic(path,{'new':True})
    assert path.read_bytes()==b'{"old":true}'
    assert list(tmp_path.iterdir())==[path]


def test_retry_calls_only_original_unit_and_waits_for_completion(monkeypatch):
    module=host();calls=[]
    def command(args,**kwargs):
        calls.append(args)
        assert kwargs['timeout']==600
        return b''
    monkeypatch.setattr(module,'command',command)
    assert module.start_retry('brazil')=='COMPLETED'
    assert calls==[['systemctl','start',JOBS['brazil']]]


def test_retry_timeout_leaves_original_service_running(monkeypatch):
    module=host();calls=[]
    def command(args,**kwargs):
        calls.append(args)
        raise module.subprocess.TimeoutExpired(args,600)
    monkeypatch.setattr(module,'command',command)
    assert module.start_retry('guangxi')=='STILL_RUNNING'
    assert not any('stop' in call or 'restart' in call for call in calls)


def test_any_busy_producer_defers_extras_and_unknown_activity_fails(monkeypatch):
    module=host()
    monkeypatch.setattr(module,'command',lambda _:b'\n'.join([b'active',*([b'inactive']*(len(JOBS)-1))]))
    assert module.producer_busy()
    monkeypatch.setattr(module,'command',lambda _:b'unknown')
    with pytest.raises(ValueError):
        module.producer_busy()


def test_reservation_survives_crash_before_service_request(tmp_path,monkeypatch):
    module=host();monkeypatch.setattr(module,'STATE_ROOT',tmp_path)
    row=evaluate(next(r for r in RULES if r.identity=='fx_CNY'),Observation('2026-10-19'),NOW)
    monkeypatch.setattr(module,'observe',lambda _:[row])
    monkeypatch.setattr(module,'producer_busy',lambda:False)
    monkeypatch.setattr(module,'check_job',lambda *args:None)
    class Crash(BaseException):
        pass
    monkeypatch.setattr(module,'start_retry',lambda _:(_ for _ in ()).throw(Crash()))
    config={'report':str(tmp_path/'report.json'),'producer':{},'jobs':{'foreign_fx':{}}}
    with pytest.raises(Crash):
        module.run(config,retry=True)
    persisted=json.loads((tmp_path/'state.json').read_text(encoding='utf-8'))
    assert persisted['retries']['foreign_fx']['outcome']=='RESERVED'
    assert persisted['retries']['foreign_fx']['attempts']==1


def test_recovery_journal_replays_after_crash_without_duplicate_notification(tmp_path,monkeypatch):
    module=host();monkeypatch.setattr(module,'STATE_ROOT',tmp_path)
    row=evaluate(next(r for r in RULES if r.identity=='fx_CNY'),Observation('2026-10-19'),NOW)
    monkeypatch.setattr(module,'observe',lambda _:[row])
    archive=module.archive_events
    def fail(_):
        raise OSError('interrupted event archive')
    monkeypatch.setattr(module,'archive_events',fail)
    config={'report':str(tmp_path/'report.json'),'producer':{}}
    with pytest.raises(OSError):
        module.publish(config,{},[row])
    original=json.loads((tmp_path/'state.json').read_text(encoding='utf-8'))['recent_events'][0]
    monkeypatch.setattr(module,'archive_events',archive)
    result=module.run(config,retry=False)
    assert result['recent_events']==[original]
    assert len(list((tmp_path/'events').glob('*.json')))==1
    module.run(config,retry=False)
    assert len(list((tmp_path/'events').glob('*.json')))==1


def test_service_drift_blocks_retry_before_reservation(tmp_path,monkeypatch):
    module=host();monkeypatch.setattr(module,'STATE_ROOT',tmp_path)
    row=evaluate(next(r for r in RULES if r.identity=='fx_CNY'),Observation('2026-10-19'),NOW)
    monkeypatch.setattr(module,'observe',lambda _:[row])
    monkeypatch.setattr(module,'producer_busy',lambda:False)
    def reject(*args):
        raise ValueError('producer config changed')
    monkeypatch.setattr(module,'check_job',reject)
    started=[];monkeypatch.setattr(module,'start_retry',lambda job:started.append(job))
    config={'report':str(tmp_path/'report.json'),'producer':{},'jobs':{'foreign_fx':{}}}
    with pytest.raises(ValueError,match='config changed'):
        module.run(config,retry=True)
    assert not started
    assert not json.loads((tmp_path/'state.json').read_text(encoding='utf-8'))['retries']


def test_group_timeout_does_not_hide_unrelated_pages(monkeypatch,capsys):
    module=load('monitor_worker_test',ROOT/'04_scripts/monitoring/observe_pages.py')
    monkeypatch.setattr(sys,'argv',['observe_pages.py'])
    called=[]
    def run(args,**kwargs):
        group=args[-1];called.append(group)
        if group=='brazil_':
            raise module.subprocess.TimeoutExpired(args,120)
        rows=[evaluate(rule,Observation(None),NOW) for rule in RULES if module.group(rule.identity)==group]
        return type('Result',(),{'stdout':json.dumps(rows).encode(),'stderr':b''})()
    monkeypatch.setattr(module.subprocess,'run',run)
    module.main()
    rows=json.loads(capsys.readouterr().out)
    assert len(rows)==len(RULES) and len(called)==len(set(called))
    assert next(r for r in rows if r['identity']=='brazil_PLANTED')['status']=='ERROR'
    assert next(r for r in rows if r['identity']=='guangxi')['status']=='MISSING'
    assert {r['identity'] for r in rows if r['status']=='EXCLUDED'}=={r.identity for r in RULES if r.excluded}


@pytest.mark.parametrize('age,stale',[(10,False),(61,True)])
def test_website_detects_its_own_stale_monitor(tmp_path,age,stale,monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT/'05_apps'))
    module=load('monitor_ui_test',ROOT/'05_apps/page_monitor_page.py')
    path=tmp_path/'report.json'
    path.write_text(json.dumps({'schema_version':SCHEMA,'checked_at':(NOW-timedelta(minutes=age)).isoformat(),
        'rows':[{'identity':rule.identity,'status':'CURRENT'} for rule in RULES]}),encoding='utf-8')
    assert module.load_report(path,NOW)[1] is stale


def test_monitor_render_shows_real_date_and_exclusions(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.syspath_prepend(str(ROOT/'05_apps'))
    folder=tmp_path/'update_status';folder.mkdir()
    now=datetime.now(timezone.utc)
    rows=[evaluate(rule,Observation(now.date().isoformat()[:7] if rule.period_kind=='month' else now.date().isoformat()),now) for rule in RULES]
    (folder/'page_monitor.json').write_text(json.dumps({'schema_version':SCHEMA,'checked_at':now.isoformat(),'rows':rows}),encoding='utf-8')
    script='from pathlib import Path\nfrom page_monitor_page import render_page_monitor\nrender_page_monitor(Path('+repr(str(tmp_path))+'))'
    app=AppTest.from_string(script).run()
    assert not app.exception
    assert any('最近核验' in item.value for item in app.caption)
    app.checkbox[0].uncheck().run()
    assert not app.exception
    table=app.dataframe[0].value
    assert '按要求暂缓' in set(table['状态'])
    assert '实际数据日期' in table
