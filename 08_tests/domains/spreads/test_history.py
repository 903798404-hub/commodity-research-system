from __future__ import annotations

import pandas as pd
import pytest

from agri_research_agent.domains.spreads.history import (
    five_year_mean_seasons,
    is_non_season_name,
    latest_plot_value,
    latest_summary,
    prepare_history_view,
    season_sort_key,
)


def history_frame() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for season_index, start_year in enumerate(range(2019, 2026)):
        season = f"{start_year}/{start_year + 1}"
        for offset in range(9):
            rows.append(
                {
                    "date": pd.Timestamp(f"{start_year}-01-01") + pd.Timedelta(days=offset),
                    "spread_name": "M 9-1",
                    "season": season,
                    "calendar_offset": offset,
                    "month_day": f"01-{offset + 1:02d}",
                    "plot_value": float(season_index * 10 + offset),
                    "value_label": "绝对价差 A-B",
                }
            )
    return pd.DataFrame(rows)


def test_season_order_sample_selection_and_pseudo_season_exclusion() -> None:
    assert season_sort_key("2025/2026") == 2025
    assert season_sort_key("mean") == -1
    assert five_year_mean_seasons([str(year) for year in range(2017, 2025)]) == [
        "2020",
        "2021",
        "2022",
        "2023",
        "2024",
    ]
    assert is_non_season_name("avg")
    assert is_non_season_name("五年均值")
    assert not is_non_season_name("2025/2026")

    data = history_frame()
    pseudo = data.iloc[[0]].assign(season="mean", plot_value=9999.0)
    view = prepare_history_view(pd.concat([data, pseudo], ignore_index=True))
    assert "mean" not in view.seasons
    assert view.latest_season == "2025/2026"
    assert view.selected_mean_seasons == (
        "2020/2021",
        "2021/2022",
        "2022/2023",
        "2023/2024",
        "2024/2025",
    )


def test_calendar_offset_mean_and_centered_seven_point_rolling_match_legacy() -> None:
    view = prepare_history_view(history_frame())
    assert view.mean_curve["calendar_offset"].tolist() == list(range(9))
    assert view.mean_curve["raw_five_year_mean"].tolist() == pytest.approx(
        [30.0 + offset for offset in range(9)]
    )
    assert view.mean_curve["smooth_five_year_mean"].tolist() == pytest.approx(
        [31.5, 32.0, 32.5, 33.0, 34.0, 35.0, 35.5, 36.0, 36.5]
    )


def test_latest_value_mean_and_lte_historical_percentile_are_independent_of_rolling() -> None:
    data = history_frame()
    latest_mask = (data["season"] == "2025/2026") & (data["calendar_offset"] == 8)
    data.loc[latest_mask, "plot_value"] = 38.0

    assert latest_plot_value(data) == 38.0
    summary = latest_summary(data)
    assert summary is not None
    assert summary.latest_season == "2025/2026"
    assert summary.latest_date == pd.Timestamp("2025-01-09")
    assert summary.latest_value == 38.0
    assert summary.five_year_mean == 38.0
    assert summary.difference_from_mean == 0.0
    assert summary.historical_percentile == 0.6


def test_latest_summary_handles_missing_history_at_latest_offset() -> None:
    data = history_frame()
    latest = data[data["season"] == "2025/2026"].copy()
    summary = latest_summary(latest)
    assert summary is not None
    assert summary.five_year_mean is None
    assert summary.difference_from_mean is None
    assert summary.historical_percentile is None
