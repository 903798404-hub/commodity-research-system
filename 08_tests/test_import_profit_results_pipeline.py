from dataclasses import replace
from datetime import date, datetime, timezone
import importlib.util
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.import_profit.business_days import (
    NonBusinessWeekdayError,
)
from agri_research_agent.import_profit.cnf_store import CNF_SCHEMA
from agri_research_agent.import_profit.config import Origin
from agri_research_agent.import_profit.models import (
    CalculationStatus,
    MissingReason,
)
from agri_research_agent.import_profit.recalculation import (
    DuplicateRecalculationKeyError,
)
from agri_research_agent.import_profit.standard_io import StandardFileIdentity
from agri_research_agent.pipelines.import_profit_results import (
    CandidateStatus,
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
from test_import_profit_standard_io import (
    CBOT_SCHEMA,
    DCE_INCREMENTAL_SCHEMA,
    FX_SCHEMA,
    cbot_row,
    dce_row,
    fx_row,
)


FIXED_TIME = datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc)


def identity(filename: str, count: int) -> StandardFileIdentity:
    return StandardFileIdentity(
        filename=filename,
        exists=True,
        size_bytes=100,
        sha256="A" * 64,
        schema_version="test-v1",
        schema_fingerprint="B" * 64,
        record_count=count,
        earliest_date=date(2026, 7, 28),
        latest_date=date(2026, 7, 28),
        duplicate_key_count=0,
    )


def build(
    keys,
    *,
    cnf_records,
    cbot_records=None,
    fx_records=None,
    dce_records=None,
    synthetic=False,
):
    return build_soybean_result_candidate(
        keys,
        config=CONFIG,
        cnf_records=cnf_records,
        cbot_records=[cbot()] if cbot_records is None else cbot_records,
        fx_records=[fx(5)] if fx_records is None else fx_records,
        dce_records=(
            [dce("M2701", 3200), dce("Y2701", 8000)]
            if dce_records is None
            else dce_records
        ),
        calculated_at=FIXED_TIME,
        generated_at=FIXED_TIME,
        input_files=(
            identity("cnf.parquet", len(cnf_records)),
            identity("cbot.parquet", len([cbot()] if cbot_records is None else cbot_records)),
        ),
        synthetic_input=synthetic,
    )


def test_single_complete_fixed_sample_and_status() -> None:
    business_key = key()
    candidate = build(
        [business_key],
        cnf_records=[cnf(business_key)],
        synthetic=True,
    )
    result = candidate.recalculation_batch.items[0].calculation_result
    assert candidate.candidate_status is CandidateStatus.PASSED
    assert candidate.synthetic_input is True
    assert result.usd_cost_per_tonne == pytest.approx(496.03995, abs=1e-12)
    assert result.duty_paid_cost_cny_per_tonne == pytest.approx(
        4009.709173428001,
        abs=1e-12,
    )
    assert result.net_crush_margin_cny_per_tonne == pytest.approx(
        -267.709173428001,
        abs=1e-12,
    )


def test_multiple_explicit_keys_sort_and_ignore_extra_market_data() -> None:
    requested = [key(origin="us_gulf"), key(origin="brazil")]
    extra = key(origin="argentina")
    candidate = build(
        requested,
        cnf_records=[*(cnf(item) for item in requested), cnf(extra)],
        cbot_records=[cbot(), cbot(month=3)],
        fx_records=[fx(5), fx(6)],
        dce_records=[
            dce("M2701", 3200),
            dce("Y2701", 8000),
            dce("M2705", 3300),
            dce("Y2705", 8100),
        ],
    )
    returned = tuple(
        item.business_key for item in candidate.recalculation_batch.items
    )
    assert returned == tuple(
        sorted(
            requested,
            key=lambda item: (
                item.business_date,
                item.commodity,
                item.origin,
                item.shipment_year,
                item.shipment_month,
            ),
        )
    )
    assert extra not in returned


@pytest.mark.parametrize(
    ("missing", "reason"),
    [
        ("cnf", MissingReason.MISSING_CNF),
        ("cbot", MissingReason.MISSING_CBOT),
        ("fx", MissingReason.MISSING_FX),
        ("soymeal", MissingReason.MISSING_SOYMEAL),
        ("soyoil", MissingReason.MISSING_SOYOIL),
    ],
)
def test_each_normal_market_gap_is_structured_incomplete(
    missing: str,
    reason: MissingReason,
) -> None:
    business_key = key()
    kwargs = {
        "cnf_records": [] if missing == "cnf" else [cnf(business_key)],
        "cbot_records": [] if missing == "cbot" else None,
        "fx_records": [] if missing == "fx" else None,
        "dce_records": (
            [dce("Y2701", 8000)]
            if missing == "soymeal"
            else [dce("M2701", 3200)]
            if missing == "soyoil"
            else None
        ),
    }
    candidate = build([business_key], **kwargs)
    result = candidate.recalculation_batch.items[0].calculation_result
    assert candidate.candidate_status is CandidateStatus.PASSED_WITH_INCOMPLETE
    assert result.calculation_status is CalculationStatus.INCOMPLETE
    assert result.missing_reasons == (reason,)
    assert result.usd_cost_per_tonne is None
    assert result.duty_paid_cost_cny_per_tonne is None
    assert result.net_crush_margin_cny_per_tonne is None


def test_multiple_missing_reasons_are_all_retained() -> None:
    business_key = key()
    candidate = build(
        [business_key],
        cnf_records=[],
        cbot_records=[],
        fx_records=[],
        dce_records=[],
    )
    assert (
        candidate.recalculation_batch.items[0].calculation_result.missing_reasons
        == (
            MissingReason.MISSING_CNF,
            MissingReason.MISSING_CBOT,
            MissingReason.MISSING_FX,
            MissingReason.MISSING_SOYMEAL,
            MissingReason.MISSING_SOYOIL,
        )
    )


@pytest.mark.parametrize("value", [0, -25])
def test_zero_and_negative_cnf_are_successful(value: float) -> None:
    business_key = key()
    candidate = build(
        [business_key],
        cnf_records=[cnf(business_key, value)],
    )
    assert candidate.candidate_status is CandidateStatus.PASSED


def test_same_day_fx_interpolation_and_cross_year_mappings() -> None:
    december = key()
    interpolated = build(
        [december],
        cnf_records=[cnf(december)],
        fx_records=[fx(4, 7.0), fx(6, 7.4)],
    )
    snapshot = interpolated.recalculation_batch.items[0].market_snapshot
    assert snapshot.fx_value == pytest.approx(7.2)
    assert snapshot.fx_is_interpolated is True
    assert snapshot.cbot_contract_year == 2027
    assert snapshot.soymeal_contract_code == "M2701"

    january = key(shipment_year=2027, shipment_month=1)
    next_year = build(
        [january],
        cnf_records=[cnf(january)],
        fx_records=[fx(6)],
        dce_records=[dce("M2705", 3200), dce("Y2705", 8000)],
    )
    january_snapshot = next_year.recalculation_batch.items[0].market_snapshot
    assert january_snapshot.cbot_contract_year == 2027
    assert january_snapshot.soymeal_contract_code == "M2705"


def test_duplicate_weekend_and_input_mutation_boundaries() -> None:
    business_key = key()
    with pytest.raises(DuplicateRecalculationKeyError):
        build(
            [business_key, business_key],
            cnf_records=[cnf(business_key)],
        )
    weekend = key(business_date=date(2026, 8, 1))
    with pytest.raises(NonBusinessWeekdayError):
        build([weekend], cnf_records=[cnf(weekend)])

    keys = [business_key]
    cnf_records = [cnf(business_key)]
    original = (tuple(keys), tuple(cnf_records))
    first = build(keys, cnf_records=cnf_records)
    second = build(keys, cnf_records=cnf_records)
    assert first == second
    assert original == (tuple(keys), tuple(cnf_records))


def load_cli_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "04_scripts/import_profit/build_soybean_results_candidate.py"
    )
    spec = importlib.util.spec_from_file_location("stage8_candidate_cli", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cli_key_and_timestamp_contracts() -> None:
    cli = load_cli_module()
    localized_config = replace(
        CONFIG,
        origins=(
            Origin("brazil", "巴西"),
            Origin("us_gulf", "美湾"),
            Origin("us_pnw", "美西"),
            Origin("argentina", "阿根廷"),
        ),
    )
    parsed = cli.parse_explicit_key(
        "2026-07-28,巴西,2026-12",
        localized_config,
    )
    assert parsed.origin == "brazil"
    assert parsed.shipment_period == "2026-12"
    assert cli.parse_utc_datetime("2026-07-28T08:00:00Z") == FIXED_TIME
    with pytest.raises(cli.CandidateCliError):
        cli.parse_explicit_key("2026-07-28,巴西,12", CONFIG)
    with pytest.raises(cli.CandidateCliError):
        cli.parse_utc_datetime("2026-07-28T08:00:00+08:00")


def test_cli_parser_supports_repeated_keys_and_requires_input_files() -> None:
    cli = load_cli_module()
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--config",
            "config.yaml",
            "--cnf-store",
            "cnf.parquet",
            "--cbot-parquet",
            "cbot.parquet",
            "--fx-parquet",
            "fx.parquet",
            "--dce-parquet",
            "dce.parquet",
            "--key",
            "2026-07-28,巴西,2026-12",
            "--key",
            "2026-07-28,美湾,2026-12",
            "--output-dir",
            "candidate",
        ]
    )
    assert len(args.key) == 2
    with pytest.raises(SystemExit) as help_exit:
        cli.main(["--help"])
    assert help_exit.value.code == 0


def test_cli_build_from_args_writes_one_explicit_key_outside_repo(
    tmp_path,
    monkeypatch,
) -> None:
    cli = load_cli_module()
    inputs = tmp_path / "inputs"
    output = tmp_path / "candidate"
    inputs.mkdir()
    cbot_path = inputs / "cbot.parquet"
    fx_path = inputs / "fx.parquet"
    dce_path = inputs / "dce.parquet"
    cnf_path = inputs / "cnf.parquet"
    pq.write_table(
        pa.Table.from_pylist([cbot_row()], schema=CBOT_SCHEMA),
        cbot_path,
    )
    pq.write_table(
        pa.Table.from_pylist([fx_row()], schema=FX_SCHEMA),
        fx_path,
    )
    pq.write_table(
        pa.Table.from_pylist(
            [dce_row(), dce_row("Y2701")],
            schema=DCE_INCREMENTAL_SCHEMA,
        ),
        dce_path,
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "business_date": date(2026, 7, 28),
                    "commodity": "soybean",
                    "origin": "brazil",
                    "shipment_year": 2026,
                    "shipment_month": 12,
                    "cnf_cents_per_bushel": 150.0,
                    "source": "manual_ui",
                    "updated_at": FIXED_TIME,
                    "batch_id": "cli-fixed",
                }
            ],
            schema=CNF_SCHEMA,
        ),
        cnf_path,
    )
    monkeypatch.setattr(cli, "load_soybean_config", lambda _: CONFIG)
    args = cli.build_parser().parse_args(
        [
            "--config",
            "fixed-memory-config",
            "--cnf-store",
            str(cnf_path),
            "--cbot-parquet",
            str(cbot_path),
            "--fx-parquet",
            str(fx_path),
            "--dce-parquet",
            str(dce_path),
            "--key",
            "2026-07-28,brazil,2026-12",
            "--output-dir",
            str(output),
            "--calculated-at",
            "2026-07-28T08:00:00Z",
        ]
    )
    payload = cli.build_from_args(args)
    assert payload["candidate_status"] == "passed"
    assert payload["requested_key_count"] == 1
    assert {item["filename"] for item in payload["output_files"]} == {
        "soybean_market_snapshots.parquet",
        "soybean_net_crush_results.parquet",
        "quality_report.json",
        "manifest.json",
    }
