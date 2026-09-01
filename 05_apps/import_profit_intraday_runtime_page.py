"""Local-asset runtime entry for the soybean AM/PM page."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import streamlit as st

from agri_research_agent.import_profit import (
    ImportProfitConfigError,
    load_soybean_config,
)
from agri_research_agent.import_profit.runtime_store import (
    RuntimeStoreError,
    resolve_current_runtime_release,
)
from agri_research_agent.pipelines.soybean_intraday import (
    save_manual_cnf_and_materialize_am,
)
from import_profit_intraday_page import (
    FinalUiPreviewPaths,
    IntradayPageDataPaths,
    IntradayPageMode,
    render_import_profit_intraday_page,
)


def render_import_profit_intraday_runtime_page(
    runtime_root: str | Path | None,
    *,
    result_root: str | Path | None,
    snapshot_root: str | Path | None = None,
    config_path: str | Path,
    page_mode: str = IntradayPageMode.STRICT_RUNTIME.value,
    environment: str = "FORMAL",
    preview_historical_cnf_path: str | Path | None = None,
    intraday_cnf_store_path: str | Path | None = None,
    allow_cnf_save: bool = False,
) -> None:
    """Resolve CNF identity locally and render without loading legacy results."""

    if runtime_root is None or not str(runtime_root).strip():
        st.info("运行数据尚未配置。")
        return
    if result_root is None or not str(result_root).strip():
        st.info("AM/PM 结果尚未配置。")
        return
    runtime = Path(runtime_root)
    results = Path(result_root)
    snapshots = Path(snapshot_root) if snapshot_root and str(snapshot_root).strip() else None
    config_file = Path(config_path)
    if not runtime.is_dir() or not results.is_dir():
        st.error("AM/PM 页面本地运行资产不可读取。")
        return
    try:
        resolved = resolve_current_runtime_release(runtime)
        config = load_soybean_config(config_file)
    except (RuntimeStoreError, ImportProfitConfigError, OSError):
        st.error("AM/PM 页面本地资产校验失败。")
        return
    cnf_store = (
        Path(intraday_cnf_store_path)
        if intraday_cnf_store_path and str(intraday_cnf_store_path).strip()
        else resolved.manual_cnf_path
    )
    try:
        mode = IntradayPageMode(page_mode)
    except ValueError:
        st.error("AM/PM 页面模式无效。")
        return
    preview = None
    if mode is IntradayPageMode.FINAL_UI_PREVIEW:
        if environment != "TEST_ISOLATED_NON_PRODUCTION":
            st.error(
                "FINAL_UI_PREVIEW 只能运行在 TEST_ISOLATED_NON_PRODUCTION。"
            )
            return
    if preview_historical_cnf_path is None:
        st.error("页面历史 CNF 只读 Cache 未配置。")
        return
    if preview is None:
        preview = FinalUiPreviewPaths(
            historical_cnf_cache_path=Path(preview_historical_cnf_path),
            historical_business_keys_path=resolved.business_keys_path,
            historical_snapshots_path=resolved.snapshots_path,
            historical_profit_path=resolved.results_path,
            historical_profit_sha256=str(
                resolved.manifest["output_files"][
                    resolved.results_path.name
                ]["sha256"]
            ),
            historical_as_of_date=date.fromisoformat(
                str(resolved.manifest["date_range"][1])
            ),
        )
    save_handler = None
    if allow_cnf_save:
        if (
            mode is not IntradayPageMode.FINAL_UI_PREVIEW
            or environment != "TEST_ISOLATED_NON_PRODUCTION"
            or snapshots is None
        ):
            st.error("manual_ui 保存环境未满足隔离运行要求。")
            return

        def save_handler(values):
            return save_manual_cnf_and_materialize_am(
                snapshot_root=snapshots,
                result_root=results,
                cnf_store_path=cnf_store,
                business_date=date(2026, 8, 31),
                values=values,
                config=config,
            )

    render_import_profit_intraday_page(
        IntradayPageDataPaths(
            results,
            cnf_store,
            snapshots,
            preview,
        ),
        config=config,
        mode=mode,
        save_cnf_handler=save_handler,
    )


__all__ = ["render_import_profit_intraday_runtime_page"]
