"""Pure FX tenor and same-day curve selection functions."""

from __future__ import annotations

from datetime import date

from .config import FxPolicy
from .models import (
    FxCurve,
    FxSelection,
    FxSelectionStatus,
    FxTenorError,
    MissingReason,
)


def calculate_tenor_months(
    business_date: date,
    shipment_year: int,
    shipment_month: int,
    policy: FxPolicy,
) -> int:
    if type(business_date) is not date:
        raise FxTenorError("business_date must be a real date", MissingReason.FX_TENOR_OUT_OF_RANGE)
    if isinstance(shipment_year, bool) or not isinstance(shipment_year, int):
        raise FxTenorError("shipment_year must be an integer", MissingReason.FX_TENOR_OUT_OF_RANGE)
    if isinstance(shipment_month, bool) or not isinstance(shipment_month, int) or not 1 <= shipment_month <= 12:
        raise FxTenorError("shipment_month must be between 1 and 12", MissingReason.FX_TENOR_OUT_OF_RANGE)
    tenor = (shipment_year - business_date.year) * 12 + shipment_month - business_date.month
    if not policy.minimum_tenor_months <= tenor <= policy.maximum_tenor_months:
        raise FxTenorError(
            f"FX tenor {tenor} is outside configured range "
            f"{policy.minimum_tenor_months}..{policy.maximum_tenor_months}",
            MissingReason.FX_TENOR_OUT_OF_RANGE,
        )
    return tenor


def select_fx(
    business_date: date,
    target_tenor: int,
    curve: FxCurve,
    policy: FxPolicy,
) -> FxSelection:
    if isinstance(target_tenor, bool) or not isinstance(target_tenor, int):
        raise FxTenorError("target_tenor must be an integer", MissingReason.FX_TENOR_OUT_OF_RANGE)
    if not policy.minimum_tenor_months <= target_tenor <= policy.maximum_tenor_months:
        raise FxTenorError("target_tenor is outside configured range", MissingReason.FX_TENOR_OUT_OF_RANGE)
    if type(business_date) is not date:
        raise FxTenorError("business_date must be a real date", MissingReason.FX_TENOR_OUT_OF_RANGE)
    if curve.market_date != business_date:
        return FxSelection(
            fx_value=None,
            target_tenor=target_tenor,
            is_interpolated=False,
            lower_tenor=None,
            upper_tenor=None,
            selection_status=FxSelectionStatus.DATE_MISMATCH,
        )

    by_tenor = {point.tenor_months: point for point in curve.points}
    direct = by_tenor.get(target_tenor)
    if direct is not None:
        return FxSelection(
            fx_value=direct.value,
            target_tenor=target_tenor,
            is_interpolated=False,
            lower_tenor=target_tenor,
            upper_tenor=target_tenor,
            selection_status=FxSelectionStatus.DIRECT,
        )

    lower = max((tenor for tenor in by_tenor if tenor < target_tenor), default=None)
    upper = min((tenor for tenor in by_tenor if tenor > target_tenor), default=None)
    if lower is None or upper is None:
        return FxSelection(
            fx_value=None,
            target_tenor=target_tenor,
            is_interpolated=False,
            lower_tenor=lower,
            upper_tenor=upper,
            selection_status=FxSelectionStatus.INTERPOLATION_UNAVAILABLE,
        )

    lower_value = by_tenor[lower].value
    upper_value = by_tenor[upper].value
    interpolated = lower_value + (target_tenor - lower) / (upper - lower) * (upper_value - lower_value)
    return FxSelection(
        fx_value=interpolated,
        target_tenor=target_tenor,
        is_interpolated=True,
        lower_tenor=lower,
        upper_tenor=upper,
        selection_status=FxSelectionStatus.INTERPOLATED,
    )
