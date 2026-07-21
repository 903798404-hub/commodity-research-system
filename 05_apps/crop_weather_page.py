"""Configuration-driven crop-weather research page using normalized local snapshots."""

from __future__ import annotations

import html
import json
import os
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "03_src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agri_research_agent.data_sources.weather_adapter import load_weather_records  # noqa: E402
from agri_research_agent.weather.crop_weather import (  # noqa: E402
    add_season_columns,
    latest_observation_date,
    load_weather_config,
    region_weights,
    select_latest_forecasts,
    weekly_metric_summary,
    weighted_values,
)


CONFIG_FILE = PROJECT_ROOT / "02_configs" / "soybean_weather_us.yaml"
STABLE_DATA_FILE = PROJECT_ROOT / "01_data" / "processed" / "weather" / "soybean" / "us" / "soybean_weather_us.parquet"
NORMAL_DATA_FILE = PROJECT_ROOT / "01_data" / "processed" / "weather" / "soybean" / "us" / "soybean_weather_us_30y_normal.parquet"
STATUS_FILE = PROJECT_ROOT / "01_data" / "update_status" / "soybean_weather_us.json"
FIXTURE_ENV = "SOYBEAN_WEATHER_FIXTURE_PATH"
NORMAL_FIXTURE_ENV = "SOYBEAN_WEATHER_NORMAL_FIXTURE_PATH"
PAGE_TITLE = "美国大豆天气研究"

WEATHER_COUNTRY_FILES = {
    "USA": {"config": CONFIG_FILE, "data": STABLE_DATA_FILE, "normal": NORMAL_DATA_FILE, "status": STATUS_FILE, "fixture_enabled": True},
    "BRA": {
        "config": PROJECT_ROOT / "02_configs" / "soybean_weather_br.yaml",
        "data": PROJECT_ROOT / "01_data" / "processed" / "weather" / "soybean" / "br" / "soybean_weather_br.parquet",
        "normal": PROJECT_ROOT / "01_data" / "processed" / "weather" / "soybean" / "br" / "soybean_weather_br_30y_normal.parquet",
        "status": PROJECT_ROOT / "01_data" / "update_status" / "soybean_weather_br.json",
    },
    "ARG": {
        "config": PROJECT_ROOT / "02_configs" / "soybean_weather_ar.yaml",
        "data": PROJECT_ROOT / "01_data" / "processed" / "weather" / "soybean" / "ar" / "soybean_weather_ar.parquet",
        "normal": PROJECT_ROOT / "01_data" / "processed" / "weather" / "soybean" / "ar" / "soybean_weather_ar_30y_normal.parquet",
        "status": PROJECT_ROOT / "01_data" / "update_status" / "soybean_weather_ar.json",
    },
    "CAN": {
        "config": PROJECT_ROOT / "02_configs" / "rapeseed_weather_can.yaml",
        "data": PROJECT_ROOT / "01_data" / "processed" / "weather" / "rapeseed" / "can" / "rapeseed_weather_can.parquet",
        "normal": PROJECT_ROOT / "01_data" / "processed" / "weather" / "rapeseed" / "can" / "rapeseed_weather_can_30y_normal.parquet",
        "status": PROJECT_ROOT / "01_data" / "update_status" / "rapeseed_weather_can.json",
    },
    "AUS": {
        "config": PROJECT_ROOT / "02_configs" / "rapeseed_weather_aus.yaml",
        "data": PROJECT_ROOT / "01_data" / "processed" / "weather" / "rapeseed" / "aus" / "rapeseed_weather_aus.parquet",
        "normal": None,
        "status": PROJECT_ROOT / "01_data" / "update_status" / "rapeseed_weather_aus.json",
    },
}

MODULES = {
    "rainfall_summary": ("降雨分析", "weekly", "precipitation"),
    "temperature_summary": ("最高气温分析", "weekly", "temperature_max"),
    "daily_rainfall": ("单日降雨", "daily_rain", "precipitation"),
    "cumulative_rainfall": ("累计降雨", "cumulative_rain", "precipitation"),
    "maximum_temperature": ("最高气温", "temperature", "temperature_max"),
    "soil_moisture": ("土壤墒情", "soil", "soil_moisture"),
}

OLD_PAGE_COLORS = {
    "historical_rain": "#3b6fd4",
    "current": "#bd2c25",
    "previous": "#334155",
    "history": "#94a3b8",
    "five_year_mean": "#2a9d6e",
    "gfs": "#3b6fd4",
    "ec": "#e07b39",
}
HISTORY_SERIES_COLORS = {
    "current": "#e53935",
    "previous": "#172033",
    "five_year_mean": "#15803d",
    "gfs": "#1688e8",
    "ec": "#ff8a22",
}
HISTORY_SERIES_PALETTE = (
    "#8fbce6", "#efb5c5", "#8ccfc8", "#c0a8df", "#efbe86",
    "#a9d28d", "#9fb7e6", "#e6aacb", "#8dcfd5", "#ceb7e8",
)
REFERENCE_YEAR = 2000
DAILY_BAR_WIDTH_MS = 0.19 * 24 * 60 * 60 * 1000


def _country_files(country: str) -> dict[str, object]:
    """Keep every country-specific local path at one page-boundary lookup."""

    try:
        return WEATHER_COUNTRY_FILES[country]
    except KeyError as exc:
        raise ValueError(f"未支持的天气国家：{country}") from exc


def _data_path(files: dict[str, object]) -> tuple[Path | None, str]:
    fixture_path = os.environ.get(FIXTURE_ENV, "").strip() if bool(files.get("fixture_enabled")) else ""
    if fixture_path:
        return Path(fixture_path), "本地测试 fixture"
    data_file = files["data"]
    if isinstance(data_file, Path) and data_file.is_file():
        return data_file, "本地历史快照"
    return None, "本地历史快照未接入"


def _normal_path(files: dict[str, object]) -> Path | None:
    """Use a normal-baseline fixture only when a test explicitly requests one."""

    fixture_path = os.environ.get(NORMAL_FIXTURE_ENV, "").strip() if bool(files.get("fixture_enabled")) else ""
    if fixture_path:
        return Path(fixture_path)
    normal_file = files.get("normal")
    return normal_file if isinstance(normal_file, Path) and normal_file.is_file() else None


@st.cache_data(show_spinner=False)
def _load_selected_records(data_path: str, data_mtime_ns: int, crop: str, country: str, metric: str) -> pd.DataFrame:
    del data_mtime_ns
    return load_weather_records(data_path, crop=crop, country=country, metric=metric)


@st.cache_data(show_spinner=False)
def _load_config(config_path: str, config_mtime_ns: int) -> dict[str, object]:
    del config_mtime_ns
    return load_weather_config(config_path)


@st.cache_data(show_spinner=False)
def _load_snapshot_status(status_path: str, status_mtime_ns: int) -> dict[str, object]:
    del status_mtime_ns
    try:
        return json.loads(Path(status_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@st.cache_data(show_spinner=False)
def _load_normals(normal_path: str, normal_mtime_ns: int, normal_size: int) -> pd.DataFrame:
    """Load the static 30-year normal with a file-identity cache dependency."""

    del normal_mtime_ns, normal_size
    data = pd.read_parquet(normal_path)
    required = {"month_day", "country", "region", "metric", "normal_value", "unit", "baseline_label", "source_workbook_sha256", "source_sheet"}
    if missing := sorted(required - set(data.columns)):
        raise ValueError("30年历史基准缺少字段：" + "、".join(missing))
    return data


def _region_label(region: pd.Series | object, config: dict[str, object] | None = None) -> str:
    country_name = str((config or {}).get("country_display_name", "美国"))
    parent_label = getattr(region, "parent_label", None)
    if parent_label and not pd.isna(parent_label):
        return f"{parent_label} - {region.display_name}"
    return f"{country_name}_{region.display_name}（{float(region.weight):.1f}%）"


def _reference_date(value: object, season_start_month_day: str = "01-01") -> pd.Timestamp:
    """Map natural or cross-year seasons onto a fixed leap-year reference axis."""

    timestamp = pd.Timestamp(value)
    start_month, start_day = (int(part) for part in season_start_month_day.split("-"))
    year = REFERENCE_YEAR if (timestamp.month, timestamp.day) >= (start_month, start_day) else REFERENCE_YEAR + 1
    day = 28 if year % 4 and timestamp.month == 2 and timestamp.day == 29 else timestamp.day
    return pd.Timestamp(year=year, month=timestamp.month, day=day)


def _reference_window(start_month_day: str, end_month_day: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    end_year = REFERENCE_YEAR + 1 if end_month_day < start_month_day else REFERENCE_YEAR
    return (
        pd.Timestamp(f"{REFERENCE_YEAR}-{start_month_day}"),
        pd.Timestamp(f"{end_year}-{end_month_day}"),
    )


def _daily_rain_default_window(records: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """Return the approved 28-calendar-day daily-rain view around the last observation."""

    latest = latest_observation_date(records)
    if latest is None:
        return None
    latest = latest.normalize()
    return latest - pd.Timedelta(days=13), latest + pd.Timedelta(days=14)


def _week_start(value: pd.Timestamp) -> pd.Timestamp:
    return value.normalize() - pd.Timedelta(days=value.weekday())


def _weekly_windows(
    latest: pd.Timestamp,
    forecast_start: pd.Timestamp | None = None,
) -> tuple[list[tuple[pd.Timestamp, pd.Timestamp]], list[tuple[pd.Timestamp, pd.Timestamp]]]:
    """Use the legacy Monday-to-Sunday weekly windows without filling missing data."""

    latest_complete_end = _week_start(latest) - pd.Timedelta(days=1)
    history = [
        (latest_complete_end - pd.Timedelta(days=7 * offset + 6), latest_complete_end - pd.Timedelta(days=7 * offset))
        for offset in range(3, -1, -1)
    ]
    first_forecast_start = forecast_start.normalize() if forecast_start is not None else latest_complete_end + pd.Timedelta(days=1)
    forecast = [
        (first_forecast_start + pd.Timedelta(days=7 * offset), first_forecast_start + pd.Timedelta(days=7 * offset + 6))
        for offset in range(2)
    ]
    return history, forecast


def _format_value(value: object, *, percent: bool = False, signed: bool = False) -> str:
    if value is None or pd.isna(value):
        return "—"
    if percent:
        return f"{float(value):+.1f}%"
    return f"{float(value):+.1f}" if signed else f"{float(value):.1f}"


def _weekly_row(
    records: pd.DataFrame,
    config: dict[str, object],
    *,
    metric: str,
    window: tuple[pd.Timestamp, pd.Timestamp],
    data_type: str,
    model: str | None = None,
) -> pd.DataFrame:
    return weekly_metric_summary(
        records,
        config,
        metric=metric,
        start=window[0],
        end=window[1],
        data_type=data_type,
        model=model,
    ).set_index("region")


def _value_cell(value: object, css_class: str = "", *, percent: bool = False, signed: bool = False) -> str:
    return f'<td colspan="2" class="{css_class}">{_format_value(value, percent=percent, signed=signed)}</td>'


def _forecast_value_cells(ec: object, gfs: object, ec_css: str = "", gfs_css: str = "", *, percent: bool = False, signed: bool = False) -> str:
    return (
        f'<td class="forecast-ec {ec_css}"><strong>{_format_value(ec, percent=percent, signed=signed)}</strong></td>'
        f'<td class="forecast-gfs {gfs_css}"><strong>{_format_value(gfs, percent=percent, signed=signed)}</strong></td>'
    )


def _normal_summary(normals: pd.DataFrame, config: dict[str, object], *, metric: str, window: tuple[pd.Timestamp, pd.Timestamp]) -> pd.DataFrame:
    month_days = pd.date_range(window[0], window[1], freq="D").strftime("%m-%d").tolist()
    values = normals[(normals["metric"] == metric) & (normals["month_day"].isin(month_days))].copy()
    expected = len(config["regions"]) * len(month_days)
    if len(values) != expected or values.duplicated(["month_day", "region"], keep=False).any():
        raise ValueError("30年历史基准无法覆盖当前7日窗口")
    aggregation = "sum" if metric == "precipitation" else "mean"
    grouped = values.groupby("region", as_index=True)["normal_value"].agg(aggregation)
    weighted, coverage = weighted_values(grouped, config)
    region_frame = region_weights(config).set_index("key")
    result = region_frame.copy()
    result["value"] = grouped.reindex(result.index)
    result = result.reset_index().rename(columns={"key": "region"})
    result = pd.concat([pd.DataFrame([{"region": "weighted", "display_name": "主产区加权", "weight": float(config["weighted_coverage_percent"]), "display_order": 0, "value": weighted, "coverage_percent": coverage}]), result.assign(coverage_percent=100.0)], ignore_index=True)
    return result.set_index("region")


def _summary_value(summary: pd.DataFrame, region: str) -> float | None:
    if region not in summary.index or pd.isna(summary.loc[region, "value"]):
        return None
    return float(summary.loc[region, "value"])


def _anomaly(value: float | None, normal: float | None, *, percent: bool) -> float | None:
    if value is None or normal is None:
        return None
    if percent:
        return _anomaly_pct(value, normal)
    return value - normal


def _anomaly_ratio(value: float | None, normal: float | None) -> float | None:
    """Return an unformatted ratio, e.g. 0.605 for a 60.5% anomaly."""

    if value is None or normal is None or normal == 0:
        return None
    return (value - normal) / normal


def _anomaly_pct(value: float | None, normal: float | None) -> float | None:
    """Return percentage points exactly once: 60.5, never 0.605 or 6050."""

    ratio = _anomaly_ratio(value, normal)
    return None if ratio is None else ratio * 100


def _anomaly_class(anomaly_pct: float | None) -> str:
    if anomaly_pct is None or pd.isna(anomaly_pct):
        return "anomaly-unavailable"
    if anomaly_pct <= -50:
        return "anomaly-deep-negative"
    if anomaly_pct <= -20:
        return "anomaly-medium-negative"
    if anomaly_pct < 0:
        return "anomaly-light-negative"
    if anomaly_pct < 20:
        return "anomaly-light-positive"
    if anomaly_pct < 50:
        return "anomaly-medium-positive"
    return "anomaly-deep-positive"


def _wide_row(
    window: tuple[pd.Timestamp, pd.Timestamp],
    regions: pd.DataFrame,
    current: dict[str, pd.DataFrame],
    normal: pd.DataFrame,
    *,
    forecast: bool,
    measure: str,
    metric: str,
) -> str:
    start, end = window
    cells = [f"<td>{start:%Y-%m-%d}</td>", "<td>—</td>", f"<td>{end:%Y-%m-%d}</td>"]
    for key in ["weighted", *regions["key"].tolist()]:
        def converted(model: str) -> tuple[float | None, float | None]:
            value = _summary_value(current[model], key)
            baseline = _summary_value(normal, key)
            if measure == "value":
                return value, None
            return _anomaly(value, baseline, percent=measure == "percent"), _anomaly_pct(value, baseline)
        if forecast:
            (ec, ec_pct), (gfs, gfs_pct) = converted("ECMWF"), converted("GFS")
            css_ec = _anomaly_class(ec_pct) if measure != "value" else ""
            css_gfs = _anomaly_class(gfs_pct) if measure != "value" else ""
            cells.append(_forecast_value_cells(ec, gfs, css_ec, css_gfs, percent=measure == "percent", signed=measure == "absolute"))
        else:
            value, anomaly_pct = converted("observed")
            cells.append(_value_cell(value, _anomaly_class(anomaly_pct) if measure != "value" else "", percent=measure == "percent", signed=measure == "absolute"))
    return f'<tr class="{"row-forecast" if forecast else ""}">{"".join(cells)}</tr>'


def _build_weekly_wide_table(
    records: pd.DataFrame,
    normals: pd.DataFrame,
    config: dict[str, object],
    *,
    metric: str,
    latest: pd.Timestamp,
    snapshot_date: str,
    show_all_regions: bool,
) -> str:
    """Build a fresh Streamlit table matching the old page's wide-table grammar."""

    all_regions = region_weights(config)
    regions = all_regions if show_all_regions else all_regions.head(9)
    country_name = str(config.get("country_display_name", ""))
    crop_name = str(config.get("crop_display_name", "大豆"))
    subject = str(config.get("summary_subject", f"{country_name}{crop_name}"))
    title = f"{subject}主产区" + ("降水" if metric == "precipitation" else "最高气温")
    metric_label = "周度降雨（mm）" if metric == "precipitation" else "周度最高气温（℃）"
    colspan = 3 + 2 * (1 + len(regions))
    forecasts = select_latest_forecasts(records, latest)
    forecast_start = forecasts["date"].min() if not forecasts.empty else None
    history_windows, forecast_windows = _weekly_windows(latest, forecast_start)
    coverage = float(config["weighted_coverage_percent"])

    weighted_header = str(config.get("weighted_header_label", f"主产区 / {coverage:.1f}%"))
    rows = [
        f'<tr><td colspan="{colspan}" class="table-title">{title}</td></tr>',
        f'<tr><td colspan="{colspan}" class="snapshot-date">业务日期：周一 ｜ — ｜ 周日</td></tr>',
        f'<tr class="weight-row"><td colspan="3">权重</td><td colspan="2">{weighted_header}</td>'
        + "".join(f'<td colspan="2">{region.weight:.1f}%</td>' for region in regions.itertuples(index=False))
        + "</tr>",
        '<tr class="section-header"><td colspan="3">' + metric_label + "</td>"
        + '<td colspan="2">主产区加权</td>'
        + "".join(f'<td colspan="2">{html.escape(str(region.display_name))}</td>' for region in regions.itertuples(index=False))
        + "</tr>",
    ]
    for window in history_windows:
        normal = _normal_summary(normals, config, metric=metric, window=window)
        observed = _weekly_row(records, config, metric=metric, window=window, data_type="observed")
        rows.append(_wide_row(window, regions, {"observed": observed}, normal, forecast=False, measure="value", metric=metric))
    rows.append('<tr class="forecast-header"><td colspan="3">预测：未来2周</td>' + "".join('<td class="forecast-sub">EC</td><td class="forecast-sub">GFS</td>' for _ in range(1 + len(regions))) + "</tr>")
    for window in forecast_windows:
        normal = _normal_summary(normals, config, metric=metric, window=window)
        forecasts = {"ECMWF": _weekly_row(records, config, metric=metric, window=window, data_type="forecast", model="ECMWF"), "GFS": _weekly_row(records, config, metric=metric, window=window, data_type="forecast", model="GFS")}
        rows.append(_wide_row(window, regions, forecasts, normal, forecast=True, measure="value", metric=metric))
    for section, measure, forecast_heading in (
        ("周度偏差（mm）" if metric == "precipitation" else "周度偏差（℃）", "absolute", "预测偏差：未来2周"),
        ("周度偏差幅度（%）", "percent", "预测偏差幅度：未来2周"),
    ):
        rows.append(f'<tr class="section-header"><td colspan="3">{section}</td><td colspan="{colspan - 3}">沿用旧项目30年历史同期基准</td></tr>')
        for window in history_windows:
            normal = _normal_summary(normals, config, metric=metric, window=window)
            observed = _weekly_row(records, config, metric=metric, window=window, data_type="observed")
            rows.append(_wide_row(window, regions, {"observed": observed}, normal, forecast=False, measure=measure, metric=metric))
        rows.append(f'<tr class="forecast-header"><td colspan="3">{forecast_heading}</td>' + "".join('<td class="forecast-sub">EC</td><td class="forecast-sub">GFS</td>' for _ in range(1 + len(regions))) + "</tr>")
        for window in forecast_windows:
            normal = _normal_summary(normals, config, metric=metric, window=window)
            forecasts = {"ECMWF": _weekly_row(records, config, metric=metric, window=window, data_type="forecast", model="ECMWF"), "GFS": _weekly_row(records, config, metric=metric, window=window, data_type="forecast", model="GFS")}
            rows.append(_wide_row(window, regions, forecasts, normal, forecast=True, measure=measure, metric=metric))
    rows.append(f'<tr><td colspan="{colspan}" class="table-note">主产区加权固定使用全部{len(all_regions)}个展示地区：sum(region_value × region_weight) / {coverage:.1f}。偏差按当前值减30年基准，偏差幅度按当前值/30年基准−1计算。</td></tr>')
    return '<div class="weather-table-scroll"><table class="weather-wide-table">' + "".join(rows) + "</table></div>"


def _inject_weather_styles() -> None:
    st.markdown(
        """
        <style>
        .weather-section-title {color:#0f3d68;font-size:1.45rem;font-weight:800;margin:0.65rem 0 0.35rem;}
        .weather-table-scroll {overflow-x:auto;border:1px solid #d7dee7;margin:0.35rem 0 0.7rem;}
        table.weather-wide-table {border-collapse:collapse;width:100%;min-width:1460px;font-size:12px;color:#1f2937;}
        .weather-wide-table td {border:1px solid #d7dee7;padding:4px 6px;text-align:center;vertical-align:middle;line-height:1.2;white-space:nowrap;}
        .weather-wide-table .table-title {background:#0f3d68;color:#fff;font-size:16px;font-weight:800;padding:8px;text-align:left;}
        .weather-wide-table .snapshot-date {background:#eaf3fb;color:#36536f;text-align:center;font-weight:700;}
        .weather-wide-table .weight-row td,.weather-wide-table .section-header td {background:#dcecf9;color:#0b365a;font-weight:900;}
        .weather-wide-table .forecast-header td,.weather-wide-table .forecast-sub {background:#fff2cc;color:#795a13;font-weight:800;}
        .weather-wide-table .row-forecast td {background:#fff9e7;}
        .weather-wide-table .forecast-ec,.weather-wide-table .forecast-gfs {font-size:12px;color:#1f2937;}
        .weather-wide-table .forecast-ec strong,.weather-wide-table .forecast-gfs strong {font-size:12px;color:#1f2937;}
        .weather-wide-table .anomaly-deep-negative {background:#C62828;color:#fff;font-weight:800;}
        .weather-wide-table .anomaly-medium-negative {background:#EF9A9A;color:#8b1f1f;font-weight:700;}
        .weather-wide-table .anomaly-light-negative {background:#FFEBEE;color:#9f1d1d;}
        .weather-wide-table .anomaly-light-positive {background:#E8F5E9;color:#17613b;}
        .weather-wide-table .anomaly-medium-positive {background:#A5D6A7;color:#17613b;font-weight:700;}
        .weather-wide-table .anomaly-deep-positive {background:#2E7D32;color:#fff;font-weight:800;}
        .weather-wide-table .anomaly-unavailable {background:#f1f3f5;color:#7a8490;}
        .weather-wide-table .anomaly-deep-negative strong,.weather-wide-table .anomaly-deep-positive strong {color:#fff;}
        .weather-wide-table .anomaly-medium-negative strong {color:#8b1f1f;}
        .weather-wide-table .anomaly-light-negative strong {color:#9f1d1d;}
        .weather-wide-table .anomaly-light-positive strong,.weather-wide-table .anomaly-medium-positive strong {color:#17613b;}
        .weather-wide-table .anomaly-unavailable strong {color:#7a8490;}
        .weather-wide-table .table-note {background:#f7f9fb;color:#596579;text-align:left;white-space:normal;font-size:11px;}
        div[class*="st-key-daily-rain-card-"] {background:#fff;border:1px solid #dce3eb;border-radius:8px;padding:0.15rem 0.35rem 0;margin-bottom:0.15rem;box-shadow:none;}
        div[class*="st-key-daily-rain-card-selected-"] {border-color:#efb4ad !important;box-shadow:0 0 0 1px rgba(239,180,173,.18);}
        div[class*="st-key-history-card-"] {background:#fff;border:1px solid #dce3eb;border-radius:8px;padding:0.28rem 0.55rem 0.10rem;margin-bottom:0.22rem;box-shadow:none;}
        .history-region-name {color:#172033;font-size:16px;font-weight:800;line-height:1.2;margin:0 0 0.06rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _section_heading(title: str) -> None:
    st.markdown(f'<div class="weather-section-title">{html.escape(title)}</div>', unsafe_allow_html=True)


def _aligned_history(
    records: pd.DataFrame,
    region_key: str,
    start_month_day: str,
    end_month_day: str,
    config: dict[str, object],
) -> tuple[pd.DataFrame, list[object], object | None]:
    observed = records[(records["data_type"] == "observed") & (records["region"] == region_key)].copy()
    if observed.empty:
        return pd.DataFrame(), [], None
    if str(config.get("season_label_style", "year")) != "range":
        observed["year"] = observed["date"].dt.year
        observed["month_day"] = observed["date"].dt.strftime("%m-%d")
        observed = observed[(observed["month_day"] >= start_month_day) & (observed["month_day"] <= end_month_day)]
        observed["aligned_date"] = observed["date"].map(_reference_date)
        years = sorted(observed["year"].unique().tolist())[-12:]
        current_year = max(years) if years else None
        return observed, years, current_year
    observed = add_season_columns(observed, config)
    # The 365-day static normal has no Feb 29.  Keep seasonal comparisons
    # deterministic by excluding this unmatched leap-day observation.
    observed = observed[~((observed["date"].dt.month == 2) & (observed["date"].dt.day == 29))]
    observed["aligned_date"] = observed["date"].map(lambda value: _reference_date(value, start_month_day))
    axis_start, axis_end = _reference_window(start_month_day, end_month_day)
    observed = observed[(observed["aligned_date"] >= axis_start) & (observed["aligned_date"] <= axis_end)]
    observed["year"] = observed["season"]
    years = sorted(observed["year"].unique().tolist())[-12:]
    return observed, years, years[-1] if years else None


def _history_card_heading(region: pd.Series | object) -> str:
    parent_label = getattr(region, "parent_label", None)
    if parent_label and not pd.isna(parent_label):
        return (
            '<div class="history-region-name">'
            f"{html.escape(str(parent_label))} - {html.escape(str(region.display_name))}"
            "</div>"
        )
    return (
        '<div class="history-region-name">'
        f"{html.escape(str(region.display_name))}（{float(region.weight):.1f}%）"
        "</div>"
    )


def _line_layout(unit: str, start_month_day: str, end_month_day: str, height: int = 350) -> go.Figure:
    figure = go.Figure()
    figure.update_layout(
        title={"text": ""},
        height=height,
        hovermode="x unified",
        plot_bgcolor="#ffffff",
        paper_bgcolor="#ffffff",
        margin={"l": 45, "r": 16, "t": 6, "b": 56},
        legend={"orientation": "h", "y": -0.15, "x": 0.5, "xanchor": "center", "font": {"size": 10, "color": "#475569"}, "traceorder": "normal"},
    )
    axis_start, axis_end = _reference_window(start_month_day, end_month_day)
    figure.update_xaxes(
        type="date",
        range=[axis_start, axis_end],
        tickformat="%m-%d",
        hoverformat="%m-%d",
        tick0=axis_start,
        dtick=20 * 24 * 60 * 60 * 1000,
        gridcolor="#edf3f7",
        griddash="dot",
        tickangle=-45,
    )
    figure.update_yaxes(title=unit, gridcolor="#d7e1ea", griddash="dot", gridwidth=0.9)
    return figure


def _add_line(
    figure: go.Figure,
    data: pd.DataFrame,
    *,
    name: str,
    color: str,
    width: float,
    dash: str = "solid",
    opacity: float = 1.0,
    legendrank: int = 1000,
    hover: bool = True,
) -> None:
    if data.empty:
        return
    figure.add_trace(
        go.Scatter(
            x=data["aligned_date"],
            y=data["value"],
            name=name,
            mode="lines",
            line={"color": color, "width": width, "dash": dash},
            opacity=opacity,
            connectgaps=False,
            legendrank=legendrank,
            hoverinfo=None if hover else "skip",
            hovertemplate="%{y:.1f}<extra>%{fullData.name}</extra>" if hover else None,
        )
    )


def _region_line_figure(records: pd.DataFrame, config: dict[str, object], region: object, *, kind: str) -> go.Figure:
    legacy_windows = {
        "cumulative_rain": ("04-15", "11-20", "累计降水（mm）"),
        "soil": ("03-01", "12-26", "原始值，单位待确认"),
        "temperature": ("03-01", "11-20", "最高气温（℃）"),
    }
    config_window = dict(config.get("chart_windows", {})).get(kind)
    if isinstance(config_window, dict):
        start_month_day, end_month_day = str(config_window["start"]), str(config_window["end"])
        unit = legacy_windows[kind][2]
    else:
        start_month_day, end_month_day, unit = legacy_windows[kind]
    observed, years, current_year = _aligned_history(records, region.key, start_month_day, end_month_day, config)
    figure = _line_layout(unit, start_month_day, end_month_day)
    if current_year is None:
        return figure
    history_by_year: dict[object, pd.DataFrame] = {}
    for year in years:
        series = observed[observed["year"] == year][["aligned_date", "value"]].sort_values("aligned_date").copy()
        if kind == "cumulative_rain":
            series["value"] = series["value"].cumsum()
        history_by_year[year] = series
    previous_year = years[-2] if len(years) > 1 else None
    mean_years = [year for year in years if year not in {current_year, previous_year}][-5:]
    for history_rank, year in enumerate(years):
        if year in {current_year, previous_year}:
            continue
        _add_line(
            figure,
            history_by_year[year],
            name=str(year),
            color=HISTORY_SERIES_PALETTE[history_rank % len(HISTORY_SERIES_PALETTE)],
            width=1.1,
            opacity=0.58,
            legendrank=history_rank,
            hover=False,
        )
    if mean_years:
        mean = pd.concat([history_by_year[year].assign(year=year) for year in mean_years]).groupby("aligned_date", as_index=False)["value"].mean().sort_values("aligned_date")
        _add_line(figure, mean, name="5年均值", color=HISTORY_SERIES_COLORS["five_year_mean"], width=2.5, dash="dash", legendrank=100)
    if previous_year is not None:
        _add_line(figure, history_by_year[previous_year], name=str(previous_year), color=HISTORY_SERIES_COLORS["previous"], width=2.8, legendrank=110)
    if kind != "soil":
        latest = latest_observation_date(records)
        forecast = select_latest_forecasts(records, latest) if latest is not None else pd.DataFrame()
        for model, label, color, legendrank in (
            ("GFS", "GFS预测", HISTORY_SERIES_COLORS["gfs"], 130),
            ("ECMWF", "EC预测", HISTORY_SERIES_COLORS["ec"], 140),
        ):
            model_data = forecast[(forecast["model"] == model) & (forecast["region"] == region.key)].copy()
            if not model_data.empty:
                model_data["aligned_date"] = model_data["date"].map(lambda value: _reference_date(value, start_month_day))
                axis_start, axis_end = _reference_window(start_month_day, end_month_day)
                model_data = model_data[(model_data["aligned_date"] >= axis_start) & (model_data["aligned_date"] <= axis_end)].sort_values("aligned_date")
            if kind == "cumulative_rain" and not model_data.empty:
                anchor = float(history_by_year[current_year]["value"].iloc[-1]) if not history_by_year[current_year].empty else 0.0
                model_data["value"] = anchor + model_data["value"].cumsum()
                anchor_row = pd.DataFrame({"aligned_date": [history_by_year[current_year]["aligned_date"].iloc[-1]], "value": [anchor]})
                model_data = pd.concat([anchor_row, model_data[["aligned_date", "value"]]], ignore_index=True)
            _add_line(figure, model_data, name=label, color=color, width=2.3, dash="dash", legendrank=legendrank)
    _add_line(
        figure,
        history_by_year[current_year],
        name=str(current_year),
        color=HISTORY_SERIES_COLORS["current"],
        width=3.5,
        legendrank=120,
    )
    return figure


def _daily_rain_figure(
    records: pd.DataFrame,
    region: object,
    date_range: tuple[pd.Timestamp, pd.Timestamp] | None = None,
    config: dict[str, object] | None = None,
) -> go.Figure:
    latest = latest_observation_date(records)
    observed = records[(records["data_type"] == "observed") & (records["region"] == region.key)].copy()
    if latest is not None:
        observed = observed[observed["date"] <= latest]
    forecasts = select_latest_forecasts(records, latest) if latest is not None else pd.DataFrame()
    active_range = date_range or _daily_rain_default_window(records)
    if active_range is not None:
        start, end = active_range
        observed = observed[(observed["date"] >= start) & (observed["date"] <= end)]
        forecasts = forecasts[(forecasts["date"] >= start) & (forecasts["date"] <= end)]
    season_start = str((config or {}).get("season_start_month_day", "01-01"))
    observed["aligned_date"] = observed["date"].map(lambda value: _reference_date(value, season_start))
    forecast_aligned = forecasts.copy()
    if not forecast_aligned.empty:
        forecast_aligned["aligned_date"] = forecast_aligned["date"].map(lambda value: _reference_date(value, season_start))
    figure = go.Figure()
    figure.add_trace(go.Bar(x=observed["aligned_date"], y=observed["value"], name="历史降雨", marker_color=OLD_PAGE_COLORS["historical_rain"], width=DAILY_BAR_WIDTH_MS))
    for model, label, color in (("ECMWF", "EC预测", OLD_PAGE_COLORS["ec"]), ("GFS", "GFS预测", OLD_PAGE_COLORS["five_year_mean"])):
        data = forecasts[(forecasts["model"] == model) & (forecasts["region"] == region.key)].sort_values("date")
        if not data.empty:
            data = data.copy()
            data["aligned_date"] = data["date"].map(lambda value: _reference_date(value, season_start))
        figure.add_trace(go.Bar(x=data["aligned_date"], y=data["value"], name=label, marker_color=color, width=DAILY_BAR_WIDTH_MS))
    available = pd.concat([observed["aligned_date"], forecast_aligned.get("aligned_date", pd.Series(dtype="datetime64[ns]"))], ignore_index=True).dropna()
    axis_start = _reference_date(active_range[0], season_start) if active_range is not None else (available.min() if not available.empty else None)
    axis_end = _reference_date(active_range[1], season_start) if active_range is not None else (available.max() if not available.empty else None)
    figure.update_layout(
        title={"text": _region_label(region), "x": 0.5, "y": 0.99, "xanchor": "center", "yanchor": "top", "font": {"color": "#0f172a", "size": 14, "family": "Inter, Helvetica Neue, Arial", "weight": "bold"}},
        barmode="group",
        bargap=0.18,
        bargroupgap=0.03,
        height=400,
        margin={"l": 42, "r": 12, "t": 52, "b": 62},
        plot_bgcolor="#ffffff",
        paper_bgcolor="#ffffff",
        hovermode="closest",
        legend={"orientation": "h", "y": 0.93, "yanchor": "top", "x": 0.5, "xanchor": "center", "traceorder": "normal", "font": {"size": 10}},
    )
    figure.update_xaxes(
        type="date",
        range=[axis_start, axis_end] if axis_start is not None and axis_end is not None else None,
        tickmode="linear",
        tick0=axis_start,
        dtick=24 * 60 * 60 * 1000,
        tickformat="%m-%d",
        tickangle=-55,
        showgrid=True,
        gridcolor="#dbe7f1",
        griddash="dot",
        gridwidth=0.8,
        rangeslider={"visible": True, "thickness": 0.05, "bgcolor": "#fcfdfe", "bordercolor": "#edf1f5", "borderwidth": 0},
    )
    figure.update_yaxes(title="降水（mm）", showgrid=True, gridcolor="#dbe7f1", griddash="dot", gridwidth=0.8, zeroline=False)
    return figure


def _render_grid(records: pd.DataFrame, config: dict[str, object], *, kind: str, columns: int) -> None:
    regions = region_weights(config)
    selected_range: tuple[pd.Timestamp, pd.Timestamp] | None = None
    if kind == "daily_rain":
        latest = latest_observation_date(records)
        default_window = _daily_rain_default_window(records)
        observed = records[records["data_type"] == "observed"].sort_values("date")
        if latest is not None:
            observed = observed[observed["date"] <= latest]
        forecasts = select_latest_forecasts(records, latest) if latest is not None else pd.DataFrame()
        available_dates = pd.concat([observed["date"], forecasts["date"]], ignore_index=True).dropna()
        if default_window is not None:
            st.caption("默认展示最后观测日前14天及后14天模型预测。")
        if not available_dates.empty and default_window is not None:
            minimum = min(available_dates.min().normalize(), default_window[0]).date()
            maximum = max(available_dates.max().normalize(), default_window[1]).date()
            selected = st.date_input(
                "单日降雨日期范围",
                value=(default_window[0].date(), default_window[1].date()),
                min_value=minimum,
                max_value=maximum,
                key=f"soybean_daily_rain_date_range_{config['country']}",
            )
            if isinstance(selected, tuple) and len(selected) == 2:
                selected_range = (pd.Timestamp(selected[0]), pd.Timestamp(selected[1]))
    for start in range(0, len(regions), columns):
        grid = st.columns(columns, gap="small")
        for index, (column, region) in enumerate(zip(grid, regions.iloc[start:start + columns].itertuples(index=False), strict=False)):
            with column:
                card_key = (
                    f"daily-rain-card-{'selected-' if kind == 'daily_rain' and start == 0 and index == 0 else ''}{region.key}"
                    if kind == "daily_rain"
                    else f"history-card-{region.key}"
                )
                with st.container(border=True, key=card_key):
                    if kind in {"cumulative_rain", "temperature", "soil"}:
                        st.markdown(_history_card_heading(region), unsafe_allow_html=True)
                    figure = _daily_rain_figure(records, region, selected_range, config) if kind == "daily_rain" else _region_line_figure(records, config, region, kind=kind)
                    st.plotly_chart(
                        figure,
                        use_container_width=True,
                        config={
                            "displaylogo": False,
                            "displayModeBar": False if kind == "daily_rain" else "hover",
                            "modeBarButtonsToRemove": [
                                "pan2d", "select2d", "lasso2d", "zoomIn2d", "zoomOut2d", "autoScale2d",
                                "hoverClosestCartesian", "hoverCompareCartesian", "toggleSpikelines",
                            ],
                        },
                    )


def _render_weekly(records: pd.DataFrame, normals: pd.DataFrame, config: dict[str, object], *, metric: str, snapshot_date: str) -> None:
    latest = latest_observation_date(records)
    if latest is None:
        st.warning("尚无可用于周度分析的观测数据。")
        return
    st.caption("沿用旧项目30年历史同期基准，具体起止年份未确认。")
    region_count = len(config["regions"])
    coverage = float(config["weighted_coverage_percent"])
    initial_count = min(int(config.get("default_summary_regions", 9)), region_count)
    expand_label = str(config.get("summary_expand_label", f"展开全部{region_count}个地区"))
    show_all = st.checkbox(expand_label, value=region_count <= initial_count, key=f"weather_wide_table_{config['country']}_{metric}")
    st.caption(f"默认展示前{initial_count}个地区；主产区加权始终使用全部{region_count}个展示地区，覆盖权重{coverage:.1f}%。")
    st.markdown(_build_weekly_wide_table(records, normals, config, metric=metric, latest=latest, snapshot_date=snapshot_date, show_all_regions=show_all), unsafe_allow_html=True)


def render_weather_page(country: str = "USA") -> None:
    """Render a country page solely from its local country configuration."""

    try:
        files = _country_files(country)
    except ValueError as exc:
        st.error(str(exc))
        return
    config_file = files["config"]
    if not config_file.is_file():
        st.error(f"缺少天气页面配置：{config_file}")
        return
    config = _load_config(str(config_file), config_file.stat().st_mtime_ns)
    data_path, source_label = _data_path(files)
    st.title(str(config.get("page_title", PAGE_TITLE)))
    if data_path is None or not data_path.is_file():
        st.error(
            f"{config.get('country_display_name', country)}天气稳定数据不可用："
            f"{source_label}。当前模块未加载任何回退业务数据。"
        )
        return
    status_file = files["status"]
    assert isinstance(status_file, Path)
    snapshot_status = _load_snapshot_status(str(status_file), status_file.stat().st_mtime_ns) if status_file.is_file() else {}
    snapshot_date = str(snapshot_status.get("source_snapshot_date", "—"))
    enabled = dict(config.get("enabled_sections", {}))
    module_keys = [key for key in MODULES if bool(enabled.get(key, True))]
    if not module_keys:
        st.info("当前国家尚未配置可展示的天气研究模块。")
        return
    module_key = st.radio(
        "页面章节",
        module_keys,
        format_func=lambda key: MODULES[key][0],
        horizontal=True,
        key=f"weather_module_{config['country']}",
    )
    module_name, kind, metric = MODULES[module_key]
    try:
        records = _load_selected_records(str(data_path), data_path.stat().st_mtime_ns, str(config["crop"]), str(config["country"]), metric)
    except (OSError, ValueError, ImportError) as exc:
        st.error(f"天气数据读取失败：{exc}")
        return
    if records.empty:
        st.info(f"当前选择没有可用的{config['metrics'][metric]['display_name']}数据。")
        return
    observed_end = latest_observation_date(records)
    observed_label = f"{observed_end:%Y-%m-%d}" if observed_end is not None else "—"
    st.caption(
        f"数据来源：{source_label}（快照日期：{snapshot_date}；非实时数据）｜"
        f"观测截止：{observed_label}"
    )
    if bool(config.get("weighted_aggregation", True)):
        coverage_note = str(config.get("coverage_caption", f"覆盖{len(config['regions'])}个主要产区，权重{float(config['weighted_coverage_percent']):.1f}%"))
        st.caption(f"{coverage_note}｜EC 截止：{snapshot_status.get('ecmwf_forecast_end_date', '—')}｜GFS 截止：{snapshot_status.get('gfs_forecast_end_date', '—')}")
    else:
        st.caption(f"直接展示{len(config['regions'])}个稳定地区序列；父级产量标签仅用于说明，不参与地区级或全国加权。｜EC 截止：{snapshot_status.get('ecmwf_forecast_end_date', '—')}｜GFS 截止：{snapshot_status.get('gfs_forecast_end_date', '—')}")
    _inject_weather_styles()
    _section_heading(module_name)
    if kind == "weekly":
        normal_path = _normal_path(files)
        if normal_path is None or not normal_path.is_file():
            st.error("30年历史同期基准稳定数据不可用。")
            return
        try:
            normal_stat = normal_path.stat()
            normals = _load_normals(str(normal_path), normal_stat.st_mtime_ns, normal_stat.st_size)
        except (OSError, ValueError, ImportError) as exc:
            st.error(f"30年历史同期基准读取失败：{exc}")
            return
        _render_weekly(records, normals, config, metric=metric, snapshot_date=snapshot_date)
    elif kind == "daily_rain":
        _render_grid(records, config, kind=kind, columns=3)
    elif kind == "cumulative_rain":
        _render_grid(records, config, kind=kind, columns=3)
    elif kind == "temperature":
        _render_grid(records, config, kind=kind, columns=2)
    else:
        st.caption("土壤墒情单位：原始值，单位待确认；本模块不展示预测曲线。")
        _render_grid(records, config, kind=kind, columns=3)


def render_soybean_weather_page(country: str = "USA") -> None:
    """Compatibility entry point retained for the existing soybean page route."""

    render_weather_page(country)
