"""Chinese weekly soybean crop comparison page for the main workspace."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "03_src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agri_research_agent.pipelines.soybean_crop_comparison import (  # noqa: E402
    build_dashboard_comparisons,
    load_display_config,
    style_comparison_table,
)
from agri_research_agent.summary_engine.crop import build_crop_summary  # noqa: E402
from agri_research_agent.summary_engine.io import file_identity  # noqa: E402
from summary_panel import render_summary_panel  # noqa: E402


PROCESSED_DIR = (
    PROJECT_ROOT / "01_data" / "processed" / "soybean_crop_progress"
)
PROGRESS_FILE = PROCESSED_DIR / "soybeans_crop_progress_weekly.parquet"
CONDITION_FILE = PROCESSED_DIR / "soybeans_crop_condition_weekly.parquet"
LEGACY_PROGRESS_FILE = (
    PROCESSED_DIR / "soybeans_crop_progress_weekly_2021_2026.parquet"
)
LEGACY_CONDITION_FILE = (
    PROCESSED_DIR / "soybeans_crop_condition_weekly_2021_2026.parquet"
)
DISPLAY_CONFIG_FILE = (
    PROJECT_ROOT / "02_configs" / "soybean_crop_progress_display.yaml"
)
MATCHING_METHODOLOGY_NOTE = (
    "累计生长进度优先采用精确同期；精确日期缺失时，采用此前7天内最近一次USDA官方记录。"
    "优良率仅采用精确同期。"
)


@st.cache_data(show_spinner=False)
def load_processed_crop_data(
    progress_path: str,
    progress_mtime_ns: int,
    condition_path: str,
    condition_mtime_ns: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read approved Parquets; mtimes are part of the cache key."""

    del progress_mtime_ns, condition_mtime_ns
    return pd.read_parquet(progress_path), pd.read_parquet(condition_path)


@st.cache_data(show_spinner=False)
def load_crop_display_config(
    config_path: str, config_mtime_ns: int
) -> dict[str, object]:
    """Read the display-only ordering and translation config."""

    del config_mtime_ns
    return load_display_config(config_path)


def resolve_processed_crop_paths(
    config: dict[str, object],
    *,
    processed_dir: Path = PROCESSED_DIR,
) -> tuple[Path, Path]:
    """Select the stable pair together, otherwise use the legacy pair together."""

    data_files = config["data_files"]
    if not isinstance(data_files, dict):
        raise ValueError("美豆页面数据文件配置无效")

    def configured_path(family: str, role: str) -> Path:
        selection = data_files[family]
        if not isinstance(selection, dict):
            raise ValueError(f"美豆页面{family}数据文件配置无效")
        name = str(selection[role])
        path = (processed_dir / name).resolve()
        if path.parent != processed_dir.resolve():
            raise ValueError("美豆页面数据文件必须位于正式 Processed 目录")
        return path

    preferred = (
        configured_path("progress", "preferred"),
        configured_path("condition", "preferred"),
    )
    if all(path.is_file() for path in preferred):
        return preferred
    return (
        configured_path("progress", "fallback"),
        configured_path("condition", "fallback"),
    )


def _require_page_inputs(progress_file: Path, condition_file: Path) -> bool:
    missing = [
        path
        for path in (progress_file, condition_file, DISPLAY_CONFIG_FILE)
        if not path.exists()
    ]
    if missing:
        st.error("缺少美豆种植生长页面所需文件：" + "、".join(str(path) for path in missing))
        return False
    return True


def render_soybean_crop_progress_page() -> None:
    """Render six metric tabs inside the existing 8501 workspace."""

    st.title("美豆种植生长")
    st.caption(
        "美国全国值直接采用 USDA US TOTAL；州权重仅用于默认排序和名称展示，不参与全国值计算。"
    )
    if not DISPLAY_CONFIG_FILE.exists():
        st.error(f"缺少美豆种植生长页面所需文件：{DISPLAY_CONFIG_FILE}")
        return

    config = load_crop_display_config(
        str(DISPLAY_CONFIG_FILE), DISPLAY_CONFIG_FILE.stat().st_mtime_ns
    )
    progress_file, condition_file = resolve_processed_crop_paths(config)
    if not _require_page_inputs(progress_file, condition_file):
        return
    progress, condition = load_processed_crop_data(
        str(progress_file),
        progress_file.stat().st_mtime_ns,
        str(condition_file),
        condition_file.stat().st_mtime_ns,
    )
    comparisons = build_dashboard_comparisons(progress, condition, config)
    try:
        render_summary_panel(build_crop_summary(progress, condition, source_identity={
            "progress": file_identity(progress_file), "condition": file_identity(condition_file)
        }, display_config=config))
    except (OSError, ValueError) as exc:
        st.warning(f"美豆种植生长摘要暂不可用：{exc}")

    tabs = st.tabs([comparison.definition.tab_label for comparison in comparisons])
    for tab, comparison in zip(tabs, comparisons, strict=True):
        with tab:
            st.subheader(comparison.definition.display_name)
            if comparison.baseline_week is None:
                latest = comparison.latest_historical_week
                latest_text = latest.strftime("%Y-%m-%d") if latest is not None else "—"
                st.info(
                    f"{comparison.current_year}年度{comparison.definition.display_name}尚未发布。\n\n"
                    f"当前历史数据最新截至{latest_text}。"
                )
                continue

            st.caption(f"数据截至：{comparison.baseline_week.strftime('%Y-%m-%d')}")
            st.markdown(
                style_comparison_table(comparison).to_html(),
                unsafe_allow_html=True,
            )
            st.caption(MATCHING_METHODOLOGY_NOTE)
