from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from agri_research_agent.warehouse_receipts.charts import build_figure, seasonal_rows
from agri_research_agent.warehouse_receipts.model import (
    FIELDS, ReceiptError, aggregate_daily, load_snapshot, normalize_rows, write_snapshot,
)
from agri_research_agent.warehouse_receipts.provider import acquire_window

ROOT = Path(__file__).resolve().parents[1]


def row(symbol="M", date="20261009", warehouse="仓库A", vol=100, **extra):
    result = {name: None for name in FIELDS.split(",")}
    result.update(trade_date=date, symbol=symbol, warehouse=warehouse, wh_id="001",
                  fut_name="白糖" if symbol == "SR" else "豆粕", vol=vol, pre_vol=95, vol_chg=5,
                  unit="张" if symbol == "SR" else "手", exchange="CZCE" if symbol == "SR" else "DCE")
    result.update(extra)
    return result


def test_full_dimensions_keep_brands_and_deduplicate_identical_rows():
    a = row(symbol="SR", brand="甲", year=2526)
    b = row(symbol="SR", brand="乙", year=2526, vol=50)
    raw = normalize_rows([a, a, b])
    assert len(raw) == 2
    assert aggregate_daily(raw).vol.iloc[0] == 150
    with pytest.raises(ReceiptError, match="冲突"):
        normalize_rows([a, {**a, "vol": 99}])


def test_totals_never_double_count_and_null_components_never_become_zero():
    data = normalize_rows([row(), row(warehouse="仓库B", wh_id="002", vol=0),
                           row(warehouse="合计", wh_id=None, vol=100)])
    daily = aggregate_daily(data)
    assert daily.vol.iloc[0] == 100
    assert daily.aggregation.iloc[0] == "source_total"
    with pytest.raises(ReceiptError, match="不一致"):
        aggregate_daily(normalize_rows([row(), row(warehouse="合计", wh_id=None, vol=101)]))
    daily = aggregate_daily(normalize_rows([row(), row(warehouse="B", wh_id="002", vol=None, pre_vol=None, vol_chg=None)]))
    assert pd.isna(daily.vol.iloc[0]) and pd.isna(daily.pre_vol.iloc[0]) and pd.isna(daily.vol_chg.iloc[0])
    assert daily.status.iloc[0] == "missing_component"
    zero = aggregate_daily(normalize_rows([row(vol=0)]))
    assert zero.vol.iloc[0] == 0 and zero.status.iloc[0] == "actual"


def test_mixed_units_fail_and_historical_unit_changes_remain_separate():
    with pytest.raises(ReceiptError, match="单位不一致"):
        aggregate_daily(normalize_rows([row(), row(warehouse="B", wh_id="002", unit="吨")]))
    daily = aggregate_daily(normalize_rows([row(), row(date="20251009", unit="吨")]))
    with pytest.raises(ReceiptError, match="单位无效"):
        build_figure(daily, field="vol", focus_year=2026, years=[2025, 2026], title="测试", unit="手",
                     trading_calendar=pd.date_range("2025-01-01", "2026-12-31"))


def test_negative_provider_quantity_preserves_raw_and_blanks_the_affected_total():
    raw = normalize_rows([row(vol=-50), row(warehouse="B", wh_id="002", vol=100)])
    assert raw.loc[raw.warehouse.eq("仓库A"), "vol"].iloc[0] == -50
    daily = aggregate_daily(raw)
    assert pd.isna(daily.vol.iloc[0])
    assert daily.status.iloc[0] == "invalid_component"


def test_recursive_windows_are_disjoint_and_single_day_cap_fails(tmp_path):
    class Client:
        endpoint = "https://example.invalid"
        calls = []
        def query(self, symbol, start, end):
            self.calls.append((start, end))
            return dict(api_name="fut_wsr", endpoint=self.endpoint,
                        params=dict(symbol=symbol, exchange="DCE", start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")),
                        rows=[row(date=start.strftime("%Y%m%d"))] * (1000 if start != end else 1))
    client = Client()
    first, end = dt.date(2026, 10, 8), dt.date(2026, 10, 9)
    result = acquire_window(client, "M", first, end, tmp_path)
    assert len(result) == 2
    assert client.calls == [(first, end), (first, first), (end, end)]
    assert acquire_window(client, "M", first, end, tmp_path) == result
    class Capped(Client):
        def query(self, symbol, start, end):
            data = super().query(symbol, start, end)
            data["rows"] *= 1000
            return data
    with pytest.raises(ReceiptError, match="单日"):
        acquire_window(Capped(), "M", first, first, tmp_path / "capped")


def test_snapshot_identity_and_recalculation(tmp_path):
    raw = normalize_rows([row(), row(date="20251009")])
    daily = aggregate_daily(raw)
    manifest = write_snapshot(tmp_path, raw, daily, dict(requested_start="2025-01-01", requested_end="2026-10-09"))
    reread, _ = load_snapshot(tmp_path)
    pd.testing.assert_frame_equal(daily, reread)
    (tmp_path / "snapshots" / manifest["snapshot_id"] / "daily.csv").write_text("tampered", encoding="utf-8")
    with pytest.raises(ReceiptError, match="身份"):
        load_snapshot(tmp_path)


def test_seasonality_missing_days_leap_alignment_and_year_lines_only():
    daily = aggregate_daily(normalize_rows([
        row(date="20240301", vol=20), row(date="20250301", vol=40),
        row(date="20260302", vol=0), row(date="20260304", vol=60),
    ]))
    calendar = pd.date_range("2024-01-01", "2026-12-31")
    aligned = seasonal_rows(daily, 2026, "vol", calendar)
    assert pd.isna(aligned.loc[aligned.date.eq("2026-03-03"), "value"]).all()
    assert aligned.loc[aligned.date.eq("2026-03-02"), "value"].iloc[0] == 0
    figure = build_figure(daily, field="vol", focus_year=2026, years=[2024, 2025, 2026], title="测试", unit="手", trading_calendar=calendar)
    assert figure.data[-1].line.color == "#C1493F" and figure.data[-1].line.width == 3.4
    assert all(trace.connectgaps is False for trace in figure.data)
    assert len(figure.data) == 3
    assert all("均值" not in trace.name for trace in figure.data)
    for trace in figure.data[:2]:
        assert pd.Timestamp("2000-03-01") in trace.x


def test_page_empty_and_source_backed_chart_detail_paths(tmp_path, monkeypatch):
    script = f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(ROOT / '05_apps')!r})
from warehouse_receipts_page import render_warehouse_receipts
render_warehouse_receipts(Path({str(ROOT)!r}))
"""
    monkeypatch.setenv("WAREHOUSE_RECEIPTS_ROOT", str(tmp_path))
    app = AppTest.from_string(script).run()
    assert not app.exception and "尚未接入" in app.info[0].value
    raw = normalize_rows([row(date="20251009"), row(date="20261009", vol=0)])
    daily = aggregate_daily(raw)
    write_snapshot(tmp_path, raw, daily, dict(requested_start="2025-01-01", requested_end="2026-10-09",
                   coverage={"M": {"first": "2025-10-09", "last": "2026-10-09", "units": ["手"], "observed_days": 2}}))
    app = AppTest.from_string(script, default_timeout=20).run()
    assert not app.exception
    assert len(app.get("plotly_chart")) == 1
    assert not app.checkbox
    assert len(app.dataframe) == 2
    assert app.metric[0].value == "0"
    app.selectbox[1].set_value("每日增减").run()
    assert not app.exception
    spec = json.loads(app.get("plotly_chart")[0].proto.spec)
    assert "每日增减" in spec["layout"]["yaxis"]["title"]["text"]


def test_warehouse_view_is_independent_of_spread_database(tmp_path, monkeypatch):
    monkeypatch.setenv("WAREHOUSE_RECEIPTS_ROOT", str(tmp_path))
    script = f"""
import sys
sys.path.insert(0, {str(ROOT / '05_apps')!r})
import streamlit as st
from streamlit_app import render_spread_dashboard
st.session_state['spread_research_view'] = '仓单季节性'
render_spread_dashboard()
"""
    app = AppTest.from_string(script, default_timeout=20).run()
    assert not app.exception and not app.error
    assert "尚未接入" in app.info[0].value
