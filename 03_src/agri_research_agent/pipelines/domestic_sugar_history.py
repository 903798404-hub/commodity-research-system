"""Prepare a standalone historical candidate from an approved local capture."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from agri_research_agent.data_sources.domestic_sugar_history import (
    SCHEMA, SPOT_SERIES, SugarHistoryError, file_sha256, load_sugar_history,
    read_sugar_database,
)


def prepare_sugar_history(source_directory: Path, candidate_root: Path) -> dict:
    """Never read the network or merge into any existing oilseed basis Current."""
    source_directory = Path(source_directory).resolve()
    candidate_root = Path(candidate_root).resolve()
    database = source_directory / "白糖柳州历史库.sqlite3"
    contract = json.loads((source_directory / "history_contract.json").read_text(encoding="utf-8", errors="strict"))
    audit = json.loads((source_directory / "audit_summary.json").read_text(encoding="utf-8", errors="strict"))
    verification = json.loads((source_directory / "verification.json").read_text(encoding="utf-8", errors="strict"))
    if (contract.get("main_spot_series", {}).get("series_id") != SPOT_SERIES
            or contract.get("futures", {}).get("field") != "close"
            or contract.get("main_spot_series", {}).get("unit") != "CNY/tonne"):
        raise SugarHistoryError("历史来源不符合柳州贸易商现货及原始收盘口径")
    digest = file_sha256(database)
    verified_files = [item for item in verification.get("files", []) if item.get("file") == database.name]
    if verification.get("result") != "PASS" or len(verified_files) != 1 or verified_files[0].get("sha256") != digest:
        raise SugarHistoryError("历史库与已核验来源身份不一致")
    frames = read_sugar_database(database)
    if digest != file_sha256(database):
        raise SugarHistoryError("输入历史库在校验期间发生变化")
    manifest = dict(schema_version=SCHEMA, snapshot_id=digest, database_file="history.sqlite3",
        database_sha256=digest, spot_series_id=SPOT_SERIES, price_field="close", currency="CNY", unit="CNY/tonne",
        table_rows={name: len(frame) for name, frame in frames.items()},
        spot_min_date=frames["spot_daily"].business_date.min().date().isoformat(),
        spot_max_date=frames["spot_daily"].business_date.max().date().isoformat(),
        futures_max_date=frames["futures_daily"].business_date.max().date().isoformat(),
        common_max_date=frames["basis_fixed_contract"].loc[frames["basis_fixed_contract"].basis.notna(), "business_date"].max().date().isoformat(),
        source_workbook=dict(name="钢联数据_白糖现货价.xlsx",sha256=contract["main_spot_series"]["source_sha256"],
                             sheet=contract["main_spot_series"]["source_sheet"],column=contract["main_spot_series"]["source_column"],
                             definition=contract["main_spot_series"]["definition"]),
        futures_source=dict(endpoint=contract["futures"]["endpoint"],source=contract["futures"]["source"],
                            capture_sha256=contract["futures"]["response_file_sha256"]),
        capture_date=audit["as_of"], refresh_policy="manual_history_snapshot_no_scheduled_refresh",
        source_verification_sha256=file_sha256(source_directory / "verification.json"),
        created_at=dt.datetime.now(dt.timezone.utc).isoformat())
    target = candidate_root / "snapshots" / digest
    if (candidate_root / "current.json").exists():
        current = load_sugar_history(candidate_root)
        if current.snapshot_id != digest:
            raise SugarHistoryError("候选目录已有不同快照；请使用新的候选目录")
        return dict(status="NO_CHANGE", snapshot_id=digest, candidate_root=str(candidate_root))
    if target.exists():
        raise SugarHistoryError("目标快照目录已存在且没有有效指针")
    candidate_root.mkdir(parents=True, exist_ok=True)
    (candidate_root / "snapshots").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sugar-prepare-", dir=candidate_root) as folder:
        work = Path(folder)
        shutil.copyfile(database, work / "history.sqlite3")
        if file_sha256(work / "history.sqlite3") != digest:
            raise SugarHistoryError("历史副本身份不一致")
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        (work / "manifest.json").write_bytes(manifest_bytes)
        work.rename(target)
    pointer = dict(schema_version=SCHEMA, snapshot_id=digest, manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest())
    pointer_bytes = json.dumps(pointer, ensure_ascii=False, indent=2).encode("utf-8")
    with (candidate_root / "current.json").open("xb") as stream:
        stream.write(pointer_bytes)
    load_sugar_history(candidate_root)
    return dict(status="PREPARED", snapshot_id=digest, candidate_root=str(candidate_root), table_rows=manifest["table_rows"])
