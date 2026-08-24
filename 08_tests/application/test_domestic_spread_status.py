from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pandas as pd

from agri_research_agent.application.domestic_spreads import (
    TANKAN_DOMESTIC_SPREAD_INSTRUMENTS,
    TANKAN_DOMESTIC_SPREAD_MONTHS,
    load_domestic_spread_database,
    load_domestic_spread_status,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APPS_DIR = PROJECT_ROOT / "05_apps"
if str(APPS_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_DIR))


def _current_artifact_rows() -> pd.DataFrame:
    pairs = [
        (instrument, month)
        for instrument in TANKAN_DOMESTIC_SPREAD_INSTRUMENTS
        for month in TANKAN_DOMESTIC_SPREAD_MONTHS
    ]
    rows = []
    for index, (instrument, month) in enumerate(pairs):
        rows.append(
            {
                "date": pd.Timestamp("2026-08-24"),
                "status": "success",
                "leg1_instrument": instrument,
                "leg1_month": month,
                "leg2_instrument": "M",
                "leg2_month": 1,
                "spread_name": f"fixture-{index}",
                "calendar_offset": index,
                "season": "2025/2026",
                "spread_value": float(index),
                "leg1_price": float(100 + index),
                "leg2_price": 100.0,
            }
        )
    rows.append({**rows[0], "date": pd.Timestamp("2026-08-14")})
    return pd.DataFrame(rows)


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
    assert "合约：15/15 成功，0 失败" in messages[0]
    assert "Goal E / Tankan Domestic Spread" in messages[0]
    assert "2026-08-14" not in messages[0]
    assert "开始：" not in messages[0]
    assert "结束：" not in messages[0]
    pd.testing.assert_frame_equal(payload, before, check_exact=True)
