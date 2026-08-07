"""Thin Streamlit consumer for USDA soybean export sales and inspections research."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "03_src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agri_research_agent.soybean_exports.research import (  # noqa: E402
    load_soybean_export_page_payload,
)


RUNTIME_ROOT_ENV = "SOYBEAN_EXPORT_RUNTIME_ROOT"
USDA_PROJECT_ROOT = PROJECT_ROOT / "11_独立应用" / "USDA平衡表"
WORLD_COLOR = "#244A73"
CHINA_COLOR = "#C05A3D"
NON_CHINA_COLOR = "#4F8A83"
PREVIOUS_COLOR = "#7A8797"
HISTORY_YEAR_COLORS = (
    "#B3473A",  # current: muted brick red
    "#62788F",  # previous: slate blue grey
    "#5D8E92",  # two years back: muted teal
    "#7E9A73",  # three years back: sage green
    "#B29A5B",  # four years back: muted gold brown
    "#C08267",  # five years back: muted terracotta
    "#8D8098",  # six years back: grey purple
)
GRID_COLOR = "rgba(102, 112, 133, 0.14)"
CHART_HEIGHT = 382


def resolve_export_runtime_root() -> Path:
    configured = os.getenv(RUNTIME_ROOT_ENV, "").strip()
    return Path(configured).resolve() if configured else PROJECT_ROOT


@st.cache_data(show_spinner=False)
def load_export_page_payload(
    runtime_root: str,
    fgis_mtime_ns: int | None,
    fas_mtime_ns: int | None,
    fgis_manifest_mtime_ns: int | None,
    fas_manifest_mtime_ns: int | None,
    fgis_status_mtime_ns: int | None,
    fas_status_mtime_ns: int | None,
    psd_version_mtime_ns: int | None,
) -> dict[str, Any]:
    del (
        fgis_mtime_ns,
        fas_mtime_ns,
        fgis_manifest_mtime_ns,
        fas_manifest_mtime_ns,
        fgis_status_mtime_ns,
        fas_status_mtime_ns,
        psd_version_mtime_ns,
    )
    return load_soybean_export_page_payload(
        Path(runtime_root), usda_project_root=USDA_PROJECT_ROOT
    )


def render_soybean_exports_page(payload: dict[str, Any] | None = None) -> None:
    """Render one source-tolerant page; all business facts arrive precomputed."""

    _inject_export_styles()
    st.title("出口销售与装船")
    st.caption(
        "USDA FGIS 出口检验与 FAS 出口销售的周度研究视图；两个来源独立更新、独立降级。"
    )
    if payload is None:
        runtime_root = resolve_export_runtime_root()
        paths = _consumer_paths(runtime_root)
        payload = load_export_page_payload(
            str(runtime_root), *(_mtime(path) for path in paths)
        )

    _render_source_status(payload)
    _render_kpis(payload)
    _render_weekly_observation(payload)

    fgis = payload.get("fgis")
    if fgis:
        st.markdown("## 出口检验 · FGIS")
        st.caption("美国大豆出口检验及中国大陆接收进度的历史同期比较。")
        figures = build_fgis_figures(fgis)
        _render_two_column_figures(figures)
    else:
        st.warning("出口检验数据暂不可用；出口销售内容仍可继续使用。")

    fas = payload.get("fas")
    if fas:
        current_figures, next_figures, execution_figure = build_fas_figures(fas)
        st.markdown("## 本年度销售 · FAS")
        st.caption(
            "Export Sales Reporting 本年度总销售及中国大陆采购的历史同期比较。"
        )
        _render_two_column_figures(current_figures)
        st.markdown("### 销售执行情况")
        st.caption("当前销售承诺中已出口与待执行的进度；与 FGIS 出口检验属于不同统计体系。")
        _render_single_figure(execution_figure)

        st.markdown("## 下一年度销售 · FAS")
        st.caption("在各自 report MY 的同一 report week，对下一 target MY 预售承诺的历史比较。")
        _render_next_year_numbers(fas["next_summary"])
        _render_two_column_figures(next_figures)
    else:
        st.warning("出口销售数据暂不可用；出口检验内容仍可继续使用。")

    _render_methodology_note()


def build_fgis_figures(section: dict[str, Any]) -> list[go.Figure]:
    frame = pd.DataFrame(section["series"])
    current = int(section["current_market_year_end"])
    previous = int(section["previous_market_year_end"])
    weekly = _seasonal_figure(
        frame,
        x_field="my_week",
        value_field="world_weekly_mt",
        year_field="market_year_end",
        label_field="market_year_label",
        title="周度出口检验",
        subtitle="美国大豆 · 万吨",
        chart_id="fgis_weekly_inspections",
        current_year=current,
        previous_year=previous,
        allow_negative=False,
    )
    cumulative = _seasonal_figure(
        frame,
        x_field="my_week",
        value_field="world_cumulative_mt",
        year_field="market_year_end",
        label_field="market_year_label",
        title="累计出口检验",
        subtitle="美国大豆 · 万吨",
        chart_id="fgis_cumulative_inspections",
        current_year=current,
        previous_year=previous,
        allow_negative=False,
        cumulative_comparison=section["seasonal_comparisons"]["world_cumulative_mt"],
    )
    china_weekly = _seasonal_figure(
        frame,
        x_field="my_week",
        value_field="china_weekly_mt",
        year_field="market_year_end",
        label_field="market_year_label",
        title="周度出口检验至中国",
        subtitle="中国 · 万吨",
        chart_id="fgis_china_weekly_inspections",
        current_year=current,
        previous_year=previous,
        allow_negative=False,
    )
    china_cumulative = _seasonal_figure(
        frame,
        x_field="my_week",
        value_field="china_cumulative_mt",
        year_field="market_year_end",
        label_field="market_year_label",
        title="累计出口检验至中国",
        subtitle="中国 · 万吨",
        chart_id="fgis_china_cumulative_inspections",
        current_year=current,
        previous_year=previous,
        allow_negative=False,
        cumulative_comparison=section["seasonal_comparisons"]["china_cumulative_mt"],
    )
    return [weekly, cumulative, china_weekly, china_cumulative]


def build_fas_figures(
    section: dict[str, Any],
) -> tuple[list[go.Figure], list[go.Figure], go.Figure]:
    frame = pd.DataFrame(section["series"])
    current = int(section["current_report_market_year_end"])
    previous = int(section["previous_report_market_year_end"])
    weekly = _seasonal_figure(
        frame,
        x_field="report_week",
        value_field="world_current_my_net_sales_mt",
        year_field="report_market_year_end",
        label_field="report_market_year_label",
        title="本年度周度净销售",
        subtitle="美国大豆 · 万吨",
        chart_id="fas_current_weekly_net_sales",
        current_year=current,
        previous_year=previous,
        allow_negative=True,
    )
    cumulative = _seasonal_figure(
        frame,
        x_field="report_week",
        value_field="world_current_my_total_commitment_mt",
        year_field="report_market_year_end",
        label_field="report_market_year_label",
        title="本年度累计销售",
        subtitle="美国大豆 · 万吨",
        chart_id="fas_current_total_commitments",
        current_year=current,
        previous_year=previous,
        allow_negative=False,
        cumulative_comparison=section["seasonal_comparisons"][
            "world_current_my_total_commitment_mt"
        ],
    )
    china_weekly = _seasonal_figure(
        frame,
        x_field="report_week",
        value_field="china_current_my_net_sales_mt",
        year_field="report_market_year_end",
        label_field="report_market_year_label",
        title="本年度当周对中国净销售",
        subtitle="中国 · 万吨",
        chart_id="fas_current_china_weekly_net_sales",
        current_year=current,
        previous_year=previous,
        allow_negative=True,
    )
    china_cumulative = _seasonal_figure(
        frame,
        x_field="report_week",
        value_field="china_current_my_total_commitment_mt",
        year_field="report_market_year_end",
        label_field="report_market_year_label",
        title="本年度累计对中国销售",
        subtitle="中国 · 万吨",
        chart_id="fas_current_china_total_commitments",
        current_year=current,
        previous_year=previous,
        allow_negative=False,
        cumulative_comparison=section["seasonal_comparisons"][
            "china_current_my_total_commitment_mt"
        ],
    )

    next_weekly = _seasonal_figure(
        frame,
        x_field="report_week",
        value_field="world_next_my_net_sales_mt",
        year_field="report_market_year_end",
        label_field="report_market_year_label",
        title="下一年度周度净销售",
        subtitle="美国大豆 · 万吨",
        chart_id="fas_next_weekly_net_sales",
        current_year=current,
        previous_year=previous,
        allow_negative=True,
        target_year_field="next_target_market_year_end",
    )
    next_cumulative = _seasonal_figure(
        frame,
        x_field="report_week",
        value_field="world_next_my_outstanding_sales_mt",
        year_field="report_market_year_end",
        label_field="report_market_year_label",
        title="下一年度累计销售",
        subtitle="美国大豆 · 万吨",
        chart_id="fas_next_total_presales",
        current_year=current,
        previous_year=previous,
        allow_negative=False,
        target_year_field="next_target_market_year_end",
        cumulative_comparison=section["seasonal_comparisons"][
            "world_next_my_outstanding_sales_mt"
        ],
    )
    next_china_weekly = _seasonal_figure(
        frame,
        x_field="report_week",
        value_field="china_next_my_net_sales_mt",
        year_field="report_market_year_end",
        label_field="report_market_year_label",
        title="下一年度当周对中国净销售",
        subtitle="中国 · 万吨",
        chart_id="fas_next_china_weekly_net_sales",
        current_year=current,
        previous_year=previous,
        allow_negative=True,
        target_year_field="next_target_market_year_end",
    )
    next_china_cumulative = _seasonal_figure(
        frame,
        x_field="report_week",
        value_field="china_next_my_outstanding_sales_mt",
        year_field="report_market_year_end",
        label_field="report_market_year_label",
        title="下一年度累计对中国销售",
        subtitle="中国 · 万吨",
        chart_id="fas_next_china_total_presales",
        current_year=current,
        previous_year=previous,
        allow_negative=False,
        target_year_field="next_target_market_year_end",
        cumulative_comparison=section["seasonal_comparisons"][
            "china_next_my_outstanding_sales_mt"
        ],
    )
    execution = _commitment_export_figure(frame, current, previous, section)
    return (
        [weekly, cumulative, china_weekly, china_cumulative],
        [next_weekly, next_cumulative, next_china_weekly, next_china_cumulative],
        execution,
    )


def _seasonal_figure(
    frame: pd.DataFrame,
    *,
    x_field: str,
    value_field: str,
    year_field: str,
    label_field: str,
    title: str,
    subtitle: str,
    chart_id: str,
    current_year: int,
    previous_year: int,
    allow_negative: bool,
    target_year_field: str | None = None,
    cumulative_comparison: dict[str, Any] | None = None,
) -> go.Figure:
    figure = go.Figure()
    years = sorted(int(value) for value in frame[year_field].dropna().unique())
    for year in years:
        annual = frame.loc[frame[year_field].eq(year)].sort_values(x_field)
        color, width, dash, opacity = _year_style(year, current_year, previous_year)
        target_labels = (
            annual[target_year_field].map(_market_year_label)
            if target_year_field
            else pd.Series([""] * len(annual), index=annual.index)
        )
        custom = pd.DataFrame(
            {
                "report_label": annual[label_field],
                "week_ending": annual["week_ending_date"],
                "target_label": target_labels,
            }
        )
        figure.add_trace(
            go.Scatter(
                x=annual[x_field],
                y=annual[value_field] / 10_000,
                mode="lines",
                name=str(annual.iloc[0][label_field]),
                line={"color": color, "width": width, "dash": dash},
                opacity=opacity,
                connectgaps=False,
                customdata=custom,
                hovertemplate=_seasonal_hover(x_field, target_year_field is not None),
            )
        )
        if year == current_year:
            _add_latest_annotation(figure, annual, x_field, value_field, color)
    _style_figure(
        figure,
        title=title,
        subtitle=subtitle,
        x_title="MY week" if x_field == "my_week" else "Report week",
        allow_negative=allow_negative,
        meta={
            "chart_id": chart_id,
            "core_seasonal": True,
            "x_field": x_field,
            "value_field": value_field,
            "unit": "万吨",
            "history_years": years,
            "current_year": current_year,
            "previous_year": previous_year,
            "latest_point_label": True,
            "connect_gaps": False,
            "comparison_callout": cumulative_comparison is not None,
            "same_week_rank": (
                cumulative_comparison.get("same_week_rank")
                if cumulative_comparison is not None
                else None
            ),
            "same_week_comparable_years": (
                cumulative_comparison.get("same_week_comparable_years")
                if cumulative_comparison is not None
                else None
            ),
            "target_my_week": False if target_year_field else None,
        },
    )
    if cumulative_comparison is not None:
        _add_cumulative_callout(figure, cumulative_comparison)
    return figure


def _commitment_export_figure(
    frame: pd.DataFrame,
    current_year: int,
    previous_year: int,
    section: dict[str, Any],
) -> go.Figure:
    figure = go.Figure()
    years = [year for year in (previous_year, current_year) if year in set(frame["report_market_year_end"])]
    for year in years:
        annual = frame.loc[frame["report_market_year_end"].eq(year)].sort_values("report_week")
        is_current = year == current_year
        opacity = 1.0 if is_current else 0.34
        dash = "solid" if is_current else "dash"
        custom = pd.DataFrame(
            {
                "label": annual["report_market_year_label"],
                "date": annual["week_ending_date"],
                "outstanding": annual["world_outstanding_sales_mt"] / 10_000,
            }
        )
        figure.add_trace(
            go.Scatter(
                x=annual["report_week"],
                y=annual["world_accumulated_exports_mt"] / 10_000,
                mode="lines",
                name=f"出口 · {annual.iloc[0]['report_market_year_label']}",
                line={"color": NON_CHINA_COLOR if is_current else PREVIOUS_COLOR, "width": 2.5, "dash": dash},
                opacity=opacity,
                connectgaps=False,
                customdata=custom,
                hovertemplate="Report MY %{customdata[0]}<br>Report week %{x}<br>Week ending %{customdata[1]}<br>累计出口 %{y:,.2f}万吨<br>待执行 %{customdata[2]:,.2f}万吨<extra></extra>",
            )
        )
        figure.add_trace(
            go.Scatter(
                x=annual["report_week"],
                y=annual["world_current_my_total_commitment_mt"] / 10_000,
                mode="lines",
                name=f"销售 · {annual.iloc[0]['report_market_year_label']}",
                line={"color": WORLD_COLOR if is_current else PREVIOUS_COLOR, "width": 3 if is_current else 1.6, "dash": dash},
                opacity=opacity,
                fill="tonexty" if is_current else None,
                fillcolor="rgba(36, 74, 115, 0.10)" if is_current else None,
                connectgaps=False,
                customdata=custom,
                hovertemplate="Report MY %{customdata[0]}<br>Report week %{x}<br>Week ending %{customdata[1]}<br>累计销售 %{y:,.2f}万吨<br>待执行 %{customdata[2]:,.2f}万吨<extra></extra>",
            )
        )
        if is_current:
            _add_latest_annotation(
                figure, annual, "report_week", "world_current_my_total_commitment_mt", WORLD_COLOR
            )
    summary = section["current_summary"]
    progress = _format_pct(summary.get("sales_progress_pct"), signed=False)
    figure.add_annotation(
        xref="paper",
        yref="paper",
        x=0.99,
        y=0.98,
        xanchor="right",
        yanchor="top",
        showarrow=False,
        align="right",
        text=(
            f"累计销售 {_format_wan(summary['world_total_commitments_mt'])}<br>"
            f"累计出口 {_format_wan(summary['world_accumulated_exports_mt'])}<br>"
            f"待执行 {_format_wan(summary['world_outstanding_sales_mt'])}<br>"
            f"销售完成率 {progress}"
        ),
        font={"size": 13, "color": "#536477"},
        bgcolor="rgba(255,255,255,.82)",
        borderpad=5,
    )
    _style_figure(
        figure,
        title="累计销售与出口执行",
        subtitle="本年度 · 万吨",
        x_title="Report week",
        allow_negative=False,
        meta={
            "chart_id": "fas_commitments_vs_exports",
            "core_seasonal": False,
            "auxiliary_execution": True,
            "x_field": "report_week",
            "unit": "万吨",
            "history_years": years,
            "current_year": current_year,
            "previous_year": previous_year,
            "official_outstanding_field": "world_outstanding_sales_mt",
            "latest_point_label": True,
            "connect_gaps": False,
        },
    )
    figure.update_layout(
        legend={"y": 1.24, "yanchor": "bottom"},
        margin={"l": 50, "r": 28, "t": 125, "b": 48},
    )
    return figure


def _render_source_status(payload: dict[str, Any]) -> None:
    columns = st.columns(2)
    for column, key, title in zip(
        columns,
        ("fgis", "fas"),
        ("出口检验 · FGIS", "出口销售 · FAS"),
        strict=True,
    ):
        section = payload.get(key)
        with column, st.container(border=True):
            st.markdown(f"**{title}**")
            if section:
                summary = section["summary"] if key == "fgis" else section["current_summary"]
                st.markdown(f"更新至 `{summary['latest_week']}`")
                health = section.get("health")
                if health == "warning":
                    st.caption("最近尝试异常，当前展示最近成功 stable。")
                else:
                    st.caption("稳定数据及身份校验通过。")
                if key == "fas" and section.get("release_timestamp_raw"):
                    suffix = " ET" if section.get("release_timezone") == "America/New_York" else ""
                    st.caption(
                        f"官方发布：{str(section['release_timestamp_raw']).replace('T', ' ')}{suffix}"
                    )
            else:
                st.markdown("更新日期 `—`")
                st.caption(payload.get("errors", {}).get(key) or "数据暂不可用。")


def _render_kpis(payload: dict[str, Any]) -> None:
    st.markdown("### 核心指标")
    kpis = payload.get("kpis", {})
    reasons = payload.get("kpi_reasons", {})
    definitions = (
        ("累计出口检验同比", "cumulative_export_inspections_yoy_pct", "pct"),
        ("本年度销售完成率", "current_my_sales_progress_pct", "progress"),
        ("下一年度累计销售", "next_my_total_sales_mt", "mt"),
        ("中国下一年度累计采购", "china_next_my_total_purchases_mt", "mt"),
    )
    for column, (label, key, kind) in zip(st.columns(4), definitions, strict=True):
        value = kpis.get(key)
        display = (
            _format_pct(value)
            if kind == "pct"
            else _format_pct(value, signed=False)
            if kind == "progress"
            else _format_wan(value)
        )
        with column:
            st.metric(label, display, help=reasons.get(key))


def _render_weekly_observation(payload: dict[str, Any]) -> None:
    st.markdown("### 本周观察")
    fgis = payload.get("fgis")
    fas = payload.get("fas")
    with st.container(border=True):
        if fgis:
            item = fgis["summary"]
            previous = _format_wan(item.get("previous_week_world_mt"))
            revision = (
                f"前一周初值 {_format_wan(item['previous_week_initial_mt'])}。"
                if item.get("previous_week_initial_observed")
                else "尚无由本系统观察到的前一周初值/修订证据。"
            )
            st.markdown(
                f"**出口检验**  截至 {item['latest_week']} （MY {item['market_year_label']} 第{item['my_week']}周），"
                f"本周 World {_format_wan(item['weekly_world_mt'])}，前一周 {previous}；"
                f"累计 {_format_wan(item['cumulative_world_mt'])}，累计同比 {_format_pct(item['cumulative_yoy_pct'])}。"
                f"中国本周 {_format_wan(item['weekly_china_mt'])}，占比 {_format_pct(item['china_share_pct'], signed=False)}。{revision}"
            )
        else:
            st.markdown("**出口检验**  FGIS 数据暂不可用。")
        if fas:
            current = fas["current_summary"]
            next_item = fas["next_summary"]
            st.markdown(
                f"**本年度销售**  截至 {current['latest_week']} （Report MY {current['report_market_year_label']} 第{current['report_week']}周），"
                f"本周净销售 {_format_wan(current['world_weekly_net_sales_mt'])}，累计销售 {_format_wan(current['world_total_commitments_mt'])}，"
                f"累计出口 {_format_wan(current['world_accumulated_exports_mt'])}，待执行销售 {_format_wan(current['world_outstanding_sales_mt'])}，"
                f"销售完成率 {_format_pct(current.get('sales_progress_pct'), signed=False)}。"
            )
            st.markdown(
                f"**下一年度销售**  在 Report MY {next_item['report_market_year_label']} 第{next_item['report_week']}周，"
                f"对目标 MY {_market_year_label(next_item['target_market_year_end'])} 的本周净销售为 {_format_wan(next_item['world_weekly_net_sales_mt'])}，"
                f"累计预售 {_format_wan(next_item['world_total_presales_mt'])}；中国本周净销售 {_format_wan(next_item['china_weekly_net_sales_mt'])}，"
                f"非中国本周净销售 {_format_wan(next_item['non_china_weekly_net_sales_mt'])}，中国累计预售 {_format_wan(next_item['china_total_presales_mt'])}。"
            )
        else:
            st.markdown("**本年度/下一年度销售**  FAS 数据暂不可用。")


def _render_next_year_numbers(summary: dict[str, Any]) -> None:
    st.markdown(
        f"**报告年度：{summary['report_market_year_label']}** · "
        f"**销售年度：{_market_year_label(summary['target_market_year_end'])}** · "
        f"Report week {summary['report_week']}"
    )
    labels = (
        ("下一年度本周净销售", summary["world_weekly_net_sales_mt"]),
        ("下一年度累计销售", summary["world_total_presales_mt"]),
        ("中国下一年度本周净销售", summary["china_weekly_net_sales_mt"]),
        ("中国下一年度累计销售", summary["china_total_presales_mt"]),
    )
    for column, (label, value) in zip(st.columns(4), labels, strict=True):
        with column:
            st.metric(label, _format_wan(value))


def _render_two_column_figures(figures: list[go.Figure]) -> None:
    for start in range(0, len(figures), 2):
        pair = figures[start : start + 2]
        columns = st.columns(len(pair))
        for column, figure in zip(columns, pair, strict=True):
            with column:
                chart_id = str((figure.layout.meta or {}).get("chart_id", start))
                st.plotly_chart(
                    figure,
                    width="stretch",
                    config={"displayModeBar": False, "responsive": True},
                    key=f"soybean_exports_{chart_id}",
                )


def _render_single_figure(figure: go.Figure) -> None:
    chart_id = str((figure.layout.meta or {}).get("chart_id", "single"))
    st.plotly_chart(
        figure,
        width="stretch",
        config={"displayModeBar": False, "responsive": True},
        key=f"soybean_exports_{chart_id}",
    )


def _render_methodology_note() -> None:
    with st.expander("数据来源与口径说明"):
        st.markdown(
            "- **FGIS**：USDA Federal Grain Inspection Service · Grain Inspections，反映出口检验与目的地结构。\n"
            "- **FAS**：USDA Foreign Agricultural Service · Export Sales Reporting，反映销售、累计出口与待执行承诺。\n"
            "- FGIS 出口检验与 FAS 出口数据属于不同统计体系，数字不要求完全一致。\n"
            "- 下一年度累计销售是下一 MY 尚未执行的预售承诺存量；销售完成率为当前 MY Total Commitments ÷ USDA PS&D 美国大豆同 MY 出口预测。"
        )


def _style_figure(
    figure: go.Figure,
    *,
    title: str,
    subtitle: str,
    x_title: str,
    allow_negative: bool,
    meta: dict[str, Any],
) -> None:
    max_x = max((max(trace.x) for trace in figure.data if len(trace.x)), default=53)
    tick_values = list(range(1, int(max_x) + 1, 4))
    if int(max_x) not in tick_values:
        tick_values.append(int(max_x))
    figure.update_layout(
        title={
            "text": f"{title}<br><sup>{subtitle}</sup>",
            "font": {"size": 20, "color": "#20354C"},
            "x": 0.01,
            "xanchor": "left",
            "y": 0.955,
            "yanchor": "top",
        },
        height=CHART_HEIGHT,
        margin={"l": 58, "r": 34, "t": 116, "b": 58},
        paper_bgcolor="#FFFFFF",
        plot_bgcolor="#FFFFFF",
        hovermode="closest",
        hoverlabel={"font": {"size": 14}},
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "left",
            "x": 0,
            "font": {"size": 13, "color": "#465668"},
            "traceorder": "normal",
        },
        xaxis={
            "title": {"text": x_title, "font": {"size": 14}},
            "tickfont": {"size": 12},
            "tickmode": "array",
            "tickvals": tick_values,
            "showgrid": False,
            "zeroline": False,
            "linecolor": "#D8E0E9",
        },
        yaxis={
            "title": {"text": "万吨", "font": {"size": 14}},
            "tickfont": {"size": 12},
            "gridcolor": GRID_COLOR,
            "zeroline": allow_negative,
            "zerolinecolor": "#8B97A6",
            "zerolinewidth": 1,
        },
        meta=meta,
    )


def _add_latest_annotation(
    figure: go.Figure,
    annual: pd.DataFrame,
    x_field: str,
    value_field: str,
    color: str,
) -> None:
    valid = annual.dropna(subset=[x_field, value_field])
    if valid.empty:
        return
    latest = valid.iloc[-1]
    value = float(latest[value_field]) / 10_000
    figure.add_annotation(
        x=latest[x_field],
        y=value,
        text=f"{value:,.1f}",
        showarrow=False,
        xshift=-4,
        yshift=13,
        xanchor="right",
        font={"size": 14, "color": color},
        bgcolor="rgba(255,255,255,.82)",
    )


def _add_cumulative_callout(figure: go.Figure, comparison: dict[str, Any]) -> None:
    rank = comparison.get("same_week_rank")
    comparable_years = comparison.get("same_week_comparable_years")
    rank_text = (
        f"同期排名 {int(rank)} / {int(comparable_years)}"
        if rank is not None and comparable_years
        else "同期排名 —"
    )
    figure.add_annotation(
        xref="paper",
        yref="paper",
        x=0.99,
        y=1.21,
        xanchor="right",
        yanchor="bottom",
        showarrow=False,
        align="right",
        text=(
            f"<span style='color:{HISTORY_YEAR_COLORS[0]}'>"
            f"当前 {_format_wan(comparison.get('current_mt'))}</span><br>"
            f"<span style='color:{HISTORY_YEAR_COLORS[1]}'>"
            f"去年同期 {_format_wan(comparison.get('previous_same_week_mt'))}</span><br>"
            f"同比 {_format_pct(comparison.get('yoy_pct'))}<br>"
            f"{rank_text}"
        ),
        font={"size": 13, "color": "#536477"},
        bgcolor="rgba(255,255,255,.82)",
        borderpad=5,
    )
    figure.update_layout(legend={"y": 0.98})


def _year_style(year: int, current: int, previous: int) -> tuple[str, float, str, float]:
    age = max(0, current - year)
    color = HISTORY_YEAR_COLORS[min(age, len(HISTORY_YEAR_COLORS) - 1)]
    if year == current:
        return color, 3.9, "solid", 1.0
    if year == previous:
        return color, 2.4, "dash", 0.95
    return color, 1.9, "solid", 0.90


def _seasonal_hover(x_field: str, has_target: bool) -> str:
    if x_field == "my_week":
        identity = "MY %{customdata[0]}<br>MY week %{x}"
    elif has_target:
        identity = (
            "Report MY %{customdata[0]}<br>"
            "Target MY %{customdata[2]}<br>Report week %{x}"
        )
    else:
        identity = "Report MY %{customdata[0]}<br>Report week %{x}"
    return f"{identity}<br>Week ending %{{customdata[1]}}<br>%{{y:,.2f}}万吨<extra></extra>"


def _consumer_paths(runtime_root: Path) -> tuple[Path, ...]:
    data = runtime_root / "01_data"
    return (
        data / "processed/soybean_export_inspections/soybean_export_inspections_weekly.parquet",
        data / "processed/soybean_export_sales/soybean_export_sales_weekly.parquet",
        data / "processed/soybean_export_inspections/soybean_export_inspections_weekly.manifest.json",
        data / "processed/soybean_export_sales/soybean_export_sales_weekly.manifest.json",
        data / "update_status/soybean_export_inspections.json",
        data / "update_status/soybean_export_sales.json",
        USDA_PROJECT_ROOT / "public/data/report_version.json",
    )


def _mtime(path: Path) -> int | None:
    return path.stat().st_mtime_ns if path.is_file() else None


def _format_pct(value: Any, *, signed: bool = True) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):+.1f}%" if signed else f"{float(value):.1f}%"


def _format_wan(value: Any) -> str:
    return "—" if value is None or pd.isna(value) else f"{float(value) / 10_000:,.1f} 万吨"


def _market_year_label(market_year_end: int) -> str:
    return f"{int(market_year_end) - 1}/{str(int(market_year_end))[-2:]}"


def _inject_export_styles() -> None:
    st.html(
        """
<style>
div[data-testid="stMetric"] { min-height: 84px; padding: 10px 14px; border: 1px solid #E1E7EE; border-radius: 12px; background: #FFF; box-shadow: 0 2px 8px rgba(23,32,51,.04); }
div[data-testid="stMetricLabel"] { color: #667085; }
div[data-testid="stMetricLabel"] p { font-size: .94rem; }
div[data-testid="stMetricValue"] { color: #172E46; font-size: clamp(28px, 2.3vw, 36px); line-height: 1.12; }
div[data-testid="stVerticalBlockBorderWrapper"] > div { padding-top: .58rem; padding-bottom: .58rem; }
div[data-testid="stVerticalBlockBorderWrapper"] div[data-testid="stVerticalBlock"] { gap: .42rem; }
div[data-testid="stVerticalBlockBorderWrapper"] p { font-size: 1.02rem; line-height: 1.62; margin-bottom: .45rem; }
div[data-testid="stPlotlyChart"] { overflow: hidden; border: 1px solid #E1E7EE; border-radius: 12px; background: #FFF; box-shadow: 0 2px 8px rgba(23,32,51,.04); }
@media (max-width: 1100px) {
  div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetric"]) { flex-wrap: wrap; }
  div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetric"]) > div[data-testid="stColumn"] { flex: 1 1 calc(50% - .5rem) !important; min-width: calc(50% - .5rem) !important; }
}
@media (max-width: 900px) {
  div[data-testid="stHorizontalBlock"]:has(div[data-testid="stPlotlyChart"]) { flex-wrap: wrap; }
  div[data-testid="stHorizontalBlock"]:has(div[data-testid="stPlotlyChart"]) > div[data-testid="stColumn"] { flex: 1 1 100% !important; min-width: 100% !important; }
}
@media (max-width: 640px) {
  div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetric"]) > div[data-testid="stColumn"] { flex-basis: 100% !important; min-width: 100% !important; }
}
</style>
        """
    )
