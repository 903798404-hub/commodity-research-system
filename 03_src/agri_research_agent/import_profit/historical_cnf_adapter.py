"""Read-only adapter for historical soybean CNF quotes stored in OOXML."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Iterable
from xml.etree import ElementTree as ET
import zipfile

import pyarrow as pa


ADAPTER_VERSION = "historical_cnf_adapter/1"
SCHEMA_VERSION = "historical-cnf-v1"
SHEET_NAME = "CNF报价"
SOURCE = "historical_excel"
COMMODITY = "soybean"
ORIGIN_GROUPS = (
    ("美湾", "us_gulf", 2),
    ("美西", "us_pnw", 14),
    ("巴西", "brazil", 26),
    ("阿根廷", "argentina", 38),
)
CNF_FIELDS = (
    "business_date",
    "commodity",
    "origin",
    "shipment_year",
    "shipment_month",
    "cnf_cents_per_bushel",
    "source",
    "updated_at",
    "batch_id",
)
HISTORICAL_CNF_SCHEMA = pa.schema(
    [
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("commodity", pa.string(), nullable=False),
        pa.field("origin", pa.string(), nullable=False),
        pa.field("shipment_year", pa.int16(), nullable=False),
        pa.field("shipment_month", pa.int8(), nullable=False),
        pa.field("cnf_cents_per_bushel", pa.float64(), nullable=True),
        pa.field("source", pa.string(), nullable=False),
        pa.field("updated_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("batch_id", pa.string(), nullable=False),
    ]
)

_MAIN_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CELL_REFERENCE = re.compile(r"([A-Z]+)([1-9]\d*)")


class HistoricalCnfError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        quality_report: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.quality_report = quality_report


@dataclass(frozen=True, slots=True)
class ExcelSourceIdentity:
    filename: str
    size: int
    sha256: str
    mtime: str


@dataclass(frozen=True, slots=True)
class HistoricalCnfQuote:
    business_date: date
    commodity: str
    origin: str
    shipment_year: int
    shipment_month: int
    cnf_cents_per_bushel: float | None
    source: str
    updated_at: datetime
    batch_id: str

    @property
    def key(self) -> tuple[date, str, str, int, int]:
        return (
            self.business_date,
            self.commodity,
            self.origin,
            self.shipment_year,
            self.shipment_month,
        )


@dataclass(frozen=True, slots=True)
class HistoricalCnfResult:
    source_identity: ExcelSourceIdentity
    records: tuple[HistoricalCnfQuote, ...]
    manifest_core: dict[str, Any]
    quality_report: dict[str, Any]


def shipment_year_for(business_date: date, shipment_month: int) -> int:
    """Map each wide month column to the rolling future twelve-month year."""

    if type(business_date) is not date:
        raise HistoricalCnfError("business_date must be a real date")
    if isinstance(shipment_month, bool) or not isinstance(shipment_month, int):
        raise HistoricalCnfError("shipment_month must be an integer")
    if not 1 <= shipment_month <= 12:
        raise HistoricalCnfError("shipment_month must be between 1 and 12")
    return business_date.year if shipment_month > business_date.month else business_date.year + 1


def inspect_excel_identity(path: str | Path) -> ExcelSourceIdentity:
    source = Path(path)
    if not source.is_file():
        raise HistoricalCnfError("historical CNF Excel is not a readable file")
    digest = hashlib.sha256()
    try:
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        stat = source.stat()
    except OSError as exc:
        raise HistoricalCnfError(
            f"historical CNF Excel is not readable: {exc.__class__.__name__}"
        ) from exc
    return ExcelSourceIdentity(
        filename=source.name,
        size=stat.st_size,
        sha256=digest.hexdigest().upper(),
        mtime=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    )


def adapt_historical_cnf_excel(path: str | Path) -> HistoricalCnfResult:
    source = Path(path)
    identity = inspect_excel_identity(source)
    updated_at = datetime.fromtimestamp(source.stat().st_mtime, timezone.utc)
    batch_id = f"historical-excel-{identity.sha256[:16].lower()}"
    fatal_issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    info: list[dict[str, Any]] = []
    records: list[HistoricalCnfQuote] = []
    dates: list[date] = []
    seen_dates: set[date] = set()
    duplicate_dates = 0
    counts = Counter()
    origin_counts: dict[str, Counter[str]] = defaultdict(Counter)
    earliest_numeric: dict[str, date] = {}
    latest_numeric: dict[str, date] = {}
    complete_null_dates = 0

    try:
        with zipfile.ZipFile(source) as archive:
            shared_strings = _read_shared_strings(archive)
            sheet_xml, used_range, date_1904 = _locate_sheet(archive, SHEET_NAME)
            rows = ET.fromstring(archive.read(sheet_xml)).findall(
                "m:sheetData/m:row", _MAIN_NS
            )
            if not rows:
                raise HistoricalCnfError("CNF报价 contains no rows")
            headers = _row_values(rows[0], shared_strings)
            expected_headers = _expected_headers()
            header_errors = [
                {
                    "column": _column_letters(column),
                    "expected": expected,
                    "actual": headers.get(column),
                }
                for column, expected in expected_headers.items()
                if headers.get(column) != expected
            ]
            if headers.get(1) != "日期":
                header_errors.insert(
                    0,
                    {"column": "A", "expected": "日期", "actual": headers.get(1)},
                )
            if header_errors:
                fatal_issues.append(
                    {
                        "code": "cnf_header_mismatch",
                        "count": len(header_errors),
                        "samples": header_errors[:8],
                    }
                )
                failed_quality = _quality_report(
                    fatal_issues, warnings, info, counts, origin_counts
                )
                failed_quality.update(
                    {
                        "date_parse_error_count": 0,
                        "header_error_count": len(header_errors),
                        "duplicate_business_date_count": 0,
                        "duplicate_stable_key_count": 0,
                    }
                )
                raise HistoricalCnfError(
                    "CNF报价 headers do not match the fixed four-origin layout",
                    quality_report=failed_quality,
                )

            for row in rows[1:]:
                cells = _row_cells(row)
                try:
                    business_date = _parse_excel_date(
                        cells.get(1), shared_strings, date_1904=date_1904
                    )
                except HistoricalCnfError as exc:
                    fatal_issues.append(
                        {
                            "code": "cnf_date_parse_error",
                            "count": 1,
                            "sample": {"row": int(row.get("r", "0")), "reason": str(exc)},
                        }
                    )
                    continue
                dates.append(business_date)
                if business_date in seen_dates:
                    duplicate_dates += 1
                seen_dates.add(business_date)
                row_numeric = 0
                for label, origin, start_column in ORIGIN_GROUPS:
                    for shipment_month in range(1, 13):
                        column = start_column + shipment_month - 1
                        value, category = _parse_quote(
                            cells.get(column), shared_strings
                        )
                        counts[category] += 1
                        origin_counts[origin][category] += 1
                        if category == "invalid":
                            fatal_issues.append(
                                {
                                    "code": "cnf_invalid_value",
                                    "count": 1,
                                    "sample": {
                                        "row": int(row.get("r", "0")),
                                        "column": _column_letters(column),
                                    },
                                }
                            )
                        if value is not None:
                            row_numeric += 1
                            earliest_numeric.setdefault(origin, business_date)
                            latest_numeric[origin] = business_date
                            if value == 0:
                                counts["zero"] += 1
                                origin_counts[origin]["zero"] += 1
                            if value < 0:
                                counts["negative"] += 1
                                origin_counts[origin]["negative"] += 1
                        records.append(
                            HistoricalCnfQuote(
                                business_date=business_date,
                                commodity=COMMODITY,
                                origin=origin,
                                shipment_year=shipment_year_for(
                                    business_date, shipment_month
                                ),
                                shipment_month=shipment_month,
                                cnf_cents_per_bushel=value,
                                source=SOURCE,
                                updated_at=updated_at,
                                batch_id=batch_id,
                            )
                        )
                if row_numeric == 0:
                    complete_null_dates += 1
    except zipfile.BadZipFile as exc:
        raise HistoricalCnfError("historical CNF input is not a valid OOXML workbook") from exc
    except KeyError as exc:
        raise HistoricalCnfError("historical CNF workbook structure is incomplete") from exc

    if duplicate_dates:
        fatal_issues.append({"code": "duplicate_business_date", "count": duplicate_dates})
    keys = [record.key for record in records]
    duplicate_keys = len(keys) - len(set(keys))
    if duplicate_keys:
        fatal_issues.append({"code": "duplicate_stable_key", "count": duplicate_keys})
    if not dates:
        fatal_issues.append({"code": "no_business_dates", "count": 1})
    invalid_count = counts["invalid"]
    if invalid_count and not any(
        item["code"] == "cnf_invalid_value" for item in fatal_issues
    ):
        fatal_issues.append({"code": "cnf_invalid_value", "count": invalid_count})

    records.sort(key=lambda item: item.key)
    last_any_numeric = max(latest_numeric.values()) if latest_numeric else None
    future_placeholder_dates = (
        sum(item > last_any_numeric for item in dates)
        if last_any_numeric is not None
        else len(dates)
    )
    null_count = counts["error"] + counts["blank"]
    if null_count:
        warnings.append({"code": "cnf_missing_quotes", "count": null_count})
    if future_placeholder_dates:
        warnings.append(
            {
                "code": "future_placeholder_dates",
                "count": future_placeholder_dates,
                "definition": "business dates after the last numeric CNF quote",
            }
        )
    if complete_null_dates:
        info.append(
            {"code": "complete_null_business_dates", "count": complete_null_dates}
        )
    quality = _quality_report(fatal_issues, warnings, info, counts, origin_counts)
    quality.update(
        {
            "date_parse_error_count": sum(
                item["count"]
                for item in fatal_issues
                if item["code"] == "cnf_date_parse_error"
            ),
            "header_error_count": sum(
                item["count"]
                for item in fatal_issues
                if item["code"] == "cnf_header_mismatch"
            ),
            "duplicate_business_date_count": duplicate_dates,
            "duplicate_stable_key_count": duplicate_keys,
            "business_date_range": _date_range(dates),
            "complete_null_business_date_count": complete_null_dates,
            "future_placeholder_date_count": future_placeholder_dates,
            "last_numeric_date_by_origin": {
                origin: latest_numeric.get(origin).isoformat()
                if origin in latest_numeric
                else None
                for _, origin, _ in ORIGIN_GROUPS
            },
            "first_numeric_date_by_origin": {
                origin: earliest_numeric.get(origin).isoformat()
                if origin in earliest_numeric
                else None
                for _, origin, _ in ORIGIN_GROUPS
            },
            "year_rule_samples": _year_rule_samples(),
        }
    )
    if fatal_issues:
        raise HistoricalCnfError(
            "historical CNF quality checks failed", quality_report=quality
        )

    manifest_core = {
        "adapter_version": ADAPTER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "worksheet": SHEET_NAME,
        "used_range": used_range,
        "date_column": "A",
        "origin_column_mapping": {
            label: {
                "origin": origin,
                "columns": f"{_column_letters(start)}:{_column_letters(start + 11)}",
                "headers": [f"{label}{month}月" for month in range(1, 13)],
            }
            for label, origin, start in ORIGIN_GROUPS
        },
        "business_date_range": _date_range(dates),
        "standard_record_count": len(records),
        "nonnull_count": counts["numeric"],
        "null_count": null_count,
        "zero_count": counts["zero"],
        "negative_count": counts["negative"],
        "source_updated_at_policy": (
            "Excel file mtime in UTC; initialization metadata, not original quote time"
        ),
        "batch_id": batch_id,
    }
    return HistoricalCnfResult(identity, tuple(records), manifest_core, quality)


def records_as_dicts(
    records: Iterable[HistoricalCnfQuote],
) -> list[dict[str, Any]]:
    return [asdict(record) for record in records]


def _locate_sheet(
    archive: zipfile.ZipFile, sheet_name: str
) -> tuple[str, str | None, bool]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        item.get("Id"): item.get("Target")
        for item in relationships.findall("r:Relationship", _REL_NS)
    }
    sheet = next(
        (
            item
            for item in workbook.findall("m:sheets/m:sheet", _MAIN_NS)
            if item.get("name") == sheet_name
        ),
        None,
    )
    if sheet is None:
        raise HistoricalCnfError(f"required worksheet is missing: {sheet_name}")
    relation_id = sheet.get(f"{{{_DOC_REL}}}id")
    target = targets.get(relation_id)
    if not target:
        raise HistoricalCnfError("worksheet relationship is missing")
    normalized = target.lstrip("/")
    if not normalized.startswith("xl/"):
        normalized = f"xl/{normalized}"
    root = ET.fromstring(archive.read(normalized))
    dimension = root.find("m:dimension", _MAIN_NS)
    workbook_properties = workbook.find("m:workbookPr", _MAIN_NS)
    date_1904 = (
        workbook_properties is not None
        and workbook_properties.get("date1904", "0") in {"1", "true", "True"}
    )
    return normalized, None if dimension is None else dimension.get("ref"), date_1904


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(node.text or "" for node in item.iterfind(".//m:t", _MAIN_NS))
        for item in root.findall("m:si", _MAIN_NS)
    ]


def _row_cells(row: ET.Element) -> dict[int, ET.Element]:
    return {
        _column_number(cell.get("r", "")): cell
        for cell in row.findall("m:c", _MAIN_NS)
    }


def _row_values(row: ET.Element, shared_strings: list[str]) -> dict[int, str | None]:
    return {
        column: _cell_text(cell, shared_strings)
        for column, cell in _row_cells(row).items()
    }


def _cell_text(cell: ET.Element, shared_strings: list[str]) -> str | None:
    kind = cell.get("t")
    value = cell.find("m:v", _MAIN_NS)
    raw = None if value is None else value.text
    if kind == "s" and raw is not None:
        try:
            return shared_strings[int(raw)]
        except (IndexError, ValueError) as exc:
            raise HistoricalCnfError("shared string reference is invalid") from exc
    if kind == "inlineStr":
        inline = cell.find("m:is", _MAIN_NS)
        if inline is None:
            return None
        return "".join(node.text or "" for node in inline.iterfind(".//m:t", _MAIN_NS))
    return raw


def _parse_excel_date(
    cell: ET.Element | None,
    shared_strings: list[str],
    *,
    date_1904: bool,
) -> date:
    if cell is None:
        raise HistoricalCnfError("date cell is blank")
    text = _cell_text(cell, shared_strings)
    if text in (None, "") or cell.get("t") == "e":
        raise HistoricalCnfError("date cell is blank or an Excel error")
    if cell.get("t") in {"s", "inlineStr", "str", "d"}:
        try:
            return datetime.fromisoformat(text).date()
        except ValueError as exc:
            raise HistoricalCnfError("date text is not ISO-compatible") from exc
    try:
        serial = float(text)
    except ValueError as exc:
        raise HistoricalCnfError("date serial is not numeric") from exc
    if not math.isfinite(serial):
        raise HistoricalCnfError("date serial is not finite")
    base = datetime(1904, 1, 1) if date_1904 else datetime(1899, 12, 30)
    result = (base + timedelta(days=serial)).date()
    if not 1900 <= result.year <= 2199:
        raise HistoricalCnfError("date is outside the supported range")
    return result


def _parse_quote(
    cell: ET.Element | None, shared_strings: list[str]
) -> tuple[float | None, str]:
    if cell is None:
        return None, "blank"
    text = _cell_text(cell, shared_strings)
    if text in (None, ""):
        return None, "blank"
    if cell.get("t") == "e" or text == "#N/A":
        return None, "error"
    if cell.get("t") in {"s", "inlineStr", "str", "b", "d"}:
        return None, "invalid"
    try:
        value = float(text)
    except ValueError:
        return None, "invalid"
    if not math.isfinite(value):
        return None, "invalid"
    return value, "numeric"


def _expected_headers() -> dict[int, str]:
    return {
        start + month - 1: f"{label}{month}月"
        for label, _, start in ORIGIN_GROUPS
        for month in range(1, 13)
    }


def _column_number(reference: str) -> int:
    match = _CELL_REFERENCE.fullmatch(reference)
    if match is None:
        raise HistoricalCnfError("cell reference is invalid")
    result = 0
    for char in match.group(1):
        result = result * 26 + ord(char) - 64
    return result


def _column_letters(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _quality_report(
    fatal_issues: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    info: list[dict[str, Any]],
    counts: Counter[str],
    origin_counts: dict[str, Counter[str]],
) -> dict[str, Any]:
    status = "failed" if fatal_issues else ("passed_with_warnings" if warnings else "passed")
    return {
        "candidate_status": status,
        "fatal_issues": fatal_issues[:20],
        "warnings": warnings[:20],
        "info": info[:20],
        "value_counts": {
            "numeric": counts["numeric"],
            "null": counts["error"] + counts["blank"],
            "excel_error": counts["error"],
            "true_blank": counts["blank"],
            "zero": counts["zero"],
            "negative": counts["negative"],
            "invalid": counts["invalid"],
        },
        "origin_value_counts": {
            origin: {
                "numeric": values["numeric"],
                "null": values["error"] + values["blank"],
                "excel_error": values["error"],
                "true_blank": values["blank"],
                "zero": values["zero"],
                "negative": values["negative"],
                "invalid": values["invalid"],
            }
            for _, origin, _ in ORIGIN_GROUPS
            for values in (origin_counts[origin],)
        },
    }


def _year_rule_samples() -> list[dict[str, Any]]:
    business_date = date(2026, 6, 25)
    return [
        {
            "business_date": business_date.isoformat(),
            "shipment_month": month,
            "shipment_year": shipment_year_for(business_date, month),
        }
        for month in (1, 7, 8, 12)
    ]


def _date_range(values: list[date]) -> dict[str, str | None]:
    return {
        "earliest": min(values).isoformat() if values else None,
        "latest": max(values).isoformat() if values else None,
    }
