"""Deterministic automatic-to-effective soybean contract selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from .config import ContractOverrideRule, SoybeanImportProfitConfig
from .contract_mapping import map_soybean_contracts
from .models import (
    BusinessKey,
    CbotContract,
    ContractMappingError,
    DceContract,
    MappedContracts,
    MissingReason,
)


class ContractSelectionMode(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL_OVERRIDE = "manual_override"
    LEGACY_UNKNOWN = "legacy_unknown"


@dataclass(frozen=True, slots=True)
class ContractLegSelection:
    automatic_contract: CbotContract | DceContract
    override_contract: CbotContract | DceContract | None
    effective_contract: CbotContract | DceContract
    selection_mode: ContractSelectionMode
    reason: str | None
    effective_from_business_date: date | None
    effective_to_business_date: date | None


@dataclass(frozen=True, slots=True)
class SoybeanContractSelection:
    automatic: MappedContracts
    cbot: ContractLegSelection
    soymeal: ContractLegSelection
    soyoil: ContractLegSelection
    contract_override_hash: str

    @property
    def all_automatic(self) -> bool:
        return all(
            leg.selection_mode is ContractSelectionMode.AUTOMATIC
            for leg in (self.cbot, self.soymeal, self.soyoil)
        )


def select_soybean_contracts(
    config: SoybeanImportProfitConfig,
    business_key: BusinessKey,
) -> SoybeanContractSelection:
    if not isinstance(config, SoybeanImportProfitConfig):
        raise ContractMappingError(
            "config must be a SoybeanImportProfitConfig",
            MissingReason.INVALID_CONTRACT_MAPPING,
        )
    if not isinstance(business_key, BusinessKey):
        raise ContractMappingError(
            "business_key must be a BusinessKey",
            MissingReason.INVALID_CONTRACT_MAPPING,
        )
    automatic = map_soybean_contracts(
        config,
        business_key.shipment_year,
        business_key.shipment_month,
    )
    matching = (
        tuple(
            rule
            for rule in config.contract_override.rules
            if _matches(rule, business_key)
        )
        if config.contract_override.enabled
        else ()
    )
    return SoybeanContractSelection(
        automatic=automatic,
        cbot=_select_leg(automatic.cbot, matching, "cbot"),
        soymeal=_select_leg(automatic.soymeal, matching, "soymeal"),
        soyoil=_select_leg(automatic.soyoil, matching, "soyoil"),
        contract_override_hash=config.contract_override_hash,
    )


def _matches(rule: ContractOverrideRule, key: BusinessKey) -> bool:
    return (
        rule.origin == key.origin
        and rule.shipment_year == key.shipment_year
        and rule.shipment_month == key.shipment_month
        and rule.effective_from_business_date <= key.business_date
        and (
            rule.effective_to_business_date is None
            or key.business_date <= rule.effective_to_business_date
        )
    )


def _select_leg(
    automatic: CbotContract | DceContract,
    matching: tuple[ContractOverrideRule, ...],
    leg: str,
) -> ContractLegSelection:
    rules = tuple(
        rule
        for rule in matching
        if getattr(rule, f"{leg}_contract") is not None
    )
    if len(rules) > 1:
        raise ContractMappingError(
            f"multiple contract override rules match {leg}",
            MissingReason.INVALID_CONTRACT_MAPPING,
        )
    if not rules:
        return ContractLegSelection(
            automatic_contract=automatic,
            override_contract=None,
            effective_contract=automatic,
            selection_mode=ContractSelectionMode.AUTOMATIC,
            reason=None,
            effective_from_business_date=None,
            effective_to_business_date=None,
        )
    rule = rules[0]
    override = getattr(rule, f"{leg}_contract")
    assert override is not None
    return ContractLegSelection(
        automatic_contract=automatic,
        override_contract=override,
        effective_contract=override,
        selection_mode=ContractSelectionMode.MANUAL_OVERRIDE,
        reason=rule.reason,
        effective_from_business_date=rule.effective_from_business_date,
        effective_to_business_date=rule.effective_to_business_date,
    )
