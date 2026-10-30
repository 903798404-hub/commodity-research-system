"""Interactive domestic price charts, with exact observation dates in tooltips."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go


def seasonal_chart(data: pd.DataFrame, *, title: str, years: list[int],
                   year_colors: dict[int, str], fallback_colors: list[str],
                   unit: str, zero_line: bool, missing_trading_days,
                   start_month: int = 1, cycle_labels: bool = False) -> go.Figure:
    """Input columns: business_date, chart_year, value; optional ts_code."""
    # Leap-year anchors keep Feb 29, including the Feb--Jan delivery window.
    anchor_year = 2000 if start_month in (1, 2) else 1999
    anchor_start = pd.Timestamp(anchor_year, start_month, 1)
    figure = go.Figure()
    for year in years:
        rows = data.loc[data.chart_year.eq(year)].sort_values("business_date")
        if rows.empty:
            continue
        if rows.business_date.duplicated().any():
            raise ValueError("同一图表日期存在重复记录")
        xs, ys, custom = [], [], []
        previous = None
        for row in rows.itertuples(index=False):
            date = pd.Timestamp(row.business_date)
            if previous is not None and missing_trading_days(previous, date) > 5:
                xs.append(None); ys.append(None); custom.append([None, None])
            anchored = pd.Timestamp(anchor_year + int(date.month < start_month), date.month, date.day)
            xs.append((anchored - anchor_start).days + 1)
            ys.append(float(row.value) if pd.notna(row.value) else None)
            code = getattr(row, "ts_code", "")
            custom.append([date.strftime("%Y-%m-%d"), code if pd.notna(code) else ""])
            previous = date
        figure.add_trace(go.Scatter(
            x=xs, y=ys, customdata=custom, mode="lines", connectgaps=False,
            name=f"{year}/{str(year + 1)[2:]}" if cycle_labels else str(year),
            line=dict(color=year_colors.get(year, fallback_colors[(year - min(year_colors)) % len(fallback_colors)]), width=1.8),
            hovertemplate="%{customdata[0]}<br>%{y:,.0f} 元/吨<br>%{customdata[1]}<extra>%{fullData.name}</extra>",
        ))
    ticks = [anchor_start + pd.DateOffset(months=i) for i in range(12)]
    figure.update_layout(
        title=dict(text=title, font=dict(size=16)), height=380,
        margin=dict(l=15, r=15, t=80, b=30), paper_bgcolor="white", plot_bgcolor="white",
        hovermode="closest", dragmode="zoom",
        legend=dict(orientation="h", y=1.14, x=0), yaxis_title=unit,
        xaxis=dict(range=[1, 366], tickmode="array",
                   tickvals=[(tick - anchor_start).days + 1 for tick in ticks],
                   ticktext=[tick.strftime("%m-%d") for tick in ticks], showgrid=False),
        uirevision=title + ":" + ",".join(map(str, years)),
    )
    figure.update_yaxes(gridcolor="#D9D9D9", zeroline=False)
    if zero_line:
        figure.add_hline(y=0, line_color="#222222", line_width=.9)
    return figure


PLOTLY_CONFIG = dict(displaylogo=False, scrollZoom=True,
                     toImageButtonOptions=dict(format="png", scale=2))
