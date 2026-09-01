from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.import_profit.config import (
    ContractOverrideConfig,
    ContractOverrideRule,
    load_soybean_config,
)
from agri_research_agent.import_profit.contract_mapping import map_soybean_contracts
from agri_research_agent.import_profit.historical_cnf_adapter import shipment_year_for
from agri_research_agent.import_profit.models import CbotContract
from agri_research_agent.import_profit.morning_external_inputs import (
    CBOT_SOURCE_FILENAME,
    FX_SOURCE_FILENAME,
    MorningExternalInputsError,
    build_morning_external_inputs_candidate,
    load_current_external_input_candidate,
    store_morning_external_inputs_candidate,
)
from agri_research_agent.import_profit.standard_io import CBOT_SCHEMA, FX_SCHEMA


CONFIG_PATH = Path("02_configs/import_profit_soybean.yaml")
SHA = "A" * 64


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def reuters_candidate(
    root: Path,
    *,
    business_date: date = date(2026, 8, 5),
    missing_cbot: bool = False,
    fx_tenors=range(13),
    extra_cbot=(),
) -> Path:
    config = load_soybean_config(CONFIG_PATH)
    root.mkdir()
    contracts = sorted(
        {
            (
                mapped.cbot.contract_year,
                mapped.cbot.contract_month,
            )
            for month in range(1, 13)
            for mapped in (
                map_soybean_contracts(
                    config, shipment_year_for(business_date, month), month
                ),
            )
        }
    )
    contracts = sorted(set(contracts) | set(extra_cbot))
    if missing_cbot:
        contracts.pop()
    cbot_rows = [
        {
            "market_date": business_date,
            "contract_year": year,
            "contract_month": month,
            "price_cents_per_bushel": 1000.0 + index,
            "lead_months": (year - business_date.year) * 12
            + month
            - business_date.month,
            "exchange_quality_status": "standard_window",
            "is_usable": True,
            "eligible_for_import_profit": True,
            "source_table": "us_cbot_soybean",
            "source_column": f"c{year}{month:02d}",
            "source_statement_index": index,
            "source_snapshot_sha256": SHA,
        }
        for index, (year, month) in enumerate(contracts)
    ]
    fx_rows = [
        {
            "market_date": business_date,
            "tenor_months": tenor,
            "fx_value": 7.0 + tenor / 100,
            "unit": "cnh_per_usd",
            "source_table": "fx",
            "source_column": f"fwd_{tenor}",
            "source_statement_index": tenor,
            "source_snapshot_sha256": SHA,
            "quality_status": "valid",
            "is_usable": True,
        }
        for tenor in fx_tenors
    ]
    pq.write_table(
        pa.Table.from_pylist(cbot_rows, schema=CBOT_SCHEMA),
        root / CBOT_SOURCE_FILENAME,
    )
    pq.write_table(
        pa.Table.from_pylist(fx_rows, schema=FX_SCHEMA),
        root / FX_SOURCE_FILENAME,
    )
    outputs = {
        name: {
            "filename": name,
            "size": (root / name).stat().st_size,
            "sha256": sha(root / name),
        }
        for name in (CBOT_SOURCE_FILENAME, FX_SOURCE_FILENAME)
    }
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "source_filename": "prices.sql",
                "source_size": 123,
                "source_sha256": "B" * 64,
                "candidate_status": "passed",
                "output_files": outputs,
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.mark.parametrize("hour,minute", [(8, 5), (8, 55)])
def test_build_accepts_actual_upload_time_and_exact_same_day_inputs(
    tmp_path, hour, minute
):
    source = reuters_candidate(tmp_path / "reuters")
    result = build_morning_external_inputs_candidate(
        reuters_candidate_dir=source,
        business_date=date(2026, 8, 5),
        config=load_soybean_config(CONFIG_PATH),
        output_dir=tmp_path / "candidate",
        source_file_uploaded_at=datetime(
            2026, 8, 5, hour, minute, tzinfo=timezone.utc
        ),
        prepared_at=datetime(2026, 8, 5, 1, tzinfo=timezone.utc),
    )
    manifest = json.loads(
        (result.candidate_dir / "manifest.json").read_text("utf-8")
    )

    assert result.candidate_status == "passed"
    assert len(result.required_cbot_contracts) <= 12
    assert manifest["source_file_uploaded_at"].startswith(
        f"2026-08-05T{hour:02d}:{minute:02d}"
    )
    assert manifest["direct_fx_count"] == 12
    assert str(tmp_path) not in json.dumps(manifest)


def test_morning_inputs_include_effective_cbot_override_contract(tmp_path):
    business_date = date(2026, 8, 5)
    source = reuters_candidate(
        tmp_path / "reuters",
        business_date=business_date,
        extra_cbot=((2028, 3),),
    )
    config = load_soybean_config(CONFIG_PATH)
    overridden = replace(
        config,
        contract_override=ContractOverrideConfig(
            True,
            (
                ContractOverrideRule(
                    origin="brazil",
                    shipment_year=2026,
                    shipment_month=12,
                    effective_from_business_date=business_date,
                    effective_to_business_date=None,
                    cbot_contract=CbotContract(2028, 3),
                    soymeal_contract=None,
                    soyoil_contract=None,
                    reason="source contract anomaly",
                ),
            ),
        ),
    )

    result = build_morning_external_inputs_candidate(
        reuters_candidate_dir=source,
        business_date=business_date,
        config=overridden,
        output_dir=tmp_path / "candidate",
        source_file_uploaded_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        prepared_at=datetime(2026, 8, 5, 1, tzinfo=timezone.utc),
    )

    assert "2028-03" in result.required_cbot_contracts
    assert "2027-01" in result.required_cbot_contracts


def test_missing_target_or_fx_is_incomplete_and_never_cross_fills_date(tmp_path):
    source = reuters_candidate(
        tmp_path / "reuters", missing_cbot=True, fx_tenors=(0, 2, 4)
    )
    result = build_morning_external_inputs_candidate(
        reuters_candidate_dir=source,
        business_date=date(2026, 8, 5),
        config=load_soybean_config(CONFIG_PATH),
        output_dir=tmp_path / "candidate",
        source_file_uploaded_at=datetime(2026, 8, 5, 0, 5, tzinfo=timezone.utc),
        prepared_at=datetime(2026, 8, 5, 1, tzinfo=timezone.utc),
    )

    assert result.candidate_status == "passed_with_incomplete"
    assert result.missing_cbot_contracts
    assert result.missing_fx_count > 0


def test_weekend_and_source_identity_mismatch_fail_without_output(tmp_path):
    source = reuters_candidate(
        tmp_path / "reuters", business_date=date(2026, 8, 8)
    )
    with pytest.raises(ValueError):
        build_morning_external_inputs_candidate(
            reuters_candidate_dir=source,
            business_date=date(2026, 8, 8),
            config=load_soybean_config(CONFIG_PATH),
            output_dir=tmp_path / "weekend",
            source_file_uploaded_at=datetime.now(timezone.utc),
            prepared_at=datetime.now(timezone.utc),
        )
    manifest = json.loads((source / "manifest.json").read_text("utf-8"))
    manifest["output_files"][CBOT_SOURCE_FILENAME]["sha256"] = "0" * 64
    (source / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    with pytest.raises(MorningExternalInputsError):
        build_morning_external_inputs_candidate(
            reuters_candidate_dir=source,
            business_date=date(2026, 8, 10),
            config=load_soybean_config(CONFIG_PATH),
            output_dir=tmp_path / "bad",
            source_file_uploaded_at=datetime.now(timezone.utc),
            prepared_at=datetime.now(timezone.utc),
        )
    assert not (tmp_path / "bad").exists()


def test_store_promotes_current_previous_and_same_sql_is_no_change(tmp_path):
    source = reuters_candidate(tmp_path / "reuters")
    root = tmp_path / "external"
    kwargs = dict(
        reuters_candidate_dir=source,
        business_date=date(2026, 8, 5),
        config=load_soybean_config(CONFIG_PATH),
        source_file_uploaded_at=datetime(2026, 8, 5, 0, 5, tzinfo=timezone.utc),
        prepared_at=datetime(2026, 8, 5, 1, tzinfo=timezone.utc),
        promoted_at=datetime(2026, 8, 5, 1, 1, tzinfo=timezone.utc),
    )
    first = store_morning_external_inputs_candidate(
        root, candidate_id="external-001", **kwargs
    )
    index_before = (root / "external_input_index.json").read_bytes()
    second = store_morning_external_inputs_candidate(
        root, candidate_id="external-002", **kwargs
    )

    assert first.status == "promoted"
    assert second.status == "no_change"
    assert (root / "external_input_index.json").read_bytes() == index_before
    assert load_current_external_input_candidate(root).candidate_id == "external-001"
