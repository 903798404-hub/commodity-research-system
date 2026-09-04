from __future__ import annotations

from dataclasses import replace
from datetime import date
from math import inf, nan
from pathlib import Path

import pytest

import agri_research_agent.import_profit.scenario as scenario_module
from agri_research_agent.import_profit.config import ParameterOverride
from agri_research_agent.import_profit.parameter_snapshot import (
    ParameterProvenance,
    build_parameter_snapshot,
)
from agri_research_agent.import_profit.scenario import (
    SCENARIO_TARIFF_STATE,
    SCENARIO_VAT_STATE,
    ScenarioRates,
    ScenarioUnavailableError,
    ScenarioValidationError,
    calculate_scenario_batch,
    calculate_tariff_vat_scenario,
    initialize_scenario_state,
    official_scenario_rates,
    reset_scenario_state,
    validate_scenario_rate,
)
from test_import_profit_components import CONFIG, page_dataset


def complete_record(tmp_path):
    dataset, _ = page_dataset(tmp_path)
    record = dataset.get_by_date_origin_month(
        date(2026, 6, 25), "brazil", 7
    )
    assert record is not None
    return record


def test_tariff_scenario_uses_release_parameters_and_preserves_official(
    tmp_path, monkeypatch
) -> None:
    import agri_research_agent.import_profit.scenario as scenario_module

    record = complete_record(tmp_path)
    provenance = build_parameter_snapshot(CONFIG)
    before = record
    official_hash = record.parameter_hash
    calls = []
    real_calculator = scenario_module.calculate_soybean_net_crush_margin

    def observed(calculation_input, config):
        calls.append((calculation_input, config))
        return real_calculator(calculation_input, config)

    monkeypatch.setattr(
        scenario_module, "calculate_soybean_net_crush_margin", observed
    )
    result = calculate_tariff_vat_scenario(
        record,
        config=CONFIG,
        provenance=provenance,
        rates=ScenarioRates(tariff_rate=0.20, vat_rate=0.09),
    )

    assert calls
    assert result.official_tariff_rate == pytest.approx(0.03)
    assert result.scenario_tariff_rate == pytest.approx(0.20)
    assert result.official_vat_rate == pytest.approx(0.09)
    assert result.scenario_vat_rate == pytest.approx(0.09)
    assert result.official_net_crush_margin_cny_per_tonne == (
        record.net_crush_margin_cny_per_tonne
    )
    assert result.scenario_impact_cny_per_tonne == pytest.approx(
        result.scenario_net_crush_margin_cny_per_tonne
        - record.net_crush_margin_cny_per_tonne
    )
    assert result.official_parameter_hash == official_hash
    assert provenance.parameter_hash == official_hash
    assert record == before
    assert result.market_provenance == (
        record.cbot_contract,
        record.soymeal_contract,
        record.soyoil_contract,
        record.mapping_identity,
        record.mapping_hash,
        record.soymeal_contract_identity_status,
        record.soyoil_contract_identity_status,
        record.soymeal_quote_date_evidence_status,
        record.soyoil_quote_date_evidence_status,
    )


def test_reset_and_vat_scenario_never_change_official_result(tmp_path) -> None:
    record = complete_record(tmp_path)
    provenance = build_parameter_snapshot(CONFIG)
    vat = calculate_tariff_vat_scenario(
        record,
        config=CONFIG,
        provenance=provenance,
        rates=ScenarioRates(0.03, 0.20),
    )
    reset = calculate_tariff_vat_scenario(
        record,
        config=CONFIG,
        provenance=provenance,
        rates=official_scenario_rates(provenance, "brazil"),
    )

    assert vat.official_net_crush_margin_cny_per_tonne == (
        record.net_crush_margin_cny_per_tonne
    )
    assert vat.scenario_net_crush_margin_cny_per_tonne != (
        record.net_crush_margin_cny_per_tonne
    )
    assert reset.scenario_net_crush_margin_cny_per_tonne == (
        record.net_crush_margin_cny_per_tonne
    )
    assert reset.scenario_impact_cny_per_tonne == 0.0


@pytest.mark.parametrize("value", [-0.01, nan, inf, -inf, True, "20", 1.01])
def test_scenario_rate_validation_is_bounded(value) -> None:
    with pytest.raises(ScenarioValidationError):
        validate_scenario_rate(value, "tariff_rate")


def test_legacy_release_cannot_fall_back_to_current_yaml(tmp_path) -> None:
    record = complete_record(tmp_path)
    legacy = ParameterProvenance("legacy_unavailable", None, None)
    with pytest.raises(ScenarioUnavailableError):
        official_scenario_rates(legacy, "brazil")
    with pytest.raises(ScenarioUnavailableError):
        calculate_tariff_vat_scenario(
            record,
            config=CONFIG,
            provenance=legacy,
            rates=ScenarioRates(0.20, 0.09),
        )


def test_release_snapshot_stays_official_when_current_config_has_drifted(
    tmp_path,
) -> None:
    record = complete_record(tmp_path)
    provenance = build_parameter_snapshot(CONFIG)
    current_config = replace(
        CONFIG,
        default_parameters=replace(CONFIG.default_parameters, tariff_rate=0.20),
    )

    result = calculate_tariff_vat_scenario(
        record,
        config=current_config,
        provenance=provenance,
        rates=official_scenario_rates(provenance, "brazil"),
    )

    assert current_config.resolve_parameters("brazil").tariff_rate == 0.20
    assert result.official_tariff_rate == pytest.approx(0.03)
    assert result.scenario_tariff_rate == pytest.approx(0.03)
    assert result.scenario_net_crush_margin_cny_per_tonne == (
        record.net_crush_margin_cny_per_tonne
    )
    assert result.official_parameter_hash == record.parameter_hash


def test_origin_switch_reinitializes_from_target_origin_official_values() -> None:
    config = replace(
        CONFIG,
        origin_overrides=(
            ("us_gulf", ParameterOverride((("tariff_rate", 0.10),))),
        ),
    )
    provenance = build_parameter_snapshot(config)
    state: dict[str, object] = {}

    brazil = initialize_scenario_state(
        state,
        context_id="release-a",
        origin="brazil",
        provenance=provenance,
    )
    assert brazil == ScenarioRates(0.03, 0.09)
    state[SCENARIO_TARIFF_STATE] = 0.20

    us = initialize_scenario_state(
        state,
        context_id="release-a",
        origin="us_gulf",
        provenance=provenance,
    )
    assert us == ScenarioRates(0.10, 0.09)
    assert state[SCENARIO_TARIFF_STATE] == pytest.approx(0.10)
    state[SCENARIO_VAT_STATE] = 0.20
    restored = reset_scenario_state(
        state, origin="us_gulf", provenance=provenance
    )
    assert restored == ScenarioRates(0.10, 0.09)


def test_recent_batch_is_transient_and_does_not_overwrite_official_rows(
    tmp_path,
) -> None:
    dataset, _ = page_dataset(tmp_path)
    records = tuple(
        record
        for record in dataset.records
        if record.origin == "brazil"
    )
    before = tuple(records)
    results = calculate_scenario_batch(
        records,
        config=CONFIG,
        provenance=build_parameter_snapshot(CONFIG),
        rates=ScenarioRates(0.20, 0.09),
    )

    assert results
    assert records == before
    assert all(
        result.official_parameter_hash == records[index].parameter_hash
        for index, result in enumerate(results)
    )


def test_scenario_domain_has_no_runtime_or_release_write_path() -> None:
    source = Path(scenario_module.__file__).read_text(
        encoding="utf-8"
    ).lower()
    for forbidden in (
        "update_runtime_cnf_quotes",
        "write_release_index",
        "create_release",
        "seal_release",
        "write_parquet",
        "yaml.write",
        "manifest.write",
        "upsert_cnf",
    ):
        assert forbidden not in source
