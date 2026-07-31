from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.import_profit.cnf_repricing import (
    CnfRepricingError,
    reprice_query_record_with_manual_cnf,
)
from agri_research_agent.import_profit.query import (
    HISTORICAL_BUSINESS_KEY_SCHEMA,
    load_soybean_query_dataset,
)
from agri_research_agent.import_profit.result_store import (
    RESULT_SCHEMA,
    SNAPSHOT_SCHEMA,
)
from test_import_profit_components import CONFIG, configured_rows
from test_import_profit_query import write_dataset


CALCULATED_AT = datetime(2026, 7, 30, 2, tzinfo=timezone.utc)


def repricing_fixture(tmp_path, **values):
    rows = configured_rows(
        date(2026, 6, 10),
        "brazil",
        2026,
        12,
        **values,
    )
    paths = write_dataset(tmp_path, [rows])
    dataset = load_soybean_query_dataset(*paths)
    return (
        dataset.records[0],
        pq.read_table(paths[0]).to_pylist()[0],
        pq.read_table(paths[1]).to_pylist()[0],
    )


@pytest.mark.parametrize("cnf", [100.0, -25.5, 0.0])
def test_finite_manual_cnf_reprices_with_existing_calculator(
    tmp_path, cnf
):
    record, key_row, snapshot_row = repricing_fixture(
        tmp_path, cnf=None
    )
    before_snapshot = dict(snapshot_row)
    result = reprice_query_record_with_manual_cnf(
        record,
        current_business_key_row=key_row,
        current_snapshot_row=snapshot_row,
        config=CONFIG,
        cnf_cents_per_bushel=cnf,
        calculated_at=CALCULATED_AT,
    )
    assert result.new_cnf == cnf
    assert result.new_cnf_source == "manual_ui"
    assert result.new_status == "success"
    assert result.new_missing_reasons == ()
    assert result.updated_result_row["usd_cost_per_tonne"] == pytest.approx(
        (1200.0 + cnf) * 0.367437
    )
    assert result.updated_result_row["calculated_at"] == CALCULATED_AT
    assert result.updated_snapshot_row["snapshot_status"] == "complete"
    assert result.updated_business_key_row["cnf_is_null"] is False
    assert result.updated_business_key_row["cnf_source"] == "manual_ui"
    assert snapshot_row == before_snapshot
    with pytest.raises(TypeError):
        result.updated_snapshot_row["fx_value"] = 1
    with pytest.raises(FrozenInstanceError):
        result.new_status = "changed"


def test_fixed_real_meaning_sample_matches_expected_results(tmp_path):
    record, key_row, snapshot_row = repricing_fixture(
        tmp_path,
        cnf=None,
        cbot=1152.25,
        fx=6.691957,
        soymeal=2995.0,
        soyoil=8301.0,
    )
    result = reprice_query_record_with_manual_cnf(
        record,
        current_business_key_row=key_row,
        current_snapshot_row=snapshot_row,
        config=CONFIG,
        cnf_cents_per_bushel=100,
        calculated_at=CALCULATED_AT,
    )
    assert result.updated_result_row["usd_cost_per_tonne"] == pytest.approx(
        460.12298325
    )
    assert result.updated_result_row[
        "duty_paid_cost_cny_per_tonne"
    ] == pytest.approx(3456.931637545483)
    assert result.updated_result_row[
        "net_crush_margin_cny_per_tonne"
    ] == pytest.approx(179.828362454517)


@pytest.mark.parametrize(
    ("missing_values", "expected_reasons"),
    [
        ({"cnf": None, "cbot": None}, ("missing_cbot",)),
        ({"cnf": None, "fx": None}, ("missing_fx",)),
        ({"cnf": None, "soymeal": None}, ("missing_soymeal",)),
        ({"cnf": None, "soyoil": None}, ("missing_soyoil",)),
        (
            {
                "cnf": None,
                "cbot": None,
                "fx": None,
                "soymeal": None,
                "soyoil": None,
            },
            (
                "missing_cbot",
                "missing_fx",
                "missing_soymeal",
                "missing_soyoil",
            ),
        ),
    ],
)
def test_remaining_missing_market_inputs_stay_ordered_and_null(
    tmp_path, missing_values, expected_reasons
):
    record, key_row, snapshot_row = repricing_fixture(
        tmp_path, **missing_values
    )
    result = reprice_query_record_with_manual_cnf(
        record,
        current_business_key_row=key_row,
        current_snapshot_row=snapshot_row,
        config=CONFIG,
        cnf_cents_per_bushel=100,
        calculated_at=CALCULATED_AT,
    )
    assert result.new_status == "incomplete"
    assert result.new_missing_reasons == expected_reasons
    assert result.updated_result_row["usd_cost_per_tonne"] is None
    assert result.updated_result_row[
        "duty_paid_cost_cny_per_tonne"
    ] is None
    assert result.updated_result_row[
        "net_crush_margin_cny_per_tonne"
    ] is None


def test_clearing_cnf_adds_first_reason_and_nulls_results(tmp_path):
    record, key_row, snapshot_row = repricing_fixture(tmp_path, cnf=100)
    result = reprice_query_record_with_manual_cnf(
        record,
        current_business_key_row=key_row,
        current_snapshot_row=snapshot_row,
        config=CONFIG,
        cnf_cents_per_bushel=None,
        calculated_at=CALCULATED_AT,
    )
    assert result.new_missing_reasons == ("missing_cnf",)
    assert result.updated_snapshot_row["snapshot_status"] == "incomplete"
    assert result.updated_business_key_row["cnf_is_null"] is True
    assert result.updated_result_row["net_crush_margin_cny_per_tonne"] is None


def test_market_audit_fields_contracts_and_identities_are_preserved(
    tmp_path,
):
    record, key_row, snapshot_row = repricing_fixture(tmp_path, cnf=None)
    result = reprice_query_record_with_manual_cnf(
        record,
        current_business_key_row=key_row,
        current_snapshot_row=snapshot_row,
        config=CONFIG,
        cnf_cents_per_bushel=100,
        calculated_at=CALCULATED_AT,
    )
    changed = {
        "cnf_cents_per_bushel",
        "cnf_source",
        "snapshot_status",
        "missing_reasons",
    }
    for field in SNAPSHOT_SCHEMA.names:
        if field not in changed:
            assert result.updated_snapshot_row[field] == snapshot_row[field]
    assert result.updated_result_row["mapping_identity"] == record.mapping_identity
    assert result.updated_result_row["parameter_version"] == record.parameter_version


def test_rows_conform_to_existing_schemas_and_key_mismatch_is_rejected(
    tmp_path,
):
    record, key_row, snapshot_row = repricing_fixture(tmp_path, cnf=None)
    result = reprice_query_record_with_manual_cnf(
        record,
        current_business_key_row=key_row,
        current_snapshot_row=snapshot_row,
        config=CONFIG,
        cnf_cents_per_bushel=100,
        calculated_at=CALCULATED_AT,
    )
    assert pa.Table.from_pylist(
        [dict(result.updated_business_key_row)],
        schema=HISTORICAL_BUSINESS_KEY_SCHEMA,
    ).schema == HISTORICAL_BUSINESS_KEY_SCHEMA
    assert pa.Table.from_pylist(
        [dict(result.updated_snapshot_row)], schema=SNAPSHOT_SCHEMA
    ).schema == SNAPSHOT_SCHEMA
    assert pa.Table.from_pylist(
        [dict(result.updated_result_row)], schema=RESULT_SCHEMA
    ).schema == RESULT_SCHEMA
    broken = dict(snapshot_row)
    broken["origin"] = "us_gulf"
    with pytest.raises(CnfRepricingError, match="key"):
        reprice_query_record_with_manual_cnf(
            record,
            current_business_key_row=key_row,
            current_snapshot_row=broken,
            config=CONFIG,
            cnf_cents_per_bushel=100,
            calculated_at=CALCULATED_AT,
        )


def test_calculated_at_must_be_explicit_and_timezone_aware(tmp_path):
    record, key_row, snapshot_row = repricing_fixture(tmp_path, cnf=None)
    with pytest.raises(CnfRepricingError, match="timezone"):
        reprice_query_record_with_manual_cnf(
            record,
            current_business_key_row=key_row,
            current_snapshot_row=snapshot_row,
            config=CONFIG,
            cnf_cents_per_bushel=100,
            calculated_at=datetime(2026, 7, 30, 2),
        )


def test_module_contains_no_formula_copy_or_io():
    source = (
        Path(__file__).resolve().parents[1]
        / "03_src"
        / "agri_research_agent"
        / "import_profit"
        / "cnf_repricing.py"
    ).read_text(encoding="utf-8")
    assert "cents_per_bushel_to_usd_per_tonne" not in source
    assert "meal_yield" not in source
    assert "oil_yield" not in source
    assert "read_table" not in source
    assert "write_table" not in source
    assert source.count("calculate_soybean_net_crush_margin(") == 1
