"""One chronological, three-region chart alongside the unchanged Liuzhou views."""
from __future__ import annotations

import os
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from agri_research_agent.data_sources.domestic_sugar_history import SugarHistoryError
from agri_research_agent.data_sources.domestic_sugar_regional_history import (
    REGIONS, RegionalSugarHistory, load_regional_history, resolve_regional_history,
)
from agri_research_agent.shared.chart_style import GRID_COLOR, HISTORICAL_YEAR_COLORS, MEAN_COLOR

REGION_COLORS = dict(zip(REGIONS, (HISTORICAL_YEAR_COLORS[0], HISTORICAL_YEAR_COLORS[6], MEAN_COLOR)))
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@st.cache_data(show_spinner=False, max_entries=4)
def _cached_regional(root: str, snapshot_id: str) -> RegionalSugarHistory:
    snapshot = load_regional_history(Path(root))
    if snapshot.snapshot_id != snapshot_id:
        raise SugarHistoryError("三地白糖历史在显示前发生变化")
    return snapshot


def load_regional_page_data(root: Path) -> RegionalSugarHistory:
    _, sid = resolve_regional_history(root)
    snapshot = _cached_regional(str(root.resolve()), sid)
    _, after = resolve_regional_history(root)
    if after != sid:
        raise SugarHistoryError("三地白糖历史指针在显示期间发生变化")
    return snapshot


def build_regional_basis_figure(daily: pd.DataFrame, *, missing_trading_days) -> Figure:
    figure = Figure(figsize=(12, 4.2), dpi=120); FigureCanvasAgg(figure)
    axis = figure.subplots()
    for region in REGIONS:
        selected = daily.loc[daily.region.eq(region)].sort_values("business_date")
        xs, ys = [], []; previous = None
        for row in selected.itertuples(index=False):
            if previous is not None and missing_trading_days(previous, row.business_date) > 5:
                xs.append(row.business_date); ys.append(float("nan"))
            xs.append(row.business_date)
            ys.append(float(row.basis) if pd.notna(row.basis) else float("nan"))
            previous = row.business_date
        axis.plot(xs, ys, label=region, color=REGION_COLORS[region], linewidth=1.6,
                  linestyle="--" if region == "乌鲁木齐" else "-")
    axis.axhline(0, color="#333333", linewidth=.9)
    axis.set_ylabel("基差（元/吨）", fontsize=10)
    axis.grid(True, color=GRID_COLOR, linewidth=.6)
    axis.spines[["top", "right"]].set_visible(False)
    locator = mdates.AutoDateLocator(minticks=4, maxticks=9)
    axis.xaxis.set_major_locator(locator)
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y年%m月"))
    axis.legend(loc="upper left", bbox_to_anchor=(0, 1.12), ncol=3, frameon=False, fontsize=10)
    axis.tick_params(labelsize=9)
    if not daily.empty:
        first, last = daily.business_date.min(), daily.business_date.max()
        if first < last:
            axis.set_xlim(first, last)
    figure.tight_layout()
    return figure


def render_regional_sugar_history(*, missing_trading_days, root: Path | None = None) -> None:
    configured = root or Path(os.environ.get("DOMESTIC_SUGAR_REGIONAL_HISTORY_ROOT",
        str(PROJECT_ROOT / "01_data" / "processed" / "domestic_sugar_regional_history")))
    # Existing installations without this optional independent dataset retain their exact old views.
    if not configured.exists():
        return
    st.subheader("三地白糖主力基差 · 长周期")
    try:
        snapshot = load_regional_page_data(configured)
    except (SugarHistoryError, OSError, ValueError):
        st.warning("三地白糖历史快照校验失败，该图暂停显示。")
        return
    daily = snapshot.daily
    lower, upper = daily.business_date.min().date(), daily.business_date.max().date()
    default_start = max(lower, min(upper, pd.Timestamp("2025-01-01").date()))
    left, right = st.columns(2)
    with left:
        start = st.date_input("开始日期", value=default_start, min_value=lower, max_value=upper,
                              key="sugar_regional_start_" + snapshot.snapshot_id)
    with right:
        end = st.date_input("结束日期", value=upper, min_value=lower, max_value=upper,
                            key="sugar_regional_end_" + snapshot.snapshot_id)
    if start > end:
        st.info("开始日期不能晚于结束日期。")
        return
    selected = daily.loc[daily.business_date.between(pd.Timestamp(start), pd.Timestamp(end))].copy()
    if selected.basis.notna().sum() == 0:
        st.info("所选日期没有有效的同日现货与期货配对。")
        return
    st.caption("基差 = 各地贸易商现货报价平均值 − 同日SR主力合约未复权收盘价。"
               "三地使用相同主力映射；换月跳变保留，缺失不填补。")
    figure = build_regional_basis_figure(selected, missing_trading_days=missing_trading_days)
    st.pyplot(figure, width="stretch"); plt.close(figure)
    quote_date = daily.loc[daily.cash_price.notna(), "business_date"].max()
    paired_date = daily.loc[daily.basis.notna(), "business_date"].max()
    st.caption(f"现货最新 {quote_date:%Y-%m-%d}｜同日配对最新 {paired_date:%Y-%m-%d}｜默认从2025年1月开始，可调整起止日期。")
    with st.expander("三地基差明细与下载", expanded=False):
        detail = selected[["business_date", "region", "ts_code", "cash_price", "futures_price", "basis", "source_cell"]]
        st.dataframe(detail.sort_values(["business_date", "region"], ascending=[False, True]).head(300),
                     hide_index=True, width="stretch")
        st.download_button("下载三地完整日数据", detail.to_csv(index=False).encode("utf-8-sig"),
                           file_name="sugar_three_region_main_basis.csv", mime="text/csv", key="sugar_regional_download")
