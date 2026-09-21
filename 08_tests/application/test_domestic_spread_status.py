from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from agri_research_agent.application.domestic_spreads import (
    DEFAULT_SPREAD_CONFIG,
    TANKAN_DOMESTIC_SPREAD_INSTRUMENTS,
    TANKAN_DOMESTIC_SPREAD_MONTHS,
    _active_required_identities,
    load_domestic_spread_database,
    load_domestic_spread_status,
)
from agri_research_agent.data_sources.tankan.domestic_spread import full_contract_code


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APPS_DIR = PROJECT_ROOT / "05_apps"
if str(APPS_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_DIR))


def _rows_for_date(value: str, *, omit: tuple[str, int] | None = None) -> pd.DataFrame:
    business_date = pd.Timestamp(value)
    season = f"{business_date.year}/{business_date.year + 1}"
    pairs = [
        (instrument, month)
        for instrument in TANKAN_DOMESTIC_SPREAD_INSTRUMENTS
        for month in TANKAN_DOMESTIC_SPREAD_MONTHS
        if (instrument, month) != omit
    ]
    rows = []
    for index, (instrument, month) in enumerate(pairs):
        contract = full_contract_code(instrument, season, month)
        rows.append(
            {
                "date": business_date,
                "status": "success",
                "leg1_instrument": instrument,
                "leg1_month": month,
                "leg1_contract": contract,
                "leg2_instrument": instrument,
                "leg2_month": month,
                "leg2_contract": contract,
                "spread_name": f"fixture-{index}",
                "calendar_offset": index,
                "season": season,
                "spread_value": float(index),
                "leg1_price": float(100 + index),
                "leg2_price": 100.0,
            }
        )
    return pd.DataFrame(rows)


def _current_artifact_rows() -> pd.DataFrame:
    current = _rows_for_date("2026-08-24")
    previous = _rows_for_date("2026-08-14").iloc[[0]]
    return pd.concat([current, previous], ignore_index=True)


def test_status_uses_current_artifact_latest_and_tankan_completeness(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "historical_spread_database.parquet"
    _current_artifact_rows().to_parquet(artifact, index=False)
    payload = load_domestic_spread_database(artifact)
    before = payload.copy(deep=True)

    status = load_domestic_spread_status(payload)

    assert status.latest_business_date == "2026-08-24"
    assert (status.success_contracts, status.required_contracts) == (15, 15)
    assert status.failure_contracts == 0
    assert status.status == "success"
    pd.testing.assert_frame_equal(payload, before, check_exact=True)


def test_legacy_akshare_status_cannot_override_formal_page_status(
    tmp_path: Path, monkeypatch
) -> None:
    legacy = tmp_path / "update_status.json"
    legacy.write_text(
        json.dumps(
            {
                "source": "akshare_futures_zh_daily_sina",
                "started_at": "2026-08-14",
                "finished_at": "2026-08-14",
                "latest_date": "2026-08-14",
                "success_contracts": 15,
                "required_contracts": 15,
            }
        ),
        encoding="utf-8",
    )
    page = importlib.import_module("streamlit_app")
    monkeypatch.setattr(page, "UPDATE_STATUS_FILE", legacy, raising=False)
    messages: list[str] = []
    monkeypatch.setattr(page.st, "success", messages.append)

    payload = _current_artifact_rows()
    before = payload.copy(deep=True)
    page.render_update_status(payload)

    assert len(messages) == 1
    assert "最新交易日：2026-08-24" in messages[0]
    assert "更新状态：更新成功" in messages[0]
    assert "合约：15/15 成功，0 项缺失" in messages[0]
    assert "Goal E / Tankan Domestic Spread" in messages[0]
    assert "2026-08-14" not in messages[0]
    assert "开始：" not in messages[0]
    assert "结束：" not in messages[0]
    pd.testing.assert_frame_equal(payload, before, check_exact=True)


@pytest.mark.parametrize(
    ("success", "required", "expected_status", "expected_label"),
    [
        (10, 10, "SUCCESS", "更新成功"),
        (2, 10, "PARTIAL", "部分更新"),
        (9, 10, "PARTIAL", "部分更新"),
        (0, 10, "FAILED", "更新失败"),
        (0, 0, "NOT_APPLICABLE", "暂无应更新合约"),
    ],
)
def test_page_status_presentation_uses_authoritative_counts(
    success: int,
    required: int,
    expected_status: str,
    expected_label: str,
) -> None:
    page = importlib.import_module("streamlit_app")

    assert page.domestic_spread_status_presentation(success, required) == (
        expected_status,
        expected_label,
    )


def test_page_renders_september_partial_status_without_fixed_fifteen(
    monkeypatch,
) -> None:
    page = importlib.import_module("streamlit_app")
    payload = _rows_for_date("2026-09-21")
    payload = payload[payload["leg1_instrument"].eq("M")].copy()
    messages: list[tuple[str, str]] = []
    for method in ("success", "warning", "error", "info"):
        monkeypatch.setattr(
            page.st,
            method,
            lambda message, method=method: messages.append((method, message)),
        )

    page.render_update_status(payload)

    assert messages == [
        (
            "warning",
            "更新状态：部分更新 | 最新交易日：2026-09-21 | "
            "合约：2/10 成功，8 项缺失 | 来源：Goal E / Tankan Domestic Spread",
        )
    ]


@pytest.mark.parametrize(
    ("success", "required", "streamlit_method", "contract_summary"),
    [
        (10, 10, "success", "合约：10/10 成功，0 项缺失"),
        (2, 10, "warning", "合约：2/10 成功，8 项缺失"),
        (0, 10, "error", "合约：0/10 成功，10 项缺失"),
        (0, 0, "info", "合约：暂无应更新合约"),
    ],
)
def test_page_status_uses_expected_message_style(
    monkeypatch,
    success: int,
    required: int,
    streamlit_method: str,
    contract_summary: str,
) -> None:
    page = importlib.import_module("streamlit_app")
    emitted: list[tuple[str, str]] = []
    for method in ("success", "warning", "error", "info"):
        monkeypatch.setattr(
            page.st,
            method,
            lambda message, method=method: emitted.append((method, message)),
        )
    monkeypatch.setattr(
        page,
        "load_domestic_spread_status",
        lambda _data: SimpleNamespace(
            success_contracts=success,
            required_contracts=required,
            failure_contracts=max(required - success, 0),
            latest_business_date="2026-09-21",
            source="fixture",
        ),
    )

    page.render_update_status(pd.DataFrame())

    assert len(emitted) == 1
    assert emitted[0][0] == streamlit_method
    assert contract_summary in emitted[0][1]


def test_september_status_excludes_contracts_after_their_seasonal_window() -> None:
    status = load_domestic_spread_status(_rows_for_date("2026-09-08", omit=("M", 9)))

    assert status.latest_business_date == "2026-09-08"
    assert (status.success_contracts, status.required_contracts) == (10, 10)
    assert status.failure_contracts == 0
    assert status.status == "success"


def test_september_required_identities_roll_to_full_next_year_contracts() -> None:
    required = _active_required_identities(
        pd.Timestamp("2026-09-21"), DEFAULT_SPREAD_CONFIG
    )

    assert required == {
        f"{instrument}27{month:02d}"
        for instrument in TANKAN_DOMESTIC_SPREAD_INSTRUMENTS
        for month in (1, 5)
    }
    assert not any(code.endswith("09") for code in required)


def test_old_year_contract_cannot_satisfy_current_full_identity() -> None:
    data = _rows_for_date("2026-09-21")
    old_year = data["leg1_instrument"].eq("M") & data["leg1_month"].eq(1)
    data.loc[old_year, ["leg1_contract", "leg2_contract"]] = "M2601"

    status = load_domestic_spread_status(data)

    assert (status.success_contracts, status.required_contracts) == (9, 10)
    assert status.failure_contracts == 1
    assert status.status == "failed"


def test_current_full_identity_is_recognized_and_month_only_payload_fails_closed() -> None:
    data = _rows_for_date("2026-09-21")

    current = load_domestic_spread_status(data)
    month_only = load_domestic_spread_status(
        data.drop(columns=["leg1_contract", "leg2_contract"])
    )

    assert (current.success_contracts, current.required_contracts) == (10, 10)
    assert current.status == "success"
    assert (month_only.success_contracts, month_only.required_contracts) == (0, 10)
    assert month_only.status == "failed"


def test_rollover_continues_without_a_fixed_2027_year() -> None:
    status = load_domestic_spread_status(_rows_for_date("2027-09-21"))
    required = _active_required_identities(
        pd.Timestamp("2027-09-21"), DEFAULT_SPREAD_CONFIG
    )

    assert required == {
        f"{instrument}28{month:02d}"
        for instrument in TANKAN_DOMESTIC_SPREAD_INSTRUMENTS
        for month in (1, 5)
    }
    assert (status.success_contracts, status.required_contracts) == (10, 10)
    assert status.status == "success"


def test_cross_year_window_keeps_the_same_season_then_rolls_forward() -> None:
    january_2027 = _active_required_identities(
        pd.Timestamp("2027-01-05"), DEFAULT_SPREAD_CONFIG
    )
    january_2028 = _active_required_identities(
        pd.Timestamp("2028-01-05"), DEFAULT_SPREAD_CONFIG
    )

    assert january_2027 == {
        f"{instrument}{year_month}"
        for instrument in TANKAN_DOMESTIC_SPREAD_INSTRUMENTS
        for year_month in ("2609", "2701", "2705")
    }
    assert january_2028 == {
        f"{instrument}{year_month}"
        for instrument in TANKAN_DOMESTIC_SPREAD_INSTRUMENTS
        for year_month in ("2709", "2801", "2805")
    }


def test_window_end_still_requires_month_nine_and_active_missing_series_fails() -> None:
    complete = load_domestic_spread_status(_rows_for_date("2026-08-31"))
    missing = load_domestic_spread_status(_rows_for_date("2026-08-31", omit=("M", 9)))

    assert (complete.success_contracts, complete.required_contracts) == (15, 15)
    assert complete.status == "success"
    assert (missing.success_contracts, missing.required_contracts) == (14, 15)
    assert missing.failure_contracts == 1
    assert missing.status == "failed"
    required = _active_required_identities(
        pd.Timestamp("2026-08-31"), DEFAULT_SPREAD_CONFIG
    )
    assert {f"{instrument}2609" for instrument in TANKAN_DOMESTIC_SPREAD_INSTRUMENTS} <= required
