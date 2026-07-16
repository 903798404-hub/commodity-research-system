from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import re
import shutil
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter

from .paths import resolve_configured_source_workbook


class BuildError(RuntimeError):
    """Raised when the audited source structure cannot be confirmed."""


def _remove_tree(path: Path, attempts: int = 5) -> None:
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.1 * (attempt + 1))


@dataclass(frozen=True)
class SourceRange:
    sheet: str
    min_col: int
    max_col: int
    min_row: int
    max_row: int

    @property
    def width(self) -> int:
        return self.max_col - self.min_col + 1

    def cells_for_column(self, offset: int) -> list[str]:
        col = get_column_letter(self.min_col + offset)
        return [f"{self.sheet}!{col}{row}" for row in range(self.min_row, self.max_row + 1)]


RANGE_RE = re.compile(r"([A-Za-z0-9]+)!([A-Z]+)(\d+):([A-Z]+)(\d+)")
YEAR_RE = re.compile(r"(?:(20\d{2})|(?<!\d)(\d{2})/(\d{2}))(F)?", re.IGNORECASE)

QUANTITY_METRICS = {
    "Beginning Stocks",
    "Production",
    "Imports",
    "Exports",
    "Crush",
    "Product Output",
    "Domestic Consumption",
    "Ending Stocks",
}

REPORT_BASIS = {
    "AN139900": "Oil World作物年度",
    "AN149900": "Oil World作物年度",
    "AN147900": "Oil World作物年度",
    "AN13993": "Oil World作物年度",
    "AN14993": "Oil World作物年度",
    "AN14793": "Oil World作物年度",
    "AN13992": "Oct–Sept",
    "AN14992": "Oct–Sept",
    "AN14792": "Oct–Sept",
    "AN23992": "Oct–Sept",
    "AN33992": "Oct–Sept",
    "AN24992": "Oct–Sept",
    "AN34992": "Oct–Sept",
    "AN24792": "Oct–Sept",
    "AN34792": "Oct–Sept",
    "AN26392": "Oct–Sept",
}

BASIS_PREFIXES = {
    "sept aug": "Sept–Aug",
    "oct sept": "Oct–Sept",
    "aug july": "Aug–July",
    "july june": "July–June",
    "june may": "June–May",
    "apr mar": "Apr–Mar",
    "jan dec": "Jan–Dec",
}

SYSTEM_SLUGS = {
    "大豆体系": "soy",
    "菜籽体系": "rapeseed",
    "葵花体系": "sunflower",
    "棕榈油体系": "palm",
}

PRODUCT_SLUGS = {
    "Soybeans": "soybeans",
    "Soybean Oil": "soybean-oil",
    "Soybean Meal": "soybean-meal",
    "Rapeseed / Canola": "rapeseed-canola",
    "Rapeseed Oil": "rapeseed-oil",
    "Rapeseed Meal": "rapeseed-meal",
    "Sunflowerseed": "sunflowerseed",
    "Sunflower Oil": "sunflower-oil",
    "Sunflower Meal": "sunflower-meal",
    "Palm Oil": "palm-oil",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _text_dump(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _metric_unit(metric: str) -> str:
    if metric in QUANTITY_METRICS:
        return "1000 T"
    if metric == "Stocks/Use Ratio":
        return "%"
    if metric == "Area Harvested":
        return "1000 ha"
    if metric == "Yield":
        return "T/ha"
    raise BuildError(f"Unknown standard metric: {metric}")


def _parse_ranges(expression: str) -> list[SourceRange]:
    ranges: list[SourceRange] = []
    for match in RANGE_RE.finditer(expression or ""):
        sheet, c1, r1, c2, r2 = match.groups()
        source_range = SourceRange(
            sheet=sheet,
            min_col=column_index_from_string(c1),
            max_col=column_index_from_string(c2),
            min_row=int(r1),
            max_row=int(r2),
        )
        if source_range.width <= 0 or source_range.max_row < source_range.min_row:
            raise BuildError(f"Invalid source range: {match.group(0)}")
        ranges.append(source_range)
    return ranges


def _split_periods(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split("|") if part.strip()]


def _canonical_period(label: str) -> str:
    text = str(label).strip()
    match = YEAR_RE.search(text)
    if not match:
        raise BuildError(f"Cannot recognize annual period: {label!r}")
    full_year, start, end, _forecast = match.groups()
    if full_year:
        return full_year
    return f"20{start}/{end}"


def _period_basis(label: str) -> str | None:
    lower = re.sub(r"\s+", " ", str(label).lower()).strip()
    for prefix, basis in BASIS_PREFIXES.items():
        if lower.startswith(prefix):
            return basis
    return None


def _forecast_status(label: str, row_status: str, offset: int) -> str:
    if str(label).strip().upper().endswith("F"):
        return "explicit_forecast"
    if row_status == "implicit_forecast" and offset == 0:
        return "implicit_forecast"
    return "historical"


def _standardize(value: float | None, original_unit: str, metric: str) -> float | None:
    if value is None:
        return None
    if original_unit == "Mn T":
        return round(value * 1000.0, 8)
    if original_unit in {"1000 T", "1000 ha", "T/ha", "%"}:
        return round(value, 8)
    if not original_unit:
        return round(value, 8)
    raise BuildError(f"Unsupported original unit {original_unit!r} for {metric}")


def _numeric_or_none(value: Any, source: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        percent_match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)%\s*", value)
        if percent_match:
            return float(percent_match.group(1))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise BuildError(f"Source cell {source} is neither numeric nor blank: {value!r}")
    return float(value)


def _read_matrix(workbook: Any, source_range: SourceRange) -> list[list[float | None]]:
    if source_range.sheet not in workbook.sheetnames:
        raise BuildError(f"Required worksheet missing: {source_range.sheet}")
    sheet = workbook[source_range.sheet]
    matrix: list[list[float | None]] = []
    for row in range(source_range.min_row, source_range.max_row + 1):
        values: list[float | None] = []
        for col in range(source_range.min_col, source_range.max_col + 1):
            address = f"{source_range.sheet}!{get_column_letter(col)}{row}"
            values.append(_numeric_or_none(sheet.cell(row, col).value, address))
        matrix.append(values)
    return matrix


def _column_sum(matrix: list[list[float | None]], offset: int) -> float | None:
    values = [row[offset] for row in matrix]
    if any(value is None for value in values):
        return None
    return float(sum(value for value in values if value is not None))


def _find_periods_from_header(workbook: Any, row: dict[str, Any], ranges: list[SourceRange]) -> list[str]:
    if not ranges:
        return []
    source_range = ranges[0]
    sheet = workbook[source_range.sheet]
    expected_width = source_range.width
    for header_row in range(1, 9):
        candidates = [sheet.cell(header_row, col).value for col in range(source_range.min_col, source_range.max_col + 1)]
        labels = [str(value).strip() if value is not None else "" for value in candidates]
        if len(labels) == expected_width and all(YEAR_RE.search(label) for label in labels):
            return labels
    raise BuildError(
        f"Cannot confirm full annual header for {row['commodity']} / {row['country_or_region']} / "
        f"{row['metric']} from {row['source_cell_or_range']}"
    )


def _validate_report_title(workbook: Any, report_id: str, expected_title: str) -> None:
    if report_id not in workbook.sheetnames:
        raise BuildError(f"Required worksheet missing: {report_id}")
    if not expected_title:
        raise BuildError(f"Audit catalog has no report title for {report_id}")
    sheet = workbook[report_id]
    candidates: list[str] = []
    for row in range(1, min(6, sheet.max_row) + 1):
        for col in range(1, min(14, sheet.max_column) + 1):
            value = sheet.cell(row, col).value
            if isinstance(value, str) and value.strip():
                candidates.append(value)
    expected = _normalize(expected_title)
    if not any(expected in _normalize(value) or _normalize(value) in expected for value in candidates if len(_normalize(value)) >= 12):
        raise BuildError(f"Report title drift detected for {report_id}: expected {expected_title!r}")


def _evaluate_derived(
    row: dict[str, Any], matrices: list[list[list[float | None]]], width: int
) -> list[float | None]:
    metric = row["metric"]
    formula = str(row.get("derivation_formula") or "")
    results: list[float | None] = []
    for offset in range(width):
        column_values = [_column_sum(matrix, offset) for matrix in matrices]
        if metric == "Stocks/Use Ratio":
            if len(column_values) != 3:
                raise BuildError(f"Unexpected Stocks/Use components: {row['source_cell_or_range']}")
            ending, crush, other_use = column_values
            denominator = None if crush is None or other_use is None else crush + other_use
            value = None if ending is None or denominator in {None, 0} else ending / denominator * 100.0
        elif metric == "Yield" and (formula.startswith("EU Yield") or formula.startswith("G3 Yield")):
            if len(column_values) % 2:
                raise BuildError(f"Unexpected Yield components: {row['source_cell_or_range']}")
            midpoint = len(column_values) // 2
            production_values = column_values[:midpoint]
            area_values = column_values[midpoint:]
            if any(value is None for value in production_values + area_values):
                value = None
            else:
                production = sum(value for value in production_values if value is not None)
                area = sum(value for value in area_values if value is not None)
                value = None if area == 0 else production / area
        else:
            value = None if any(item is None for item in column_values) else sum(item for item in column_values if item is not None)
        results.append(value)
    return results


def _infer_basis(
    row: dict[str, Any], raw_periods: list[str], config: dict[str, Any]
) -> str:
    override = config.get("conflict_basis_overrides", {}).get(
        f"{row['commodity']}|{row['country_or_region']}"
    )
    if row["mapping_status"] == "conflict" and override:
        return override
    for period in raw_periods:
        basis = _period_basis(period)
        if basis:
            return basis
    report_ids = [item for item in str(row.get("primary_report_id") or "").split(";") if item]
    bases = {REPORT_BASIS[item] for item in report_ids if item in REPORT_BASIS}
    if len(bases) == 1:
        return next(iter(bases))
    if len(bases) > 1:
        return " / ".join(sorted(bases))
    return "未提供（该指标无可展示数值）"


def _source_titles(row: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> list[str]:
    report_ids = [item for item in str(row.get("primary_report_id") or "").split(";") if item]
    return [str(catalog.get(report_id, {}).get("report_title") or row.get("report_title") or "") for report_id in report_ids]


def _period_family_and_role(row: dict[str, Any]) -> tuple[str, str]:
    if row.get("period_family") and row.get("source_role"):
        return str(row["period_family"]), str(row["source_role"])
    report_ids = [item for item in str(row.get("primary_report_id") or "").split(";") if item]
    crop_table = any(REPORT_BASIS.get(item) == "Oil World作物年度" for item in report_ids)
    return ("crop_year", "production_table") if crop_table else ("marketing_year", "balance")


def _build_metric(
    workbook: Any,
    row: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    status = row["mapping_status"]
    metric = row["metric"]
    if metric not in config["metric_order"]:
        raise BuildError(f"Unknown metric in audit: {metric}")
    source_expression = str(row.get("source_cell_or_range") or "")
    ranges = _parse_ranges(source_expression)
    raw_periods = _split_periods(str(row.get("market_years") or ""))
    values: dict[str, float | None] = {}
    original_periods: dict[str, str] = {}
    forecast_status: dict[str, str] = {}
    source_cells: dict[str, list[str]] = {}
    basis = _infer_basis(row, raw_periods, config)

    if status in {"direct", "derived"}:
        if not ranges:
            raise BuildError(f"Audited {status} mapping has no source range: {row}")
        widths = {source_range.width for source_range in ranges}
        if len(widths) != 1:
            raise BuildError(f"Source range width mismatch: {source_expression}")
        width = next(iter(widths))
        if len(raw_periods) != width:
            raw_periods = _find_periods_from_header(workbook, row, ranges)
        if len(raw_periods) != width:
            raise BuildError(f"Period count does not match source columns: {source_expression}")
        matrices = [_read_matrix(workbook, source_range) for source_range in ranges]
        if status == "direct":
            if len(matrices) != 1 or len(matrices[0]) != 1:
                raise BuildError(f"Direct mapping must be one source row: {source_expression}")
            raw_values = matrices[0][0]
        else:
            raw_values = _evaluate_derived(row, matrices, width)
        basis = _infer_basis(row, raw_periods, config)
        for offset, (raw_period, raw_value) in enumerate(zip(raw_periods, raw_values, strict=True)):
            period = _canonical_period(raw_period)
            if period in values:
                raise BuildError(f"Duplicate canonical period {period} in {source_expression}")
            values[period] = _standardize(raw_value, str(row.get("original_unit") or ""), metric)
            original_periods[period] = raw_period
            forecast_status[period] = _forecast_status(raw_period, str(row.get("forecast_status") or ""), offset)
            source_cells[period] = [cell for source_range in ranges for cell in source_range.cells_for_column(offset)]
    elif status not in {"missing", "conflict", "not_applicable"}:
        raise BuildError(f"Unsupported mapping status: {status}")

    ordered_periods = list(values)
    annual_change: dict[str, Any] | None = None
    if _as_bool(row.get("latest_annual_change_ready")) and status in {"direct", "derived"} and len(ordered_periods) >= 2:
        current_period, previous_period = ordered_periods[0], ordered_periods[1]
        current_value, previous_value = values[current_period], values[previous_period]
        if current_value is not None and previous_value is not None:
            change_unit = "percentage points" if metric == "Stocks/Use Ratio" else _metric_unit(metric)
            annual_change = {
                "current_period": current_period,
                "previous_period": previous_period,
                "value": round(current_value - previous_value, 8),
                "unit": change_unit,
            }

    quality_note = str(row.get("main_risk") or "")
    if status == "conflict":
        quality_note = " ".join(part for part in [config["conflict_message"], quality_note] if part)
    if status == "missing" and not quality_note:
        quality_note = "原始Oil World报表没有可确认的该指标数值，保持缺失。"

    report_ids = [item for item in str(row.get("primary_report_id") or "").split(";") if item]
    period_family, source_role = _period_family_and_role(row)
    return {
        "metric": metric,
        "mapping_status": status,
        "market_year_basis": str(row.get("basis_label") or basis),
        "period_family": period_family,
        "period_basis": str(row.get("period_basis") or basis),
        "source_role": source_role,
        "periods": ordered_periods,
        "original_periods": original_periods,
        "forecast_status": forecast_status,
        "original_metric": str(row.get("original_metric") or ""),
        "values": values,
        "annual_change": annual_change,
        "quarter_revision": None,
        "quarter_revision_note": "暂无上一期",
        "unit": str(row.get("standard_unit") or _metric_unit(metric)),
        "original_unit": str(row.get("original_unit") or ""),
        "source_report_id": report_ids,
        "source_report_title": _source_titles(row, catalog),
        "source_sheet": sorted({source_range.sheet for source_range in ranges} or set(report_ids)),
        "source_cells": source_cells,
        "source_cell_or_range": source_expression,
        "is_derived": status == "derived" or _as_bool(row.get("is_derived")),
        "derivation_method": str(row.get("derivation_formula") or ""),
        "derivation_components": str(row.get("derivation_components") or ""),
        "quality_note": quality_note,
        "has_footnote": _as_bool(row.get("has_footnote")),
        "has_star": _as_bool(row.get("has_star")),
    }


def _period_sort_key(period: str) -> tuple[int, str]:
    match = re.match(r"(20\d{2})", period)
    return (int(match.group(1)) if match else -1, period)


def _file_slug(product: str, region: str) -> str:
    product_slug = PRODUCT_SLUGS[product]
    region_slug = re.sub(r"[^a-z0-9]+", "-", region.casefold()).strip("-")
    return f"{product_slug}__{region_slug}.json"


def _validate_audit_sources(
    audit: dict[str, Any],
    coverage_csv: list[dict[str, str]],
    catalog_csv: list[dict[str, str]],
    metric_csv: list[dict[str, str]],
    config: dict[str, Any],
) -> None:
    expected_base_count = config.get("expected_base_mapping_count", config["expected_mapping_count"])
    if len(audit["coverage_matrix"]) != expected_base_count:
        raise BuildError("Mapping audit JSON count drifted")
    if len(coverage_csv) != expected_base_count:
        raise BuildError("Coverage CSV count drifted")
    json_keys = {
        (row["system"], row["commodity"], row["country_or_region"], row["metric"], row["mapping_status"])
        for row in audit["coverage_matrix"]
    }
    csv_keys = {
        (row["system"], row["commodity"], row["country_or_region"], row["metric"], row["mapping_status"])
        for row in coverage_csv
    }
    if json_keys != csv_keys:
        raise BuildError("Coverage CSV no longer mirrors mapping audit JSON")
    if len(catalog_csv) != len(audit["report_catalog"]):
        raise BuildError("Report catalog CSV count drifted")
    if len(metric_csv) != len(audit["metric_mapping"]):
        raise BuildError("Metric mapping CSV count drifted")
    if audit["scope"]["systems"] != config["systems"]:
        raise BuildError("Audited product or region scope drifted from the fixed first-version configuration")
    mapped_metrics = {row["standard_metric"] for row in metric_csv if row["standard_metric"] != "not in scope"}
    if not set(config["metric_order"]).issubset(mapped_metrics):
        raise BuildError("Metric mapping CSV is missing a configured standard metric")


SPECIAL_TIME_AXES = {
    ("Soybeans", "Brazil"): {
        "report_id": "AN51000A", "production_report_id": "AN13993", "production_row": 11,
        "title": "BRAZIL: Soybean Balance (1000 T)",
    },
    ("Sunflowerseed", "Argentina"): {
        "report_id": "AN50002", "production_report_id": "AN14793", "production_row": 25,
        "title": "ARGENTINA : Sunflowerseed Balance (1000 T)",
    },
}


def _apply_special_time_axes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Restore audited Jan-Dec balances without merging them into crop-year data."""
    result: list[dict[str, Any]] = []
    additions: list[dict[str, Any]] = []
    direct_rows = {"Beginning Stocks": 3, "Production": 4, "Imports": 5, "Exports": 6, "Crush": 7, "Ending Stocks": 9}
    for row in rows:
        key = (row["commodity"], row["country_or_region"])
        special = SPECIAL_TIME_AXES.get(key)
        if special and row["metric"] in {"Area Harvested", "Yield"}:
            updated = dict(row)
            updated.update({
                "period_family": "crop_year", "period_basis": "Oil World crop year",
                "source_role": "production_table", "basis_label": "Oil World作物年度｜世界生产表",
            })
            result.append(updated)
            continue
        if not special or row["metric"] not in {*direct_rows, "Domestic Consumption", "Stocks/Use Ratio"}:
            result.append(row)
            continue
        metric = row["metric"]
        updated = dict(row)
        updated.update({
            "mapping_status": "direct" if metric in direct_rows else "derived",
            "primary_report_id": special["report_id"], "source_sheet": special["report_id"],
            "report_title": special["title"], "toc_section": "Country Section",
            "title_row": 1, "header_row": 2, "market_years": "",
            "market_year_columns": "B:D", "excluded_period_columns": "partial-period columns excluded",
            "original_unit": "1000 T", "standard_unit": "%" if metric == "Stocks/Use Ratio" else "1000 T",
            "forecast_status": "header F marker", "latest_annual_change_ready": True,
            "quarter_revision_ready": True, "period_family": "calendar_year", "period_basis": "Jan–Dec",
            "source_role": "balance", "basis_label": "Jan–Dec自然年｜Oil World国家平衡表",
            "main_risk": "Jan–Dec natural-year balance; kept separate from Oil World crop-year production table.",
        })
        if metric in direct_rows:
            r = direct_rows[metric]
            updated.update({"original_metric": {3: "Open'g stocks", 4: "Crop", 5: "Imports", 6: "Exports", 7: "Crushings", 9: "Ending stocks"}[r], "data_row": r, "source_cell_or_range": f"{special['report_id']}!B{r}:D{r}", "is_derived": False, "derivation_formula": "", "derivation_components": ""})
        elif metric == "Domestic Consumption":
            updated.update({"original_metric": "Crushings + Other use", "data_row": "7+8", "source_cell_or_range": f"{special['report_id']}!B7:D7 + {special['report_id']}!B8:D8", "is_derived": True, "derivation_formula": "Domestic Consumption = Crushings + Other use", "derivation_components": f"{special['report_id']} rows 7 + 8"})
        else:
            updated.update({"original_metric": "Ending stocks / (Crushings + Other use)", "data_row": "9/(7+8)", "source_cell_or_range": f"{special['report_id']}!B9:D9 / ({special['report_id']}!B7:D7 + {special['report_id']}!B8:D8)", "is_derived": True, "derivation_formula": "Stocks/Use Ratio = Ending Stocks / Domestic Consumption × 100", "derivation_components": f"{special['report_id']} rows 7, 8, 9"})
        result.append(updated)
    for key, special in SPECIAL_TIME_AXES.items():
        template = next(row for row in rows if (row["commodity"], row["country_or_region"], row["metric"]) == (*key, "Area Harvested"))
        production = dict(template)
        production.update({
            "metric": "Production", "mapping_status": "direct", "primary_report_id": special["production_report_id"],
            "source_sheet": special["production_report_id"], "original_metric": "PRODUCTION", "data_row": special["production_row"],
            "market_year_columns": "C:E", "source_cell_or_range": f"{special['production_report_id']}!C{special['production_row']}:E{special['production_row']}",
            "original_unit": "1000 T", "standard_unit": "1000 T", "period_family": "crop_year",
            "period_basis": "Oil World crop year", "source_role": "production_table",
            "basis_label": "Oil World作物年度｜世界生产表", "is_derived": False,
            "derivation_formula": "", "derivation_components": "", "main_risk": "",
        })
        additions.append(production)
    return result + additions


def _copy_release(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def build_release(project_root: Path, replace: bool = False) -> dict[str, Any]:
    project_root = project_root.resolve()
    config_path = project_root / "02_configs" / "release_2026-06.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    release = config["release"]
    workbook_path = resolve_configured_source_workbook(project_root, config["source_workbook"])
    audit_dir = project_root / config["audit_directory"]
    audit_paths = {name: audit_dir / filename for name, filename in config["audit_files"].items()}
    for required in [config_path, workbook_path, *audit_paths.values()]:
        if not required.exists():
            raise BuildError(f"Required source file missing: {required}")
    source_hash_before = _sha256(workbook_path)
    if source_hash_before != config["source_workbook_sha256"]:
        raise BuildError("Source workbook hash does not match the audited baseline")

    audit = json.loads(audit_paths["mapping_audit"].read_text(encoding="utf-8"))
    if audit.get("source_workbook_sha256") != config["source_workbook_sha256"]:
        raise BuildError("Mapping audit and release configuration reference different source workbook hashes")
    coverage_csv = _read_csv(audit_paths["coverage_matrix"])
    catalog_csv = _read_csv(audit_paths["report_catalog"])
    metric_csv = _read_csv(audit_paths["metric_mapping"])
    _validate_audit_sources(audit, coverage_csv, catalog_csv, metric_csv, config)
    audit = dict(audit)
    audit["coverage_matrix"] = _apply_special_time_axes(list(audit["coverage_matrix"]))
    if len(audit["coverage_matrix"]) != config["expected_mapping_count"]:
        raise BuildError("Transformed mapping count drifted")
    transformed_status_counts = Counter(row["mapping_status"] for row in audit["coverage_matrix"])
    if dict(transformed_status_counts) != config["expected_status_counts"]:
        raise BuildError(f"Transformed mapping status counts changed: {dict(transformed_status_counts)}")
    catalog = {row["report_id"]: row for row in audit["report_catalog"]}
    adopted = {report_id for report_id, row in catalog.items() if row["decision"] == "adopted"}
    if len(adopted) != config["expected_adopted_report_count"]:
        raise BuildError(f"Unexpected adopted report count: {len(adopted)}")

    numeric_report_ids = {
        report_id
        for row in audit["coverage_matrix"]
        if row["mapping_status"] in {"direct", "derived"}
        for report_id in str(row.get("primary_report_id") or "").split(";")
        if report_id
    }
    if len(numeric_report_ids) != config["expected_numeric_source_report_count"]:
        raise BuildError(f"Unexpected numeric source report count: {len(numeric_report_ids)}")
    if not numeric_report_ids.issubset(adopted):
        raise BuildError("A numeric mapping points to a report not adopted by the audit")

    workbook = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
    for report_id in sorted(numeric_report_ids):
        _validate_report_title(workbook, report_id, catalog[report_id]["report_title"])

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in audit["coverage_matrix"]:
        grouped[(row["system"], row["commodity"], row["country_or_region"])].append(row)
    if len(grouped) != config["expected_combination_count"]:
        raise BuildError(f"Unexpected combination count: {len(grouped)}")

    temp_root = project_root / ".oil_world_build_tmp"
    if temp_root.exists():
        _remove_tree(temp_root)
    release_root = temp_root / "release"
    combo_root = release_root / "combinations"
    combo_root.mkdir(parents=True, exist_ok=True)

    index_files: list[dict[str, Any]] = []
    combination_payloads: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    numeric_observation_count = 0
    direct_observations: list[dict[str, Any]] = []
    systems_index: dict[str, dict[str, Any]] = {}

    scope_systems = config["systems"]
    for system, system_scope in scope_systems.items():
        system_entry = {"id": SYSTEM_SLUGS[system], "label": system, "products": []}
        systems_index[system] = system_entry
        for product in system_scope["commodities"]:
            product_entry = {"id": PRODUCT_SLUGS[product], "label": product, "regions": []}
            system_entry["products"].append(product_entry)
            for region in system_scope["countries"]:
                rows = grouped[(system, product, region)]
                if set(row["metric"] for row in rows) != set(config["metric_order"]):
                    raise BuildError(f"Metric scope mismatch for {product} / {region}")
                metric_rank = {metric: position for position, metric in enumerate(config["metric_order"])}
                rows.sort(key=lambda row: (metric_rank[row["metric"]], row.get("source_role", "balance")))
                metrics = [_build_metric(workbook, row, catalog, config) for row in rows]
                for metric_payload in metrics:
                    status_counts[metric_payload["mapping_status"]] += 1
                    numeric_observation_count += sum(value is not None for value in metric_payload["values"].values())
                    if metric_payload["mapping_status"] == "direct":
                        for period, value in metric_payload["values"].items():
                            if value is not None and len(metric_payload["source_cells"].get(period, [])) == 1:
                                direct_observations.append(
                                    {
                                        "system": system,
                                        "product": product,
                                        "region": region,
                                        "metric": metric_payload["metric"],
                                        "period": period,
                                        "source_cell": metric_payload["source_cells"][period][0],
                                        "generated_value": value,
                                        "original_unit": metric_payload["original_unit"],
                                        "standard_unit": metric_payload["unit"],
                                    }
                                )
                period_status: dict[str, str] = {}
                bases: set[str] = set()
                for metric_payload in metrics:
                    if (
                        metric_payload["mapping_status"] != "not_applicable"
                        and not metric_payload["market_year_basis"].startswith("未提供")
                    ):
                        bases.add(metric_payload["market_year_basis"])
                    for period, forecast in metric_payload["forecast_status"].items():
                        existing = period_status.get(period)
                        priority = {"historical": 0, "implicit_forecast": 1, "explicit_forecast": 2}
                        if existing is None or priority[forecast] > priority[existing]:
                            period_status[period] = forecast
                periods = sorted(period_status, key=_period_sort_key, reverse=True)
                payload = {
                    "schema_version": 1,
                    "release": release,
                    "system": system,
                    "product": product,
                    "region": region,
                    "market_year_basis": sorted(bases),
                    "periods": periods,
                    "forecast_status": {period: period_status[period] for period in periods},
                    "metrics": metrics,
                    "quality_note": "G3/G2仅包含映射审计确认安全的指标。" if region in {"G2", "G3"} else "",
                }
                filename = _file_slug(product, region)
                _json_dump(combo_root / filename, payload)
                relative_path = f"combinations/{filename}"
                index_files.append({"system": system, "product": product, "region": region, "path": relative_path})
                combination_payloads.append(payload)
                product_entry["regions"].append(region)

    if dict(status_counts) != config["expected_status_counts"]:
        raise BuildError(f"Generated status counts differ from audit: {dict(status_counts)}")
    annual_change_count = sum(
        metric["annual_change"] is not None
        for payload in combination_payloads
        for metric in payload["metrics"]
    )
    expected_annual_change_count = config.get("expected_annual_change_count")
    if expected_annual_change_count is not None and annual_change_count != expected_annual_change_count:
        raise BuildError(f"Unexpected annual change count: {annual_change_count}")

    randomizer = random.Random(config["spot_check_seed"])
    if len(direct_observations) < config["spot_check_count"]:
        raise BuildError("Not enough direct observations for spot checks")
    selected_checks = randomizer.sample(direct_observations, config["spot_check_count"])
    spot_checks: list[dict[str, Any]] = []
    for item in selected_checks:
        sheet_name, address = item["source_cell"].split("!", 1)
        raw_value = _numeric_or_none(workbook[sheet_name][address].value, item["source_cell"])
        expected_value = _standardize(raw_value, item["original_unit"], item["metric"])
        passed = expected_value == item["generated_value"]
        spot_checks.append({**item, "raw_value": raw_value, "expected_value": expected_value, "passed": passed})
    if not all(item["passed"] for item in spot_checks):
        raise BuildError("A source-cell spot check failed")

    index = {
        "schema_version": 1,
        "release": release,
        "release_label": config["release_label"],
        "source": "Oil World",
        "systems": list(systems_index.values()),
        "metric_order": config["metric_order"],
        "files": index_files,
        "combination_count": len(index_files),
        "mapping_record_count": sum(status_counts.values()),
        "numeric_observation_count": numeric_observation_count,
        "annual_change_count": annual_change_count,
        "status_counts": dict(status_counts),
        "quarter_revision_available": False,
    }
    _json_dump(release_root / "index.json", index)
    _json_dump(
        release_root / "quality_report.json",
        {
            "release": release,
            "passed": True,
            "source_workbook_sha256": source_hash_before,
            "audit_file_sha256": {name: _sha256(path) for name, path in audit_paths.items()},
            "validated_source_reports": sorted(numeric_report_ids),
            "validated_source_report_count": len(numeric_report_ids),
            "mapping_status_counts": dict(status_counts),
            "combination_count": len(index_files),
            "numeric_observation_count": numeric_observation_count,
            "annual_change_count": annual_change_count,
            "spot_check_count": len(spot_checks),
            "spot_checks_passed": sum(item["passed"] for item in spot_checks),
            "quarter_revision": "暂无上一期",
            "warnings": [
                "Brazil Soybeans与Argentina Sunflowerseed保留Jan–Dec自然年冲突说明，数值不展示。",
                "油脂、油粕与Palm Oil的Stocks/Use Ratio按审计保持conflict或missing。",
                "Other Countries口径风险仅保留在审计说明，不扩展普通前端范围。",
            ],
        },
    )
    _json_dump(
        release_root / "spot_check_report.json",
        {
            "release": release,
            "seed": config["spot_check_seed"],
            "count": len(spot_checks),
            "passed": all(item["passed"] for item in spot_checks),
            "checks": spot_checks,
        },
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": 1,
        "release": release,
        "release_label": config["release_label"],
        "generated_at": generated_at,
        "source_workbook": config["source_workbook"],
        "source_workbook_sha256": source_hash_before,
        "mapping_audit": config["audit_files"],
        "combination_count": len(index_files),
        "mapping_record_count": sum(status_counts.values()),
        "numeric_observation_count": numeric_observation_count,
        "annual_change_count": annual_change_count,
        "adopted_report_count": len(adopted),
        "numeric_source_report_count": len(numeric_report_ids),
        "status_counts": dict(status_counts),
        "available": True,
    }
    _json_dump(release_root / "manifest.json", manifest)
    _text_dump(
        release_root / "说明.md",
        "# 2026-06发布说明\n\n"
        "本目录是Oil World第一版年度研究数据快照，由原始Excel和四份映射审计生成。"
        "组合文件保留direct、derived、missing、not_applicable和conflict状态；不得人工补0或改写冲突值。",
    )

    workbook.close()
    source_hash_after = _sha256(workbook_path)
    if source_hash_after != source_hash_before:
        raise BuildError("Source workbook changed during the read-only build")

    data_destination = project_root / "01_data" / "releases" / release
    public_destination = project_root / "public" / "data" / "oil_world" / "releases" / release
    if (data_destination.exists() or public_destination.exists()) and not replace:
        raise BuildError(f"Immutable release {release} already exists; use replace only during local first-version development")
    _copy_release(release_root, data_destination)
    _copy_release(release_root, public_destination)
    _json_dump(project_root / "public" / "data" / "oil_world" / "latest.json", {"release": release})
    _json_dump(
        project_root / "public" / "data" / "oil_world" / "releases.json",
        {"releases": [{"release": release, "label": config["release_label"], "available": True}]},
    )
    _remove_tree(temp_root)
    return {
        "release": release,
        "combination_count": len(index_files),
        "mapping_record_count": sum(status_counts.values()),
        "numeric_observation_count": numeric_observation_count,
        "annual_change_count": annual_change_count,
        "status_counts": dict(status_counts),
        "adopted_report_count": len(adopted),
        "numeric_source_report_count": len(numeric_report_ids),
        "spot_check_count": len(spot_checks),
        "source_workbook_sha256": source_hash_after,
        "data_directory": str(data_destination),
        "public_directory": str(public_destination),
    }
