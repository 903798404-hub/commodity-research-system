"""Read-only website notifications from the independent server monitor."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from agri_research_agent.monitoring.freshness import SCHEMA, SHANGHAI, aware
from agri_research_agent.monitoring.catalog import BY_ID
from agri_research_agent.monitoring.task_ledger import EXECUTION_LABELS, validate_ledger
from navigation import NAVIGATION_GROUPS

STATUS_LABELS = {
    'CURRENT':'已覆盖检查期', 'EXCLUDED':'按要求暂缓', 'STATIC':'静态内容',
    'SEASON_COMPLETE':'季节结束或已完成', 'MISSED':'已确认漏更',
    'CHECK_DUE':'到期待核实', 'WAITING_FOR_SOURCE':'等待原来源发布',
    'SOURCE_UNVERIFIED':'来源未核实', 'WAITING_MANUAL':'等待人工资料',
    'MISSING':'数据缺失', 'ERROR':'读者校验异常',
    'REFERENCE':'按需更新 · 保留截止日',
}
OUTCOMES = {'RESERVED':'已登记，执行结果待核实','COMPLETED':'更新器执行完成，已重新核对日期',
    'FAILED':'更新器执行失败','STILL_RUNNING':'最近一次等待超时；按最新数据日期判断恢复','CHECK_FAILED':'重试后核验失败'}


def retry_status(row, retry, now):
    if not row.get('retry_eligible'):
        return row.get('retry_blocked_reason') or '当前无需额外重试'
    today=now.astimezone(SHANGHAI).date().isoformat()
    if retry.get('day')==today and retry.get('attempts',0)>=3:
        return '今日额外重试已达3次，等待原定时任务或次日检查'
    if retry.get('reserved_at'):
        due=aware(retry['reserved_at'])+timedelta(minutes=60)
        if due>now:
            return '冷却至 '+due.astimezone(SHANGHAI).strftime('%m-%d %H:%M')
    return '等待下一轮；已有更新器运行时顺延'


def load_report(path: Path, now=None):
    now = now or datetime.now(timezone.utc)
    if path.is_symlink() or path.stat().st_size > 16 * 1024**2:
        raise ValueError('监测回执文件身份或大小异常')
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('schema_version') != SCHEMA or not isinstance(value.get('rows'),list) or not value['rows']:
        raise ValueError('监测回执格式或页面覆盖异常')
    checked = aware(value['checked_at'])
    if checked > now + timedelta(minutes=5):
        raise ValueError('监测时间位于未来')
    ids = [r['identity'] for r in value['rows']]
    if len(ids) != len(set(ids)) or set(ids)!=set(BY_ID) or any(r['status'] not in STATUS_LABELS for r in value['rows']):
        raise ValueError('监测节点或状态异常')
    return value, now - checked > timedelta(minutes=60)


def render_task_ledger(report, names, keys):
    st.subheader('统一任务台账')
    st.caption('原更新入口逐项接入同一台账；原定时器、采集、校验和发布流程保持不变。执行完成与实际数据是否更新分别核对。')
    if report.get('task_ledger_error'):
        st.warning(report['task_ledger_error'])
    ledger = report.get('task_ledger')
    if ledger is None:
        st.info('任务台账尚未提供执行观察回执；上方实际数据日期监测继续有效。')
        return
    now = datetime.now(timezone.utc)
    try:
        validate_ledger(ledger, now)
    except (ValueError, KeyError, TypeError, OverflowError):
        st.warning('任务台账回执校验失败；当前不能确认执行状态，上方数据日期单独核对。')
        return
    checked = aware(ledger['checked_at'])
    if now - checked > timedelta(minutes=60):
        st.warning('任务台账超过60分钟未刷新；以下为上次有效执行观察。')
    if report.get('task_ledger_error') and checked < aware(report['checked_at']):
        st.caption('以下为上次有效观察，不代表当前执行状态。')
    st.caption('台账最近观察：'+checked.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')+'（北京时间）')
    st.caption(f"已接入{len(ledger['tasks'])}个原自动更新入口，共登记{len(ledger['datasets'])}项数据依赖；人工资料和暂缓项目单独保留。")
    display = []
    timer_labels = {'active':'已启用', 'inactive':'未启用', 'failed':'异常'}
    for task in ledger['tasks']:
        if keys and not keys.intersection(task['pages']):
            continue
        timer = task['timer_observation']
        evidence = task['evidence']
        pending = task['pending_datasets']
        display.append({'任务':task['label'], '页面':'、'.join(names.get(key,key) for key in task['pages']),
            '执行状态':EXECUTION_LABELS[task['execution_status']],
            '数据核验':f'{pending}项待处理或核实' if pending else '关联数据无待处理项',
            '原定时器':timer_labels.get(timer['ActiveState'],'未核实'),
            '下次原定时触发':timer['NextElapseUSecRealtime'] or '原定时器未提供日历时间',
            '最近启动':evidence['ExecMainStartTimestamp'] or '未观察到',
            '最近退出':evidence['ExecMainExitTimestamp'] or '未观察到',
            '原服务结果':('等待本次执行结束' if task['execution_status'] in {'RUNNING','QUEUED','STOPPING'}
                else '尚无执行证据' if task['execution_status']=='NOT_OBSERVED' else evidence['Result'])})
    if display:
        st.dataframe(pd.DataFrame(display),hide_index=True,width='stretch')
    else:
        st.info('此页面没有已接入的自动更新入口；资料交付或缺少更新器的状态见上方日期监测。')
    st.caption(ledger['history_scope']+'。定时触发时间保留原服务器回执；业务核对窗口与原定时计划分别登记。')
    with st.expander('任务执行观察记录'):
        history=[]
        task_ids={task['identity'] for task in ledger['tasks'] if not keys or keys.intersection(task['pages'])}
        for event in reversed(ledger['recent_observations']):
            if event['task_id'] not in task_ids:
                continue
            history.append({'观察时间':aware(event['at']).astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S'),
                '任务':event['label'],'执行状态':EXECUTION_LABELS[event['execution_status']],
                '原服务结果':event['evidence']['Result'],'执行记录标识':event['run_id'] or '未观察到新执行标识'})
        if history:
            st.dataframe(pd.DataFrame(history),hide_index=True,width='stretch')
        else:
            st.info('暂无执行状态观察记录。')
    with st.expander('数据与任务登记范围'):
        modes={'AUTOMATIC':'已接入原更新器','MANUAL':'原人工或按需交付','NO_UPDATER':'尚无独立更新入口','EXCLUDED':'按要求暂缓','STATIC':'静态内容'}
        registry=[{'数据项':row['label'],'接入方式':modes[row['mode']],
            '原来源':row['source'],'原业务核对周期':row['cadence'],'原业务核对窗口':row['deadline'],
            '页面':'、'.join(names.get(key,key) for key in row['pages'])}
            for row in ledger['datasets'] if not keys or keys.intersection(row['pages'])]
        st.dataframe(pd.DataFrame(registry),hide_index=True,width='stretch')


@st.fragment(run_every=60)
def render_page_monitor(data_root: Path):
    st.subheader('全部页面 · 实际数据日期监测')
    st.caption('数据日期取自网页原输入。多序列列出共同截止日，逐序列日期见明细；检查时间和更新器成功时间不代表数据已更新。人工录价仍按原流程交付。')
    try:
        report, stale = load_report(data_root / 'update_status/page_monitor.json')
    except FileNotFoundError:
        st.error('服务器业务日期监测尚未提供回执，当前不能确认各页面更新状态。')
        return
    except (OSError,ValueError,KeyError,TypeError):
        st.error('服务器业务日期监测回执校验失败，当前不能确认各页面更新状态。')
        return
    stamp = aware(report['checked_at']).astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')
    st.caption('最近核验：' + stamp + '（北京时间）；服务器每轮结束后15分钟再次检查，页面每60秒读取回执。')
    if stale:
        st.error('监测回执超过60分钟未刷新：监测进程可能异常。以下为上次核验结果。')
    if report.get('monitor_error'):
        st.error(report['monitor_error'])
    rows = report['rows']
    error_count = sum(r['severity']=='error' for r in rows)
    warning_count = sum(r['severity']=='warning' for r in rows)
    if error_count:
        st.error(f'{error_count} 项需处理：详见下方数据缺失、读者异常或已确认漏更。')
    elif warning_count:
        st.warning(f'{warning_count} 项等待来源核实、发布或人工资料。')
    else:
        st.success('本轮监测未发现需要处理的数据日期异常。')
    names = {item.key:item.label for group in NAVIGATION_GROUPS for item in group.items}
    page = st.selectbox('查看页面', ['全部页面',*names.values()], key='page-monitor-filter')
    keys = {k for k,v in names.items() if v==page}
    selected = [r for r in rows if page=='全部页面' or keys.intersection(r['pages'])]
    only_alerts = st.checkbox('只显示待处理或待核实项', value=True, key='page-monitor-alert-only')
    if only_alerts:
        selected = [r for r in selected if r['severity'] in {'warning','error'}]
    display = []
    now=datetime.now(timezone.utc)
    for row in selected:
        retry = report.get('retries',{}).get(row.get('job'),{})
        display.append({
            '页面':'、'.join(names.get(k,k) for k in row['pages']), '数据项':row['label'],
            '状态':STATUS_LABELS[row['status']], '实际数据日期':row['actual_date'] or '未核验',
            '已核实来源日期':row['source_date'] or '未核验', '本轮检查期':row['expected_check_date'] or '—',
            '原因':row['reason'], '自动重试':retry_status(row,retry,now),
            '最近重试结果':OUTCOMES.get(retry.get('outcome'), '尚未执行额外重试'),
            '当日额外重试次数':retry.get('attempts',0) if retry.get('day')==now.astimezone(SHANGHAI).date().isoformat() else 0,
        })
    if display:
        st.dataframe(pd.DataFrame(display), hide_index=True, width='stretch')
    else:
        st.info('当前筛选下没有待处理项。')
    st.caption('额外重试：每个已有更新器每天最多3次，间隔至少60分钟，每轮最多1个；其他更新器运行时顺延。暂缓的美国、加拿大、巴西与棕榈油天气不参与监测或重试。')
    render_task_ledger(report,names,keys)
    with st.expander('提醒与恢复记录'):
        events=[]
        for event in reversed(report.get('recent_events',[])):
            events.append({'时间':aware(event['at']).astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S'),
                '数据项':event['label'],'变化':{'OPENED':'发现异常','CHANGED':'状态变化','RECOVERED':'已恢复','SCOPE_REMOVED':'监测范围已校正'}.get(event['kind'],event['kind']),
                '说明':event.get('reason','实际数据重新通过核验')})
        if events:
            st.dataframe(pd.DataFrame(events),hide_index=True,width='stretch')
        else:
            st.info('暂无状态变化记录。同一问题持续存在时不重复新增提醒。')
    with st.expander('逐来源截止日与覆盖范围'):
        st.json([{'数据项':r['label'],'页面':[names.get(k,k) for k in r['pages']], '来源':r['source'],
            '实际数据日期':r['actual_date'],'来源核实时间':r.get('source_checked_at'),'状态':STATUS_LABELS[r['status']],'明细':r.get('extra',{})} for r in rows])
