"""Native Plotly charts with explicit units, zero baseline, and visible gaps."""
from datetime import date

import plotly.graph_objects as go

PALETTE = ("#356FA3", "#B08A32", "#C77B48", "#737F42", "#AE6D87")
DASHES = ("solid", "dash", "dot", "dashdot", "longdash")
SEAT_PALETTE = ("#2563EB", "#E59622", "#7C3AED", "#0D9488", "#DC4F86")
CHART_WIDTH = 1050


def chinese_date(value):
    if not value:
        return "无"
    day = date.fromisoformat(str(value)[:10])
    return f"{day.year}年{day.month}月{day.day}日"


def chinese_date_axis(figure):
    figure.update_xaxes(tickformat="%Y年<br>%-m月%-d日", hoverformat="%Y年%-m月%-d日")


def trend(rows, title, labels, *, direct_labels=False):
    figure = go.Figure()
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
                r.get("net_change"), chinese_date(r["report_date"])] for r in data],
            hovertemplate="%{customdata[4]}<br>净持仓 %{y:,.0f} 手<br>多仓 %{customdata[0]:,.0f}"
                "<br>空仓 %{customdata[1]:,.0f}<br>净变化 %{customdata[3]:+,.0f}"
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
        yaxis_title="净持仓（手）", xaxis_title="持仓截至日期",
        hovermode="x unified" if direct_labels else "closest")
    figure.add_hline(y=0, line_width=1.5 if direct_labels else 1, line_color="#5C6368")
    figure.update_yaxes(rangemode="tozero", gridcolor="#E8EBED")
    chinese_date_axis(figure)
    if direct_labels:
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
    known = [r for r in rows if r["net_change"] is not None]
    figure = go.Figure(go.Bar(x=[r["report_date"] for r in known],
        y=[r["net_change"] for r in known],
        marker_color=["#BC4749" if r["net_change"] > 0 else "#278568" if r["net_change"] < 0 else "#7A838B" for r in known],
        customdata=[[chinese_date(r["previous_date"]), chinese_date(r["report_date"])] for r in known],
        hovertemplate="%{customdata[1]}<br>净变化 %{y:+,.0f} 手<br>比较日期 %{customdata[0]}<extra></extra>"))
    figure.update_layout(title=title, height=230, template="plotly_white",
        font=dict(family="Microsoft YaHei, sans-serif", color="#263238"),
        yaxis_title="净持仓变化（手）", margin=dict(l=40, r=25, t=60, b=35),
        annotations=[dict(text="红：向多变化 · 绿：向空变化", x=1, y=1.15,
            xref="paper", yref="paper", showarrow=False, xanchor="right")])
    figure.add_hline(y=0, line_width=1, line_color="#5C6368")
    chinese_date_axis(figure)
    return figure
