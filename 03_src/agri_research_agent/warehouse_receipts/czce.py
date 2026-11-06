"""Direct CZCE sugar receipts, with immutable original reports and strict totals."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

import requests

from .model import FIELDS, ReceiptError, normalize_rows

SOURCE = "CZCE official FutureDataWhsheet.txt"
ENDPOINT = "https://www.czce.com.cn"
HEADER = ["仓库编号", "仓库简称", "年度", "等级", "品牌", "仓单数量", "当日增减", "有效预报", "升贴水"]


def report_url(day: dt.date) -> str:
    return f"{ENDPOINT}/cn/DFSStaticFiles/Future/{day:%Y}/{day:%Y%m%d}/FutureDataWhsheet.txt"


def parse_sugar_report(content: bytes, day: dt.date) -> list[dict]:
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise ReceiptError("郑商所仓单日报不是有效UTF-8原文") from exc
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if re.search(r"品种[：:]\s*白糖\s*SR\b", line)]
    if len(starts) != 1:
        raise ReceiptError("郑商所仓单日报白糖区块不唯一")
    first = starts[0]
    title = lines[first]
    if not re.search(r"单位[：:]\s*张\b", title) or not re.search(rf"日期[：:]\s*{day.isoformat()}\b", title):
        raise ReceiptError("郑商所白糖仓单日期或单位不符")
    stop = next((i for i in range(first + 1, len(lines)) if re.search(r"品种[：:]", lines[i])), len(lines))
    body = [line.split("|") for line in lines[first + 1:stop] if "|" in line]
    if not body or [v.strip() for v in body[0]] != HEADER:
        raise ReceiptError("郑商所白糖仓单表头不符")
    rows, group, totals = [], [], []
    warehouse_id = warehouse = None
    subtotal_seen = False

    def number(value):
        value = value.strip()
        if not value:
            return None
        if not re.fullmatch(r"-?\d+", value):
            raise ReceiptError("郑商所白糖仓单数值无效")
        return int(value)

    def check_summary(parts, members):
        if not members:
            raise ReceiptError("郑商所白糖仓单缺少明细")
        for index, name in [(5, "vol"), (6, "vol_chg")]:
            values = [row[name] for row in members]
            total = number(parts[index])
            if total is None or any(v is None for v in values) or sum(values) != total:
                raise ReceiptError("郑商所白糖仓单明细与小计/总计不一致")

    for raw in body[1:]:
        parts = [v.strip() for v in raw]
        if len(parts) != len(HEADER):
            raise ReceiptError("郑商所白糖仓单列数不符")
        if parts[0] == "小计":
            check_summary(parts, group)
            group = []
            warehouse_id = warehouse = None
            subtotal_seen = True
            continue
        if parts[0] == "总计":
            if group or totals or not subtotal_seen:
                raise ReceiptError("郑商所白糖仓单总计结构无效")
            check_summary(parts, rows)
            total = dict.fromkeys(FIELDS.split(","))
            total.update(trade_date=f"{day:%Y%m%d}", symbol="SR", fut_name="白糖", warehouse="总计",
                         vol=number(parts[5]), vol_chg=number(parts[6]), unit="张", exchange="CZCE")
            totals.append(total)
            continue
        if totals:
            raise ReceiptError("郑商所白糖仓单总计之后仍有数据")
        if parts[0] or parts[1]:
            if group or not re.fullmatch(r"\d{4}", parts[0]) or not parts[1]:
                raise ReceiptError("郑商所白糖仓库边界无效")
            warehouse_id, warehouse = parts[0], parts[1]
        if warehouse_id is None:
            raise ReceiptError("郑商所白糖仓库续行缺少首行")
        row = dict.fromkeys(FIELDS.split(","))
        row.update(trade_date=f"{day:%Y%m%d}", symbol="SR", fut_name="白糖", warehouse=warehouse,
                   wh_id=warehouse_id, year=parts[2] or None, grade=parts[3] or None,
                   brand=parts[4] or None, vol=number(parts[5]), vol_chg=number(parts[6]),
                   pd=number(parts[8]), unit="张", exchange="CZCE")
        if row["vol"] is None or row["vol"] < 0:
            raise ReceiptError("郑商所白糖仓单数量缺失或为负")
        rows.append(row)
        group.append(row)
    if len(totals) != 1 or group:
        raise ReceiptError("郑商所白糖仓单缺少完整总计")
    normalized = normalize_rows(rows + totals)
    if len(normalized) != len(rows) + 1:
        raise ReceiptError("郑商所白糖仓单存在重复明细")
    return rows + totals


def acquire_sugar_reports(days: list[dt.date], raw_root: Path, *, session=None) -> list[dict]:
    session = session or requests.Session()
    raw_root.mkdir(parents=True, exist_ok=True)
    records = []
    for day in days:
        url = report_url(day)
        original = raw_root / f"SR-{day:%Y%m%d}-FutureDataWhsheet.txt"
        receipt = original.with_suffix(".json")
        if original.exists() or receipt.exists():
            if not original.is_file() or not receipt.is_file():
                raise ReceiptError("郑商所原始仓单证据不完整")
            content = original.read_bytes()
            evidence = json.loads(receipt.read_text(encoding="utf-8", errors="strict"))
            if evidence["url"] != url or evidence["sha256"] != hashlib.sha256(content).hexdigest():
                raise ReceiptError("郑商所原始仓单身份不一致")
        else:
            try:
                with session.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": ENDPOINT + "/"},
                                 timeout=(10, 40), allow_redirects=False, stream=True) as response:
                    if response.status_code == 404:
                        records.append(dict(source=SOURCE, url=url, trade_date=day.isoformat(), status="NOT_PUBLISHED", rows=[]))
                        continue
                    if response.status_code != 200 or response.url != url:
                        raise ReceiptError("郑商所官方仓单请求失败")
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 4 * 1024**2:
                            raise ReceiptError("郑商所仓单日报超过大小限制")
                        chunks.append(chunk)
                    content = b"".join(chunks)
            except requests.RequestException as exc:
                raise ReceiptError("郑商所官方仓单网络请求失败") from exc
            # Keep exact original bytes even if validation fails, outside the snapshot.
            original.write_bytes(content)
            evidence = dict(source=SOURCE, url=url, trade_date=day.isoformat(),
                            captured_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                            sha256=hashlib.sha256(content).hexdigest(), size=len(content))
            receipt.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        rows = parse_sugar_report(content, day)
        records.append({**evidence, "status": "VALIDATED", "rows": rows})
    return records
