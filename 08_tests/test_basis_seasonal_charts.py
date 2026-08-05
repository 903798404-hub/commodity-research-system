from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPS_DIR = PROJECT_ROOT / "05_apps"
if str(APPS_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_DIR))

from basis_page import (  # noqa: E402
    MAX_MISSING_TRADING_DAYS,
    _card_pairs,
    _commodity_sort_key,
    build_seasonal_report_figure,
    calculate_wholesale_spread,
    filter_display_data,
    missing_trading_days_between,
    prepare_cash_price_data,
    prepare_seasonal_data,
)


def test_seasonal_chart_legend_contains_years_only() -> None:
    data = pd.DataFrame(
        {
            "date": [
                "2024-12-31",
                "2024-01-02",
                "2025-01-02",
                "2026-01-02",
            ],
            "region": ["华东"] * 4,
            "commodity": ["豆粕"] * 4,
            "basis": [100, 80, 90, 110],
            "quote_type": ["基差报价"] * 4,
            "delivery_month": ["现货"] * 4,
        }
    )

    figure = build_seasonal_report_figure(
        data,
        value_col="basis",
        title="华东豆粕（基差）",
        years=[2024, 2025, 2026],
        zero_line=True,
    )

    axis = figure.axes[0]
    assert [line.get_label() for line in axis.get_lines()[:3]] == [
        "2024",
        "2025",
        "2026",
    ]
    assert axis.get_title() == "华东豆粕（基差）"
    assert axis.get_ylabel() == "基差（元/吨）"
    assert [text.get_text() for text in axis.get_legend().get_texts()] == [
        "2024",
        "2025",
        "2026",
    ]
    assert any(
        list(line.get_ydata()) == [0, 0]
        for line in axis.get_lines()
        if len(line.get_ydata()) == 2
    )

    prepared = prepare_seasonal_data(data, "basis")
    year_2024 = prepared[prepared["year"] == 2024]
    assert year_2024["month_day"].tolist() == ["01-02", "12-31"]


def test_basis_chart_renders_chinese_without_missing_glyph_warning() -> None:
    data = pd.DataFrame(
        {
            "date": ["2024-01-02", "2025-01-02", "2026-01-02"],
            "region": ["华东"] * 3,
            "commodity": ["豆油"] * 3,
            "basis": [-100, 0, 100],
            "quote_type": ["基差报价"] * 3,
            "delivery_month": ["现货"] * 3,
        }
    )

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        figure = build_seasonal_report_figure(
            data,
            value_col="basis",
            title="华东豆油（基差）",
            years=[2024, 2025, 2026],
            zero_line=True,
        )
        figure.canvas.draw()

    assert not [
        warning
        for warning in captured
        if "Glyph" in str(warning.message) and "missing from font" in str(warning.message)
    ]
    axis = figure.axes[0]
    assert axis.get_title() == "华东豆油（基差）"
    assert axis.get_ylabel() == "基差（元/吨）"


def test_seasonal_chart_breaks_real_date_gaps_without_interpolation() -> None:
    data = pd.DataFrame(
        {
            "date": ["2026-03-20", "2026-05-25", "2026-05-26"],
            "basis": [230.0, 0.0, 20.0],
        }
    )

    figure = build_seasonal_report_figure(
        data,
        value_col="basis",
        title="华东菜粕（基差）",
        years=[2026],
        zero_line=True,
    )
    plotted = figure.axes[0].get_lines()[0]

    assert MAX_MISSING_TRADING_DAYS == 5
    assert pd.isna(plotted.get_ydata()[1])
    assert [value for value in plotted.get_ydata() if pd.notna(value)] == [
        230.0,
        0.0,
        20.0,
    ]


@pytest.mark.parametrize(
    ("previous", "current", "expected_missing"),
    [
        ("2026-07-31", "2026-08-03", 0),
        ("2025-01-27", "2025-02-05", 0),
        ("2025-09-30", "2025-10-09", 0),
        ("2026-07-01", "2026-07-03", 1),
        ("2026-07-01", "2026-07-09", 5),
        ("2026-07-01", "2026-07-10", 6),
    ],
)
def test_missing_days_use_bundled_futures_calendar(
    previous: str,
    current: str,
    expected_missing: int,
) -> None:
    assert missing_trading_days_between(previous, current) == expected_missing


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        ("2026-07-31", "2026-08-03"),
        ("2025-01-27", "2025-02-05"),
        ("2025-09-30", "2025-10-09"),
        ("2026-07-01", "2026-07-09"),
        ("2026-05-29", "2026-06-01"),
    ],
)
def test_normal_market_closures_and_up_to_five_missing_sessions_connect(
    previous: str,
    current: str,
) -> None:
    data = pd.DataFrame({"date": [previous, current], "basis": [10.0, 20.0]})

    figure = build_seasonal_report_figure(
        data,
        value_col="basis",
        title="交易日连续性",
        years=[pd.Timestamp(previous).year],
        zero_line=True,
    )
    plotted = figure.axes[0].get_lines()[0]

    assert plotted.get_ydata().tolist() == [10.0, 20.0]


@pytest.mark.parametrize(
    ("previous", "current", "expected_missing"),
    [
        ("2026-07-01", "2026-07-10", 6),
        ("2026-01-15", "2026-02-26", 23),
        ("2026-03-20", "2026-05-25", 41),
    ],
)
def test_more_than_five_missing_sessions_break_without_filling_values(
    previous: str,
    current: str,
    expected_missing: int,
) -> None:
    data = pd.DataFrame({"date": [previous, current], "basis": [230.0, 80.0]})

    figure = build_seasonal_report_figure(
        data,
        value_col="basis",
        title="华东菜粕（基差）",
        years=[2026],
        zero_line=True,
    )
    plotted = figure.axes[0].get_lines()[0]
    y_values = plotted.get_ydata()

    assert missing_trading_days_between(previous, current) == expected_missing
    assert pd.isna(y_values[1])
    assert [value for value in y_values if pd.notna(value)] == [230.0, 80.0]


def test_source_cutover_does_not_force_a_break() -> None:
    data = pd.DataFrame(
        {
            "date": ["2026-05-29", "2026-06-01"],
            "basis": [79.0, 80.0],
            "source_sheet": ["菜粕基差", "basis_price:菜粕"],
        }
    )

    figure = build_seasonal_report_figure(
        data,
        value_col="basis",
        title="华东菜粕（基差）",
        years=[2026],
        zero_line=True,
    )

    assert figure.axes[0].get_lines()[0].get_ydata().tolist() == [79.0, 80.0]


def test_seasonal_chart_rejects_duplicate_dates_instead_of_averaging() -> None:
    data = pd.DataFrame(
        {
            "date": ["2026-03-20", "2026-03-20"],
            "basis": [100.0, 200.0],
        }
    )

    with pytest.raises(ValueError, match="同一图表日期存在重复记录"):
        build_seasonal_report_figure(
            data,
            value_col="basis",
            title="华东菜粕（基差）",
            years=[2026],
            zero_line=True,
        )


def test_card_pairs_hide_all_four_excluded_soymeal_regions() -> None:
    data = pd.DataFrame(
        {
            "commodity": [
                "豆粕",
                "豆粕",
                "豆粕",
                "豆粕",
                "豆粕",
                "菜粕",
                "菜粕",
                "菜油",
            ],
            "region": [
                "东北",
                "西南",
                "华中",
                "华北",
                "华东",
                "东北",
                "西南",
                "华北",
            ],
        }
    )

    assert _card_pairs(data) == [
        ("菜油", "华北"),
        ("豆粕", "华东"),
        ("菜粕", "东北"),
        ("菜粕", "西南"),
    ]


def test_card_pairs_limit_oils_to_existing_east_north_south_regions() -> None:
    data = pd.DataFrame(
        {
            "commodity": [
                "豆油",
                "豆油",
                "棕榈油",
                "菜油",
                "菜油",
                "菜粕",
            ],
            "region": ["华东", "西南", "华南", "华北", "东北", "西南"],
        }
    )

    assert _card_pairs(data) == [
        ("豆油", "华东"),
        ("棕榈油", "华南"),
        ("菜油", "华北"),
        ("菜粕", "西南"),
    ]


def test_display_data_keeps_only_spot_delivery_without_changing_database() -> None:
    source = pd.DataFrame(
        {
            "commodity": ["一豆", "一豆", "豆粕"],
            "region": ["华东", "华东", "华东"],
            "delivery_month": ["现货", "6-9月", "现货"],
        }
    )

    displayed = filter_display_data(source)

    assert displayed["delivery_month"].astype(str).unique().tolist() == ["现货"]
    assert len(source) == 3


def test_wholesale_spread_is_cash_price_a_minus_b() -> None:
    data = pd.DataFrame(
        {
            "date": [
                "2024-01-02",
                "2024-01-02",
                "2025-01-02",
                "2025-01-02",
            ],
            "region": ["华东"] * 4,
            "commodity": ["豆粕", "菜粕", "豆粕", "菜粕"],
            "quote_type": ["基差报价"] * 4,
            "delivery_month": ["现货"] * 4,
            "cash_price": [3200, 2500, 3300, 2600],
        }
    )

    spread, missing = calculate_wholesale_spread(
        data,
        region="华东",
        commodity_a="豆粕",
        commodity_b="菜粕",
        quote_type="基差报价",
        delivery_month="现货",
    )

    assert missing == []
    assert spread["spread_value"].tolist() == [700, 700]
    assert spread["commodity"].unique().tolist() == ["豆粕-菜粕"]


def test_wholesale_spread_reports_missing_commodity() -> None:
    data = pd.DataFrame(
        {
            "date": ["2026-01-02"],
            "region": ["华东"],
            "commodity": ["豆粕"],
            "quote_type": ["基差报价"],
            "delivery_month": ["现货"],
            "cash_price": [3200],
        }
    )

    spread, missing = calculate_wholesale_spread(
        data,
        region="华东",
        commodity_a="豆粕",
        commodity_b="菜粕",
        quote_type="基差报价",
        delivery_month="现货",
    )

    assert spread.empty
    assert missing == ["菜粕"]


def test_display_data_maps_supported_codes_and_excludes_other_oils() -> None:
    data = pd.DataFrame(
        {
            "commodity": [
                "一豆",
                "24度",
                "三菜",
                "豆粕",
                "菜粕",
                "一葵",
                "一级玉米油",
                "葵粕",
            ],
            "quote_type": [
                "基差报价",
                "一口价",
                "一口价",
                "一口价",
                "基差报价",
                "一口价",
                "一口价",
                "一口价",
            ],
            "cash_price": [8200, 7800, 9100, 3200, 2500, 9000, 8800, 2300],
            "basis": [pd.NA, pd.NA, pd.NA, 100, -500, pd.NA, pd.NA, pd.NA],
        }
    )

    visible = filter_display_data(data)
    cash = prepare_cash_price_data(visible)

    assert visible["commodity_code"].tolist() == [
        "一豆",
        "24度",
        "三菜",
        "豆粕",
        "菜粕",
    ]
    assert visible["commodity"].tolist() == [
        "豆油",
        "棕榈油",
        "菜油",
        "豆粕",
        "菜粕",
    ]
    assert [_commodity_sort_key(value) for value in visible["commodity"]] == [
        (0, "豆油"),
        (1, "棕榈油"),
        (2, "菜油"),
        (3, "豆粕"),
        (4, "菜粕"),
    ]
    assert set(cash["commodity"]) == {"豆油", "棕榈油", "菜油", "豆粕", "菜粕"}
    assert not {"一葵", "一级玉米油", "葵粕"} & set(visible["commodity_code"])
