"""Quanyong-inspired soybean table and seasonality, using the existing workspace."""
from __future__ import annotations

from datetime import date, datetime
from html import escape
import os
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from agri_research_agent.soybean_margin.model import (
    ORIGINS, FIELDS, KEY, daily_rows, history_matrix, read_history, resolve_history,
    number, read_chart_history, overlay_cnf, calculate,
)
from agri_research_agent.soybean_margin.public_inputs import apply_public_inputs, read_public_tables
from agri_research_agent.market_data.activated_runtime import resolve_public_data_root, resolve_domestic_spread_path
from agri_research_agent.soybean_margin.model import digest
from agri_research_agent.soybean_margin.charts import build_margin_charts, _pm_seasonal_figure
from agri_research_agent.soybean_margin.store import load, save, read_all
from agri_research_agent.soybean_margin.runtime import validate_cnf_write

TITLE = "日度进口大豆盘面净榨利"
SOURCE_DIR = Path(__file__).parent / "soybean_margin_assets"


@st.cache_data(show_spinner=False, max_entries=2)
def _history(path: str, identity: str):
    del identity
    return read_history(Path(path))


@st.cache_data(show_spinner=False, max_entries=2)
def _chart_history(path: str, identity: str):
    del identity
    return read_chart_history(Path(path))


@st.cache_data(show_spinner=False, max_entries=2)
def _public_tables(root: str, domestic_path: str, identity: str):
    result = read_public_tables(Path(root), Path(domestic_path))
    if identity != digest(Path(root) / "public-market-data" / "tankan" / "current.json") + digest(Path(domestic_path)):
        raise ValueError("公共行情在读取时发生变化，请重新读取")
    return result


def _public_rows(history_path: str, history_identity: str, root: str, domestic_path: str,
                 public_identity: str, day: date, manual_path: str, manual_identity: str):
    market, fx, domestic, _ = _public_tables(root, domestic_path, public_identity)
    data = _overlay_manual(_history(history_path, history_identity), manual_path)
    if manual_identity and digest(Path(manual_path)) != manual_identity:
        raise ValueError("人工CNF在读取时发生变化，请重新读取")
    return apply_public_inputs(data, market, fx, domestic, day)


def _cell(value, *, profit=False):
    value = number(value)
    if value is None:
        return '<span class="missing">—</span>'
    text = f"{value:,.2f}"
    if profit and value:
        return f'<span class="{"positive" if value > 0 else "negative"}">{"+" if value > 0 else ""}{text}</span>'
    return text


def daily_html(rows, region, day):
    css = (SOURCE_DIR / "tables.css").read_text(encoding="utf-8")
    labels = ["船期", "CNF (美分/蒲)", "美金成本", "CBOT合约", "CBOT价格", "汇率",
              "国内合约", "豆粕盘面", "豆油盘面", "关税%", "增值税%", "完税成本", "盘面净榨利"]
    result = f'<style>{css}</style><section class="panel"><header>中国进口大豆盘面净榨利 · {escape(region)}<small>{day.isoformat()} · 元/吨</small></header><div class="scroll"><table class="terminal-table"><thead><tr>'
    result += "".join(f"<th>{escape(label)}</th>" for label in labels) + "</tr></thead><tbody>"
    for row in rows:
        values = [escape(row["shipment_period"]), _cell(row[FIELDS[0]]), _cell(row["usd_cost"]),
                  escape(row["cbot_contract"]), _cell(row[FIELDS[1]]), _cell(row[FIELDS[2]]),
                  escape(row["domestic_contract"]), _cell(row[FIELDS[3]]), _cell(row[FIELDS[4]]),
                  "3", "9", _cell(row["duty_paid_cost"]), _cell(row["net_margin"], profit=True)]
        result += '<tr class="profit-row">' + "".join(f"<td>{value}</td>" for value in values) + "</tr>"
    result += '</tbody></table></div><footer>出粕率 79.5% · 出油率 19% · 港杂费 50元/吨 · 加工费 150元/吨<br>盘面净榨利 = 豆粕盘面×0.795 + 豆油盘面×0.19 − 完税成本 − 50 − 150</footer></section>'
    return result


def _overlay_manual(data, path):
    if not path or not Path(path).is_file():
        return data
    manual = pd.read_parquet(path)
    if not set(KEY + [FIELDS[0]]).issubset(manual.columns):
        raise ValueError("已保存CNF字段不完整")
    manual["business_date"] = pd.to_datetime(manual.business_date).dt.date
    if manual[KEY].isna().any().any() or manual.duplicated(KEY).any():
        raise ValueError("已保存CNF业务键缺失或重复")
    if not manual.origin.isin(ORIGINS).all() or not manual.shipment_month.isin(range(1,13)).all():
        raise ValueError("已保存CNF产地或月份无效")
    return overlay_cnf(data, manual[KEY + [FIELDS[0]]].to_dict("records"))


def render_soybean_margin_page(history_root: str | Path | None):
    st.markdown(f"## {TITLE}")
    if not history_root:
        st.info("运行数据尚未配置。")
        return
    try:
        path, identity = resolve_history(Path(history_root))
        data = _overlay_manual(_history(str(path), identity),
            os.getenv("SOYBEAN_MARGIN_LEGACY_CNF_PATH") or os.getenv("IMPORT_PROFIT_INTRADAY_CNF_STORE_PATH"))
    except (OSError, ValueError, KeyError) as exc:
        st.error(f"榨利输入数据不可读取或校验失败：{type(exc).__name__}")
        return
    origin = st.radio("产地", list(ORIGINS), format_func=ORIGINS.get, horizontal=True,
                      key="soy-margin-origin")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    day = st.date_input("数据日期", today, max_value=today, key="soy-margin-date")
    public_enabled = any(os.getenv(name, "").strip() for name in
        ("SOYBEAN_MARGIN_PUBLIC_ROOT", "PUBLIC_MARKET_DATA_RUNTIME_ROOT", "PUBLIC_DATA_SERVER_STORE_ROOT"))
    market_tables = None
    if public_enabled:
        try:
            public_root = resolve_public_data_root(os.getenv("SOYBEAN_MARGIN_PUBLIC_ROOT")
                or os.getenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT") or Path(history_root))
            domestic_path = Path(os.getenv("SOYBEAN_MARGIN_DOMESTIC_PATH", "")) if os.getenv("SOYBEAN_MARGIN_DOMESTIC_PATH") else resolve_domestic_spread_path(public_root / "consumer-artifacts" / "domestic-spread")
            public_identity = digest(public_root / "public-market-data" / "tankan" / "current.json") + digest(domestic_path)
            if st.button("重新读取已更新行情", key="soy-margin-refresh"):
                _public_tables.clear(str(public_root), str(domestic_path), public_identity)
            manual_path = os.getenv("SOYBEAN_MARGIN_LEGACY_CNF_PATH") or os.getenv("IMPORT_PROFIT_INTRADAY_CNF_STORE_PATH") or ""
            manual_identity = digest(Path(manual_path)) if manual_path and Path(manual_path).is_file() else ""
            data, source_dates = _public_rows(str(path), identity, str(public_root),
                str(domestic_path), public_identity, day, manual_path, manual_identity)
            market_tables = True
            dates_text = " · ".join(f"{label}：{value.isoformat() if value else '暂无'}" for label,value in source_dates.items())
            st.caption(f"已读取公共数据库发布数据 · {dates_text}。全量更新并同步后，重新打开页面即可读取新发布版本。")
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            st.error(f"公共行情读取或校验失败：{type(exc).__name__}。当前不能确认新行情。")
            return
    last = max(data.business_date)
    if not public_enabled:
        st.caption(f"历史行情截至 {last.isoformat()}。历史价格为连续合约收盘口径；缺失日期保留空值。")
    if day.weekday() >= 5:
        st.info("请选择周一至周五的业务日期。")
    if day > last or (market_tables is not None and any(value is None or day > value for value in source_dates.values())):
        st.info("所选日期尚无已发布行情。可预览CNF，行情和榨利保持空值。")
    root = os.getenv("SOYBEAN_MARGIN_STORAGE_ROOT", "").strip()
    database = Path(root) / "cnf.sqlite3" if root else None
    try:
        saved_quotes = read_all(database) if database else []
        data = overlay_cnf(data, saved_quotes)
        overrides, revision = load(database, day, origin) if database else ({}, 0)
    except (OSError, ValueError, sqlite3.Error) as exc:
        st.error(f"CNF存储校验失败：{type(exc).__name__}")
        return
    rows = daily_rows(data, day, origin, overrides)
    allowed = bool(database) and os.getenv("SOYBEAN_MARGIN_ALLOW_SAVE") == "1" and day.weekday() < 5
    with st.popover("录入 / 预览 CNF", use_container_width=False):
        st.caption("只编辑CNF；0为平水报价，留空为缺失。计算参数保持固定。")
        frame = pd.DataFrame({"船期": [r["shipment_period"] for r in rows],
                              "CNF": [r[FIELDS[0]] for r in rows]})
        frame["CNF"] = pd.to_numeric(frame.CNF, errors="coerce")
        edited = st.data_editor(frame, hide_index=True, disabled=["船期"],
            column_config={"CNF": st.column_config.NumberColumn("CNF (美分/蒲)", step=.5)},
            key=f"soy-margin-editor-{day}-{origin}-{revision}", use_container_width=True)
        proposed = {i+1: number(value) for i, value in enumerate(edited.CNF)}
        rows = daily_rows(data, day, origin, proposed)
        if st.button("保存CNF", disabled=not allowed, key="soy-margin-save"):
            try:
                validate_cnf_write(database)
                save(database, day, origin, proposed, revision)
                st.success("CNF已保存。")
                st.rerun()
            except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
                st.error(str(exc) if isinstance(exc, ValueError) else "CNF保存失败，请重新读取后重试。")
        if not allowed:
            st.caption("当前仅支持会话预览；正式保存入口尚未启用。")
    components.html(daily_html(rows, ORIGINS[origin], day), height=640, scrolling=True)
    for title, metric in [("大豆历史 CNF 报价", FIELDS[0]),
                          ("中国进口大豆历史盘面净榨利", "net_margin")]:
        st.markdown(f"#### {title} · {ORIGINS[origin]}")
        st.dataframe(history_matrix(data, day, origin, metric), hide_index=True,
                     use_container_width=True, column_config={f"{m}月": st.column_config.NumberColumn(format="%.2f") for m in range(1,13)})
    st.markdown("### 盘面榨利历史季节性")
    st.caption(f"中国进口大豆 · {ORIGINS[origin]} · 元/吨 · 沿用原图周期和历史观测值。相邻有效报价间隔不超过10天时连线，较长缺口留白。")
    try:
        if market_tables is not None:
            original = _chart_history(str(path), identity)
            newer = data.loc[data.business_date > max(original.business_date)].copy()
            newer["retained_net_margin"] = [calculate(*(row[name] for name in FIELDS))["net_margin"]
                for row in newer.to_dict("records")]
            newer["chart_history_source"] = "public_current"
            chart_data = overlay_cnf(pd.concat([original, newer], ignore_index=True), saved_quotes,
                recalculate_retained=True)
        else:
            chart_data = overlay_cnf(_chart_history(str(path), identity), saved_quotes,
                recalculate_retained=True)
    except (OSError, ValueError, KeyError):
        st.error("历史榨利图数据校验失败。")
    else:
        charts = build_margin_charts(chart_data, origin, day)
        available_years = sorted({series.series_year for chart in charts for series in chart.series}, reverse=True)
        controls = st.columns([3, 1])
        with controls[0]:
            years = st.multiselect("对比年份", available_years, default=available_years[:6],
                key=f"soy-margin-chart-years-{origin}")
        with controls[1]:
            per_row = st.selectbox("每行图数", [3, 2, 1], key="soy-margin-chart-columns")
        if not years:
            st.info("请选择至少一个对比年份。")
            return
        for start in range(0,12,per_row):
            for column,chart in zip(st.columns(per_row),charts[start:start+per_row]):
                with column:
                    st.plotly_chart(_pm_seasonal_figure(chart, years), use_container_width=True,
                        config={"displayModeBar": False, "displaylogo": False},
                        key=f"soy-margin-original-{origin}-{chart.spec.shipment_month}")
