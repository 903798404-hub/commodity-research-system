"""Read-only positioning dashboard; all collection happens through a separate CLI."""
from datetime import date, datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from agri_research_agent.sugar_positions.charts import (
    chinese_date, label_latest_position, movements, trend, seasonality,
)
from agri_research_agent.sugar_positions.model import (
    GROUPS, MARKETS, COFCO_MEMBER, COFCO_HISTORY_RECORD, COFCO_PARTIAL_RECORDS,
    cofco_history_metrics, cofco_official_metrics,
    domestic_metrics, foreign_metrics, load_members, member_metrics, positioning_signal,
)
from agri_research_agent.sugar_positions.storage import read_snapshot
from agri_research_agent.positions.workspace import data_root as resolve_positions_root, validate_domain

REPORT_LABELS = {"纯期货": "futures_only", "期货＋期权": "combined"}
RANKED_NET_NOTICE = ("榜内净仓＝已披露多仓－已披露空仓；未披露一侧按0参与计算，"
    "原始多仓、空仓仍保留未披露。五家合计为榜内净仓之和；两侧均未披露时榜内净仓为0，"
    "不代表实际零仓位或多空平衡。进出排名也会影响榜内净变化。")


from computation_cache import cache_computation

# Reuse only already-validated, pure value computations; readers/writers stay unchanged.
foreign_metrics = cache_computation(foreign_metrics)
domestic_metrics = cache_computation(domestic_metrics)
trend = cache_computation(trend)
movements = cache_computation(movements)
seasonality = cache_computation(seasonality)
member_metrics = cache_computation(member_metrics)
cofco_history_metrics = cache_computation(cofco_history_metrics)
cofco_official_metrics = cache_computation(cofco_official_metrics)

SEASONAL_FIELDS = {"净持仓": "net", "多仓": "long", "空仓": "short"}


def seasonal_selection(rows, *, key):
    years = sorted({int(r["report_date"][:4]) for r in rows})
    selected = st.multiselect("对比年份", years, default=years[-5:], key=key,
                             format_func=lambda y: f"{y}年")
    st.caption("按公历月日叠加；保留实际报告日期，缺失不补零、不延长；超过10天的报告间隔断线。")
    if len(years) < 2:
        st.caption("已保存数据仅覆盖一个年份，暂无跨年对比。")
    if not selected:
        st.info("请选择至少一个对比年份。")
    return [r for r in rows if int(r["report_date"][:4]) in selected]


def labeled_seasonality(rows, selected, title, *, field="net"):
    figure = seasonality(selected, title, field=field,
        current_year=datetime.now(ZoneInfo("Asia/Shanghai")).year)
    if rows[-1] in selected:
        label_latest_position(figure, rows, field=field, seasonal=True)
    return figure


def chart_pair(first, second, *, keys, preserve_legend=False):
    """Keep related charts visible together, stacking on narrow screens."""
    key = f"position-charts-{keys[0]}"
    st.html(f"""<style>@media(max-width:1000px) {{
      .st-key-{key} [data-testid="stHorizontalBlock"] {{flex-direction:column;}}
      .st-key-{key} [data-testid="stColumn"] {{width:100% !important; flex:1 1 100% !important;}}
    }}</style>""")
    with st.container(key=key):
        height = 400 if first.layout.margin.b >= 115 else 340
        if len(first.data) > 1 and not preserve_legend:
            first.update_layout(showlegend=False, margin=dict(l=40, r=130, t=70, b=first.layout.margin.b))
        for column, figure, chart_key in zip(st.columns(2), (first, second), keys):
            with column:
                figure.update_layout(height=height)
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


@cache_computation
def detail(rows):
    return pd.DataFrame([{
        "日期": chinese_date(r["report_date"]), "对象": r.get("label", GROUPS.get(r["group"], r["group"])),
        "多仓（手）": fmt(r["long"]), "空仓（手）": fmt(r["short"]), "净持仓（手）": fmt(r["net"], True),
        "净变化（手）": fmt(r["net_change"], True), "比较日期": chinese_date(r["previous_date"]),
        "持仓情绪": positioning_signal(r["net"], r["net_change"]),
        "披露情况": r.get("coverage", "分类持仓已披露"),
    } for r in rows]).convert_dtypes()


@cache_computation
def domestic_detail(rows):
    table = detail(rows).rename(columns={"净持仓（手）": "榜内净持仓（手）",
        "净变化（手）": "榜内净变化（手）", "持仓情绪": "榜内持仓倾向"})
    table.loc[[r["long"] is None and r["short"] is None and r["net"] is not None for r in rows], "榜内持仓倾向"] = "两侧未披露 · 榜内按0"
    table.loc[[r["long"] is None and r["short"] is None and r["net"] is None for r in rows], "榜内持仓倾向"] = "两侧未披露 · 留空"
    return table


def render_member_seasonality(rows, *, name, title, key):
    latest = rows[-1]
    for column, field, label in zip(st.columns(3), ("long", "short", "net"), ("多仓", "空仓", "榜内净持仓")):
        value = latest[field] if latest["long"] is not None or latest["short"] is not None else None
        column.metric(f"{name}{label}（手）", fmt(value))
    st.caption("图中变化以同口径上一份已保存报告为基准。")
    selected = seasonal_selection(rows, key=f"{key}_years")
    if selected:
        figures = [labeled_seasonality(rows, selected, f"{title}{label}季节性", field=field)
            for label, field in (("净仓", "net"), ("多仓", "long"), ("空仓", "short"))]
        chart_pair(*figures[:2], keys=(f"{key}_net_seasonal", f"{key}_long_seasonal"), preserve_legend=True)
        st.plotly_chart(figures[2], width="stretch", key=f"{key}_short_seasonal")
    with st.expander(f"{name}期货持仓明细"):
        st.dataframe(domestic_detail(rows).iloc[::-1], hide_index=True, width="stretch")


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
        view = st.radio("图表模式", ["季节性", "连续历史"], horizontal=True, key="sugar_foreign_view")
        field_label = st.radio("季节性指标", list(SEASONAL_FIELDS), horizontal=True, key="sugar_foreign_field")
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
            if view == "季节性":
                selected = seasonal_selection(data, key=f"foreign_years_{market}")
                if selected:
                    figure = labeled_seasonality(data, selected, f"{label} · {group_label}{field_label}季节性",
                        field=SEASONAL_FIELDS[field_label])
                    chart_pair(figure,
                        movements(data[-26:], "近26份报告净持仓变化"),
                        keys=(f"foreign_seasonal_{market}", f"foreign_changes_{market}"), preserve_legend=True)
            elif len(data) >= 8:
                figure = trend(data, f"{label} · {group_label}净持仓", {group: group_label})
                label_latest_position(figure, data)
                chart_pair(figure,
                    movements(data[-26:], "近26份报告净持仓变化"),
                    keys=(f"foreign_trend_{market}", f"foreign_changes_{market}"))
            else:
                st.caption("已保存报告不足8期，先展示数值明细。")
            with st.expander(f"{label}持仓明细"):
                st.dataframe(detail(data).iloc[::-1], hide_index=True, width="stretch")
    with domestic_tab:
        ranking_rows = [r for r in snapshot["domestic"] if r.get("record_type") not in COFCO_PARTIAL_RECORDS]
        scopes = sorted({r["scope"] for r in ranking_rows}, key=lambda s: (s != "SR", s))
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
            st.subheader("中粮期货 · 独立席位季节性")
            contract_history = cofco_history_metrics(snapshot["domestic"])
            basis = st.radio("中粮统计口径",
                ["白糖品种汇总", "当前范围代客排名", "合约榜单可见持仓（历史）"], key="cofco_basis", horizontal=True)
            history_mode = basis == "合约榜单可见持仓（历史）"
            if basis == "白糖品种汇总":
                cofco = [r for r in cofco_official_metrics(snapshot["domestic"]) if r["report_date"] <= day]
                st.caption("直接采用郑商所SR品种总排名中的中粮席位，与文华白糖汇总核对；不再加总各合约可见榜单。"
                    "独立于上方单合约选择，截止日期沿用上方选择。")
                st.caption("早年会员名未区分账户时保留“未区分”，明确标注代客后采用代客排名；不伪造早年账户分类。"
                    "原始账户变化处分段、不计算跨口径净变化。缺少一侧仅在榜内净仓中按0计算，两侧未披露留空。")
                cofco_title = "中粮期货 · 白糖"
            elif history_mode:
                cofco = [r for r in contract_history if r["report_date"] <= day]
                st.warning("此历史接口存在漏项，当前为合约榜单可见持仓，不是完整代客汇总。"
                    "与文华白糖汇总核对时，请选择“白糖品种汇总”。")
                st.caption("白糖各合约榜单内中粮可见持仓求和；独立于上方统计范围，截止日期沿用上方选择。"
                    "原始账户标签保留，此处合计接口所列账户；早年未区分代客、自营，不能当作代客总持仓。")
                st.caption("来源：用户指定的Tushare兼容服务。仅覆盖当日返回的合约与榜内席位，未入榜不是实际零仓；"
                    "接口明确返回的0原样保留。可见净仓＝可见多仓－可见空仓；两侧均缺失留空。合约清单变化时不计算净变化。")
                cofco_title = "中粮期货 · 白糖合约可见"
            else:
                cofco = [r for r in member_metrics(snapshot["domestic"], COFCO_MEMBER, account=account)
                         if r["scope"] == scope and r["report_date"] <= day]
                st.caption("中粮期货代客席位公开排名；不是中粮集团完整持仓。榜内净仓按已披露多仓减空仓，未披露一侧按0计算；两侧未披露时图表留空。")
                cofco_title = f"中粮期货 · 白糖 {scope} 代客"
            if any(r["long"] is not None or r["short"] is not None for r in cofco):
                render_member_seasonality(cofco, name="中粮", title=cofco_title, key="cofco")
                if history_mode:
                    with st.expander("中粮各合约及原始账户明细"):
                        raw_history = [r for r in snapshot["domestic"] if r.get("record_type") == COFCO_HISTORY_RECORD and r["report_date"] == cofco[-1]["report_date"]]
                        st.dataframe(pd.DataFrame(raw_history)[["report_date", "scope", "raw_member", "account", "side", "positions", "disclosure"]],
                            hide_index=True, width="stretch")
            else:
                st.info("当前统计范围及日期内，已保存排名尚无中粮期货代客席位披露，季节性暂留空。")
            st.subheader("高盛期货 · 独立席位季节性")
            goldman = [r for r in history if r["group"] == "goldman"]
            st.caption("采用上方所选白糖品种或合约的公开代客排名，沿用截止日期。"
                "多空缺项保留未披露，两侧均未披露时净仓图留空。")
            if any(r["long"] is not None or r["short"] is not None for r in goldman):
                title = "高盛期货 · 白糖" if scope == "SR" else f"高盛期货 · 白糖 {scope} "
                render_member_seasonality(goldman, name="高盛", title=title, key="goldman")
            else:
                st.info("当前统计范围及日期内，已保存排名尚无高盛期货代客席位披露，季节性暂留空。")
            with st.expander("历史榜内净持仓与比较日期"):
                st.dataframe(domestic_detail(history).iloc[::-1], hide_index=True, width="stretch")
            with st.expander("交易所原始排名"):
                raw = [r for r in ranking_rows if r["scope"] == scope and r["report_date"] == day]
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
            if source_id.startswith("czce_cofco_official_"):
                source_day = datetime.strptime(source_id.removeprefix("czce_cofco_official_"), "%Y%m%d").date()
                source_label = f"郑商所SR品种中粮汇总 · {chinese_date(source_day)}"
            else:
                source_label = (f"郑商所 · {chinese_date(datetime.strptime(source_id[5:], '%Y%m%d').date())}"
                    if source_id.startswith("czce_") else source_id)
            st.markdown(f"**{source_label}** · 采集时间 {chinese_time(metadata['retrieved_at'])} · [原始来源]({metadata['url']})")
        st.caption("当前为本地预览。公开展示前需确认相关数据展示授权。" if preview_mode
                   else "页面展示已保存报告；刷新页面不会触发数据采集。")
        with st.expander("最近一次采集结果"):
            st.json(display_attempt(json.loads(attempt_file.read_text(encoding="utf-8"))
                if attempt_file.is_file() else snapshot["attempts"]))
