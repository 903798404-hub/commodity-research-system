"""Contract mapping driven exclusively by the validated YAML configuration."""

from __future__ import annotations

from .config import SoybeanImportProfitConfig
from .models import CbotContract, ContractMappingError, DceContract, MappedContracts, MissingReason


def map_soybean_contracts(
    config: SoybeanImportProfitConfig,
    shipment_year: int,
    shipment_month: int,
) -> MappedContracts:
    if isinstance(shipment_year, bool) or not isinstance(shipment_year, int):
        raise ContractMappingError("shipment_year must be an integer", MissingReason.INVALID_CONTRACT_MAPPING)
    if isinstance(shipment_month, bool) or not isinstance(shipment_month, int) or not 1 <= shipment_month <= 12:
        raise ContractMappingError(
            "shipment_month must be between 1 and 12",
            MissingReason.INVALID_CONTRACT_MAPPING,
        )
    rules = {rule.shipment_month: rule for rule in config.contract_mapping}
    try:
        rule = rules[shipment_month]
    except KeyError as exc:
        raise ContractMappingError(
            f"no contract mapping for shipment month {shipment_month}",
            MissingReason.INVALID_CONTRACT_MAPPING,
        ) from exc

    cbot_year = shipment_year + rule.cbot.year_offset
    dce_year = shipment_year + rule.dce.year_offset
    return MappedContracts(
        cbot=CbotContract(cbot_year, rule.cbot.contract_month),
        soymeal=DceContract.soymeal(dce_year, rule.dce.contract_month),
        soyoil=DceContract.soyoil(dce_year, rule.dce.contract_month),
        mapping_identity=config.contract_mapping_identity,
        mapping_hash=config.contract_mapping_hash,
    )
