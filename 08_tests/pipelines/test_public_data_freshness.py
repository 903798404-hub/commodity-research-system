from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pandas as pd

from agri_research_agent.pipelines.public_data_freshness import (
    FreshnessGate,
    evaluate_consumer_freshness,
)


ROOT = Path(__file__).resolve().parents[2]


def _updater():
    path = ROOT / "04_scripts" / "update_price_long_from_akshare.py"
    spec = importlib.util.spec_from_file_location("domestic_spread_updater_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_freshness_uses_upstream_business_date_not_wall_clock() -> None:
    report = evaluate_consumer_freshness(
        {
            "weekend": (
                FreshnessGate.HARD,
                lambda: (date(2026, 8, 21), date(2026, 8, 21), "Sunday run"),
            ),
            "lagging": (
                FreshnessGate.HARD,
                lambda: (date(2026, 8, 14), date(2026, 8, 21), "consumer lag"),
            ),
        }
    )
    assert report.results[0].status.value == "PASS"
    assert report.results[1].status.value == "STALE"
    assert report.hard_pass is False


def test_daily_history_backfills_only_exchange_dated_rows(monkeypatch) -> None:
    updater = _updater()
    candidates = pd.DataFrame(
        [{"instrument": "M", "delivery_month": 1, "season": "2026/2027", "symbol": "M2701"}]
    )
    raw = pd.DataFrame(
        {
            "date": ["2026-08-14", "2026-08-17", "2026-08-18", "2026-08-21"],
            "close": [3165.0, 3176.0, 3241.0, 3228.0],
        }
    )
    monkeypatch.setattr(updater.ak, "futures_zh_daily_sina", lambda *, symbol: raw)
    success, failures, _ = updater.fetch_daily_history(
        candidates, start_date=date(2026, 8, 15)
    )
    assert failures.empty
    assert success["date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-08-17", "2026-08-18", "2026-08-21"
    ]
    assert success["source"].unique().tolist() == ["akshare_futures_zh_daily_sina"]
    assert "2026-08-15" not in success["date"].dt.strftime("%Y-%m-%d").tolist()


def test_daily_history_fails_closed_when_one_contract_is_unavailable(monkeypatch) -> None:
    updater = _updater()
    candidates = pd.DataFrame(
        [
            {"instrument": "M", "delivery_month": 1, "season": "2026/2027", "symbol": "M2701"},
            {"instrument": "RM", "delivery_month": 1, "season": "2026/2027", "symbol": "RM2701"},
        ]
    )

    def fetch(*, symbol: str):
        if symbol == "RM2701":
            raise TimeoutError("source timeout")
        return pd.DataFrame({"date": ["2026-08-21"], "close": [3228.0]})

    monkeypatch.setattr(updater.ak, "futures_zh_daily_sina", fetch)
    success, failures, _ = updater.fetch_daily_history(
        candidates, start_date=date(2026, 8, 15)
    )
    assert success["symbol"].tolist() == ["M2701"]
    assert failures.to_dict("records") == [
        {"symbol": "RM2701", "error": "TimeoutError: source timeout"}
    ]
