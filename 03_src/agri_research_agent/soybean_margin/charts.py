"""Soybean seasonal charts with original cycles and observed-value continuity."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
import pandas as pd
import plotly.graph_objects as go

from .model import FIELDS, calculate, number

@dataclass(frozen=True)
class PmSeasonalChartSpec:
    shipment_month: int
    domestic_contract_month: str
    window_months: tuple[int, ...]
    cross_year: bool
    @property
    def title(self):
        return f"大豆盘面榨利：{self.shipment_month}月对{self.domestic_contract_month}"

@dataclass(frozen=True)
class Point:
    trade_date: date
    series_year: int
    shipment_month: int
    domestic_contract: str
    net_crush_margin_cny_per_tonne: float | None
    season_position: int
    release_id: str = "retained-history"
    history_source: str = "legacy"

@dataclass(frozen=True)
class Series:
    series_year: int
    points: tuple[Point, ...]

@dataclass(frozen=True)
class PmSeasonalChart:
    spec: PmSeasonalChartSpec
    series: tuple[Series, ...]

PM_SEASONAL_CHART_SPECS = (
    PmSeasonalChartSpec(1, "01", (5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3, 4), True),
    PmSeasonalChartSpec(2, "05", (5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3, 4), True),
    PmSeasonalChartSpec(3, "05", (5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3, 4), True),
    PmSeasonalChartSpec(4, "05", (5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3, 4), True),
    PmSeasonalChartSpec(5, "05", (5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3, 4), True),
    PmSeasonalChartSpec(6, "09", tuple(range(1, 10)), False),
    PmSeasonalChartSpec(7, "09", tuple(range(1, 10)), False),
    PmSeasonalChartSpec(8, "09", tuple(range(1, 10)), False),
    PmSeasonalChartSpec(9, "09", tuple(range(1, 10)), False),
    PmSeasonalChartSpec(10, "01", tuple(range(4, 13)), False),
    PmSeasonalChartSpec(11, "01", tuple(range(4, 13)), False),
    PmSeasonalChartSpec(12, "01", tuple(range(4, 13)), False),
)

SEASONAL_YEAR_COLORS = (
    "#D95F8D",
    "#1B9E77",
    "#66A61E",
    "#E6AB02",
    "#E67E22",
    "#1F77B4",
    "#8C564B",
    "#9467BD",
    "#17BECF",
    "#BCBD22",
    "#3366CC",
    "#A6761D",
)

LATEST_SEASONAL_YEAR_COLOR = "#D62728"
MAX_CONNECTED_GAP_DAYS = 10


def _line_points(points: tuple[Point, ...]) -> list[Point | None]:
    """Connect nearby observations; never fill values or bridge long data outages."""
    observed = sorted(
        (point for point in points if point.net_crush_margin_cny_per_tonne is not None),
        key=lambda point: point.trade_date,
    )
    result: list[Point | None] = []
    for previous, point in zip([None] + observed, observed):
        if previous and (point.trade_date - previous.trade_date).days > MAX_CONNECTED_GAP_DAYS:
            result.append(None)
        result.append(point)
    return result

def _pm_seasonal_figure(chart: PmSeasonalChart, years=None) -> go.Figure:
    figure = go.Figure()
    visible_series = [series for series in chart.series if years is None or series.series_year in years]
    latest_year = max(
        (series.series_year for series in visible_series), default=None
    )
    for series in visible_series:
        latest = series.series_year == latest_year
        color = (
            LATEST_SEASONAL_YEAR_COLOR
            if latest
            else _seasonal_year_color(series.series_year)
        )
        points = _line_points(series.points)
        customdata = [
            (
                point.trade_date.isoformat(),
                point.series_year,
                point.shipment_month,
                point.domestic_contract,
                point.release_id,
                (
                    "历史盘面榨利"
                    if point.history_source == "legacy"
                    else "人工CNF重算" if point.history_source == "manual_cnf" else "公共行情与人工CNF"
                ),
            ) if point else None
            for point in points
        ]
        # Keep a dot for isolated observations and the newest endpoint only.
        marker_sizes = [
            5 if point and (
                (index == 0 or points[index-1] is None)
                and (index == len(points)-1 or points[index+1] is None)
                or (latest and index == len(points)-1)
            ) else 0
            for index, point in enumerate(points)
        ]
        figure.add_trace(
            go.Scatter(
                x=[point.season_position if point else None for point in points],
                y=[
                    point.net_crush_margin_cny_per_tonne if point else None
                    for point in points
                ],
                customdata=customdata,
                mode="lines+markers",
                name=str(series.series_year),
                line={"color": color, "width": 2.8 if latest else 1.6, "shape": "linear"},
                marker={"color": color, "size": marker_sizes},
                opacity=1 if latest else 0.75,
                connectgaps=False,
                hovertemplate=(
                    "真实日期：%{customdata[0]}<br>"
                    "周期年份：%{customdata[1]}周期<br>"
                    "船期：%{customdata[2]}月船期<br>"
                    "国内合约：%{customdata[3]}<br>"
                    "盘面榨利：%{y:,.2f} 元/吨<br>"
                    "数据口径：%{customdata[5]}"
                    "<extra></extra>"
                ),
            )
        )
    if not visible_series:
        figure.add_annotation(
            text="暂无盘面榨利历史数据",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font={"color": "#7b8b9b", "size": 12},
        )
    tickvals = [index * 31 + 14 for index in range(len(chart.spec.window_months))]
    figure.update_layout(
        title={"text": chart.spec.title, "x": 0, "xanchor": "left", "y": .97},
        title_font={"size": 15, "color": "#182638"},
        height=380,
        margin={"l": 55, "r": 15, "t": 55, "b": 88},
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        font={"family": "Microsoft YaHei, Arial, sans-serif", "color": "#46515d", "size": 11},
        hovermode="closest",
        hoverlabel={"bgcolor": "white", "font_size": 12},
        legend={
            "orientation": "h",
            "x": 0,
            "y": -.19,
            "yanchor": "top",
            "font": {"size": 11},
            "traceorder": "reversed",
            "itemclick": "toggle",
            "itemdoubleclick": "toggleothers",
        },
        xaxis={
            "tickmode": "array",
            "tickvals": tickvals,
            "ticktext": [
                f"{month}月" for month in chart.spec.window_months
            ],
            "tickangle": 0,
            "tickfont": {"size": 10},
            "range": [-1, len(chart.spec.window_months) * 31],
            "showgrid": False,
            "showline": True,
            "linecolor": "#dce2e8",
            "zeroline": False,
            "fixedrange": True,
        },
        yaxis={
            "title": "盘面榨利（元/吨）",
            "rangemode": "tozero",
            "gridcolor": "#edf1f5",
            "tickformat": ",.0f",
            "zeroline": True,
            "zerolinecolor": "#8fa3b5",
            "zerolinewidth": 1.2,
        },
    )
    return figure

def _seasonal_year_color(series_year: int) -> str:
    return SEASONAL_YEAR_COLORS[series_year % len(SEASONAL_YEAR_COLORS)]


def build_margin_charts(data: pd.DataFrame, origin: str, as_of: date):
    """Keep the original windows, cycle years, date positions and chart order."""
    charts = []
    selected = data[(data.origin == origin) & (data.business_date <= as_of)]
    for spec in PM_SEASONAL_CHART_SPECS:
        grouped = {}
        for row in selected[selected.shipment_month == spec.shipment_month].to_dict("records"):
            day = row["business_date"]
            if day.month not in spec.window_months:
                continue
            value = (number(row["retained_net_margin"]) if "retained_net_margin" in row
                     else calculate(*(row[name] for name in FIELDS))["net_margin"])
            year = day.year + (spec.cross_year and day.month >= 5)
            code = row.get("soymeal_contract_code")
            code = code.lstrip("Mm") if isinstance(code, str) else "—"
            grouped.setdefault(year, []).append(Point(day, year, spec.shipment_month, code,
                value, spec.window_months.index(day.month)*31+day.day-1,
                history_source=row["chart_history_source"] if row.get("chart_history_source") in {"manual_cnf", "public_current"} else "legacy"))
        # Keep source gaps here; the renderer applies the bounded connection policy.
        series = tuple(Series(year,tuple(sorted(points,key=lambda p:p.trade_date)))
                       for year,points in sorted(grouped.items())
                       if any(p.net_crush_margin_cny_per_tonne is not None for p in points))
        charts.append(PmSeasonalChart(spec, series))
    return tuple(charts)
