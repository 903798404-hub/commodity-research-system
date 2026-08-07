from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import requests

from .common import (
    SCHEMA_VERSION,
    PipelineError,
    append_revisions,
    canonical_json_bytes,
    iso_utc,
    make_batch_id,
    publish_candidate,
    read_last_success,
    sha256_bytes,
    utc_now,
    write_json_atomic,
    write_parquet_atomic,
    write_raw_snapshot,
)


FAS_SOURCE = "usda_fas_esr"
FAS_BASE_URL = "https://api.fas.usda.gov"
FAS_API_KEY_ENV = "FAS_EXPORT_SALES_API_KEY"
FAS_DEVELOPMENT_FALLBACK_ENV = "USDA_API_KEY"
FAS_COMMODITY_CODE = 801
FAS_UNIT_ID = 1
FAS_RAW_FIELDS = (
    "commodityCode",
    "countryCode",
    "weeklyExports",
    "accumulatedExports",
    "outstandingSales",
    "grossNewSales",
    "currentMYNetSales",
    "currentMYTotalCommitment",
    "nextMYOutstandingSales",
    "nextMYNetSales",
    "unitId",
    "weekEndingDate",
)
FAS_METRIC_MAP = {
    "weeklyExports": "weekly_exports_mt",
    "accumulatedExports": "accumulated_exports_mt",
    "outstandingSales": "outstanding_sales_mt",
    "grossNewSales": "gross_new_sales_mt",
    "currentMYNetSales": "current_my_net_sales_mt",
    "currentMYTotalCommitment": "current_my_total_commitment_mt",
    "nextMYOutstandingSales": "next_my_outstanding_sales_mt",
    "nextMYNetSales": "next_my_net_sales_mt",
}
FAS_CLOSURE_METRICS = (
    "weekly_exports_mt",
    "accumulated_exports_mt",
    "outstanding_sales_mt",
    "current_my_net_sales_mt",
    "current_my_total_commitment_mt",
    "next_my_outstanding_sales_mt",
    "next_my_net_sales_mt",
)
FAS_STABLE_COLUMNS = (
    "schema_version",
    "source",
    "commodity_code",
    "commodity_name",
    "country_code",
    "country_name",
    "country_description",
    "country_genc_code",
    "unit_id",
    "unit_name",
    "report_market_year_start",
    "report_market_year_end",
    "report_market_year_label",
    "report_week",
    "week_ending_date",
    "current_target_market_year_end",
    "next_target_market_year_end",
    "weekly_exports_mt",
    "accumulated_exports_mt",
    "outstanding_sales_mt",
    "gross_new_sales_mt",
    "current_my_net_sales_mt",
    "current_my_total_commitment_mt",
    "next_my_outstanding_sales_mt",
    "next_my_net_sales_mt",
    "source_release_time_raw",
    "source_release_timezone",
    "source_fetch_id",
    "batch_id",
    "raw_snapshot_id",
    "raw_snapshot_sha256",
    "fetch_time_utc",
)
FAS_KEY = (
    "source",
    "report_market_year_end",
    "commodity_code",
    "country_code",
    "week_ending_date",
)
FAS_REVISION_FIELDS = tuple(FAS_METRIC_MAP.values())
FAS_BUSINESS_COLUMNS = tuple(
    column
    for column in FAS_STABLE_COLUMNS
    if column
    not in {
        "source_fetch_id",
        "batch_id",
        "raw_snapshot_id",
        "raw_snapshot_sha256",
        "fetch_time_utc",
    }
)


class FasAdapterError(PipelineError):
    pass


@dataclass(frozen=True)
class FasRelease:
    report_market_year_end: int
    market_year_start: str
    market_year_end: str
    release_timestamp_raw: str


@dataclass(frozen=True)
class FasFetchResult:
    records: list[dict[str, Any]]
    countries: list[dict[str, Any]]
    commodity: dict[str, Any]
    unit: dict[str, Any]
    release: FasRelease
    report_market_years: list[int]
    fetch_time_utc: str
    requests: list[dict[str, Any]]


def resolve_fas_api_key(
    environment: Mapping[str, str], *, allow_development_fallback: bool = False
) -> tuple[str, str]:
    primary = environment.get(FAS_API_KEY_ENV, "").strip()
    if primary:
        return primary, FAS_API_KEY_ENV
    if allow_development_fallback:
        fallback = environment.get(FAS_DEVELOPMENT_FALLBACK_ENV, "").strip()
        if fallback:
            return fallback, FAS_DEVELOPMENT_FALLBACK_ENV
    raise FasAdapterError(f"Missing required environment variable {FAS_API_KEY_ENV}")


class FasAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        session: requests.Session | None = None,
        timeout_seconds: float = 60,
        use_environment_proxy: bool = True,
    ) -> None:
        if not api_key:
            raise FasAdapterError("FAS API key is empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.api_key = api_key
        self.session = session or requests.Session()
        if session is None and not use_environment_proxy:
            self.session.trust_env = False
        self.timeout_seconds = timeout_seconds

    def _get(self, path: str) -> tuple[Any, bytes]:
        try:
            response = self.session.get(
                f"{FAS_BASE_URL}{path}",
                headers={"Accept": "application/json", "X-Api-Key": self.api_key},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise FasAdapterError(f"FAS request failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise FasAdapterError(f"FAS HTTP status {response.status_code}")
        try:
            return response.json(), response.content
        except ValueError as exc:
            raise FasAdapterError("FAS returned invalid JSON") from exc

    def get_soybean_release(self) -> tuple[FasRelease, dict[str, Any]]:
        payload, content = self._get("/api/esr/datareleasedates")
        if not isinstance(payload, list):
            raise FasAdapterError("FAS data release response is not an array")
        matches = [
            item
            for item in payload
            if isinstance(item, dict)
            and int(item.get("commodityCode", -1)) == FAS_COMMODITY_CODE
        ]
        if len(matches) != 1:
            raise FasAdapterError("FAS soybean release identity is not unique")
        item = matches[0]
        required = {
            "marketYearStart",
            "marketYearEnd",
            "marketYear",
            "releaseTimeStamp",
        }
        missing = sorted(required - set(item))
        if missing:
            raise FasAdapterError(f"FAS release schema missing: {', '.join(missing)}")
        release = FasRelease(
            report_market_year_end=int(item["marketYear"]),
            market_year_start=str(item["marketYearStart"]),
            market_year_end=str(item["marketYearEnd"]),
            release_timestamp_raw=str(item["releaseTimeStamp"]),
        )
        return release, {
            "endpoint": "/api/esr/datareleasedates",
            "response_sha256": sha256_bytes(content),
            "row_count": len(payload),
        }

    def fetch_export_sales(
        self, report_market_years: Sequence[int] | None = None
    ) -> FasFetchResult:
        fetched_at = iso_utc(utc_now())
        release, release_audit = self.get_soybean_release()
        years = sorted(
            set(
                int(value)
                for value in (
                    report_market_years
                    if report_market_years is not None
                    else range(release.report_market_year_end - 6, release.report_market_year_end + 1)
                )
            )
        )
        countries, countries_content = self._get("/api/esr/countries")
        commodities, commodities_content = self._get("/api/esr/commodities")
        units, units_content = self._get("/api/esr/unitsOfMeasure")
        if not all(isinstance(value, list) for value in (countries, commodities, units)):
            raise FasAdapterError("FAS metadata response is not an array")
        commodity_matches = [
            item
            for item in commodities
            if isinstance(item, dict) and int(item.get("commodityCode", -1)) == FAS_COMMODITY_CODE
        ]
        unit_matches = [
            item
            for item in units
            if isinstance(item, dict) and int(item.get("unitId", -1)) == FAS_UNIT_ID
        ]
        if len(commodity_matches) != 1 or len(unit_matches) != 1:
            raise FasAdapterError("FAS soybean commodity or MT unit identity is not unique")
        commodity = dict(commodity_matches[0])
        unit = dict(unit_matches[0])
        if commodity.get("commodityName") != "Soybeans" or commodity.get("unitId") != FAS_UNIT_ID:
            raise FasAdapterError("FAS soybean commodity identity changed")
        unit_name = unit.get("unitNames")
        if unit_name != "Metric Tons":
            raise FasAdapterError("FAS unit 1 is no longer Metric Tons")

        requests_audit = [
            release_audit,
            _response_audit("/api/esr/countries", countries, countries_content),
            _response_audit("/api/esr/commodities", commodities, commodities_content),
            _response_audit("/api/esr/unitsOfMeasure", units, units_content),
        ]
        combined: list[dict[str, Any]] = []
        for year in years:
            path = (
                f"/api/esr/exports/commodityCode/{FAS_COMMODITY_CODE}"
                f"/allCountries/marketYear/{year}"
            )
            payload, content = self._get(path)
            if not isinstance(payload, list):
                raise FasAdapterError(f"FAS {year} response is not an array")
            if not payload:
                raise FasAdapterError(f"FAS {year} returned an empty array")
            for raw in payload:
                if not isinstance(raw, dict):
                    raise FasAdapterError("FAS response contains a non-object row")
                missing = sorted(set(FAS_RAW_FIELDS) - set(raw))
                if missing:
                    raise FasAdapterError(f"FAS row missing required fields: {', '.join(missing)}")
                row = dict(raw)
                row["_report_market_year_end"] = year
                combined.append(row)
            requests_audit.append(_response_audit(path, payload, content))
        return FasFetchResult(
            records=combined,
            countries=[dict(item) for item in countries if isinstance(item, dict)],
            commodity=commodity,
            unit=unit,
            release=release,
            report_market_years=years,
            fetch_time_utc=fetched_at,
            requests=requests_audit,
        )


def _response_audit(path: str, payload: list[Any], content: bytes) -> dict[str, Any]:
    return {
        "endpoint": path,
        "response_sha256": sha256_bytes(content),
        "row_count": len(payload),
    }


def normalize_fas_records(
    records: Sequence[Mapping[str, Any]],
    *,
    countries: Sequence[Mapping[str, Any]],
    release_timestamp_raw: str,
    batch_id: str,
    raw_snapshot_id: str,
    raw_snapshot_sha256: str,
    fetch_time_utc: str,
) -> pd.DataFrame:
    if not records:
        raise PipelineError("FAS returned no soybean records")
    raw = pd.DataFrame([dict(item) for item in records])
    expected_fields = set(FAS_RAW_FIELDS) | {"_report_market_year_end"}
    missing = sorted(expected_fields - set(raw.columns))
    if missing:
        raise PipelineError(f"FAS raw schema missing: {', '.join(missing)}")
    raw["commodity_code"] = pd.to_numeric(raw["commodityCode"], errors="raise").astype("int32")
    raw["country_code"] = pd.to_numeric(raw["countryCode"], errors="raise").astype("int32")
    raw["unit_id"] = pd.to_numeric(raw["unitId"], errors="raise").astype("int16")
    if not raw["commodity_code"].eq(FAS_COMMODITY_CODE).all():
        raise PipelineError("FAS stable contains a non-801 commodity")
    if not raw["unit_id"].eq(FAS_UNIT_ID).all():
        raise PipelineError("FAS stable contains a non-Metric-Tons unit")
    raw["report_market_year_end"] = pd.to_numeric(
        raw["_report_market_year_end"], errors="raise"
    ).astype("int16")
    raw["report_market_year_start"] = raw["report_market_year_end"] - 1
    raw["report_market_year_label"] = raw.apply(
        lambda row: f"{int(row.report_market_year_start)}/{str(int(row.report_market_year_end))[-2:]}",
        axis=1,
    )
    raw["week_ending_date"] = pd.to_datetime(raw["weekEndingDate"], errors="raise")
    for original, normalized in FAS_METRIC_MAP.items():
        numeric = pd.to_numeric(raw[original], errors="raise")
        if numeric.isna().any() or (numeric % 1 != 0).any():
            raise PipelineError(f"FAS {original} must be a non-null integer")
        raw[normalized] = numeric.astype("int64")
    country_map: dict[int, dict[str, Any]] = {}
    for item in countries:
        code = int(item.get("countryCode", -1))
        if code in country_map:
            raise PipelineError(f"FAS country metadata code {code} is duplicated")
        country_map[code] = dict(item)
    missing_country = sorted(set(raw["country_code"]) - set(country_map))
    if missing_country:
        raise PipelineError(f"FAS country metadata missing codes: {missing_country}")
    raw["country_name"] = raw["country_code"].map(
        lambda code: str(country_map[int(code)].get("countryName", ""))
    )
    raw["country_description"] = raw["country_code"].map(
        lambda code: country_map[int(code)].get("countryDescription")
    )
    raw["country_genc_code"] = raw["country_code"].map(
        lambda code: country_map[int(code)].get("gencCode")
    )
    if raw["country_name"].eq("").any():
        raise PipelineError("FAS country name is empty")
    axis = (
        raw[["report_market_year_end", "week_ending_date"]]
        .drop_duplicates()
        .sort_values(["report_market_year_end", "week_ending_date"])
    )
    axis["report_week"] = axis.groupby("report_market_year_end").cumcount() + 1
    raw = raw.merge(axis, on=["report_market_year_end", "week_ending_date"], validate="many_to_one")
    raw["schema_version"] = SCHEMA_VERSION
    raw["source"] = FAS_SOURCE
    raw["commodity_name"] = "Soybeans"
    raw["unit_name"] = "Metric Tons"
    raw["current_target_market_year_end"] = raw["report_market_year_end"]
    raw["next_target_market_year_end"] = raw["report_market_year_end"] + 1
    raw["source_release_time_raw"] = release_timestamp_raw
    raw["source_release_timezone"] = "America/New_York"
    raw["source_fetch_id"] = batch_id
    raw["batch_id"] = batch_id
    raw["raw_snapshot_id"] = raw_snapshot_id
    raw["raw_snapshot_sha256"] = raw_snapshot_sha256
    raw["fetch_time_utc"] = fetch_time_utc
    result = raw.loc[:, FAS_STABLE_COLUMNS].sort_values(
        ["report_market_year_end", "report_week", "country_code"], kind="stable"
    )
    return result.reset_index(drop=True)


def validate_fas_stable(stable: pd.DataFrame) -> dict[str, Any]:
    if tuple(stable.columns) != FAS_STABLE_COLUMNS:
        missing = sorted(set(FAS_STABLE_COLUMNS) - set(stable.columns))
        extra = sorted(set(stable.columns) - set(FAS_STABLE_COLUMNS))
        raise PipelineError(f"FAS stable schema mismatch; missing={missing}, extra={extra}")
    if stable.empty:
        raise PipelineError("FAS stable is empty")
    nullable = {"country_description", "country_genc_code"}
    required = [column for column in FAS_STABLE_COLUMNS if column not in nullable]
    if stable[required].isna().any().any():
        raise PipelineError("FAS stable contains null required values")
    if stable.duplicated(list(FAS_KEY)).any():
        raise PipelineError("FAS stable business key is not unique")
    if not stable["commodity_code"].eq(FAS_COMMODITY_CODE).all():
        raise PipelineError("FAS commodity identity mismatch")
    if not stable["unit_id"].eq(FAS_UNIT_ID).all() or not stable["unit_name"].eq("Metric Tons").all():
        raise PipelineError("FAS unit identity mismatch")
    if not stable["current_target_market_year_end"].eq(stable["report_market_year_end"]).all():
        raise PipelineError("FAS current target MY mismatch")
    if not stable["next_target_market_year_end"].eq(stable["report_market_year_end"] + 1).all():
        raise PipelineError("FAS next target MY mismatch")
    if not stable["current_my_total_commitment_mt"].eq(
        stable["accumulated_exports_mt"] + stable["outstanding_sales_mt"]
    ).all():
        raise PipelineError("FAS Total Commitment identity failed")
    for report_year, annual in stable.groupby("report_market_year_end", observed=True):
        axis = annual[["report_week", "week_ending_date"]].drop_duplicates().sort_values("report_week")
        if axis["report_week"].tolist() != list(range(1, len(axis) + 1)):
            raise PipelineError(f"FAS report_week sequence failed for {report_year}")
    research = build_fas_research_view_without_validation(stable)
    for metric in FAS_CLOSURE_METRICS:
        if not research[f"world_{metric}"].eq(
            research[f"china_{metric}"] + research[f"non_china_{metric}"]
        ).all():
            raise PipelineError(f"FAS China/Non-China closure failed for {metric}")
    return {
        "passed": True,
        "row_count": len(stable),
        "report_market_year_count": int(stable["report_market_year_end"].nunique()),
        "latest_week": stable["week_ending_date"].max().date().isoformat(),
    }


def build_fas_research_view(stable: pd.DataFrame) -> pd.DataFrame:
    validate_fas_stable(stable)
    return build_fas_research_view_without_validation(stable)


def build_fas_research_view_without_validation(stable: pd.DataFrame) -> pd.DataFrame:
    group = [
        "report_market_year_start",
        "report_market_year_end",
        "report_market_year_label",
        "report_week",
        "week_ending_date",
        "current_target_market_year_end",
        "next_target_market_year_end",
    ]
    world = stable.groupby(group, as_index=False, observed=True)[list(FAS_CLOSURE_METRICS)].sum()
    world = world.rename(columns={metric: f"world_{metric}" for metric in FAS_CLOSURE_METRICS})
    china = (
        stable.loc[stable["country_code"].eq(5700)]
        .groupby(group, as_index=False, observed=True)[list(FAS_CLOSURE_METRICS)]
        .sum()
        .rename(columns={metric: f"china_{metric}" for metric in FAS_CLOSURE_METRICS})
    )
    result = world.merge(china, on=group, how="left", validate="one_to_one")
    for metric in FAS_CLOSURE_METRICS:
        result[f"china_{metric}"] = result[f"china_{metric}"].fillna(0).astype("int64")
        result[f"non_china_{metric}"] = result[f"world_{metric}"] - result[f"china_{metric}"]
    return result.sort_values(["report_market_year_end", "report_week"]).reset_index(drop=True)


def detect_fas_revisions(
    old: pd.DataFrame | None, new: pd.DataFrame, *, detected_at_utc: str
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
    merged = old.merge(new, on=list(FAS_KEY), how="inner", suffixes=("_old", "_new"))
    rows: list[dict[str, Any]] = []
    for field in FAS_REVISION_FIELDS:
        changed = merged.loc[merged[f"{field}_old"].ne(merged[f"{field}_new"])]
        for row in changed.itertuples(index=False):
            key = {name: getattr(row, name) for name in FAS_KEY}
            old_value = getattr(row, f"{field}_old")
            new_value = getattr(row, f"{field}_new")
            rows.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "source": FAS_SOURCE,
                    "business_key_json": json.dumps(key, default=str, sort_keys=True),
                    "field_name": field,
                    "metric_unit": "MT",
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
    return pd.DataFrame(rows, columns=columns)


def fas_paths(runtime_root: Path, batch_id: str) -> dict[str, Path]:
    processed = runtime_root / "01_data" / "processed" / "soybean_export_sales"
    return {
        "raw_root": runtime_root / "01_data" / "raw",
        "candidate_dir": runtime_root / "01_data" / "candidates" / "soybean_export_sales" / batch_id,
        "stable": processed / "soybean_export_sales_weekly.parquet",
        "stable_manifest": processed / "soybean_export_sales_weekly.manifest.json",
        "revision": processed / "revisions" / "soybean_export_sales_revisions.parquet",
        "backup_root": runtime_root / "01_data" / "backups" / "soybean_export_sales",
        "status": runtime_root / "01_data" / "update_status" / "soybean_export_sales.json",
    }


def run_fas_pipeline(
    *,
    runtime_root: Path,
    adapter: FasAdapter | Any,
    git_head: str,
    report_market_years: Sequence[int] | None = None,
    batch_id: str | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    if not _valid_git_head(git_head):
        raise PipelineError("git_head must be a full 40-character hexadecimal SHA")
    selected_batch = batch_id or make_batch_id()
    paths = fas_paths(Path(runtime_root), selected_batch)
    started = iso_utc(utc_now())
    previous_success = read_last_success(paths["status"])
    try:
        fetched: FasFetchResult = adapter.fetch_export_sales(report_market_years)
        raw_records = [
            {key: value for key, value in row.items() if key in FAS_RAW_FIELDS or key == "_report_market_year_end"}
            for row in fetched.records
        ]
        raw_manifest = write_raw_snapshot(
            raw_root=paths["raw_root"],
            pipeline_name="soybean_export_sales",
            batch_id=selected_batch,
            records=raw_records,
            manifest={
                "source": FAS_SOURCE,
                "commodity_code": FAS_COMMODITY_CODE,
                "fetch_time_utc": fetched.fetch_time_utc,
                "report_market_years": fetched.report_market_years,
                "requests": fetched.requests,
                "source_release_time_raw": fetched.release.release_timestamp_raw,
                "source_release_timezone": "America/New_York",
                "git_head": git_head,
                "initial_value_known": False,
            },
        )
        candidate = normalize_fas_records(
            raw_records,
            countries=fetched.countries,
            release_timestamp_raw=fetched.release.release_timestamp_raw,
            batch_id=selected_batch,
            raw_snapshot_id=selected_batch,
            raw_snapshot_sha256=raw_manifest["raw_file_sha256"],
            fetch_time_utc=fetched.fetch_time_utc,
        )
        validation = validate_fas_stable(candidate)
        generated_at = iso_utc(utc_now())
        stable_relative_path = (
            "01_data/processed/soybean_export_sales/soybean_export_sales_weekly.parquet"
        )
        candidate_path = paths["candidate_dir"] / "soybean_export_sales_weekly.parquet"
        identity = write_parquet_atomic(
            candidate,
            candidate_path,
            metadata={
                "schema_version": SCHEMA_VERSION,
                "source": FAS_SOURCE,
                "batch_id": selected_batch,
                "git_head": git_head,
                "raw_snapshot_sha256": raw_manifest["raw_file_sha256"],
                "quality_status": "passed",
                "generated_at_utc": generated_at,
                "source_latest_week": validation["latest_week"],
                "source_release_time_raw": fetched.release.release_timestamp_raw,
                "source_release_timezone": "America/New_York",
                "stable_relative_path": stable_relative_path,
                "generator_version": SCHEMA_VERSION,
            },
        )
        manifest = {
            **identity,
            "schema_version": SCHEMA_VERSION,
            "source": FAS_SOURCE,
            "generated_at_utc": generated_at,
            "fetch_started_at_utc": started,
            "fetch_completed_at_utc": fetched.fetch_time_utc,
            "batch_id": selected_batch,
            "source_latest_week": validation["latest_week"],
            "source_release_time_raw": fetched.release.release_timestamp_raw,
            "source_release_timezone": "America/New_York",
            "source_dataset_updated_at": None,
            "stable_relative_path": stable_relative_path,
            "raw_snapshot_id": selected_batch,
            "raw_snapshot_sha256": raw_manifest["raw_file_sha256"],
            "raw_manifest_sha256": raw_manifest["manifest_sha256"],
            "git_head": git_head,
            "generator_version": SCHEMA_VERSION,
            "fetch_scope": {"report_market_years": fetched.report_market_years},
            "revision_coverage": {
                "mode": "candidate_vs_current",
                "report_market_years": fetched.report_market_years,
            },
            "quality_status": "passed",
            "quality": validation,
        }
        write_json_atomic(paths["candidate_dir"] / "manifest.json", manifest, overwrite=False)
        old = pd.read_parquet(paths["stable"]) if paths["stable"].exists() else None
        revisions = detect_fas_revisions(old, candidate, detected_at_utc=iso_utc(utc_now()))
        publication = {"status": "candidate_only", "published": False, "backup_dir": None}
        if publish:
            if old is not None and _business_equal(old, candidate):
                publication = {"status": "no_change", "published": False, "backup_dir": None}
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
                    metadata={"schema_version": SCHEMA_VERSION, "source": FAS_SOURCE, "batch_id": selected_batch},
                )
        research = build_fas_research_view(candidate)
        latest = research.sort_values("week_ending_date").iloc[-1]
        latest_values: dict[str, Any] = {
            "week_ending_date": latest["week_ending_date"].date().isoformat(),
            "release_timestamp_raw": fetched.release.release_timestamp_raw,
            "report_market_year_end": int(latest["report_market_year_end"]),
            "current_target_market_year_end": int(latest["current_target_market_year_end"]),
            "next_target_market_year_end": int(latest["next_target_market_year_end"]),
            "report_week": int(latest["report_week"]),
        }
        for role in ("world", "china", "non_china"):
            for metric in FAS_CLOSURE_METRICS:
                latest_values[f"{role}_{metric}"] = int(latest[f"{role}_{metric}"])
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
                "source_release_time_raw": fetched.release.release_timestamp_raw,
            }
        audit = {
            "schema_version": SCHEMA_VERSION,
            "source": FAS_SOURCE,
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
            "latest": latest_values,
            "latest_attempt": latest_attempt,
            "last_success": last_success,
        }
        write_json_atomic(paths["status"], audit)
        return audit
    except Exception as exc:
        write_json_atomic(
            paths["status"],
            {
                "schema_version": SCHEMA_VERSION,
                "source": FAS_SOURCE,
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
            },
        )
        raise


def _valid_git_head(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdefABCDEF" for character in value)


def _business_equal(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    left_view = left.loc[:, FAS_BUSINESS_COLUMNS].sort_values(list(FAS_KEY)).reset_index(drop=True)
    right_view = right.loc[:, FAS_BUSINESS_COLUMNS].sort_values(list(FAS_KEY)).reset_index(drop=True)
    return left_view.equals(right_view)
