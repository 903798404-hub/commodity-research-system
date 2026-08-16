"""International-spread V1 page backed only by sealed contract payloads."""

from __future__ import annotations

import os
from datetime import date, datetime
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
YEAR_COLORS = {
    2021: "#91A7C4",
    2022: "#7895B8",
    2023: "#5F83AC",
    2024: "#3F6F9E",
    2025: "#244F7C",
    2026: "#C1493F",
}
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
                    "color": YEAR_COLORS[year],
                    "width": 3.5 if is_current else 1.6,
                },
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
        height=300,
        margin={"l": 48, "r": 12, "t": 10, "b": 42},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#FFFFFF",
        hovermode="x unified",
        legend={
            "orientation": "h",
            "x": 0,
            "y": 1.02,
            "xanchor": "left",
            "yanchor": "bottom",
            "font": {"size": 10},
        },
        xaxis={
            "tickmode": "array",
            "tickvals": MONTH_TICKS,
            "ticktext": MONTH_LABELS,
            "tickfont": {"size": 10},
            "showgrid": False,
            "range": [datetime(2000, 1, 1), datetime(2000, 12, 31)],
        },
        yaxis={
            "title": {"text": metric.display_unit, "font": {"size": 11}},
            "tickfont": {"size": 10},
            "gridcolor": "#E8EDF3",
            "zerolinecolor": "#CAD3DE",
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
            st.subheader(heading)
        for row in section.rows:
            columns = st.columns(len(row.metrics), gap="small")
            for column, metric in zip(columns, row.metrics, strict=False):
                with column:
                    _render_metric_card(metric)


def _render_freshness(payload: InternationalSpreadPayload) -> None:
    as_of = payload.as_of_date.isoformat() if payload.as_of_date else "暂无有效数据"
    st.markdown(
        '<div class="international-spread-freshness">'
        f'<strong>数据截至：{as_of}</strong>'
        f'<span>数据来源：{payload.source_summary}</span>'
        f'<span>更新方式：{payload.acquisition_summary}</span>'
        "</div>",
        unsafe_allow_html=True,
    )
    if len(payload.metric_latest_dates) > 1:
        st.caption("不同指标截至日期可能不同；每张图保留自身最新有效日期。")


def _render_metric_card(metric: MetricPayload) -> None:
    with st.container(border=True):
        st.markdown(f"#### {metric.display_title}")
        latest = (
            metric.latest_observation_date.isoformat()
            if metric.latest_observation_date
            else "—"
        )
        st.caption(f"{metric.display_unit} · 最新有效日期 {latest}")
        if metric.status is MetricStatus.READY:
            st.plotly_chart(
                build_seasonality_figure(metric),
                width="stretch",
                config={"displayModeBar": False, "responsive": True},
                key=f"international_spread_{metric.contract_id}",
            )
        elif metric.status is MetricStatus.STALE:
            st.warning("数据晚于合同预期更新日，当前展示截至最近有效日期。")
            st.plotly_chart(
                build_seasonality_figure(metric),
                width="stretch",
                config={"displayModeBar": False, "responsive": True},
                key=f"international_spread_{metric.contract_id}",
            )
        elif metric.status is MetricStatus.NO_DATA:
            st.info("暂无可展示数据。")
        else:
            st.warning(metric.quality_summary)
        with st.expander("来源与口径", expanded=False):
            st.markdown(f"**来源**：{metric.provider_summary}")
            st.markdown(f"**公式/类型**：{metric.formula_summary}")
            if metric.leg_summary:
                st.markdown("**组成**：  \n" + "  \n".join(metric.leg_summary))
            for assumption in metric.fixed_assumptions:
                st.markdown(f"**固定业务假设**：{assumption}")
            st.caption(metric.quality_summary)


def _reference_date(value: date) -> datetime:
    return datetime(2000, value.month, value.day)


def _inject_page_style() -> None:
    st.html(
        """
<style>
[data-testid="stVerticalBlockBorderWrapper"] {
  border-color: #E1E7EE;
  border-radius: 12px;
  box-shadow: 0 2px 8px rgba(23, 32, 51, .045);
}
[data-testid="stVerticalBlockBorderWrapper"] h4 {
  min-height: 2.8em;
  margin-bottom: .15rem;
  color: #172C43;
  font-size: 1rem;
  line-height: 1.4;
}
.international-spread-freshness {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: .45rem 1.25rem;
  margin: .2rem 0 .35rem;
  padding: .7rem .9rem;
  border: 1px solid #DCE5ED;
  border-radius: 10px;
  background: #F7FAFC;
  color: #40556B;
  font-size: .9rem;
}
.international-spread-freshness strong {
  color: #173A5E;
}
</style>
"""
    )
