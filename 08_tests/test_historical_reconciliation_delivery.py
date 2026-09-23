from __future__ import annotations

import csv
from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pandas as pd
import pytest

from agri_research_agent.automation import historical_reconciliation as history


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ("M2609", "M2701", "M2705", "RM2609", "RM2701", "RM2705",
             "Y2609", "Y2701", "Y2705", "OI2609", "OI2701", "OI2705",
             "P2609", "P2701", "P2705")
SHA = "a" * 64


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(tmp_path: Path, *, daily_dates: int = 42) -> tuple[Path, dict]:
    evidence = tmp_path / "source.csv"
    fields = ["trade_date", "full_contract", "source_row_exists", "source_close",
              "source_provider", "source_field", "source_observed_at", "source_symbol",
              "normalized_full_contract", "identity_match", "source_row_count",
              "source_close_valid", "source_error", "approval_class", "scoped_raw_sha256"]
    fields += ["legacy_public_current_input_value", "legacy_input_semantic", "legacy_vs_daily_close"]
    daily = []
    rows = []
    for offset in range(daily_dates):
        day = (date(2026, 6, 16) + timedelta(days=offset)).isoformat()
        if day == "2026-06-19":
            continue
        for contract in CONTRACTS:
            daily.append({"trade_date": day, "full_contract": contract, "value": "2945.0"})
            rows.append({"trade_date": day, "full_contract": contract,
                         "source_row_exists": "YES", "source_close": "2945.0",
                         "source_provider": history.PROVIDER, "source_field": "close",
                         "source_observed_at": day, "source_symbol": contract,
                         "normalized_full_contract": contract, "identity_match": "YES",
                         "source_row_count": "1", "source_close_valid": "YES", "source_error": "",
                         "approval_class": "DAILY_CLOSE_VERIFIED", "scoped_raw_sha256": SHA,
                         "legacy_public_current_input_value": "2945.0",
                         "legacy_input_semantic": "akshare_futures_zh_spot.current_price",
                         "legacy_vs_daily_close": "LEGACY_SPOT_EQUALS_DAILY_CLOSE"})
    # Keep exactly daily_dates trading dates despite the holiday in the date sequence.
    if daily_dates == 42:
        day = "2026-07-28"
        for contract in CONTRACTS:
            daily.append({"trade_date": day, "full_contract": contract, "value": "2945.0"})
            rows.append({"trade_date": day, "full_contract": contract,
                         "source_row_exists": "YES", "source_close": "2945.0",
                         "source_provider": history.PROVIDER, "source_field": "close",
                         "source_observed_at": day, "source_symbol": contract,
                         "normalized_full_contract": contract, "identity_match": "YES",
                         "source_row_count": "1", "source_close_valid": "YES", "source_error": "",
                         "approval_class": "DAILY_CLOSE_VERIFIED", "scoped_raw_sha256": SHA,
                         "legacy_public_current_input_value": "2945.0",
                         "legacy_input_semantic": "akshare_futures_zh_spot.current_price",
                         "legacy_vs_daily_close": "LEGACY_SPOT_EQUALS_DAILY_CLOSE"})
    non_trading = [{"trade_date": "2026-06-19", "full_contract": contract} for contract in CONTRACTS]
    rows.extend({"trade_date": "2026-06-19", "full_contract": contract,
                 "source_row_exists": "NO", "source_close": "",
                 "source_provider": history.PROVIDER, "source_field": "close",
                 "source_observed_at": "", "source_symbol": contract,
                 "normalized_full_contract": contract, "identity_match": "NOT_VERIFIABLE_NO_ROW",
                 "source_row_count": "0", "source_close_valid": "NO", "source_error": "",
                 "approval_class": "DAILY_CLOSE_MISSING", "scoped_raw_sha256": SHA,
                 "legacy_public_current_input_value": "2942.0",
                 "legacy_input_semantic": "akshare_futures_zh_spot.current_price",
                 "legacy_vs_daily_close": ""}
                for contract in CONTRACTS)
    with evidence.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)
    audit = tmp_path / "audit.json"
    audit.write_text('{"audit":"fixture"}', encoding="utf-8")
    derived = [f"2026-06-19|{name}|2026/2027"
               for name in ("M 1-5", "M 9-1", "M 9-5", "RM 1-5", "RM 9-1", "RM 9-5",
                            "Y 1-5", "Y 9-1", "Y 9-5", "OI 1-5", "OI 9-1", "OI 9-5",
                            "P 1-5", "P 9-1", "P 9-5", "M-RM 1", "Y-OI 1", "Y-P 1")]
    manifest = {"schema_version": history.SCHEMA, "operation_type": "HISTORICAL_RECONCILIATION",
                "dataset": history.DATASET, "incident_id": "domestic-spread-2026-06-19",
                "reason": "Verified DAILY_CLOSE semantic repair and non-trading day exclusion",
                "expected_current": {"id": "public-current-" + "b" * 24,
                                     "artifact_sha256": "c" * 64, "manifest_sha256": "d" * 64},
                "source_evidence": {"path": str(evidence), "sha256": _sha(evidence)},
                "audit_evidence": {"path": str(audit), "sha256": _sha(audit)},
                "daily_close": daily, "non_trading": non_trading,
                "derived_scope": {"closure": "CONFIG_DERIVED_EXACT",
                                  "non_trading_derived_keys": derived},
                "counts": {"daily_close": len(daily), "non_trading_underlying": 15,
                           "non_trading_derived": 18}}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest


def _save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_exact_630_daily_close_and_15_missing_keys_accept(tmp_path: Path):
    path, value = _manifest(tmp_path)
    approved, checked = history.load_manifest(path)
    assert approved["counts"] == {"daily_close": 630, "non_trading_underlying": 15,
                                  "non_trading_derived": 18}
    assert len(checked["approved"]) == 630
    assert len(checked["missing"]) == 15
    assert len(checked["delete"]) == 18
    assert checked["manifest_sha256"] == _sha(path)


@pytest.mark.parametrize("mutate", [
    lambda m: m["daily_close"].append(m["daily_close"][0]),
    lambda m: m["daily_close"][0].update(full_contract="M*"),
    lambda m: m["counts"].update(daily_close=1),
    lambda m: m["source_evidence"].update(sha256="0" * 64),
    lambda m: m["expected_current"].update(id="public-current-*"),
    lambda m: m["derived_scope"].update(closure="ALL_HISTORY"),
    lambda m: m["non_trading"][0].update(trade_date=m["daily_close"][0]["trade_date"]),
])
def test_manifest_rejects_duplicate_wildcard_count_evidence_or_scope(tmp_path: Path, mutate):
    path, value = _manifest(tmp_path, daily_dates=2)
    mutate(value); _save(path, value)
    with pytest.raises((ValueError, TypeError)):
        history.load_manifest(path)


def test_spot_current_price_evidence_cannot_claim_daily_close(tmp_path: Path):
    path, value = _manifest(tmp_path, daily_dates=2)
    source = Path(value["source_evidence"]["path"])
    raw = source.read_text(encoding="utf-8").replace(history.PROVIDER, "akshare_futures_zh_spot")
    source.write_text(raw, encoding="utf-8")
    value["source_evidence"]["sha256"] = _sha(source)
    _save(path, value)
    with pytest.raises(ValueError, match="source identity"):
        history.load_manifest(path)


def test_current_binding_rejects_stale_artifact_or_manifest(monkeypatch, tmp_path: Path):
    path, value = _manifest(tmp_path, daily_dates=2)
    public = tmp_path / "public"; public.mkdir()
    (public / "manifest.json").write_bytes(b"current")
    value["expected_current"]["manifest_sha256"] = _sha(public / "manifest.json")
    package = {"package_id": value["expected_current"]["id"],
               "delivery_artifacts": {"domestic-spread": {"sha256": value["expected_current"]["artifact_sha256"]}}}
    import agri_research_agent.pipelines.public_data_delivery as delivery
    monkeypatch.setattr(delivery, "validate_production_package", lambda _: SimpleNamespace(manifest=package))
    assert history.check_current(value, public)["id"] == value["expected_current"]["id"]
    value["expected_current"]["artifact_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="Current differs"):
        history.check_current(value, public)


def test_formal_cli_routes_manifest_without_daily_end_date(monkeypatch, tmp_path: Path):
    spec = importlib.util.spec_from_file_location(
        "historical_reconciliation_cli_test",
        ROOT / "04_scripts/automation/run_production_data_delta_windows.py")
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    config = tmp_path / "config.json"; config.write_text('{"approved_commit":"fixture"}', encoding="utf-8")
    manifest = tmp_path / "manifest.json"; manifest.write_text("{}", encoding="utf-8")
    seen = {}
    fake = SimpleNamespace(validate_config=lambda _: None,
                           verify_clean_detached_clone=lambda *_: None,
                           run_domain=lambda _, domain, **kwargs:
                           seen.update(domain=domain, **kwargs) or {"status": "CANDIDATE"})
    monkeypatch.setattr(cli, "_bootstrap", lambda _: None)
    monkeypatch.setattr(cli, "load_module", lambda: fake)
    assert cli.main(["--config", str(config), "--domain", "akshare",
                     "--historical-reconciliation-manifest", str(manifest)]) == 0
    assert seen["reconciliation_manifest"] == manifest
    assert seen["end_date"] is None
    assert cli.main(["--config", str(config), "--domain", "akshare", "--end-date", "2026-06-20",
                     "--historical-reconciliation-manifest", str(manifest)]) == 1


def test_reconciliation_reuses_bounded_business_logic_and_removes_only_holiday_rows(tmp_path: Path):
    fixture_path = ROOT / "08_tests/pipelines/test_domestic_spread_incremental.py"
    spec = importlib.util.spec_from_file_location("domestic_incremental_fixture", fixture_path)
    fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
    base_prices, baseline, _full, _ = fixture._incident_replay()
    source = tmp_path / "source"
    data = source / "01_data"; data.mkdir(parents=True)
    scripts = source / "04_scripts"; scripts.mkdir()
    configs = source / "02_configs"; configs.mkdir()
    shutil.copytree(ROOT / "03_src", source / "03_src")
    shutil.copy2(ROOT / "04_scripts/server_update_spreads.py", scripts)
    shutil.copy2(ROOT / "04_scripts/calculate_historical_spreads.py", scripts)
    shutil.copy2(ROOT / "02_configs/historical_spread_config.xlsx", configs)
    with pd.ExcelWriter(data / "historical_price_long.xlsx", engine="openpyxl") as writer:
        base_prices.to_excel(writer, sheet_name="price_long", index=False)
    holiday_rows = baseline.loc[pd.to_datetime(baseline["date"]).eq(pd.Timestamp("2026-07-16"))].copy()
    holiday_rows["date"] = pd.Timestamp("2026-06-19")
    holiday_rows["month_day"] = "06-19"
    holiday_rows = holiday_rows.head(18)
    assert len(holiday_rows) == 18
    baseline = pd.concat([baseline, holiday_rows], ignore_index=True)
    baseline.to_parquet(data / "historical_spread_database.parquet", index=False)
    with pd.ExcelWriter(data / "historical_spread_database.xlsx", engine="openpyxl") as writer:
        baseline.to_excel(writer, sheet_name="spread_long", index=False)
        pd.DataFrame([{"metric": "spread_long_rows", "value": len(baseline)}]).to_excel(
            writer, sheet_name="summary", index=False)
    late = {"RM2701": "2341", "Y2701": "8895", "OI2701": "10205", "P2701": "9816"}
    validated = {"approved": {("2026-09-21", key): history._price(value)
                              for key, value in late.items()},
                 "missing": {("2026-06-19", key) for key in CONTRACTS},
                 "delete": {(pd.Timestamp(row.date).date().isoformat(), str(row.spread_name), str(row.season))
                            for row in holiday_rows[["date", "spread_name", "season"]].itertuples(index=False)}}
    manifest = {"operation_type": "HISTORICAL_RECONCILIATION"}
    result = history.stage_and_calculate(source, manifest, validated,
                                         python=__import__("sys").executable,
                                         env=__import__("os").environ.copy())
    assert result["historical_diff_guard"] == "PASS"
    assert result["approved_underlying_key_count"] == 4
    assert result["approved_non_trading_day_key_count"] == 15
    after = pd.read_parquet(data / "historical_spread_database.parquet")
    assert not pd.to_datetime(after["date"]).eq(pd.Timestamp("2026-06-19")).any()
    original = baseline.loc[~pd.to_datetime(baseline["date"]).eq(pd.Timestamp("2026-06-19"))]
    changed = fixture.historical_changed_keys(original, after)
    assert all(key[0] == "2026-09-21" for key in changed)
    prices = pd.read_excel(data / "historical_price_long.xlsx", sheet_name="price_long")
    assert len(prices) == len(base_prices) + 4
