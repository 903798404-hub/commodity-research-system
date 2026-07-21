"""Pure calculations for configuration-driven soybean weather research pages."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml


def load_weather_config(path: str | Path) -> dict[str, object]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    candidates = [
        value
        for key, value in (payload or {}).items()
        if (str(key).startswith("soybean_weather_") or str(key).endswith("_weather")) and isinstance(value, dict)
    ]
    if len(candidates) != 1:
        raise ValueError("天气配置必须包含唯一的 *_weather 根节点")
    config = candidates[0]
    regions = config.get("regions")
    if not isinstance(regions, list) or not regions:
        raise ValueError("大豆天气配置必须包含展示地区")
    keys = [str(item.get("key", "")) for item in regions if isinstance(item, dict)]
    if len(keys) != len(regions) or len(set(keys)) != len(keys) or any(not key for key in keys):
        raise ValueError("大豆天气地区标识必须唯一")
    if bool(config.get("weighted_aggregation", True)):
        total = sum(float(item["weight"]) for item in regions if isinstance(item, dict))
        coverage = float(config.get("weighted_coverage_percent", 0))
        if round(total, 1) != round(coverage, 1):
            raise ValueError("天气展示权重合计必须等于固定加权分母")
    source_regions = config.get("source_regions", regions)
    if not isinstance(source_regions, list) or not source_regions:
        raise ValueError("大豆天气源地区配置无效")
    source_keys = {str(item.get("key", "")) for item in source_regions if isinstance(item, dict)}
    if not set(keys).issubset(source_keys):
        raise ValueError("展示地区必须全部存在于源地区配置")
    if not str(config.get("crop", "")) or not str(config.get("country", "")):
        raise ValueError("天气作物或国家标识无效")
    return config


def region_weights(config: dict[str, object]) -> pd.DataFrame:
    regions = pd.DataFrame(config["regions"])
    if "weight" not in regions.columns:
        regions["weight"] = 0.0
    return regions.sort_values("display_order").reset_index(drop=True)


def season_for_date(date: pd.Timestamp, season_start_month_day: str) -> str:
    month, day = (int(part) for part in season_start_month_day.split("-"))
    start_year = date.year if (date.month, date.day) >= (month, day) else date.year - 1
    return f"{start_year}/{start_year + 1}"


def season_day_for_date(date: pd.Timestamp, season_start_month_day: str) -> int:
    month, day = (int(part) for part in season_start_month_day.split("-"))
    start_year = date.year if (date.month, date.day) >= (month, day) else date.year - 1
    season_start = pd.Timestamp(year=start_year, month=month, day=day)
    return int((date.normalize() - season_start).days + 1)


def add_season_columns(records: pd.DataFrame, config: dict[str, object]) -> pd.DataFrame:
    enriched = records.copy()
    season_start = str(config["season_start_month_day"])
    enriched["season"] = enriched["date"].map(lambda value: season_for_date(value, season_start))
    enriched["season_day"] = enriched["date"].map(lambda value: season_day_for_date(value, season_start))
    return enriched


def latest_observation_date(records: pd.DataFrame) -> pd.Timestamp | None:
    observed = records[records["data_type"] == "observed"]
    if observed.empty:
        return None
    return pd.Timestamp(observed["date"].max())


def previous_five_complete_seasons(records: pd.DataFrame, current_season: str) -> list[str]:
    seasons = sorted({str(value) for value in records["season"].dropna() if str(value) < current_season})
    return seasons[-5:]


def weighted_values(values_by_region: pd.Series, config: dict[str, object]) -> tuple[float, float]:
    """Return the fixed configured-denominator value and available-weight coverage.

    Missing regions are not renormalized: the approved denominator remains fixed
    and the caller receives the actual coverage for explicit display.
    """

    weights = region_weights(config).set_index("key")["weight"].astype(float)
    aligned = pd.to_numeric(values_by_region.reindex(weights.index), errors="coerce")
    present = aligned.notna()
    coverage = float(weights[present].sum())
    if not present.any():
        return float("nan"), coverage
    value = float((aligned[present] * weights[present]).sum() / float(config["weighted_coverage_percent"]))
    return value, coverage


def aggregate_weighted_daily(records: pd.DataFrame, config: dict[str, object]) -> pd.DataFrame:
    """Aggregate selected regional records with the approved fixed denominator."""

    if records.empty:
        return pd.DataFrame(columns=["date", "data_type", "model", "forecast_run_at", "value", "coverage_percent"])
    group_columns = ["date", "data_type", "model", "forecast_run_at"]
    rows: list[dict[str, object]] = []
    for keys, group in records.groupby(group_columns, dropna=False, sort=True):
        values = group.set_index("region")["value"]
        weighted, coverage = weighted_values(values, config)
        rows.append(
            {
                "date": keys[0],
                "data_type": keys[1],
                "model": keys[2],
                "forecast_run_at": keys[3],
                "value": weighted,
                "coverage_percent": coverage,
            }
        )
    return add_season_columns(pd.DataFrame(rows), config).sort_values("date").reset_index(drop=True)


def five_year_mean(weighted_observed: pd.DataFrame) -> pd.DataFrame:
    if weighted_observed.empty:
        return pd.DataFrame(columns=["season_day", "value"])
    current_season = str(weighted_observed["season"].max())
    historical = previous_five_complete_seasons(weighted_observed, current_season)
    if len(historical) < 5:
        return pd.DataFrame(columns=["season_day", "value"])
    baseline = weighted_observed[
        (weighted_observed["data_type"] == "observed")
        & (weighted_observed["season"].isin(historical))
    ]
    return (
        baseline.groupby("season_day", as_index=False)["value"]
        .mean()
        .sort_values("season_day")
        .reset_index(drop=True)
    )


def select_latest_forecasts(records: pd.DataFrame, latest_observed: pd.Timestamp | None) -> pd.DataFrame:
    forecasts = records[records["data_type"] == "forecast"].copy()
    if forecasts.empty:
        return forecasts
    forecasts["_batch_at"] = forecasts["forecast_run_at"].fillna(forecasts["source_updated_at"])
    latest_batch = forecasts.groupby("model")["_batch_at"].transform("max")
    selected = forecasts[forecasts["_batch_at"] == latest_batch].drop(columns="_batch_at")
    if latest_observed is not None:
        selected = selected[selected["date"] > latest_observed]
    return selected.reset_index(drop=True)


def combined_observed_and_forecast(records: pd.DataFrame, config: dict[str, object]) -> pd.DataFrame:
    """Keep observations authoritative where observed and forecast dates overlap."""

    latest = latest_observation_date(records)
    observed = records[records["data_type"] == "observed"].copy()
    forecasts = select_latest_forecasts(records, latest)
    combined = pd.concat([observed, forecasts], ignore_index=True)
    return add_season_columns(combined, config).sort_values(["date", "data_type", "model"]).reset_index(drop=True)


def weekly_metric_summary(
    records: pd.DataFrame,
    config: dict[str, object],
    *,
    metric: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    data_type: str,
    model: str = "",
) -> pd.DataFrame:
    selected = records[
        (records["metric"] == metric)
        & (records["data_type"] == data_type)
        & (records["date"] >= start)
        & (records["date"] <= end)
    ].copy()
    if model:
        selected = selected[selected["model"] == model]
    if selected.empty:
        return pd.DataFrame(columns=["region", "value", "coverage_percent"])
    aggregation = str(config["metrics"][metric]["weekly_aggregation"])
    if aggregation == "sum":
        values = selected.groupby("region")["value"].sum()
    else:
        values = selected.groupby("region")["value"].mean()
    weights = region_weights(config)
    result = weights[["key", "display_name", "display_order", "weight"]].rename(columns={"key": "region"})
    result["value"] = result["region"].map(values)
    weighted, coverage = weighted_values(values, config)
    weighted_row = pd.DataFrame(
        [{"region": "weighted", "display_name": "主产区加权", "display_order": 0, "weight": float(config["weighted_coverage_percent"]), "value": weighted, "coverage_percent": coverage}]
    )
    result["coverage_percent"] = result["weight"].where(result["value"].notna(), 0.0)
    return pd.concat([weighted_row, result], ignore_index=True)


def weekly_historical_baseline(
    records: pd.DataFrame,
    config: dict[str, object],
    *,
    metric: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Calculate the approved previous-five-complete-seasons weekly baseline."""

    enriched = add_season_columns(records, config)
    current_season = season_for_date(end, str(config["season_start_month_day"]))
    historical_seasons = previous_five_complete_seasons(enriched, current_season)
    if len(historical_seasons) < 5:
        return pd.DataFrame(columns=["region", "baseline_value"])
    target_start = season_day_for_date(start, str(config["season_start_month_day"]))
    target_end = season_day_for_date(end, str(config["season_start_month_day"]))
    selected = enriched[
        (enriched["metric"] == metric)
        & (enriched["data_type"] == "observed")
        & (enriched["season"].isin(historical_seasons))
        & (enriched["season_day"] >= target_start)
        & (enriched["season_day"] <= target_end)
    ]
    if selected.empty:
        return pd.DataFrame(columns=["region", "baseline_value"])
    aggregation = str(config["metrics"][metric]["weekly_aggregation"])
    if aggregation == "sum":
        per_season = selected.groupby(["season", "region"], as_index=False)["value"].sum()
    else:
        per_season = selected.groupby(["season", "region"], as_index=False)["value"].mean()
    mean_by_region = per_season.groupby("region")["value"].mean()
    weighted_per_season = per_season.groupby("season").apply(
        lambda group: weighted_values(group.set_index("region")["value"], config)[0],
        include_groups=False,
    )
    weights = region_weights(config)[["key", "display_name", "display_order", "weight"]].rename(columns={"key": "region"})
    result = weights.copy()
    result["baseline_value"] = result["region"].map(mean_by_region)
    weighted_row = pd.DataFrame(
        [{"region": "weighted", "display_name": "主产区加权", "display_order": 0, "weight": float(config["weighted_coverage_percent"]), "baseline_value": weighted_per_season.mean()}]
    )
    return pd.concat([weighted_row, result], ignore_index=True)


def absolute_and_relative_anomaly(value: float, baseline: float) -> tuple[float, float]:
    if pd.isna(value) or pd.isna(baseline):
        return float("nan"), float("nan")
    absolute = float(value - baseline)
    relative = float("nan") if baseline == 0 else float(absolute / abs(baseline) * 100)
    return absolute, relative
