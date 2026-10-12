from datetime import date
import importlib.util
from pathlib import Path

import pandas as pd

from agri_research_agent.automation.positions_scheduled_collection import domestic_window

ROOT = Path(__file__).resolve().parents[2]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "04_scripts" / filename)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value


def test_direct_calculation_matches_full_formula_and_preserves_unrelated_history_bytes(tmp_path):
    calculator = load("direct_calc_fixture", "calculate_historical_spreads.py")
    server = load("direct_update_fixture", "server_update_spreads.py")
    config = pd.DataFrame([dict(spread_name="M01-M05", spread_group="meal", enabled=True, formula="leg1-leg2",
        leg1_instrument="M", leg1_month=1, leg2_instrument="M", leg2_month=5,
        window_start_month=10, window_start_day=1, window_end_month=4, window_end_day=30)])
    def price(day, month, amount):
        year = int(day[:4]) + 1
        return dict(date=pd.Timestamp(day), instrument="M", delivery_month=month, price=amount,
            source_file="akshare_futures_zh_daily_sina", source_column=f"M{year%100:02d}{month:02d}:close",
            status="success", error="", updated_at="2026-10-01T00:00:00")
    previous = pd.DataFrame([price("2025-10-08", 1, 3000), price("2025-10-08", 5, 2800),
        price("2026-10-08", 1, 3380), price("2026-10-08", 5, 3300)])
    old, failure = calculator.calculate_one_spread(config.iloc[0], previous, "unchanged-history")
    assert failure is None
    current = pd.concat([previous, pd.DataFrame([price("2026-10-09", 1, 3400), price("2026-10-09", 5, 3320)])], ignore_index=True)
    for name, frame, sheet in [("old.xlsx", previous, "price_long"), ("new.xlsx", current, "price_long"),
                               ("config.xlsx", config, "spread_config"), ("spreads.xlsx", old, "spread_long")]:
        with pd.ExcelWriter(tmp_path / name) as writer: frame.to_excel(writer, sheet_name=sheet, index=False)
    old.to_parquet(tmp_path / "old.parquet", index=False); old.to_parquet(tmp_path / "output.parquet", index=False)
    args = dict(price_baseline=tmp_path / "old.xlsx", price_candidate=tmp_path / "new.xlsx",
        spread_baseline=tmp_path / "old.parquet", spread_output=tmp_path / "output.parquet",
        spread_workbook=tmp_path / "spreads.xlsx", config_file=tmp_path / "config.xlsx")
    before = (tmp_path / "old.parquet").read_bytes()
    status, drift, affected = server.apply_direct_incremental_materialization(**args)
    actual = pd.read_parquet(tmp_path / "output.parquet")
    pd.testing.assert_frame_equal(actual.iloc[:len(old)].reset_index(drop=True), old.reset_index(drop=True))
    assert len(affected) == status["calculated_spread_rows"] == 1 and not drift
    assert not status["full_recompute_performed"] and actual.iloc[-1].spread_value == 80
    assert (tmp_path / "old.parquet").read_bytes() == before
    output_before = (tmp_path / "output.parquet").read_bytes()
    args["price_baseline"] = args["price_candidate"]
    status, _, affected = server.apply_direct_incremental_materialization(**args)
    assert not affected and (tmp_path / "output.parquet").read_bytes() == output_before


def test_contract_gap_longer_than_seven_days_is_planned_from_its_own_success_date():
    updater = load("long_gap_fixture", "update_price_long_from_akshare.py")
    plan = updater.plan_contract_refreshes(pd.DataFrame([dict(symbol="M2701")]),
        {"M2701": date(2026, 9, 1)}, target_date=date(2026, 10, 9))
    assert plan.iloc[0].missing_gap_start == date(2026, 9, 2)
    assert plan.iloc[0].refresh_start == date(2026, 8, 30)


def test_domestic_catchup_uses_published_date_and_bounded_batches_without_advancing_on_failure():
    current = {"2026-09-29"}
    assert domestic_window(current, date(2026, 10, 9)) == (date(2026, 9, 30), date(2026, 10, 9))
    assert domestic_window(current, date(2026, 10, 9)) == domestic_window(current, date(2026, 10, 9))
    start, end = domestic_window({"2026-01-01"}, date(2026, 10, 9))
    assert start == date(2026, 1, 2) and (end - start).days == 30
