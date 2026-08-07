from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import requests

from agri_research_agent.soybean_exports.common import PipelineError
from agri_research_agent.soybean_exports.fgis import (
    FGIS_RAW_FIELDS,
    FgisAdapter,
    FgisAdapterError,
    build_fgis_research_view,
    detect_fgis_revisions,
    fgis_paths,
    normalize_fgis_records,
    run_fgis_pipeline,
    validate_fgis_stable,
)


GIT_HEAD = "a" * 40


def raw_row(
    *,
    cert_date: str = "2025-09-01",
    week_ending: str = "2025-09-04",
    destination: str = "CHINA",
    mt: int = 10,
    grain: str = "SOYBEANS",
) -> dict[str, Any]:
    row = {
        "date": week_ending,
        "cert_date": cert_date,
        "week": "36",
        "month": cert_date[5:7].lstrip("0") or "0",
        "quarter": "3",
        "year": cert_date[:4],
        "type_shipm": "BU",
        "type_carrier": "1",
        "type_carrier_text": "VESSEL",
        "carrier_name": "PUBLIC",
        "grain": grain,
        "grade": "1",
        "class": "YSB",
        "subclass": None,
        "destination": destination,
        "port": "GULF",
        "ams_reg": "GULF",
        "fgis_reg": "GULF",
        "state": "LA",
        "mt": str(mt),
        "pounds": str(mt * 2205),
        "field_office": "NEW ORLEANS",
    }
    assert set(row) == set(FGIS_RAW_FIELDS)
    return row


class Response:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.content = json.dumps(payload, sort_keys=True).encode()

    def json(self) -> Any:
        return self._payload


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


def metadata(*, missing: str | None = None) -> dict[str, Any]:
    fields = [field for field in FGIS_RAW_FIELDS if field != missing]
    return {"columns": [{"fieldName": field} for field in fields], "rowsUpdatedAt": 1_700_000_000}


class StaticAdapter:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records

    def fetch_soybeans(self, **_: Any) -> Any:
        from agri_research_agent.soybean_exports.fgis import FgisFetchResult

        return FgisFetchResult(
            records=self.records,
            fetch_time_utc="2026-08-07T00:00:00Z",
            dataset_updated_at="2026-08-03T16:48:42Z",
            pages=[{"offset": 0, "row_count": len(self.records), "response_sha256": "B" * 64}],
            query_scope={"grain": "SOYBEANS"},
        )


def normalize(rows: list[dict[str, Any]], batch: str = "batch") -> pd.DataFrame:
    return normalize_fgis_records(
        rows,
        batch_id=batch,
        raw_snapshot_id=batch,
        raw_snapshot_sha256="C" * 64,
        fetch_time_utc="2026-08-07T00:00:00Z",
        dataset_updated_at="2026-08-03T16:48:42Z",
    )


def test_adapter_uses_fixed_pagination_stable_order_timeout_and_strict_filter() -> None:
    rows = [raw_row(destination="CHINA"), raw_row(destination="TAIWAN"), raw_row(destination="JAPAN")]
    session = Session([Response(metadata()), Response(rows[:2]), Response(rows[2:])])
    result = FgisAdapter(session=session, page_size=2, timeout_seconds=17).fetch_soybeans()

    assert len(result.records) == 3
    assert [page["row_count"] for page in result.pages] == [2, 1]
    assert session.calls[1]["params"]["$order"] == ":id"
    assert session.calls[1]["params"]["$where"] == "grain='SOYBEANS'"
    assert session.calls[1]["timeout"] == 17
    assert session.calls[2]["params"]["$offset"] == 2


def test_adapter_can_explicitly_ignore_environment_proxy() -> None:
    adapter = FgisAdapter(use_environment_proxy=False)
    assert adapter.session.trust_env is False


def test_adapter_materializes_omitted_nullable_socrata_fields() -> None:
    row = raw_row()
    for field in ("carrier_name", "grade", "class", "subclass", "state"):
        row.pop(field)
    adapter = FgisAdapter(
        session=Session([Response(metadata()), Response([row])]), page_size=10
    )
    result = adapter.fetch_soybeans()
    assert all(result.records[0][field] is None for field in ("carrier_name", "grade", "class", "subclass", "state"))


def test_adapter_rejects_timeout_http_schema_and_non_soybeans() -> None:
    with pytest.raises(FgisAdapterError, match="Timeout"):
        FgisAdapter(session=Session(error=requests.Timeout())).fetch_soybeans()
    with pytest.raises(FgisAdapterError, match="HTTP status 503"):
        FgisAdapter(session=Session([Response({}, 503)])).fetch_soybeans()
    with pytest.raises(FgisAdapterError, match="metadata missing"):
        FgisAdapter(session=Session([Response(metadata(missing="cert_date"))])).fetch_soybeans()
    with pytest.raises(FgisAdapterError, match="strict SOYBEANS"):
        FgisAdapter(
            session=Session([Response(metadata()), Response([raw_row(grain="SOYBEAN MEAL")])])
        ).fetch_soybeans()


def test_cert_date_assigns_market_year_before_cross_boundary_week_aggregation() -> None:
    stable = normalize(
        [
            raw_row(cert_date="2025-08-31", week_ending="2025-09-04", destination="CHINA", mt=11),
            raw_row(cert_date="2025-09-01", week_ending="2025-09-04", destination="CHINA", mt=13),
            raw_row(cert_date="2025-09-01", week_ending="2025-09-04", destination="CHINA", mt=7),
        ]
    )

    assert stable["market_year_end"].tolist() == [2025, 2026]
    assert stable["weekly_mt"].tolist() == [11, 20]
    assert stable["source_row_count"].tolist() == [1, 2]
    assert stable["week_ending_date"].nunique() == 1


def test_august_31_and_september_1_are_different_market_years() -> None:
    stable = normalize(
        [
            raw_row(cert_date="2026-08-31", week_ending="2026-09-03"),
            raw_row(cert_date="2026-09-01", week_ending="2026-09-03"),
        ]
    )
    assert stable["market_year_label"].tolist() == ["2025/26", "2026/27"]


@pytest.mark.parametrize("bucket_count", [53, 54])
def test_my_week_supports_53_and_54_buckets(bucket_count: int) -> None:
    rows = []
    start = pd.Timestamp("2025-09-01")
    for index in range(bucket_count):
        inspection = start + pd.Timedelta(days=min(index * 6, 364))
        week = pd.Timestamp("2025-09-04") + pd.Timedelta(days=index * 7)
        rows.append(raw_row(cert_date=inspection.date().isoformat(), week_ending=week.date().isoformat(), destination="JAPAN"))
    stable = normalize(rows)
    assert stable["my_week"].tolist() == list(range(1, bucket_count + 1))
    assert validate_fgis_stable(stable)["passed"] is True


def test_partial_year_and_destination_research_closure_with_zero_china() -> None:
    stable = normalize(
        [
            raw_row(destination="HONG KONG", mt=20),
            raw_row(destination="TAIWAN", mt=30),
            raw_row(destination="JAPAN", mt=50),
        ]
    )
    research = build_fgis_research_view(stable)
    latest = research.iloc[0]
    assert latest["world_weekly_mt"] == 100
    assert latest["china_weekly_mt"] == 0
    assert latest["non_china_weekly_mt"] == 100
    assert set(stable["destination"]) == {"HONG KONG", "TAIWAN", "JAPAN"}
    assert stable["my_week"].max() == 1


def test_weekly_cumulative_and_duplicate_aggregation() -> None:
    stable = normalize(
        [
            raw_row(week_ending="2025-09-04", destination="CHINA", mt=10),
            raw_row(week_ending="2025-09-04", destination="CHINA", mt=15),
            raw_row(cert_date="2025-09-08", week_ending="2025-09-11", destination="CHINA", mt=5),
        ]
    )
    assert stable["weekly_mt"].tolist() == [25, 5]
    assert stable["cumulative_mt"].tolist() == [25, 30]
    assert stable["my_week"].tolist() == [1, 2]


def test_revision_and_no_revision() -> None:
    old = normalize([raw_row(mt=10)], "old")
    unchanged = normalize([raw_row(mt=10)], "new")
    revised = normalize([raw_row(mt=12)], "newer")
    assert detect_fgis_revisions(old, unchanged, detected_at_utc="2026-08-08T00:00:00Z").empty
    changes = detect_fgis_revisions(old, revised, detected_at_utc="2026-08-08T00:00:00Z")
    assert set(changes["field_name"]) == {"weekly_mt", "cumulative_mt"}
    assert set(changes["delta"]) == {2}
    assert set(changes["old_snapshot_identity"]) == {"C" * 64}


def test_pipeline_raw_candidate_stable_backup_revision_and_idempotence(tmp_path: Path) -> None:
    first = run_fgis_pipeline(
        runtime_root=tmp_path,
        adapter=StaticAdapter([raw_row(mt=10)]),
        git_head=GIT_HEAD,
        batch_id="first",
    )
    paths = fgis_paths(tmp_path, "first")
    first_bytes = paths["stable"].read_bytes()
    assert first["status"] == "initialized"
    assert paths["stable"].is_file()
    assert Path(first["raw_snapshot_dir"]).joinpath("records.jsonl.gz").is_file()
    manifest = json.loads(paths["stable_manifest"].read_text(encoding="utf-8"))
    assert manifest["parquet_schema_sha256"]
    assert manifest["source_release_time_raw"] is None
    assert manifest["source_release_timezone"] is None
    assert manifest["stable_relative_path"].startswith("01_data/processed/")
    assert manifest["revision_coverage"]["mode"] == "candidate_vs_current"

    second = run_fgis_pipeline(
        runtime_root=tmp_path,
        adapter=StaticAdapter([raw_row(mt=10)]),
        git_head=GIT_HEAD,
        batch_id="second",
    )
    assert second["status"] == "no_change"
    assert paths["stable"].read_bytes() == first_bytes

    third = run_fgis_pipeline(
        runtime_root=tmp_path,
        adapter=StaticAdapter([raw_row(mt=12)]),
        git_head=GIT_HEAD,
        batch_id="third",
    )
    assert third["status"] == "updated"
    backup = Path(third["backup_dir"]) / paths["stable"].name
    assert backup.read_bytes() == first_bytes
    revisions = pd.read_parquet(paths["revision"])
    assert set(revisions["field_name"]) == {"weekly_mt", "cumulative_mt"}


def test_immutable_raw_same_batch_and_candidate_failure_preserve_stable(tmp_path: Path) -> None:
    run_fgis_pipeline(
        runtime_root=tmp_path,
        adapter=StaticAdapter([raw_row(mt=10)]),
        git_head=GIT_HEAD,
        batch_id="same",
    )
    paths = fgis_paths(tmp_path, "same")
    stable_bytes = paths["stable"].read_bytes()
    with pytest.raises(FileExistsError, match="already exists"):
        run_fgis_pipeline(
            runtime_root=tmp_path,
            adapter=StaticAdapter([raw_row(mt=10)]),
            git_head=GIT_HEAD,
            batch_id="same",
        )
    assert paths["stable"].read_bytes() == stable_bytes

    with pytest.raises(PipelineError, match="non-negative"):
        run_fgis_pipeline(
            runtime_root=tmp_path,
            adapter=StaticAdapter([raw_row(mt=-1)]),
            git_head=GIT_HEAD,
            batch_id="bad",
        )
    assert paths["stable"].read_bytes() == stable_bytes
    status = json.loads(paths["status"].read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert status["latest_attempt"]["batch_id"] == "bad"
    assert status["last_success"]["batch_id"] == "same"
    assert status["last_success"]["source_latest_week"] == "2025-09-04"
