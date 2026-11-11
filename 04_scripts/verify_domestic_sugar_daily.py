"""Independently validate a daily candidate and preserve every existing row."""
from __future__ import annotations

import argparse
import hashlib
import json
import datetime as dt
import math
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "03_src"))
from agri_research_agent.data_sources.domestic_sugar_daily import load_daily, load_fixed_closes
from agri_research_agent.pipelines.domestic_sugar_regional_history import _capture_rows
from prepare_domestic_sugar_daily import COLUMNS


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def verify(candidate: Path, current: Path | None = None) -> dict:
    feed_path = candidate / "feed.json"
    version, frame = load_daily(feed_path)
    feed = json.loads(feed_path.read_text(encoding="utf-8"))
    mapping_raw, daily_raw = (candidate / "mapping.json").read_bytes(), (candidate / "daily.json").read_bytes()
    workbook_raw = (candidate / "workbook.xlsx").read_bytes()
    if (digest(mapping_raw) != feed["mapping_sha256"] or digest(daily_raw) != feed["daily_sha256"]
            or digest(workbook_raw) != feed["workbook_sha256"]):
        raise ValueError("候选文件身份不匹配")
    maps = _capture_rows(json.loads(mapping_raw), {"ts_code", "trade_date", "mapping_ts_code"})
    mapping = {r["trade_date"]: r["mapping_ts_code"] for r in maps if r["ts_code"] == "SR.ZCE"}
    if len(mapping) != len(maps):
        raise ValueError("主力映射不唯一")
    closes = {}
    for item in json.loads(daily_raw):
        for r in _capture_rows(item, {"ts_code", "trade_date", "close"}):
            key = (r["trade_date"], r["ts_code"])
            if key in closes:
                raise ValueError("收盘行情重复")
            closes[key] = r["close"]
    fixed = load_fixed_closes(feed_path)
    if "fixed_closes" in feed:
        expected_fixed = {}
        for date in {r["date"] for r in feed["records"]}:
            day = dt.date.fromisoformat(date)
            for month in (1, 5, 9):
                code = f"SR{(day.year + int(month < day.month)) % 100:02d}{month:02d}.ZCE"
                value = closes.get((day.strftime("%Y%m%d"), code))
                if value is not None:
                    expected_fixed[(date, code)] = value
        actual_fixed = {(r.business_date.strftime("%Y-%m-%d"), r.ts_code): r.futures_price for r in fixed.itertuples()}
        if actual_fixed != expected_fixed:
            raise ValueError("固定合约收盘价与原始行情不符")
    book = openpyxl.load_workbook(candidate / "workbook.xlsx", read_only=False, data_only=True)
    try:
        sheet = book["钢联原始数据"]
        dates = {}
        for row in sheet.iter_rows(min_row=5, min_col=1, max_col=1):
            cell = row[0]
            if isinstance(cell.value, (dt.date, dt.datetime)):
                date = cell.value.strftime("%Y-%m-%d")
                if date in dates:
                    raise ValueError("工作簿日期重复")
                dates[date] = cell.row
        candidate_dates = {r["date"] for r in feed["records"]}
        if not candidate_dates or candidate_dates != {d for d in dates if "2026-10-28" < d <= max(candidate_dates)}:
            raise ValueError("候选遗漏工作簿中的日期")
        for row in feed["records"]:
            date = row["date"].replace("-", "")
            if row["contract"] != mapping.get(date) or row["close"] != closes.get((date, row["contract"])):
                raise ValueError("主力合约或收盘价不符")
            number = dates.get(row["date"])
            if number is None:
                raise ValueError("工作簿缺少报价日期")
            expected = []
            for name, col in COLUMNS[row["region"]].items():
                value = sheet[f"{col}{number}"].value
                if value is None or value == "":
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError("现货价格格式无效")
                if value > 0:
                    expected.append(dict(name=name, cell=f"{col}{number}", price=value))
            if expected != row["components"]:
                raise ValueError("现货报价来源或均价样本不符")
    finally:
        book.close()
    if current is not None:
        load_daily(current)
        previous_fixed = load_fixed_closes(current)
        fixed_map = {(r.business_date, r.ts_code): r.futures_price for r in fixed.itertuples()}
        if any(fixed_map.get((r.business_date, r.ts_code)) != r.futures_price for r in previous_fixed.itertuples()):
            raise ValueError("候选会改写或删除已发布的固定合约收盘价")
        old = json.loads(current.read_text(encoding="utf-8"))["records"]
        new = {(r["date"], r["region"]): r for r in feed["records"]}
        for previous in old:
            latest = new.get((previous["date"], previous["region"]))
            if (latest is None
                    or (previous["components"] and latest["components"] != previous["components"])
                    or (previous["contract"] is not None and latest["contract"] != previous["contract"])
                    or (previous["close"] is not None and latest["close"] != previous["close"])):
                raise ValueError("候选会改写或删除已发布的日度记录")
    return dict(feed_sha256=version, rows=len(frame), valid_spot=int(frame.cash_price.notna().sum()),
                paired=int(frame.basis.notna().sum()), max_date=frame.business_date.max().strftime("%Y-%m-%d"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--current", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.candidate, args.current), ensure_ascii=False))
