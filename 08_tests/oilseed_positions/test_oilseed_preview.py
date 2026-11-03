from pathlib import Path
import sys
from copy import deepcopy
import json
import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "05_apps"))


def test_palm_without_verified_source_displays_gap_without_charts_or_fabricated_metrics(monkeypatch):
    import oilseed_positions_page
    monkeypatch.setattr(oilseed_positions_page, "read_snapshot", lambda root: {"foreign": [], "domestic": []})
    app = AppTest.from_file(str(ROOT / "05_apps/palm_positions_preview.py")).run(timeout=20)
    assert not app.exception
    assert app.title[0].value == "棕榈油资金情绪"
    assert any("尚无已验证" in item.value for item in app.info)
    assert not app.metric


def test_soybean_page_shows_foreign_positions_and_domestic_gap_with_chinese_dates(monkeypatch):
    import oilseed_positions_page
    rows = [dict(market=m, report_type="futures_only", group="managed_money", report_date=d,
        long=l,short=10,open_interest=100,unit="contracts")
        for m in ("cbot_soybean", "cbot_meal", "cbot_oil") for d,l in [("2026-09-22",20),("2026-09-29",25)]]
    monkeypatch.setattr(oilseed_positions_page,"read_snapshot", lambda root: {"foreign":rows,"domestic":[],"sources":{}})
    app = AppTest.from_file(str(ROOT / "05_apps/soybean_positions_preview.py")).run(timeout=20)
    assert not app.exception
    assert len(app.metric) == 9
    assert any("2026年9月29日" in item.value for item in app.caption)
    assert any("品种汇总数据待接入" in item.value for item in app.info)
    assert app.selectbox(key='domestic_scope_soybean').value=='M'
    assert app.radio(key="soybean_foreign_view").value == "季节性"
    app.radio(key="soybean_foreign_field").set_value("多仓").run(timeout=20)
    assert not app.exception
    charts = [json.loads(c.proto.spec) for c in app.get("plotly_chart")]
    seasonal = [c for c in charts if "季节性" in c["layout"]["title"]["text"]]
    assert len(seasonal) == 3 and all("多仓" in c["layout"]["yaxis"]["title"]["text"] for c in seasonal)
    app.radio(key="soybean_foreign_view").set_value("连续历史").run(timeout=20)
    assert not app.exception and len(app.get("plotly_chart")) == 6
    app.radio[0].set_value("期货＋期权").run(timeout=20)
    assert not app.exception and any("此口径暂无" in item.value for item in app.info)


def test_rapeseed_combined_seasonality_keeps_unit_year_filter_and_original_data(monkeypatch):
    import oilseed_positions_page
    rows = [dict(market="euronext_rapeseed", report_type="combined", group="investment_funds",
        report_date=d, long=11.25, short=5.1, open_interest=None, unit="delta_equivalent_contracts")
        for d in ("2025-10-03", "2026-10-02")]
    original = deepcopy(rows)
    monkeypatch.setattr(oilseed_positions_page, "read_snapshot", lambda root: {"foreign": rows, "domestic": [], "sources": {}})
    app = AppTest.from_file(str(ROOT / "05_apps/rapeseed_positions_preview.py")).run(timeout=20)
    app.radio[0].set_value("期货＋期权").run(timeout=20)
    assert not app.exception and len(app.get("plotly_chart")) == 2
    spec = json.loads(app.get("plotly_chart")[0].proto.spec)
    assert "Delta等价手" in spec["layout"]["yaxis"]["title"]["text"]
    assert len(spec["data"]) == 2
    app.multiselect(key="foreign_years_euronext_rapeseed").set_value([2025]).run(timeout=20)
    assert not app.exception
    spec = json.loads(app.get("plotly_chart")[0].proto.spec)
    assert len(spec["data"]) == 1 and spec["data"][0]["name"] == "2025年"
    assert rows == original


def test_euronext_combined_chart_preserves_decimal_hover_and_unit():
    from oilseed_positions_page import series_figure
    figure = series_figure([dict(report_date="2026-10-02",group="investment_funds",unit="delta_equivalent_contracts",
        long=11.25,short=5.1,net=6.15,net_change=0.1,previous_date="2026-09-25")], "欧洲菜籽", "investment_funds")
    assert figure.data[0].y[0] == 6.15
    assert ".2f" in figure.data[0].hovertemplate and "Delta等价手" in figure.layout.yaxis.title.text


def test_legacy_history_defaults_to_combined_and_never_fabricates_futures_only(monkeypatch):
    import oilseed_positions_page
    rows = [dict(market="euronext_rapeseed", report_type="combined", group="investment_funds",
        report_date=f"{year}-01-07", long=11.25, short=5.1, open_interest=None,
        unit="delta_equivalent_contracts", report_basis="mifid_legacy_combined_v1") for year in range(2022, 2027)]
    monkeypatch.setattr(oilseed_positions_page, "read_snapshot", lambda root: {"foreign":rows,"domestic":[],"sources":{}})
    app = AppTest.from_file(str(ROOT / "05_apps/rapeseed_positions_preview.py")).run(timeout=20)
    assert not app.exception and app.radio[0].value == "期货＋期权"
    figure = json.loads(app.get("plotly_chart")[0].proto.spec)
    assert len(figure['data']) == 5 and "Delta等价手" in figure['layout']['yaxis']['title']['text']
    app.radio[0].set_value("纯期货").run(timeout=20)
    assert not app.exception and not app.get("plotly_chart")


@pytest.mark.parametrize("domain,scope,member,label,short", [
    ("palm", "P2701", "高盛期货", "高盛", 45991),
    ("rapeseed", "OI", "永安期货", "永安", 7149),
])
def test_domestic_page_shows_single_sided_ranked_net_and_original_missing_side(
        monkeypatch, domain, scope, member, label, short):
    import oilseed_positions_page
    rows = []
    for day in ("2026-10-08", "2026-10-09"):
        rows.extend([dict(report_date=day, scope=scope, side=side, rank=1,
            member=name, raw_member=name + "（代客）", account="代客",
            positions=value, reported_change=0, source_provider="stock_api")
            for side, name, value in (("long", "其他期货", 100), ("short", member, short))])
    original = deepcopy(rows)
    monkeypatch.setattr(oilseed_positions_page, "read_snapshot",
        lambda root: {"foreign": [], "domestic": rows, "sources": {}})
    app = AppTest.from_file(str(ROOT / f"05_apps/{domain}_positions_preview.py")).run(timeout=20)
    app.selectbox(key=f'domestic_scope_{domain}').set_value(scope).run(timeout=20)
    assert not app.exception
    assert any(m.label == "五家合计榜内净持仓" and m.value == format(-short, ",") for m in app.metric)
    fixed = next(d.value for d in app.dataframe if "榜内持仓倾向" in d.value and len(d.value) == 5)
    seat = fixed[fixed["对象"] == label].iloc[0]
    assert seat["多仓（手）"] == "未披露"
    assert seat["空仓（手）"] == format(short, ",")
    assert seat["榜内净持仓（手）"] == format(-short, "+,")
    assert any("未披露一侧按0" in c.value for c in app.caption)
    assert app.get("plotly_chart") and rows == original


@pytest.mark.parametrize('domain,expected',[('soybean','M'),('palm','P')])
def test_variety_summary_precedes_contracts_without_falling_back_to_old_reference(monkeypatch,domain,expected):
    import oilseed_positions_page
    monkeypatch.setattr(oilseed_positions_page,'read_snapshot',lambda root:{'foreign':[],'domestic':[],'sources':{}})
    app=AppTest.from_file(str(ROOT/f'05_apps/{domain}_positions_preview.py')).run(timeout=20)
    assert not app.exception
    selection=app.selectbox(key=f'domestic_scope_{domain}')
    assert selection.value==expected and '品种汇总' in selection.options[0]
    assert all('主力参考' not in item.value for item in app.caption)
    assert any('品种汇总数据待接入' in item.value for item in app.info)
