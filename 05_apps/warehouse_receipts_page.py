"""Read-only warehouse receipt panel inside the existing spread dashboard."""
from __future__ import annotations

import datetime as dt
import os
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from agri_research_agent.warehouse_receipts.charts import build_figure
from agri_research_agent.warehouse_receipts.model import PRODUCTS, ReceiptError, load_snapshot, sha256


def default_receipt_root(project_root: Path) -> Path:
    explicit = os.getenv("WAREHOUSE_RECEIPTS_ROOT", "").strip()
    return Path(explicit) if explicit else Path(os.getenv("SPREAD_DATA_ROOT", str(project_root / "01_data"))) / "processed/warehouse_receipts"


@st.cache_data(show_spinner=False)
def cached_snapshot(root: str, pointer_sha: str) -> tuple[pd.DataFrame, dict]:
    if sha256(Path(root) / "current.json") != pointer_sha:
        raise ReceiptError("仓单指针在读取前发生变化")
    return load_snapshot(Path(root), full_validation=False)


@st.cache_data(show_spinner=False)
def trading_calendar() -> pd.DatetimeIndex:
    from akshare.futures.cons import get_calendar
    return pd.DatetimeIndex(pd.to_datetime(get_calendar(), format="%Y%m%d", errors="raise")).drop_duplicates().sort_values()


def render_warehouse_receipts(project_root: Path) -> None:
    st.subheader("仓单季节性")
    root = default_receipt_root(project_root)
    try:
        digest = sha256(root / "current.json")
        data, manifest = cached_snapshot(str(root.resolve()), digest)
        if sha256(root / "current.json") != digest:
            raise ReceiptError("仓单指针在显示前发生变化")
    except (OSError, ReceiptError) as exc:
        st.info("仓单历史尚未接入。" if isinstance(exc, OSError) else str(exc))
        return
    available = sorted(data.date.dt.year.unique().tolist())
    now_year = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).year
    controls = st.columns(3)
    with controls[0]:
        selected = st.multiselect("品种", list(PRODUCTS), default=list(PRODUCTS),
                                 format_func=lambda s: f"{PRODUCTS[s][0]} {s}", key="warehouse_products")
    with controls[1]:
        focus = int(st.selectbox("对比年份", sorted(set(available + [now_year]), reverse=True), key="warehouse_focus"))
    with controls[2]:
        metric = st.selectbox("仓单指标", ["仓单总量", "每日增减"], key="warehouse_metric")
    years = st.multiselect("显示年份", available, default=[y for y in available if y >= 2022], key="warehouse_years")
    st.caption("自然年按月日对齐；红线为对比年，历史年份固定配色。按源单位分别绘图，缺测断线，未来不延长。")
    if not selected or not years:
        st.info("请选择品种和显示年份。")
        return
    field = "vol" if metric == "仓单总量" else "vol_chg"
    columns = st.columns(2)
    calendar = (pd.DatetimeIndex(pd.to_datetime(manifest["trading_dates"], format="%Y-%m-%d", errors="raise"))
                if manifest.get("trading_dates") else trading_calendar())
    status_path = root / "update_status.json"
    if status_path.is_file():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8", errors="strict"))
            checked = pd.Timestamp(status["checked_at"]).tz_convert("Asia/Shanghai")
            label = {"UPDATED": "已更新", "NO_CHANGE": "无新增数据", "WAITING_FOR_SOURCE": "等待源数据", "FAILED": "更新失败，保留上次数据"}.get(status["status"], "已检查")
            st.caption(f"每日17:30更新 · 最近检查 {checked:%Y-%m-%d %H:%M} · {label}")
        except (OSError, ValueError, KeyError, TypeError):
            st.caption("最近更新状态暂不可读取；当前图表仍使用已校验快照。")
    for index, symbol in enumerate(selected):
        with columns[index % 2]:
            with st.container(border=True):
                product = data.loc[data.symbol.eq(symbol)].copy()
                st.markdown(f"#### {PRODUCTS[symbol][0]} · {symbol}")
                if product.empty:
                    st.info("暂无真实仓单观测。")
                    continue
                invalid_days = int(product.status.eq("invalid_component").sum())
                if invalid_days:
                    st.caption(f"源数据有 {invalid_days} 天包含负仓单量，相关总量留空；原始记录已保留。")
                for unit in sorted(product.unit.unique()):
                    block = product.loc[product.unit.eq(unit)]
                    actual = block.loc[block.date.dt.year.eq(focus) & block.vol.notna()].sort_values("date")
                    if actual.empty:
                        st.caption(f"{focus} 年暂无有效仓单观测（{unit}）。")
                    else:
                        latest = actual.iloc[-1]
                        delta = f"{latest.vol_chg:+,.0f}{unit}" if pd.notna(latest.vol_chg) else None
                        st.metric(f"{focus} 年最新仓单（{unit}）", f"{latest.vol:,.0f}", delta=delta, delta_color="off")
                        st.caption(f"观测日期 {latest.date:%Y-%m-%d} · {PRODUCTS[symbol][1]} · 源单位 {unit}")
                    chart_data = block.loc[block.date.dt.year.isin(years)]
                    if chart_data.empty or chart_data[field].notna().sum() == 0:
                        st.info(f"所选年份暂无{metric}观测（{unit}）。")
                        continue
                    try:
                        figure = build_figure(block, field=field, focus_year=focus, years=years,
                            title=f"{PRODUCTS[symbol][0]} · {metric}", unit=unit, trading_calendar=calendar)
                        st.plotly_chart(figure, width="stretch", key=f"warehouse_chart_{symbol}_{unit}")
                    except ReceiptError as exc:
                        st.error(str(exc))
                panel = st.expander("查看日度明细", key=f"warehouse_detail_{symbol}", on_change="rerun")
                if panel.open:
                    with panel:
                        detail = product.loc[product.date.dt.year.isin(years)].sort_values("date", ascending=False)
                        st.dataframe(detail.head(200), hide_index=True, width="stretch")
                        st.download_button("下载完整日度数据", detail.to_csv(index=False).encode("utf-8-sig"),
                            file_name=f"warehouse_receipts_{symbol}.csv", mime="text/csv", key=f"warehouse_download_{symbol}", on_click="ignore")
    scope = st.expander("仓单数据口径", key="warehouse_scope", on_change="rerun")
    if scope.open:
        with scope:
            st.write("来源：Tushare兼容接口 fut_wsr，仓库/厂库日报。原始年度、等级、品牌、地区及折算标识均保留；总计与明细不重复累加。这里展示仓单，不能直接等同于产业库存。")
            st.write("每日增减直接汇总源 vol_chg，昨日量缺失不反推；任一分项缺失或数量为负时对应总量留空。单位切换单独绘图，不推定吨数换算。")
            st.write(f"本次请求 {manifest['requested_start']} 至 {manifest['requested_end']}；实际覆盖如下。")
            st.dataframe(pd.DataFrame(manifest["coverage"]).T, width="stretch")
            st.caption(f"快照 {manifest['snapshot_id'][:12]} · 更新与页面访问分开执行，打开网页不触发外网取数。")
