"""Build a reviewable local daily-feed candidate; never writes production."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import sys
import urllib.request
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "03_src"))
from agri_research_agent.data_sources.domestic_sugar_daily import SCHEMA, validate_daily
from agri_research_agent.pipelines.domestic_sugar_regional_history import _capture_rows

COLUMNS = {"广西糖厂": {"东糖": "J", "凤糖": "K", "湘桂": "L", "南华": "M"},
           "昆明": {"南华昆明": "O", "英茂昆明": "R"}, "乌鲁木齐": {"乌鲁木齐现货": "AB"}}


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def request(config: dict, api: str, params: dict, fields: str) -> dict:
    if config.get("url") != "https://t.xiaodefa.top/" or not config.get("token"):
        raise ValueError("行情网关配置无效")
    body = json.dumps(dict(api_name=api, token=config["token"], params=params, fields=fields)).encode()
    req = urllib.request.Request(config["url"], data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", errors="strict"))


def prepare(workbook: Path, config_path: Path, output: Path, end: dt.date) -> dict:
    raw = workbook.read_bytes(); workbook_hash = sha(raw)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    book = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    try:
        sheet = book["钢联原始数据"]
        expected_headers = {"J": "白砂糖：一级：企业报价：广西东糖集团（日）",
                            "K": "白砂糖：一级：企业报价：凤糖生化（日）",
                            "L": "白砂糖：一级：企业报价：湘桂集团（日）",
                            "M": "白砂糖：一级：企业报价：广西南华（日）",
                            "O": "白砂糖：一级：企业报价：南华昆明（日）",
                            "R": "白砂糖：一级：企业报价：英茂昆明（日）",
                            "AB": "白糖：现货价：乌鲁木齐（日）"}
        if any(sheet[f"{column}2"].value != expected for column, expected in expected_headers.items()):
            raise ValueError("工作簿列口径不符")
        prices, dates = [], set()
        for number, row in enumerate(sheet.iter_rows(min_row=5, max_col=28, values_only=True), 5):
            value = row[0]
            if not isinstance(value, (dt.date, dt.datetime)):
                continue
            day = value.date() if isinstance(value, dt.datetime) else value
            if day <= dt.date(2026, 10, 28) or day > end:
                continue
            if day in dates:
                raise ValueError("日度表日期重复")
            dates.add(day)
            for region, fields in COLUMNS.items():
                components = []
                for name, col in fields.items():
                    price = row[openpyxl.utils.column_index_from_string(col) - 1]
                    if price is None or price == "":
                        continue
                    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
                        raise ValueError("日度报价不是有效数字")
                    if price > 0:
                        components.append(dict(name=name, price=price, cell=f"{col}{number}"))
                prices.append(dict(date=day.isoformat(), region=region, components=components))
    finally:
        book.close()
    if sha(workbook.read_bytes()) != workbook_hash or not dates:
        raise ValueError("工作簿发生变化或没有新日期")
    start, stop = min(dates).strftime("%Y%m%d"), max(dates).strftime("%Y%m%d")
    mapping = request(config, "fut_mapping", dict(ts_code="SR.ZCE", start_date=start, end_date=stop),
                      "ts_code,trade_date,mapping_ts_code")
    maps = _capture_rows(mapping, {"ts_code", "trade_date", "mapping_ts_code"})
    fixed_codes = {f"SR{(day.year + int(month < day.month)) % 100:02d}{month:02d}.ZCE"
                   for day in dates for month in (1, 5, 9)}
    codes = sorted({r["mapping_ts_code"] for r in maps} | fixed_codes)
    daily = [request(config, "fut_daily", dict(ts_code=code, start_date=start, end_date=stop),
                     "ts_code,trade_date,close") for code in codes]
    mapped, closes = {}, {}
    for r in maps:
        if r["ts_code"] != "SR.ZCE" or r["trade_date"] in mapped:
            raise ValueError("主力映射无效或重复")
        mapped[r["trade_date"]] = r["mapping_ts_code"]
    for capture in daily:
        for r in _capture_rows(capture, {"ts_code", "trade_date", "close"}):
            key = (r["trade_date"], r["ts_code"])
            if key in closes:
                raise ValueError("收盘行情重复")
            closes[key] = r["close"]
    for r in prices:
        trade_date = r["date"].replace("-", "")
        r["contract"] = mapped.get(trade_date)
        r["close"] = closes.get((trade_date, r["contract"]))
    mapping_raw = json.dumps(mapping, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    daily_raw = json.dumps(daily, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    fixed = [dict(date=day.isoformat(), contract=code, close=closes[(day.strftime("%Y%m%d"), code)])
             for day in sorted(dates) for month in (1, 5, 9)
             for code in [f"SR{(day.year + int(month < day.month)) % 100:02d}{month:02d}.ZCE"]
             if (day.strftime("%Y%m%d"), code) in closes and closes[(day.strftime("%Y%m%d"), code)] is not None]
    payload = dict(schema_version=SCHEMA, workbook_sha256=workbook_hash,
                   mapping_sha256=sha(mapping_raw), daily_sha256=sha(daily_raw), records=prices, fixed_closes=fixed)
    validate_daily(payload)
    output.mkdir(parents=True, exist_ok=False)
    (output / "workbook.xlsx").write_bytes(raw)
    (output / "mapping.json").write_bytes(mapping_raw)
    (output / "daily.json").write_bytes(daily_raw)
    feed = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    (output / "feed.json").write_bytes(feed)
    return dict(feed_sha256=sha(feed), workbook_sha256=workbook_hash, dates=len(dates), rows=len(prices))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--gateway-config", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "01_data" / "candidates" / "domestic_sugar_daily"
    if not args.candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("候选目录必须在白糖日度候选目录内")
    if args.end > dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date():
        raise ValueError("截止日期不能晚于今天")
    print(json.dumps(prepare(args.workbook, args.gateway_config, args.candidate, args.end), ensure_ascii=False))
