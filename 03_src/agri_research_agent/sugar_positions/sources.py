"""Bounded official HTTP collectors and strict parsers (no vendor API keys)."""
from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from io import BytesIO, StringIO
import json
import re
from zipfile import BadZipFile

import openpyxl
import requests

from .model import integer, split_member, unique_rows

CFTC_DATASETS = {"futures_only": "72hh-3qpy", "combined": "kh3c-gbw2"}
FIELDS = {"producer": "prod_merc", "swap": "swap", "managed_money": "m_money",
          "other": "other_rept", "nonreportable": "nonrept"}
CFTC_FIELDS = {
    "producer": ("prod_merc_positions_long", "prod_merc_positions_short", None),
    "swap": ("swap_positions_long_all", "swap__positions_short_all", "swap__positions_spread_all"),
    "managed_money": ("m_money_positions_long_all", "m_money_positions_short_all", "m_money_positions_spread"),
    "other": ("other_rept_positions_long", "other_rept_positions_short", "other_rept_positions_spread"),
    "nonreportable": ("nonrept_positions_long_all", "nonrept_positions_short_all", None),
}


def _day(text):
    return date.fromisoformat(str(text)[:10]).isoformat()


def _cot_record(row, market, report_type, source_url, retrieved_at):
    lowered = {k.lower(): v for k, v in row.items()}
    day = _day(lowered["report_date_as_yyyy_mm_dd"]) if market == "sugar11" else (
        datetime.strptime(lowered["as_of_date_form_mm/dd/yyyy"], "%m/%d/%Y").date().isoformat())
    if date.fromisoformat(day) > datetime.now(timezone.utc).date():
        raise ValueError("持仓日期在未来")
    oi = integer(lowered["open_interest_all"])
    result = []
    for group, prefix in FIELDS.items():
        # Socrata API names differ from the official CSV names (including double underscores).
        names = CFTC_FIELDS[group] if market == "sugar11" else (
            f"{prefix}_positions_long_all", f"{prefix}_positions_short_all",
            f"{prefix}_positions_spread_all" if group in ("swap", "managed_money", "other") else None)
        long = integer(lowered[names[0]])
        short = integer(lowered[names[1]])
        spread = integer(lowered[names[2]]) if names[2] else 0
        result.append(dict(market=market, report_type=report_type, group=group,
            report_date=day, long=long, short=short, spreading=spread, open_interest=oi,
            unit="contracts", scope="all_expiries", source_url=source_url,
            retrieved_at=retrieved_at))
    # Combined reports round delta-equivalent category counts independently.
    # Eight rounded category terms vs one rounded OI can differ by up to four lots.
    tolerance = 4 if report_type == "combined" else 0
    for side in ("long", "short"):
        total = sum(r[side] + r["spreading"] for r in result)
        if abs(total - oi) > tolerance:
            raise ValueError(f"{market} {day} {side} 未与总持仓核对一致")
        if market == "sugar11":
            aggregate_key = "tot_rept_positions_long_all" if side == "long" else "tot_rept_positions_short"
            aggregate = integer(lowered[aggregate_key])
            nonreportable = next(r[side] for r in result if r["group"] == "nonreportable")
            if abs(aggregate + nonreportable - oi) > (1 if report_type == "combined" else 0):
                raise ValueError(f"{market} {day} {side} 官方报告者合计未与总持仓一致")
    return result


def parse_cftc(payload, report_type, source_url, retrieved_at):
    if report_type not in CFTC_DATASETS or not isinstance(payload, list) or not payload:
        raise ValueError("CFTC 报告为空或口径无效")
    rows = []
    for item in payload:
        if (item.get("cftc_contract_market_code") != "080732"
                or "SUGAR NO. 11" not in item.get("market_and_exchange_names", "")):
            raise ValueError("CFTC 返回了非糖11数据")
        rows.extend(_cot_record(item, "sugar11", report_type, source_url, retrieved_at))
    unique_rows(rows, ("market", "report_type", "group", "report_date"))
    return rows


def parse_ice(content, year, source_url, retrieved_at):
    rows = []
    for item in csv.DictReader(StringIO(content.decode("utf-8-sig", errors="strict"))):
        name = item.get("Market_and_Exchange_Names", "").strip()
        if not name.startswith("ICE White Sugar Futures"):
            continue
        kind = {"FutOnly": "futures_only", "Combined": "combined"}.get(item.get("FutOnly_or_Combined"))
        if kind is None or item.get("CFTC_Commodity_Code") != "W":
            raise ValueError("ICE 白砂糖报告类型或市场代码未知")
        parsed = _cot_record(item, "white_sugar", kind, source_url, retrieved_at)
        if date.fromisoformat(parsed[0]["report_date"]).year != year:
            raise ValueError("ICE 文件年份与报告日期不一致")
        rows.extend(parsed)
    if not rows:
        raise ValueError("ICE 文件没有白砂糖数据")
    unique_rows(rows, ("market", "report_type", "group", "report_date"))
    return rows


def parse_czce(content, expected_day, source_url, retrieved_at):
    if not content.startswith(b"PK"):
        raise ValueError("郑商所返回的不是 XLSX，可能是访问限制页面")
    try:
        book = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=True)
    except (BadZipFile, openpyxl.utils.exceptions.InvalidFileException) as exc:
        raise ValueError("郑商所 XLSX 文件损坏") from exc
    sections = {}
    scope = None
    header = False
    try:
        for values in book.active.values:
            values = tuple(values) + (None,) * max(0, 10 - len(values))
            title = str(values[0] or "")
            if title.startswith(("品种：", "合约：")):
                match = re.search(r"(SR\d*)\s+日期：(\d{4}-\d{2}-\d{2})", title)
                scope = None
                header = False
                if match:
                    scope, day = match.groups()
                    if day != expected_day:
                        raise ValueError("郑商所文件与请求日期不一致")
                    if scope in sections:
                        raise ValueError("郑商所重复白糖排名区块")
                    sections[scope] = {"rows": [], "totals": None}
                continue
            if scope is None:
                continue
            if title == "名次":
                if values[5] != "持买仓量" or values[8] != "持卖仓量":
                    raise ValueError("郑商所持仓列发生变化")
                header = True
                continue
            if title == "合计":
                sections[scope]["totals"] = {"long": integer(values[5]), "short": integer(values[8])}
                scope = None
                continue
            if values[0] is None:
                continue
            if not header:
                raise ValueError("郑商所持仓表头缺失")
            rank = integer(values[0])
            if not 1 <= rank <= 20:
                raise ValueError("郑商所排名超出前20名")
            for side, offset in (("long", 4), ("short", 7)):
                if values[offset] is None or str(values[offset]).strip() in ("", "-", "—"):
                    if any(v not in (None, "", "-", "—") for v in values[offset+1:offset+3]):
                        raise ValueError("匿名持仓行")
                    continue
                raw = str(values[offset]).strip()
                member, account = split_member(raw)
                sections[scope]["rows"].append(dict(report_date=expected_day, scope=scope,
                    side=side, rank=rank, raw_member=raw, member=member, account=account,
                    positions=integer(values[offset+1]),
                    reported_change=integer(values[offset+2], signed=True, optional=True),
                    unit="contracts", source_url=source_url, retrieved_at=retrieved_at))
    finally:
        book.close()
    result = []
    if "SR" not in sections:
        raise ValueError("郑商所文件缺少 SR 品种排名")
    for key, section in sections.items():
        if section["totals"] is None or not section["rows"]:
            raise ValueError(f"{key}排名区块不完整")
        for side in ("long", "short"):
            selected = [r for r in section["rows"] if r["side"] == side]
            ranks = [r["rank"] for r in selected]
            if ranks != list(range(1, len(ranks) + 1)):
                raise ValueError(f"{key}排名不连续")
            if sum(r["positions"] for r in selected) != section["totals"][side]:
                raise ValueError(f"{key}排名合计校验失败")
        result.extend(section["rows"])
    unique_rows(result, ("report_date", "scope", "side", "member", "account"))
    return result


class OfficialSources:
    def __init__(self, session=None, timeout=25):
        self.session = session or requests.Session()
        self.timeout = timeout

    def _get(self, url, **kwargs):
        response = self.session.get(url, timeout=self.timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; sugar-position-research/1.0)"}, **kwargs)
        response.raise_for_status()
        return response

    def cftc(self, report_type, start_year):
        url = f"https://publicreporting.cftc.gov/resource/{CFTC_DATASETS[report_type]}.json"
        response = self._get(url, params={"cftc_contract_market_code": "080732", "$limit": 10000,
            "$where": f"report_date_as_yyyy_mm_dd >= '{start_year}-01-01T00:00:00'",
            "$order": "report_date_as_yyyy_mm_dd ASC"})
        payload = response.json()
        if len(payload) >= 10000:
            raise ValueError("CFTC 请求达到分页上限")
        timestamp = datetime.now(timezone.utc).isoformat()
        return parse_cftc(payload, report_type, response.url, timestamp), response.content, response.url

    def ice(self, year):
        url = f"https://www.ice.com/publicdocs/futures/COTHist{year}.csv"
        response = self._get(url)
        timestamp = datetime.now(timezone.utc).isoformat()
        return parse_ice(response.content, year, response.url, timestamp), response.content, response.url

    def czce(self, day):
        if day < date(2025, 11, 2):
            raise ValueError("首版仅支持郑商所当前 XLSX 格式（2025-11-02 起）")
        url = (f"https://www.czce.com.cn/cn/DFSStaticFiles/Future/{day.year}/"
               f"{day:%Y%m%d}/FutureDataHolding.xlsx")
        response = self._get(url)
        timestamp = datetime.now(timezone.utc).isoformat()
        return parse_czce(response.content, day.isoformat(), response.url, timestamp), response.content, response.url
