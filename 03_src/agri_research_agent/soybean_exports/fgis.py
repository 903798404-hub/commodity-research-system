from __future__ import annotations

import csv
import gzip
import io
import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import requests

from .common import (
    SCHEMA_VERSION,
    PipelineError,
    atomic_copy,
    append_revisions,
    iso_utc,
    make_batch_id,
    publish_candidate,
    read_last_success,
    sha256_bytes,
    sha256_file,
    utc_now,
    write_json_atomic,
    write_parquet_atomic,
    write_raw_snapshot,
)


FGIS_SOURCE = "usda_fgis_export_inspections"
FGIS_SOURCE_AUTHORITY = "USDA FGIS"
FGIS_SOCRATA_SOURCE_CHANNEL = "agtransport_socrata"
FGIS_YEARLY_SOURCE_CHANNEL = "yearly_export_grain_csv"
FGIS_DATASET_ID = "sruw-w49i"
FGIS_RESOURCE_URL = f"https://agtransport.usda.gov/resource/{FGIS_DATASET_ID}.json"
FGIS_METADATA_URL = f"https://agtransport.usda.gov/api/views/{FGIS_DATASET_ID}"
FGIS_YEARLY_BASE_URL = "https://fgisonline.ams.usda.gov/exportgrainreport"
FGIS_YEARLY_MAX_ATTEMPTS = 5
FGIS_YEARLY_RETRY_DELAYS_SECONDS = (1, 2, 4, 8)
FGIS_YEARLY_REQUIRED_FIELDS = (
    "Thursday",
    "Cert Date",
    "Grain",
    "Destination",
    "Metric Ton",
)
FGIS_RAW_FIELDS = (
    "date",
    "cert_date",
    "week",
    "month",
    "quarter",
    "year",
    "type_shipm",
    "type_carrier",
    "type_carrier_text",
    "carrier_name",
    "grain",
    "grade",
    "class",
    "subclass",
    "destination",
    "port",
    "ams_reg",
    "fgis_reg",
    "state",
    "mt",
    "pounds",
    "field_office",
)
FGIS_NULLABLE_RAW_FIELDS = {
    "type_carrier_text",
    "carrier_name",
    "grade",
    "class",
    "subclass",
    "state",
}
FGIS_REQUIRED_ROW_FIELDS = set(FGIS_RAW_FIELDS) - FGIS_NULLABLE_RAW_FIELDS
FGIS_STABLE_COLUMNS = (
    "schema_version",
    "source",
    "source_dataset_id",
    "commodity",
    "market_year_start",
    "market_year_end",
    "market_year_label",
    "my_week",
    "week_ending_date",
    "destination",
    "destination_role",
    "weekly_mt",
    "cumulative_mt",
    "source_row_count",
    "source_fetch_id",
    "batch_id",
    "raw_snapshot_id",
    "raw_snapshot_sha256",
    "source_dataset_updated_at",
    "fetch_time_utc",
)
FGIS_KEY = (
    "source",
    "commodity",
    "market_year_end",
    "week_ending_date",
    "destination",
)
FGIS_REVISION_FIELDS = ("weekly_mt", "cumulative_mt", "source_row_count")
FGIS_BUSINESS_COLUMNS = (
    "source",
    "source_dataset_id",
    "commodity",
    "market_year_start",
    "market_year_end",
    "market_year_label",
    "my_week",
    "week_ending_date",
    "destination",
    "destination_role",
    "weekly_mt",
    "cumulative_mt",
    "source_row_count",
)


class FgisAdapterError(PipelineError):
    pass


class _FgisYearlyTransferError(Exception):
    """A complete Yearly response was not received and may be retried."""


@dataclass(frozen=True)
class FgisFetchResult:
    records: list[dict[str, Any]]
    fetch_time_utc: str
    dataset_updated_at: str | None
    pages: list[dict[str, Any]]
    query_scope: dict[str, Any]
    source_authority: str = FGIS_SOURCE_AUTHORITY
    source_channel: str = FGIS_SOCRATA_SOURCE_CHANNEL
    calendar_year: int | None = None
    source_file: str | None = None
    source_url: str | None = None
    source_sha256: str | None = None
    http_status: int | None = None
    content_length: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    source_bytes: bytes | None = None


class FgisAdapter:
    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout_seconds: float = 50,
        page_size: int = 50_000,
        use_environment_proxy: bool = True,
    ) -> None:
        if timeout_seconds <= 0 or page_size <= 0:
            raise ValueError("timeout_seconds and page_size must be positive")
        self.session = session or requests.Session()
        if session is None and not use_environment_proxy:
            self.session.trust_env = False
        self.timeout_seconds = timeout_seconds
        self.page_size = page_size

    def _get_json(self, url: str, *, params: Mapping[str, Any] | None = None) -> Any:
        try:
            response = self.session.get(
                url,
                params=params,
                timeout=self.timeout_seconds,
                headers={"Accept": "application/json"},
            )
        except requests.RequestException as exc:
            raise FgisAdapterError(f"FGIS request failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise FgisAdapterError(f"FGIS HTTP status {response.status_code}")
        try:
            return response.json(), response.content
        except ValueError as exc:
            raise FgisAdapterError("FGIS returned invalid JSON") from exc

    def fetch_soybeans(
        self,
        *,
        cert_date_start: date | None = None,
        cert_date_end: date | None = None,
    ) -> FgisFetchResult:
        fetched_at = iso_utc(utc_now())
        metadata, _ = self._get_json(FGIS_METADATA_URL)
        columns = {
            str(item.get("fieldName"))
            for item in metadata.get("columns", [])
            if isinstance(item, dict)
        }
        missing_metadata = sorted(set(FGIS_RAW_FIELDS) - columns)
        if missing_metadata:
            raise FgisAdapterError(
                f"FGIS metadata missing required fields: {', '.join(missing_metadata)}"
            )

        clauses = ["grain='SOYBEANS'"]
        if cert_date_start is not None:
            clauses.append(f"cert_date >= '{cert_date_start.isoformat()}T00:00:00.000'")
        if cert_date_end is not None:
            clauses.append(f"cert_date <= '{cert_date_end.isoformat()}T23:59:59.999'")
        where = " AND ".join(clauses)
        records: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        offset = 0
        while True:
            params = {
                "$select": "*",
                "$where": where,
                "$order": ":id",
                "$limit": self.page_size,
                "$offset": offset,
            }
            payload, content = self._get_json(FGIS_RESOURCE_URL, params=params)
            if not isinstance(payload, list):
                raise FgisAdapterError("FGIS response is not an array")
            page_records: list[dict[str, Any]] = []
            for raw in payload:
                if not isinstance(raw, dict):
                    raise FgisAdapterError("FGIS response contains a non-object row")
                missing = sorted(FGIS_REQUIRED_ROW_FIELDS - set(raw))
                if missing:
                    raise FgisAdapterError(
                        f"FGIS row missing required fields: {', '.join(missing)}"
                    )
                if raw.get("grain") != "SOYBEANS":
                    raise FgisAdapterError("FGIS strict SOYBEANS filter was violated")
                normalized_raw = dict(raw)
                for nullable_field in FGIS_NULLABLE_RAW_FIELDS:
                    normalized_raw.setdefault(nullable_field, None)
                page_records.append(normalized_raw)
            pages.append(
                {
                    "offset": offset,
                    "limit": self.page_size,
                    "row_count": len(page_records),
                    "response_sha256": sha256_bytes(content),
                }
            )
            records.extend(page_records)
            if len(page_records) < self.page_size:
                break
            offset += self.page_size
        return FgisFetchResult(
            records=records,
            fetch_time_utc=fetched_at,
            dataset_updated_at=_socrata_timestamp(metadata.get("rowsUpdatedAt")),
            pages=pages,
            query_scope={
                "grain": "SOYBEANS",
                "cert_date_start": cert_date_start.isoformat() if cert_date_start else None,
                "cert_date_end": cert_date_end.isoformat() if cert_date_end else None,
                "order": ":id",
                "page_size": self.page_size,
            },
            source_url=FGIS_RESOURCE_URL,
            http_status=200,
        )


class FgisYearlyAdapter:
    """Fetch the active calendar-year USDA FGIS Yearly Export Grain CSV."""

    _CONTENT_TYPES = {
        "application/csv",
        "application/octet-stream",
        "binary/octet-stream",
        "text/csv",
        "text/plain",
    }

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout_seconds: float = 50,
        calendar_year: int | None = None,
        max_attempts: int = FGIS_YEARLY_MAX_ATTEMPTS,
        use_environment_proxy: bool = True,
    ) -> None:
        if timeout_seconds <= 0 or max_attempts <= 0:
            raise ValueError("timeout_seconds and max_attempts must be positive")
        if calendar_year is not None and not 1983 <= calendar_year <= 9999:
            raise ValueError("calendar_year is outside the supported Yearly range")
        self.session = session or requests.Session()
        if session is None and not use_environment_proxy:
            self.session.trust_env = False
        self.timeout_seconds = timeout_seconds
        self.calendar_year = calendar_year
        self.max_attempts = max_attempts

    def fetch_soybeans(
        self,
        *,
        cert_date_start: date | None = None,
        cert_date_end: date | None = None,
    ) -> FgisFetchResult:
        if cert_date_start is not None or cert_date_end is not None:
            raise FgisAdapterError(
                "Yearly production fetch always reads the complete active calendar-year file"
            )
        calendar_year = self.calendar_year or date.today().year
        source_file = f"CY{calendar_year}.csv"
        source_url = f"{FGIS_YEARLY_BASE_URL}/{source_file}"
        fetched_at = iso_utc(utc_now())
        response = None
        content: bytes | None = None
        declared_length: int | None = None
        last_transfer_error: BaseException | None = None
        retryable_errors = (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
            IncompleteRead,
            _FgisYearlyTransferError,
        )
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.get(
                    source_url,
                    timeout=self.timeout_seconds,
                    headers={"Accept": "text/csv, application/octet-stream"},
                )
                if response.status_code != 200:
                    raise FgisAdapterError(
                        f"FGIS Yearly HTTP status {response.status_code}"
                    )
                content_type = _response_header(response, "Content-Type")
                media_type = (content_type or "").split(";", 1)[0].strip().lower()
                if media_type not in self._CONTENT_TYPES:
                    raise FgisAdapterError(
                        "FGIS Yearly unexpected Content-Type: "
                        f"{content_type or 'missing'}"
                    )
                content = bytes(response.content)
                content_length_header = _response_header(response, "Content-Length")
                if content_length_header is not None:
                    try:
                        declared_length = int(content_length_header)
                    except ValueError as exc:
                        raise FgisAdapterError(
                            "FGIS Yearly invalid Content-Length"
                        ) from exc
                    if declared_length != len(content):
                        raise _FgisYearlyTransferError(
                            "FGIS Yearly Content-Length mismatch"
                        )
                else:
                    declared_length = len(content)
                break
            except retryable_errors as exc:
                response = None
                content = None
                declared_length = None
                last_transfer_error = exc
                if attempt < self.max_attempts:
                    delay_index = min(
                        attempt - 1, len(FGIS_YEARLY_RETRY_DELAYS_SECONDS) - 1
                    )
                    time.sleep(FGIS_YEARLY_RETRY_DELAYS_SECONDS[delay_index])
        if response is None or content is None or declared_length is None:
            assert last_transfer_error is not None
            raise FgisAdapterError(
                f"FGIS Yearly request failed after {self.max_attempts} attempts: "
                f"{type(last_transfer_error).__name__}"
            ) from last_transfer_error
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise FgisAdapterError("FGIS Yearly CSV is not strict UTF-8") from exc
        reader = csv.DictReader(io.StringIO(text, newline=""))
        fields = tuple(reader.fieldnames or ())
        missing = sorted(set(FGIS_YEARLY_REQUIRED_FIELDS) - set(fields))
        if missing:
            raise FgisAdapterError(
                f"FGIS Yearly CSV missing required fields: {', '.join(missing)}"
            )
        records: list[dict[str, Any]] = []
        source_rows = 0
        for raw in reader:
            source_rows += 1
            if raw.get("Grain") != "SOYBEANS":
                continue
            normalized = _normalize_yearly_row(raw)
            if date.fromisoformat(normalized["cert_date"]).year != calendar_year:
                raise FgisAdapterError(
                    "FGIS Yearly Cert Date does not match the active calendar year"
                )
            records.append(normalized)
        if not records:
            raise FgisAdapterError("FGIS Yearly CSV contains no exact SOYBEANS rows")
        last_modified = _response_header(response, "Last-Modified")
        source_sha256 = sha256_bytes(content)
        return FgisFetchResult(
            records=records,
            fetch_time_utc=fetched_at,
            dataset_updated_at=_http_timestamp(last_modified),
            pages=[
                {
                    "offset": 0,
                    "row_count": len(records),
                    "source_row_count": source_rows,
                    "response_sha256": source_sha256,
                }
            ],
            query_scope={
                "grain": "SOYBEANS",
                "calendar_year": calendar_year,
                "source_file": source_file,
                "complete_calendar_year_file": True,
            },
            source_channel=FGIS_YEARLY_SOURCE_CHANNEL,
            calendar_year=calendar_year,
            source_file=source_file,
            source_url=source_url,
            source_sha256=source_sha256,
            http_status=response.status_code,
            content_length=declared_length,
            etag=_response_header(response, "ETag"),
            last_modified=last_modified,
            source_bytes=content,
        )


def _response_header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", {})
    if not isinstance(headers, Mapping):
        return None
    value = headers.get(name)
    return str(value) if value is not None else None


def _http_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return iso_utc(parsedate_to_datetime(value))
    except (TypeError, ValueError, OverflowError):
        return value


def _parse_yearly_date(value: Any, *, field_name: str) -> date:
    text = str(value or "").strip()
    if len(text) != 8 or not text.isascii() or not text.isdigit():
        raise FgisAdapterError(f"FGIS Yearly {field_name} must be YYYYMMDD")
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise FgisAdapterError(f"FGIS Yearly {field_name} is invalid") from exc


def _parse_yearly_integer(value: Any, *, field_name: str, required: bool) -> int | None:
    text = str(value or "").strip()
    if not text and not required:
        return None
    if not text.isascii() or not text.isdigit():
        raise FgisAdapterError(f"FGIS Yearly {field_name} must be a non-negative integer")
    return int(text)


def _optional_yearly_text(raw: Mapping[str, Any], field_name: str) -> str | None:
    value = str(raw.get(field_name) or "").strip()
    return value or None


def _normalize_yearly_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    week_ending = _parse_yearly_date(raw.get("Thursday"), field_name="Thursday")
    inspection = _parse_yearly_date(raw.get("Cert Date"), field_name="Cert Date")
    if week_ending.weekday() != 3:
        raise FgisAdapterError("FGIS Yearly Thursday is not a Thursday")
    days_to_week_end = (week_ending - inspection).days
    if not 0 <= days_to_week_end <= 6:
        raise FgisAdapterError(
            "FGIS Yearly Cert Date must fall within its Thursday week-ending bucket"
        )
    destination = str(raw.get("Destination") or "").strip()
    if not destination:
        raise FgisAdapterError("FGIS Yearly Destination must be non-empty")
    mt = _parse_yearly_integer(raw.get("Metric Ton"), field_name="Metric Ton", required=True)
    pounds = _parse_yearly_integer(raw.get("Pounds"), field_name="Pounds", required=False)
    return {
        "date": week_ending.isoformat(),
        "cert_date": inspection.isoformat(),
        "week": str(inspection.isocalendar().week),
        "month": str(inspection.month),
        "quarter": str((inspection.month - 1) // 3 + 1),
        "year": str(inspection.year),
        "type_shipm": _optional_yearly_text(raw, "Type Shipm"),
        "type_carrier": _optional_yearly_text(raw, "Type Carrier"),
        "type_carrier_text": None,
        "carrier_name": _optional_yearly_text(raw, "Carrier Name"),
        "grain": "SOYBEANS",
        "grade": _optional_yearly_text(raw, "Grade"),
        "class": _optional_yearly_text(raw, "Class"),
        "subclass": _optional_yearly_text(raw, "SubClass"),
        "destination": destination,
        "port": _optional_yearly_text(raw, "Port"),
        "ams_reg": _optional_yearly_text(raw, "AMS Reg"),
        "fgis_reg": _optional_yearly_text(raw, "FGIS Reg"),
        "state": _optional_yearly_text(raw, "State"),
        "mt": mt,
        "pounds": pounds,
        "field_office": _optional_yearly_text(raw, "Field Office"),
        "source_mkt_yr": _optional_yearly_text(raw, "MKT YR"),
        "source_serial_no": _optional_yearly_text(raw, "Serial No."),
    }


def _socrata_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    return str(value)


def _market_year_start(value: pd.Timestamp) -> int:
    return int(value.year if value.month >= 9 else value.year - 1)


def normalize_fgis_records(
    records: Sequence[Mapping[str, Any]],
    *,
    batch_id: str,
    raw_snapshot_id: str,
    raw_snapshot_sha256: str,
    fetch_time_utc: str,
    dataset_updated_at: str | None,
    source_dataset_id: str = FGIS_DATASET_ID,
) -> pd.DataFrame:
    if not records:
        raise PipelineError("FGIS returned no soybean records")
    raw = pd.DataFrame([dict(item) for item in records])
    missing = sorted(set(FGIS_RAW_FIELDS) - set(raw.columns))
    if missing:
        raise PipelineError(f"FGIS raw schema missing: {', '.join(missing)}")
    if not raw["grain"].eq("SOYBEANS").all():
        raise PipelineError("FGIS raw records contain a non-SOYBEANS commodity")
    raw["inspection_date"] = pd.to_datetime(raw["cert_date"], errors="raise")
    raw["week_ending_date"] = pd.to_datetime(raw["date"], errors="raise")
    raw["quantity_mt"] = pd.to_numeric(raw["mt"], errors="raise")
    if raw["quantity_mt"].isna().any() or (raw["quantity_mt"] < 0).any():
        raise PipelineError("FGIS mt must be non-negative and non-null")
    if (raw["quantity_mt"] % 1 != 0).any():
        raise PipelineError("FGIS mt must be an integer quantity")
    raw["market_year_start"] = raw["inspection_date"].map(_market_year_start)
    raw["market_year_end"] = raw["market_year_start"] + 1
    raw["destination"] = raw["destination"].astype("string")
    if raw["destination"].isna().any() or raw["destination"].str.strip().eq("").any():
        raise PipelineError("FGIS destination must be non-empty")

    grouped = (
        raw.groupby(
            [
                "market_year_start",
                "market_year_end",
                "week_ending_date",
                "destination",
            ],
            as_index=False,
            observed=True,
        )
        .agg(weekly_mt=("quantity_mt", "sum"), source_row_count=("mt", "size"))
        .sort_values(
            ["market_year_end", "week_ending_date", "destination"],
            kind="stable",
        )
        .reset_index(drop=True)
    )
    week_axis = (
        grouped[["market_year_end", "week_ending_date"]]
        .drop_duplicates()
        .sort_values(["market_year_end", "week_ending_date"])
    )
    week_axis["my_week"] = week_axis.groupby("market_year_end").cumcount() + 1
    grouped = grouped.merge(
        week_axis,
        on=["market_year_end", "week_ending_date"],
        validate="many_to_one",
    )
    grouped = grouped.sort_values(
        ["market_year_end", "destination", "my_week"], kind="stable"
    )
    grouped["weekly_mt"] = grouped["weekly_mt"].astype("int64")
    grouped["source_row_count"] = grouped["source_row_count"].astype("int64")
    grouped["cumulative_mt"] = grouped.groupby(
        ["market_year_end", "destination"], observed=True
    )["weekly_mt"].cumsum()
    grouped["schema_version"] = SCHEMA_VERSION
    grouped["source"] = FGIS_SOURCE
    grouped["source_dataset_id"] = source_dataset_id
    grouped["commodity"] = "SOYBEANS"
    grouped["market_year_label"] = grouped.apply(
        lambda row: f"{int(row.market_year_start)}/{str(int(row.market_year_end))[-2:]}",
        axis=1,
    )
    grouped["destination_role"] = "source_destination"
    grouped["source_fetch_id"] = batch_id
    grouped["batch_id"] = batch_id
    grouped["raw_snapshot_id"] = raw_snapshot_id
    grouped["raw_snapshot_sha256"] = raw_snapshot_sha256
    grouped["source_dataset_updated_at"] = dataset_updated_at
    grouped["fetch_time_utc"] = fetch_time_utc
    result = grouped.loc[:, FGIS_STABLE_COLUMNS].sort_values(
        ["market_year_end", "my_week", "destination"], kind="stable"
    )
    return result.reset_index(drop=True)


def build_fgis_research_view(stable: pd.DataFrame) -> pd.DataFrame:
    validate_fgis_stable(stable)
    group = [
        "market_year_start",
        "market_year_end",
        "market_year_label",
        "my_week",
        "week_ending_date",
    ]
    world = stable.groupby(group, as_index=False, observed=True)["weekly_mt"].sum()
    world = world.rename(columns={"weekly_mt": "world_weekly_mt"})
    china = (
        stable.loc[stable["destination"].eq("CHINA")]
        .groupby(group, as_index=False, observed=True)["weekly_mt"]
        .sum()
        .rename(columns={"weekly_mt": "china_weekly_mt"})
    )
    result = world.merge(china, on=group, how="left", validate="one_to_one")
    result["china_weekly_mt"] = result["china_weekly_mt"].fillna(0).astype("int64")
    result["non_china_weekly_mt"] = (
        result["world_weekly_mt"] - result["china_weekly_mt"]
    )
    if not (
        result["world_weekly_mt"]
        == result["china_weekly_mt"] + result["non_china_weekly_mt"]
    ).all():
        raise PipelineError("FGIS World = China + Non-China closure failed")
    for role in ("world", "china", "non_china"):
        result[f"{role}_cumulative_mt"] = result.groupby(
            "market_year_end", observed=True
        )[f"{role}_weekly_mt"].cumsum()
    result["china_share_pct"] = (
        result["china_weekly_mt"]
        .div(result["world_weekly_mt"].where(result["world_weekly_mt"].ne(0)))
        .mul(100)
    )
    return result.sort_values(["market_year_end", "my_week"]).reset_index(drop=True)


def validate_fgis_stable(stable: pd.DataFrame) -> dict[str, Any]:
    if tuple(stable.columns) != FGIS_STABLE_COLUMNS:
        missing = sorted(set(FGIS_STABLE_COLUMNS) - set(stable.columns))
        extra = sorted(set(stable.columns) - set(FGIS_STABLE_COLUMNS))
        raise PipelineError(f"FGIS stable schema mismatch; missing={missing}, extra={extra}")
    if stable.empty:
        raise PipelineError("FGIS stable candidate is empty")
    required = [column for column in FGIS_STABLE_COLUMNS if column != "source_dataset_updated_at"]
    if stable[required].isna().any().any():
        raise PipelineError("FGIS stable has null required values")
    if stable.duplicated(list(FGIS_KEY)).any():
        raise PipelineError("FGIS stable business key is not unique")
    if not stable["schema_version"].eq(SCHEMA_VERSION).all():
        raise PipelineError("FGIS schema_version mismatch")
    if not stable["source"].eq(FGIS_SOURCE).all():
        raise PipelineError("FGIS source identity mismatch")
    if not stable["commodity"].eq("SOYBEANS").all():
        raise PipelineError("FGIS commodity identity mismatch")
    if not stable["destination_role"].eq("source_destination").all():
        raise PipelineError("FGIS derived destinations cannot enter stable")
    if (stable[["weekly_mt", "cumulative_mt", "source_row_count"]] < 0).any().any():
        raise PipelineError("FGIS quantities and row counts must be non-negative")
    for market_year, annual in stable.groupby("market_year_end", observed=True):
        axis = (
            annual[["my_week", "week_ending_date"]]
            .drop_duplicates()
            .sort_values("my_week")
        )
        expected = list(range(1, len(axis) + 1))
        if axis["my_week"].tolist() != expected:
            raise PipelineError(f"FGIS MY week sequence is invalid for {market_year}")
        if not axis["week_ending_date"].is_monotonic_increasing:
            raise PipelineError(f"FGIS week-ending order is invalid for {market_year}")
    ordered = stable.sort_values(["market_year_end", "destination", "my_week"])
    expected_cumulative = ordered.groupby(
        ["market_year_end", "destination"], observed=True
    )["weekly_mt"].cumsum()
    if not expected_cumulative.equals(ordered["cumulative_mt"]):
        raise PipelineError("FGIS cumulative_mt validation failed")
    research = build_fgis_research_view_without_validation(stable)
    if not (
        research["world_weekly_mt"]
        == research["china_weekly_mt"] + research["non_china_weekly_mt"]
    ).all():
        raise PipelineError("FGIS research closure validation failed")
    return {
        "passed": True,
        "row_count": len(stable),
        "market_year_count": int(stable["market_year_end"].nunique()),
        "latest_week": stable["week_ending_date"].max().date().isoformat(),
    }


def build_fgis_research_view_without_validation(stable: pd.DataFrame) -> pd.DataFrame:
    group = [
        "market_year_start",
        "market_year_end",
        "market_year_label",
        "my_week",
        "week_ending_date",
    ]
    world = stable.groupby(group, as_index=False, observed=True)["weekly_mt"].sum()
    world = world.rename(columns={"weekly_mt": "world_weekly_mt"})
    china = (
        stable.loc[stable["destination"].eq("CHINA")]
        .groupby(group, as_index=False, observed=True)["weekly_mt"]
        .sum()
        .rename(columns={"weekly_mt": "china_weekly_mt"})
    )
    result = world.merge(china, on=group, how="left", validate="one_to_one")
    result["china_weekly_mt"] = result["china_weekly_mt"].fillna(0).astype("int64")
    result["non_china_weekly_mt"] = result["world_weekly_mt"] - result["china_weekly_mt"]
    return result


def detect_fgis_revisions(
    old: pd.DataFrame | None,
    new: pd.DataFrame,
    *,
    detected_at_utc: str,
    allowed_keys: set[tuple[Any, ...]] | None = None,
) -> pd.DataFrame:
    columns = [
        "schema_version",
        "source",
        "business_key_json",
        "field_name",
        "metric_unit",
        "old_value",
        "new_value",
        "delta",
        "old_snapshot_identity",
        "new_snapshot_identity",
        "first_seen_fetch",
        "revised_fetch",
        "detected_at_utc",
    ]
    if old is None or old.empty:
        return pd.DataFrame(columns=columns)
    merged = old.merge(new, on=list(FGIS_KEY), how="inner", suffixes=("_old", "_new"))
    if allowed_keys is not None:
        merged = merged.loc[
            merged.apply(
                lambda row: tuple(row[name] for name in FGIS_KEY) in allowed_keys,
                axis=1,
            )
        ]
    revisions: list[dict[str, Any]] = []
    for field in FGIS_REVISION_FIELDS:
        changed = merged.loc[merged[f"{field}_old"].ne(merged[f"{field}_new"])]
        for row in changed.itertuples(index=False):
            key = {name: getattr(row, name) for name in FGIS_KEY}
            old_value = getattr(row, f"{field}_old")
            new_value = getattr(row, f"{field}_new")
            revisions.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "source": FGIS_SOURCE,
                    "business_key_json": json.dumps(key, default=str, sort_keys=True),
                    "field_name": field,
                    "metric_unit": "MT" if field != "source_row_count" else "rows",
                    "old_value": old_value,
                    "new_value": new_value,
                    "delta": new_value - old_value,
                    "old_snapshot_identity": getattr(row, "raw_snapshot_sha256_old"),
                    "new_snapshot_identity": getattr(row, "raw_snapshot_sha256_new"),
                    "first_seen_fetch": getattr(row, "source_fetch_id_old"),
                    "revised_fetch": getattr(row, "source_fetch_id_new"),
                    "detected_at_utc": detected_at_utc,
                }
            )
    return pd.DataFrame(revisions, columns=columns)


def _yearly_dataset_identity(fetch: FgisFetchResult) -> str:
    if fetch.source_channel != FGIS_YEARLY_SOURCE_CHANNEL or not fetch.source_file:
        return FGIS_DATASET_ID
    return f"{FGIS_YEARLY_SOURCE_CHANNEL}:{fetch.source_file}"


def _empty_fgis_stable() -> pd.DataFrame:
    return pd.DataFrame(columns=FGIS_STABLE_COLUMNS)


def _read_raw_records(snapshot_dir: str) -> list[dict[str, Any]]:
    path = Path(snapshot_dir) / "records.jsonl.gz"
    if not path.is_file():
        raise PipelineError("Previous FGIS Yearly raw snapshot is unavailable")
    records: list[dict[str, Any]] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="strict", newline="") as handle:
            for line in handle:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise PipelineError("Previous FGIS Yearly raw record is not an object")
                records.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError("Previous FGIS Yearly raw snapshot is unreadable") from exc
    return records


def _previous_yearly_active(
    *,
    previous_success: Mapping[str, Any] | None,
    old_stable: pd.DataFrame | None,
    calendar_year: int,
) -> tuple[pd.DataFrame, str]:
    if (
        previous_success
        and previous_success.get("source_channel") == FGIS_YEARLY_SOURCE_CHANNEL
        and previous_success.get("calendar_year") == calendar_year
    ):
        records = _read_raw_records(str(previous_success.get("raw_snapshot_dir") or ""))
        previous = normalize_fgis_records(
            records,
            batch_id=str(previous_success["batch_id"]),
            raw_snapshot_id=str(previous_success["batch_id"]),
            raw_snapshot_sha256=str(previous_success["raw_snapshot_sha256"]),
            fetch_time_utc=str(previous_success["fetch_time_utc"]),
            dataset_updated_at=previous_success.get("source_dataset_updated_at"),
            source_dataset_id=str(previous_success["source_dataset_id"]),
        )
        return previous, "previous_observed_current_calendar_year"
    if (
        previous_success
        and previous_success.get("source_channel") == FGIS_YEARLY_SOURCE_CHANNEL
        and previous_success.get("calendar_year") != calendar_year
    ):
        return _empty_fgis_stable(), "calendar_year_switch"
    if old_stable is not None and not old_stable.empty:
        return _empty_fgis_stable(), "initial_yearly_observation_against_frozen_baseline"
    return _empty_fgis_stable(), "no_historical_baseline"


def _merge_frozen_baseline_with_active_year(
    old_stable: pd.DataFrame | None,
    previous_active: pd.DataFrame,
    current_active: pd.DataFrame,
    *,
    batch_id: str,
    raw_snapshot_sha256: str,
    fetch_time_utc: str,
    dataset_updated_at: str | None,
) -> pd.DataFrame:
    if old_stable is None or old_stable.empty:
        merged = current_active.copy()
    else:
        merged = old_stable.copy().reset_index(drop=True)
        merged["week_ending_date"] = pd.to_datetime(merged["week_ending_date"])
        previous_lookup = {
            tuple(row[name] for name in FGIS_KEY): row
            for _, row in previous_active.iterrows()
        }
        current_lookup = {
            tuple(row[name] for name in FGIS_KEY): row
            for _, row in current_active.iterrows()
        }
        index_lookup = {
            tuple(getattr(row, name) for name in FGIS_KEY): index
            for index, row in enumerate(merged.itertuples(index=False))
        }
        drop_indexes: list[int] = []
        additions: list[dict[str, Any]] = []
        for key in sorted(set(previous_lookup) | set(current_lookup), key=str):
            previous_row = previous_lookup.get(key)
            current_row = current_lookup.get(key)
            previous_mt = int(previous_row["weekly_mt"]) if previous_row is not None else 0
            previous_count = int(previous_row["source_row_count"]) if previous_row is not None else 0
            current_mt = int(current_row["weekly_mt"]) if current_row is not None else 0
            current_count = int(current_row["source_row_count"]) if current_row is not None else 0
            if previous_mt == current_mt and previous_count == current_count:
                continue
            existing_index = index_lookup.get(key)
            if existing_index is None:
                if previous_mt or previous_count:
                    raise PipelineError("Frozen FGIS baseline is missing a previously observed active key")
                if current_row is None:
                    continue
                additions.append(current_row.to_dict())
                continue
            existing_mt = int(merged.at[existing_index, "weekly_mt"])
            existing_count = int(merged.at[existing_index, "source_row_count"])
            residual_mt = existing_mt - previous_mt
            residual_count = existing_count - previous_count
            if residual_mt < 0 or residual_count < 0:
                raise PipelineError("Active Yearly facts exceed the frozen FGIS baseline")
            combined_mt = residual_mt + current_mt
            combined_count = residual_count + current_count
            if combined_mt == 0 and combined_count == 0:
                drop_indexes.append(existing_index)
                continue
            if combined_count <= 0:
                raise PipelineError("FGIS merged source_row_count must remain positive")
            merged.at[existing_index, "weekly_mt"] = combined_mt
            merged.at[existing_index, "source_row_count"] = combined_count
            merged.at[existing_index, "source_dataset_id"] = (
                "historical_baseline+yearly_export_grain_csv"
                if residual_mt or residual_count
                else current_row["source_dataset_id"]
            )
            merged.at[existing_index, "source_fetch_id"] = batch_id
            merged.at[existing_index, "batch_id"] = batch_id
            merged.at[existing_index, "raw_snapshot_id"] = batch_id
            merged.at[existing_index, "raw_snapshot_sha256"] = raw_snapshot_sha256
            merged.at[existing_index, "source_dataset_updated_at"] = dataset_updated_at
            merged.at[existing_index, "fetch_time_utc"] = fetch_time_utc
        if drop_indexes:
            merged = merged.drop(index=drop_indexes)
        if additions:
            additions_frame = pd.DataFrame(additions).loc[:, FGIS_STABLE_COLUMNS]
            merged = pd.concat([merged, additions_frame], ignore_index=True)

    if merged.empty:
        raise PipelineError("FGIS unified stable cannot be empty")
    merged["week_ending_date"] = pd.to_datetime(merged["week_ending_date"])
    merged["market_year_start"] = pd.to_numeric(merged["market_year_end"]) - 1
    merged["market_year_label"] = merged["market_year_end"].map(
        lambda end: f"{int(end) - 1}/{str(int(end))[-2:]}"
    )
    week_axis = (
        merged[["market_year_end", "week_ending_date"]]
        .drop_duplicates()
        .sort_values(["market_year_end", "week_ending_date"])
    )
    week_axis["my_week"] = week_axis.groupby("market_year_end").cumcount() + 1
    merged = merged.drop(columns="my_week").merge(
        week_axis,
        on=["market_year_end", "week_ending_date"],
        validate="many_to_one",
    )
    merged = merged.sort_values(
        ["market_year_end", "destination", "my_week"], kind="stable"
    )
    merged["weekly_mt"] = pd.to_numeric(merged["weekly_mt"]).astype("int64")
    merged["source_row_count"] = pd.to_numeric(merged["source_row_count"]).astype("int64")
    merged["cumulative_mt"] = merged.groupby(
        ["market_year_end", "destination"], observed=True
    )["weekly_mt"].cumsum()
    return (
        merged.loc[:, FGIS_STABLE_COLUMNS]
        .sort_values(["market_year_end", "my_week", "destination"], kind="stable")
        .reset_index(drop=True)
    )


def _active_change_summary(previous: pd.DataFrame, current: pd.DataFrame) -> dict[str, int]:
    columns = [*FGIS_KEY, "weekly_mt", "source_row_count"]
    old = previous.loc[:, columns].rename(
        columns={"weekly_mt": "weekly_mt_old", "source_row_count": "source_row_count_old"}
    )
    new = current.loc[:, columns].rename(
        columns={"weekly_mt": "weekly_mt_new", "source_row_count": "source_row_count_new"}
    )
    compared = old.merge(new, on=list(FGIS_KEY), how="outer", indicator=True)
    for name in ("weekly_mt_old", "weekly_mt_new", "source_row_count_old", "source_row_count_new"):
        compared[name] = compared[name].fillna(0).astype("int64")
    revised = compared.loc[
        compared["_merge"].eq("both")
        & (
            compared["weekly_mt_old"].ne(compared["weekly_mt_new"])
            | compared["source_row_count_old"].ne(compared["source_row_count_new"])
        )
    ]
    old_weeks = set(pd.to_datetime(previous["week_ending_date"]).dt.date) if not previous.empty else set()
    new_weeks = set(pd.to_datetime(current["week_ending_date"]).dt.date) if not current.empty else set()
    return {
        "new_business_keys": int(compared["_merge"].eq("right_only").sum()),
        "removed_business_keys": int(compared["_merge"].eq("left_only").sum()),
        "revised_business_keys": int(len(revised)),
        "new_weeks": len(new_weeks - old_weeks),
        "added_source_rows": int(
            (compared["source_row_count_new"] - compared["source_row_count_old"])
            .clip(lower=0)
            .sum()
        ),
        "removed_source_rows": int(
            (compared["source_row_count_old"] - compared["source_row_count_new"])
            .clip(lower=0)
            .sum()
        ),
    }


def _active_revision_keys(
    previous: pd.DataFrame, current: pd.DataFrame
) -> set[tuple[Any, ...]]:
    columns = [*FGIS_KEY, "weekly_mt", "source_row_count"]
    old = previous.loc[:, columns].rename(
        columns={"weekly_mt": "weekly_mt_old", "source_row_count": "source_row_count_old"}
    )
    new = current.loc[:, columns].rename(
        columns={"weekly_mt": "weekly_mt_new", "source_row_count": "source_row_count_new"}
    )
    compared = old.merge(new, on=list(FGIS_KEY), how="inner")
    changed = compared.loc[
        compared["weekly_mt_old"].ne(compared["weekly_mt_new"])
        | compared["source_row_count_old"].ne(compared["source_row_count_new"])
    ]
    return {
        tuple(row[name] for name in FGIS_KEY)
        for _, row in changed.iterrows()
    }


def fgis_paths(runtime_root: Path, batch_id: str) -> dict[str, Path]:
    processed = runtime_root / "01_data" / "processed" / "soybean_export_inspections"
    return {
        "raw_root": runtime_root / "01_data" / "raw",
        "candidate_dir": runtime_root / "01_data" / "candidates" / "soybean_export_inspections" / batch_id,
        "stable": processed / "soybean_export_inspections_weekly.parquet",
        "stable_manifest": processed / "soybean_export_inspections_weekly.manifest.json",
        "revision": processed / "revisions" / "soybean_export_inspections_revisions.parquet",
        "backup_root": runtime_root / "01_data" / "backups" / "soybean_export_inspections",
        "status": runtime_root / "01_data" / "update_status" / "soybean_export_inspections.json",
    }


def _publish_yearly_manifest_only(
    *,
    manifest: Mapping[str, Any],
    stable_path: Path,
    stable_manifest_path: Path,
    backup_root: Path,
    batch_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not stable_path.is_file():
        raise PipelineError("Cannot publish Yearly provenance without an existing stable")
    backup_dir = backup_root / batch_id
    backup_dir.mkdir(parents=True, exist_ok=False)
    if stable_manifest_path.exists():
        atomic_copy(stable_manifest_path, backup_dir / stable_manifest_path.name)
    stable_manifest = {
        **dict(manifest),
        "path": str(stable_path),
        "byte_size": stable_path.stat().st_size,
        "sha256": sha256_file(stable_path),
        "stable_path": str(stable_path),
        "published_batch_id": batch_id,
        "stable_content_changed": False,
    }
    write_json_atomic(stable_manifest_path, stable_manifest)
    return (
        {
            "status": "source_updated_no_business_change",
            "published": False,
            "backup_dir": str(backup_dir),
        },
        stable_manifest,
    )


def run_fgis_pipeline(
    *,
    runtime_root: Path,
    adapter: FgisAdapter | Any,
    git_head: str,
    batch_id: str | None = None,
    cert_date_start: date | None = None,
    cert_date_end: date | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    if not _valid_git_head(git_head):
        raise PipelineError("git_head must be a full 40-character hexadecimal SHA")
    selected_batch = batch_id or make_batch_id()
    paths = fgis_paths(Path(runtime_root), selected_batch)
    started = iso_utc(utc_now())
    previous_success = read_last_success(paths["status"])
    try:
        fetched: FgisFetchResult = adapter.fetch_soybeans(
            cert_date_start=cert_date_start, cert_date_end=cert_date_end
        )
        is_yearly = fetched.source_channel == FGIS_YEARLY_SOURCE_CHANNEL
        if is_yearly:
            if not all(
                (
                    fetched.calendar_year,
                    fetched.source_file,
                    fetched.source_url,
                    fetched.source_sha256,
                    fetched.http_status == 200,
                    fetched.source_bytes is not None,
                )
            ):
                raise PipelineError("FGIS Yearly fetch identity is incomplete")
            if sha256_bytes(fetched.source_bytes or b"") != fetched.source_sha256:
                raise PipelineError("FGIS Yearly source SHA-256 identity mismatch")
            if fetched.content_length != len(fetched.source_bytes or b""):
                raise PipelineError("FGIS Yearly source byte length identity mismatch")
            if (
                previous_success
                and previous_success.get("source_channel") == FGIS_YEARLY_SOURCE_CHANNEL
                and previous_success.get("calendar_year") == fetched.calendar_year
                and previous_success.get("source_sha256") == fetched.source_sha256
            ):
                previous_stable = Path(str(previous_success.get("stable_path") or ""))
                previous_raw = Path(str(previous_success.get("raw_snapshot_dir") or "")) / str(
                    previous_success.get("source_file") or ""
                )
                if (
                    not previous_stable.is_file()
                    or sha256_file(previous_stable) != previous_success.get("stable_sha256")
                ):
                    raise PipelineError("FGIS Yearly no-change stable identity is unavailable")
                if (
                    not previous_raw.is_file()
                    or sha256_file(previous_raw) != previous_success.get("source_sha256")
                ):
                    raise PipelineError("FGIS Yearly no-change raw source identity is unavailable")
                completed = iso_utc(utc_now())
                audit = {
                    "schema_version": SCHEMA_VERSION,
                    "source": FGIS_SOURCE,
                    "source_authority": FGIS_SOURCE_AUTHORITY,
                    "source_channel": FGIS_YEARLY_SOURCE_CHANNEL,
                    "calendar_year": fetched.calendar_year,
                    "source_file": fetched.source_file,
                    "source_url": fetched.source_url,
                    "source_sha256": fetched.source_sha256,
                    "batch_id": selected_batch,
                    "git_head": git_head,
                    "status": "no_change",
                    "published": False,
                    "backup_dir": None,
                    "raw_snapshot_dir": None,
                    "candidate_path": None,
                    "revision_count": 0,
                    "source_change": {
                        "sha256_changed": False,
                        "processing_skipped": True,
                    },
                    "latest": previous_success.get("latest"),
                    "latest_attempt": {
                        "batch_id": selected_batch,
                        "started_at_utc": started,
                        "completed_at_utc": completed,
                        "status": "no_change",
                        "published": False,
                    },
                    "last_success": previous_success,
                }
                write_json_atomic(paths["status"], audit)
                return audit

        dataset_identity = _yearly_dataset_identity(fetched)
        raw_manifest = write_raw_snapshot(
            raw_root=paths["raw_root"],
            pipeline_name="soybean_export_inspections",
            batch_id=selected_batch,
            records=fetched.records,
            manifest={
                "source": FGIS_SOURCE,
                "source_authority": fetched.source_authority,
                "source_channel": fetched.source_channel,
                "source_dataset_id": dataset_identity,
                "calendar_year": fetched.calendar_year,
                "source_file": fetched.source_file,
                "source_url": fetched.source_url,
                "source_sha256": fetched.source_sha256,
                "http_status": fetched.http_status,
                "content_length": fetched.content_length,
                "etag": fetched.etag,
                "last_modified": fetched.last_modified,
                "fetch_time_utc": fetched.fetch_time_utc,
                "query_scope": fetched.query_scope,
                "pages": fetched.pages,
                "source_dataset_updated_at": fetched.dataset_updated_at,
                "git_head": git_head,
                "source_release_time_raw": None,
                "initial_value_known": False,
            },
            source_file_name=fetched.source_file if is_yearly else None,
            source_file_bytes=fetched.source_bytes if is_yearly else None,
        )
        active = normalize_fgis_records(
            fetched.records,
            batch_id=selected_batch,
            raw_snapshot_id=selected_batch,
            raw_snapshot_sha256=raw_manifest["raw_file_sha256"],
            fetch_time_utc=fetched.fetch_time_utc,
            dataset_updated_at=fetched.dataset_updated_at,
            source_dataset_id=dataset_identity,
        )
        old = pd.read_parquet(paths["stable"]) if paths["stable"].exists() else None
        previous_active = _empty_fgis_stable()
        active_change = None
        baseline_mode = None
        if is_yearly:
            if (
                old is not None
                and not old.empty
                and pd.to_datetime(active["week_ending_date"]).max()
                < pd.to_datetime(old["week_ending_date"]).max()
            ):
                raise PipelineError("FGIS Yearly active source is older than the frozen baseline")
            previous_active, baseline_mode = _previous_yearly_active(
                previous_success=previous_success,
                old_stable=old,
                calendar_year=int(fetched.calendar_year),
            )
            active_change = _active_change_summary(previous_active, active)
            candidate = _merge_frozen_baseline_with_active_year(
                old,
                previous_active,
                active,
                batch_id=selected_batch,
                raw_snapshot_sha256=raw_manifest["raw_file_sha256"],
                fetch_time_utc=fetched.fetch_time_utc,
                dataset_updated_at=fetched.dataset_updated_at,
            )
        else:
            candidate = active
        validation = validate_fgis_stable(candidate)
        generated_at = iso_utc(utc_now())
        stable_relative_path = (
            "01_data/processed/soybean_export_inspections/"
            "soybean_export_inspections_weekly.parquet"
        )
        candidate_path = paths["candidate_dir"] / "soybean_export_inspections_weekly.parquet"
        candidate_identity = write_parquet_atomic(
            candidate,
            candidate_path,
            metadata={
                "schema_version": SCHEMA_VERSION,
                "source": FGIS_SOURCE,
                "source_authority": fetched.source_authority,
                "source_channel": fetched.source_channel,
                "batch_id": selected_batch,
                "git_head": git_head,
                "raw_snapshot_sha256": raw_manifest["raw_file_sha256"],
                "quality_status": "passed",
                "generated_at_utc": generated_at,
                "source_latest_week": validation["latest_week"],
                "source_dataset_updated_at": fetched.dataset_updated_at,
                "stable_relative_path": stable_relative_path,
                "generator_version": SCHEMA_VERSION,
            },
        )
        manifest = {
            **candidate_identity,
            "schema_version": SCHEMA_VERSION,
            "source": FGIS_SOURCE,
            "source_authority": fetched.source_authority,
            "source_channel": fetched.source_channel,
            "calendar_year": fetched.calendar_year,
            "source_file": fetched.source_file,
            "source_url": fetched.source_url,
            "source_sha256": fetched.source_sha256,
            "http_status": fetched.http_status,
            "content_length": fetched.content_length,
            "etag": fetched.etag,
            "last_modified": fetched.last_modified,
            "generated_at_utc": generated_at,
            "fetch_started_at_utc": started,
            "fetch_completed_at_utc": fetched.fetch_time_utc,
            "batch_id": selected_batch,
            "source_latest_week": validation["latest_week"],
            "source_release_time_raw": None,
            "source_release_timezone": None,
            "source_dataset_updated_at": fetched.dataset_updated_at,
            "stable_relative_path": stable_relative_path,
            "raw_snapshot_id": selected_batch,
            "raw_snapshot_sha256": raw_manifest["raw_file_sha256"],
            "raw_manifest_sha256": raw_manifest["manifest_sha256"],
            "git_head": git_head,
            "generator_version": SCHEMA_VERSION,
            "fetch_scope": fetched.query_scope,
            "revision_coverage": {
                "mode": (
                    "current_calendar_year_observed_changes_only"
                    if is_yearly
                    else "candidate_vs_current"
                ),
                "calendar_year": fetched.calendar_year,
                "cert_date_start": fetched.query_scope.get("cert_date_start"),
                "cert_date_end": fetched.query_scope.get("cert_date_end"),
                "tracks_prior_calendar_year_revisions": False if is_yearly else None,
            },
            "historical_baseline_provenance": (
                {
                    "strategy": "frozen_historical_baseline",
                    "source_authority": FGIS_SOURCE_AUTHORITY,
                    "source_channels": sorted(
                        {
                            FGIS_SOCRATA_SOURCE_CHANNEL
                            if value == FGIS_DATASET_ID
                            else FGIS_YEARLY_SOURCE_CHANNEL
                            for value in (
                                set(old["source_dataset_id"].astype(str))
                                if old is not None
                                else set()
                            )
                        }
                    ),
                    "source_dataset_ids": sorted(
                        set(old["source_dataset_id"].astype(str)) if old is not None else set()
                    ),
                    "stable_sha256_before_run": sha256_file(paths["stable"])
                    if paths["stable"].exists()
                    else None,
                    "baseline_mode": baseline_mode,
                    "old_calendar_year_revisions_tracked": False,
                }
                if is_yearly
                else None
            ),
            "active_update_provenance": (
                {
                    "source_authority": FGIS_SOURCE_AUTHORITY,
                    "source_channel": FGIS_YEARLY_SOURCE_CHANNEL,
                    "calendar_year": fetched.calendar_year,
                    "source_file": fetched.source_file,
                    "source_url": fetched.source_url,
                    "source_sha256": fetched.source_sha256,
                    "etag": fetched.etag,
                    "last_modified": fetched.last_modified,
                    "fetch_time_utc": fetched.fetch_time_utc,
                    "change_summary": active_change,
                }
                if is_yearly
                else None
            ),
            "quality_status": "passed",
            "quality": validation,
        }
        write_json_atomic(paths["candidate_dir"] / "manifest.json", manifest, overwrite=False)
        revisions = detect_fgis_revisions(
            old,
            candidate,
            detected_at_utc=iso_utc(utc_now()),
            allowed_keys=(
                _active_revision_keys(previous_active, active) if is_yearly else None
            ),
        )
        publication = {"status": "candidate_only", "published": False, "backup_dir": None}
        if publish:
            if old is not None and _business_equal(old, candidate):
                if is_yearly:
                    publication, stable_manifest = _publish_yearly_manifest_only(
                        manifest=manifest,
                        stable_path=paths["stable"],
                        stable_manifest_path=paths["stable_manifest"],
                        backup_root=paths["backup_root"],
                        batch_id=selected_batch,
                    )
                    manifest = stable_manifest
                else:
                    publication = {
                        "status": "no_change",
                        "published": False,
                        "backup_dir": None,
                    }
            else:
                publication = publish_candidate(
                    candidate_path=candidate_path,
                    candidate_manifest=manifest,
                    stable_path=paths["stable"],
                    stable_manifest_path=paths["stable_manifest"],
                    backup_root=paths["backup_root"],
                    batch_id=selected_batch,
                )
            if publication["published"]:
                append_revisions(
                    paths["revision"],
                    revisions,
                    metadata={
                        "schema_version": SCHEMA_VERSION,
                        "source": FGIS_SOURCE,
                        "batch_id": selected_batch,
                    },
                )
        research = build_fgis_research_view(candidate)
        latest = research.sort_values("week_ending_date").iloc[-1]
        completed = iso_utc(utc_now())
        latest_attempt = {
            "batch_id": selected_batch,
            "started_at_utc": started,
            "completed_at_utc": completed,
            "status": publication["status"],
            "published": publication["published"],
        }
        last_success = previous_success
        if publish and (publication["published"] or is_yearly):
            last_success = {
                "batch_id": selected_batch,
                "completed_at_utc": completed,
                "status": publication["status"],
                "stable_path": str(paths["stable"]),
                "stable_manifest_path": str(paths["stable_manifest"]),
                "stable_sha256": (
                    manifest["sha256"]
                    if publication["published"]
                    else sha256_file(paths["stable"])
                ),
                "source_latest_week": validation["latest_week"],
                "source_authority": fetched.source_authority,
                "source_channel": fetched.source_channel,
                "source_dataset_id": dataset_identity,
                "calendar_year": fetched.calendar_year,
                "source_file": fetched.source_file,
                "source_url": fetched.source_url,
                "source_sha256": fetched.source_sha256,
                "etag": fetched.etag,
                "last_modified": fetched.last_modified,
                "fetch_time_utc": fetched.fetch_time_utc,
                "source_dataset_updated_at": fetched.dataset_updated_at,
                "raw_snapshot_dir": raw_manifest["snapshot_dir"],
                "raw_snapshot_sha256": raw_manifest["raw_file_sha256"],
                "latest": {
                    "week_ending_date": latest["week_ending_date"].date().isoformat(),
                    "market_year_end": int(latest["market_year_end"]),
                    "my_week": int(latest["my_week"]),
                    "world_mt": int(latest["world_weekly_mt"]),
                    "china_mt": int(latest["china_weekly_mt"]),
                    "non_china_mt": int(latest["non_china_weekly_mt"]),
                },
            }
        audit = {
            "schema_version": SCHEMA_VERSION,
            "source": FGIS_SOURCE,
            "source_authority": fetched.source_authority,
            "source_channel": fetched.source_channel,
            "calendar_year": fetched.calendar_year,
            "source_file": fetched.source_file,
            "source_url": fetched.source_url,
            "source_sha256": fetched.source_sha256,
            "batch_id": selected_batch,
            "git_head": git_head,
            "status": publication["status"],
            "published": publication["published"],
            "backup_dir": publication["backup_dir"],
            "raw_snapshot_dir": raw_manifest["snapshot_dir"],
            "candidate_path": str(candidate_path),
            "stable_path": str(paths["stable"]),
            "revision_count": len(revisions),
            "source_change": {
                "sha256_changed": True if is_yearly else None,
                "processing_skipped": False,
                "active_calendar_year": active_change,
            },
            "validation": validation,
            "latest": {
                "week_ending_date": latest["week_ending_date"].date().isoformat(),
                "market_year_end": int(latest["market_year_end"]),
                "my_week": int(latest["my_week"]),
                "world_mt": int(latest["world_weekly_mt"]),
                "china_mt": int(latest["china_weekly_mt"]),
                "non_china_mt": int(latest["non_china_weekly_mt"]),
            },
            "latest_attempt": latest_attempt,
            "last_success": last_success,
        }
        write_json_atomic(paths["status"], audit)
        return audit
    except Exception as exc:
        failure = {
            "schema_version": SCHEMA_VERSION,
            "source": FGIS_SOURCE,
            "batch_id": selected_batch,
            "git_head": git_head,
            "status": "failed",
            "published": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "attempted_at_utc": started,
            "latest_attempt": {
                "batch_id": selected_batch,
                "started_at_utc": started,
                "completed_at_utc": iso_utc(utc_now()),
                "status": "failed",
                "published": False,
                "error_type": type(exc).__name__,
            },
            "last_success": previous_success,
        }
        write_json_atomic(paths["status"], failure)
        raise


def _valid_git_head(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdefABCDEF" for character in value)


def _business_equal(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    left_view = left.loc[:, FGIS_BUSINESS_COLUMNS].sort_values(list(FGIS_KEY)).reset_index(drop=True)
    right_view = right.loc[:, FGIS_BUSINESS_COLUMNS].sort_values(list(FGIS_KEY)).reset_index(drop=True)
    return left_view.equals(right_view)
