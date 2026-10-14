from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.warehouse_receipts.model import FIELDS, PRODUCTS, ReceiptError, normalize_rows, aggregate_daily, load_snapshot, write_snapshot
from agri_research_agent.warehouse_receipts.update import merge_observations, prepare_incremental


def observation(symbol="M", date="20261009", warehouse="A", vol=10):
    r = dict.fromkeys(FIELDS.split(","))
    r.update(symbol=symbol, trade_date=date, warehouse=warehouse, wh_id=warehouse, vol=vol,
             pre_vol=None, vol_chg=0, unit="手" if PRODUCTS[symbol][1]=="DCE" else "张",
             exchange=PRODUCTS[symbol][1], fut_name=PRODUCTS[symbol][0])
    return r


def test_returned_date_replaces_all_warehouses_without_touching_other_dates_or_empty_symbols():
    baseline = normalize_rows([observation(date="20261008"), observation(), observation(warehouse="B"), observation("Y")])
    incoming = normalize_rows([observation(vol=20), observation(date="20261012", vol=50)])
    result = merge_observations(baseline, incoming, dt.date(2026,10,9), dt.date(2026,10,12))
    assert result.loc[result.trade_date.eq("20261009") & result.symbol.eq("M"), "vol"].tolist() == [20]
    assert result.loc[result.trade_date.eq("20261008"), "vol"].tolist() == [10]
    assert result.loc[result.symbol.eq("Y"), "vol"].tolist() == [10]
    with pytest.raises(ReceiptError, match="窗口"):
        merge_observations(baseline, incoming, dt.date(2026,10,12), dt.date(2026,10,12))


def test_api_order_and_csv_numeric_dimensions_do_not_create_false_revisions():
    rows = [dict(observation("SR"), brand="甲",year=2526,pd=100),
            dict(observation("SR"),brand="乙",year=2526.0,pd=None)]
    first = normalize_rows(rows)
    reread = [{**row, "year":"2526.0", "pd":"100.0" if row["pd"] is not None else None} for row in reversed(rows)]
    second = normalize_rows(reread)
    assert first[FIELDS.split(",")].to_csv(index=False)==second[FIELDS.split(",")].to_csv(index=False)


class Client:
    endpoint = "https://example.invalid"
    def __init__(self, rows): self.rows = rows
    def query(self, symbol, start, end):
        return dict(api_name="fut_wsr", endpoint=self.endpoint, params=dict(symbol=symbol, exchange=PRODUCTS[symbol][1],
           start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")), rows=[r for r in self.rows if r["symbol"]==symbol])
    def calendar(self, start, end):
        return [dict(exchange="DCE",cal_date=(start+dt.timedelta(days=i)).strftime("%Y%m%d"),
                     is_open=int((start+dt.timedelta(days=i)).weekday()<5)) for i in range((end-start).days+1)]


def seed(root):
    raw = normalize_rows([observation(symbol) for symbol in PRODUCTS])
    return write_snapshot(root, raw, aggregate_daily(raw), dict(source="Tushare-compatible",requested_start="2026-10-01",
          requested_end="2026-10-09", coverage={s:{} for s in PRODUCTS}))


def test_incremental_checks_all_products_and_preserves_baseline(tmp_path):
    baseline = tmp_path / "baseline"; seed(baseline)
    pointer = (baseline / "current.json").read_bytes()
    result = prepare_incremental(Client([observation(date="20261012",vol=40)]), baseline,
            tmp_path/"candidate", tmp_path/"raw", dt.datetime(2026,10,12,18,tzinfo=dt.timezone(dt.timedelta(hours=8))))
    assert result["status"] == "WAITING_FOR_SOURCE" and "M" not in result["pending_products"]
    assert result["expected_trade_date"] == "2026-10-12"
    assert (baseline/"current.json").read_bytes() == pointer
    data, _ = load_snapshot(tmp_path/"candidate")
    assert len(data)==7 and data.loc[data.symbol.eq("M"),"vol"].tolist()==[10,40]


def test_empty_update_is_no_change_and_failure_keeps_existing_pointer(tmp_path):
    baseline = tmp_path / "baseline"; seed(baseline)
    pointer = (baseline/"current.json").read_bytes()
    result = prepare_incremental(Client([]), baseline, tmp_path/"candidate", tmp_path/"raw",
            dt.datetime(2026,10,10,18,tzinfo=dt.timezone(dt.timedelta(hours=8))))
    assert result["status"] == "NO_CHANGE" and not result["business_changed"]
    assert (tmp_path/"candidate/current.json").read_bytes()==pointer
    class Broken(Client):
        def query(self,*args): raise ReceiptError("unavailable")
    with pytest.raises(ReceiptError):
        prepare_incremental(Broken([]),baseline,tmp_path/"failure",tmp_path/"failed_raw",
                             dt.datetime(2026,10,12,18,tzinfo=dt.timezone(dt.timedelta(hours=8))))
    assert (baseline/"current.json").read_bytes()==pointer
    assert not (tmp_path/"failure/current.json").exists()
