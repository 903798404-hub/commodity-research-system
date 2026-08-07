from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import requests

from .common import (
    SCHEMA_VERSION,
    PipelineError,
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
FGIS_DATASET_ID = "sruw-w49i"
FGIS_RESOURCE_URL = f"https://agtransport.usda.gov/resource/{FGIS_DATASET_ID}.json"
FGIS_METADATA_URL = f"https://agtransport.usda.gov/api/views/{FGIS_DATASET_ID}"
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
FGIS_NULLABLE_RAW_FIELDS = {"carrier_name", "grade", "class", "subclass", "state"}
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


@dataclass(frozen=True)
class FgisFetchResult:
    records: list[dict[str, Any]]
    fetch_time_utc: str
    dataset_updated_at: str | None
    pages: list[dict[str, Any]]
    query_scope: dict[str, Any]


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
        )


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
    grouped["source_dataset_id"] = FGIS_DATASET_ID
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
        raw_manifest = write_raw_snapshot(
            raw_root=paths["raw_root"],
            pipeline_name="soybean_export_inspections",
            batch_id=selected_batch,
            records=fetched.records,
            manifest={
                "source": FGIS_SOURCE,
                "source_dataset_id": FGIS_DATASET_ID,
                "fetch_time_utc": fetched.fetch_time_utc,
                "query_scope": fetched.query_scope,
                "pages": fetched.pages,
                "source_dataset_updated_at": fetched.dataset_updated_at,
                "git_head": git_head,
                "source_release_time_raw": None,
                "initial_value_known": False,
            },
        )
        candidate = normalize_fgis_records(
            fetched.records,
            batch_id=selected_batch,
            raw_snapshot_id=selected_batch,
            raw_snapshot_sha256=raw_manifest["raw_file_sha256"],
            fetch_time_utc=fetched.fetch_time_utc,
            dataset_updated_at=fetched.dataset_updated_at,
        )
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
                "mode": "candidate_vs_current",
                "cert_date_start": fetched.query_scope.get("cert_date_start"),
                "cert_date_end": fetched.query_scope.get("cert_date_end"),
            },
            "quality_status": "passed",
            "quality": validation,
        }
        write_json_atomic(paths["candidate_dir"] / "manifest.json", manifest, overwrite=False)
        old = pd.read_parquet(paths["stable"]) if paths["stable"].exists() else None
        revisions = detect_fgis_revisions(
            old, candidate, detected_at_utc=iso_utc(utc_now())
        )
        publication = {"status": "candidate_only", "published": False, "backup_dir": None}
        if publish:
            if old is not None and _business_equal(old, candidate):
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
        if publication["published"]:
            last_success = {
                "batch_id": selected_batch,
                "completed_at_utc": completed,
                "status": publication["status"],
                "stable_path": str(paths["stable"]),
                "stable_manifest_path": str(paths["stable_manifest"]),
                "stable_sha256": manifest["sha256"],
                "source_latest_week": validation["latest_week"],
            }
        audit = {
            "schema_version": SCHEMA_VERSION,
            "source": FGIS_SOURCE,
            "batch_id": selected_batch,
            "git_head": git_head,
            "status": publication["status"],
            "published": publication["published"],
            "backup_dir": publication["backup_dir"],
            "raw_snapshot_dir": raw_manifest["snapshot_dir"],
            "candidate_path": str(candidate_path),
            "stable_path": str(paths["stable"]),
            "revision_count": len(revisions),
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
