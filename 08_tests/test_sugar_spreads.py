"""White sugar routes, exact-leg pricing, and data candidate regression coverage."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import importlib
import json
from pathlib import Path
import sys

import pandas as pd
import pyarrow as pa
import pytest
from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

from agri_research_agent.application.sugar_spreads import (
    DATA_PATH, DOMESTIC_CURVE_PATH, PREVIEW_FILES, PREVIEW_RELEASES, PREVIEW_ROOT,
    load_quotes, resolve_preview_release, spread_history,
)


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "05_apps"), str(ROOT / "04_scripts")]
ENTRY = ROOT / "05_apps/streamlit_app.py"
AS_OF = date(2026, 11, 24)


def write_quotes(tmp_path):
    path = tmp_path / DATA_PATH
    path.parent.mkdir(parents=True)
    path.write_text("""trade_date,contract,price_type,price,provider,source_url
2025-11-24,SB202603,close,15.50,Test,https://example.com/raw
2025-11-24,SB202605,close,15.25,Test,https://example.com/raw
2026-11-23,SB202703,close,17.61,Test,https://example.com/raw
2026-11-23,SB202705,close,16.98,Test,https://example.com/raw
2026-11-23,SB202710,close,16.60,Test,https://example.com/raw
2026-11-23,SB202803,close,17.05,Test,https://example.com/raw
2026-11-24,SB202703,close,17.70,Test,https://example.com/raw
2026-11-24,SB202703,settlement,17.66,Source,https://example.com/report
2026-11-24,W202703,settlement,501.00,Source,https://example.com/report
2026-11-24,SB202705,settlement,17.08,Source,https://example.com/report
2026-11-24,W202705,settlement,503.00,Other,https://example.com/other
2026-11-24,W202710,settlement,483.00,Source,https://example.com/report
""", encoding="utf-8")
    return path


def test_pairs_require_same_date_basis_and_provider_and_handle_year_roll(tmp_path):
    quotes = load_quotes(write_quotes(tmp_path), as_of=AS_OF)
    monthly = spread_history(quotes, 3, 5, premium=False, price_type="close")
    assert [(str(p.trade_date), str(p.value)) for p in monthly] == [
        ("2025-11-24", "0.25"), ("2026-11-23", "0.63"),
    ]
    roll, = spread_history(quotes, 10, 3, premium=False, price_type="close")
    assert (roll.left.contract, roll.right.contract, roll.value) == (
        "SB202710", "SB202803", Decimal("-0.45"),
    )
    premium, = spread_history(quotes, 3, 3, premium=True, price_type="settlement")
    assert premium.value.quantize(Decimal("0.01")) == Decimal("111.66")
    assert spread_history(quotes, 5, 5, premium=True, price_type="settlement") == []
    assert spread_history(quotes, 10, 10, premium=True, price_type="settlement") == []


def test_duplicate_snapshot_key_is_rejected(tmp_path):
    path = write_quotes(tmp_path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("2026-11-24,SB202703,close,17.70,Test,https://example.com/raw\n")
    with pytest.raises(ValueError, match="唯一键"):
        load_quotes(path, as_of=AS_OF)


def test_sugar_group_order_and_default_price_views(tmp_path, monkeypatch):
    nav = importlib.import_module("navigation")
    groups = nav.research_navigation_groups()
    expected = ["白糖价差", "白糖持仓", "白糖基差", "白糖进口利润", "白糖天气"]
    sugar, = (group for group in groups if group.title == "白糖")
    assert [item.label for item in sugar.items] == expected
    assert [item.label for group in groups for item in group.items if item.label in expected] == expected

    monkeypatch.setenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", str(tmp_path))
    write_quotes(tmp_path)
    app = AppTest.from_file(str(ENTRY), default_timeout=20)
    app.query_params["workspace_page"] = "白糖价差"
    app.run()
    assert not app.exception
    assert app.radio(key="sugar_spread_view").options == ["纽约原糖月差", "伦敦白糖升水", "郑糖月差"]
    assert app.radio(key="sugar_spread_view").value == "纽约原糖月差"
    assert app.metric[0].value == "+0.63 美分/磅"
    assert app.get("plotly_chart")
    app.radio(key="sugar_spread_view").set_value("伦敦白糖升水").run()
    assert not app.exception
    assert app.metric[0].value == "+111.66 美元/吨"
    assert app.metric[1].value == "暂无"
    assert app.get("plotly_chart")

    positions = importlib.import_module("sugar_positions_page")
    calls = []
    monkeypatch.setattr(positions, "render_sugar_positions_page",
                        lambda project_root, **kwargs: calls.append((project_root, kwargs)))
    app.query_params["workspace_page"] = "白糖持仓"
    app.run()
    assert not app.exception
    assert calls == [(ROOT, {"preview_mode": False})]


def test_domestic_term_structure_uses_the_latest_complete_source_and_exact_delivery_months(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", str(tmp_path))
    write_quotes(tmp_path)
    target = tmp_path / DOMESTIC_CURVE_PATH
    target.write_text("""trade_date,contract,price_type,price,provider,source_url
2026-11-23,SR202703,close,5050,郑商所,https://example.com/old
2026-11-24,SR202711,close,5214,郑商所,https://example.com/latest
2026-11-24,SR202701,close,5114,郑商所,https://example.com/latest
2026-11-24,SR202701,settlement,5075,郑商所,https://example.com/latest
2026-11-24,SR202705,close,5159,郑商所,https://example.com/latest
2026-11-24,SR202709,close,5202,另一来源,https://example.com/other
""", encoding="utf-8")
    app = AppTest.from_string("""
from pathlib import Path
import streamlit as st
from sugar_spreads_page import render_sugar_spreads_page
render_sugar_spreads_page(Path.cwd(), lambda _path: st.subheader("郑糖月差季节性"))
""", default_timeout=20).run()
    app.radio(key="sugar_spread_view").set_value("郑糖月差").run()
    assert not app.exception and not app.error
    chart, = app.get("plotly_chart")
    trace, = json.loads(chart.proto.spec)["data"]
    assert trace["x"] == ["2027-01", "2027-05", "2027-11"]
    assert trace["y"] == [5114.0, 5159.0, 5214.0]
    assert "元/吨" in trace["hovertemplate"]
    assert [header.value for header in app.subheader] == ["郑糖期限结构", "郑糖月差季节性"]
    assert app.caption[0].value == "2026-11-24 · 日收盘价 · 郑商所"

    target.unlink()
    app.run()
    assert not app.exception and not app.get("plotly_chart")
    assert [header.value for header in app.subheader] == ["郑糖月差季节性"]


def test_local_candidate_merges_without_rewriting_prior_observations(tmp_path):
    cli = importlib.import_module("prepare_sugar_spread_candidate")
    quote = load_quotes(write_quotes(tmp_path), as_of=AS_OF)[0]
    newer = replace(quote, source_url="https://example.com/new")
    assert cli.merge_quotes([quote, newer]) == [quote]
    with pytest.raises(ValueError, match="冲突"):
        cli.merge_quotes([quote, replace(quote, price=Decimal("17.71"))])
    assert cli.serialize_quotes([quote]).decode().splitlines()[1].endswith(quote.source_url)


def test_monitor_uses_matching_pair_dates_and_keeps_missing_visible(tmp_path):
    from datetime import datetime, timezone
    from agri_research_agent.monitoring.observations import Inputs, Reader

    write_quotes(tmp_path)
    reader = Reader(Inputs(*([tmp_path] * 7)), datetime(2026, 11, 25, tzinfo=timezone.utc))
    raw = reader.read("sugar_raw_spread")
    assert raw.actual is None
    assert raw.extra["series_dates"] == {"03–05": "2026-11-23", "05–07": None,
                                         "07–10": None, "10–次年03": "2026-11-23"}
    premium = reader.read("sugar_white_premium")
    assert premium.actual is None
    assert premium.extra["series_dates"] == {"03月": "2026-11-24", "05月": None, "10月": None}

def test_refresh_uses_exact_sr_closes_and_existing_month_windows():
    refresh = importlib.import_module("refresh_sugar_spreads_preview")
    source = importlib.import_module("agri_research_agent.application.sugar_spread_sources")
    book = Workbook(write_only=True)
    sheet = book.create_sheet()
    sheet.append([f"郑州商品交易所期货每日行情表({AS_OF})"])
    sheet.append(["合约代码", "今收盘", "今结算", "成交量(手)"])
    sheet.append(["SR701", "5,114", "5,075", "495,948"])
    sheet.append(["SR705", "5,159", "5,122", "9"])
    sheet.append(["SR709", "5,202", "5,179", "6"])
    sheet.append(["SR711", "0", "5,198", "0"])
    data = BytesIO()
    book.save(data)
    url = source.report_url(AS_OF, "domestic")
    quotes = source.parse_czce_daily(data.getvalue(), AS_OF, url)
    assert [(q.contract, q.price) for q in quotes] == [
        ("SR202701", 5114), ("SR202705", 5159), ("SR202709", 5202),
    ]
    with pytest.raises(ValueError, match="日期"):
        source.parse_czce_daily(data.getvalue(), date(2026, 11, 25), url)
    config = pd.read_excel(ROOT / "02_configs/historical_spread_config.xlsx", sheet_name="spread_config")
    config = config[config.spread_name.isin(refresh.SR_SPREADS)]
    rows = refresh.domestic_spread_rows(quotes, config)
    assert {row["spread_name"]: row["spread_value"] for row in rows} == {"SR 1-5": -45, "SR 5-9": -43}
    prior = pa.Table.from_pylist([rows[0]])
    result = refresh.extend_spread_database(prior, rows)
    assert result.num_rows == 2 and result.slice(0, 1).equals(prior)
    assert refresh.extend_spread_database(result, rows) is result
    with pytest.raises(ValueError, match="冲突"):
        refresh.extend_spread_database(result, [dict(rows[0], leg1_price=999)])


def test_refresh_validates_both_settlement_tables_and_reuses_covered_report():
    source = importlib.import_module("agri_research_agent.application.sugar_spread_sources")
    url = source.report_url(AS_OF, "foreign")
    payload = """<h1>Daily Market Price Updates and Commentary 24th November 2026</h1>
    <script>{"datePublished":"2026-11-24T18:11:05+00:00"}</script>
    <h2>ICE Futures U.S. Sugar No.11 Contract</h2>
    <table><tr><th>Settlement (cUS$/lb)</th></tr>
    <tr><td>Mar27</td><td>17.66</td></tr><tr><td>May27</td><td>17.08</td></tr></table>
    <h2>ICE Europe White Sugar Futures Contract</h2>
    <table><tr><th>Settlement (US$/mt)</th></tr>
    <tr><td>Mar27</td><td>501.00</td></tr><tr><td>May27</td><td>505.40</td></tr></table>"""
    parsed = source.parse_czapp_settlements(payload.encode(), AS_OF, url)
    assert [(q.contract, str(q.price)) for q in parsed] == [
        ("SB202703", "17.66"), ("SB202705", "17.08"), ("W202703", "501.00"), ("W202705", "505.40"),
    ]
    with pytest.raises(ValueError, match="发布日"):
        source.parse_czapp_settlements(payload.replace("T18:", "T08:").encode(), AS_OF, url)
    with pytest.raises(ValueError, match="配对"):
        source.parse_czapp_settlements(payload.replace("Settlement (US$/mt)", "Last (US$/mt)").encode(), AS_OF, url)
    calls = []
    batches, receipts, _ = source.collect_reports([AS_OF, date(2026, 11, 25)], "foreign", parsed,
                                                  fetcher=lambda url: calls.append(url))
    assert calls == [source.report_url(date(2026, 11, 25), "foreign")]
    assert batches == [] and receipts[0]["status"] == "not_published"


def test_local_generation_ties_every_input_to_one_validated_pointer(tmp_path):
    (tmp_path / ".git").write_text("gitdir: linked-worktree", encoding="utf-8")
    preview = tmp_path / PREVIEW_ROOT
    base = preview / PREVIEW_RELEASES
    release = base / ("1" * 24)
    release.mkdir(parents=True)
    for name in PREVIEW_FILES:
        (release / name).write_bytes(name.encode())
    (base / "current.json").write_text(json.dumps({
        "release_id": release.name,
        "files": {name: sha256((release / name).read_bytes()).hexdigest() for name in PREVIEW_FILES},
    }), encoding="utf-8")
    assert resolve_preview_release(tmp_path, preview) == release
    assert resolve_preview_release(tmp_path, tmp_path / "external") is None
    (release / DATA_PATH.name).write_bytes(b"changed")
    with pytest.raises(ValueError, match="文件校验失败"):
        resolve_preview_release(tmp_path, preview)
