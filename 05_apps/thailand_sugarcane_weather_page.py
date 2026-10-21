"""Native workbench page using the palm-weather chart style and isolated snapshots."""
from __future__ import annotations
import json
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from agri_research_agent.weather.thailand_sugarcane import load_config, load_snapshot
from crop_weather_page import (
    HISTORY_SERIES_COLORS, HISTORY_SERIES_PALETTE, _add_line,
    _inject_weather_styles, _line_layout, _reference_date,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "02_configs/thailand_sugarcane_weather.json"
SECTIONS = {"累计降雨": "precipitation", "单日降雨": "precipitation",
            "最高气温": "temperature_max", "土壤墒情": "soil_moisture"}


@st.cache_data(show_spinner=False, max_entries=4)
def _read(root: str, pointer: str, config_path: str, config_sha256: str):
    del pointer
    config = load_config(Path(config_path))
    if config["config_sha256"] != config_sha256:
        raise ValueError("读取期间产区配置发生变化")
    return load_snapshot(Path(root), config)


def seasonal_figure(records: pd.DataFrame, region: str, metric: str, *, cumulative: bool,
                    current_year: int, baseline=(2020, 2024)) -> go.Figure:
    unit = {"precipitation": "累计降雨（mm）", "temperature_max": "最高气温（℃）", "soil_moisture": "0—100厘米土壤含水率（%）"}[metric]
    figure = _line_layout(unit, "01-01", "12-31")
    part = records[(records.region == region) & (records.metric == metric)]
    history = part[part.model.eq("HISTORY")].copy()
    series_by_year = {}
    for year, subset in history.groupby(history.date.dt.year):
        # Calendar slots remain present. Missing rain breaks every subsequent annual total.
        series = subset.sort_values("date")[["date", "value"]].copy()
        if cumulative:
            series["value"] = series.value.cumsum(skipna=False)
        series = series[~((series.date.dt.month == 2) & (series.date.dt.day == 29))]
        series["aligned_date"] = series.date.map(_reference_date)
        series_by_year[int(year)] = series
        if year < current_year - 1:
            _add_line(figure, series, name=str(year), color=HISTORY_SERIES_PALETTE[(year-2020) % len(HISTORY_SERIES_PALETTE)], width=1.1, opacity=.55, legendrank=int(year)-2020)
    baseline_frames = [s.assign(year=y) for y, s in series_by_year.items() if baseline[0] <= y <= baseline[1]]
    if len(baseline_frames) == baseline[1] - baseline[0] + 1:
        grouped = pd.concat(baseline_frames).groupby("aligned_date").value
        normal = grouped.mean().where(grouped.count().eq(5)).rename("value").reset_index()
        _add_line(figure, normal, name="2020—2024年均值", color=HISTORY_SERIES_COLORS["five_year_mean"], width=2.5, dash="dash", legendrank=100)
    for year, role in ((current_year-1, "previous"), (current_year, "current")):
        if year in series_by_year:
            _add_line(figure, series_by_year[year], name=str(year), color=HISTORY_SERIES_COLORS[role], width=3 if role == "current" else 2.5, legendrank=120 if role == "current" else 110)
    if metric != "soil_moisture":
        for model, label, role, rank in (("ECMWF", "EC预测", "ec", 140), ("GFS", "GFS预测", "gfs", 130)):
            data = part[part.model.eq(model)].sort_values("date").copy()
            data = data[data.date.dt.year.eq(current_year)]
            data["aligned_date"] = data.date.map(_reference_date)
            if cumulative:
                current = series_by_year.get(current_year)
                if current is None or current.empty or pd.isna(current.value.iloc[-1]):
                    continue
                # Never bridge a historical or history-to-forecast gap.
                if data.empty or data.date.iloc[0] != current.date.iloc[-1] + pd.Timedelta(days=1):
                    continue
                data["value"] = current.value.iloc[-1] + data.value.cumsum(skipna=False)
                data = pd.concat([current.iloc[[-1]], data], ignore_index=True)
            _add_line(figure, data, name=label, color=HISTORY_SERIES_COLORS[role], width=2.3, dash="dash", legendrank=rank)
    return figure


def daily_figure(records: pd.DataFrame, region: str, start, end) -> go.Figure:
    figure = go.Figure()
    part = records[(records.region == region) & records.metric.eq("precipitation") & records.date.between(pd.Timestamp(start), pd.Timestamp(end))]
    for model, label, color in (("HISTORY", "IFS历史模式", "#3b6fd4"), ("ECMWF", "EC预测", "#ff8a22"), ("GFS", "GFS预测", "#15803d")):
        data = part[part.model.eq(model)].sort_values("date")
        figure.add_bar(x=data.date, y=data.value, name=label, marker_color=color)
    figure.update_layout(height=350, barmode="group", hovermode="x unified", plot_bgcolor="white", paper_bgcolor="white",
                         margin=dict(l=42,r=12,t=10,b=65), legend=dict(orientation="h", y=-.2))
    figure.update_xaxes(range=[str(start), str(end)], tickformat="%m-%d", tickangle=-45, gridcolor="#edf3f7")
    figure.update_yaxes(title="降雨（mm）", gridcolor="#edf3f7")
    return figure


def render_thailand_sugarcane_weather_page() -> None:
    st.title("泰国甘蔗天气研究")
    try:
        config = load_config(CONFIG)
    except (OSError, ValueError, KeyError) as exc:
        st.error(f"泰国产区配置不可用：{exc}")
        return
    regions = config["regions"]
    covered = sum(r["national_share"] for r in regions)
    st.caption(f"16个主要产蔗省 · 2025年工业甘蔗收获面积占全国{covered:.2%} · 泰国当地日界UTC+7")
    with st.expander("产区、面积占比与取样依据"):
        st.markdown(f"面积来源：[OAE《泰国农业统计2025》表1.17，第41—42页]({config['source_url']})；原始机构为OCSB。采用2025年已公布的**工业甘蔗收获面积**，全国 **9,628,085莱**，1莱=0.16公顷。2026列为预测，不参与权重。")
        st.caption("占比=该省2025年甘蔗收获面积÷47省全国合计；不按行政土地面积计算。占比用于产区重要性标签，本版不生成全国加权天气指数。")
        st.caption("每省选择一个有甘蔗种植依据的县级地区，坐标为GISTDA县界内的地理代表点。县内点不是蔗田中心，也不是全省蔗田面积积分；依据年份逐点保留。")
        table = pd.DataFrame([dict(省=r["display_name"],收获面积_莱=r["area_rai"],全国占比_pct=round(r["national_share"]*100,4),代表县=r["district"],纬度=r["latitude"],经度=r["longitude"],种植依据年份=r["locality_source_period"],种植依据=r["locality_source"]) for r in regions])
        st.dataframe(table, hide_index=True, width="stretch")
        st.download_button("下载47省完整面积表", pd.DataFrame(config["all_province_areas"]).to_csv(index=False).encode("utf-8-sig"), "thailand_cane_harvested_area_2025.csv", "text/csv")
    root = Path(os.environ.get("THAILAND_SUGARCANE_WEATHER_ROOT", str(ROOT / "01_data/processed/thailand_sugarcane_weather")))
    try:
        pointer = (root / "current.json").read_text(encoding="utf-8")
        records, meta = _read(str(root.resolve()), pointer, str(CONFIG), config["config_sha256"])
    except (OSError, ValueError, KeyError) as exc:
        st.error(f"泰国天气快照暂不可用：{exc}。产区表可查看；天气图表等待后台校验通过的数据。")
        return
    st.caption(f"历史模式截至 {meta['history_end']} · EC/GFS共同起报 {meta['forecast_run_at']} · 预报 {meta['forecast_start']}—{meta['forecast_end']}")
    st.caption(f"快照发布时间UTC：{meta['published_at_utc']} · 历史采用ECMWF IFS模式资料，非气象站实测、非ERA5再分析；固定比较基期2020—2024年。")
    if pd.Timestamp(meta["history_end"]).date() < (pd.Timestamp.now(tz="Asia/Bangkok") - pd.Timedelta(days=1)).date():
        st.warning("当前显示上一成功快照；历史截止日落后于昨日，请以页面日期判断时效。")
    if meta["null_history_values"]:
        st.warning(f"历史含{meta['null_history_values']}条缺测，保留为空；累计降雨遇缺测后停止接续，不补零。")
    attempt = root / "last_attempt.json"
    if attempt.is_file():
        try:
            status = json.loads(attempt.read_text(encoding="utf-8"))
            if status.get("status") == "FAILED":
                st.warning("最近一次后台更新失败，页面继续显示上次成功快照。")
        except (OSError, ValueError):
            st.warning("最近更新状态暂不可读取，请以快照截止日判断时效。")
    section = st.radio("页面章节", list(SECTIONS), horizontal=True, key="tha_weather_section")
    labels = {r["key"]:r["display_name"] for r in regions}
    selected = st.multiselect("显示产区", list(labels), default=list(labels), format_func=labels.get, key="tha_weather_regions")
    if not selected:
        st.info("请选择至少一个产区。")
        return
    metric = SECTIONS[section]
    start = pd.Timestamp(meta["history_end"]) - pd.Timedelta(days=13)
    end = pd.Timestamp(meta["forecast_end"])
    if section == "单日降雨":
        chosen = st.date_input("单日降雨日期范围", (start.date(), end.date()), min_value=pd.Timestamp(meta["history_start"]).date(), max_value=end.date(), key="tha_weather_daily_range")
        if len(chosen) != 2:
            st.info("请选择完整日期范围。")
            return
        start, end = chosen
    if section == "土壤墒情":
        st.caption("0—100厘米土层体积含水率，按7、21、72厘米厚度加权；单位%。本版仅展示历史模式资料。")
    _inject_weather_styles()
    current_year = pd.Timestamp(meta["history_end"]).year
    if pd.Timestamp(meta["forecast_end"]).year != current_year:
        st.caption("跨年预报完整值见单日降雨；年度曲线按自然年分别展示，不跨年累计。")
    visible = [r for r in regions if r["key"] in selected]
    columns = st.columns(2 if section == "最高气温" else 3)
    for index, region in enumerate(visible):
        with columns[index % len(columns)]:
            st.markdown(f"**{region['display_name']}（{region['national_share']:.2%}）**")
            if section == "单日降雨":
                figure = daily_figure(records, region["key"], start, end)
            else:
                figure = seasonal_figure(records, region["key"], metric, cumulative=section == "累计降雨", current_year=current_year)
            st.plotly_chart(figure, width="stretch", key=f"tha_{section}_{region['key']}", config={"displaylogo":False})
    with st.expander("当前章节数据"):
        data = records[records.region.isin(selected) & records.metric.eq(metric)]
        data = data.drop(columns=["data_type"]).rename(columns={"source_kind":"资料类型"})
        data["资料类型"] = data["资料类型"].map({"historical_model":"历史模式资料", "forecast":"模式预报"})
        st.dataframe(data.sort_values(["region", "date", "model"]).tail(1000), hide_index=True, width="stretch")
        st.download_button("下载完整章节数据", data.to_csv(index=False).encode("utf-8-sig"), f"thailand_{metric}.csv", "text/csv")
