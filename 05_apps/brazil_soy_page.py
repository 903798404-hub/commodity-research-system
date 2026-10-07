"""Read-only soybean progress, crop-season comparisons and national phenology."""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st

from agri_research_agent.pipelines.brazil_soy import (
    METRICS, REGIONS, SOURCE_URL, STAGES, STABLE_RELATIVE_PATH, compare_metric,
    comparison_table, current_season, load_bundle, observations_frame, regional_comparison_table,
    seasonal_figure, sha256_file,
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
    if "地区" in display:
        display = display[["地区", "最新", "上季同期", "同期均值", "较上季", "较均值", "均值依据"]]
        display.loc[display.index[0], "地区"] = "全国汇总"
    for column in ("最新", "较上次", "上季同期", "较上季", "同期均值", "较均值"):
        if column in display:
            display[column] = display[column].map(lambda x: "—" if pd.isna(x) else f"{x:+.1f}" if column in {"较上次", "较上季", "较均值"} else f"{x:.1f}")
    styled = display.style.map(difference_cell_style, subset=[c for c in ("较上季", "较均值") if c in display])
    if "地区" in display:
        styled = styled.apply(lambda row: ["font-weight:700;" if row.name == 0 else "" for _ in row], axis=1)
    st.dataframe(styled, hide_index=True, width="stretch", height=len(display) * 35 + 38,
                 column_config={column: st.column_config.TextColumn(width="medium" if column == "地区" else "small")
                                for column in display.columns})


def _history_years(frame: pd.DataFrame, season: str, key: str) -> list[str]:
    history = sorted(set(frame["season"]) - {season}, reverse=True)
    return st.multiselect("历史作物季（2022/2023起默认显示）", history,
        default=[s for s in history if "2022/2023" <= s < season], key=key)


def _progress_tab(frame: pd.DataFrame, metric: str, label: str, season: str) -> None:
    st.subheader(label)
    dates = frame.loc[(frame["season"] == season) & (frame["metric"] == metric), "date"]
    if dates.empty:
        st.info(f"{season}季暂无已核实的{label}；下方曲线可查看往季。")
    else:
        st.caption(f"数据截至：{dates.max():%Y-%m-%d} · 全国与主要12州")
    overview = regional_comparison_table(frame, metric, season)
    _table(overview)
    stale = overview.loc[overview["最新"].isna() & (overview["日期"] != "—")]
    if not stale.empty:
        st.caption("本期缺报，最新列留空：" + "；".join(f"{row['地区']}最近观测为{row['日期']}" for _, row in stale.iterrows()))
    st.caption("全国汇总直接采用 CONAB 主要12州合计口径，现代报告覆盖约96%种植面积。最新列只展示本期观测；日期较早的州不沿用旧值。")
    st.caption("数值为%；较上季、较均值为百分点。较均值红色表示高于、蓝色表示低于，不能直接判断单产或价格方向。")
    st.caption(MATCHING_NOTE + "表格官方参考均值与图中的历史计算均值分开标示。")
    st.subheader("全国与各州历史对比")
    extra = _history_years(frame, season, f"brazil-years-{metric}")
    with st.container(key="brazil-history-charts"):
        regions = list(REGIONS)
        for offset in range(0, len(regions), 3):
            for column, region in zip(st.columns(3, gap="small"), regions[offset:offset + 3]):
                with column:
                    st.markdown(f"#### {'全国汇总' if region == 'BR' else REGIONS[region]}")
                    current = compare_metric(frame, region, metric, season)["current"]
                    if current:
                        st.caption(f"最近观测：{current['date']:%Y-%m-%d} · {current['value']:.1f}%")
                    else:
                        st.caption("本季暂无已核实观测")
                    figure = seasonal_figure(frame, region, metric, season, extra)
                    for trace in figure.data:
                        if trace.name == "前五季同期均值":
                            trace.name = "五季均值"
                        else:
                            trace.name = trace.name[2:4] + "/" + trace.name[7:9] + (" 当前" if "当前季" in trace.name else "")
                    figure.update_layout(height=320, margin=dict(l=8, r=8, t=85, b=10),
                        legend=dict(y=1.02, entrywidth=0.48, entrywidthmode="fraction"),
                        xaxis=dict(nticks=4))
                    st.plotly_chart(figure, width="stretch", key=f"brazil-history-{metric}-{region}")


def _growth_tab(frame: pd.DataFrame, season: str) -> None:
    st.subheader("全国生长进度")
    st.caption("此处仅为全国数据。官方阶段按已播面积加权，表示已播作物所处阶段；收割表示已收割部分。")
    stages = frame.loc[(frame["region"] == "BR") & frame["metric"].isin(STAGES)]
    dates = stages.loc[stages["season"] == season, "date"]
    if dates.empty:
        st.info(f"{season}季暂无已核实的全国生长阶段数据；不使用旧季数值代替。")
    else:
        st.caption(f"全国阶段数据截至：{dates.max():%Y-%m-%d}")
    stage_table = comparison_table(frame, "BR", season, stages=True)
    if not dates.empty:
        stale = stage_table["日期"] != dates.max().strftime("%Y-%m-%d")
        stage_table.loc[stale, ["最新", "较上次", "上季同期", "同期均值", "较均值"]] = None
        stage_table.loc[stale, "日期"] = "—"
    _table(stage_table)
    st.caption("各阶段不补零、不强制合计100%；阶段占比不能当作累计完成率或优良率。")
    st.caption(MATCHING_NOTE)
    st.subheader("全国阶段历史曲线")
    available = set(stages.loc[stages["season"] == season, "metric"])
    preferred = "VEGETATIVE" if "VEGETATIVE" in available else "FLOWERING"
    stage = st.selectbox("全国阶段历史对比", list(STAGES), format_func=STAGES.get, index=list(STAGES).index(preferred))
    extra = _history_years(frame, season, "brazil-years-growth")
    st.plotly_chart(seasonal_figure(frame, "BR", stage, season, extra), width="stretch", key="brazil-national-stage")


def render_brazil_soy_page() -> None:
    st.title("巴西大豆种植与生长")
    st.caption("播种、收割分别查看全国及主要12州的同期对比；生长阶段仅为全国数据。按作物季比较历史进度。")
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
    seasons = sorted(set(frame["season"]) | {current_season()}, reverse=True)
    season = st.selectbox("作物季", seasons, index=seasons.index(current_season()))
    st.html("""<style>
        @media (max-width: 900px) {
            .st-key-brazil-history-charts [data-testid="stHorizontalBlock"] { flex-direction: column; }
            .st-key-brazil-history-charts [data-testid="stColumn"] { width: 100% !important; flex: 1 1 100% !important; }
        }
    </style>""")
    planting, harvest, growth = st.tabs(["播种进度", "收割进度", "生长进度"],
                                      key="brazil-metric-tabs", on_change="rerun")
    if planting.open:
        with planting:
            _progress_tab(frame, "PLANTED", METRICS["PLANTED"], season)
    if harvest.open:
        with harvest:
            _progress_tab(frame, "HARVESTED", METRICS["HARVESTED"], season)
    if growth.open:
        with growth:
            _growth_tab(frame, season)

    with st.expander("来源与取数说明"):
        st.markdown(f"[查看 CONAB 官方周报]({SOURCE_URL})")
        audit = []
        for metric, label in (METRICS | STAGES).items():
            for region in (REGIONS if metric in METRICS else {"BR": REGIONS["BR"]}):
                item = compare_metric(frame, region, metric, season)
                current = item["current"]
                if not current:
                    continue
                audit.append({"指标": label, "地区": REGIONS[region], "作物季": season, "数据日期": current["date"].strftime("%Y-%m-%d"),
                    "日期依据": "历史文件日期" if current["date_basis"] == "workbook_date" else "报告统计截止日",
                    "发布日期": current["published_at"] or "未逐期核实", "来源链接": current["source_url"],
                    "原始位置": current["source_locator"], "文件SHA256": current["source_sha256"],
                    "均值依据": item["reference_label"]})
        st.dataframe(pd.DataFrame(audit), hide_index=True, width="stretch",
                     column_config={"来源链接": st.column_config.LinkColumn(display_text="查看来源")})
        for note in bundle["import_notes"]:
            st.caption(note)
