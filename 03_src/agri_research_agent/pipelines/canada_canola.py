"""Canadian canola observations, workbook provenance and seasonal comparisons.

One external JSON bundle is the serving unit. No national weighting, provider
requests or file writes happen when a page builds a comparison.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import pandas as pd

SCHEMA = "canada-canola/1"
STABLE_RELATIVE_PATH = Path("processed/canada_canola/canola_weekly.json")
PROVINCES = {"SK": "萨省（55%）", "AB": "阿尔伯塔省（28%）", "MB": "曼省（16%）"}
METRICS = {"PLANTED": "播种进度", "HARVESTED": "收割进度", "GOOD_EXCELLENT": "优良率"}
STAGES = {
    "PRE_EMERGING": "萌发前", "SEEDLING": "幼苗期", "ROSETTE": "莲座期",
    "BOLTING": "抽薹期", "FLOWERING": "开花期", "PODDED": "结荚期", "RIPE": "成熟期",
}
SOURCE_URLS = {
    "SK": "https://www.saskatchewan.ca/business/agriculture-natural-resources-and-industry/agribusiness-farmers-and-ranchers/market-and-trade-statistics/crops-statistics/crop-report",
    "AB": "https://open.alberta.ca/dataset/2830245",
    "MB": "https://www.gov.mb.ca/agriculture/crops/seasonal-reports/crop-report/index.html",
}
SOURCE_HOSTS = {"SK": {"www.saskatchewan.ca", "saskatchewan.ca", "dashboard.saskatchewan.ca"},
                "AB": {"open.alberta.ca", "www.alberta.ca", "alberta.ca"},
                "MB": {"www.gov.mb.ca", "gov.mb.ca"}}
RECORD_FIELDS = {"province", "metric", "date", "value", "source_url", "source_sha256",
                 "source_locator", "date_basis", "published_at", "retrieved_at", "status"}
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def strict_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def check_source_url(province: str, url: str) -> None:
    parsed = urlparse(url)
    if (province not in PROVINCES or parsed.scheme != "https"
            or parsed.hostname not in SOURCE_HOSTS[province]
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("source must be an HTTPS official provincial URL")


def validate_bundle(bundle: dict, *, today: date | None = None) -> dict:
    today = today or date.today()
    if not isinstance(bundle, dict) or set(bundle) != {"schema_version", "generated_at", "records", "import_notes"}:
        raise ValueError("invalid canola bundle fields")
    if bundle["schema_version"] != SCHEMA:
        raise ValueError("unsupported canola schema")
    if not isinstance(bundle["generated_at"], str):
        raise ValueError("generated_at must be an ISO timestamp")
    timestamp = datetime.fromisoformat(bundle["generated_at"])
    if timestamp.utcoffset() is None:
        raise ValueError("generated_at must include a timezone")
    if not isinstance(bundle["records"], list) or not bundle["records"]:
        raise ValueError("canola bundle has no observations")
    if not isinstance(bundle["import_notes"], list) or not all(isinstance(x, str) for x in bundle["import_notes"]):
        raise ValueError("import_notes must be strings")
    keys = set()
    for item in bundle["records"]:
        if not isinstance(item, dict) or set(item) != RECORD_FIELDS:
            raise ValueError("invalid observation fields")
        if any(not isinstance(item[key], str) for key in RECORD_FIELDS - {"value", "published_at"}):
            raise ValueError("observation identity and date fields must be strings")
        if item["published_at"] is not None and not isinstance(item["published_at"], str):
            raise ValueError("publication date must be an ISO date or null")
        check_source_url(item["province"], item["source_url"])
        if item["metric"] not in METRICS | STAGES:
            raise ValueError("unknown metric")
        observed = date.fromisoformat(item["date"])
        if item["date"] != observed.isoformat() or observed.year < 2000 or observed > today:
            raise ValueError("invalid or future observation date")
        value = item["value"]
        if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("observation must be a finite percentage between 0 and 100")
        if item["date_basis"] not in {"workbook_date", "report_cutoff"}:
            raise ValueError("unknown observation date basis")
        if item["published_at"] is not None:
            published = date.fromisoformat(item["published_at"])
            if item["published_at"] != published.isoformat() or published < observed or published > today:
                raise ValueError("publication date precedes cutoff or is in the future")
        if item["date_basis"] == "report_cutoff" and item["published_at"] is None:
            raise ValueError("official report requires its publication date")
        if not re.fullmatch(r"[0-9a-f]{64}", item["source_sha256"]):
            raise ValueError("invalid source SHA-256")
        if not isinstance(item["source_locator"], str) or not item["source_locator"].strip():
            raise ValueError("missing source cell/page/table locator")
        if datetime.fromisoformat(item["retrieved_at"]).utcoffset() is None:
            raise ValueError("retrieved_at must include a timezone")
        if item["status"] not in {"reported", "season_complete"}:
            raise ValueError("unknown report status")
        key = (item["province"], item["metric"], item["date"])
        if key in keys:
            raise ValueError(f"duplicate observation: {key}")
        keys.add(key)
    return bundle


def load_bundle(path: Path) -> dict:
    return validate_bundle(strict_json(path))


def _sheet_cells(archive: zipfile.ZipFile, sheet_name: str) -> dict:
    strings = ["".join(node.itertext()) for node in
               ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("s:si", NS)]
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {node.attrib["Id"]: node.attrib["Target"] for node in rels}
    sheet = next((node for node in workbook.findall("s:sheets/s:sheet", NS)
                  if node.attrib["name"] == sheet_name), None)
    if sheet is None:
        raise ValueError(f"missing source sheet: {sheet_name}")
    target = targets[sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]]
    member = target.lstrip("/") if target.startswith("/") else "xl/" + target
    root = ET.fromstring(archive.read(member))
    cells = {}
    for cell in root.findall("s:sheetData/s:row/s:c", NS):
        value = cell.find("s:v", NS)
        if value is None or value.text is None:
            continue
        if cell.attrib.get("t") == "s":
            parsed = strings[int(value.text)]
        elif cell.attrib.get("t") in {"e", "str"}:
            parsed = value.text
        else:
            parsed = float(value.text)
        cells[cell.attrib["r"]] = parsed
    return cells


def import_workbook(path: Path) -> dict:
    """Read only raw provincial columns, never pivot totals or cumulative stages."""
    identity, retrieved = sha256_file(path), utc_now()
    records, notes = [], []
    specs = {
        "SK": ("萨省（产量55%）", [("C", {"J": "PLANTED"}, 100),
              ("CU", {"CV": "GOOD_EXCELLENT"}, 100),
              ("ID", {"IK": "HARVESTED"}, 100),
              ("BR", dict(zip(("BS", "BT", "BU", "BV", "BW", "BX", "BY"), STAGES)), 100)]),
        "AB": ("阿尔伯塔（产量28%）", [("E", {"K": "PLANTED"}, 100),
              ("AB", {"AH": "GOOD_EXCELLENT"}, 100), ("AS", {"AY": "HARVESTED"}, 100)]),
        "MB": ("曼省（产量16%）", [("C", {"D": "PLANTED"}, 100),
              ("N", {"T": "GOOD_EXCELLENT"}, 1), ("V", {"W": "HARVESTED"}, 100)]),
    }
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        properties = workbook.find("s:workbookPr", NS)
        if properties is not None and properties.attrib.get("date1904") in {"1", "true"}:
            raise ValueError("this importer requires the workbook's 1900 date system")
        for province, (sheet_name, blocks) in specs.items():
            cells = _sheet_cells(archive, sheet_name)
            required_headers = {"SK": {"C2": "日期", "J2": "Provincial", "CU2": "日期",
                                         "CV2": "Provincial", "BR3": "日期", "ID2": "Provincial", "IK2": "收割率"},
                                "AB": {"E1": "日期", "K1": "Alberta", "AB2": "日期", "AH2": "Alberta",
                                         "AS2": "日期", "AY2": "Alberta"},
                                "MB": {"C1": "日期", "D1": "曼省", "N2": "日期", "T2": "Provincial",
                                         "V1": "日期", "W1": "收割进度"}}
            for cell, label in required_headers[province].items():
                if cells.get(cell) != label:
                    raise ValueError(f"source workbook layout changed: {sheet_name}!{cell}")
            for date_col, value_cols, multiplier in blocks:
                missing = 0
                for ref, serial in cells.items():
                    col, row = re.fullmatch(r"([A-Z]+)(\d+)", ref).groups()
                    if col != date_col or not isinstance(serial, float) or not 30000 < serial < 60000:
                        continue
                    observed = (datetime(1899, 12, 30) + timedelta(days=serial)).date().isoformat()
                    for value_col, metric in value_cols.items():
                        value = cells.get(f"{value_col}{row}")
                        if not isinstance(value, float):
                            missing += 1
                            continue
                        records.append({"province": province, "metric": metric,
                            "date": observed, "value": round(value * multiplier, 8),
                            "source_url": SOURCE_URLS[province], "source_sha256": identity,
                            "source_locator": f"{sheet_name}!{date_col}{row},{value_col}{row}",
                            "date_basis": "workbook_date", "published_at": None,
                            "retrieved_at": retrieved, "status": "reported"})
                if missing:
                    notes.append(f"{province}/{date_col}: {missing} 个空值或 Excel 错误值未导入")
    if sha256_file(path) != identity:
        raise ValueError("source workbook changed during import")
    notes.extend(["历史日期沿用文件日期，未经逐期核实的发布日期留空。",
                  "萨省收割沿用原始 IK 收割率列；历史定义包含 Combined、Harvested As Feed、Other。",
                  "萨省生长阶段沿用 BR:BY 的阶段占比，不使用 CA:CH 累计阶段表，也不强制合计100%。"])
    return validate_bundle({"schema_version": SCHEMA, "generated_at": retrieved,
                            "records": sorted(records, key=lambda r: (r["province"], r["metric"], r["date"])),
                            "import_notes": notes})


def merge_observations(baseline: dict, updates: list[dict], *, allow_revisions: bool = False) -> tuple[dict, dict]:
    validate_bundle(baseline)
    candidate = {"schema_version": SCHEMA, "generated_at": utc_now(), "records": updates, "import_notes": []}
    validate_bundle(candidate)
    rows = {(x["province"], x["metric"], x["date"]): dict(x) for x in baseline["records"]}
    added, revised, unchanged = 0, 0, 0
    for item in updates:
        key = (item["province"], item["metric"], item["date"])
        old = rows.get(key)
        if old is not None and all(old[k] == item[k] for k in RECORD_FIELDS - {"retrieved_at"}):
            unchanged += 1
            continue
        if old is not None:
            if not allow_revisions:
                raise ValueError(f"existing observation requires explicit revision: {key}")
            revised += 1
        else:
            added += 1
        rows[key] = item
    result = {**baseline, "generated_at": candidate["generated_at"] if added or revised else baseline["generated_at"],
              "records": sorted(rows.values(), key=lambda r: (r["province"], r["metric"], r["date"]))}
    return validate_bundle(result), {"added": added, "revised": revised, "unchanged": unchanged}


def observations_frame(bundle: dict) -> pd.DataFrame:
    frame = pd.DataFrame(validate_bundle(bundle)["records"])
    frame["date"] = pd.to_datetime(frame["date"])
    frame["year"] = frame["date"].dt.year
    return frame.sort_values("date")


def match_history(frame: pd.DataFrame, target: date, year: int) -> dict | None:
    """Use the calendar anniversary or its preceding seven days; never interpolate."""
    try:
        anniversary = target.replace(year=year)
    except ValueError:
        anniversary = date(year, 2, 28)
    end = pd.Timestamp(anniversary)
    rows = frame.loc[(frame["year"] == year) & (frame["date"] <= end)
                     & (frame["date"] >= end - pd.Timedelta(days=7))]
    return rows.iloc[-1].to_dict() if not rows.empty else None


def compare_metric(frame: pd.DataFrame, province: str, metric: str, year: int) -> dict:
    series = frame.loc[(frame["province"] == province) & (frame["metric"] == metric)]
    current = series.loc[series["year"] == year]
    result = {"metric": metric, "current": None, "previous_change": None, "previous_days": None,
              "last_year": None, "mean": None, "samples": [], "latest_historical": None}
    if not series.empty:
        result["latest_historical"] = series.iloc[-1].to_dict()
    if current.empty:
        return result
    latest = current.iloc[-1].to_dict()
    target = latest["date"].date()
    samples = [matched for y in range(year - 5, year) if (matched := match_history(series, target, y))]
    result.update(current=latest, last_year=match_history(series, target, year - 1), samples=samples,
                  mean=sum(x["value"] for x in samples) / len(samples) if samples else None)
    if len(current) >= 2:
        prior = current.iloc[-2]
        result.update(previous_change=latest["value"] - prior["value"],
                      previous_days=(latest["date"] - prior["date"]).days)
    return result


def comparison_table(frame: pd.DataFrame, province: str, year: int, *, stages: bool = False) -> pd.DataFrame:
    rows = []
    for metric, name in (STAGES if stages else METRICS).items():
        comparison = compare_metric(frame, province, metric, year)
        current, last, mean = comparison["current"], comparison["last_year"], comparison["mean"]
        rows.append({"指标": name, "数据日期": current["date"].strftime("%Y-%m-%d") if current else "—",
                     "最新（%）": current["value"] if current else None,
                     "较上次（百分点）": comparison["previous_change"],
                     "距上次（天）": comparison["previous_days"],
                     "去年同期（%）": last["value"] if last else None,
                     "历史同期均值（%）": mean, "有效样本": f"{len(comparison['samples'])}/5",
                     "较去年（百分点）": current["value"] - last["value"] if current and last else None,
                     "较均值（百分点）": current["value"] - mean if current and mean is not None else None,
                     "状态": "本年度暂无数据" if not current else "本季已结束" if current["status"] == "season_complete" else "已记录"})
    return pd.DataFrame(rows)


def seasonal_figure(frame: pd.DataFrame, province: str, metric: str, year: int, extra_years: list[int]):
    import plotly.graph_objects as go
    figure = go.Figure()
    series = frame.loc[(frame["province"] == province) & (frame["metric"] == metric)]
    axis = lambda stamp: stamp.replace(year=2000)
    for y in sorted(set(extra_years) | {year - 1, year}):
        rows = series.loc[series["year"] == y]
        if rows.empty:
            continue
        current = y == year
        figure.add_trace(go.Scatter(x=[axis(x) for x in rows["date"]], y=rows["value"],
            name=f"{y}年", mode="lines+markers", connectgaps=False,
            line={"color": "#2457A7" if current else "#667085" if y == year - 1 else "#B6C1CE",
                  "width": 3 if current else 1.5}, marker={"size": 6 if current else 3},
            customdata=rows["date"].dt.strftime("%Y-%m-%d"),
            hovertemplate="%{customdata}<br>%{y:.1f}%<extra>%{fullData.name}</extra>"))
    # Weekly anchors stop at the historical season's bounds; each point has its own sample count.
    history = series.loc[series["year"].between(year - 5, year - 1)]
    anchors = sorted({axis(x).normalize() for x in history["date"]})
    means, counts = [], []
    for anchor in anchors:
        target = anchor.date().replace(year=year) if not (anchor.month == 2 and anchor.day == 29 and year % 4) else date(year, 2, 28)
        samples = [row["value"] for y in range(year - 5, year) if (row := match_history(series, target, y))]
        means.append(sum(samples) / len(samples) if samples else None)
        counts.append(len(samples))
    if anchors:
        figure.add_trace(go.Scatter(x=anchors, y=means, name="前五年同期均值", mode="lines",
            line={"color": "#D69A32", "width": 2, "dash": "dash"}, connectgaps=False,
            customdata=counts, hovertemplate="%{x|%m-%d}<br>%{y:.1f}%<br>有效样本 %{customdata}/5<extra></extra>"))
    figure.update_layout(height=420, margin=dict(l=15, r=15, t=15, b=15),
        template="plotly_white", legend=dict(orientation="h", y=1.12),
        xaxis=dict(title="季节日期", tickformat="%m-%d"),
        yaxis=dict(title="%", range=[0, 100]), hovermode="closest")
    return figure
