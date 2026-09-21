from __future__ import annotations

import ast
import argparse
import importlib.util
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from agri_research_agent.pipelines.domestic_spread_integrity import (
    HistoricalMutationPolicy,
    HistoricalPublicationBlocked,
    HistoricalPublicationMode,
    select_canonical_historical_prices,
    validate_historical_publication,
)


ROOT = Path(__file__).resolve().parents[2]
INCIDENT = (
    ROOT
    / "08_tests"
    / "fixtures"
    / "domestic_spread"
    / "incident_2026_09_08_changed_records.csv"
)


def _price(
    value: float,
    *,
    source: str,
    field: str,
    business_date: str = "2026-07-16",
) -> dict[str, object]:
    return {
        "date": pd.Timestamp(business_date),
        "instrument": "M",
        "delivery_month": 1,
        "price": value,
        "source_file": source,
        "source_column": f"M2701:{field}",
        "updated_at": "2026-09-08T00:00:00",
        "status": "success",
        "error": "",
    }


def _spread(value: float, *, business_date: str, updated_at: str = "one") -> dict[str, object]:
    return {
        "date": pd.Timestamp(business_date),
        "spread_name": "M 1-5",
        "season": "2026/2027",
        "leg1_price": 3137.0,
        "leg2_price": 3137.0 - value,
        "spread_value": value,
        "status": "success",
        "error": "",
        "updated_at": updated_at,
    }


def _normal_policy() -> HistoricalMutationPolicy:
    return HistoricalMutationPolicy(
        HistoricalPublicationMode.NORMAL,
        date(2026, 8, 15),
        date(2026, 9, 8),
    )


def _load_script(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_daily_close_wins_and_spot_only_cannot_impersonate_history() -> None:
    daily = _price(
        3137.0, source="akshare_futures_zh_daily_sina", field="close"
    )
    spot = _price(
        3144.0, source="akshare_futures_zh_spot", field="current_price"
    )

    canonical = select_canonical_historical_prices(pd.DataFrame([daily, spot]))
    assert canonical["price"].tolist() == [3137.0]
    assert select_canonical_historical_prices(pd.DataFrame([spot])).empty

    later_daily = dict(daily, date=pd.Timestamp("2026-07-17"), price=3139.0)
    selected = select_canonical_historical_prices(pd.DataFrame([spot, later_daily]))
    assert selected[["date", "price"]].to_records(index=False).tolist() == [
        (pd.Timestamp("2026-07-17"), 3139.0)
    ]

    legacy_base = _price(
        3000.0,
        source=r"D:\market-data\data\manual_history\historical_price_base.xlsx",
        field="legacy-column",
    )
    assert select_canonical_historical_prices(
        pd.DataFrame([legacy_base])
    )["price"].tolist() == [3000.0]


def test_historical_calculator_uses_daily_close_not_same_day_spot() -> None:
    calculator = _load_script(
        "04_scripts/calculate_historical_spreads.py", "integrity_spread_calculator"
    )
    rows = [
        _price(3137.0, source="akshare_futures_zh_daily_sina", field="close"),
        _price(3144.0, source="akshare_futures_zh_spot", field="current_price"),
        {
            **_price(
                2835.0, source="akshare_futures_zh_daily_sina", field="close"
            ),
            "delivery_month": 5,
            "source_column": "M2705:close",
        },
        {
            **_price(
                2837.0, source="akshare_futures_zh_spot", field="current_price"
            ),
            "delivery_month": 5,
            "source_column": "M2705:current_price",
        },
    ]
    config = pd.Series(
        {
            "spread_name": "M 1-5",
            "spread_group": "M",
            "leg1_instrument": "M",
            "leg1_month": 1,
            "leg2_instrument": "M",
            "leg2_month": 5,
            "formula": "leg1-leg2",
            "window_start_month": 6,
            "window_start_day": 1,
            "window_end_month": 1,
            "window_end_day": 10,
        }
    )
    result, failure = calculator.calculate_one_spread(
        config, pd.DataFrame(rows), "ignored"
    )
    assert failure is None
    assert result.iloc[0]["leg1_price"] == 3137.0
    assert result.iloc[0]["leg2_price"] == 2835.0
    assert result.iloc[0]["spread_value"] == 302.0


def test_normal_refresh_allows_in_range_business_change_and_ignores_metadata() -> None:
    before = pd.DataFrame([_spread(100.0, business_date="2026-08-20")])
    after = pd.DataFrame([
        _spread(101.0, business_date="2026-08-20", updated_at="two")
    ])
    report = validate_historical_publication(before, after, _normal_policy())
    assert report.publication_allowed is True
    assert len(report.changed_keys) == 1

    metadata_only = before.copy()
    metadata_only["updated_at"] = "deterministic-new-time"
    report = validate_historical_publication(before, metadata_only, _normal_policy())
    assert report.changed_keys == ()


def test_normal_refresh_blocks_out_of_range_change_but_exact_reconciliation_allows_it() -> None:
    before = pd.DataFrame([_spread(302.0, business_date="2026-07-16")])
    after = pd.DataFrame([_spread(307.0, business_date="2026-07-16")])
    with pytest.raises(HistoricalPublicationBlocked) as blocked:
        validate_historical_publication(before, after, _normal_policy())
    assert blocked.value.report.blocked_keys == (
        ("2026-07-16", "M 1-5", "2026/2027"),
    )

    policy = HistoricalMutationPolicy(
        HistoricalPublicationMode.HISTORICAL_RECONCILIATION,
        date(2026, 8, 15),
        date(2026, 9, 8),
        frozenset({("2026-07-16", "M 1-5", "2026/2027")}),
    )
    assert validate_historical_publication(before, after, policy).publication_allowed


def test_incident_replay_blocks_all_18_rows_and_retains_15_daily_closes() -> None:
    audit = pd.read_csv(INCIDENT)
    assert len(audit) == 18
    before = pd.DataFrame(
        {
            "date": pd.to_datetime(audit["business_date"]),
            "spread_name": audit["spread_identity"],
            "season": audit["season"],
            "leg1_price": audit["old_leg1_price"],
            "leg2_price": audit["old_leg2_price"],
            "spread_value": audit["old_value"],
            "status": "success",
            "error": "",
            "updated_at": audit["old_updated_at"],
        }
    )
    candidate = before.copy()
    candidate["leg1_price"] = audit["new_leg1_price"]
    candidate["leg2_price"] = audit["new_leg2_price"]
    candidate["spread_value"] = audit["new_value"]
    candidate["updated_at"] = audit["new_updated_at"]

    with pytest.raises(HistoricalPublicationBlocked) as blocked:
        validate_historical_publication(before, candidate, _normal_policy())
    assert len(blocked.value.report.changed_keys) == 18
    assert len(blocked.value.report.blocked_keys) == 18
    assert blocked.value.report.publication_allowed is False

    observations: dict[tuple[str, int], tuple[float, float, str]] = {}
    for encoded in audit["legs"]:
        for leg in ast.literal_eval(encoded):
            key = (str(leg["instrument"]), int(leg["delivery_month"]))
            observations[key] = (
                float(leg["old_daily_close"]),
                float(leg["new_stored_price"]),
                str(leg["symbol"]),
            )
    assert len(observations) == 15
    rows = []
    for (instrument, month), (daily_close, spot, symbol) in observations.items():
        common = {
            "date": pd.Timestamp("2026-07-16"),
            "instrument": instrument,
            "delivery_month": month,
            "status": "success",
            "error": "",
        }
        rows.extend(
            [
                {
                    **common,
                    "price": daily_close,
                    "source_file": "akshare_futures_zh_daily_sina",
                    "source_column": f"{symbol}:close",
                },
                {
                    **common,
                    "price": spot,
                    "source_file": "akshare_futures_zh_spot",
                    "source_column": f"{symbol}:current_price",
                },
            ]
        )
    selected = select_canonical_historical_prices(pd.DataFrame(rows))
    assert len(selected) == 15
    actual = {
        (row.instrument, int(row.delivery_month)): float(row.price)
        for row in selected.itertuples(index=False)
    }
    assert actual == {
        key: daily_close for key, (daily_close, _spot, _symbol) in observations.items()
    }


def test_server_transaction_restores_candidate_when_guard_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _load_script(
        "04_scripts/server_update_spreads.py", "integrity_server_update_spreads"
    )
    data = tmp_path / "01_data"
    config_dir = tmp_path / "02_configs"
    data.mkdir()
    config_dir.mkdir()
    price = data / "historical_price_long.xlsx"
    excel = data / "historical_spread_database.xlsx"
    parquet = data / "historical_spread_database.parquet"
    baseline = pd.DataFrame([_spread(302.0, business_date="2026-07-16")])
    with pd.ExcelWriter(price, engine="openpyxl") as writer:
        pd.DataFrame({"date": [pd.Timestamp("2026-08-14")]}).to_excel(
            writer, sheet_name="price_long", index=False
        )
    with pd.ExcelWriter(excel, engine="openpyxl") as writer:
        baseline.to_excel(writer, sheet_name="spread_long", index=False)
    baseline.to_parquet(parquet, index=False)
    (config_dir / "historical_spread_config.xlsx").write_bytes(b"fixture")
    originals = {path: path.read_bytes() for path in (price, excel, parquet)}
    args = argparse.Namespace(
        update_from_akshare=False,
        update_from_tankan=True,
        recalculate_from_existing_price_long=False,
        dry_run=False,
        end_date=date(2026, 9, 8),
        refresh_start_date=date(2026, 8, 15),
        historical_publication_mode="normal",
        historical_allowed_key=[],
        tankan_secret_file=Path("unused"),
        lock_timeout=1,
    )
    monkeypatch.setattr(server, "project_root", lambda: tmp_path)
    monkeypatch.setattr(server, "parse_args", lambda: args)

    def run(step, _root, _logger):
        if step[0] == "update_price_long_from_tankan.py":
            result_path = Path(step[step.index("--result-json") + 1])
            result_path.write_text(
                json.dumps(
                    {
                        "success_contracts": 15,
                        "failure_contracts": 0,
                        "required_contracts": 15,
                        "failed_contracts": [],
                        "latest_date": "2026-09-08",
                        "price_long_written": True,
                    }
                ),
                encoding="utf-8",
            )
        else:
            changed = pd.DataFrame([_spread(307.0, business_date="2026-07-16")])
            with pd.ExcelWriter(excel, engine="openpyxl") as writer:
                changed.to_excel(writer, sheet_name="spread_long", index=False)
            changed.to_parquet(parquet, index=False)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(server, "run_script_args", run)
    assert server.main() == 1
    assert {path: path.read_bytes() for path in originals} == originals
    status = json.loads((data / "update_status.json").read_text(encoding="utf-8"))
    assert status["historical_diff_guard"] == "FAIL"
    assert status["historical_blocked_keys"] == [
        "2026-07-16|M 1-5|2026/2027"
    ]
