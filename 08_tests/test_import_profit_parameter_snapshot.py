from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agri_research_agent.import_profit import load_soybean_config
from agri_research_agent.import_profit.config import ParameterOverride
from agri_research_agent.import_profit.parameter_snapshot import (
    PARAMETER_FIELDS,
    build_parameter_snapshot,
    parameter_hash,
    read_parameter_provenance,
    config_with_parameter_provenance,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "02_configs" / "import_profit_soybean.yaml"
CONFIG = load_soybean_config(CONFIG_PATH)


def test_parameter_snapshot_contains_every_effective_calculation_parameter():
    provenance = build_parameter_snapshot(CONFIG)

    assert provenance.status == "available"
    assert tuple(provenance.snapshot["default_parameters"]) == PARAMETER_FIELDS
    assert tuple(provenance.snapshot["parameters_by_origin"]) == tuple(
        sorted(CONFIG.origin_codes)
    )
    for origin in CONFIG.origin_codes:
        assert tuple(
            provenance.snapshot["parameters_by_origin"][origin]
        ) == PARAMETER_FIELDS
        assert provenance.parameters_for_origin(origin).tariff_rate == 0.03

    overridden = replace(
        CONFIG,
        origin_overrides=(
            ("us_gulf", ParameterOverride((("tariff_rate", 0.20),))),
        ),
    )
    overridden_provenance = build_parameter_snapshot(overridden)
    assert (
        overridden_provenance.parameters_for_origin("us_gulf").tariff_rate
        == 0.20
    )
    assert (
        overridden_provenance.parameters_for_origin("brazil").tariff_rate
        == 0.03
    )


def test_parameter_hash_is_canonical_and_changes_only_with_parameter_content():
    provenance = build_parameter_snapshot(CONFIG)
    reordered = {
        key: provenance.snapshot[key]
        for key in reversed(tuple(provenance.snapshot))
    }
    reordered["parameters_by_origin"] = {
        key: reordered["parameters_by_origin"][key]
        for key in reversed(tuple(reordered["parameters_by_origin"]))
    }

    assert parameter_hash(reordered) == provenance.parameter_hash
    metadata_only = replace(CONFIG, page_title="测试环境标题变化")
    assert (
        build_parameter_snapshot(metadata_only).parameter_hash
        == provenance.parameter_hash
    )

    changed = replace(
        CONFIG,
        default_parameters=replace(
            CONFIG.default_parameters, tariff_rate=0.20
        ),
    )
    changed_provenance = build_parameter_snapshot(changed)
    assert changed_provenance.parameter_hash != provenance.parameter_hash
    assert changed_provenance.parameters_for_origin("brazil").tariff_rate == 0.20


def test_release_parameter_reader_is_self_contained_and_legacy_is_explicit():
    provenance = build_parameter_snapshot(CONFIG)
    manifest = {
        "parameter_snapshot": provenance.snapshot,
        "parameter_hash": provenance.parameter_hash,
    }

    restored = read_parameter_provenance(manifest)
    assert restored.status == "available"
    assert restored.parameter_hash == provenance.parameter_hash
    assert restored.parameters_for_origin("us_gulf").meal_yield == 0.795
    assert restored.parameters_for_origin("us_gulf").oil_yield == 0.19
    assert (
        restored.parameters_for_origin("us_gulf")
        .cents_per_bushel_to_usd_per_tonne
        == 0.367437
    )
    assert restored.parameters_for_origin("us_gulf").tariff_rate == 0.03
    assert restored.parameters_for_origin("us_gulf").vat_rate == 0.09
    assert (
        restored.parameters_for_origin("us_gulf")
        .port_charge_cny_per_tonne
        == 50
    )
    assert (
        restored.parameters_for_origin("us_gulf")
        .processing_fee_cny_per_tonne
        == 150
    )
    assert (
        restored.parameters_for_origin("us_gulf")
        .additional_fees_cny_per_tonne
        == 0
    )

    legacy = read_parameter_provenance({"parameter_version": "1"})
    assert legacy.status == "legacy_unavailable"
    assert legacy.parameter_hash is None
    assert legacy.snapshot is None


def test_release_parameters_override_future_current_yaml_values():
    release = build_parameter_snapshot(CONFIG)
    future_config = replace(
        CONFIG,
        default_parameters=replace(
            CONFIG.default_parameters, tariff_rate=0.20
        ),
    )

    restored = config_with_parameter_provenance(future_config, release)

    assert future_config.resolve_parameters("us_gulf").tariff_rate == 0.20
    assert restored.resolve_parameters("us_gulf").tariff_rate == 0.03
    assert build_parameter_snapshot(restored).parameter_hash == release.parameter_hash
