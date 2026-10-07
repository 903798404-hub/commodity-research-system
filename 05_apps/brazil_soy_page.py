"""Read-only soybean progress, crop-season comparisons and national phenology."""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st

from agri_research_agent.pipelines.brazil_soy import (
    METRICS, REGIONS, SOURCE_URL, STAGES, STABLE_RELATIVE_PATH, compare_metric,
    comparison_table, current_season, load_bundle, observations_frame, seasonal_figure, sha256_file,
)
from agri_research_agent.shared.chart_style import difference_cell_style

ROOT = Path(__file__).resolve().parents[1]
MATCHING_NOTE = "历史同期取相同季节月日及此前7天内最近观测，不插值、不延续更早记录；均值为前五个作物季，有效样本不足时显示n/5。"


def data_path() -> Path:
    runtime = os.getenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", "").strip()
    return (Path(runtime) if runtime else ROOT / "01_data/brazil-soy-dev") / STABLE_RELATIVE_PATH


@st.cache_data(show_spinner=False)
def read_data(path: str, identity: str) -> dict:
    bundle = load_bundle(Path(path))
    if sha256_file(Path(path)) != identity:
        raise ValueError("数据在读取期间发生变化，请刷新页面")
    return bundle


def _table(table: pd.DataFrame) -> None:
    display = table.copy()
    for column in ("最新", "较上次", "上季同期", "同期均值", "较均值"):
        display[column] = display[column].map(lambda x: "—" if pd.isna(x) else f"{x:+.1f}" if column in {"较上次", "较均值"} else f"{x:.1f}")
    st.dataframe(display.style.map(difference_cell_style, subset=["较均值"]), hide_index=True, width="stretch",
                 column_config={column: st.column_config.TextColumn(width="small") for column in display.columns})


def render_brazil_soy_page() -> None:
    st.title("巴西大豆种植与生长")
    st.caption("播种与收割查看全国和主要12州；生长阶段仅为全国数据。按作物季比较历史进度。")
    path = data_path()
    if not path.is_file():
        st.info("巴西历史数据尚未导入。")
        return
    try:
        bundle = read_data(str(path), sha256_file(path))
        frame = observations_frame(bundle)
    except (ValueError, OSError, KeyError) as exc:
        st.error(f"巴西数据暂不可读：{exc}")
        return
    with st.container(key="brazil-controls"):
        region_col, season_col = st.columns([2, 1])
        with region_col:
            region = st.selectbox("地区（播种 / 收割）", list(REGIONS), format_func=REGIONS.get)
        with season_col:
            seasons = sorted(set(frame["season"]) | {current_season()}, reverse=True)
            season = st.selectbox("作物季", seasons, index=seasons.index(current_season()))
    st.subheader("最新进度与历史同期")
    _table(comparison_table(frame, region, season))
    st.caption("数值为%；较上次、较均值为百分点。红色高于均值、蓝色低于均值，不能直接判断单产或价格方向。官方参考均值与图中的历史计算均值分开标示。")
    for metric, label in METRICS.items():
        item = compare_metric(frame, region, metric, season)
        if not item["current"]:
            st.caption(f"{label}：{season}季暂无有效观测。")
        elif item["previous_days"] is not None and item["previous_days"] != 7:
            st.caption(f"{label}较上次变化间隔{item['previous_days']}天。")
    history = sorted(set(frame["season"]) - {season}, reverse=True)
    extra = st.multiselect("历史作物季（2022/2023起默认显示）", history,
        default=[s for s in history if "2022/2023" <= s < season])
    st.html("""<style>@media(max-width:900px){
        .st-key-brazil-history-charts [data-testid="stHorizontalBlock"]{flex-direction:column;}
        .st-key-brazil-history-charts [data-testid="stColumn"]{width:100%!important;flex:1 1 100%!important;}
        }</style>""")
    with st.container(key="brazil-history-charts"):
        for column, (metric, label) in zip(st.columns(2, gap="small"), METRICS.items()):
            with column:
                st.markdown(f"#### {label}")
                latest = compare_metric(frame, region, metric, season)["current"]
                st.caption(f"最新观测：{latest['date']:%Y-%m-%d} · {latest['value']:.1f}%" if latest else f"{season}季暂无观测，显示已有历史。")
                st.plotly_chart(seasonal_figure(frame, region, metric, season, extra), width="stretch", key=f"brazil-history-{metric}")
    st.caption(MATCHING_NOTE)

    st.subheader("全国生长进度")
    st.caption("此处始终为全国数据，不随州选择改变。官方阶段按已播面积加权，表示已播作物所处阶段；收割表示已收割部分。")
    stages = frame.loc[(frame["region"] == "BR") & frame["metric"].isin(STAGES)]
    dates = stages.loc[stages["season"] == season, "date"]
    if dates.empty:
        st.info(f"{season}季暂无已核实的全国生长阶段数据；不使用旧季数值代替。")
    else:
        latest_date = dates.max()
        st.caption(f"全国阶段数据截至：{latest_date:%Y-%m-%d}")
    stage_table = comparison_table(frame, "BR", season, stages=True)
    # A stage missing at the latest common cutoff must not borrow a stale earlier stage value.
    if not dates.empty:
        stale = stage_table["日期"] != dates.max().strftime("%Y-%m-%d")
        stage_table.loc[stale, ["最新", "较上次", "上季同期", "同期均值", "较均值"]] = None
        stage_table.loc[stale, "日期"] = "—"
    _table(stage_table)
    available = set(stages.loc[stages["season"] == season, "metric"])
    preferred = "VEGETATIVE" if "VEGETATIVE" in available else "FLOWERING"
    stage = st.selectbox("全国阶段历史对比", list(STAGES), format_func=STAGES.get, index=list(STAGES).index(preferred))
    st.plotly_chart(seasonal_figure(frame, "BR", stage, season, extra), width="stretch", key="brazil-national-stage")
    st.caption("各阶段不补零、不强制合计100%；阶段占比不能当作累计完成率或优良率。生长曲线为单个阶段的历史比较。")

    with st.expander("来源与取数说明"):
        st.markdown(f"[查看 CONAB 官方周报]({SOURCE_URL})")
        audit = []
        for metric, label in (METRICS | STAGES).items():
            item = compare_metric(frame, "BR" if metric in STAGES else region, metric, season)
            current = item["current"]
            if current:
                audit.append({"指标": label, "作物季": season, "数据日期": current["date"].strftime("%Y-%m-%d"),
                    "日期依据": "历史文件日期" if current["date_basis"] == "workbook_date" else "报告统计截止日",
                    "发布日期": current["published_at"] or "未逐期核实", "来源链接": current["source_url"],
                    "原始位置": current["source_locator"], "文件SHA256": current["source_sha256"],
                    "均值依据": item["reference_label"]})
        st.dataframe(pd.DataFrame(audit), hide_index=True, width="stretch",
                     column_config={"来源链接": st.column_config.LinkColumn(display_text="查看来源")})
        for note in bundle["import_notes"]:
            st.caption(note)
