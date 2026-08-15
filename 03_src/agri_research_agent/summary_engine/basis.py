from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

import pandas as pd

from .rules import freshness_status, load_summary_rules
from .schema import Summary


BASE_KEY = ["commodity", "region", "quote_type", "delivery_month", "futures_contract"]
def _comparison_key(frame: pd.DataFrame, rules: Mapping[str, Any]) -> list[str]:
    fields = rules["basis"].get("optional_quote_point_fields", [])
    point = next((column for column in fields if column in frame.columns), None)
    return [*BASE_KEY, point] if point else BASE_KEY.copy()


def _number(value: object) -> float | None:
    return None if pd.isna(value) else float(value)


def build_basis_summary(frame: pd.DataFrame, *, source_identity: Mapping[str, Any], generated_at: datetime | None = None) -> Summary:
    rules = load_summary_rules(); data = frame.copy(); data["date"] = pd.to_datetime(data["date"], errors="raise")
    key = _comparison_key(data, rules)
    display_names = rules["basis"].get("display_names", {})
    data["commodity"] = data["commodity"].replace(display_names)
    data = data[data["basis"].notna()].sort_values([*key, "date"])
    if data.empty:
        return Summary.create(module="basis", source_dataset="basis_quotes", source_identity=source_identity,
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
    return Summary.create(module="basis", source_dataset="basis_quotes", source_identity=source_identity,
        source_date=latest_date.date().isoformat(), comparison_identity={"key": key, "method": "previous_valid_quote"},
        generated_at=generated_at, calculation_version=rules["calculation_version"], rule_version=rules["rule_version"],
        freshness_status=freshness_status("basis", latest_date.date().isoformat(), rules), facts={"quotes": quotes, "top_changes": top},
        classifications=["basis_up" if q["change"] > 0 else "basis_down" if q["change"] < 0 else "basis_unchanged" for q in top],
        headline="今日主要基差变化", detail_text=detail, short_text=f"基差（{latest_date:%Y-%m-%d}）：{detail}")
