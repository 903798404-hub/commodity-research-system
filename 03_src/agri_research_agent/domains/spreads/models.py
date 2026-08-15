"""Immutable models for two-leg commodity spreads."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from agri_research_agent.market_data.contracts import (
    ContinuousInstrumentId,
    ContractId,
    InstrumentId,
)


class SpreadCalculation(StrEnum):
    DIFFERENCE = "difference"
    RATIO = "ratio"


class SpreadStatus(StrEnum):
    SUCCESS = "success"
    MISSING_LEG1 = "missing_leg1"
    MISSING_LEG2 = "missing_leg2"
    MISSING_BOTH_LEGS = "missing_both_legs"
    ZERO_DENOMINATOR = "zero_denominator"
    INVALID_VALUE = "invalid_value"


@dataclass(frozen=True, slots=True)
class SpreadLeg:
    instrument: InstrumentId

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, (ContractId, ContinuousInstrumentId)):
            raise TypeError("instrument must be an InstrumentId")


@dataclass(frozen=True, slots=True)
class SpreadDefinition:
    name: str
    leg1: SpreadLeg
    leg2: SpreadLeg
    calculation: SpreadCalculation = SpreadCalculation.DIFFERENCE

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            raise ValueError("spread name must be non-empty")
        if not isinstance(self.calculation, SpreadCalculation):
            raise TypeError("calculation must be a SpreadCalculation")
        if self.leg1.instrument == self.leg2.instrument:
            raise ValueError("spread legs must identify different contracts")
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class SpreadResult:
    definition: SpreadDefinition
    value: float | None
    status: SpreadStatus
    leg1_value: float | None
    leg2_value: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, SpreadStatus):
            raise TypeError("status must be a SpreadStatus")
        if self.status is SpreadStatus.SUCCESS:
            if self.value is None or not math.isfinite(self.value):
                raise ValueError("successful spread result must have a finite value")
        elif self.value is not None:
            raise ValueError("unavailable spread result cannot carry a value")
