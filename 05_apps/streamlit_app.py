from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from agri_research_agent.domains.spreads.calculation import add_plot_value
from agri_research_agent.application.domestic_spreads import (
    load_domestic_spread_database,
    load_domestic_spread_status,
)
from agri_research_agent.domains.spreads.history import (
    FIVE_YEAR_MEAN_LABEL,
    FIVE_YEAR_MEAN_SMOOTH_WINDOW,
    five_year_mean_seasons,
    is_forbidden_mean_trace_name,
    is_non_season_name,
    latest_plot_value,
    latest_summary,
    prepare_history_view,
    season_sort_key,
)
from agri_research_agent.domains.spreads.parsing import (
    BOARD_OPTIONS,
    classify_board,
    configured_spreads,
    display_spread_name,
    parse_spread_name,
    spread_sort_key,
)
from agri_research_agent.market_data.activated_runtime import (
    resolve_domestic_spread_path,
    resolve_public_data_root,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPS_DIR = PROJECT_ROOT / "05_apps"
if str(APPS_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_DIR))

from basis_page import render_basis_page
from foreign_seats_page import render_foreign_seats_page
from home import get_external_app_url, render_home
from import_profit_runtime_page import render_import_profit_runtime_page
from international_spread_page import render_international_spread_page
from navigation import (
    IMPORT_PROFIT_ROUTE_ID,
    INTERNATIONAL_SPREAD_PAGE_TITLE,
    INDIA_CROP_WEATHER_PAGE_TITLE,
    NAVIGATION_GROUPS,
    PALM_OIL_WEATHER_PAGE_TITLE,
    RAPESEED_WEATHER_PAGE_TITLE,
    RESEARCH_OVERVIEW_PAGE_TITLE,
    SOYBEAN_CROP_PAGE_TITLE,
    SOYBEAN_WEATHER_PAGE_TITLE,
    USDA_PAGE_TITLE,
    internal_workspace_pages,
)
from soybean_weekly_page import render_soybean_weekly_page
from ui_theme import inject_workspace_theme, render_sidebar_navigation
from weather_research_page import render_weather_research_page
from research_overview_page import render_research_overview


PAGE_TITLE = "油脂油料价差动态看板"
DATA_DIR = PROJECT_ROOT / "01_data"
CONFIG_DIR = PROJECT_ROOT / "02_configs"
DATABASE_XLSX_FILE = DATA_DIR / "historical_spread_database.xlsx"
DATABASE_PARQUET_FILE = DATA_DIR / "historical_spread_database.parquet"
SPREAD_CONFIG_FILE = CONFIG_DIR / "historical_spread_config.xlsx"
REPORT_CATALOG_FILE = CONFIG_DIR / "report_catalog.yaml"
_PUBLIC_RUNTIME_VALUE = os.getenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", "").strip()
PUBLIC_RUNTIME_ROOT = (
    Path(_PUBLIC_RUNTIME_VALUE)
    if _PUBLIC_RUNTIME_VALUE
    else PROJECT_ROOT.parents[1]
    / "market-data-worktree-runtime"
    / "international-spread"
)
IMPORT_PROFIT_PAGE_TITLE = "日度进口大豆盘面净榨利"
WEATHER_PAGE_ROUTES = {
    SOYBEAN_WEATHER_PAGE_TITLE: "soybean_weather",
    RAPESEED_WEATHER_PAGE_TITLE: "rapeseed_weather",
    PALM_OIL_WEATHER_PAGE_TITLE: "palm_oil_weather",
    INDIA_CROP_WEATHER_PAGE_TITLE: "india_crop_weather",
}
WORKSPACE_PAGES = list(internal_workspace_pages())
SIDEBAR_NAVIGATION = NAVIGATION_GROUPS
FOREIGN_SEATS_DATABASE_FILE = DATA_DIR / "database" / "foreign_seats" / "foreign_seat_positions.parquet"

def get_database_path() -> Path:
    return resolve_domestic_spread_path(DATA_DIR)


def get_public_basis_current_root() -> Path:
    return (
        resolve_public_data_root(PUBLIC_RUNTIME_ROOT)
        / "public-market-data"
        / "lutou-domestic-basis"
    )


@st.cache_data(show_spinner=False)
def load_database(database_path: Path, mtime: float) -> pd.DataFrame:
    del mtime
    return load_domestic_spread_database(database_path)


@st.cache_data(show_spinner=False)
def load_spread_config(config_path: Path, mtime: float) -> pd.DataFrame:
    del mtime
    if not config_path.exists():
        return pd.DataFrame(columns=["spread_name", "enabled"])
    config = pd.read_excel(config_path, sheet_name="spread_config")
    if "enabled" in config.columns:
        config = config[config["enabled"].fillna(False)].copy()
    return config


def render_update_status(data: pd.DataFrame) -> None:
    status = load_domestic_spread_status(data)
    presentation_status, presentation_label = domestic_spread_status_presentation(
        status.success_contracts,
        status.required_contracts,
    )
    contract_summary = (
        f"{status.success_contracts}/{status.required_contracts} 成功，"
        f"{status.failure_contracts} 项缺失"
        if status.required_contracts > 0
        else "暂无应更新合约"
    )
    message = (
        f"更新状态：{presentation_label} | "
        f"最新交易日：{status.latest_business_date or '-'} | "
        f"合约：{contract_summary} | "
        f"来源：{status.source}"
    )
    if presentation_status == "SUCCESS":
        st.success(message)
    elif presentation_status == "PARTIAL":
        st.warning(message)
    elif presentation_status == "FAILED":
        st.error(message)
    else:
        st.info(message)


def domestic_spread_status_presentation(
    success_contracts: int,
    required_contracts: int,
) -> tuple[str, str]:
    """Map authoritative completeness counts to their page-only presentation."""
    if required_contracts <= 0:
        return "NOT_APPLICABLE", "暂无应更新合约"
    if success_contracts == required_contracts:
        return "SUCCESS", "更新成功"
    if success_contracts > 0:
        return "PARTIAL", "部分更新"
    return "FAILED", "更新失败"


def format_market_number(value: object) -> str:
    if pd.isna(value):
        return ""
    number = float(value)
    if number.is_integer():
        return f"{number:.0f}"
    return f"{number:.1f}"


def validate_five_year_mean_trace(fig: go.Figure) -> None:
    names = [str(trace.name) for trace in fig.data]
    five_year_traces = [name for name in names if name == FIVE_YEAR_MEAN_LABEL]
    forbidden_traces = [
        name
        for name in names
        if name != FIVE_YEAR_MEAN_LABEL and ("均值" in name or is_forbidden_mean_trace_name(name))
    ]
    assert len(five_year_traces) == 1, f"必须且只能存在一条{FIVE_YEAR_MEAN_LABEL} trace，当前为：{five_year_traces}"
    assert not forbidden_traces, f"图中出现了旧均值 trace：{forbidden_traces}"


def build_figure(
    data: pd.DataFrame,
    chart_title: str,
    mean_source_data: pd.DataFrame | None = None,
) -> go.Figure:
    fig = go.Figure()
    history_view = prepare_history_view(data, mean_source_data)
    data = history_view.data

    for season in history_view.seasons:
        season_data = data[data["season"] == season].sort_values("calendar_offset")
        is_latest = season == history_view.latest_season
        line_color = None
        line_width = 2.25
        if is_latest:
            line_color = "#D62728"
            line_width = 5
        fig.add_trace(
            go.Scatter(
                x=season_data["calendar_offset"],
                y=season_data["plot_value"],
                mode="lines",
                name=f"{season}（当前年度）" if is_latest else season,
                line={
                    "width": line_width,
                    "color": line_color,
                },
                customdata=season_data[["season"]],
                hovertemplate="%{customdata[0]}：%{y:.0f}<extra></extra>",
            )
        )

    mean_data = history_view.mean_curve
    if not mean_data.empty:
        fig.add_trace(
            go.Scatter(
                x=mean_data["calendar_offset"],
                y=mean_data["smooth_five_year_mean"],
                mode="lines",
                name=FIVE_YEAR_MEAN_LABEL,
                line={"width": 3, "dash": "dash", "color": "#2CA02C"},
                customdata=mean_data[["month_day"]],
                hovertemplate="%{customdata[0]}<br>五年均值：%{y:.0f}<extra></extra>",
            )
        )

    tick_source = data[["calendar_offset", "month_day"]].drop_duplicates().sort_values("calendar_offset")
    tick_source = tick_source[tick_source["calendar_offset"] % 14 == 0]
    if tick_source.empty:
        tick_source = data[["calendar_offset", "month_day"]].drop_duplicates().iloc[::14]

    fig.update_layout(
        title={"text": f"<b>{chart_title}</b>", "x": 0, "xanchor": "left", "font": {"size": 16, "family": "Noto Sans CJK SC, Noto Sans CJK JP, Noto Sans CJK TC, Arial, sans-serif"}},
        height=350,
        hovermode="x unified",
        legend_title_text="年度",
        legend={"orientation": "h", "x": 0, "xanchor": "left", "y": 1.02, "yanchor": "bottom", "font": {"size": 10}},
        margin={"l": 54, "r": 12, "t": 105, "b": 62},
        xaxis_title={"text": "日期", "font": {"size": 13}},
        yaxis_title={"text": data["value_label"].iloc[0] if not data.empty else "价差（元/吨）", "font": {"size": 13}},
    )
    offset_to_month_day = (
        data[["calendar_offset", "month_day"]]
        .drop_duplicates()
        .sort_values("calendar_offset")
        .set_index("calendar_offset")["month_day"]
        .to_dict()
    )
    fig.update_xaxes(
        tickmode="array",
        tickvals=tick_source["calendar_offset"].tolist(),
        ticktext=tick_source["month_day"].tolist(),
        tickangle=-45,
        labelalias=offset_to_month_day,
        tickfont={"size": 10},
    )
    fig.update_yaxes(tickfont={"size": 10})
    validate_five_year_mean_trace(fig)
    return fig


def latest_metrics(data: pd.DataFrame, mean_source_data: pd.DataFrame | None = None) -> pd.DataFrame:
    summary = latest_summary(data, mean_source_data)
    if summary is None:
        return pd.DataFrame()

    return pd.DataFrame(
        [
            {
                "价差": summary.spread_name,
                "最新 season": summary.latest_season,
                "最新日期": summary.latest_date.strftime("%Y-%m-%d"),
                "最新值": format_market_number(summary.latest_value),
                "五年均值": format_market_number(summary.five_year_mean),
                "较五年均值": format_market_number(summary.difference_from_mean),
                "历史分位数": summary.historical_percentile,
            }
        ]
    )


def render_spread_grid(
    spreads: list[str],
    all_data: pd.DataFrame,
    year_count: int,
    method: str,
) -> None:
    if not spreads:
        st.info("当前配置中没有可用指标。")
        return
    for start in range(0, len(spreads), 2):
        columns = st.columns(2)
        for column, spread_name in zip(columns, spreads[start : start + 2]):
            selected_all = add_plot_value(
                all_data[all_data["spread_name"] == spread_name].copy(),
                method,
            )
            seasons = sorted(selected_all["season"].unique().tolist(), key=season_sort_key)
            selected = selected_all[selected_all["season"].isin(seasons[-year_count:])].copy()
            if selected.empty:
                continue
            latest_value = format_market_number(latest_plot_value(selected)) or "-"
            unit = "元/吨" if method == "绝对价差 A-B" else ""
            chart_title = f"{display_spread_name(spread_name)}｜最新 {latest_value}{unit}"
            with column:
                st.plotly_chart(
                    build_figure(selected, chart_title, mean_source_data=selected_all),
                    use_container_width=True,
                    key=f"spread_chart_{spread_name}_{method}_{year_count}",
                )


def render_spread_dashboard() -> None:
    database_path = get_database_path()
    if not database_path.exists():
        st.error(f"未找到历史价差数据库：{DATABASE_XLSX_FILE}")
        return

    with st.spinner("正在读取历史价差数据库，请稍等..."):
        data = load_database(database_path, database_path.stat().st_mtime)
    success_data = data[data["status"] == "success"].copy()
    config = load_spread_config(SPREAD_CONFIG_FILE, SPREAD_CONFIG_FILE.stat().st_mtime if SPREAD_CONFIG_FILE.exists() else 0)
    board_spreads = configured_spreads(success_data, config)

    with st.sidebar:
        st.header("价差看板")
        board = st.radio("板块", BOARD_OPTIONS, label_visibility="collapsed")
        with st.expander("展示设置"):
            method = st.selectbox("计算方法", ["绝对价差 A-B", "商品比值 A/B"])
            year_count = int(st.number_input("显示年份数量", min_value=1, max_value=30, value=5, step=1))

    st.title(board)
    render_update_status(data)
    if board == "品种间套利":
        for subgroup in ["油脂之间套利", "粕之间套利", "其他"]:
            names = [name for name in board_spreads[board] if classify_board(name)[1] == subgroup]
            if names:
                st.subheader(subgroup)
                render_spread_grid(names, success_data, year_count, method)
    else:
        render_spread_grid(board_spreads[board], success_data, year_count, method)


def render_status_page() -> None:
    st.title("日更运行状态")
    database_path = get_database_path()
    if not database_path.exists():
        st.warning("当前 Domestic Spread artifact 不可用。")
        return
    data = load_database(database_path, database_path.stat().st_mtime)
    render_update_status(data)
    st.json(load_domestic_spread_status(data).as_dict())


def render_usda_page() -> None:
    """Provide a workspace entry point for the independently running USDA app."""
    st.title(USDA_PAGE_TITLE)
    st.caption("全球主要农产品供需平衡表、月度修正与年度供需展示。")
    external_url = get_external_app_url(REPORT_CATALOG_FILE, USDA_PAGE_TITLE)
    if external_url:
        st.link_button("打开 USDA 平衡表", external_url, use_container_width=False)
    else:
        st.info("尚未配置 USDA 平衡表地址。请在报告目录配置或 USDA_DASHBOARD_URL 环境变量中设置访问地址。")


def render_import_profit_route() -> None:
    """Resolve environment configuration only after this route is selected."""

    runtime_root = os.getenv("IMPORT_PROFIT_RUNTIME_ROOT", "").strip()
    configured_path = os.getenv(
        "IMPORT_PROFIT_CONFIG_PATH", ""
    ).strip()
    config_path = (
        Path(configured_path)
        if configured_path
        else CONFIG_DIR / "import_profit_soybean.yaml"
    )
    render_import_profit_runtime_page(
        runtime_root or None,
        config_path=config_path,
    )


def apply_workspace_navigation_request() -> None:
    """Accept a sidebar navigation request before any page widget is created."""
    requested_page = st.query_params.get("workspace_page")
    if isinstance(requested_page, str) and requested_page in WORKSPACE_PAGES:
        st.session_state.selected_workspace_page = requested_page
        del st.query_params["workspace_page"]


def main() -> None:
    st.set_page_config(page_title="油脂油料研究工作台", layout="wide")
    if "selected_workspace_page" not in st.session_state:
        st.session_state.selected_workspace_page = "首页"
    apply_workspace_navigation_request()
    inject_workspace_theme()

    with st.sidebar:
        render_sidebar_navigation(SIDEBAR_NAVIGATION, st.session_state.selected_workspace_page)

    selected_page = st.session_state.selected_workspace_page
    render_selected_workspace_page(selected_page)


def render_selected_workspace_page(selected_page: str) -> None:
    """Dispatch one validated workspace route without preloading others."""

    if selected_page == "首页":
        render_home(REPORT_CATALOG_FILE)
    elif selected_page == RESEARCH_OVERVIEW_PAGE_TITLE:
        render_research_overview()
    elif selected_page == "基差/一口价":
        render_basis_page(get_public_basis_current_root())
    elif selected_page == SOYBEAN_CROP_PAGE_TITLE:
        render_soybean_weekly_page()
    elif selected_page in WEATHER_PAGE_ROUTES:
        render_weather_research_page(WEATHER_PAGE_ROUTES[selected_page])
    elif selected_page == USDA_PAGE_TITLE:
        render_usda_page()
    elif selected_page == "运行监控":
        render_status_page()
    elif selected_page == "外资与重点席位":
        render_foreign_seats_page(FOREIGN_SEATS_DATABASE_FILE)
    elif selected_page == IMPORT_PROFIT_ROUTE_ID:
        render_import_profit_route()
    elif selected_page == INTERNATIONAL_SPREAD_PAGE_TITLE:
        render_international_spread_page(project_root=PROJECT_ROOT)
    else:
        render_spread_dashboard()


if __name__ == "__main__":
    main()

