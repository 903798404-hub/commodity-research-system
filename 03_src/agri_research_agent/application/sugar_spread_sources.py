"""Fetch and validate dated exchange closes and Czapp settlement reports."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import re
from time import monotonic, sleep
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from .sugar_spreads import MONTHS, SugarQuote, contract_month


MONTH_NAMES = "January February March April May June July August September October November December".split()
REPORT_TABLES = {"Settlement (cUS$/lb)": ("SB", "ICE Futures U.S. Sugar No.11 Contract"),
                 "Settlement (US$/mt)": ("W", "ICE Europe White Sugar Futures Contract")}
SHANGHAI = ZoneInfo("Asia/Shanghai")
LONDON = ZoneInfo("Europe/London")


def report_url(day: date, market: str) -> str:
    if market == "domestic":
        return day.strftime("https://www.czce.com.cn/cn/DFSStaticFiles/Future/%Y/%Y%m%d/FutureDataDaily.xlsx")
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(day.day % 10, "th") if not 11 <= day.day <= 13 else "th"
    month = MONTH_NAMES[day.month - 1].lower()
    return ("https://www.czapp.com/analyst-insights/daily-market-price-updates-and-commentary-"
            f"{day.day}{suffix}-{month}-{day.year}/")


def fetch_report(url: str) -> bytes | None:
    deadline = monotonic() + 25
    with requests.get(url, timeout=(5, 12), stream=True, allow_redirects=False) as response:
        if response.status_code in {404, 410}:
            return None
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("行情请求返回了重定向或非完整响应")
        raw = bytearray()
        for chunk in response.iter_content(65536):
            raw.extend(chunk)
            if len(raw) > 2 * 1024**2 or monotonic() > deadline:
                raise ValueError("行情文件超过大小或读取时间上限")
    return bytes(raw)


def decimal_cell(value: object) -> Decimal:
    number = Decimal(str(value).replace(",", ""))
    if not number.is_finite():
        raise ValueError("行情数值无效")
    return number


def parse_czce_daily(raw: bytes, day: date, url: str) -> list[SugarQuote]:
    with ZipFile(BytesIO(raw)) as archive:
        if sum(item.file_size for item in archive.infolist()) > 12 * 1024**2:
            raise ValueError("郑商所报表解压超限")
    book = load_workbook(BytesIO(raw), read_only=True, data_only=True)
    quotes = {}
    try:
        rows = iter(book.active.values)
        if next(rows)[0] != f"郑州商品交易所期货每日行情表({day})":
            raise ValueError("郑商所报表交易日期不符")
        header = next(rows)
        code_column, close_column, volume_column = (header.index(name)
            for name in ("合约代码", "今收盘", "成交量(手)"))
        for row in rows:
            raw_code = str(row[code_column])
            if not re.fullmatch(r"SR\d{3}", raw_code):
                continue
            volume = decimal_cell(row[volume_column] or 0)
            if volume < 0:
                raise ValueError("郑糖成交量无效")
            if volume == 0:
                continue
            year = day.year - day.year % 10 + int(raw_code[2])
            if year < day.year:
                year += 10
            code = f"SR{year}{raw_code[-2:]}"
            close = decimal_cell(row[close_column])
            if (year > day.year + 2 or contract_month(code) < day.replace(day=1)
                    or code in quotes or close <= 0 or close != close.to_integral_value()):
                raise ValueError(f"郑糖合约或收盘价无效。{code}")
            quotes[code] = SugarQuote(day, code, "close", close, "郑商所", url)
    finally:
        book.close()
    if len(quotes) < 2:
        raise ValueError("郑商所报表有效郑糖收盘价不足两份")
    return list(quotes.values())


def parse_czapp_settlements(raw: bytes, day: date, url: str) -> list[SugarQuote]:
    html = raw.decode("utf-8", errors="strict")
    page = BeautifulSoup(html, "html.parser")
    title = page.find("h1")
    expected_title = url.rstrip("/").rsplit("/", 1)[1].replace("-", " ")
    stamp = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html)
    if title is None or title.get_text(" ", strip=True).lower() != expected_title or stamp is None:
        raise ValueError("外盘日报标题或发布时间不符")
    published = datetime.fromisoformat(stamp[1])
    if published.utcoffset() is None:
        raise ValueError("外盘日报缺少发布时区")
    london = published.astimezone(LONDON)
    if london.date() != day or london.time() < time(18):
        raise ValueError("外盘日报发布日或收盘时段不符")

    valid_tables, quotes = set(), {}
    for table in page.find_all("table"):
        rows = table.find_all("tr")
        header = rows[0].find(["td", "th"]) if rows else None
        info = REPORT_TABLES.get(header.get_text(" ", strip=True)) if header else None
        if info is None:
            continue
        market, market_heading = info
        if market in valid_tables or market_heading not in page.get_text(" ", strip=True):
            raise ValueError("外盘结算表市场身份不明确")
        valid_tables.add(market)
        for row in rows[1:]:
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) < 2:
                continue
            name = cells[0].get_text(" ", strip=True)
            match = re.fullmatch(r"([A-Z][a-z]{2})(\d{2})", name)
            if match is None:
                continue
            month = next((i for i, value in enumerate(MONTH_NAMES, 1) if value[:3] == match[1]), None)
            if month not in MONTHS[market]:
                raise ValueError(f"外盘合约交割月无效。{name}")
            code = f"{market}20{match[2]}{month:02d}"
            if contract_month(code) <= day:
                continue
            close = decimal_cell(cells[1].get_text(" ", strip=True))
            if close <= 0 or code in quotes:
                raise ValueError(f"外盘结算价无效或重复。{code}")
            quotes[code] = SugarQuote(day, code, "settlement", close, "Czapp", url)
    raw_codes = {code[2:] for code in quotes if code.startswith("SB")}
    white_codes = {code[1:] for code in quotes if code.startswith("W")}
    if valid_tables != {"SB", "W"} or len(raw_codes & white_codes) < 2:
        raise ValueError("外盘日报缺少可配对的纽约和伦敦结算价")
    return list(quotes.values())


def collect_reports(days: list[date], market: str, known: list[SugarQuote], *, fetcher=None) -> tuple[list[list[SugarQuote]], list[dict], dict]:
    fetcher = fetcher or fetch_report
    coverage: dict[date, set[str]] = {}
    source = ("郑商所", "close") if market == "domestic" else ("Czapp", "settlement")
    for quote in known:
        if (quote.provider, quote.price_type) == source:
            coverage.setdefault(quote.trade_date, set()).add(quote.contract)
    if market == "domestic":
        covered_days = {day for day, codes in coverage.items() if sum(c.startswith("SR") for c in codes) >= 2}
    else:
        covered_days = {day for day, codes in coverage.items() if
                        sum(c.startswith("SB") and "W" + c[2:] in codes for c in codes) >= 2}
    batches, receipts, originals = [], [], {}
    for i, day in enumerate(day for day in days if day not in covered_days):
        if market == "foreign" and i:
            sleep(3)
        url = report_url(day, market)
        record = dict(market=market, date=day.isoformat(), url=url, status="not_published")
        try:
            raw = fetcher(url)
            if raw is not None:
                parse = parse_czce_daily if market == "domestic" else parse_czapp_settlements
                quotes = parse(raw, day, url)
                name = f"{market}-{day}.{'xlsx' if market == 'domestic' else 'html'}"
                originals[name] = raw
                record.update(status="verified", file=name, sha256=sha256(raw).hexdigest(),
                              rows=len(quotes))
                batches.append(quotes)
        except (requests.RequestException, ArithmeticError, BadZipFile, InvalidFileException,
                ValueError, OSError, StopIteration) as exc:
            record.update(status="failed", reason=f"{type(exc).__name__}: {str(exc)[:160]}")
        receipts.append(record)
    return batches, receipts, originals


def preview_dates(end: date, lookback: int) -> list[date]:
    return [day for i in reversed(range(lookback)) if (day := end - timedelta(days=i)).weekday() < 5]
