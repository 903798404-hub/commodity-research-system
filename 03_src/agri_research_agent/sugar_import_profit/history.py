"""Read a verified, separately delivered Steel history; never recalculate it."""
from __future__ import annotations
from datetime import date
import hashlib
import json
import math
from pathlib import Path

SCHEMA = "sugar-import-history/1"


def validate_sources(sources):
    if set(sources) != {"巴西", "泰国", "日照"}:
        raise ValueError("白糖历史来源范围不符")
    for country, width in (("巴西", 8), ("泰国", 7), ("日照", 2)):
        item = sources[country]
        if not isinstance(item.get("filename"), str) or not item["filename"] or not item.get("rows"):
            raise ValueError("白糖历史来源信息缺失")
        previous = ""
        for row in item["rows"]:
            if not isinstance(row, list) or len(row) != width:
                raise ValueError("白糖历史字段不符")
            day = date.fromisoformat(row[0]).isoformat()
            if day <= previous:
                raise ValueError("白糖历史日期重复或未排序")
            previous = day
            if any(v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))
                                     or not math.isfinite(v)) for v in row[1:]):
                raise ValueError("白糖历史数值无效")
    return sources


def load_sources(root: Path):
    root = Path(root)
    if root.resolve() != root.absolute():
        raise ValueError("白糖历史目录不能使用路径别名")
    manifest_path, path = root / "manifest.json", root / "steel_sources.json"
    if path.is_symlink() or manifest_path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("白糖历史文件身份无效")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw = path.read_bytes()
    if (manifest.get("schema_version") != SCHEMA or manifest.get("currency") != "CNY"
            or manifest.get("profit_unit") != "yuan/tonne"
            or manifest.get("source_sha256") != hashlib.sha256(raw).hexdigest()
            or manifest.get("source_bytes") != len(raw)):
        raise ValueError("白糖历史清单或文件校验失败")
    sources = validate_sources(json.loads(raw.decode("utf-8")))
    if manifest.get("row_counts") != {k: len(v["rows"]) for k, v in sources.items()}:
        raise ValueError("白糖历史行数不符")
    return sources, manifest


def history_rows(sources, start="", end=""):
    for value in (start, end):
        if value:
            date.fromisoformat(value)
    if start and end and start > end:
        raise ValueError("起始日期不能晚于截止日期")
    rows = []
    for country in ("巴西", "泰国"):
        for row in sources[country]["rows"]:
            if (start and row[0] < start) or (end and row[0] > end):
                continue
            rows.append(dict(date=row[0], country=country,
                             profit_in=row[5] if country == "巴西" else row[2],
                             profit_out=row[7] if country == "巴西" else row[4]))
    return sorted(rows, key=lambda row: (row["date"], row["country"]))


def defaults(sources, country, day):
    from .model import COUNTRIES
    if country not in COUNTRIES:
        raise ValueError("国家无效")
    candidates = [r for r in sources[country]["rows"] if r[0] <= day and
                  (r[2] if country == "巴西" else r[6]) is not None and
                  (r[3] if country == "巴西" else r[5]) is not None]
    prices = [r for r in sources["日照"]["rows"] if r[0] <= day and r[1] is not None]
    quote = candidates[-1] if candidates else None
    price = prices[-1] if prices else None
    return dict(date=day, ice=None, premium=(quote[2] if country == "巴西" else quote[6]) if quote else None,
                polarization=1.042, conversion=22.046,
                freight=(quote[3] if country == "巴西" else quote[5]) if quote else None,
                insurance_fee=None, fx=None, tariff_in=.15, tariff_out=.50, vat=.13, loss=.04,
                processing=400., other=0., domestic=price[1] if price else None,
                loss_mode="成本加成", contract="", ice_date="", premium_date=quote[0] if quote else "",
                freight_date=quote[0] if quote else "", fx_date="", domestic_date=price[0] if price else "",
                source_note=f"升贴水/运费：钢联 {quote[0] if quote else '缺失'}；日照当地贸易商报价平均值：钢联 {price[0] if price else '缺失'}。",
                example=False)
