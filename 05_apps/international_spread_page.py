"""International-spread V1 page backed only by sealed contract payloads."""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from html import escape
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from agri_research_agent.application.international_spreads import (
    InternationalSpreadPayload,
    InternationalSpreadReferenceError,
    MetricPayload,
    MetricStatus,
    build_international_spread_payload,
    load_international_spread_reference_records,
    resolve_international_spread_snapshot,
)
from agri_research_agent.research_data.canonical_spreads import CanonicalSpreadError
from agri_research_agent.research_data.three_oil_v1 import load_three_oil_v1


PAGE_TITLE = "国际价差"
OIL_OPTIONS = {"棕榈油": "palm", "豆油": "soy", "菜油": "rape"}
REFERENCE_ROOT_ENV = "INTERNATIONAL_SPREAD_REFERENCE_DATA_ROOT"
LEGACY_REFERENCE_ROOT_ENV = "SPREAD_REFERENCE_DATA_ROOT"
CURRENT_YEAR_COLOR = "#C1493F"
MONTH_TICKS = [datetime(2000, month, 1) for month in range(1, 13)]
MONTH_LABELS = [f"{month}月" for month in range(1, 13)]


def _reference_root(project_root: Path) -> Path:
    configured = (
        os.getenv(REFERENCE_ROOT_ENV, "").strip()
        or os.getenv(LEGACY_REFERENCE_ROOT_ENV, "").strip()
    )
    return Path(configured) if configured else project_root


@st.cache_resource(show_spinner=False)
def _cached_reference_records(
    reference_root: str,
    source_path: str,
    source_size: int,
    source_mtime_ns: int,
):
    del source_path, source_size, source_mtime_ns
    catalog = load_three_oil_v1()
    return load_international_spread_reference_records(catalog, reference_root)


@st.cache_data(show_spinner=False)
def _cached_page_payload(
    oil: str,
    reference_root: str,
    source_path: str,
    source_size: int,
    source_mtime_ns: int,
) -> InternationalSpreadPayload:
    catalog = load_three_oil_v1()
    records = _cached_reference_records(
        reference_root, source_path, source_size, source_mtime_ns
    )
    return build_international_spread_payload(catalog, oil, records)


def load_international_spread_payload(
    oil: str, *, project_root: str | Path
) -> InternationalSpreadPayload:
    """Resolve the approved reference identity and cache one selected oil payload."""

    catalog = load_three_oil_v1()
    reference_root = _reference_root(Path(project_root)).resolve(strict=True)
    source = resolve_international_spread_snapshot(catalog, reference_root)
    stat = source.stat()
    return _cached_page_payload(
        oil,
        str(reference_root),
        str(source),
        stat.st_size,
        stat.st_mtime_ns,
    )


def build_seasonality_figure(metric: MetricPayload) -> go.Figure:
    """Render calendar-year observations without filling or altering any value."""

    figure = go.Figure()
    for year in (2021, 2022, 2023, 2024, 2025, 2026):
        points = tuple(item for item in metric.observations if item.year == year)
        if not points:
            continue
        is_current = year == 2026
        figure.add_trace(
            go.Scatter(
                x=[_reference_date(item.business_date) for item in points],
                y=[float(item.value) for item in points],
                mode="lines",
                name="2026 YTD" if is_current else str(year),
                line={
                    # Historical years intentionally inherit the same Streamlit
                    # categorical colorway used by the soybean month-spread page.
                    "color": CURRENT_YEAR_COLOR if is_current else None,
                    "width": 3.4 if is_current else 2.0,
                },
                opacity=1,
                connectgaps=False,
                customdata=[item.business_date.isoformat() for item in points],
                hovertemplate=(
                    "%{customdata}<br>"
                    + metric.display_title
                    + "：%{y:,.2f} "
                    + metric.display_unit
                    + "<extra></extra>"
                ),
            )
        )
    figure.update_layout(
        height=260,
        margin={"l": 42, "r": 8, "t": 30, "b": 30},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
        hovermode="x unified",
        legend={
            "orientation": "h",
            "x": 0.5,
            "y": 1.01,
            "xanchor": "center",
            "yanchor": "bottom",
            "font": {"size": 9},
        },
        xaxis={
            "tickmode": "array",
            "tickvals": MONTH_TICKS,
            "ticktext": MONTH_LABELS,
            "tickfont": {"size": 9},
            "showgrid": False,
            "showline": True,
            "linecolor": "#B7C0C9",
            "range": [datetime(2000, 1, 1), datetime(2000, 12, 31)],
        },
        yaxis={
            "tickfont": {"size": 9},
            "gridcolor": "#E1E5E9",
            "showline": True,
            "linecolor": "#B7C0C9",
            "zeroline": True,
            "zerolinecolor": "#8E99A5",
            "zerolinewidth": 1,
        },
    )
    return figure


def render_international_spread_page(*, project_root: str | Path) -> None:
    _inject_page_style()
    st.title(PAGE_TITLE)
    st.caption("基于 sealed canonical contracts 的全球油脂相对价值季节性研究")
    selected_label = st.segmented_control(
        "油种",
        options=list(OIL_OPTIONS),
        default="棕榈油",
        selection_mode="single",
        label_visibility="collapsed",
        key="international_spread_oil",
    )
    oil = OIL_OPTIONS.get(selected_label or "棕榈油", "palm")
    try:
        with st.spinner("正在准备国际价差只读数据…"):
            payload = load_international_spread_payload(
                oil, project_root=project_root
            )
    except InternationalSpreadReferenceError:
        st.warning("国际价差只读参考数据暂不可用。")
        return
    except CanonicalSpreadError:
        st.error("国际价差 sealed contract 完整性校验失败，页面已停止加载。")
        return

    _render_freshness(payload)
    for section_index, section in enumerate(payload.sections):
        if oil == "soy":
            heading = "核心相对价值" if section_index == 0 else "基差、环保信用与盘面比较"
            st.markdown(
                f'<div class="international-spread-section">{escape(heading)}</div>',
                unsafe_allow_html=True,
            )
        for row in section.rows:
            columns = st.columns(len(row.metrics), gap="small")
            for column, metric in zip(columns, row.metrics, strict=False):
                with column:
                    _render_metric_card(metric)


def _render_freshness(payload: InternationalSpreadPayload) -> None:
    as_of = payload.as_of_date.isoformat() if payload.as_of_date else "暂无有效数据"
    st.markdown(
        '<div class="international-spread-freshness">'
        f'数据截至 {as_of} ｜ {escape(payload.source_summary)} ｜ '
        f'{escape(payload.acquisition_summary)}'
        "</div>",
        unsafe_allow_html=True,
    )
    if len(payload.metric_latest_dates) > 1:
        st.caption("不同指标截至日期可能不同；每张图保留自身最新有效日期。")


def _render_metric_card(metric: MetricPayload) -> None:
    with st.container(border=True):
        st.markdown(_metric_heading(metric), unsafe_allow_html=True)
        _render_provenance(metric)
        if metric.status in (MetricStatus.READY, MetricStatus.STALE):
            st.plotly_chart(
                build_seasonality_figure(metric),
                width="stretch",
                config={"displayModeBar": False, "responsive": True},
                key=f"international_spread_{metric.contract_id}",
            )
        else:
            label = (
                "数据核查中"
                if metric.status is MetricStatus.SOURCE_DATA_UNDER_REVIEW
                else "暂无可展示数据"
            )
            st.markdown(
                '<div class="international-spread-empty">'
                f"{escape(label)}</div>",
                unsafe_allow_html=True,
            )
        if metric.status is MetricStatus.STALE:
            st.caption("数据晚于合同预期更新日，展示最近有效观测。")


def _render_provenance(metric: MetricPayload) -> None:
    with st.popover("口径", type="tertiary", width="content"):
        latest = (
            metric.latest_observation_date.isoformat()
            if metric.latest_observation_date
            else "—"
        )
        st.markdown(
            f"**公式/类型**：{metric.formula_summary}  \n"
            f"**来源**：{metric.provider_summary}  \n"
            f"**单位**：{metric.display_unit}  \n"
            f"**最新观测**：{latest}"
        )
        if metric.leg_summary:
            st.markdown("**组成**：  \n" + "  \n".join(metric.leg_summary))
        for assumption in metric.fixed_assumptions:
            st.markdown(f"**固定业务假设**：{assumption}")
        st.markdown(f"**定义依据**：{metric.definition_evidence}")
        st.caption(metric.quality_summary)


def _metric_heading(metric: MetricPayload) -> str:
    under_review = metric.status is MetricStatus.SOURCE_DATA_UNDER_REVIEW
    if under_review:
        title = f"{metric.display_title}｜数据核查中"
        date_label = "源数据核查中"
    else:
        value = _format_latest_value(metric.latest_value, metric.display_unit)
        suffix = f"｜{value} {metric.display_unit}" if value is not None else ""
        title = f"{metric.display_title}{suffix}"
        date_label = (
            f"截至 {metric.latest_observation_date.isoformat()}"
            if metric.latest_observation_date
            else "暂无有效观测"
        )
    return (
        '<div class="international-spread-chart-header">'
        f'<div class="international-spread-chart-title" data-title="{escape(title)}">'
        f"{escape(title)}</div>"
        f'<div class="international-spread-chart-date">{escape(date_label)}</div>'
        "</div>"
    )


def _format_latest_value(value: Decimal | None, unit: str) -> str | None:
    if value is None:
        return None
    decimals = 2 if unit in {"USC/LB", "USD/GAL"} else 1
    return f"{value:,.{decimals}f}"


def _reference_date(value: date) -> datetime:
    return datetime(2000, value.month, value.day)


def _inject_page_style() -> None:
    st.html(
        """
<style>
[data-testid="stMainBlockContainer"] {
  max-width: none;
  padding-top: 1.65rem;
  padding-left: 1.2rem;
  padding-right: 1.2rem;
}
[data-testid="stMainBlockContainer"] h1 {
  margin-bottom: .05rem;
  font-size: 1.75rem;
}
[data-testid="stHorizontalBlock"] {
  gap: .55rem;
}
[data-testid="stVerticalBlock"]:has(.international-spread-chart-header) {
  position: relative;
  gap: 0 !important;
  padding: .38rem .45rem .3rem !important;
  border-color: #CBD2D9 !important;
  border-radius: 2px !important;
  box-shadow: none !important;
}
.international-spread-section {
  margin: .42rem 0 .16rem;
  padding-bottom: .15rem;
  border-bottom: 1px solid #AEB7C0;
  color: #263746;
  font-size: .92rem;
  font-weight: 650;
}
.international-spread-freshness {
  margin: .1rem 0 .28rem;
  color: #68737D;
  font-size: .78rem;
  line-height: 1.2;
}
.international-spread-chart-header {
  min-height: 3.05rem;
  padding: 0 3rem;
  display: flex;
  flex-direction: column;
  justify-content: center;
  text-align: center;
}
.international-spread-chart-title {
  color: #243543;
  font-size: .91rem;
  font-weight: 650;
  line-height: 1.25;
}
.international-spread-chart-date {
  margin-top: .12rem;
  color: #7A838C;
  font-size: .69rem;
  line-height: 1.15;
}
.international-spread-empty {
  height: 260px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-top: 1px solid #E0E4E8;
  color: #8A939B;
  font-size: .82rem;
  letter-spacing: .04em;
}
[data-testid="stVerticalBlock"]:has(.international-spread-chart-header) > [data-testid="stLayoutWrapper"]:has(> [data-testid="stPopover"]) {
  position: absolute;
  top: .45rem;
  right: .45rem;
  z-index: 2;
}
[data-testid="stPopover"] button {
  min-height: 1.65rem;
  padding: .1rem .28rem;
  color: #68737D;
  font-size: .7rem;
}
[data-testid="stPlotlyChart"] {
  margin-top: -.15rem;
}
</style>
"""
    )
