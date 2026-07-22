"""Atomically refresh the page-facing weather Parquet snapshot from Navicat SQL.

The job never connects to MySQL or executes SQL.  It reuses the repository's
quote-aware streaming parser and creates the twelve daily weather files from a
verified SQL snapshot.  Four independent 30-year climate baselines are copied
byte-for-byte from the prior formal snapshot instead of being derived from SQL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "04_scripts"
SRC_DIR = PROJECT_ROOT / "03_src"
for directory in (SCRIPTS_DIR, SRC_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from agri_research_agent.weather.crop_weather import load_weather_config  # noqa: E402
from import_crop_weather_snapshot import (  # noqa: E402
    ARROW_SCHEMA,
    REQUIRED_COLUMNS,
    SnapshotParseError,
    TableSpec,
    TARGET_TABLES,
    _record_rows,
    _write_chunk,
    expected_columns,
    insert_table_name,
    parse_create_statement,
    parse_insert_statement,
    sha256_file,
    stream_sql_statements,
    target_tables,
    validate_candidate,
)


SCRIPT_VERSION = "1.0.0"
RUNTIME_ROOT = Path("/home/ubuntu/market-data-runtime/weather")
DAILY_FILES = (
    ("02_configs/soybean_weather_us.yaml", "soybean/us/soybean_weather_us.parquet"),
    ("02_configs/soybean_weather_br.yaml", "soybean/br/soybean_weather_br.parquet"),
    ("02_configs/soybean_weather_ar.yaml", "soybean/ar/soybean_weather_ar.parquet"),
    ("02_configs/rapeseed_weather_can.yaml", "rapeseed/can/rapeseed_weather_can.parquet"),
    ("02_configs/rapeseed_weather_aus.yaml", "rapeseed/aus/rapeseed_weather_aus.parquet"),
    ("02_configs/rapeseed_weather_eu.yaml", "rapeseed/eu/rapeseed_weather_eu.parquet"),
    ("02_configs/rapeseed_weather_rus.yaml", "rapeseed/rus/rapeseed_weather_rus.parquet"),
    ("02_configs/rapeseed_weather_ukr.yaml", "rapeseed/ukr/rapeseed_weather_ukr.parquet"),
    ("02_configs/palm_oil_weather_mys.yaml", "palm_oil/mys/palm_oil_weather_mys.parquet"),
    ("02_configs/palm_oil_weather_idn.yaml", "palm_oil/idn/palm_oil_weather_idn.parquet"),
    ("02_configs/cotton_weather_ind.yaml", "cotton/ind/cotton_weather_ind.parquet"),
    ("02_configs/sugarcane_weather_ind.yaml", "sugarcane/ind/sugarcane_weather_ind.parquet"),
)
STATIC_BASELINE_FILES = (
    "soybean/us/soybean_weather_us_30y_normal.parquet",
    "soybean/br/soybean_weather_br_30y_normal.parquet",
    "soybean/ar/soybean_weather_ar_30y_normal.parquet",
    "rapeseed/can/rapeseed_weather_can_30y_normal.parquet",
)
SQL_PREFIX_OVERRIDES = {"soybean_weather_us.yaml": "美国"}


@dataclass
class DailyTarget:
    config_path: Path
    relative_path: Path
    config: dict[str, Any]
    table_specs: dict[str, TableSpec]
    reference_schema: dict[str, str]
    reference_columns: list[str]
    source_rows: dict[str, int] = field(default_factory=dict)
    schemas: dict[str, list[str]] = field(default_factory=dict)
    chunks: list[dict[str, object]] = field(default_factory=list)
    source_value_issues: list[dict[str, str]] = field(default_factory=list)
    writer: pq.ParquetWriter | None = None


def _json_default(value: object) -> str:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _git_identity() -> str | None:
    result = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _parquet_metadata(path: Path, *, date_column: str) -> dict[str, object]:
    if not path.is_file():
        raise SnapshotParseError(f"缺少必需的 Parquet 文件：{path}")
    data = pd.read_parquet(path)
    if date_column not in data.columns:
        raise SnapshotParseError(f"{path.name} 缺少日期字段 {date_column}")
    if date_column == "month_day":
        month_days = data[date_column].astype("string")
        if month_days.isna().any() or not month_days.str.fullmatch(r"(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])").all():
            raise SnapshotParseError(f"{path.name} 的 month_day 不符合 MM-DD 契约")
        minimum = str(month_days.min()) if not data.empty else None
        maximum = str(month_days.max()) if not data.empty else None
    else:
        parsed_dates = pd.to_datetime(data[date_column], errors="coerce")
        if parsed_dates.isna().any():
            raise SnapshotParseError(f"{path.name} 的 {date_column} 存在无法解析的值")
        minimum = str(parsed_dates.min().date()) if not data.empty else None
        maximum = str(parsed_dates.max().date()) if not data.empty else None
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "row_count": int(len(data)),
        "columns": list(data.columns),
        "dtypes": {column: str(dtype) for column, dtype in data.dtypes.items()},
        f"min_{date_column}": minimum,
        f"max_{date_column}": maximum,
    }


def _daily_specs(config_path: Path, config: dict[str, Any], reference_file: Path) -> dict[str, TableSpec]:
    """Select source tables from the existing, page-facing file contract.

    The old files are the authority for whether a country exposes minimum
    temperature.  This prevents a missing optional config flag from silently
    removing an established page metric.
    """

    if config_path.name == "soybean_weather_us.yaml":
        return dict(TARGET_TABLES)
    reference = pd.read_parquet(reference_file, columns=["metric"])
    metrics = set(reference["metric"].dropna().astype(str))
    specs = target_tables(config)
    prefix = str(config["source_table_prefix"])
    if "temperature_min" in metrics:
        specs.update(
            {
                f"{prefix}_最低气温": TableSpec("temperature_min", "observed", "observed", "degC"),
                f"{prefix}_最低气温_预测_ec": TableSpec("temperature_min", "forecast", "ECMWF", "degC", "_lowtemp_ec"),
                f"{prefix}_最低气温_预测_gfs": TableSpec("temperature_min", "forecast", "GFS", "degC", "_lowtemp_gfs"),
            }
        )
    return specs


def _load_targets(contract_dir: Path) -> list[DailyTarget]:
    targets: list[DailyTarget] = []
    for config_relative, output_relative in DAILY_FILES:
        config_path = PROJECT_ROOT / config_relative
        reference_file = contract_dir / output_relative
        metadata = _parquet_metadata(reference_file, date_column="date")
        config = load_weather_config(config_path)
        table_specs = _daily_specs(config_path, config, reference_file)
        targets.append(
            DailyTarget(
                config_path=config_path,
                relative_path=Path(output_relative),
                config=config,
                table_specs=table_specs,
                reference_schema=dict(metadata["dtypes"]),
                reference_columns=list(metadata["columns"]),
                source_rows={table: 0 for table in table_specs},
            )
        )
    return targets


def _contract_columns(target: DailyTarget, table_name: str, actual_columns: list[str]) -> list[str]:
    """Accept only the documented SQL-v2 country-prefix column migration.

    Earlier snapshots stored US fields as ``_Illinois`` while the current
    Navicat export names the same ordered field ``美国_伊利诺伊州``.  The mapping
    is positional only after every expected column has an exact, deterministic
    country-prefix expansion; any other shape change remains a hard failure.
    """

    expected = expected_columns(target.config, target.table_specs[table_name])
    if actual_columns == expected:
        return expected
    prefix = str(target.config.get("source_table_prefix") or SQL_PREFIX_OVERRIDES.get(target.config_path.name, ""))
    if not prefix:
        raise SnapshotParseError(f"{target.relative_path} 缺少受控 SQL 列前缀映射")
    expanded = [expected[0], *(f"{prefix}_{column.lstrip('_')}" for column in expected[1:])]
    if actual_columns == expanded:
        return expected
    raise SnapshotParseError(f"表 {table_name} 的字段不符合 {target.relative_path} 的受控映射")


def _remove_tree(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise SnapshotParseError(f"预期目录却发现文件：{path}")
        shutil.rmtree(path)


def _prepare_next(processed_root: Path, static_source: Path) -> tuple[Path, dict[str, dict[str, object]]]:
    next_dir = processed_root / "next"
    _remove_tree(next_dir)
    next_dir.mkdir(parents=True, exist_ok=False)
    baseline_before: dict[str, dict[str, object]] = {}
    for relative in STATIC_BASELINE_FILES:
        source = static_source / relative
        before = _parquet_metadata(source, date_column="month_day")
        target = next_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        after = _parquet_metadata(target, date_column="month_day")
        if before["sha256"] != after["sha256"]:
            raise SnapshotParseError(f"静态气候基准复制哈希不一致：{relative}")
        baseline_before[relative] = before
    return next_dir, baseline_before


def _flush(target: DailyTarget, output: Path) -> None:
    if target.chunks:
        output.parent.mkdir(parents=True, exist_ok=True)
        target.writer = _write_chunk(target.writer, target.chunks, output)
        target.chunks.clear()


def _generate_daily_files(source: Path, next_dir: Path, targets: list[DailyTarget]) -> tuple[int, set[str]]:
    by_table: dict[str, list[DailyTarget]] = {}
    for target in targets:
        for table in target.table_specs:
            by_table.setdefault(table, []).append(target)
    source_updated_at = pd.Timestamp(datetime.fromtimestamp(source.stat().st_mtime, tz=timezone.utc))
    sql_table_count = 0
    seen_sql_tables: set[str] = set()
    try:
        for statement in stream_sql_statements(source):
            parsed_create = parse_create_statement(statement)
            if parsed_create is not None:
                table_name, columns = parsed_create
                sql_table_count += 1
                seen_sql_tables.add(table_name)
                for target in by_table.get(table_name, []):
                    target.schemas[table_name] = _contract_columns(target, table_name, columns)
                continue
            table_name = insert_table_name(statement)
            interested = by_table.get(table_name or "", [])
            if not interested:
                continue
            parsed_insert = parse_insert_statement(statement)
            if parsed_insert is None:
                continue
            parsed_table_name, rows = parsed_insert
            if parsed_table_name != table_name:
                raise SnapshotParseError("INSERT 表名解析不一致")
            for target in interested:
                columns = target.schemas.get(table_name)
                if columns is None:
                    raise SnapshotParseError(f"表 {table_name} 在 INSERT 前缺少可验证的 CREATE TABLE")
                target.source_rows[table_name] += len(rows)
                for record in _record_rows(
                    table_name,
                    columns,
                    rows,
                    target.config,
                    source_updated_at,
                    target.table_specs,
                    target.source_value_issues,
                ):
                    target.chunks.append(record)
                    if len(target.chunks) >= 50_000:
                        _flush(target, next_dir / target.relative_path)
    finally:
        for target in targets:
            output = next_dir / target.relative_path
            if target.chunks:
                _flush(target, output)
            if target.writer is not None:
                target.writer.close()
    return sql_table_count, seen_sql_tables


def _validate_daily_target(target: DailyTarget, next_dir: Path) -> dict[str, object]:
    missing_ddl = sorted(set(target.table_specs) - set(target.schemas))
    if missing_ddl:
        raise SnapshotParseError(f"{target.relative_path} 缺少目标表 DDL：{'、'.join(missing_ddl)}")
    missing_rows = sorted(table for table, count in target.source_rows.items() if count == 0)
    if missing_rows:
        raise SnapshotParseError(f"{target.relative_path} 缺少目标表 INSERT：{'、'.join(missing_rows)}")
    output = next_dir / target.relative_path
    if not output.is_file():
        raise SnapshotParseError(f"未生成目标文件：{target.relative_path}")
    quality = validate_candidate(output, target.config, source_value_issues=target.source_value_issues)
    metadata = _parquet_metadata(output, date_column="date")
    if metadata["columns"] != target.reference_columns:
        raise SnapshotParseError(f"{target.relative_path} 字段顺序或名称偏离既有页面契约")
    if metadata["dtypes"] != target.reference_schema:
        raise SnapshotParseError(f"{target.relative_path} dtype 偏离既有页面契约")
    if int(metadata["row_count"]) == 0:
        raise SnapshotParseError(f"{target.relative_path} 生成结果为空")
    metadata.update(
        {
            "source_role": "weather_sql_snapshot",
            "refresh_policy": "full_replace",
            "source_tables": sorted(target.table_specs),
            "quality": quality,
        }
    )
    return metadata


def _verify_all_files(
    next_dir: Path,
    baseline_before: dict[str, dict[str, object]],
    daily_results: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    expected = set(STATIC_BASELINE_FILES) | {output for _, output in DAILY_FILES}
    actual = {str(path.relative_to(next_dir)).replace("\\", "/") for path in next_dir.rglob("*.parquet")}
    if actual != expected:
        raise SnapshotParseError(f"正式目录文件清单不匹配；缺失={sorted(expected - actual)}，多余={sorted(actual - expected)}")
    static_after: dict[str, dict[str, object]] = {}
    for relative, before in baseline_before.items():
        after = _parquet_metadata(next_dir / relative, date_column="month_day")
        if before["sha256"] != after["sha256"] or before["columns"] != after["columns"] or before["dtypes"] != after["dtypes"]:
            raise SnapshotParseError(f"静态气候基准在刷新中发生变化：{relative}")
        after.update(
            {
                "source_role": "preserved_static_baseline",
                "refresh_policy": "preserve_unchanged",
                "pre_refresh_sha256": before["sha256"],
            }
        )
        static_after[relative] = after
    return {**static_after, **daily_results}


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _promote(processed_root: Path, next_dir: Path, status_path: Path, payload: dict[str, object]) -> bool:
    current = processed_root / "current"
    previous = processed_root / "previous"
    rolled_back = False
    moved_current = False
    try:
        _remove_tree(previous)
        if current.exists():
            os.replace(current, previous)
            moved_current = True
        os.replace(next_dir, current)
        _atomic_write_json(status_path, payload)
    except Exception:
        if moved_current and previous.exists():
            _remove_tree(current)
            os.replace(previous, current)
            rolled_back = True
        raise
    return rolled_back


def refresh(
    *,
    source: Path,
    processed_root: Path,
    contract_dir: Path,
    bootstrap_static_dir: Path,
    status_path: Path,
    promote: bool,
) -> dict[str, object]:
    if not source.is_file() or source.suffix.lower() != ".sql":
        raise SnapshotParseError("输入必须是可读的 .sql Navicat/MySQL 快照")
    started = datetime.now(timezone.utc)
    current = processed_root / "current"
    static_source = current if current.is_dir() else bootstrap_static_dir
    if not static_source.is_dir():
        raise SnapshotParseError("既有正式静态基准目录不存在，拒绝生成 next")
    targets = _load_targets(contract_dir)
    next_dir, baseline_before = _prepare_next(processed_root, static_source)
    try:
        sql_table_count, seen_sql_tables = _generate_daily_files(source, next_dir, targets)
        daily_results = {str(target.relative_path).replace("\\", "/"): _validate_daily_target(target, next_dir) for target in targets}
        files = _verify_all_files(next_dir, baseline_before, daily_results)
        latest_dates = [item.get("max_date") for item in daily_results.values() if item.get("max_date")]
        completed = datetime.now(timezone.utc)
        payload: dict[str, object] = {
            "status": "success" if promote else "validated_not_promoted",
            "source_sql_path": str(source),
            "source_sql_sha256": sha256_file(source),
            "source_sql_size_bytes": source.stat().st_size,
            "source_schema": "天气2.0",
            "sql_table_count": sql_table_count,
            "extracted_table_count": len({table for target in targets for table in target.table_specs}),
            "skipped_table_count": sql_table_count - len({table for target in targets for table in target.table_specs}),
            "skipped_reason": "not_in_page_contract",
            "parquet_file_count": len(files),
            "static_baseline_files": len(STATIC_BASELINE_FILES),
            "refreshed_daily_files": len(DAILY_FILES),
            "files": files,
            "latest_recognized_date": max(latest_dates) if latest_dates else None,
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "duration_seconds": round((completed - started).total_seconds(), 3),
            "processed_directory": str(processed_root / ("current" if promote else "next")),
            "previous_directory": str(processed_root / "previous"),
            "git_commit": _git_identity(),
            "script_version": SCRIPT_VERSION,
            "sql_tables_seen": len(seen_sql_tables),
            "rollback_performed": False,
        }
        if promote:
            payload["rollback_performed"] = _promote(processed_root, next_dir, status_path, payload)
        else:
            _atomic_write_json(status_path, payload)
        return payload
    except Exception:
        _remove_tree(next_dir)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=RUNTIME_ROOT / "current" / "weather_latest.sql")
    parser.add_argument("--processed-root", type=Path, default=RUNTIME_ROOT / "processed")
    parser.add_argument("--contract-dir", type=Path, default=PROJECT_ROOT / "01_data" / "processed" / "weather")
    parser.add_argument("--bootstrap-static-dir", type=Path, default=PROJECT_ROOT / "01_data" / "processed" / "weather")
    parser.add_argument("--status-file", type=Path, default=RUNTIME_ROOT / "status" / "weather_refresh_status.json")
    parser.add_argument("--promote", action="store_true", help="Atomically replace processed/current after validation")
    args = parser.parse_args()
    result = refresh(
        source=args.source,
        processed_root=args.processed_root,
        contract_dir=args.contract_dir,
        bootstrap_static_dir=args.bootstrap_static_dir,
        status_path=args.status_file,
        promote=args.promote,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
