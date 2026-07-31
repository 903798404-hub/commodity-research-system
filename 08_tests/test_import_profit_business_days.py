from datetime import date, datetime

import pytest

from agri_research_agent.import_profit.business_days import (
    BusinessDayError,
    NonBusinessWeekdayError,
    is_business_weekday,
    require_business_weekday,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2026, 7, 27), True),
        (date(2026, 7, 31), True),
        (date(2026, 8, 1), False),
        (date(2026, 8, 2), False),
        (date(2026, 10, 1), True),
    ],
)
def test_weekday_rule_uses_only_the_weekday(value: date, expected: bool) -> None:
    assert is_business_weekday(value) is expected


def test_weekday_rule_rejects_datetime_and_weekend_without_fallback() -> None:
    with pytest.raises(BusinessDayError):
        is_business_weekday(datetime(2026, 7, 27, 12, 0))
    with pytest.raises(NonBusinessWeekdayError, match="2026-08-01"):
        require_business_weekday(date(2026, 8, 1))


def test_weekday_rule_has_no_market_data_or_calendar_input() -> None:
    assert is_business_weekday(date(2026, 10, 1))
    assert is_business_weekday(date(2026, 10, 1))
