"""Twelve shipment-month seasonal charts using the soybean cycles and style."""
from dataclasses import dataclass
from datetime import date

from agri_research_agent.soybean_margin.charts import (
    PM_SEASONAL_CHART_SPECS, PmSeasonalChartSpec, PmSeasonalChart,
    Point, Series, _pm_seasonal_figure,
)
from .history import LABELS
from .model import number


@dataclass(frozen=True)
class CommoditySeasonalSpec(PmSeasonalChartSpec):
    commodity: str

    @property
    def title(self):
        metric = "盘面榨利" if self.commodity == "canola" else "进口利润"
        return f"{self.shipment_month}月船期 · {metric}"


def build_seasonal_charts(rows, commodity, as_of, *, source="excel"):
    if commodity not in LABELS or source not in {"excel", "local"}:
        raise ValueError("季节图品种或来源无效")
    grouped = {month: {} for month in range(1, 13)}
    for row in rows:
        stamp = date.fromisoformat(row["business_date"])
        month = row["shipment_month"]
        if type(month) is not int or not 1 <= month <= 12:
            raise ValueError("季节图船期无效")
        spec = PM_SEASONAL_CHART_SPECS[month-1]
        if stamp > as_of or stamp.month not in spec.window_months:
            continue
        margin = number(row.get("net_margin"))
        if source == "excel" and row.get("profit_quality") != "verified":
            margin = None
        year = stamp.year + int(spec.cross_year and stamp.month >= spec.window_months[0])
        contract = "原表交割连续，合约年份未知" if source == "excel" else " / ".join(row["domestic_contracts"])
        point = Point(stamp, year, month, contract, margin,
            spec.window_months.index(stamp.month)*31+stamp.day-1,
            history_source="legacy" if source == "excel" else "public_current")
        grouped[month].setdefault(year, []).append(point)
    charts = []
    for original in PM_SEASONAL_CHART_SPECS:
        spec = CommoditySeasonalSpec(original.shipment_month, "—", original.window_months,
                                     original.cross_year, commodity)
        series = tuple(Series(year, tuple(sorted(points, key=lambda p: p.trade_date)))
            for year, points in sorted(grouped[spec.shipment_month].items())
            if any(p.net_crush_margin_cny_per_tonne is not None for p in points))
        charts.append(PmSeasonalChart(spec, series))
    return tuple(charts)


def seasonal_figure(chart, years):
    figure = _pm_seasonal_figure(chart, years)
    metric = "盘面榨利" if chart.spec.commodity == "canola" else "进口利润"
    figure.update_yaxes(title=f"{metric}（元/吨）")
    for trace in figure.data:
        trace.customdata = [
            (*item[:5], "Excel原表历史" if item[5] == "历史盘面榨利" else "本地录价与当日保存行情")
            if item else None for item in trace.customdata
        ]
        trace.hovertemplate = trace.hovertemplate.replace("国内合约：", "盘面口径：").replace("盘面榨利：", f"{metric}：")
    if not figure.data:
        figure.layout.annotations[0].text = f"暂无{metric}历史数据"
    return figure
