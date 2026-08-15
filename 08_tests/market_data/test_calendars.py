from datetime import date

from agri_research_agent.market_data.calendars import (
    CalendarBusinessDayPolicy,
    DeterministicBusinessDayPolicy,
    DeterministicExchangeTradingCalendar,
    ExchangeTradingCalendar,
    NightSessionTradeDatePolicy,
    StrictNightSessionTradeDatePolicy,
)
from agri_research_agent.market_data.contracts import Exchange


DAY = date(2026, 8, 14)


def test_deterministic_calendars_satisfy_explicit_protocols() -> None:
    exchange_calendar = DeterministicExchangeTradingCalendar(frozenset({DAY}), source_identity="fixture:exchange")
    business_policy = DeterministicBusinessDayPolicy(frozenset({DAY}), source_identity="fixture:business")
    assert isinstance(exchange_calendar, ExchangeTradingCalendar)
    assert isinstance(business_policy, CalendarBusinessDayPolicy)
    assert exchange_calendar.decide(Exchange.DCE, DAY).is_trading_day is True
    assert business_policy.decide(DAY).is_business_day is True


def test_night_decision_records_each_piece_of_evidence() -> None:
    policy = StrictNightSessionTradeDatePolicy(source_identity="fixture:night")
    assert isinstance(policy, NightSessionTradeDatePolicy)
    accepted = policy.decide(
        Exchange.DCE,
        DAY,
        capture_window_valid=True,
        calendar_valid=True,
        observation_date_proven=True,
    )
    rejected = policy.decide(
        Exchange.DCE,
        DAY,
        capture_window_valid=True,
        calendar_valid=True,
        observation_date_proven=False,
    )
    assert accepted.accepted is True
    assert accepted.policy_name and accepted.source_identity
    assert rejected.accepted is False
    assert rejected.observation_date_proven is False
