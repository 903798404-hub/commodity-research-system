from pathlib import Path
import sys

from streamlit.testing.v1 import AppTest

from agri_research_agent.sugar_positions.charts import trend
from agri_research_agent.sugar_positions.model import foreign_metrics
from agri_research_agent.sugar_positions.storage import publish

ROOT = Path(__file__).resolve().parents[2]


def data():
    foreign = []
    domestic = []
    for n in range(1, 10):
        day = f"2026-09-{n:02d}"
        for market in ("sugar11", "white_sugar"):
            for kind in ("futures_only", "combined"):
                for group in ("managed_money", "producer", "swap", "other", "nonreportable"):
                    foreign.append(dict(report_date=day, market=market, report_type=kind, group=group,
                        long=n*100, short=300, open_interest=3000, source_url="https://official.example",
                        retrieved_at="2026-10-08T01:00:00Z", scope="all_expiries", unit="contracts", spreading=0))
        for scope in ("SR", "SR701"):
            for member, rank in (("永安期货", 1), ("东证期货", 2)):
                for side in ("long", "short"):
                    domestic.append(dict(report_date=day, scope=scope, side=side, rank=rank,
                        member=member, raw_member=member+"（代客）", account="代客",
                        positions=n*10 if side == "long" else 50, reported_change=1))
    return foreign, domestic


def project(tmp_path):
    (tmp_path / ".git").write_text("gitdir: test", encoding="utf-8")
    (tmp_path / "02_configs").mkdir()
    (tmp_path / "02_configs/sugar_positions.json").write_bytes((ROOT / "02_configs/sugar_positions.json").read_bytes())
    return tmp_path


def app(tmp_path):
    # Synthetic rows are confined to tests; the user preview uses official snapshots only.
    source = f'''from pathlib import Path
import sys
sys.path.insert(0, {str(ROOT / "05_apps")!r})
import streamlit as st
from sugar_positions_page import render_sugar_positions_page
st.set_page_config(layout="wide")
render_sugar_positions_page(Path({str(tmp_path)!r}))
'''
    return AppTest.from_string(source)


def test_preview_empty_and_readonly(tmp_path):
    project(tmp_path)
    at = app(tmp_path).run(timeout=20)
    assert not at.exception and at.info
    assert not (tmp_path / "01_data").exists()


def test_preview_renders_charts_details_filters_and_missing_states(tmp_path):
    project(tmp_path)
    foreign, domestic = data()
    publish(tmp_path / "01_data/sugar_positions_preview", foreign, domestic, [], [])
    at = app(tmp_path).run(timeout=30)
    assert not at.exception
    assert len(at.get("plotly_chart")) == 6
    assert len(at.dataframe) >= 4
    assert any("部分席位" in w.value for w in at.warning)
    at.radio[0].set_value("期货＋期权").run()
    at.selectbox[1].set_value("SR701").run()
    assert not at.exception
    assert any(m.label == "五家合计净持仓" and m.value == "未披露" for m in at.metric)


def test_chart_does_not_connect_missing_positions():
    rows = foreign_metrics([dict(market="sugar11", report_type="futures_only", group="managed_money",
        report_date=day, long=long, short=10) for day, long in [
            ("2026-09-01", 30), ("2026-09-08", None), ("2026-09-15", 20)]])
    figure = trend(rows, "基金净持仓", {"managed_money": "管理基金"})
    assert figure.data[0].connectgaps is False and list(figure.data[0].y) == [20, None, 10]
