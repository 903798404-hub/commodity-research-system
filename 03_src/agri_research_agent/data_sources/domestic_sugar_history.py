"""Read-only, identity-bound historical Liuzhou spot and SR basis snapshots."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

SCHEMA = "domestic-sugar-history/1"
SPOT_SERIES = "mysteel.liuzhou.trader_average"
_SHA = re.compile(r"^[a-f0-9]{64}$")
_CONTRACT = re.compile(r"^SR\d{2}(?:01|03|05|07|09|11)\.ZCE$")


class SugarHistoryError(ValueError):
    """An unavailable or inconsistent sugar snapshot cannot be displayed."""


@dataclass(frozen=True)
class SugarHistorySnapshot:
    snapshot_id: str
    database_sha256: str
    manifest: dict[str, object]
    spot: pd.DataFrame
    fixed_basis: pd.DataFrame
    main_basis: pd.DataFrame
    nearest_basis: pd.DataFrame


def _json(path: Path) -> dict:
    try:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise SugarHistoryError("历史清单包含重复字段")
                result[key] = value
            return result
        result = json.loads(path.read_text(encoding="utf-8", errors="strict"), object_pairs_hook=pairs)
        if not isinstance(result, dict):
            raise SugarHistoryError("历史清单必须是对象")
        return result
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SugarHistoryError("柳州历史快照不可读取") from exc


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_sugar_history(root: str | Path) -> tuple[Path, dict]:
    root = Path(root).resolve()
    pointer = _json(root / "current.json")
    sid = pointer.get("snapshot_id", "")
    manifest_sha = pointer.get("manifest_sha256", "")
    if pointer.get("schema_version") != SCHEMA or not isinstance(sid, str) or not isinstance(manifest_sha, str) or not _SHA.fullmatch(sid) or not _SHA.fullmatch(manifest_sha):
        raise SugarHistoryError("柳州历史指针身份无效")
    folder = root / "snapshots" / sid
    if folder.is_symlink() or folder.resolve().parent != (root / "snapshots").resolve():
        raise SugarHistoryError("柳州历史快照路径越界")
    manifest_file = folder / "manifest.json"
    try:
        if manifest_file.is_symlink() or file_sha256(manifest_file) != manifest_sha:
            raise SugarHistoryError("柳州历史清单身份不匹配")
    except OSError as exc:
        raise SugarHistoryError("柳州历史快照缺少清单") from exc
    manifest = _json(manifest_file)
    if (manifest.get("schema_version") != SCHEMA or manifest.get("snapshot_id") != sid
            or manifest.get("database_sha256") != sid or manifest.get("database_file") != "history.sqlite3"
            or manifest.get("spot_series_id") != SPOT_SERIES or manifest.get("price_field") != "close"
            or manifest.get("currency") != "CNY" or manifest.get("unit") != "CNY/tonne"):
        raise SugarHistoryError("柳州历史来源、价格或单位口径无效")
    database = folder / "history.sqlite3"
    try:
        if database.is_symlink() or file_sha256(database) != sid:
            raise SugarHistoryError("柳州历史数据库身份不匹配")
    except OSError as exc:
        raise SugarHistoryError("柳州历史数据库不可读取") from exc
    return database, manifest


def _dates(frame: pd.DataFrame) -> None:
    raw = frame["business_date"]
    if raw.isna().any() or not raw.astype(str).str.fullmatch(r"\d{4}-\d{2}-\d{2}").all():
        raise SugarHistoryError("柳州历史日期无效")
    try:
        frame["business_date"] = pd.to_datetime(raw, format="%Y-%m-%d", errors="raise")
    except ValueError as exc:
        raise SugarHistoryError("柳州历史日期无效") from exc


def _unique(frame: pd.DataFrame, keys: list[str]) -> None:
    if frame.duplicated(keys).any():
        raise SugarHistoryError("柳州历史业务键重复")


def _numbers(frame: pd.DataFrame, columns: list[str], positive: bool = False) -> None:
    for name in columns:
        try:
            values = pd.to_numeric(frame[name], errors="raise")
        except (TypeError, ValueError) as exc:
            raise SugarHistoryError("柳州历史数值字段无效") from exc
        if not np.isfinite(values.dropna()).all() or (positive and (values.dropna() <= 0).any()):
            raise SugarHistoryError("柳州历史价格包含无效值")
        frame[name] = values


def _basis(frame: pd.DataFrame) -> None:
    _numbers(frame, ["cash_price", "futures_price", "basis"])
    _numbers(frame, ["cash_price", "futures_price"], positive=True)
    expected = frame.cash_price - frame.futures_price
    if not frame.basis.isna().equals(expected.isna()) or not np.allclose(
            frame.basis.dropna(), expected.dropna(), rtol=0, atol=1e-8):
        raise SugarHistoryError("柳州基差与同日现货减收盘价不一致")


def read_sugar_database(database: Path) -> dict[str, pd.DataFrame]:
    """Read controlled tables with SQLite query_only and no source modifications."""
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            result = {name: pd.read_sql_query("SELECT * FROM " + name, connection) for name in (
                "spot_daily", "futures_daily", "main_mapping", "main_supplemental_daily",
                "basis_fixed_contract", "basis_nearest_month", "basis_main_continuous")}
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        raise SugarHistoryError("柳州历史数据库结构不可读取") from exc
    spot = result["spot_daily"]
    required = {
        "spot_daily": {"series_id", "business_date", "cash_price", "source_cell", "status"},
        "futures_daily": {"ts_code", "business_date", "close"},
        "main_supplemental_daily": {"ts_code", "business_date", "close"},
        "main_mapping": {"business_date", "mapped_contract"},
        "basis_fixed_contract": {"series_id", "ts_code", "business_date", "cash_price", "futures_price", "basis", "delivery_year", "delivery_month", "futures_price_field"},
        "basis_nearest_month": {"business_date", "delivery_month", "ts_code", "cash_price", "futures_price", "basis"},
        "basis_main_continuous": {"business_date", "ts_code", "cash_price", "futures_price", "basis", "price_field"},
    }
    if any(not fields.issubset(result[name].columns) for name, fields in required.items()):
        raise SugarHistoryError("柳州历史缺少必要字段")
    result["spot_daily"] = spot.loc[spot.series_id.eq(SPOT_SERIES)].copy()
    for name, frame in result.items():
        _dates(frame)
        keys = ["business_date", "ts_code"] if name in {"futures_daily", "main_supplemental_daily", "basis_fixed_contract"} else ["business_date", "delivery_month"] if name == "basis_nearest_month" else ["business_date"]
        _unique(frame, keys)
    if any(result[n].empty for n in ("spot_daily", "futures_daily", "main_mapping", "basis_fixed_contract", "basis_main_continuous")):
        raise SugarHistoryError("柳州历史缺少必要数据")
    _numbers(result["spot_daily"], ["cash_price"], positive=True)
    for name in ("futures_daily", "main_supplemental_daily"):
        _numbers(result[name], ["close"], positive=True)
        if not result[name].ts_code.astype(str).str.fullmatch(_CONTRACT).all():
            raise SugarHistoryError("柳州历史期货合约无效")
    fut = pd.concat([result["futures_daily"], result["main_supplemental_daily"]], ignore_index=True)
    _unique(fut, ["ts_code", "business_date"])
    spot_values = result["spot_daily"].set_index("business_date").cash_price
    for name in ("basis_fixed_contract", "basis_nearest_month", "basis_main_continuous"):
        frame = result[name]
        _basis(frame)
        expected_spot = frame.business_date.map(spot_values)
        if not np.allclose(frame.cash_price, expected_spot, equal_nan=True, rtol=0, atol=1e-8):
            raise SugarHistoryError("基差现货日期或价格与原始序列不一致")
        lookup = frame[["ts_code", "business_date"]].merge(fut[["ts_code", "business_date", "close"]], how="left", on=["ts_code", "business_date"], validate="many_to_one")
        if not np.allclose(frame.futures_price, lookup.close, equal_nan=True, rtol=0, atol=1e-8):
            raise SugarHistoryError("基差未使用精确合约的同日收盘价")
    fixed = result["basis_fixed_contract"]
    if not fixed.series_id.eq(SPOT_SERIES).all():
        raise SugarHistoryError("固定月基差的现货序列不匹配")
    if not fixed.ts_code.str.fullmatch(r"SR\d{2}(01|05|09)\.ZCE").all():
        raise SugarHistoryError("固定月基差包含其他月份")
    if not fixed.delivery_year.eq(2000 + fixed.ts_code.str[2:4].astype(int)).all() or not fixed.delivery_month.eq(fixed.ts_code.str[4:6].astype(int)).all():
        raise SugarHistoryError("固定月交割年份或月份不匹配")
    main = result["basis_main_continuous"]
    mappings = result["main_mapping"].set_index("business_date").mapped_contract
    expected_contracts = main.business_date.map(mappings).fillna("")
    if not main.ts_code.fillna("").eq(expected_contracts).all():
        raise SugarHistoryError("主力基差未使用来源的真实映射")
    if not fixed.futures_price_field.eq("close").all() or not main.price_field.eq("close").all():
        raise SugarHistoryError("白糖历史必须使用未复权收盘价")
    if "series_id" in main and not main.series_id.eq(SPOT_SERIES).all():
        raise SugarHistoryError("主力基差的现货序列不匹配")
    return result


def load_sugar_history(root: str | Path) -> SugarHistorySnapshot:
    database, manifest = resolve_sugar_history(root)
    frames = read_sugar_database(database)
    counts = manifest.get("table_rows")
    if not isinstance(counts, dict) or any(counts.get(name) != len(frame) for name, frame in frames.items()):
        raise SugarHistoryError("柳州历史条数与清单不一致")
    if file_sha256(database) != manifest["database_sha256"]:
        raise SugarHistoryError("柳州历史在读取期间发生变化")
    after_db, after_manifest = resolve_sugar_history(root)
    if after_db != database or after_manifest != manifest:
        raise SugarHistoryError("柳州历史指针在读取期间发生变化")
    return SugarHistorySnapshot(str(manifest["snapshot_id"]), str(manifest["database_sha256"]), manifest,
        frames["spot_daily"], frames["basis_fixed_contract"], frames["basis_main_continuous"], frames["basis_nearest_month"])


def reconcile_regional_history(base: SugarHistorySnapshot, regional) -> SugarHistorySnapshot:
    """A read-only combined view; never mutate either sealed source snapshot.

    The later regional capture uses the same Liuzhou trader-average definition.
    Its actual mapped contract close may extend that contract's fixed-month view;
    no other delivery month or date can borrow that price.
    """
    if regional.metadata["main_source_snapshot_id"] != base.snapshot_id:
        raise SugarHistoryError("三地来源未绑定当前柳州历史，拒绝合并")
    newer = regional.daily.loc[regional.daily.region.eq("柳州")].copy()
    for old, fields in ((base.spot, ["cash_price"]),
                        (base.main_basis, ["ts_code", "futures_price"])):
        common = old.merge(newer, on="business_date", suffixes=("_old", "_new"), validate="one_to_one")
        for field in fields:
            left, right = common[field + "_old"], common[field + "_new"]
            if (left.notna() & right.notna() & left.ne(right)).any():
                raise SugarHistoryError("三地来源与柳州历史存在重叠冲突，拒绝合并")

    source = regional.metadata["source_workbook"]
    spot = newer[["business_date", "cash_price", "source_cell"]].copy()
    spot["series_id"] = SPOT_SERIES
    spot["source_sheet"] = source["sheet"]
    spot["source_sha256"] = source["sha256"]
    spot["status"] = np.where(spot.cash_price.notna(), "OBSERVED", "SOURCE_NULL")
    spot = pd.concat([base.spot.loc[~base.spot.business_date.isin(spot.business_date)], spot], ignore_index=True)
    spot = spot.sort_values("business_date").reset_index(drop=True)
    spot_values = spot.set_index("business_date").cash_price

    main = newer[["business_date", "ts_code", "cash_price", "futures_price", "basis"]].copy()
    main["series_id"] = SPOT_SERIES
    main["continuous_code"] = "SR.ZCE"
    main["price_field"] = "close"
    main["spot_source_sha256"] = source["sha256"]
    main["source_cell"] = newer.source_cell
    main["futures_source_sha256"] = regional.snapshot_id
    main["mapping_source_sha256"] = regional.snapshot_id
    main = pd.concat([base.main_basis.loc[~base.main_basis.business_date.isin(main.business_date)], main], ignore_index=True)
    main = main.sort_values("business_date").reset_index(drop=True)

    fixed = base.fixed_basis.copy()
    extra = newer.loc[newer.ts_code.fillna("").str.fullmatch(r"SR\d{2}(01|05|09)\.ZCE")
                      & newer.futures_price.notna()].copy()
    extra["delivery_year"] = 2000 + extra.ts_code.str[2:4].astype(int)
    extra["delivery_month"] = extra.ts_code.str[4:6].astype(int)
    extra["series_id"] = SPOT_SERIES
    extra["futures_price_field"] = "close"
    extra["spot_source_sha256"] = source["sha256"]
    extra["futures_source_sha256"] = regional.snapshot_id
    # Reject contradictory non-null closes before using the newer exact quote.
    keys = ["business_date", "ts_code"]
    overlap = fixed.merge(extra, on=keys, suffixes=("_old", "_new"))
    if (overlap.futures_price_old.notna() & overlap.futures_price_new.notna()
            & overlap.futures_price_old.ne(overlap.futures_price_new)).any():
        raise SugarHistoryError("三地收盘价与固定合约历史冲突，拒绝合并")
    fixed = pd.concat([extra.reindex(columns=fixed.columns), fixed], ignore_index=True).drop_duplicates(keys)
    fixed["cash_price"] = fixed.business_date.map(spot_values)
    fixed["basis"] = fixed.cash_price - fixed.futures_price
    fixed = fixed.sort_values(["business_date", "ts_code"]).reset_index(drop=True)

    # Each latest spot date has all three month slots; absent exact closes stay NULL.
    pairs = []
    for day in spot.business_date:
        for month in (1, 5, 9):
            year = day.year + int(month < day.month)
            pairs.append(dict(business_date=day, delivery_month=month, ts_code=f"SR{year % 100:02d}{month:02d}.ZCE"))
    nearest = pd.DataFrame(pairs).merge(fixed[keys + ["futures_price"]], on=keys, how="left", validate="one_to_one")
    nearest["cash_price"] = nearest.business_date.map(spot_values)
    nearest["basis"] = nearest.cash_price - nearest.futures_price
    manifest = {**base.manifest, "regional_view_snapshot_id": regional.snapshot_id,
                "regional_source_workbook": source}
    return replace(base, manifest=manifest, spot=spot, main_basis=main,
                   fixed_basis=fixed, nearest_basis=nearest)
