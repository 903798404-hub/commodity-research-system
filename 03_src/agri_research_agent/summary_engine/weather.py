from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
from typing import Any, Mapping

import pandas as pd

from agri_research_agent.weather.crop_weather import (
    latest_observation_date,
    select_latest_forecasts,
    weighted_values,
)

from .rules import (
    classify_model_consistency,
    classify_rainfall,
    classify_temperature,
    freshness_status,
    load_summary_rules,
)
from .schema import Summary


_FIVE_YEAR_CACHE: ContextVar[dict[str, dict[Any, Any]] | None] = ContextVar(
    "weather_summary_five_year_cache", default=None
)


def _aggregate(frame: pd.DataFrame, config: dict[str, Any], metric: str) -> tuple[float | None, float | None]:
    if frame.empty:
        return None, 0.0
    grouped = frame.groupby("region")["value"]
    values = grouped.sum() if metric == "precipitation" else grouped.mean()
    value, coverage = weighted_values(values, config)
    return (None if pd.isna(value) else float(value)), float(coverage)


def _normal(normals: pd.DataFrame, config: dict[str, Any], metric: str, start: pd.Timestamp, end: pd.Timestamp) -> tuple[float | None, float | None]:
    if normals.empty:
        return None, None
    days = set(pd.date_range(start, end).strftime("%m-%d"))
    selected = normals[normals.metric.eq(metric) & normals.month_day.isin(days)]
    grouped = selected.groupby("region").normal_value
    values = grouped.sum() if metric == "precipitation" else grouped.mean()
    value, coverage = weighted_values(values, config)
    return (None if pd.isna(value) else float(value)), float(coverage)


def _normal_region(normals: pd.DataFrame, region: str, metric: str, start: pd.Timestamp, end: pd.Timestamp) -> float | None:
    if normals.empty:
        return None
    days = set(pd.date_range(start, end).strftime("%m-%d"))
    selected = normals[normals.region.eq(region) & normals.metric.eq(metric) & normals.month_day.isin(days)].normal_value
    if selected.empty:
        return None
    return float(selected.sum() if metric == "precipitation" else selected.mean())


def _five_year_region(records: pd.DataFrame, region: str, metric: str, start: pd.Timestamp, end: pd.Timestamp) -> float | None:
    # Regional pages query hundreds of overlapping five-year windows.  Build
    # the immutable region/metric series once per summary calculation so every
    # window retains the approved calculation while avoiding repeated scans of
    # the complete country frame.
    active_cache = _FIVE_YEAR_CACHE.get()
    series_cache = active_cache.setdefault("series", {}) if active_cache is not None else {}
    series_key = (region, metric)
    series = series_cache.get(series_key)
    if series is None:
        selected = records[
            records.region.eq(region)
            & records.metric.eq(metric)
            & records.data_type.eq("observed")
        ][["date", "value"]].sort_values("date")
        series = selected.set_index("date")["value"]
        series_cache[series_key] = series

    result_cache = active_cache.setdefault("results", {}) if active_cache is not None else {}
    result_key = (region, metric, int(start.value), int(end.value))
    if result_key in result_cache:
        return result_cache[result_key]
    values: list[float] = []
    for years_back in range(1, 6):
        prior_start = start - pd.DateOffset(years=years_back)
        prior_end = end - pd.DateOffset(years=years_back)
        selected = series.loc[(series.index >= prior_start) & (series.index <= prior_end)]
        if selected.empty:
            continue
        values.append(float(selected.sum() if metric == "precipitation" else selected.mean()))
    result = sum(values) / len(values) if len(values) == 5 else None
    result_cache[result_key] = result
    return result


def _anomaly(value: float | None, normal: float | None) -> tuple[float | None, float | None]:
    absolute = None if value is None or normal is None else value - normal
    relative = None if absolute is None or normal == 0 else absolute / abs(normal) * 100
    return absolute, relative


def _soil_percent(value: float | None, *, canonical_percent: bool = False) -> float | None:
    if value is None or pd.isna(value):
        return None
    if canonical_percent:
        return float(value)
    return float(value * 100 if abs(value) <= 1.5 else value)


def _fmt_window(item: Mapping[str, Any]) -> str:
    return f'{str(item["start"])[5:]}—{str(item["end"])[5:]}'


RAIN_TEXT = {
    "rainfall_near_normal": "接近正常",
    "rainfall_near_normal_absolute_guard": "接近正常",
    "rainfall_below_normal": "偏少",
    "rainfall_clearly_below_normal": "明显偏少",
    "rainfall_significantly_below_normal": "显著偏少",
    "rainfall_above_normal": "偏多",
    "rainfall_clearly_above_normal": "明显偏多",
    "rainfall_significantly_above_normal": "显著偏多",
}
TEMP_TEXT = {
    "temperature_near_normal": "中性",
    "temperature_above_normal": "偏高",
    "temperature_clearly_above_normal": "明显偏高",
    "temperature_significantly_above_normal": "显著偏高",
    "temperature_below_normal": "偏低",
    "temperature_clearly_below_normal": "明显偏低",
    "temperature_significantly_below_normal": "显著偏低",
}

SOIL_SMALL_DIFFERENCE_PCT_POINTS = 1.0


def _bold(text: str) -> str:
    return f"**{text}**"


def _effective_rainfall_signal(relative: float | None, absolute: float | None) -> float | None:
    if relative is None:
        return None
    if absolute is not None and abs(absolute) < 5:
        return 0.0
    return relative


def _model_consistency_text(
    ec: float | None,
    gfs: float | None,
    ec_absolute: float | None = None,
    gfs_absolute: float | None = None,
) -> str:
    ec = _effective_rainfall_signal(ec, ec_absolute)
    gfs = _effective_rainfall_signal(gfs, gfs_absolute)
    if ec is None or gfs is None:
        return "模型历史距平不可比"
    code = classify_model_consistency(ec, gfs)
    if code == "models_opposite_direction":
        return _bold("模型方向分歧")
    if code == "one_neutral_one_directional":
        return f"一个模型{_bold('中性')}，{_bold('信号偏弱')}"
    if abs(ec) <= 20 and abs(gfs) <= 20:
        return f"两模型方向一致、{_bold('中性')}"
    direction = "偏湿" if ec + gfs > 0 else "偏干"
    if abs(ec) >= 60 and abs(gfs) >= 60:
        return f"两模型{_bold('一致显著' + direction)}"
    if code == "models_same_direction_different_intensity":
        stronger = "EC" if abs(ec) > abs(gfs) else "GFS"
        return f"两模型{_bold('方向一致' + direction + '，但强度存在差异')}，{stronger}幅度更大"
    return f"两模型{_bold('方向一致' + direction)}"


def _rain_class(relative: float | None, absolute: float | None) -> str | None:
    if relative is None or absolute is None:
        return None
    return classify_rainfall(relative, absolute)


def _week_rain_signal(week: Mapping[str, Any]) -> str:
    if "models" not in week:
        return "missing"
    signals = []
    for model in ("ECMWF", "GFS"):
        item = week["models"][model]
        effective = _effective_rainfall_signal(item.get("relative_anomaly_pct"), item.get("absolute_anomaly_mm"))
        signals.append("missing" if effective is None else "neutral" if abs(effective) <= 20 else "wet" if effective > 0 else "dry")
    if "missing" in signals:
        return "missing"
    if set(signals) == {"wet", "dry"}:
        return "divergent"
    directional = [signal for signal in signals if signal != "neutral"]
    if not directional:
        return "neutral"
    if len(set(directional)) == 1:
        return directional[0]
    return "divergent"


def _week_is_significant(week: Mapping[str, Any], direction: str) -> bool:
    if "models" not in week:
        return False
    expected = "above" if direction == "wet" else "below"
    return all(
        _rain_class(item.get("relative_anomaly_pct"), item.get("absolute_anomaly_mm"))
        == f"rainfall_significantly_{expected}_normal"
        for item in week["models"].values()
    )


def _temperature_week_signal(week: Mapping[str, Any]) -> str:
    if "models" not in week:
        return "missing"
    signals = []
    for model in ("ECMWF", "GFS"):
        anomaly = week["models"][model].get("temperature_anomaly_c")
        signals.append("missing" if anomaly is None else "normal" if abs(anomaly) <= 1 else "high" if anomaly > 0 else "low")
    if "missing" in signals:
        return "missing"
    directional = [signal for signal in signals if signal != "normal"]
    if not directional:
        return "normal"
    return directional[0] if len(set(directional)) == 1 else "mixed"


def _rain_comprehensive_sentence(observed: Mapping[str, Any], week1: Mapping[str, Any], week2: Mapping[str, Any]) -> str:
    observed_code = _rain_class(observed.get("relative_anomaly_pct"), observed.get("absolute_anomaly_mm"))
    observed_text = "近期降雨历史同期状态暂不可比" if observed_code is None else f"过去一周降雨{_bold(RAIN_TEXT[observed_code])}"
    first, second = _week_rain_signal(week1), _week_rain_signal(week2)
    if first == "divergent":
        tail = f"未来第一周{_bold('模型方向分歧')}，短期预报不确定性较高"
        if second in {"wet", "dry"}:
            tail += f"；第二周转为{_bold('方向一致偏湿' if second == 'wet' else '方向一致偏干')}"
    elif first == "wet" and second == "wet":
        tail = f"未来两周EC/GFS均显示降雨{_bold('偏多')}"
        tail += f"，其中第二周两模型{_bold('一致显著偏湿')}" if _week_is_significant(week2, "wet") else f"，呈{_bold('方向一致偏湿')}"
    elif first == "wet" and second == "dry":
        tail = f"未来第一周两模型{_bold('方向一致偏湿')}，但第二周{_bold('方向一致偏干')}，短期水分条件改善后出现{_bold('转干信号')}"
    elif first == "wet" and second == "divergent":
        tail = f"未来第一周两模型{_bold('方向一致偏湿')}，但第二周{_bold('模型方向分歧')}，远期预报不确定性较高"
    elif first == "dry" and second == "dry":
        tail = f"未来两周两模型均为{_bold('方向一致偏干')}，降雨连续偏少"
    elif first == "dry" and second == "wet":
        tail = f"未来第一周两模型{_bold('方向一致偏干')}，第二周转为{_bold('方向一致偏湿')}"
    elif second == "divergent":
        tail = f"未来第二周{_bold('模型方向分歧')}，远期预报不确定性较高"
    elif first == second == "neutral":
        tail = f"未来两周降雨整体{_bold('接近正常')}"
    else:
        labels = {"wet": "偏湿", "dry": "偏干", "neutral": "接近正常", "missing": "数据不足", "divergent": "模型方向分歧"}
        tail = f"未来第一周{labels[first]}，第二周{labels[second]}"
    return observed_text + "；" + tail + "。"


def _temperature_comprehensive_sentence(week1: Mapping[str, Any], week2: Mapping[str, Any]) -> str:
    first, second = _temperature_week_signal(week1), _temperature_week_signal(week2)
    if first == second == "low":
        ec = sum(abs(week["models"]["ECMWF"]["temperature_anomaly_c"]) for week in (week1, week2))
        gfs = sum(abs(week["models"]["GFS"]["temperature_anomaly_c"]) for week in (week1, week2))
        stronger = "，EC冷信号更强" if ec > gfs else "，GFS冷信号更强" if gfs > ec else ""
        return f"未来两周最高气温整体{_bold('偏低')}{stronger}。"
    if first == second == "high":
        return f"未来两周最高气温整体{_bold('偏高')}。"
    if first == "high" and second == "normal":
        return f"未来第一周最高气温{_bold('偏高')}，第二周趋于{_bold('中性')}。"
    if first == "low" and second == "normal":
        return f"未来第一周最高气温{_bold('偏低')}，第二周趋于{_bold('中性')}。"
    if first == second == "normal":
        return f"未来两周最高气温整体{_bold('中性')}。"
    labels = {"high": "偏高", "low": "偏低", "normal": "中性", "mixed": "模型方向不一", "missing": "数据不足"}
    return f"未来第一周最高气温{_bold(labels[first])}，第二周{_bold(labels[second])}。"


def _soil_comprehensive_sentence(rows: list[Mapping[str, Any]]) -> str:
    differences = [row.get("difference_pct_points") for row in rows if row.get("difference_pct_points") is not None]
    if not differences:
        return "当前主要区域0—100cm土壤含水率与5年同期暂不可比。"
    if all(value > 0 for value in differences):
        word = "略高于" if max(differences) <= SOIL_SMALL_DIFFERENCE_PCT_POINTS else "高于"
        return f"当前主要产区0—100cm土壤含水率均{word}5年同期。"
    if all(value < 0 for value in differences):
        return "当前主要产区0—100cm土壤含水率均低于5年同期。"
    if any(value < 0 for value in differences):
        return "当前部分区域0—100cm土壤含水率低于5年同期。"
    return "当前主要区域0—100cm土壤含水率与5年同期差异较小。"


def _overall_weather_sentence(subject: str, week1: Mapping[str, Any], week2: Mapping[str, Any]) -> str | None:
    rain1, rain2 = _week_rain_signal(week1), _week_rain_signal(week2)
    temp1, temp2 = _temperature_week_signal(week1), _temperature_week_signal(week2)
    if rain1 == rain2 == "wet" and temp1 == temp2 == "low":
        return f"整体来看，未来两周{subject}产区呈{_bold('偏湿')}、{_bold('偏凉')}特征。"
    if rain1 == "wet" and rain2 == "dry":
        return f"短期水分条件存在改善，但第二周{_bold('转干信号')}值得继续关注。"
    if "divergent" in {rain1, rain2}:
        return f"远期降雨{_bold('模型分歧明显')}，当前预报不确定性较高。"
    return None


def _majority_signal(signals: list[str], neutral: str) -> str:
    usable = [signal for signal in signals if signal != "missing"]
    if not usable:
        return "missing"
    counts = {signal: usable.count(signal) for signal in set(usable)}
    winner, count = max(counts.items(), key=lambda item: item[1])
    return winner if count > len(usable) / 2 else neutral


def _regional_comprehensive(regions: list[Mapping[str, Any]]) -> str:
    observed_codes = [
        _rain_class(row["observed_7d"].get("relative_anomaly_pct"), row["observed_7d"].get("absolute_anomaly_mm"))
        for row in regions
    ]
    observed_labels = [RAIN_TEXT[code] for code in observed_codes if code]
    observed_label = _majority_signal(observed_labels, "区域差异较大") if observed_labels else None
    rain1 = _majority_signal([_week_rain_signal(row["forecast_weeks"][0]) for row in regions], "divergent")
    rain2 = _majority_signal([_week_rain_signal(row["forecast_weeks"][1]) for row in regions], "divergent")
    observed_text = "近期区域降雨历史同期状态不一" if observed_label is None else "过去一周主要区域降雨状态差异较大" if observed_label == "区域差异较大" else f"过去一周主要区域降雨{_bold(observed_label)}"
    labels = {"wet": "方向一致偏湿", "dry": "方向一致偏干", "neutral": "接近正常", "divergent": "模型方向分歧", "missing": "数据不足"}
    if rain1 == "wet" and rain2 == "wet":
        rain_sentence = f"{observed_text}；未来两周主要区域整体{_bold('方向一致偏湿')}。"
    elif rain1 == "wet" and rain2 == "dry":
        rain_sentence = f"{observed_text}；未来第一周整体{_bold('方向一致偏湿')}，第二周出现{_bold('转干信号')}。"
    elif rain1 == "wet" and rain2 == "divergent":
        rain_sentence = f"{observed_text}；未来第一周整体{_bold('方向一致偏湿')}，第二周{_bold('模型方向分歧')}。"
    elif rain1 == "dry" and rain2 == "dry":
        rain_sentence = f"{observed_text}；未来两周主要区域整体{_bold('方向一致偏干')}。"
    elif rain1 == "divergent":
        rain_sentence = f"{observed_text}；未来第一周存在{_bold('模型方向分歧')}，短期预报不确定性较高。"
    else:
        rain_sentence = f"{observed_text}；未来第一周{_bold(labels[rain1])}，第二周{_bold(labels[rain2])}。"
    temp1 = _majority_signal([_temperature_week_signal(row["forecast_weeks"][0]) for row in regions], "mixed")
    temp2 = _majority_signal([_temperature_week_signal(row["forecast_weeks"][1]) for row in regions], "mixed")
    temp_labels = {"high": "偏高", "low": "偏低", "normal": "中性", "mixed": "模型方向不一", "missing": "数据不足"}
    if temp1 == temp2 and temp1 in {"high", "low", "normal"}:
        temp_sentence = f"未来两周主要区域最高气温整体{_bold(temp_labels[temp1])}。"
    else:
        temp_sentence = f"未来第一周主要区域最高气温{_bold(temp_labels[temp1])}，第二周{_bold(temp_labels[temp2])}。"
    soil_rows = [{"difference_pct_points": row["soil_moisture"].get("difference_pct_points")} for row in regions]
    return rain_sentence + temp_sentence + _soil_comprehensive_sentence(soil_rows)


def _region_label(region: Mapping[str, Any]) -> str:
    name = str(region.get("display_name", region["key"]))
    weight = region.get("weight")
    if weight is None:
        return name
    return f'{name}（{float(weight):g}%）'


def _region_fact_rows(
    records: pd.DataFrame,
    normals: pd.DataFrame,
    config: dict[str, Any],
    latest: pd.Timestamp,
) -> list[dict[str, Any]]:
    observed = records[records.data_type.eq("observed")]
    soil_records = observed[observed.metric.eq("soil_moisture")]
    canonical_soil_percent = bool(
        not soil_records.empty
        and "value_semantics" in soil_records.columns
        and "unit" in soil_records.columns
        and soil_records["value_semantics"].eq("canonical").all()
        and soil_records["unit"].eq("%").all()
    )
    forecasts = select_latest_forecasts(records, latest)
    current_window = (latest - pd.Timedelta(days=6), latest)
    previous_window = (latest - pd.Timedelta(days=13), latest - pd.Timedelta(days=7))
    common = set(pd.to_datetime(forecasts[forecasts.model.eq("ECMWF")].date).dt.normalize()) & set(pd.to_datetime(forecasts[forecasts.model.eq("GFS")].date).dt.normalize())
    forecast_start = min(common) if common else None
    history_label = "30年同期" if not normals.empty else "5年同期"
    rows: list[dict[str, Any]] = []
    for order, configured in enumerate(config["regions"]):
        region = str(configured["key"])
        current = observed[observed.region.eq(region) & observed.date.between(*current_window)]
        previous = observed[observed.region.eq(region) & observed.date.between(*previous_window)]
        current_rain_values = current[current.metric.eq("precipitation")].value
        previous_rain_values = previous[previous.metric.eq("precipitation")].value
        current_rain = None if current_rain_values.empty else float(current_rain_values.sum())
        previous_rain = None if previous_rain_values.empty else float(previous_rain_values.sum())
        rain_history = _normal_region(normals, region, "precipitation", *current_window) if not normals.empty else _five_year_region(observed, region, "precipitation", *current_window)
        rain_absolute, rain_relative = _anomaly(current_rain, rain_history)
        current_temp_values = current[current.metric.eq("temperature_max")].value
        previous_temp_values = previous[previous.metric.eq("temperature_max")].value
        current_temp = None if current_temp_values.empty else float(current_temp_values.mean())
        previous_temp = None if previous_temp_values.empty else float(previous_temp_values.mean())
        temp_history = _normal_region(normals, region, "temperature_max", *current_window) if not normals.empty else _five_year_region(observed, region, "temperature_max", *current_window)
        row: dict[str, Any] = {
            "region": region,
            "region_identity": region,
            "display_name": str(configured.get("display_name", region)),
            "display_label": _region_label(configured),
            "display_order": int(configured.get("display_order", order + 1)),
            "weight_pct": None if configured.get("weight") is None else float(configured["weight"]),
            "history_label": history_label,
            "observed_7d": {
                "current_7d_start": current_window[0].date().isoformat(),
                "current_7d_end": current_window[1].date().isoformat(),
                "previous_7d_start": previous_window[0].date().isoformat(),
                "previous_7d_end": previous_window[1].date().isoformat(),
                "current_7d_mm": current_rain,
                "previous_7d_mm": previous_rain,
                "delta_vs_previous_7d_mm": None if current_rain is None or previous_rain is None else current_rain - previous_rain,
                "history_mm": rain_history,
                "absolute_anomaly_mm": rain_absolute,
                "relative_anomaly_pct": rain_relative,
            },
            "temperature_7d": {
                "current_7d_start": current_window[0].date().isoformat(),
                "current_7d_end": current_window[1].date().isoformat(),
                "previous_7d_start": previous_window[0].date().isoformat(),
                "previous_7d_end": previous_window[1].date().isoformat(),
                "current_7d_c": current_temp,
                "previous_7d_c": previous_temp,
                "delta_vs_previous_7d_c": None if current_temp is None or previous_temp is None else current_temp - previous_temp,
                "history_c": temp_history,
                "anomaly_c": None if current_temp is None or temp_history is None else current_temp - temp_history,
            },
            "forecast_weeks": [],
        }
        for index in (1, 2):
            if forecast_start is None:
                row["forecast_weeks"].append({"missing_reason": "EC/GFS无共同预测日期"})
                continue
            window = (forecast_start + pd.Timedelta(days=7 * (index - 1)), forecast_start + pd.Timedelta(days=7 * index - 1))
            week: dict[str, Any] = {"start": window[0].date().isoformat(), "end": window[1].date().isoformat(), "models": {}}
            for model in ("ECMWF", "GFS"):
                selected = forecasts[forecasts.region.eq(region) & forecasts.model.eq(model) & forecasts.date.between(*window)]
                rain_values = selected[selected.metric.eq("precipitation")].value
                temp_values = selected[selected.metric.eq("temperature_max")].value
                rain = None if rain_values.empty else float(rain_values.sum())
                rain_normal = _normal_region(normals, region, "precipitation", *window) if not normals.empty else _five_year_region(observed, region, "precipitation", *window)
                rain_absolute, rain_relative = _anomaly(rain, rain_normal)
                temp = None if temp_values.empty else float(temp_values.mean())
                temp_normal = _normal_region(normals, region, "temperature_max", *window) if not normals.empty else _five_year_region(observed, region, "temperature_max", *window)
                week["models"][model] = {
                    "precipitation_mm": rain,
                    "history_mm": rain_normal,
                    "absolute_anomaly_mm": rain_absolute,
                    "relative_anomaly_pct": rain_relative,
                    "temperature_max_c": temp,
                    "history_temperature_c": temp_normal,
                    "temperature_anomaly_c": None if temp is None or temp_normal is None else temp - temp_normal,
                }
            week["model_consistency"] = _model_consistency_text(
                week["models"]["ECMWF"]["relative_anomaly_pct"],
                week["models"]["GFS"]["relative_anomaly_pct"],
                week["models"]["ECMWF"]["absolute_anomaly_mm"],
                week["models"]["GFS"]["absolute_anomaly_mm"],
            )
            row["forecast_weeks"].append(week)
        region_soil = observed[observed.region.eq(region) & observed.metric.eq("soil_moisture") & observed.date.le(latest)]
        soil_date = None if region_soil.empty else pd.Timestamp(region_soil.date.max())
        soil_now_values = region_soil[region_soil.date.eq(soil_date)].value if soil_date is not None else pd.Series(dtype=float)
        soil_now = None if soil_now_values.empty else _soil_percent(
            float(soil_now_values.mean()), canonical_percent=canonical_soil_percent
        )
        soil_history = _soil_percent(
            None if soil_date is None else _five_year_region(observed, region, "soil_moisture", soil_date, soil_date),
            canonical_percent=canonical_soil_percent,
        )
        seven_day_date = None if soil_date is None else soil_date - pd.Timedelta(days=7)
        soil_prior_values = region_soil[region_soil.date.eq(seven_day_date)].value if seven_day_date is not None else pd.Series(dtype=float)
        soil_prior = None if soil_prior_values.empty else _soil_percent(
            float(soil_prior_values.mean()), canonical_percent=canonical_soil_percent
        )
        row["soil_moisture"] = {
            "region_identity": region,
            "date": None if soil_date is None else soil_date.date().isoformat(),
            "current_pct": soil_now,
            "five_year_same_date_mean_pct": soil_history,
            "difference_pct_points": None if soil_now is None or soil_history is None else soil_now - soil_history,
            "seven_days_ago_date": None if soil_prior is None or seven_day_date is None else seven_day_date.date().isoformat(),
            "seven_days_ago_region_identity": None if soil_prior is None else region,
            "seven_days_ago_pct": soil_prior,
            "change_vs_seven_days_ago_pct_points": None if soil_now is None or soil_prior is None else soil_now - soil_prior,
            "comparison_identity_matches": soil_prior is None or region == row["region_identity"],
            "comparison_tolerance_days": 0,
            "unit": "%",
            "depth": "0-100cm",
        }
        if str(config.get("crop")) == "palm_oil":
            month_start = latest.replace(day=1)
            ec_end = forecasts[forecasts.model.eq("ECMWF")].date.max()
            month_end = min(pd.Timestamp(latest.year, latest.month, 1) + pd.offsets.MonthEnd(0), pd.Timestamp(ec_end if pd.notna(ec_end) else latest))
            month_models = {}
            for model in ("ECMWF", "GFS"):
                observed_month = observed[observed.region.eq(region) & observed.metric.eq("precipitation") & observed.date.between(month_start, latest)].value
                future_month = forecasts[forecasts.region.eq(region) & forecasts.model.eq(model) & forecasts.metric.eq("precipitation") & forecasts.date.between(latest + pd.Timedelta(days=1), month_end)].value
                value = None if observed_month.empty else float(observed_month.sum() + future_month.sum())
                normal = _five_year_region(observed, region, "precipitation", month_start, month_end)
                absolute, relative = _anomaly(value, normal)
                month_models[model] = {"precipitation_mm": value, "five_year_mm": normal, "absolute_anomaly_mm": absolute, "relative_anomaly_pct": relative}
            row["month_to_forecast_end"] = {"month": int(latest.month), "start": month_start.date().isoformat(), "end": month_end.date().isoformat(), "models": month_models}
        rows.append(row)
    weighted = bool(config.get("weighted_aggregation", config.get("weighted_coverage_percent") is not None))
    return sorted(rows, key=lambda row: (-(row["weight_pct"] or 0), row["display_order"])) if weighted else sorted(rows, key=lambda row: row["display_order"])


def _weighted_facts(records: pd.DataFrame, normals: pd.DataFrame, config: dict[str, Any], latest: pd.Timestamp) -> dict[str, Any]:
    observed = records[records.data_type.eq("observed")]
    forecasts = select_latest_forecasts(records, latest)
    current_window = (latest - pd.Timedelta(days=6), latest)
    previous_window = (latest - pd.Timedelta(days=13), latest - pd.Timedelta(days=7))
    facts: dict[str, Any] = {
        "aggregation_mode": "weighted",
        "fixed_denominator_pct": float(config["weighted_coverage_percent"]),
        "latest_observed_date": latest.date().isoformat(),
        "forecast_end": {
            model: (None if forecasts[forecasts.model.eq(model)].empty else pd.Timestamp(forecasts[forecasts.model.eq(model)].date.max()).date().isoformat())
            for model in ("ECMWF", "GFS")
        },
    }
    for name, window in (("observed_7d", current_window), ("previous_observed_7d", previous_window)):
        rain, coverage = _aggregate(observed[observed.date.between(*window) & observed.metric.eq("precipitation")], config, "precipitation")
        temp, temp_coverage = _aggregate(observed[observed.date.between(*window) & observed.metric.eq("temperature_max")], config, "temperature_max")
        rain_normal, normal_coverage = _normal(normals, config, "precipitation", *window)
        temp_normal, _ = _normal(normals, config, "temperature_max", *window)
        rain_abs, rain_pct = _anomaly(rain, rain_normal)
        facts[name] = {
            "start": window[0].date().isoformat(), "end": window[1].date().isoformat(),
            "precipitation_mm": rain, "previous_week_mm": None, "normal_mm": rain_normal,
            "absolute_anomaly_mm": rain_abs, "relative_anomaly_pct": rain_pct,
            "coverage_pct": coverage, "normal_coverage_pct": normal_coverage,
            "temperature_max_c": temp, "temperature_normal_c": temp_normal,
            "temperature_anomaly_c": None if temp is None or temp_normal is None else temp - temp_normal,
            "temperature_coverage_pct": temp_coverage,
        }
    facts["observed_7d"]["previous_week_mm"] = facts["previous_observed_7d"]["precipitation_mm"]
    common = set(pd.to_datetime(forecasts[forecasts.model.eq("ECMWF")].date).dt.normalize()) & set(pd.to_datetime(forecasts[forecasts.model.eq("GFS")].date).dt.normalize())
    start = min(common) if common else None
    for index in (1, 2):
        key = f"forecast_week_{index}"
        if start is None:
            facts[key] = {"missing_reason": "EC/GFS无共同预测日期"}
            continue
        window = (start + pd.Timedelta(days=7 * (index - 1)), start + pd.Timedelta(days=7 * index - 1))
        item: dict[str, Any] = {"start": window[0].date().isoformat(), "end": window[1].date().isoformat(), "models": {}}
        for model in ("ECMWF", "GFS"):
            selected = forecasts[forecasts.model.eq(model) & forecasts.date.between(*window)]
            rain, coverage = _aggregate(selected[selected.metric.eq("precipitation")], config, "precipitation")
            rain_normal, _ = _normal(normals, config, "precipitation", *window)
            rain_abs, rain_pct = _anomaly(rain, rain_normal)
            temp, temp_coverage = _aggregate(selected[selected.metric.eq("temperature_max")], config, "temperature_max")
            temp_normal, _ = _normal(normals, config, "temperature_max", *window)
            item["models"][model] = {
                "precipitation_mm": rain, "normal_mm": rain_normal, "absolute_anomaly_mm": rain_abs,
                "relative_anomaly_pct": rain_pct, "coverage_pct": coverage,
                "temperature_max_c": temp, "temperature_normal_c": temp_normal,
                "temperature_anomaly_c": None if temp is None or temp_normal is None else temp - temp_normal,
                "temperature_coverage_pct": temp_coverage,
            }
        ec = item["models"]["ECMWF"]["relative_anomaly_pct"]
        gfs = item["models"]["GFS"]["relative_anomaly_pct"]
        item["model_consistency"] = None if ec is None or gfs is None else classify_model_consistency(ec, gfs)
        facts[key] = item
    region_rows = _region_fact_rows(records, normals, config, latest)
    facts["regions"] = region_rows
    soil_rows = [dict(row["soil_moisture"], region=row["region"], display_name=row["display_label"], weight_pct=row["weight_pct"]) for row in region_rows]
    facts["soil_moisture"] = {"unit": "%", "depth": "0-100cm", "regions": soil_rows, "classification": "classification_pending"}
    weighted_soil: dict[str, Any] = {}
    for key in ("current_pct", "five_year_same_date_mean_pct", "seven_days_ago_pct"):
        values = pd.Series({row["region"]: row["soil_moisture"].get(key) for row in region_rows}, dtype=float).dropna()
        value, coverage = weighted_values(values, config)
        weighted_soil[key] = None if pd.isna(value) else float(value)
        weighted_soil[f"{key}_coverage_pct"] = float(coverage)
    weighted_soil["difference_pct_points"] = None if weighted_soil["current_pct"] is None or weighted_soil["five_year_same_date_mean_pct"] is None else weighted_soil["current_pct"] - weighted_soil["five_year_same_date_mean_pct"]
    weighted_soil["change_vs_seven_days_ago_pct_points"] = None if weighted_soil["current_pct"] is None or weighted_soil["seven_days_ago_pct"] is None else weighted_soil["current_pct"] - weighted_soil["seven_days_ago_pct"]
    weighted_soil.update({"unit": "%", "depth": "0-100cm"})
    facts["weighted_soil_moisture"] = weighted_soil
    facts["current_7d_start"] = current_window[0].date().isoformat()
    facts["current_7d_end"] = current_window[1].date().isoformat()
    facts["previous_7d_start"] = previous_window[0].date().isoformat()
    facts["previous_7d_end"] = previous_window[1].date().isoformat()
    return facts


def _regional_facts(records: pd.DataFrame, config: dict[str, Any], latest: pd.Timestamp) -> dict[str, Any]:
    forecasts = select_latest_forecasts(records, latest)
    current_window = (latest - pd.Timedelta(days=6), latest)
    previous_window = (latest - pd.Timedelta(days=13), latest - pd.Timedelta(days=7))
    return {
        "aggregation_mode": "regional",
        "latest_observed_date": latest.date().isoformat(),
        "current_7d_start": current_window[0].date().isoformat(),
        "current_7d_end": current_window[1].date().isoformat(),
        "previous_7d_start": previous_window[0].date().isoformat(),
        "previous_7d_end": previous_window[1].date().isoformat(),
        "regions": _region_fact_rows(records, pd.DataFrame(), config, latest),
        "forecast_end": {model: (None if forecasts[forecasts.model.eq(model)].empty else pd.Timestamp(forecasts[forecasts.model.eq(model)].date.max()).date().isoformat()) for model in ("ECMWF", "GFS")},
    }


def _fmt_value(value: float | None, suffix: str = "", *, signed: bool = False) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f'{value:+.1f}{suffix}' if signed else f'{value:.1f}{suffix}'


def _rain_history_cell(item: Mapping[str, Any]) -> str:
    relative = item.get("relative_anomaly_pct")
    absolute = item.get("absolute_anomaly_mm")
    if relative is None or absolute is None:
        return "不可比"
    code = classify_rainfall(relative, absolute)
    if code == "rainfall_near_normal_absolute_guard":
        return f'{_bold(RAIN_TEXT[code])}（距平 {absolute:+.1f}mm）'
    return f'{_bold(RAIN_TEXT[code])} {relative:+.1f}%'


def _rain_forecast_cell(week: Mapping[str, Any], model: str) -> str:
    if "models" not in week:
        return "数据缺失"
    item = week["models"][model]
    if item.get("precipitation_mm") is None:
        return "数据缺失"
    return _rain_history_cell(item)


def _temperature_history_cell(anomaly: float | None) -> str:
    if anomaly is None or pd.isna(anomaly):
        return "不可比"
    return f'{_bold(TEMP_TEXT[classify_temperature(anomaly)])} {anomaly:+.1f}℃'


def _temperature_forecast_cell(week: Mapping[str, Any], model: str) -> str:
    if "models" not in week:
        return "数据缺失"
    return _temperature_history_cell(week["models"][model].get("temperature_anomaly_c"))


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    return "| " + " | ".join(headers) + " |\n| " + " | ".join("---" for _ in headers) + " |\n" + "\n".join("| " + " | ".join(row) + " |" for row in rows)


def _both_models_severe_rain(week: Mapping[str, Any], direction: str) -> bool:
    if "models" not in week:
        return False
    suffix = "below_normal" if direction == "dry" else "above_normal"
    approved = {f"rainfall_clearly_{suffix}", f"rainfall_significantly_{suffix}"}
    return all(
        _rain_class(week["models"][model].get("relative_anomaly_pct"), week["models"][model].get("absolute_anomaly_mm")) in approved
        for model in ("ECMWF", "GFS")
    )


def _both_models_severe_hot(week: Mapping[str, Any]) -> bool:
    if "models" not in week:
        return False
    approved = {"temperature_clearly_above_normal", "temperature_significantly_above_normal"}
    return all(classify_temperature(week["models"][model].get("temperature_anomaly_c")) in approved for model in ("ECMWF", "GFS") if week["models"][model].get("temperature_anomaly_c") is not None) and all(week["models"][model].get("temperature_anomaly_c") is not None for model in ("ECMWF", "GFS"))


def _focus_week_text(week: Mapping[str, Any]) -> str:
    signal = _week_rain_signal(week)
    if signal == "divergent":
        return "模型方向分歧"
    if signal == "wet":
        return "一致显著偏湿" if _week_is_significant(week, "wet") else "方向一致偏湿"
    if signal == "dry":
        return "一致显著偏干" if _week_is_significant(week, "dry") else "方向一致偏干"
    return {"neutral": "接近正常", "missing": "数据不足"}[signal]


def _focus_current_state(row: Mapping[str, Any]) -> str:
    soil = row["soil_moisture"]
    difference = soil.get("difference_pct_points")
    if difference is None:
        return "土墒同期比较不可用"
    if abs(difference) < SOIL_SMALL_DIFFERENCE_PCT_POINTS:
        return f"土墒接近5年同期（{difference:+.1f}个百分点）"
    direction = "高" if difference > 0 else "低"
    return f"土墒较5年同期{direction}{abs(difference):.1f}个百分点"


def _focus_recent_change(row: Mapping[str, Any]) -> str:
    change = row["soil_moisture"].get("change_vs_seven_days_ago_pct_points")
    if change is None:
        return "无7日可比值"
    if change < 0:
        return f"7日下降{abs(change):.1f}个百分点"
    if change > 0:
        return f"7日回升{change:.1f}个百分点"
    return "7日持平"


def _focus_status(row: Mapping[str, Any], *, high_weight: bool) -> tuple[str, str]:
    soil = row["soil_moisture"]
    difference = soil.get("difference_pct_points")
    change = soil.get("change_vs_seven_days_ago_pct_points")
    weeks = row["forecast_weeks"]
    rain_signals = [_week_rain_signal(week) for week in weeks]
    severe_dry = [_both_models_severe_rain(week, "dry") for week in weeks]
    soil_below = difference is not None and difference < 0
    soil_declining = change is not None and change < 0
    if soil_below and soil_declining and any(severe_dry):
        return "风险", "土墒低于同期且近7日继续下降，未来至少一段EC/GFS均明显偏干"
    if soil_below and all(severe_dry) and any(_both_models_severe_hot(week) for week in weeks):
        return "风险", "土墒低于同期，并叠加持续一致偏干及明显偏热信号"
    if soil_declining and difference is not None and difference >= 0:
        return "关注", "当前水分仍有缓冲，但近7日土墒走弱"
    if "divergent" in rain_signals:
        return "关注", "EC/GFS方向分歧，预报不确定性较高"
    if "dry" in rain_signals:
        return "关注", "未来存在一致偏干信号，需跟踪水分变化"
    observed = row["observed_7d"]
    observed_code = _rain_class(observed.get("relative_anomaly_pct"), observed.get("absolute_anomaly_mm"))
    obvious_change = observed_code in {
        "rainfall_clearly_below_normal", "rainfall_significantly_below_normal",
        "rainfall_clearly_above_normal", "rainfall_significantly_above_normal",
    } or any(signal in {"wet", "dry", "divergent"} for signal in rain_signals) or any(_temperature_week_signal(week) in {"high", "low"} for week in weeks)
    if high_weight and obvious_change:
        return "关注", "高权重核心产区出现明确天气变化"
    return "平稳", "当前暂无复合异常，研究优先级较低"


def _build_focus_regions(regions: list[Mapping[str, Any]], *, weighted: bool, limit: int = 5) -> list[dict[str, Any]]:
    weighted_order = sorted((row for row in regions if row.get("weight_pct") is not None), key=lambda row: (-float(row["weight_pct"]), int(row.get("display_order", 999))))
    high_weight_ids = {row["region_identity"] for row in weighted_order[:2]}
    rows: list[dict[str, Any]] = []
    for source in regions:
        status, reason = _focus_status(source, high_weight=weighted and source.get("region_identity") in high_weight_ids)
        week1, week2 = source["forecast_weeks"]
        rows.append({
            "region_identity": source["region_identity"],
            "region": source["display_label"],
            "status": status,
            "current_state": _focus_current_state(source),
            "recent_change": _focus_recent_change(source),
            "short_term": _focus_week_text(week1),
            "medium_term": _focus_week_text(week2),
            "reason": reason,
            "weight_pct": source.get("weight_pct"),
            "display_order": int(source.get("display_order", 999)),
        })
    priority = {"风险": 0, "关注": 1, "平稳": 2}
    selected = sorted((row for row in rows if row["status"] != "平稳"), key=lambda row: (priority[row["status"]], -(row.get("weight_pct") or 0.0) if weighted else 0.0, row["display_order"]))
    if weighted and len(selected) < min(3, len(rows)):
        selected_ids = {row["region_identity"] for row in selected}
        stable = sorted((row for row in rows if row["region_identity"] not in selected_ids), key=lambda row: (-(row.get("weight_pct") or 0.0), row["display_order"]))
        selected.extend(stable[: min(3, len(rows)) - len(selected)])
    return selected[:limit]


def _focus_regions_sentence(regions: list[Mapping[str, Any]], *, weighted: bool, limit: int = 3) -> str:
    selected = _build_focus_regions(regions, weighted=weighted, limit=max(limit, 5))[:limit]
    return _selected_focus_sentence(selected)


def _selected_focus_sentence(selected: list[Mapping[str, Any]], limit: int = 3) -> str:
    selected = selected[:limit]
    if not selected:
        return "当前暂无需要特别关注的地区。"
    return "当前主要关注" + "；".join(f'{row["region"]}：{row["reason"]}' for row in selected) + "。"


def _weighted_comprehensive(facts: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    current = facts["observed_7d"]
    regions = list(facts["regions"])
    subject = str(config.get("page_title", "天气产区")).replace("天气研究", "").replace("天气", "")
    sentences = [
        _rain_comprehensive_sentence(current, facts["forecast_week_1"], facts["forecast_week_2"]),
        _temperature_comprehensive_sentence(facts["forecast_week_1"], facts["forecast_week_2"]),
        _selected_focus_sentence(list(facts["focus_regions"])),
    ]
    overall = _overall_weather_sentence(subject, facts["forecast_week_1"], facts["forecast_week_2"])
    if overall:
        sentences.append(overall)
    return "".join(sentences[:4])


def _regional_table_comprehensive(facts: Mapping[str, Any]) -> str:
    regions = list(facts["regions"])
    return _regional_comprehensive(regions) + _selected_focus_sentence(list(facts["focus_regions"]))


def _weather_short_text(facts: Mapping[str, Any]) -> str:
    regions = list(facts["regions"])
    if facts["aggregation_mode"] == "weighted":
        return (
            _rain_comprehensive_sentence(facts["observed_7d"], facts["forecast_week_1"], facts["forecast_week_2"])
            + _temperature_comprehensive_sentence(facts["forecast_week_1"], facts["forecast_week_2"])
            + _selected_focus_sentence(list(facts["focus_regions"]), limit=1)
        )
    regional_sentences = [sentence + "。" for sentence in _regional_comprehensive(regions).split("。") if sentence]
    return "".join(regional_sentences[:2]) + _selected_focus_sentence(list(facts["focus_regions"]), limit=1)


def _table_weather_text(facts: Mapping[str, Any], config: Mapping[str, Any]) -> tuple[str, str]:
    regions = list(facts["regions"])
    weighted = facts["aggregation_mode"] == "weighted"
    facts["focus_regions"] = _build_focus_regions(regions, weighted=weighted)
    comprehensive = _weighted_comprehensive(facts, config) if weighted else _regional_table_comprehensive(facts)
    history_label = "30年同期" if weighted else "5年同期"
    rain_rows: list[list[str]] = []
    temp_rows: list[list[str]] = []
    soil_rows: list[list[str]] = []
    for row in regions:
        observed = row["observed_7d"]
        temperature = row["temperature_7d"]
        week1, week2 = row["forecast_weeks"]
        rain_rows.append([
            row["display_label"],
            _fmt_value(observed.get("current_7d_mm")),
            _fmt_value(observed.get("delta_vs_previous_7d_mm"), signed=True),
            _rain_history_cell(observed),
            _rain_forecast_cell(week1, "ECMWF"),
            _rain_forecast_cell(week1, "GFS"),
            _rain_forecast_cell(week2, "ECMWF"),
            _rain_forecast_cell(week2, "GFS"),
        ])
        temp_rows.append([
            row["display_label"],
            _fmt_value(temperature.get("current_7d_c"), "℃"),
            _fmt_value(temperature.get("delta_vs_previous_7d_c"), "℃", signed=True),
            _temperature_history_cell(temperature.get("anomaly_c")),
            _temperature_forecast_cell(week1, "ECMWF"),
            _temperature_forecast_cell(week1, "GFS"),
            _temperature_forecast_cell(week2, "ECMWF"),
            _temperature_forecast_cell(week2, "GFS"),
        ])
        soil = row["soil_moisture"]
        soil_rows.append([
            row["display_label"],
            _fmt_value(soil.get("current_pct"), "%"),
            _fmt_value(soil.get("five_year_same_date_mean_pct"), "%"),
            _fmt_value(soil.get("difference_pct_points"), signed=True),
            "无7日可比值" if soil.get("change_vs_seven_days_ago_pct_points") is None else _fmt_value(soil["change_vs_seven_days_ago_pct_points"], signed=True),
        ])
    if weighted:
        label = f'**加权结果（{float(facts["fixed_denominator_pct"]):g}%覆盖）**'
        current = facts["observed_7d"]
        previous = facts["previous_observed_7d"]
        rain_rows.append([
            label,
            _fmt_value(current.get("precipitation_mm")),
            _fmt_value(None if current.get("precipitation_mm") is None or previous.get("precipitation_mm") is None else current["precipitation_mm"] - previous["precipitation_mm"], signed=True),
            _rain_history_cell(current),
            _rain_forecast_cell(facts["forecast_week_1"], "ECMWF"),
            _rain_forecast_cell(facts["forecast_week_1"], "GFS"),
            _rain_forecast_cell(facts["forecast_week_2"], "ECMWF"),
            _rain_forecast_cell(facts["forecast_week_2"], "GFS"),
        ])
        temp_rows.append([
            label,
            _fmt_value(current.get("temperature_max_c"), "℃"),
            _fmt_value(None if current.get("temperature_max_c") is None or previous.get("temperature_max_c") is None else current["temperature_max_c"] - previous["temperature_max_c"], "℃", signed=True),
            _temperature_history_cell(current.get("temperature_anomaly_c")),
            _temperature_forecast_cell(facts["forecast_week_1"], "ECMWF"),
            _temperature_forecast_cell(facts["forecast_week_1"], "GFS"),
            _temperature_forecast_cell(facts["forecast_week_2"], "ECMWF"),
            _temperature_forecast_cell(facts["forecast_week_2"], "GFS"),
        ])
        soil = facts["weighted_soil_moisture"]
        soil_rows.append([
            label,
            _fmt_value(soil.get("current_pct"), "%"),
            _fmt_value(soil.get("five_year_same_date_mean_pct"), "%"),
            _fmt_value(soil.get("difference_pct_points"), signed=True),
            "无7日可比值" if soil.get("change_vs_seven_days_ago_pct_points") is None else _fmt_value(soil["change_vs_seven_days_ago_pct_points"], signed=True),
        ])
    date_note = f'当前窗口：{facts["current_7d_start"][5:]}—{facts["current_7d_end"][5:]}｜前一窗口：{facts["previous_7d_start"][5:]}—{facts["previous_7d_end"][5:]}。'
    forecast_windows = regions[0].get("forecast_weeks", []) if regions else []
    forecast_note = ""
    if len(forecast_windows) == 2 and all("start" in week and "end" in week for week in forecast_windows):
        forecast_note = f'短期：{_fmt_window(forecast_windows[0])}｜中期：{_fmt_window(forecast_windows[1])}。'
    baseline_note = f"历史基准：{history_label}。"
    if not weighted:
        baseline_note += "本页按正式配置区域展示，不生成未经批准的全国加权结果。"
    rain_headers = ["地区", "过去7天", "较前7天", "较历史", "短期EC", "短期GFS", "中期EC", "中期GFS"]
    temp_headers = ["地区", "过去7天", "较前7天", "较历史", "短期EC", "短期GFS", "中期EC", "中期GFS"]
    soil_headers = ["地区", "当前含水率", "5年同期", "较5年同期", "较7日前"]
    focus_rows = [[row["region"], row["status"], row["current_state"], row["recent_change"], row["short_term"], row["medium_term"], row["reason"]] for row in facts["focus_regions"]]
    focus_markdown = _markdown_table(["地区", "状态", "当前状态", "近7日变化", "短期1—7天", "中期8—14天", "关注原因"], focus_rows) if focus_rows else "当前暂无需要特别关注的地区。"
    sections = [f'### 综合\n{comprehensive}', f'### 重点关注\n{focus_markdown}']
    if str(config.get("crop")) == "palm_oil":
        month = regions[0]["month_to_forecast_end"]["month"] if regions else pd.Timestamp(facts["latest_observed_date"]).month
        month_rows = []
        for row in regions:
            month_models = row["month_to_forecast_end"]["models"]
            month_rows.append([row["display_label"], _rain_history_cell(month_models["ECMWF"]), _rain_history_cell(month_models["GFS"])])
        rain_section = f'#### {month}月累计降雨较5年同期均值\n' + _markdown_table(["地区", "EC", "GFS"], month_rows) + f'\n\n#### 近期变化\n{date_note}\n{forecast_note}\n{baseline_note}\n' + _markdown_table(rain_headers, rain_rows)
    else:
        rain_section = f'{date_note}\n{forecast_note}\n{baseline_note}\n' + _markdown_table(rain_headers, rain_rows)
    temp_section = f'{date_note}\n{forecast_note}\n{baseline_note}差值单位：℃。\n' + _markdown_table(temp_headers, temp_rows)
    soil_section = '0—100厘米土层土壤含水率；单位：%；差值单位：百分点。\n' + _markdown_table(soil_headers, soil_rows)
    sections.extend([f'### 完整降水数据\n{rain_section}', f'### 完整最高气温数据\n{temp_section}', f'### 完整土墒数据\n{soil_section}'])
    facts["weather_render"] = {
        "comprehensive": comprehensive,
        "focus_regions": facts["focus_regions"],
        "rain_markdown": rain_section,
        "temperature_markdown": temp_section,
        "soil_markdown": soil_section,
    }
    return "\n\n".join(sections), _weather_short_text(facts)


def _weighted_text(facts: Mapping[str, Any], config: Mapping[str, Any]) -> tuple[str, str]:
    current = facts["observed_7d"]
    current_mm = current["precipitation_mm"]
    rain_text = f'过去一周（{_fmt_window(current)}）：加权降雨{current_mm:.1f}mm' if current_mm is not None else f'过去一周（{_fmt_window(current)}）：降雨数据缺失'
    if current.get("relative_anomaly_pct") is not None:
        observed_code = classify_rainfall(current["relative_anomaly_pct"], current["absolute_anomaly_mm"])
        rain_text += f'，较30年同期{current["relative_anomaly_pct"]:+.1f}%，为{_bold(RAIN_TEXT[observed_code])}'
    if current_mm is not None and current.get("previous_week_mm") is not None:
        change = current_mm - current["previous_week_mm"]
        rain_text += f'，较前一周{"增加" if change >= 0 else "减少"}{abs(change):.1f}mm'
    rain_text += '。'
    forecast_lines = []
    temp_lines = []
    for index in (1, 2):
        week = facts[f"forecast_week_{index}"]
        if "models" not in week:
            forecast_lines.append(f'未来{"第一" if index == 1 else "第二"}周：{week["missing_reason"]}。')
            continue
        model_parts = []
        temp_parts = []
        for model, label in (("ECMWF", "EC"), ("GFS", "GFS")):
            item = week["models"][model]
            pct = item["relative_anomaly_pct"]
            if pct is None:
                rain_class = "历史距平不可比"
            else:
                rain_code = classify_rainfall(pct, item["absolute_anomaly_mm"])
                guard = "，绝对距平不足5mm" if rain_code == "rainfall_near_normal_absolute_guard" else ""
                rain_class = _bold(RAIN_TEXT[rain_code]) + f'（{pct:+.1f}%{guard}）'
            model_parts.append(f'{label}预计{rain_class}')
            anomaly = item["temperature_anomaly_c"]
            temp_class = "历史距平不可比" if anomaly is None else _bold(TEMP_TEXT[classify_temperature(anomaly)]) + f'（{anomaly:+.1f}℃）'
            temp_parts.append(f'{label}{temp_class}')
        week_name = "第一" if index == 1 else "第二"
        forecast_lines.append(
            f'未来{week_name}周（{_fmt_window(week)}）：'
            + '，'.join(model_parts)
            + '，'
            + _model_consistency_text(
                week["models"]["ECMWF"]["relative_anomaly_pct"],
                week["models"]["GFS"]["relative_anomaly_pct"],
                week["models"]["ECMWF"]["absolute_anomaly_mm"],
                week["models"]["GFS"]["absolute_anomaly_mm"],
            )
            + '。'
        )
        temp_lines.append(f'未来{week_name}周：' + '，'.join(temp_parts) + '。')
    temp = current.get("temperature_max_c")
    temp_anomaly = current.get("temperature_anomaly_c")
    temp_text = "过去一周最高气温数据缺失。" if temp is None else f'过去一周：最高气温加权{temp:.1f}℃' + ("。" if temp_anomaly is None else f'，较30年同期{"高" if temp_anomaly >= 0 else "低"}{abs(temp_anomaly):.1f}℃。')
    soil_rows = facts["soil_moisture"]["regions"]
    soil_text = "；".join(f'{row["display_name"]}：{row["current_pct"]:.1f}%，较5年同期均值{"高" if row["difference_pct_points"] >= 0 else "低"}{abs(row["difference_pct_points"]):.1f}个百分点' for row in soil_rows if row["current_pct"] is not None and row["difference_pct_points"] is not None) or "土墒5年同期比较暂不可用"
    subject = str(config.get("page_title", "天气产区")).replace("天气研究", "").replace("天气", "")
    comprehensive = "".join(filter(None, (
        _rain_comprehensive_sentence(current, facts["forecast_week_1"], facts["forecast_week_2"]),
        _temperature_comprehensive_sentence(facts["forecast_week_1"], facts["forecast_week_2"]),
        _soil_comprehensive_sentence(soil_rows),
        _overall_weather_sentence(subject, facts["forecast_week_1"], facts["forecast_week_2"]),
    )))
    detail = f'### 降水\n{rain_text}\n' + '\n'.join(forecast_lines) + f'\n\n### 气温\n{temp_text}\n' + '\n'.join(temp_lines) + f'\n\n### 土墒\n0—100厘米土层土壤含水率：{soil_text}。\n\n### 综合\n{comprehensive}'
    short = comprehensive
    return detail, short


def _regional_text(facts: Mapping[str, Any], config: Mapping[str, Any]) -> tuple[str, str]:
    regions = list(facts["regions"])
    palm = str(config.get("crop")) == "palm_oil"
    if palm:
        lines = []
        for row in regions:
            month = row.get("month_to_forecast_end", {})
            models = month.get("models", {})
            parts = []
            for model, label in (("ECMWF", "EC"), ("GFS", "GFS")):
                pct = models.get(model, {}).get("relative_anomaly_pct")
                if pct is None:
                    parts.append(f'{label}数据缺失')
                else:
                    item = models[model]
                    code = classify_rainfall(pct, item.get("absolute_anomaly_mm"))
                    parts.append(f'{label}{_bold(RAIN_TEXT[code])}（{pct:+.1f}%）')
            lines.append(f'{row["display_name"]}：' + '，'.join(parts))
        month_number = regions[0].get("month_to_forecast_end", {}).get("month", pd.Timestamp(facts["latest_observed_date"]).month) if regions else pd.Timestamp(facts["latest_observed_date"]).month
        flat_signals = []
        for row in regions:
            for model in ("ECMWF", "GFS"):
                item = row["month_to_forecast_end"]["models"][model]
                effective = _effective_rainfall_signal(item.get("relative_anomaly_pct"), item.get("absolute_anomaly_mm"))
                if effective is not None:
                    flat_signals.append("wet" if effective > 20 else "dry" if effective < -20 else "neutral")
        monthly = _majority_signal(flat_signals, "divergent")
        monthly_text = {"wet": "偏湿", "dry": "偏干", "neutral": "接近正常", "divergent": "模型方向分歧", "missing": "数据不足"}[monthly]
        comprehensive = f'本月主要监测区域累计降雨整体{_bold(monthly_text)}。'
        if monthly == "divergent":
            comprehensive += f'区域或模型间{_bold("模型方向分歧")}，当前预报不确定性较高。'
        detail = f'### {month_number}月累计降雨较5年同期均值\n' + '；'.join(lines) + f'。\n\n### 综合\n{comprehensive}'
        short = comprehensive + ('；'.join(lines[:2]) + '。' if lines else '')
        return detail, short
    region_lines = []
    for row in regions[:5]:
        observed = row["observed_7d"]
        current = observed.get("precipitation_mm")
        previous = observed.get("previous_week_mm")
        change = None if current is None or previous is None else current - previous
        line = f'{row["display_name"]}：过去一周{current:.1f}mm' if current is not None else f'{row["display_name"]}：过去一周数据缺失'
        if change is not None:
            line += f'，较前一周{"增加" if change >= 0 else "减少"}{abs(change):.1f}mm'
        for index, week in enumerate(row["forecast_weeks"], start=1):
            week_name = "第一" if index == 1 else "第二"
            if "models" not in week:
                line += f'；未来{week_name}周{week.get("missing_reason", "数据缺失")}'
                continue
            model_parts = []
            for model, label in (("ECMWF", "EC"), ("GFS", "GFS")):
                item = week["models"][model]
                rain = item["precipitation_mm"]
                relative = item["relative_anomaly_pct"]
                if rain is None:
                    model_parts.append(f'{label}数据缺失')
                elif relative is None:
                    model_parts.append(f'{label}预计{rain:.1f}mm，5年同期不可比')
                else:
                    code = classify_rainfall(relative, item["absolute_anomaly_mm"])
                    guard = "，绝对距平不足5mm" if code == "rainfall_near_normal_absolute_guard" else ""
                    model_parts.append(f'{label}预计{rain:.1f}mm，较5年同期{_bold(RAIN_TEXT[code])}（{relative:+.1f}%{guard}）')
            line += f'；未来{week_name}周（{_fmt_window(week)}）：' + '，'.join(model_parts) + f'，{week["model_consistency"]}'
        region_lines.append(line)
    soil_lines = [f'{row["display_name"]}{row["soil_moisture"]["current_pct"]:.1f}%（较5年同期{row["soil_moisture"]["difference_pct_points"]:+.1f}个百分点）' for row in regions[:5] if row["soil_moisture"]["current_pct"] is not None and row["soil_moisture"]["difference_pct_points"] is not None]
    temp_lines = []
    for row in regions[:5]:
        observed_temp = row["observed_temperature"]
        current_temp = observed_temp["temperature_max_c"]
        current_anomaly = observed_temp["temperature_anomaly_c"]
        text = f'{row["display_name"]}：过去一周最高温{current_temp:.1f}℃' if current_temp is not None else f'{row["display_name"]}：过去一周最高温缺失'
        if current_anomaly is not None:
            text += f'，较5年同期{current_anomaly:+.1f}℃'
        for index, week in enumerate(row["forecast_weeks"], start=1):
            if "models" not in week:
                text += f'；第{index}周预测缺失'
                continue
            parts = []
            for model, label in (("ECMWF", "EC"), ("GFS", "GFS")):
                anomaly = week["models"][model]["temperature_anomaly_c"]
                parts.append(f'{label}{_bold(TEMP_TEXT[classify_temperature(anomaly)])}（{anomaly:+.1f}℃）' if anomaly is not None else f'{label}不可比')
            text += f'；第{index}周' + '、'.join(parts)
        temp_lines.append(text)
    comprehensive = _regional_comprehensive(regions[:5])
    detail = '### 降水与未来两周\n' + '。\n'.join(region_lines) + '。\n\n### 气温\n' + '。\n'.join(temp_lines) + '。\n\n### 土墒\n0—100厘米土层土壤含水率：' + ('；'.join(soil_lines) if soil_lines else '5年同期比较暂不可用') + f'。\n\n### 综合\n{comprehensive}'
    short = comprehensive
    return detail, short


def build_weather_summary(records: pd.DataFrame, normals: pd.DataFrame, config: dict[str, Any], *, source_identity: Mapping[str, Any], generated_at: datetime | None = None) -> Summary:
    records = records.copy()
    records["date"] = pd.to_datetime(records["date"], errors="coerce").dt.normalize()
    rules = load_summary_rules()
    latest = latest_observation_date(records)
    if latest is None:
        return Summary.create(module="weather", source_dataset="weather_daily", source_identity=source_identity, source_date=None, comparison_identity=None, generated_at=generated_at, calculation_version=rules["calculation_version"], rule_version=rules["rule_version"], freshness_status="missing", facts={"aggregation_mode": "weighted" if bool(config.get("weighted_aggregation", config.get("weighted_coverage_percent") is not None)) else "regional"}, classifications=[], headline=str(config.get("page_title", "天气摘要")), detail_text="天气摘要暂不可用。", short_text="天气摘要暂不可用。", missing_reason="无实况数据")
    weighted = bool(config.get("weighted_aggregation", config.get("weighted_coverage_percent") is not None))
    cache_token = _FIVE_YEAR_CACHE.set({})
    try:
        facts = _weighted_facts(records, normals, config, latest) if weighted else _regional_facts(records, config, latest)
    finally:
        _FIVE_YEAR_CACHE.reset(cache_token)
    detail, short = _table_weather_text(facts, config)
    facts["country"] = str(config.get("country_display_name", config.get("country", "")))
    facts["crop"] = str(config.get("crop_display_name", config.get("summary_subject", config.get("crop", ""))))
    comparison = {"observed_window": facts.get("observed_7d"), "forecast_end": facts.get("forecast_end"), "aggregation_mode": facts["aggregation_mode"]}
    return Summary.create(module="weather", source_dataset="weather_daily", source_identity=source_identity, source_date=latest.date().isoformat(), comparison_identity=comparison, generated_at=generated_at, calculation_version=rules["calculation_version"], rule_version=rules["rule_version"], freshness_status=freshness_status("weather", latest.date().isoformat(), rules), facts=facts, classifications=["classification_pending"], headline=str(config.get("page_title", "天气摘要")).replace("研究", ""), detail_text=detail, short_text=short)
