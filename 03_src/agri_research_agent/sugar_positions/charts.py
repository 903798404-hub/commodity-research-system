"""Native Plotly charts with explicit units, zero baseline, and visible gaps."""
from datetime import date, timedelta

import plotly.graph_objects as go

from .model import RANKED_NET_METHOD
from agri_research_agent.shared.chart_style import (
    CURRENT_LINE_WIDTH, CURRENT_YEAR_COLOR, GRID_COLOR, HISTORY_LINE_WIDTH,
    HISTORY_OPACITY, historical_year_color,
)

PALETTE = ("#356FA3", "#B08A32", "#C77B48", "#737F42", "#AE6D87")
DASHES = ("solid", "dash", "dot", "dashdot", "longdash")
SEAT_PALETTE = ("#2563EB", "#E59622", "#7C3AED", "#0D9488", "#DC4F86")


def _position_value(row, field):
    if row.get("net_method") == RANKED_NET_METHOD and row["long"] is None and row["short"] is None:
        return None
    return row[field]


def label_latest_position(figure, rows, *, field="net", seasonal=False):
    """Show the latest report below the plot using the unfiltered report series."""
    latest = rows[-1]
    value = _position_value(latest, field)
    day = date.fromisoformat(latest["report_date"])
    ranked = latest.get("net_method") == RANKED_NET_METHOD
    net_label = "榜内净持仓" if ranked else "净持仓"
    label = {"net": net_label, "long": "多仓", "short": "空仓"}[field]
    figure.update_layout(height=400, margin=dict(b=115))
    if value is None:
        figure.add_annotation(x=0.5, y=0, xref="paper", yref="paper", yshift=-52,
            text=f"{day.isoformat()} · {label}未披露", showarrow=False, xanchor="center", yanchor="top",
            bgcolor="rgba(255,255,255,0.96)", font=dict(size=12, color="#58636C"))
        return

    change = None
    if len(rows) > 1 and latest["comparison_days"] <= 10:
        previous = rows[-2]
        same_basis = all(latest.get(k) == previous.get(k)
                         for k in ("unit", "source_provider", "aggregation", "constituent_contracts"))
        prior_value = _position_value(previous, field)
        if same_basis and prior_value is not None:
            change = latest["net_change"] if field == "net" else value - prior_value

    delta_equivalent = latest.get("unit") == "delta_equivalent_contracts"
    unit = "Delta等价手" if delta_equivalent else "手"
    decimals = 2 if delta_equivalent else 0
    change_text = "暂无可比数据"
    change_color = "#58636C"
    if change is not None:
        change = round(change, decimals) or 0
        change_text = f"{change:{'+' if change else ''},.{decimals}f} {unit}"
        direction = -change if field == "short" else change
        if direction:
            change_color = "#BC4749" if direction > 0 else "#278568"

    change_label = "日度变化" if ranked else "较上期"
    figure.add_annotation(
        x=0.5, y=0, xref="paper", yref="paper", yshift=-52,
        text=f"<b>{label} {value:,.{decimals}f} {unit}</b><br>{day.isoformat()}<br>"
             f"{change_label} <span style='color:{change_color}'>{change_text}</span>",
        hovertext=f"比较日期 {latest['previous_date']}" if change is not None else None,
        showarrow=False, xanchor="center", yanchor="top",
        align="left", font=dict(size=12, color="#263238"),
        bgcolor="rgba(255,255,255,0.96)", bordercolor="#C9D0D6", borderpad=4,
    )


def seasonality(rows, title, *, field="net", current_year=None):
    """Overlay one report series by month/day, preserving dates, units and gaps."""
    if field not in {"net", "long", "short"}:
        raise ValueError("季节性指标无效")
    identity = ("market", "report_type", "group", "scope", "account", "unit")
    if len({tuple(r.get(k) for k in identity) for r in rows}) > 1:
        raise ValueError("季节性曲线不能混合市场、口径、席位、账户或单位")
    dates = [r["report_date"] for r in rows]
    if len(set(dates)) != len(dates):
        raise ValueError("季节性曲线包含重复报告日期")
    current_year = current_year if current_year is not None else date.today().year
    ranked = bool(rows) and all(r.get("net_method") == RANKED_NET_METHOD for r in rows)
    label = {"net": "榜内净持仓" if ranked else "净持仓", "long": "多仓", "short": "空仓"}[field]
    unit = "Delta等价手" if rows and rows[0].get("unit") == "delta_equivalent_contracts" else "手"
    figure = go.Figure()
    years = sorted({date.fromisoformat(d).year for d in dates})
    for year in [y for y in years if y != current_year] + ([current_year] if current_year in years else []):
        data = sorted([r for r in rows if date.fromisoformat(r["report_date"]).year == year],
                      key=lambda r: r["report_date"])
        xs, ys, custom = [], [], []
        previous = None
        for row in data:
            day = date.fromisoformat(row["report_date"])
            # A missing report or a changed source must not become a continuous line.
            if previous and ((day - date.fromisoformat(previous["report_date"])).days > 10
                    or any(row.get(k) != previous.get(k) for k in ("source_provider", "aggregation"))):
                xs.append(day.replace(year=2000).isoformat())
                ys.append(None)
                custom.append([None])
            xs.append(day.replace(year=2000).isoformat())
            ys.append(_position_value(row, field) if ranked else row[field])
            custom.append([chinese_date(day)])
            previous = row
        current = year == current_year
        figure.add_trace(go.Scatter(x=xs, y=ys, customdata=custom,
            name=f"{year}年（当年）" if current else f"{year}年", mode="lines+markers",
            connectgaps=False, marker=dict(size=4),
            line=dict(color=CURRENT_YEAR_COLOR if current else historical_year_color(year),
                      width=CURRENT_LINE_WIDTH if current else HISTORY_LINE_WIDTH),
            opacity=1 if current else HISTORY_OPACITY,
            hovertemplate="%{customdata[0]}<br>" + label + " %{y:,.2f} " + unit + "<extra>%{fullData.name}</extra>"))
    figure.update_layout(title=title, height=340, template="plotly_white",
        margin=dict(l=40, r=25, t=90, b=40), showlegend=True,
        font=dict(family="Microsoft YaHei, sans-serif", color="#263238"),
        legend=dict(orientation="h", y=1.12), xaxis_title="持仓日期（月／日）",
        yaxis_title=f"{label}（{unit}）", hovermode="closest")
    figure.update_xaxes(range=["2000-01-01", "2000-12-31"], tickmode="array",
        tickvals=[f"2000-{m:02d}-01" for m in range(1, 13)], ticktext=[f"{m}月" for m in range(1, 13)])
    figure.update_yaxes(rangemode="tozero", gridcolor=GRID_COLOR)
    figure.add_hline(y=0, line_width=1, line_color="#5C6368")
    return figure


def chinese_date(value):
    if not value:
        return "无"
    day = date.fromisoformat(str(value)[:10])
    return f"{day.year}年{day.month}月{day.day}日"


def chinese_date_axis(figure):
    figure.update_xaxes(tickformat="%Y年<br>%-m月%-d日", hoverformat="%Y年%-m月%-d日")


def trend(rows, title, labels, *, direct_labels=False):
    figure = go.Figure()
    ranked = bool(rows) and all(r.get("net_method") == RANKED_NET_METHOD for r in rows)
    net_label = "榜内净持仓" if ranked else "净持仓"
    missing = []
    endings = []
    for index, (group, label) in enumerate(labels.items()):
        data = sorted([r for r in rows if r["group"] == group], key=lambda r: r["report_date"])
        color = SEAT_PALETTE[index % 5] if direct_labels else PALETTE[index % 5]
        figure.add_trace(go.Scatter(x=[r["report_date"] for r in data], y=[r["net"] for r in data],
            mode="lines+markers" if direct_labels else "lines", name=label, connectgaps=False,
            line=dict(color=color, dash="solid" if direct_labels else DASHES[index % 5],
                width=3 if direct_labels else 2),
            marker=dict(color=color, size=5),
            customdata=[[r["long"], r["short"], chinese_date(r.get("previous_date")),
                r.get("net_change"), chinese_date(r["report_date"]),
                "未披露" if r["long"] is None else format(r["long"], ",.0f"),
                "未披露" if r["short"] is None else format(r["short"], ",.0f")]
                for r in data],
            hovertemplate="%{customdata[4]}<br>" + net_label + " %{y:,.0f} 手<br>多仓 %{customdata[5]}"
                "<br>空仓 %{customdata[6]}<br>净变化 %{customdata[3]:+,.0f}"
                "<br>比较日期 %{customdata[2]}<extra>%{fullData.name}</extra>"))
        if direct_labels and data:
            if data[-1]["net"] is None:
                missing.append(label)
            else:
                endings.append((data[-1], label, color))
    height = 340 if direct_labels else 320
    figure.update_layout(title=title, height=height,
        margin=dict(l=40, r=160 if direct_labels else 25, t=90 if direct_labels else 65, b=40),
        template="plotly_white", font=dict(family="Microsoft YaHei, sans-serif", color="#263238"),
        legend=dict(orientation="h", y=1.14 if direct_labels else 1.12),
        yaxis_title=net_label + "（手）", xaxis_title="持仓截至日期",
        hovermode="x unified" if direct_labels else "closest")
    figure.add_hline(y=0, line_width=1.5 if direct_labels else 1, line_color="#5C6368")
    figure.update_yaxes(rangemode="tozero", gridcolor="#E8EBED")
    chinese_date_axis(figure)
    if direct_labels:
        dates = [date.fromisoformat(r["report_date"]) for r in rows if r["group"] in labels]
        if dates:
            figure.update_xaxes(range=[(min(dates) - timedelta(days=1)).isoformat(),
                (max(dates) + timedelta(days=1)).isoformat()])
        known = [r["net"] for r in rows if r["group"] in labels and r["net"] is not None]
        lower, upper = min([0] + known), max([0] + known)
        span = upper - lower or 1
        previous_pixel = None
        for row, label, color in sorted(endings, key=lambda item: item[0]["net"]):
            pixel = (row["net"] - lower) / span * (height - 130)
            placed = max(pixel, previous_pixel + 22) if previous_pixel is not None else pixel
            figure.add_annotation(x=row["report_date"], y=row["net"],
                text=f"<b>{label}</b> {row['net']:+,}", showarrow=False,
                xanchor="left", xshift=12, yshift=placed - pixel,
                font=dict(color=color, size=12), bgcolor="rgba(255,255,255,0.85)")
            previous_pixel = placed
        if missing:
            figure.add_annotation(x=0, y=1.05, xref="paper", yref="paper",
                text=f"最新未披露：{'、'.join(missing)}", showarrow=False, xanchor="left",
                font=dict(color="#77808F", size=12))
    return figure


def movements(rows, title):
    net_label = ("榜内净持仓变化" if rows and all(
        r.get("net_method") == RANKED_NET_METHOD for r in rows) else "净持仓变化")
    known = [r for r in rows if r["net_change"] is not None]
    figure = go.Figure(go.Bar(x=[r["report_date"] for r in known],
        y=[r["net_change"] for r in known],
        marker_color=["#BC4749" if r["net_change"] > 0 else "#278568" if r["net_change"] < 0 else "#7A838B" for r in known],
        customdata=[[chinese_date(r["previous_date"]), chinese_date(r["report_date"])] for r in known],
        hovertemplate="%{customdata[1]}<br>净变化 %{y:+,.0f} 手<br>比较日期 %{customdata[0]}<extra></extra>"))
    figure.update_layout(title=title, height=230, template="plotly_white",
        font=dict(family="Microsoft YaHei, sans-serif", color="#263238"),
        yaxis_title=net_label + "（手）", margin=dict(l=40, r=25, t=60, b=35),
        annotations=[dict(text="红：向多变化 · 绿：向空变化", x=1, y=1.15,
            xref="paper", yref="paper", showarrow=False, xanchor="right")])
    figure.add_hline(y=0, line_width=1, line_color="#5C6368")
    chinese_date_axis(figure)
    return figure
