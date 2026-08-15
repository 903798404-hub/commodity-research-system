from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.domains.spreads.calculation import add_plot_value
from agri_research_agent.domains.spreads.history import prepare_history_view
from agri_research_agent.domains.spreads.parsing import (
    classify_board,
    configured_spreads,
    definition_from_legacy,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
APPS_DIR = PROJECT_ROOT / "05_apps"
if str(APPS_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_DIR))


def reference_root() -> Path:
    value = os.environ.get("SPREAD_REFERENCE_DATA_ROOT", "")
    if not value:
        pytest.skip("SPREAD_REFERENCE_DATA_ROOT is required for formal-readonly comparison")
    root = Path(value).resolve(strict=True)
    if root == PROJECT_ROOT.resolve():
        pytest.fail("reference data must be outside the feature worktree")
    return root


def spread_app():
    module_name = "spread_domain_migration_streamlit_app"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, APPS_DIR / "streamlit_app.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def legacy_season_sort_key(season: str) -> int:
    try:
        return int(str(season).split("/")[0])
    except (TypeError, ValueError):
        return -1


def legacy_parse(spread_name: str) -> tuple[list[str], list[int]] | None:
    labels = {"M", "Y", "RM", "OI", "P"}
    name, _, delivery = str(spread_name).partition(" ")
    instruments = name.split("-")
    if not delivery:
        return None
    try:
        months = [int(month) for month in delivery.split("-")]
    except ValueError:
        return None
    if not instruments or not all(instrument in labels for instrument in instruments):
        return None
    return instruments, months


def legacy_classify(spread_name: str) -> tuple[str, str]:
    name = str(spread_name)
    if "/" in name or any(keyword in name.lower() for keyword in ["压榨", "crush", "ratio"]):
        return "排除", "比值或利润"
    parsed = legacy_parse(name)
    if parsed is None:
        return "品种间套利", "其他"
    instruments, months = parsed
    if len(instruments) == 1 and len(months) == 2:
        if instruments[0] in {"M", "Y"}:
            return "豆系月差", "月差"
        if instruments[0] in {"P", "OI", "RM"}:
            return "棕榈油与菜系月差", "月差"
        return "品种间套利", "其他"
    if len(instruments) == 2 and len(months) == 1:
        instruments_set = set(instruments)
        if instruments_set <= {"Y", "OI", "P"}:
            return "品种间套利", "油脂之间套利"
        if instruments_set <= {"M", "RM"}:
            return "品种间套利", "粕之间套利"
        if instruments_set & {"Y", "OI", "P"} and instruments_set & {"M", "RM"}:
            return "排除", "油粕跨类"
    return "品种间套利", "其他"


def legacy_add_plot_value(data: pd.DataFrame, method: str) -> pd.DataFrame:
    plotted = data.copy()
    if method == "商品比值 A/B":
        plotted["plot_value"] = plotted["leg1_price"] / plotted["leg2_price"]
        plotted.loc[plotted["leg2_price"] == 0, "plot_value"] = pd.NA
        plotted["value_label"] = "商品比值 A/B"
    else:
        plotted["plot_value"] = plotted["spread_value"]
        plotted["value_label"] = "绝对价差 A-B"
    return plotted.dropna(subset=["plot_value"])


def legacy_mean_curve(data: pd.DataFrame) -> tuple[str, list[str], pd.DataFrame]:
    seasons = sorted(data["season"].unique().tolist(), key=legacy_season_sort_key)
    latest = seasons[-1]
    selected = [season for season in seasons if season != latest][-5:]
    history = data[data["season"].isin(selected)].copy()
    curve = (
        history.groupby("calendar_offset", as_index=False)
        .agg(raw_five_year_mean=("plot_value", "mean"), month_day=("month_day", "first"))
        .sort_values("calendar_offset")
    )
    curve["smooth_five_year_mean"] = (
        curve["raw_five_year_mean"].rolling(window=7, center=True, min_periods=2).mean()
    )
    return latest, selected, curve


def legacy_summary(data: pd.DataFrame) -> dict[str, object]:
    seasons = sorted(data["season"].unique().tolist(), key=legacy_season_sort_key)
    latest_season = seasons[-1]
    latest_row = data[data["season"] == latest_season].sort_values("date").iloc[-1]
    history_seasons = [season for season in seasons if season != latest_season][-5:]
    values = data[
        (data["season"].isin(history_seasons))
        & (data["calendar_offset"] == latest_row["calendar_offset"])
    ]["plot_value"].dropna()
    mean = float(values.mean()) if not values.empty else None
    latest = float(latest_row["plot_value"])
    return {
        "spread_name": str(latest_row["spread_name"]),
        "latest_season": latest_season,
        "latest_date": pd.Timestamp(latest_row["date"]),
        "latest_value": latest,
        "five_year_mean": mean,
        "difference_from_mean": latest - mean if mean is not None else None,
        "historical_percentile": float((values <= latest).mean()) if not values.empty else None,
    }


def load_reference_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    root = reference_root()
    data = pd.read_parquet(root / "01_data" / "historical_spread_database.parquet")
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    for column in ("calendar_offset", "spread_value", "leg1_price", "leg2_price"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["date", "calendar_offset", "season"])
    data["season"] = data["season"].astype(str)
    data["spread_name"] = data["spread_name"].astype(str)
    config = pd.read_excel(
        root / "02_configs" / "historical_spread_config.xlsx", sheet_name="spread_config"
    )
    if "enabled" in config.columns:
        config = config[config["enabled"].fillna(False)].copy()
    return data[data["status"] == "success"].copy(), config


def test_real_reference_difference_ratio_classification_and_season_metadata_match() -> None:
    data, config = load_reference_data()
    names = sorted(data["spread_name"].unique().tolist())
    assert names
    assert configured_spreads(data, config)

    for name in names:
        source = data[data["spread_name"] == name].copy()
        assert classify_board(name) == legacy_classify(name)
        for method in ("绝对价差 A-B", "商品比值 A/B"):
            old = legacy_add_plot_value(source, method)
            new = add_plot_value(source, method)
            assert new.index.tolist() == old.index.tolist()
            pd.testing.assert_series_equal(
                new["plot_value"],
                old["plot_value"],
                check_exact=False,
                rtol=0,
                atol=1e-12,
            )

        seasons = sorted(source["season"].unique().tolist(), key=legacy_season_sort_key)
        latest_row = source[source["season"] == seasons[-1]].iloc[0]
        definition = definition_from_legacy(name, seasons[-1])
        assert definition.leg1.instrument.product == str(latest_row["leg1_instrument"])
        assert definition.leg2.instrument.product == str(latest_row["leg2_instrument"])
        assert definition.leg1.instrument.month == int(latest_row["leg1_month"])
        assert definition.leg2.instrument.month == int(latest_row["leg2_month"])


def test_real_reference_five_year_curve_latest_percentile_and_page_summary_match() -> None:
    data, _ = load_reference_data()
    app = spread_app()

    for name in sorted(data["spread_name"].unique().tolist()):
        plotted = add_plot_value(data[data["spread_name"] == name].copy(), "绝对价差 A-B")
        old_latest, old_seasons, old_curve = legacy_mean_curve(plotted)
        view = prepare_history_view(plotted)
        assert view.latest_season == old_latest
        assert list(view.selected_mean_seasons) == old_seasons
        pd.testing.assert_frame_equal(
            view.mean_curve.reset_index(drop=True),
            old_curve.reset_index(drop=True),
            check_exact=False,
            rtol=0,
            atol=1e-12,
        )

        expected = legacy_summary(plotted)
        actual = app.latest_summary(plotted)
        assert actual is not None
        assert {
            "spread_name": actual.spread_name,
            "latest_season": actual.latest_season,
            "latest_date": actual.latest_date,
            "latest_value": actual.latest_value,
            "five_year_mean": actual.five_year_mean,
            "difference_from_mean": actual.difference_from_mean,
            "historical_percentile": actual.historical_percentile,
        } == expected

        page_summary = app.latest_metrics(plotted).iloc[0]
        assert page_summary["价差"] == expected["spread_name"]
        assert page_summary["最新 season"] == expected["latest_season"]
        assert page_summary["最新日期"] == expected["latest_date"].strftime("%Y-%m-%d")
        assert page_summary["最新值"] == app.format_market_number(expected["latest_value"])
        assert page_summary["五年均值"] == app.format_market_number(expected["five_year_mean"])
        assert page_summary["较五年均值"] == app.format_market_number(
            expected["difference_from_mean"]
        )
        assert page_summary["历史分位数"] == expected["historical_percentile"]
