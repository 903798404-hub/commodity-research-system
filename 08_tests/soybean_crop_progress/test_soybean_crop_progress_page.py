from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FORMAL_ENTRY = PROJECT_ROOT / "05_apps" / "streamlit_app.py"
APPS_DIR = PROJECT_ROOT / "05_apps"
MATCHING_METHODOLOGY_NOTE = (
    "累计生长进度优先采用精确同期；精确日期缺失时，采用此前7天内最近一次USDA官方记录。"
    "优良率仅采用精确同期。"
)


def test_main_workspace_renders_six_soybean_tabs_and_harvested_empty_state() -> None:
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=30).run()
    app.session_state["selected_workspace_page"] = "美豆种植生长"
    app.run(timeout=30)

    assert not app.exception
    assert [tab.label for tab in app.tabs] == [
        "播种率",
        "出苗率",
        "开花率",
        "结荚率",
        "收割率",
        "优良率",
    ]
    assert any(title.value == "美豆种植生长" for title in app.title)
    metric_subheaders = [
        subheader.value
        for subheader in app.subheader
        if subheader.value.startswith("美豆")
    ]
    assert metric_subheaders == [
        "美豆播种率",
        "美豆出苗率",
        "美豆开花率",
        "美豆结荚率",
        "美豆收割率",
        "美豆优良率",
    ]
    rendered_tables = [
        item.value for item in app.markdown if "soybean-comparison-table" in item.value
    ]
    assert len(rendered_tables) == 5
    assert any("2026年度美豆收割率尚未发布" in item.value for item in app.info)
    assert any("当前历史数据最新截至2025-11-16" in item.value for item in app.info)
    matching_notes = [
        item.value for item in app.caption if item.value == MATCHING_METHODOLOGY_NOTE
    ]
    assert len(matching_notes) == 5


def test_parquet_cache_key_changes_when_file_mtime_changes(tmp_path: Path) -> None:
    if str(APPS_DIR) not in sys.path:
        sys.path.insert(0, str(APPS_DIR))
    from soybean_crop_progress_page import load_processed_crop_data

    first_path = tmp_path / "progress.parquet"
    second_path = tmp_path / "condition.parquet"
    pd.DataFrame({"value": [1]}).to_parquet(first_path, index=False)
    pd.DataFrame({"value": [2]}).to_parquet(second_path, index=False)
    first, _ = load_processed_crop_data(
        str(first_path),
        first_path.stat().st_mtime_ns,
        str(second_path),
        second_path.stat().st_mtime_ns,
    )

    pd.DataFrame({"value": [9]}).to_parquet(first_path, index=False)
    current_mtime = first_path.stat().st_mtime_ns
    os.utime(first_path, ns=(current_mtime + 1_000_000, current_mtime + 1_000_000))
    updated, _ = load_processed_crop_data(
        str(first_path),
        first_path.stat().st_mtime_ns,
        str(second_path),
        second_path.stat().st_mtime_ns,
    )

    assert first.loc[0, "value"] == 1
    assert updated.loc[0, "value"] == 9


def test_stable_pair_is_preferred_and_legacy_pair_is_the_fallback(
    tmp_path: Path,
) -> None:
    if str(APPS_DIR) not in sys.path:
        sys.path.insert(0, str(APPS_DIR))
    from soybean_crop_progress_page import resolve_processed_crop_paths

    config = {
        "data_files": {
            "progress": {
                "preferred": "progress.parquet",
                "fallback": "progress_legacy.parquet",
            },
            "condition": {
                "preferred": "condition.parquet",
                "fallback": "condition_legacy.parquet",
            },
        }
    }
    legacy_progress = tmp_path / "progress_legacy.parquet"
    legacy_condition = tmp_path / "condition_legacy.parquet"
    pd.DataFrame({"value": [1]}).to_parquet(legacy_progress, index=False)
    pd.DataFrame({"value": [2]}).to_parquet(legacy_condition, index=False)

    assert resolve_processed_crop_paths(config, processed_dir=tmp_path) == (
        legacy_progress,
        legacy_condition,
    )

    stable_progress = tmp_path / "progress.parquet"
    stable_condition = tmp_path / "condition.parquet"
    pd.DataFrame({"value": [3]}).to_parquet(stable_progress, index=False)
    assert resolve_processed_crop_paths(config, processed_dir=tmp_path) == (
        legacy_progress,
        legacy_condition,
    )
    pd.DataFrame({"value": [4]}).to_parquet(stable_condition, index=False)
    assert resolve_processed_crop_paths(config, processed_dir=tmp_path) == (
        stable_progress,
        stable_condition,
    )
