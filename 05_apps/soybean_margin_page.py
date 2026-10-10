"""Quanyong-inspired soybean table and seasonality, using the existing workspace."""
from __future__ import annotations

from datetime import date, datetime
from html import escape
import json
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
from agri_research_agent.soybean_margin.api_inputs import CUTOVER, read_days, apply_api_inputs
from agri_research_agent.market_data.activated_runtime import resolve_public_data_root, resolve_domestic_spread_path
from agri_research_agent.soybean_margin.model import digest
from agri_research_agent.soybean_margin.charts import build_margin_charts, _pm_seasonal_figure
from agri_research_agent.soybean_margin.store import load, save, read_all
from agri_research_agent.soybean_margin.runtime import validate_cnf_write
from agri_research_agent.soybean_margin.reference import submit, read_latest

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
        fx = _cell(row[FIELDS[2]])
        values = [escape(row["shipment_period"]), _cell(row[FIELDS[0]]), _cell(row["usd_cost"]),
                  escape(row["cbot_contract"]), _cell(row[FIELDS[1]]), fx,
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


def _cache_json(value):
    """Content keys preserve zero, NULL and dates; never cache by filename alone."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                      default=lambda item: item.isoformat() if isinstance(item, (date, datetime)) else str(item))


@st.cache_data(show_spinner=False, max_entries=1)
def _prepared_data(history_path, history_identity, public_root, domestic_path,
                   public_identity, day, manual_path, manual_identity,
                   snapshots_json, quotes_json):
    if public_root:
        data, source_dates = _public_rows(history_path, history_identity, public_root,
            domestic_path, public_identity, day, manual_path, manual_identity)
    else:
        data = _overlay_manual(_history(history_path, history_identity), manual_path)
        source_dates = {}
    snapshots = {date.fromisoformat(key): value for key, value in json.loads(snapshots_json).items()}
    data = apply_api_inputs(data, snapshots, day)
    return overlay_cnf(data, json.loads(quotes_json)), source_dates


@st.cache_data(show_spinner=False, max_entries=2)
def _history_tables(view_args, origin):
    data, _ = _prepared_data(*view_args)
    return tuple(history_matrix(data, view_args[5], origin, metric)
                 for metric in (FIELDS[0], "net_margin"))


@st.cache_data(show_spinner=False, max_entries=2)
def _chart_models(view_args, origin):
    history_path, history_identity, public_root, _, _, day, _, _, snapshots_json, quotes_json = view_args
    saved_quotes = json.loads(quotes_json)
    original = _chart_history(history_path, history_identity)
    if public_root or day >= CUTOVER:
        data, _ = _prepared_data(*view_args)
        newer = data.loc[data.business_date > max(original.business_date)].copy()
        newer["retained_net_margin"] = [calculate(*(row[name] for name in FIELDS))["net_margin"]
            for row in newer.to_dict("records")]
        newer["chart_history_source"] = ["soybean_api" if value >= CUTOVER else "public_current"
                                         for value in newer.business_date]
        original = pd.concat([original, newer], ignore_index=True)
    chart_data = overlay_cnf(original, saved_quotes, recalculate_retained=True)
    return build_margin_charts(chart_data, origin, day)


@st.cache_data(show_spinner=False, max_entries=2)
def _chart_figures(view_args, origin, years):
    return tuple(_pm_seasonal_figure(chart, years) for chart in _chart_models(view_args, origin))


def render_soybean_margin_page(history_root: str | Path | None):
    st.markdown(f"## {TITLE}")
    if not history_root:
        st.info("运行数据尚未配置。")
        return
    try:
        path, identity = resolve_history(Path(history_root))
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
    public_root_text = domestic_path_text = public_identity = ""
    manual_path = os.getenv("SOYBEAN_MARGIN_LEGACY_CNF_PATH") or os.getenv("IMPORT_PROFIT_INTRADAY_CNF_STORE_PATH") or ""
    try:
        manual_identity = digest(Path(manual_path)) if manual_path and Path(manual_path).is_file() else ""
    except OSError as exc:
        st.error(f"人工CNF不可读取：{type(exc).__name__}")
        return
    if public_enabled:
        try:
            public_root = resolve_public_data_root(os.getenv("SOYBEAN_MARGIN_PUBLIC_ROOT")
                or os.getenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT") or Path(history_root))
            domestic_path = Path(os.getenv("SOYBEAN_MARGIN_DOMESTIC_PATH", "")) if os.getenv("SOYBEAN_MARGIN_DOMESTIC_PATH") else resolve_domestic_spread_path(public_root / "consumer-artifacts" / "domestic-spread")
            public_identity = digest(public_root / "public-market-data" / "tankan" / "current.json") + digest(domestic_path)
            if st.button("重新读取已更新行情", key="soy-margin-refresh"):
                _public_tables.clear(str(public_root), str(domestic_path), public_identity)
                _prepared_data.clear()
                _history_tables.clear()
                _chart_models.clear()
                _chart_figures.clear()
            _public_tables(str(public_root), str(domestic_path), public_identity)
            public_root_text, domestic_path_text = str(public_root), str(domestic_path)
            market_tables = True
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            if day < CUTOVER:
                st.error(f"公共行情读取或校验失败：{type(exc).__name__}。当前不能确认新行情。")
                return
            st.warning(f"旧公共行情暂不可读：{type(exc).__name__}。旧历史区间仅显示已校验的保留历史。")
    api_root = Path(os.getenv("SOYBEAN_MARGIN_API_ROOT") or
        str(Path(os.getenv("IMPORT_PROFIT_INTRADAY_SNAPSHOT_ROOT") or Path(history_root).parent / "snapshots") / "soybean-api"))
    try:
        snapshots = read_days(api_root, day)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        st.error(f"API行情快照读取或校验失败：{type(exc).__name__}。")
        return
    if day >= CUTOVER:
        market_tables = True
        snapshot = snapshots.get(day)
        if snapshot:
            captured_at = datetime.fromisoformat(snapshot["captured_at"]).astimezone(ZoneInfo("Asia/Shanghai"))
            st.caption(f"实际采集时间：{captured_at:%Y-%m-%d %H:%M:%S}")
        else:
            st.info("所选日期尚无API行情快照。可录入CNF，行情和榨利保留空值。")
    root = os.getenv("SOYBEAN_MARGIN_STORAGE_ROOT", "").strip()
    database = Path(root) / "cnf.sqlite3" if root else None
    try:
        saved_quotes = read_all(database) if database else []
        overrides, revision = load(database, day, origin) if database else ({}, 0)
    except (OSError, ValueError, sqlite3.Error) as exc:
        st.error(f"CNF存储校验失败：{type(exc).__name__}")
        return
    view_args = (str(path), identity, public_root_text, domestic_path_text, public_identity,
                 day, manual_path, manual_identity,
                 _cache_json({key.isoformat(): value for key, value in snapshots.items()}),
                 _cache_json(saved_quotes))
    try:
        data, source_dates = _prepared_data(*view_args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        st.error(f"榨利输入数据不可读取或校验失败：{type(exc).__name__}")
        return
    last = max(data.business_date)
    if day < CUTOVER:
        if market_tables is not None:
            dates_text = " · ".join(f"{label}：{value.isoformat() if value else '暂无'}" for label,value in source_dates.items())
            st.caption(f"已读取公共数据库发布数据 · {dates_text}。")
        elif not public_enabled:
            st.caption(f"历史行情截至 {last.isoformat()}。历史价格为连续合约收盘口径；缺失日期保留空值。")
        if day > last or (market_tables is not None and any(value is None or day > value for value in source_dates.values())):
            st.info("所选日期尚无已发布行情。可预览CNF，行情和榨利保持空值。")
    if day.weekday() >= 5:
        st.info("请选择周一至周五的业务日期。")
    rows = daily_rows(data, day, origin, overrides)
    allowed = bool(database) and os.getenv("SOYBEAN_MARGIN_ALLOW_SAVE") == "1" and day.weekday() < 5
    submit_quotes = os.getenv('SOYBEAN_MARGIN_SUBMIT_QUOTES') == '1'
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
                if submit_quotes:
                    with st.spinner('CNF已提交，正在读取盘面报价…'):
                        submit(database, day, origin, proposed, revision)
                else:
                    save(database, day, origin, proposed, revision)
                st.success("CNF已保存。")
                st.rerun()
            except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
                st.error(str(exc) if isinstance(exc, ValueError) else "CNF保存失败，请重新读取后重试。")
        if not allowed:
            st.caption("当前仅支持会话预览；正式保存入口尚未启用。")
    components.html(daily_html(rows, ORIGINS[origin], day), height=640, scrolling=True)
    if submit_quotes and database:
        st.markdown('#### CNF提交时读取行情的参考榨利')
        try:
            reference = read_latest(database, day, origin, revision)
            if reference and reference['rows']:
                st.caption(f"提交时间：{reference['submitted_at']} · 各行情可能延迟；美豆报价时间尚未确认。")
                components.html(daily_html(reference['rows'], ORIGINS[origin], day), height=640, scrolling=True)
                with st.expander('本次行情时间与缺失状态'):
                    st.json(reference['quotes'])
            elif reference:
                st.info('CNF已保存，本次未取得参考行情：' + (reference['error'] or '行情缺失'))
            else:
                st.info('当前CNF版本尚无提交时行情记录。')
        except (ValueError, OSError, sqlite3.Error, KeyError):
            st.error('提交时行情记录校验失败。')
    for title, matrix in zip(("大豆历史 CNF 报价", "中国进口大豆历史盘面净榨利"),
                             _history_tables(view_args, origin)):
        st.markdown(f"#### {title} · {ORIGINS[origin]}")
        st.dataframe(matrix, hide_index=True,
                     use_container_width=True, column_config={f"{m}月": st.column_config.NumberColumn(format="%.2f") for m in range(1,13)})
    st.markdown("### 盘面榨利历史季节性")
    st.caption(f"中国进口大豆 · {ORIGINS[origin]} · 元/吨 · 沿用原图周期和历史观测值。相邻有效报价间隔不超过10天时连线，较长缺口留白。")
    try:
        charts = _chart_models(view_args, origin)
    except (OSError, ValueError, KeyError):
        st.error("历史榨利图数据校验失败。")
    else:
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
        figures = _chart_figures(view_args, origin, tuple(years))
        for start in range(0,12,per_row):
            for column,chart,figure in zip(st.columns(per_row),charts[start:start+per_row],figures[start:start+per_row]):
                with column:
                    st.plotly_chart(figure, use_container_width=True,
                        config={"displayModeBar": False, "displaylogo": False},
                        key=f"soy-margin-original-{origin}-{chart.spec.shipment_month}")
