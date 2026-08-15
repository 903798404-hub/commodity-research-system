"""Explicit calendar and night-session policy contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from .contracts import Exchange


@dataclass(frozen=True, slots=True)
class TradingDayDecision:
    exchange: Exchange
    business_date: date
    is_trading_day: bool
    policy_name: str
    source_identity: str


@dataclass(frozen=True, slots=True)
class BusinessDayDecision:
    business_date: date
    is_business_day: bool
    policy_name: str
    source_identity: str


@dataclass(frozen=True, slots=True)
class NightSessionTradeDateDecision:
    exchange: Exchange
    target_business_date: date
    capture_window_valid: bool
    calendar_valid: bool
    observation_date_proven: bool
    accepted: bool
    policy_name: str
    source_identity: str
    reason: str


@runtime_checkable
class ExchangeTradingCalendar(Protocol):
    def decide(self, exchange: Exchange, business_date: date) -> TradingDayDecision: ...


@runtime_checkable
class CalendarBusinessDayPolicy(Protocol):
    def decide(self, business_date: date) -> BusinessDayDecision: ...


@runtime_checkable
class NightSessionTradeDatePolicy(Protocol):
    def decide(
        self,
        exchange: Exchange,
        target_business_date: date,
        *,
        capture_window_valid: bool,
        calendar_valid: bool,
        observation_date_proven: bool,
    ) -> NightSessionTradeDateDecision: ...


@dataclass(frozen=True, slots=True)
class DeterministicExchangeTradingCalendar:
    trading_days: frozenset[date]
    policy_name: str = "deterministic-exchange-calendar-v1"
    source_identity: str = "fixture"

    def decide(self, exchange: Exchange, business_date: date) -> TradingDayDecision:
        return TradingDayDecision(
            exchange=exchange,
            business_date=business_date,
            is_trading_day=business_date in self.trading_days,
            policy_name=self.policy_name,
            source_identity=self.source_identity,
        )


@dataclass(frozen=True, slots=True)
class DeterministicBusinessDayPolicy:
    business_days: frozenset[date]
    policy_name: str = "deterministic-business-day-v1"
    source_identity: str = "fixture"

    def decide(self, business_date: date) -> BusinessDayDecision:
        return BusinessDayDecision(
            business_date=business_date,
            is_business_day=business_date in self.business_days,
            policy_name=self.policy_name,
            source_identity=self.source_identity,
        )


@dataclass(frozen=True, slots=True)
class StrictNightSessionTradeDatePolicy:
    policy_name: str = "strict-night-trade-date-v1"
    source_identity: str = "contract"

    def decide(
        self,
        exchange: Exchange,
        target_business_date: date,
        *,
        capture_window_valid: bool,
        calendar_valid: bool,
        observation_date_proven: bool,
    ) -> NightSessionTradeDateDecision:
        accepted = capture_window_valid and calendar_valid and observation_date_proven
        return NightSessionTradeDateDecision(
            exchange=exchange,
            target_business_date=target_business_date,
            capture_window_valid=capture_window_valid,
            calendar_valid=calendar_valid,
            observation_date_proven=observation_date_proven,
            accepted=accepted,
            policy_name=self.policy_name,
            source_identity=self.source_identity,
            reason="accepted" if accepted else "required evidence is missing",
        )
