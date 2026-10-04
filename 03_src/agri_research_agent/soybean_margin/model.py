"""Pure net-margin calculations and read-only access to existing source snapshots."""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import re

import pandas as pd

ORIGINS = {"brazil": "巴西", "us_gulf": "美湾", "us_pnw": "美西", "argentina": "阿根廷"}
PARAMETERS = {"meal_yield": .795, "oil_yield": .19, "tariff": .03, "vat": .09,
              "port_fee": 50., "processing_fee": 150., "conversion": .367437}
CBOT_MONTHS = {1:1, 2:3, 3:3, 4:5, 5:5, 6:7, 7:7, 8:9, 9:9, 10:11, 11:11, 12:1}
DCE_MONTHS = {m: 5 if m <= 8 else 1 for m in range(1, 13)}
KEY = ["business_date", "origin", "shipment_year", "shipment_month"]
FIELDS = ["cnf_cents_per_bushel", "cbot_price_cents_per_bushel", "fx_value",
          "soymeal_price_cny_per_tonne", "soyoil_price_cny_per_tonne"]


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def shipment_year(business_date: date, month: int) -> int:
    if type(month) is not int or not 1 <= month <= 12:
        raise ValueError("船期月份必须为1至12")
    return business_date.year + (month <= business_date.month)


def contracts(business_date: date, month: int) -> tuple[str, str, int]:
    year = shipment_year(business_date, month)
    cbot_year = year + (month == 12)
    domestic_year = year + (month >= 9)
    return (f"{cbot_year % 100:02}{CBOT_MONTHS[month]:02}",
            f"{domestic_year % 100:02}{DCE_MONTHS[month]:02}", year)


def calculate(cnf, cbot, fx, meal, oil):
    """Missing market inputs remain missing; zero CNF is a valid observation."""
    cnf, cbot, fx, meal, oil = map(number, (cnf, cbot, fx, meal, oil))
    cbot = cbot if cbot is not None and cbot > 0 else None
    fx = fx if fx is not None and fx > 0 else None
    meal = meal if meal is not None and meal > 0 else None
    oil = oil if oil is not None and oil > 0 else None
    usd = (cnf + cbot) * PARAMETERS["conversion"] if cnf is not None and cbot is not None else None
    cost = usd * fx * 1.03 * 1.09 if usd is not None and fx is not None else None
    profit = (meal * .795 + oil * .19 - cost - 50 - 150
              if cost is not None and meal is not None and oil is not None else None)
    return {"usd_cost": usd, "duty_paid_cost": cost, "net_margin": profit}


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def resolve_history(root: Path) -> tuple[Path, str]:
    """Resolve and authenticate a retained historical input; never write it."""
    root = root.resolve(strict=True)
    index = json.loads((root / "release_index.json").read_text(encoding="utf-8"))
    release = index["current_release_id"]
    if not isinstance(release, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", release):
        raise ValueError("历史数据版本标识无效")
    directory = (root / "releases" / release).resolve(strict=True)
    if not directory.is_relative_to(root / "releases"):
        raise ValueError("历史数据路径越界")
    manifest_path = directory / "manifest.json"
    if digest(manifest_path).lower() != index["current_manifest_sha256"].lower():
        raise ValueError("历史数据清单校验失败")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["release_id"] != release:
        raise ValueError("历史数据版本不一致")
    path = directory / "soybean_market_snapshots.parquet"
    identity = manifest["output_files"][path.name]
    if path.stat().st_size != identity["size_bytes"] or digest(path).lower() != identity["sha256"].lower():
        raise ValueError("历史行情文件校验失败")
    return path, identity["sha256"]


def read_history(path: Path) -> pd.DataFrame:
    data = pd.read_parquet(path)
    required = KEY + FIELDS + ["commodity", "cbot_contract_year", "cbot_contract_month",
                               "soymeal_contract_code", "soyoil_contract_code"]
    if not set(required).issubset(data.columns):
        raise ValueError("历史行情字段不完整")
    data["business_date"] = pd.to_datetime(data.business_date).dt.date
    if data[KEY].isna().any().any() or data.duplicated(KEY).any():
        raise ValueError("历史行情业务键缺失或重复")
    if not data.commodity.eq("soybean").all() or not data.origin.isin(ORIGINS).all():
        raise ValueError("历史行情品种或产地不符")
    if not data.shipment_month.isin(range(1, 13)).all():
        raise ValueError("历史行情船期无效")
    return data


def read_chart_history(path: Path) -> pd.DataFrame:
    """Keep the old published profit observations and their original contract labels."""
    data = read_history(path)
    manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
    result = path.parent / "soybean_net_crush_results.parquet"
    identity = manifest["output_files"][result.name]
    if result.stat().st_size != identity["size_bytes"] or digest(result).lower() != identity["sha256"].lower():
        raise ValueError("历史榨利图数据校验失败")
    frame = pd.read_parquet(result)
    frame["business_date"] = pd.to_datetime(frame.business_date).dt.date
    if frame[KEY].isna().any().any() or frame.duplicated(KEY).any():
        raise ValueError("历史榨利图业务键缺失或重复")
    merged = data.merge(frame[KEY + ["net_crush_margin_cny_per_tonne"]].rename(
        columns={"net_crush_margin_cny_per_tonne": "retained_net_margin"}),
        how="outer",on=KEY,validate="one_to_one",indicator=True)
    if not merged._merge.eq("both").all():
        raise ValueError("历史榨利图与行情业务键不一致")
    return merged.drop(columns="_merge")


def daily_rows(data: pd.DataFrame, day: date, origin: str, overrides=None) -> list[dict]:
    if origin not in ORIGINS:
        raise ValueError("未知产地")
    selected = data[(data.business_date == day) & (data.origin == origin)]
    indexed = {int(r["shipment_month"]): r for r in selected.to_dict("records")}
    result = []
    for month in range(1, 13):
        cbot, domestic, year = contracts(day, month)
        row = {"business_date": day, "origin": origin, "shipment_year": year,
               "shipment_month": month, "shipment_period": f"{year}-{month:02}",
               "cbot_contract": cbot, "domestic_contract": domestic}
        source = indexed.get(month, {})
        if source and int(source["shipment_year"]) != year:
            raise ValueError("历史数据船期年份与业务日期不一致")
        for name in FIELDS:
            row[name] = number(source.get(name))
        if source and number(source.get("cbot_contract_year")) is not None and number(source.get("cbot_contract_month")) is not None:
            row["cbot_contract"] = f"{int(source['cbot_contract_year'])%100:02}{int(source['cbot_contract_month']):02}"
        if source and isinstance(source.get("soymeal_contract_code"), str):
            row["domestic_contract"] = source["soymeal_contract_code"].lstrip("Mm")
        if overrides is not None and month in overrides:
            row["cnf_cents_per_bushel"] = number(overrides[month])
        row.update(calculate(*(row[name] for name in FIELDS)))
        result.append(row)
    return result


def overlay_cnf(data: pd.DataFrame, quotes: list[dict], *, recalculate_retained=False):
    """Saved CNF overrides its exact key only, including explicit zero and NULL."""
    if not quotes:
        return data
    manual = pd.DataFrame(quotes)
    manual["business_date"] = pd.to_datetime(manual.business_date).dt.date
    if manual[KEY].isna().any().any() or manual.duplicated(KEY).any():
        raise ValueError("已保存CNF业务键缺失或重复")
    if not manual.origin.isin(ORIGINS).all() or not manual.shipment_month.isin(range(1,13)).all():
        raise ValueError("已保存CNF产地或月份无效")
    merged = data.merge(manual[KEY + [FIELDS[0]]].rename(columns={FIELDS[0]: "saved_cnf"}),
        how="outer", on=KEY, indicator=True, validate="one_to_one")
    changed = merged._merge.ne("left_only")
    merged[FIELDS[0]] = pd.to_numeric(merged[FIELDS[0]], errors="raise").where(
        ~changed, pd.to_numeric(merged["saved_cnf"], errors="raise"))
    if recalculate_retained:
        for index, row in merged.loc[changed].iterrows():
            merged.loc[index, "retained_net_margin"] = calculate(*(row.get(name) for name in FIELDS))["net_margin"]
            merged.loc[index, "chart_history_source"] = "manual_cnf"
    return merged.drop(columns=["saved_cnf", "_merge"])


def history_matrix(data: pd.DataFrame, day: date, origin: str, metric: str, count: int = 12):
    dates = []
    cursor = day
    while len(dates) < count:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor -= timedelta(days=1)
    records = []
    for date_value in dates:
        record = {"日期": date_value.isoformat()}
        record.update({f"{r['shipment_month']}月": r[metric]
                       for r in daily_rows(data, date_value, origin)})
        records.append(record)
    return pd.DataFrame(records)


def seasonal_series(data: pd.DataFrame, origin: str, month: int, metric: str, as_of: date):
    target_year = shipment_year(as_of, month)
    series = {}
    for year in range(target_year - 5, target_year + 1):
        arrival = date(year, month, 1)
        start_month = year * 12 + month - 1 - 4
        start = date(start_month // 12, start_month % 12 + 1, 1)
        subset = data[(data.origin == origin) & (data.shipment_month == month)
                      & (data.shipment_year == year) & (data.business_date >= start)
                      & (data.business_date < arrival) & (data.business_date <= as_of)]
        points = {}
        for row in subset.to_dict("records"):
            value = (number(row["cnf_cents_per_bushel"]) if metric == "cnf_cents_per_bushel"
                     else calculate(*(row[name] for name in FIELDS))[metric])
            points[row["business_date"].strftime("%m-%d")] = value
        series[str(year)] = points
    labels = []
    start_month = target_year * 12 + month - 1 - 4
    cursor = date(start_month // 12, start_month % 12 + 1, 1)
    while cursor < date(target_year, month, 1):
        label = cursor.strftime("%m-%d")
        if label != "02-29":
            labels.append(label)
        cursor += timedelta(days=1)
    means = {}
    for label in labels:
        values = [series[str(year)].get(label) for year in range(target_year - 5, target_year)]
        values = [v for v in values if v is not None]
        means[label] = sum(values) / len(values) if len(values) >= 3 else None
    series["5年均值"] = means
    return labels, series
