"""Validated manually published sugar observations, separate from historical snapshots."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from pathlib import Path

import pandas as pd

from .domestic_sugar_history import SugarHistoryError

REGIONS = ("广西糖厂", "昆明", "乌鲁木齐")
SCHEMA = "domestic-sugar-daily/1"
CODE = re.compile(r"SR\d{2}(?:01|03|05|07|09|11)\.ZCE")


def validate_daily(payload: dict) -> pd.DataFrame:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA:
        raise SugarHistoryError("日度白糖记录格式无效")
    if any(not re.fullmatch(r"[0-9a-f]{64}", payload.get(field, ""))
           for field in ("workbook_sha256", "mapping_sha256", "daily_sha256")):
        raise SugarHistoryError("日度白糖来源身份无效")
    records = payload.get("records")
    if not isinstance(records, list) or len(records) > 30000:
        raise SugarHistoryError("日度白糖记录无效")
    rows, seen = [], set()
    for row in records:
        if not isinstance(row, dict) or set(row) != {"date", "region", "components", "contract", "close"}:
            raise SugarHistoryError("日度白糖字段无效")
        date, region = row["date"], row["region"]
        try:
            if not isinstance(date, str) or dt.date.fromisoformat(date).isoformat() != date:
                raise ValueError("date")
        except ValueError as exc:
            raise SugarHistoryError("日度白糖日期无效") from exc
        if date <= "2026-10-28" or region not in REGIONS or (date, region) in seen:
            raise SugarHistoryError("日度白糖日期或地区重复")
        seen.add((date, region))
        components = row["components"]
        if not isinstance(components, list) or len(components) > 4:
            raise SugarHistoryError("日度白糖报价明细无效")
        names = set()
        for component in components:
            if not isinstance(component, dict) or set(component) != {"name", "price", "cell"}:
                raise SugarHistoryError("日度白糖报价来源无效")
            name, price, cell = component["name"], component["price"], component["cell"]
            allowed = {"广西糖厂": {"东糖": "J", "凤糖": "K", "湘桂": "L", "南华": "M"},
                       "昆明": {"南华昆明": "O", "英茂昆明": "R"}, "乌鲁木齐": {"乌鲁木齐现货": "AB"}}
            if (name not in allowed[region] or name in names or not isinstance(cell, str)
                    or not re.fullmatch(allowed[region].get(name, "INVALID") + r"[1-9]\d*", cell)
                    or isinstance(price, bool) or not isinstance(price, (int, float))
                    or not math.isfinite(price) or price <= 0):
                raise SugarHistoryError("日度白糖报价无效")
            names.add(name)
        contract, close = row["contract"], row["close"]
        if contract is not None and (not isinstance(contract, str) or not CODE.fullmatch(contract)):
            raise SugarHistoryError("日度白糖合约无效")
        if close is not None and (contract is None or isinstance(close, bool) or not isinstance(close, (int, float))
                                  or not math.isfinite(close) or close <= 0):
            raise SugarHistoryError("日度白糖收盘价无效")
        price = sum(c["price"] for c in components) / len(components) if components else None
        rows.append(dict(business_date=pd.Timestamp(date), region=region, cash_price=price,
                         ts_code=contract, futures_price=close,
                         basis=price - close if price is not None and close is not None else None,
                         source_cell=",".join(c["cell"] for c in components)))
    frame = pd.DataFrame(rows, columns=["business_date", "region", "cash_price", "ts_code", "futures_price", "basis", "source_cell"])
    for _, group in frame.groupby("business_date"):
        if (set(group.region) != set(REGIONS) or group.ts_code.nunique(dropna=False) != 1
                or group.futures_price.nunique(dropna=False) != 1):
            raise SugarHistoryError("日度白糖三地日期或行情不一致")
    return frame


def load_daily(path: Path) -> tuple[str, pd.DataFrame]:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    try:
        raw = path.read_bytes()
        if len(raw) > 4_000_000:
            raise ValueError("size")
        payload = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=unique_pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite")))
        return hashlib.sha256(raw).hexdigest(), validate_daily(payload)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise SugarHistoryError("日度白糖记录不可读取") from exc
