"""Liuzhou history section inside the existing domestic spot/basis page."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Callable

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg

from agri_research_agent.data_sources.domestic_sugar_history import (
    SugarHistorySnapshot, file_sha256, load_sugar_history, resolve_sugar_history,
)


@st.cache_data(show_spinner=False)
def _cached_history(root: str, snapshot_id: str, manifest_sha256: str) -> SugarHistorySnapshot:
    snapshot = load_sugar_history(root)
    if snapshot.snapshot_id != snapshot_id or file_sha256(Path(root) / "snapshots" / snapshot_id / "manifest.json") != manifest_sha256:
        raise ValueError("柳州历史身份在读取前发生变化")
    return snapshot


def load_sugar_page_data(root: Path) -> SugarHistorySnapshot:
    database, manifest = resolve_sugar_history(root)
    manifest_sha = file_sha256(database.parent / "manifest.json")
    snapshot = _cached_history(str(root.resolve()), str(manifest["snapshot_id"]), manifest_sha)
    after_database, after = resolve_sugar_history(root)
    if after["snapshot_id"] != snapshot.snapshot_id or file_sha256(after_database.parent / "manifest.json") != manifest_sha:
        raise ValueError("柳州历史指针在显示前发生变化")
    return snapshot


def sugar_chart_rows(snapshot: SugarHistorySnapshot, kind: str) -> pd.DataFrame:
    if kind == "spot":
        data = snapshot.spot.copy()
        data["value"] = data.cash_price
        data["chart_year"] = data.business_date.dt.year
    elif kind == "main":
        data = snapshot.main_basis.copy()
        data["value"] = data.basis
        data["chart_year"] = data.business_date.dt.year
    elif kind in {"01", "05", "09"}:
        data = snapshot.fixed_basis.loc[snapshot.fixed_basis.delivery_month.eq(int(kind))].copy()
        data["chart_year"] = data.delivery_year - 1
        month = int(kind)
        start = pd.to_datetime(dict(year=data.chart_year, month=pd.Series(month + 1, index=data.index), day=pd.Series(1, index=data.index)))
        end = pd.to_datetime(dict(year=data.delivery_year, month=pd.Series(month + 1, index=data.index), day=pd.Series(1, index=data.index)))
        data = data.loc[data.business_date.ge(start) & data.business_date.lt(end)].copy()
        data["value"] = data.basis
    else:
        raise ValueError("白糖图表类型无效")
    return data.sort_values(["chart_year", "business_date"])


def build_sugar_seasonal_figure(
    data: pd.DataFrame, *, kind: str, title: str, years: list[int],
    year_colors: dict[int, str], fallback_colors: list[str],
    missing_trading_days: Callable[[object, object], int],
) -> Figure:
    """Same colors, dimensions, widths and grid as domestic spot/basis charts."""
    fixed = kind in {"01", "05", "09"}
    month = int(kind) if fixed else None
    prior_anchor_year = (2000 if month == 1 else 1999) if fixed else None
    anchor_start = dt.date(prior_anchor_year, month + 1, 1) if fixed else dt.date(2000, 1, 1)
    figure = Figure(figsize=(6.2, 3.55), dpi=120)
    FigureCanvasAgg(figure)
    axis = figure.subplots()
    for index, year in enumerate(years):
        selected = data.loc[data.chart_year.eq(year)].sort_values("business_date")
        if selected.empty:
            continue
        if selected.business_date.duplicated().any():
            plt.close(figure)
            raise ValueError("白糖同一季节曲线日期存在重复记录")
        xs: list[float] = []
        ys: list[float] = []
        previous = None
        for row in selected.itertuples(index=False):
            date = row.business_date.date()
            if previous is not None and missing_trading_days(previous, row.business_date) > 5:
                xs.append(float("nan")); ys.append(float("nan"))
            anchor_year = (prior_anchor_year if date.month > month else prior_anchor_year + 1) if fixed else 2000
            anchor = dt.date(anchor_year, date.month, date.day)
            xs.append((anchor - anchor_start).days + 1)
            ys.append(float(row.value) if pd.notna(row.value) else float("nan"))
            previous = row.business_date
        anchor_year = min(year_colors)
        axis.plot(xs, ys, color=year_colors.get(year, fallback_colors[(year - anchor_year) % len(fallback_colors)]),
                  linewidth=1.8, label=f"{year}/{str(year + 1)[2:]}" if fixed else str(year))
    if kind != "spot":
        axis.axhline(0, color="#222222", linewidth=0.9, alpha=0.9)
    ticks, labels = [], []
    first_month = month + 1 if fixed else 1
    for offset in range(12):
        m = (first_month + offset - 1) % 12 + 1
        y = (prior_anchor_year if m > month else prior_anchor_year + 1) if fixed else 2000
        ticks.append((dt.date(y, m, 1) - anchor_start).days + 1)
        labels.append(f"{m:02d}-01")
    axis.set_title(title, fontsize=12, fontweight="bold", pad=9)
    axis.set_xlim(1, 366)
    axis.set_xticks(ticks)
    axis.set_xticklabels(labels, rotation=45, ha="right", fontsize=7)
    axis.set_ylabel("现货价格（元/吨）" if kind == "spot" else "基差（元/吨）", fontsize=9)
    axis.grid(True, color="#D9D9D9", linewidth=0.6, alpha=0.8)
    axis.tick_params(axis="y", labelsize=8)
    axis.spines["top"].set_visible(False); axis.spines["right"].set_visible(False)
    if axis.lines:
        axis.legend(loc="upper right", frameon=False, fontsize=8, ncol=min(3, len(years)))
    figure.tight_layout()
    return figure


def _render_chart(snapshot: SugarHistorySnapshot, kind: str, title: str, years: list[int],
                  year_colors: dict[int, str], fallback_colors: list[str], missing_trading_days) -> None:
    data = sugar_chart_rows(snapshot, kind)
    filtered = data.loc[data.chart_year.isin(years)].copy()
    with st.container(border=True):
        if filtered.value.notna().sum() == 0:
            st.markdown(f"#### {title}")
            st.info("所选年份没有有效报价。")
            return
        figure = build_sugar_seasonal_figure(filtered, kind=kind, title=title, years=years,
            year_colors=year_colors, fallback_colors=fallback_colors, missing_trading_days=missing_trading_days)
        st.pyplot(figure, width="stretch")
        plt.close(figure)
        if kind in {"01", "05", "09"}:
            st.caption("图例为交割周期：2026/27 对应 2027 年交割合约；按周期起始年沿用年度线色。")
        with st.expander("查看明细", expanded=False):
            cols = ["business_date", "ts_code", "cash_price", "futures_price", "basis"] if kind != "spot" else ["business_date", "cash_price", "source_cell", "status"]
            detail = filtered[cols].sort_values("business_date", ascending=False)
            st.dataframe(detail.head(200), hide_index=True, width="stretch")
            st.download_button("下载完整日数据", detail.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"liuzhou_{kind}_history.csv", mime="text/csv", key=f"sugar_download_{kind}")


def render_sugar_history(snapshot: SugarHistorySnapshot, *, year_colors: dict[int, str],
                         fallback_colors: list[str], missing_trading_days) -> None:
    st.subheader("白糖 · 柳州现货与基差")
    observed = snapshot.spot.loc[snapshot.spot.cash_price.notna()].sort_values("business_date")
    if observed.empty:
        st.info("柳州历史没有有效现货报价。")
        return
    latest = observed.iloc[-1]
    common = snapshot.nearest_basis.loc[snapshot.nearest_basis.basis.notna(), "business_date"].max()
    prices = snapshot.nearest_basis.loc[snapshot.nearest_basis.business_date.eq(common)].sort_values("delivery_month")
    cards = st.columns(4)
    cards[0].metric("柳州现货（元/吨）", f"{latest.cash_price:,.0f}")
    for card, month in zip(cards[1:], (1, 5, 9)):
        rows = prices.loc[prices.delivery_month.eq(month)]
        row = rows.iloc[0] if not rows.empty else None
        card.metric(f"{row.ts_code.removesuffix('.ZCE')} 基差" if row is not None and isinstance(row.ts_code, str) else f"SR{month:02d} 基差",
                    f"{row.basis:,.0f}" if row is not None and pd.notna(row.basis) else "—")
    common_label = common.strftime("%Y-%m-%d") if pd.notna(common) else "暂无同日配对"
    st.caption(f"现货报价 {latest.business_date:%Y-%m-%d}｜同日基差 {common_label}｜"
               "基差 = 柳州贸易商报价平均值 − SR 原始合约收盘价；不同日不配对，缺失不填补。")
    years = sorted(observed.business_date.dt.year.unique().tolist())
    selected = st.multiselect("白糖对比年份", years, default=years[-3:], key="sugar_history_years")
    st.caption("现货和主力按自然年；固定 01/05/09 合约按上年下一月至交割月排列。所有曲线使用完整日数据。")
    if not selected:
        st.info("请选择至少一个对比年份。")
        return
    columns = st.columns(2)
    for index, (kind, title) in enumerate((("spot", "柳州现货季节性"), ("01", "柳州 SR01 基差"), ("05", "柳州 SR05 基差"), ("09", "柳州 SR09 基差"), ("main", "柳州主力基差（辅助）"))):
        with columns[index % 2]:
            _render_chart(snapshot, kind, title, selected, year_colors, fallback_colors, missing_trading_days)
    with st.expander("白糖数据口径", expanded=False):
        source = snapshot.manifest["source_workbook"]
        st.write(f"现货：钢联柳州当地贸易商报价平均值。原表工作表 {source['sheet']}，C列。")
        st.write("另一条一级糖含税车板价独立保存，不与主序列拼接。期货来自已确认的Tushare兼容网关，使用close而非settle或复权价。")
        st.write("主力依据SR.ZCE实际合约映射，保留换月跳变；历史中03、07月主力使用对应原始行情。")
        st.write("当前为一次性历史快照，尚未开启自动更新。")
