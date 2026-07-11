from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st
from matplotlib.figure import Figure
from matplotlib.font_manager import fontManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "03_src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agri_research_agent.data_sources.basis_upload import (  # noqa: E402
    update_basis_from_upload,
)


UNIT_LABEL = "元/吨"
EXCLUDED_DISPLAY_COMMODITIES = {"葵油", "一葵"}
YEAR_COLORS = {
    2024: "#4C78A8",
    2025: "#F58518",
    2026: "#D62728",
}
FALLBACK_COLORS = ["#4C78A8", "#F58518", "#D62728"]
COMMODITY_ORDER = [
    "豆粕",
    "菜粕",
    "葵粕",
    "一豆",
    "24度",
    "三菜",
    "一葵",
    "一级玉米油",
]
MONTH_TICKS = pd.to_datetime(
    [f"2000-{month:02d}-01" for month in range(1, 13)]
).dayofyear.tolist()
MONTH_LABELS = [f"{month:02d}-01" for month in range(1, 13)]


def _configure_matplotlib() -> None:
    available = {font.name for font in fontManager.ttflist}
    for font_name in (
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "Arial Unicode MS",
    ):
        if font_name in available:
            plt.rcParams["font.sans-serif"] = [font_name]
            break
    plt.rcParams["axes.unicode_minus"] = False


_configure_matplotlib()


def _missing_columns(
    dataframe: pd.DataFrame,
    required: set[str],
) -> list[str]:
    return sorted(required - set(dataframe.columns))


def _options(dataframe: pd.DataFrame, column: str) -> list[str]:
    if column not in dataframe.columns:
        return []
    return sorted(
        dataframe[column].dropna().astype(str).unique().tolist()
    )


def filter_display_data(dataframe: pd.DataFrame) -> pd.DataFrame:
    if "commodity" not in dataframe.columns:
        return dataframe.copy()
    commodity = dataframe["commodity"].astype("string").str.strip()
    return dataframe[
        ~commodity.isin(EXCLUDED_DISPLAY_COMMODITIES)
    ].copy()


def prepare_cash_price_data(dataframe: pd.DataFrame) -> pd.DataFrame:
    if "cash_price" not in dataframe.columns:
        return pd.DataFrame(columns=dataframe.columns)
    cash_price = pd.to_numeric(dataframe["cash_price"], errors="coerce")
    prepared = dataframe[cash_price.notna()].copy()
    prepared["cash_price"] = cash_price[cash_price.notna()]
    return prepared


def _commodity_sort_key(commodity: str) -> tuple[int, str]:
    try:
        return COMMODITY_ORDER.index(commodity), commodity
    except ValueError:
        return len(COMMODITY_ORDER), commodity


def _preferred_index(options: list[str], preferred: str) -> int:
    return options.index(preferred) if preferred in options else 0


def _latest_three_years(dataframe: pd.DataFrame) -> list[int]:
    years = sorted(
        pd.to_datetime(dataframe["date"], errors="coerce")
        .dropna()
        .dt.year.unique()
        .tolist()
    )
    return years[-3:]


def _module_header(title: str, years: list[int]) -> None:
    year_text = "、".join(str(year) for year in years)
    st.markdown(
        f"""
        <div style="margin-top: 0.2rem;">
          <h2 style="margin: 0; font-weight: 700;">{title}</h2>
          <div style="height: 3px; background: #B91C1C; margin: 0.45rem 0 0.55rem;"></div>
          <div style="color: #666; font-size: 0.9rem;">季节对比年份：{year_text}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def prepare_seasonal_data(
    dataframe: pd.DataFrame,
    value_col: str,
) -> pd.DataFrame:
    missing = _missing_columns(dataframe, {"date", value_col})
    if missing:
        raise ValueError("缺少字段：" + "、".join(missing))

    prepared = dataframe.copy()
    prepared["date"] = pd.to_datetime(prepared["date"], errors="coerce")
    prepared[value_col] = pd.to_numeric(
        prepared[value_col], errors="coerce"
    )
    prepared = prepared.dropna(subset=["date", value_col]).copy()
    prepared["year"] = prepared["date"].dt.year.astype(int)
    prepared["month_day"] = prepared["date"].dt.strftime("%m-%d")
    prepared["day_of_year"] = pd.to_datetime(
        "2000-" + prepared["month_day"],
        format="%Y-%m-%d",
        errors="coerce",
    ).dt.dayofyear
    prepared = prepared.dropna(subset=["day_of_year"])
    return prepared.sort_values(["year", "day_of_year"])


def build_seasonal_report_figure(
    dataframe: pd.DataFrame,
    *,
    value_col: str,
    title: str,
    years: list[int],
    zero_line: bool,
) -> Figure:
    prepared = prepare_seasonal_data(dataframe, value_col)
    prepared = prepared[prepared["year"].isin(years)]
    seasonal = (
        prepared.groupby(["year", "day_of_year"], as_index=False)[value_col]
        .mean()
        .sort_values(["year", "day_of_year"])
    )

    figure, axis = plt.subplots(figsize=(6.2, 3.55), dpi=120)
    for index, year in enumerate(years):
        year_data = seasonal[seasonal["year"] == year]
        if year_data.empty:
            continue
        axis.plot(
            year_data["day_of_year"],
            year_data[value_col],
            color=YEAR_COLORS.get(
                year, FALLBACK_COLORS[index % len(FALLBACK_COLORS)]
            ),
            linewidth=1.8,
            label=str(year),
        )

    if zero_line:
        axis.axhline(0, color="#222222", linewidth=0.9, alpha=0.9)
    axis.set_title(title, fontsize=12, fontweight="bold", pad=9)
    axis.set_xlim(1, 366)
    axis.set_xticks(MONTH_TICKS)
    axis.set_xticklabels(MONTH_LABELS, rotation=45, ha="right", fontsize=7)
    axis.set_ylabel(UNIT_LABEL, fontsize=9)
    axis.grid(True, color="#D9D9D9", linewidth=0.6, alpha=0.8)
    axis.tick_params(axis="y", labelsize=8)
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)
    axis.legend(
        loc="upper right",
        frameon=False,
        fontsize=8,
        ncol=min(3, len(years)),
    )
    figure.tight_layout()
    return figure


def calculate_wholesale_spread(
    dataframe: pd.DataFrame,
    *,
    region: str,
    commodity_a: str,
    commodity_b: str,
    quote_type: str,
    delivery_month: str,
) -> tuple[pd.DataFrame, list[str]]:
    required = {
        "date",
        "region",
        "commodity",
        "quote_type",
        "delivery_month",
        "cash_price",
    }
    missing = _missing_columns(dataframe, required)
    if missing:
        return pd.DataFrame(), missing

    selected = dataframe[
        (dataframe["region"].astype(str) == region)
        & (dataframe["quote_type"].astype(str) == quote_type)
        & (dataframe["delivery_month"].astype(str) == delivery_month)
        & dataframe["commodity"].astype(str).isin(
            [commodity_a, commodity_b]
        )
    ].copy()
    selected["date"] = pd.to_datetime(selected["date"], errors="coerce")
    selected["cash_price"] = pd.to_numeric(
        selected["cash_price"], errors="coerce"
    )
    selected = selected.dropna(subset=["date", "cash_price"])

    available = set(selected["commodity"].astype(str).unique())
    missing_commodities = [
        commodity
        for commodity in (commodity_a, commodity_b)
        if commodity not in available
    ]
    if missing_commodities:
        return pd.DataFrame(), missing_commodities

    wide = selected.pivot_table(
        index="date",
        columns="commodity",
        values="cash_price",
        aggfunc="last",
    ).dropna(subset=[commodity_a, commodity_b])
    if wide.empty:
        return pd.DataFrame(), [commodity_a, commodity_b]

    spread_name = f"{commodity_a}-{commodity_b}"
    result = wide.reset_index()
    result["region"] = region
    result["commodity"] = spread_name
    result["spread_value"] = result[commodity_a] - result[commodity_b]
    return result[
        ["date", "region", "commodity", "spread_value"]
    ], []


def _filter_selector(
    label: str,
    options: list[str],
    *,
    key: str,
    preferred: str | None = None,
    include_all: bool = True,
) -> str:
    selectable = ["全部", *options] if include_all else options
    preferred_value = preferred if preferred in selectable else selectable[0]
    return st.selectbox(
        label,
        selectable,
        index=_preferred_index(selectable, preferred_value),
        key=key,
    )


def _apply_common_filters(
    dataframe: pd.DataFrame,
    *,
    commodity: str,
    region: str,
    quote_type: str,
    delivery_month: str,
    years: list[int],
) -> pd.DataFrame:
    filtered = dataframe.copy()
    dates = pd.to_datetime(filtered["date"], errors="coerce")
    filtered = filtered[dates.dt.year.isin(years)]
    for column, value in (
        ("commodity", commodity),
        ("region", region),
        ("quote_type", quote_type),
        ("delivery_month", delivery_month),
    ):
        if value != "全部":
            filtered = filtered[filtered[column].astype(str) == value]
    return filtered


def _card_pairs(dataframe: pd.DataFrame) -> list[tuple[str, str]]:
    pairs = (
        dataframe[["commodity", "region"]]
        .dropna()
        .astype(str)
        .drop_duplicates()
        .itertuples(index=False, name=None)
    )
    return sorted(
        pairs,
        key=lambda pair: (_commodity_sort_key(pair[0]), pair[1]),
    )


def _render_report_card(
    dataframe: pd.DataFrame,
    *,
    title: str,
    value_col: str,
    years: list[int],
    zero_line: bool,
    detail_columns: list[str],
) -> None:
    with st.container(border=True):
        if dataframe.empty:
            st.markdown(f"#### {title}")
            st.info("缺少对应数据。")
            return
        figure = build_seasonal_report_figure(
            dataframe,
            value_col=value_col,
            title=title,
            years=years,
            zero_line=zero_line,
        )
        st.pyplot(figure, width="stretch")
        plt.close(figure)
        with st.expander("查看明细", expanded=False):
            available_columns = [
                column
                for column in detail_columns
                if column in dataframe.columns
            ]
            detail = dataframe[available_columns].sort_values(
                "date", ascending=False
            )
            st.dataframe(detail, hide_index=True, width="stretch")


def _render_matrix(
    dataframe: pd.DataFrame,
    *,
    value_col: str,
    suffix: str,
    years: list[int],
    zero_line: bool,
    detail_columns: list[str],
) -> None:
    pairs = _card_pairs(dataframe)
    if not pairs:
        st.info("当前筛选条件下没有可展示的数据。")
        return
    columns = st.columns(2)
    for index, (commodity, region) in enumerate(pairs):
        card_data = dataframe[
            (dataframe["commodity"].astype(str) == commodity)
            & (dataframe["region"].astype(str) == region)
        ]
        with columns[index % 2]:
            _render_report_card(
                card_data,
                title=f"{region}{commodity}（{suffix}）",
                value_col=value_col,
                years=years,
                zero_line=zero_line,
                detail_columns=detail_columns,
            )


def _render_basis_tab(dataframe: pd.DataFrame, years: list[int]) -> None:
    _module_header("基差", years)
    required = {
        "date",
        "commodity",
        "region",
        "quote_type",
        "delivery_month",
        "cash_price",
        "futures_price",
    }
    missing = _missing_columns(dataframe, required)
    if missing:
        st.info("缺少字段：" + "、".join(missing))
        return

    basis_data = dataframe.copy()
    calculated = (
        pd.to_numeric(basis_data["cash_price"], errors="coerce")
        - pd.to_numeric(basis_data["futures_price"], errors="coerce")
    )
    if "basis" not in basis_data.columns:
        basis_data["basis"] = calculated
    else:
        basis_data["basis"] = pd.to_numeric(
            basis_data["basis"], errors="coerce"
        ).fillna(calculated)
    basis_data = basis_data.dropna(subset=["basis"])

    filters = st.columns(4)
    with filters[0]:
        commodity = _filter_selector(
            "品种",
            sorted(
                _options(basis_data, "commodity"),
                key=_commodity_sort_key,
            ),
            key="basis_matrix_commodity",
        )
    with filters[1]:
        region = _filter_selector(
            "地区",
            _options(basis_data, "region"),
            key="basis_matrix_region",
        )
    with filters[2]:
        quote_type = _filter_selector(
            "报价类型",
            _options(basis_data, "quote_type"),
            key="basis_matrix_quote_type",
            preferred="基差报价",
        )
    with filters[3]:
        delivery_month = _filter_selector(
            "提货月",
            _options(basis_data, "delivery_month"),
            key="basis_matrix_delivery",
            preferred="现货",
            include_all=False,
        )

    filtered = _apply_common_filters(
        basis_data,
        commodity=commodity,
        region=region,
        quote_type=quote_type,
        delivery_month=delivery_month,
        years=years,
    )
    _render_matrix(
        filtered,
        value_col="basis",
        suffix="基差",
        years=years,
        zero_line=True,
        detail_columns=[
            "date",
            "commodity",
            "region",
            "delivery_month",
            "futures_contract",
            "cash_price",
            "futures_price",
            "basis",
        ],
    )


def _render_cash_price_tab(dataframe: pd.DataFrame, years: list[int]) -> None:
    _module_header("一口价", years)
    required = {
        "date",
        "commodity",
        "region",
        "quote_type",
        "delivery_month",
        "cash_price",
    }
    missing = _missing_columns(dataframe, required)
    if missing:
        st.info("缺少字段：" + "、".join(missing))
        return

    cash_data = prepare_cash_price_data(dataframe)
    if cash_data.empty:
        st.info(
            "一口价暂无数据：cash_price 全为空，"
            "或当前正式数据库未包含有效现货价格。"
        )
        return
    filters = st.columns(4)
    with filters[0]:
        commodity = _filter_selector(
            "品种",
            sorted(
                _options(cash_data, "commodity"),
                key=_commodity_sort_key,
            ),
            key="cash_matrix_commodity",
        )
    with filters[1]:
        region = _filter_selector(
            "地区",
            _options(cash_data, "region"),
            key="cash_matrix_region",
        )
    with filters[2]:
        quote_type = _filter_selector(
            "报价类型",
            _options(cash_data, "quote_type"),
            key="cash_matrix_quote_type",
            preferred="全部",
        )
    with filters[3]:
        delivery_month = _filter_selector(
            "提货月",
            _options(cash_data, "delivery_month"),
            key="cash_matrix_delivery",
            preferred="现货",
            include_all=False,
        )

    filtered = _apply_common_filters(
        cash_data,
        commodity=commodity,
        region=region,
        quote_type=quote_type,
        delivery_month=delivery_month,
        years=years,
    )
    if filtered.empty:
        st.info(
            "一口价筛选后无数据：请检查品种、地区、报价类型或提货月。"
        )
        return
    _render_matrix(
        filtered,
        value_col="cash_price",
        suffix="一口价",
        years=years,
        zero_line=False,
        detail_columns=[
            "date",
            "commodity",
            "region",
            "quote_type",
            "delivery_month",
            "cash_price",
        ],
    )


def _render_wholesale_spread_tab(
    dataframe: pd.DataFrame,
    years: list[int],
) -> None:
    _module_header("批发价差", years)
    required = {
        "date",
        "commodity",
        "region",
        "quote_type",
        "delivery_month",
        "cash_price",
    }
    missing = _missing_columns(dataframe, required)
    if missing:
        st.info("缺少字段：" + "、".join(missing))
        return

    cash_data = prepare_cash_price_data(dataframe)
    commodities = sorted(
        _options(cash_data, "commodity"),
        key=_commodity_sort_key,
    )
    filters = st.columns(5)
    with filters[0]:
        region_choice = _filter_selector(
            "地区",
            _options(cash_data, "region"),
            key="spread_matrix_region",
        )
    with filters[1]:
        commodity_a = _filter_selector(
            "品种A",
            commodities,
            key="spread_matrix_commodity_a",
            preferred="豆粕",
            include_all=False,
        )
    with filters[2]:
        commodity_b = _filter_selector(
            "品种B",
            commodities,
            key="spread_matrix_commodity_b",
            preferred="菜粕",
            include_all=False,
        )
    with filters[3]:
        quote_type = _filter_selector(
            "报价类型",
            _options(cash_data, "quote_type"),
            key="spread_matrix_quote_type",
            preferred="基差报价",
            include_all=False,
        )
    with filters[4]:
        delivery_month = _filter_selector(
            "提货月",
            _options(cash_data, "delivery_month"),
            key="spread_matrix_delivery",
            preferred="现货",
            include_all=False,
        )

    if commodity_a == commodity_b:
        st.info("品种A和品种B不能相同。")
        return

    candidate = cash_data[
        (cash_data["quote_type"].astype(str) == quote_type)
        & (cash_data["delivery_month"].astype(str) == delivery_month)
        & cash_data["commodity"].astype(str).isin(
            [commodity_a, commodity_b]
        )
    ]
    regions = _options(candidate, "region")
    selected_regions = (
        regions if region_choice == "全部" else [region_choice]
    )
    columns = st.columns(2)
    spread_name = f"{commodity_a}-{commodity_b}"
    for index, region in enumerate(selected_regions):
        spread, missing_commodities = calculate_wholesale_spread(
            cash_data,
            region=region,
            commodity_a=commodity_a,
            commodity_b=commodity_b,
            quote_type=quote_type,
            delivery_month=delivery_month,
        )
        if not spread.empty:
            dates = pd.to_datetime(spread["date"], errors="coerce")
            spread = spread[dates.dt.year.isin(years)]
        with columns[index % 2]:
            if missing_commodities:
                with st.container(border=True):
                    st.markdown(f"#### {region} {spread_name}（价差）")
                    st.info(
                        "缺少对应品种数据："
                        + "、".join(missing_commodities)
                    )
                continue
            _render_report_card(
                spread,
                title=f"{region} {spread_name}（价差）",
                value_col="spread_value",
                years=years,
                zero_line=True,
                detail_columns=[
                    "date",
                    "region",
                    "commodity",
                    "spread_value",
                ],
            )


def _render_upload_update() -> None:
    with st.expander("上传更新国内基差 Excel", expanded=False):
        st.caption(
            "仅支持 .xlsx。上传成功后会先备份旧文件，再生成最新正式 parquet。"
        )
        uploaded_file = st.file_uploader(
            "选择最新国内现货基差 Excel",
            type=["xlsx"],
            key="basis_excel_upload",
        )
        if st.button(
            "备份并导入更新",
            type="primary",
            disabled=uploaded_file is None,
            key="basis_excel_upload_submit",
        ):
            try:
                with st.spinner("正在备份、校验并导入数据..."):
                    result = update_basis_from_upload(
                        uploaded_file.getvalue(),
                        uploaded_file.name,
                    )
                st.session_state["basis_upload_result"] = {
                    "success": True,
                    "output_file": str(result["output_file"]),
                    "rows": result["rows"],
                    "latest_date": result["latest_date"].strftime(
                        "%Y-%m-%d"
                    ),
                    "modified_time": result["modified_time"].strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "backup_file": (
                        str(result["backup_file"])
                        if result["backup_file"] is not None
                        else "-"
                    ),
                }
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.session_state["basis_upload_result"] = {
                    "success": False,
                    "error": str(exc),
                }
                st.rerun()

        result = st.session_state.get("basis_upload_result")
        if result:
            if result["success"]:
                st.success("导入成功，图表已读取最新正式数据。")
                st.write(f"Parquet 路径：`{result['output_file']}`")
                status_columns = st.columns(3)
                status_columns[0].metric("总行数", f"{result['rows']:,}")
                status_columns[1].metric(
                    "最新日期", result["latest_date"]
                )
                status_columns[2].metric(
                    "修改时间", result["modified_time"]
                )
                st.caption(f"旧 Excel 备份：{result['backup_file']}")
            else:
                st.error(f"导入失败：{result['error']}")


def render_basis_page(
    formal_database_path: Path,
    sample_database_path: Path,
) -> None:
    st.markdown(
        "<h1 style='text-align:center;'>国内现货基差、一口价及价差</h1>",
        unsafe_allow_html=True,
    )
    _render_upload_update()

    if formal_database_path.exists():
        database_path = formal_database_path
        source_label = "正式数据"
    elif sample_database_path.exists():
        database_path = sample_database_path
        source_label = "样例数据"
    else:
        st.info("暂未找到正式基差数据库或样例数据库。")
        return

    try:
        data = pd.read_parquet(database_path)
        data["date"] = pd.to_datetime(data["date"], errors="coerce")
        data = filter_display_data(data)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"基差数据库读取失败：{exc}")
        return

    latest_date = data["date"].max()
    st.success(
        f"当前读取：{source_label}（{database_path.name}），"
        f"共 {len(data)} 行。"
    )
    if source_label == "正式数据" and sample_database_path.exists():
        try:
            sample_rows = len(pd.read_parquet(sample_database_path))
            st.success(
                "样例回退文件可用（当前未读取），"
                f"共 {sample_rows} 行。"
            )
        except Exception:  # noqa: BLE001
            pass
    status_columns = st.columns(3)
    status_columns[0].metric("数据状态", source_label)
    status_columns[1].metric("总行数", f"{len(data):,}")
    status_columns[2].metric(
        "最新日期",
        latest_date.strftime("%Y-%m-%d")
        if pd.notna(latest_date)
        else "-",
    )
    years = _latest_three_years(data)

    basis_tab, cash_tab, spread_tab = st.tabs(
        ["基差", "一口价", "批发价差"]
    )
    with basis_tab:
        _render_basis_tab(data, years)
    with cash_tab:
        _render_cash_price_tab(data, years)
    with spread_tab:
        _render_wholesale_spread_tab(data, years)
