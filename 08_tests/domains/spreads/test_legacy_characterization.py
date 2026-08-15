from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
APPS_DIR = PROJECT_ROOT / "05_apps"
if str(APPS_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_DIR))


def legacy_app():
    return importlib.import_module("streamlit_app")


def test_legacy_name_parsing_display_classification_and_sorting() -> None:
    app = legacy_app()

    assert app.parse_spread_name("M 9-1") == (["M"], [9, 1])
    assert app.parse_spread_name("OI-Y 5") == (["OI", "Y"], [5])
    assert app.parse_spread_name("M09-M01") is None
    assert app.display_spread_name("M 9-1") == "豆粕 09-01"
    assert app.display_spread_name("OI-Y 5") == "豆油-菜油 05"
    assert app.classify_board("M 9-1") == ("豆系月差", "月差")
    assert app.classify_board("P 1-5") == ("棕榈油与菜系月差", "月差")
    assert app.classify_board("OI-Y 5") == ("品种间套利", "油脂之间套利")
    assert app.classify_board("M-RM 9") == ("品种间套利", "粕之间套利")
    assert app.classify_board("Y-M 9") == ("排除", "油粕跨类")
    assert app.classify_board("M/RM 9") == ("排除", "比值或利润")
    assert app.classify_board("unknown") == ("品种间套利", "其他")

    data = pd.DataFrame(
        {"spread_name": ["M 5-9", "Y 9-1", "M 9-1", "OI-Y 5", "unknown"]}
    )
    config = pd.DataFrame(
        {"spread_name": ["unknown", "M 5-9", "Y 9-1", "M 9-1", "OI-Y 5"]}
    )
    assert app.configured_spreads(data, config) == {
        "豆系月差": ["Y 9-1", "M 9-1", "M 5-9"],
        "棕榈油与菜系月差": [],
        "品种间套利": ["OI-Y 5", "unknown"],
    }


def test_legacy_season_sorting_and_pseudo_season_rules() -> None:
    app = legacy_app()

    seasons = ["2025/2026", "mean", "2023/2024", "avg", "2024/2025"]
    assert sorted(seasons, key=app.season_sort_key) == [
        "mean",
        "avg",
        "2023/2024",
        "2024/2025",
        "2025/2026",
    ]
    assert app.five_year_mean_seasons([str(year) for year in range(2017, 2025)]) == [
        "2020",
        "2021",
        "2022",
        "2023",
        "2024",
    ]
    for name in ("mean", "avg", "五年均值", "历史均值", "ten-year average"):
        assert app.is_non_season_name(name)
    assert not app.is_non_season_name("2025/2026")


def test_legacy_difference_ratio_zero_denominator_and_missing_values() -> None:
    app = legacy_app()
    source = pd.DataFrame(
        {
            "leg1_price": [10.0, float("nan"), 5.0, 7.0],
            "leg2_price": [2.0, 2.0, 0.0, float("nan")],
            "spread_value": [8.0, 4.0, 5.0, float("nan")],
        }
    )

    difference = app.add_plot_value(source, "绝对价差 A-B")
    assert difference["plot_value"].tolist() == [8.0, 4.0, 5.0]
    assert difference["value_label"].unique().tolist() == ["绝对价差 A-B"]

    ratio = app.add_plot_value(source, "商品比值 A/B")
    assert ratio.index.tolist() == [0]
    assert ratio["plot_value"].tolist() == [5.0]
    assert ratio["value_label"].unique().tolist() == ["商品比值 A/B"]


def _history_frame() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    seasons = [f"{year}/{year + 1}" for year in range(2019, 2026)]
    for season_index, season in enumerate(seasons):
        for offset in range(9):
            rows.append(
                {
                    "date": pd.Timestamp(f"{2019 + season_index}-01-01")
                    + pd.Timedelta(days=offset),
                    "spread_name": "M 9-1",
                    "season": season,
                    "calendar_offset": offset,
                    "month_day": f"01-{offset + 1:02d}",
                    "plot_value": float(season_index * 10 + offset),
                    "value_label": "绝对价差 A-B",
                }
            )
    rows.append(
        {
            "date": pd.Timestamp("2025-01-01"),
            "spread_name": "M 9-1",
            "season": "五年均值",
            "calendar_offset": 0,
            "month_day": "01-01",
            "plot_value": 9999.0,
            "value_label": "绝对价差 A-B",
        }
    )
    return pd.DataFrame(rows)


def test_legacy_figure_freezes_seasons_latest_trace_and_centered_rolling_mean() -> None:
    app = legacy_app()
    data = _history_frame()
    figure = app.build_figure(data, "豆粕 09-01｜最新 68元/吨")

    assert [trace.name for trace in figure.data] == [
        "2019/2020",
        "2020/2021",
        "2021/2022",
        "2022/2023",
        "2023/2024",
        "2024/2025",
        "2025/2026（当前年度）",
        "五年均值",
    ]
    assert list(figure.data[-1].x) == list(range(9))
    assert list(figure.data[-1].y) == pytest.approx(
        [31.5, 32.0, 32.5, 33.0, 34.0, 35.0, 35.5, 36.0, 36.5]
    )
    assert figure.layout.title.text == "<b>豆粕 09-01｜最新 68元/吨</b>"
    assert figure.layout.legend.title.text == "年度"
    assert figure.layout.yaxis.title.text == "绝对价差 A-B"


def test_legacy_latest_summary_uses_five_recent_history_values_and_lte_percentile() -> None:
    app = legacy_app()
    history = _history_frame()
    latest_season = "2025/2026"
    latest_offset = 8
    history.loc[
        (history["season"] == latest_season)
        & (history["calendar_offset"] == latest_offset),
        "plot_value",
    ] = 38.0

    summary = app.latest_metrics(history)
    assert summary.to_dict(orient="records") == [
        {
            "价差": "M 9-1",
            "最新 season": latest_season,
            "最新日期": "2025-01-09",
            "最新值": "38",
            "五年均值": "38",
            "较五年均值": "0",
            "历史分位数": 0.6,
        }
    ]
    assert app.latest_plot_value(history) == 38.0
