from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_intraday_schedule_is_lightweight_bounded_and_session_specific() -> None:
    payload = yaml.safe_load(
        (ROOT / "02_configs/public_intraday_schedule.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert payload["timezone"] == "Asia/Shanghai"
    assert payload["job"] == {
        "command": "04_scripts/capture_public_intraday.py",
        "source": "tankan_only",
        "unified_refresh": False,
        "trigger": "manual_after_public_update",
        "retries": {"interval_seconds": 30, "max_attempts": 21},
    }
    assert payload["sessions"] == {
        "AM": {
            "window_start": "08:00:00",
            "window_end_exclusive": "12:00:00",
            "command_args": ["--session", "AM"],
        },
        "PM": {
            "window_start": "15:00:00",
            "window_end_exclusive": "21:00:00",
            "command_args": ["--session", "PM"],
        },
    }
    assert payload["non_trading_day_status"] == "NOT_SCHEDULED"
    assert payload["fallback_sources"] == []
