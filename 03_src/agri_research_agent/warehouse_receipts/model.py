"""Warehouse-level observations and conservative, unit-preserving totals."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

PRODUCTS = {"M": ("豆粕", "DCE"), "Y": ("豆油", "DCE"), "P": ("棕榈油", "DCE"),
            "RM": ("菜粕", "CZCE"), "OI": ("菜油", "CZCE"), "SR": ("白糖", "CZCE")}
FIELDS = "trade_date,symbol,fut_name,warehouse,wh_id,pre_vol,vol,vol_chg,area,year,grade,brand,place,pd,is_ct,unit,exchange"
DIMENSIONS = ["trade_date", "symbol", "warehouse", "wh_id", "area", "year", "grade", "brand", "place", "pd", "is_ct", "unit", "exchange"]
SCHEMA = "warehouse-receipts/1"


class ReceiptError(ValueError):
    """Invalid or incomplete source evidence must not become a chart."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_rows(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        raise ReceiptError("仓单接口未返回观测")
    data = pd.DataFrame(rows)
    if not set(FIELDS.split(",")).issubset(data.columns):
        raise ReceiptError("仓单接口缺少完整维度或单位字段")
    if data.symbol.isna().any() or not data.symbol.isin(PRODUCTS).all():
        raise ReceiptError("仓单品种无效")
    if not data.trade_date.astype(str).str.fullmatch(r"\d{8}").all():
        raise ReceiptError("仓单日期无效")
    data["date"] = pd.to_datetime(data.trade_date, format="%Y%m%d", errors="raise")
    if data.unit.isna().any() or data.unit.astype(str).str.strip().eq("").any():
        raise ReceiptError("仓单单位缺失")
    data["unit"] = data.unit.astype(str).str.strip()
    if data.warehouse.isna().any() or data.warehouse.astype(str).str.strip().eq("").any():
        raise ReceiptError("仓单仓库标识缺失")
    if not data.exchange.eq(data.symbol.map(lambda s: PRODUCTS[s][1])).all():
        raise ReceiptError("仓单交易所与品种不一致")
    for name in ("vol", "pre_vol", "vol_chg"):
        data[name] = pd.to_numeric(data[name], errors="raise")
        if not np.isfinite(data[name].dropna()).all():
            raise ReceiptError("仓单数值无效")
        # Preserve provider mistakes in raw evidence. Negative stock quantities
        # invalidate the affected daily aggregate instead of fabricating a value.
    for name in ("year", "pd"):
        data[name] = data[name].map(lambda value: format(float(value), ".15g") if pd.notna(value) else None)
    # The same warehouse can carry several production years, grades and brands.
    # Conflicts on the full source key fail; only byte-equivalent observations deduplicate.
    data = data.drop_duplicates(subset=FIELDS.split(","))
    if data.duplicated(DIMENSIONS).any():
        raise ReceiptError("仓单完整业务键存在冲突记录")
    # API row order within a warehouse can change between identical requests.
    # Stable full-key ordering prevents false revisions and redundant snapshots.
    order = data[DIMENSIONS].astype("string").fillna("").sort_values(DIMENSIONS, kind="stable").index
    return data.loc[order].reset_index(drop=True)


def aggregate_daily(raw: pd.DataFrame) -> pd.DataFrame:
    results = []
    for (symbol, date), block in raw.groupby(["symbol", "date"], sort=True):
        # Never sum quantities in different units, including apparently similar ones.
        if block.unit.nunique() != 1:
            raise ReceiptError(f"{symbol} 同日仓单单位不一致")
        names = block.warehouse.astype(str).str.strip()
        total = block.loc[names.isin(["合计", "总计", "总合计", "全部合计"])]
        subtotals = names.str.contains("合计|总计|小计", regex=True)
        detail = block.loc[~subtotals]
        if not total.empty:
            if len(total) != 1:
                raise ReceiptError(f"{symbol} 同日存在多个总计")
            chosen = total
            basis = "source_total"
            if (not detail.empty and detail.vol.notna().all() and chosen.vol.notna().all()
                    and detail.vol.ge(0).all() and chosen.vol.ge(0).all()):
                if not np.isclose(detail.vol.sum(), chosen.vol.iloc[0]):
                    raise ReceiptError(f"{symbol} 总计与仓库明细不一致")
        else:
            if detail.empty:
                raise ReceiptError(f"{symbol} 仅有小计，无法确定品种总量")
            chosen, basis = detail, "warehouse_sum"
        def strict_sum(name):
            valid = chosen[name].notna().all() and (name == "vol_chg" or chosen[name].ge(0).all())
            return float(chosen[name].sum()) if valid else None
        volume = strict_sum("vol")
        invalid = chosen.vol.lt(0).any()
        results.append(dict(symbol=symbol, date=date, unit=block.unit.iloc[0], vol=volume,
                            pre_vol=strict_sum("pre_vol"), vol_chg=strict_sum("vol_chg"),
                            status="invalid_component" if invalid else "actual" if volume is not None else "missing_component",
                            aggregation=basis, source_rows=len(block), component_rows=len(chosen),
                            source="Tushare-compatible fut_wsr"))
    result = pd.DataFrame(results)
    for name in ("vol", "pre_vol", "vol_chg"):
        result[name] = pd.to_numeric(result[name], errors="raise").astype(float)
    # A historical unit switch is a separate series, never spliced into one curve.
    return result.sort_values(["symbol", "unit", "date"]).reset_index(drop=True)


def write_snapshot(root: Path, raw: pd.DataFrame, daily: pd.DataFrame, evidence: dict) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    folder = root / "snapshots"
    folder.mkdir(exist_ok=True)
    raw_bytes = raw.to_csv(index=False).encode("utf-8-sig")
    daily_bytes = daily.to_csv(index=False).encode("utf-8-sig")
    sid = hashlib.sha256(raw_bytes + daily_bytes).hexdigest()
    target = folder / sid
    if target.exists():
        raise ReceiptError("候选快照已经存在，未覆盖")
    target.mkdir()
    (target / "raw.csv").write_bytes(raw_bytes)
    (target / "daily.csv").write_bytes(daily_bytes)
    manifest = dict(schema_version=SCHEMA, snapshot_id=sid, raw_sha256=sha256(target / "raw.csv"),
                    daily_sha256=sha256(target / "daily.csv"), raw_rows=len(raw), daily_rows=len(daily),
                    **evidence)
    manifest_path = target / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    pointer = dict(schema_version=SCHEMA, snapshot_id=sid, manifest_sha256=sha256(manifest_path))
    temporary = root / "current.json.tmp"
    temporary.write_text(json.dumps(pointer), encoding="utf-8")
    temporary.replace(root / "current.json")
    return manifest


def load_snapshot(root: Path, *, full_validation: bool = True) -> tuple[pd.DataFrame, dict]:
    """Publishers replay raw observations; readers verify the compact serving file.

    The read-only daily table remains bound to the same immutable manifest and
    pointer. Raw replay is performed before publication, rather than per page open.
    """
    try:
        pointer_bytes = (root / "current.json").read_bytes()
        pointer = json.loads(pointer_bytes.decode("utf-8", errors="strict"))
        sid = pointer["snapshot_id"]
        import re
        if pointer["schema_version"] != SCHEMA or not re.fullmatch(r"[a-f0-9]{64}", sid):
            raise ReceiptError("仓单指针无效")
        folder = root / "snapshots" / sid
        if folder.is_symlink() or folder.resolve().parent != (root / "snapshots").resolve():
            raise ReceiptError("仓单快照路径无效")
        manifest_path = folder / "manifest.json"
        if manifest_path.is_symlink() or sha256(manifest_path) != pointer["manifest_sha256"]:
            raise ReceiptError("仓单清单身份不匹配")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8", errors="strict"))
        if manifest["schema_version"] != SCHEMA or manifest["snapshot_id"] != sid:
            raise ReceiptError("仓单快照身份不匹配")
        for name in (("raw", "daily") if full_validation else ("daily",)):
            path = folder / f"{name}.csv"
            if path.is_symlink() or sha256(path) != manifest[f"{name}_sha256"]:
                raise ReceiptError("仓单数据身份不匹配")
        stored = pd.read_csv(folder / "daily.csv", parse_dates=["date"])
        validate_daily(stored)
        if full_validation:
            raw_bytes = (folder / "raw.csv").read_bytes()
            daily_bytes = (folder / "daily.csv").read_bytes()
            if hashlib.sha256(raw_bytes + daily_bytes).hexdigest() != sid:
                raise ReceiptError("仓单快照内容身份不匹配")
            raw = pd.read_csv(folder / "raw.csv", dtype={key: str for key in DIMENSIONS}, keep_default_na=False,
                              na_values=[""])
            raw = normalize_rows(raw[FIELDS.split(",")].to_dict("records"))
            daily = aggregate_daily(raw)
            pd.testing.assert_frame_equal(daily, stored, check_dtype=False, check_exact=False, rtol=0, atol=1e-8)
            if len(raw) != manifest["raw_rows"]:
                raise ReceiptError("仓单原始条数与清单不一致")
        if len(stored) != manifest["daily_rows"]:
            raise ReceiptError("仓单条数与清单不一致")
        if pointer_bytes != (root / "current.json").read_bytes():
            raise ReceiptError("仓单指针在读取期间变化")
        return stored, manifest
    except ReceiptError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AssertionError) as exc:
        raise ReceiptError("仓单快照缺失或校验失败") from exc


def validate_daily(data: pd.DataFrame) -> None:
    required = {"symbol", "date", "unit", "vol", "pre_vol", "vol_chg", "status", "aggregation",
                "source_rows", "component_rows", "source"}
    if set(data.columns) != required or data.empty:
        raise ReceiptError("仓单日度表结构无效")
    if (data.date.isna().any() or data.duplicated(["symbol", "date"]).any()
            or not data.symbol.isin(PRODUCTS).all() or data.unit.isna().any()
            or data.unit.astype(str).str.strip().eq("").any()
            or not data.status.isin(["actual", "missing_component", "invalid_component"]).all()):
        raise ReceiptError("仓单日度键或状态无效")
    for name in ("vol", "pre_vol", "vol_chg"):
        values = pd.to_numeric(data[name], errors="raise")
        if not np.isfinite(values.dropna()).all() or (name != "vol_chg" and values.dropna().lt(0).any()):
            raise ReceiptError("仓单日度数值无效")
    if not data.status.eq("actual").equals(data.vol.notna()):
        raise ReceiptError("仓单日度状态与缺失不一致")
    if not data.aggregation.isin(["source_total", "warehouse_sum"]).all():
        raise ReceiptError("仓单汇总口径无效")
    if (data.source_rows.le(0).any() or data.component_rows.le(0).any()
            or data.component_rows.gt(data.source_rows).any()):
        raise ReceiptError("仓单分项条数无效")
