"""Read-only foreign FX research page; external requests belong to collection."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from agri_research_agent.market_data.foreign_fx import (
    BY_CODE, CURRENCIES, BCB_CATALOG_URL, ECB_CATALOG_URL, FxDataError,
    load_snapshot, load_update_status, overview, strength_comparison, seasonality,
)
from agri_research_agent.shared.chart_style import (
    CURRENT_YEAR_COLOR, CURRENT_LINE_WIDTH, GRID_COLOR, HISTORY_LINE_WIDTH,
    HISTORICAL_YEAR_COLORS, MEAN_COLOR, HISTORY_OPACITY, historical_year_color,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def snapshot_path(project_root: Path = PROJECT_ROOT) -> Path:
    configured = os.environ.get("FOREIGN_FX_SNAPSHOT_FILE", "").strip()
    return Path(configured) if configured else project_root / "01_data/processed/foreign_fx/daily.json"


def _layout(figure: go.Figure, unit: str) -> go.Figure:
    figure.update_layout(height=390, margin=dict(l=10, r=10, t=25, b=15),
                         paper_bgcolor="white", plot_bgcolor="white", hovermode="x unified",
                         legend=dict(orientation="h", y=1.1), yaxis_title=unit,
                         font=dict(family="Arial, sans-serif", size=12))
    figure.update_yaxes(gridcolor=GRID_COLOR, zeroline=False)
    figure.update_xaxes(showgrid=False)
    return figure


def _scope_table() -> pd.DataFrame:
    return pd.DataFrame([{"币种": c.name, "代码": c.code, "国家": c.country,
                          "关联品种": c.commodities, "研究含义": c.meaning} for c in CURRENCIES])


def build_seasonality_figure(frame: pd.DataFrame, code: str, years: list[int], *,
                             current_year: int, normalize: bool = False) -> go.Figure:
    rows, average = seasonality(frame, code, years, current_year=current_year, normalize=normalize)
    figure = go.Figure()
    for year in sorted(set(rows["year"])):
        points = rows.loc[rows["year"] == year]
        current = year == current_year
        figure.add_trace(go.Scatter(
            x=points["season_date"], y=points["value"], mode="lines",
            name=f"{year}年" + ("（今年）" if current else ""),
            customdata=[d.isoformat() for d in points["date"]],
            hovertemplate="%{customdata}<br>%{y:.4f}<extra>%{fullData.name}</extra>",
            opacity=1 if current else HISTORY_OPACITY,
            line=dict(color=CURRENT_YEAR_COLOR if current else historical_year_color(year),
                      width=CURRENT_LINE_WIDTH if current else HISTORY_LINE_WIDTH)))
    if average["value"].notna().any():
        figure.add_trace(go.Scatter(
            x=average["season_date"], y=average["value"], mode="lines+markers", name="所选历史月度均值",
            customdata=average["year_count"], connectgaps=False,
            hovertemplate="%{x|%m}月<br>%{y:.4f}（%{customdata}年有报价）<extra>历史月度均值</extra>",
            line=dict(color=MEAN_COLOR, width=2.5, dash="dash")))
    _layout(figure, "汇率指数（年初＝100）" if normalize else f"{code} / USD")
    figure.update_xaxes(tickmode="array", tickvals=[date(2000, m, 1) for m in range(1, 13)],
                        ticktext=[f"{m}月" for m in range(1, 13)],
                        range=[date(2000, 1, 1), date(2000, 12, 31)], title="月份")
    return figure


def render_foreign_fx_page(*, project_root: Path = PROJECT_ROOT) -> None:
    st.title("外盘汇率")
    st.caption("农产品出口竞争力与中国进口成本。统一展示1美元兑换多少本币，数值下降代表本币升值。")
    try:
        payload, frame = load_snapshot(snapshot_path(project_root))
    except FileNotFoundError:
        st.info("暂无已发布的日度汇率数据。先查看币种范围与研究含义。")
        st.dataframe(_scope_table(), hide_index=True, use_container_width=True)
        return
    except (OSError, FxDataError) as exc:
        st.error(f"日度汇率数据暂不可用：{exc}")
        return

    beijing = timezone(timedelta(hours=8))
    collected = datetime.fromisoformat(payload['generated_at']).astimezone(beijing)
    st.caption(f"当前数据采集时间：{collected:%Y-%m-%d %H:%M} 北京时间。各币种业务日期单独列示，参考汇率用于日度研究。")
    status = load_update_status(snapshot_path(project_root), expected_payload=payload)
    if status:
        checked = datetime.fromisoformat(status['checked_at']).astimezone(beijing)
        label = {"UPDATED": "已更新", "NO_CHANGE": "来源未新增或修订报价", "FAILED": "更新失败",
                 "FAILED_AFTER_PUBLISH": "数据已写入，更新状态记录异常"}[status['result']]
        st.caption(f"最近检查：{checked:%Y-%m-%d %H:%M} 北京时间 · {label}。")
        if status['result'] == 'FAILED':
            st.warning("最近一次采集失败，当前展示上次通过校验的数据；请以各币种业务日期判断时效。")
        elif status['result'] == 'FAILED_AFTER_PUBLISH':
            st.warning("更新后的状态记录未完整写入，请核查更新日志和业务日期。")
    summary = overview(frame)
    table = summary[["name", "commodities", "latest_date", "rate", "return_1", "return_5", "return_20"]].rename(columns={
        "name": "币种", "commodities": "关联品种", "latest_date": "业务日期", "rate": "本币/美元",
        "return_1": "本币1日涨跌", "return_5": "本币5日涨跌", "return_20": "本币20日涨跌",
    })
    for col in ("本币1日涨跌", "本币5日涨跌", "本币20日涨跌"):
        table[col] *= 100
    st.dataframe(table, hide_index=True, use_container_width=True, column_config={
        "本币/美元": st.column_config.NumberColumn(format="%.4f"),
        **{col: st.column_config.NumberColumn(format="%+.2f%%") for col in
           ("本币1日涨跌", "本币5日涨跌", "本币20日涨跌")},
    })
    st.caption("1、5、20日均按各币种相邻有报价日计算。缺失显示为空，不补零，不延用旧报价。")

    available = [c.code for c in CURRENCIES if c.code in set(frame["currency"])]
    code = st.selectbox("研究币种", available, format_func=lambda c: f"{BY_CODE[c].name} · USD/{c}")
    trend, compare, definitions = st.tabs(["季节性", "本币强弱对比", "数据与研究含义"])
    with trend:
        rows = frame.loc[frame["currency"] == code].sort_values("date")
        last = rows.iloc[-1]
        cols = st.columns(3)
        cols[0].metric(f"USD/{code}", f"{last['local_per_usd']:.4f}")
        cols[1].metric("最新业务日期", str(last["date"]))
        ret = summary.loc[summary["currency"] == code, "return_1"].iloc[0]
        cols[2].metric("本币相对美元日涨跌", "—" if pd.isna(ret) else f"{ret:+.2%}")
        years = sorted({d.year for d in rows["date"]})
        chosen = st.multiselect("比较年份", years, default=years[-6:], key=f"fx_season_years_{code}")
        mode = st.radio("图表口径", ["汇率水平", "年初＝100"], horizontal=True, key="fx_season_mode")
        current_year = datetime.now(timezone(timedelta(hours=8))).year
        if chosen:
            st.plotly_chart(build_seasonality_figure(frame, code, chosen, current_year=current_year,
                            normalize=mode == "年初＝100"), use_container_width=True)
        else:
            st.info("选择至少一个年份查看季节性。")
        st.caption("灰线为所选历史年份的月度均值：先算各年月均值，再对年份等权平均；至少2年有报价，不含今年。日线不补缺失，闰日单独对齐。")
        if mode == "年初＝100":
            st.caption("指数＝当日汇率÷各年首个有报价日汇率×100；高于100代表美元相对本币升值。")
        st.caption("年份对齐用于同期比较，不代表已证实稳定的季节规律。")
        st.write(BY_CODE[code].meaning)
    with compare:
        horizon = st.radio("研究区间", ["近3个月", "近1年", "2021年以来"], index=1, horizontal=True)
        latest = max(frame["date"])
        start = date(2021, 1, 1) if horizon == "2021年以来" else latest - timedelta(days=92 if horizon == "近3个月" else 365)
        defaults = [c for c in ("BRL", "CAD", "AUD", "MYR") if c in available]
        selected = st.multiselect("比较币种", available, default=defaults, format_func=lambda c: BY_CODE[c].name,
                                  key="fx_compare_currencies")
        if selected:
            compared, base = strength_comparison(frame, selected, start)
            if base is None:
                st.info("所选币种在该区间没有共同有报价日，无法建立统一起点。")
            else:
                st.caption(f"共同起点：{base}＝100。高于100表示本币相对美元升值，低于100表示贬值。")
                figure = go.Figure()
                for c in selected:
                    rows = compared.loc[compared["currency"] == c]
                    figure.add_trace(go.Scatter(x=rows["date"], y=rows["strength"], mode="lines",
                                               name=BY_CODE[c].name,
                                               line=dict(color=HISTORICAL_YEAR_COLORS[list(BY_CODE).index(c)],
                                                         width=HISTORY_LINE_WIDTH)))
                figure.add_hline(y=100, line_dash="dash", line_color=MEAN_COLOR)
                st.plotly_chart(_layout(figure, "本币强弱指数"), use_container_width=True)
        else:
            st.info("选择至少一个币种进行比较。")
    with definitions:
        st.dataframe(_scope_table(), hide_index=True, use_container_width=True)
        st.markdown(f"巴西： [巴西央行SGS序列1]({BCB_CATALOG_URL})，采用PTAX卖出参考价。\n\n"
                    f"其他币种： [欧洲央行参考汇率]({ECB_CATALOG_URL})，使用同一业务日的欧元基准交叉换算。")
        st.write("人民币使用在岸CNY，与已有进口榨利使用的离岸CNH区分，不替换榨利的原有汇率。")
        st.write("汇率还受美联储与本国利差、财政和债务可信度、商品价格、贸易收支、资本流动及央行干预影响。")
        st.write("本币涨跌＝上期USD/本币÷本期USD/本币－1。强弱指数＝起点USD/本币÷当期USD/本币×100。")
        st.write("汇率与商品价格可能相互影响。此页展示研究背景，单独的汇率变化不能证明商品涨跌原因。")
        export = frame.copy()
        st.download_button("下载日度汇率CSV", export.to_csv(index=False).encode("utf-8-sig"),
                           file_name="foreign_fx_daily.csv", mime="text/csv")
