from datetime import date

import pytest

from agri_research_agent.commodity_import_margin.charts import build_seasonal_charts, seasonal_figure
from agri_research_agent.soybean_margin.charts import LATEST_SEASONAL_YEAR_COLOR


def row(stamp, value, month=1, quality="verified"):
    return dict(business_date=stamp, shipment_month=month, net_margin=value,
                profit_quality=quality, domestic_contracts=["DCE:P:2027-01"])


def test_cross_year_alignment_keeps_real_dates_zeroes_and_all_twelve_months():
    rows=[row("2024-05-10",10),row("2024-12-05",20),row("2025-01-04",0),
          row("2025-05-10",30),row("2026-01-04",40),row("2026-10-01",50,11)]
    charts=build_seasonal_charts(rows,"canola",date(2026,10,10))
    assert [c.spec.shipment_month for c in charts]==list(range(1,13))
    assert [s.series_year for s in charts[0].series]==[2025,2026]
    assert [p.trade_date.isoformat() for p in charts[0].series[0].points]==["2024-05-10","2024-12-05","2025-01-04"]
    assert [p.season_position for p in charts[0].series[0].points]==[9,7*31+4,8*31+3]
    assert charts[0].series[0].points[-1].net_crush_margin_cny_per_tonne==0
    assert charts[0].series[0].points[0].season_position==charts[0].series[1].points[0].season_position
    assert charts[10].series[0].series_year==2026
    figure=seasonal_figure(charts[0],[2025,2026])
    assert figure.data[-1].line.color==LATEST_SEASONAL_YEAR_COLOR
    assert figure.layout.xaxis.ticktext==("5月","6月","7月","8月","9月","10月","11月","12月","1月","2月","3月","4月")
    assert figure.data[0].customdata[-1][0]=="2025-01-04"
    assert figure.data[0].customdata[-1][5]=="Excel原表历史"
    assert "大豆" not in figure.layout.title.text


def test_unverified_missing_future_and_long_gaps_do_not_form_fake_series():
    rows=[row("2024-01-01",0,6),row("2024-01-10",10,6),row("2024-01-12",None,6,"missing"),
          row("2024-01-21",20,6),row("2024-01-22",999,6,"mismatch"),
          row("2024-10-01",888,6),row("2025-01-01",777,6)]
    chart=build_seasonal_charts(rows,"palm",date(2024,12,31))[5]
    figure=seasonal_figure(chart,[2024])
    assert list(figure.data[0].y)==[0,10,None,20]
    assert not figure.data[0].connectgaps
    assert figure.layout.yaxis.title.text=="进口利润（元/吨）"
    assert "进口利润：" in figure.data[0].hovertemplate
    assert "合约年份未知" in figure.data[0].customdata[0][3]


def test_leap_day_positions_and_local_profit_are_retained():
    chart=build_seasonal_charts([row("2024-02-29",123),row("2024-03-01",124)],
                               "palm",date(2024,3,1),source="local")[0]
    assert [p.season_position for p in chart.series[0].points]==[9*31+28,10*31]
    figure=seasonal_figure(chart,[2024])
    assert list(figure.data[0].y)==[123,124]
    assert figure.data[0].customdata[0][3]=="DCE:P:2027-01"
    assert figure.data[0].customdata[0][5]=="本地录价与当日保存行情"
    empty=seasonal_figure(chart,[2023])
    assert not empty.data and "暂无进口利润历史数据" in empty.layout.annotations[0].text


@pytest.mark.parametrize("month,inside,outside",[(6,"2024-09-30","2024-10-01"),
                                                (10,"2024-04-01","2024-03-31")])
def test_non_cross_year_cycles_follow_soybean_windows(month,inside,outside):
    chart=build_seasonal_charts([row(inside,1,month),row(outside,2,month)],"palm",date(2024,12,31))[month-1]
    assert len(chart.series)==1 and len(chart.series[0].points)==1
    assert chart.series[0].points[0].trade_date.isoformat()==inside
