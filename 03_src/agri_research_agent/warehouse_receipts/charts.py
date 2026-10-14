"""Natural-year warehouse receipt charts using the shared visual contract."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from agri_research_agent.shared.chart_style import (
    CURRENT_LINE_WIDTH, CURRENT_YEAR_COLOR, GRID_COLOR, HISTORY_LINE_WIDTH,
    HISTORY_OPACITY, historical_year_color,
)
from .model import ReceiptError


def seasonal_rows(data: pd.DataFrame, year: int, field: str,
                  trading_calendar: pd.DatetimeIndex) -> pd.DataFrame:
    selected = data.loc[data.date.dt.year.eq(year)].sort_values("date")
    if selected.empty:
        return pd.DataFrame(columns=["date", "value", "anchor"])
    if selected.date.duplicated().any():
        raise ReceiptError("季节性曲线包含重复日期或多个单位")
    if selected.date.min() < trading_calendar.min() or selected.date.max() > trading_calendar.max():
        raise ReceiptError("交易日历未覆盖仓单观测日期")
    dates = trading_calendar[(trading_calendar.year == year) & (trading_calendar <= selected.date.max())]
    dates = dates.union(pd.DatetimeIndex(selected.date)).sort_values()
    aligned = selected.set_index("date")[field].reindex(dates)
    result = aligned.rename("value").rename_axis("date").reset_index()
    # Leap-year anchor keeps March 1 aligned across leap and non-leap years.
    result["anchor"] = pd.to_datetime("2000-" + result.date.dt.strftime("%m-%d"))
    return result


def build_figure(data: pd.DataFrame, *, field: str, focus_year: int, years: list[int],
                 title: str, unit: str, trading_calendar: pd.DatetimeIndex) -> go.Figure:
    if field not in {"vol", "vol_chg"} or data.unit.nunique() != 1:
        raise ReceiptError("仓单图表指标或单位无效")
    figure = go.Figure()
    # Render focus last, so its line remains above the historical curves.
    for year in [y for y in sorted(years) if y != focus_year] + ([focus_year] if focus_year in years else []):
        rows = seasonal_rows(data, year, field, trading_calendar)
        if rows.empty:
            continue
        current = year == focus_year
        figure.add_trace(go.Scatter(x=rows.anchor, y=rows.value,
            customdata=rows.date.dt.strftime("%Y-%m-%d"), mode="lines", connectgaps=False,
            name=f"{year}（对比年）" if current else str(year),
            line=dict(color=CURRENT_YEAR_COLOR if current else historical_year_color(year),
                      width=CURRENT_LINE_WIDTH if current else HISTORY_LINE_WIDTH),
            opacity=1 if current else HISTORY_OPACITY,
            hovertemplate="%{customdata}<br>%{y:,.0f}" + unit + "<extra>%{fullData.name}</extra>"))
    months = pd.date_range("2000-01-01", "2000-12-01", freq="MS")
    figure.update_layout(title=title, height=390, margin=dict(l=35, r=15, t=100, b=35),
        plot_bgcolor="white", paper_bgcolor="white", hovermode="x unified",
        legend=dict(orientation="h", y=1.15, x=0),
        xaxis=dict(range=["2000-01-01", "2000-12-31"], tickvals=months, ticktext=[f"{m}月" for m in range(1, 13)], gridcolor=GRID_COLOR),
        yaxis=dict(title=f"{'仓单量' if field == 'vol' else '每日增减'}（{unit}）", gridcolor=GRID_COLOR),
        uirevision=f"warehouse-{title}-{field}-{unit}")
    return figure
