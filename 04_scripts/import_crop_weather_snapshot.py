"""Stream a Navicat/MySQL SQL snapshot into a configured crop-weather contract.

The importer deliberately does not connect to a database.  It identifies only the
approved stable US summary tables, parses their DDL and INSERT statements with a
quote-aware SQL reader, writes a candidate Parquet, validates it, and optionally
promotes the candidate with a recoverable stable-file switch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "03_src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agri_research_agent.weather.crop_weather import load_weather_config  # noqa: E402


CONFIG_FILE = PROJECT_ROOT / "02_configs" / "soybean_weather_us.yaml"
CANDIDATE_ROOT = PROJECT_ROOT / "01_data" / "candidates" / "weather" / "soybean_us"
STABLE_FILE = PROJECT_ROOT / "01_data" / "processed" / "weather" / "soybean" / "us" / "soybean_weather_us.parquet"
BACKUP_ROOT = PROJECT_ROOT / "01_data" / "backups" / "weather" / "soybean_us"
STATUS_FILE = PROJECT_ROOT / "01_data" / "update_status" / "soybean_weather_us.json"


class SnapshotParseError(ValueError):
    """Raised when a dump cannot be safely mapped to the approved contract."""


@dataclass(frozen=True)
class TableSpec:
    metric: str
    data_type: str
    model: str
    unit: str
    column_suffix: str = ""


TARGET_TABLES: dict[str, TableSpec] = {
    "美国_降雨": TableSpec("precipitation", "observed", "observed", "mm"),
    "美国_降雨_预测_ec": TableSpec("precipitation", "forecast", "ECMWF", "mm", "_precip_ec"),
    "美国_降雨_预测_gfs": TableSpec("precipitation", "forecast", "GFS", "mm", "_precip_gfs"),
    "美国_最高气温": TableSpec("temperature_max", "observed", "observed", "degC"),
    "美国_最高气温_预测_ec": TableSpec("temperature_max", "forecast", "ECMWF", "degC", "_hightemp_ec"),
    "美国_最高气温_预测_gfs": TableSpec("temperature_max", "forecast", "GFS", "degC", "_hightemp_gfs"),
    "美国_最低气温": TableSpec("temperature_min", "observed", "observed", "degC"),
    "美国_最低气温_预测_ec": TableSpec("temperature_min", "forecast", "ECMWF", "degC", "_lowtemp_ec"),
    "美国_最低气温_预测_gfs": TableSpec("temperature_min", "forecast", "GFS", "degC", "_lowtemp_gfs"),
    "美国_土壤墒情": TableSpec("soil_moisture", "observed", "observed", "原始值，单位待确认"),
}


def target_tables(config: dict[str, object]) -> dict[str, TableSpec]:
    """Build only the stable summary-table identities enabled by one config."""

    prefix = str(config.get("source_table_prefix", ""))
    if not prefix:
        raise SnapshotParseError("天气配置缺少非敏感源表前缀")
    tables = {
        f"{prefix}_降雨": TableSpec("precipitation", "observed", "observed", "mm"),
        f"{prefix}_降雨_预测_ec": TableSpec("precipitation", "forecast", "ECMWF", "mm", "_precip_ec"),
        f"{prefix}_降雨_预测_gfs": TableSpec("precipitation", "forecast", "GFS", "mm", "_precip_gfs"),
        f"{prefix}_最高气温": TableSpec("temperature_max", "observed", "observed", "degC"),
        f"{prefix}_最高气温_预测_ec": TableSpec("temperature_max", "forecast", "ECMWF", "degC", "_hightemp_ec"),
        f"{prefix}_最高气温_预测_gfs": TableSpec("temperature_max", "forecast", "GFS", "degC", "_hightemp_gfs"),
        f"{prefix}_土壤墒情": TableSpec("soil_moisture", "observed", "observed", "原始值，单位待确认"),
    }
    enabled_sections = config.get("enabled_sections", {})
    if not isinstance(enabled_sections, dict):
        raise SnapshotParseError("天气配置的启用模块无效")
    if bool(enabled_sections.get("minimum_temperature", False)):
        tables.update(
            {
                f"{prefix}_最低气温": TableSpec("temperature_min", "observed", "observed", "degC"),
                f"{prefix}_最低气温_预测_ec": TableSpec("temperature_min", "forecast", "ECMWF", "degC", "_lowtemp_ec"),
                f"{prefix}_最低气温_预测_gfs": TableSpec("temperature_min", "forecast", "GFS", "degC", "_lowtemp_gfs"),
            }
        )
    return tables

REQUIRED_COLUMNS = [
    "date", "crop", "country", "region", "metric", "data_type", "model", "value", "unit", "source_updated_at",
]
ARROW_SCHEMA = pa.schema(
    [
        pa.field("date", pa.date32()),
        pa.field("crop", pa.string()),
        pa.field("country", pa.string()),
        pa.field("region", pa.string()),
        pa.field("metric", pa.string()),
        pa.field("data_type", pa.string()),
        pa.field("model", pa.string()),
        pa.field("value", pa.float64()),
        pa.field("unit", pa.string()),
        pa.field("source_updated_at", pa.timestamp("us", tz="UTC")),
        pa.field("forecast_run_at", pa.timestamp("us", tz="UTC")),
        pa.field("source_table", pa.string()),
    ]
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stream_sql_statements(path: Path) -> Iterator[str]:
    """Yield semicolon-terminated statements without loading the dump into memory."""

    buffer: list[str] = []
    quote: str | None = None
    escaped = False
    with path.open("r", encoding="utf-8", errors="strict", newline="") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            for char in chunk:
                buffer.append(char)
                if quote is not None:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == quote:
                        quote = None
                    continue
                if char in {"'", '"', "`"}:
                    quote = char
                elif char == ";":
                    yield "".join(buffer)
                    buffer.clear()
            if len(buffer) > 16 * 1024 * 1024:
                raise SnapshotParseError("单条 SQL 语句超过 16 MiB，拒绝在不明确边界下解析")
    if "".join(buffer).strip():
        raise SnapshotParseError("SQL 导出最后存在未终止语句")
    if quote is not None:
        raise SnapshotParseError("SQL 导出存在未闭合引号")


def _read_backtick_identifier(text: str, index: int) -> tuple[str, int]:
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] != "`":
        raise SnapshotParseError("目标 SQL 未使用可解析的反引号表标识")
    index += 1
    result: list[str] = []
    while index < len(text):
        char = text[index]
        if char == "`":
            if index + 1 < len(text) and text[index + 1] == "`":
                result.append("`")
                index += 2
                continue
            return "".join(result), index + 1
        result.append(char)
        index += 1
    raise SnapshotParseError("SQL 反引号标识未闭合")


def _split_top_level(text: str, separator: str = ",") -> list[str]:
    values: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise SnapshotParseError("SQL 括号层级无效")
        elif char == separator and depth == 0:
            values.append(text[start:index].strip())
            start = index + 1
    if quote is not None or depth != 0:
        raise SnapshotParseError("SQL 字符串或括号未闭合")
    values.append(text[start:].strip())
    return values


def parse_create_statement(statement: str) -> tuple[str, list[str]] | None:
    match = re.match(r"^\s*CREATE\s+TABLE\s+", statement, flags=re.IGNORECASE)
    if match is None:
        return None
    table_name, index = _read_backtick_identifier(statement, match.end())
    while index < len(statement) and statement[index].isspace():
        index += 1
    if index >= len(statement) or statement[index] != "(":
        raise SnapshotParseError(f"表 {table_name} 的 CREATE TABLE 缺少字段列表")
    closing = statement.rfind(")")
    if closing <= index:
        raise SnapshotParseError(f"表 {table_name} 的 CREATE TABLE 括号无效")
    definitions = _split_top_level(statement[index + 1:closing])
    columns: list[str] = []
    for definition in definitions:
        candidate = definition.lstrip()
        if not candidate.startswith("`"):
            continue
        column, _ = _read_backtick_identifier(candidate, 0)
        columns.append(column)
    if not columns:
        raise SnapshotParseError(f"表 {table_name} 未解析到任何字段")
    return table_name, columns


def _unescape_sql_string(token: str) -> str:
    content = token[1:-1]
    result: list[str] = []
    index = 0
    escapes = {"0": "\0", "b": "\b", "n": "\n", "r": "\r", "t": "\t", "Z": "\x1a"}
    while index < len(content):
        char = content[index]
        if char == "\\" and index + 1 < len(content):
            next_char = content[index + 1]
            result.append(escapes.get(next_char, next_char))
            index += 2
        elif char == "'" and index + 1 < len(content) and content[index + 1] == "'":
            result.append("'")
            index += 2
        else:
            result.append(char)
            index += 1
    return "".join(result)


def _parse_literal(token: str) -> str | float | None:
    value = token.strip()
    if value.upper() == "NULL":
        return None
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return _unescape_sql_string(value)
    try:
        return float(value)
    except ValueError as exc:
        raise SnapshotParseError(f"不支持的 SQL 字面量：{value[:80]}") from exc


def parse_insert_statement(statement: str) -> tuple[str, list[list[str | float | None]]] | None:
    match = re.match(r"^\s*INSERT\s+INTO\s+", statement, flags=re.IGNORECASE)
    if match is None:
        return None
    table_name, index = _read_backtick_identifier(statement, match.end())
    remainder = statement[index:].strip().rstrip(";").strip()
    values_match = re.match(r"^VALUES\s+", remainder, flags=re.IGNORECASE)
    if values_match is None:
        raise SnapshotParseError(f"表 {table_name} 的 INSERT 不符合 VALUES 语法")
    payload = remainder[values_match.end():]
    rows: list[list[str | float | None]] = []
    index = 0
    while index < len(payload):
        while index < len(payload) and (payload[index].isspace() or payload[index] == ","):
            index += 1
        if index >= len(payload):
            break
        if payload[index] != "(":
            raise SnapshotParseError(f"表 {table_name} 的 INSERT VALUES 行格式无效")
        start = index + 1
        depth = 1
        quote: str | None = None
        escaped = False
        index += 1
        while index < len(payload) and depth:
            char = payload[index]
            if quote is not None:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            elif char in {"'", '"'}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        if depth != 0 or quote is not None:
            raise SnapshotParseError(f"表 {table_name} 的 INSERT 行括号或引号未闭合")
        rows.append([_parse_literal(item) for item in _split_top_level(payload[start:index - 1])])
    return table_name, rows


def insert_table_name(statement: str) -> str | None:
    """Return an INSERT target without parsing its values payload.

    The dump can contain unrelated business tables.  Checking the target
    before parsing values keeps this importer restricted to its ten declared
    weather tables and avoids materialising unrelated raw records.
    """

    match = re.match(r"^\s*INSERT\s+INTO\s+", statement, flags=re.IGNORECASE)
    if match is None:
        return None
    table_name, _ = _read_backtick_identifier(statement, match.end())
    return table_name


def _source_regions_for_spec(config: dict[str, object], spec: TableSpec) -> list[dict[str, object]]:
    """Return the approved source columns for one table without inventing gaps.

    A configured missing forecast column remains absent from the normalized
    snapshot.  It is never represented by a zero, another model, or raw data.
    """

    regions = config.get("source_regions", config["regions"])
    if not isinstance(regions, list) or not all(isinstance(region, dict) for region in regions):
        raise SnapshotParseError("天气配置地区列表无效")
    missing = config.get("allowed_missing_forecast_regions", {})
    if not isinstance(missing, dict):
        raise SnapshotParseError("天气配置的允许缺失预测地区无效")
    omitted: set[str] = set()
    if spec.data_type == "forecast":
        by_metric = missing.get(spec.metric, {})
        if not isinstance(by_metric, dict):
            raise SnapshotParseError("天气配置的允许缺失预测指标无效")
        model_regions = by_metric.get(spec.model, [])
        if not isinstance(model_regions, list) or not all(isinstance(key, str) for key in model_regions):
            raise SnapshotParseError("天气配置的允许缺失预测地区无效")
        omitted = set(model_regions)
        known = {str(region.get("key", "")) for region in regions}
        if not omitted.issubset(known):
            raise SnapshotParseError("天气配置的允许缺失预测地区不在源地区配置中")
    return [region for region in regions if str(region.get("key", "")) not in omitted]


def expected_columns(config: dict[str, object], spec: TableSpec) -> list[str]:
    regions = _source_regions_for_spec(config, spec)
    columns = ["日期"]
    for region in regions:
        if not isinstance(region, dict):
            raise SnapshotParseError("天气配置地区项无效")
        source_column = str(region.get(f"source_column_{spec.metric}", region.get("source_column", "")))
        if not source_column:
            prefix = str(config.get("source_table_prefix", ""))
            source_column = f"{prefix}_{region['display_name']}"
        columns.append(f"{source_column}{spec.column_suffix}")
    extras_by_metric = config.get("source_extra_columns_by_metric", {})
    if not isinstance(extras_by_metric, dict):
        raise SnapshotParseError("天气配置的指标额外源列无效")
    extra_columns = extras_by_metric.get(spec.metric, []) if spec.data_type == "observed" else []
    if not isinstance(extra_columns, list) or not all(isinstance(column, str) for column in extra_columns):
        raise SnapshotParseError("天气配置的额外源列无效")
    columns.extend(extra_columns)
    return columns


def _record_rows(
    table_name: str,
    columns: list[str],
    values: list[list[str | float | None]],
    config: dict[str, object],
    source_updated_at: pd.Timestamp,
    table_specs: dict[str, TableSpec] | None = None,
    source_value_issues: list[dict[str, str]] | None = None,
) -> Iterator[dict[str, object]]:
    spec = (table_specs or TARGET_TABLES)[table_name]
    regions = _source_regions_for_spec(config, spec)
    display_keys = {str(region["key"]) for region in config["regions"]}
    if columns != expected_columns(config, spec):
        raise SnapshotParseError(f"表 {table_name} 的字段与受控源地区配置不完全一致")
    for row in values:
        if len(row) != len(columns):
            raise SnapshotParseError(f"表 {table_name} 的 INSERT 值数量与 DDL 字段数量不一致")
        date_value = row[0]
        if date_value is None:
            if any(value is not None for value in row[1:]):
                raise SnapshotParseError(f"表 {table_name} 存在无日期的非空记录")
            continue
        try:
            date = pd.Timestamp(str(date_value)).normalize()
        except (TypeError, ValueError) as exc:
            raise SnapshotParseError(f"表 {table_name} 存在无法解析的日期：{date_value}") from exc
        for region, value in zip(regions, row[1:1 + len(regions)], strict=True):
            if value is None or str(region["key"]) not in display_keys:
                continue
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                if source_value_issues is not None:
                    source_value_issues.append(
                        {"table": table_name, "date": str(date.date()), "region": str(region["key"]), "reason": "non_numeric_source_value"}
                    )
                continue
            yield {
                "date": date,
                "crop": str(config["crop"]),
                "country": str(config["country"]),
                "region": str(region["key"]),
                "metric": spec.metric,
                "data_type": spec.data_type,
                "model": spec.model,
                "value": numeric_value,
                "unit": spec.unit,
                "source_updated_at": source_updated_at,
                "forecast_run_at": source_updated_at if spec.data_type == "forecast" else pd.NaT,
                "source_table": table_name,
            }


def _write_chunk(writer: pq.ParquetWriter | None, rows: list[dict[str, object]], output: Path) -> pq.ParquetWriter:
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["source_updated_at"] = pd.to_datetime(frame["source_updated_at"], utc=True)
    frame["forecast_run_at"] = pd.to_datetime(frame["forecast_run_at"], utc=True)
    table = pa.Table.from_pandas(frame, schema=ARROW_SCHEMA, preserve_index=False, safe=True)
    if writer is None:
        writer = pq.ParquetWriter(output, ARROW_SCHEMA, compression="zstd")
    writer.write_table(table)
    return writer


def _date_range(values: pd.Series) -> dict[str, str | None]:
    if values.empty:
        return {"start": None, "end": None}
    return {"start": str(values.min().date()), "end": str(values.max().date())}


def validate_candidate(candidate_file: Path, config: dict[str, object], *, source_value_issues: list[dict[str, str]] | None = None) -> dict[str, object]:
    """Run candidate quality checks without filling, mutating, or hiding gaps."""

    data = pd.read_parquet(candidate_file)
    missing = [column for column in REQUIRED_COLUMNS if column not in data.columns]
    if missing:
        raise SnapshotParseError("候选 Parquet 缺少标准字段：" + "、".join(missing))
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data["value"] = pd.to_numeric(data["value"], errors="coerce")
    if data[REQUIRED_COLUMNS].isna().any().any():
        raise SnapshotParseError("候选 Parquet 存在空的标准字段")
    if set(data["crop"].astype(str)) != {str(config["crop"])} or set(data["country"].astype(str)) != {str(config["country"])}:
        raise SnapshotParseError("候选 Parquet 的作物或国家不符合当前配置")
    allowed_metrics = {"precipitation", "temperature_max", "temperature_min", "soil_moisture"}
    allowed_types = {"observed", "forecast"}
    allowed_models = {"observed", "ECMWF", "GFS"}
    for column, allowed in (("metric", allowed_metrics), ("data_type", allowed_types), ("model", allowed_models)):
        unexpected = sorted(set(data[column].astype(str)) - allowed)
        if unexpected:
            raise SnapshotParseError(f"候选 Parquet 的 {column} 存在未允许值：{unexpected}")
    invalid_models = data[
        ((data["data_type"] == "observed") & (data["model"] != "observed"))
        | ((data["data_type"] == "forecast") & ~data["model"].isin({"ECMWF", "GFS"}))
    ]
    if not invalid_models.empty:
        raise SnapshotParseError("候选 Parquet 的 observed/forecast 与 model 映射不一致")
    configured_regions = [str(region["key"]) for region in config["regions"]]
    actual_regions = set(data["region"].astype(str))
    if actual_regions != set(configured_regions):
        raise SnapshotParseError(f"候选 Parquet 的州集合与配置不一致：{sorted(actual_regions ^ set(configured_regions))}")
    key_columns = ["date", "region", "metric", "data_type", "model"]
    duplicates = data[data.duplicated(key_columns, keep=False)]
    if not duplicates.empty:
        raise SnapshotParseError("候选 Parquet 存在重复或冲突唯一键")
    if (data.loc[data["metric"] == "precipitation", "value"] < 0).any():
        raise SnapshotParseError("候选 Parquet 存在负降雨")
    temp = data[data["metric"].isin({"temperature_max", "temperature_min"})]
    abnormal_temperatures = int(((temp["value"] < -70) | (temp["value"] > 60)).sum())
    warnings: list[str] = ["local_historical_snapshot_not_realtime", "soil_moisture_unit_unconfirmed"]
    configured_warnings = config.get("status_warnings", [])
    if not isinstance(configured_warnings, list) or not all(isinstance(value, str) for value in configured_warnings):
        raise SnapshotParseError("天气配置的状态警示无效")
    warnings.extend(value for value in configured_warnings if value not in warnings)
    if abnormal_temperatures:
        warnings.append(f"obvious_temperature_outliers={abnormal_temperatures}")
    if source_value_issues:
        warnings.append(f"non_numeric_source_values_skipped={len(source_value_issues)}")
    observed = data[data["data_type"] == "observed"]
    forecast = data[data["data_type"] == "forecast"]
    observed_dates = observed.groupby("metric")["date"].agg(lambda values: set(values))
    forecast_dates = forecast.groupby("metric")["date"].agg(lambda values: set(values))
    overlap_metrics = sorted(metric for metric in set(observed_dates.index) & set(forecast_dates.index) if observed_dates[metric] & forecast_dates[metric])
    if overlap_metrics:
        warnings.append("observed_forecast_date_overlap=" + ",".join(overlap_metrics))
    missing_runs: dict[str, int] = {}
    for keys, group in data.groupby(["region", "metric", "data_type", "model"], sort=True):
        dates = pd.Series(pd.to_datetime(group["date"].unique())).sort_values().reset_index(drop=True)
        gaps = dates.diff().dt.days.fillna(1).sub(1).clip(lower=0)
        gap_count = int(gaps.sum())
        if gap_count:
            missing_runs["|".join(map(str, keys))] = gap_count
    table_ranges = {
        table: {"rows": int(len(group)), **_date_range(group["date"])}
        for table, group in data.groupby("source_table", sort=True)
    }
    ecmwf = forecast[forecast["model"] == "ECMWF"]
    gfs = forecast[forecast["model"] == "GFS"]
    return {
        "row_count": int(len(data)),
        "schema": {column: str(dtype) for column, dtype in data.dtypes.items()},
        "table_ranges": table_ranges,
        "historical_range": _date_range(observed["date"]),
        "observed_latest_date": str(observed["date"].max().date()),
        "ecmwf_forecast_range": _date_range(ecmwf["date"]),
        "gfs_forecast_range": _date_range(gfs["date"]),
        "soil_moisture_range": _date_range(data.loc[data["metric"] == "soil_moisture", "date"]),
        "record_counts": {"|".join(map(str, keys)): int(len(group)) for keys, group in data.groupby(["region", "metric", "model"], sort=True)},
        "missing_date_counts": missing_runs,
        "source_value_issues": source_value_issues or [],
        "warnings": warnings,
    }


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def promote_candidate(
    candidate_file: Path,
    status: dict[str, object],
    *,
    stable_file: Path | None = None,
    backup_root: Path | None = None,
    status_file: Path | None = None,
) -> tuple[Path | None, str]:
    stable_file = stable_file or STABLE_FILE
    backup_root = backup_root or BACKUP_ROOT
    status_file = status_file or STATUS_FILE
    backup_path: Path | None = None
    if stable_file.exists():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_root / timestamp / stable_file.name
        backup_path.parent.mkdir(parents=True, exist_ok=False)
        shutil.copy2(stable_file, backup_path)
        if sha256_file(stable_file) != sha256_file(backup_path):
            raise SnapshotParseError("稳定文件备份哈希不一致，拒绝切换")
    stable_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_stable = stable_file.with_name(f".{stable_file.name}.{uuid.uuid4().hex}.tmp")
    shutil.copy2(candidate_file, temporary_stable)
    os.replace(temporary_stable, stable_file)
    stable_hash = sha256_file(stable_file)
    status["stable_file_sha256"] = stable_hash
    status["status"] = "success"
    atomic_write_json(status_file, status)
    return backup_path, stable_hash


def _country_paths(config_file: Path, config: dict[str, object]) -> tuple[Path, Path, Path, Path, str]:
    """Resolve local candidate/stable/status paths without putting paths in page code."""

    slug = str(config.get("data_slug", "us"))
    if config_file == CONFIG_FILE:
        return CANDIDATE_ROOT, STABLE_FILE, BACKUP_ROOT, STATUS_FILE, slug
    crop = str(config["crop"])
    return (
        PROJECT_ROOT / "01_data" / "candidates" / "weather" / f"{crop}_{slug}",
        PROJECT_ROOT / "01_data" / "processed" / "weather" / crop / slug / f"{crop}_weather_{slug}.parquet",
        PROJECT_ROOT / "01_data" / "backups" / "weather" / f"{crop}_{slug}",
        PROJECT_ROOT / "01_data" / "update_status" / f"{crop}_weather_{slug}.json",
        slug,
    )


def import_snapshot(source: Path, *, run_id: str, promote: bool, config_file: Path = CONFIG_FILE) -> dict[str, object]:
    if not source.is_file() or source.suffix.lower() != ".sql":
        raise SnapshotParseError("输入必须是可读的 .sql Navicat/MySQL 导出文件")
    config = load_weather_config(config_file)
    table_specs = target_tables(config) if config_file != CONFIG_FILE else TARGET_TABLES
    candidate_root, stable_file, backup_root, status_file, slug = _country_paths(config_file, config)
    source_updated_at = pd.Timestamp(datetime.fromtimestamp(source.stat().st_mtime, tz=timezone.utc))
    source_hash = sha256_file(source)
    candidate_dir = candidate_root / run_id
    if candidate_dir.exists():
        raise SnapshotParseError(f"候选目录已存在，拒绝覆盖：{candidate_dir}")
    candidate_dir.mkdir(parents=True, exist_ok=False)
    crop = str(config["crop"])
    temporary_parquet = candidate_dir / f".{crop}_weather_{slug}.parquet.partial"
    candidate_file = candidate_dir / f"{crop}_weather_{slug}.parquet"
    schemas: dict[str, list[str]] = {}
    source_rows: dict[str, int] = {table: 0 for table in table_specs}
    chunk: list[dict[str, object]] = []
    source_value_issues: list[dict[str, str]] = []
    writer: pq.ParquetWriter | None = None
    try:
        for statement in stream_sql_statements(source):
            parsed_create = parse_create_statement(statement)
            if parsed_create is not None:
                table_name, columns = parsed_create
                if table_name in table_specs:
                    schemas[table_name] = columns
                continue
            table_name = insert_table_name(statement)
            if table_name is None or table_name not in table_specs:
                continue
            parsed_insert = parse_insert_statement(statement)
            if parsed_insert is None:
                continue
            parsed_table_name, rows = parsed_insert
            if parsed_table_name != table_name:
                raise SnapshotParseError("INSERT 表名解析不一致")
            if table_name not in schemas:
                raise SnapshotParseError(f"表 {table_name} 在 INSERT 前缺少可验证的 CREATE TABLE")
            source_rows[table_name] += len(rows)
            for record in _record_rows(
                table_name, schemas[table_name], rows, config, source_updated_at, table_specs, source_value_issues
            ):
                chunk.append(record)
                if len(chunk) >= 50_000:
                    writer = _write_chunk(writer, chunk, temporary_parquet)
                    chunk.clear()
        if set(schemas) != set(table_specs):
            missing = sorted(set(table_specs) - set(schemas))
            raise SnapshotParseError("缺少目标表 DDL：" + "、".join(missing))
        if any(count == 0 for count in source_rows.values()):
            missing = sorted(table for table, count in source_rows.items() if count == 0)
            raise SnapshotParseError("目标表缺少 INSERT 记录：" + "、".join(missing))
        if chunk:
            writer = _write_chunk(writer, chunk, temporary_parquet)
            chunk.clear()
        if writer is None:
            raise SnapshotParseError("未从目标表生成任何候选记录")
    finally:
        if writer is not None:
            writer.close()
    os.replace(temporary_parquet, candidate_file)
    quality = validate_candidate(candidate_file, config, source_value_issues=source_value_issues)
    candidate_hash = sha256_file(candidate_file)
    status: dict[str, object] = {
        "status": "candidate_validated",
        "run_id": run_id,
        "source_filename": source.name,
        "source_sha256": source_hash,
        "source_snapshot_date": source_updated_at.date().isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "row_count": quality["row_count"],
        "observed_latest_date": quality["observed_latest_date"],
        "ecmwf_forecast_end_date": quality["ecmwf_forecast_range"]["end"],
        "gfs_forecast_end_date": quality["gfs_forecast_range"]["end"],
        "region_count": len(config["regions"]),
        "metric_count": len(config["metrics"]),
        "warnings": quality["warnings"],
        "candidate_file_sha256": candidate_hash,
    }
    atomic_write_json(candidate_dir / "candidate_report.json", {"status": status, "quality": quality, "source_rows": source_rows})
    result: dict[str, object] = {"candidate_dir": str(candidate_dir), "candidate_file": str(candidate_file), "candidate_sha256": candidate_hash, "quality": quality, "status": status}
    if promote:
        backup_path, stable_hash = promote_candidate(candidate_file, status, stable_file=stable_file, backup_root=backup_root, status_file=status_file)
        result["stable_file"] = str(stable_file)
        result["stable_sha256"] = stable_hash
        result["backup_path"] = str(backup_path) if backup_path else None
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Verified local Navicat/MySQL .sql snapshot")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="Country weather configuration")
    parser.add_argument("--run-id", default=None, help="Unique candidate run identifier")
    parser.add_argument("--promote", action="store_true", help="Promote only after candidate validation succeeds")
    args = parser.parse_args()
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + sha256_file(args.source)[:12].lower()
    result = import_snapshot(args.source, run_id=run_id, promote=args.promote, config_file=args.config)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
