"""Validated historical/SQL cutover and atomic activation for basis data."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import pyarrow.parquet as pq
import yaml

from agri_research_agent.core.paths import CONFIG_DIR, PROJECT_ROOT
from agri_research_agent.data_sources.basis_database import DATABASE_COLUMNS
from agri_research_agent.data_sources.basis_sql_loader import (
    NEAR_CONTRACT_GROUP_KEY,
    OUTPUT_DELIVERY_MONTH,
    PAGE_KEY,
    PRODUCT_MAP,
    STABLE_KEY,
    build_basis_candidate,
    sha256_file,
)


DEFAULT_CONFIG_PATH = CONFIG_DIR / "basis_sql.yaml"
CUTOVER_DATE = pd.Timestamp("2026-06-01")


def _resolve(value: object, project_root: Path) -> Path:
    path = Path(str(value))
    return (project_root / path).resolve() if not path.is_absolute() else path.resolve()


def load_sql_update_config(
    config_path: Path | str | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    path = Path(config_path or DEFAULT_CONFIG_PATH).resolve()
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError(f"基差 SQL 配置必须是 YAML 对象：{path}")
    source = config.get("source", {})
    history = config.get("history", {})
    output = config.get("output", {})
    quality = config.get("quality", {})
    required = {
        "source_sql": source.get("path"),
        "history_baseline": history.get("baseline_path"),
        "history_identity": history.get("identity_path"),
        "current_parquet": output.get("parquet_path"),
        "status_file": output.get("status_path"),
        "backup_dir": output.get("backup_dir"),
        "quality_report": output.get("quality_report_path"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError("基差 SQL 配置缺少路径：" + ", ".join(missing))
    source_backup_value = history.get("source_backup_path")
    if not source_backup_value:
        raise ValueError("基差 SQL 配置缺少历史基线引导来源")
    primary_key = quality.get("primary_key")
    if primary_key != STABLE_KEY:
        raise ValueError(f"基差稳定键配置必须为：{STABLE_KEY}")
    near_contract_group_key = quality.get(
        "near_contract_group_key", NEAR_CONTRACT_GROUP_KEY
    )
    if near_contract_group_key != NEAR_CONTRACT_GROUP_KEY:
        raise ValueError(
            "近月合约分组键配置必须为："
            f"{NEAR_CONTRACT_GROUP_KEY}"
        )
    delivery_month_value = str(
        quality.get("delivery_month_value", OUTPUT_DELIVERY_MONTH)
    )
    if delivery_month_value != OUTPUT_DELIVERY_MONTH:
        raise ValueError(
            f"正式 delivery_month 配置必须为：{OUTPUT_DELIVERY_MONTH}"
        )
    cutover_date = pd.Timestamp(history.get("cutover_date", ""))
    if cutover_date != CUTOVER_DATE:
        raise ValueError(f"基差切换日期必须为：{CUTOVER_DATE:%Y-%m-%d}")
    expected_history_sha256 = str(history.get("source_backup_sha256", "")).lower()
    if len(expected_history_sha256) != 64:
        raise ValueError("历史基线引导来源必须配置预期 SHA-256")
    minimum_latest_value = quality.get("minimum_latest_date")
    if not minimum_latest_value:
        raise ValueError("基差 SQL 配置缺少最小业务日期门槛")
    minimum_latest_date = pd.Timestamp(minimum_latest_value)
    return {
        "config_path": path,
        **{name: _resolve(value, project_root) for name, value in required.items()},
        "history_source_backup": _resolve(source_backup_value, project_root),
        "history_source_backup_label": str(source_backup_value),
        "history_baseline_label": str(history.get("baseline_path")),
        "table": str(source.get("table", "")),
        "expected_history_sha256": expected_history_sha256,
        "cutover_date": cutover_date,
        "no_valid_group_limit": float(quality.get("no_valid_group_limit", 0.01)),
        "minimum_latest_date": minimum_latest_date.strftime("%Y-%m-%d"),
        "primary_key": list(primary_key),
        "near_contract_group_key": list(near_contract_group_key),
        "delivery_month_value": delivery_month_value,
    }


def _identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
        "sha256": sha256_file(path),
    }


def _validate_schema(candidate: Path, current: Path) -> None:
    candidate_schema = pq.read_schema(candidate).remove_metadata()
    current_schema = pq.read_schema(current).remove_metadata()
    if candidate_schema != current_schema:
        raise ValueError(
            "候选 Parquet 字段或类型与正式契约不兼容："
            f"candidate={candidate_schema}, current={current_schema}"
        )


def _validate_candidate(
    candidate: Path,
    result: dict[str, Any],
    minimum_latest_date: str,
) -> pd.DataFrame:
    frame = pd.read_parquet(candidate)
    if frame.columns.tolist() != DATABASE_COLUMNS:
        raise ValueError(f"候选字段顺序不兼容：{frame.columns.tolist()}")
    if frame.empty:
        raise ValueError("候选 Parquet 为空")
    if frame.duplicated(STABLE_KEY, keep=False).any():
        raise ValueError("候选稳定键存在重复")
    page_contracts = frame.groupby(PAGE_KEY, dropna=False)["futures_contract"].nunique()
    if (page_contracts > 1).any():
        raise ValueError("候选页面粒度存在多个期货合约")
    if set(frame["commodity"].astype(str)) != set(PRODUCT_MAP.values()):
        raise ValueError("候选品种不是固定五品种")
    if set(frame["quote_type"].astype(str)) != {"基差报价"}:
        raise ValueError("候选混入非现货基差报价")
    if set(frame["delivery_month"].astype(str)) != {OUTPUT_DELIVERY_MONTH}:
        raise ValueError("候选 delivery_month 必须全部为现货")
    if frame["basis"].isna().any():
        raise ValueError("候选基差存在空值")
    latest = pd.to_datetime(frame["date"], errors="raise").max()
    if latest < pd.Timestamp(minimum_latest_date):
        raise ValueError(
            "候选最新业务日期早于允许下限："
            f"实际={latest:%Y-%m-%d}，下限={minimum_latest_date}"
        )
    if result["source_sha256_before"] != result["source_sha256_after"]:
        raise ValueError("SQL 读取前后 SHA-256 不一致")
    return frame


def _coerce_contract_schema(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.loc[:, DATABASE_COLUMNS].copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise").astype(
        "datetime64[us]"
    )
    for column in (
        "commodity",
        "region",
        "quote_type",
        "delivery_month",
        "futures_contract",
        "source_sheet",
    ):
        result[column] = result[column].astype("string")
    for column in ("cash_price", "futures_price", "basis"):
        result[column] = pd.to_numeric(result[column], errors="coerce").astype(
            "float64"
        )
    return result


def _sorted(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.sort_values(STABLE_KEY, kind="mergesort").reset_index(drop=True)


def _write_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def _assert_same_rows(actual: pd.DataFrame, expected: pd.DataFrame, label: str) -> None:
    try:
        pd.testing.assert_frame_equal(
            _sorted(_coerce_contract_schema(actual)),
            _sorted(_coerce_contract_schema(expected)),
            check_dtype=True,
            check_exact=True,
        )
    except AssertionError as exc:
        raise ValueError(f"{label}与可信来源不一致") from exc


def _history_identity_payload(
    source_backup_label: str,
    source_sha256: str,
    baseline_label: str,
    baseline: Path,
    history: pd.DataFrame,
) -> dict[str, Any]:
    return {
        "source_backup_path": source_backup_label,
        "source_backup_sha256": source_sha256,
        "cutover_date": CUTOVER_DATE.strftime("%Y-%m-%d"),
        "history_rows": len(history),
        "earliest_date": history["date"].min().strftime("%Y-%m-%d"),
        "latest_date": history["date"].max().strftime("%Y-%m-%d"),
        "history_baseline_path": baseline_label,
        "history_baseline_sha256": sha256_file(baseline),
    }


def _validate_existing_history(
    baseline: Path,
    identity_path: Path,
    *,
    source_backup_label: str,
    source_sha256: str,
    baseline_label: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if not baseline.is_file() or not identity_path.is_file():
        raise ValueError("历史基线和身份记录必须同时存在")
    history = pd.read_parquet(baseline)
    if (pd.to_datetime(history["date"]) >= CUTOVER_DATE).any():
        raise ValueError("稳定历史基线包含切换日及之后的记录")
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    expected = _history_identity_payload(
        source_backup_label,
        source_sha256,
        baseline_label,
        baseline,
        history,
    )
    if identity != expected:
        raise ValueError("稳定历史基线身份记录不匹配")
    return _coerce_contract_schema(history), identity


def _validate_merged_candidate(
    candidate: Path,
    history: pd.DataFrame,
    sql_after_cutover: pd.DataFrame,
    current: Path,
    minimum_latest_date: str,
) -> pd.DataFrame:
    frame = pd.read_parquet(candidate)
    if frame.columns.tolist() != DATABASE_COLUMNS:
        raise ValueError(f"合并候选字段顺序不兼容：{frame.columns.tolist()}")
    if frame.duplicated(STABLE_KEY, keep=False).any():
        raise ValueError("合并候选稳定键存在重复")
    dates = pd.to_datetime(frame["date"], errors="raise")
    actual_history = frame.loc[dates < CUTOVER_DATE]
    actual_sql = frame.loc[dates >= CUTOVER_DATE]
    _assert_same_rows(actual_history, history, "切换日前历史部分")
    _assert_same_rows(actual_sql, sql_after_cutover, "切换日起 SQL 部分")
    if actual_history["source_sheet"].astype(str).str.startswith("basis_price:").any():
        raise ValueError("切换日前混入 SQL 来源")
    if not actual_sql["source_sheet"].astype(str).str.startswith("basis_price:").all():
        raise ValueError("切换日起混入非 SQL 来源")
    latest = dates.max()
    if latest < pd.Timestamp(minimum_latest_date):
        raise ValueError(
            "合并候选最新业务日期早于允许下限："
            f"实际={latest:%Y-%m-%d}，下限={minimum_latest_date}"
        )
    _validate_schema(candidate, current)
    return frame


def _read_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("update_status.json 顶层必须是对象")
    return value


def _atomic_replace(staged: list[tuple[Path, Path]]) -> None:
    token = uuid.uuid4().hex
    rollback: dict[Path, Path | None] = {}
    replaced: list[Path] = []
    try:
        for _, target in staged:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                copy = target.parent / f".{target.name}.{token}.rollback"
                shutil.copy2(target, copy)
                rollback[target] = copy
            else:
                rollback[target] = None
        for source, target in staged:
            os.replace(source, target)
            replaced.append(target)
    except Exception as exc:
        errors: list[str] = []
        for target in reversed(replaced):
            backup = rollback[target]
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(backup, target)
            except OSError as restore_error:
                errors.append(f"{target}: {restore_error}")
        if errors:
            raise RuntimeError("正式基差切换失败且回滚不完整：" + "; ".join(errors)) from exc
        raise
    finally:
        for backup in rollback.values():
            if backup is not None:
                backup.unlink(missing_ok=True)


def apply_basis_sql_update(
    *,
    config_path: Path | str | None = None,
    project_root: Path = PROJECT_ROOT,
    builder: Callable[..., dict[str, Any]] = build_basis_candidate,
    now: datetime | None = None,
) -> dict[str, Any]:
    settings = load_sql_update_config(config_path, project_root=project_root)
    source = settings["source_sql"]
    source_backup = settings["history_source_backup"]
    history_baseline = settings["history_baseline"]
    history_identity_path = settings["history_identity"]
    current = settings["current_parquet"]
    status_path = settings["status_file"]
    quality_report_path = settings["quality_report"]
    if settings["table"] != "basis_price":
        raise ValueError("正式基差 SQL 表必须为 basis_price")
    if not source.is_file():
        raise FileNotFoundError(f"国内基差 SQL 不存在：{source}")
    if not current.is_file():
        raise FileNotFoundError(f"正式基差 Parquet 不存在：{current}")
    sql_identity_before = _identity(source)
    current_latest = pd.to_datetime(
        pd.read_parquet(current, columns=["date"])["date"], errors="raise"
    ).max()
    required_latest_date = max(
        pd.Timestamp(settings["minimum_latest_date"]),
        pd.Timestamp(current_latest),
    ).strftime("%Y-%m-%d")

    old_identity = _identity(current)
    executed_at = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    with tempfile.TemporaryDirectory(prefix="basis_sql_candidate_") as directory:
        directory_path = Path(directory)
        sql_candidate_first = directory_path / "basis_sql_first.parquet"
        sql_candidate_second = directory_path / "basis_sql_second.parquet"
        result = builder(
            source,
            sql_candidate_first,
            no_valid_group_limit=settings["no_valid_group_limit"],
            minimum_latest_date=required_latest_date,
        )
        second_result = builder(
            source,
            sql_candidate_second,
            no_valid_group_limit=settings["no_valid_group_limit"],
            minimum_latest_date=required_latest_date,
        )
        sql_full = _validate_candidate(
            sql_candidate_first,
            result,
            required_latest_date,
        )
        _validate_candidate(
            sql_candidate_second,
            second_result,
            required_latest_date,
        )
        source_hashes = {
            sql_identity_before["sha256"],
            result["source_sha256_before"],
            result["source_sha256_after"],
            second_result["source_sha256_before"],
            second_result["source_sha256_after"],
        }
        if len(source_hashes) != 1:
            raise ValueError("SQL 在连续两次候选生成期间发生变化")
        if sql_candidate_first.read_bytes() != sql_candidate_second.read_bytes():
            raise ValueError("SQL 候选连续两次生成的二进制结果不一致")
        sql_dates = pd.to_datetime(sql_full["date"], errors="raise")
        sql_after_cutover = _coerce_contract_schema(
            sql_full.loc[sql_dates >= CUTOVER_DATE]
        )
        if sql_after_cutover.empty:
            raise ValueError("切换日起 SQL 候选为空")

        staged_history: Path | None = None
        staged_history_identity: Path | None = None
        if history_baseline.exists() or history_identity_path.exists():
            history, history_identity = _validate_existing_history(
                history_baseline,
                history_identity_path,
                source_backup_label=settings["history_source_backup_label"],
                source_sha256=settings["expected_history_sha256"],
                baseline_label=settings["history_baseline_label"],
            )
        else:
            if not source_backup.is_file():
                raise FileNotFoundError(
                    f"首次生成稳定历史基线需要指定备份：{source_backup}"
                )
            history_source_identity = _identity(source_backup)
            if history_source_identity["sha256"] != settings["expected_history_sha256"]:
                raise ValueError("历史备份 SHA-256 与受信身份不一致")
            old_backup = pd.read_parquet(source_backup)
            old_dates = pd.to_datetime(old_backup["date"], errors="raise")
            expected_history = _coerce_contract_schema(
                old_backup.loc[old_dates < CUTOVER_DATE]
            )
            if expected_history.empty:
                raise ValueError("切换日前历史基线为空")
            staged_history = directory_path / history_baseline.name
            _write_parquet(expected_history, staged_history)
            staged_history_identity = directory_path / history_identity_path.name
            history = pd.read_parquet(staged_history)
            _assert_same_rows(history, expected_history, "新生成的历史基线")
            history_identity = _history_identity_payload(
                settings["history_source_backup_label"],
                history_source_identity["sha256"],
                settings["history_baseline_label"],
                staged_history,
                history,
            )
            staged_history_identity.write_text(
                json.dumps(history_identity, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        merged = _sorted(
            _coerce_contract_schema(
                pd.concat([history, sql_after_cutover], ignore_index=True)
            )
        )
        merged_first = directory_path / "basis_merged_first.parquet"
        merged_second = directory_path / "basis_merged_second.parquet"
        _write_parquet(merged, merged_first)
        _write_parquet(merged, merged_second)
        if merged_first.read_bytes() != merged_second.read_bytes():
            raise ValueError("合并候选连续两次生成的二进制结果不一致")
        frame = _validate_merged_candidate(
            merged_first,
            history,
            sql_after_cutover,
            current,
            required_latest_date,
        )

        quality_report = {
            "cutover_date": CUTOVER_DATE.strftime("%Y-%m-%d"),
            "rule": "date < cutover: history; date >= cutover: basis_price",
            "history_source": {
                "path": settings["history_source_backup_label"],
                "sha256": settings["expected_history_sha256"],
            },
            "sql_source": sql_identity_before,
            "history_baseline": history_identity,
            "history_rows": len(history),
            "sql_after_cutover_rows": len(sql_after_cutover),
            "merged_rows": len(frame),
            "merged_earliest_date": frame["date"].min().strftime("%Y-%m-%d"),
            "merged_latest_date": frame["date"].max().strftime("%Y-%m-%d"),
            "stable_key": STABLE_KEY,
            "stable_key_duplicates": int(frame.duplicated(STABLE_KEY).sum()),
            "source_overlap_dates": [],
            "sql_candidate_deterministic": True,
            "merged_candidate_deterministic": True,
            "sql_quality": {
                "raw_rows": result["raw_rows"],
                "spot_basis_rows": result["spot_basis_rows"],
                "valid_basis_rows": result["valid_basis_rows"],
                "far_contract_excluded_rows": result["far_contract_excluded_rows"],
                "missing_contract_rows": result["missing_contract_rows"],
                "invalid_contract_rows": result["invalid_contract_rows"],
                "expired_contract_rows": result["expired_contract_rows"],
            },
        }

        backup_directory = settings["backup_dir"] / datetime.now().strftime(
            "%Y%m%d_%H%M%S_%f"
        )
        backup_directory.mkdir(parents=True, exist_ok=False)
        backup = backup_directory / current.name
        shutil.copy2(current, backup)
        if sha256_file(backup) != old_identity["sha256"]:
            raise ValueError("正式 Parquet 备份 SHA-256 不一致")

        staged_parquet = current.parent / f".{current.name}.{uuid.uuid4().hex}.tmp"
        staged_status = status_path.parent / f".{status_path.name}.{uuid.uuid4().hex}.tmp"
        staged_quality = quality_report_path.parent / f".{quality_report_path.name}.{uuid.uuid4().hex}.tmp"
        try:
            shutil.copy2(merged_first, staged_parquet)
            status = _read_status(status_path)
            status["basis_update"] = {
                "status": "success",
                "source": "history_before_20260601_plus_basis_price_sql",
                "executed_at": executed_at,
                "latest_business_date": result["latest_date"].strftime("%Y-%m-%d"),
                "rows": len(frame),
                "cutover_date": CUTOVER_DATE.strftime("%Y-%m-%d"),
                "history_rows": len(history),
                "sql_after_cutover_rows": len(sql_after_cutover),
                "sql_sha256": result["source_sha256_after"],
                "parquet_sha256": sha256_file(staged_parquet),
                "far_contract_excluded_rows": result["far_contract_excluded_rows"],
                "missing_contract_rows": result["missing_contract_rows"],
                "invalid_contract_rows": result["invalid_contract_rows"],
                "expired_contract_rows": result["expired_contract_rows"],
                "no_valid_groups": len(result["no_valid_groups"]),
                "raw_delivery_month_counts": result["raw_delivery_month_counts"],
                "merged_delivery_month_groups": len(
                    result["merged_delivery_month_groups"]
                ),
            }
            status_path.parent.mkdir(parents=True, exist_ok=True)
            staged_status.write_text(
                json.dumps(status, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            quality_report_path.parent.mkdir(parents=True, exist_ok=True)
            staged_quality.write_text(
                json.dumps(quality_report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            staged: list[tuple[Path, Path]] = [
                (staged_parquet, current),
                (staged_status, status_path),
                (staged_quality, quality_report_path),
            ]
            if staged_history is not None and staged_history_identity is not None:
                staged.extend(
                    [
                        (staged_history, history_baseline),
                        (staged_history_identity, history_identity_path),
                    ]
                )
            _atomic_replace(staged)
        finally:
            staged_parquet.unlink(missing_ok=True)
            staged_status.unlink(missing_ok=True)
            staged_quality.unlink(missing_ok=True)

    return {
        **result,
        "rows": len(frame),
        "old_parquet": old_identity,
        "new_parquet": _identity(current),
        "backup_file": backup,
        "status_file": status_path,
        "quality_report": quality_report_path,
        "history_baseline": _identity(history_baseline),
        "history_identity": history_identity_path,
        "history_rows": len(history),
        "sql_after_cutover_rows": len(sql_after_cutover),
    }
