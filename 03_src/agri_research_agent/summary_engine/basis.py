from __future__ import annotations

from datetime import date, datetime, timezone
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from .rules import DEFAULT_CONFIG, freshness_status, load_summary_rules
from .schema import Summary


BASE_KEY = ["commodity", "region", "quote_type", "delivery_month", "futures_contract"]
def _comparison_key(frame: pd.DataFrame, rules: Mapping[str, Any]) -> list[str]:
    fields = rules["basis"].get("optional_quote_point_fields", [])
    point = next((column for column in fields if column in frame.columns), None)
    return [*BASE_KEY, point] if point else BASE_KEY.copy()


def _number(value: object) -> float | None:
    return None if pd.isna(value) else float(value)


def build_basis_summary(
    frame: pd.DataFrame,
    *,
    source_identity: Mapping[str, Any],
    generated_at: datetime | None = None,
    source_dataset: str = "basis_quotes",
    rules: Mapping[str, Any] | None = None,
    today: date | None = None,
) -> Summary:
    rules = load_summary_rules() if rules is None else rules
    data = frame.copy(); data["date"] = pd.to_datetime(data["date"], errors="raise")
    key = _comparison_key(data, rules)
    display_names = rules["basis"].get("display_names", {})
    data["commodity"] = data["commodity"].replace(display_names)
    data = data[data["basis"].notna()].sort_values([*key, "date"])
    if data.empty:
        return Summary.create(module="basis", source_dataset=source_dataset, source_identity=source_identity,
            source_date=None, comparison_identity=None, generated_at=generated_at,
            calculation_version=rules["calculation_version"], rule_version=rules["rule_version"],
            freshness_status="missing", facts={"quotes": []}, classifications=[], headline="国内基差",
            detail_text="基差摘要暂不可用。", short_text="基差摘要暂不可用", missing_reason="无有效基差记录")
    latest_date = data["date"].max(); latest = data[data["date"].eq(latest_date)]
    quotes = []
    for row in latest.itertuples(index=False):
        mask = pd.Series(True, index=data.index)
        for field in key:
            value = getattr(row, field); mask &= data[field].isna() if pd.isna(value) else data[field].eq(value)
        previous = data[mask & data["date"].lt(latest_date)].tail(1)
        prior = previous.iloc[0] if not previous.empty else None
        change = None if prior is None else float(row.basis) - float(prior["basis"])
        continuity_key = [field for field in key if field not in {"delivery_month", "futures_contract"}]
        continuity = pd.Series(True, index=data.index)
        for field in continuity_key:
            value = getattr(row, field); continuity &= data[field].isna() if pd.isna(value) else data[field].eq(value)
        has_earlier_series = bool((continuity & data["date"].lt(latest_date)).any())
        quote = {field: (None if pd.isna(getattr(row, field)) else getattr(row, field)) for field in key} | {
            "current_date": latest_date.date().isoformat(), "current_basis": float(row.basis),
            "cash_price": _number(getattr(row, "cash_price", None)),
            "futures_price": _number(getattr(row, "futures_price", None)),
            "previous_date": None if prior is None else pd.Timestamp(prior["date"]).date().isoformat(),
            "previous_basis": None if prior is None else float(prior["basis"]), "change": change,
            "missing_reason": None if prior is not None else ("换月" if has_earlier_series else "无前值")}
        if key == BASE_KEY:
            quote["quote_point"] = None
        quotes.append(quote)
    comparable = [q for q in quotes if q["change"] is not None]
    comparable.sort(key=lambda q: abs(q["change"]), reverse=True)
    top = comparable[: int(rules["basis"]["top_changes"])]
    def format_quote(q: dict[str, Any]) -> str:
        point = f'｜{q["quote_point"]}' if q.get("quote_point") else ""
        comparison = ("0" if q["change"] == 0 else f'{q["change"]:+g}') if q["change"] is not None else q["missing_reason"]
        return f'{q["commodity"]}｜{q["region"]}{point} {q["current_basis"]:g}（{comparison}）'
    detail = "；".join(format_quote(q) for q in top) or "最新报价均无连续可比前值。"
    return Summary.create(module="basis", source_dataset=source_dataset, source_identity=source_identity,
        source_date=latest_date.date().isoformat(), comparison_identity={"key": key, "method": "previous_valid_quote"},
        generated_at=generated_at, calculation_version=rules["calculation_version"], rule_version=rules["rule_version"],
        freshness_status=freshness_status("basis", latest_date.date().isoformat(), rules, today=today), facts={"quotes": quotes, "top_changes": top},
        classifications=["basis_up" if q["change"] > 0 else "basis_down" if q["change"] < 0 else "basis_unchanged" for q in top],
        headline="今日主要基差变化", detail_text=detail, short_text=f"基差（{latest_date:%Y-%m-%d}）：{detail}")


@lru_cache(maxsize=1)
def _shared_basis_services():
    # Website caching initializes only on the first shared-service call.
    # Existing pure builder consumers never import Streamlit or the reader.
    import streamlit as st

    @st.cache_data(show_spinner=False, max_entries=8)
    def _load_basis_versioned(
        root: str, release_id: str, manifest_sha256: str,
    ) -> tuple[pd.DataFrame, dict[str, object]]:
        from agri_research_agent.market_data.public_basis_current import load_public_basis_current
        snapshot = load_public_basis_current(
            root, expected_release_id=release_id,
            expected_manifest_sha256=manifest_sha256,
        )
        return snapshot.records, snapshot.source_identity


    @st.cache_data(show_spinner=False, max_entries=64)
    def _summarize_basis_versioned(
        records_sha256: str,
        source_identity: dict[str, Any],
        source_dataset: str,
        rules_sha256: str,
        as_of_utc_day: str,
        _records: pd.DataFrame,
        _rules: Mapping[str, Any],
    ) -> Summary:
        # Underscored inputs are excluded only because the public wrapper hashes
        # their full content above; caller-owned mutable frames are never cached.
        del records_sha256, rules_sha256
        return build_basis_summary(
            _records, source_identity=source_identity, source_dataset=source_dataset,
            rules=_rules, today=date.fromisoformat(as_of_utc_day),
        )


    return _load_basis_versioned, _summarize_basis_versioned


def load_shared_basis_current(
    public_current_root: str | Path,
) -> tuple[pd.DataFrame, dict[str, object]]:
    """Share one verified version, including on a cross-page cache hit."""
    from agri_research_agent.market_data.public_basis_current import (
        PublicBasisCurrentError, PublicBasisCurrentErrorCode, resolve_public_basis_current_identity,
    )
    root = str(Path(public_current_root).resolve())
    before = resolve_public_basis_current_identity(root)
    key = before.release_id, before.manifest_sha256
    records, source = _shared_basis_services()[0](root, *key)
    after = resolve_public_basis_current_identity(root)
    if (after.release_id, after.manifest_sha256) != key:
        raise PublicBasisCurrentError(
            PublicBasisCurrentErrorCode.CURRENT_IDENTITY_MISMATCH,
            "Domestic Basis Current changed during shared read",
        )
    return records, source


def _records_sha256(records: pd.DataFrame) -> str:
    # Streamlit samples large DataFrames by default. Hash every observation so
    # a revision outside that sample cannot reuse an old calculation. Column
    # order, types and index also distinguish the original page view scopes.
    schema = [(str(name), str(dtype)) for name, dtype in zip(records.columns, records.dtypes)]
    digest = sha256(json.dumps(schema, ensure_ascii=False).encode('utf-8'))
    digest.update(pd.util.hash_pandas_object(records, index=True).to_numpy().tobytes())
    return digest.hexdigest()


def build_shared_basis_summary(
    records: pd.DataFrame, *, source_identity: Mapping[str, Any],
    source_dataset: str = 'public_domestic_basis_current',
    rules_path: str | Path = DEFAULT_CONFIG,
    today: date | None = None,
) -> Summary:
    """Keep existing calculations and freshness policy with exact dependencies.

    The Basis page passes its existing filtered display frame; the overview
    passes the full Public frame. Their different scopes cannot share results.
    UTC dates retain the original summary engine's freshness day boundary.
    """
    import yaml
    body = Path(rules_path).read_bytes()
    rules = yaml.safe_load(body.decode('utf-8', 'strict'))
    if rules.get('schema_version') != 1 or not rules.get('rule_version'):
        raise ValueError('Summary规则配置版本无效')
    return _shared_basis_services()[1](
        _records_sha256(records), dict(source_identity), source_dataset,
        sha256(body).hexdigest(), (today or datetime.now(timezone.utc).date()).isoformat(),
        _records=records, _rules=rules,
    )


def clear_shared_basis_cache() -> None:
    read, summarize = _shared_basis_services()
    read.clear()
    summarize.clear()
