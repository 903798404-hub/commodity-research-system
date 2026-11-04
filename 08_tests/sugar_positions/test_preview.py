from pathlib import Path
import json
import sys

from streamlit.testing.v1 import AppTest

from agri_research_agent.sugar_positions.charts import trend
from agri_research_agent.sugar_positions.model import domestic_metrics, foreign_metrics, load_members
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
render_sugar_positions_page(Path({str(tmp_path)!r}), data_root=Path({str(tmp_path / "01_data/sugar_positions_preview")!r}))
'''
    return AppTest.from_string(source)


def test_preview_empty_and_readonly(tmp_path):
    project(tmp_path)
    at = app(tmp_path).run(timeout=20)
    assert not at.exception and at.info
    assert not (tmp_path / "01_data").exists()


def test_preview_renders_official_cofco_source_identity(tmp_path):
    project(tmp_path)
    foreign, domestic = data()
    publish(tmp_path / "01_data/sugar_positions_preview", foreign, domestic,
        [("czce_cofco_official_20260901", b"synthetic source label fixture",
          "https://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/20260901/FutureDataHolding.txt", "zip")], [])
    at = app(tmp_path).run(timeout=30)
    assert not at.exception
    assert any("郑商所SR品种中粮汇总 · 2026年9月1日" in item.value for item in at.markdown)


def test_preview_renders_charts_details_filters_and_missing_states(tmp_path):
    project(tmp_path)
    foreign, domestic = data()
    publish(tmp_path / "01_data/sugar_positions_preview", foreign, domestic, [], [])
    at = app(tmp_path).run(timeout=30)
    assert not at.exception
    assert len(at.get("plotly_chart")) == 6
    assert len(at.dataframe) >= 4
    assert any("未披露一侧按0" in c.value for c in at.caption)
    fixed = next(d.value for d in at.dataframe if "榜内持仓倾向" in d.value and len(d.value) == 5)
    goldman = fixed[fixed["对象"] == "高盛"].iloc[0]
    assert goldman["多仓（手）"] == "未披露" and goldman["空仓（手）"] == "未披露"
    assert goldman["榜内净持仓（手）"] == "+0"
    assert goldman["榜内持仓倾向"] == "两侧未披露 · 榜内按0"
    assert at.radio(key="sugar_foreign_view").value == "季节性"
    assert any("尚无中粮期货" in item.value for item in at.info)
    at.multiselect(key="foreign_years_sugar11").set_value([]).run()
    assert not at.exception and len(at.get("plotly_chart")) == 4
    at.radio(key="sugar_foreign_view").set_value("连续历史").run()
    assert not at.exception and len(at.get("plotly_chart")) == 6
    trend_spec = json.loads(at.get("plotly_chart")[0].proto.spec)
    day, value, change = trend_spec["layout"]["annotations"]
    assert day["xref"] == day["yref"] == "paper" and day["yshift"] == 12
    assert "2026-09-09" in day["text"] and "600" in value["text"] and "+100" in change["text"]
    at.radio[0].set_value("期货＋期权").run()
    at.selectbox[1].set_value("SR701").run()
    assert not at.exception
    assert any(m.label == "五家合计榜内净持仓" and m.value == "80" for m in at.metric)


def test_cofco_independent_seasonality_preserves_fixed_five_and_cutoff(tmp_path):
    project(tmp_path)
    foreign, domestic = data()
    for day in ("2025-09-01", "2026-09-01", "2026-09-09"):
        for side, value in (("long", 100), ("short", 30)):
            domestic.append(dict(report_date=day, scope="SR", side=side, rank=3,
                member="中粮期货", raw_member="中粮期货（代客）", account="代客",
                positions=value, reported_change=0))
    publish(tmp_path / "01_data/sugar_positions_preview", foreign, domestic, [], [])
    at = app(tmp_path).run(timeout=30)
    assert not at.exception and len(at.get("plotly_chart")) == 9
    assert at.multiselect(key="cofco_years").value == [2025, 2026]
    assert any(m.label == "中粮榜内净持仓（手）" and m.value == "70" for m in at.metric)
    fixed = next(d.value for d in at.dataframe if "榜内持仓倾向" in d.value and len(d.value) == 5)
    assert "中粮期货" not in set(fixed["对象"])
    at.selectbox[2].set_value("2026-09-02").run()
    assert not at.exception
    cofco_table = next(d.value for d in at.dataframe if "对象" in d.value and "中粮期货" in set(d.value["对象"]))
    assert len(cofco_table) == 3 and "2026年9月9日" not in set(cofco_table["日期"])
    at.selectbox[1].set_value("SR701").run()
    assert not at.exception and any(m.label == "中粮榜内净持仓（手）" and m.value == "70" for m in at.metric)


def test_seat_charts_show_sugar_titles_and_latest_daily_changes(tmp_path):
    project(tmp_path)
    foreign, domestic = data()
    for day, goldman, short in (("2026-09-08", "乾坤期货", 100), ("2026-09-09", "高盛期货", 125)):
        for member, side, rank, value in ((goldman, "short", 3, short),
                ("中粮期货", "short", 4, 30), ("中粮期货", "long", 3, short)):
            domestic.append(dict(report_date=day, scope="SR", side=side, rank=rank,
                member=member, raw_member=member + "（代客）", account="代客",
                positions=value, reported_change=0))
    publish(tmp_path / "01_data/sugar_positions_preview", foreign, domestic, [], [])
    at = app(tmp_path).run(timeout=30)
    assert not at.exception
    charts = [json.loads(c.proto.spec)["layout"] for c in at.get("plotly_chart")]
    labels = {chart["title"]["text"]: chart["annotations"] for chart in charts if chart.get("annotations")}
    for title, value, change in (
        ("高盛期货 · 白糖净仓季节性", "-125 手", "-25 手"),
        ("高盛期货 · 白糖空仓季节性", "125 手", "+25 手"),
        ("中粮期货 · 白糖净仓季节性", "95 手", "+25 手"),
        ("中粮期货 · 白糖多仓季节性", "125 手", "+25 手"),
        ("中粮期货 · 白糖空仓季节性", "30 手", "0 手"),
    ):
        day, position, daily = labels[title]
        assert value.replace(" 手", "") in position["text"]
        assert change.replace(" 手", "") in daily["text"]
        assert "日度变化" in daily["text"] and "2026-09-09" in day["text"]
        assert "比较日期 2026-09-08" in daily["hovertext"]
    assert "未披露" in labels["高盛期货 · 白糖多仓季节性"][1]["text"]


def test_ranked_chart_draws_one_sided_net_and_preserves_undisclosed_hover():
    members = load_members(ROOT / "02_configs/sugar_positions.json")
    rows = domestic_metrics([dict(report_date="2026-10-09", scope="P2701", side="short",
        rank=1, member="高盛期货", raw_member="高盛期货（代客）", account="代客", positions=45991)], members)
    figure = trend(rows, "榜内净仓", {"goldman": "高盛", "yongan": "永安"}, direct_labels=True)
    assert list(figure.data[0].y) == [-45991] and list(figure.data[1].y) == [0]
    assert figure.data[0].customdata[0][0] is None
    assert figure.data[0].customdata[0][5] == "未披露"
    assert figure.data[0].customdata[0][6] == "45,991"
    assert "榜内净持仓" in figure.data[0].hovertemplate
    assert figure.layout.yaxis.title.text == "榜内净持仓（手）"


def test_cofco_contract_history_has_separate_basis_and_does_not_replace_rankings(tmp_path):
    project(tmp_path)
    foreign, domestic = data()
    for day, contract in (("2022-09-01", "SR2301"), ("2025-09-01", "SR2601"), ("2026-09-01", "SR2701")):
        for side, value in (("long", 120), ("short", 30)):
            domestic.append(dict(record_type="cofco_contract_history_v1", report_date=day, scope=contract,
                side=side, member="中粮期货", raw_member="中粮期货", account="未区分", positions=value,
                disclosure="source_value"))
    for side in ("long", "short"):
        domestic.append(dict(record_type="cofco_contract_history_v1", report_date="2022-09-02", scope="SR2301",
            side=side, member="中粮期货", raw_member=None, account="未披露", positions=None, disclosure="report_absent"))
    publish(tmp_path / "01_data/sugar_positions_preview", foreign, domestic, [], [])
    at = app(tmp_path).run(timeout=30)
    assert not at.exception and at.radio(key="cofco_basis").value == "白糖品种汇总"
    at.radio(key="cofco_basis").set_value("合约榜单可见持仓（历史）").run()
    assert not at.exception and any("此历史接口存在漏项" in item.value for item in at.warning)
    assert at.multiselect(key="cofco_years").value == [2022, 2025, 2026]
    assert len(at.get("plotly_chart")) == 9
    assert at.selectbox[1].options == ["SR 品种总排名", "SR701"]
    assert any(m.label == "五家合计榜内净持仓" and m.value == "80" for m in at.metric)
    assert any("早年未区分代客、自营" in item.value for item in at.caption)
    table = next(d.value for d in at.dataframe if "披露情况" in d.value and "两侧未披露 · 留空" in set(d.value.get("榜内持仓倾向", [])))
    assert table[table["日期"] == "2022年9月2日"].iloc[0]["榜内净持仓（手）"] == "未披露"
    at.radio(key="cofco_basis").set_value("当前范围代客排名").run()
    assert not at.exception and any("尚无中粮期货" in item.value for item in at.info)


def test_chart_does_not_connect_missing_positions():
    rows = foreign_metrics([dict(market="sugar11", report_type="futures_only", group="managed_money",
        report_date=day, long=long, short=10) for day, long in [
            ("2026-09-01", 30), ("2026-09-08", None), ("2026-09-15", 20)]])
    figure = trend(rows, "基金净持仓", {"managed_money": "管理基金"})
    assert figure.data[0].connectgaps is False and list(figure.data[0].y) == [20, None, 10]
