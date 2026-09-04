from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
import yaml

from agri_research_agent.import_profit.config import (
    ContractLegRule,
    ContractMappingRule,
    load_soybean_config,
)
from agri_research_agent.import_profit.contract_mapping import (
    map_soybean_contracts,
)
from agri_research_agent.import_profit.mapping_snapshot import (
    MAPPING_SNAPSHOT_SCHEMA,
    MappingProvenance,
    build_mapping_snapshot,
    canonical_mapping_bytes,
    config_with_mapping_provenance,
    read_mapping_provenance,
)
from agri_research_agent.import_profit.parameter_snapshot import (
    build_parameter_snapshot,
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
from test_import_profit_recalculation import (
    CONFIG,
    cbot,
    cnf,
    dce,
    fx,
    key,
)


ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG = ROOT / "02_configs" / "import_profit_soybean.yaml"
FIXED_TIME = datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc)


def changed_mapping_config():
    first, *remaining = CONFIG.contract_mapping
    changed = replace(
        first,
        cbot=ContractLegRule(3, first.cbot.year_offset),
    )
    return replace(CONFIG, contract_mapping=(changed, *remaining))


def result_candidate(config):
    business_key = key()
    return build_soybean_result_candidate(
        [business_key],
        config=config,
        cnf_records=[cnf(business_key)],
        cbot_records=[cbot()],
        fx_records=[fx(5)],
        dce_records=[dce("M2701", 3200), dce("Y2701", 8000)],
        calculated_at=FIXED_TIME,
        generated_at=FIXED_TIME,
        synthetic_input=True,
    )


def test_real_mapping_snapshot_is_complete_and_matches_resolution() -> None:
    config = load_soybean_config(REAL_CONFIG)
    provenance = build_mapping_snapshot(config)

    assert provenance.status == "available"
    assert provenance.snapshot["snapshot_schema"] == MAPPING_SNAPSHOT_SCHEMA
    assert provenance.snapshot["contract_year_expression"] == (
        "shipment_year + year_offset"
    )
    assert provenance.snapshot["code_policy"] == "month_and_year_offset_only"
    rows = provenance.snapshot["rows"]
    assert [row["shipment_month"] for row in rows] == list(range(1, 13))
    assert rows[7]["soymeal"] == rows[7]["soyoil"] == {
        "contract_month": 5,
        "year_offset": 0,
    }
    assert rows[11]["cbot"] == {
        "contract_month": 1,
        "year_offset": 1,
    }
    december = map_soybean_contracts(config, 2026, 12)
    assert december.cbot.label == "2027-01"
    assert december.soymeal.code == "M2701"
    assert december.soyoil.code == "Y2701"
    assert december.mapping_hash == provenance.mapping_hash


def test_mapping_hash_is_canonical_path_independent_and_order_stable(
    tmp_path,
) -> None:
    config = load_soybean_config(REAL_CONFIG)
    provenance = build_mapping_snapshot(config)
    copied = tmp_path / "reordered.yaml"
    copied.write_text(
        yaml.safe_dump(
            yaml.safe_load(REAL_CONFIG.read_text(encoding="utf-8")),
            allow_unicode=True,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    copied_config = load_soybean_config(copied)
    reversed_snapshot = {
        key: value
        for key, value in reversed(tuple(provenance.snapshot.items()))
    }
    reversed_snapshot["rows"] = list(
        reversed(provenance.snapshot["rows"])
    )

    assert build_mapping_snapshot(copied_config).mapping_hash == (
        provenance.mapping_hash
    )
    assert canonical_mapping_bytes(reversed_snapshot) == (
        canonical_mapping_bytes(provenance.snapshot)
    )


def test_mapping_rule_change_changes_only_mapping_provenance() -> None:
    changed = changed_mapping_config()
    before = build_mapping_snapshot(CONFIG)
    after = build_mapping_snapshot(changed)

    assert before.mapping_hash != after.mapping_hash
    assert build_parameter_snapshot(CONFIG).parameter_hash == (
        build_parameter_snapshot(changed).parameter_hash
    )
    for month in range(2, 13):
        assert map_soybean_contracts(CONFIG, 2026, month).cbot == (
            map_soybean_contracts(changed, 2026, month).cbot
        )
        assert map_soybean_contracts(CONFIG, 2026, month).soymeal == (
            map_soybean_contracts(changed, 2026, month).soymeal
        )
        assert map_soybean_contracts(CONFIG, 2026, month).soyoil == (
            map_soybean_contracts(changed, 2026, month).soyoil
        )


def test_release_a_and_b_seal_independent_mapping_and_row_hashes(
    tmp_path,
) -> None:
    config_b = changed_mapping_config()
    candidate_a = result_candidate(CONFIG)
    candidate_b = result_candidate(config_b)
    result_a = candidate_a.recalculation_batch.items[0]
    result_b = candidate_b.recalculation_batch.items[0]
    output_a = tmp_path / "release-a"
    output_b = tmp_path / "release-b"
    write_soybean_result_candidate(candidate_a, output_a)
    a_bytes_before = {
        path.name: path.read_bytes() for path in output_a.iterdir()
    }
    write_soybean_result_candidate(candidate_b, output_b)

    manifest_a = json.loads(
        (output_a / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    manifest_b = json.loads(
        (output_b / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    provenance_a = read_mapping_provenance(manifest_a)
    provenance_b = read_mapping_provenance(manifest_b)
    assert provenance_a.mapping_hash != provenance_b.mapping_hash
    assert provenance_a.snapshot == build_mapping_snapshot(CONFIG).snapshot
    assert provenance_b.snapshot == build_mapping_snapshot(config_b).snapshot
    assert config_with_mapping_provenance(
        config_b, provenance_a
    ).contract_mapping == CONFIG.contract_mapping
    assert a_bytes_before == {
        path.name: path.read_bytes() for path in output_a.iterdir()
    }
    for output, provenance in (
        (output_a, provenance_a),
        (output_b, provenance_b),
    ):
        assert {
            row["mapping_hash"]
            for row in pq.read_table(output / SNAPSHOT_FILENAME).to_pylist()
        } == {provenance.mapping_hash}
        assert {
            row["mapping_hash"]
            for row in pq.read_table(output / RESULT_FILENAME).to_pylist()
        } == {provenance.mapping_hash}

    assert result_a.business_key == result_b.business_key
    assert result_a.market_snapshot.mapped_contracts.cbot == (
        result_b.market_snapshot.mapped_contracts.cbot
    )
    assert result_a.market_snapshot.mapped_contracts.soymeal == (
        result_b.market_snapshot.mapped_contracts.soymeal
    )
    assert result_a.market_snapshot.mapped_contracts.soyoil == (
        result_b.market_snapshot.mapped_contracts.soyoil
    )
    for field in (
        "cnf_cents_per_bushel",
        "cbot_price_cents_per_bushel",
        "fx_value",
        "soymeal_price_cny_per_tonne",
        "soyoil_price_cny_per_tonne",
        "snapshot_status",
        "missing_reasons",
        "soymeal_contract_identity_status",
        "soymeal_source_contract_code",
        "soymeal_source_delivery_month",
        "soyoil_contract_identity_status",
        "soyoil_source_contract_code",
        "soyoil_source_delivery_month",
        "soymeal_quote_date_evidence_status",
        "soymeal_source_quote_date",
        "soymeal_source_quote_time",
        "soyoil_quote_date_evidence_status",
        "soyoil_source_quote_date",
        "soyoil_source_quote_time",
    ):
        assert getattr(result_a.market_snapshot, field) == getattr(
            result_b.market_snapshot, field
        )
    for field in (
        "usd_cost_per_tonne",
        "duty_paid_cost_cny_per_tonne",
        "net_crush_margin_cny_per_tonne",
        "calculation_status",
        "missing_reasons",
        "parameter_hash",
    ):
        assert getattr(result_a.calculation_result, field) == getattr(
            result_b.calculation_result, field
        )
    revenue_a = (
        result_a.market_snapshot.soymeal_price_cny_per_tonne
        * CONFIG.default_parameters.meal_yield
        + result_a.market_snapshot.soyoil_price_cny_per_tonne
        * CONFIG.default_parameters.oil_yield
    )
    revenue_b = (
        result_b.market_snapshot.soymeal_price_cny_per_tonne
        * config_b.default_parameters.meal_yield
        + result_b.market_snapshot.soyoil_price_cny_per_tonne
        * config_b.default_parameters.oil_yield
    )
    assert revenue_a == revenue_b


def test_legacy_manifest_is_readable_without_current_mapping_fallback() -> None:
    legacy = read_mapping_provenance(
        {"mapping_identity": "import_profit_soybean:schema_version=1"}
    )
    assert legacy == MappingProvenance(
        status="legacy_unavailable",
        mapping_hash=None,
        snapshot=None,
    )


@pytest.mark.parametrize(
    "field",
    ["mapping_snapshot", "mapping_hash"],
)
def test_partial_mapping_provenance_is_rejected(field) -> None:
    provenance = build_mapping_snapshot(CONFIG)
    manifest = {
        "mapping_snapshot": provenance.snapshot,
        "mapping_hash": provenance.mapping_hash,
    }
    manifest.pop(field)
    with pytest.raises(ValueError):
        read_mapping_provenance(manifest)
