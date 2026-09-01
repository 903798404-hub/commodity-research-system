from __future__ import annotations

import importlib.util
import argparse
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from agri_research_agent.data_sources.tankan.domestic_spread import (
    DomesticSpreadSourceError,
    build_price_long_rows,
    normalize_rows,
    required_symbols,
)


ROOT = Path(__file__).resolve().parents[3]
PRODUCTS = ("豆粕", "菜籽粕", "豆油", "菜籽油", "棕榈油")
MONTHS = ("01", "05", "09")


def source_rows(day: date = date(2026, 8, 21)) -> list[dict[str, object]]:
    return [
        {
            "trade_date": day,
            "product_name": product,
            "contract": month,
            "close_price": float(3000 + index),
            "updated_at": "2026-08-21T18:00:00+08:00",
        }
        for index, (product, month) in enumerate(
            (pair for product in PRODUCTS for pair in ((product, item) for item in MONTHS))
        )
    ]


def load_script(relative: str, name: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_15_tankan_contracts_normalize_to_actual_business_symbols() -> None:
    normalized = normalize_rows(source_rows(), season="2026/2027")
    assert len(normalized) == 15
    assert set(normalized["symbol"]) == set(required_symbols("2026/2027"))
    assert set(normalized["price_field"]) == {"close_price"}
    assert set(normalized["exchange"]) == {"DCE", "CZCE"}


def test_missing_required_contract_fails_closed() -> None:
    with pytest.raises(DomesticSpreadSourceError, match="15-contract completeness"):
        normalize_rows(source_rows()[:-1], season="2026/2027")


def test_wrong_price_field_cannot_be_silently_accepted() -> None:
    rows = source_rows()
    rows[0]["settlement"] = rows[0].pop("close_price")
    with pytest.raises(DomesticSpreadSourceError, match="row schema"):
        normalize_rows(rows, season="2026/2027")


def test_weekend_and_duplicate_source_dates_fail_closed() -> None:
    with pytest.raises(DomesticSpreadSourceError, match="weekend"):
        normalize_rows(source_rows(date(2026, 8, 22)), season="2026/2027")
    rows = source_rows()
    rows.append(dict(rows[0]))
    with pytest.raises(DomesticSpreadSourceError, match="duplicate"):
        normalize_rows(rows, season="2026/2027")


def test_incremental_append_is_no_change_for_repeated_business_data() -> None:
    updater = load_script("04_scripts/update_price_long_from_tankan.py", "tankan_updater")
    existing = build_price_long_rows(
        normalize_rows(source_rows(), season="2026/2027"),
        existing_columns=[
            "date", "instrument", "instrument_cn", "delivery_month", "price",
            "source_column", "source_file", "updated_at", "status", "error",
        ],
        updated_at="first-run",
    )
    combined, appended = updater.append_new_prices(
        existing, normalize_rows(source_rows(), season="2026/2027"), season="2026/2027"
    )
    assert appended.empty
    pd.testing.assert_frame_equal(combined, existing)


def test_incremental_append_updates_without_akshare() -> None:
    updater = load_script("04_scripts/update_price_long_from_tankan.py", "tankan_append")
    columns = [
        "date", "instrument", "instrument_cn", "delivery_month", "price",
        "source_column", "source_file", "updated_at", "status", "error",
    ]
    existing = build_price_long_rows(
        normalize_rows(source_rows(date(2026, 8, 20)), season="2026/2027"),
        existing_columns=columns,
        updated_at="first-run",
    )
    combined, appended = updater.append_new_prices(
        existing,
        normalize_rows(source_rows(date(2026, 8, 21)), season="2026/2027"),
        season="2026/2027",
    )
    assert len(appended) == 15
    assert len(combined) == 30
    assert set(appended["source_file"]) == {"tankan.market.futures_spread"}


def test_spread_formula_is_unchanged_for_tankan_prices() -> None:
    calculator = load_script("04_scripts/calculate_historical_spreads.py", "spread_calculator")
    prices = pd.DataFrame(
        [
            {"date": pd.Timestamp("2026-08-21"), "instrument": "M", "delivery_month": 9, "price": 3100.0, "status": "success", "error": ""},
            {"date": pd.Timestamp("2026-08-21"), "instrument": "RM", "delivery_month": 9, "price": 2600.0, "status": "success", "error": ""},
        ]
    )
    config = pd.Series(
        {
            "spread_name": "M09-RM09", "spread_group": "test",
            "leg1_instrument": "M", "leg1_month": 9,
            "leg2_instrument": "RM", "leg2_month": 9,
            "formula": "leg1-leg2", "window_start_month": 5,
            "window_start_day": 1, "window_end_month": 9, "window_end_day": 30,
        }
    )
    result, failure = calculator.calculate_one_spread(config, prices, "ignored")
    assert failure is None
    assert result.iloc[0]["leg1_price"] - result.iloc[0]["leg2_price"] == result.iloc[0]["spread_value"] == 500.0


def test_unified_producer_invokes_tankan_not_akshare(monkeypatch, tmp_path: Path) -> None:
    refresh = load_script("04_scripts/refresh_public_data.py", "refresh_public_data_e2")
    seen: list[str] = []
    artifact = tmp_path / "01_data" / "historical_spread_database.parquet"
    artifact.parent.mkdir()
    artifact.write_bytes(b"isolated-test-artifact")

    def run(command, **_):
        seen.extend(str(value) for value in command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(refresh.subprocess, "run", run)
    monkeypatch.setattr(refresh, "ROOT", tmp_path)
    result = refresh._refresh_domestic_spread_artifact(
        tankan_secret_file=Path("safe-secret-file"), end_date=date(2026, 8, 24)
    )
    assert result["domestic-spread"].name == "historical_spread_database.parquet"
    assert "--update-from-tankan" in seen
    assert "--update-from-akshare" not in seen


def test_tankan_update_failure_restores_all_domestic_spread_files(
    monkeypatch, tmp_path: Path
) -> None:
    server = load_script("04_scripts/server_update_spreads.py", "server_update_spreads_e2")
    data = tmp_path / "01_data"
    config = tmp_path / "02_configs"
    data.mkdir()
    config.mkdir()
    price = data / "historical_price_long.xlsx"
    database = data / "historical_spread_database.xlsx"
    parquet = data / "historical_spread_database.parquet"
    with pd.ExcelWriter(price, engine="openpyxl") as writer:
        pd.DataFrame({"date": [pd.Timestamp("2026-08-21")]}).to_excel(
            writer, sheet_name="price_long", index=False
        )
    spread = pd.DataFrame(
        {"date": [pd.Timestamp("2026-08-21")], "status": ["success"]}
    )
    with pd.ExcelWriter(database, engine="openpyxl") as writer:
        spread.to_excel(writer, sheet_name="spread_long", index=False)
    spread.to_parquet(parquet, index=False)
    (config / "historical_spread_config.xlsx").write_bytes(b"required")
    originals = {path: path.read_bytes() for path in (price, database, parquet)}

    monkeypatch.setattr(server, "project_root", lambda: tmp_path)
    monkeypatch.setattr(
        server,
        "parse_args",
        lambda: argparse.Namespace(
            update_from_akshare=False,
            update_from_tankan=True,
            recalculate_from_existing_price_long=False,
            dry_run=False,
            end_date=date(2026, 8, 24),
            tankan_secret_file=Path("unused"),
            lock_timeout=1,
        ),
    )

    def fail_update(_args, _root, _logger):
        price.write_bytes(b"partial update")
        return SimpleNamespace(returncode=1, stdout="", stderr="failed")

    monkeypatch.setattr(server, "run_script_args", fail_update)
    assert server.main() == 1
    assert {path: path.read_bytes() for path in originals} == originals
