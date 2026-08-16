"""Fail-closed, side-effect-free readiness for Soybean Import Crush inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable

from .contracts import ContractId, Exchange
from .soybean_shadow import CbotSoybeanShadowQuote, FxSpotShadowQuote


class SoybeanReadinessError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExistingInputReadiness:
    """Read-only status supplied by an existing bounded-context source."""

    name: str
    status: str
    ready: bool

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.status.strip():
            raise SoybeanReadinessError("existing input readiness fields must be non-empty")
        if type(self.ready) is not bool:
            raise SoybeanReadinessError("existing input ready must be boolean")


@dataclass(frozen=True, slots=True)
class CbotInputReadiness:
    latest_source_date: date | None
    required_contracts: tuple[str, ...]
    available_contracts: tuple[str, ...]
    missing_contracts: tuple[str, ...]
    ready: bool


@dataclass(frozen=True, slots=True)
class FxSpotInputReadiness:
    latest_source_date: date | None
    ready: bool


@dataclass(frozen=True, slots=True)
class SoybeanMarketInputReadiness:
    target_business_date: date
    cbot: CbotInputReadiness
    fx_spot: FxSpotInputReadiness
    dce: ExistingInputReadiness
    fx_forward: ExistingInputReadiness
    cnf: ExistingInputReadiness
    overall_ready: bool
    blocking_reasons: tuple[str, ...]
    captured_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "target_business_date": self.target_business_date.isoformat(),
            "cbot": {
                "latest_source_date": (
                    self.cbot.latest_source_date.isoformat()
                    if self.cbot.latest_source_date
                    else None
                ),
                "required_contracts": list(self.cbot.required_contracts),
                "available_contracts": list(self.cbot.available_contracts),
                "missing_contracts": list(self.cbot.missing_contracts),
                "ready": self.cbot.ready,
            },
            "fx_spot": {
                "latest_source_date": (
                    self.fx_spot.latest_source_date.isoformat()
                    if self.fx_spot.latest_source_date
                    else None
                ),
                "ready": self.fx_spot.ready,
            },
            "dce": _existing_dict(self.dce),
            "fx_forward": _existing_dict(self.fx_forward),
            "cnf": _existing_dict(self.cnf),
            "overall_ready": self.overall_ready,
            "blocking_reasons": list(self.blocking_reasons),
            "captured_at": self.captured_at.astimezone(timezone.utc).isoformat(),
        }


def evaluate_soybean_market_input_readiness(
    *,
    target_business_date: date,
    required_cbot_contracts: Iterable[ContractId],
    cbot_quotes: Iterable[CbotSoybeanShadowQuote],
    fx_spot_quotes: Iterable[FxSpotShadowQuote],
    existing_dce_status: ExistingInputReadiness,
    existing_forward_fx_status: ExistingInputReadiness,
    cnf_status: ExistingInputReadiness,
    captured_at: datetime,
) -> SoybeanMarketInputReadiness:
    """Evaluate exact-date readiness without filling or fallback.

    Supplying the same immutable inputs and ``captured_at`` returns an equal
    report, so a 09:00 check and later retry do not mutate any source state.
    """

    if type(target_business_date) is not date:
        raise SoybeanReadinessError("target_business_date must be an exact date")
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise SoybeanReadinessError("captured_at must be timezone-aware")
    required = tuple(required_cbot_contracts)
    if not required:
        raise SoybeanReadinessError("required_cbot_contracts cannot be empty")
    if len(set(required)) != len(required):
        raise SoybeanReadinessError("required CBOT contracts are duplicated")
    for contract in required:
        if contract.exchange is not Exchange.CBOT or contract.product != "SOYBEAN":
            raise SoybeanReadinessError("readiness requires exact CBOT Soybean contracts")

    cbot_rows = tuple(cbot_quotes)
    cbot_latest = max((quote.business_date for quote in cbot_rows), default=None)
    exact_cbot: dict[ContractId, CbotSoybeanShadowQuote] = {}
    for quote in cbot_rows:
        if quote.business_date != target_business_date or not quote.is_usable:
            continue
        if quote.contract in exact_cbot:
            raise SoybeanReadinessError("usable CBOT shadow key is duplicated")
        exact_cbot[quote.contract] = quote
    ordered = tuple(sorted(required, key=str))
    available = tuple(str(contract) for contract in ordered if contract in exact_cbot)
    missing = tuple(str(contract) for contract in ordered if contract not in exact_cbot)
    cbot_status = CbotInputReadiness(
        latest_source_date=cbot_latest,
        required_contracts=tuple(str(contract) for contract in ordered),
        available_contracts=available,
        missing_contracts=missing,
        ready=not missing,
    )

    fx_rows = tuple(fx_spot_quotes)
    fx_latest = max((quote.quote_date for quote in fx_rows), default=None)
    exact_fx = [
        quote
        for quote in fx_rows
        if quote.quote_date == target_business_date and quote.is_usable
    ]
    if len(exact_fx) > 1:
        raise SoybeanReadinessError("usable FX Spot shadow key is duplicated")
    fx_status = FxSpotInputReadiness(
        latest_source_date=fx_latest,
        ready=len(exact_fx) == 1,
    )

    blockers = [f"cbot_missing_contract:{contract}" for contract in missing]
    if not fx_status.ready:
        blockers.append("fx_spot_not_ready")
    for label, status in (
        ("dce", existing_dce_status),
        ("fx_forward", existing_forward_fx_status),
        ("cnf", cnf_status),
    ):
        if not status.ready:
            blockers.append(f"{label}_not_ready:{status.status}")
    return SoybeanMarketInputReadiness(
        target_business_date=target_business_date,
        cbot=cbot_status,
        fx_spot=fx_status,
        dce=existing_dce_status,
        fx_forward=existing_forward_fx_status,
        cnf=cnf_status,
        overall_ready=not blockers,
        blocking_reasons=tuple(blockers),
        captured_at=captured_at.astimezone(timezone.utc),
    )


def _existing_dict(value: ExistingInputReadiness) -> dict[str, object]:
    return {"name": value.name, "status": value.status, "ready": value.ready}


__all__ = [
    "CbotInputReadiness",
    "ExistingInputReadiness",
    "FxSpotInputReadiness",
    "SoybeanMarketInputReadiness",
    "SoybeanReadinessError",
    "evaluate_soybean_market_input_readiness",
]
