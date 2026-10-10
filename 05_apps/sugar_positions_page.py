"""Read-only positioning dashboard; all collection happens through a separate CLI."""
from datetime import date, datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from agri_research_agent.sugar_positions.charts import chinese_date, movements, trend
from agri_research_agent.sugar_positions.model import (
    GROUPS, MARKETS, domestic_metrics, foreign_metrics, load_members, positioning_signal,
)
from agri_research_agent.sugar_positions.storage import read_snapshot
from agri_research_agent.positions.workspace import data_root as resolve_positions_root, validate_domain

REPORT_LABELS = {"纯期货": "futures_only", "期货＋期权": "combined"}
RANKED_NET_NOTICE = ("榜内净仓＝已披露多仓－已披露空仓；未披露一侧按0参与计算，"
    "原始多仓、空仓仍保留未披露。五家合计为榜内净仓之和；两侧均未披露时榜内净仓为0，"
    "不代表实际零仓位或多空平衡。进出排名也会影响榜内净变化。")


def chart_pair(first, second, *, keys):
    """Keep related charts visible together, stacking on narrow screens."""
    key = f"position-charts-{keys[0]}"
    st.html(f"""<style>@media(max-width:1000px) {{
      .st-key-{key} [data-testid="stHorizontalBlock"] {{flex-direction:column;}}
      .st-key-{key} [data-testid="stColumn"] {{width:100% !important; flex:1 1 100% !important;}}
    }}</style>""")
    with st.container(key=key):
        if len(first.data) > 1:
            first.update_layout(showlegend=False, margin=dict(l=40, r=130, t=70, b=40))
        for column, figure, chart_key in zip(st.columns(2), (first, second), keys):
            with column:
                figure.update_layout(height=340)
                figure.update_xaxes(nticks=4, tickangle=0)
                st.plotly_chart(figure, width="stretch", key=chart_key)


def fmt(value, signed=False):
    return "未披露" if value is None else format(value, "+," if signed else ",")


def chinese_time(value):
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    moment = moment.astimezone(ZoneInfo("Asia/Shanghai"))
    return f"{chinese_date(moment.date())} {moment.hour:02}时{moment.minute:02}分{moment.second:02}秒（北京时间）"


def display_attempt(value):
    if isinstance(value, list):
        return [display_attempt(item) for item in value]
    if isinstance(value, dict):
        return {key: chinese_time(item) if key in ("attempted_at", "retrieved_at") and item
            else display_attempt(item) for key, item in value.items()}
    return value


def detail(rows):
    return pd.DataFrame([{
        "日期": chinese_date(r["report_date"]), "对象": r.get("label", GROUPS.get(r["group"], r["group"])),
        "多仓（手）": fmt(r["long"]), "空仓（手）": fmt(r["short"]), "净持仓（手）": fmt(r["net"], True),
        "净变化（手）": fmt(r["net_change"], True), "比较日期": chinese_date(r["previous_date"]),
        "持仓情绪": positioning_signal(r["net"], r["net_change"]),
        "披露情况": r.get("coverage", "分类持仓已披露"),
    } for r in rows]).convert_dtypes()


def domestic_detail(rows):
    table = detail(rows).rename(columns={"净持仓（手）": "榜内净持仓（手）",
        "净变化（手）": "榜内净变化（手）", "持仓情绪": "榜内持仓倾向"})
    table.loc[[r["long"] is None and r["short"] is None for r in rows], "榜内持仓倾向"] = "两侧未披露 · 榜内按0"
    return table


def render_sugar_positions_page(project_root: Path, *, data_root=None, preview_mode=True):
    st.title("白糖资金情绪")
    members = load_members(project_root / "02_configs" / "sugar_positions.json")
    root = Path(data_root) if data_root is not None else resolve_positions_root(project_root, "sugar")
    try:
        snapshot = validate_domain(read_snapshot(root), project_root, "sugar")
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        st.error(f"数据校验未通过：{exc}")
        return
    attempt_file = root / "last_attempt.json"
    if attempt_file.is_file():
        attempt = json.loads(attempt_file.read_text(encoding="utf-8"))
        failures = [a for a in attempt["attempts"] if a["status"] == "failed"]
        if failures:
            st.warning(f"最近一次采集有 {len(failures)} 个来源请求失败，相关来源保留上一份已验证数据。")
    if not snapshot["foreign"] and not snapshot["domestic"]:
        st.info("尚未采集持仓数据。运行独立白糖采集脚本后，此页面会读取已验证快照。")
        return
    all_foreign = foreign_metrics(snapshot["foreign"])
    overview = []
    for market, label in MARKETS.items():
        rows = [r for r in all_foreign if r["market"] == market
            and r["report_type"] == "futures_only" and r["group"] == "managed_money"]
        overview.append((label + " · 基金", rows[-1] if rows else None))
    domestic_default = [r for r in domestic_metrics(snapshot["domestic"], members)
        if r["scope"] == "SR" and r["group"] == "top20"]
    overview.append(("郑糖 · 前20名", domestic_default[-1] if domestic_default else None))
    st.subheader("资金情绪速览")
    for column, (label, row) in zip(st.columns(3), overview):
        with column, st.container(border=True):
            st.markdown(f"**{label}**")
            if row is None:
                st.caption("暂无已验证数据")
                continue
            st.markdown(positioning_signal(row["net"], row["net_change"]))
            st.metric("净持仓（手）", fmt(row["net"]),
                delta=fmt(row["net_change"], True) if row["net_change"] is not None else None,
                delta_color="inverse")
            st.caption(f"截至 {chinese_date(row['report_date'])} · 对比 {chinese_date(row['previous_date'])}")
    st.caption("红：向多 · 绿：向空 ｜ 外盘基金纯期货 · 国内SR前20名")
    foreign_tab, domestic_tab, source_tab = st.tabs(["外盘基金", "国内持仓", "数据来源"])
    with foreign_tab:
        kind = REPORT_LABELS[st.radio("外盘口径", list(REPORT_LABELS), horizontal=True)]
        group_label = st.selectbox("交易者分类", list(GROUPS.values()))
        group = next(key for key, label in GROUPS.items() if label == group_label)
        for market, label in MARKETS.items():
            data = [r for r in all_foreign if r["market"] == market and r["report_type"] == kind and r["group"] == group]
            st.subheader(label)
            if not data:
                st.info("此口径暂无已验证数据。")
                continue
            latest = data[-1]
            st.markdown(f"**持仓情绪：{positioning_signal(latest['net'], latest['net_change'])}**")
            a, b, c = st.columns(3)
            a.metric(f"{group_label}净持仓", fmt(latest["net"]))
            b.metric("较上一报告变化", fmt(latest["net_change"], True))
            c.metric("总持仓量", fmt(latest["open_interest"]))
            if latest["open_interest"] and latest["net"] is not None:
                st.caption(f"净持仓占总持仓量 {latest['net'] / latest['open_interest']:+.1%} · 用比例辅助观察倾向强弱。")
            st.caption(f"持仓截至 {chinese_date(latest['report_date'])} · 比较日期 {chinese_date(latest['previous_date'])} · "
                "各到期月份汇总；每周公布，持仓日期与公布日期不同。")
            if (datetime.now(ZoneInfo("Asia/Shanghai")).date() - date.fromisoformat(latest["report_date"])).days > 14:
                st.warning("这份周报距今超过14天，请核对最新发布状态。")
            if len(data) >= 8:
                chart_pair(trend(data, f"{label} · {group_label}净持仓", {group: group_label}),
                    movements(data[-26:], "近26份报告净持仓变化"),
                    keys=(f"foreign_trend_{market}", f"foreign_changes_{market}"))
            else:
                st.caption("已保存报告不足8期，先展示数值明细。")
            with st.expander(f"{label}持仓明细"):
                st.dataframe(detail(data).iloc[::-1], hide_index=True, width="stretch")
    with domestic_tab:
        scopes = sorted({r["scope"] for r in snapshot["domestic"]}, key=lambda s: (s != "SR", s))
        if not scopes:
            st.info("国内持仓尚未采集。")
        else:
            scope = st.selectbox("国内统计范围", scopes, format_func=lambda s: "SR 品种总排名" if s == "SR" else s)
            account = "代客"
            data = [r for r in domestic_metrics(snapshot["domestic"], members, account=account) if r["scope"] == scope]
            day = st.selectbox("持仓日期", sorted({r["report_date"] for r in data}, reverse=True),
                format_func=chinese_date)
            current = {r["group"]: r for r in data if r["report_date"] == day}
            st.markdown(f"**排名持仓情绪：{positioning_signal(current['top20']['net'], current['top20']['net_change'])}**")
            st.caption("国内反映公开排名与会员代客持仓倾向，不能直接识别基金资金；产业套保也会影响净持仓。")
            a, b, c = st.columns(3)
            a.metric("前20名榜内净持仓", fmt(current["top20"]["net"]))
            b.metric("较上一保存交易日变化", fmt(current["top20"]["net_change"], True))
            c.metric("五家合计榜内净持仓", fmt(current["fixed5"]["net"]))
            st.caption(f"持仓截至 {chinese_date(day)} · 比较日期 {chinese_date(current['top20']['previous_date'])} · "
                f"{current['fixed5']['coverage']}。")
            st.caption("前20名多头和空头名单可不同；净变化按两份排名汇总之差计算，包含名单变化。")
            st.subheader("五家固定席位")
            fixed = [current[m["id"]] for m in members]
            st.caption(RANKED_NET_NOTICE)
            st.dataframe(domestic_detail(fixed)[["对象", "榜内持仓倾向", "榜内净持仓（手）", "榜内净变化（手）",
                "多仓（手）", "空仓（手）", "披露情况"]], hide_index=True, width="stretch")
            history = [r for r in data if r["report_date"] <= day]
            if len({r["report_date"] for r in history}) >= 8:
                chart_pair(trend(history, f"五家固定席位 · {account}榜内净持仓", {
                    m["id"]: m["label"] for m in members}, direct_labels=True),
                    movements([r for r in history if r["group"] == "top20"], "前20名榜内净持仓变化"),
                    keys=("domestic_fixed_trend", "domestic_changes"))
                st.caption("零线上方榜内偏多，下方榜内偏空；曲线末端显示席位名称与最新榜内净持仓。")
            with st.expander("历史榜内净持仓与比较日期"):
                st.dataframe(domestic_detail(history).iloc[::-1], hide_index=True, width="stretch")
            with st.expander("交易所原始排名"):
                raw = [r for r in snapshot["domestic"] if r["scope"] == scope and r["report_date"] == day]
                st.dataframe(pd.DataFrame(raw)[["side", "rank", "raw_member", "positions", "reported_change"]],
                    hide_index=True, width="stretch")
    with source_tab:
        st.subheader("统计口径与来源")
        st.write("净持仓＝多仓－空仓。外盘为分类交易者、各到期月份汇总；国内为排名披露范围。")
        st.write("净持仓体现多空倾向，净变化体现倾向增强或减弱，不等于资金流入流出。各市场合约规格和截至日期不同，手数不直接比较资金规模。")
        st.write("五家固定席位默认展示代客持仓；原始数据保留账户类型。")
        st.write("五家固定席位：高盛、摩根大通、永安、国泰君安、东证。" + RANKED_NET_NOTICE)
        st.write("比较日期来自上一条已保存的同口径数据；间隔超过10天不计算变化。国内比较榜内净仓，外盘任一侧缺失时不计算净仓及变化。")
        st.markdown("[CFTC COT](https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm) · "
            "[ICE COT](https://www.ice.com/report/122) · "
            "[郑商所持仓排名](https://www.czce.com.cn/cn/jysj/ccpm/H077003004index_1.htm)")
        for source_id, metadata in snapshot["sources"].items():
            source_label = (f"郑商所 · {chinese_date(datetime.strptime(source_id[5:], '%Y%m%d').date())}"
                if source_id.startswith("czce_") else source_id)
            st.markdown(f"**{source_label}** · 采集时间 {chinese_time(metadata['retrieved_at'])} · [原始来源]({metadata['url']})")
        st.caption("当前为本地预览。公开展示前需确认相关数据展示授权。" if preview_mode
                   else "页面展示已保存报告；刷新页面不会触发数据采集。")
        with st.expander("最近一次采集结果"):
            st.json(display_attempt(json.loads(attempt_file.read_text(encoding="utf-8"))
                if attempt_file.is_file() else snapshot["attempts"]))
