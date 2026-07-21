"""Adapters for the normalized oilseed-weather data contract.

This module has no credentials, network calls, or production-source assumptions.
It reads a caller-supplied stable file (or an explicitly supplied local fixture)
and returns only the selected crop, country, metric, and regions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd


REQUIRED_WEATHER_COLUMNS = (
    "date",
    "crop",
    "country",
    "region",
    "metric",
    "data_type",
    "model",
    "value",
    "unit",
    "source_updated_at",
)

ALLOWED_METRICS = {"precipitation", "temperature_max", "temperature_min", "soil_moisture"}
ALLOWED_DATA_TYPES = {"observed", "forecast"}
ALLOWED_MODELS = {"OBSERVED", "ECMWF", "GFS"}


def _region_key(value: object) -> str:
    return "_".join(str(value).strip().lower().replace("-", " ").split())


def validate_weather_records(records: pd.DataFrame) -> pd.DataFrame:
    """Validate and normalize the page-facing weather contract."""

    missing = [column for column in REQUIRED_WEATHER_COLUMNS if column not in records.columns]
    if missing:
        raise ValueError("天气数据缺少必填字段：" + "、".join(missing))

    normalized = records.copy()
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce").dt.normalize()
    normalized["source_updated_at"] = pd.to_datetime(
        normalized["source_updated_at"], errors="coerce", utc=True
    )
    normalized["forecast_run_at"] = pd.to_datetime(
        normalized.get("forecast_run_at"), errors="coerce", utc=True
    )
    normalized["value"] = pd.to_numeric(normalized["value"], errors="coerce")
    for column in ("crop", "country", "metric", "data_type", "unit"):
        normalized[column] = normalized[column].astype("string").str.strip()
    normalized["region"] = normalized["region"].map(_region_key).astype("string")
    normalized["model"] = normalized["model"].fillna("").astype("string").str.strip().str.upper()

    invalid = normalized[
        normalized[["date", "crop", "country", "region", "metric", "data_type", "value", "unit", "source_updated_at"]]
        .isna()
        .any(axis=1)
    ]
    if not invalid.empty:
        raise ValueError(f"天气数据存在 {len(invalid)} 条必填字段为空或格式无效的记录")

    if not set(normalized["crop"]).issubset({"soybean", "rapeseed"}) or not set(normalized["country"]).issubset({"USA", "BRA", "ARG", "CAN", "AUS"}):
        raise ValueError("天气数据不符合已批准的油料作物天气标准化契约")
    for column, allowed in (
        ("metric", ALLOWED_METRICS),
        ("data_type", ALLOWED_DATA_TYPES),
        ("model", ALLOWED_MODELS),
    ):
        unknown = sorted(set(normalized[column]) - allowed)
        if unknown:
            raise ValueError(f"天气数据的 {column} 存在未允许值：{unknown}")
    model_mismatch = normalized[
        ((normalized["data_type"] == "observed") & (normalized["model"] != "OBSERVED"))
        | ((normalized["data_type"] == "forecast") & ~normalized["model"].isin({"ECMWF", "GFS"}))
    ]
    if not model_mismatch.empty:
        raise ValueError("天气数据的 data_type 与 model 不一致")
    normalized.loc[normalized["model"] == "OBSERVED", "model"] = "observed"

    identity = [
        "date",
        "crop",
        "country",
        "region",
        "metric",
        "data_type",
        "model",
        "forecast_run_at",
    ]
    conflicts = normalized.groupby(identity, dropna=False)["value"].nunique(dropna=False)
    if (conflicts > 1).any():
        raise ValueError("天气数据存在同一业务键的冲突数值")
    return normalized.drop_duplicates(subset=identity, keep="last").reset_index(drop=True)


def load_weather_records(
    path: str | Path,
    *,
    crop: str,
    country: str,
    metric: str,
    regions: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Load only the current page selection from a CSV or Parquet adapter input."""

    source_path = Path(path)
    if not source_path.is_file():
        raise FileNotFoundError(f"天气数据文件不存在：{source_path}")
    if source_path.suffix.lower() == ".parquet":
        filters = [("crop", "=", crop), ("country", "=", country), ("metric", "=", metric)]
        try:
            raw = pd.read_parquet(source_path, filters=filters)
        except (ImportError, OSError, ValueError):
            raw = pd.read_parquet(source_path)
    elif source_path.suffix.lower() == ".csv":
        raw = pd.read_csv(source_path)
    else:
        raise ValueError("天气适配器只支持 CSV 或 Parquet 输入")

    records = validate_weather_records(raw)
    selected = records[
        (records["crop"] == crop)
        & (records["country"] == country)
        & (records["metric"] == metric)
    ].copy()
    if regions is not None:
        selected_keys = {_region_key(region) for region in regions}
        selected = selected[selected["region"].isin(selected_keys)].copy()
    return selected.sort_values(["date", "region", "data_type", "model"]).reset_index(drop=True)
