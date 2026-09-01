"""Soybean-only ACL and calculation over sealed Public Intraday snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Mapping

from agri_research_agent.market_data.contracts import ContractId, Exchange
from agri_research_agent.market_data.intraday import (
    IntradayQuote,
    IntradaySnapshot,
    IntradaySnapshotNotFoundError,
    MarketSession,
    load_intraday_snapshot,
    quote_by_instrument,
)

from .cnf_store import CnfStoreSnapshot, business_key_tuple
from .config import SoybeanImportProfitConfig
from .contract_override import select_soybean_contracts
from .daily_increment import generate_daily_business_keys
from .models import BusinessKey, CalculationStatus, MissingReason
from .soybean import (
    SoybeanCalculationInput,
    SoybeanCalculationOutput,
    calculate_soybean_net_crush_margin,
)


class SoybeanIntradayAclError(RuntimeError):
    status = "ACL_ERROR"


class SoybeanIntradaySnapshotMissingError(SoybeanIntradayAclError):
    status = "SNAPSHOT_MISSING"


class SoybeanIntradayCnfError(SoybeanIntradayAclError):
    status = "CNF_MISSING"


class SoybeanIntradayCnfMismatchError(SoybeanIntradayAclError):
    status = "CNF_MISMATCH"


@dataclass(frozen=True, slots=True)
class SoybeanIntradayMarketInputs:
    business_key: BusinessKey
    session: MarketSession
    captured_at: datetime
    snapshot_release_id: str
    snapshot_content_sha256: str
    cbot_contract_code: str
    soymeal_contract_code: str
    soyoil_contract_code: str
    cbot: IntradayQuote | None
    fx: IntradayQuote
    soymeal: IntradayQuote | None
    soyoil: IntradayQuote | None
    unavailable_contracts: tuple[str, ...]
    mapping_identity: str
    mapping_hash: str
    contract_override_hash: str

    @property
    def provenance(self) -> Mapping[str, object]:
        return {
            "snapshot_release_id": self.snapshot_release_id,
            "snapshot_content_sha256": self.snapshot_content_sha256,
            "captured_at": self.captured_at.isoformat(),
            "business_date": self.business_key.business_date.isoformat(),
            "session": self.session.value,
            "instruments": {
                "cbot": None if self.cbot is None else self.cbot.as_dict(),
                "fx": self.fx.as_dict(),
                "soymeal": None if self.soymeal is None else self.soymeal.as_dict(),
                "soyoil": None if self.soyoil is None else self.soyoil.as_dict(),
            },
            "availability_status": self.availability_status,
            "unavailable_contracts": list(self.unavailable_contracts),
        }

    @property
    def availability_status(self) -> str:
        return "SUCCESS" if not self.unavailable_contracts else "CONTRACT_NOT_AVAILABLE"


@dataclass(frozen=True, slots=True)
class SoybeanIntradayProfitResult:
    business_key: BusinessKey
    session: MarketSession
    market_snapshot_release_id: str
    market_snapshot_sha256: str
    market_captured_at: datetime
    cnf_identity: str
    cnf_cents_per_bushel: float | None
    cnf_source: str | None
    calculation: SoybeanCalculationOutput
    market_inputs: SoybeanIntradayMarketInputs
    calculated_at: datetime

    def __post_init__(self) -> None:
        if (
            self.calculation.calculation_status is CalculationStatus.SUCCESS
            and self.market_inputs.unavailable_contracts
        ):
            raise SoybeanIntradayAclError("successful result cannot have unavailable contracts")
        if (
            self.calculation.calculation_status is CalculationStatus.INCOMPLETE
            and not self.market_inputs.unavailable_contracts
            and MissingReason.MISSING_CNF not in self.calculation.missing_reasons
        ):
            raise SoybeanIntradayAclError(
                "incomplete result needs market or CNF availability evidence"
            )
        if self.calculation.business_key != self.business_key:
            raise SoybeanIntradayAclError("calculation business key mismatch")
        if not self.cnf_identity:
            raise SoybeanIntradayCnfError("CNF identity must be explicit")
        if self.calculated_at.tzinfo is None:
            raise SoybeanIntradayAclError("calculated_at must be timezone-aware")

    @property
    def key(self) -> tuple[date, str, str, str, int, int]:
        key = self.business_key
        return (
            key.business_date,
            self.session.value,
            key.commodity,
            key.origin,
            key.shipment_year,
            key.shipment_month,
        )

    @property
    def availability_status(self) -> str:
        return self.market_inputs.availability_status


def required_intraday_contracts_for_date(
    business_date: date,
    config: SoybeanImportProfitConfig,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Resolve the union of exact effective contracts for all daily shipment keys."""

    cbot: set[str] = set()
    meal: set[str] = set()
    oil: set[str] = set()
    for key in generate_daily_business_keys(business_date, config):
        selection = select_soybean_contracts(config, key)
        cbot_contract = selection.cbot.effective_contract
        cbot.add(f"{cbot_contract.contract_year % 100:02d}{cbot_contract.contract_month:02d}")
        meal.add(selection.soymeal.effective_contract.code)
        oil.add(selection.soyoil.effective_contract.code)
    return tuple(sorted(cbot)), tuple(sorted(meal)), tuple(sorted(oil))


def load_soybean_intraday_market_inputs(
    snapshot_root: str,
    *,
    business_key: BusinessKey,
    session: MarketSession,
    config: SoybeanImportProfitConfig,
) -> SoybeanIntradayMarketInputs:
    """Formal Soybean ACL. It only reads the sealed Public asset, never Tankan."""

    try:
        snapshot = load_intraday_snapshot(snapshot_root, business_key.business_date, session)
    except IntradaySnapshotNotFoundError as exc:
        raise SoybeanIntradaySnapshotMissingError(str(exc)) from None
    return select_soybean_intraday_market_inputs(snapshot, business_key=business_key, config=config)


def select_soybean_intraday_market_inputs(
    snapshot: IntradaySnapshot,
    *,
    business_key: BusinessKey,
    config: SoybeanImportProfitConfig,
) -> SoybeanIntradayMarketInputs:
    if snapshot.business_date != business_key.business_date:
        raise SoybeanIntradayAclError("snapshot and business key dates differ")
    selection = select_soybean_contracts(config, business_key)
    cbot_contract = selection.cbot.effective_contract
    meal_contract = selection.soymeal.effective_contract
    oil_contract = selection.soyoil.effective_contract
    cbot_code = f"{cbot_contract.contract_year % 100:02d}{cbot_contract.contract_month:02d}"
    cbot = _quote_or_unavailable(
        snapshot,
        str(ContractId(Exchange.CBOT, "SOYBEAN", cbot_contract.contract_year, cbot_contract.contract_month)),
        cbot_code,
    )
    try:
        fx = quote_by_instrument(snapshot, "FX:USD/CNH:SPOT")
    except IntradaySnapshotNotFoundError as exc:
        raise SoybeanIntradaySnapshotMissingError(str(exc)) from None
    meal = _quote_or_unavailable(
        snapshot,
        str(ContractId(Exchange.DCE, "SOYMEAL", meal_contract.contract_year, meal_contract.contract_month)),
        meal_contract.code,
    )
    oil = _quote_or_unavailable(
        snapshot,
        str(ContractId(Exchange.DCE, "SOYOIL", oil_contract.contract_year, oil_contract.contract_month)),
        oil_contract.code,
    )
    unavailable_contracts = tuple(
        code
        for code, quote in (
            (cbot_code, cbot),
            (meal_contract.code, meal),
            (oil_contract.code, oil),
        )
        if quote is None
    )
    return SoybeanIntradayMarketInputs(
        business_key=business_key,
        session=snapshot.session,
        captured_at=snapshot.captured_at,
        snapshot_release_id=snapshot.release_id,
        snapshot_content_sha256=snapshot.content_sha256,
        cbot_contract_code=cbot_code,
        soymeal_contract_code=meal_contract.code,
        soyoil_contract_code=oil_contract.code,
        cbot=cbot,
        fx=fx,
        soymeal=meal,
        soyoil=oil,
        unavailable_contracts=unavailable_contracts,
        mapping_identity=config.contract_mapping_identity,
        mapping_hash=config.contract_mapping_hash,
        contract_override_hash=config.contract_override_hash,
    )


def calculate_soybean_intraday_profit(
    market_inputs: SoybeanIntradayMarketInputs,
    *,
    cnf_store: CnfStoreSnapshot,
    config: SoybeanImportProfitConfig,
    calculated_at: datetime,
) -> SoybeanIntradayProfitResult:
    if not cnf_store.store_exists or not cnf_store.store_sha256:
        raise SoybeanIntradayCnfError("CNF store identity is unavailable")
    by_key = {business_key_tuple(record.business_key): record for record in cnf_store.records}
    cnf = by_key.get(business_key_tuple(market_inputs.business_key))
    selection = select_soybean_contracts(config, market_inputs.business_key)
    calculation_input = SoybeanCalculationInput(
        business_key=market_inputs.business_key,
        cnf_cents_per_bushel=(
            None if cnf is None else cnf.cnf_cents_per_bushel
        ),
        cbot_contract=selection.cbot.effective_contract,
        cbot_daily_price_cents_per_bushel=(
            None if market_inputs.cbot is None else market_inputs.cbot.price
        ),
        fx_value=market_inputs.fx.price,
        soymeal_contract=selection.soymeal.effective_contract,
        soymeal_price_cny_per_tonne=(
            None if market_inputs.soymeal is None else market_inputs.soymeal.price
        ),
        soyoil_contract=selection.soyoil.effective_contract,
        soyoil_price_cny_per_tonne=(
            None if market_inputs.soyoil is None else market_inputs.soyoil.price
        ),
        resolved_parameters=config.resolve_parameters(market_inputs.business_key.origin),
        mapping_identity=config.contract_mapping_identity,
        mapping_hash=config.contract_mapping_hash,
        contract_override_hash=config.contract_override_hash,
    )
    calculation = calculate_soybean_net_crush_margin(calculation_input, config)
    return SoybeanIntradayProfitResult(
        business_key=market_inputs.business_key,
        session=market_inputs.session,
        market_snapshot_release_id=market_inputs.snapshot_release_id,
        market_snapshot_sha256=market_inputs.snapshot_content_sha256,
        market_captured_at=market_inputs.captured_at,
        cnf_identity=cnf_store.store_sha256,
        cnf_cents_per_bushel=(
            None
            if cnf is None or cnf.cnf_cents_per_bushel is None
            else float(cnf.cnf_cents_per_bushel)
        ),
        cnf_source=None if cnf is None else cnf.source,
        calculation=calculation,
        market_inputs=market_inputs,
        calculated_at=calculated_at.astimezone(timezone.utc),
    )


def _quote_or_unavailable(
    snapshot: IntradaySnapshot,
    instrument_id: str,
    contract_code: str,
) -> IntradayQuote | None:
    try:
        return quote_by_instrument(snapshot, instrument_id, contract_code)
    except IntradaySnapshotNotFoundError as exc:
        evidence = [
            item
            for item in snapshot.unavailable_instruments
            if item.key == (instrument_id, contract_code)
        ]
        if len(evidence) == 1:
            return None
        raise SoybeanIntradaySnapshotMissingError(str(exc)) from None


def require_shared_cnf_identity(results: Iterable[SoybeanIntradayProfitResult]) -> str:
    identities = {result.cnf_identity for result in results}
    if len(identities) != 1:
        raise SoybeanIntradayCnfMismatchError("AM and PM results do not share one CNF identity")
    return next(iter(identities))


__all__ = [
    "SoybeanIntradayAclError", "SoybeanIntradayCnfError",
    "SoybeanIntradayCnfMismatchError", "SoybeanIntradayMarketInputs",
    "SoybeanIntradayProfitResult", "SoybeanIntradaySnapshotMissingError",
    "calculate_soybean_intraday_profit", "load_soybean_intraday_market_inputs",
    "require_shared_cnf_identity", "required_intraday_contracts_for_date",
    "select_soybean_intraday_market_inputs",
]
