from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from import_profit import build_reuters_candidate as candidate
from test_import_profit_reuters_adapter import (
    CBOT_TABLE,
    FX_TABLE,
    cbot_values,
    fx_values,
    synthetic_sql,
    write_sql,
)


FIXED_TIME = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def build_good_candidate(tmp_path: Path, name: str = "candidate"):
    source = write_sql(
        tmp_path,
        synthetic_sql(
            cbot_rows=[
                cbot_values(
                    "1969-06-26",
                    {"CBOT大豆 2007年11月": 633},
                ),
                cbot_values(
                    "2026-01-02",
                    {
                        "CBOT大豆 2027年1月": 1200.5,
                        "CBOT大豆 2027年3月": None,
                    },
                ),
            ],
            fx_rows=[fx_values("2026-01-02")],
        ),
    )
    output = tmp_path / name
    result = candidate.build_reuters_candidate(
        source,
        output,
        generated_at=FIXED_TIME,
    )
    return source, output, result


def test_candidate_writes_verified_parquet_manifest_and_quality_report(tmp_path: Path) -> None:
    source, output, result = build_good_candidate(tmp_path)

    assert sorted(path.name for path in output.iterdir()) == [
        "cbot_soybean_daily.parquet",
        "manifest.json",
        "quality_report.json",
        "usdcny_forward_daily.parquet",
    ]
    cbot = pq.read_table(output / candidate.CBOT_FILENAME)
    fx = pq.read_table(output / candidate.FX_FILENAME)
    assert cbot.schema == candidate.CBOT_SCHEMA
    assert fx.schema == candidate.FX_SCHEMA
    assert cbot.schema.names == [
        "market_date",
        "contract_year",
        "contract_month",
        "price_cents_per_bushel",
        "lead_months",
        "exchange_quality_status",
        "is_usable",
        "eligible_for_import_profit",
        "source_table",
        "source_column",
        "source_statement_index",
        "source_snapshot_sha256",
    ]
    assert cbot.num_rows == 2
    assert fx.num_rows == 13
    assert cbot.column("is_usable").to_pylist() == [False, True]
    assert cbot.column("eligible_for_import_profit").to_pylist() == [False, True]
    assert result["record_counts"] == {CBOT_TABLE: 2, FX_TABLE: 13}

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    quality = json.loads((output / "quality_report.json").read_text(encoding="utf-8"))
    assert manifest["source_filename"] == source.name
    assert manifest["source_sha256"] == sha256(source)
    assert manifest["source_export_time"] == "28/07/2026 09:23:50"
    assert manifest["ddl_fingerprints"][CBOT_TABLE]["field_count"] == 204
    assert manifest["ddl_fingerprints"][FX_TABLE]["field_count"] == 14
    assert manifest["candidate_status"] == "passed_with_warnings"
    assert manifest["fatal_issue_count"] == 0
    assert manifest["known_extreme_anomaly_count"] == 1
    assert manifest["unknown_extreme_anomaly_count"] == 0
    assert manifest["cbot_import_profit_eligible_records"] == 1
    assert manifest["output_file_names"] == [
        candidate.CBOT_FILENAME,
        candidate.FX_FILENAME,
        candidate.QUALITY_FILENAME,
        candidate.MANIFEST_FILENAME,
    ]
    assert quality["candidate_pass"] is True
    assert quality["candidate_status"] == "passed_with_warnings"
    assert quality["fatal_issues"] == []
    assert any(
        warning["code"] == "known_extreme_source_anomaly"
        for warning in quality["warnings"]
    )


def test_manifest_contains_only_safe_filenames_and_correct_output_hashes(tmp_path: Path) -> None:
    _, output, _ = build_good_candidate(tmp_path)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    serialized = json.dumps(manifest, ensure_ascii=False)

    assert str(tmp_path) not in serialized
    assert "C:\\" not in serialized
    assert "/home/" not in serialized
    assert "Source Server" not in serialized
    for filename, identity in manifest["output_files"].items():
        path = output / filename
        assert identity == {
            "filename": filename,
            "size": path.stat().st_size,
            "sha256": sha256(path),
        }
        assert manifest["output_sizes"][filename] == path.stat().st_size
        assert manifest["output_sha256"][filename] == sha256(path)
    assert sha256(manifest_path)


def test_output_business_content_is_stable_across_distinct_candidate_directories(
    tmp_path: Path,
) -> None:
    source = write_sql(tmp_path, synthetic_sql())
    first = tmp_path / "first"
    second = tmp_path / "second"
    candidate.build_reuters_candidate(source, first, generated_at=FIXED_TIME)
    candidate.build_reuters_candidate(source, second, generated_at=FIXED_TIME)

    assert pq.read_table(first / candidate.CBOT_FILENAME).equals(
        pq.read_table(second / candidate.CBOT_FILENAME)
    )
    assert pq.read_table(first / candidate.FX_FILENAME).equals(
        pq.read_table(second / candidate.FX_FILENAME)
    )
    first_manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    second_manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    assert first_manifest == second_manifest


def test_mid_publish_failure_leaves_no_formal_or_temporary_candidate_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = write_sql(tmp_path, synthetic_sql())
    output = tmp_path / "failed"
    real_replace = candidate.os.replace
    calls = 0

    def fail_second_replace(source_path, target_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("forced replace failure")
        return real_replace(source_path, target_path)

    monkeypatch.setattr(candidate.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="forced replace failure"):
        candidate.build_reuters_candidate(source, output, generated_at=FIXED_TIME)
    assert output.is_dir()
    assert list(output.iterdir()) == []


def test_rejected_quality_candidate_writes_no_files(tmp_path: Path) -> None:
    source = write_sql(
        tmp_path,
        synthetic_sql(
            cbot_rows=[cbot_values(overrides={"CBOT大豆 2027年1月": 0})]
        ),
    )
    output = tmp_path / "rejected"
    with pytest.raises(candidate.ReutersCandidateBuildError, match="quality report rejected"):
        candidate.build_reuters_candidate(source, output, generated_at=FIXED_TIME)
    assert output.is_dir()
    assert list(output.iterdir()) == []


def test_unknown_extreme_anomaly_is_fatal_and_writes_no_files(tmp_path: Path) -> None:
    source = write_sql(
        tmp_path,
        synthetic_sql(
            cbot_rows=[
                cbot_values(
                    "1969-06-26",
                    {"CBOT大豆 2008年7月": 633},
                )
            ]
        ),
    )
    output = tmp_path / "unknown-extreme"
    with pytest.raises(candidate.ReutersCandidateBuildError, match="quality report rejected"):
        candidate.build_reuters_candidate(source, output, generated_at=FIXED_TIME)
    assert output.is_dir()
    assert list(output.iterdir()) == []


def test_clean_candidate_status_is_passed(tmp_path: Path) -> None:
    source = write_sql(tmp_path, synthetic_sql())
    output = tmp_path / "clean"
    result = candidate.build_reuters_candidate(source, output, generated_at=FIXED_TIME)
    assert result["quality_report"]["candidate_status"] == "passed"
    manifest = json.loads((output / candidate.MANIFEST_FILENAME).read_text("utf-8"))
    assert manifest["candidate_status"] == "passed"
    assert manifest["warning_count"] == 0


def test_extended_and_extreme_window_warnings_do_not_reject_candidate(
    tmp_path: Path,
) -> None:
    source = write_sql(
        tmp_path,
        synthetic_sql(
            cbot_rows=[
                cbot_values(
                    "2024-12-31",
                    {"CBOT大豆 2028年7月": 1050.25},
                ),
                cbot_values(
                    "2002-06-21",
                    {"CBOT大豆 2006年7月": 600},
                ),
            ]
        ),
    )
    output = tmp_path / "warning-candidate"
    result = candidate.build_reuters_candidate(source, output, generated_at=FIXED_TIME)
    assert result["quality_report"]["candidate_status"] == "passed_with_warnings"
    assert {
        item["code"] for item in result["quality_report"]["warnings"]
    } == {"extended_window_unverified", "extreme_window_warning"}
    assert sorted(path.name for path in output.iterdir()) == [
        candidate.CBOT_FILENAME,
        candidate.MANIFEST_FILENAME,
        candidate.QUALITY_FILENAME,
        candidate.FX_FILENAME,
    ]


def test_source_identity_change_is_fatal_and_leaves_no_candidate_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = write_sql(tmp_path, synthetic_sql())
    output = tmp_path / "identity-changed"
    real_inspect = candidate.inspect_source_identity

    def changed_identity(path):
        return replace(real_inspect(path), sha256="B" * 64)

    monkeypatch.setattr(candidate, "inspect_source_identity", changed_identity)
    with pytest.raises(candidate.ReutersCandidateBuildError, match="identity changed"):
        candidate.build_reuters_candidate(source, output, generated_at=FIXED_TIME)
    assert output.is_dir()
    assert list(output.iterdir()) == []


def test_candidate_requires_empty_output_directory(tmp_path: Path) -> None:
    source = write_sql(tmp_path, synthetic_sql())
    output = tmp_path / "occupied"
    output.mkdir()
    (output / "keep.txt").write_text("user file", encoding="utf-8")
    with pytest.raises(candidate.ReutersCandidateBuildError, match="must be empty"):
        candidate.build_reuters_candidate(source, output, generated_at=FIXED_TIME)
    assert (output / "keep.txt").read_text(encoding="utf-8") == "user file"
