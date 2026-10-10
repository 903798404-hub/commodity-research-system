"""Manual CNF entry refreshes domestic quotes and FX before calculating."""
from __future__ import annotations

from datetime import date, datetime
from html import escape
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from agri_research_agent.commodity_import_margin.model import START, PROFILES, daily_rows, number
from agri_research_agent.commodity_import_margin.live import capture as capture_live, LiveError
from agri_research_agent.commodity_import_margin.history import read_history, LABELS
from agri_research_agent.commodity_import_margin.charts import build_seasonal_charts, seasonal_figure
from agri_research_agent.commodity_import_margin.store import (
    load_cnf, save_cnf, read_market, history_dates, cnf_provenance,
)
from agri_research_agent.commodity_import_margin.runtime import database_path, authorize_write, save_enabled

def _live_key(commodity, day):
    return f"commodity-import-live-{commodity}-{day}"


def _refresh_live(commodity, day):
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    key = _live_key(commodity, day)
    if day != today:
        st.session_state[key] = dict(snapshot=None, error="历史日期不使用当前行情重算，读取当日已保存行情。")
        return
    try:
        st.session_state[key] = dict(snapshot=capture_live(commodity), error=None)
    except (ValueError, KeyError, TypeError) as exc:
        st.session_state[key] = dict(snapshot=None, error=str(exc) if isinstance(exc, LiveError) else "行情获取或校验失败")


def _show_sources(snapshot):
    st.caption(f"行情获取时间：{snapshot['captured_at']} · 在岸USD/CNY · 国内：新浪 · 汇率：CFETS")
    fx = snapshot["sources"].get("fx", {})
    if fx:
        kind = "买卖报价中值" if fx.get("rate_kind") == "spot_bid_ask_mid" else "最新成交价（即期报价休市清空时）"
        st.caption(f"即期口径：{kind} · 汇率来源时间：{' / '.join(fx.get('published_at', []))}")
    quotes = snapshot["sources"].get("domestic", {}).get("quotes", {})
    if quotes:
        st.caption("国内合约报价时间：" + "；".join(f"{s} {q['quoted_at']}" for s,q in quotes.items()))
    if snapshot.get("schema_version") == "commodity-import-live-inputs/1":
        st.caption("按来源最新可用报价计算；休市时保留来源实际时间，超过7天的报价不用。")
    if snapshot["errors"]:
        st.warning("部分行情缺失：" + "；".join(f"{k}: {v}" for k,v in snapshot["errors"].items()))


def _table(rows, commodity):
    records = []
    for r in rows:
        record = {"船期":r["shipment_period"], "CNF ($/吨)":r["cnf_usd_per_tonne"], "汇率":r["fx_value"]}
        contract_code = " / ".join(dict.fromkeys(
            f"{year[-2:]}{month}" for year, month in
            (code.rsplit(":", 1)[-1].split("-") for code in r["domestic_contracts"])))
        if commodity == "canola":
            record.update({"国内合约":contract_code, "菜粕盘面":r["domestic_prices"][0],
                           "菜油盘面":r["domestic_prices"][1]})
        record.update({"关税%":PROFILES[commodity]["tariff"]*100, "增值税%":9., "完税成本 (元/吨)":r["duty_paid_cost"]})
        if commodity == "palm":
            record.update({"国内合约":contract_code, "内盘价格":r["domestic_prices"][0]})
        record["盘面榨利 (元/吨)" if commodity == "canola" else "进口利润 (元/吨)"] = r["net_margin"]
        records.append(record)
    return pd.DataFrame(records)


def daily_html(rows, commodity):
    frame = _table(rows, commodity)
    headings = "".join(f"<th>{escape(label)}</th>" for label in frame.columns)
    body = []
    for record in frame.to_dict("records"):
        cells = []
        for key, value in record.items():
            parsed = number(value)
            if value is None or (isinstance(value, float) and parsed is None):
                text = "--"
            elif parsed is not None:
                text = f"{parsed:.4f}" if key == "汇率" else (f"{parsed:.2f}" if "元/吨" in key else f"{parsed:g}")
            else:
                text = str(value)
            cells.append(f"<td>{escape(text)}</td>")
        body.append("<tr>"+"".join(cells)+"</tr>")
    return ("<style>body{font-family:Arial,'Microsoft YaHei',sans-serif;color:#17283f;}"
        ".scroll{overflow-x:auto;}table{border-collapse:collapse;width:100%;min-width:1000px;}"
        "th{background:#ba2924;color:white;padding:15px 10px;font-size:14px;}"
        "td{border:1px solid #e5eaf1;text-align:center;padding:14px 8px;font-size:14px;}"
        "tr:nth-child(even){background:#f8fafc;}</style>"
        f"<div class='scroll'><table><thead><tr>{headings}</tr></thead><tbody>{''.join(body)}</tbody></table></div>")


def _render_seasonal_history(rows, commodity, as_of, *, source):
    metric = "盘面榨利" if commodity == "canola" else "进口利润"
    st.subheader(f"{metric}历史季节性 · {LABELS[commodity]}")
    st.caption("按12个船期月份对比多年周期；相邻有效报价间隔不超过10天时连线，较长缺口留白。")
    st.caption("周期同大豆：1—5月船期从上年5月至当年4月；6—9月船期从当年1月至9月；10—12月船期从当年4月至12月。")
    charts = build_seasonal_charts(rows, commodity, as_of, source=source)
    available_years = sorted({series.series_year for chart in charts for series in chart.series}, reverse=True)
    controls = st.columns([3, 1])
    with controls[0]:
        years = st.multiselect("对比年份", available_years, default=available_years[:6],
                               key=f"season-years-{source}-{commodity}")
    with controls[1]:
        per_row = st.selectbox("每行图数", [3, 2, 1], index=0,
                               key=f"season-columns-v2-{source}-{commodity}")
    if available_years and not years:
        st.info("请选择至少一个对比年份。")
        return
    for start in range(0, 12, per_row):
        for column, chart in zip(st.columns(per_row), charts[start:start+per_row]):
            with column:
                st.plotly_chart(seasonal_figure(chart, years), width="stretch",
                    config={"displayModeBar":False, "displaylogo":False},
                    key=f"season-chart-{source}-{commodity}-{chart.spec.shipment_month}")


def _render_excel_history(database, commodity, as_of):
    st.subheader("历史CNF与利润")
    kinds = ["canola", "canola_oil"] if commodity == "canola" else ["palm"]
    selected_kind = st.selectbox("历史品种", kinds, format_func=LABELS.get,
                                 key=f"excel-history-kind-{commodity}") if len(kinds)>1 else commodity
    try:
        value, revision = read_history(database, selected_kind, as_of)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        st.error("Excel历史身份或数据校验失败，停止展示。")
        return False
    if not value or not value["rows"]:
        st.info("所选日期之前暂无已导入的Excel历史。")
        return False
    data = pd.DataFrame(value["rows"])
    quoted = data[data.cnf_usd_per_tonne.notna()]
    profits = data[data.net_margin.notna()]
    source = value["source"]
    bad = profits[~profits.profit_quality.eq("verified")]
    if len(bad):
        st.warning(f"有{len(bad)}条利润未通过原公式核验；原值在明细中保留，图表不使用。")
    for metric, title in (("cnf_usd_per_tonne", "历史CNF ($/吨)"), ("net_margin", "历史利润 (元/吨)")):
        st.markdown(f"#### {title}")
        recent_days = sorted(data.business_date.unique(), reverse=True)[:12]
        matrix = data[data.business_date.isin(recent_days)].pivot(index="business_date", columns="shipment_month", values=metric).sort_index(ascending=False)
        matrix.columns = [f"{m}月" for m in matrix.columns]
        matrix.index.name = "报价日期"
        st.dataframe(matrix, width="stretch", column_config={
            column: st.column_config.NumberColumn(column, format="%.2f") for column in matrix.columns})
    _render_seasonal_history(value["rows"], selected_kind, as_of, source="excel")
    with st.expander("历史明细与来源凭证"):
        st.caption(f"来源：{source['filename']} / {source['sheet']} · 原表缓存值 · 历史汇率：离岸USD/CNH")
        st.caption(f"报价日期：{data.business_date.min()} 至 {data.business_date.max()} · "
                   f"{quoted.business_date.nunique()}个报价日，{len(quoted):,}条CNF，{len(profits):,}条利润")
        st.caption("原表当月船期按当年当月解释；盘面为1/5/9月交割连续价格，完整合约年份未知。")
        if selected_kind == "canola":
            st.caption("历史口径：出粕58% · 出油42% · 关税9% · 增值税9% · 港杂80元/吨 · 加工220元/吨。历史利润按原表保留。")
        elif selected_kind == "canola_oil":
            st.caption("加拿大菜油为直接进口利润，单独展示；历史税费因子有变更，保留各行原公式和数值。")
        else:
            st.caption("历史口径：关税9% · 增值税9% · 港杂80元/吨；利润 = 国内棕油盘面 − 到港成本。")
        month = st.selectbox("明细船期月份", list(range(1, 13)), key=f"excel-history-month-{selected_kind}")
        filtered = data[data.shipment_month.eq(month)].sort_values("business_date")
        st.caption(f"原文件SHA-256：{source['sha256']} · 导入时间：{value['imported_at']} · 历史版本：{revision}")
        st.dataframe(filtered.rename(columns={"business_date":"报价日期", "shipment_period":"原船期", "cnf_usd_per_tonne":"CNF ($/吨)", "net_margin":"原表利润 (元/吨)", "fx_value":"原表USD/CNH", "duty_paid_cost":"原表到港成本", "profit_quality":"核验状态"}), hide_index=True, width="stretch")
    return True


def render_commodity_import_margin_page(commodity: str, *, today: date | None = None):
    today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    profile = PROFILES[commodity]
    st.title(f"{profile['label']}进口{'盘面净榨利' if commodity == 'canola' else '利润'}")
    st.caption("未来12个月船期 · 当月行表示次年同月 · CNF为美元/吨完整报价")
    st.caption("确认CNF输入后自动刷新国内合约与CFETS在岸USD/CNY；汇率同大豆：近1—2个月即期，3个月起远期。")
    day = st.date_input("业务日期", value=today, max_value=today, key=f"import-date-{commodity}")
    eligible = day >= START
    try:
        database = database_path()
        cnf, revision = load_cnf(database, day, commodity) if eligible else ({}, 0)
        snapshot, identity = read_market(database, day, commodity) if eligible else (None, None)
        provenance = cnf_provenance(database, day, commodity, revision) if eligible else None
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        st.error(f"数据读取或运行身份校验失败：{type(exc).__name__}")
        return
    if not eligible:
        st.info(f"当前仅预览。保存日期须为{START.isoformat()}起的日期。")
    if commodity == "canola":
        st.caption("加拿大菜籽暂无自动CNF来源，采用手工报价；可由Codex按注明业务日与完整船期的报价协助录入。")
    if provenance and provenance.get("entry_method") == "manual_codex":
        st.caption(f"CNF由Codex协助人工录入 · 来源：{provenance['source']} · 录入时间：{provenance['entered_at']} · 原报价时间未知")
    attempt = st.session_state.get(_live_key(commodity, day))
    if attempt:
        if day == today:
            snapshot = attempt["snapshot"]
        if attempt["error"]:
            st.warning(attempt["error"])
    if snapshot:
        _show_sources(snapshot)
    else:
        st.info("所选日期暂无已保存行情。录入CNF后自动获取国内盘面与汇率，缺失的输入保持空值。")
    if day == today:
        st.button("刷新国内盘面与汇率", key=f"import-live-refresh-{commodity}", on_click=_refresh_live, args=(commodity, day))
    rows = daily_rows(day, commodity, cnf, snapshot)
    editor = pd.DataFrame({"船期": [r["shipment_period"] for r in rows],
                           "CNF": pd.Series([r["cnf_usd_per_tonne"] for r in rows], dtype="float64")})
    with st.popover("录入 / 预览 CNF"):
        st.caption("只编辑CNF。完整报价单位美元/吨；留空表示缺失，0可保存并按0计算。")
        edited = st.data_editor(editor, hide_index=True, disabled=["船期"],
            column_config={"CNF": st.column_config.NumberColumn("CNF ($/吨)", min_value=0., step=.01)},
            key=f"import-editor-{commodity}-{day}-{revision}", width="stretch",
            on_change=_refresh_live, args=(commodity, day))
        proposed = {r["shipment_month"]: number(v) for r,v in zip(rows, edited.CNF)}
        rows = daily_rows(day, commodity, proposed, snapshot)
        allowed = eligible and save_enabled()
        if st.button("保存CNF", disabled=not allowed, key=f"import-save-{commodity}"):
            try:
                save_cnf(database, day, commodity, proposed, revision, authorize=authorize_write,
                         market_snapshot=snapshot if snapshot and snapshot.get("schema_version") == "commodity-import-live-inputs/1" else None,
                         expected_identity=identity)
                st.success("CNF已保存。")
                st.rerun()
            except (OSError, ValueError, sqlite3.Error) as exc:
                st.error(str(exc) if isinstance(exc, ValueError) else "保存失败，请重新读取后重试。")
        if not allowed:
            st.caption("当前仅支持会话预览，保存入口尚未启用。")
    st.iframe(daily_html(rows, commodity), height=690, width="stretch")
    with st.expander("汇率期限与计算口径"):
        st.dataframe(pd.DataFrame([{"船期":r["shipment_period"], "目标期限(月)":r["fx_tenor_months"],
            "类型":"即期" if r["fx_tenor_months"] <= 2 else "远期", "插值":r["fx_is_interpolated"]} for r in rows]), hide_index=True)
    if commodity == "canola":
        st.caption("出粕率57.3% · 出油率41.7% · 关税口径14.9% · 增值税9% · 港杂50元/吨 · 加工220元/吨")
        st.caption("完税成本 = CNF × 汇率 × 1.149 × 1.09；净榨利 = 菜粕 × 57.3% + 菜油 × 41.7% − 完税成本 − 50 − 220")
    else:
        st.caption("完税成本 = CNF × 汇率 × 1.09 × 1.09 + 港杂80元/吨；进口利润 = 棕油盘面 − 完税成本")
    st.caption("参数采用本次确认的截图研究口径。历史观察保留各业务日实际输入，不用当前价格补历史。")
    if st.button("刷新已存行情", key=f"import-refresh-{commodity}"):
        st.rerun()
    has_excel = _render_excel_history(database, commodity, day)
    try:
        dates = history_dates(database, commodity, day)
        observations = []
        total_bytes = 0
        for historical_day in dates:
            saved, _ = load_cnf(database, historical_day, commodity)
            market, _ = read_market(database, historical_day, commodity)
            if market:
                from agri_research_agent.commodity_import_margin.inputs import encoded
                total_bytes += len(encoded(market))
                if total_bytes > 64*1024*1024:
                    raise ValueError("历史行情超出64MiB读取上限")
            observations.extend(daily_rows(historical_day, commodity, saved, market))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        st.error("本地历史数据读取或校验失败。")
        return
    st.subheader("本地录价历史")
    if not observations:
        st.info("暂无本地录价历史；Excel来源历史见上方。" if has_excel else "暂无真实历史数据；保存后按业务日期积累，不生成示例报价。")
        return
    data = pd.DataFrame(observations)
    for metric, title in (("cnf_usd_per_tonne", "历史CNF ($/吨)"), ("net_margin", "历史利润 (元/吨)")):
        st.markdown(f"#### {title}")
        recent = data[data.business_date.isin([d.isoformat() for d in dates[:12]])]
        matrix = recent.pivot(index="business_date", columns="shipment_month", values=metric).sort_index(ascending=False)
        matrix.columns = [f"{m}月" for m in matrix.columns]
        st.dataframe(matrix, width="stretch")
    if not data.net_margin.notna().any():
        st.info("历史利润尚无完整输入，图表保持空白。")
        return
    _render_seasonal_history(observations, commodity, day, source="local")
