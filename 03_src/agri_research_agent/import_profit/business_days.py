"""Thin Beijing-time weekday rules for import-profit orchestration."""

from __future__ import annotations

from datetime import date


class BusinessDayError(ValueError):
    """Base error for the weekday-only business calendar."""


class NonBusinessWeekdayError(BusinessDayError):
    """Raised when a business key falls on Saturday or Sunday."""


def is_business_weekday(value: date) -> bool:
    """Return whether *value* is Monday through Friday.

    This rule deliberately does not consult holiday calendars, exchange
    calendars, market-data availability, the network, or adjacent dates.
    """

    if type(value) is not date:
        raise BusinessDayError("business date must be a real date")
    return value.weekday() < 5


def require_business_weekday(value: date) -> None:
    """Reject weekend dates without applying any fallback."""

    if not is_business_weekday(value):
        raise NonBusinessWeekdayError(
            f"business date {value.isoformat()} is not a Monday-Friday weekday"
        )
