from __future__ import annotations

import sys
import tempfile
from functools import lru_cache
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st
import yaml
from akshare.futures.cons import get_calendar
from matplotlib.figure import Figure


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "03_src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agri_research_agent.data_sources.basis_database import build_basis_database  # noqa: E402
from agri_research_agent.data_sources.basis_entry import (  # noqa: E402
    adapt_excel_result, classify_duplicates, config as entry_config, parse_fixed_rows, parse_paste, parse_text,
)
from agri_research_agent.utils.matplotlib_config import (  # noqa: E402
    configure_matplotlib_chinese_fonts,
)


UNIT_LABEL = "元/吨"
VALUE_AXIS_LABELS = {
    "basis": f"基差（{UNIT_LABEL}）",
    "cash_price": f"一口价（{UNIT_LABEL}）",
    "spread_value": f"价差（{UNIT_LABEL}）",
}
DISPLAY_COMMODITY_MAP = {
    "一豆": "豆油",
    "24度": "棕榈油",
    "三菜": "菜油",
    "豆粕": "豆粕",
    "菜粕": "菜粕",
}
SUPPORTED_COMMODITY_CODES = set(DISPLAY_COMMODITY_MAP)
EXCLUDED_COMMODITY_CODES = {"一葵", "一级玉米油", "葵粕"}
YEAR_COLORS = {
    2024: "#4C78A8",
    2025: "#F58518",
    2026: "#D62728",
}
FALLBACK_COLORS = ["#4C78A8", "#F58518", "#D62728"]
MAX_MISSING_TRADING_DAYS = 5
HIDDEN_CARD_PAIRS = {
    ("豆粕", "东北"),
    ("豆粕", "西南"),
    ("豆粕", "华中"),
    ("豆粕", "华北"),
}
OIL_CARD_COMMODITIES = {"豆油", "棕榈油", "菜油", "一豆", "24度", "三菜"}
OIL_CARD_REGIONS = {"华东", "华北", "华南"}
COMMODITY_ORDER = [
    "豆油",
    "棕榈油",
    "菜油",
    "豆粕",
    "菜粕",
]
MONTH_TICKS = pd.to_datetime(
    [f"2000-{month:02d}-01" for month in range(1, 13)]
).dayofyear.tolist()
MONTH_LABELS = [f"{month:02d}-01" for month in range(1, 13)]


configure_matplotlib_chinese_fonts()


@lru_cache(maxsize=1)
def china_futures_trading_days() -> pd.DatetimeIndex:
    """Return AKShare's bundled China futures trading calendar."""
    raw_calendar = get_calendar()
    calendar = pd.DatetimeIndex(
        pd.to_datetime(raw_calendar, format="%Y%m%d", errors="raise")
    ).normalize()
    calendar = calendar.drop_duplicates().sort_values()
    required_start = pd.Timestamp("2022-01-01")
    required_end = pd.Timestamp("2026-12-31")
    if calendar.empty or calendar.min() > required_start or calendar.max() < required_end:
        raise RuntimeError("国内期货交易日历覆盖范围不足")
    return calendar


def missing_trading_days_between(
    previous_date: object,
    current_date: object,
    *,
    trading_days: pd.DatetimeIndex | None = None,
) -> int:
    previous = pd.Timestamp(previous_date).normalize()
    current = pd.Timestamp(current_date).normalize()
    if current <= previous:
        raise ValueError("报价日期必须严格递增")
    calendar = (
        china_futures_trading_days()
        if trading_days is None
        else pd.DatetimeIndex(trading_days).normalize().sort_values()
    )
    if previous < calendar.min() or current > calendar.max():
        raise ValueError("报价日期超出国内期货交日历覆盖范围")
    return int(((calendar > previous) & (calendar < current)).sum())


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
    commodity_code = dataframe["commodity"].astype("string").str.strip()
    filtered = dataframe[commodity_code.isin(SUPPORTED_COMMODITY_CODES)].copy()
    filtered["commodity_code"] = commodity_code.loc[filtered.index]
    filtered["commodity"] = filtered["commodity_code"].map(
        DISPLAY_COMMODITY_MAP
    ).astype("string")
    if "delivery_month" in filtered.columns:
        filtered = filtered[
            filtered["delivery_month"].astype(str).eq("现货")
        ].copy()
    return filtered


def prepare_cash_price_data(dataframe: pd.DataFrame) -> pd.DataFrame:
    if "cash_price" not in dataframe.columns:
        return pd.DataFrame(columns=dataframe.columns)
    cash_price = pd.to_numeric(dataframe["cash_price"], errors="coerce")
    prepared = dataframe[cash_price.notna()].copy()
    prepared["cash_price"] = cash_price[cash_price.notna()]
    return prepared


def prepare_latest_basis_table(summary: object) -> pd.DataFrame:
    """Render structured Summary facts without recalculating basis comparisons."""
    payload = summary.to_dict() if hasattr(summary, "to_dict") else dict(summary)
    rows = []
    for quote in payload.get("facts", {}).get("quotes", []):
        comparison = (
            ("0" if quote["change"] == 0 else f'{quote["change"]:+g}')
            if quote.get("change") is not None
            else str(quote.get("missing_reason") or "无前值")
        )
        row = {
            "品种": quote.get("commodity"),
            "地区": quote.get("region"),
            "报价类型": quote.get("quote_type"),
            "交货月": quote.get("delivery_month"),
            "期货合约": quote.get("futures_contract"),
            "现货价": "—" if quote.get("cash_price") is None else f'{quote["cash_price"]:g}',
            "期货价": "—" if quote.get("futures_price") is None else f'{quote["futures_price"]:g}',
            "基差": f'{quote["current_basis"]:g}（{comparison}）',
        }
        if quote.get("quote_point"):
            row = {"品种": row.pop("品种"), "地区": row.pop("地区"), "工厂/报价点": quote["quote_point"], **row}
        rows.append(row)
    result = pd.DataFrame(rows)
    if not result.empty:
        result["__品种顺序"] = result["品种"].map(lambda value: _commodity_sort_key(str(value)))
        result = result.sort_values(["__品种顺序", "地区"], kind="mergesort").drop(columns="__品种顺序").reset_index(drop=True)
    return result


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
    duplicate_dates = prepared.duplicated(["date"], keep=False)
    if duplicate_dates.any():
        dates = sorted(
            prepared.loc[duplicate_dates, "date"]
            .dt.strftime("%Y-%m-%d")
            .unique()
            .tolist()
        )
        raise ValueError("同一图表日期存在重复记录：" + "、".join(dates))
    seasonal = prepared.sort_values(["year", "date"])

    figure, axis = plt.subplots(figsize=(6.2, 3.55), dpi=120)
    for index, year in enumerate(years):
        year_data = seasonal[seasonal["year"] == year]
        if year_data.empty:
            continue
        x_values: list[float] = []
        y_values: list[float] = []
        previous_date: pd.Timestamp | None = None
        for row in year_data.itertuples(index=False):
            current_date = pd.Timestamp(row.date)
            if (
                previous_date is not None
                and missing_trading_days_between(previous_date, current_date)
                > MAX_MISSING_TRADING_DAYS
            ):
                x_values.append(float("nan"))
                y_values.append(float("nan"))
            x_values.append(float(row.day_of_year))
            y_values.append(float(getattr(row, value_col)))
            previous_date = current_date
        axis.plot(
            x_values,
            y_values,
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
    axis.set_ylabel(VALUE_AXIS_LABELS.get(value_col, UNIT_LABEL), fontsize=9)
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
    visible_pairs = [
        pair
        for pair in pairs
        if pair not in HIDDEN_CARD_PAIRS
        and not (
            pair[0] in OIL_CARD_COMMODITIES
            and pair[1] not in OIL_CARD_REGIONS
        )
    ]
    return sorted(
        visible_pairs,
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
    if cash_data.empty:
        st.info(
            "批发价差暂无数据：当前正式数据库未包含可用于价差的现货价格。"
        )
        return
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
    """Keep the legacy workbook importer and add an explicit text-entry flow."""
    cfg = entry_config(PROJECT_ROOT / "02_configs" / "basis_varieties.yaml")
    quote_date = st.date_input("统一报价日期", value=pd.Timestamp.today().date(), key="basis_entry_date")
    source = st.text_input("统一数据来源", value="手工录入", key="basis_entry_source")
    excel_tab, table_tab, paste_tab = st.tabs(["Excel 导入", "表格录入", "批量粘贴"])
    with excel_tab:
        _render_legacy_excel_upload()
    with table_tab:
        columns=["地区","品种","基准合约","基差","一口价","报价名称","备注"]
        blank=pd.DataFrame([["","豆油","Y2609",pd.NA,pd.NA,"",""]],columns=columns)
        editable=st.data_editor(blank,num_rows="dynamic",use_container_width=True,key="basis_table_input",column_config={"品种":st.column_config.SelectboxColumn(options=["豆油","菜油","棕榈油","豆粕","菜粕"]),"基差":st.column_config.NumberColumn(),"一口价":st.column_config.NumberColumn()})
        if st.button("生成表格录入预览",key="basis_table_parse"):
            st.session_state["basis_preview"]=parse_fixed_rows(editable,quote_date,source,cfg,"table")
    with paste_tab:
        raw=st.text_area("地区|品种|基准合约|基差|一口价|报价名称|备注",height=150,key="basis_paste_input")
        if st.button("解析固定格式粘贴",key="basis_paste_parse"):
            st.session_state["basis_preview"]=parse_paste(raw,quote_date,source,cfg)
        with st.expander("实验性快速文字识别",expanded=False):
            st.warning("该功能会推断日期和合约，必须检查预览结果后再写入。")
            natural=st.text_area("自然语言报价",key="basis_natural_input")
            if st.button("实验性识别",key="basis_natural_parse"):
                st.session_state["basis_preview"]=parse_text(natural,quote_date,source,cfg)
    _render_shared_preview()


def _render_shared_preview() -> None:
    preview=st.session_state.get("basis_preview")
    if not isinstance(preview,pd.DataFrame) or preview.empty:return
    path=PROJECT_ROOT/"01_data"/"database"/"basis"/"basis_quotes.parquet"
    existing=pd.read_parquet(path) if path.exists() else pd.DataFrame()
    preview=classify_duplicates(preview,existing)
    edited=st.data_editor(preview,use_container_width=True,key="basis_shared_preview")
    st.caption(f"有效 {int((edited.parse_status=='parsed').sum())}｜待确认 {int((edited.parse_status=='needs_review').sum())}｜无效 {int((edited.parse_status=='invalid').sum())}｜重复 {int(edited.duplicate_status.str.startswith('duplicate').sum())}")
    st.checkbox(
        "冲突时使用新值覆盖",
        key="basis_shared_overwrite",
        disabled=True,
    )
    st.button(
        "确认写入",
        type="primary",
        key="basis_shared_write",
        disabled=True,
    )
    st.caption(
        "正式基差已切换为 basis_price SQL；页面仅保留录入预览，"
        "不得直接改写正式 Parquet。"
    )


def _render_legacy_excel_upload() -> None:
    with st.expander("Excel 只读预览（不写入正式数据）", expanded=False):
        uploaded_file = st.file_uploader("选择 Excel", type=["xlsx"], key="basis_excel_shared")
        if st.button("解析 Excel 到共享预览", disabled=uploaded_file is None, key="basis_excel_shared_parse"):
            try:
                with tempfile.TemporaryDirectory(prefix="basis_excel_preview_") as folder:
                    folder_path = Path(folder); source_path = folder_path / uploaded_file.name
                    source_path.write_bytes(uploaded_file.getvalue())
                    cfg_data = yaml.safe_load((PROJECT_ROOT / "02_configs" / "basis_excel.yaml").read_text(encoding="utf-8"))
                    cfg_data["source_file"]["path"] = str(source_path)
                    cfg_path = folder_path / "basis_excel.yaml"
                    cfg_path.write_text(yaml.safe_dump(cfg_data, allow_unicode=True), encoding="utf-8")
                    output_path = folder_path / "legacy_basis.parquet"
                    build_basis_database(config_path=cfg_path, output_path=output_path)
                    legacy = pd.read_parquet(output_path)
                cfg = entry_config(PROJECT_ROOT / "02_configs" / "basis_varieties.yaml")
                st.session_state["basis_preview"] = adapt_excel_result(legacy, uploaded_file.name, cfg)
                st.success("Excel 已进入共享预览表，请检查后确认写入。")
                st.rerun()
            except Exception as exc:
                st.error(f"Excel 解析失败：{exc}")


def render_basis_page(
    formal_database_path: Path,
    runtime_fallback_path: Path,
) -> None:
    st.markdown(
        "<h1 style='text-align:center;'>国内基差研究</h1>",
        unsafe_allow_html=True,
    )
    if formal_database_path.exists():
        database_path = formal_database_path
        source_label = "正式数据"
    elif runtime_fallback_path.exists():
        database_path = runtime_fallback_path
        source_label = "本地回退数据"
    else:
        st.info("暂未找到正式基差数据库或本地回退数据库。")
        return

    try:
        data = pd.read_parquet(database_path)
        data["date"] = pd.to_datetime(data["date"], errors="coerce")
        data = filter_display_data(data)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"基差数据库读取失败：{exc}")
        return

    try:
        from agri_research_agent.summary_engine.basis import build_basis_summary
        from agri_research_agent.summary_engine.io import file_identity
        summary = build_basis_summary(data, source_identity=file_identity(database_path))
    except (OSError, ValueError, KeyError) as exc:
        st.warning(f"基差摘要暂不可用：{exc}")
        summary = None

    latest_date = data["date"].max()
    if source_label == "正式数据":
        st.caption(
            "数据源：正式国内基差数据库｜"
            f"更新至 {latest_date:%Y-%m-%d}｜"
            "2026-06-01前为历史数据库，之后为basis_price现货基差"
        )
    else:
        st.caption(f"数据源：本地回退数据｜更新至 {latest_date:%Y-%m-%d}")
    st.subheader("最新基差")
    if summary is not None:
        latest_table = prepare_latest_basis_table(summary)
        st.dataframe(latest_table, hide_index=True, width="stretch")
        st.caption(summary.detail_text)
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
