"""White sugar spread research views. Reads verified daily quotes from disk."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from agri_research_agent.application.sugar_spreads import (
    DATA_PATH, DOMESTIC_CURVE_PATH, PREMIUM_PAIRS, RAW_PAIRS, SugarQuote, SugarSpread,
    contract_month, latest_pair, load_quotes, resolve_preview_release, resolve_server_quotes, spread_history,
)
from agri_research_agent.shared.chart_style import (
    CURRENT_LINE_WIDTH, CURRENT_YEAR_COLOR, GRID_COLOR, HISTORY_LINE_WIDTH, historical_year_color,
)


RAW_VIEW = "纽约原糖月差"
PREMIUM_VIEW = "伦敦白糖升水"
DOMESTIC_VIEW = "郑糖月差"
PRICE_LABELS = {"close": "日收盘价", "settlement": "日结算价"}


def _plot_history(points: list[SugarSpread], active_year: int, unit: str) -> go.Figure:
    fig = go.Figure()
    years = sorted({point.delivery_month.year for point in points})
    for year in years[-5:]:
        x, y, hover = [], [], []
        previous = None
        for point in points:
            maturity = point.delivery_month
            days = (maturity - point.trade_date).days
            if maturity.year != year or not 1 <= days <= 366:
                continue
            if previous and (point.trade_date - previous.trade_date > timedelta(days=7)
                             or previous.left.provider != point.left.provider):
                x.append(None)
                y.append(None)
                hover.append(None)
            x.append(-days)
            y.append(float(point.value))
            hover.append(f"{point.trade_date} · {point.left.contract} − {point.right.contract}")
            previous = point
        if x:
            active = year == active_year
            fig.add_scatter(
                x=x, y=y, text=hover, mode="lines+markers", marker={"size": 4}, name=str(year),
                line={"color": CURRENT_YEAR_COLOR if active else historical_year_color(year),
                      "width": CURRENT_LINE_WIDTH if active else HISTORY_LINE_WIDTH},
                hovertemplate=f"%{{text}}<br>%{{y:,.2f}} {unit}<extra></extra>",
                connectgaps=False,
            )
    fig.update_layout(height=365, margin=dict(l=48, r=22, t=15, b=48), hovermode="closest",
                      legend=dict(orientation="h"))
    fig.update_xaxes(title="相对近腿交割月月初（天）", gridcolor=GRID_COLOR)
    fig.update_yaxes(title=unit, gridcolor=GRID_COLOR)
    return fig


def _render_term_structure(quotes: list[SugarQuote], title: str, unit: str, key: str) -> bool:
    if not quotes:
        return False
    day = max(quote.trade_date for quote in quotes)
    providers: dict[str, list[SugarQuote]] = {}
    for quote in quotes:
        if quote.trade_date == day:
            providers.setdefault(quote.provider, []).append(quote)
    curve = sorted(max(providers.values(), key=len), key=lambda quote: contract_month(quote.contract))
    if len(curve) < 2:
        return False

    st.subheader(title)
    st.caption(f"{day:%Y-%m-%d} · {PRICE_LABELS[curve[0].price_type]} · {curve[0].provider}")
    precision = 0 if unit == "元/吨" else 2
    figure = go.Figure(go.Scatter(
        x=[contract_month(quote.contract).strftime("%Y-%m") for quote in curve],
        y=[float(quote.price) for quote in curve],
        mode="lines+markers", line=dict(color=CURRENT_YEAR_COLOR),
        text=[quote.contract for quote in curve],
        hovertemplate=f"%{{text}}<br>%{{y:,.{precision}f}} {unit}<extra></extra>",
    ))
    figure.update_layout(height=260, margin=dict(l=48, r=22, t=15, b=35))
    figure.update_xaxes(type="category", title="交割年月")
    figure.update_yaxes(title=unit, gridcolor=GRID_COLOR)
    st.plotly_chart(figure, width="stretch", key=key)
    return True


def _render_domestic_term_structure(quote_path: Path, *, as_of: date) -> None:
    try:
        quotes = load_quotes(quote_path, as_of=as_of)
    except FileNotFoundError:
        st.info("暂无郑糖期限结构行情。")
        return
    except (OSError, UnicodeError, ValueError) as exc:
        st.error(f"郑糖期限结构行情读取失败。{exc}")
        return
    close = [quote for quote in quotes if quote.contract.startswith("SR") and quote.price_type == "close"]
    if not _render_term_structure(close, "郑糖期限结构", "元/吨", "sugar_domestic_curve"):
        st.info("最新交易日的有效郑糖合约收盘价不足两份。")


def render_sugar_spreads_page(project_root: Path, render_domestic: Callable[[Path | None], None]) -> None:
    st.title("白糖价差")
    view = st.radio("价差视图", [RAW_VIEW, PREMIUM_VIEW, DOMESTIC_VIEW], horizontal=True,
                    key="sugar_spread_view")
    configured = os.environ.get("PUBLIC_MARKET_DATA_RUNTIME_ROOT")
    data_root = Path(configured) if configured else project_root / "01_data"
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    try:
        release = resolve_preview_release(project_root, data_root)
        official = resolve_server_quotes(data_root) if release is None else {}
    except (OSError, UnicodeError, ValueError) as exc:
        st.error(f"白糖行情快照校验失败。{exc}")
        if view == DOMESTIC_VIEW:
            render_domestic(None)
        return
    if view == DOMESTIC_VIEW:
        quote_path = (release / DOMESTIC_CURVE_PATH.name if release
                      else official.get("domestic", data_root / DOMESTIC_CURVE_PATH))
        _render_domestic_term_structure(quote_path, as_of=today)
        render_domestic(release / "historical_spread_database.parquet" if release else None)
        return

    premium = view == PREMIUM_VIEW
    pairs = PREMIUM_PAIRS if premium else RAW_PAIRS
    unit = "美元/吨" if premium else "美分/磅"
    st.caption("伦敦白糖 − 纽约原糖（折美元/吨） · 同交割月" if premium
               else "纽约原糖近月 − 远月 · 美分/磅")
    try:
        quote_path = release / DATA_PATH.name if release else official.get("foreign", data_root / DATA_PATH)
        quotes = [quote for quote in load_quotes(quote_path, as_of=today)
                  if quote.contract.startswith(("SB", "W"))]
    except FileNotFoundError:
        st.info("暂无白糖外盘日度行情快照。")
        return
    except (OSError, UnicodeError, ValueError) as exc:
        st.error(f"白糖外盘行情读取失败。{exc}")
        return

    available = {}
    preferred = ("settlement", "close") if premium or "foreign" in official else ("close", "settlement")
    for basis in preferred:
        histories = {label: spread_history(quotes, *months, premium=premium, price_type=basis)
                     for label, months in pairs.items()}
        if any(histories.values()):
            available[basis] = histories
    if not available:
        st.info("暂无两个有效报价日期及口径一致的合约行情。")
        return

    basis = st.radio("报价口径", list(available), format_func=PRICE_LABELS.__getitem__,
                     horizontal=True, key=f"sugar_basis_{view}") if len(available) > 1 else next(iter(available))
    histories = available[basis]
    relevant = [quote for quote in quotes if quote.price_type == basis
                and (premium or quote.contract.startswith("SB"))]
    reference_date = max(quote.trade_date for quote in relevant)
    if premium or not _render_term_structure(relevant, "纽约原糖期限曲线", unit, "sugar_raw_curve"):
        st.caption(f"{PRICE_LABELS[basis]} · 最新行情 {reference_date:%Y-%m-%d}")

    metrics = {label: latest_pair(points, reference_date) for label, points in histories.items()}
    for column, (label, point) in zip(st.columns(len(metrics)), metrics.items()):
        with column:
            st.metric(label, f"{point.value:+.2f} {unit}" if point else "暂无")
            if point:
                st.caption(f"{point.left.contract} − {point.right.contract} · {point.trade_date:%Y-%m-%d}")

    options = [label for label, point in metrics.items() if point]
    front = min(options, key=lambda label: (reference_date - metrics[label].trade_date > timedelta(days=7),
                                            metrics[label].delivery_month))
    pair = st.selectbox("季节性合约组合", options, index=options.index(front), key=f"sugar_pair_{view}")
    points = histories[pair]
    st.subheader("季节性")
    plotted = _plot_history(points, metrics[pair].delivery_month.year, unit)
    if plotted.data:
        st.plotly_chart(plotted, width="stretch", key=f"sugar_history_{view}")
        if len(plotted.data) == 1:
            st.caption(f"当前只有 {plotted.data[0].name} 交割年可绘制。")
    else:
        st.info("此组合暂无交割月前一年的有效配对行情。")
    st.caption(f"有效配对覆盖 {points[0].trade_date:%Y-%m-%d} 至 {points[-1].trade_date:%Y-%m-%d}"
               " · 各条线按近腿交割年对齐，最多展示近五个有数据的交割年。")

    with st.expander("两腿报价明细"):
        left_label = "白糖" if premium else "近腿"
        right_label = "原糖" if premium else "远腿"
        table = pd.DataFrame([{
            "交易日": point.trade_date, f"{left_label}合约": point.left.contract,
            f"{left_label}价格（{unit}）": float(point.left.price), f"{right_label}合约": point.right.contract,
            f"{right_label}价格（美分/磅）": float(point.right.price),
            f"价差（{unit}）": round(float(point.value), 2), "来源": point.left.provider,
            "近腿来源": point.left.source_url, "远腿来源": point.right.source_url,
        } for point in reversed(points)])
        st.dataframe(table, hide_index=True, width="stretch", column_config={
            "近腿来源": st.column_config.LinkColumn("近腿来源", display_text="查看"),
            "远腿来源": st.column_config.LinkColumn("远腿来源", display_text="查看"),
        })
