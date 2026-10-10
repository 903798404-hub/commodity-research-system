"""Independent same-day market inputs using the existing CFETS and Sina providers."""
from __future__ import annotations

from datetime import date, datetime, time
import json

from agri_research_agent.soybean_margin import api_sources as provider
from .model import START, PROFILES, expected_contracts, number

SCHEMA = "commodity-import-inputs/1"
CAPTURE_START = date(2026, 10, 12)
MAX_BYTES = 256 * 1024


def encoded(value: dict) -> bytes:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("行情快照超出大小上限")
    return raw


def validate(value: dict) -> date:
    if value.get("schema_version") != SCHEMA or value.get("commodity") not in PROFILES:
        raise ValueError("行情快照品种或版本无效")
    day = date.fromisoformat(value["business_date"])
    captured = datetime.fromisoformat(value["captured_at"])
    if day < CAPTURE_START or day.weekday() >= 5 or captured.utcoffset() is None or captured.astimezone(provider.SHANGHAI).date() != day:
        raise ValueError("行情快照日期无效")
    if not time(9, 35) <= captured.astimezone(provider.SHANGHAI).time() <= time(15):
        raise ValueError("行情采集时间无效")
    if value.get("units") != dict(cnf="USD/tonne", domestic="CNY/tonne", fx="CNY_per_USD") or value.get("fx_currency") != "USD/CNY":
        raise ValueError("行情单位或币种不符")
    if value.get("parameters") != dict(PROFILES[value["commodity"]]) or value.get("fx_policy") != "soybean-spot-forward/1":
        raise ValueError("行情计算参数或汇率规则不符")
    expected = expected_contracts(day, value["commodity"])
    if value.get("contracts") != expected or set(value.get("domestic", {})) != set(expected):
        raise ValueError("行情合约集合不符")
    if not isinstance(value.get("fx_curve"), dict) or not set(value["fx_curve"]).issubset({"0", "1", "3", "6", "9", "12"}):
        raise ValueError("汇率期限无效")
    sources = value.get("sources", {})
    if set(sources) != {"fx", "domestic"} or any(not isinstance(v, dict) for v in sources.values()):
        raise ValueError("行情来源缺失")
    calendar = value.get("calendar", {})
    if calendar.get("provider") != "AkShare/Sina" or not calendar.get("is_trading_day") or date.fromisoformat(calendar["covered_through"]) < day:
        raise ValueError("交易日历验证缺失")
    errors = value.get("errors")
    if not isinstance(errors, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k,v in errors.items()):
        raise ValueError("行情错误码无效")
    for quotes in (value["domestic"], value["fx_curve"]):
        if any(v is not None and (number(v) is None or number(v) <= 0) for v in quotes.values()):
            raise ValueError("行情价格无效")
    for code, price in value["domestic"].items():
        if price is not None:
            evidence = sources["domestic"].get("quotes", {}).get(code, {})
            if evidence.get("price_field") != "current_price" or not evidence.get("raw_contract", "").endswith(code[-4:]):
                raise ValueError("国内行情合约凭证不符")
            provider.fresh(datetime.fromisoformat(evidence["quoted_at"]), captured)
    if any(v is not None for v in value["fx_curve"].values()):
        if sources["fx"].get("currency") != "USD/CNY" or sources["fx"].get("rate_kind") != "mid":
            raise ValueError("汇率来源币种不符")
        stamps = sources["fx"].get("published_at", [])
        if len(stamps) != 2:
            raise ValueError("汇率发布时间缺失")
        for stamp in stamps:
            provider.fresh(datetime.fromisoformat(stamp), captured)
    encoded(value)
    return day


def capture(commodity: str, *, now: datetime | None = None) -> dict:
    PROFILES[commodity]
    clock = (lambda: now.astimezone(provider.SHANGHAI)) if now is not None else lambda: datetime.now(provider.SHANGHAI)
    stamp = clock()
    day = stamp.date()
    if day < CAPTURE_START or day.weekday() >= 5 or not time(9, 35) <= stamp.time() <= time(15):
        raise provider.SourceError("capture_outside_same_day_0935_1500_window")
    dates = {date.fromisoformat(item) for item in provider._worker("calendar", 35)}
    if not dates or day > max(dates):
        raise provider.SourceError("trading_calendar_out_of_range")
    if day not in dates:
        raise provider.SourceError("domestic_market_holiday")
    expected = expected_contracts(day, commodity)
    result = dict(schema_version=SCHEMA, commodity=commodity, business_date=day.isoformat(),
        captured_at=stamp.isoformat(), units=dict(cnf="USD/tonne", domestic="CNY/tonne", fx="CNY_per_USD"),
        parameters=dict(PROFILES[commodity]), fx_policy="soybean-spot-forward/1",
        fx_currency="USD/CNY", contracts=expected, domestic=dict.fromkeys(expected), fx_curve={},
        sources=dict(fx={}, domestic={}), errors={},
        calendar=dict(provider="AkShare/Sina", covered_through=max(dates).isoformat(), is_trading_day=True))
    try:
        result["fx_curve"], result["sources"]["fx"] = provider.fx_curve(clock())
    except Exception as exc:
        result["errors"]["fx"] = str(exc) if isinstance(exc, provider.SourceError) else "fx_transport_or_schema_error"
    try:
        result["domestic"], result["sources"]["domestic"], errors = provider.domestic(sorted(expected))
        result["errors"].update(errors)
    except Exception as exc:
        result["errors"]["domestic"] = str(exc) if isinstance(exc, provider.SourceError) else "domestic_transport_or_schema_error"
    result["captured_at"] = clock().isoformat()
    validate(result)
    return result
