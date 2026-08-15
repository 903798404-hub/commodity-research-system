"""Season alignment and historical comparison rules for materialized spreads."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


FIVE_YEAR_MEAN_LABEL = "五年均值"
FIVE_YEAR_MEAN_SMOOTH_WINDOW = 7
NON_SEASON_KEYWORDS = [
    "历史均值",
    "十年均值",
    "五年均值",
    "均值",
    "mean",
    "avg",
    "average",
    "five_year",
    "五年",
    "十年",
]
FORBIDDEN_MEAN_TRACE_KEYWORDS = [
    "历史均值",
    "十年均值",
    "全部历史",
    "mean",
    "avg",
    "average",
    "five_year",
]


@dataclass(frozen=True, slots=True)
class HistoryView:
    data: pd.DataFrame
    mean_source_data: pd.DataFrame
    seasons: tuple[str, ...]
    latest_season: str
    selected_mean_seasons: tuple[str, ...]
    mean_curve: pd.DataFrame


@dataclass(frozen=True, slots=True)
class SpreadHistorySummary:
    spread_name: str
    latest_season: str
    latest_date: pd.Timestamp
    latest_value: float
    five_year_mean: float | None
    difference_from_mean: float | None
    historical_percentile: float | None


def season_sort_key(season: str) -> int:
    try:
        return int(str(season).split("/")[0])
    except (TypeError, ValueError):
        return -1


def is_non_season_name(name: object) -> bool:
    lowered = str(name).lower()
    return any(keyword.lower() in lowered for keyword in NON_SEASON_KEYWORDS)


def is_forbidden_mean_trace_name(name: object) -> bool:
    lowered = str(name).lower()
    return any(keyword.lower() in lowered for keyword in FORBIDDEN_MEAN_TRACE_KEYWORDS)


def five_year_mean_seasons(history_seasons: list[str]) -> list[str]:
    return history_seasons[-5:]


def _without_pseudo_seasons(data: pd.DataFrame) -> pd.DataFrame:
    return data[
        ~data["season"].astype(str).str.contains(
            "|".join(NON_SEASON_KEYWORDS), case=False, na=False
        )
    ].copy()


def prepare_history_view(
    data: pd.DataFrame,
    mean_source_data: pd.DataFrame | None = None,
) -> HistoryView:
    plotted = _without_pseudo_seasons(data)
    source = plotted.copy() if mean_source_data is None else _without_pseudo_seasons(mean_source_data)
    seasons = sorted(plotted["season"].unique().tolist(), key=season_sort_key)
    source_seasons = sorted(source["season"].unique().tolist(), key=season_sort_key)
    latest_season = source_seasons[-1] if source_seasons else (seasons[-1] if seasons else "")
    history_seasons = [season for season in source_seasons if season != latest_season]
    selected = five_year_mean_seasons(history_seasons)
    mean_curve = pd.DataFrame(
        columns=["calendar_offset", "raw_five_year_mean", "month_day", "smooth_five_year_mean"]
    )
    if len(source_seasons) >= 2:
        history = source[source["season"].isin(selected)].copy()
        mean_curve = (
            history.groupby("calendar_offset", as_index=False)
            .agg(raw_five_year_mean=("plot_value", "mean"), month_day=("month_day", "first"))
            .sort_values("calendar_offset")
        )
        if not mean_curve.empty:
            mean_curve["smooth_five_year_mean"] = (
                mean_curve["raw_five_year_mean"]
                .rolling(window=FIVE_YEAR_MEAN_SMOOTH_WINDOW, center=True, min_periods=2)
                .mean()
            )
    return HistoryView(
        plotted,
        source,
        tuple(seasons),
        latest_season,
        tuple(selected),
        mean_curve,
    )


def latest_plot_value(data: pd.DataFrame) -> object:
    seasons = sorted(data["season"].unique().tolist(), key=season_sort_key)
    if not seasons:
        return pd.NA
    latest_data = data[data["season"] == seasons[-1]].sort_values("calendar_offset")
    latest_values = latest_data["plot_value"].dropna()
    return latest_values.iloc[-1] if not latest_values.empty else pd.NA


def latest_summary(
    data: pd.DataFrame,
    mean_source_data: pd.DataFrame | None = None,
) -> SpreadHistorySummary | None:
    seasons = sorted(data["season"].unique().tolist(), key=season_sort_key)
    if not seasons:
        return None
    latest_season = seasons[-1]
    latest_season_data = data[data["season"] == latest_season].sort_values("date")
    latest_row = latest_season_data.iloc[-1]

    source = data if mean_source_data is None else mean_source_data
    source_seasons = sorted(source["season"].unique().tolist(), key=season_sort_key)
    source_latest_season = source_seasons[-1] if source_seasons else latest_season
    history_seasons = [season for season in source_seasons if season != source_latest_season]
    selected = five_year_mean_seasons(history_seasons)
    history_same_offset = source[
        (source["season"].isin(selected))
        & (source["calendar_offset"] == latest_row["calendar_offset"])
    ]["plot_value"].dropna()
    mean = float(history_same_offset.mean()) if not history_same_offset.empty else None
    latest_value = float(latest_row["plot_value"])
    difference = latest_value - mean if mean is not None else None
    percentile = (
        float((history_same_offset <= latest_value).mean())
        if not history_same_offset.empty
        else None
    )
    return SpreadHistorySummary(
        spread_name=str(latest_row["spread_name"]),
        latest_season=str(latest_season),
        latest_date=pd.Timestamp(latest_row["date"]),
        latest_value=latest_value,
        five_year_mean=mean,
        difference_from_mean=difference,
        historical_percentile=percentile,
    )
