"""Native Plotly charts with explicit units, zero baseline, and visible gaps."""
import plotly.graph_objects as go

PALETTE = ("#356FA3", "#B08A32", "#C77B48", "#737F42", "#AE6D87")
DASHES = ("solid", "dash", "dot", "dashdot", "longdash")


def trend(rows, title, labels):
    figure = go.Figure()
    for index, (group, label) in enumerate(labels.items()):
        data = sorted([r for r in rows if r["group"] == group], key=lambda r: r["report_date"])
        figure.add_trace(go.Scatter(x=[r["report_date"] for r in data], y=[r["net"] for r in data],
            mode="lines", name=label, connectgaps=False,
            line=dict(color=PALETTE[index % 5], dash=DASHES[index % 5], width=2),
            customdata=[[r["long"], r["short"], r.get("previous_date"), r.get("net_change")] for r in data],
            hovertemplate="%{x}<br>净持仓 %{y:,.0f} 手<br>多仓 %{customdata[0]:,.0f}"
                "<br>空仓 %{customdata[1]:,.0f}<br>净变化 %{customdata[3]:+,.0f}"
                "<br>比较日期 %{customdata[2]}<extra>%{fullData.name}</extra>"))
    figure.update_layout(title=title, height=390, margin=dict(l=40, r=25, t=65, b=40),
        template="plotly_white", font=dict(family="Microsoft YaHei, sans-serif", color="#263238"),
        legend=dict(orientation="h", y=1.12), yaxis_title="净持仓（手）", xaxis_title="持仓截至日期")
    figure.add_hline(y=0, line_width=1, line_color="#5C6368")
    figure.update_yaxes(rangemode="tozero", gridcolor="#E8EBED")
    return figure


def movements(rows, title):
    known = [r for r in rows if r["net_change"] is not None]
    figure = go.Figure(go.Bar(x=[r["report_date"] for r in known],
        y=[r["net_change"] for r in known],
        marker_color=["#BC4749" if r["net_change"] > 0 else "#278568" if r["net_change"] < 0 else "#7A838B" for r in known],
        customdata=[[r["previous_date"]] for r in known],
        hovertemplate="%{x}<br>净变化 %{y:+,.0f} 手<br>比较日期 %{customdata[0]}<extra></extra>"))
    figure.update_layout(title=title, height=280, template="plotly_white",
        font=dict(family="Microsoft YaHei, sans-serif", color="#263238"),
        yaxis_title="净持仓变化（手）", margin=dict(l=40, r=25, t=60, b=35),
        annotations=[dict(text="红：向多变化 · 绿：向空变化", x=1, y=1.15,
            xref="paper", yref="paper", showarrow=False, xanchor="right")])
    figure.add_hline(y=0, line_width=1, line_color="#5C6368")
    return figure
