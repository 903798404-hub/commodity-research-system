"""Local-asset runtime entry for the soybean AM/PM page."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import os
from zoneinfo import ZoneInfo

import streamlit as st

from agri_research_agent.import_profit import (
    ImportProfitConfigError,
    load_soybean_config,
)
from agri_research_agent.import_profit.runtime_store import (
    RuntimeStoreError,
    resolve_current_runtime_release,
)
from agri_research_agent.import_profit.intraday_store import (
    load_intraday_profit_batch, IntradayProfitStoreNotFoundError,
)
from agri_research_agent.market_data.intraday import MarketSession
from agri_research_agent.pipelines.soybean_intraday import (
    save_manual_cnf_and_materialize_am,
)
from agri_research_agent.shared.runtime_context import (
    RuntimeContext, RuntimeMode,
)
from agri_research_agent.import_profit.operational_runtime import (
    configured_operational_write, validate_operational_write,
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
    operational_result_root: str | Path | None = None,
    allow_cnf_save: bool = False,
    business_date: date | None = None,
    write_context: RuntimeContext | None = None,
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
    if not runtime.is_dir() or (results.exists() and not results.is_dir()):
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
    selected_date = business_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    if type(selected_date) is not date:
        st.error("业务日期无效。")
        return
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
            business_date=selected_date,
            historical_profit_sha256=str(
                resolved.manifest["output_files"][
                    resolved.results_path.name
                ]["sha256"]
            ),
            historical_as_of_date=date.fromisoformat(
                str(resolved.manifest["date_range"][1])
            ),
        )
    am_results = Path(operational_result_root) if operational_result_root else results
    save_handler = None
    if allow_cnf_save:
        if (
            mode is not IntradayPageMode.STRICT_RUNTIME
            or snapshots is None
            or write_context is None
            or write_context.module_id not in {"soybean-pm", "shared-intraday"}
            or environment != ("FORMAL" if write_context.mode in {RuntimeMode.PRODUCTION_WRITE, RuntimeMode.CANDIDATE_VALIDATION}
                               else "TEST_ISOLATED_NON_PRODUCTION")
        ):
            st.error("manual_ui 保存环境未满足正式运行授权要求。")
            return

        validate_operational_write(write_context, cnf_store, am_results)

        def save_handler(values):
            validate_operational_write(write_context, cnf_store, am_results)
            if results != am_results:
                try:
                    load_intraday_profit_batch(results, selected_date, MarketSession.AM)
                except IntradayProfitStoreNotFoundError:
                    pass
                else:
                    raise RuntimeError("AM result is already sealed in readonly history")
            return save_manual_cnf_and_materialize_am(
                snapshot_root=snapshots,
                result_root=am_results,
                cnf_store_path=cnf_store,
                business_date=selected_date,
                values=values,
                config=config,
            )

    render_import_profit_intraday_page(
        IntradayPageDataPaths(
            results,
            cnf_store,
            snapshots,
            preview,
            business_date=selected_date,
            environment=environment,
            operational_result_root=Path(operational_result_root) if operational_result_root else None,
            historical_cnf_store_path=resolved.manual_cnf_path,
        ),
        config=config,
        mode=mode,
        save_cnf_handler=save_handler,
    )


def render_configured_intraday_runtime_page(runtime_root, *, config_path, allow_save=True):
    """PM-owned route adapter; the shared Streamlit router remains unchanged."""
    def setting(name, default=""):
        return os.getenv(name, default).strip()
    write_context = None
    enabled = allow_save and setting("IMPORT_PROFIT_INTRADAY_ALLOW_CNF_SAVE", "0") == "1"
    try:
        selected = setting("IMPORT_PROFIT_INTRADAY_BUSINESS_DATE")
        business_date = date.fromisoformat(selected) if selected else None
        if enabled:
            write_context = configured_operational_write()
    except (ValueError, OSError, RuntimeError):
        st.error("AM/PM 业务日期或写入运行身份校验失败。")
        return
    render_import_profit_intraday_runtime_page(runtime_root,
        result_root=setting("IMPORT_PROFIT_INTRADAY_RESULT_ROOT") or None,
        snapshot_root=setting("IMPORT_PROFIT_INTRADAY_SNAPSHOT_ROOT") or None,
        config_path=config_path,
        page_mode=setting("IMPORT_PROFIT_INTRADAY_PAGE_MODE", "STRICT_RUNTIME"),
        environment=setting("IMPORT_PROFIT_INTRADAY_ENVIRONMENT", "FORMAL"),
        preview_historical_cnf_path=setting("IMPORT_PROFIT_PREVIEW_HISTORICAL_CNF_PATH") or None,
        intraday_cnf_store_path=setting("IMPORT_PROFIT_INTRADAY_CNF_STORE_PATH") or None,
        operational_result_root=setting("IMPORT_PROFIT_INTRADAY_AM_RESULT_ROOT") or None,
        allow_cnf_save=enabled, business_date=business_date, write_context=write_context)


__all__ = ["render_import_profit_intraday_runtime_page", "render_configured_intraday_runtime_page"]
