from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "03_src", ROOT / "05_apps"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from agri_research_agent.import_profit.intraday_history import (
    IntradayProfitHistoryError,
    PM_SEASONAL_CHART_SPECS,
    build_pm_seasonal_charts,
    load_unified_seasonal_history,
)
import agri_research_agent.import_profit.intraday_history as history_module
from agri_research_agent.import_profit.intraday_store import (
    ResolvedIntradayProfitRelease,
)
from agri_research_agent.market_data.intraday import MarketSession
from import_profit_intraday_page import (
    LATEST_SEASONAL_YEAR_COLOR,
    _pm_seasonal_figure,
    _seasonal_year_color,
)


def _row(
    trade_date: date,
    shipment_month: int,
    *,
    margin: float | None = 100.0,
    origin: str = "brazil",
) -> dict[str, object]:
    shipment_year = (
        trade_date.year + 1
        if shipment_month <= 5 and trade_date.month >= 5
        else trade_date.year
    )
    return {
        "business_date": trade_date.isoformat(),
        "session": "PM",
        "origin": origin,
        "shipment_year": shipment_year,
        "shipment_month": shipment_month,
        "shipment_period": f"{shipment_year:04d}-{shipment_month:02d}",
        "soymeal_contract": "M2701",
        "soyoil_contract": "Y2701",
        "net_crush_margin_cny_per_tonne": margin,
        "availability_status": "SUCCESS",
        "calculation_status": "success" if margin is not None else "missing",
    }


def _release(
    trade_date: date,
    rows: tuple[dict[str, object], ...],
    *,
    session: MarketSession = MarketSession.PM,
) -> ResolvedIntradayProfitRelease:
    return ResolvedIntradayProfitRelease(
        business_date=trade_date,
        session=session,
        release_id=f"{trade_date.isoformat()}-{session.value}",
        market_snapshot_release_id=f"{trade_date.isoformat()}-{session.value}",
        market_snapshot_sha256="a" * 64,
        market_captured_at=datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            15,
            30,
            tzinfo=timezone.utc,
        ),
        cnf_identity="b" * 64,
        calculated_at=datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            15,
            31,
            tzinfo=timezone.utc,
        ),
        content_sha256="c" * 64,
        rows=rows,
    )


def _chart(charts, month: int):
    return next(item for item in charts if item.spec.shipment_month == month)


def _write_legacy(path: Path, rows: list[dict[str, object]]) -> None:
    schema = pa.schema(
        [
            pa.field("business_date", pa.date32(), nullable=False),
            pa.field("origin", pa.string(), nullable=False),
            pa.field("shipment_year", pa.int16(), nullable=False),
            pa.field("shipment_month", pa.int8(), nullable=False),
            pa.field("shipment_period", pa.string(), nullable=False),
            pa.field("net_crush_margin_cny_per_tonne", pa.float64()),
            pa.field("calculation_status", pa.string(), nullable=False),
        ]
    )
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def _legacy_row(
    trade_date: date,
    shipment_month: int,
    *,
    margin: float = 100.0,
) -> dict[str, object]:
    shipment_year = (
        trade_date.year + 1
        if shipment_month <= 5 and trade_date.month >= 5
        else trade_date.year
    )
    return {
        "business_date": trade_date,
        "origin": "brazil",
        "shipment_year": shipment_year,
        "shipment_month": shipment_month,
        "shipment_period": f"{shipment_year:04d}-{shipment_month:02d}",
        "net_crush_margin_cny_per_tonne": margin,
        "calculation_status": "success",
    }


def test_fixed_twelve_chart_contract_and_windows() -> None:
    assert [item.shipment_month for item in PM_SEASONAL_CHART_SPECS] == [
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12
    ]
    assert all(
        item.window_months == (5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3, 4)
        and item.cross_year
        for item in PM_SEASONAL_CHART_SPECS[:5]
    )
    assert all(
        item.window_months == tuple(range(1, 10))
        and not item.cross_year
        for item in PM_SEASONAL_CHART_SPECS[5:9]
    )
    assert all(
        item.window_months == tuple(range(4, 13))
        and not item.cross_year
        for item in PM_SEASONAL_CHART_SPECS[9:]
    )


def test_same_year_windows_keep_only_real_pm_dates() -> None:
    releases = tuple(
        _release(day, tuple(_row(day, month) for month in (9, 10, 11, 12)))
        for day in (
            date(2026, 3, 31),
            date(2026, 4, 1),
            date(2026, 8, 31),
            date(2026, 9, 1),
            date(2026, 12, 31),
        )
    )
    charts = build_pm_seasonal_charts(releases, origin="brazil")
    september_dates = {
        point.trade_date
        for series in _chart(charts, 9).series
        for point in series.points
    }
    assert september_dates == {
        date(2026, 3, 31),
        date(2026, 4, 1),
        date(2026, 8, 31),
        date(2026, 9, 1),
    }
    for month in (10, 11, 12):
        assert {
            point.trade_date
            for series in _chart(charts, month).series
            for point in series.points
        } == {
            date(2026, 4, 1),
            date(2026, 8, 31),
            date(2026, 9, 1),
            date(2026, 12, 31),
        }


def test_cross_year_series_year_and_sort_order() -> None:
    days = (
        date(2026, 8, 31),
        date(2027, 1, 3),
        date(2027, 4, 30),
        date(2027, 5, 1),
    )
    charts = build_pm_seasonal_charts(
        tuple(_release(day, (_row(day, 1),)) for day in days),
        origin="brazil",
    )
    january = _chart(charts, 1)
    assert [series.series_year for series in january.series] == [2027, 2028]
    assert [point.trade_date for point in january.series[0].points] == [
        date(2026, 8, 31),
        date(2027, 1, 3),
        date(2027, 4, 30),
    ]
    assert january.series[1].points[0].trade_date == date(2027, 5, 1)
    assert january.series[0].points[0].series_year == 2027
    assert january.series[0].points[1].series_year == 2027
    assert january.series[0].points[2].series_year == 2027
    assert january.series[1].points[0].series_year == 2028
    assert tuple(january.spec.window_months) == (
        5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3, 4
    )
    assert [point.season_position for point in january.series[0].points] == sorted(
        point.season_position for point in january.series[0].points
    )


def test_all_three_window_families_keep_daily_points_without_fill() -> None:
    days = (
        date(2026, 1, 2),
        date(2026, 4, 30),
        date(2026, 5, 1),
        date(2026, 8, 31),
        date(2026, 9, 1),
        date(2026, 10, 1),
        date(2027, 1, 15),
        date(2027, 4, 30),
        date(2027, 5, 1),
    )
    charts = build_pm_seasonal_charts(
        tuple(
            _release(
                day,
                tuple(_row(day, month) for month in range(1, 13)),
            )
            for day in days
        ),
        origin="brazil",
    )
    assert len(charts) == 12
    assert [chart.spec.shipment_month for chart in charts] == list(range(1, 13))
    for month in range(1, 6):
        chart = _chart(charts, month)
        assert chart.spec.window_months == (
            5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3, 4
        )
        series = {item.series_year: item for item in chart.series}
        assert [point.trade_date for point in series[2027].points] == [
            date(2026, 5, 1),
            date(2026, 8, 31),
            date(2026, 9, 1),
            date(2026, 10, 1),
            date(2027, 1, 15),
            date(2027, 4, 30),
        ]
        assert series[2028].points[0].trade_date == date(2027, 5, 1)
    for month in range(6, 10):
        chart = _chart(charts, month)
        assert chart.spec.window_months == tuple(range(1, 10))
        assert {
            point.trade_date
            for series in chart.series
            for point in series.points
            if point.trade_date.year == 2026
        } == {
            date(2026, 1, 2),
            date(2026, 4, 30),
            date(2026, 5, 1),
            date(2026, 8, 31),
            date(2026, 9, 1),
        }
    for month in range(10, 13):
        chart = _chart(charts, month)
        assert chart.spec.window_months == tuple(range(4, 13))
        assert {
            point.trade_date
            for series in chart.series
            for point in series.points
            if point.trade_date.year == 2026
        } == {
            date(2026, 4, 30),
            date(2026, 5, 1),
            date(2026, 8, 31),
            date(2026, 9, 1),
            date(2026, 10, 1),
        }


def test_missing_values_create_no_fake_points_and_years_are_dynamic() -> None:
    charts = build_pm_seasonal_charts(
        (
            _release(date(2024, 8, 30), (_row(date(2024, 8, 30), 9),)),
            _release(date(2026, 8, 31), (_row(date(2026, 8, 31), 9),)),
            _release(
                date(2026, 8, 30),
                (_row(date(2026, 8, 30), 9, margin=None),),
            ),
        ),
        origin="brazil",
    )
    september = _chart(charts, 9)
    assert [series.series_year for series in september.series] == [2024, 2026]
    assert september.point_count == 2
    assert all(
        point.net_crush_margin_cny_per_tonne != 0
        for series in september.series
        for point in series.points
    )


def test_am_is_rejected_and_duplicate_pm_dates_fail_closed() -> None:
    day = date(2026, 8, 31)
    am_row = _row(day, 9)
    am_row["session"] = "AM"
    with pytest.raises(IntradayProfitHistoryError, match="PM releases only"):
        build_pm_seasonal_charts(
            (_release(day, (am_row,), session=MarketSession.AM),),
            origin="brazil",
        )
    with pytest.raises(IntradayProfitHistoryError, match="duplicate"):
        build_pm_seasonal_charts(
            (_release(day, (_row(day, 9), _row(day, 9))),),
            origin="brazil",
        )


def test_latest_pm_date_reaches_chart_payload_and_tooltip() -> None:
    day = date(2026, 8, 31)
    chart = _chart(
        build_pm_seasonal_charts(
            (_release(day, (_row(day, 2, margin=-123.45),)),),
            origin="brazil",
        ),
        2,
    )
    assert chart.series[0].series_year == 2027
    point = chart.series[0].points[0]
    assert point.trade_date == day
    assert point.domestic_contract == "2701"
    figure = _pm_seasonal_figure(chart)
    assert list(figure.data[0].customdata)[0][0] == "2026-08-31"
    assert list(figure.data[0].customdata)[0][1] == 2027
    assert "真实日期" in figure.data[0].hovertemplate
    assert "周期年份" in figure.data[0].hovertemplate
    assert "船期" in figure.data[0].hovertemplate
    assert "国内合约" in figure.data[0].hovertemplate
    assert "盘面榨利" in figure.data[0].hovertemplate
    assert "数据口径" in figure.data[0].hovertemplate
    assert "元/吨" in figure.data[0].hovertemplate
    assert figure.layout.yaxis.rangemode == "tozero"
    assert figure.layout.yaxis.zeroline is True
    assert list(figure.layout.xaxis.ticktext) == [
        "5月", "6月", "7月", "8月", "9月", "10月",
        "11月", "12月", "1月", "2月", "3月", "4月",
    ]


def test_latest_year_is_dynamic_red_and_history_colors_are_stable() -> None:
    charts = build_pm_seasonal_charts(
        (
            _release(
                date(2025, 8, 31),
                (_row(date(2025, 8, 31), 2), _row(date(2025, 8, 31), 10)),
            ),
            _release(
                date(2026, 8, 31),
                (_row(date(2026, 8, 31), 2), _row(date(2026, 8, 31), 10)),
            ),
        ),
        origin="brazil",
    )
    february = _pm_seasonal_figure(_chart(charts, 2))
    october = _pm_seasonal_figure(_chart(charts, 10))
    assert february.data[-1].name == "2027"
    assert february.data[-1].line.color == LATEST_SEASONAL_YEAR_COLOR
    assert february.data[-1].line.width == 4.0
    assert october.data[-1].name == "2026"
    assert october.data[-1].line.color == LATEST_SEASONAL_YEAR_COLOR
    assert october.data[-1].line.width == 4.0
    assert february.data[0].line.color == _seasonal_year_color(2026)
    assert october.data[0].line.color == _seasonal_year_color(2025)
    assert _seasonal_year_color(2025) == _seasonal_year_color(2025)
    assert len({_seasonal_year_color(year) for year in range(2017, 2028)}) == 11


def test_unified_history_uses_all_legacy_when_pm_store_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.parquet"
    _write_legacy(
        source,
        [
            _legacy_row(date(2020, 4, 1), 10, margin=10.0),
            _legacy_row(date(2025, 4, 1), 10, margin=20.0),
        ],
    )
    monkeypatch.setattr(
        history_module, "list_intraday_profit_batches", lambda *_: ()
    )
    history = load_unified_seasonal_history(
        source,
        legacy_source_sha256="D" * 64,
        result_root=tmp_path,
        origin="brazil",
    )
    assert history.audit.pm_cutover_date is None
    assert history.audit.legacy_point_count == 2
    assert history.audit.pm_point_count == 0
    assert history.audit.merged_point_count == 2
    october = _chart(history.charts, 10)
    assert [series.series_year for series in october.series] == [2020, 2025]
    assert all(
        point.history_source == "legacy"
        for series in october.series
        for point in series.points
    )


def test_unified_history_cutover_keeps_one_continuous_year_and_pm_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.parquet"
    cutover = date(2026, 8, 31)
    _write_legacy(
        source,
        [
            _legacy_row(date(2026, 4, 1), 10, margin=10.0),
            _legacy_row(date(2026, 6, 1), 10, margin=20.0),
            _legacy_row(cutover, 10, margin=30.0),
            _legacy_row(date(2026, 5, 1), 2, margin=40.0),
            _legacy_row(cutover, 2, margin=50.0),
        ],
    )
    pm_release = _release(
        cutover,
        (
            _row(cutover, 10, margin=300.0),
            _row(cutover, 2, margin=500.0),
        ),
    )
    monkeypatch.setattr(
        history_module,
        "list_intraday_profit_batches",
        lambda *_: (pm_release,),
    )
    history = load_unified_seasonal_history(
        source,
        legacy_source_sha256="E" * 64,
        result_root=tmp_path,
        origin="brazil",
    )
    assert history.audit.pm_cutover_date == cutover
    assert history.audit.legacy_overlap_removed == 2
    assert history.audit.legacy_post_cutover_excluded == 2
    assert history.audit.merged_point_count == 5
    october = _chart(history.charts, 10)
    assert [series.series_year for series in october.series] == [2026]
    assert [point.history_source for point in october.series[0].points] == [
        "legacy", "legacy", "pm"
    ]
    assert october.series[0].points[-1].net_crush_margin_cny_per_tonne == 300.0
    february = _chart(history.charts, 2)
    assert [series.series_year for series in february.series] == [2027]
    assert [point.history_source for point in february.series[0].points] == [
        "legacy", "pm"
    ]
    assert february.series[0].points[-1].trade_date == cutover
