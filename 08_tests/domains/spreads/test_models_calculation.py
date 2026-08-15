from __future__ import annotations

import math

import pandas as pd
import pytest

from agri_research_agent.domains.spreads.calculation import add_plot_value, calculate_spread
from agri_research_agent.domains.spreads.models import (
    SpreadCalculation,
    SpreadDefinition,
    SpreadLeg,
    SpreadStatus,
)
from agri_research_agent.market_data.contracts import ContractId, Exchange


def definition(calculation: SpreadCalculation) -> SpreadDefinition:
    return SpreadDefinition(
        name="M 9-1",
        leg1=SpreadLeg(ContractId(Exchange.DCE, "M", 2025, 9)),
        leg2=SpreadLeg(ContractId(Exchange.DCE, "M", 2026, 1)),
        calculation=calculation,
    )


def test_definition_uses_standard_instrument_identity_and_rejects_same_contract() -> None:
    spread = definition(SpreadCalculation.DIFFERENCE)
    assert str(spread.leg1.instrument) == "DCE:M:2025-09"
    assert str(spread.leg2.instrument) == "DCE:M:2026-01"

    same = SpreadLeg(ContractId(Exchange.DCE, "M", 2025, 9))
    with pytest.raises(ValueError, match="different contracts"):
        SpreadDefinition("invalid", same, same)


def test_difference_is_leg1_minus_leg2_and_ratio_is_leg1_divided_by_leg2() -> None:
    difference = calculate_spread(definition(SpreadCalculation.DIFFERENCE), 3580.0, 3500.0)
    assert difference.status is SpreadStatus.SUCCESS
    assert difference.value == 80.0

    ratio = calculate_spread(definition(SpreadCalculation.RATIO), 7500.0, 6000.0)
    assert ratio.status is SpreadStatus.SUCCESS
    assert ratio.value == 1.25


@pytest.mark.parametrize(
    ("leg1", "leg2", "status"),
    [
        (None, 2.0, SpreadStatus.MISSING_LEG1),
        (2.0, None, SpreadStatus.MISSING_LEG2),
        (None, None, SpreadStatus.MISSING_BOTH_LEGS),
        (float("nan"), 2.0, SpreadStatus.MISSING_LEG1),
        (2.0, float("inf"), SpreadStatus.MISSING_LEG2),
    ],
)
def test_missing_or_nonfinite_leg_returns_explicit_status(
    leg1: float | None,
    leg2: float | None,
    status: SpreadStatus,
) -> None:
    result = calculate_spread(definition(SpreadCalculation.DIFFERENCE), leg1, leg2)
    assert result.status is status
    assert result.value is None


def test_zero_denominator_is_unavailable_in_ratio_but_valid_in_difference() -> None:
    ratio = calculate_spread(definition(SpreadCalculation.RATIO), 10.0, 0.0)
    assert ratio.status is SpreadStatus.ZERO_DENOMINATOR
    assert ratio.value is None

    difference = calculate_spread(definition(SpreadCalculation.DIFFERENCE), 10.0, 0.0)
    assert difference.status is SpreadStatus.SUCCESS
    assert difference.value == 10.0


def test_legacy_adapter_preserves_materialized_difference_and_ratio_nan_behavior() -> None:
    source = pd.DataFrame(
        {
            "leg1_price": [12.0, 8.0, math.nan],
            "leg2_price": [3.0, 0.0, 2.0],
            "spread_value": [9.0, 8.0, math.nan],
        }
    )
    difference = add_plot_value(source, "绝对价差 A-B")
    assert difference["plot_value"].tolist() == [9.0, 8.0]
    ratio = add_plot_value(source, "商品比值 A/B")
    assert ratio["plot_value"].tolist() == [4.0]
