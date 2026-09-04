"""Transient tariff/VAT research scenarios for immutable Releases."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from math import isfinite
from typing import Iterable, MutableMapping

from .config import (
    ParameterOverride,
    SoybeanImportProfitConfig,
    SoybeanParameters,
)
from .contract_override import select_soybean_contracts
from .models import BusinessKey
from .parameter_snapshot import (
    ParameterProvenance,
    ParameterSnapshotError,
    config_with_parameter_provenance,
)
from .query import SoybeanQueryRecord
from .soybean import (
    SoybeanCalculationInput,
    calculate_soybean_net_crush_margin,
)


SCENARIO_CONTEXT_STATE = "import_profit_page:scenario_context"
SCENARIO_TARIFF_STATE = "import_profit_page:scenario_tariff_rate"
SCENARIO_VAT_STATE = "import_profit_page:scenario_vat_rate"
MAX_SCENARIO_RATE = 1.0


class ScenarioError(ValueError):
    """Base error for transient tariff/VAT scenarios."""


class ScenarioUnavailableError(ScenarioError):
    """Raised when a Release has no trustworthy parameter provenance."""


class ScenarioValidationError(ScenarioError):
    """Raised when a temporary rate or persisted row is incompatible."""


@dataclass(frozen=True, slots=True)
class ScenarioRates:
    tariff_rate: float
    vat_rate: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "tariff_rate",
            validate_scenario_rate(self.tariff_rate, "tariff_rate"),
        )
        object.__setattr__(
            self,
            "vat_rate",
            validate_scenario_rate(self.vat_rate, "vat_rate"),
        )


@dataclass(frozen=True, slots=True)
class ScenarioCalculation:
    business_key: BusinessKey
    shipment_period: str
    official_tariff_rate: float
    scenario_tariff_rate: float
    official_vat_rate: float
    scenario_vat_rate: float
    official_duty_paid_cost_cny_per_tonne: float | None
    scenario_duty_paid_cost_cny_per_tonne: float | None
    official_net_crush_margin_cny_per_tonne: float | None
    scenario_net_crush_margin_cny_per_tonne: float | None
    scenario_impact_cny_per_tonne: float | None
    scenario_status: str
    missing_reasons: tuple[str, ...]
    official_parameter_hash: str
    market_provenance: tuple[object, ...]


def validate_scenario_rate(value: object, field_name: str) -> float:
    """Accept only finite decimal rates in the bounded research interval."""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(float(value))
    ):
        raise ScenarioValidationError(f"{field_name} must be finite numeric")
    parsed = float(value)
    if not 0.0 <= parsed <= MAX_SCENARIO_RATE:
        raise ScenarioValidationError(
            f"{field_name} must be between 0 and {MAX_SCENARIO_RATE:g}"
        )
    return parsed


def official_scenario_rates(
    provenance: ParameterProvenance,
    origin: str,
) -> ScenarioRates:
    """Read effective official rates only from one Release snapshot."""

    try:
        parameters = provenance.parameters_for_origin(origin)
    except ParameterSnapshotError as exc:
        raise ScenarioUnavailableError(
            "Official parameter snapshot unavailable"
        ) from exc
    return ScenarioRates(parameters.tariff_rate, parameters.vat_rate)


def calculate_tariff_vat_scenario(
    record: SoybeanQueryRecord,
    *,
    config: SoybeanImportProfitConfig,
    provenance: ParameterProvenance,
    rates: ScenarioRates,
) -> ScenarioCalculation:
    """Recalculate one immutable official row with only tariff/VAT overridden."""

    if not isinstance(record, SoybeanQueryRecord):
        raise ScenarioValidationError("record must be a SoybeanQueryRecord")
    if not isinstance(rates, ScenarioRates):
        raise ScenarioValidationError("rates must be ScenarioRates")
    if not provenance.available or provenance.parameter_hash is None:
        raise ScenarioUnavailableError("Official parameter snapshot unavailable")
    if record.parameter_hash != provenance.parameter_hash:
        raise ScenarioValidationError(
            "record parameter hash does not match the selected Release"
        )
    try:
        release_config = config_with_parameter_provenance(config, provenance)
        official_parameters = provenance.parameters_for_origin(record.origin)
    except ParameterSnapshotError as exc:
        raise ScenarioUnavailableError(
            "Official parameter snapshot unavailable"
        ) from exc
    scenario_parameters = replace(
        official_parameters,
        tariff_rate=rates.tariff_rate,
        vat_rate=rates.vat_rate,
    )
    scenario_config = _config_with_origin_parameters(
        release_config,
        record.origin,
        scenario_parameters,
    )
    key = BusinessKey(
        record.business_date,
        record.commodity,
        record.origin,
        record.shipment_year,
        record.shipment_month,
        scenario_config.origin_codes,
        scenario_config.commodity,
        record.shipment_period,
    )
    selection = select_soybean_contracts(scenario_config, key)
    mapped = selection.automatic
    if (
        record.cbot_contract != selection.cbot.effective_contract.label
        or record.soymeal_contract != selection.soymeal.effective_contract.code
        or record.soyoil_contract != selection.soyoil.effective_contract.code
        or record.mapping_identity != mapped.mapping_identity
        or record.mapping_hash != mapped.mapping_hash
        or record.contract_override_hash != selection.contract_override_hash
    ):
        raise ScenarioValidationError(
            "persisted contracts or mapping are incompatible with the Release"
        )
    official_rates = ScenarioRates(
        official_parameters.tariff_rate,
        official_parameters.vat_rate,
    )
    if rates == official_rates:
        scenario_duty = record.duty_paid_cost_cny_per_tonne
        scenario_margin = record.net_crush_margin_cny_per_tonne
        scenario_status = record.calculation_status
        missing_reasons = record.missing_reasons
    else:
        output = calculate_soybean_net_crush_margin(
            SoybeanCalculationInput(
                business_key=key,
                cnf_cents_per_bushel=record.cnf_cents_per_bushel,
                cbot_contract=selection.cbot.effective_contract,
                cbot_daily_price_cents_per_bushel=(
                    record.cbot_price_cents_per_bushel
                ),
                fx_value=record.fx_value,
                soymeal_contract=selection.soymeal.effective_contract,
                soymeal_price_cny_per_tonne=(
                    record.soymeal_price_cny_per_tonne
                ),
                soyoil_contract=selection.soyoil.effective_contract,
                soyoil_price_cny_per_tonne=(
                    record.soyoil_price_cny_per_tonne
                ),
                resolved_parameters=scenario_parameters,
                mapping_identity=record.mapping_identity,
                mapping_hash=record.mapping_hash,
                contract_override_hash=record.contract_override_hash,
            ),
            scenario_config,
        )
        scenario_duty = output.duty_paid_cost_cny_per_tonne
        scenario_margin = output.net_crush_margin_cny_per_tonne
        scenario_status = output.calculation_status.value
        missing_reasons = tuple(
            reason.value for reason in output.missing_reasons
        )
    impact = (
        None
        if scenario_margin is None
        or record.net_crush_margin_cny_per_tonne is None
        else scenario_margin - record.net_crush_margin_cny_per_tonne
    )
    return ScenarioCalculation(
        business_key=key,
        shipment_period=record.shipment_period,
        official_tariff_rate=official_parameters.tariff_rate,
        scenario_tariff_rate=rates.tariff_rate,
        official_vat_rate=official_parameters.vat_rate,
        scenario_vat_rate=rates.vat_rate,
        official_duty_paid_cost_cny_per_tonne=(
            record.duty_paid_cost_cny_per_tonne
        ),
        scenario_duty_paid_cost_cny_per_tonne=scenario_duty,
        official_net_crush_margin_cny_per_tonne=(
            record.net_crush_margin_cny_per_tonne
        ),
        scenario_net_crush_margin_cny_per_tonne=scenario_margin,
        scenario_impact_cny_per_tonne=impact,
        scenario_status=scenario_status,
        missing_reasons=tuple(missing_reasons),
        official_parameter_hash=provenance.parameter_hash,
        market_provenance=(
            record.cbot_contract,
            record.soymeal_contract,
            record.soyoil_contract,
            record.mapping_identity,
            record.mapping_hash,
            record.soymeal_contract_identity_status,
            record.soyoil_contract_identity_status,
            record.soymeal_quote_date_evidence_status,
            record.soyoil_quote_date_evidence_status,
        ),
    )


def calculate_scenario_batch(
    records: Iterable[SoybeanQueryRecord],
    *,
    config: SoybeanImportProfitConfig,
    provenance: ParameterProvenance,
    rates: ScenarioRates,
) -> tuple[ScenarioCalculation, ...]:
    """Calculate transient results without mutating or persisting source rows."""

    return tuple(
        calculate_tariff_vat_scenario(
            record,
            config=config,
            provenance=provenance,
            rates=rates,
        )
        for record in records
    )


def initialize_scenario_state(
    state: MutableMapping[str, object],
    *,
    context_id: str,
    origin: str,
    provenance: ParameterProvenance,
) -> ScenarioRates:
    """Reset temporary rates when Release identity or origin changes."""

    official = official_scenario_rates(provenance, origin)
    context = f"{context_id}|{provenance.parameter_hash}|{origin}"
    if state.get(SCENARIO_CONTEXT_STATE) != context:
        state[SCENARIO_CONTEXT_STATE] = context
        state[SCENARIO_TARIFF_STATE] = official.tariff_rate
        state[SCENARIO_VAT_STATE] = official.vat_rate
    return ScenarioRates(
        state.get(SCENARIO_TARIFF_STATE, official.tariff_rate),
        state.get(SCENARIO_VAT_STATE, official.vat_rate),
    )


def reset_scenario_state(
    state: MutableMapping[str, object],
    *,
    origin: str,
    provenance: ParameterProvenance,
) -> ScenarioRates:
    """Restore the selected origin's Release rates in page-local memory."""

    official = official_scenario_rates(provenance, origin)
    state[SCENARIO_TARIFF_STATE] = official.tariff_rate
    state[SCENARIO_VAT_STATE] = official.vat_rate
    return official


def _config_with_origin_parameters(
    config: SoybeanImportProfitConfig,
    origin: str,
    parameters: SoybeanParameters,
) -> SoybeanImportProfitConfig:
    effective = {
        code: config.resolve_parameters(code) for code in config.origin_codes
    }
    effective[origin] = parameters
    default = config.default_parameters
    overrides = []
    for code in config.origin_codes:
        values = tuple(
            (item.name, getattr(effective[code], item.name))
            for item in fields(SoybeanParameters)
            if getattr(effective[code], item.name)
            != getattr(default, item.name)
        )
        if values:
            overrides.append((code, ParameterOverride(values)))
    return replace(config, origin_overrides=tuple(overrides))
