from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.soybean_exports.common import PipelineError
from agri_research_agent.soybean_exports.fas import fas_paths, run_fas_pipeline
from agri_research_agent.soybean_exports.fgis import fgis_paths, run_fgis_pipeline
from agri_research_agent.soybean_exports.research import (
    build_soybean_export_research_payload,
    read_usda_psd_soybean_exports_mt,
)
from test_fas_data_layer import StaticAdapter as FasStaticAdapter
from test_fas_data_layer import fas_row
from test_fgis_data_layer import StaticAdapter as FgisStaticAdapter
from test_fgis_data_layer import raw_row


GIT_HEAD = "c" * 40


def make_usda(root: Path, *, report_month: str = "2026-07", value: float = 4136.8) -> Path:
    project = root / "USDA"
    data = project / "public" / "data"
    snapshot = data / "snapshots" / "usda_psd" / report_month / "matrix"
    current = data / "matrix"
    snapshot.mkdir(parents=True)
    current.mkdir(parents=True)
    (data / "report_version.json").write_text(
        json.dumps({"currentReportMonth": report_month, "previousReportMonth": "2026-06"}),
        encoding="utf-8",
    )
    matrix = {
        "commodityCode": "2222000",
        "commodity": "Oilseed, Soybean",
        "countryCode": "US",
        "country": "United States",
        "years": [2024, 2025, 2026],
        "rows": [{"name": "出口量", "values": [5000, value, 4517.8]}],
    }
    content = json.dumps(matrix, ensure_ascii=False)
    (snapshot / "2222000_US.json").write_text(content, encoding="utf-8")
    (current / "2222000_US.json").write_text(content, encoding="utf-8")
    return project


def test_psd_reader_report_identity_year_matching_and_unit_conversion(tmp_path: Path) -> None:
    usda = make_usda(tmp_path)
    result = read_usda_psd_soybean_exports_mt(usda, target_market_year_end=2026)
    assert result["report_month"] == "2026-07"
    assert result["market_year_start"] == 2025
    assert result["exports_mt"] == 41_368_000
    assert len(result["snapshot_sha256"]) == 64


@pytest.mark.parametrize("mode", ["missing_report", "missing_year", "missing_field", "snapshot_mismatch"])
def test_psd_reader_rejects_missing_or_inconsistent_inputs(tmp_path: Path, mode: str) -> None:
    usda = make_usda(tmp_path)
    data = usda / "public" / "data"
    if mode == "missing_report":
        (data / "report_version.json").unlink()
        expected = "report_version"
        target = 2026
    elif mode == "missing_year":
        expected = "target market year"
        target = 2030
    elif mode == "missing_field":
        path = data / "snapshots/usda_psd/2026-07/matrix/2222000_US.json"
        matrix = json.loads(path.read_text(encoding="utf-8"))
        matrix["rows"] = []
        text = json.dumps(matrix)
        path.write_text(text, encoding="utf-8")
        (data / "matrix/2222000_US.json").write_text(text, encoding="utf-8")
        expected = "Exports row"
        target = 2026
    else:
        (data / "matrix/2222000_US.json").write_text("{}", encoding="utf-8")
        expected = "does not match"
        target = 2026
    with pytest.raises(PipelineError, match=expected):
        read_usda_psd_soybean_exports_mt(usda, target_market_year_end=target)


def test_unified_payload_allows_different_source_weeks_and_returns_four_kpis(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    fgis_rows = [
        raw_row(cert_date="2024-09-01", week_ending="2024-09-05", destination="CHINA", mt=10),
        raw_row(cert_date="2025-09-01", week_ending="2025-09-04", destination="CHINA", mt=20),
        raw_row(cert_date="2025-09-08", week_ending="2025-09-11", destination="JAPAN", mt=80),
    ]
    fas_rows = [fas_row(country=5700), fas_row(country=9990)]
    run_fgis_pipeline(runtime_root=runtime, adapter=FgisStaticAdapter(fgis_rows), git_head=GIT_HEAD, batch_id="fgis")
    run_fas_pipeline(runtime_root=runtime, adapter=FasStaticAdapter(fas_rows), git_head=GIT_HEAD, batch_id="fas")
    fgis = pd.read_parquet(fgis_paths(runtime, "x")["stable"])
    fas = pd.read_parquet(fas_paths(runtime, "x")["stable"])
    payload = build_soybean_export_research_payload(
        fgis_stable=fgis,
        fas_stable=fas,
        usda_project_root=make_usda(tmp_path),
    )
    assert payload["fgis"]["latest_week"] == "2025-09-11"
    assert payload["fas_current"]["latest_week"] == "2026-07-30"
    assert payload["fas_current"]["sales_progress_denominator"]["exports_mt"] == 41_368_000
    assert payload["kpis"]["next_my_total_sales_mt"] == 80
    assert payload["kpis"]["china_next_my_total_purchases_mt"] == 40
    assert set(payload["kpis"]) == {
        "cumulative_export_inspections_yoy_pct",
        "current_my_sales_progress_pct",
        "next_my_total_sales_mt",
        "china_next_my_total_purchases_mt",
    }


def test_pipeline_failures_status_backup_revision_and_stable_are_independent(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    fgis_initial = run_fgis_pipeline(
        runtime_root=runtime,
        adapter=FgisStaticAdapter([raw_row(mt=10)]),
        git_head=GIT_HEAD,
        batch_id="fgis-ok",
    )
    fas_initial = run_fas_pipeline(
        runtime_root=runtime,
        adapter=FasStaticAdapter([fas_row(country=5700), fas_row(country=9990)]),
        git_head=GIT_HEAD,
        batch_id="fas-ok",
    )
    fgis = fgis_paths(runtime, "x")
    fas = fas_paths(runtime, "x")
    fgis_bytes = fgis["stable"].read_bytes()
    fas_bytes = fas["stable"].read_bytes()
    fas_status = fas["status"].read_bytes()

    with pytest.raises(PipelineError):
        run_fgis_pipeline(
            runtime_root=runtime,
            adapter=FgisStaticAdapter([raw_row(mt=-1)]),
            git_head=GIT_HEAD,
            batch_id="fgis-bad",
        )
    assert fas["stable"].read_bytes() == fas_bytes
    assert fas["status"].read_bytes() == fas_status

    bad_fas = [fas_row(country=5700), fas_row(country=9990)]
    bad_fas[0]["unitId"] = 2
    fgis_status = fgis["status"].read_bytes()
    with pytest.raises(PipelineError):
        run_fas_pipeline(
            runtime_root=runtime,
            adapter=FasStaticAdapter(bad_fas),
            git_head=GIT_HEAD,
            batch_id="fas-bad",
        )
    assert fgis["stable"].read_bytes() == fgis_bytes
    assert fgis["status"].read_bytes() == fgis_status
    assert Path(fgis_initial["backup_dir"]).parent != Path(fas_initial["backup_dir"]).parent
    assert fgis["revision"] != fas["revision"]
