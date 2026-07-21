from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPS_DIR = PROJECT_ROOT / "05_apps"
if str(APPS_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_DIR))

from basis_page import render_basis_page
from foreign_seats_page import render_foreign_seats_page
from home import apply_home_navigation_request, get_external_app_url, render_home
from soybean_crop_progress_page import render_soybean_crop_progress_page
from ui_theme import inject_workspace_theme, render_sidebar_navigation
from weather_research_page import render_weather_research_page


PAGE_TITLE = "油脂油料价差动态看板"
DATA_DIR = PROJECT_ROOT / "01_data"
CONFIG_DIR = PROJECT_ROOT / "02_configs"
DATABASE_XLSX_FILE = DATA_DIR / "historical_spread_database.xlsx"
DATABASE_PARQUET_FILE = DATA_DIR / "historical_spread_database.parquet"
SPREAD_CONFIG_FILE = CONFIG_DIR / "historical_spread_config.xlsx"
UPDATE_STATUS_FILE = DATA_DIR / "update_status.json"
REPORT_CATALOG_FILE = CONFIG_DIR / "report_catalog.yaml"
BASIS_DATABASE_FILE = DATA_DIR / "database" / "basis" / "basis_quotes.parquet"
BASIS_RUNTIME_FALLBACK_FILE = (
    DATA_DIR / "database" / "basis" / "basis_quotes_sample.parquet"
)
USDA_PAGE_TITLE = "USDA平衡表"
SOYBEAN_CROP_PAGE_TITLE = "美豆种植生长"
SOYBEAN_WEATHER_PAGE_TITLE = "大豆天气"
RAPESEED_WEATHER_PAGE_TITLE = "菜籽天气"
PALM_OIL_WEATHER_PAGE_TITLE = "棕榈油天气"
INDIA_CROP_WEATHER_PAGE_TITLE = "印度作物天气"
WEATHER_PAGE_ROUTES = {
    SOYBEAN_WEATHER_PAGE_TITLE: "soybean_weather",
    RAPESEED_WEATHER_PAGE_TITLE: "rapeseed_weather",
    PALM_OIL_WEATHER_PAGE_TITLE: "palm_oil_weather",
    INDIA_CROP_WEATHER_PAGE_TITLE: "india_crop_weather",
}
WORKSPACE_PAGES = [
    "首页",
    "价差动态看板",
    "基差/一口价",
    SOYBEAN_CROP_PAGE_TITLE,
    *WEATHER_PAGE_ROUTES,
    USDA_PAGE_TITLE,
    "外资与重点席位",
    "运行监控",
]
SIDEBAR_NAVIGATION = (
    ("工作台", (("工作台首页", "首页", None),)),
    ("市场行情", (("价差动态", "价差动态看板", None), ("国内现货（基差与一口价）", "基差/一口价", None))),
    ("周度跟踪", (("美豆周度跟踪", SOYBEAN_CROP_PAGE_TITLE, None),)),
    ("天气研究", (("大豆天气", SOYBEAN_WEATHER_PAGE_TITLE, None), ("菜籽天气", RAPESEED_WEATHER_PAGE_TITLE, None), ("棕榈油天气", PALM_OIL_WEATHER_PAGE_TITLE, None), ("印度作物天气", INDIA_CROP_WEATHER_PAGE_TITLE, None))),
    ("国际供需", (("USDA供需平衡", USDA_PAGE_TITLE, None), ("Oil World供需平衡", "", "OIL_WORLD_DASHBOARD_URL"))),
    ("研究工具", (("外资与重点席位", "外资与重点席位", None), ("运行监控", "运行监控", None))),
)
FOREIGN_SEATS_DATABASE_FILE = DATA_DIR / "database" / "foreign_seats" / "foreign_seat_positions.parquet"

BOARD_OPTIONS = ["豆系月差", "棕榈油与菜系月差", "品种间套利"]
INSTRUMENT_LABELS = {"M": "豆粕", "Y": "豆油", "RM": "菜粕", "OI": "菜油", "P": "棕榈油"}
SOY_INSTRUMENTS = {"M", "Y"}
PALM_RAPESEED_INSTRUMENTS = {"P", "OI", "RM"}
OIL_INSTRUMENTS = {"Y", "OI", "P"}
MEAL_INSTRUMENTS = {"M", "RM"}
FIVE_YEAR_MEAN_LABEL = "五年均值"
FIVE_YEAR_MEAN_SMOOTH_WINDOW = 7
NON_SEASON_KEYWORDS = ["历史均值", "十年均值", "五年均值", "均值", "mean", "avg", "average", "five_year", "五年", "十年"]
FORBIDDEN_MEAN_TRACE_KEYWORDS = ["历史均值", "十年均值", "全部历史", "mean", "avg", "average", "five_year"]


def get_database_path() -> Path:
    return DATABASE_PARQUET_FILE if DATABASE_PARQUET_FILE.exists() else DATABASE_XLSX_FILE


@st.cache_data(show_spinner=False)
def load_database(database_path: Path, mtime: float) -> pd.DataFrame:
    del mtime
    if database_path.suffix.lower() == ".parquet":
        data = pd.read_parquet(database_path)
    else:
        data = pd.read_excel(database_path, sheet_name="spread_long")
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data["calendar_offset"] = pd.to_numeric(data["calendar_offset"], errors="coerce")
    data["spread_value"] = pd.to_numeric(data["spread_value"], errors="coerce")
    data["leg1_price"] = pd.to_numeric(data["leg1_price"], errors="coerce")
    data["leg2_price"] = pd.to_numeric(data["leg2_price"], errors="coerce")
    data = data.dropna(subset=["date", "calendar_offset", "season"])
    data["season"] = data["season"].astype(str)
    data["spread_name"] = data["spread_name"].astype(str)
    return data


@st.cache_data(show_spinner=False)
def load_spread_config(config_path: Path, mtime: float) -> pd.DataFrame:
    del mtime
    if not config_path.exists():
        return pd.DataFrame(columns=["spread_name", "enabled"])
    config = pd.read_excel(config_path, sheet_name="spread_config")
    if "enabled" in config.columns:
        config = config[config["enabled"].fillna(False)].copy()
    return config


@st.cache_data(show_spinner=False)
def load_update_status(status_path: Path, mtime: float) -> dict[str, object]:
    del mtime
    return json.loads(status_path.read_text(encoding="utf-8"))


def render_update_status() -> None:
    if not UPDATE_STATUS_FILE.exists():
        st.info("未找到更新状态文件。")
        return
    try:
        status = load_update_status(UPDATE_STATUS_FILE, UPDATE_STATUS_FILE.stat().st_mtime)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        st.warning(f"更新状态文件读取失败：{exc}")
        return

    status_name = str(status.get("status", "unknown"))
    message = (
        f"更新状态：{status_name} | "
        f"开始：{status.get('started_at') or '-'} | "
        f"结束：{status.get('finished_at') or '-'} | "
        f"最新交易日：{status.get('latest_date') or '-'} | "
        f"合约：{status.get('success_contracts', 0)}/{status.get('required_contracts', 0)} 成功，"
        f"{status.get('failure_contracts', 0)} 失败"
    )
    if status_name == "success":
        st.success(message)
    elif status_name in {"failed", "skipped_locked"}:
        st.warning(message)
    else:
        st.info(message)


def season_sort_key(season: str) -> int:
    try:
        return int(str(season).split("/")[0])
    except (TypeError, ValueError):
        return -1


def parse_spread_name(spread_name: str) -> tuple[list[str], list[int]] | None:
    name, _, delivery = str(spread_name).partition(" ")
    instruments = name.split("-")
    if not delivery:
        return None
    month_parts = delivery.split("-")
    try:
        months = [int(month) for month in month_parts]
    except ValueError:
        return None
    if not instruments or not all(instrument in INSTRUMENT_LABELS for instrument in instruments):
        return None
    return instruments, months


def classify_board(spread_name: str) -> tuple[str, str]:
    """Classify configured spreads from their instrument legs; unknowns remain visible."""
    name = str(spread_name)
    if "/" in name or any(keyword in name.lower() for keyword in ["压榨", "crush", "ratio"]):
        return "排除", "比值或利润"
    parsed = parse_spread_name(name)
    if parsed is None:
        return "品种间套利", "其他"
    instruments, months = parsed
    if len(instruments) == 1 and len(months) == 2:
        if instruments[0] in SOY_INSTRUMENTS:
            return "豆系月差", "月差"
        if instruments[0] in PALM_RAPESEED_INSTRUMENTS:
            return "棕榈油与菜系月差", "月差"
        return "品种间套利", "其他"
    if len(instruments) == 2 and len(months) == 1:
        instrument_set = set(instruments)
        if instrument_set <= OIL_INSTRUMENTS:
            return "品种间套利", "油脂之间套利"
        if instrument_set <= MEAL_INSTRUMENTS:
            return "品种间套利", "粕之间套利"
        if instrument_set & OIL_INSTRUMENTS and instrument_set & MEAL_INSTRUMENTS:
            return "排除", "油粕跨类"
    return "品种间套利", "其他"


def display_spread_name(spread_name: str) -> str:
    parsed = parse_spread_name(spread_name)
    if parsed is None:
        return str(spread_name)
    instruments, months = parsed
    labels = [INSTRUMENT_LABELS[instrument] for instrument in instruments]
    if len(instruments) == 1 and len(months) == 2:
        return f"{labels[0]} {months[0]:02d}-{months[1]:02d}"
    if len(instruments) == 2 and len(months) == 1:
        preferred_order = {"Y": 0, "OI": 1, "P": 2, "M": 0, "RM": 1}
        labels = [label for _, label in sorted(zip(instruments, labels), key=lambda item: preferred_order[item[0]])]
        return f"{'-'.join(labels)} {months[0]:02d}"
    return str(spread_name)


def spread_sort_key(spread_name: str) -> tuple[int, int, int, str]:
    """Use dashboard order instead of source/config-file row order."""
    parsed = parse_spread_name(spread_name)
    if parsed is None:
        return (9, 9, 9, str(spread_name))
    instruments, months = parsed
    if len(instruments) == 1 and len(months) == 2:
        instrument_order = {"Y": 0, "M": 1, "P": 0, "OI": 1, "RM": 2}
        month_order = {(9, 1): 0, (1, 5): 1, (5, 9): 2}
        return (0, instrument_order.get(instruments[0], 9), month_order.get(tuple(months), 9), str(spread_name))
    if len(instruments) == 2 and len(months) == 1:
        pair_order = {
            frozenset({"Y", "P"}): 0,
            frozenset({"Y", "OI"}): 1,
            frozenset({"OI", "P"}): 2,
            frozenset({"M", "RM"}): 3,
        }
        month_order = {1: 0, 5: 1, 9: 2}
        return (1, pair_order.get(frozenset(instruments), 9), month_order.get(months[0], 9), str(spread_name))
    return (9, 9, 9, str(spread_name))


def configured_spreads(data: pd.DataFrame, config: pd.DataFrame) -> dict[str, list[str]]:
    available = set(data["spread_name"].dropna().astype(str))
    configured = config.get("spread_name", pd.Series(dtype=str)).dropna().astype(str).tolist()
    names = [name for name in configured if name in available]
    names.extend(sorted(available - set(names)))
    grouped = {board: [] for board in BOARD_OPTIONS}
    for name in names:
        board, _ = classify_board(name)
        if board in grouped:
            grouped[board].append(name)
    for board in grouped:
        grouped[board] = sorted(grouped[board], key=spread_sort_key)
    return grouped


def add_plot_value(data: pd.DataFrame, method: str) -> pd.DataFrame:
    plotted = data.copy()
    if method == "商品比值 A/B":
        plotted["plot_value"] = plotted["leg1_price"] / plotted["leg2_price"]
        plotted.loc[plotted["leg2_price"] == 0, "plot_value"] = pd.NA
        plotted["value_label"] = "商品比值 A/B"
    else:
        plotted["plot_value"] = plotted["spread_value"]
        plotted["value_label"] = "绝对价差 A-B"
    return plotted.dropna(subset=["plot_value"])


def format_market_number(value: object) -> str:
    if pd.isna(value):
        return ""
    number = float(value)
    if number.is_integer():
        return f"{number:.0f}"
    return f"{number:.1f}"


def latest_plot_value(data: pd.DataFrame) -> object:
    """Return the most recent plotted value without changing the source data."""
    seasons = sorted(data["season"].unique().tolist(), key=season_sort_key)
    if not seasons:
        return pd.NA
    latest_data = data[data["season"] == seasons[-1]].sort_values("calendar_offset")
    latest_values = latest_data["plot_value"].dropna()
    return latest_values.iloc[-1] if not latest_values.empty else pd.NA


def is_non_season_name(name: object) -> bool:
    lowered = str(name).lower()
    return any(keyword.lower() in lowered for keyword in NON_SEASON_KEYWORDS)


def five_year_mean_seasons(history_seasons: list[str]) -> list[str]:
    sample_size = 5
    return history_seasons[-sample_size:]


def is_forbidden_mean_trace_name(name: object) -> bool:
    lowered = str(name).lower()
    return any(keyword.lower() in lowered for keyword in FORBIDDEN_MEAN_TRACE_KEYWORDS)


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
    data = data[
        ~data["season"].astype(str).str.contains("|".join(NON_SEASON_KEYWORDS), case=False, na=False)
    ].copy()
    if mean_source_data is None:
        mean_source_data = data.copy()
    else:
        mean_source_data = mean_source_data[
            ~mean_source_data["season"].astype(str).str.contains("|".join(NON_SEASON_KEYWORDS), case=False, na=False)
        ].copy()
    seasons = sorted(data["season"].unique().tolist(), key=season_sort_key)
    mean_source_seasons = sorted(mean_source_data["season"].unique().tolist(), key=season_sort_key)
    latest_season = mean_source_seasons[-1] if mean_source_seasons else (seasons[-1] if seasons else "")

    for season in seasons:
        season_data = data[data["season"] == season].sort_values("calendar_offset")
        is_latest = season == latest_season
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

    if len(mean_source_seasons) >= 2:
        history_seasons = [season for season in mean_source_seasons if season != latest_season]
        selected_mean_seasons = five_year_mean_seasons(history_seasons)
        history = mean_source_data[mean_source_data["season"].isin(selected_mean_seasons)].copy()
        mean_data = (
            history.groupby("calendar_offset", as_index=False)
            .agg(raw_five_year_mean=("plot_value", "mean"), month_day=("month_day", "first"))
            .sort_values("calendar_offset")
        )
        if not mean_data.empty:
            mean_data["smooth_five_year_mean"] = (
                mean_data["raw_five_year_mean"]
                .rolling(window=FIVE_YEAR_MEAN_SMOOTH_WINDOW, center=True, min_periods=2)
                .mean()
            )
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
        legend={"orientation": "v", "x": 0.99, "xanchor": "right", "y": 1, "yanchor": "top", "font": {"size": 10}},
        margin={"l": 54, "r": 12, "t": 54, "b": 62},
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
    seasons = sorted(data["season"].unique().tolist(), key=season_sort_key)
    if not seasons:
        return pd.DataFrame()
    latest_season = seasons[-1]
    latest_season_data = data[data["season"] == latest_season].sort_values("date")
    latest_row = latest_season_data.iloc[-1]

    if mean_source_data is None:
        mean_source_data = data
    source_seasons = sorted(mean_source_data["season"].unique().tolist(), key=season_sort_key)
    source_latest_season = source_seasons[-1] if source_seasons else latest_season
    history_seasons = [season for season in source_seasons if season != source_latest_season]
    selected_mean_seasons = five_year_mean_seasons(history_seasons)
    history_same_offset = mean_source_data[
        (mean_source_data["season"].isin(selected_mean_seasons))
        & (mean_source_data["calendar_offset"] == latest_row["calendar_offset"])
    ]["plot_value"].dropna()
    five_year_mean = history_same_offset.mean() if not history_same_offset.empty else pd.NA
    mean_diff = latest_row["plot_value"] - five_year_mean if pd.notna(five_year_mean) else pd.NA
    percentile = (
        (history_same_offset <= latest_row["plot_value"]).mean()
        if not history_same_offset.empty
        else pd.NA
    )

    return pd.DataFrame(
        [
            {
                "价差": latest_row["spread_name"],
                "最新 season": latest_season,
                "最新日期": latest_row["date"].strftime("%Y-%m-%d"),
                "最新值": format_market_number(latest_row["plot_value"]),
                "五年均值": format_market_number(five_year_mean),
                "较五年均值": format_market_number(mean_diff),
                "历史分位数": percentile,
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
    render_update_status()
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
    render_update_status()
    if UPDATE_STATUS_FILE.exists():
        try:
            status = load_update_status(UPDATE_STATUS_FILE, UPDATE_STATUS_FILE.stat().st_mtime)
            st.json(status)
        except (OSError, ValueError, json.JSONDecodeError):
            pass


def render_usda_page() -> None:
    """Provide a workspace entry point for the independently running USDA app."""
    st.title(USDA_PAGE_TITLE)
    st.caption("全球主要农产品供需平衡表、月度修正与年度供需展示。")
    external_url = get_external_app_url(REPORT_CATALOG_FILE, USDA_PAGE_TITLE)
    if external_url:
        st.link_button("打开 USDA 平衡表", external_url, use_container_width=False)
    else:
        st.info("尚未配置 USDA 平衡表地址。请在报告目录配置或 USDA_DASHBOARD_URL 环境变量中设置访问地址。")


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
    apply_home_navigation_request()
    apply_workspace_navigation_request()
    inject_workspace_theme()

    with st.sidebar:
        render_sidebar_navigation(SIDEBAR_NAVIGATION, st.session_state.selected_workspace_page)

    selected_page = st.session_state.selected_workspace_page

    if selected_page == "首页":
        render_home(REPORT_CATALOG_FILE)
    elif selected_page == "基差/一口价":
        render_basis_page(BASIS_DATABASE_FILE, BASIS_RUNTIME_FALLBACK_FILE)
    elif selected_page == SOYBEAN_CROP_PAGE_TITLE:
        render_soybean_crop_progress_page()
    elif selected_page in WEATHER_PAGE_ROUTES:
        render_weather_research_page(WEATHER_PAGE_ROUTES[selected_page])
    elif selected_page == USDA_PAGE_TITLE:
        render_usda_page()
    elif selected_page == "运行监控":
        render_status_page()
    elif selected_page == "外资与重点席位":
        render_foreign_seats_page(FOREIGN_SEATS_DATABASE_FILE)
    else:
        render_spread_dashboard()


if __name__ == "__main__":
    main()

