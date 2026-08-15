"""Pure two-leg calculations and the legacy materialized-data adapter."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from .models import (
    SpreadCalculation,
    SpreadDefinition,
    SpreadResult,
    SpreadStatus,
)


LEGACY_DIFFERENCE_LABEL = "绝对价差 A-B"
LEGACY_RATIO_LABEL = "商品比值 A/B"


def _finite_or_none(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def calculate_spread(
    definition: SpreadDefinition,
    leg1_value: float | None,
    leg2_value: float | None,
) -> SpreadResult:
    """Calculate A-B or A/B without fetching quotes or reading storage."""

    leg1 = _finite_or_none(leg1_value)
    leg2 = _finite_or_none(leg2_value)
    if leg1 is None and leg2 is None:
        status = SpreadStatus.MISSING_BOTH_LEGS
    elif leg1 is None:
        status = SpreadStatus.MISSING_LEG1
    elif leg2 is None:
        status = SpreadStatus.MISSING_LEG2
    elif definition.calculation is SpreadCalculation.RATIO and leg2 == 0:
        status = SpreadStatus.ZERO_DENOMINATOR
    else:
        value = (
            leg1 - leg2
            if definition.calculation is SpreadCalculation.DIFFERENCE
            else leg1 / leg2
        )
        if math.isfinite(value):
            return SpreadResult(definition, value, SpreadStatus.SUCCESS, leg1, leg2)
        status = SpreadStatus.INVALID_VALUE
    return SpreadResult(definition, None, status, leg1, leg2)


def calculation_from_legacy_label(method: str) -> SpreadCalculation:
    return (
        SpreadCalculation.RATIO
        if method == LEGACY_RATIO_LABEL
        else SpreadCalculation.DIFFERENCE
    )


def add_plot_value(data: pd.DataFrame, method: str) -> pd.DataFrame:
    """Adapt the already-materialized legacy database without opening a path.

    Difference mode deliberately consumes ``spread_value`` because that column is
    the existing authoritative page input. Ratio mode preserves the dashboard's
    A/B and zero-denominator behavior.
    """

    plotted = data.copy()
    calculation = calculation_from_legacy_label(method)
    if calculation is SpreadCalculation.RATIO:
        plotted["plot_value"] = plotted["leg1_price"] / plotted["leg2_price"]
        plotted.loc[plotted["leg2_price"] == 0, "plot_value"] = pd.NA
        plotted["value_label"] = LEGACY_RATIO_LABEL
    else:
        plotted["plot_value"] = plotted["spread_value"]
        plotted["value_label"] = LEGACY_DIFFERENCE_LABEL
    return plotted.dropna(subset=["plot_value"])
