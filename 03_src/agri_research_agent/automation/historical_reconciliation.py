"""Exact, evidence-bound Domestic Spread reconciliation in an isolated producer clone.

This module does not fetch prices or calculate spreads. It validates a sealed source
observation and delegates calculation, dependency closure, materialization, and the
historical publication guard to the existing Domestic Spread business implementation.
"""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

from agri_research_agent.data_sources.tankan.domestic_spread import (
    normalize_full_contract_code,
)
from agri_research_agent.pipelines.domestic_spread_integrity import (
    HistoricalMutationPolicy,
    HistoricalPublicationMode,
    PriceSemantic,
    changed_daily_close_keys,
    derive_affected_spread_keys,
    historical_changed_keys,
    infer_price_semantic,
    validate_historical_publication,
)

SCHEMA = "domestic-spread-historical-reconciliation/1"
DATASET = "domestic-spread"
PROVIDER = "akshare.futures_zh_daily_sina"
SOURCE_FILE = "akshare_futures_zh_daily_sina"
FIELD = "close"
SHA = re.compile(r"[0-9a-f]{64}\Z")
CURRENT_ID = re.compile(r"public-current-[0-9a-f]{24}\Z")
INCIDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,99}\Z")
CSV_FIELDS = {
    "trade_date", "full_contract", "source_row_exists", "source_close",
    "source_provider", "source_field", "source_observed_at", "source_symbol",
    "normalized_full_contract", "identity_match", "source_row_count",
    "source_close_valid", "source_error", "approval_class", "scoped_raw_sha256",
    "legacy_public_current_input_value", "legacy_input_semantic", "legacy_vs_daily_close",
}


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _file(value: object, expected_sha: object) -> Path:
    _require(isinstance(value, str) and Path(value).is_absolute(), "absolute evidence path required")
    path = Path(value).absolute()
    _require(not any(part.is_symlink() for part in (path, *path.parents)), "linked evidence forbidden")
    _require(path.is_file() and path.stat().st_size > 0, "evidence file missing or empty")
    _require(isinstance(expected_sha, str) and SHA.fullmatch(expected_sha) and _sha(path) == expected_sha,
             "evidence SHA-256 mismatch")
    return path


def _date(value: object) -> str:
    _require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), "invalid exact date")
    _require(date.fromisoformat(value).isoformat() == value, "invalid exact date")
    _require(date.fromisoformat(value) <= date.today(), "future reconciliation date forbidden")
    return value


def _contract(value: object) -> str:
    _require(isinstance(value, str), "full contract required")
    result = normalize_full_contract_code(value)
    _require(result == value, "noncanonical full contract")
    return result


def _price(value: object) -> Decimal:
    _require(isinstance(value, str), "decimal price string required")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid DAILY_CLOSE value") from None
    _require(result.is_finite() and result > 0 and str(result) == value,
             "noncanonical DAILY_CLOSE value")
    return result


def _spread_key(value: object) -> tuple[str, str, str]:
    _require(isinstance(value, str) and value.count("|") == 2, "exact derived key required")
    day, name, season = value.split("|")
    _date(day)
    _require(bool(name.strip()) and bool(season.strip()) and
             not any(char in value for char in "*?[]"), "wildcard derived scope forbidden")
    return day, name, season


def _closed(value: object, fields: set[str], label: str) -> dict:
    _require(isinstance(value, dict) and set(value) == fields, f"closed {label} schema required")
    return value


def load_manifest(path: Path) -> tuple[dict, dict]:
    """Validate exact approval, source CSV and audit bytes before any producer work."""
    path = Path(path).absolute()
    _require(not any(part.is_symlink() for part in (path, *path.parents)) and path.is_file(),
             "reconciliation manifest unavailable")
    raw = path.read_bytes()
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate manifest JSON key")
            result[key] = value
        return result
    manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    _closed(manifest, {"schema_version", "operation_type", "dataset", "incident_id", "reason",
                       "expected_current", "source_evidence", "audit_evidence", "daily_close",
                       "non_trading", "derived_scope", "counts"}, "reconciliation")
    _require(manifest["schema_version"] == SCHEMA and
             manifest["operation_type"] == "HISTORICAL_RECONCILIATION" and
             manifest["dataset"] == DATASET, "reconciliation operation/dataset differs")
    _require(isinstance(manifest["incident_id"], str) and INCIDENT.fullmatch(manifest["incident_id"])
             and isinstance(manifest["reason"], str) and 12 <= len(manifest["reason"]) <= 1000,
             "incident reason required")
    current = _closed(manifest["expected_current"],
                      {"id", "artifact_sha256", "manifest_sha256"}, "expected Current")
    _require(CURRENT_ID.fullmatch(current["id"]) and
             SHA.fullmatch(current["artifact_sha256"]) and
             SHA.fullmatch(current["manifest_sha256"]), "expected Current identity invalid")
    source = _closed(manifest["source_evidence"], {"path", "sha256"}, "source evidence")
    audit = _closed(manifest["audit_evidence"], {"path", "sha256"}, "audit evidence")
    source_path = _file(source["path"], source["sha256"])
    _file(audit["path"], audit["sha256"])
    counts = _closed(manifest["counts"], {"daily_close", "non_trading_underlying", "non_trading_derived"}, "counts")
    _require(all(type(value) is int and value > 0 for value in counts.values()), "positive exact counts required")
    daily = manifest["daily_close"]
    non_trading = manifest["non_trading"]
    derived = manifest["derived_scope"]
    _require(isinstance(daily, list) and len(daily) == counts["daily_close"] and
             isinstance(non_trading, list) and len(non_trading) == counts["non_trading_underlying"] and
             isinstance(derived, dict) and set(derived) == {"closure", "non_trading_derived_keys"} and
             derived["closure"] == "CONFIG_DERIVED_EXACT" and
             isinstance(derived["non_trading_derived_keys"], list) and
             len(derived["non_trading_derived_keys"]) == counts["non_trading_derived"],
             "manifest exact counts or closure differ")
    approved: dict[tuple[str, str], Decimal] = {}
    for item in daily:
        _closed(item, {"trade_date", "full_contract", "value"}, "DAILY_CLOSE key")
        key = (_date(item["trade_date"]), _contract(item["full_contract"]))
        _require(key not in approved, "duplicate DAILY_CLOSE key")
        approved[key] = _price(item["value"])
    missing = set()
    for item in non_trading:
        _closed(item, {"trade_date", "full_contract"}, "non-trading key")
        key = (_date(item["trade_date"]), _contract(item["full_contract"]))
        _require(key not in missing and key not in approved, "duplicate or conflicting non-trading key")
        missing.add(key)
    _require(len({day for day, _ in missing}) == 1, "one exact non-trading date required")
    holiday = next(iter(missing))[0]
    deletion = {_spread_key(value) for value in derived["non_trading_derived_keys"]}
    _require(len(deletion) == len(derived["non_trading_derived_keys"]) and
             all(key[0] == holiday for key in deletion), "non-trading derived scope invalid")
    _require(all(key[0] != holiday for key in approved), "non-trading day cannot contain DAILY_CLOSE")
    observed = {}
    with source_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        _require(reader.fieldnames is not None and CSV_FIELDS <= set(reader.fieldnames) and
                 len(reader.fieldnames) == len(set(reader.fieldnames)), "source evidence CSV schema invalid")
        for row in reader:
            _require(None not in row and all(value is not None for value in row.values()), "malformed source evidence row")
            key = (_date(row["trade_date"]), _contract(row["full_contract"]))
            _require(key not in observed, "duplicate source evidence key")
            _require(row["source_provider"] == PROVIDER and row["source_field"] == FIELD and
                     row["source_symbol"] == key[1] and row["normalized_full_contract"] == key[1] and
                     SHA.fullmatch(row["scoped_raw_sha256"]), "source identity/field is not approved DAILY_CLOSE")
            _require(infer_price_semantic(SOURCE_FILE, key[1] + ":" + FIELD) is PriceSemantic.DAILY_CLOSE
                     and row["legacy_input_semantic"] == "akshare_futures_zh_spot.current_price",
                     "source/legacy semantics differ")
            if key in approved:
                _require(row["source_row_exists"] == "YES" and row["identity_match"] == "YES" and
                         row["source_close_valid"] == "YES" and row["source_row_count"] == "1" and
                         row["approval_class"] == "DAILY_CLOSE_VERIFIED" and not row["source_error"] and
                         row["source_observed_at"] == key[0] and
                         row["legacy_vs_daily_close"] == "LEGACY_SPOT_EQUALS_DAILY_CLOSE" and
                         _price(row["legacy_public_current_input_value"]) == approved[key] and
                         _price(row["source_close"]) == approved[key],
                         "DAILY_CLOSE evidence does not match approved value")
            else:
                _require(key in missing and row["source_row_exists"] == "NO" and
                         row["source_close"] == "" and row["source_row_count"] == "0" and
                         row["approval_class"] == "DAILY_CLOSE_MISSING", "non-trading evidence is not absent")
            observed[key] = row
    _require(set(observed) == set(approved) | missing, "source evidence has missing or unapproved keys")
    return manifest, {"manifest_sha256": hashlib.sha256(raw).hexdigest(),
                      "source_path": source_path, "audit_path": Path(audit["path"]),
                      "approved": approved, "missing": missing, "delete": deletion}


def check_current(manifest: dict, public: Path) -> dict:
    """Bind approval to the exact validated Public Current replica."""
    from agri_research_agent.pipelines.public_data_delivery import validate_production_package
    package = validate_production_package(public)
    actual = package.manifest
    approved = manifest["expected_current"]
    artifact = actual["delivery_artifacts"][DATASET]
    _require(actual["package_id"] == approved["id"] and
             artifact["sha256"] == approved["artifact_sha256"] and
             _sha(public / "manifest.json") == approved["manifest_sha256"],
             "expected Public Current differs")
    return {"id": actual["package_id"], "artifact_sha256": artifact["sha256"],
            "manifest_sha256": _sha(public / "manifest.json")}


def _business_module(source: Path):
    path = source / "04_scripts/server_update_spreads.py"
    spec = importlib.util.spec_from_file_location("formal_domestic_spread_business", path)
    _require(spec is not None and spec.loader is not None, "approved business module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stage_and_calculate(source: Path, manifest: dict, validated: dict, *, python: str, env: dict) -> dict:
    """Stage approved daily closes; reuse the existing calculator and publication guard."""
    import pandas as pd
    business = _business_module(source)
    data = source / "01_data"
    price_path = data / business.PRICE_LONG_NAME
    spread_path = data / business.PARQUET_NAME
    excel_path = data / business.DATABASE_NAME
    config_path = source / "02_configs" / business.CONFIG_NAME
    price_before = pd.read_excel(price_path, sheet_name="price_long")
    _require({"date", "instrument", "instrument_cn", "delivery_month", "price", "source_column",
              "source_file", "updated_at", "status", "error"} <= set(price_before),
             "price-long schema differs")
    _require(spread_path.is_file() and excel_path.is_file() and config_path.is_file(),
             "historical baseline is incomplete")
    config = pd.read_excel(config_path, sheet_name="spread_config")
    holiday_closure = derive_affected_spread_keys(
        config, ((contract, day) for day, contract in validated["missing"])
    )
    _require(holiday_closure == validated["delete"],
             "non-trading deletion differs from config dependency closure")
    previous_spread = data / ".reconciliation_spread_baseline.parquet"
    previous_price = data / ".reconciliation_price_baseline.xlsx"
    previous_spread.write_bytes(spread_path.read_bytes())
    previous_price.write_bytes(price_path.read_bytes())
    # A historical close is a new provenance row. Raw spot/current_price rows remain untouched.
    rows = []
    # Preserve the baseline's timestamp timezone convention. Pandas rejects a
    # mixed aware/naive or mixed-offset column before the business calculator runs.
    marker = next((value for value in price_before["updated_at"] if pd.notna(value)), None)
    parsed_marker = pd.Timestamp(marker) if marker is not None else None
    stamp = (pd.Timestamp.now(tz=parsed_marker.tzinfo).isoformat(timespec="seconds")
             if parsed_marker is not None and parsed_marker.tzinfo is not None
             else pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"))
    instrument_cn = {str(row.instrument): row.instrument_cn for row in
                     price_before[["instrument", "instrument_cn"]].drop_duplicates("instrument").itertuples(index=False)}
    for (day, contract), value in sorted(validated["approved"].items()):
        prefix, yymm = re.fullmatch(r"([A-Z]+)(\d{4})", contract).groups()
        rows.append({"date": pd.Timestamp(day), "instrument": prefix,
                     "instrument_cn": instrument_cn.get(prefix, prefix), "delivery_month": int(yymm[-2:]),
                     "price": float(value), "source_column": contract + ":close",
                     "source_file": SOURCE_FILE, "updated_at": stamp,
                     "status": "success", "error": ""})
    holiday = next(iter(validated["missing"]))[0]
    for row in price_before.to_dict("records"):
        if infer_price_semantic(row.get("source_file"), row.get("source_column")) is PriceSemantic.DAILY_CLOSE:
            day = pd.Timestamp(row["date"]).date().isoformat()
            if day == holiday:
                raise ValueError("non-trading day already has DAILY_CLOSE")
            column = str(row["source_column"])
            key = (day, column.split(":", 1)[0])
            if key in validated["approved"]:
                raise ValueError("approved DAILY_CLOSE already exists in baseline")
    candidate = pd.concat([price_before, pd.DataFrame(rows, columns=price_before.columns)], ignore_index=True)
    _require(len(candidate) == len(price_before) + len(rows), "price staging row count differs")
    sheets = pd.read_excel(price_path, sheet_name=None)
    _require("price_long" in sheets, "price-long worksheet missing")
    sheets["price_long"] = candidate
    with pd.ExcelWriter(price_path, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    changed = changed_daily_close_keys(previous_price, price_path)
    expected = frozenset((contract, day) for day, contract in validated["approved"])
    _require(changed == expected, "actual DAILY_CLOSE change scope differs")
    result = subprocess.run([python, "-I", "-B", "-X", "utf8",
                             str(source / "04_scripts/calculate_historical_spreads.py")],
                            cwd=source, env=env, capture_output=True, timeout=3600, check=False)
    _require(result.returncode == 0, "approved historical calculator failed")
    _, unrelated_drift, closure = business.apply_bounded_incremental_materialization(
        price_baseline=previous_price, price_candidate=price_path,
        spread_baseline=previous_spread, spread_full_recalculation=spread_path,
        spread_workbook=excel_path, config_file=config_path)
    _require(not (closure & validated["delete"]), "trading closure overlaps non-trading deletion")
    current = pd.read_parquet(spread_path)
    keys = [(pd.Timestamp(row.date).date().isoformat(), str(row.spread_name), str(row.season))
            for row in current[["date", "spread_name", "season"]].itertuples(index=False)]
    _require(set(keys) & validated["delete"] == validated["delete"],
             "approved non-trading derived rows are not present")
    current = current.loc[[key not in validated["delete"] for key in keys]].copy()
    business.write_incremental_spread_outputs(excel_path, spread_path, current)
    allowed = frozenset(closure | validated["delete"])
    policy = HistoricalMutationPolicy(HistoricalPublicationMode.HISTORICAL_RECONCILIATION,
                                      min(date.fromisoformat(day) for day, _ in validated["approved"]),
                                      max(date.fromisoformat(day) for day, _ in validated["approved"]),
                                      allowed, allowed)
    diff = validate_historical_publication(previous_spread, spread_path, policy)
    _require(set(diff.changed_keys) <= allowed and validated["delete"] <= set(diff.changed_keys),
             "requested/effective reconciliation scope differs")
    _require(all(key not in {(pd.Timestamp(row.date).date().isoformat(), str(row.spread_name), str(row.season))
                                 for row in pd.read_parquet(spread_path)[["date", "spread_name", "season"]].itertuples(index=False)}
                 for key in validated["delete"]), "non-trading derived row remains")
    return {"approved_underlying_key_count": len(expected),
            "approved_non_trading_day_key_count": len(validated["missing"]),
            "approved_derived_key_count": len(allowed), "effective_derived_changed_key_count": len(diff.changed_keys),
            "unrelated_full_recompute_drift_count": len(unrelated_drift),
            "historical_diff_guard": "PASS", "reconciliation_mode": "HISTORICAL_RECONCILIATION"}
