"""Official daily FX snapshots and explicit USD-base research calculations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
import hashlib
import math
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET

import pandas as pd

SCHEMA_VERSION = "foreign-fx-daily/1"
ECB_HISTORY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"
BCB_CATALOG_URL = "https://dadosabertos.bcb.gov.br/dataset/1-taxa-de-cambio---livre---dolar-americano-venda---diario"
ECB_CATALOG_URL = "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html"


@dataclass(frozen=True)
class Currency:
    code: str
    name: str
    country: str
    commodities: str
    meaning: str


CURRENCIES = (
    Currency("BRL", "巴西雷亚尔", "巴西", "白糖、大豆、豆粕、咖啡", "贬值提高同等美元出口报价折算的本币收入，可能增强售糖、售豆意愿。"),
    Currency("CAD", "加拿大元", "加拿大", "菜籽、菜油、小麦", "影响出口报价折算的本币收入。对菜籽的作用还取决于供需和加工利润。"),
    Currency("AUD", "澳大利亚元", "澳大利亚", "菜籽、小麦、棉花", "影响农产品出口的本币收入与相对竞争力。商品价格也会反过来影响澳元。"),
    Currency("MYR", "马来西亚林吉特", "马来西亚", "棕榈油", "马盘棕榈油以林吉特计价，研究时同时看本币价格与折美元价格。"),
    Currency("IDR", "印度尼西亚卢比", "印度尼西亚", "棕榈油", "影响棕榈油出口的本币收入，出口税费和国内政策可能抵消汇率作用。"),
    Currency("THB", "泰铢", "泰国", "白糖、橡胶、稻米", "影响出口收入与报价竞争力。糖的供给还取决于甘蔗产量及糖厂销售安排。"),
    Currency("INR", "印度卢比", "印度", "白糖、棉花、植物油进口", "贬值提高进口植物油的本币成本。糖出口还受配额与乙醇政策约束。"),
    Currency("CNY", "人民币（在岸）", "中国", "大豆、白糖、油脂进口", "美元兑人民币上涨会提高相同美元报价的人民币进口成本。此处为在岸CNY。"),
)
BY_CODE = {c.code: c for c in CURRENCIES}


class FxDataError(ValueError):
    """Reject data that would otherwise yield plausible but misleading results."""


def bcb_url(start: date, end: date) -> str:
    return ("https://api.bcb.gov.br/dados/serie/bcdata.sgs.1/dados?formato=json"
            f"&dataInicial={start:%d/%m/%Y}&dataFinal={end:%d/%m/%Y}")


def _positive(value: object) -> float:
    if isinstance(value, bool):
        raise FxDataError("汇率不能是布尔值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FxDataError("汇率不是数值") from exc
    if not math.isfinite(number) or number <= 0:
        raise FxDataError("汇率必须是有限正数")
    return number


def parse_bcb(raw: bytes, start: date, end: date) -> list[dict[str, Any]]:
    values = json.loads(raw.decode("utf-8-sig", errors="strict"))
    if not isinstance(values, list) or not values:
        raise FxDataError("巴西央行返回空数据或错误响应")
    result = []
    for row in values:
        business_date = datetime.strptime(row["data"], "%d/%m/%Y").date()
        if start <= business_date <= end:
            result.append(dict(currency="BRL", date=business_date.isoformat(),
                               local_per_usd=_positive(row["valor"]), provider="BCB_SGS1"))
    if not result:
        raise FxDataError("巴西央行在请求期间没有报价")
    return result


def parse_ecb(raw: bytes, start: date, end: date) -> list[dict[str, Any]]:
    """Cross only EUR-base observations from the exact same daily ECB fixing."""
    root = ET.fromstring(raw)
    result = []
    for node in root.iter():
        day = node.get("time")
        if day is None:
            continue
        business_date = date.fromisoformat(day)
        if not start <= business_date <= end:
            continue
        quotes: dict[str, Decimal] = {}
        for child in node:
            code = child.get("currency")
            if code:
                if code in quotes:
                    raise FxDataError(f"ECB同日报价重复：{day} {code}")
                quotes[code] = Decimal(str(_positive(child.get("rate"))))
        if "USD" not in quotes:
            # Never use a different day's EUR/USD denominator.
            continue
        for code in BY_CODE:
            if code != "BRL" and code in quotes:
                result.append(dict(currency=code, date=day,
                                   local_per_usd=float(quotes[code] / quotes["USD"]),
                                   provider="ECB_CROSS"))
    if not result:
        raise FxDataError("ECB在请求期间没有可用的同日交叉汇率")
    return result


def validate_snapshot(payload: dict[str, Any]) -> pd.DataFrame:
    if not isinstance(payload, dict):
        raise FxDataError("日度汇率文件必须包含数据对象")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise FxDataError("日度汇率数据版本不匹配")
    try:
        generated = datetime.fromisoformat(payload["generated_at"])
        if generated.tzinfo is None:
            raise FxDataError("采集时间缺少时区")
        sources = {s["provider"]: s for s in payload["sources"]}
        if len(sources) != len(payload["sources"]):
            raise FxDataError("汇率来源重复登记")
        for provider, source in sources.items():
            if provider not in {"BCB_SGS1", "ECB_CROSS"}:
                raise FxDataError("未知汇率来源")
            if not re.fullmatch(r"[0-9a-f]{64}", source["raw_sha256"]):
                raise FxDataError("来源原始文件身份缺失")
            if not source["url"].startswith("https://"):
                raise FxDataError("来源网址缺失")
        records = []
        keys = set()
        for row in payload["observations"]:
            code, day, provider = row["currency"], row["date"], row["provider"]
            if code not in BY_CODE or provider not in sources:
                raise FxDataError("币种或来源未登记")
            if provider != ("BCB_SGS1" if code == "BRL" else "ECB_CROSS"):
                raise FxDataError("币种来源口径不匹配")
            business_date = date.fromisoformat(day)
            if business_date.isoformat() != day or business_date > generated.date():
                raise FxDataError("业务日期格式错误或晚于采集日期")
            key = code, day
            if key in keys:
                raise FxDataError(f"日度报价重复：{code} {day}")
            keys.add(key)
            records.append(dict(currency=code, date=business_date,
                                local_per_usd=_positive(row["local_per_usd"]), provider=provider))
        if not records:
            raise FxDataError("日度汇率数据为空")
        return pd.DataFrame(records).sort_values(["currency", "date"]).reset_index(drop=True)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, FxDataError):
            raise
        raise FxDataError("日度汇率字段或日期不完整") from exc


def load_snapshot(path: Path) -> tuple[dict[str, Any], pd.DataFrame]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FxDataError("日度汇率文件无法读取") from exc
    return payload, validate_snapshot(payload)


def load_update_status(snapshot: Path, *, expected_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Read only a status bound to the exact stable bytes currently on the page."""
    try:
        status = json.loads(snapshot.with_name("status.json").read_text(encoding="utf-8"))
        if status["schema_version"] != "foreign-fx-update/1":
            return None
        if status["result"] not in {"UPDATED", "NO_CHANGE", "FAILED", "FAILED_AFTER_PUBLISH"}:
            return None
        stable_bytes = snapshot.read_bytes()
        if expected_payload is not None and json.loads(stable_bytes) != expected_payload:
            return None
        if status["stable_sha256"] != hashlib.sha256(stable_bytes).hexdigest():
            return None
        checked = datetime.fromisoformat(status["checked_at"])
        if checked.tzinfo is None:
            return None
        return status
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return None


def currency_return(current: float, previous: float) -> float:
    """Return of the local currency against USD; inverse of USD/local quotation."""
    return _positive(previous) / _positive(current) - 1


def seasonality(frame: pd.DataFrame, code: str, years: list[int], *,
                current_year: int, normalize: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Align observed month/day on leap year 2000; never fill missing quotes.

    The index is the USD/local quote relative to each year's first quote.
    Monthly averages weight each historical year equally and require two years.
    """
    if code not in BY_CODE:
        raise FxDataError("请选择有效币种")
    rows = frame.loc[frame["currency"] == code].copy().sort_values("date")
    rows["year"] = rows["date"].map(lambda d: d.year)
    rows = rows.loc[rows["year"].isin(years)].copy()
    rows["season_date"] = rows["date"].map(lambda d: d.replace(year=2000))
    rows["value"] = rows["local_per_usd"]
    if normalize and not rows.empty:
        bases = rows.groupby("year")["local_per_usd"].transform("first")
        rows["value"] = rows["local_per_usd"] / bases * 100
    historical = rows.loc[rows["year"] < current_year]
    historical = historical.assign(month=historical["date"].map(lambda d: d.month))
    monthly = historical.groupby(["year", "month"], as_index=False)["value"].mean()
    average = monthly.groupby("month", as_index=False).agg(
        value=("value", "mean"), year_count=("year", "nunique"))
    average["season_date"] = average["month"].map(lambda month: date(2000, month, 15))
    average.loc[average["year_count"] < 2, "value"] = float("nan")
    return rows.reset_index(drop=True), average


def overview(frame: pd.DataFrame) -> pd.DataFrame:
    result = []
    for currency in CURRENCIES:
        history = frame.loc[frame["currency"] == currency.code].sort_values("date")
        row = dict(currency=currency.code, name=currency.name, country=currency.country,
                   commodities=currency.commodities, latest_date=None, rate=None,
                   return_1=None, return_5=None, return_20=None)
        if not history.empty:
            values = history["local_per_usd"].tolist()
            row.update(latest_date=history.iloc[-1]["date"], rate=values[-1])
            for periods in (1, 5, 20):
                if len(values) > periods:
                    row[f"return_{periods}"] = currency_return(values[-1], values[-periods-1])
        result.append(row)
    return pd.DataFrame(result)


def strength_comparison(frame: pd.DataFrame, codes: list[str], start: date) -> tuple[pd.DataFrame, date | None]:
    """Use one observed base date shared by all selected currencies; never fill gaps."""
    if not codes or any(code not in BY_CODE for code in codes):
        raise FxDataError("请选择有效币种")
    selected = frame.loc[frame["currency"].isin(codes) & (frame["date"] >= start)].copy()
    sets = [set(selected.loc[selected["currency"] == code, "date"]) for code in codes]
    common = set.intersection(*sets)
    if not common:
        return pd.DataFrame(columns=["currency", "date", "strength"]), None
    base = min(common)
    selected = selected.loc[selected["date"] >= base].copy()
    base_values = selected.loc[selected["date"] == base].set_index("currency")["local_per_usd"]
    selected["strength"] = selected["currency"].map(base_values) / selected["local_per_usd"] * 100
    return selected[["currency", "date", "strength"]].sort_values(["currency", "date"]), base
