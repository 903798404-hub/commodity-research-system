from __future__ import annotations

import importlib.util
import argparse
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from agri_research_agent.application.domestic_spreads import load_domestic_spread_status


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "08_tests" / "fixtures" / "domestic_spread" / "late_arrival_2026_09_21.csv"
CONFIG = ROOT / "02_configs" / "historical_spread_config.xlsx"
PRICE_COLUMNS = [
    "date", "instrument", "instrument_cn", "delivery_month", "price",
    "source_file", "source_column", "updated_at", "status", "error",
]


def _load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def updater():
    return _load(
        "04_scripts/update_price_long_from_akshare.py",
        "late_arrival_update_price_long_from_akshare",
    )


def _observations(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    records = []
    for symbol, business_date, price in rows:
        instrument = symbol[:-4]
        records.append(
            {
                "date": pd.Timestamp(business_date),
                "instrument": instrument,
                "delivery_month": int(symbol[-2:]),
                "symbol": symbol,
                "season": "2026/2027",
                "price": price,
                "price_field": "close",
                "source": "akshare_futures_zh_daily_sina",
            }
        )
    return pd.DataFrame(records)


def _price_rows(updater, rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    return updater.build_price_long_rows(_observations(rows), PRICE_COLUMNS)


def test_per_contract_watermark_queries_late_contract_when_global_max_is_current(
    updater, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = date(2026, 9, 21)
    candidates = pd.DataFrame(
        [
            {"instrument": "M", "delivery_month": 1, "season": "2026/2027", "symbol": "M2701"},
            {"instrument": "RM", "delivery_month": 1, "season": "2026/2027", "symbol": "RM2701"},
        ]
    )
    existing = _price_rows(
        updater,
        [("M2701", "2026-09-21", 3386), ("RM2701", "2026-09-18", 2357)],
    )

    watermarks = updater.per_contract_watermarks(existing, candidates)
    plan = updater.plan_contract_refreshes(candidates, watermarks, target_date=target)

    assert watermarks == {"M2701": target, "RM2701": date(2026, 9, 18)}
    assert plan["symbol"].tolist() == ["RM2701"]
    assert plan.iloc[0]["missing_gap_start"] == date(2026, 9, 19)

    calls: list[str] = []

    def daily(*, symbol: str) -> pd.DataFrame:
        calls.append(symbol)
        return pd.DataFrame(
            {"date": ["2026-09-18", "2026-09-21"], "close": [2357, 2341]}
        )

    monkeypatch.setattr(updater.ak, "futures_zh_daily_sina", daily)
    selected, failures, _ = updater.fetch_daily_history(plan, target_date=target)

    assert calls == ["RM2701"]
    assert failures.empty
    assert selected.loc[selected["date"].eq(pd.Timestamp(target)), "price"].tolist() == [2341]


def test_current_contracts_do_not_trigger_unnecessary_gap_queries(updater) -> None:
    target = date(2026, 9, 21)
    candidates = updater.build_active_candidates(CONFIG, target)
    existing = _price_rows(
        updater,
        [(symbol, target.isoformat(), 1000 + index) for index, symbol in enumerate(candidates["symbol"])],
    )

    plan = updater.plan_contract_refreshes(
        candidates,
        updater.per_contract_watermarks(existing, candidates),
        target_date=target,
    )

    assert len(candidates) == 10
    assert plan.empty


def test_endpoint_success_is_separate_from_target_date_completeness(
    updater, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = date(2026, 9, 21)
    candidates = pd.DataFrame(
        [{"instrument": "RM", "delivery_month": 1, "season": "2026/2027", "symbol": "RM2701"}]
    )
    existing = _price_rows(updater, [("RM2701", "2026-09-18", 2357)])
    plan = updater.plan_contract_refreshes(
        candidates,
        updater.per_contract_watermarks(existing, candidates),
        target_date=target,
    )
    monkeypatch.setattr(
        updater.ak,
        "futures_zh_daily_sina",
        lambda **_: pd.DataFrame({"date": ["2026-09-18"], "close": [2357]}),
    )

    selected, endpoint_failures, _ = updater.fetch_daily_history(plan, target_date=target)
    combined, _, _ = updater.upsert_daily_closes(
        existing,
        updater.build_price_long_rows(selected, PRICE_COLUMNS),
    )
    present, missing = updater.target_date_completeness(
        combined, {"RM2701"}, target_date=target
    )

    assert endpoint_failures.empty
    assert present == set()
    assert missing == {"RM2701"}


def test_target_daily_close_is_complete_but_spot_current_price_is_not(updater) -> None:
    target = date(2026, 9, 21)
    daily = _price_rows(updater, [("RM2701", target.isoformat(), 2341)])
    present, missing = updater.target_date_completeness(
        daily, {"RM2701"}, target_date=target
    )
    assert present == {"RM2701"}
    assert not missing

    spot = daily.copy()
    spot["source_file"] = "akshare_futures_zh_spot"
    spot["source_column"] = "RM2701:current_price"
    present, missing = updater.target_date_completeness(
        spot, {"RM2701"}, target_date=target
    )
    assert present == set()
    assert missing == {"RM2701"}


def test_late_close_on_next_run_is_inserted(updater) -> None:
    target = date(2026, 9, 21)
    existing = _price_rows(updater, [("RM2701", "2026-09-18", 2357)])
    late = _price_rows(updater, [("RM2701", target.isoformat(), 2341)])

    combined, inserted, unchanged = updater.upsert_daily_closes(existing, late)
    present, missing = updater.target_date_completeness(
        combined, {"RM2701"}, target_date=target
    )

    assert len(inserted) == 1
    assert unchanged == 0
    assert present == {"RM2701"}
    assert not missing


def test_identical_close_is_idempotent_no_change(updater) -> None:
    existing = _price_rows(updater, [("RM2701", "2026-09-21", 2341)])
    combined, inserted, unchanged = updater.upsert_daily_closes(existing, existing.copy())

    pd.testing.assert_frame_equal(combined, existing)
    assert inserted.empty
    assert unchanged == 1


def test_conflicting_historical_close_is_never_silently_overwritten(updater) -> None:
    existing = _price_rows(updater, [("RM2701", "2026-09-21", 2341)])
    conflicting = _price_rows(updater, [("RM2701", "2026-09-21", 2342)])

    with pytest.raises(updater.HistoricalDailyCloseConflict):
        updater.upsert_daily_closes(existing, conflicting)


def test_incident_replay_selects_four_late_arrivals_and_restores_status(
    updater, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = date(2026, 9, 21)
    fixture = pd.read_csv(FIXTURE)
    candidates = updater.build_active_candidates(CONFIG, target)
    late_symbols = {"RM2701", "Y2701", "OI2701", "P2701"}
    current = fixture.loc[~fixture["symbol"].isin(late_symbols)]
    old = pd.DataFrame(
        [
            ("RM2701", "2026-09-18", 2357),
            ("Y2701", "2026-09-18", 8875),
            ("OI2701", "2026-09-18", 10111),
            ("P2701", "2026-09-18", 9855),
        ],
        columns=["symbol", "date", "close"],
    )
    existing_rows = [
        (row.symbol, row.date, float(row.close))
        for row in pd.concat([current[["symbol", "date", "close"]], old]).itertuples(index=False)
    ]
    existing = _price_rows(updater, existing_rows)
    plan = updater.plan_contract_refreshes(
        candidates,
        updater.per_contract_watermarks(existing, candidates),
        target_date=target,
    )
    assert set(plan["symbol"]) == late_symbols

    by_symbol = fixture.set_index("symbol")

    def daily(*, symbol: str) -> pd.DataFrame:
        row = by_symbol.loc[symbol]
        return pd.DataFrame({"date": [row["date"]], "close": [row["close"]]})

    monkeypatch.setattr(updater.ak, "futures_zh_daily_sina", daily)
    selected, failures, _ = updater.fetch_daily_history(plan, target_date=target)
    assert failures.empty
    target_selected = selected.loc[selected["date"].eq(pd.Timestamp(target))]
    assert dict(zip(target_selected["symbol"], target_selected["price"], strict=True)) == {
        "RM2701": 2341,
        "Y2701": 8895,
        "OI2701": 10205,
        "P2701": 9816,
    }

    incoming = updater.build_price_long_rows(selected, PRICE_COLUMNS)
    combined, inserted, _ = updater.upsert_daily_closes(existing, incoming)
    assert len(inserted.loc[inserted["date"].eq(pd.Timestamp(target))]) == 4

    calculator = _load(
        "04_scripts/calculate_historical_spreads.py",
        "late_arrival_calculate_historical_spreads",
    )
    config = pd.read_excel(CONFIG, sheet_name="spread_config")
    frames = []
    for _, rule in config.iterrows():
        frame, _ = calculator.calculate_one_spread(rule, combined, "fixture")
        if not frame.empty:
            frames.append(frame)
    spreads = pd.concat(frames, ignore_index=True)
    target_spreads = spreads.loc[spreads["date"].eq(pd.Timestamp(target))]
    recovered = target_spreads.loc[
        target_spreads["spread_name"].isin(["RM 1-5", "Y 1-5", "OI 1-5", "P 1-5"])
    ]
    assert set(recovered["status"]) == {"success"}
    assert len(recovered) == 4

    status = load_domestic_spread_status(target_spreads, CONFIG)
    assert status.required_contracts == 10
    assert status.success_contracts == 10
    assert status.failure_contracts == 0
    assert status.status == "success"


def test_business_end_date_parser_is_strict_and_rejects_future(updater) -> None:
    assert updater.parse_business_end_date("2026-09-21") == date(2026, 9, 21)
    for value in ("", "20260921", "2026-09-31"):
        with pytest.raises(argparse.ArgumentTypeError):
            updater.parse_business_end_date(value)
    future = date.today() + date.resolution
    with pytest.raises(argparse.ArgumentTypeError, match="future"):
        updater.parse_business_end_date(future.isoformat())


def test_server_business_end_date_parser_is_strict_and_rejects_future() -> None:
    server = _load(
        "04_scripts/server_update_spreads.py",
        "server_business_end_date_parser",
    )
    assert server.parse_business_end_date("2026-09-21") == date(2026, 9, 21)
    for value in ("", "20260921", "2026-09-31"):
        with pytest.raises(argparse.ArgumentTypeError):
            server.parse_business_end_date(value)
    future = date.today() + date.resolution
    with pytest.raises(argparse.ArgumentTypeError, match="future"):
        server.parse_business_end_date(future.isoformat())


def test_server_rejects_partial_target_data_even_when_endpoint_job_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _load(
        "04_scripts/server_update_spreads.py",
        "late_arrival_server_update_spreads",
    )
    data = tmp_path / "01_data"
    config_dir = tmp_path / "02_configs"
    data.mkdir()
    config_dir.mkdir()
    with pd.ExcelWriter(data / "historical_price_long.xlsx", engine="openpyxl") as writer:
        pd.DataFrame({"date": [pd.Timestamp("2026-09-20")]}).to_excel(
            writer, sheet_name="price_long", index=False
        )
    baseline = pd.DataFrame(
        {"date": [pd.Timestamp("2026-09-20")], "status": ["success"]}
    )
    with pd.ExcelWriter(data / "historical_spread_database.xlsx", engine="openpyxl") as writer:
        baseline.to_excel(writer, sheet_name="spread_long", index=False)
    baseline.to_parquet(data / "historical_spread_database.parquet", index=False)
    (config_dir / "historical_spread_config.xlsx").write_bytes(b"fixture")
    args = argparse.Namespace(
        update_from_akshare=True,
        update_from_tankan=False,
        recalculate_from_existing_price_long=False,
        dry_run=True,
        end_date=date(2026, 9, 21),
        refresh_start_date=None,
        historical_publication_mode="normal",
        historical_allowed_key=[],
        tankan_secret_file=Path("unused"),
        lock_timeout=1,
    )
    monkeypatch.setattr(server, "project_root", lambda: tmp_path)
    monkeypatch.setattr(server, "parse_args", lambda: args)

    def run(step, _root, _logger):
        assert step[:3] == [
            "update_price_long_from_akshare.py",
            "--target-business-date",
            "2026-09-21",
        ]
        result_path = Path(step[step.index("--result-json") + 1])
        result_path.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "job_execution_status": "SUCCESS",
                    "target_business_date": "2026-09-21",
                    "requested_end_date": "2026-09-21",
                    "effective_end_date": "2026-09-21",
                    "target_date_data_completeness": "PARTIAL",
                    "required_contracts": 10,
                    "success_contracts": 6,
                    "failure_contracts": 4,
                    "failed_contracts": ["RM2701", "Y2701", "OI2701", "P2701"],
                    "target_required_contract_keys": [],
                    "target_present_contract_keys": [],
                    "target_missing_contract_keys": ["RM2701", "Y2701", "OI2701", "P2701"],
                    "latest_date": "2026-09-21",
                    "price_long_written": False,
                    "error_message": "",
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(server, "run_script_args", run)
    assert server.main() == 1
    status = json.loads((data / "update_status.json").read_text(encoding="utf-8"))
    assert status["job_execution_status"] == "SUCCESS"
    assert status["target_date_data_completeness"] == "PARTIAL"
    assert status["success_contracts"] == 6
    assert status["required_contracts"] == 10
    assert status["requested_end_date"] == "2026-09-21"
    assert status["effective_end_date"] == "2026-09-21"
