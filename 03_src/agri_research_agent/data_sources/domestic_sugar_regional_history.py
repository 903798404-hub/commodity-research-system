"""Independent three-region sugar history; the Liuzhou snapshot stays untouched."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from agri_research_agent.data_sources.domestic_sugar_history import (
    SugarHistoryError, file_sha256,
)

SCHEMA = "domestic-sugar-regional-history/1"
REGIONS = ("柳州", "昆明", "乌鲁木齐")
SOURCE_COLUMNS = dict(zip(REGIONS, "BCD"))
_CONTRACT = re.compile(r"SR\d{2}(?:01|03|05|07|09|11)\.ZCE")


@dataclass(frozen=True)
class RegionalSugarHistory:
    snapshot_id: str
    metadata: dict
    daily: pd.DataFrame


def read_json(path: Path) -> dict:
    def reject_constant(value):
        raise ValueError(f"非有限数值: {value}")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SugarHistoryError("三地白糖历史包含重复JSON字段")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"),
                           object_pairs_hook=unique_pairs,
                           parse_constant=reject_constant)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SugarHistoryError("三地白糖历史文件不可读取") from exc
    if not isinstance(value, dict):
        raise SugarHistoryError("三地白糖历史必须为JSON对象")
    return value


def _date_column(frame: pd.DataFrame) -> None:
    raw = frame["business_date"]
    if raw.isna().any() or not raw.astype(str).str.fullmatch(r"\d{4}-\d{2}-\d{2}").all():
        raise SugarHistoryError("三地白糖日期格式无效")
    try:
        frame["business_date"] = pd.to_datetime(raw, format="%Y-%m-%d", errors="raise")
    except ValueError as exc:
        raise SugarHistoryError("三地白糖日期无效") from exc


def build_regional_basis(spot: pd.DataFrame, main: pd.DataFrame) -> pd.DataFrame:
    """Exact date join only; one actual main contract for all regions on a date."""
    spot = spot.copy(); main = main.copy()
    if not {"business_date", "region", "cash_price", "source_cell"}.issubset(spot.columns):
        raise SugarHistoryError("三地白糖缺少现货字段")
    if not {"business_date", "ts_code", "futures_price"}.issubset(main.columns):
        raise SugarHistoryError("三地白糖缺少主力行情字段")
    if spot.empty or main.empty or set(spot.region) != set(REGIONS):
        raise SugarHistoryError("三地白糖历史缺少地区或主力数据")
    _date_column(spot); _date_column(main)
    if spot.duplicated(["business_date", "region"]).any() or main.business_date.duplicated().any():
        raise SugarHistoryError("三地白糖日期或地区业务键重复")
    if not spot.groupby("business_date").region.nunique().eq(3).all():
        raise SugarHistoryError("三地白糖每个来源日期必须保留三个地区，缺价使用空值")
    if (not spot.source_cell.astype(str).str.fullmatch(r"[BCD][1-9]\d*").all()
            or not spot.source_cell.str[0].eq(spot.region.map(SOURCE_COLUMNS)).all()):
        raise SugarHistoryError("三地白糖原表单元格无效")
    for frame, field in ((spot, "cash_price"), (main, "futures_price")):
        if frame[field].map(lambda value: isinstance(value, (bool, np.bool_))).any():
            raise SugarHistoryError("三地白糖价格不能为布尔值")
        try:
            values = pd.to_numeric(frame[field], errors="raise")
        except (TypeError, ValueError) as exc:
            raise SugarHistoryError("三地白糖价格不是数值") from exc
        if not np.isfinite(values.dropna()).all() or values.dropna().le(0).any():
            raise SugarHistoryError("三地白糖价格无效")
        frame[field] = values.astype(float)
    codes = main.ts_code.dropna().astype(str)
    if not codes.str.fullmatch(_CONTRACT).all() or (main.futures_price.notna() & main.ts_code.isna()).any():
        raise SugarHistoryError("三地白糖主力合约无效")
    # Reindex on source observation dates, including explicit weekend/missing quotes.
    result = spot.merge(main, on="business_date", how="left", validate="many_to_one")
    result["basis"] = result.cash_price - result.futures_price
    return result.sort_values(["business_date", "region"]).reset_index(drop=True)


def validate_payload(payload: dict) -> pd.DataFrame:
    if (payload.get("schema_version") != SCHEMA or payload.get("unit") != "CNY/tonne"
            or payload.get("price_field") != "close" or payload.get("currency") != "CNY"
            or payload.get("spot_definition") != "当地贸易商报价平均值"):
        raise SugarHistoryError("三地白糖来源、价格或单位口径无效")
    try:
        source = payload["source_workbook"]
        if (source["sheet"] != "导出数据" or source["columns"] != SOURCE_COLUMNS
                or not re.fullmatch(r"[a-f0-9]{64}", source["sha256"])
                or not re.fullmatch(r"[a-f0-9]{64}", payload["main_source_snapshot_id"])):
            raise SugarHistoryError("三地白糖来源身份无效")
        extension = payload.get("main_extension_sha256")
        if extension is not None and not re.fullmatch(r"[a-f0-9]{64}", extension):
            raise SugarHistoryError("三地白糖补充行情身份无效")
        return build_regional_basis(pd.DataFrame(payload["spot_records"]), pd.DataFrame(payload["main_records"]))
    except (KeyError, TypeError) as exc:
        raise SugarHistoryError("三地白糖历史结构无效") from exc


def resolve_regional_history(root: Path) -> tuple[Path, str]:
    root = Path(root).resolve()
    pointer = read_json(root / "current.json")
    sid = pointer.get("snapshot_id", "")
    if pointer.get("schema_version") != SCHEMA or not isinstance(sid, str) or not re.fullmatch(r"[a-f0-9]{64}", sid):
        raise SugarHistoryError("三地白糖历史指针身份无效")
    database = root / "snapshots" / sid / "history.json"
    if database.resolve().parent != (root / "snapshots" / sid).resolve() or (database.parent.is_symlink()
            or database.is_symlink() or not database.resolve().is_relative_to(root)):
        raise SugarHistoryError("三地白糖历史路径越界")
    try:
        if file_sha256(database) != sid:
            raise SugarHistoryError("三地白糖历史内容身份不匹配")
    except OSError as exc:
        raise SugarHistoryError("三地白糖历史不可读取") from exc
    return database, sid


def load_regional_history(root: Path) -> RegionalSugarHistory:
    path, sid = resolve_regional_history(root)
    payload = read_json(path)
    daily = validate_payload(payload)
    after_path, after_sid = resolve_regional_history(root)
    if after_path != path or after_sid != sid:
        raise SugarHistoryError("三地白糖历史在读取期间发生变化")
    metadata = {key: value for key, value in payload.items() if key not in {"spot_records", "main_records"}}
    return RegionalSugarHistory(sid, metadata, daily)
