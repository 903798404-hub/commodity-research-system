from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
import yaml

from agri_research_agent.import_profit import (
    BusinessKey,
    CalculationStatus,
    ContractMappingError,
    MissingReason,
    SoybeanCalculationInput,
    calculate_soybean_net_crush_margin,
    load_soybean_config,
    map_soybean_contracts,
)
from agri_research_agent.import_profit.models import InvalidParameterError, InvalidPriceError


ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG = ROOT / "02_configs" / "import_profit_soybean.yaml"
CONFIG = load_soybean_config(REAL_CONFIG)


def business_key(
    *,
    origin: str = "brazil",
    shipment_year: int = 2026,
    shipment_month: int = 12,
) -> BusinessKey:
    return BusinessKey(
        date(2026, 7, 28),
        "soybean",
        origin,
        shipment_year,
        shipment_month,
        CONFIG.origin_codes,
        CONFIG.commodity,
    )


def complete_input(config=CONFIG, *, key: BusinessKey | None = None, **overrides) -> SoybeanCalculationInput:
    key = key or business_key()
    mapped = map_soybean_contracts(config, key.shipment_year, key.shipment_month)
    values = {
        "business_key": key,
        "cnf_cents_per_bushel": 150.0,
        "cbot_contract": mapped.cbot,
        "cbot_daily_price_cents_per_bushel": 1200.0,
        "fx_value": 7.2,
        "soymeal_contract": mapped.soymeal,
        "soymeal_price_cny_per_tonne": 3200.0,
        "soyoil_contract": mapped.soyoil,
        "soyoil_price_cny_per_tonne": 8000.0,
        "resolved_parameters": config.resolve_parameters(key.origin),
        "mapping_identity": mapped.mapping_identity,
        "mapping_hash": mapped.mapping_hash,
    }
    values.update(overrides)
    return SoybeanCalculationInput(**values)


def test_fixed_complete_input_reconciles_to_independent_manual_formula() -> None:
    calculation_input = complete_input()
    result = calculate_soybean_net_crush_margin(calculation_input, CONFIG)

    expected_usd_cost = (1200.0 + 150.0) * 0.367437
    expected_duty_paid = expected_usd_cost * 7.2 * (1 + 0.03) * (1 + 0.09)
    expected_product_value = 3200.0 * 0.795 + 8000.0 * 0.19
    expected_net_margin = expected_product_value - expected_duty_paid - 50.0 - 150.0
    assert result.usd_cost_per_tonne == pytest.approx(expected_usd_cost)
    assert result.duty_paid_cost_cny_per_tonne == pytest.approx(expected_duty_paid)
    assert result.net_crush_margin_cny_per_tonne == pytest.approx(expected_net_margin)
    assert result.calculation_status is CalculationStatus.SUCCESS
    assert result.missing_reasons == ()
    assert result.usd_cost_per_tonne != round(result.usd_cost_per_tonne, 2)


def test_formal_parameter_baseline_and_old_baseline_produce_different_results() -> None:
    params = CONFIG.default_parameters
    assert (
        params.tariff_rate,
        params.vat_rate,
        params.port_charge_cny_per_tonne,
        params.processing_fee_cny_per_tonne,
        params.meal_yield,
        params.oil_yield,
        params.cents_per_bushel_to_usd_per_tonne,
    ) == (0.03, 0.09, 50.0, 150.0, 0.795, 0.19, 0.367437)

    old_params = replace(
        params,
        port_charge_cny_per_tonne=100.0,
        meal_yield=0.785,
        oil_yield=0.185,
    )
    old_config = replace(CONFIG, default_parameters=old_params)
    new_result = calculate_soybean_net_crush_margin(complete_input(), CONFIG)
    old_result = calculate_soybean_net_crush_margin(
        complete_input(old_config), old_config
    )
    assert new_result.net_crush_margin_cny_per_tonne != pytest.approx(
        old_result.net_crush_margin_cny_per_tonne
    )


def test_zero_cnf_is_a_valid_quote() -> None:
    result = calculate_soybean_net_crush_margin(
        complete_input(cnf_cents_per_bushel=0),
        CONFIG,
    )
    assert result.calculation_status is CalculationStatus.SUCCESS
    assert result.usd_cost_per_tonne == pytest.approx(1200.0 * 0.367437)


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("cnf_cents_per_bushel", MissingReason.MISSING_CNF),
        ("cbot_daily_price_cents_per_bushel", MissingReason.MISSING_CBOT),
        ("fx_value", MissingReason.MISSING_FX),
        ("soymeal_price_cny_per_tonne", MissingReason.MISSING_SOYMEAL),
        ("soyoil_price_cny_per_tonne", MissingReason.MISSING_SOYOIL),
    ],
)
def test_each_missing_input_returns_structured_incomplete_result(
    field: str,
    reason: MissingReason,
) -> None:
    result = calculate_soybean_net_crush_margin(complete_input(**{field: None}), CONFIG)
    assert result.calculation_status is CalculationStatus.INCOMPLETE
    assert result.missing_reasons == (reason,)
    assert result.usd_cost_per_tonne is None
    assert result.duty_paid_cost_cny_per_tonne is None
    assert result.net_crush_margin_cny_per_tonne is None


def test_multiple_missing_reasons_have_stable_business_order() -> None:
    result = calculate_soybean_net_crush_margin(
        complete_input(
            cnf_cents_per_bushel=None,
            fx_value=None,
            soyoil_price_cny_per_tonne=None,
        ),
        CONFIG,
    )
    assert result.missing_reasons == (
        MissingReason.MISSING_CNF,
        MissingReason.MISSING_FX,
        MissingReason.MISSING_SOYOIL,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cbot_daily_price_cents_per_bushel", 0),
        ("fx_value", -1),
        ("soymeal_price_cny_per_tonne", float("nan")),
        ("soyoil_price_cny_per_tonne", float("inf")),
        ("cnf_cents_per_bushel", "0"),
    ],
)
def test_invalid_prices_raise_controlled_errors(field: str, value: object) -> None:
    with pytest.raises(InvalidPriceError) as exc_info:
        complete_input(**{field: value})
    assert exc_info.value.reason is MissingReason.INVALID_PRICE


def test_contract_mapping_mismatch_raises_controlled_error() -> None:
    key = business_key(shipment_year=2026, shipment_month=12)
    wrong = map_soybean_contracts(CONFIG, 2027, 1)
    calculation_input = complete_input(key=key, soymeal_contract=wrong.soymeal)
    with pytest.raises(ContractMappingError) as exc_info:
        calculate_soybean_net_crush_margin(calculation_input, CONFIG)
    assert exc_info.value.reason is MissingReason.INVALID_CONTRACT_MAPPING


def test_december_cross_year_contracts_are_accepted() -> None:
    calculation_input = complete_input()
    assert calculation_input.cbot_contract.label == "2027-01"
    assert calculation_input.soymeal_contract.code == "M2701"
    assert calculation_input.soyoil_contract.code == "Y2701"
    assert calculate_soybean_net_crush_margin(calculation_input, CONFIG).calculation_status is CalculationStatus.SUCCESS


def test_calculator_requires_parameters_resolved_from_configuration() -> None:
    wrong_parameters = replace(CONFIG.default_parameters, port_charge_cny_per_tonne=999)
    with pytest.raises(InvalidParameterError) as exc_info:
        calculate_soybean_net_crush_margin(
            complete_input(resolved_parameters=wrong_parameters),
            CONFIG,
        )
    assert exc_info.value.reason is MissingReason.INVALID_PARAMETER


def test_origin_override_changes_only_the_configured_calculation_term(tmp_path: Path) -> None:
    payload = yaml.safe_load(REAL_CONFIG.read_text(encoding="utf-8"))
    payload["origin_overrides"] = {"brazil": {"port_charge_cny_per_tonne": 125}}
    path = tmp_path / "override.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    override_config = load_soybean_config(path)
    key = business_key()

    baseline = calculate_soybean_net_crush_margin(complete_input(), CONFIG)
    overridden = calculate_soybean_net_crush_margin(
        complete_input(override_config, key=key),
        override_config,
    )
    assert overridden.net_crush_margin_cny_per_tonne == pytest.approx(
        baseline.net_crush_margin_cny_per_tonne - 75  # type: ignore[operator]
    )


def test_calculator_is_deterministic_and_does_not_mutate_input() -> None:
    calculation_input = complete_input()
    snapshot = calculation_input
    first = calculate_soybean_net_crush_margin(calculation_input, CONFIG)
    second = calculate_soybean_net_crush_margin(calculation_input, CONFIG)

    assert first == second
    assert calculation_input == snapshot
