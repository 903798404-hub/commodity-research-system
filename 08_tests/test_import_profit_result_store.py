from datetime import datetime, timezone
import hashlib
import json

import pandas as pd
import pyarrow.parquet as pq
import pytest

import agri_research_agent.import_profit.result_store as result_store
from agri_research_agent.import_profit.result_store import (
    MANIFEST_FILENAME,
    QUALITY_FILENAME,
    RESULT_FILENAME,
    RESULT_SCHEMA,
    SNAPSHOT_FILENAME,
    SNAPSHOT_SCHEMA,
    ResultStoreValidationError,
    ResultStoreWriteError,
    write_soybean_result_candidate,
)
from agri_research_agent.import_profit.parameter_snapshot import (
    build_parameter_snapshot,
)
from agri_research_agent.import_profit.standard_io import StandardFileIdentity
from agri_research_agent.pipelines.import_profit_results import (
    CandidateStatus,
    build_soybean_result_candidate,
)
from test_import_profit_recalculation import CONFIG, cbot, cnf, dce, fx, key


FIXED_TIME = datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc)


def input_identity(
    filename: str,
    *,
    exists: bool = True,
    count: int = 1,
) -> StandardFileIdentity:
    return StandardFileIdentity(
        filename=filename,
        exists=exists,
        size_bytes=100 if exists else None,
        sha256="A" * 64 if exists else None,
        schema_version="test-v1",
        schema_fingerprint="B" * 64,
        record_count=count if exists else 0,
        earliest_date=key().business_date if exists else None,
        latest_date=key().business_date if exists else None,
        duplicate_key_count=0,
    )


def candidate(*, complete: bool = True, missing_cnf_file: bool = False):
    business_key = key()
    return build_soybean_result_candidate(
        [business_key],
        config=CONFIG,
        cnf_records=[cnf(business_key)] if complete else [],
        cbot_records=[cbot()],
        fx_records=[fx(5)],
        dce_records=[dce("M2701", 3200), dce("Y2701", 8000)],
        calculated_at=FIXED_TIME,
        generated_at=FIXED_TIME,
        input_files=(
            input_identity(
                "cnf.parquet",
                exists=not missing_cnf_file,
                count=1 if complete else 0,
            ),
            input_identity("cbot.parquet"),
            input_identity("fx.parquet"),
            input_identity("dce.parquet", count=2),
        ),
        synthetic_input=True,
    )


def file_sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def test_fixed_schemas_dual_readback_and_output_sha(tmp_path) -> None:
    output = tmp_path / "candidate"
    write_result = write_soybean_result_candidate(candidate(), output)
    assert {item.filename for item in write_result.output_files} == {
        SNAPSHOT_FILENAME,
        RESULT_FILENAME,
        MANIFEST_FILENAME,
        QUALITY_FILENAME,
    }
    snapshot_arrow = pq.read_table(output / SNAPSHOT_FILENAME)
    result_arrow = pq.read_table(output / RESULT_FILENAME)
    assert snapshot_arrow.schema == SNAPSHOT_SCHEMA
    assert result_arrow.schema == RESULT_SCHEMA
    assert tuple(pd.read_parquet(output / SNAPSHOT_FILENAME).columns) == tuple(
        SNAPSHOT_SCHEMA.names
    )
    assert tuple(pd.read_parquet(output / RESULT_FILENAME).columns) == tuple(
        RESULT_SCHEMA.names
    )
    snapshot_row = snapshot_arrow.to_pylist()[0]
    assert snapshot_row["soymeal_contract_identity_status"] == "legacy_unknown"
    assert snapshot_row["soymeal_source_contract_code"] is None
    assert snapshot_row["soymeal_source_delivery_month"] is None
    by_name = {item.filename: item for item in write_result.output_files}
    for name in by_name:
        assert by_name[name].sha256 == file_sha(output / name)


def test_incomplete_values_remain_null_and_reasons_are_arrow_lists(tmp_path) -> None:
    output = tmp_path / "incomplete"
    write_soybean_result_candidate(candidate(complete=False), output)
    snapshot = pq.read_table(output / SNAPSHOT_FILENAME).to_pylist()[0]
    result = pq.read_table(output / RESULT_FILENAME).to_pylist()[0]
    assert snapshot["cnf_cents_per_bushel"] is None
    assert snapshot["missing_reasons"] == ["missing_cnf"]
    assert result["usd_cost_per_tonne"] is None
    assert result["duty_paid_cost_cny_per_tonne"] is None
    assert result["net_crush_margin_cny_per_tonne"] is None
    assert result["missing_reasons"] == ["missing_cnf"]
    assert result["calculated_at"] == FIXED_TIME


def test_manifest_quality_and_candidate_status_are_bounded(tmp_path) -> None:
    output = tmp_path / "reports"
    write_soybean_result_candidate(
        candidate(complete=False, missing_cnf_file=True),
        output,
    )
    manifest = json.loads((output / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    quality = json.loads((output / QUALITY_FILENAME).read_text(encoding="utf-8"))
    assert manifest["candidate_status"] == "passed_with_incomplete"
    assert manifest["synthetic_input"] is True
    assert manifest["requested_key_count"] == 1
    assert manifest["success_count"] == 0
    assert manifest["incomplete_count"] == 1
    provenance = build_parameter_snapshot(CONFIG)
    assert manifest["parameter_snapshot"] == provenance.snapshot
    assert manifest["parameter_hash"] == provenance.parameter_hash
    snapshot_rows = pq.read_table(output / SNAPSHOT_FILENAME).to_pylist()
    result_rows = pq.read_table(output / RESULT_FILENAME).to_pylist()
    assert {
        row["parameter_hash"] for row in snapshot_rows
    } == {provenance.parameter_hash}
    assert {
        row["parameter_hash"] for row in result_rows
    } == {provenance.parameter_hash}
    cnf_identity = next(
        item for item in manifest["input_files"] if item["filename"] == "cnf.parquet"
    )
    assert cnf_identity["exists"] is False
    assert cnf_identity["sha256"] is None
    assert "\\" not in json.dumps(manifest, ensure_ascii=False)
    assert ":/" not in json.dumps(manifest, ensure_ascii=False)
    assert len(quality["key_results"]) == 1
    assert quality["fatal_issues"] == []
    assert quality["warnings"] == ["one_or_more_requested_keys_are_incomplete"]
    assert quality["candidate_status"] == "passed_with_incomplete"


def test_passed_status_and_failed_enum_are_explicit(tmp_path) -> None:
    output = tmp_path / "passed"
    write_result = write_soybean_result_candidate(candidate(), output)
    assert write_result.candidate_status == "passed"
    assert CandidateStatus.FAILED.value == "failed"


def test_output_rejects_nonempty_or_repository_directory(tmp_path) -> None:
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "existing.txt").write_text("existing", encoding="utf-8")
    with pytest.raises(ResultStoreValidationError, match="empty"):
        write_soybean_result_candidate(candidate(), nonempty)

    repository = tmp_path / "repo"
    repository.mkdir()
    with pytest.raises(ResultStoreValidationError, match="outside"):
        write_soybean_result_candidate(
            candidate(),
            repository / "candidate",
            repository_root=repository,
        )


def test_replace_failure_removes_all_partial_and_temp_outputs(
    tmp_path,
    monkeypatch,
) -> None:
    output = tmp_path / "replace-failure"
    real_replace = result_store.os.replace
    calls = 0

    def failing_replace(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(result_store.os, "replace", failing_replace)
    with pytest.raises(ResultStoreWriteError, match="replace failure"):
        write_soybean_result_candidate(candidate(), output)
    assert output.exists()
    assert list(output.iterdir()) == []


def test_json_failure_removes_all_temporary_outputs(tmp_path, monkeypatch) -> None:
    output = tmp_path / "json-failure"
    real_write_json = result_store._write_json
    calls = 0

    def failing_json(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected JSON failure")
        return real_write_json(path, payload)

    monkeypatch.setattr(result_store, "_write_json", failing_json)
    with pytest.raises(ResultStoreWriteError, match="JSON failure"):
        write_soybean_result_candidate(candidate(), output)
    assert output.exists()
    assert list(output.iterdir()) == []


def test_repeated_fixed_candidate_has_identical_business_files(tmp_path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    write_soybean_result_candidate(candidate(), first_dir)
    write_soybean_result_candidate(candidate(), second_dir)
    for name in (
        SNAPSHOT_FILENAME,
        RESULT_FILENAME,
        QUALITY_FILENAME,
        MANIFEST_FILENAME,
    ):
        assert file_sha(first_dir / name) == file_sha(second_dir / name)
