from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.application.domestic_spreads import (
    load_domestic_spread_status,
)
from agri_research_agent.pipelines.domestic_spread_integrity import (
    HistoricalMutationPolicy,
    HistoricalPublicationBlocked,
    HistoricalPublicationMode,
    changed_daily_close_keys,
    derive_affected_spread_keys,
    historical_changed_keys,
    materialize_affected_spreads,
    validate_historical_publication,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "02_configs" / "historical_spread_config.xlsx"
INCIDENT = (
    ROOT
    / "08_tests"
    / "fixtures"
    / "domestic_spread"
    / "incident_2026_09_08_changed_records.csv"
)


def _load_calculator():
    path = ROOT / "04_scripts" / "calculate_historical_spreads.py"
    spec = importlib.util.spec_from_file_location("bounded_spread_calculator", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_server():
    path = ROOT / "04_scripts" / "server_update_spreads.py"
    spec = importlib.util.spec_from_file_location("bounded_spread_server", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _price(symbol: str, business_date: str, value: float) -> dict[str, object]:
    return {
        "date": pd.Timestamp(business_date),
        "instrument": symbol[:-4],
        "instrument_cn": "fixture",
        "delivery_month": int(symbol[-2:]),
        "price": value,
        "source_column": f"{symbol}:close",
        "source_file": "akshare_futures_zh_daily_sina",
        "updated_at": "2026-09-22T00:00:00+08:00",
        "status": "success",
        "error": "",
    }


def test_october_may_september_calculation_rejects_expired_year():
    calculator = _load_calculator()
    config = pd.read_excel(CONFIG, sheet_name="spread_config")
    rule = config.loc[config["spread_name"].eq("M 5-9")].iloc[0]
    correct, error = calculator.calculate_one_spread(
        rule, pd.DataFrame([_price("M2705", "2026-10-08", 3200),
                            _price("M2709", "2026-10-08", 3100)]), "fixture",
    )
    assert error is None
    assert correct.iloc[0]["status"] == "success"
    assert correct.iloc[0]["spread_value"] == 100
    assert correct.iloc[0]["leg2_contract"] == "M2709"
    wrong, error = calculator.calculate_one_spread(
        rule, pd.DataFrame([_price("M2705", "2026-10-08", 3200),
                            _price("M2609", "2026-10-08", 3100)]), "fixture",
    )
    assert error is None
    assert wrong.iloc[0]["status"] == "wrong_contract"
    assert pd.isna(wrong.iloc[0]["spread_value"])
    keys = derive_affected_spread_keys(config, [("M2709", "2026-10-08")])
    assert ("2026-10-08", "M 5-9", "2026/2027") in keys
    expired = derive_affected_spread_keys(config, [("M2609", "2026-10-08")])
    assert ("2026-10-08", "M 5-9", "2026/2027") not in expired


def _calculate(prices: pd.DataFrame) -> pd.DataFrame:
    calculator = _load_calculator()
    config = pd.read_excel(CONFIG, sheet_name="spread_config")
    frames = []
    for _, rule in config.loc[config["enabled"]].iterrows():
        frame, _failure = calculator.calculate_one_spread(rule, prices, "fixture")
        if not frame.empty:
            frames.append(frame)
    return pd.concat(frames, ignore_index=True).loc[:, calculator.SPREAD_COLUMNS]


def _incident_row(row: object, *, use_new: bool) -> dict[str, object]:
    prefix = "new" if use_new else "old"
    return {
        "date": pd.Timestamp(getattr(row, "business_date")),
        "spread_group": getattr(row, "series"),
        "spread_name": getattr(row, "spread_identity"),
        "leg1_instrument": "M",
        "leg1_month": 1,
        "leg1_contract": "M2701",
        "leg1_price": getattr(row, f"{prefix}_leg1_price"),
        "leg2_instrument": "M",
        "leg2_month": 5,
        "leg2_contract": "M2705",
        "leg2_price": getattr(row, f"{prefix}_leg2_price"),
        "spread_value": getattr(row, f"{prefix}_value"),
        "season": getattr(row, "season"),
        "calendar_offset": 45,
        "month_day": "07-16",
        "status": "success",
        "error": "",
        "updated_at": getattr(row, f"{prefix}_updated_at"),
    }


def _incident_replay() -> tuple[
    pd.DataFrame, pd.DataFrame, pd.DataFrame, frozenset[tuple[str, str]]
]:
    target = "2026-09-21"
    prior = "2026-09-18"
    current_values = {
        "M2701": 3386,
        "M2705": 3013,
        "RM2705": 2355,
        "Y2705": 8731,
        "OI2705": 9954,
        "P2705": 10257,
    }
    late_values = {
        "RM2701": 2341,
        "Y2701": 8895,
        "OI2701": 10205,
        "P2701": 9816,
    }
    old_values = {
        "RM2701": 2357,
        "Y2701": 8875,
        "OI2701": 10111,
        "P2701": 9855,
    }
    base_prices = pd.DataFrame(
        [_price(symbol, target, value) for symbol, value in current_values.items()]
        + [_price(symbol, prior, value) for symbol, value in old_values.items()]
    )
    candidate_prices = pd.concat(
        [
            base_prices,
            pd.DataFrame(
                [_price(symbol, target, value) for symbol, value in late_values.items()]
            ),
        ],
        ignore_index=True,
    )
    underlying = changed_daily_close_keys(base_prices, candidate_prices)
    baseline = _calculate(base_prices)
    full = _calculate(candidate_prices)

    incident = pd.read_csv(INCIDENT)
    baseline = pd.concat(
        [
            baseline,
            pd.DataFrame(
                [_incident_row(row, use_new=False) for row in incident.itertuples()]
            ).loc[:, baseline.columns],
        ],
        ignore_index=True,
    )
    full = pd.concat(
        [
            full,
            pd.DataFrame(
                [_incident_row(row, use_new=True) for row in incident.itertuples()]
            ).loc[:, full.columns],
        ],
        ignore_index=True,
    )
    return base_prices, baseline, full, underlying


def test_dependency_closure_uses_exact_config_contracts() -> None:
    config = pd.read_excel(CONFIG, sheet_name="spread_config")
    affected = derive_affected_spread_keys(
        config, {("RM2701", "2026-09-21")}
    )
    assert affected == {
        ("2026-09-21", "RM 1-5", "2026/2027"),
        ("2026-09-21", "M-RM 1", "2026/2027"),
    }


def test_multiple_inputs_union_and_deduplicate_shared_spreads() -> None:
    config = pd.read_excel(CONFIG, sheet_name="spread_config")
    affected = derive_affected_spread_keys(
        config,
        {
            ("Y2701", "2026-09-21"),
            ("OI2701", "2026-09-21"),
            ("P2701", "2026-09-21"),
        },
    )
    assert affected == {
        ("2026-09-21", "Y 1-5", "2026/2027"),
        ("2026-09-21", "OI 1-5", "2026/2027"),
        ("2026-09-21", "P 1-5", "2026/2027"),
        ("2026-09-21", "Y-P 1", "2026/2027"),
        ("2026-09-21", "OI-Y 1", "2026/2027"),
        ("2026-09-21", "OI-P 1", "2026/2027"),
    }


def test_changed_daily_close_rejects_mutated_legacy_identity() -> None:
    before = pd.DataFrame(
        [
            {
                **_price("M2701", "2026-09-21", 3386),
                "source_file": "historical_price_base.xlsx",
                "source_column": "M01",
            }
        ]
    )
    after = before.copy()
    after["price"] = 3387
    with pytest.raises(ValueError, match="lacks full contract identity"):
        changed_daily_close_keys(before, after)


def test_bounded_materialization_rejects_missing_calculated_dependency() -> None:
    _prices, baseline, full, underlying = _incident_replay()
    config = pd.read_excel(CONFIG, sheet_name="spread_config")
    affected = derive_affected_spread_keys(config, underlying)
    missing_key = next(iter(affected))
    full_keys = full.apply(
        lambda row: (
            pd.Timestamp(row["date"]).date().isoformat(),
            str(row["spread_name"]),
            str(row["season"]),
        ),
        axis=1,
    )
    incomplete = full.loc[full_keys.map(lambda key: key != missing_key)].copy()
    with pytest.raises(ValueError, match="omitted affected key"):
        materialize_affected_spreads(
            baseline,
            incomplete,
            underlying_keys=underlying,
            affected_keys=affected,
        )


def test_incident_replay_materializes_only_eight_target_dependencies() -> None:
    _prices, baseline, full, underlying = _incident_replay()
    config = pd.read_excel(CONFIG, sheet_name="spread_config")
    affected = derive_affected_spread_keys(config, underlying)
    assert underlying == {
        ("RM2701", "2026-09-21"),
        ("Y2701", "2026-09-21"),
        ("OI2701", "2026-09-21"),
        ("P2701", "2026-09-21"),
    }
    assert len(affected) == 8

    materialized, report = materialize_affected_spreads(
        baseline,
        full,
        underlying_keys=underlying,
        affected_keys=affected,
    )
    changed = historical_changed_keys(baseline, materialized)
    assert len(changed) == 8
    assert {key[0] for key in changed} == {"2026-09-21"}
    assert report.unrelated_rewritten_keys == ()
    assert len(historical_changed_keys(baseline, full)) == 26

    old_incident = baseline.loc[
        pd.to_datetime(baseline["date"]).eq(pd.Timestamp("2026-07-16"))
    ].sort_values(["spread_name", "season"]).reset_index(drop=True)
    preserved = materialized.loc[
        pd.to_datetime(materialized["date"]).eq(pd.Timestamp("2026-07-16"))
    ].sort_values(["spread_name", "season"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(old_incident, preserved)
    assert len(preserved) == 18

    target_rows = materialized.loc[
        pd.to_datetime(materialized["date"]).eq(pd.Timestamp("2026-09-21"))
    ]
    status = load_domestic_spread_status(target_rows, CONFIG)
    assert status.required_contracts == 10
    assert status.success_contracts == 10
    assert status.failure_contracts == 0
    assert status.status == "success"


def test_server_materialization_starts_from_pinned_current(
    tmp_path: Path,
) -> None:
    base_prices, baseline, full, _underlying = _incident_replay()
    candidate_prices = pd.concat(
        [
            base_prices,
            pd.DataFrame(
                [
                    _price("RM2701", "2026-09-21", 2341),
                    _price("Y2701", "2026-09-21", 8895),
                    _price("OI2701", "2026-09-21", 10205),
                    _price("P2701", "2026-09-21", 9816),
                ]
            ),
        ],
        ignore_index=True,
    )
    base_price_path = tmp_path / "base-price.xlsx"
    candidate_price_path = tmp_path / "candidate-price.xlsx"
    baseline_path = tmp_path / "current.parquet"
    output_path = tmp_path / "candidate.parquet"
    workbook_path = tmp_path / "candidate.xlsx"
    for path, frame in (
        (base_price_path, base_prices),
        (candidate_price_path, candidate_prices),
    ):
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            frame.to_excel(writer, sheet_name="price_long", index=False)
    baseline.to_parquet(baseline_path, index=False)
    full.to_parquet(output_path, index=False)
    with pd.ExcelWriter(workbook_path, engine="openpyxl") as writer:
        full.to_excel(writer, sheet_name="spread_long", index=False)

    server = _load_server()
    payload, unrelated, affected = server.apply_bounded_incremental_materialization(
        price_baseline=base_price_path,
        price_candidate=candidate_price_path,
        spread_baseline=baseline_path,
        spread_full_recalculation=output_path,
        spread_workbook=workbook_path,
        config_file=CONFIG,
    )

    candidate = pd.read_parquet(output_path)
    assert len(historical_changed_keys(baseline, candidate)) == 8
    assert len(affected) == 8
    assert len(unrelated) == 18
    assert payload["full_recompute_business_diff_count"] == 26
    assert payload["full_recompute_drift_count"] == 18
    assert payload["full_recompute_drift_date_count"] == 1
    assert payload["incremental_unrelated_rewritten_keys"] == []
    assert not [
        key
        for key in historical_changed_keys(baseline, candidate)
        if key[0] != "2026-09-21"
    ]


def test_historical_guard_still_blocks_unauthorized_output_mutation() -> None:
    _prices, baseline, full, underlying = _incident_replay()
    config = pd.read_excel(CONFIG, sheet_name="spread_config")
    affected = derive_affected_spread_keys(config, underlying)
    materialized, _ = materialize_affected_spreads(
        baseline,
        full,
        underlying_keys=underlying,
        affected_keys=affected,
    )
    unauthorized = materialized.copy()
    mask = (
        pd.to_datetime(unauthorized["date"]).eq(pd.Timestamp("2026-09-21"))
        & unauthorized["spread_name"].eq("M 1-5")
    )
    unauthorized.loc[mask, "spread_value"] += 1
    policy = HistoricalMutationPolicy(
        HistoricalPublicationMode.NORMAL,
        pd.Timestamp("2026-09-21").date(),
        pd.Timestamp("2026-09-21").date(),
        exact_keys=affected,
    )
    with pytest.raises(HistoricalPublicationBlocked):
        validate_historical_publication(baseline, unauthorized, policy)
