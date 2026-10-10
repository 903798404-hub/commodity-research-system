"""Read-only oilseed positioning pages with explicit missing-source states."""
from datetime import date, datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from agri_research_agent.oilseed_positions.model import config, metrics
from agri_research_agent.oilseed_positions.aggregation import domestic_metrics, METHOD
from agri_research_agent.sugar_positions.charts import chinese_date, movements, trend
from agri_research_agent.sugar_positions.model import load_members, positioning_signal
from agri_research_agent.sugar_positions.storage import read_snapshot
from agri_research_agent.positions.workspace import data_root as resolve_positions_root, validate_domain
from sugar_positions_page import (
    RANKED_NET_NOTICE, chart_pair, chinese_time, detail, domestic_detail, display_attempt,
)

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
    root = Path(data_root) if data_root is not None else resolve_positions_root(project_root, domain)
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
    foreign = metrics(snapshot["foreign"])
    members = load_members(project_root / "02_configs/sugar_positions.json")
    account = "代客"
    domestic = domestic_metrics(snapshot["domestic"], members, account=account)
    overview = []
    for market, item in spec["foreign"].items():
        data = [r for r in foreign if r["market"] == market and r["report_type"] == "futures_only"
            and r["group"] == item["fund_group"]]
        overview.append((item["label"] + " · 基金", data[-1] if data else None))
    for variety, label in spec["domestic"].items():
        scope = spec.get("default_scopes",{}).get(variety,variety)
        data = [r for r in domestic if r["scope"] == scope and r["group"] == "top20"]
        overview.append((label + (f" {scope} · 主力参考" if scope != variety else " · 品种汇总") + " · 前20名", data[-1] if data else None))
    st.subheader("资金情绪速览")
    for offset in range(0, len(overview), 3):
        for column, (label, row) in zip(st.columns(3), overview[offset:offset+3]):
            with column, st.container(border=True):
                st.markdown(f"**{label}**")
                if row is None:
                    st.caption("汇总数据待接入" if "品种汇总" in label else
                               "暂无已验证排名持仓" if "前20名" in label else "暂无已验证基金持仓")
                    continue
                st.markdown(positioning_signal(row["net"], row["net_change"]))
                st.metric("净持仓（手）", fmt(row["net"]),
                    delta=fmt(row["net_change"], True) if row["net_change"] is not None else None,
                    delta_color="inverse")
                st.caption(f"截至 {chinese_date(row['report_date'])} · 对比 {chinese_date(row['previous_date'])}")
                if row.get("aggregation") == METHOD:
                    st.caption(f"全合约已披露汇总 · {len(row['constituent_contracts'])}个合约 · 东方财富排名")
                elif "主力参考" in label:
                    st.caption("单合约排名 · " + ("东方财富" if row.get("source_provider") == "eastmoney" else "按原始来源披露") + " · 不代表品种汇总")
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
                figure = movements(data[-26:], "最近报告净持仓变化")
                known_changes = [r for r in data[-26:] if r["net_change"] is not None]
                if len(known_changes) == 1:
                    figure.update_xaxes(tickmode="array", tickvals=[known_changes[0]["report_date"]],
                        ticktext=[chinese_date(known_changes[0]["report_date"])])
                if unit == "Delta等价手":
                    figure.update_yaxes(title="净变化（Delta等价手）")
                    figure.update_traces(hovertemplate="%{customdata[1]}<br>净变化 %{y:+,.2f} Delta等价手<br>比较日期 %{customdata[0]}<extra></extra>")
                chart_pair(series_figure(data, f"{item['label']} · 基金净持仓", item["fund_group"]),
                    figure, keys=(f"trend_{market}", f"changes_{market}"))
            with st.expander(item["label"] + "持仓明细"):
                view = [dict(r, label="投资基金" if market == "euronext_rapeseed" else "管理基金") for r in data]
                frame = detail(view).iloc[::-1]
                if unit == "Delta等价手":
                    frame = frame.rename(columns={c: c.replace("（手）", "（Delta等价手）") for c in frame.columns})
                st.dataframe(frame, hide_index=True, width="stretch")
    with inside:
        references = {v for k, v in spec.get("default_scopes", {}).items() if k != v}
        scopes = list(dict.fromkeys([*spec.get("default_scopes", {}).values(), *spec["domestic"],
            *sorted({r["scope"] for r in domestic} - set(spec["domestic"]))]))
        scope = st.selectbox("国内统计范围", scopes,
            index=scopes.index("OI") if domain == "rapeseed" else 0, key=f"domestic_scope_{domain}",
            format_func=lambda v: f"{spec['domestic'][v]} {v} · 品种汇总" if v in spec["domestic"]
                else f"{v} · 已核对主力参考" if v in references else f"{v} · 合约参考")
        data = [r for r in domestic if r["scope"] == scope]
        if not data:
            st.info(f"{spec['domestic'][scope]}品种汇总数据待接入。" if scope in spec["domestic"] else "此合约暂无已验证排名数据。")
            st.caption("接入经过核验的品种汇总排名后，再展示净持仓、变化和五家固定席位。")
        else:
            observed_summary = any(r.get("aggregation") == METHOD for r in data)
            if scope not in spec["domestic"]:
                st.caption("当前为单合约参考，不代表品种汇总，也未自动跟踪主力。")
                if scope in references:
                    check = spec["reference_check"]
                    st.caption(f"主力核对 {chinese_date(check['checked_on'])} · 行情交易日 {chinese_date(check['quote_trade_date'])}（夜盘）；持仓以排名报告日期为准。")
                    if (datetime.now(ZoneInfo("Asia/Shanghai")).date() - date.fromisoformat(check["checked_on"])).days > 7:
                        st.warning("主力核对已超过7天，请复核换月；当前仍展示已保存的合约参考。")
            day = st.selectbox("持仓日期", sorted({r["report_date"] for r in data}, reverse=True), format_func=chinese_date)
            current = {r["group"]: r for r in data if r["report_date"] == day}
            if observed_summary:
                st.caption("全合约已披露持仓汇总：按席位合并后重新排名；未上榜部分不属于已披露持仓。")
                st.caption("覆盖合约：" + "、".join(current['top20']['constituent_contracts']))
            st.markdown(f"**{positioning_signal(current['top20']['net'], current['top20']['net_change'])}**")
            a, b, c = st.columns(3)
            a.metric("前20名榜内净持仓", fmt(current["top20"]["net"]))
            b.metric("较上一保存交易日变化", fmt(current["top20"]["net_change"], True))
            c.metric("五家合计榜内净持仓", fmt(current["fixed5"]["net"]))
            st.caption(f"截至 {chinese_date(day)} · 对比 {chinese_date(current['top20']['previous_date'])} · {current['fixed5']['coverage']}")
            st.caption("公开排名的多空名单可不同，净变化包含名单变化。" + ("当前来源未区分账户类型。" if current["fixed5"]["account"] == "未区分" else "固定席位展示代客持仓。"))
            if observed_summary and current['top20'].get('excluded_contracts'):
                st.caption("未混入旧报告：" + "、".join(f"{r['scope']}（{chinese_date(r['report_date'])}）" for r in current['top20']['excluded_contracts']))
            st.subheader("五家固定席位")
            st.caption(RANKED_NET_NOTICE)
            st.dataframe(domestic_detail([current[m["id"]] for m in members])[["对象", "榜内持仓倾向", "榜内净持仓（手）", "榜内净变化（手）", "多仓（手）", "空仓（手）", "披露情况"]], hide_index=True, width="stretch")
            history = [r for r in data if r["report_date"] <= day]
            if len({r["report_date"] for r in history}) >= 2:
                chart_pair(trend(history, f"五家固定席位 · {current['fixed5']['account']}榜内净持仓", {m["id"]: m["label"] for m in members}, direct_labels=True),
                    movements([r for r in history if r["group"] == "top20"], "前20名榜内净持仓变化"),
                    keys=("fixed_trend", "domestic_changes"))
            with st.expander("历史榜内净持仓与比较日期"):
                st.dataframe(domestic_detail(history).iloc[::-1], hide_index=True, width="stretch")
    with sources:
        st.write("净持仓＝多仓－空仓。净变化反映倾向变化，不等于资金流入流出。国内会员持仓也包含产业套保。")
        st.write("CFTC管理基金与Euronext投资基金的分类不同，各市场单独观察。Euronext期货＋期权保留两位小数；未从分类合计推算交易所总持仓。")
        st.write("五家固定席位：高盛、摩根大通、永安、国泰君安、东证。" + RANKED_NET_NOTICE)
        st.write("仅比较相邻已保存的同口径报告；间隔超过10天时不计算净变化，外盘缺仓也不计算。合约排名不冒充品种总排名，来源、账户、聚合方法或合约范围不同不混比。")
        if domain != "rapeseed":
            check = spec["reference_check"]
            st.markdown(f"国内默认显示已核对主力合约参考：{'、'.join(spec['default_scopes'].values())}。{chinese_date(check['checked_on'])}核对新浪连续行情及实际合约持仓量；行情交易日为{chinese_date(check['quote_trade_date'])}夜盘。配置暂不自动换月，历史按实际合约独立比较，不拼接为主力连续持仓。")
            st.markdown("品种汇总可在国内统计范围中选择：逐合约读取多空排名，按同一席位合并后重新排名。未上榜持仓不可见，不能视作席位完整仓位。净变化比较相同来源、相同合约范围的两份已保存报告。")
            st.markdown("已有排名通过登录浏览器读取[东方财富多空持仓排名](https://qhweb.eastmoney.com/lhb/dkcc/dce/m)，不采用净持仓页的估算数值。联网采集尝试新浪单合约排名；请求失败或尚未发布时保留原报告，不能以行情日期代替持仓日期。服务器自动采集尚未接通。")
            st.markdown("其他接口：[Tushare fut_holding](https://tushare.pro/document/2?doc_id=139)（至少2000积分）和[RQData会员排名](https://www.ricequant.com/doc/rqdata/python/futures-mod)（需账户权限）支持合约或品种查询；尚未取得授权数据，M/Y/P品种汇总覆盖及账户分类仍需样本核验。")
        st.markdown("[CFTC](https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm) · [Euronext](https://live.euronext.com/en/products/commodities/commitments_of_traders) · [郑商所](https://www.czce.com.cn/cn/jysj/ccpm/H077003004index_1.htm)")
        with st.expander("原始来源与采集时间"):
            for source_id, metadata in snapshot.get("sources", {}).items():
                if source_id.startswith(("czce_", "euronext_", "sina_", "browser_contracts_")):
                    provider = "东方财富 · 全合约读取" if source_id.startswith("browser_contracts_") else "郑商所" if source_id.startswith("czce_") else "新浪 · " + source_id.split("_")[1] if source_id.startswith("sina_") else "Euronext"
                    label = provider + " · " + chinese_date(datetime.strptime(source_id.rsplit("_",1)[1], "%Y%m%d").date())
                else:
                    label = source_id
                st.markdown(f"**{label}** · {chinese_time(metadata['retrieved_at'])} · [原始来源]({metadata['url']})")
        with st.expander("最近一次采集结果"):
            st.json(display_attempt(attempt))
        st.caption("本地预览，页面只读；公开上线前需确认数据展示授权。" if preview_mode
                   else "页面展示已保存报告；刷新页面不会触发数据采集。")
