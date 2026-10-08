"""Read-only oilseed positioning pages with explicit missing-source states."""
from datetime import date, datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from agri_research_agent.oilseed_positions.model import config, metrics, preview_root
from agri_research_agent.sugar_positions.charts import chinese_date, movements, trend
from agri_research_agent.sugar_positions.model import domestic_metrics, load_members, positioning_signal
from agri_research_agent.sugar_positions.storage import read_snapshot
from agri_research_agent.positions.workspace import validate_domain
from sugar_positions_page import chinese_time, detail, display_attempt

REPORTS = {"纯期货": "futures_only", "期货＋期权": "combined"}


def fmt(value, signed=False):
    if value is None:
        return "未披露"
    number = float(value)
    return format(int(number), "+," if signed else ",") if number.is_integer() else format(number, "+,.2f" if signed else ",.2f")


def series_figure(data, title, group):
    figure = trend(data, title, {group: "基金"})
    if data[0].get("unit") == "delta_equivalent_contracts":
        figure.update_yaxes(title="净持仓（Delta等价手）")
        figure.update_traces(hovertemplate="%{customdata[4]}<br>净持仓 %{y:,.2f} Delta等价手"
            "<br>多仓 %{customdata[0]:,.2f}<br>空仓 %{customdata[1]:,.2f}"
            "<br>净变化 %{customdata[3]:+,.2f}<br>比较日期 %{customdata[2]}<extra>%{fullData.name}</extra>")
    return figure


def render_oilseed_positions_page(project_root, domain, *, data_root=None, preview_mode=True):
    spec = config(project_root)[domain]
    st.title(spec["title"])
    root = Path(data_root) if data_root is not None else preview_root(project_root, domain)
    try:
        snapshot = validate_domain(read_snapshot(root), project_root, domain)
        attempt_file = root / "last_attempt.json"
        attempt = json.loads(attempt_file.read_text(encoding="utf-8")) if attempt_file.is_file() else {"attempts": []}
    except (ValueError, OSError, KeyError) as exc:
        st.error(f"数据校验未通过：{exc}")
        return
    pending = [a for a in attempt["attempts"] if a["status"] in ("failed", "pending")]
    if pending:
        st.warning("部分来源尚未接通，相关品种保留已验证数据或显示缺口。")
    if not snapshot["foreign"] and not snapshot["domestic"]:
        st.info("尚无已验证持仓数据。")
        st.caption("数据接通后才展示资金情绪。总持仓量不能代替基金净持仓。")
        return
    foreign = metrics(snapshot["foreign"])
    members = load_members(project_root / "02_configs/sugar_positions.json")
    account = "代客" if domain == "rapeseed" else "未区分"
    domestic = domestic_metrics(snapshot["domestic"], members, account=account)
    overview = []
    for market, item in spec["foreign"].items():
        data = [r for r in foreign if r["market"] == market and r["report_type"] == "futures_only"
            and r["group"] == item["fund_group"]]
        overview.append((item["label"] + " · 基金", data[-1] if data else None))
    for variety, label in spec["domestic"].items():
        scope = spec.get("default_scopes",{}).get(variety,variety)
        data = [r for r in domestic if r["scope"] == scope and r["group"] == "top20"]
        overview.append((label + (f" {scope}" if scope != variety else "") + " · 前20名", data[-1] if data else None))
    st.subheader("资金情绪速览")
    for offset in range(0, len(overview), 3):
        for column, (label, row) in zip(st.columns(3), overview[offset:offset+3]):
            with column, st.container(border=True):
                st.markdown(f"**{label}**")
                if row is None:
                    st.caption("暂无已验证排名持仓" if "前20名" in label else "暂无已验证基金持仓")
                    continue
                st.markdown(positioning_signal(row["net"], row["net_change"]))
                st.metric("净持仓（手）", fmt(row["net"]),
                    delta=fmt(row["net_change"], True) if row["net_change"] is not None else None,
                    delta_color="inverse")
                st.caption(f"截至 {chinese_date(row['report_date'])} · 对比 {chinese_date(row['previous_date'])}")
    st.caption("红：向多 · 绿：向空 ｜ 净持仓与变化反映多空倾向。各市场日期不同，手数不合并。")
    outside, inside, sources = st.tabs(["外盘基金", "国内持仓", "数据来源"])
    with outside:
        kind = REPORTS[st.radio("外盘口径", list(REPORTS), horizontal=True)]
        for market, item in spec["foreign"].items():
            st.subheader(item["label"])
            data = [r for r in foreign if r["market"] == market and r["report_type"] == kind and r["group"] == item["fund_group"]]
            if not data:
                st.info("此口径暂无已验证基金持仓。")
                continue
            latest = data[-1]
            unit = "Delta等价手" if latest.get("unit") == "delta_equivalent_contracts" else "手"
            st.markdown(f"**{positioning_signal(latest['net'], latest['net_change'])}**")
            a, b = st.columns(2)
            a.metric(f"基金净持仓（{unit}）", fmt(latest["net"]))
            b.metric("较上一报告变化", fmt(latest["net_change"], True), delta_color="inverse")
            st.caption(f"持仓截至 {chinese_date(latest['report_date'])} · 对比 {chinese_date(latest['previous_date'])}")
            st.caption("Euronext 投资基金分类；首版保留明确标注期货/期权的报告，较早历史口径待核查。"
                if market == "euronext_rapeseed" else "CFTC 管理基金分类；各到期月份汇总，每周公布。")
            if (datetime.now(ZoneInfo("Asia/Shanghai")).date() - date.fromisoformat(latest["report_date"])).days > 14:
                st.warning("这份周报距今超过14天，请核对最新发布状态。")
            if len(data) >= 2:
                st.plotly_chart(series_figure(data, f"{item['label']} · 基金净持仓", item["fund_group"]), width="stretch", key=f"trend_{market}")
                figure = movements(data[-26:], "最近报告净持仓变化")
                known_changes = [r for r in data[-26:] if r["net_change"] is not None]
                if len(known_changes) == 1:
                    figure.update_xaxes(tickmode="array", tickvals=[known_changes[0]["report_date"]],
                        ticktext=[chinese_date(known_changes[0]["report_date"])])
                if unit == "Delta等价手":
                    figure.update_yaxes(title="净变化（Delta等价手）")
                    figure.update_traces(hovertemplate="%{customdata[1]}<br>净变化 %{y:+,.2f} Delta等价手<br>比较日期 %{customdata[0]}<extra></extra>")
                st.plotly_chart(figure, width="stretch", key=f"changes_{market}")
            with st.expander(item["label"] + "持仓明细"):
                view = [dict(r, label="投资基金" if market == "euronext_rapeseed" else "管理基金") for r in data]
                frame = detail(view).iloc[::-1]
                if unit == "Delta等价手":
                    frame = frame.rename(columns={c: c.replace("（手）", "（Delta等价手）") for c in frame.columns})
                st.dataframe(frame, hide_index=True, width="stretch")
    with inside:
        if any(r.get("source_provider") == "sina" for r in snapshot["domestic"]):
            st.caption("国内使用新浪公开备用来源的具体合约排名；未自动选定主力合约，待交易所入口恢复后复核。")
        scopes = sorted({r["scope"] for r in domestic}, key=lambda v: (v not in spec["domestic"], v))
        if not scopes:
            st.info("大商所豆粕、豆油、棕榈油持仓排名尚未接通。")
        else:
            scope = st.selectbox("国内统计范围", scopes,
                format_func=lambda v: f"{spec['domestic'][v]} {v} · 品种总排名" if v in spec["domestic"] else f"{v} · 合约排名")
            data = [r for r in domestic if r["scope"] == scope]
            day = st.selectbox("持仓日期", sorted({r["report_date"] for r in data}, reverse=True), format_func=chinese_date)
            current = {r["group"]: r for r in data if r["report_date"] == day}
            st.markdown(f"**{positioning_signal(current['top20']['net'], current['top20']['net_change'])}**")
            a, b, c = st.columns(3)
            a.metric("前20名净持仓", fmt(current["top20"]["net"]))
            b.metric("较上一保存交易日变化", fmt(current["top20"]["net_change"], True))
            c.metric("五家合计净持仓", fmt(current["fixed5"]["net"]))
            st.caption(f"截至 {chinese_date(day)} · 对比 {chinese_date(current['top20']['previous_date'])} · {current['fixed5']['coverage']}")
            st.caption("公开排名的多空名单可不同，净变化包含名单变化。" + ("当前来源未区分账户类型。" if account == "未区分" else "固定席位展示代客持仓。"))
            st.subheader("五家固定席位")
            if current["fixed5"]["net"] is None:
                st.warning("部分席位缺少一侧披露，无法计算准确合计；未披露不代表零仓位。")
            st.dataframe(detail([current[m["id"]] for m in members])[["对象", "持仓情绪", "净持仓（手）", "净变化（手）", "披露情况"]], hide_index=True, width="stretch")
            history = [r for r in data if r["report_date"] <= day]
            if len({r["report_date"] for r in history}) >= 2:
                st.plotly_chart(trend(history, f"五家固定席位 · {account}净持仓", {m["id"]: m["label"] for m in members}, direct_labels=True), width="stretch", key="fixed_trend")
                st.plotly_chart(movements([r for r in history if r["group"] == "top20"], "前20名净持仓变化"), width="stretch", key="domestic_changes")
            with st.expander("历史净持仓与比较日期"):
                st.dataframe(detail(history).iloc[::-1], hide_index=True, width="stretch")
    with sources:
        st.write("净持仓＝多仓－空仓。净变化反映倾向变化，不等于资金流入流出。国内会员持仓也包含产业套保。")
        st.write("CFTC管理基金与Euronext投资基金的分类不同，各市场单独观察。Euronext期货＋期权保留两位小数；未从分类合计推算交易所总持仓。")
        st.write("五家固定席位：高盛、摩根大通、永安、国泰君安、东证。两侧均披露才计算净仓；五家完整才计算合计。")
        st.write("仅比较相邻已保存的同口径报告；缺仓或间隔超过10天时不计算净变化。合约排名不冒充品种总排名。")
        if domain != "rapeseed":
            st.markdown("国内备用来源：[新浪成交持仓](https://vip.stock.finance.sina.com.cn/q/view/vFutures_Positions_cjcc.php)。当前固定展示2701合约，不代表所有合约汇总，也不自动拼接主力。")
        st.markdown("[CFTC](https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm) · [Euronext](https://live.euronext.com/en/products/commodities/commitments_of_traders) · [郑商所](https://www.czce.com.cn/cn/jysj/ccpm/H077003004index_1.htm)")
        with st.expander("原始来源与采集时间"):
            for source_id, metadata in snapshot["sources"].items():
                if source_id.startswith(("czce_", "euronext_", "sina_")):
                    provider = "郑商所" if source_id.startswith("czce_") else "新浪 · " + source_id.split("_")[1] if source_id.startswith("sina_") else "Euronext"
                    label = provider + " · " + chinese_date(datetime.strptime(source_id.rsplit("_",1)[1], "%Y%m%d").date())
                else:
                    label = source_id
                st.markdown(f"**{label}** · {chinese_time(metadata['retrieved_at'])} · [原始来源]({metadata['url']})")
        with st.expander("最近一次采集结果"):
            st.json(display_attempt(attempt))
        st.caption("本地预览，页面只读；公开上线前需确认数据展示授权。" if preview_mode
                   else "页面展示已保存报告；刷新页面不会触发数据采集。")
