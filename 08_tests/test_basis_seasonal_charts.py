from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPS_DIR = PROJECT_ROOT / "05_apps"
if str(APPS_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_DIR))

from basis_page import (  # noqa: E402
    _commodity_sort_key,
    build_seasonal_report_figure,
    calculate_wholesale_spread,
    filter_display_data,
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
    assert axis.get_ylabel() == "元/吨"
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
