"""Monitoring policy only; never calculate or publish research data."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEMA = "page-business-date-monitor/1"
QUIET = {"CURRENT", "EXCLUDED", "STATIC", "SEASON_COMPLETE", "REFERENCE"}
RETRY_STATES = {"MISSED", "CHECK_DUE", "WAITING_FOR_SOURCE", "SOURCE_UNVERIFIED"}


@dataclass(frozen=True)
class Rule:
    identity: str
    label: str
    pages: tuple[str, ...]
    source: str
    cadence: str = "daily"
    job: str | None = None
    deadline: str = "18:00"
    publish_weekday: int = 1
    data_lag_days: int = 2
    manual: bool = False
    excluded: bool = False
    active_months: tuple[int, ...] = ()
    period_kind: str = "date"
    business_lag_days: int = 0

    def __post_init__(self):
        if self.cadence not in {"daily","static","on_demand","china_daily","weekly","monthly","quarterly","weekday_daily","weather"}:
            raise ValueError("Unknown monitoring cadence")
        time.fromisoformat(self.deadline)
        if not self.pages or not 0 <= self.publish_weekday <= 6 or self.period_kind not in {"date","month"}:
            raise ValueError("Invalid monitoring identity or period")


@dataclass(frozen=True)
class Observation:
    actual: str | None
    source_actual: str | None = None
    source_checked_at: str | None = None
    error: str | None = None
    retry_safe: bool = True
    season_complete: bool = False
    extra: Mapping[str, object] = field(default_factory=dict)


def aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ValueError("Monitoring timestamps must include their timezone")
    return parsed.astimezone(timezone.utc)


def ordinal(value: str, kind: str = "date") -> int:
    if kind == "month":
        if len(value) != 7:
            raise ValueError("Report month must be YYYY-MM")
        day = date.fromisoformat(value + "-01")
        return day.year * 12 + day.month
    if len(value) != 10:
        raise ValueError("Business date must be YYYY-MM-DD")
    return date.fromisoformat(value).toordinal()


def expected_period(rule: Rule, now: datetime, trading_days: Iterable[date] = ()) -> str | None:
    """A check window, never proof that the provider actually published."""
    local = aware(now).astimezone(SHANGHAI)
    hour, minute = (int(x) for x in rule.deadline.split(":"))
    ready = local.time().replace(tzinfo=None) >= time(hour, minute)
    day = local.date() if ready else local.date() - timedelta(days=1)
    if rule.cadence == "static":
        return None
    if rule.cadence == "china_daily":
        # Never infer Chinese exchange holidays from Monday-Friday alone.
        days = sorted(trading_days)
        if not days or day > days[-1]:
            return None
        return max((d.isoformat() for d in days if d <= day), default=None)
    if rule.cadence == "weekly":
        publication = day - timedelta(days=(day.weekday() - rule.publish_weekday) % 7)
        return (publication - timedelta(days=rule.data_lag_days)).isoformat()
    if rule.cadence == "monthly":
        month = day.replace(day=1)
        if day.day < 20:
            month = (month - timedelta(days=1)).replace(day=1)
        return month.strftime("%Y-%m")
    if rule.cadence == "quarterly":
        end_month = ((day.month - 1) // 3) * 3
        year = day.year
        if not end_month:
            year, end_month = year - 1, 12
        if day.month % 3 == 1 and day.day < 20:
            end_month -= 3
            if not end_month:
                year, end_month = year - 1, 12
        return f"{year:04d}-{end_month:02d}"
    if rule.cadence == "weekday_daily":
        day -= timedelta(days=rule.business_lag_days)
        while day.weekday() > 4:
            day -= timedelta(days=1)
    if rule.cadence == "weather":
        day -= timedelta(days=1)
    return day.isoformat()


def evaluate(rule: Rule, observation: Observation, now: datetime, trading_days: Iterable[date] = ()) -> dict:
    now = aware(now)
    row = dict(identity=rule.identity, label=rule.label, pages=list(rule.pages), source=rule.source,
               actual_date=observation.actual, source_date=observation.source_actual,
               source_checked_at=observation.source_checked_at, period_kind=rule.period_kind,
               expected_check_date=None, job=rule.job, manual=rule.manual,
               extra=dict(observation.extra), retry_eligible=False)
    if rule.excluded:
        # Excluded observations are never read by the collector, even for display.
        row.update(status="EXCLUDED", severity="info", reason="按用户要求暂不监测或重试", actual_date=None, source_date=None)
        return row
    if rule.cadence == "static":
        row.update(status="STATIC", severity="info", reason="静态导航或参考配置，无日更要求")
        return row
    try:
        actual = None if observation.actual is None else ordinal(observation.actual, rule.period_kind)
        source = None if observation.source_actual is None else ordinal(observation.source_actual, rule.period_kind)
        source_recent = False
        if observation.source_checked_at:
            checked = aware(observation.source_checked_at)
            if checked > now + timedelta(minutes=5):
                raise ValueError("Source check timestamp is in the future")
            source_recent = now - checked <= timedelta(hours=12)
        if actual is not None and rule.period_kind == "date" and actual > now.astimezone(SHANGHAI).date().toordinal():
            raise ValueError("Observed business date is in the future")
        if observation.error:
            row.update(status="SOURCE_UNVERIFIED" if observation.retry_safe else "ERROR",
                       reason=observation.error)
        elif rule.cadence == 'on_demand' and (source is None or actual is not None and source <= actual):
            row.update(status='REFERENCE',reason='原设计为按需交付或保存；展示实际截止日，不虚构每日更新要求')
        elif actual is None and rule.active_months and now.astimezone(SHANGHAI).month not in rule.active_months:
            row.update(status="SEASON_COMPLETE", reason="当前处于原有季节窗口之外；未发布的数据继续留空")
        elif actual is None:
            row.update(status="WAITING_MANUAL" if rule.manual else "MISSING",
                       reason="缺少可核验的实际数据日期")
        elif source is not None and source > actual:
            row.update(status="MISSED", reason="已核实来源日期领先于页面实际数据")
        elif observation.season_complete or (rule.active_months and now.astimezone(SHANGHAI).month not in rule.active_months):
            row.update(status="SEASON_COMPLETE", reason="本指标处于原有季节窗口之外或已完成；保留实际截止日")
        else:
            expected = expected_period(rule, now, trading_days)
            row["expected_check_date"] = expected
            if expected is None:
                row.update(status="SOURCE_UNVERIFIED", reason="缺少已核验日历，不能断言数据已更新")
            elif actual >= ordinal(expected, rule.period_kind):
                row.update(status="CURRENT", reason="实际数据已覆盖本轮检查期")
            elif source is not None and source <= actual and source_recent:
                row.update(status="WAITING_FOR_SOURCE", reason="来源已核实但尚未提供预期检查期；保留旧值")
            elif rule.manual:
                row.update(status="WAITING_MANUAL", reason="已到资料核对期，等待原人工资料或交付")
            else:
                row.update(status="CHECK_DUE", reason="超过常规检查窗口，需重试核实来源；尚未确认漏更")
    except (ValueError, TypeError, OverflowError):
        row.update(status="ERROR", reason="日期或时间身份校验失败")
    row["retry_eligible"] = bool(rule.job and not rule.manual and observation.retry_safe and row["status"] in RETRY_STATES)
    row["severity"] = "error" if row["status"] in {"ERROR", "MISSING", "MISSED"} else "info" if row["status"] in QUIET else "warning"
    return row


def reconcile(rows: list[dict], previous: dict, now: datetime) -> dict:
    """Stable incidents and recovery receipts; old events stay in the host journal."""
    stamp = aware(now).isoformat()
    prior = previous.get("incidents", {})
    active, events = {}, []
    for row in rows:
        if row["status"] in QUIET:
            continue
        key = row["identity"]
        content = [key, row["status"], row["actual_date"], row["source_date"], row["expected_check_date"], row["reason"]]
        fingerprint = sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()
        old = prior.get(key, {})
        incident = dict(identity=key, fingerprint=fingerprint, first_seen_at=old.get("first_seen_at", stamp),
                        last_seen_at=stamp, status=row["status"], label=row["label"], reason=row["reason"])
        active[key] = incident
        if old.get("fingerprint") != fingerprint:
            events.append(dict(kind="OPENED" if not old else "CHANGED", at=stamp, **incident))
    for key, old in prior.items():
        if key not in active:
            events.append(dict(kind="RECOVERED", at=stamp, identity=key, label=old["label"], first_seen_at=old["first_seen_at"]))
    return {"incidents": active, "events": events,
            "recent_events": (previous.get("recent_events", []) + events)[-100:],
            "retries": previous.get("retries", {})}


def retry_plan(rows: list[dict], state: dict, now: datetime, *, busy: bool = False,
               cooldown_minutes: int = 60, max_daily_attempts: int = 3) -> list[str]:
    """One existing producer per pass; no parallel writers or unlimited retries."""
    if busy:
        return []
    now = aware(now)
    day = now.astimezone(SHANGHAI).date().isoformat()
    candidates = {}
    for row in rows:
        if not row.get("retry_eligible"):
            continue
        job = row["job"]
        old = state.get("retries", {}).get(job, {})
        if old.get("day") == day and old.get("attempts", 0) >= max_daily_attempts:
            continue
        last = old.get("reserved_at")
        if last and now - aware(last) < timedelta(minutes=cooldown_minutes):
            continue
        candidates[job] = last or ""
    return sorted(candidates, key=lambda job: (candidates[job], job))[:1]


def reserve_retry(state: dict, job: str, now: datetime) -> dict:
    """Persist before requesting systemd so a controller crash cannot double start."""
    stamp = aware(now)
    day = stamp.astimezone(SHANGHAI).date().isoformat()
    retries = dict(state.get("retries", {}))
    old = retries.get(job, {})
    attempts = old.get("attempts", 0) if old.get("day") == day else 0
    retries[job] = dict(day=day, attempts=attempts + 1, reserved_at=stamp.isoformat(), outcome="RESERVED")
    return {**state, "retries": retries}
