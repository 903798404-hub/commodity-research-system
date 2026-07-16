from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import yaml

from agri_research_agent.core.paths import CONFIG_DIR, PROJECT_ROOT
from agri_research_agent.data_sources.basis_database import (
    DATABASE_COLUMNS,
    build_basis_database,
)


DEFAULT_CONFIG_PATH = CONFIG_DIR / "basis_excel.yaml"
NUMERIC_COLUMNS = ["cash_price", "futures_price", "basis"]


def _resolve_path(value: object, project_root: Path) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def load_basis_update_config(
    config_path: Path | str | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    resolved_config = Path(config_path or DEFAULT_CONFIG_PATH).resolve()
    with resolved_config.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    if not isinstance(config, dict):
        raise ValueError(f"基差配置必须是 YAML 对象：{resolved_config}")

    source = config.get("source_file", {})
    update = config.get("update", {})
    required = {
        "current_excel": source.get("path"),
        "incoming_excel": update.get("incoming_path"),
        "current_parquet": update.get("parquet_path"),
        "status_file": update.get("status_path"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"基差更新配置缺少路径：{', '.join(missing)}")

    primary_key = update.get("primary_key")
    if not isinstance(primary_key, list) or not primary_key:
        raise ValueError("基差更新配置缺少 primary_key")
    formal = update.get("formal_display_commodities")
    if not isinstance(formal, dict) or not formal:
        raise ValueError("基差更新配置缺少 formal_display_commodities")

    return {
        "config_path": resolved_config,
        **{
            name: _resolve_path(value, project_root)
            for name, value in required.items()
        },
        "primary_key": [str(column) for column in primary_key],
        "formal_display_commodities": {
            str(code): str(name) for code, name in formal.items()
        },
        "excluded_display_commodities": {
            str(value)
            for value in update.get("excluded_display_commodities", [])
        },
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"现有 update_status.json 无法读取：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("现有 update_status.json 顶层必须是对象")
    return value


def _normalized_keys(dataframe: pd.DataFrame, key: list[str]) -> pd.DataFrame:
    normalized = dataframe.copy()
    normalized["date"] = pd.to_datetime(
        normalized["date"], errors="coerce"
    ).dt.normalize()
    for column in key:
        if column != "date":
            normalized[column] = (
                normalized[column]
                .astype("string")
                .fillna("<NULL>")
                .str.strip()
            )
    return normalized


def _validate_candidate(
    dataframe: pd.DataFrame,
    build_result: dict[str, Any],
    settings: dict[str, Any],
) -> None:
    missing_columns = [
        column for column in DATABASE_COLUMNS if column not in dataframe.columns
    ]
    if missing_columns:
        raise ValueError(
            f"临时 Parquet 缺少字段：{', '.join(missing_columns)}"
        )
    if dataframe.empty:
        raise ValueError("临时 Parquet 没有有效记录")

    key = settings["primary_key"]
    always_required_key = [
        "date",
        "commodity",
        "region",
        "quote_type",
        "delivery_month",
    ]
    missing_key = dataframe[always_required_key].isna().any(axis=1)
    if missing_key.any():
        raise ValueError(f"临时 Parquet 有 {int(missing_key.sum())} 行主键为空")
    if dataframe.duplicated(key, keep=False).any():
        count = int(dataframe.duplicated(key, keep=False).sum())
        raise ValueError(f"临时 Parquet 有 {count} 行异常重复业务记录")

    basis_rows = dataframe["quote_type"].eq("基差报价")
    basis_required = [
        "futures_contract",
        "cash_price",
        "futures_price",
        "basis",
    ]
    basis_missing = dataframe.loc[basis_rows, basis_required].isna().any(axis=1)
    if basis_missing.any():
        raise ValueError(
            f"基差报价有 {int(basis_missing.sum())} 行关键数值为空"
        )
    cash_rows = dataframe["quote_type"].eq("一口价")
    cash_missing = dataframe.loc[cash_rows, "cash_price"].isna()
    if cash_missing.any():
        raise ValueError(f"一口价有 {int(cash_missing.sum())} 行数值为空")

    invalid_counts = {
        field: sum(int(summary[field]) for summary in build_result["sheet_summaries"])
        for field in (
            "invalid_dates",
            "invalid_cash_prices",
            "invalid_futures_prices",
            "invalid_basis_values",
            "duplicate_rows",
        )
    }
    if any(invalid_counts.values()):
        details = ", ".join(
            f"{name}={value}"
            for name, value in invalid_counts.items()
            if value
        )
        raise ValueError(f"Excel 存在异常日期、数值或重复记录：{details}")

    known = set(settings["formal_display_commodities"])
    known.update(settings["excluded_display_commodities"])
    actual = set(dataframe["commodity"].dropna().astype(str))
    unknown = sorted(actual - known)
    if unknown:
        raise ValueError(f"发现未知品种：{', '.join(unknown)}")
    missing_formal = sorted(
        set(settings["formal_display_commodities"]) - actual
    )
    if missing_formal:
        raise ValueError(f"缺少正式展示品种：{', '.join(missing_formal)}")


def _compare_history(
    current: pd.DataFrame,
    candidate: pd.DataFrame,
    key: list[str],
) -> dict[str, int]:
    current = _normalized_keys(current, key)
    candidate = _normalized_keys(candidate, key)
    current_keys = current[key].drop_duplicates()
    candidate_keys = candidate[key].drop_duplicates()
    key_diff = current_keys.merge(
        candidate_keys,
        on=key,
        how="outer",
        indicator=True,
        validate="one_to_one",
    )
    removed = key_diff.loc[key_diff["_merge"].eq("left_only"), key]
    if not removed.empty:
        sample = removed.head(3).to_dict(orient="records")
        raise ValueError(
            f"新 Excel 删除了 {len(removed)} 条历史记录；示例主键：{sample}"
        )

    common = current.merge(
        candidate,
        on=key,
        how="inner",
        suffixes=("_current", "_candidate"),
        validate="one_to_one",
    )
    changed = np.zeros(len(common), dtype=bool)
    changed_fields: dict[str, int] = {}
    for column in NUMERIC_COLUMNS:
        old = pd.to_numeric(common[f"{column}_current"], errors="coerce")
        new = pd.to_numeric(common[f"{column}_candidate"], errors="coerce")
        equal = np.isclose(old, new, rtol=0, atol=1e-9, equal_nan=True)
        changed |= ~equal
        changed_fields[column] = int((~equal).sum())
    if changed.any():
        sample_columns = [
            *key,
            *[
                name
                for column in NUMERIC_COLUMNS
                for name in (f"{column}_current", f"{column}_candidate")
            ],
        ]
        sample = common.loc[changed, sample_columns].head(3).to_dict(
            orient="records"
        )
        counts = ", ".join(
            f"{field}={count}"
            for field, count in changed_fields.items()
            if count
        )
        raise ValueError(
            f"新 Excel 改写了 {int(changed.sum())} 条历史记录（{counts}）；"
            f"示例：{sample}"
        )

    return {
        "current_rows": len(current),
        "candidate_rows": len(candidate),
        "added_rows": int(key_diff["_merge"].eq("right_only").sum()),
        "removed_rows": 0,
        "changed_rows": 0,
    }


def _atomic_replace_many(staged_targets: list[tuple[Path, Path]]) -> None:
    token = uuid.uuid4().hex
    backups: dict[Path, Path | None] = {}
    replaced: list[Path] = []
    try:
        for _, target in staged_targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup = target.parent / f".{target.name}.{token}.rollback"
                shutil.copy2(target, backup)
                backups[target] = backup
            else:
                backups[target] = None
        for staged, target in staged_targets:
            os.replace(staged, target)
            replaced.append(target)
    except Exception as exc:
        restore_errors: list[str] = []
        for target in reversed(replaced):
            backup = backups.get(target)
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                elif backup.exists():
                    os.replace(backup, target)
            except OSError as restore_exc:
                restore_errors.append(f"{target}: {restore_exc}")
        if restore_errors:
            raise RuntimeError(
                "正式文件切换失败且回滚不完整：" + "; ".join(restore_errors)
            ) from exc
        raise
    finally:
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)


def apply_basis_update(
    input_path: Path | str | None = None,
    *,
    config_path: Path | str | None = None,
    project_root: Path = PROJECT_ROOT,
    build_function: Callable[..., dict[str, Any]] = build_basis_database,
    now: datetime | None = None,
) -> dict[str, Any]:
    settings = load_basis_update_config(
        config_path,
        project_root=project_root,
    )
    incoming = Path(input_path).resolve() if input_path else settings["incoming_excel"]
    current_excel = settings["current_excel"]
    current_parquet = settings["current_parquet"]
    status_file = settings["status_file"]
    if incoming == current_excel:
        raise ValueError("上传路径不能与当前正式 Excel 相同")
    if not incoming.is_file():
        raise FileNotFoundError(f"未找到待更新 Excel：{incoming}")
    if incoming.suffix.lower() != ".xlsx":
        raise ValueError("待更新文件必须是 .xlsx")
    if not current_excel.is_file():
        raise FileNotFoundError(f"未找到当前正式 Excel：{current_excel}")
    if not current_parquet.is_file():
        raise FileNotFoundError(f"未找到当前正式 Parquet：{current_parquet}")

    token = uuid.uuid4().hex
    staged_excel = current_excel.parent / (
        f".{current_excel.stem}.{token}.tmp.xlsx"
    )
    staged_parquet = current_parquet.parent / (
        f".{current_parquet.stem}.{token}.tmp.parquet"
    )
    staged_status = status_file.parent / f".{status_file.name}.{token}.tmp"
    temporary_paths = [staged_excel, staged_parquet, staged_status]
    cleanup_incoming = incoming == settings["incoming_excel"]

    try:
        current_excel.parent.mkdir(parents=True, exist_ok=True)
        current_parquet.parent.mkdir(parents=True, exist_ok=True)
        status_file.parent.mkdir(parents=True, exist_ok=True)
        if cleanup_incoming:
            os.replace(incoming, staged_excel)
        else:
            shutil.copy2(incoming, staged_excel)

        try:
            build_result = build_function(
                config_path=settings["config_path"],
                output_path=staged_parquet,
                source_path=staged_excel,
            )
        except Exception as exc:
            raise ValueError(f"Excel 解析或 Parquet 生成失败：{exc}") from exc

        candidate = pd.read_parquet(staged_parquet)
        _validate_candidate(candidate, build_result, settings)
        current = pd.read_parquet(current_parquet)
        comparison = _compare_history(
            current,
            candidate,
            settings["primary_key"],
        )
        latest_date = pd.to_datetime(candidate["date"], errors="coerce").max()
        executed_at = (now or datetime.now().astimezone()).isoformat(
            timespec="seconds"
        )
        status = _read_status(status_file)
        excel_sha256 = _sha256(staged_excel)
        parquet_sha256 = _sha256(staged_parquet)
        status["basis_update"] = {
            "status": "success",
            "executed_at": executed_at,
            "latest_business_date": latest_date.strftime("%Y-%m-%d"),
            "rows": len(candidate),
            "added_rows": comparison["added_rows"],
            "excel_sha256": excel_sha256,
            "parquet_sha256": parquet_sha256,
        }
        staged_status.write_text(
            json.dumps(status, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _atomic_replace_many(
            [
                (staged_excel, current_excel),
                (staged_parquet, current_parquet),
                (staged_status, status_file),
            ]
        )

        return {
            **comparison,
            "input_file": incoming,
            "excel_file": current_excel,
            "parquet_file": current_parquet,
            "status_file": status_file,
            "latest_date": latest_date,
            "excel_sha256": excel_sha256,
            "parquet_sha256": parquet_sha256,
        }
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)
