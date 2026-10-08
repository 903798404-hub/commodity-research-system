"""Sum observed contract rankings, preserving the limits of disclosure."""
from collections import defaultdict
from datetime import date, datetime
import json
import re
from urllib.parse import urlparse

from agri_research_agent.sugar_positions.model import (
    changes, domestic_metrics as exchange_metrics, integer, split_member, unique_rows,
)

METHOD = "disclosed_contracts_v1"


def aggregate_contract_rows(rows, contracts):
    """Require every expected contract and side before publishing a summary."""
    contracts = sorted(contracts)
    if not contracts or len(set(contracts)) != len(contracts):
        raise ValueError("汇总合约清单为空或重复")
    if any(not re.fullmatch(r"[MYP]\d{4}", c) for c in contracts):
        raise ValueError("汇总合约范围无效")
    variety = contracts[0][0]
    if any(c[0] != variety for c in contracts):
        raise ValueError("不同品种不能相加")
    if {r["scope"] for r in rows} != set(contracts):
        raise ValueError("合约覆盖不完整，不能发布汇总")
    days = {r["report_date"] for r in rows}
    providers = {r["source_provider"] for r in rows}
    if len(days) != 1 or len(providers) != 1:
        raise ValueError("汇总不能混用日期或来源")
    unique_rows(rows, ("scope", "side", "member", "account"))
    for contract in contracts:
        for side in ("long", "short"):
            ranks = sorted(r["rank"] for r in rows if r["scope"] == contract and r["side"] == side)
            if not ranks or len(ranks) > 20 or ranks != list(range(1, len(ranks) + 1)):
                raise ValueError("合约多空排名缺失或不连续")
    groups = defaultdict(list)
    for row in rows:
        groups[(row["side"], row["member"], row["account"])].append(row)
    result = []
    for side in ("long", "short"):
        ranked = []
        for (direction, member, account), observations in groups.items():
            if direction != side:
                continue
            ranked.append(dict(report_date=next(iter(days)), scope=variety, side=side,
                member=member, account=account, raw_member=observations[0]["raw_member"],
                positions=sum(r["positions"] for r in observations), reported_change=None,
                unit="contracts", aggregation=METHOD, constituent_contracts=contracts,
                observed_contracts=sorted(r["scope"] for r in observations),
                source_provider=next(iter(providers)),
                source_url=observations[0]["source_url"],
                source_urls=sorted({r["source_url"] for r in observations}),
                retrieved_at=max(r["retrieved_at"] for r in observations)))
        ranked.sort(key=lambda r: (-r["positions"], r["member"], r["account"]))
        result.extend(dict(row, rank=i) for i, row in enumerate(ranked, 1))
    return result


def _em_rows(report):
    contract = report["contract"]
    if len(report["tables"]) != 2 or len(report["scope"]) != 1:
        raise ValueError("多空排名表或合约选择不完整")
    if not report["scope"][0].endswith(contract[1:]):
        raise ValueError("页面选中合约不一致")
    source = urlparse(report["source_url"])
    if source.scheme != "https" or source.hostname != "qhweb.eastmoney.com" or source.path != f"/lhb/dkcc/dce/{contract.lower()}":
        raise ValueError("原始来源不是该合约的多空持仓页")
    stamp = report["retrieved_at"]
    datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    day = date.fromisoformat(report["date"]).isoformat()
    result = []
    for side, title, table in zip(("long", "short"), ("多头龙虎榜", "空头龙虎榜"), report["tables"]):
        if not table["heading"].endswith(contract[1:] + title):
            raise ValueError("合约排名标题或多空方向不一致")
        records = []
        for row in table["rows"]:
            cells = row["cells"]
            if len(cells) != 4:
                raise ValueError("多空排名字段缺失")
            member, account = split_member(cells[1])
            if not member:
                raise ValueError("会员身份缺失")
            records.append(dict(report_date=day, scope=contract, side=side,
                rank=integer(cells[0]), member=member, raw_member=cells[1], account=account,
                positions=integer(cells[2]), reported_change=integer(cells[3], signed=True, optional=True),
                unit="contracts", source_provider="eastmoney", source_url=report["source_url"],
                retrieved_at=stamp))
        total = re.findall(r"本日合计\s+([\d,]+)\b", table["text"])
        if len(total) != 1 or integer(total[0]) != sum(r["positions"] for r in records):
            raise ValueError("合约排名合计不一致")
        result.extend(records)
    return result


def parse_browser_capture(content, varieties):
    """Validate visible browser captures; no login credentials or guessed HTTP calls."""
    if len(content) > 10_000_000:
        raise ValueError("浏览器采集文件过大")
    payload = json.loads(content.decode("utf-8", errors="strict"))
    if payload.get("schema_version") != 1 or payload.get("acquisition") != "browser_dom":
        raise ValueError("浏览器采集版本或来源类型无效")
    day = date.fromisoformat(payload["report_date"])
    if not payload["reports"] or len(payload["reports"]) > 60:
        raise ValueError("浏览器采集合约数量无效")
    groups = defaultdict(list)
    for report in payload["reports"]:
        if not re.fullmatch(r"[MYP]\d{4}", report["contract"]):
            raise ValueError("浏览器采集合约无效")
        if report["contract"][0] in varieties:
            groups[report["contract"][0]].append(report)
    if not groups:
        raise ValueError("采集文件没有目标品种")
    domestic, exclusions = [], []
    for variety, reports in groups.items():
        expected = reports[0]["contracts"]
        if (not expected or len(set(expected)) != len(expected)
                or any(not re.fullmatch(variety + r"\d{4}", c) for c in expected)
                or any(set(r["contracts"]) != set(expected) for r in reports)
                or sorted(r["contract"] for r in reports) != sorted(expected)):
            raise ValueError("未逐一读取完整合约清单")
        current = []
        for report in reports:
            rows = _em_rows(report)
            actual = date.fromisoformat(report["date"])
            if actual > day:
                raise ValueError("页面报告晚于请求日期")
            if actual < day:
                exclusions.append(dict(scope=report["contract"], report_date=report["date"],
                    reason="页面最近报告早于目标日期，未混入汇总"))
            else:
                current.extend(rows)
        active = sorted({r["scope"] for r in current})
        summaries = aggregate_contract_rows(current, active)
        for row in summaries:
            row["excluded_contracts"] = [r for r in exclusions if r["scope"][0] == variety]
        domestic.extend(current + summaries)
    return domestic, exclusions


def domestic_metrics(rows, members, *, account="代客"):
    """Compare like disclosures and suppress changes when contract coverage changes."""
    sections = defaultdict(list)
    for row in rows:
        sections[(row["report_date"], row["scope"])].append(row)
    metrics = []
    for section in sections.values():
        selected_account = account if any(r["account"] == account for r in section) else "未区分"
        result = exchange_metrics(section, members, account=selected_account)
        methods = {r.get("aggregation", "exchange_rankings") for r in section}
        if len(methods) != 1:
            raise ValueError("同一范围混用汇总口径")
        method = next(iter(methods))
        for item in result:
            item["aggregation"] = method
            item["source_provider"] = section[0].get("source_provider", "exchange")
            if method == METHOD:
                item["constituent_contracts"] = section[0]["constituent_contracts"]
                item["excluded_contracts"] = section[0].get("excluded_contracts", [])
                item["coverage"] = "已披露持仓汇总 · " + item["coverage"]
                fixed = next((m for m in members if m["id"] == item["group"]), None)
                if fixed:
                    counts = {side: sum(len(r["observed_contracts"]) for r in section
                        if r["side"] == side and r["member"] in fixed["aliases"] and r["account"] == selected_account)
                        for side in ("long", "short")}
                    item["coverage"] = f"多仓 {counts['long']}/{len(item['constituent_contracts'])} · 空仓 {counts['short']}/{len(item['constituent_contracts'])} 合约已披露"
            metrics.append(item)
    result = changes(metrics, ("scope", "group", "account", "aggregation", "source_provider"))
    previous = {}
    for item in result:
        key = tuple(item[k] for k in ("scope", "group", "account", "aggregation", "source_provider"))
        old = previous.get(key)
        if old and item.get("constituent_contracts") != old.get("constituent_contracts"):
            item["net_change"] = None
            item["coverage"] += " · 合约范围变化，暂不比较"
        previous[key] = item
    return result
