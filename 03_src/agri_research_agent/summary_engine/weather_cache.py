from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias

import pandas as pd
import streamlit as st

from agri_research_agent.data_sources.weather_adapter import validate_weather_records
from agri_research_agent.weather.crop_weather import load_weather_config

from .rules import DEFAULT_CONFIG, load_summary_rules
from .schema import Summary
from .weather import build_weather_summary


FileStatIdentity: TypeAlias = tuple[str, int, int]
NORMAL_REQUIRED_COLUMNS = {
    "month_day", "country", "region", "metric", "normal_value", "unit",
    "baseline_label", "source_workbook_sha256", "source_sheet",
}


def file_stat_identity(path: str | Path) -> FileStatIdentity:
    resolved = Path(path).resolve()
    stat = resolved.stat()
    return str(resolved), stat.st_size, stat.st_mtime_ns


def _read_weather(path: str) -> pd.DataFrame:
    source = Path(path)
    if source.suffix.lower() == ".parquet":
        raw = pd.read_parquet(source)
    elif source.suffix.lower() == ".csv":
        raw = pd.read_csv(source)
    else:
        raise ValueError("天气适配器只支持 CSV 或 Parquet 输入")
    return validate_weather_records(raw)


def _read_normals(path: str | None) -> pd.DataFrame:
    if path is None:
        return pd.DataFrame()
    data = pd.read_parquet(path)
    if missing := sorted(NORMAL_REQUIRED_COLUMNS - set(data.columns)):
        raise ValueError("30年历史基准缺少字段：" + "、".join(missing))
    return data


@st.cache_data(show_spinner=False, max_entries=48)
def _load_weather_summary_versioned(
    data_identity: FileStatIdentity,
    config_identity: FileStatIdentity,
    normal_identity: FileStatIdentity | None,
    rules_identity: FileStatIdentity,
    rule_version: str,
    calculation_version: str,
) -> Summary:
    # Identity arguments are deliberately part of the cache key.  The loader
    # reads each source only after a miss; no TTL or cache artifact is used.
    del rules_identity, rule_version, calculation_version
    data_path, data_size, data_mtime_ns = data_identity
    config_path, config_size, config_mtime_ns = config_identity
    del data_size, config_size, config_mtime_ns
    config = load_weather_config(config_path)
    records = _read_weather(data_path)
    selected = records[
        records["crop"].eq(str(config["crop"]))
        & records["country"].eq(str(config["country"]))
        & records["metric"].isin(("precipitation", "temperature_max", "soil_moisture"))
    ].copy()
    normal_path = normal_identity[0] if normal_identity is not None else None
    normals = _read_normals(normal_path)
    source_identity = {
        "weather": {
            "path": data_path,
            "size": data_identity[1],
            "mtime_ns": data_mtime_ns,
        },
        "normal": None if normal_identity is None else {
            "path": normal_identity[0],
            "size": normal_identity[1],
            "mtime_ns": normal_identity[2],
        },
        "config": {
            "path": config_identity[0],
            "size": config_identity[1],
            "mtime_ns": config_identity[2],
        },
    }
    generated_at = datetime.fromtimestamp(data_mtime_ns / 1_000_000_000, timezone.utc)
    return build_weather_summary(
        selected,
        normals,
        config,
        source_identity=source_identity,
        generated_at=generated_at,
    )


def load_weather_summary_cached(
    data_path: str | Path,
    config_path: str | Path,
    normal_path: str | Path | None = None,
    *,
    rules_path: str | Path = DEFAULT_CONFIG,
) -> Summary:
    data_identity = file_stat_identity(data_path)
    config_identity = file_stat_identity(config_path)
    normal_identity = file_stat_identity(normal_path) if normal_path is not None else None
    rules_identity = file_stat_identity(rules_path)
    rules = load_summary_rules(rules_path)
    return _load_weather_summary_versioned(
        data_identity,
        config_identity,
        normal_identity,
        rules_identity,
        str(rules["rule_version"]),
        str(rules["calculation_version"]),
    )


def clear_weather_summary_cache() -> None:
    """Test/diagnostic hook; production invalidation is identity-driven."""

    _load_weather_summary_versioned.clear()
