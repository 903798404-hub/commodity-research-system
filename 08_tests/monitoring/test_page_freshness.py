from datetime import date, datetime, timezone, timedelta
import pytest

from agri_research_agent.monitoring.freshness import (
    Observation, Rule, evaluate, expected_period, reconcile, reserve_retry, retry_plan,
)

NOW = datetime(2026, 10, 22, 8, tzinfo=timezone.utc)
DAYS = [date(2026, 9, 30), date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)]


def rule(**kwargs):
    return Rule("series", "测试", ("page",), "原来源", **kwargs)


def test_retired_source_is_paused_with_real_cutoff_and_never_retried():
    r = rule(source_paused=True, job="old_source")
    row = evaluate(r, Observation("2026-08-31"), NOW)
    assert row['status'] == 'SOURCE_PAUSED'
    assert row['actual_date'] == '2026-08-31' and row['expected_check_date'] is None
    assert not row['retry_eligible'] and retry_plan([row], {}, NOW) == []
    failed = evaluate(r, Observation(None, error="原历史损坏"), NOW)
    assert not failed['retry_eligible'] and failed['status'] == 'SOURCE_UNVERIFIED'


def test_pausing_an_old_incident_does_not_claim_the_data_recovered():
    initial = evaluate(rule(job="old_source"), Observation("2026-08-31"), NOW)
    previous = reconcile([initial], {}, NOW)
    paused = evaluate(rule(source_paused=True), Observation("2026-08-31"), NOW)
    changed = reconcile([paused], previous, NOW + timedelta(minutes=15))
    assert changed['incidents'] == {} and changed['events'][0]['kind'] == 'SOURCE_PAUSED'
    assert all(event['kind'] != 'RECOVERED' for event in changed['events'])
    assert not reconcile([paused], changed, NOW + timedelta(minutes=30))['events']


def test_trading_calendar_holiday_and_deadline():
    r=rule(cadence="china_daily",deadline="18:00")
    assert expected_period(r,NOW,DAYS)=="2026-10-21"
    holiday=datetime(2026,10,5,12,tzinfo=timezone.utc)
    assert expected_period(r,holiday,DAYS)=="2026-09-30"
    assert expected_period(r,NOW+timedelta(hours=3),DAYS)=="2026-10-22"


def test_calendar_out_of_coverage_never_guesses_weekday():
    row=evaluate(rule(cadence="china_daily"),Observation("2026-10-21"),NOW,[date(2025,12,31)])
    assert row["status"]=="SOURCE_UNVERIFIED"


@pytest.mark.parametrize("cadence,weekday,lag,expected", [
    ("weekly",1,2,"2026-10-18"),
    ("weekly",1,5,"2026-10-15"),
    ("weekly",4,8,"2026-10-08"),
    ("weekly",5,1,"2026-10-16"),
])
def test_weekly_windows_preserve_different_report_cutoffs(cadence,weekday,lag,expected):
    assert expected_period(rule(cadence=cadence,publish_weekday=weekday,data_lag_days=lag,deadline="07:15"),NOW)==expected


@pytest.mark.parametrize("cadence,expected",[("monthly","2026-10"),("quarterly","2026-09")])
def test_month_and_quarter_keep_period_identity(cadence,expected):
    r=rule(cadence=cadence,period_kind="month",deadline="00:00",manual=True)
    assert expected_period(r,NOW)==expected
    row=evaluate(r,Observation("2026-06"),NOW)
    assert row["status"]=="WAITING_MANUAL" and not row["retry_eligible"]


def test_completed_summer_indicator_is_not_reported_as_october_missed_update():
    r=rule(cadence="weekly",active_months=(4,5,6),job="crop")
    row=evaluate(r,Observation("2026-06-14"),NOW)
    assert row["status"]=="SEASON_COMPLETE" and not row["retry_eligible"]


def test_new_verified_source_still_takes_priority_over_seasonal_window():
    r=rule(active_months=(4,5,6),job="crop")
    assert evaluate(r,Observation("2026-06-07","2026-06-14"),NOW)["status"]=="MISSED"


def test_successful_task_timestamp_cannot_mask_stale_business_date():
    row=evaluate(rule(job="safe_job",deadline="00:00"),Observation("2026-10-01",extra={"task_result":"success","task_finished_at":NOW.isoformat()}),NOW)
    assert row["status"]=="CHECK_DUE" and row["retry_eligible"]
    assert row["actual_date"]=="2026-10-01"


def test_confirmed_source_lag_is_different_from_unverified_check_due():
    r=rule(job="safe_job",deadline="00:00")
    assert evaluate(r,Observation("2026-10-01","2026-10-15"),NOW)["status"]=="MISSED"
    assert evaluate(r,Observation("2026-10-01"),NOW)["status"]=="CHECK_DUE"
    assert evaluate(r,Observation("2026-10-01","2026-10-01",NOW.isoformat()),NOW)["status"]=="WAITING_FOR_SOURCE"
    old=(NOW-timedelta(days=8)).isoformat()
    assert evaluate(r,Observation("2026-10-01","2026-10-01",old),NOW)["status"]=="CHECK_DUE"


def test_corruption_and_future_business_date_stop_automatic_retry():
    r=rule(job="safe_job",deadline="00:00")
    row=evaluate(r,Observation(None,error="正式数据身份校验失败",retry_safe=False),NOW)
    assert row["status"]=="ERROR" and not row["retry_eligible"]
    row=evaluate(r,Observation("2026-10-23"),NOW)
    assert row["status"]=="ERROR" and not row["retry_eligible"]


def test_source_clock_must_be_aware_and_not_from_future():
    r=rule(job="safe_job")
    for checked in ["2026-10-22T08:00:00",(NOW+timedelta(hours=1)).isoformat()]:
        assert evaluate(r,Observation("2026-10-01",source_checked_at=checked),NOW)["status"]=="ERROR"


def test_excluded_series_never_uses_observation_or_retry():
    row=evaluate(rule(excluded=True,job="must_not_run"),Observation("invalid",error="do not inspect"),NOW)
    assert row["status"]=="EXCLUDED" and row["actual_date"] is None
    assert retry_plan([row],{},NOW)==[]


def test_stable_incident_deduplication_and_recovery_event():
    r=rule(job="safe_job",deadline="00:00")
    row=evaluate(r,Observation("2026-10-01","2026-10-15"),NOW)
    first=reconcile([row],{},NOW)
    again=reconcile([row],first,NOW+timedelta(minutes=15))
    assert len(first["events"])==1 and again["events"]==[]
    assert again["incidents"]["series"]["first_seen_at"]==NOW.isoformat()
    current=evaluate(r,Observation("2026-10-22"),NOW)
    recovered=reconcile([current],again,NOW+timedelta(minutes=30))
    assert recovered["incidents"]=={} and recovered["events"][0]["kind"]=="RECOVERED"


def test_bounded_retry_reservation_cooldown_and_next_day():
    row=evaluate(rule(job="safe_job",deadline="00:00"),Observation("2026-10-01"),NOW)
    assert retry_plan([row,row],{},NOW)==["safe_job"]
    state=reserve_retry({},"safe_job",NOW)
    assert retry_plan([row],state,NOW+timedelta(minutes=59))==[]
    state=reserve_retry(state,"safe_job",NOW+timedelta(hours=1))
    state=reserve_retry(state,"safe_job",NOW+timedelta(hours=2))
    assert retry_plan([row],state,NOW+timedelta(hours=5))==[]
    assert retry_plan([row],state,NOW+timedelta(days=1))==["safe_job"]


def test_active_producer_blocks_all_extra_retry():
    row=evaluate(rule(job="safe_job",deadline="00:00"),Observation("2026-10-01"),NOW)
    assert retry_plan([row],{},NOW,busy=True)==[]
