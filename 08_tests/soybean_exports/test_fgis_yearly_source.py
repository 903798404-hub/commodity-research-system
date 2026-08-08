from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import requests

from agri_research_agent.soybean_exports.common import PipelineError, sha256_bytes
from agri_research_agent.soybean_exports.fgis import (
    FGIS_DATASET_ID,
    FGIS_YEARLY_SOURCE_CHANNEL,
    FgisAdapterError,
    FgisFetchResult,
    FgisYearlyAdapter,
    build_fgis_research_view,
    fgis_paths,
    normalize_fgis_records,
    run_fgis_pipeline,
)


GIT_HEAD = "d" * 40
YEARLY_FIELDS = (
    "Thursday",
    "Serial No.",
    "Type Shipm",
    "Cert Date",
    "Type Carrier",
    "Carrier Name",
    "Grade",
    "Grain",
    "Class",
    "SubClass",
    "Pounds",
    "Destination",
    "Field Office",
    "Port",
    "AMS Reg",
    "FGIS Reg",
    "State",
    "MKT YR",
    "Metric Ton",
    "Future Optional Field",
)


def yearly_row(
    *,
    thursday: str = "20260730",
    cert_date: str = "20260730",
    destination: str = "CHINA",
    mt: str = "10",
    pounds: str = "22046",
    grain: str = "SOYBEANS",
) -> dict[str, str]:
    return {
        "Thursday": thursday,
        "Serial No.": "700001",
        "Type Shipm": "BU",
        "Cert Date": cert_date,
        "Type Carrier": "6",
        "Carrier Name": "PUBLIC",
        "Grade": "1",
        "Grain": grain,
        "Class": "YSB",
        "SubClass": "",
        "Pounds": pounds,
        "Destination": destination,
        "Field Office": "DIOO",
        "Port": "INTERIOR",
        "AMS Reg": "INTERIOR",
        "FGIS Reg": "INTERIOR",
        "State": "IL",
        "MKT YR": "2526",
        "Metric Ton": mt,
        "Future Optional Field": "allowed",
    }


def csv_bytes(rows: list[dict[str, str]], fields: tuple[str, ...] = YEARLY_FIELDS) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


class CsvResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status: int = 200,
        content_type: str = "application/octet-stream",
    ) -> None:
        self.content = content
        self.status_code = status
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(content)),
            "Last-Modified": "Mon, 03 Aug 2026 15:00:25 GMT",
            "ETag": '"yearly-etag"',
        }


class CsvSession:
    def __init__(self, response: CsvResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> CsvResponse:
        self.calls.append({"url": url, **kwargs})
        return self.response


class RetrySession(CsvSession):
    def __init__(self, response: CsvResponse) -> None:
        super().__init__(response)
        self.failures = 1

    def get(self, url: str, **kwargs: Any) -> CsvResponse:
        self.calls.append({"url": url, **kwargs})
        if self.failures:
            self.failures -= 1
            raise requests.ConnectionError("transient")
        return self.response


def yearly_adapter(rows: list[dict[str, str]], *, year: int) -> tuple[FgisYearlyAdapter, bytes]:
    content = csv_bytes(rows)
    return (
        FgisYearlyAdapter(
            session=CsvSession(CsvResponse(content)),
            calendar_year=year,
            timeout_seconds=17,
        ),
        content,
    )


def socrata_row(
    *,
    cert_date: str,
    week_ending: str,
    destination: str,
    mt: int,
) -> dict[str, Any]:
    return {
        "date": week_ending,
        "cert_date": cert_date,
        "week": "1",
        "month": cert_date[5:7].lstrip("0") or "0",
        "quarter": "1",
        "year": cert_date[:4],
        "type_shipm": "BU",
        "type_carrier": "6",
        "type_carrier_text": "RAIL",
        "carrier_name": "PUBLIC",
        "grain": "SOYBEANS",
        "grade": "1",
        "class": "YSB",
        "subclass": None,
        "destination": destination,
        "port": "INTERIOR",
        "ams_reg": "INTERIOR",
        "fgis_reg": "INTERIOR",
        "state": "IL",
        "mt": str(mt),
        "pounds": str(mt * 2205),
        "field_office": "DIOO",
    }


class SocrataStaticAdapter:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def fetch_soybeans(self, **_: Any) -> FgisFetchResult:
        return FgisFetchResult(
            records=self.rows,
            fetch_time_utc="2026-08-08T00:00:00Z",
            dataset_updated_at="2026-08-03T15:00:25Z",
            pages=[{"row_count": len(self.rows), "response_sha256": "A" * 64}],
            query_scope={"grain": "SOYBEANS"},
        )


def seed_baseline(runtime: Path, rows: list[dict[str, Any]]) -> None:
    run_fgis_pipeline(
        runtime_root=runtime,
        adapter=SocrataStaticAdapter(rows),
        git_head=GIT_HEAD,
        batch_id="socrata-baseline",
    )


def run_yearly(
    runtime: Path,
    rows: list[dict[str, str]],
    *,
    year: int,
    batch: str,
) -> tuple[dict[str, Any], bytes]:
    adapter, content = yearly_adapter(rows, year=year)
    result = run_fgis_pipeline(
        runtime_root=runtime,
        adapter=adapter,
        git_head=GIT_HEAD,
        batch_id=batch,
    )
    return result, content


def test_yearly_adapter_dynamic_url_schema_mapping_filter_and_destination_identity() -> None:
    rows = [
        yearly_row(thursday="20270729", cert_date="20270729", grain="WHEAT", destination="CHINA"),
        yearly_row(thursday="20270729", cert_date="20270729", destination="CHINA", mt="11"),
        yearly_row(thursday="20270729", cert_date="20270729", destination="HONG KONG", mt="12"),
        yearly_row(thursday="20270729", cert_date="20270729", destination="TAIWAN", mt="13"),
    ]
    content = csv_bytes(rows)
    session = CsvSession(CsvResponse(content))
    result = FgisYearlyAdapter(
        session=session, calendar_year=2027, timeout_seconds=17
    ).fetch_soybeans()

    assert session.calls[0]["url"].endswith("/CY2027.csv")
    assert session.calls[0]["timeout"] == 17
    assert result.source_channel == FGIS_YEARLY_SOURCE_CHANNEL
    assert result.source_file == "CY2027.csv"
    assert result.source_sha256 == sha256_bytes(content)
    assert result.content_length == len(content)
    assert result.etag == '"yearly-etag"'
    assert [row["destination"] for row in result.records] == ["CHINA", "HONG KONG", "TAIWAN"]
    assert result.records[0]["date"] == "2027-07-29"
    assert result.records[0]["cert_date"] == "2027-07-29"
    assert result.records[0]["mt"] == 11
    assert result.records[0]["pounds"] == 22046
    assert result.records[0]["source_mkt_yr"] == "2526"


def test_yearly_adapter_retries_transient_request_error_only() -> None:
    content = csv_bytes([yearly_row()])
    session = RetrySession(CsvResponse(content))
    result = FgisYearlyAdapter(
        session=session, calendar_year=2026, max_attempts=2
    ).fetch_soybeans()
    assert len(session.calls) == 2
    assert result.source_sha256 == sha256_bytes(content)


@pytest.mark.parametrize("case", ["schema", "date", "mt", "encoding", "content_type", "filter"])
def test_yearly_adapter_fails_closed_on_schema_type_encoding_media_and_filter(case: str) -> None:
    fields = YEARLY_FIELDS
    rows = [yearly_row()]
    content_type = "application/octet-stream"
    if case == "schema":
        fields = tuple(field for field in YEARLY_FIELDS if field != "Metric Ton")
    elif case == "date":
        rows[0]["Cert Date"] = "2026-07-30"
    elif case == "mt":
        rows[0]["Metric Ton"] = "1.5"
    elif case == "encoding":
        content = b"Thursday,Cert Date,Grain,Destination,Metric Ton\n\xff"
    elif case == "content_type":
        content_type = "text/html"
    elif case == "filter":
        rows[0]["Grain"] = "soybeans"
    if case != "encoding":
        content = csv_bytes(rows, fields)
    adapter = FgisYearlyAdapter(
        session=CsvSession(CsvResponse(content, content_type=content_type)),
        calendar_year=2026,
    )
    with pytest.raises(FgisAdapterError):
        adapter.fetch_soybeans()


def test_cert_date_controls_two_marketing_years_inside_one_calendar_file() -> None:
    adapter, _ = yearly_adapter(
        [
            yearly_row(thursday="20260101", cert_date="20260101", mt="10"),
            yearly_row(thursday="20260903", cert_date="20260831", mt="11"),
            yearly_row(thursday="20260903", cert_date="20260901", mt="12"),
        ],
        year=2026,
    )
    fetched = adapter.fetch_soybeans()
    stable = normalize_fgis_records(
        fetched.records,
        batch_id="yearly",
        raw_snapshot_id="yearly",
        raw_snapshot_sha256="B" * 64,
        fetch_time_utc=fetched.fetch_time_utc,
        dataset_updated_at=fetched.dataset_updated_at,
        source_dataset_id="yearly_export_grain_csv:CY2026.csv",
    )
    assert stable["market_year_end"].tolist() == [2026, 2026, 2027]
    assert stable.loc[stable["week_ending_date"].eq(pd.Timestamp("2026-09-03")), "weekly_mt"].tolist() == [11, 12]


def test_source_sha_no_change_skips_raw_candidate_and_stable_mtime(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    baseline = [
        socrata_row(cert_date="2024-09-01", week_ending="2024-09-05", destination="JAPAN", mt=30),
        socrata_row(cert_date="2026-07-30", week_ending="2026-07-30", destination="CHINA", mt=10),
    ]
    seed_baseline(runtime, baseline)
    rows = [yearly_row(destination="CHINA", mt="10")]
    first, content = run_yearly(runtime, rows, year=2026, batch="yearly-first")
    paths = fgis_paths(runtime, "unused")
    stable_bytes = paths["stable"].read_bytes()
    stable_mtime = paths["stable"].stat().st_mtime_ns
    raw_file = Path(first["raw_snapshot_dir"]) / "CY2026.csv"
    assert raw_file.read_bytes() == content
    manifest = json.loads(paths["stable_manifest"].read_text(encoding="utf-8"))
    assert manifest["source_channel"] == FGIS_YEARLY_SOURCE_CHANNEL
    assert manifest["active_update_provenance"]["source_file"] == "CY2026.csv"
    assert manifest["active_update_provenance"]["source_sha256"] == sha256_bytes(content)
    assert manifest["revision_coverage"]["tracks_prior_calendar_year_revisions"] is False

    second, _ = run_yearly(runtime, rows, year=2026, batch="yearly-same")
    assert second["status"] == "no_change"
    assert second["source_change"] == {"sha256_changed": False, "processing_skipped": True}
    assert not (runtime / "01_data/raw/soybean_export_inspections/yearly-same").exists()
    assert not (runtime / "01_data/candidates/soybean_export_inspections/yearly-same").exists()
    assert paths["stable"].read_bytes() == stable_bytes
    assert paths["stable"].stat().st_mtime_ns == stable_mtime


def test_changed_current_year_revises_and_adds_without_deleting_frozen_history(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    seed_baseline(
        runtime,
        [
            socrata_row(cert_date="2024-09-01", week_ending="2024-09-05", destination="JAPAN", mt=30),
            socrata_row(cert_date="2026-07-16", week_ending="2026-07-16", destination="CHINA", mt=10),
        ],
    )
    run_yearly(
        runtime,
        [yearly_row(thursday="20260716", cert_date="20260716", destination="CHINA", mt="10")],
        year=2026,
        batch="yearly-observed",
    )
    changed, _ = run_yearly(
        runtime,
        [
            yearly_row(thursday="20260716", cert_date="20260716", destination="CHINA", mt="12"),
            yearly_row(thursday="20260723", cert_date="20260723", destination="JAPAN", mt="5"),
        ],
        year=2026,
        batch="yearly-changed",
    )
    stable = pd.read_parquet(fgis_paths(runtime, "unused")["stable"])
    assert changed["status"] == "updated"
    assert changed["source_change"]["active_calendar_year"] == {
        "new_business_keys": 1,
        "removed_business_keys": 0,
        "revised_business_keys": 1,
        "new_weeks": 1,
        "added_source_rows": 1,
        "removed_source_rows": 0,
    }
    historical = stable.loc[stable["week_ending_date"].eq(pd.Timestamp("2024-09-05"))].iloc[0]
    assert historical["weekly_mt"] == 30
    assert historical["source_dataset_id"] == FGIS_DATASET_ID
    assert not stable.duplicated(
        ["source", "commodity", "market_year_end", "week_ending_date", "destination"]
    ).any()
    current = stable.loc[stable["market_year_end"].eq(2026)].sort_values("my_week")
    assert current["weekly_mt"].tolist() == [12, 5]
    research = build_fgis_research_view(stable)
    current_research = research.loc[research["market_year_end"].eq(2026)]
    assert current_research["world_weekly_mt"].tolist() == [12, 5]
    assert current_research["china_weekly_mt"].tolist() == [12, 0]
    assert current_research["non_china_weekly_mt"].tolist() == [0, 5]
    revisions = pd.read_parquet(fgis_paths(runtime, "unused")["revision"])
    assert {"weekly_mt", "cumulative_mt"}.issubset(set(revisions["field_name"]))
    manifest = json.loads(
        fgis_paths(runtime, "unused")["stable_manifest"].read_text(encoding="utf-8")
    )
    assert manifest["active_update_provenance"]["source_channel"] == FGIS_YEARLY_SOURCE_CHANNEL
    assert "sruw-w49i" not in manifest["active_update_provenance"]["source_file"]


def test_calendar_year_switch_freezes_old_year_and_continues_my_week(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    seed_baseline(
        runtime,
        [
            socrata_row(cert_date="2026-12-31", week_ending="2026-12-31", destination="CHINA", mt=10),
        ],
    )
    run_yearly(
        runtime,
        [yearly_row(thursday="20261231", cert_date="20261231", destination="CHINA", mt="10")],
        year=2026,
        batch="cy2026",
    )
    switched, _ = run_yearly(
        runtime,
        [yearly_row(thursday="20270107", cert_date="20270102", destination="CHINA", mt="5")],
        year=2027,
        batch="cy2027",
    )
    stable = pd.read_parquet(fgis_paths(runtime, "unused")["stable"])
    annual = stable.loc[stable["market_year_end"].eq(2027)].sort_values("my_week")
    assert switched["source_file"] == "CY2027.csv"
    assert switched["revision_count"] == 0
    assert annual["my_week"].tolist() == [1, 2]
    assert annual["weekly_mt"].tolist() == [10, 5]
    assert annual["cumulative_mt"].tolist() == [10, 15]
    assert annual.iloc[0]["source_dataset_id"] == FGIS_DATASET_ID
    manifest = json.loads(
        fgis_paths(runtime, "unused")["stable_manifest"].read_text(encoding="utf-8")
    )
    assert manifest["historical_baseline_provenance"]["baseline_mode"] == "calendar_year_switch"
    assert manifest["revision_coverage"]["calendar_year"] == 2027
    assert manifest["revision_coverage"]["tracks_prior_calendar_year_revisions"] is False


def test_shared_thursday_across_calendar_files_is_new_fact_not_revision(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    seed_baseline(
        runtime,
        [
            socrata_row(cert_date="2025-12-31", week_ending="2026-01-01", destination="CHINA", mt=10),
        ],
    )
    run_yearly(
        runtime,
        [yearly_row(thursday="20260101", cert_date="20251231", destination="CHINA", mt="10")],
        year=2025,
        batch="cy2025",
    )
    switched, _ = run_yearly(
        runtime,
        [
            yearly_row(thursday="20260101", cert_date="20260101", destination="CHINA", mt="5"),
            yearly_row(thursday="20260108", cert_date="20260102", destination="CHINA", mt="7"),
        ],
        year=2026,
        batch="cy2026",
    )
    annual = pd.read_parquet(fgis_paths(runtime, "unused")["stable"])
    annual = annual.loc[annual["market_year_end"].eq(2026)].sort_values("my_week")
    assert switched["revision_count"] == 0
    assert annual["my_week"].tolist() == [1, 2]
    assert annual["weekly_mt"].tolist() == [15, 7]
    assert annual["cumulative_mt"].tolist() == [15, 22]
    assert annual.iloc[0]["source_dataset_id"] == "historical_baseline+yearly_export_grain_csv"


def test_changed_yearly_candidate_failure_preserves_stable(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    seed_baseline(
        runtime,
        [socrata_row(cert_date="2026-07-30", week_ending="2026-07-30", destination="CHINA", mt=10)],
    )
    run_yearly(runtime, [yearly_row(mt="10")], year=2026, batch="good")
    paths = fgis_paths(runtime, "unused")
    stable_bytes = paths["stable"].read_bytes()
    source = b"changed-invalid-source"
    bad_fetch = FgisFetchResult(
        records=[socrata_row(cert_date="2026-07-30", week_ending="2026-07-30", destination="CHINA", mt=-1)],
        fetch_time_utc="2026-08-08T01:00:00Z",
        dataset_updated_at="2026-08-08T01:00:00Z",
        pages=[{"row_count": 1}],
        query_scope={"calendar_year": 2026},
        source_channel=FGIS_YEARLY_SOURCE_CHANNEL,
        calendar_year=2026,
        source_file="CY2026.csv",
        source_url="https://fgisonline.ams.usda.gov/exportgrainreport/CY2026.csv",
        source_sha256=sha256_bytes(source),
        http_status=200,
        content_length=len(source),
        source_bytes=source,
    )

    class BadAdapter:
        def fetch_soybeans(self, **_: Any) -> FgisFetchResult:
            return bad_fetch

    with pytest.raises(PipelineError, match="non-negative"):
        run_fgis_pipeline(
            runtime_root=runtime,
            adapter=BadAdapter(),
            git_head=GIT_HEAD,
            batch_id="bad",
        )
    assert paths["stable"].read_bytes() == stable_bytes
    status = json.loads(paths["status"].read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert status["last_success"]["batch_id"] == "good"
