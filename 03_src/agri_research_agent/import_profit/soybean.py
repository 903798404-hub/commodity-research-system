"""Pure soybean screen net crush-margin calculator."""

from __future__ import annotations

from dataclasses import dataclass

from .config import SoybeanImportProfitConfig, SoybeanParameters
from .contract_override import ContractSelectionMode, select_soybean_contracts
from .models import (
    BusinessKey,
    BusinessKeyError,
    CalculationStatus,
    CbotContract,
    ContractMappingError,
    DceContract,
    InvalidParameterError,
    MissingReason,
    require_finite_number,
)
from .parameter_snapshot import build_parameter_snapshot


@dataclass(frozen=True, slots=True)
class SoybeanCalculationInput:
    business_key: BusinessKey
    cnf_cents_per_bushel: float | None
    cbot_contract: CbotContract
    cbot_daily_price_cents_per_bushel: float | None
    fx_value: float | None
    soymeal_contract: DceContract
    soymeal_price_cny_per_tonne: float | None
    soyoil_contract: DceContract
    soyoil_price_cny_per_tonne: float | None
    resolved_parameters: SoybeanParameters
    mapping_identity: str
    mapping_hash: str
    contract_override_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.business_key, BusinessKey):
            raise BusinessKeyError(
                "business_key must be a validated BusinessKey",
                MissingReason.INVALID_BUSINESS_KEY,
            )
        if not isinstance(self.cbot_contract, CbotContract):
            raise ContractMappingError(
                "cbot_contract must be a CbotContract",
                MissingReason.INVALID_CONTRACT_MAPPING,
            )
        if not isinstance(self.soymeal_contract, DceContract) or self.soymeal_contract.symbol != "M":
            raise ContractMappingError(
                "soymeal_contract must be a DCE M contract",
                MissingReason.INVALID_CONTRACT_MAPPING,
            )
        if not isinstance(self.soyoil_contract, DceContract) or self.soyoil_contract.symbol != "Y":
            raise ContractMappingError(
                "soyoil_contract must be a DCE Y contract",
                MissingReason.INVALID_CONTRACT_MAPPING,
            )
        if not isinstance(self.resolved_parameters, SoybeanParameters):
            raise InvalidParameterError(
                "resolved_parameters must be SoybeanParameters",
                MissingReason.INVALID_PARAMETER,
            )
        _validate_optional_number(self.cnf_cents_per_bushel, "cnf_cents_per_bushel", positive=False)
        _validate_optional_number(
            self.cbot_daily_price_cents_per_bushel,
            "cbot_daily_price_cents_per_bushel",
            positive=True,
        )
        _validate_optional_number(self.fx_value, "fx_value", positive=True)
        _validate_optional_number(
            self.soymeal_price_cny_per_tonne,
            "soymeal_price_cny_per_tonne",
            positive=True,
        )
        _validate_optional_number(
            self.soyoil_price_cny_per_tonne,
            "soyoil_price_cny_per_tonne",
            positive=True,
        )
        if not isinstance(self.mapping_identity, str) or not self.mapping_identity:
            raise InvalidParameterError("mapping_identity must be non-empty", MissingReason.INVALID_PARAMETER)
        if not isinstance(self.mapping_hash, str) or len(self.mapping_hash) != 64:
            raise InvalidParameterError("mapping_hash must be SHA-256", MissingReason.INVALID_PARAMETER)
        if self.contract_override_hash is not None and (
            not isinstance(self.contract_override_hash, str)
            or len(self.contract_override_hash) != 64
        ):
            raise InvalidParameterError(
                "contract_override_hash must be SHA-256",
                MissingReason.INVALID_PARAMETER,
            )


@dataclass(frozen=True, slots=True)
class SoybeanCalculationOutput:
    business_key: BusinessKey
    usd_cost_per_tonne: float | None
    duty_paid_cost_cny_per_tonne: float | None
    net_crush_margin_cny_per_tonne: float | None
    calculation_status: CalculationStatus
    missing_reasons: tuple[MissingReason, ...]
    parameter_version: str
    parameter_hash: str
    mapping_identity: str
    mapping_hash: str
    contract_override_hash: str


def calculate_soybean_net_crush_margin(
    calculation_input: SoybeanCalculationInput,
    config: SoybeanImportProfitConfig,
) -> SoybeanCalculationOutput:
    key = calculation_input.business_key
    provenance = build_parameter_snapshot(config)
    assert provenance.parameter_hash is not None
    if key.commodity != config.commodity or key.origin not in config.origin_codes:
        raise ContractMappingError(
            "business key is inconsistent with configuration",
            MissingReason.INVALID_CONTRACT_MAPPING,
        )
    selection = select_soybean_contracts(config, key)
    expected = selection.automatic
    if (
        calculation_input.cbot_contract != selection.cbot.effective_contract
        or calculation_input.soymeal_contract
        != selection.soymeal.effective_contract
        or calculation_input.soyoil_contract
        != selection.soyoil.effective_contract
        or calculation_input.mapping_identity != expected.mapping_identity
        or calculation_input.mapping_hash != expected.mapping_hash
        or (
            calculation_input.contract_override_hash is not None
            and calculation_input.contract_override_hash
            != selection.contract_override_hash
        )
    ):
        raise ContractMappingError(
            "input contracts or provenance do not match configured selection",
            MissingReason.INVALID_CONTRACT_MAPPING,
        )

    expected_parameters = config.resolve_parameters(key.origin)
    if calculation_input.resolved_parameters != expected_parameters:
        raise InvalidParameterError(
            "resolved_parameters do not match configured origin parameters",
            MissingReason.INVALID_PARAMETER,
        )

    missing_reasons = tuple(
        reason
        for value, reason in (
            (calculation_input.cnf_cents_per_bushel, MissingReason.MISSING_CNF),
            (
                calculation_input.cbot_daily_price_cents_per_bushel,
                MissingReason.MISSING_OVERRIDE_CBOT
                if selection.cbot.selection_mode
                is ContractSelectionMode.MANUAL_OVERRIDE
                else MissingReason.MISSING_CBOT,
            ),
            (calculation_input.fx_value, MissingReason.MISSING_FX),
            (
                calculation_input.soymeal_price_cny_per_tonne,
                MissingReason.MISSING_OVERRIDE_SOYMEAL
                if selection.soymeal.selection_mode
                is ContractSelectionMode.MANUAL_OVERRIDE
                else MissingReason.MISSING_SOYMEAL,
            ),
            (
                calculation_input.soyoil_price_cny_per_tonne,
                MissingReason.MISSING_OVERRIDE_SOYOIL
                if selection.soyoil.selection_mode
                is ContractSelectionMode.MANUAL_OVERRIDE
                else MissingReason.MISSING_SOYOIL,
            ),
        )
        if value is None
    )
    if missing_reasons:
        return SoybeanCalculationOutput(
            business_key=key,
            usd_cost_per_tonne=None,
            duty_paid_cost_cny_per_tonne=None,
            net_crush_margin_cny_per_tonne=None,
            calculation_status=CalculationStatus.INCOMPLETE,
            missing_reasons=missing_reasons,
            parameter_version=str(config.schema_version),
            parameter_hash=provenance.parameter_hash,
            mapping_identity=expected.mapping_identity,
            mapping_hash=expected.mapping_hash,
            contract_override_hash=selection.contract_override_hash,
        )

    params = calculation_input.resolved_parameters
    cbot = calculation_input.cbot_daily_price_cents_per_bushel
    cnf = calculation_input.cnf_cents_per_bushel
    fx_value = calculation_input.fx_value
    meal = calculation_input.soymeal_price_cny_per_tonne
    oil = calculation_input.soyoil_price_cny_per_tonne
    assert cbot is not None and cnf is not None and fx_value is not None and meal is not None and oil is not None

    usd_cost = (cbot + cnf) * params.cents_per_bushel_to_usd_per_tonne
    duty_paid_cost = usd_cost * fx_value * (1 + params.tariff_rate) * (1 + params.vat_rate)
    net_margin = (
        meal * params.meal_yield
        + oil * params.oil_yield
        - duty_paid_cost
        - params.port_charge_cny_per_tonne
        - params.processing_fee_cny_per_tonne
        - params.additional_fees_cny_per_tonne
    )
    return SoybeanCalculationOutput(
        business_key=key,
        usd_cost_per_tonne=usd_cost,
        duty_paid_cost_cny_per_tonne=duty_paid_cost,
        net_crush_margin_cny_per_tonne=net_margin,
        calculation_status=CalculationStatus.SUCCESS,
        missing_reasons=(),
        parameter_version=str(config.schema_version),
        parameter_hash=provenance.parameter_hash,
        mapping_identity=expected.mapping_identity,
        mapping_hash=expected.mapping_hash,
        contract_override_hash=selection.contract_override_hash,
    )


def _validate_optional_number(value: object, field_name: str, *, positive: bool) -> None:
    if value is None:
        return
    require_finite_number(
        value,
        field_name=field_name,
        positive=positive,
        reason=MissingReason.INVALID_PRICE,
    )
