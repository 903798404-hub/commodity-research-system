"""Read-only CGC canola export dashboard."""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from agri_research_agent.canola_exports.data import BASE_URL, STABLE, STATUS, digest, load_bundle, page_payload
from agri_research_agent.shared.chart_style import CURRENT_YEAR_COLOR, GRID_COLOR, historical_year_color

ROOT = Path(__file__).resolve().parents[1]
CHARTS = (("weekly_mt", "周度出口"), ("cumulative_mt", "作物年度累计出口"),
          ("four_week_mt", "近四周出口合计"))


def data_root() -> Path:
    return Path(os.getenv("CANOLA_EXPORT_RUNTIME_ROOT", "").strip()
                or os.getenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", "").strip() or ROOT / "01_data")


@st.cache_data(show_spinner=False, max_entries=2)
def read_data(path: str, identity: str) -> dict:
    bundle = load_bundle(Path(path))
    if digest(Path(path).read_bytes()) != identity:
        raise ValueError("数据读取期间发生变化，请刷新")
    return bundle


def amount(value: float | None) -> str:
    return "暂无" if value is None else f"{value / 10000:,.2f} 万吨"


def percent(value: float | None) -> str:
    return "暂无可比值" if value is None else f"{value:+.1f}%"


def crop_year_label(value: str) -> str:
    return "/".join(value.split("-"))


def seasonal_date(value: str | None) -> str | None:
    if value is None:
        return None
    actual = date.fromisoformat(value)
    # A leap-year August-to-July axis preserves every reported month/day.
    return actual.replace(year=1999 if actual.month >= 8 else 2000).isoformat()


def build_figures(payload: dict) -> list[go.Figure]:
    figures = []
    for metric, title in CHARTS:
        figure = go.Figure()
        for year in sorted(payload["tracks"]):
            track = payload["tracks"][year]
            current = year == payload["current_year"]
            previous = year == payload["previous_year"]
            color = CURRENT_YEAR_COLOR if current else "#0068C9" if previous else historical_year_color(int(year[:4]))
            figure.add_trace(go.Scatter(
                x=[None if p.get("date_quality") else seasonal_date(p["week_ending"]) for p in track],
                y=[None if p[metric] is None or p.get("date_quality") else p[metric] / 10000 for p in track],
                customdata=[f"{p['week_ending']}（源日期待复核）" if p.get("date_quality") else p["week_ending"] for p in track], name=crop_year_label(year),
                mode="lines", connectgaps=False, opacity=1 if current or previous else 0.65,
                line={"color": color, "width": 3.4 if current else 2, "dash": "dash" if previous else "solid"},
                hovertemplate="%{fullData.name}<br>截止 %{customdata}<br>%{y:,.2f} 万公吨<extra></extra>",
            ))
        latest = payload["latest"]
        if metric == "cumulative_mt" and payload["rank"] is not None:
            figure.add_annotation(x=0, y=1.24, xref="paper", yref="paper", showarrow=False,
                                  xanchor="left", text=f"截至 {latest['week_ending']} · 同比 {percent(payload['cumulative_yoy'])}"
                                  f" · 同期排名 {payload['rank']}/{payload['rank_samples']}")
        figure.update_layout(template="plotly_white", height=330,
                             margin={"l": 16, "r": 16, "t": 92, "b": 35},
                             legend={"orientation": "h", "x": 0, "y": 1.14, "font": {"size": 11}},
                             hovermode="x unified", font={"size": 12},
                             xaxis={"title": "周截止日期 · 月/日", "type": "date", "range": ["1999-08-01", "2000-07-31"],
                                    "dtick": "M1", "tickformat": "%m/%d", "gridcolor": GRID_COLOR},
                             yaxis={"title": "万公吨", "rangemode": "tozero", "gridcolor": GRID_COLOR})
        figures.append(figure)
    return figures


def observation(payload: dict) -> str:
    latest = payload["latest"]
    text = (f"截至 {latest['week_ending']}，当周出口 {amount(latest['weekly_mt'])}，"
            f"{crop_year_label(payload['current_year'])} 作物年累计 {amount(latest['cumulative_mt'])}。")
    for name, key in (("累计出口", "cumulative_yoy"), ("近四周出口", "four_week_yoy")):
        value = payload[key]
        if value is not None:
            text += f"{name}较去年同期{'增加' if value > 0 else '减少' if value < 0 else '持平'} {abs(value):.1f}%。"
    return text


def render_canola_exports_page() -> None:
    st.html("""<style>
      .st-key-canola-export-title h1 {font-size:32px; letter-spacing:0;}
      @media (max-width:640px) {
        .st-key-canola-export-kpis [data-testid="stHorizontalBlock"] {
          display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px;
        }
        .st-key-canola-export-kpis [data-testid="stColumn"] {width:100% !important; min-width:0;}
        .st-key-canola-export-kpis [data-testid="stMetricValue"] {font-size:26px;}
      }
    </style>""")
    with st.container(key="canola-export-title"):
        st.title("加拿大菜籽周度出口")
    root = data_root()
    path = root / STABLE
    if not path.is_file():
        st.info("加拿大菜籽周度出口数据尚未发布。")
        return
    try:
        bundle = read_data(str(path), digest(path.read_bytes()))
        default = page_payload(bundle)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        st.error(f"菜籽出口数据暂不可读：{exc}")
        return
    latest = default["latest"]
    st.caption(f"{crop_year_label(default['current_year'])} · 数据截至 {latest['week_ending']}"
               " · CGC 报告体系出口，非海关全口径")
    status_path = root / STATUS
    if status_path.is_file():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            if status.get("status") == "FAILED":
                st.warning(f"最近更新未完成，继续显示已发布数据。检查时间：{status.get('checked_at', '未知')}")
        except (OSError, ValueError):
            st.warning("更新状态暂不可读；以下为已发布数据。")
    with st.container(key="canola-export-kpis"):
        columns = st.columns(4)
        for column, (name, value) in zip(columns, (
                ("当周出口", amount(latest["weekly_mt"])), ("作物年累计", amount(latest["cumulative_mt"])),
                ("累计同比", percent(default["cumulative_yoy"])), ("近四周出口同比", percent(default["four_week_yoy"])))):
            column.metric(name, value)
    st.subheader("本周观察")
    st.write(observation(default))
    if latest["four_week_mt"] is None and latest["grain_week"] >= 4:
        st.caption("近四周出口同比暂无：窗口内存在源数据缺周或缺少出口分项，未按零补齐。")
    years = st.multiselect("对比作物年度", default["years"], default=default["years"][:7], key="canola-export-years")
    payload = page_payload(bundle, years)
    if not years:
        st.info("未选择对比作物年度。")
    else:
        if any(p.get("date_quality") for track in payload["tracks"].values() for p in track):
            st.caption("部分历史记录的官方源日期待复核，图中留空；原值保留在数据与来源中。")
        chart_tabs = st.tabs([title for _, title in CHARTS])
        for chart_tab, (metric, _), figure in zip(chart_tabs, CHARTS, build_figures(payload)):
            with chart_tab:
                st.plotly_chart(figure, width=1050, key=f"canola-export-{metric}")
    with st.expander("数据与来源"):
        st.caption("单位：万公吨。同作物周比较；四周窗口不跨作物年。缺周、缺值留空。累计采用官方修订值。")
        st.caption(f"数据生成时间（UTC）：{default['generated_at']} · 已记录数值修订：{default['revision_count']}")
        issues = [r for r in bundle["records"] if r.get("date_quality")
                  or any(r[m] is None for m, _ in CHARTS[:2])]
        if issues:
            st.warning(f"原始 CSV 有 {len(issues)} 个周记录存在缺值或日期异常；保留原值，按官方作物周比较。")
            st.dataframe(pd.DataFrame(issues), hide_index=True, width="stretch")
        st.markdown(f"[CGC 周报]({BASE_URL}) · [历史年度]({BASE_URL}archived.html)")
        st.dataframe(pd.DataFrame([{"作物年度": year, **source} for year, source in bundle["sources"].items()]),
                     hide_index=True, width="stretch")
        frame = pd.DataFrame(bundle["records"])
        st.dataframe(frame, hide_index=True, width="stretch")
        st.download_button("下载周度数据", frame.to_csv(index=False).encode("utf-8-sig"),
                           file_name="canada_canola_exports.csv", mime="text/csv")


if __name__ == "__main__":
    st.set_page_config(page_title="加拿大菜籽周度出口", layout="wide")
    render_canola_exports_page()
