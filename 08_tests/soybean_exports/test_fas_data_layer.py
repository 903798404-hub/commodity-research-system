from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import requests

from agri_research_agent.soybean_exports.common import PipelineError
from agri_research_agent.soybean_exports.fas import (
    FAS_RAW_FIELDS,
    FasAdapter,
    FasAdapterError,
    FasFetchResult,
    FasRelease,
    build_fas_research_view,
    detect_fas_revisions,
    fas_paths,
    normalize_fas_records,
    resolve_fas_api_key,
    run_fas_pipeline,
    validate_fas_stable,
)


GIT_HEAD = "b" * 40
SECRET = "fixture-secret-value"


def fas_row(
    *,
    country: int = 5700,
    week: str = "2026-07-30T00:00:00",
    report_year: int = 2026,
    current_net: int = 10,
    next_net: int = 20,
    accumulated: int = 80,
    outstanding: int = 20,
    commodity: int = 801,
    unit: int = 1,
) -> dict[str, Any]:
    row = {
        "commodityCode": commodity,
        "countryCode": country,
        "weeklyExports": 5,
        "accumulatedExports": accumulated,
        "outstandingSales": outstanding,
        "grossNewSales": 15,
        "currentMYNetSales": current_net,
        "currentMYTotalCommitment": accumulated + outstanding,
        "nextMYOutstandingSales": 40,
        "nextMYNetSales": next_net,
        "unitId": unit,
        "weekEndingDate": week,
        "_report_market_year_end": report_year,
    }
    assert set(row) == set(FAS_RAW_FIELDS) | {"_report_market_year_end"}
    return row


COUNTRIES = [
    {"countryCode": 5700, "countryName": "CHINA", "countryDescription": "CHINA, PEOPLES REPUBLIC OF", "gencCode": "CHN"},
    {"countryCode": 5820, "countryName": "HG KONG", "countryDescription": "HONG KONG", "gencCode": "HKG"},
    {"countryCode": 5830, "countryName": "TAIWAN", "countryDescription": "TAIWAN", "gencCode": "TWN"},
    {"countryCode": 9990, "countryName": "UNKNOWN", "countryDescription": "UNKNOWN", "gencCode": "AX1"},
]


def normalize(rows: list[dict[str, Any]], batch: str = "batch") -> pd.DataFrame:
    return normalize_fas_records(
        rows,
        countries=COUNTRIES,
        release_timestamp_raw="2026-08-06T08:30:06.973",
        batch_id=batch,
        raw_snapshot_id=batch,
        raw_snapshot_sha256="D" * 64,
        fetch_time_utc="2026-08-07T00:00:00Z",
    )


class Response:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self.payload = payload
        self.status_code = status
        self.content = json.dumps(payload, sort_keys=True).encode()

    def json(self) -> Any:
        return self.payload


class Session:
    def __init__(self, responses: list[Response] | None = None, error: Exception | None = None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> Response:
        self.calls.append({"url": url, **kwargs})
        if self.error:
            raise self.error
        return self.responses.pop(0)


def releases() -> list[dict[str, Any]]:
    return [{"commodityCode": 801, "marketYearStart": "2025-09-01", "marketYearEnd": "2026-08-31", "marketYear": 2026, "releaseTimeStamp": "2026-08-06T08:30:06.973"}]


def commodities() -> list[dict[str, Any]]:
    return [{"commodityCode": 801, "commodityName": "Soybeans", "unitId": 1}]


def units(name: str = "Metric Tons") -> list[dict[str, Any]]:
    return [{"unitId": 1, "unitNames": name}]


class StaticAdapter:
    def __init__(self, rows: list[dict[str, Any]], release: str = "2026-08-06T08:30:06.973") -> None:
        self.rows = rows
        self.release = release

    def fetch_export_sales(self, years: Any = None) -> FasFetchResult:
        selected = sorted({int(row["_report_market_year_end"]) for row in self.rows})
        return FasFetchResult(
            records=self.rows,
            countries=COUNTRIES,
            commodity=commodities()[0],
            unit=units()[0],
            release=FasRelease(2026, "2025-09-01", "2026-08-31", self.release),
            report_market_years=selected,
            fetch_time_utc="2026-08-07T00:00:00Z",
            requests=[{"endpoint": "fixture", "row_count": len(self.rows), "response_sha256": "E" * 64}],
        )


def test_api_key_governance_and_development_fallback() -> None:
    assert resolve_fas_api_key({"FAS_EXPORT_SALES_API_KEY": "primary", "USDA_API_KEY": "fallback"}) == ("primary", "FAS_EXPORT_SALES_API_KEY")
    with pytest.raises(FasAdapterError, match="FAS_EXPORT_SALES_API_KEY"):
        resolve_fas_api_key({"USDA_API_KEY": "fallback"})
    assert resolve_fas_api_key({"USDA_API_KEY": "fallback"}, allow_development_fallback=True) == ("fallback", "USDA_API_KEY")


def test_adapter_auth_header_release_check_and_recent_year_fetch() -> None:
    row = {key: value for key, value in fas_row().items() if key in FAS_RAW_FIELDS}
    session = Session(
        [
            Response(releases()),
            Response(COUNTRIES),
            Response(commodities()),
            Response(units()),
            Response([row]),
        ]
    )
    result = FasAdapter(api_key=SECRET, session=session).fetch_export_sales([2026])
    assert result.release.release_timestamp_raw == "2026-08-06T08:30:06.973"
    assert result.records[0]["_report_market_year_end"] == 2026
    assert session.calls[0]["headers"]["X-Api-Key"] == SECRET
    assert SECRET not in json.dumps(result.requests)


def test_adapter_rejects_timeout_http_empty_schema_and_unit_change() -> None:
    with pytest.raises(FasAdapterError, match="Timeout"):
        FasAdapter(api_key=SECRET, session=Session(error=requests.Timeout())).get_soybean_release()
    with pytest.raises(FasAdapterError, match="HTTP status 403"):
        FasAdapter(api_key=SECRET, session=Session([Response({}, 403)])).get_soybean_release()

    empty_session = Session([Response(releases()), Response(COUNTRIES), Response(commodities()), Response(units()), Response([])])
    with pytest.raises(FasAdapterError, match="empty array"):
        FasAdapter(api_key=SECRET, session=empty_session).fetch_export_sales([2026])

    bad_unit = Session([Response(releases()), Response(COUNTRIES), Response(commodities()), Response(units("Bushels"))])
    with pytest.raises(FasAdapterError, match="Metric Tons"):
        FasAdapter(api_key=SECRET, session=bad_unit).fetch_export_sales([2026])

    missing_row = {key: value for key, value in fas_row().items() if key in FAS_RAW_FIELDS and key != "currentMYNetSales"}
    drift = Session([Response(releases()), Response(COUNTRIES), Response(commodities()), Response(units()), Response([missing_row])])
    with pytest.raises(FasAdapterError, match="currentMYNetSales"):
        FasAdapter(api_key=SECRET, session=drift).fetch_export_sales([2026])


def test_report_and_target_market_years_share_report_week_without_target_week() -> None:
    stable = normalize(
        [
            fas_row(week="2025-09-04", report_year=2026),
            fas_row(week="2025-09-11", report_year=2026),
        ]
    )
    assert stable["report_market_year_label"].unique().tolist() == ["2025/26"]
    assert stable["current_target_market_year_end"].unique().tolist() == [2026]
    assert stable["next_target_market_year_end"].unique().tolist() == [2027]
    assert stable["report_week"].tolist() == [1, 2]
    assert "target_my_week" not in stable.columns


def test_negative_net_sales_and_total_commitment_native_values() -> None:
    stable = normalize([fas_row(current_net=-100, next_net=-50, accumulated=80, outstanding=20)])
    assert stable.iloc[0]["current_my_net_sales_mt"] == -100
    assert stable.iloc[0]["next_my_net_sales_mt"] == -50
    assert stable.iloc[0]["current_my_total_commitment_mt"] == 100
    assert validate_fas_stable(stable)["passed"] is True

    broken = stable.copy()
    broken.loc[0, "current_my_total_commitment_mt"] = 999
    with pytest.raises(PipelineError, match="Total Commitment"):
        validate_fas_stable(broken)


def test_country_roles_unknown_and_seven_metric_closure() -> None:
    stable = normalize(
        [
            fas_row(country=5700, current_net=10),
            fas_row(country=5820, current_net=20),
            fas_row(country=5830, current_net=30),
            fas_row(country=9990, current_net=40),
        ]
    )
    view = build_fas_research_view(stable)
    row = view.iloc[0]
    assert row["world_current_my_net_sales_mt"] == 100
    assert row["china_current_my_net_sales_mt"] == 10
    assert row["non_china_current_my_net_sales_mt"] == 90
    for metric in (
        "weekly_exports_mt", "accumulated_exports_mt", "outstanding_sales_mt",
        "current_my_net_sales_mt", "current_my_total_commitment_mt",
        "next_my_outstanding_sales_mt", "next_my_net_sales_mt",
    ):
        assert row[f"world_{metric}"] == row[f"china_{metric}"] + row[f"non_china_{metric}"]


def test_nullable_country_metadata_is_allowed_but_business_fields_are_required() -> None:
    countries = [dict(item) for item in COUNTRIES]
    countries[0]["countryDescription"] = None
    countries[0]["gencCode"] = None
    stable = normalize_fas_records(
        [fas_row(country=5700)],
        countries=countries,
        release_timestamp_raw="2026-08-06T08:30:06.973",
        batch_id="nullable",
        raw_snapshot_id="nullable",
        raw_snapshot_sha256="D" * 64,
        fetch_time_utc="2026-08-07T00:00:00Z",
    )
    assert validate_fas_stable(stable)["passed"] is True
    broken = stable.copy()
    broken.loc[0, "weekly_exports_mt"] = None
    with pytest.raises(PipelineError, match="null required"):
        validate_fas_stable(broken)


def test_revision_and_no_revision() -> None:
    old = normalize([fas_row(current_net=10)], "old")
    same = normalize([fas_row(current_net=10)], "same")
    changed = normalize([fas_row(current_net=12)], "new")
    assert detect_fas_revisions(old, same, detected_at_utc="2026-08-08T00:00:00Z").empty
    revisions = detect_fas_revisions(old, changed, detected_at_utc="2026-08-08T00:00:00Z")
    assert revisions["field_name"].tolist() == ["current_my_net_sales_mt"]
    assert revisions.iloc[0]["delta"] == 2


def test_pipeline_raw_stable_backup_revision_failure_and_idempotence(tmp_path: Path) -> None:
    base = [fas_row(country=5700), fas_row(country=9990)]
    first = run_fas_pipeline(runtime_root=tmp_path, adapter=StaticAdapter(base), git_head=GIT_HEAD, batch_id="first")
    paths = fas_paths(tmp_path, "first")
    stable_bytes = paths["stable"].read_bytes()
    assert first["status"] == "initialized"
    assert Path(first["raw_snapshot_dir"]).joinpath("records.jsonl.gz").is_file()
    manifest = json.loads(paths["stable_manifest"].read_text(encoding="utf-8"))
    assert manifest["parquet_schema_sha256"]
    assert manifest["source_release_time_raw"] == "2026-08-06T08:30:06.973"
    assert manifest["source_release_timezone"] == "America/New_York"
    assert manifest["source_dataset_updated_at"] is None
    assert manifest["stable_relative_path"].startswith("01_data/processed/")
    assert manifest["revision_coverage"]["mode"] == "candidate_vs_current"

    second = run_fas_pipeline(runtime_root=tmp_path, adapter=StaticAdapter(base), git_head=GIT_HEAD, batch_id="second")
    assert second["status"] == "no_change"
    assert paths["stable"].read_bytes() == stable_bytes

    revised = [fas_row(country=5700, current_net=11), fas_row(country=9990)]
    third = run_fas_pipeline(runtime_root=tmp_path, adapter=StaticAdapter(revised), git_head=GIT_HEAD, batch_id="third")
    assert third["status"] == "updated"
    assert (Path(third["backup_dir"]) / paths["stable"].name).read_bytes() == stable_bytes
    revision = pd.read_parquet(paths["revision"])
    assert "current_my_net_sales_mt" in set(revision["field_name"])

    before_failure = paths["stable"].read_bytes()
    bad = [dict(fas_row(country=5700)), dict(fas_row(country=9990))]
    bad[0]["unitId"] = 2
    with pytest.raises(PipelineError, match="non-Metric-Tons"):
        run_fas_pipeline(runtime_root=tmp_path, adapter=StaticAdapter(bad), git_head=GIT_HEAD, batch_id="bad")
    assert paths["stable"].read_bytes() == before_failure
    status = json.loads(paths["status"].read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert status["latest_attempt"]["batch_id"] == "bad"
    assert status["last_success"]["batch_id"] == "third"
    assert status["last_success"]["source_latest_week"] == "2026-07-30"
