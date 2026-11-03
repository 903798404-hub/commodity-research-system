"""Bounded official HTTP collectors and strict parsers (no vendor API keys)."""
from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from io import BytesIO, StringIO
import json
import hashlib
import re
from zipfile import BadZipFile, ZipFile

import openpyxl
import requests

from .model import integer, split_member, unique_rows, COFCO_MEMBER, COFCO_OFFICIAL_RECORD

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
    day = _day(lowered["report_date_as_yyyy_mm_dd"]) if market != "white_sugar" else (
        datetime.strptime(lowered["as_of_date_form_mm/dd/yyyy"], "%m/%d/%Y").date().isoformat())
    if date.fromisoformat(day) > datetime.now(timezone.utc).date():
        raise ValueError("持仓日期在未来")
    oi = integer(lowered["open_interest_all"])
    result = []
    for group, prefix in FIELDS.items():
        # Socrata API names differ from the official CSV names (including double underscores).
        names = CFTC_FIELDS[group] if market != "white_sugar" else (
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
        if market != "white_sugar":
            aggregate_key = "tot_rept_positions_long_all" if side == "long" else "tot_rept_positions_short"
            aggregate = integer(lowered[aggregate_key])
            nonreportable = next(r[side] for r in result if r["group"] == "nonreportable")
            if abs(aggregate + nonreportable - oi) > (1 if report_type == "combined" else 0):
                raise ValueError(f"{market} {day} {side} 官方报告者合计未与总持仓一致")
    return result


def parse_cftc(payload, report_type, source_url, retrieved_at, *,
               market="sugar11", expected_code="080732", expected_name="SUGAR NO. 11"):
    if report_type not in CFTC_DATASETS or not isinstance(payload, list) or not payload:
        raise ValueError("CFTC 报告为空或口径无效")
    rows = []
    for item in payload:
        if (item.get("cftc_contract_market_code") != expected_code
                or expected_name not in item.get("market_and_exchange_names", "")):
            raise ValueError("CFTC 返回了非糖11数据" if market == "sugar11" else "CFTC 返回了非目标品种数据")
        rows.extend(_cot_record(item, market, report_type, source_url, retrieved_at))
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


def parse_czce(content, expected_day, source_url, retrieved_at, *, varieties=("SR",)):
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
                match = re.search(r"([A-Z]+\d*)\s+日期：(\d{4}-\d{2}-\d{2})", title)
                scope = None
                header = False
                if match and re.sub(r"\d", "", match.group(1)) in varieties:
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
    if not any(variety in sections for variety in varieties):
        raise ValueError("郑商所文件缺少目标品种排名")
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


def parse_czce_cofco_text(content, expected_day, source_url, retrieved_at):
    """Normalize one SR product section through the existing ranking validator.

    The generated workbook is an internal adapter, never stored as source data.
    The complete original TXT bytes remain in the lossless source capture.
    """
    if len(content) > 8 * 1024 * 1024:
        raise ValueError('郑商所历史原文超过上限')
    text = content.decode('utf-8-sig', errors='strict')
    if f'郑州商品交易所期货持仓排名表({expected_day})' not in text:
        raise ValueError('郑商所历史文件标题或日期不符')
    section, active, count = [], False, 0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(('品种：', '合约：')):
            if active:
                raise ValueError('白糖历史品种排名缺少合计')
            match = re.fullmatch(r'品种：\s*白糖\s*SR\s+日期：(\d{4}-\d{2}-\d{2})', line)
            if match:
                if match[1] != expected_day:
                    raise ValueError('白糖历史品种日期不符')
                count += 1
                active = True
                section.append([line])
            continue
        if not active or not line:
            continue
        cells = [cell.strip() for cell in line.split('|')]
        if len(cells) != 10:
            raise ValueError('白糖历史排名列数无效')
        section.append(cells)
        if cells[0] == '合计':
            active = False
    if count != 1 or active or not 4 <= len(section) <= 23:
        raise ValueError('白糖历史品种区块缺失、重复或不完整')
    book = openpyxl.Workbook(write_only=True)
    sheet = book.create_sheet()
    for row in section:
        sheet.append(row)
    buffer = BytesIO()
    book.save(buffer)
    return parse_czce(buffer.getvalue(), expected_day, source_url, retrieved_at)


def parse_czce_cofco_capture(content, expected_day, source_url, retrieved_at):
    day = date.fromisoformat(expected_day)
    if day < date(2022, 1, 1) or day > date.today():
        raise ValueError('中粮官方历史日期超出范围')
    expected_url = day.strftime('https://www.czce.com.cn/cn/DFSStaticFiles/Future/%Y/%Y%m%d/FutureDataHolding.txt')
    if source_url != expected_url or len(content) > 4 * 1024 * 1024:
        raise ValueError('中粮官方历史来源不符')
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if (len(entries) != 2 or {e.filename for e in entries} != {'manifest.json', 'FutureDataHolding.txt'}
                    or sum(e.file_size for e in entries) > 8 * 1024 * 1024 + 4096
                    or any(e.is_dir() or e.flag_bits & 1 for e in entries)):
                raise ValueError('中粮官方历史封装无效')
            if archive.getinfo('manifest.json').file_size > 4096:
                raise ValueError('中粮官方历史清单超限')
            manifest = json.loads(archive.read('manifest.json').decode('utf-8', errors='strict'))
            raw = archive.read('FutureDataHolding.txt')
    except (BadZipFile, KeyError, UnicodeError) as exc:
        raise ValueError('中粮官方历史封装损坏') from exc
    required = {'schema_version', 'report_date', 'source_url', 'source_bytes', 'source_sha256'}
    if (not isinstance(manifest, dict) or set(manifest) != required
            or manifest['schema_version'] != 'czce-text-lossless/1'
            or manifest['report_date'] != expected_day or manifest['source_url'] != source_url
            or type(manifest['source_bytes']) is not int or manifest['source_bytes'] != len(raw)
            or manifest['source_sha256'] != hashlib.sha256(raw).hexdigest()):
        raise ValueError('中粮官方历史原文身份不符')
    ranking = parse_czce_cofco_text(raw, expected_day, source_url, retrieved_at)
    account = '代客' if any(r['account'] == '代客' for r in ranking) else '未区分'
    result = []
    for side in ('long', 'short'):
        selected = [r for r in ranking if r['side'] == side and r['account'] == account
                    and r['member'] in COFCO_MEMBER['aliases']]
        if len(selected) > 1:
            raise ValueError('官方中粮会员身份重复')
        row = (dict(selected[0]) if selected else dict(report_date=expected_day, scope='SR', side=side,
            rank=None, raw_member=None, member='中粮期货', account=account, positions=None,
            reported_change=None, unit='contracts', source_url=source_url, retrieved_at=retrieved_at))
        row.update(record_type=COFCO_OFFICIAL_RECORD, disclosure='observed' if selected else 'member_absent')
        result.append(row)
    return result
