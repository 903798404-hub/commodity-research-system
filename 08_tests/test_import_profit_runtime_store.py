from __future__ import annotations

from datetime import date, datetime, timezone
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from agri_research_agent.import_profit import (
    BusinessKey,
    ContractOverrideConfig,
    ContractOverrideRule,
    DceContract,
    build_mapping_snapshot,
    build_contract_override_snapshot,
    build_parameter_snapshot,
    load_soybean_config,
    select_soybean_contracts,
)
from agri_research_agent.import_profit.runtime_store import (
    BUSINESS_KEYS_FILENAME,
    MANIFEST_FILENAME,
    QUALITY_FILENAME,
    RESULTS_FILENAME,
    SNAPSHOTS_FILENAME,
    RUNTIME_SCHEMA_VERSION,
    RuntimeReleaseIndex,
    RuntimeReleaseValidationError,
    RuntimeWriteError,
    file_sha256,
    load_runtime_release_dataset,
    resolve_current_runtime_release,
    validate_release_id,
    write_release_index_atomically,
    parquet_identity,
)
from agri_research_agent.import_profit.result_store import (
    LEGACY_CONTRACT_IDENTITY_SNAPSHOT_SCHEMA,
    LEGACY_QUOTE_DATE_SNAPSHOT_SCHEMA,
    LEGACY_RESULT_SCHEMA,
    LEGACY_SNAPSHOT_SCHEMA,
    LEGACY_MAPPING_PROVENANCE_RESULT_SCHEMA,
    LEGACY_MAPPING_PROVENANCE_SNAPSHOT_SCHEMA,
    LEGACY_OVERRIDE_PROVENANCE_RESULT_SCHEMA,
    LEGACY_OVERRIDE_PROVENANCE_SNAPSHOT_SCHEMA,
    contract_selection_row,
)
from agri_research_agent.pipelines.import_profit_runtime import (
    RuntimePipelineError,
    bootstrap_import_profit_runtime,
)
from test_import_profit_components import configured_rows
from test_import_profit_query import write_dataset


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "02_configs" / "import_profit_soybean.yaml"
CONFIG = load_soybean_config(CONFIG_PATH)
NOW = datetime(2026, 7, 30, 1, 2, 3, tzinfo=timezone.utc)


def make_historical_candidate(
    tmp_path: Path,
    records=None,
    *,
    parameter_config=CONFIG,
) -> Path:
    candidate = tmp_path / "candidate"
    records = records or [
        configured_rows(
            date(2026, 6, 10),
            "brazil",
            2026,
            12,
            cnf=None,
        ),
        configured_rows(
            date(2026, 6, 10),
            "us_gulf",
            2026,
            12,
            cnf=125.0,
        ),
    ]
    paths = write_dataset(candidate, list(records))
    provenance = build_parameter_snapshot(parameter_config)
    mapping_provenance = build_mapping_snapshot(parameter_config)
    override_provenance = build_contract_override_snapshot(parameter_config)
    for path in paths[1:]:
        table = pq.read_table(path)
        rows = table.to_pylist()
        for row in rows:
            row["parameter_hash"] = provenance.parameter_hash
            row["mapping_hash"] = mapping_provenance.mapping_hash
            row["contract_override_hash"] = (
                override_provenance.contract_override_hash
            )
            if path == paths[1]:
                business_key = BusinessKey(
                    row["business_date"],
                    row["commodity"],
                    row["origin"],
                    row["shipment_year"],
                    row["shipment_month"],
                    parameter_config.origin_codes,
                    parameter_config.commodity,
                    row["shipment_period"],
                )
                selection = select_soybean_contracts(
                    parameter_config, business_key
                )
                row.update(
                    contract_selection_row(
                        SimpleNamespace(contract_selection=selection)
                    )
                )
                row["cbot_contract_year"] = (
                    selection.cbot.effective_contract.contract_year
                )
                row["cbot_contract_month"] = (
                    selection.cbot.effective_contract.contract_month
                )
                row["soymeal_contract_code"] = (
                    selection.soymeal.effective_contract.code
                )
                row["soyoil_contract_code"] = (
                    selection.soyoil.effective_contract.code
                )
                if row["soymeal_price_cny_per_tonne"] is not None:
                    row["soymeal_source_delivery_month"] = (
                        selection.soymeal.effective_contract.contract_month
                    )
                if row["soyoil_price_cny_per_tonne"] is not None:
                    row["soyoil_source_delivery_month"] = (
                        selection.soyoil.effective_contract.contract_month
                    )
        pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    quality_path = candidate / QUALITY_FILENAME
    quality_path.write_text(
        json.dumps({"final_status": "passed_with_incomplete"}),
        encoding="utf-8",
    )
    outputs = {}
    for path in (*paths, quality_path):
        outputs[path.name] = {
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": file_sha256(path),
        }
    manifest = {
        "schema_version": "1",
        "calculated_at": "2026-07-30T00:00:00Z",
        "output_files": outputs,
        "parameter_snapshot": provenance.snapshot,
        "parameter_hash": provenance.parameter_hash,
        "mapping_snapshot": mapping_provenance.snapshot,
        "mapping_hash": mapping_provenance.mapping_hash,
        "contract_override_snapshot": override_provenance.snapshot,
        "contract_override_hash": override_provenance.contract_override_hash,
    }
    (candidate / MANIFEST_FILENAME).write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return candidate


def test_bootstrap_rejects_legacy_candidate_without_parameter_snapshot(
    tmp_path,
):
    candidate = make_historical_candidate(tmp_path)
    manifest_path = candidate / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("parameter_snapshot")
    manifest.pop("parameter_hash")
    manifest.pop("mapping_snapshot")
    manifest.pop("mapping_hash")
    manifest.pop("contract_override_snapshot")
    manifest.pop("contract_override_hash")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimePipelineError, match="legacy historical candidate"):
        bootstrap_import_profit_runtime(
            candidate,
            tmp_path / "runtime",
            config=CONFIG,
            config_path=CONFIG_PATH,
            release_id="base-001",
            generated_at=NOW,
        )

    assert not (tmp_path / "runtime").exists()


def test_bootstrap_rejects_candidate_without_mapping_snapshot(tmp_path):
    candidate = make_historical_candidate(tmp_path)
    manifest_path = candidate / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("mapping_snapshot")
    manifest.pop("mapping_hash")
    manifest.pop("contract_override_snapshot")
    manifest.pop("contract_override_hash")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimePipelineError, match="mapping snapshot"):
        bootstrap_import_profit_runtime(
            candidate,
            tmp_path / "runtime",
            config=CONFIG,
            config_path=CONFIG_PATH,
            release_id="base-001",
            generated_at=NOW,
        )

    assert not (tmp_path / "runtime").exists()


def bootstrap_fixture(
    tmp_path: Path,
    *,
    release_id: str = "base-001",
    records=None,
):
    candidate = make_historical_candidate(tmp_path, records)
    runtime_root = tmp_path / "runtime"
    result = bootstrap_import_profit_runtime(
        candidate,
        runtime_root,
        config=CONFIG,
        config_path=CONFIG_PATH,
        release_id=release_id,
        generated_at=NOW,
    )
    return runtime_root, candidate, result


def test_bootstrap_empty_root_and_resolve_fixed_release_contract(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    candidate = make_historical_candidate(tmp_path)

    result = bootstrap_import_profit_runtime(
        candidate,
        runtime_root,
        config=CONFIG,
        config_path=CONFIG_PATH,
        release_id="base-001",
        generated_at=NOW,
    )
    loaded = load_runtime_release_dataset(runtime_root)
    resolved = loaded.resolved

    assert result.status == "success"
    assert result.promotion.generation == resolved.generation == 1
    assert resolved.previous_release_id is None
    assert resolved.manual_cnf_exists is False
    assert resolved.identity.manual_cnf_sha256 is None
    assert resolved.manifest["manual_cnf_record_count"] == 0
    assert {
        item.filename for item in resolved.files
    } == {
        BUSINESS_KEYS_FILENAME,
        SNAPSHOTS_FILENAME,
        RESULTS_FILENAME,
        QUALITY_FILENAME,
    }
    assert loaded.dataset.business_key_count == 2
    assert tuple(json.loads(
        (runtime_root / "release_index.json").read_text(encoding="utf-8")
    )) == (
        "schema_version",
        "generation",
        "current_release_id",
        "previous_release_id",
        "updated_at",
        "index_reason",
        "current_manifest_sha256",
    )
    assert not any(
        Path(value).is_absolute()
        for value in (
            resolved.runtime_root_name,
            *(
                item.filename for item in resolved.files
            ),
        )
    )


def test_bootstrap_marked_isolated_root_preserves_runtime_identity(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    marker_path = runtime_root / ".market-data-runtime.json"
    marker_bytes = json.dumps(
        {
            "schema_version": 1,
            "runtime_id": "isolated-dev-soybean-import-crush-test",
            "classification": "isolated-dev",
            "module_id": "soybean-import-crush",
            "created_at": "2026-08-16T00:00:00Z",
        }
    ).encode("utf-8")
    marker_path.write_bytes(marker_bytes)
    candidate = make_historical_candidate(runtime_root)
    (runtime_root / "tmp").mkdir()

    result = bootstrap_import_profit_runtime(
        candidate,
        runtime_root,
        config=CONFIG,
        config_path=CONFIG_PATH,
        release_id="base-isolated-001",
        generated_at=NOW,
    )

    assert result.status == "success"
    assert result.promotion.generation == 1
    assert marker_path.read_bytes() == marker_bytes
    assert candidate.is_dir()
    assert not list((runtime_root / "releases").glob(".building-*"))
    assert resolve_current_runtime_release(runtime_root).release_id == (
        "base-isolated-001"
    )


def test_release_a_and_b_seal_independent_parameters_and_hashes(tmp_path):
    runtime_a, _, _ = bootstrap_fixture(tmp_path / "a", release_id="release-a")
    release_a = resolve_current_runtime_release(runtime_a)
    a_files_before = {
        path.name: file_sha256(path)
        for path in release_a.release_dir.iterdir()
        if path.is_file()
    }

    config_b = replace(
        CONFIG,
        default_parameters=replace(
            CONFIG.default_parameters, tariff_rate=0.20
        ),
    )
    candidate_b = make_historical_candidate(
        tmp_path / "b", parameter_config=config_b
    )
    runtime_b = tmp_path / "b-runtime"
    bootstrap_import_profit_runtime(
        candidate_b,
        runtime_b,
        config=config_b,
        config_path=CONFIG_PATH,
        release_id="release-b",
        generated_at=NOW,
    )
    release_b = resolve_current_runtime_release(runtime_b)

    params_a = release_a.parameter_provenance.parameters_for_origin("brazil")
    params_b = release_b.parameter_provenance.parameters_for_origin("brazil")
    assert params_a.tariff_rate == 0.03
    assert params_b.tariff_rate == 0.20
    assert (
        release_a.parameter_provenance.parameter_hash
        != release_b.parameter_provenance.parameter_hash
    )
    for field_name in (
        "meal_yield",
        "oil_yield",
        "cents_per_bushel_to_usd_per_tonne",
        "vat_rate",
        "port_charge_cny_per_tonne",
        "processing_fee_cny_per_tonne",
        "additional_fees_cny_per_tonne",
    ):
        assert getattr(params_a, field_name) == getattr(params_b, field_name)
    assert a_files_before == {
        path.name: file_sha256(path)
        for path in release_a.release_dir.iterdir()
        if path.is_file()
    }
    assert {
        record.parameter_hash
        for record in load_runtime_release_dataset(runtime_a).dataset.records
    } == {release_a.parameter_provenance.parameter_hash}
    assert {
        record.parameter_hash
        for record in load_runtime_release_dataset(runtime_b).dataset.records
    } == {release_b.parameter_provenance.parameter_hash}


def test_release_a_and_b_seal_independent_mapping_rules_and_hashes(tmp_path):
    runtime_a, _, _ = bootstrap_fixture(tmp_path / "a", release_id="release-a")
    release_a = resolve_current_runtime_release(runtime_a)
    a_files_before = {
        path.name: file_sha256(path)
        for path in release_a.release_dir.iterdir()
        if path.is_file()
    }
    first, *remaining = CONFIG.contract_mapping
    config_b = replace(
        CONFIG,
        contract_mapping=(
            replace(first, cbot=replace(first.cbot, contract_month=3)),
            *remaining,
        ),
    )
    candidate_b = make_historical_candidate(
        tmp_path / "b", parameter_config=config_b
    )
    runtime_b = tmp_path / "b-runtime"
    bootstrap_import_profit_runtime(
        candidate_b,
        runtime_b,
        config=config_b,
        config_path=CONFIG_PATH,
        release_id="release-b",
        generated_at=NOW,
    )
    loaded_a = load_runtime_release_dataset(runtime_a)
    loaded_b = load_runtime_release_dataset(runtime_b)
    release_b = loaded_b.resolved

    assert release_a.mapping_provenance.available
    assert release_b.mapping_provenance.available
    assert release_a.mapping_provenance.mapping_hash != (
        release_b.mapping_provenance.mapping_hash
    )
    assert release_a.parameter_provenance.parameter_hash == (
        release_b.parameter_provenance.parameter_hash
    )
    assert {
        record.mapping_hash for record in loaded_a.dataset.records
    } == {release_a.mapping_provenance.mapping_hash}
    assert {
        record.mapping_hash for record in loaded_b.dataset.records
    } == {release_b.mapping_provenance.mapping_hash}
    assert [
        replace(record, mapping_hash=None) for record in loaded_a.dataset.records
    ] == [
        replace(record, mapping_hash=None) for record in loaded_b.dataset.records
    ]
    assert a_files_before == {
        path.name: file_sha256(path)
        for path in release_a.release_dir.iterdir()
        if path.is_file()
    }


def test_release_a_and_b_seal_independent_contract_override_rules(tmp_path):
    runtime_a, _, _ = bootstrap_fixture(
        tmp_path / "a", release_id="override-release-a"
    )
    loaded_a = load_runtime_release_dataset(runtime_a)
    config_b = replace(
        CONFIG,
        contract_override=ContractOverrideConfig(
            True,
            (
                ContractOverrideRule(
                    origin="brazil",
                    shipment_year=2026,
                    shipment_month=12,
                    effective_from_business_date=date(2026, 6, 10),
                    effective_to_business_date=None,
                    cbot_contract=None,
                    soymeal_contract=DceContract.soymeal(2027, 9),
                    soyoil_contract=None,
                    reason="source contract anomaly",
                ),
            ),
        ),
    )
    candidate_b = make_historical_candidate(
        tmp_path / "b", parameter_config=config_b
    )
    runtime_b = tmp_path / "b-runtime"
    bootstrap_import_profit_runtime(
        candidate_b,
        runtime_b,
        config=config_b,
        config_path=CONFIG_PATH,
        release_id="override-release-b",
        generated_at=NOW,
    )
    loaded_b = load_runtime_release_dataset(runtime_b)

    assert (
        loaded_a.resolved.contract_override_provenance.contract_override_hash
        != loaded_b.resolved.contract_override_provenance.contract_override_hash
    )
    assert (
        loaded_a.resolved.parameter_provenance.parameter_hash
        == loaded_b.resolved.parameter_provenance.parameter_hash
    )
    assert (
        loaded_a.resolved.mapping_provenance.mapping_hash
        == loaded_b.resolved.mapping_provenance.mapping_hash
    )
    automatic = next(
        record
        for record in loaded_a.dataset.records
        if record.origin == "brazil"
    )
    overridden = next(
        record
        for record in loaded_b.dataset.records
        if record.origin == "brazil"
    )
    assert automatic.soymeal_contract == "M2701"
    assert automatic.soymeal_selection_mode == "automatic"
    assert overridden.soymeal_automatic_contract == "M2701"
    assert overridden.soymeal_override_contract == "M2709"
    assert overridden.soymeal_contract == "M2709"
    assert overridden.soymeal_selection_mode == "manual_override"


def test_goal_e_release_without_override_snapshot_is_legacy_unavailable(
    tmp_path,
):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    resolved = resolve_current_runtime_release(runtime_root)
    contracts_before = {
        record.key: (
            record.cbot_contract,
            record.soymeal_contract,
            record.soyoil_contract,
        )
        for record in load_runtime_release_dataset(runtime_root).dataset.records
    }
    snapshot_rows = pq.read_table(resolved.snapshots_path).to_pylist()
    result_rows = pq.read_table(resolved.results_path).to_pylist()
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    name: row[name]
                    for name in LEGACY_OVERRIDE_PROVENANCE_SNAPSHOT_SCHEMA.names
                }
                for row in snapshot_rows
            ],
            schema=LEGACY_OVERRIDE_PROVENANCE_SNAPSHOT_SCHEMA,
        ),
        resolved.snapshots_path,
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    name: row[name]
                    for name in LEGACY_OVERRIDE_PROVENANCE_RESULT_SCHEMA.names
                }
                for row in result_rows
            ],
            schema=LEGACY_OVERRIDE_PROVENANCE_RESULT_SCHEMA,
        ),
        resolved.results_path,
    )
    manifest = dict(resolved.manifest)
    manifest.pop("contract_override_snapshot")
    manifest.pop("contract_override_hash")
    outputs = dict(manifest["output_files"])
    outputs[SNAPSHOTS_FILENAME] = parquet_identity(
        resolved.snapshots_path,
        SNAPSHOTS_FILENAME,
        LEGACY_OVERRIDE_PROVENANCE_SNAPSHOT_SCHEMA,
    ).as_dict()
    outputs[RESULTS_FILENAME] = parquet_identity(
        resolved.results_path,
        RESULTS_FILENAME,
        LEGACY_OVERRIDE_PROVENANCE_RESULT_SCHEMA,
    ).as_dict()
    manifest["output_files"] = outputs
    resolved.manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_release_index_atomically(
        runtime_root,
        replace(
            resolved.index,
            current_manifest_sha256=file_sha256(resolved.manifest_path),
        ),
        expected_previous_sha256=resolved.identity.index_sha256,
    )

    loaded = load_runtime_release_dataset(runtime_root)
    assert loaded.resolved.contract_override_provenance.status == (
        "legacy_unavailable"
    )
    assert {record.contract_override_hash for record in loaded.dataset.records} == {
        None
    }
    assert {
        record.cbot_selection_mode for record in loaded.dataset.records
    } == {"legacy_unknown"}
    assert {
        record.key: (
            record.cbot_contract,
            record.soymeal_contract,
            record.soyoil_contract,
        )
        for record in loaded.dataset.records
    } == contracts_before


def test_goal_d_release_without_mapping_snapshot_remains_readable(
    tmp_path,
):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    resolved = resolve_current_runtime_release(runtime_root)
    contracts_before = {
        (
            record.key,
            record.cbot_contract,
            record.soymeal_contract,
            record.soyoil_contract,
        )
        for record in load_runtime_release_dataset(runtime_root).dataset.records
    }
    snapshot_rows = pq.read_table(resolved.snapshots_path).to_pylist()
    result_rows = pq.read_table(resolved.results_path).to_pylist()
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    name: row[name]
                    for name in LEGACY_MAPPING_PROVENANCE_SNAPSHOT_SCHEMA.names
                }
                for row in snapshot_rows
            ],
            schema=LEGACY_MAPPING_PROVENANCE_SNAPSHOT_SCHEMA,
        ),
        resolved.snapshots_path,
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    name: row[name]
                    for name in LEGACY_MAPPING_PROVENANCE_RESULT_SCHEMA.names
                }
                for row in result_rows
            ],
            schema=LEGACY_MAPPING_PROVENANCE_RESULT_SCHEMA,
        ),
        resolved.results_path,
    )
    manifest = dict(resolved.manifest)
    manifest.pop("mapping_snapshot")
    manifest.pop("mapping_hash")
    manifest.pop("contract_override_snapshot")
    manifest.pop("contract_override_hash")
    outputs = dict(manifest["output_files"])
    outputs[SNAPSHOTS_FILENAME] = parquet_identity(
        resolved.snapshots_path,
        SNAPSHOTS_FILENAME,
        LEGACY_MAPPING_PROVENANCE_SNAPSHOT_SCHEMA,
    ).as_dict()
    outputs[RESULTS_FILENAME] = parquet_identity(
        resolved.results_path,
        RESULTS_FILENAME,
        LEGACY_MAPPING_PROVENANCE_RESULT_SCHEMA,
    ).as_dict()
    manifest["output_files"] = outputs
    resolved.manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_release_index_atomically(
        runtime_root,
        replace(
            resolved.index,
            current_manifest_sha256=file_sha256(resolved.manifest_path),
        ),
        expected_previous_sha256=resolved.identity.index_sha256,
    )

    loaded = load_runtime_release_dataset(runtime_root)
    assert loaded.resolved.parameter_provenance.available
    assert loaded.resolved.mapping_provenance.status == "legacy_unavailable"
    assert {record.mapping_hash for record in loaded.dataset.records} == {None}
    assert {
        (
            record.key,
            record.cbot_contract,
            record.soymeal_contract,
            record.soyoil_contract,
        )
        for record in loaded.dataset.records
    } == contracts_before


def test_legacy_release_remains_readable_without_current_parameter_fallback(
    tmp_path,
):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    resolved = resolve_current_runtime_release(runtime_root)
    for path, legacy_schema in (
        (resolved.snapshots_path, LEGACY_SNAPSHOT_SCHEMA),
        (resolved.results_path, LEGACY_RESULT_SCHEMA),
    ):
        rows = pq.read_table(path).to_pylist()
        legacy_rows = [
            {name: row[name] for name in legacy_schema.names}
            for row in rows
        ]
        pq.write_table(
            pa.Table.from_pylist(legacy_rows, schema=legacy_schema), path
        )

    manifest = dict(resolved.manifest)
    manifest.pop("mapping_snapshot")
    manifest.pop("mapping_hash")
    manifest.pop("contract_override_snapshot")
    manifest.pop("contract_override_hash")
    manifest.pop("parameter_snapshot")
    manifest.pop("parameter_hash")
    outputs = dict(manifest["output_files"])
    outputs[SNAPSHOTS_FILENAME] = parquet_identity(
        resolved.snapshots_path,
        SNAPSHOTS_FILENAME,
        LEGACY_SNAPSHOT_SCHEMA,
    ).as_dict()
    outputs[RESULTS_FILENAME] = parquet_identity(
        resolved.results_path,
        RESULTS_FILENAME,
        LEGACY_RESULT_SCHEMA,
    ).as_dict()
    manifest["output_files"] = outputs
    resolved.manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    legacy_index = replace(
        resolved.index,
        current_manifest_sha256=file_sha256(resolved.manifest_path),
    )
    write_release_index_atomically(
        runtime_root,
        legacy_index,
        expected_previous_sha256=resolved.identity.index_sha256,
    )

    loaded = load_runtime_release_dataset(runtime_root)
    assert loaded.resolved.parameter_provenance.status == "legacy_unavailable"
    assert loaded.resolved.parameter_provenance.snapshot is None
    assert {record.parameter_hash for record in loaded.dataset.records} == {None}
    assert {
        record.soymeal_contract_identity_status
        for record in loaded.dataset.records
    } == {"legacy_unknown"}
    assert {
        record.soyoil_contract_identity_status
        for record in loaded.dataset.records
    } == {"legacy_unknown"}


def test_goal_a_release_without_contract_identity_reads_as_legacy_unknown(
    tmp_path,
):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    resolved = resolve_current_runtime_release(runtime_root)
    rows = pq.read_table(resolved.snapshots_path).to_pylist()
    legacy_rows = [
        {
            name: row[name]
            for name in LEGACY_CONTRACT_IDENTITY_SNAPSHOT_SCHEMA.names
        }
        for row in rows
    ]
    pq.write_table(
        pa.Table.from_pylist(
            legacy_rows, schema=LEGACY_CONTRACT_IDENTITY_SNAPSHOT_SCHEMA
        ),
        resolved.snapshots_path,
    )
    manifest = dict(resolved.manifest)
    result_rows = pq.read_table(resolved.results_path).to_pylist()
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    name: row[name]
                    for name in LEGACY_MAPPING_PROVENANCE_RESULT_SCHEMA.names
                }
                for row in result_rows
            ],
            schema=LEGACY_MAPPING_PROVENANCE_RESULT_SCHEMA,
        ),
        resolved.results_path,
    )
    manifest.pop("mapping_snapshot")
    manifest.pop("mapping_hash")
    manifest.pop("contract_override_snapshot")
    manifest.pop("contract_override_hash")
    outputs = dict(manifest["output_files"])
    outputs[SNAPSHOTS_FILENAME] = parquet_identity(
        resolved.snapshots_path,
        SNAPSHOTS_FILENAME,
        LEGACY_CONTRACT_IDENTITY_SNAPSHOT_SCHEMA,
    ).as_dict()
    outputs[RESULTS_FILENAME] = parquet_identity(
        resolved.results_path,
        RESULTS_FILENAME,
        LEGACY_MAPPING_PROVENANCE_RESULT_SCHEMA,
    ).as_dict()
    manifest["output_files"] = outputs
    resolved.manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_release_index_atomically(
        runtime_root,
        replace(
            resolved.index,
            current_manifest_sha256=file_sha256(resolved.manifest_path),
        ),
        expected_previous_sha256=resolved.identity.index_sha256,
    )

    loaded = load_runtime_release_dataset(runtime_root)
    assert loaded.resolved.parameter_provenance.available
    assert {
        record.soymeal_contract_identity_status
        for record in loaded.dataset.records
    } == {"legacy_unknown"}
    assert {
        record.soyoil_contract_identity_status
        for record in loaded.dataset.records
    } == {"legacy_unknown"}
    assert {
        record.parameter_hash for record in loaded.dataset.records
    } == {loaded.resolved.parameter_provenance.parameter_hash}


def test_goal_b_release_without_quote_date_evidence_reads_as_legacy_unknown(
    tmp_path,
):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    resolved = resolve_current_runtime_release(runtime_root)
    rows = pq.read_table(resolved.snapshots_path).to_pylist()
    legacy_rows = [
        {name: row[name] for name in LEGACY_QUOTE_DATE_SNAPSHOT_SCHEMA.names}
        for row in rows
    ]
    pq.write_table(
        pa.Table.from_pylist(
            legacy_rows, schema=LEGACY_QUOTE_DATE_SNAPSHOT_SCHEMA
        ),
        resolved.snapshots_path,
    )
    manifest = dict(resolved.manifest)
    result_rows = pq.read_table(resolved.results_path).to_pylist()
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    name: row[name]
                    for name in LEGACY_MAPPING_PROVENANCE_RESULT_SCHEMA.names
                }
                for row in result_rows
            ],
            schema=LEGACY_MAPPING_PROVENANCE_RESULT_SCHEMA,
        ),
        resolved.results_path,
    )
    manifest.pop("mapping_snapshot")
    manifest.pop("mapping_hash")
    manifest.pop("contract_override_snapshot")
    manifest.pop("contract_override_hash")
    outputs = dict(manifest["output_files"])
    outputs[SNAPSHOTS_FILENAME] = parquet_identity(
        resolved.snapshots_path,
        SNAPSHOTS_FILENAME,
        LEGACY_QUOTE_DATE_SNAPSHOT_SCHEMA,
    ).as_dict()
    outputs[RESULTS_FILENAME] = parquet_identity(
        resolved.results_path,
        RESULTS_FILENAME,
        LEGACY_MAPPING_PROVENANCE_RESULT_SCHEMA,
    ).as_dict()
    manifest["output_files"] = outputs
    resolved.manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_release_index_atomically(
        runtime_root,
        replace(
            resolved.index,
            current_manifest_sha256=file_sha256(resolved.manifest_path),
        ),
        expected_previous_sha256=resolved.identity.index_sha256,
    )

    loaded = load_runtime_release_dataset(runtime_root)
    assert {
        record.soymeal_quote_date_evidence_status
        for record in loaded.dataset.records
    } == {"legacy_unknown"}
    assert {
        record.soyoil_quote_date_evidence_status
        for record in loaded.dataset.records
    } == {"legacy_unknown"}
    assert loaded.resolved.parameter_provenance.available
    assert {
        record.soymeal_contract_identity_status
        for record in loaded.dataset.records
    } == {"continuous_inferred"}


@pytest.mark.parametrize("preexisting", ["file.txt", "release_index.json"])
def test_bootstrap_rejects_nonempty_root(tmp_path, preexisting):
    candidate = make_historical_candidate(tmp_path)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    (runtime_root / preexisting).write_text("occupied", encoding="utf-8")

    with pytest.raises(Exception, match="empty"):
        bootstrap_import_profit_runtime(
            candidate,
            runtime_root,
            config=CONFIG,
            config_path=CONFIG_PATH,
            release_id="base-001",
            generated_at=NOW,
        )


@pytest.mark.parametrize(
    "release_id", ["", ".", "..", "../bad", r"bad\path", "含中文"]
)
def test_release_id_is_strict_ascii(release_id):
    with pytest.raises(RuntimeReleaseValidationError):
        validate_release_id(release_id)


def test_expected_release_and_index_are_concurrency_guards(tmp_path):
    runtime_root, _, result = bootstrap_fixture(tmp_path)
    with pytest.raises(Exception, match="no longer Current"):
        load_runtime_release_dataset(
            runtime_root, expected_release_id="other"
        )
    with pytest.raises(Exception, match="stale"):
        load_runtime_release_dataset(
            runtime_root, expected_index_sha256="0" * 64
        )
    assert (
        load_runtime_release_dataset(
            runtime_root,
            expected_release_id="base-001",
            expected_index_sha256=result.promotion.index_sha256,
        ).resolved.release_id
        == "base-001"
    )


@pytest.mark.parametrize(
    ("target", "contents", "message"),
    [
        ("release_index.json", b"{", "JSON"),
        ("manifest.json", b"{", "Manifest"),
        (
            BUSINESS_KEYS_FILENAME,
            b"not parquet",
            "identity",
        ),
    ],
)
def test_corruption_is_rejected_without_previous_fallback(
    tmp_path, target, contents, message
):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    resolved = resolve_current_runtime_release(runtime_root)
    path = (
        runtime_root / target
        if target == "release_index.json"
        else resolved.release_dir / target
    )
    path.write_bytes(contents)

    with pytest.raises(RuntimeReleaseValidationError, match=message):
        resolve_current_runtime_release(runtime_root)


def test_missing_current_directory_is_rejected(tmp_path):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    release = runtime_root / "releases" / "base-001"
    moved = runtime_root / "releases" / "not-current"
    release.rename(moved)
    with pytest.raises(RuntimeReleaseValidationError, match="does not exist"):
        resolve_current_runtime_release(runtime_root)


def test_atomic_index_replace_failure_preserves_current(tmp_path, monkeypatch):
    runtime_root, _, result = bootstrap_fixture(tmp_path)
    index_path = runtime_root / "release_index.json"
    before = index_path.read_bytes()
    current = resolve_current_runtime_release(runtime_root)
    next_index = RuntimeReleaseIndex(
        schema_version=RUNTIME_SCHEMA_VERSION,
        generation=2,
        current_release_id="next-002",
        previous_release_id="base-001",
        updated_at=NOW,
        index_reason="test",
        current_manifest_sha256=current.identity.manifest_sha256,
    )

    def fail_replace(source, target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(
        "agri_research_agent.import_profit.runtime_store.os.replace",
        fail_replace,
    )
    with pytest.raises(RuntimeWriteError, match="atomic"):
        write_release_index_atomically(
            runtime_root,
            next_index,
            expected_previous_sha256=result.promotion.index_sha256,
        )
    assert index_path.read_bytes() == before
    assert not list(runtime_root.glob(".release_index.json.*"))


def test_manifest_identity_and_key_set_corruption_are_rejected(tmp_path):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    resolved = resolve_current_runtime_release(runtime_root)
    manifest = json.loads(resolved.manifest_path.read_text(encoding="utf-8"))
    manifest["record_count"] = 999
    resolved.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    index = json.loads(
        (runtime_root / "release_index.json").read_text(encoding="utf-8")
    )
    index["current_manifest_sha256"] = file_sha256(resolved.manifest_path)
    (runtime_root / "release_index.json").write_text(
        json.dumps(index, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeReleaseValidationError, match="counts|statistics"
    ):
        load_runtime_release_dataset(runtime_root)
