from __future__ import annotations

import datetime as dt
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
from home import render_home


PAGE_TITLE = "油脂油料价差动态看板"
DATA_DIR = PROJECT_ROOT / "01_data"
DATABASE_XLSX_FILE = DATA_DIR / "historical_spread_database.xlsx"
DATABASE_PARQUET_FILE = DATA_DIR / "historical_spread_database.parquet"
UPDATE_STATUS_FILE = DATA_DIR / "update_status.json"
REPORT_CATALOG_FILE = PROJECT_ROOT / "02_configs" / "report_catalog.yaml"
BASIS_DATABASE_FILE = DATA_DIR / "database" / "basis" / "basis_quotes.parquet"
BASIS_SAMPLE_DATABASE_FILE = (
    DATA_DIR / "database" / "basis" / "basis_quotes_sample.parquet"
)
WORKSPACE_PAGES = ["首页", "价差动态看板", "基差/一口价", "运行监控"]

SERIES_SPREADS = {
    "09月主力系列": ["M 9-1", "RM 9-1", "Y 9-1", "OI 9-1", "P 9-1"],
    "01月主力系列": ["M 1-5", "RM 1-5", "Y 1-5", "OI 1-5", "P 1-5"],
    "05月主力系列": ["M 5-9", "RM 5-9", "Y 5-9", "OI 5-9", "P 5-9"],
    "品种套利": [
        "M-RM 1",
        "M-RM 5",
        "M-RM 9",
        "Y-P 1",
        "Y-P 5",
        "Y-P 9",
        "OI-Y 1",
        "OI-Y 5",
        "OI-Y 9",
        "OI-P 1",
        "OI-P 5",
        "OI-P 9",
    ],
    "油粕比": ["Y/M 1", "Y/M 5", "Y/M 9", "OI/RM 1", "OI/RM 5", "OI/RM 9", "P/M 1", "P/M 5", "P/M 9"],
}

SERIES_BUTTONS = ["智能季节推荐", "09月主力系列", "01月主力系列", "05月主力系列", "品种套利", "油粕比"]
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


def classify_spread(spread_name: str) -> str:
    name = str(spread_name)
    if "-" in name.split(" ")[0]:
        return "品种间价差"
    return "月间价差"


def spread_options(data: pd.DataFrame, mode: str) -> list[str]:
    names = sorted(data["spread_name"].dropna().unique().tolist())
    if mode == "全部":
        return names
    return [name for name in names if classify_spread(name) == mode]


def analysis_mode_for_spread(spread_name: str) -> str:
    spread_type = classify_spread(spread_name)
    if spread_type in {"月间价差", "品种间价差"}:
        return spread_type
    return "全部"


def available_first(spreads: list[str], available: set[str]) -> str | None:
    return next((spread for spread in spreads if spread in available), None)


def recommended_series(today: dt.date | None = None) -> list[str]:
    month = (today or dt.date.today()).month
    recommendations: list[str] = []
    if 2 <= month <= 8:
        recommendations.append("09月主力系列")
    if month >= 6 or month <= 1:
        recommendations.append("01月主力系列")
    if month >= 10 or month <= 4:
        recommendations.append("05月主力系列")
    return recommendations


def smart_spreads() -> tuple[list[str], list[str]]:
    groups = recommended_series()
    spreads: list[str] = []
    for group in groups:
        spreads.extend(SERIES_SPREADS[group])
    return groups, spreads


def set_selected_spread(spread_name: str) -> None:
    st.session_state.selected_spread = spread_name
    st.session_state.selected_analysis_mode = analysis_mode_for_spread(spread_name)


def set_series_group(series_group: str, available: set[str]) -> None:
    st.session_state.selected_series_group = series_group
    if series_group == "智能季节推荐":
        _, candidate_spreads = smart_spreads()
    else:
        candidate_spreads = SERIES_SPREADS.get(series_group, [])
    first = available_first(candidate_spreads, available)
    if first:
        set_selected_spread(first)
    elif series_group == "品种套利":
        st.session_state.selected_analysis_mode = "品种间价差"
    elif series_group in {"09月主力系列", "01月主力系列", "05月主力系列"}:
        st.session_state.selected_analysis_mode = "月间价差"


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
    spread_name: str,
    latest_red_only: bool,
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
    previous_season = seasons[-2] if len(seasons) >= 2 else ""

    for season in seasons:
        season_data = data[data["season"] == season].sort_values("calendar_offset")
        is_latest = season == latest_season
        is_previous = season == previous_season
        line_color = None
        line_width = 2
        if is_latest and latest_red_only:
            line_color = "#D62728"
            line_width = 4
        elif is_previous:
            line_color = "#111111"
            line_width = 3
        fig.add_trace(
            go.Scatter(
                x=season_data["calendar_offset"],
                y=season_data["plot_value"],
                mode="lines",
                name=season,
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
        title=spread_name,
        height=620,
        hovermode="x unified",
        legend_title_text="season",
        legend={"orientation": "v", "x": 1.02, "xanchor": "left", "y": 1, "yanchor": "top"},
        margin={"l": 40, "r": 150, "t": 70, "b": 70},
        xaxis_title="month_day",
        yaxis_title=data["value_label"].iloc[0] if not data.empty else "spread_value",
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
    )
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


def render_button_grid(spreads: list[str], available: set[str], columns: int = 5) -> None:
    cols = st.columns(columns)
    for index, spread in enumerate(spreads):
        exists = spread in available
        with cols[index % columns]:
            if st.button(
                spread,
                key=f"watch_{spread}",
                disabled=not exists,
                use_container_width=True,
                help=None if exists else "当前尚未生成该指标",
            ):
                set_selected_spread(spread)
                st.rerun()


def render_spread_dashboard() -> None:
    st.title(PAGE_TITLE)
    render_update_status()

    database_path = get_database_path()
    if not database_path.exists():
        st.error(f"未找到历史价差数据库：{DATABASE_XLSX_FILE}")
        return

    with st.spinner("正在读取历史价差数据库，请稍等..."):
        data = load_database(database_path, database_path.stat().st_mtime)
    success_data = data[data["status"] == "success"].copy()
    available_spreads = set(success_data["spread_name"].dropna().astype(str).unique())

    if "selected_analysis_mode" not in st.session_state:
        st.session_state.selected_analysis_mode = "月间价差"
    if "selected_series_group" not in st.session_state:
        st.session_state.selected_series_group = "智能季节推荐"
    if "selected_spread" not in st.session_state:
        st.session_state.selected_spread = "RM 5-9" if "RM 5-9" in available_spreads else sorted(available_spreads)[0]
    if "latest_red_only" not in st.session_state:
        st.session_state.latest_red_only = True

    with st.sidebar:
        st.header("参数")
        st.subheader("智能看板")
        series_cols = st.columns(2)
        for index, group in enumerate(SERIES_BUTTONS):
            with series_cols[index % 2]:
                if st.button(group, key=f"series_{group}", use_container_width=True):
                    set_series_group(group, available_spreads)
                    st.rerun()

        current_group = st.session_state.selected_series_group
        if current_group == "智能季节推荐":
            groups, watch_spreads = smart_spreads()
            st.caption(f"当前推荐：{' / '.join(groups) if groups else '暂无推荐'}")
        else:
            watch_spreads = SERIES_SPREADS.get(current_group, [])
        st.markdown("**常规看盘**")
        render_button_grid(watch_spreads, available_spreads, columns=2)
        if current_group == "油粕比":
            missing_ratios = [spread for spread in SERIES_SPREADS["油粕比"] if spread not in available_spreads]
            if missing_ratios:
                st.caption("当前尚未生成该指标：" + "、".join(missing_ratios))

        st.divider()
        mode = st.selectbox(
            "分析模式",
            ["月间价差", "品种间价差", "全部"],
            index=["月间价差", "品种间价差", "全部"].index(st.session_state.selected_analysis_mode),
            key=f"analysis_mode_selectbox_{st.session_state.selected_analysis_mode}",
        )
        if mode != st.session_state.selected_analysis_mode:
            st.session_state.selected_analysis_mode = mode
        options = spread_options(success_data, mode)
        if not options:
            st.warning("当前模式下没有可用价差。")
            return
        if st.session_state.selected_spread not in options:
            st.session_state.selected_spread = options[0]
        selected_index = options.index(st.session_state.selected_spread)
        spread_name = st.selectbox(
            "价差选择",
            options,
            index=selected_index,
            key=f"spread_selectbox_{st.session_state.selected_spread}",
        )
        if spread_name != st.session_state.selected_spread:
            st.session_state.selected_spread = spread_name
        method = st.selectbox("计算方法", ["绝对价差 A-B", "商品比值 A/B"])
        year_count = st.number_input("显示年份数量", min_value=1, max_value=30, value=5, step=1)
        latest_red_only = st.checkbox(
            "是否只显示最新年份加粗红线",
            key="latest_red_only",
        )

    spread_name = st.session_state.selected_spread
    selected_all = success_data[success_data["spread_name"] == spread_name].copy()
    selected_all = add_plot_value(selected_all, method)
    seasons = sorted(selected_all["season"].unique().tolist(), key=season_sort_key)
    selected_seasons = seasons[-int(year_count) :]
    selected = selected_all[selected_all["season"].isin(selected_seasons)].copy()

    if selected.empty:
        st.warning("当前筛选没有可用成功记录。")
        return

    fig = build_figure(
        selected,
        spread_name,
        latest_red_only,
        mean_source_data=selected_all,
    )
    chart_key = (
        f"spread_chart_{spread_name}_{st.session_state.selected_analysis_mode}_"
        f"latest_{latest_red_only}_{method}_{int(year_count)}"
    )
    st.plotly_chart(fig, use_container_width=True, key=chart_key)

    st.subheader("最新值与历史对比")
    metrics = latest_metrics(selected, mean_source_data=selected_all)
    st.dataframe(
        metrics.style.format(
            {
                "历史分位数": "{:.1%}",
            },
            na_rep="",
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("当前图表数据")
    display_columns = [
        "date",
        "spread_name",
        "season",
        "month_day",
        "calendar_offset",
        "leg1_price",
        "leg2_price",
        "spread_value",
        "plot_value",
    ]
    st.dataframe(selected.loc[:, display_columns].sort_values(["season", "calendar_offset"]), use_container_width=True)


def render_status_page() -> None:
    st.title("日更运行状态")
    render_update_status()
    if UPDATE_STATUS_FILE.exists():
        try:
            status = load_update_status(UPDATE_STATUS_FILE, UPDATE_STATUS_FILE.stat().st_mtime)
            st.json(status)
        except (OSError, ValueError, json.JSONDecodeError):
            pass


def main() -> None:
    st.set_page_config(page_title="油脂油料研究工作台", layout="wide")
    if "selected_workspace_page" not in st.session_state:
        st.session_state.selected_workspace_page = "首页"

    selected_page = st.session_state.selected_workspace_page
    with st.sidebar:
        st.subheader("研究工作台")
        page_from_nav = st.radio(
            "页面",
            WORKSPACE_PAGES,
            index=WORKSPACE_PAGES.index(selected_page),
            key=f"workspace_navigation_{selected_page}",
            label_visibility="collapsed",
        )
    if page_from_nav != st.session_state.selected_workspace_page:
        st.session_state.selected_workspace_page = page_from_nav
        selected_page = page_from_nav

    if selected_page == "首页":
        render_home(REPORT_CATALOG_FILE)
    elif selected_page == "基差/一口价":
        render_basis_page(BASIS_DATABASE_FILE, BASIS_SAMPLE_DATABASE_FILE)
    elif selected_page == "运行监控":
        render_status_page()
    else:
        render_spread_dashboard()


if __name__ == "__main__":
    main()

