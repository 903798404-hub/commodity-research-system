"""Position semantics: report grain, observed differences, and disclosure gaps."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

GROUPS = {
    "managed_money": "管理基金",
    "producer": "产业商",
    "swap": "掉期商",
    "other": "其他报告者",
    "nonreportable": "非报告者",
}
MARKETS = {"sugar11": "糖11（原糖）", "white_sugar": "伦敦白砂糖"}


def positioning_signal(net, change):
    """Describe observed positioning, without inferring cash flows or intent."""
    if net is None:
        return "披露不足，情绪无法判断"
    direction = "偏多" if net > 0 else "偏空" if net < 0 else "多空平衡"
    if change is None:
        return f"{direction} · 变化不可比"
    if change == 0:
        return f"{direction} · 净持仓不变"
    if net == 0:
        return f"{direction} · {'向多' if change > 0 else '向空'}变化"
    stronger = (net > 0 and change > 0) or (net < 0 and change < 0)
    return f"{direction} · {'增强' if stronger else '减弱'}"


def integer(value, *, signed=False, optional=False):
    if value is None or str(value).strip() in ("", "-", "—"):
        if optional:
            return None
        raise ValueError("持仓字段缺失")
    try:
        number = Decimal(str(value).replace(",", "").strip())
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError("持仓必须是有限整数")
        result = int(number)
    except InvalidOperation as exc:
        raise ValueError("持仓字段不是数字") from exc
    if not signed and result < 0:
        raise ValueError("持仓不能为负数")
    return result


def load_members(path: Path):
    config = json.loads(path.read_text(encoding="utf-8"))
    members = config["members"]
    if config.get("schema_version") != 1 or len(members) != 5:
        raise ValueError("必须配置五家固定席位")
    ids = [m["id"] for m in members]
    aliases = [a for m in members for a in m["aliases"]]
    if len(set(ids)) != 5 or len(set(aliases)) != len(aliases):
        raise ValueError("席位或名称映射重复")
    return members


def split_member(raw: str):
    text = re.sub(r"\s+", "", str(raw))
    match = re.search(r"[（(](代客|自营)[）)]$", text)
    return (text[:match.start()], match.group(1)) if match else (text, "未区分")


def unique_rows(rows, fields):
    seen = set()
    for row in rows:
        key = tuple(row[field] for field in fields)
        if key in seen:
            raise ValueError(f"重复业务键: {key}")
        seen.add(key)


def changes(rows, key_fields):
    """Compare adjacent stored observations; preserve gaps and comparison date."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[tuple(row[k] for k in key_fields)].append(dict(row))
    result = []
    for group in grouped.values():
        previous = None
        for row in sorted(group, key=lambda x: x["report_date"]):
            row["net"] = (row["long"] - row["short"]
                          if row["long"] is not None and row["short"] is not None else None)
            row["previous_date"] = previous["report_date"] if previous else None
            row["comparison_days"] = ((date.fromisoformat(row["report_date"])
                - date.fromisoformat(previous["report_date"])).days if previous else None)
            row["net_change"] = None
            if (previous and row["net"] is not None and previous["net"] is not None
                    and row["comparison_days"] <= 10):
                row["net_change"] = row["net"] - previous["net"]
            result.append(row)
            previous = row
    return sorted(result, key=lambda x: x["report_date"])


def foreign_metrics(rows):
    return changes(rows, ("market", "report_type", "group"))


def domestic_metrics(rows, members, *, account="代客"):
    """Top ranks are separate groups; fixed seats require both disclosed sides."""
    sections = defaultdict(list)
    for row in rows:
        sections[(row["report_date"], row["scope"])].append(row)
    metrics = []
    for (day, scope), section in sorted(sections.items()):
        for n in (20, 5):
            sides = {side: [r for r in section if r["side"] == side and r["rank"] <= n]
                     for side in ("long", "short")}
            # Parsers have checked section totals and contiguous disclosed ranks.
            metrics.append(dict(report_date=day, scope=scope, group=f"top{n}",
                label=f"前{n}名多空差", account="交易所排名全部席位",
                long=sum(r["positions"] for r in sides["long"]),
                short=sum(r["positions"] for r in sides["short"]), coverage="排名披露完整"))
        fixed = []
        for member in members:
            selected = [r for r in section if r["member"] in member["aliases"]
                        and r["account"] == account]
            sides = {side: [r for r in selected if r["side"] == side]
                     for side in ("long", "short")}
            if any(len(value) > 1 for value in sides.values()):
                raise ValueError(f"{member['label']}同日存在多条名称映射，请核对会员身份")
            values = {side: sides[side][0]["positions"] if sides[side] else None
                      for side in sides}
            missing = ["多仓" if s == "long" else "空仓" for s, v in values.items() if v is None]
            item = dict(report_date=day, scope=scope, group=member["id"],
                label=member["label"], account=account, **values,
                long_raw_name=sides["long"][0]["raw_member"] if sides["long"] else None,
                short_raw_name=sides["short"][0]["raw_member"] if sides["short"] else None,
                coverage="缺少" + "、".join(missing) if missing else "两侧已披露")
            fixed.append(item)
            metrics.append(item)
        complete = sum(r["long"] is not None and r["short"] is not None for r in fixed)
        metrics.append(dict(report_date=day, scope=scope, group="fixed5", label="五家合计",
            account=account, long=sum(r["long"] for r in fixed) if complete == 5 else None,
            short=sum(r["short"] for r in fixed) if complete == 5 else None,
            coverage=f"{complete}/5 家两侧已披露"))
    return changes(metrics, ("scope", "group", "account"))
