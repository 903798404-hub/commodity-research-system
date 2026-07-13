from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

VARIETIES = ["P", "Y", "OI", "M", "RM"]
ROWS = [("外资合计", "foreign_total"), ("摩根大通期货", "摩根大通期货"), ("乾坤期货", "乾坤期货")]


@st.cache_data(show_spinner=False)
def load_data(path: str, mtime: float) -> pd.DataFrame:
    del mtime
    data = pd.read_parquet(path)
    data["trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce")
    for col in ["long_position", "short_position", "net_position", "long_change", "short_change", "net_change", "settlement_price"]:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    return data.dropna(subset=["trade_date"])


def total_foreign(data: pd.DataFrame) -> pd.DataFrame:
    foreign = data[data["seat_group"].eq("foreign")].copy()
    totals = foreign.groupby(["trade_date", "variety"], as_index=False).agg(
        long_position=("long_position", lambda s: s.sum(min_count=1)), short_position=("short_position", lambda s: s.sum(min_count=1)),
        long_change=("long_change", lambda s: s.sum(min_count=1)), short_change=("short_change", lambda s: s.sum(min_count=1)),
        settlement_price=("settlement_price", "last"),
    )
    totals["net_position"] = totals["long_position"] - totals["short_position"]
    totals["net_change"] = totals["long_change"] - totals["short_change"]
    totals["data_status"] = "listed"
    totals["seat_name_normalized"] = "foreign_total"
    return totals


def label_change(value: object) -> str:
    if pd.isna(value) or abs(float(value)) < 1000:
        return "基本维持"
    direction = "增多" if float(value) > 0 else "增空"
    return ("大幅" if abs(float(value)) > 5000 else "") + direction


def render_summary(data: pd.DataFrame) -> None:
    rows = []
    merged = pd.concat([data, total_foreign(data)], ignore_index=True)
    for name, key in ROWS:
        for variety in VARIETIES:
            selected = merged[(merged["seat_name_normalized"] == key) & (merged["variety"] == variety)].dropna(subset=["trade_date"])
            if selected.empty:
                continue
            latest = selected.sort_values("trade_date").iloc[-1]
            rows.append(f"{name}{variety} {label_change(latest.net_change)}（净变化 {latest.net_change:,.0f} 手）") if pd.notna(latest.net_change) else None
    st.info("最新交易日摘要：" + "；".join(rows) if rows else "暂无可用于生成摘要的公开排名数据。")


def chart(data: pd.DataFrame) -> go.Figure:
    fig = make_subplots(rows=3, cols=5, specs=[[{"secondary_y": True}]*5 for _ in range(3)], row_titles=[x[0] for x in ROWS], column_titles=VARIETIES, vertical_spacing=0.12, horizontal_spacing=0.04)
    all_rows = pd.concat([data, total_foreign(data)], ignore_index=True)
    for r, (_, key) in enumerate(ROWS, 1):
        for c, variety in enumerate(VARIETIES, 1):
            item = all_rows[(all_rows.seat_name_normalized == key) & (all_rows.variety == variety)].sort_values("trade_date")
            if item.empty:
                continue
            if item["net_position"].notna().sum() == 0:
                latest_status = str(item.iloc[-1].get("data_status", ""))
                message = "当日未满足双边公开条件" if latest_status != "source_error" else "大商所数据源暂不可用"
                fig.add_annotation(text=message, xref=f"x{(r-1)*5+c if (r-1)*5+c > 1 else ''} domain", yref=f"y{(r-1)*10+c*2-1 if (r-1)*10+c*2-1 > 1 else ''} domain", x=0.5, y=0.5, showarrow=False, font={"color":"#64748b", "size":11})
            custom = item[["long_position", "short_position", "net_position", "long_change", "short_change", "net_change", "settlement_price", "data_status"]].to_numpy()
            hover = "日期=%{x|%Y-%m-%d}<br>多单=%{customdata[0]:,.0f}<br>空单=%{customdata[1]:,.0f}<br>净持仓=%{customdata[2]:,.0f}<br>多单变化=%{customdata[3]:,.0f}<br>空单变化=%{customdata[4]:,.0f}<br>净持仓变化=%{customdata[5]:,.0f}<br>结算价=%{customdata[6]:,.0f}<br>公开排名=%{customdata[7]}<extra></extra>"
            fig.add_trace(go.Scatter(x=item.trade_date, y=item.net_position, mode="lines", fill="tozeroy", line={"color":"#2563eb"}, fillcolor="rgba(37,99,235,.22)", customdata=custom, hovertemplate=hover, name="净持仓", showlegend=(r==1 and c==1)), row=r, col=c, secondary_y=False)
            fig.add_trace(go.Scatter(x=item.trade_date, y=item.settlement_price, mode="lines", line={"color":"#f97316"}, name="主力连续结算价", showlegend=(r==1 and c==1), hovertemplate="价格=%{y:,.0f}<extra></extra>"), row=r, col=c, secondary_y=True)
            latest = item.dropna(subset=["net_position"]).tail(1)
            if not latest.empty:
                fig.add_trace(go.Scatter(x=latest.trade_date, y=latest.net_position, mode="markers+text", text=[f"{latest.net_position.iloc[0]:,.0f}"], textposition="top center", marker={"color":"#1d4ed8", "size":6}, showlegend=False, hoverinfo="skip"), row=r, col=c, secondary_y=False)
            fig.update_yaxes(zeroline=True, zerolinecolor="#64748b", row=r, col=c, secondary_y=False)
    fig.update_layout(height=1080, margin={"l":70,"r":45,"t":60,"b":35}, hovermode="closest", legend={"orientation":"h"})
    fig.update_xaxes(rangeslider_visible=True)
    return fig


def render_foreign_seats_page(database_path: Path) -> None:
    st.title("外资与重点席位")
    if not database_path.exists():
        st.info("尚未生成本地席位标准化数据。请先运行更新脚本；页面不会在加载时访问外部网站。")
        return
    data = load_data(str(database_path), database_path.stat().st_mtime)
    dates = sorted(data.trade_date.dropna().unique())
    if not dates:
        st.info("本地数据文件为空。")
        return
    latest_date = pd.Timestamp(dates[-1])
    status_cards = st.columns(5)
    for index, variety in enumerate(VARIETIES):
        subset = data[data.variety.eq(variety)].sort_values("trade_date")
        latest = subset[subset.trade_date.eq(latest_date)]
        state = "/".join(sorted(latest.data_status.dropna().unique())) if not latest.empty else "缺失"
        valid_dates = subset.loc[~subset.data_status.eq("source_error"), "trade_date"]
        missing_days = 0 if not valid_dates.empty and valid_dates.max() == latest_date else int((latest_date - (valid_dates.max() if not valid_dates.empty else latest_date)).days)
        status_cards[index].metric(variety, state, f"连续缺失 {missing_days} 天")
    status_file = database_path.parents[2] / "foreign_seats_update_status.json"
    updated_at = "-"
    if status_file.exists():
        import json
        updated_at = json.loads(status_file.read_text(encoding="utf-8")).get("foreign_seats", {}).get("updated_at", "-")
    st.caption(f"最新数据交易日：{latest_date:%Y-%m-%d}｜本次更新时间：{updated_at}｜当前数据源：交易所公开排名 / AKShare 优先")
    if all((data[(data.variety.eq(v)) & (data.trade_date.eq(latest_date))].data_status == "source_error").all() for v in ["P", "Y", "M"]):
        st.warning("大商所数据源暂不可用：页面保留本地历史展示，但不表示当日更新成功。")
    period = st.radio("显示区间", ["最近60日", "最近120日", "最近250日", "自定义日期"], index=1, horizontal=True)
    if period == "自定义日期":
        selected = st.date_input("日期范围", value=(pd.Timestamp(dates[max(0, len(dates)-120)]).date(), pd.Timestamp(dates[-1]).date()))
        if isinstance(selected, tuple) and len(selected) == 2:
            start, end = pd.Timestamp(selected[0]), pd.Timestamp(selected[1]); data = data[data.trade_date.between(start, end)]
    else:
        count = int(period.replace("最近", "").replace("日", "")); keep = dates[-count:]; data = data[data.trade_date.isin(keep)]
    render_summary(data)
    st.plotly_chart(chart(data), use_container_width=True)
    st.caption("持仓仅采用交易所公开的品种级会员排名；席位未上榜保持空值，不以 0 补齐。价格为 P0/Y0/OI0/M0/RM0 主力连续的动态结算价。")
