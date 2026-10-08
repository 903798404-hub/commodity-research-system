from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
import re
from zipfile import BadZipFile, ZipFile

from bs4 import BeautifulSoup
import requests

from agri_research_agent.sugar_positions.model import integer, split_member, unique_rows
from agri_research_agent.sugar_positions.sources import CFTC_DATASETS, parse_cftc, parse_czce

EURO_GROUPS = ("investment_firms", "investment_funds", "other_financial", "commercial", "compliance")
EURO_HEADERS = ("Investment Firms or Credit Institutions", "Investment Funds",
    "Other Financial Institutions", "Commercial Undertakings",
    "Operators with compliance obligations under Directive 2003/87/EC")
DCE_URL = "http://www.dce.com.cn/dcereport/publicweb/dailystat/memberDealPosi/batchDownload"


def decimal_number(value):
    try:
        number = Decimal(str(value).replace(" ", "").replace(",", ""))
        if not number.is_finite() or number < 0 or number.as_tuple().exponent < -2:
            raise ValueError("持仓必须是非负、最多两位小数")
        return number
    except InvalidOperation as exc:
        raise ValueError("持仓不是有效数字") from exc


def _table_grid(table):
    grid = {}
    trs = table.find_all("tr")
    for index, tr in enumerate(trs):
        column = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while (index, column) in grid:
                column += 1
            width, height = int(cell.get("colspan", 1)), int(cell.get("rowspan", 1))
            if not (1 <= width <= 13 and 1 <= height <= 20 and column + width <= 13):
                raise ValueError("Euronext 表格布局发生变化")
            value = cell.get_text(" ", strip=True)
            for r in range(index, index + height):
                for c in range(column, column + width):
                    if (r, c) in grid:
                        raise ValueError("Euronext 表格单元重叠")
                    grid[r, c] = value
            column += width
    return [[grid.get((r, c), "") for c in range(13)] for r in range(len(trs))]


def parse_euronext(content, publication_day, source_url, stamp):
    soup = BeautifulSoup(content.decode("utf-8", errors="strict"), "html.parser")
    tables = soup.find_all("table")
    rows = []
    kinds = set()
    for table in tables:
        if table.find("table") is not None:
            continue
        table_rows = _table_grid(table)
        if len(table_rows) < 6:
            raise ValueError("Euronext 持仓表不完整")
        title = " ".join(table_rows[0])
        if "Contract code ECO" not in title or "Identifier XMAT" not in title:
            raise ValueError("Euronext 返回了非欧洲菜籽报告")
        report_match = re.search(r"Report date (\d{2}/\d{2}/\d{4})", title)
        published_match = re.search(r"Publication date (\d{2}/\d{2}/\d{4})", title)
        kind_match = re.search(r"Report type (FUTR|COMB)\b", title)
        if not report_match or not published_match or not kind_match:
            raise ValueError("Euronext 缺少日期或明确的期货/期权口径")
        day = datetime.strptime(report_match[1], "%d/%m/%Y").date()
        published = datetime.strptime(published_match[1], "%d/%m/%Y").date()
        if published != publication_day or day > published or published > datetime.now(timezone.utc).date():
            raise ValueError("Euronext 日期不一致")
        kind = {"FUTR": "futures_only", "COMB": "combined"}[kind_match[1]]
        if kind in kinds:
            raise ValueError("Euronext 重复报告口径")
        kinds.add(kind)
        if len(table_rows[1]) != 13 or tuple(table_rows[1][3::2]) != EURO_HEADERS:
            raise ValueError("Euronext 分类表头发生变化")
        if table_rows[2][3:] != ["Long", "Short"] * 5:
            raise ValueError("Euronext 多空列发生变化")
        totals = [row for row in table_rows if len(row) == 13
            and row[:3] == ["Number of positions", "LOTS", "Total"]]
        if len(totals) != 1:
            raise ValueError("Euronext 持仓合计不唯一")
        amounts = [decimal_number(value) for value in totals[0][3:]]
        if kind == "futures_only" and any(value != value.to_integral_value() for value in amounts):
            raise ValueError("Euronext 纯期货持仓不是整数")
        components = [row for row in table_rows if row[:2] == ["Number of positions", "LOTS"]
            and row[2] in ("Risk Reducing directly related to commercial activities", "Other")]
        if len(components) != 2:
            raise ValueError("Euronext 分类持仓明细缺失")
        parts = [[decimal_number(v) for v in row[3:]] for row in components]
        if any(abs(parts[0][i] + parts[1][i] - amounts[i]) > Decimal("0.01") for i in range(10)):
            raise ValueError("Euronext 分类持仓明细与合计不一致")
        # The disclosure is classified holdings, not an official total-OI series.
        # Keep total OI absent rather than treating the category sum as exchange OI.
        for index, group in enumerate(EURO_GROUPS):
            rows.append(dict(market="euronext_rapeseed", report_type=kind, group=group,
                report_date=day.isoformat(), publication_date=published.isoformat(),
                long=float(amounts[index*2]), short=float(amounts[index*2+1]), spreading=0,
                open_interest=None, unit="contracts" if kind == "futures_only" else "delta_equivalent_contracts",
                scope="all_expiries", source_url=source_url, retrieved_at=stamp))
    if not rows:
        raise ValueError("Euronext 没有欧洲菜籽持仓表")
    unique_rows(rows, ("market", "report_type", "group", "report_date"))
    return rows


def parse_dce(content, requested_day, varieties, source_url, stamp):
    if not content.startswith(b"PK"):
        raise ValueError("大商所未返回持仓ZIP")
    result = []
    try:
        archive = ZipFile(BytesIO(content))
    except BadZipFile as exc:
        raise ValueError("大商所持仓ZIP损坏") from exc
    with archive:
        if sum(info.file_size for info in archive.infolist()) > 30_000_000:
            raise ValueError("大商所文件超出解析上限")
        for filename in archive.namelist():
            match = re.search(r"(\d{8})_([a-z]+\d*)_.*\.txt$", filename, re.I)
            if not match or re.sub(r"\d", "", match[2]).upper() not in varieties:
                continue
            if match[1] != requested_day.strftime("%Y%m%d"):
                raise ValueError("大商所文件日期不一致")
            scope = match[2].upper()
            raw = archive.read(filename)
            try:
                text = raw.decode("utf-8-sig", errors="strict")
            except UnicodeDecodeError:
                text = raw.decode("gb18030", errors="strict")
            sides, active = {"long": [], "short": []}, None
            totals = {}
            for line in text.splitlines():
                values = [cell.strip() for cell in line.split("\t")]
                if values[0].startswith("名次"):
                    header = " ".join(values)
                    active = "long" if re.search("持买|买持仓", header) else "short" if re.search("持卖|卖持仓", header) else None
                    continue
                if active is None:
                    continue
                if values[0] in ("合计", "总计"):
                    if len(values) < 3:
                        raise ValueError("大商所缺少持仓合计")
                    totals[active] = integer(values[2])
                    active = None
                    continue
                if not values[0].isdigit():
                    continue
                if len(values) < 4:
                    raise ValueError("大商所排名行不完整")
                rank = integer(values[0])
                if not 1 <= rank <= 20:
                    raise ValueError("大商所排名超出前20名")
                member, account = split_member(values[1])
                sides[active].append(dict(report_date=requested_day.isoformat(), scope=scope, side=active,
                    rank=rank, member=member, raw_member=values[1], account=account,
                    positions=integer(values[2]), reported_change=integer(values[3], signed=True, optional=True),
                    unit="contracts", source_url=source_url, retrieved_at=stamp))
            for side, records in sides.items():
                if [r["rank"] for r in records] != list(range(1, len(records)+1)) or not records:
                    raise ValueError("大商所排名不连续或缺失")
                if totals.get(side) != sum(r["positions"] for r in records):
                    raise ValueError("大商所排名合计不一致")
                result.extend(records)
    if not result:
        raise ValueError("大商所没有目标品种的明确排名")
    unique_rows(result, ("report_date", "scope", "side", "member", "account"))
    return result


class Sources:
    def __init__(self, timeout=20):
        self.session = requests.Session()
        self.timeout = timeout

    def _response(self, method, url, **kwargs):
        response = self.session.request(method, url, timeout=self.timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; commodity-position-research/1.0)"}, **kwargs)
        response.raise_for_status()
        return response

    def cftc(self, market, spec, kind, start_year):
        url = f"https://publicreporting.cftc.gov/resource/{CFTC_DATASETS[kind]}.json"
        response = self._response("GET", url, params={"cftc_contract_market_code": spec["code"],
            "$limit": 10000, "$order": "report_date_as_yyyy_mm_dd ASC",
            "$where": f"report_date_as_yyyy_mm_dd >= '{start_year}-01-01T00:00:00'"})
        payload = response.json()
        if len(payload) >= 10000:
            raise ValueError("CFTC 达到查询上限，不能确认完整")
        return parse_cftc(payload, kind, response.url, datetime.now(timezone.utc).isoformat(),
            market=market, expected_code=spec["code"], expected_name=spec["name"]), response.content, response.url

    def euronext(self, published):
        url = f"https://live.euronext.com/sites/default/files/commodities_reporting/{published:%Y/%m/%d}/en/cdwpr_ECO_{published:%Y%m%d}.html"
        response = self._response("GET", url)
        return parse_euronext(response.content, published, url, datetime.now(timezone.utc).isoformat()), response.content, url

    def czce(self, day, varieties):
        url = f"https://www.czce.com.cn/cn/DFSStaticFiles/Future/{day.year}/{day:%Y%m%d}/FutureDataHolding.xlsx"
        response = self._response("GET", url)
        return parse_czce(response.content, day.isoformat(), url, datetime.now(timezone.utc).isoformat(),
            varieties=varieties), response.content, url

    def dce(self, day, varieties, seed_contract):
        # The public batch endpoint requires a valid seed contract. Never guess "all".
        if not re.fullmatch(r"[a-z]+\d{4}", seed_contract) or re.sub(r"\d", "", seed_contract).upper() not in varieties:
            raise ValueError("大商所下载需要明确的目标品种合约")
        response = self._response("POST", DCE_URL, json={"tradeDate": day.strftime("%Y%m%d"),
            "varietyId": re.sub(r"\d", "", seed_contract), "contractId": seed_contract,
            "tradeType": "1", "lang": "zh"})
        return parse_dce(response.content, day, varieties, DCE_URL, datetime.now(timezone.utc).isoformat()), response.content, DCE_URL
