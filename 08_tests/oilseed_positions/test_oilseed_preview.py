from pathlib import Path
import sys
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
    assert any("尚未接通" in item.value for item in app.info)
    app.radio[0].set_value("期货＋期权").run(timeout=20)
    assert not app.exception and any("此口径暂无" in item.value for item in app.info)


def test_euronext_combined_chart_preserves_decimal_hover_and_unit():
    from oilseed_positions_page import series_figure
    figure = series_figure([dict(report_date="2026-10-02",group="investment_funds",unit="delta_equivalent_contracts",
        long=11.25,short=5.1,net=6.15,net_change=0.1,previous_date="2026-09-25")], "欧洲菜籽", "investment_funds")
    assert figure.data[0].y[0] == 6.15
    assert ".2f" in figure.data[0].hovertemplate and "Delta等价手" in figure.layout.yaxis.title.text
