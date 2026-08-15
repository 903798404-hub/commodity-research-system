from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from agri_research_agent.summary_engine.basis import build_basis_summary
from agri_research_agent.summary_engine.crop import build_crop_summary
from agri_research_agent.summary_engine.io import file_identity
from agri_research_agent.summary_engine.weather_cache import load_weather_summary_cached
from agri_research_agent.soybean_exports.research import (
    format_soybean_export_weekly_observation,
)
from soybean_exports_page import load_current_export_page_payload
from summary_panel import render_summary_panel


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEATHER_GROUP_NAMES = (
    "\u5927\u8c46\u5929\u6c14",
    "\u83dc\u7c7d\u5929\u6c14",
    "\u68d5\u6988\u6cb9\u5929\u6c14",
    "\u5370\u5ea6\u4f5c\u7269\u5929\u6c14",
)
_LOAD_FROM_SOURCE = object()

WEATHER_OVERVIEW_SOURCES = (
    (WEATHER_GROUP_NAMES[0], "USA", "soybean/us/soybean_weather_us.parquet", "soybean_weather_us.yaml", "soybean/us/soybean_weather_us_30y_normal.parquet"),
    (WEATHER_GROUP_NAMES[0], "BRA", "soybean/br/soybean_weather_br.parquet", "soybean_weather_br.yaml", "soybean/br/soybean_weather_br_30y_normal.parquet"),
    (WEATHER_GROUP_NAMES[0], "ARG", "soybean/ar/soybean_weather_ar.parquet", "soybean_weather_ar.yaml", "soybean/ar/soybean_weather_ar_30y_normal.parquet"),
    (WEATHER_GROUP_NAMES[1], "CAN", "rapeseed/can/rapeseed_weather_can.parquet", "rapeseed_weather_can.yaml", "rapeseed/can/rapeseed_weather_can_30y_normal.parquet"),
    (WEATHER_GROUP_NAMES[1], "AUS", "rapeseed/aus/rapeseed_weather_aus.parquet", "rapeseed_weather_aus.yaml", None),
    (WEATHER_GROUP_NAMES[1], "EU", "rapeseed/eu/rapeseed_weather_eu.parquet", "rapeseed_weather_eu.yaml", None),
    (WEATHER_GROUP_NAMES[1], "RUS", "rapeseed/rus/rapeseed_weather_rus.parquet", "rapeseed_weather_rus.yaml", None),
    (WEATHER_GROUP_NAMES[1], "UKR", "rapeseed/ukr/rapeseed_weather_ukr.parquet", "rapeseed_weather_ukr.yaml", None),
    (WEATHER_GROUP_NAMES[2], "MYS", "palm_oil/mys/palm_oil_weather_mys.parquet", "palm_oil_weather_mys.yaml", None),
    (WEATHER_GROUP_NAMES[2], "IDN", "palm_oil/idn/palm_oil_weather_idn.parquet", "palm_oil_weather_idn.yaml", None),
    (WEATHER_GROUP_NAMES[3], "IND_COTTON", "cotton/ind/cotton_weather_ind.parquet", "cotton_weather_ind.yaml", None),
    (WEATHER_GROUP_NAMES[3], "IND_SUGARCANE", "sugarcane/ind/sugarcane_weather_ind.parquet", "sugarcane_weather_ind.yaml", None),
)


def _summary_payload(summary: object | None) -> dict[str, Any]:
    if summary is None:
        return {}
    if hasattr(summary, "to_dict"):
        return dict(summary.to_dict())
    return dict(summary)


def _summary_available(summary: object | None) -> bool:
    payload = _summary_payload(summary)
    return bool(
        payload
        and not payload.get("missing_reason")
        and str(payload.get("short_text") or "").strip()
    )


def load_weather_overview_summaries() -> tuple[dict[str, list[object]], list[str]]:
    groups = {name: [] for name in WEATHER_GROUP_NAMES}
    warnings: list[str] = []
    weather_root = Path(
        os.getenv("WEATHER_DATA_DIR", "").strip()
        or PROJECT_ROOT / "01_data/processed/weather"
    )
    for group, code, relative, config_name, normal_relative in WEATHER_OVERVIEW_SOURCES:
        path = weather_root / relative
        if not path.is_file():
            warnings.append(f"{code}\u5929\u6c14\u6458\u8981\u6682\u4e0d\u53ef\u7528")
            continue
        try:
            normal_path = weather_root / normal_relative if normal_relative else None
            summary = load_weather_summary_cached(
                path,
                PROJECT_ROOT / "02_configs" / config_name,
                normal_path if normal_path is not None and normal_path.is_file() else None,
            )
            if _summary_available(summary):
                groups[group].append(summary)
            else:
                warnings.append(f"{code}\u5929\u6c14\u6458\u8981\u6682\u4e0d\u53ef\u7528")
        except (OSError, ValueError, KeyError, ImportError):
            warnings.append(f"{code}\u5929\u6c14\u6458\u8981\u6682\u4e0d\u53ef\u7528")
    return groups, warnings


def load_basis_overview_summary() -> object | None:
    basis_path = PROJECT_ROOT / "01_data/database/basis/basis_quotes.parquet"
    if not basis_path.is_file():
        return None
    return build_basis_summary(
        pd.read_parquet(basis_path),
        source_identity=file_identity(basis_path),
    )


def load_crop_overview_summary() -> object | None:
    crop_root = PROJECT_ROOT / "01_data/processed/soybean_crop_progress"
    progress = crop_root / "soybeans_crop_progress_weekly.parquet"
    condition = crop_root / "soybeans_crop_condition_weekly.parquet"
    if not progress.is_file():
        progress = crop_root / "soybeans_crop_progress_weekly_2021_2026.parquet"
    if not condition.is_file():
        condition = crop_root / "soybeans_crop_condition_weekly_2021_2026.parquet"
    if not progress.is_file() or not condition.is_file():
        return None
    return build_crop_summary(
        pd.read_parquet(progress),
        pd.read_parquet(condition),
        source_identity={
            "progress": file_identity(progress),
            "condition": file_identity(condition),
        },
    )


def load_research_overview_summaries() -> tuple[list[object], list[str]]:
    summaries: list[object] = []
    warnings: list[str] = []
    for label, loader in (
        ("\u57fa\u5dee", load_basis_overview_summary),
        ("\u7f8e\u8c46\u79cd\u690d\u751f\u957f", load_crop_overview_summary),
    ):
        try:
            summary = loader()
        except (OSError, ValueError, KeyError, ImportError):
            summary = None
        if _summary_available(summary):
            summaries.append(summary)
        else:
            warnings.append(f"{label}\u6458\u8981\u6682\u4e0d\u53ef\u7528")
    return summaries, warnings


def _render_weather_overview(
    groups: dict[str, list[object]] | None,
    warnings: list[str] | None = None,
) -> None:
    if not groups or not any(groups.values()):
        st.info("\u5929\u6c14\u6458\u8981\u6682\u4e0d\u53ef\u7528")
        return
    for group_name in WEATHER_GROUP_NAMES:
        group_items = [
            item for item in groups.get(group_name, []) if _summary_available(item)
        ]
        with st.expander(f"{group_name}\uff08{len(group_items)}\uff09", expanded=False):
            if not group_items:
                st.caption(f"{group_name}\u6458\u8981\u6682\u4e0d\u53ef\u7528")
                continue
            for summary in group_items:
                render_summary_panel(summary, compact=True)
    if warnings:
        st.caption("\u90e8\u5206\u5929\u6c14\u6458\u8981\u6682\u4e0d\u53ef\u7528")


def _render_single_overview(summary: object | None, unavailable_text: str) -> None:
    if not _summary_available(summary):
        st.info(unavailable_text)
        return
    render_summary_panel(summary, compact=True)


def _render_export_overview(payload: dict[str, Any] | None) -> None:
    if not payload or not (payload.get("fgis") or payload.get("fas")):
        st.info("美豆出口销售与装船摘要暂不可用")
        return
    for line in format_soybean_export_weekly_observation(payload):
        st.markdown(line)


def render_research_overview(
    *,
    weather_groups: dict[str, list[object]] | object = _LOAD_FROM_SOURCE,
    basis_summary: object | None = _LOAD_FROM_SOURCE,
    crop_summary: object | None = _LOAD_FROM_SOURCE,
    export_payload: dict[str, Any] | object = _LOAD_FROM_SOURCE,
) -> None:
    st.title("\u7814\u7a76\u5feb\u89c8 / \u6700\u65b0\u53d8\u5316")
    st.caption(
        "\u5404\u6a21\u5757\u6309\u81ea\u5df1\u7684\u4e1a\u52a1\u65e5\u671f\u5c55\u793a\uff1b"
        "\u672c\u9875\u53ea\u805a\u5408\u7ed3\u6784\u5316Summary\uff0c\u4e0d\u91cd\u65b0\u8ba1\u7b97\u4e1a\u52a1\u6570\u636e\u3002"
    )

    with st.container():
        st.subheader("\u5929\u6c14")
        weather_slot = st.container()
    with st.container():
        st.subheader("\u56fd\u5185\u57fa\u5dee")
        basis_slot = st.container()
    with st.container():
        st.subheader("\u7f8e\u8c46\u79cd\u690d\u751f\u957f")
        crop_slot = st.container()
    with st.container():
        st.subheader("美豆出口销售与装船")
        export_slot = st.container()

    # Fill the non-weather slots first while preserving their display positions.
    with basis_slot:
        try:
            resolved_basis = (
                load_basis_overview_summary()
                if basis_summary is _LOAD_FROM_SOURCE
                else basis_summary
            )
        except (OSError, ValueError, KeyError, ImportError):
            resolved_basis = None
        _render_single_overview(
            resolved_basis, "\u57fa\u5dee\u6458\u8981\u6682\u4e0d\u53ef\u7528"
        )

    with crop_slot:
        try:
            resolved_crop = (
                load_crop_overview_summary()
                if crop_summary is _LOAD_FROM_SOURCE
                else crop_summary
            )
        except (OSError, ValueError, KeyError, ImportError):
            resolved_crop = None
        _render_single_overview(
            resolved_crop,
            "\u7f8e\u8c46\u79cd\u690d\u751f\u957f\u6458\u8981\u6682\u4e0d\u53ef\u7528",
        )

    with export_slot:
        try:
            resolved_export = (
                load_current_export_page_payload()
                if export_payload is _LOAD_FROM_SOURCE
                else export_payload
            )
        except (OSError, ValueError, KeyError, ImportError):
            resolved_export = None
        _render_export_overview(resolved_export)

    with weather_slot:
        weather_warnings: list[str] = []
        try:
            if weather_groups is _LOAD_FROM_SOURCE:
                with st.spinner("天气摘要加载中…"):
                    resolved_weather, weather_warnings = load_weather_overview_summaries()
            else:
                resolved_weather = weather_groups
        except (OSError, ValueError, KeyError, ImportError):
            resolved_weather = None
        _render_weather_overview(resolved_weather, weather_warnings)
