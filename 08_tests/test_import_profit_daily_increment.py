from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.import_profit.config import (
    ContractOverrideConfig,
    ContractOverrideRule,
    load_soybean_config,
)
from agri_research_agent.import_profit.models import DceContract
from agri_research_agent.import_profit.daily_increment import (
    DailyIncrementError,
    capture_and_store_dce_night_session_close,
    generate_daily_business_keys,
    load_current_dce_outcome,
    required_dce_contracts,
)
from agri_research_agent.import_profit.dce_daily import (
    CAPTURE_ZONE,
    normalize_full_contract_code,
)


CONFIG_PATH = Path("02_configs/import_profit_soybean.yaml")
TARGET = date(2026, 8, 5)
PREVIOUS_TRADING_DATE = date(2026, 8, 4)


def frame_for(
    request: str,
    *,
    omit_last: bool = False,
    omit_all: bool = False,
    quote_date: date | None = PREVIOUS_TRADING_DATE,
) -> pd.DataFrame:
    contracts = [normalize_full_contract_code(code) for code in request.split(",")]
    if omit_last:
        contracts = contracts[:-1]
    if omit_all:
        contracts = []
    return pd.DataFrame(
        {
            "symbol": [item.display_symbol for item in contracts],
            "time": ["23:00:00"] * len(contracts),
            "current_price": [
                3000.0 + index * 10
                if item.instrument == "soymeal"
                else 8000.0 + index * 10
                for index, item in enumerate(contracts)
            ],
            "date": [quote_date] * len(contracts),
        }
    )


def clock_at(hour: int, minute: int, second: int):
    values = iter(
        [
            datetime(2026, 8, 5, hour, minute, second, tzinfo=CAPTURE_ZONE),
            datetime(2026, 8, 5, hour, minute, min(second + 1, 59), tzinfo=CAPTURE_ZONE),
        ]
    )
    return lambda: next(values)


def calendar_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"trade_date": [PREVIOUS_TRADING_DATE, TARGET]}
    )


def test_fixed_daily_keys_and_unique_dce_contracts():
    config = load_soybean_config(CONFIG_PATH)
    keys = generate_daily_business_keys(TARGET, config)
    contracts = required_dce_contracts(TARGET, config)

    assert len(keys) == 48
    assert {key.origin for key in keys} == {
        "brazil",
        "us_gulf",
        "us_pnw",
        "argentina",
    }
    assert all(
        len([key for key in keys if key.origin == origin]) == 12
        for origin in config.origin_codes
    )
    assert len(contracts) == len(set(contracts))


def test_required_dce_contracts_include_effective_override_without_dropping_automatic():
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
                    effective_from_business_date=TARGET,
                    effective_to_business_date=None,
                    cbot_contract=None,
                    soymeal_contract=DceContract.soymeal(2028, 9),
                    soyoil_contract=None,
                    reason="source contract anomaly",
                ),
            ),
        ),
    )

    contracts = required_dce_contracts(TARGET, overridden)

    assert "M2809" in contracts
    assert "M2701" in contracts
    assert "Y2701" in contracts


@pytest.mark.parametrize("minute,second", [(30, 0), (32, 59)])
def test_legal_window_success_capture_writes_strict_candidate(
    tmp_path, minute, second
):
    root = tmp_path / "dce"
    result = capture_and_store_dce_night_session_close(
        root,
        business_date=TARGET,
        config=load_soybean_config(CONFIG_PATH),
        candidate_id=f"dce-{minute}-{second}",
        snapshot_batch_id=f"snapshot-{minute}-{second}",
        fetcher=lambda request: frame_for(request),
        trade_calendar_fetcher=calendar_frame,
        clock=clock_at(8, minute, second),
    )

    assert result.outcome.attempt_status == "success"
    assert result.outcome.record_count == len(result.outcome.requested_contracts)
    assert (result.outcome.candidate_dir / "dce_night_session_close_prices.parquet").is_file()
    assert load_current_dce_outcome(root).candidate_id == result.outcome.candidate_id


def test_missing_contract_freezes_valid_subset_as_incomplete(tmp_path):
    root = tmp_path / "dce"
    result = capture_and_store_dce_night_session_close(
        root,
        business_date=TARGET,
        config=load_soybean_config(CONFIG_PATH),
        candidate_id="dce-failed",
        snapshot_batch_id="snapshot-failed",
        fetcher=lambda request: frame_for(request, omit_last=True),
        trade_calendar_fetcher=calendar_frame,
        clock=clock_at(8, 30, 1),
    )

    assert result.outcome.attempt_status == "passed_with_incomplete"
    assert result.outcome.missing_contracts
    assert (
        result.outcome.candidate_dir / "dce_night_session_close_prices.parquet"
    ).is_file()
    manifest = json.loads(
        (result.outcome.candidate_dir / "manifest.json").read_text("utf-8")
    )
    assert manifest["capture_gate_status"] == "valid"
    assert manifest["price_type"] == "night_session_close"


def test_all_contracts_failed_records_failed_without_parquet(tmp_path):
    root = tmp_path / "dce"
    result = capture_and_store_dce_night_session_close(
        root,
        business_date=TARGET,
        config=load_soybean_config(CONFIG_PATH),
        candidate_id="dce-all-failed",
        snapshot_batch_id="snapshot-all-failed",
        fetcher=lambda request: frame_for(request, omit_all=True),
        trade_calendar_fetcher=calendar_frame,
        clock=clock_at(8, 30, 1),
    )
    assert result.outcome.attempt_status == "failed"
    assert not (
        result.outcome.candidate_dir / "dce_night_session_close_prices.parquet"
    ).exists()


def test_time_only_candidate_retains_diagnostics_but_is_not_usable(tmp_path):
    root = tmp_path / "dce"
    result = capture_and_store_dce_night_session_close(
        root,
        business_date=TARGET,
        config=load_soybean_config(CONFIG_PATH),
        candidate_id="dce-time-only",
        snapshot_batch_id="snapshot-time-only",
        fetcher=lambda request: frame_for(request, quote_date=None),
        trade_calendar_fetcher=calendar_frame,
        clock=clock_at(8, 30, 1),
    )

    assert result.outcome.attempt_status == "failed"
    assert result.outcome.available_contracts == ()
    assert "quote_date_unconfirmed" in result.outcome.failure_reasons
    parquet = result.outcome.candidate_dir / "dce_night_session_close_prices.parquet"
    assert parquet.is_file()
    rows = pd.read_parquet(parquet).to_dict("records")
    assert rows
    assert {row["quote_date_evidence_status"] for row in rows} == {
        "time_only_unconfirmed"
    }
    assert {row["is_usable"] for row in rows} == {False}


def test_outside_window_fails_without_candidate_or_index(tmp_path):
    root = tmp_path / "dce"
    called = False

    def fetcher(request):
        nonlocal called
        called = True
        return frame_for(request)

    with pytest.raises(DailyIncrementError):
        capture_and_store_dce_night_session_close(
            root,
            business_date=TARGET,
            config=load_soybean_config(CONFIG_PATH),
            candidate_id="dce-outside",
            snapshot_batch_id="snapshot-outside",
            fetcher=fetcher,
            trade_calendar_fetcher=calendar_frame,
            clock=clock_at(8, 29, 59),
        )

    assert called is False
    assert not (root / "dce_input_index.json").exists()
    assert not list((root / "candidates").glob("*")) if (root / "candidates").exists() else True
