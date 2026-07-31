from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.import_profit.config import load_soybean_config
from agri_research_agent.import_profit.daily_increment import (
    DailyIncrementError,
    capture_and_store_dce_morning_input,
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


def frame_for(request: str, *, omit_last: bool = False) -> pd.DataFrame:
    contracts = [normalize_full_contract_code(code) for code in request.split(",")]
    if omit_last:
        contracts = contracts[:-1]
    return pd.DataFrame(
        {
            "symbol": [item.display_symbol for item in contracts],
            "time": ["09:00:30"] * len(contracts),
            "current_price": [
                3000.0 + index * 10
                if item.instrument == "soymeal"
                else 8000.0 + index * 10
                for index, item in enumerate(contracts)
            ],
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


@pytest.mark.parametrize("minute,second", [(0, 0), (2, 59)])
def test_legal_window_passed_capture_writes_strict_candidate(
    tmp_path, minute, second
):
    root = tmp_path / "dce"
    result = capture_and_store_dce_morning_input(
        root,
        business_date=TARGET,
        config=load_soybean_config(CONFIG_PATH),
        candidate_id=f"dce-{minute}-{second}",
        snapshot_batch_id=f"snapshot-{minute}-{second}",
        fetcher=lambda request: frame_for(request),
        clock=clock_at(9, minute, second),
    )

    assert result.outcome.attempt_status == "passed"
    assert result.outcome.record_count == len(result.outcome.requested_contracts)
    assert (result.outcome.candidate_dir / "dce_morning_open_prices.parquet").is_file()
    assert load_current_dce_outcome(root).candidate_id == result.outcome.candidate_id


def test_missing_contract_creates_legal_failed_outcome_without_fake_parquet(tmp_path):
    root = tmp_path / "dce"
    result = capture_and_store_dce_morning_input(
        root,
        business_date=TARGET,
        config=load_soybean_config(CONFIG_PATH),
        candidate_id="dce-failed",
        snapshot_batch_id="snapshot-failed",
        fetcher=lambda request: frame_for(request, omit_last=True),
        clock=clock_at(9, 0, 1),
    )

    assert result.outcome.attempt_status == "failed"
    assert result.outcome.missing_contracts
    assert not (
        result.outcome.candidate_dir / "dce_morning_open_prices.parquet"
    ).exists()
    manifest = json.loads(
        (result.outcome.candidate_dir / "manifest.json").read_text("utf-8")
    )
    assert manifest["capture_gate_status"] == "valid"


def test_outside_window_fails_without_candidate_or_index(tmp_path):
    root = tmp_path / "dce"
    called = False

    def fetcher(request):
        nonlocal called
        called = True
        return frame_for(request)

    with pytest.raises(DailyIncrementError):
        capture_and_store_dce_morning_input(
            root,
            business_date=TARGET,
            config=load_soybean_config(CONFIG_PATH),
            candidate_id="dce-outside",
            snapshot_batch_id="snapshot-outside",
            fetcher=fetcher,
            clock=clock_at(8, 59, 59),
        )

    assert called is False
    assert not (root / "dce_input_index.json").exists()
    assert not list((root / "candidates").glob("*")) if (root / "candidates").exists() else True
