"""Read-only Canadian canola progress and historical comparison page."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from agri_research_agent.shared.chart_style import difference_cell_style

from agri_research_agent.pipelines.canada_canola import (
    METRICS, PROVINCES, SOURCE_URLS, STAGES, STABLE_RELATIVE_PATH,
    compare_metric, comparison_table, load_bundle, observations_frame,
    seasonal_figure, sha256_file,
)

ROOT = Path(__file__).resolve().parents[1]
MATCHING_NOTE = "同期按目标月日及此前7天内最近观测匹配，不插值、不延续更早记录。均值仅使用所选年份之前五年；不足五年时显示实际样本数。"


def data_path() -> Path:
    root = Path(os.getenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", "").strip() or ROOT / "01_data")
    return root / STABLE_RELATIVE_PATH


@st.cache_data(show_spinner=False)
def read_data(path: str, identity: str) -> dict:
    bundle = load_bundle(Path(path))
    if sha256_file(Path(path)) != identity:
        raise ValueError("数据在读取期间发生变化，请刷新页面")
    return bundle


def _table(table: pd.DataFrame) -> None:
    compact = table[["指标", "数据日期", "最新（%）", "较上次（百分点）",
                     "去年同期（%）", "历史同期均值（%）", "较均值（百分点）"]].copy()
    for column in compact.columns[2:]:
        compact[column] = compact[column].map(lambda value: "—" if pd.isna(value) else f"{value:+.1f}" if "百分点" in column else f"{value:.1f}")
    compact["历史同期均值（%）"] = [f"{value}（{samples}）" if value != "—" else "—"
                                     for value, samples in zip(compact["历史同期均值（%）"], table["有效样本"])]
    compact.columns = ["指标", "日期", "最新", "较上次", "去年同期", "同期均值", "较均值"]
    st.dataframe(compact.style.map(difference_cell_style, subset=["较均值"]), hide_index=True, width="stretch",
                 column_config={name: st.column_config.TextColumn(width="small") for name in compact.columns})


def render_canada_canola_page() -> None:
    st.title("加拿大菜籽种植与生长")
    st.caption("查看三省最新观测与历史同期差异。55%、28%、16%用于省份标签展示。")
    path = data_path()
    if not path.is_file():
        st.info("加拿大历史数据尚未导入。完成历史数据导入后，这里会展示三省进度和同期曲线。")
        return
    try:
        bundle = read_data(str(path), sha256_file(path))
        frame = observations_frame(bundle)
    except (OSError, ValueError, KeyError) as exc:
        st.error(f"加拿大数据暂不可读：{exc}")
        return
    province = st.radio("省份", list(PROVINCES), format_func=PROVINCES.get, horizontal=True)
    years = sorted(set(frame["year"]) | {date.today().year}, reverse=True)
    year = st.selectbox("作物年份", years, index=years.index(date.today().year))
    st.subheader("最新进度与历史同期")
    _table(comparison_table(frame, province, year))
    for metric, name in METRICS.items():
        item = compare_metric(frame, province, metric, year)
        if not item["current"]:
            last = item["latest_historical"]
            suffix = f"；已有历史最新为 {last['date']:%Y-%m-%d}" if last else ""
            st.caption(f"{name}：{year}年暂无有效数值{suffix}。")
        else:
            current = item["current"]
            if current["status"] == "season_complete":
                st.caption(f"{name}：本季已结束。")
            if item["previous_days"] is not None and item["previous_days"] != 7:
                st.caption(f"{name}较上次变化对应间隔 {item['previous_days']} 天。")
    st.caption("数值单位为%；变化和差异为百分点。均值括号内为有效年份数/5；正负表示高低，不直接代表单产或价格方向。")

    st.subheader("历史进度对比")
    metric = st.radio("对比指标", list(METRICS), index=1, format_func=METRICS.get, horizontal=True)
    historical_years = sorted(set(frame.loc[(frame["province"] == province) & (frame["metric"] == metric), "year"])
                              - {year, year - 1}, reverse=True)
    extra = st.multiselect("其他历史年份（2022年起默认显示）", historical_years,
                           default=[y for y in historical_years if 2022 <= y < year])
    st.plotly_chart(seasonal_figure(frame, province, metric, year, extra), width="stretch")
    st.caption(MATCHING_NOTE)

    with st.expander("生长阶段", expanded=False):
        stages = frame.loc[(frame["province"] == province) & frame["metric"].isin(STAGES)]
        if stages.empty:
            st.info("该省历史文件暂无可用的生长阶段占比。")
        else:
            _table(comparison_table(frame, province, year, stages=True))
            st.caption("阶段占比描述作物当前所处阶段，不能直接当作累计完成率；缺失阶段留空。")

    with st.expander("来源与对比取数日期", expanded=False):
        st.markdown(f"[查看该省官方报告]({SOURCE_URLS[province]})")
        st.dataframe(comparison_table(frame, province, year), hide_index=True, width="stretch")
        audit = []
        for key, name in (METRICS | STAGES).items():
            item = compare_metric(frame, province, key, year)
            if not item["current"]:
                continue
            current = item["current"]
            audit.append({"指标": name, "当年日期": current["date"].strftime("%Y-%m-%d"),
                          "日期依据": "历史文件日期" if current["date_basis"] == "workbook_date" else "报告统计截止日",
                          "发布日期": "未核实" if pd.isna(current["published_at"]) else current["published_at"],
                          "去年取数日期": item["last_year"]["date"].strftime("%Y-%m-%d") if item["last_year"] else "—",
                          "均值取数日期": "、".join(x["date"].strftime("%Y-%m-%d") for x in item["samples"]),
                          "来源链接": current["source_url"],
                          "原始位置": current["source_locator"], "来源SHA256": current["source_sha256"]})
        st.dataframe(pd.DataFrame(audit), hide_index=True, width="stretch",
                     column_config={"来源链接": st.column_config.LinkColumn(display_text="查看来源")})
        for note in bundle["import_notes"]:
            st.caption(note)
