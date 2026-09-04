from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.import_profit.historical_cnf_adapter import (
    HISTORICAL_CNF_SCHEMA,
)
from agri_research_agent.import_profit.historical_dce_adapter import (
    HISTORICAL_DCE_CONTINUOUS_SCHEMA,
)
from agri_research_agent.import_profit.result_store import (
    RESULT_SCHEMA,
    SNAPSHOT_SCHEMA,
)
from agri_research_agent.import_profit.standard_io import CBOT_SCHEMA, FX_SCHEMA
from import_profit import build_historical_soybean_results_candidate as candidate
from test_import_profit_historical_recalculation import (
    DAY,
    cbot_points,
    cnf_grid,
    dce_points,
    fx_points,
)


CONFIG = candidate.REPOSITORY_ROOT / "02_configs" / "import_profit_soybean.yaml"
CALCULATED_AT = datetime(2026, 7, 30, tzinfo=timezone.utc)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def rows(records, schema: pa.Schema) -> list[dict]:
    result = []
    for record in records:
        row = {}
        for name in schema.names:
            if name == "lead_months":
                row[name] = (
                    (record.contract_year - record.market_date.year) * 12
                    + record.contract_month
                    - record.market_date.month
                )
            elif name == "unit":
                row[name] = "cnh_per_usd"
            elif name == "quality_status":
                row[name] = "valid"
            elif name == "is_usable":
                row[name] = getattr(record, name, True)
            elif name == "source_statement_index":
                row[name] = 1
            else:
                row[name] = getattr(record, name)
        result.append(row)
    return result


def write_candidate_parent(
    parent: Path,
    files: dict[str, tuple[pa.Schema, list[dict]]],
) -> dict[str, Path]:
    parent.mkdir()
    paths = {}
    identities = {}
    for filename, (schema, data) in files.items():
        path = parent / filename
        pq.write_table(pa.Table.from_pylist(data, schema=schema), path)
        paths[filename] = path
        identities[filename] = {
            "filename": filename,
            "size": path.stat().st_size,
            "sha256": sha(path),
        }
    (parent / "manifest.json").write_text(
        json.dumps(
            {"candidate_status": "passed", "output_files": identities},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return paths


def source_candidates(
    tmp_path: Path,
    *,
    complete: bool,
) -> tuple[Path, Path, Path, Path]:
    cnf = cnf_grid(default=150.0, special_values=not complete)
    dce = dce_points()
    cbot = cbot_points()
    fx = fx_points()
    stage9 = write_candidate_parent(
        tmp_path / "stage9",
        {
            "historical_cnf_quotes.parquet": (
                HISTORICAL_CNF_SCHEMA,
                sorted(
                    rows(cnf, HISTORICAL_CNF_SCHEMA),
                    key=lambda row: tuple(row[name] for name in (
                        "business_date", "commodity", "origin",
                        "shipment_year", "shipment_month",
                    )),
                ),
            ),
            "historical_dce_continuous.parquet": (
                HISTORICAL_DCE_CONTINUOUS_SCHEMA,
                sorted(
                    rows(dce, HISTORICAL_DCE_CONTINUOUS_SCHEMA),
                    key=lambda row: (
                        row["business_date"], row["instrument"],
                        row["delivery_month"],
                    ),
                ),
            ),
        },
    )
    stage4 = write_candidate_parent(
        tmp_path / "stage4",
        {
            "cbot_soybean_daily.parquet": (
                CBOT_SCHEMA,
                sorted(
                    rows(cbot, CBOT_SCHEMA),
                    key=lambda row: (
                        row["market_date"], row["contract_year"],
                        row["contract_month"],
                    ),
                ),
            ),
            "usdcny_forward_daily.parquet": (
                FX_SCHEMA,
                sorted(
                    rows(fx, FX_SCHEMA),
                    key=lambda row: (
                        row["market_date"], row["tenor_months"],
                    ),
                ),
            ),
        },
    )
    return (
        stage9["historical_cnf_quotes.parquet"],
        stage9["historical_dce_continuous.parquet"],
        stage4["cbot_soybean_daily.parquet"],
        stage4["usdcny_forward_daily.parquet"],
    )


def build(tmp_path: Path, *, complete: bool, name: str = "output"):
    cnf, dce, cbot, fx = source_candidates(tmp_path, complete=complete)
    before = {path: sha(path) for path in (cnf, dce, cbot, fx)}
    result = candidate.build_historical_soybean_results_candidate(
        config_path=CONFIG,
        historical_cnf_path=cnf,
        historical_dce_path=dce,
        cbot_parquet_path=cbot,
        fx_parquet_path=fx,
        as_of_date=DAY,
        output_dir=tmp_path / name,
        batch_size=7,
        calculated_at=CALCULATED_AT,
    )
    assert {path: sha(path) for path in before} == before
    return result, (cnf, dce, cbot, fx)


def test_complete_candidate_has_five_verified_safe_outputs(tmp_path):
    result, _ = build(tmp_path, complete=True)
    output = result["output_dir"]
    assert {path.name for path in output.iterdir()} == {
        candidate.BUSINESS_KEYS_FILENAME,
        candidate.SNAPSHOTS_FILENAME,
        candidate.RESULTS_FILENAME,
        candidate.MANIFEST_FILENAME,
        candidate.QUALITY_FILENAME,
    }
    manifest = json.loads((output / candidate.MANIFEST_FILENAME).read_text("utf-8"))
    quality = json.loads((output / candidate.QUALITY_FILENAME).read_text("utf-8"))
    assert manifest["candidate_status"] == quality["candidate_status"] == "passed"
    assert manifest["historical_input"] is True
    assert manifest["as_of_date"] == DAY.isoformat()
    assert manifest["as_of_policy"] == "explicit_latest_real_cnf_date"
    assert manifest["batch_size"] == 7
    assert manifest["final_business_key_count"] == 48
    assert manifest["snapshot_count"] == manifest["result_count"] == 48
    assert manifest["success_count"] == 48
    assert manifest["incomplete_count"] == 0
    assert len(manifest["input_files"]) == 5
    config_identity = next(
        item for item in manifest["input_files"] if item["filename"] == CONFIG.name
    )
    assert config_identity["sha256"] == sha(CONFIG)
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert "C:\\" not in serialized
    assert all(
        identity["sha256"] == sha(output / filename)
        for filename, identity in manifest["output_files"].items()
    )
    assert pq.read_table(output / candidate.BUSINESS_KEYS_FILENAME).schema == (
        candidate.BUSINESS_KEY_SCHEMA
    )
    assert pq.read_table(output / candidate.SNAPSHOTS_FILENAME).schema == SNAPSHOT_SCHEMA
    assert pq.read_table(output / candidate.RESULTS_FILENAME).schema == RESULT_SCHEMA
    assert all(len(values) <= 5 for values in quality["samples"].values())


def test_null_cnf_produces_passed_with_incomplete_and_null_result_values(tmp_path):
    result, _ = build(tmp_path, complete=False)
    assert result["manifest"]["candidate_status"] == "passed_with_incomplete"
    assert result["manifest"]["incomplete_count"] == 1
    assert result["manifest"]["missing_reason_counts"]["missing_cnf"] == 1
    table = pq.read_table(result["files"][candidate.RESULTS_FILENAME])
    incomplete = [
        row for row in table.to_pylist()
        if row["calculation_status"] == "incomplete"
    ]
    assert len(incomplete) == 1
    for field in (
        "usd_cost_per_tonne",
        "duty_paid_cost_cny_per_tonne",
        "net_crush_margin_cny_per_tonne",
    ):
        assert incomplete[0][field] is None


def test_candidate_preserves_historical_and_reuters_provenance(tmp_path):
    result, _ = build(tmp_path, complete=True)
    snapshots = pq.read_table(
        result["files"][candidate.SNAPSHOTS_FILENAME]
    ).to_pylist()
    assert {row["cnf_source"] for row in snapshots} == {"historical_excel"}
    assert {row["soymeal_source"] for row in snapshots} == {"reuters_sql"}
    assert {row["soyoil_source"] for row in snapshots} == {"reuters_sql"}
    assert {row["soymeal_price_type"] for row in snapshots} == {
        "historical_continuous_close"
    }
    assert {row["soyoil_price_type"] for row in snapshots} == {
        "historical_continuous_close"
    }
    assert {
        row["soymeal_contract_identity_status"] for row in snapshots
    } == {"continuous_inferred"}
    assert {
        row["soyoil_contract_identity_status"] for row in snapshots
    } == {"continuous_inferred"}
    assert {row["soymeal_source_contract_code"] for row in snapshots} == {
        None
    }
    assert {row["soyoil_source_contract_code"] for row in snapshots} == {
        None
    }
    assert {
        row["soymeal_source_delivery_month"] for row in snapshots
    } <= {1, 5, 9}
    assert {
        row["soyoil_source_delivery_month"] for row in snapshots
    } <= {1, 5, 9}
    assert {
        row["soymeal_quote_date_evidence_status"] for row in snapshots
    } == {"source_confirmed"}
    assert {
        row["soyoil_quote_date_evidence_status"] for row in snapshots
    } == {"source_confirmed"}


def test_source_identity_failure_is_fatal_and_writes_nothing(tmp_path):
    cnf, dce, cbot, fx = source_candidates(tmp_path, complete=True)
    manifest_path = cbot.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["output_files"][cbot.name]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "output"
    with pytest.raises(candidate.HistoricalResultsCandidateError, match="identity"):
        candidate.build_historical_soybean_results_candidate(
            config_path=CONFIG,
            historical_cnf_path=cnf,
            historical_dce_path=dce,
            cbot_parquet_path=cbot,
            fx_parquet_path=fx,
            as_of_date=DAY,
            output_dir=output,
            calculated_at=CALCULATED_AT,
        )
    assert not output.exists()


def test_atomic_replace_failure_cleans_every_partial_output(tmp_path, monkeypatch):
    cnf, dce, cbot, fx = source_candidates(tmp_path, complete=True)
    output = tmp_path / "output"
    real_replace = candidate.os.replace
    call_count = 0

    def fail_second(source, target):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("simulated atomic replace failure")
        real_replace(source, target)

    monkeypatch.setattr(candidate.os, "replace", fail_second)
    with pytest.raises(OSError, match="simulated"):
        candidate.build_historical_soybean_results_candidate(
            config_path=CONFIG,
            historical_cnf_path=cnf,
            historical_dce_path=dce,
            cbot_parquet_path=cbot,
            fx_parquet_path=fx,
            as_of_date=DAY,
            output_dir=output,
            calculated_at=CALCULATED_AT,
        )
    assert not output.exists()


def test_fixed_inputs_have_identical_business_content_across_batch_sizes(tmp_path):
    cnf, dce, cbot, fx = source_candidates(tmp_path, complete=True)
    outputs = []
    for index, batch_size in enumerate((1, 1000)):
        result = candidate.build_historical_soybean_results_candidate(
            config_path=CONFIG,
            historical_cnf_path=cnf,
            historical_dce_path=dce,
            cbot_parquet_path=cbot,
            fx_parquet_path=fx,
            as_of_date=DAY,
            output_dir=tmp_path / f"output-{index}",
            batch_size=batch_size,
            calculated_at=CALCULATED_AT,
        )
        outputs.append(result)
    for filename in (
        candidate.BUSINESS_KEYS_FILENAME,
        candidate.SNAPSHOTS_FILENAME,
        candidate.RESULTS_FILENAME,
    ):
        left = pq.read_table(outputs[0]["files"][filename]).to_pylist()
        right = pq.read_table(outputs[1]["files"][filename]).to_pylist()
        assert left == right


def test_destination_must_be_external_and_empty(tmp_path):
    cnf, dce, cbot, fx = source_candidates(tmp_path, complete=True)
    with pytest.raises(candidate.HistoricalResultsCandidateError, match="outside"):
        candidate.build_historical_soybean_results_candidate(
            config_path=CONFIG,
            historical_cnf_path=cnf,
            historical_dce_path=dce,
            cbot_parquet_path=cbot,
            fx_parquet_path=fx,
            as_of_date=DAY,
            output_dir=candidate.REPOSITORY_ROOT / "forbidden-history-output",
            calculated_at=CALCULATED_AT,
        )
