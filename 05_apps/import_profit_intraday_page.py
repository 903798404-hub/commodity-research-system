"""Read-only AM/PM imported-soybean research page.

This module deliberately depends only on sealed local result and CNF stores; it
has no database or write-side pipeline dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
import hashlib
from html import escape
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from agri_research_agent.import_profit.cnf_store import load_cnf_store
from agri_research_agent.import_profit.config import SoybeanImportProfitConfig
from agri_research_agent.import_profit.historical_cnf_adapter import shipment_year_for
from agri_research_agent.import_profit.intraday import (
    select_soybean_intraday_market_inputs,
)
from agri_research_agent.import_profit.intraday_history import (
    IntradayProfitHistoryError,
    PmSeasonalChart,
    UnifiedSeasonalHistory,
    load_unified_seasonal_history,
)
from agri_research_agent.import_profit.models import BusinessKey
from agri_research_agent.import_profit.intraday_store import (
    IntradayProfitStoreError,
    IntradayProfitStoreNotFoundError,
    ResolvedIntradayProfitRelease,
    list_intraday_profit_batches,
    load_intraday_profit_batch,
    load_latest_intraday_profit_batch,
)
from agri_research_agent.market_data.intraday import MarketSession
from agri_research_agent.market_data.intraday import (
    IntradaySnapshotError,
    load_intraday_snapshot,
    load_latest_intraday_snapshot,
)


PAGE_TITLE = "中国进口大豆盘面榨利"
PREVIEW_CNF_SOURCE = "preview_session_state"
TANKAN_CNF_SOURCE = "tankan:quanyong.market.soybean_param:cnf"
SEASONAL_YEAR_COLORS = (
    "#D95F8D",
    "#1B9E77",
    "#66A61E",
    "#E6AB02",
    "#E67E22",
    "#1F77B4",
    "#8C564B",
    "#9467BD",
    "#17BECF",
    "#BCBD22",
    "#3366CC",
    "#A6761D",
)
LATEST_SEASONAL_YEAR_COLOR = "#D62728"


class IntradayPageMode(StrEnum):
    STRICT_RUNTIME = "STRICT_RUNTIME"
    FINAL_UI_PREVIEW = "FINAL_UI_PREVIEW"


@dataclass(frozen=True, slots=True)
class FinalUiPreviewPaths:
    historical_cnf_cache_path: Path
    historical_business_keys_path: Path
    historical_snapshots_path: Path
    historical_profit_path: Path
    business_date: date = date(2026, 8, 31)
    historical_profit_sha256: str = ""
    historical_as_of_date: date | None = None


@dataclass(frozen=True, slots=True)
class IntradayPageDataPaths:
    result_root: Path
    cnf_store_path: Path
    snapshot_root: Path | None = None
    preview: FinalUiPreviewPaths | None = None
    business_date: date | None = None
    environment: str | None = None
    operational_result_root: Path | None = None
    historical_cnf_store_path: Path | None = None


def render_import_profit_intraday_page(
    paths: IntradayPageDataPaths,
    *,
    config: SoybeanImportProfitConfig,
    mode: IntradayPageMode = IntradayPageMode.STRICT_RUNTIME,
    save_cnf_handler: Callable[
        [Mapping[tuple[str, int], float | None]], object
    ] | None = None,
) -> None:
    """Render both data modes through the accepted Final UI presentation."""

    _render_final_ui(
        paths,
        config=config,
        mode=mode,
        save_cnf_handler=save_cnf_handler,
    )


def _render_strict_runtime(
    paths: IntradayPageDataPaths,
    *,
    config: SoybeanImportProfitConfig,
) -> None:
    """Render only formally persisted CNF and sealed AM/PM result batches."""

    labels = {origin.code: origin.label for origin in config.origins}
    selected_origin = st.selectbox(
        "产地",
        options=list(labels),
        format_func=labels.get,
        key="soybean_intraday:origin",
    )

    am = _load_latest(paths.result_root, MarketSession.AM)
    pm = _load_latest(paths.result_root, MarketSession.PM)
    _render_session_table(
        "大豆早间榨利",
        am,
        session=MarketSession.AM,
        market_snapshot_ready=_has_snapshot(paths.snapshot_root, MarketSession.AM),
        origin=selected_origin,
        config=config,
    )
    _render_session_table(
        "大豆下午榨利",
        pm,
        session=MarketSession.PM,
        market_snapshot_ready=_has_snapshot(paths.snapshot_root, MarketSession.PM),
        origin=selected_origin,
        config=config,
    )
    _render_cnf_history(
        paths.cnf_store_path, origin=selected_origin, config=config
    )
    _render_profit_history(
        paths.result_root,
        origin=selected_origin,
        origin_label=labels[selected_origin],
    )
    params = config.resolve_parameters(selected_origin)
    st.caption(
        "正式盘面榨利参数："
        f"关税{params.tariff_rate:.0%}｜增值税{params.vat_rate:.0%}｜"
        f"港杂费{params.port_charge_cny_per_tonne:g}元/吨｜"
        f"加工成本{params.processing_fee_cny_per_tonne:g}元/吨｜"
        f"豆油得率{params.oil_yield:.1%}｜豆粕得率{params.meal_yield:.1%}｜"
        f"换算系数{params.cents_per_bushel_to_usd_per_tonne:.6f}"
    )


def _render_final_ui(
    paths: IntradayPageDataPaths,
    *,
    config: SoybeanImportProfitConfig,
    mode: IntradayPageMode,
    save_cnf_handler: Callable[
        [Mapping[tuple[str, int], float | None]], object
    ] | None,
) -> None:
    presentation = paths.preview
    _inject_preview_style()
    history = pd.DataFrame(
        columns=("trade_date", "origin", "month", "cnf", "source_identity")
    )
    if presentation is not None:
        try:
            history = _load_preview_cnf_cache(
                presentation.historical_cnf_cache_path
            )
        except (OSError, ValueError):
            st.error("Tankan 历史 CNF 本地只读缓存校验失败。")
            return

    labels = {origin.code: origin.label for origin in config.origins}
    am_snapshot = None
    if mode is IntradayPageMode.FINAL_UI_PREVIEW:
        if presentation is None or paths.snapshot_root is None:
            st.error("FINAL_UI_PREVIEW 本地 Snapshot 资产未配置。")
            return
        try:
            am_snapshot = load_intraday_snapshot(
                paths.snapshot_root,
                presentation.business_date,
                MarketSession.AM,
            )
        except (IntradaySnapshotError, OSError):
            st.error("指定的真实 SEALED AM Snapshot 不可读取。")
            return
        business_date = presentation.business_date
        am_captured_at = am_snapshot.captured_at
        pm_captured_at = None
    else:
        business_date = paths.business_date or datetime.now(
            ZoneInfo("Asia/Shanghai")
        ).date()
    # Never mix yesterday's AM with today's PM (or vice versa). Header status,
    # tables, and the editor all share these exact business-date/session releases.
    am_release = _load_selected_release(paths, business_date, MarketSession.AM)
    pm_release = _load_selected_release(paths, business_date, MarketSession.PM)
    if mode is not IntradayPageMode.FINAL_UI_PREVIEW:
        am_captured_at = (
            None if am_release is None else am_release.market_captured_at
        )
        pm_captured_at = (
            None if pm_release is None else pm_release.market_captured_at
        )
    am_display_time = _display_time(am_captured_at)
    pm_display_time = _display_time(pm_captured_at)
    selected_origin = _render_preview_header(
        labels,
        business_date=business_date,
        am_display_time=am_display_time,
        pm_display_time=pm_display_time,
        am_release=am_release,
        pm_release=pm_release,
    )
    edited = _render_preview_cnf_editor(
        paths.cnf_store_path,
        historical_cnf_store_path=paths.historical_cnf_store_path,
        business_date=business_date,
        labels=labels,
        config=config,
        save_cnf_handler=save_cnf_handler,
        editable=(mode is IntradayPageMode.FINAL_UI_PREVIEW or save_cnf_handler is not None),
        am_result_available=(
            am_release is not None
            and am_release.business_date == business_date
            and am_release.session is MarketSession.AM
        ),
    )
    cnf_by_month = {
        month: _number(edited.loc[selected_origin, f"{month}月船期"])
        for month in range(1, 13)
    }
    if mode is IntradayPageMode.FINAL_UI_PREVIEW:
        am_rows = _preview_profit_rows(
            am_snapshot,
            origin=selected_origin,
            cnf_by_month=cnf_by_month,
            config=config,
            pm_fixture=False,
        )
    else:
        am_rows = _result_profit_rows(
            am_release, business_date=business_date, session=MarketSession.AM,
            origin=selected_origin, config=config
        )
    _render_preview_session_table(
        "大豆早间榨利",
        am_rows,
        short_meta=f"AM · {am_display_time}",
    )
    if mode is IntradayPageMode.FINAL_UI_PREVIEW:
        pm_rows = _preview_profit_rows(
            am_snapshot,
            origin=selected_origin,
            cnf_by_month=cnf_by_month,
            config=config,
            pm_fixture=True,
        )
    else:
        pm_rows = _result_profit_rows(
            pm_release, business_date=business_date, session=MarketSession.PM,
            origin=selected_origin, config=config
        )
    _render_preview_session_table(
        "大豆下午榨利",
        pm_rows,
        short_meta=("PM · 15:30" if pm_display_time == "—" else f"PM · {pm_display_time}"),
        preview=mode is IntradayPageMode.FINAL_UI_PREVIEW,
    )
    history = _merge_manual_cnf_history(
        history,
        paths.cnf_store_path,
        allowed_origins=config.origin_codes,
        historical_cnf_store_path=paths.historical_cnf_store_path,
    )
    _render_tankan_cnf_history(history, origin=selected_origin)
    if presentation is None:
        st.subheader("盘面榨利历史季节性")
        st.html('<p class="soy-note">历史盘面榨利只读数据暂不可用。</p>')
    else:
        _render_board_crush_margin_seasonality(
            presentation,
            result_root=paths.result_root,
            origin=selected_origin,
            origin_label=labels[selected_origin],
            expected_environment=paths.environment,
            snapshot_root=paths.snapshot_root,
        )
    params = config.resolve_parameters(selected_origin)
    am_unavailable = [
        str(row["船期"]) for row in am_rows if row["状态"] != "可计算"
    ]
    pm_unavailable = [
        str(row["船期"]) for row in pm_rows if row["状态"] != "可计算"
    ]
    with st.expander("数据状态"):
        if mode is IntradayPageMode.FINAL_UI_PREVIEW:
            identity_lines = (
                "- 页面模式：`FINAL_UI_PREVIEW`",
                f"- AM：真实 SEALED `{am_snapshot.release_id}`；captured_at `{am_snapshot.captured_at.isoformat()}`",
                "- PM：页面内确定性 Preview；不保存、不发布、不进入历史序列",
                f"- Preview CNF：`{PREVIEW_CNF_SOURCE}`；仅当前 Streamlit Session",
            )
        else:
            identity_lines = (
                "- 页面模式：`STRICT_RUNTIME`",
                "- AM：" + _release_status(am_release),
                "- PM：" + _release_status(pm_release),
                "- CNF：正式 `manual_ui` 只读展示",
            )
        st.markdown(
            "\n".join(
                (
                    "- 环境：`TEST_ISOLATED_NON_PRODUCTION`",
                    *identity_lines,
                    f"- 历史 CNF：`{TANKAN_CNF_SOURCE}` 本地只读缓存",
                    "- AM 不可计算船期："
                    + ("、".join(am_unavailable) if am_unavailable else "无"),
                    "- PM 不可计算船期："
                    + ("、".join(pm_unavailable) if pm_unavailable else "无"),
                    "- 参数："
                    f"关税{params.tariff_rate:.0%}｜增值税{params.vat_rate:.0%}｜"
                    f"港杂费{params.port_charge_cny_per_tonne:g}｜"
                    f"加工费{params.processing_fee_cny_per_tonne:g}｜"
                    f"豆粕得率{params.meal_yield:.1%}｜豆油得率{params.oil_yield:.1%}｜"
                    f"换算系数{params.cents_per_bushel_to_usd_per_tonne:.6f}",
                )
            )
        )


def _inject_preview_style() -> None:
    st.html(
        """
        <style>
        .stApp { background: #f5f8fc; color: #24364b; }
        .block-container { max-width: 1680px; padding: 4rem 1.35rem 2.25rem; }
        h1, h2, h3, p { color: #102a43; }
        [data-testid="stDataEditor"] { background:#fff; border:1px solid #d8e3ee; border-radius:3px; padding:2px; }
        [data-testid="stDataEditor"] [data-testid="stElementToolbar"] { display:none; }
        [data-testid="stExpander"] { background:#fff; border-color:#d8e3ee; border-radius:3px; }
        [data-testid="stPlotlyChart"] { background:#fff; border:1px solid #d8e3ee; border-radius:3px; padding:7px 7px 1px; box-shadow:none; }
        [data-testid="stVerticalBlockBorderWrapper"]:has(.soy-cnf-head) { background:#fff; border:1px solid #d8e3ee; border-radius:3px; padding:0; box-shadow:none; }
        [data-testid="stVerticalBlockBorderWrapper"]:has(.soy-cnf-head) > div { gap:.5rem; }
        button[kind="primary"] { background:#1f4e78 !important; border-color:#1f4e78 !important; color:#fff !important; border-radius:3px !important; font-weight:700 !important; }
        button[kind="primary"]:hover { background:#173e61 !important; border-color:#173e61 !important; }
        button[kind="primary"]:disabled,
        button[kind="primary"][disabled] { background:#dce8f3 !important; border-color:#dce8f3 !important; color:#355a7a !important; font-weight:700 !important; opacity:1 !important; }
        .soy-header { margin:0 0 10px; padding-bottom:9px; border-bottom:1px solid #d8e3ee; }
        .soy-header-main { display:flex; align-items:center; justify-content:space-between; gap:18px; flex-wrap:wrap; }
        .soy-title-row { display:flex; align-items:center; gap:9px; flex-wrap:wrap; }
        .soy-title-dot { color:#1f4e78; font-size:13px; line-height:1; }
        .soy-title { margin:0; font-size:28px; font-weight:800; letter-spacing:-.35px; color:#102a43; }
        .soy-origin-switch { display:flex; gap:7px; }
        .soy-origin-switch a { display:inline-block; min-width:68px; text-align:center; padding:6px 15px; border:1px solid #3e6f9e; border-radius:3px; background:#fff; color:#1f4e78; text-decoration:none !important; font-size:13px; font-weight:720; }
        .soy-origin-switch a:hover { border-color:#1f4e78; background:#edf4fa; }
        .soy-origin-switch a.active { background:#1f4e78; border-color:#1f4e78; color:#fff; }
        .soy-statusbar { margin-top:7px; color:#6d7f91; font-size:11.5px; letter-spacing:.05px; }
        .soy-statusbar .sealed { color:#3e6f9e; font-weight:700; }
        .soy-statusbar .divider { margin:0 8px; color:#b8c8d8; }
        .soy-cnf-head { display:flex; justify-content:space-between; align-items:baseline; gap:12px; background:#e5eff8; border-bottom:1px solid #d8e3ee; margin:-1px -1px 0; padding:8px 11px; }
        .soy-cnf-head h2 { margin:0; font-size:17px; font-weight:800; color:#102a43; }
        .soy-cnf-meta { color:#3e6f9e; font-size:11px; font-weight:650; }
        .soy-cnf-note { color:#6d7f91; font-size:10.5px; margin:0; padding:0 2px; }
        .soy-section-card { background:#fff; border:1px solid #d8e3ee; border-radius:3px; padding:11px 11px 8px; margin:9px 0 11px; box-shadow:none; }
        .soy-section-head { display:flex; align-items:baseline; gap:9px; margin-bottom:8px; }
        .soy-section-head h2 { margin:0; font-size:20px; font-weight:800; color:#102a43; }
        .soy-section-meta { color:#3e6f9e; font-size:12px; font-weight:700; }
        .soy-preview-pill { color:#1f4e78; background:#edf4fa; border:1px solid #c7d9e9; border-radius:3px; padding:1px 5px; font-size:9px; font-weight:800; letter-spacing:.3px; }
        .soy-accent { display:none; }
        table.soy-table { width:100%; border-collapse:collapse; table-layout:fixed; font-size:12px; color:#24364b; font-variant-numeric:tabular-nums; }
        .soy-table th { background:#1f4e78; color:#fff; font-weight:800; padding:11px 4px; border-right:1px solid rgba(255,255,255,.25); border-bottom:1px solid #163d60; text-align:center; white-space:normal; line-height:1.18; }
        .soy-table td { padding:9px 4px; border-right:1px solid #d8e3ee; border-bottom:1px solid #d8e3ee; text-align:right; white-space:nowrap; line-height:1.2; }
        .soy-table th:last-child,.soy-table td:last-child { border-right:0; }
        .soy-table tbody tr:nth-child(even) { background:#f8fafd; }
        .soy-table tbody tr:hover { background:#edf4fa; }
        .soy-profit-table th:nth-child(1),.soy-profit-table td:nth-child(1) { width:7.6%; }
        .soy-profit-table th:nth-child(14),.soy-profit-table td:nth-child(14) { width:10%; }
        .soy-profit-table td:nth-child(1),.soy-profit-table td:nth-child(2),.soy-profit-table td:nth-child(4),.soy-profit-table td:nth-child(7),.soy-profit-table td:nth-child(9),.soy-profit-table td:nth-child(11),.soy-profit-table td:nth-child(12) { text-align:center; }
        .soy-profit-table td:nth-child(1) { font-weight:700; color:#25384a; }
        .soy-profit-table .group-start { border-left:2px solid #b7cadd; }
        .soy-profit-table th.group-start { border-left:2px solid #8ba9c3; }
        .soy-profit-table th.group-result { background:#102a43; }
        .soy-profit-table td.group-quote { background:#f4f8fc; font-weight:800; }
        .soy-profit-table td.group-cost:last-of-type,.soy-profit-table td:nth-child(13) { font-weight:800; color:#102a43; }
        .soy-profit-table td.group-result { background:#f7fafd; }
        .soy-margin { display:inline-block; min-width:72px; padding:4px 8px; border-radius:3px; text-align:right; font-size:12px; font-weight:850; letter-spacing:.05px; color:#fff; }
        .soy-margin.pos { color:#fff; background:#b53b34; }
        .soy-margin.neg { color:#fff; background:#2f7d5a; }
        .soy-margin.na { color:#a4a9ae; background:transparent; text-align:center; }
        .soy-history-table { font-size:11.8px; }
        .soy-history-table th { background:#3e6f9e; border-bottom-color:#315c84; }
        .soy-history-table th:first-child,.soy-history-table td:first-child { width:10%; text-align:center; }
        .soy-history-table td:not(:first-child) { text-align:center; }
        .soy-history-table tbody tr:first-child { background:#edf4fa !important; font-weight:700; }
        .soy-note { color:#6d7f91; font-size:11.5px; margin:4px 0 8px; }
        .soy-formula-note { color:#6d8195; font-size:11.5px; line-height:1.45; margin:9px 2px 1px; }
        @media (max-width: 1100px) { table.soy-table { font-size:10.8px; } .soy-table th,.soy-table td { padding-left:3px; padding-right:3px; } .soy-title { font-size:24px; } }

        /* User-provided legacy-main-table sizing, mapped to AM/PM markup. */
        .block-container {
            max-width:1920px !important;
            width:100% !important;
            padding-left:1.2rem !important;
            padding-right:1.2rem !important;
            padding-top:4rem !important;
            padding-bottom:3rem !important;
        }
        .profit-card {
            width:100%;
            background:#fff;
            border:1px solid #d8e3ee;
            border-radius:8px;
            padding:14px 12px 10px;
            margin-bottom:28px;
            box-shadow:0 1px 2px rgba(16,42,67,.03);
        }
        .profit-card-title {
            display:flex;
            align-items:baseline;
            gap:12px;
            margin:0 0 14px 2px;
            color:#102a43;
            line-height:1.2;
        }
        .profit-card-title h2 {
            font-size:26px;
            font-weight:800;
        }
        .profit-card-time {
            color:#3e6f9e;
            font-size:14px;
            font-weight:700;
        }
        .profit-table {
            width:100% !important;
            border-collapse:collapse;
            border-spacing:0;
            table-layout:fixed;
            font-size:15px;
            color:#24364b;
            background:#fff;
        }
        .profit-table thead th {
            height:48px;
            padding:8px 5px;
            background:#1f4e78;
            color:#fff;
            font-size:14px;
            font-weight:800;
            text-align:center;
            vertical-align:middle;
            white-space:nowrap;
            border-right:1px solid rgba(255,255,255,.24);
            border-bottom:1px solid #173d60;
        }
        .profit-table thead th:last-child { background:#123653; }
        .profit-table tbody td {
            height:43px;
            padding:7px 6px;
            vertical-align:middle;
            border-right:1px solid #d8e3ee;
            border-bottom:1px solid #d8e3ee;
            white-space:nowrap;
            font-size:14.5px;
            overflow:hidden;
            text-overflow:ellipsis;
        }
        .profit-table tbody tr:nth-child(even) td { background:#f7fafd; }
        .profit-table tbody tr:hover td { background:#edf4fa; }
        .profit-table td.shipment { font-weight:800; text-align:center; color:#24364b; }
        .profit-table td.cnf { font-weight:800; text-align:center; background:#f3f8fc; }
        .profit-table td.contract { text-align:center; font-weight:600; color:#405a70; }
        .profit-table td.number { text-align:right; }
        .profit-table td.fx,.profit-table td.tax { text-align:center; }
        .profit-table td.landed-cost { text-align:right; font-weight:800; color:#102a43; }
        .profit-table td.margin-cell { text-align:right; padding-right:6px; }
        .margin-positive,.margin-negative {
            display:inline-flex;
            align-items:center;
            justify-content:center;
            min-width:84px;
            padding:5px 9px;
            border-radius:4px;
            color:#fff;
            font-size:14px;
            font-weight:800;
            line-height:1;
        }
        .margin-positive { background:#d9363e; }
        .margin-negative { background:#2f855a; }
        .margin-null { color:#a7b4c2; font-weight:600; }
        .profit-table th:nth-child(1),.profit-table td:nth-child(1) { width:7.5%; }
        .profit-table th:nth-child(2),.profit-table td:nth-child(2) { width:6.5%; }
        .profit-table th:nth-child(3),.profit-table td:nth-child(3) { width:7%; }
        .profit-table th:nth-child(4),.profit-table td:nth-child(4) { width:6.5%; }
        .profit-table th:nth-child(5),.profit-table td:nth-child(5) { width:7%; }
        .profit-table th:nth-child(6),.profit-table td:nth-child(6) { width:6.5%; }
        .profit-table th:nth-child(7),.profit-table td:nth-child(7),
        .profit-table th:nth-child(8),.profit-table td:nth-child(8),
        .profit-table th:nth-child(9),.profit-table td:nth-child(9),
        .profit-table th:nth-child(10),.profit-table td:nth-child(10) { width:7%; }
        .profit-table th:nth-child(11),.profit-table td:nth-child(11),
        .profit-table th:nth-child(12),.profit-table td:nth-child(12) { width:6%; }
        .profit-table th:nth-child(13),.profit-table td:nth-child(13) { width:9%; }
        .profit-table th:nth-child(14),.profit-table td:nth-child(14) { width:10%; }
        @media (max-width:1400px) {
            .block-container { width:100% !important; padding-left:.5rem !important; padding-right:.5rem !important; }
            .profit-table { font-size:13px; }
            .profit-table tbody td { font-size:13px; padding-left:4px; padding-right:4px; }
        }
        </style>
        """
    )


def _render_preview_header(
    labels: dict[str, str],
    *,
    business_date: date,
    am_display_time: str,
    pm_display_time: str,
    am_release: ResolvedIntradayProfitRelease | None,
    pm_release: ResolvedIntradayProfitRelease | None,
) -> str:
    requested = str(st.query_params.get("soybean_origin", "brazil"))
    selected = requested if requested in labels else next(iter(labels))
    am_status = _release_status(
        am_release,
        business_date=business_date,
        session=MarketSession.AM,
        compact=True,
    )
    pm_status = _release_status(
        pm_release,
        business_date=business_date,
        session=MarketSession.PM,
        compact=True,
    )
    am_class = ' class="sealed"' if am_status == "已封存" else ""
    pm_class = ' class="sealed"' if pm_status == "已封存" else ""
    links = []
    for code, label in labels.items():
        query = urlencode({"workspace_page": "import_profit", "soybean_origin": code})
        active = " active" if code == selected else ""
        links.append(
            f'<a class="{active.strip()}" href="?{query}" target="_self">{escape(label)}</a>'
        )
    st.html(
        '<header class="soy-header">'
        '<div class="soy-header-main"><div class="soy-title-row">'
        '<span class="soy-title-dot">●</span>'
        f'<h1 class="soy-title">{escape(PAGE_TITLE)}</h1></div>'
        '<nav class="soy-origin-switch">'
        + "".join(links)
        + '</nav></div><div class="soy-statusbar">'
        f'<span>{business_date.isoformat()}</span><span class="divider">|</span>'
        f'<span{am_class}>AM {escape(am_display_time)} {escape(am_status)}</span>'
        '<span class="divider">|</span>'
        f'<span{pm_class}>PM {escape(pm_display_time)} {escape(pm_status)}</span>'
        + '</div></header>'
    )
    return selected


@st.cache_data(show_spinner=False)
def _load_preview_cnf_cache(path: Path) -> pd.DataFrame:
    frame = pd.read_parquet(path)
    required = {
        "trade_date", "origin", "month", "cnf", "source_identity"
    }
    if not required.issubset(frame.columns):
        raise ValueError("preview CNF cache schema mismatch")
    if set(frame["source_identity"].dropna().unique()) != {TANKAN_CNF_SOURCE}:
        raise ValueError("preview CNF cache source mismatch")
    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    return frame


def _render_preview_cnf_editor(
    cnf_store_path: Path,
    *,
    business_date: date,
    labels: dict[str, str],
    config: SoybeanImportProfitConfig,
    save_cnf_handler: Callable[
        [Mapping[tuple[str, int], float | None]], object
    ] | None,
    editable: bool,
    historical_cnf_store_path: Path | None = None,
    am_result_available: bool = False,
) -> pd.DataFrame:
    records = _manual_cnf_records(cnf_store_path, historical_cnf_store_path, config.origin_codes)
    operational_records = load_cnf_store(
        cnf_store_path, allowed_origins=config.origin_codes
    ).records
    values = {
        (record.business_key.origin, record.business_key.shipment_month):
        record.cnf_cents_per_bushel
        for record in records
        if record.business_key.business_date == business_date
        and record.business_key.shipment_year == shipment_year_for(business_date, record.business_key.shipment_month)
    }
    cnf_saved = any(
        record.business_key.business_date == business_date
        and record.business_key.shipment_year == shipment_year_for(
            business_date, record.business_key.shipment_month
        )
        and record.source == "manual_ui"
        for record in operational_records
    )
    seed = pd.DataFrame(
        [
            {
                "origin": origin,
                "产地": label,
                **{
                    f"{month}月船期": values.get((origin, month))
                    for month in range(1, 13)
                },
            }
            for origin, label in labels.items()
        ]
    ).set_index("origin")
    requested_origin = str(st.query_params.get("soybean_origin", "brazil"))
    selected_origin = (
        requested_origin if requested_origin in labels else next(iter(labels))
    )
    with st.container(border=True):
        st.html(
            '<div class="soy-cnf-head"><h2>今日 CNF</h2>'
            f'<span class="soy-cnf-meta">人工录入 · {business_date.isoformat()}</span></div>'
            '<p class="soy-cnf-note">当前选择：'
            f'{escape(labels[selected_origin])}'
            ' · 点击保存后写入正式 manual_ui</p>'
        )
        edited = st.data_editor(
            seed,
            hide_index=True,
            width="stretch",
            disabled=(
                ["产地"]
                if editable
                else ["产地", *(f"{month}月船期" for month in range(1, 13))]
            ),
            key=f"soybean_intraday:cnf_editor:{business_date.isoformat()}",
            column_config={
                f"{month}月船期": st.column_config.NumberColumn(
                    format="%.1f", step=1.0
                )
                for month in range(1, 13)
            },
        )
        actions = st.columns([8.3, 1.7])
        if cnf_saved:
            st.caption("今日 CNF 已保存在 operational store。")
        with actions[1]:
            preview_saved = st.button(
                ("今日 CNF 已保存" if am_result_available else "重试上午盘面榨利")
                if cnf_saved else "保存今日 CNF",
                type="primary",
                width="stretch",
                key="soybean_intraday_preview:save_cnf",
                disabled=(cnf_saved and am_result_available) or not editable,
            )
        if preview_saved:
            if save_cnf_handler is None:
                st.error("正式 manual_ui 保存服务尚未启用。")
            else:
                payload = {
                    (origin, month): _number(
                        edited.iloc[position][f"{month}月船期"]
                    )
                    for position, origin in enumerate(seed.index)
                    for month in range(1, 13)
                }
                try:
                    receipt = save_cnf_handler(payload)
                except Exception as exc:
                    st.error(f"今日 CNF 保存失败：{exc}")
                else:
                    st.session_state[
                        "soybean_intraday:am_closure_receipt"
                    ] = receipt
                    status = getattr(receipt, "am_materialization_status", None)
                    if status == "MATERIALIZED":
                        st.success("今日 CNF 已正式保存，上午盘面榨利结果已生成。")
                        if getattr(receipt, "unavailable_periods", ()):
                            st.info("部分船期输入不完整，相关榨利字段保持空值。")
                    elif status == "INPUT_INCOMPLETE":
                        st.success("今日 CNF 已正式保存。")
                        st.info("当前缺少 AM Snapshot 或必要行情输入，盘面榨利待计算。")
                    else:
                        st.success("今日 CNF 已正式保存。")
                        st.warning(
                            "上午盘面榨利未能生成："
                            + str(getattr(receipt, "am_diagnostic", None) or "请检查计算输入与运行日志。")
                        )
    edited.index = seed.index
    return edited


def _preview_profit_rows(
    snapshot,
    *,
    origin: str,
    cnf_by_month: dict[int, float | None],
    config: SoybeanImportProfitConfig,
    pm_fixture: bool,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    params = config.resolve_parameters(origin)
    for month in range(1, 13):
        key = BusinessKey(
            business_date=snapshot.business_date,
            commodity="soybean",
            origin=origin,
            shipment_year=shipment_year_for(snapshot.business_date, month),
            shipment_month=month,
            allowed_origins=config.origin_codes,
            expected_commodity=config.commodity,
        )
        inputs = select_soybean_intraday_market_inputs(
            snapshot, business_key=key, config=config
        )
        cnf = cnf_by_month[month]
        cbot = None if inputs.cbot is None else inputs.cbot.price
        fx = inputs.fx.price
        meal = None if inputs.soymeal is None else inputs.soymeal.price
        oil = None if inputs.soyoil is None else inputs.soyoil.price
        if pm_fixture:
            cbot = None if cbot is None else round(cbot * 1.002, 2)
            fx = round(fx * 0.9998, 4)
            meal = None if meal is None else round(meal * 1.003, 0)
            oil = None if oil is None else round(oil * 0.998, 0)
        available = not inputs.unavailable_contracts
        usd_cost = (
            None
            if cnf is None or cbot is None
            else (cbot + cnf) * params.cents_per_bushel_to_usd_per_tonne
        )
        landed = (
            None
            if usd_cost is None
            else usd_cost * fx * (1 + params.tariff_rate) * (1 + params.vat_rate)
        )
        margin = (
            None
            if landed is None or meal is None or oil is None
            else meal * params.meal_yield
            + oil * params.oil_yield
            - landed
            - params.port_charge_cny_per_tonne
            - params.processing_fee_cny_per_tonne
        )
        rows.append(
            {
                "船期": key.shipment_period,
                "状态": "可计算" if available else "合约暂不可用",
                "CNF（美分/蒲）": cnf,
                "美金成本（美元/吨）": usd_cost,
                "CBOT合约": inputs.cbot_contract_code,
                "CBOT价格": cbot,
                "汇率": fx,
                "豆粕合约": inputs.soymeal_contract_code,
                "豆粕盘面": meal,
                "豆油合约": inputs.soyoil_contract_code,
                "豆油盘面": oil,
                "关税": params.tariff_rate,
                "增值税": params.vat_rate,
                "完税成本（元/吨）": landed,
                "盘面榨利（元/吨）": margin,
            }
        )
    return rows


def _result_profit_rows(
    release: ResolvedIntradayProfitRelease | None,
    *,
    business_date: date,
    session: MarketSession,
    origin: str,
    config: SoybeanImportProfitConfig,
) -> list[dict[str, object]]:
    """Left join one session's sealed results onto twelve calendar months.

    Month identity is independent of data availability. The existing rolling
    shipment-year rule distinguishes this year's month from next year's month.
    """

    params = config.resolve_parameters(origin)
    by_period = {}
    if (release is not None and release.business_date == business_date
            and release.session == session):
        by_period = {
            (item.get("shipment_year"), item.get("shipment_month")): item
            for item in release.rows
            if item.get("business_date") == business_date.isoformat()
            and item.get("origin") == origin
            and item.get("commodity") == "soybean"
            and item.get("session") == session.value
        }
    rows = []
    for month in range(1, 13):
        year = shipment_year_for(business_date, month)
        row = by_period.get((year, month), {})
        calculated = (
            row.get("availability_status") == "SUCCESS"
            and str(row.get("calculation_status", "")).lower() == "success"
            and row.get("net_crush_margin_cny_per_tonne") is not None
        )
        rows.append(
            {
                "船期": f"{year:04d}-{month:02d}",
                "状态": "可计算" if calculated else "合约暂不可用",
                "CNF（美分/蒲）": row.get("cnf_cents_per_bushel"),
                "美金成本（美元/吨）": row.get("usd_cost_per_tonne"),
                "CBOT合约": row.get("cbot_contract"),
                "CBOT价格": row.get("cbot_price_cents_per_bushel"),
                "汇率": row.get("fx_value"),
                "豆粕合约": row.get("soymeal_contract"),
                "豆粕盘面": row.get("soymeal_price_cny_per_tonne"),
                "豆油合约": row.get("soyoil_contract"),
                "豆油盘面": row.get("soyoil_price_cny_per_tonne"),
                "关税": params.tariff_rate,
                "增值税": params.vat_rate,
                "完税成本（元/吨）": row.get(
                    "duty_paid_cost_cny_per_tonne"
                ),
                "盘面榨利（元/吨）": row.get(
                    "net_crush_margin_cny_per_tonne"
                ),
            }
        )
    return rows


def _manual_cnf_records(current, historical, allowed_origins):
    """Operational records override only an identical complete historical key."""
    records = {}
    for path in (historical, current):
        if path is not None:
            for record in load_cnf_store(path, allowed_origins=allowed_origins).records:
                records[record.key] = record
    return tuple(records.values())


def _merge_manual_cnf_history(
    history: pd.DataFrame,
    cnf_store_path: Path,
    *,
    allowed_origins: tuple[str, ...],
    historical_cnf_store_path: Path | None = None,
) -> pd.DataFrame:
    """Overlay manual_ui for research display without altering either source."""

    records = _manual_cnf_records(cnf_store_path, historical_cnf_store_path, allowed_origins)
    manual = pd.DataFrame(
        (
            {
                "trade_date": record.business_key.business_date,
                "origin": record.business_key.origin,
                "month": record.business_key.shipment_month,
                "cnf": record.cnf_cents_per_bushel,
                "source_identity": record.source,
            }
            for record in records
        ),
        columns=("trade_date", "origin", "month", "cnf", "source_identity"),
    )
    return pd.concat((history, manual), ignore_index=True)


def _display_time(value: object) -> str:
    if value is None:
        return "—"
    return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")


def _release_status(
    release: ResolvedIntradayProfitRelease | None,
    *,
    business_date: date | None = None,
    session: MarketSession | None = None,
    compact: bool = False,
) -> str:
    if (
        release is None
        or (business_date is not None and release.business_date != business_date)
        or (session is not None and release.session is not session)
    ):
        return "尚未封存"
    if compact:
        return "已封存"
    return (
        f"真实 SEALED `{release.release_id}`；"
        f"captured_at `{release.market_captured_at.isoformat()}`"
    )


def _render_preview_session_table(
    heading: str,
    rows: list[dict[str, object]],
    *,
    short_meta: str,
    preview: bool = False,
) -> None:
    headings = (
        ("船期", "group-key"),
        ("CNF", "group-quote group-start"),
        ("美金成本", "group-foreign group-start"),
        ("CBOT合约", "group-foreign"),
        ("CBOT价格", "group-foreign"),
        ("汇率", "group-foreign"),
        ("豆粕合约", "group-domestic group-start"),
        ("豆粕盘面", "group-domestic"),
        ("豆油合约", "group-domestic"),
        ("豆油盘面", "group-domestic"),
        ("关税%", "group-cost group-start"),
        ("增值税%", "group-cost"),
        ("完税成本", "group-cost"),
        ("盘面榨利", "group-result group-start"),
    )
    body = []
    for row in rows:
        available = row["状态"] == "可计算"
        status_title = "" if available else ' title="合约暂不可用，详细状态见页面底部数据状态"'
        margin = _number(row.get("盘面榨利（元/吨）"))
        if margin is None:
            margin_html = (
                '<span class="soy-margin na margin-null" title="暂不可计算，详细状态见页面底部数据状态">—</span>'
            )
        else:
            margin_class = "pos" if margin >= 0 else "neg"
            semantic_margin_class = (
                "margin-positive" if margin >= 0 else "margin-negative"
            )
            margin_html = (
                f'<span class="soy-margin {margin_class} {semantic_margin_class}">'
                f"{margin:,.2f}</span>"
            )
        cells = (
            (f'<span{status_title}>{escape(str(row["船期"]))}</span>', "group-key shipment"),
            (_fmt_quote(row.get("CNF（美分/蒲）")), "group-quote group-start cnf"),
            (_fmt_number(row.get("美金成本（美元/吨）"), 2), "group-foreign group-start number"),
            (_fmt_text(row.get("CBOT合约")), "group-foreign contract"),
            (_fmt_number(row.get("CBOT价格"), 2), "group-foreign number"),
            (_fmt_number(row.get("汇率"), 4), "group-foreign fx"),
            (_fmt_text(row.get("豆粕合约")), "group-domestic group-start contract"),
            (_fmt_market_price(row.get("豆粕盘面")), "group-domestic number"),
            (_fmt_text(row.get("豆油合约")), "group-domestic contract"),
            (_fmt_market_price(row.get("豆油盘面")), "group-domestic number"),
            (_fmt_percent(row.get("关税")), "group-cost group-start tax"),
            (_fmt_percent(row.get("增值税")), "group-cost tax"),
            (_fmt_number(row.get("完税成本（元/吨）"), 2), "group-cost landed-cost"),
            (margin_html, "group-result group-start margin-cell"),
        )
        body.append(
            "<tr>"
            + "".join(
                f'<td class="{css_class}">{cell}</td>'
                for cell, css_class in cells
            )
            + "</tr>"
        )
    preview_badge = (
        '<span class="soy-preview-pill" title="仅用于本地页面验收">Preview</span>'
        if preview
        else ""
    )
    st.html(
        '<section class="soy-section-card profit-card">'
        '<div class="soy-section-head profit-card-title">'
        f'<h2>{escape(heading)}</h2><span class="soy-section-meta profit-card-time">{escape(short_meta)}</span>'
        + preview_badge
        + '</div><div class="soy-accent"></div><table class="soy-table soy-profit-table profit-table"><thead><tr>'
        + "".join(
            f'<th class="{css_class}">{item}</th>'
            for item, css_class in headings
        )
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
        + '<p class="soy-formula-note">'
        + '完税成本 = (CNF + CBOT) × 0.367437 × 汇率 × (1 + 3%) × (1 + 9%)'
        + ' ｜ 盘面榨利 = 豆粕盘面 × 79.5% + 豆油盘面 × 19% − 完税成本 − 50 − 150'
        + '</p></section>'
    )


def _render_tankan_cnf_history(history: pd.DataFrame, *, origin: str) -> None:
    selected = history[history["origin"] == origin]
    frame = selected.pivot_table(
        index="trade_date", columns="month", values="cnf", aggfunc="last"
    ).sort_index(ascending=False)
    frame = frame.reindex(columns=range(1, 13)).dropna(how="all").head(10)
    body = []
    for business_date, row in frame.iterrows():
        cells = [escape(str(business_date))]
        cells.extend(_fmt_quote(row.get(month)) for month in range(1, 13))
        body.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    headings = ("日期", *(f"{month}月" for month in range(1, 13)))
    st.html(
        '<section class="soy-section-card">'
        '<div class="soy-section-head"><h2>最近10日 CNF</h2></div>'
        '<table class="soy-table soy-history-table"><thead><tr>'
        + "".join(f"<th>{item}</th>" for item in headings)
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></section>"
    )


@st.cache_data(show_spinner=False)
def _load_unified_seasonal_history_cached(
    legacy_result_path: str,
    legacy_source_sha256: str,
    result_root: str,
    index_sha256: str,
    origin: str,
    expected_environment: str | None = None,
    snapshot_root: str | None = None,
) -> UnifiedSeasonalHistory:
    del index_sha256
    return load_unified_seasonal_history(
        legacy_result_path,
        legacy_source_sha256=legacy_source_sha256,
        result_root=result_root,
        origin=origin,
        expected_environment=expected_environment,
        snapshot_root=snapshot_root,
    )


def _small_file_sha(path: Path) -> str:
    if not path.is_file():
        return "ABSENT"
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _render_board_crush_margin_seasonality(
    preview: FinalUiPreviewPaths,
    *,
    result_root: Path,
    origin: str,
    origin_label: str,
    expected_environment: str | None = None,
    snapshot_root: Path | None = None,
) -> None:
    st.subheader("盘面榨利历史季节性")
    st.html(
        f'<p class="soy-note">中国进口大豆 - {escape(origin_label)} - 盘面榨利；'
        '历史口径延续原数据，正式切换日起使用已封存 PM 榨利。</p>'
    )
    try:
        intraday_index_sha = _small_file_sha(
            result_root / "intraday_profit_index.json"
        )
        history = _load_unified_seasonal_history_cached(
            str(preview.historical_profit_path),
            preview.historical_profit_sha256,
            str(result_root),
            intraday_index_sha,
            origin,
            expected_environment,
            None if snapshot_root is None else str(snapshot_root),
        )
    except (
        FileNotFoundError,
        OSError,
        ValueError,
        IntradayProfitStoreError,
        IntradayProfitHistoryError,
        IntradaySnapshotError,
    ):
        st.html('<p class="soy-note">历史盘面榨利只读数据暂不可用。</p>')
        return
    for start in range(0, len(history.charts), 3):
        columns = st.columns(3)
        for offset, chart in enumerate(history.charts[start:start + 3]):
            with columns[offset]:
                figure = _pm_seasonal_figure(chart)
                st.plotly_chart(
                    figure,
                    width="stretch",
                    key=(
                        "soybean_intraday:pm_margin_history:"
                        f"{origin}:{chart.spec.shipment_month}"
                    ),
                )


def _pm_seasonal_figure(chart: PmSeasonalChart) -> go.Figure:
    figure = go.Figure()
    latest_year = max(
        (series.series_year for series in chart.series), default=None
    )
    for series in chart.series:
        latest = series.series_year == latest_year
        color = (
            LATEST_SEASONAL_YEAR_COLOR
            if latest
            else _seasonal_year_color(series.series_year)
        )
        customdata = [
            (
                point.trade_date.isoformat(),
                point.series_year,
                point.shipment_month,
                point.domestic_contract,
                point.release_id,
                (
                    "历史盘面榨利"
                    if point.history_source == "legacy"
                    else "PM"
                ),
            )
            for point in series.points
        ]
        figure.add_trace(
            go.Scatter(
                x=[point.season_position for point in series.points],
                y=[
                    point.net_crush_margin_cny_per_tonne
                    for point in series.points
                ],
                customdata=customdata,
                mode="lines+markers",
                name=str(series.series_year),
                line={"color": color, "width": 4.0 if latest else 1.9},
                marker={"color": color, "size": 5.5 if latest else 3.8},
                opacity=1 if latest else 0.82,
                connectgaps=False,
                hovertemplate=(
                    "真实日期：%{customdata[0]}<br>"
                    "周期年份：%{customdata[1]}周期<br>"
                    "船期：%{customdata[2]}月船期<br>"
                    "国内合约：%{customdata[3]}<br>"
                    "盘面榨利：%{y:,.2f} 元/吨<br>"
                    "数据口径：%{customdata[5]}"
                    "<extra></extra>"
                ),
            )
        )
    if not chart.series:
        figure.add_annotation(
            text="暂无盘面榨利历史数据",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font={"color": "#7b8b9b", "size": 12},
        )
    tickvals = [index * 31 + 14 for index in range(len(chart.spec.window_months))]
    figure.update_layout(
        title=chart.spec.title,
        title_font={"size": 15, "color": "#182638"},
        height=330,
        margin={"l": 52, "r": 14, "t": 78, "b": 42},
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        font={"color": "#46515d", "size": 10.5},
        legend={
            "orientation": "h",
            "x": 0,
            "y": 1.02,
            "yanchor": "bottom",
            "font": {"size": 9},
        },
        xaxis={
            "tickmode": "array",
            "tickvals": tickvals,
            "ticktext": [
                f"{month}月" for month in chart.spec.window_months
            ],
            "range": [-1, len(chart.spec.window_months) * 31],
            "gridcolor": "#eef0f2",
            "zeroline": False,
            "fixedrange": True,
        },
        yaxis={
            "title": "盘面榨利（元/吨）",
            "rangemode": "tozero",
            "gridcolor": "#eef0f2",
            "zeroline": True,
            "zerolinecolor": "#8fa3b5",
            "zerolinewidth": 1.2,
        },
    )
    return figure


def _seasonal_year_color(series_year: int) -> str:
    return SEASONAL_YEAR_COLORS[series_year % len(SEASONAL_YEAR_COLORS)]


def _fmt_text(value: object) -> str:
    if value is None or pd.isna(value):
        return "—"
    return escape(str(value))


def _fmt_number(value: object, decimals: int) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:,.{decimals}f}"


def _fmt_quote(value: object) -> str:
    number = _number(value)
    if number is None:
        return "—"
    return f"{number:,.0f}" if number.is_integer() else f"{number:,.1f}"


def _fmt_market_price(value: object) -> str:
    number = _number(value)
    if number is None:
        return "—"
    return f"{number:,.0f}" if number.is_integer() else f"{number:,.1f}"


def _fmt_percent(value: object) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:.0%}"


def _load_latest(
    result_root: Path,
    session: MarketSession,
) -> ResolvedIntradayProfitRelease | None:
    try:
        return load_latest_intraday_profit_batch(result_root, session)
    except IntradayProfitStoreError:
        return None


def _load_selected_release(paths, business_date, session):
    if session is MarketSession.AM and paths.operational_result_root is not None:
        try:
            return load_intraday_profit_batch(paths.operational_result_root, business_date, session,
                expected_environment=paths.environment, snapshot_root=paths.snapshot_root)
        except IntradayProfitStoreNotFoundError:
            pass
        except (IntradayProfitStoreError, IntradaySnapshotError, OSError):
            return None
    try:
        return load_intraday_profit_batch(paths.result_root, business_date, session,
            expected_environment=paths.environment, snapshot_root=paths.snapshot_root)
    except (IntradayProfitStoreError, IntradaySnapshotError, OSError):
        return None


def _has_snapshot(snapshot_root: Path | None, session: MarketSession) -> bool:
    if snapshot_root is None:
        return False
    try:
        load_latest_intraday_snapshot(snapshot_root, session)
    except (IntradaySnapshotError, OSError):
        return False
    return True


def _load_latest_snapshot(
    snapshot_root: Path | None, session: MarketSession
):
    if snapshot_root is None:
        return None
    try:
        return load_latest_intraday_snapshot(snapshot_root, session)
    except (IntradaySnapshotError, OSError):
        return None


def _render_session_table(
    heading: str,
    release: ResolvedIntradayProfitRelease | None,
    *,
    session: MarketSession,
    market_snapshot_ready: bool,
    origin: str,
    config: SoybeanImportProfitConfig,
) -> None:
    st.subheader(heading)
    if release is None:
        if market_snapshot_ready:
            st.info(f"{session.value}_IMPORT_PROFIT_WAITING_FOR_CNF")
        else:
            st.info(f"{session.value}_NOT_CAPTURED_YET")
        return
    rows = [row for row in release.rows if row.get("origin") == origin]
    st.caption(
        f"Market Session: {release.session.value} | "
        f"Snapshot captured_at: {release.market_captured_at.isoformat()} | "
        f"Business Date: {release.business_date.isoformat()}"
    )
    if not rows:
        st.info("当前产地没有可展示的结果。")
        return
    params = config.resolve_parameters(origin)
    records = []
    for row in sorted(rows, key=lambda item: str(item.get("shipment_period", ""))):
        meal = _number(row.get("soymeal_price_cny_per_tonne"))
        oil = _number(row.get("soyoil_price_cny_per_tonne"))
        available = row.get("availability_status") == "SUCCESS"
        records.append(
            {
                "船期": row.get("shipment_period"),
                "状态": "可用" if available else "暂无可用合约",
                "CNF（美分/蒲）": row.get("cnf_cents_per_bushel"),
                "美金成本（美元/吨）": row.get("usd_cost_per_tonne"),
                "CBOT合约": row.get("cbot_contract"),
                "CBOT价格": row.get("cbot_price_cents_per_bushel"),
                "汇率": row.get("fx_value"),
                "豆粕合约": row.get("soymeal_contract"),
                "豆粕盘面": meal,
                "豆油合约": row.get("soyoil_contract"),
                "豆油盘面": oil,
                "关税": params.tariff_rate,
                "增值税": params.vat_rate,
                "完税成本（元/吨）": row.get("duty_paid_cost_cny_per_tonne"),
                "进口盘面榨利（元/吨）": row.get(
                    "net_crush_margin_cny_per_tonne"
                ),
                "粕价值（元/吨大豆）": None
                if meal is None
                else meal * params.meal_yield,
                "油价值（元/吨大豆）": None
                if oil is None
                else oil * params.oil_yield,
            }
        )
    st.dataframe(
        pd.DataFrame(records),
        hide_index=True,
        width="stretch",
        column_config={
            "关税": st.column_config.NumberColumn(format="%.2%%"),
            "增值税": st.column_config.NumberColumn(format="%.2%%"),
        },
    )


def _render_cnf_history(
    cnf_store_path: Path,
    *,
    origin: str,
    config: SoybeanImportProfitConfig,
) -> None:
    st.subheader("大豆历史 CNF 报价")
    try:
        snapshot = load_cnf_store(
            cnf_store_path, allowed_origins=config.origin_codes
        )
    except Exception:
        st.error("CNF store 校验失败。")
        return
    rows = [
        {
            "日期": item.business_key.business_date,
            "船期": f"{item.business_key.shipment_year:04d}-{item.business_key.shipment_month:02d}",
            "CNF": item.cnf_cents_per_bushel,
        }
        for item in snapshot.records
        if item.business_key.origin == origin
    ]
    if not rows:
        st.info("当前产地尚无 CNF 历史报价。")
        return
    frame = pd.DataFrame(rows).pivot(index="日期", columns="船期", values="CNF")
    frame = frame.sort_index(ascending=False).reset_index()
    st.dataframe(frame, hide_index=True, width="stretch")


def _render_profit_history(
    result_root: Path,
    *,
    origin: str,
    origin_label: str,
) -> None:
    st.subheader("进口盘面榨利历史折线图")
    session_value = st.selectbox(
        "Session",
        options=[MarketSession.PM.value, MarketSession.AM.value],
        index=0,
        key="soybean_intraday:history_session",
    )
    try:
        batches = list_intraday_profit_batches(result_root, session_value)
    except IntradayProfitStoreError:
        st.info("尚无可用的 AM/PM 历史结果。")
        return
    selected_rows = [
        row
        for batch in batches
        for row in batch.rows
        if row.get("origin") == origin
        and row.get("calculation_status") == "success"
        and row.get("availability_status") == "SUCCESS"
        and row.get("net_crush_margin_cny_per_tonne") is not None
    ]
    if not selected_rows:
        st.info("当前筛选条件尚无历史榨利。")
        return
    shipment_options = sorted(
        {str(row["shipment_period"]) for row in selected_rows}
    )
    shipment = st.selectbox(
        "船期", shipment_options, key="soybean_intraday:history_shipment"
    )
    filtered = [row for row in selected_rows if row["shipment_period"] == shipment]
    available_years = sorted(
        {int(str(row["business_date"])[:4]) for row in filtered}, reverse=True
    )
    years = st.multiselect(
        "历史年份",
        available_years,
        default=available_years[:3],
        key="soybean_intraday:history_years",
    )
    figure = go.Figure()
    for year in years:
        year_rows = sorted(
            (
                row
                for row in filtered
                if int(str(row["business_date"])[:4]) == year
            ),
            key=lambda row: str(row["business_date"]),
        )
        figure.add_scatter(
            x=[str(row["business_date"])[5:] for row in year_rows],
            y=[row["net_crush_margin_cny_per_tonne"] for row in year_rows],
            mode="lines+markers",
            name=str(year),
        )
    figure.update_layout(
        title=f"{origin_label} | {shipment} | {session_value}",
        xaxis_title="日期（MM-DD）",
        yaxis_title="进口盘面榨利（元/吨）",
        legend_title="年份",
    )
    st.plotly_chart(
        figure,
        width="stretch",
        key="soybean_intraday:import_profit_history",
    )


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool) or pd.isna(value):
        return None
    return float(value)


__all__ = [
    "FinalUiPreviewPaths",
    "IntradayPageDataPaths",
    "IntradayPageMode",
    "PAGE_TITLE",
    "render_import_profit_intraday_page",
]
