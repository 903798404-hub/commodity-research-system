from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest
import pyarrow.parquet as pq
import yaml

from agri_research_agent.import_profit.config import (
    ContractOverrideConfig,
    ContractOverrideRule,
    ImportProfitConfigError,
    load_soybean_config,
)
from agri_research_agent.import_profit.contract_override import (
    ContractSelectionMode,
    select_soybean_contracts,
)
from agri_research_agent.import_profit.mapping_snapshot import (
    build_mapping_snapshot,
)
from agri_research_agent.import_profit.models import (
    CbotContract,
    DceContract,
    MissingReason,
)
from agri_research_agent.import_profit.override_snapshot import (
    ContractOverrideProvenance,
    build_contract_override_snapshot,
    canonical_contract_override_bytes,
    config_with_contract_override_provenance,
    read_contract_override_provenance,
)
from agri_research_agent.import_profit.parameter_snapshot import (
    build_parameter_snapshot,
)
from agri_research_agent.import_profit.market_snapshot import (
    SnapshotStatus,
    build_soybean_market_snapshot,
)
from agri_research_agent.import_profit.recalculation import (
    recalculate_soybean_keys,
)
from agri_research_agent.import_profit.result_store import (
    MANIFEST_FILENAME,
    RESULT_FILENAME,
    SNAPSHOT_FILENAME,
    write_soybean_result_candidate,
)
from agri_research_agent.pipelines.import_profit_results import (
    build_soybean_result_candidate,
)
from test_import_profit_config import REAL_CONFIG, write_variant
from test_import_profit_recalculation import CONFIG, cbot, cnf, dce, fx, key


MATCH_DATE = date(2026, 8, 18)


def rule(
    *,
    origin: str = "brazil",
    shipment_year: int = 2026,
    shipment_month: int = 12,
    effective_from: date = date(2026, 8, 16),
    effective_to: date | None = None,
    cbot_contract: CbotContract | None = None,
    soymeal_contract: DceContract | None = None,
    soyoil_contract: DceContract | None = None,
    reason: str = "source contract anomaly",
) -> ContractOverrideRule:
    return ContractOverrideRule(
        origin=origin,
        shipment_year=shipment_year,
        shipment_month=shipment_month,
        effective_from_business_date=effective_from,
        effective_to_business_date=effective_to,
        cbot_contract=cbot_contract,
        soymeal_contract=soymeal_contract,
        soyoil_contract=soyoil_contract,
        reason=reason,
    )


def config_with_rules(
    *rules: ContractOverrideRule,
    enabled: bool = True,
):
    return replace(
        CONFIG,
        contract_override=ContractOverrideConfig(enabled, tuple(rules)),
    )


def build(config, business_key, *, cbot_records=None, dce_records=None):
    return build_soybean_market_snapshot(
        business_key,
        config=config,
        cnf_records=[cnf(business_key)],
        cbot_records=[cbot(business_date=business_key.business_date)]
        if cbot_records is None
        else cbot_records,
        fx_records=[
            replace(fx(5), market_date=business_key.business_date)
        ],
        dce_records=(
            [
                replace(
                    dce("M2701", 3200),
                    business_date=business_key.business_date,
                    source_quote_date=business_key.business_date,
                ),
                replace(
                    dce("Y2701", 8000),
                    business_date=business_key.business_date,
                    source_quote_date=business_key.business_date,
                ),
            ]
            if dce_records is None
            else dce_records
        ),
    )


def test_real_config_defaults_override_gate_off_and_empty() -> None:
    config = load_soybean_config(REAL_CONFIG)

    assert config.contract_override == ContractOverrideConfig(False, ())


@pytest.mark.parametrize(
    ("enabled", "rules"),
    [
        (False, ()),
        (True, ()),
        (
            False,
            (
                rule(soymeal_contract=DceContract.soymeal(2027, 9)),
            ),
        ),
    ],
)
def test_gate_without_active_match_is_exactly_automatic(enabled, rules) -> None:
    business_key = key(business_date=MATCH_DATE)
    baseline = build(CONFIG, business_key)
    candidate = build(
        replace(
            CONFIG,
            contract_override=ContractOverrideConfig(enabled, rules),
        ),
        business_key,
    )

    assert candidate.contract_selection.all_automatic
    assert candidate.mapped_contracts == baseline.mapped_contracts
    assert candidate.cbot_price_cents_per_bushel == baseline.cbot_price_cents_per_bushel
    assert candidate.soymeal_price_cny_per_tonne == baseline.soymeal_price_cny_per_tonne
    assert candidate.soyoil_price_cny_per_tonne == baseline.soyoil_price_cny_per_tonne
    assert candidate.snapshot_status == baseline.snapshot_status
    assert candidate.missing_reasons == baseline.missing_reasons


def test_matching_rule_changes_only_selected_leg_and_keeps_automatic_mapping() -> None:
    business_key = key(business_date=MATCH_DATE)
    config = config_with_rules(
        rule(soymeal_contract=DceContract.soymeal(2027, 9))
    )
    selection = select_soybean_contracts(config, business_key)

    assert selection.automatic.mapping_hash == CONFIG.contract_mapping_hash
    assert selection.soymeal.automatic_contract.code == "M2701"
    assert selection.soymeal.override_contract.code == "M2709"
    assert selection.soymeal.effective_contract.code == "M2709"
    assert selection.soymeal.selection_mode is ContractSelectionMode.MANUAL_OVERRIDE
    assert selection.cbot.effective_contract == selection.cbot.automatic_contract
    assert selection.cbot.selection_mode is ContractSelectionMode.AUTOMATIC
    assert selection.soyoil.effective_contract == selection.soyoil.automatic_contract
    assert selection.soyoil.selection_mode is ContractSelectionMode.AUTOMATIC
    assert selection.soymeal.reason == "source contract anomaly"
    assert selection.soymeal.effective_from_business_date == date(2026, 8, 16)
    assert selection.soymeal.effective_to_business_date is None


def test_enabled_gate_without_rules_preserves_all_pre_goal_f_business_outputs() -> None:
    business_key = key()
    baseline = recalculate_soybean_keys(
        [business_key],
        config=CONFIG,
        cnf_records=[cnf(business_key)],
        cbot_records=[cbot()],
        fx_records=[fx(5)],
        dce_records=[dce("M2701", 3200), dce("Y2701", 8000)],
    ).items[0]
    enabled = replace(
        CONFIG,
        contract_override=ContractOverrideConfig(True, ()),
    )
    candidate = recalculate_soybean_keys(
        [business_key],
        config=enabled,
        cnf_records=[cnf(business_key)],
        cbot_records=[cbot()],
        fx_records=[fx(5)],
        dce_records=[dce("M2701", 3200), dce("Y2701", 8000)],
    ).items[0]

    assert baseline.business_key == candidate.business_key
    assert baseline.market_snapshot.mapped_contracts == (
        candidate.market_snapshot.mapped_contracts
    )
    for field in (
        "cnf_cents_per_bushel",
        "cbot_price_cents_per_bushel",
        "fx_value",
        "soymeal_price_cny_per_tonne",
        "soyoil_price_cny_per_tonne",
        "snapshot_status",
        "missing_reasons",
        "parameter_hash",
        "mapping_hash",
        "soymeal_contract_identity_status",
        "soyoil_contract_identity_status",
        "soymeal_quote_date_evidence_status",
        "soyoil_quote_date_evidence_status",
    ):
        assert getattr(baseline.market_snapshot, field) == getattr(
            candidate.market_snapshot, field
        )
    for field in (
        "usd_cost_per_tonne",
        "duty_paid_cost_cny_per_tonne",
        "net_crush_margin_cny_per_tonne",
        "calculation_status",
        "missing_reasons",
        "parameter_hash",
        "mapping_hash",
    ):
        assert getattr(baseline.calculation_result, field) == getattr(
            candidate.calculation_result, field
        )


def test_all_three_bounded_contract_models_can_be_overridden() -> None:
    business_key = key(business_date=MATCH_DATE)
    selection = select_soybean_contracts(
        config_with_rules(
            rule(
                cbot_contract=CbotContract(2027, 3),
                soymeal_contract=DceContract.soymeal(2027, 9),
                soyoil_contract=DceContract.soyoil(2027, 9),
            )
        ),
        business_key,
    )

    assert selection.cbot.effective_contract == CbotContract(2027, 3)
    assert selection.soymeal.effective_contract == DceContract.soymeal(2027, 9)
    assert selection.soyoil.effective_contract == DceContract.soyoil(2027, 9)
    assert {
        selection.cbot.selection_mode,
        selection.soymeal.selection_mode,
        selection.soyoil.selection_mode,
    } == {ContractSelectionMode.MANUAL_OVERRIDE}


@pytest.mark.parametrize(
    ("business_date", "expected_manual"),
    [
        (date(2026, 8, 15), False),
        (date(2026, 8, 16), True),
        (date(2026, 9, 1), True),
        (date(2026, 9, 2), False),
    ],
)
def test_override_effective_period_is_closed_and_business_date_based(
    business_date,
    expected_manual,
) -> None:
    config = config_with_rules(
        rule(
            effective_to=date(2026, 9, 1),
            soymeal_contract=DceContract.soymeal(2027, 9),
        )
    )
    selection = select_soybean_contracts(
        config,
        key(business_date=business_date),
    )

    assert (
        selection.soymeal.selection_mode
        is ContractSelectionMode.MANUAL_OVERRIDE
    ) is expected_manual


def test_override_isolated_by_origin_and_shipment_key() -> None:
    config = config_with_rules(
        rule(soymeal_contract=DceContract.soymeal(2027, 9))
    )

    assert select_soybean_contracts(
        config, key(business_date=MATCH_DATE, origin="brazil")
    ).soymeal.selection_mode is ContractSelectionMode.MANUAL_OVERRIDE
    assert select_soybean_contracts(
        config, key(business_date=MATCH_DATE, origin="us_gulf")
    ).all_automatic
    assert select_soybean_contracts(
        config,
        key(
            business_date=MATCH_DATE,
            shipment_year=2027,
            shipment_month=1,
        ),
    ).all_automatic


def test_overlapping_rules_for_same_leg_fail_config_load(tmp_path) -> None:
    def mutate(payload):
        payload["contract_override"] = {
            "enabled": True,
            "rules": [
                {
                    "origin": "brazil",
                    "shipment_year": 2026,
                    "shipment_month": 12,
                    "effective_from_business_date": "2026-08-16",
                    "effective_to_business_date": "2026-09-01",
                    "contracts": {
                        "cbot": None,
                        "soymeal": {"contract_year": 2027, "contract_month": 9},
                        "soyoil": None,
                    },
                    "reason": "first",
                },
                {
                    "origin": "brazil",
                    "shipment_year": 2026,
                    "shipment_month": 12,
                    "effective_from_business_date": "2026-09-01",
                    "effective_to_business_date": None,
                    "contracts": {
                        "cbot": None,
                        "soymeal": {"contract_year": 2027, "contract_month": 5},
                        "soyoil": None,
                    },
                    "reason": "second",
                },
            ],
        }

    with pytest.raises(ImportProfitConfigError, match="overlapping"):
        load_soybean_config(write_variant(tmp_path, mutate))


@pytest.mark.parametrize(
    ("leg", "contract"),
    [
        ("cbot", {"contract_year": 2027, "contract_month": 13}),
        ("soymeal", {"contract_year": 2027, "contract_month": 0}),
        ("soyoil", {"contract_year": 1999, "contract_month": 9}),
    ],
)
def test_invalid_override_contracts_fail_config_load(tmp_path, leg, contract) -> None:
    def mutate(payload):
        payload["contract_override"] = {
            "enabled": True,
            "rules": [
                {
                    "origin": "brazil",
                    "shipment_year": 2026,
                    "shipment_month": 12,
                    "effective_from_business_date": "2026-08-16",
                    "effective_to_business_date": None,
                    "contracts": {
                        "cbot": None,
                        "soymeal": None,
                        "soyoil": None,
                    },
                    "reason": "invalid contract",
                }
            ],
        }
        payload["contract_override"]["rules"][0]["contracts"][leg] = contract

    with pytest.raises(ImportProfitConfigError, match="contract"):
        load_soybean_config(write_variant(tmp_path, mutate))


def test_override_snapshot_hash_is_canonical_and_independent() -> None:
    config_a = config_with_rules(
        rule(soymeal_contract=DceContract.soymeal(2027, 9))
    )
    config_b = config_with_rules(
        rule(soymeal_contract=DceContract.soymeal(2027, 5))
    )
    provenance_a = build_contract_override_snapshot(config_a)
    provenance_b = build_contract_override_snapshot(config_b)
    reordered = {
        name: value
        for name, value in reversed(tuple(provenance_a.snapshot.items()))
    }

    assert canonical_contract_override_bytes(reordered) == (
        canonical_contract_override_bytes(provenance_a.snapshot)
    )
    assert provenance_a.contract_override_hash != provenance_b.contract_override_hash
    assert build_parameter_snapshot(config_a).parameter_hash == (
        build_parameter_snapshot(config_b).parameter_hash
    )
    assert build_mapping_snapshot(config_a).mapping_hash == (
        build_mapping_snapshot(config_b).mapping_hash
    )
    assert config_with_contract_override_provenance(
        config_b, provenance_a
    ).contract_override == config_a.contract_override


def test_legacy_override_provenance_is_unavailable_without_current_fallback() -> None:
    assert read_contract_override_provenance({}) == ContractOverrideProvenance(
        status="legacy_unavailable",
        contract_override_hash=None,
        snapshot=None,
    )


def test_override_missing_price_fails_closed_without_automatic_fallback() -> None:
    business_key = key(business_date=MATCH_DATE)
    config = config_with_rules(
        rule(soymeal_contract=DceContract.soymeal(2027, 9))
    )
    snapshot = build(config, business_key)

    assert snapshot.soymeal_contract_code == "M2709"
    assert snapshot.soymeal_price_cny_per_tonne is None
    assert snapshot.snapshot_status is SnapshotStatus.INCOMPLETE
    assert MissingReason.MISSING_OVERRIDE_SOYMEAL in snapshot.missing_reasons
    assert MissingReason.MISSING_SOYMEAL not in snapshot.missing_reasons


@pytest.mark.parametrize(
    ("override_rule", "contract_field", "expected_contract", "reason"),
    [
        (
            rule(cbot_contract=CbotContract(2027, 3)),
            "cbot_contract_month",
            3,
            MissingReason.MISSING_OVERRIDE_CBOT,
        ),
        (
            rule(soyoil_contract=DceContract.soyoil(2027, 9)),
            "soyoil_contract_code",
            "Y2709",
            MissingReason.MISSING_OVERRIDE_SOYOIL,
        ),
    ],
)
def test_other_override_legs_also_fail_closed_without_price_fallback(
    override_rule,
    contract_field,
    expected_contract,
    reason,
) -> None:
    business_key = key(business_date=MATCH_DATE)
    snapshot = build(
        config_with_rules(override_rule),
        business_key,
    )

    assert getattr(snapshot, contract_field) == expected_contract
    assert snapshot.snapshot_status is SnapshotStatus.INCOMPLETE
    assert reason in snapshot.missing_reasons


def test_overlapping_dates_are_allowed_when_rules_target_different_legs() -> None:
    config = config_with_rules(
        rule(soymeal_contract=DceContract.soymeal(2027, 9), reason="meal"),
        rule(soyoil_contract=DceContract.soyoil(2027, 9), reason="oil"),
    )
    selection = select_soybean_contracts(
        config, key(business_date=MATCH_DATE)
    )

    assert selection.soymeal.selection_mode is ContractSelectionMode.MANUAL_OVERRIDE
    assert selection.soyoil.selection_mode is ContractSelectionMode.MANUAL_OVERRIDE
    assert selection.soymeal.reason == "meal"
    assert selection.soyoil.reason == "oil"


def test_manual_contract_source_identity_and_quote_date_are_not_forged() -> None:
    business_key = key(business_date=MATCH_DATE)
    config = config_with_rules(
        rule(soymeal_contract=DceContract.soymeal(2027, 9))
    )
    override_quote = replace(
        dce("M2709", 3300),
        business_date=MATCH_DATE,
        contract_identity_status="continuous_inferred",
        source_contract_code=None,
        source_delivery_month=9,
        quote_date_evidence_status="time_only_unconfirmed",
        source_quote_date=None,
        source_quote_time=time(23, 0),
        is_usable=False,
    )
    snapshot = build(
        config,
        business_key,
        dce_records=[
            replace(
                dce("M2701", 3200),
                business_date=MATCH_DATE,
                source_quote_date=MATCH_DATE,
            ),
            override_quote,
            replace(
                dce("Y2701", 8000),
                business_date=MATCH_DATE,
                source_quote_date=MATCH_DATE,
            ),
        ],
    )

    assert snapshot.soymeal_contract_code == "M2709"
    assert snapshot.soymeal_contract_identity_status == "continuous_inferred"
    assert snapshot.soymeal_quote_date_evidence_status == "time_only_unconfirmed"
    assert snapshot.soymeal_price_cny_per_tonne is None
    assert MissingReason.MISSING_OVERRIDE_SOYMEAL in snapshot.missing_reasons


def test_effective_contract_price_is_the_only_business_value_change() -> None:
    business_key = key(business_date=MATCH_DATE)
    baseline = recalculate_soybean_keys(
        [business_key],
        config=CONFIG,
        cnf_records=[cnf(business_key)],
        cbot_records=[cbot(business_date=MATCH_DATE)],
        fx_records=[replace(fx(4), market_date=MATCH_DATE)],
        dce_records=[
            replace(
                dce("M2701", 3200),
                business_date=MATCH_DATE,
                source_quote_date=MATCH_DATE,
            ),
            replace(
                dce("Y2701", 8000),
                business_date=MATCH_DATE,
                source_quote_date=MATCH_DATE,
            ),
        ],
    ).items[0]
    config = config_with_rules(
        rule(soymeal_contract=DceContract.soymeal(2027, 9))
    )
    overridden = recalculate_soybean_keys(
        [business_key],
        config=config,
        cnf_records=[cnf(business_key)],
        cbot_records=[cbot(business_date=MATCH_DATE)],
        fx_records=[replace(fx(4), market_date=MATCH_DATE)],
        dce_records=[
            replace(
                dce("M2701", 3200),
                business_date=MATCH_DATE,
                source_quote_date=MATCH_DATE,
            ),
            replace(
                dce("M2709", 3300),
                business_date=MATCH_DATE,
                source_quote_date=MATCH_DATE,
            ),
            replace(
                dce("Y2701", 8000),
                business_date=MATCH_DATE,
                source_quote_date=MATCH_DATE,
            ),
        ],
    ).items[0]

    assert baseline.business_key == overridden.business_key
    assert baseline.market_snapshot.mapped_contracts == (
        overridden.market_snapshot.mapped_contracts
    )
    assert baseline.market_snapshot.parameter_hash == (
        overridden.market_snapshot.parameter_hash
    )
    assert baseline.market_snapshot.mapping_hash == overridden.market_snapshot.mapping_hash
    assert baseline.market_snapshot.cbot_price_cents_per_bushel == (
        overridden.market_snapshot.cbot_price_cents_per_bushel
    )
    assert baseline.market_snapshot.fx_value == overridden.market_snapshot.fx_value
    assert baseline.market_snapshot.soyoil_price_cny_per_tonne == (
        overridden.market_snapshot.soyoil_price_cny_per_tonne
    )
    assert baseline.market_snapshot.soymeal_price_cny_per_tonne == 3200
    assert overridden.market_snapshot.soymeal_price_cny_per_tonne == 3300
    assert baseline.calculation_result.duty_paid_cost_cny_per_tonne == (
        overridden.calculation_result.duty_paid_cost_cny_per_tonne
    )
    expected_delta = 100 * CONFIG.default_parameters.meal_yield
    assert overridden.calculation_result.net_crush_margin_cny_per_tonne == pytest.approx(
        baseline.calculation_result.net_crush_margin_cny_per_tonne + expected_delta
    )


def test_yaml_key_order_does_not_change_override_hash(tmp_path) -> None:
    payload = yaml.safe_load(REAL_CONFIG.read_text(encoding="utf-8"))
    rule_payload = {
        "origin": "brazil",
        "shipment_year": 2026,
        "shipment_month": 12,
        "effective_from_business_date": "2026-08-16",
        "effective_to_business_date": None,
        "contracts": {
            "cbot": None,
            "soymeal": {"contract_year": 2027, "contract_month": 9},
            "soyoil": None,
        },
        "reason": "source contract anomaly",
    }
    payload["contract_override"] = {"enabled": True, "rules": [rule_payload]}
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    second.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")

    assert build_contract_override_snapshot(
        load_soybean_config(first)
    ).contract_override_hash == build_contract_override_snapshot(
        load_soybean_config(second)
    ).contract_override_hash


def test_candidate_seals_override_snapshot_hash_and_selection_rows(tmp_path) -> None:
    business_key = key(business_date=MATCH_DATE)
    config = config_with_rules(
        rule(soymeal_contract=DceContract.soymeal(2027, 9))
    )
    candidate = build_soybean_result_candidate(
        [business_key],
        config=config,
        cnf_records=[cnf(business_key)],
        cbot_records=[cbot(business_date=MATCH_DATE)],
        fx_records=[replace(fx(4), market_date=MATCH_DATE)],
        dce_records=[
            replace(
                dce("M2709", 3300),
                business_date=MATCH_DATE,
                source_quote_date=MATCH_DATE,
            ),
            replace(
                dce("Y2701", 8000),
                business_date=MATCH_DATE,
                source_quote_date=MATCH_DATE,
            ),
        ],
        calculated_at=datetime(2026, 8, 18, 1, 0, tzinfo=timezone.utc),
        synthetic_input=True,
    )
    output = tmp_path / "candidate"
    write_soybean_result_candidate(candidate, output)
    manifest = yaml.safe_load(
        (output / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    provenance = read_contract_override_provenance(manifest)
    snapshot_row = pq.read_table(output / SNAPSHOT_FILENAME).to_pylist()[0]
    result_row = pq.read_table(output / RESULT_FILENAME).to_pylist()[0]

    assert provenance.snapshot == build_contract_override_snapshot(config).snapshot
    assert snapshot_row["contract_override_hash"] == provenance.contract_override_hash
    assert result_row["contract_override_hash"] == provenance.contract_override_hash
    assert snapshot_row["cbot_selection_mode"] == "automatic"
    assert snapshot_row["cbot_automatic_contract_year"] == 2027
    assert snapshot_row["cbot_automatic_contract_month"] == 1
    assert snapshot_row["cbot_override_contract_year"] is None
    assert snapshot_row["soymeal_selection_mode"] == "manual_override"
    assert snapshot_row["soymeal_automatic_contract_code"] == "M2701"
    assert snapshot_row["soymeal_override_contract_code"] == "M2709"
    assert snapshot_row["soymeal_contract_code"] == "M2709"
    assert snapshot_row["soymeal_override_reason"] == "source contract anomaly"
    assert snapshot_row["soymeal_override_effective_from"] == date(2026, 8, 16)
    assert snapshot_row["soymeal_override_effective_to"] is None
    assert snapshot_row["soyoil_selection_mode"] == "automatic"
