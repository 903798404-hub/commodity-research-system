from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from agri_research_agent.import_profit import (
    FxCurve,
    FxCurvePoint,
    FxSelectionStatus,
    calculate_tenor_months,
    load_soybean_config,
    select_fx,
)
from agri_research_agent.import_profit.models import FxTenorError, InvalidPriceError, MissingReason


ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_soybean_config(ROOT / "02_configs" / "import_profit_soybean.yaml")
POLICY = CONFIG.fx_policy
MARKET_DATE = date(2026, 7, 28)


def point(tenor: int, value: float, market_date: date = MARKET_DATE) -> FxCurvePoint:
    return FxCurvePoint(market_date, tenor, value, "美元兑人民币历史汇率")


def curve(*points: FxCurvePoint, market_date: date = MARKET_DATE) -> FxCurve:
    return FxCurve(market_date, tuple(points))


@pytest.mark.parametrize(
    ("business_date", "shipment_year", "shipment_month", "expected"),
    [
        (date(2026, 7, 28), 2026, 7, 0),
        (date(2026, 7, 28), 2026, 8, 1),
        (date(2026, 7, 28), 2027, 1, 6),
        (date(2026, 12, 31), 2027, 1, 1),
    ],
)
def test_integer_month_tenor_ignores_day(
    business_date: date,
    shipment_year: int,
    shipment_month: int,
    expected: int,
) -> None:
    assert calculate_tenor_months(business_date, shipment_year, shipment_month, POLICY) == expected


@pytest.mark.parametrize("tenor", range(13))
def test_spot_forward_months_and_one_year_use_direct_values(tenor: int) -> None:
    selection = select_fx(MARKET_DATE, tenor, curve(point(tenor, 7.1 + tenor / 100)), POLICY)

    assert selection.fx_value == pytest.approx(7.1 + tenor / 100)
    assert selection.target_tenor == tenor
    assert selection.is_interpolated is False
    assert selection.lower_tenor == tenor
    assert selection.upper_tenor == tenor
    assert selection.selection_status is FxSelectionStatus.DIRECT


def test_same_day_nearest_bounds_interpolate_with_non_symmetric_distance() -> None:
    selection = select_fx(
        MARKET_DATE,
        3,
        curve(point(1, 7.10), point(5, 7.50)),
        POLICY,
    )

    assert selection.fx_value == pytest.approx(7.30)
    assert selection.is_interpolated is True
    assert selection.lower_tenor == 1
    assert selection.upper_tenor == 5
    assert selection.selection_status is FxSelectionStatus.INTERPOLATED


def test_real_target_value_has_priority_over_interpolation() -> None:
    selection = select_fx(
        MARKET_DATE,
        3,
        curve(point(1, 7.10), point(3, 7.33), point(5, 7.50)),
        POLICY,
    )
    assert selection.fx_value == pytest.approx(7.33)
    assert selection.is_interpolated is False
    assert selection.selection_status is FxSelectionStatus.DIRECT


def test_cross_date_curve_is_never_used() -> None:
    other_date = date(2026, 7, 27)
    selection = select_fx(
        MARKET_DATE,
        3,
        curve(point(3, 7.33, other_date), market_date=other_date),
        POLICY,
    )
    assert selection.fx_value is None
    assert selection.selection_status is FxSelectionStatus.DATE_MISMATCH
    assert selection.lower_tenor is None
    assert selection.upper_tenor is None


@pytest.mark.parametrize(
    ("target", "points", "expected_lower", "expected_upper"),
    [
        (2, (point(3, 7.3),), None, 3),
        (10, (point(9, 7.3),), 9, None),
    ],
)
def test_missing_one_interpolation_bound_returns_unavailable(
    target: int,
    points: tuple[FxCurvePoint, ...],
    expected_lower: int | None,
    expected_upper: int | None,
) -> None:
    selection = select_fx(MARKET_DATE, target, curve(*points), POLICY)
    assert selection.fx_value is None
    assert selection.is_interpolated is False
    assert selection.lower_tenor == expected_lower
    assert selection.upper_tenor == expected_upper
    assert selection.selection_status is FxSelectionStatus.INTERPOLATION_UNAVAILABLE


@pytest.mark.parametrize("tenor", [-1, 13])
def test_negative_and_above_twelve_tenors_fail(tenor: int) -> None:
    with pytest.raises(FxTenorError) as exc_info:
        select_fx(MARKET_DATE, tenor, curve(point(0, 7.1)), POLICY)
    assert exc_info.value.reason is MissingReason.FX_TENOR_OUT_OF_RANGE


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), float("-inf"), "7.1"])
def test_zero_non_finite_negative_and_string_fx_values_are_invalid(value: object) -> None:
    with pytest.raises(InvalidPriceError):
        point(0, value)  # type: ignore[arg-type]
