from __future__ import annotations

from datetime import date, datetime, timezone
from dataclasses import replace
import math
from pathlib import Path

from filelock import FileLock
import pytest

from agri_research_agent.import_profit import (
    BusinessKey,
    ContractOverrideConfig,
    ContractOverrideRule,
    DceContract,
)
from agri_research_agent.import_profit.cnf_store import load_cnf_store
from agri_research_agent.import_profit.runtime_store import (
    MANIFEST_FILENAME,
    RuntimeConcurrentUpdateError,
    RuntimeLockedError,
    RuntimeWriteError,
    file_sha256,
    load_runtime_release_dataset,
    resolve_current_runtime_release,
)
from agri_research_agent.pipelines.import_profit_runtime import (
    RuntimeCnfUpdate,
    RuntimePipelineError,
    update_runtime_cnf_quotes,
)
from test_import_profit_runtime_store import (
    CONFIG,
    CONFIG_PATH,
    NOW,
    bootstrap_fixture,
)


UPDATED_AT = datetime(2026, 7, 30, 2, 0, 0, tzinfo=timezone.utc)


def key(
    *,
    origin: str = "brazil",
    business_date: date = date(2026, 6, 10),
    shipment_year: int = 2026,
    shipment_month: int = 12,
) -> BusinessKey:
    return BusinessKey(
        business_date,
        "soybean",
        origin,
        shipment_year,
        shipment_month,
        CONFIG.origin_codes,
        CONFIG.commodity,
        f"{shipment_year:04d}-{shipment_month:02d}",
    )


def update(
    business_key: BusinessKey,
    value: float | None,
    *,
    batch_id: str = "batch-001",
    updated_at: datetime = UPDATED_AT,
) -> RuntimeCnfUpdate:
    return RuntimeCnfUpdate(
        business_key=business_key,
        cnf_cents_per_bushel=value,
        updated_at=updated_at,
        batch_id=batch_id,
    )


def apply_update(
    runtime_root: Path,
    updates,
    *,
    release_id: str = "manual-002",
    batch_id: str = "batch-001",
    expected=None,
    lock_timeout_seconds: float = 1.0,
):
    current = resolve_current_runtime_release(runtime_root)
    if expected is None:
        expected = (
            current.release_id,
            current.identity.index_sha256,
            current.identity.manual_cnf_sha256,
        )
    return update_runtime_cnf_quotes(
        runtime_root,
        updates,
        config=CONFIG,
        config_path=CONFIG_PATH,
        expected_release_id=expected[0],
        expected_index_sha256=expected[1],
        expected_manual_cnf_sha256=expected[2],
        calculated_at=UPDATED_AT,
        batch_id=batch_id,
        release_id=release_id,
        lock_timeout_seconds=lock_timeout_seconds,
    )


def release_file_shas(release_dir: Path) -> dict[str, str]:
    return {
        path.name: file_sha256(path)
        for path in release_dir.iterdir()
        if path.is_file()
    }


def assert_clean_failed_transaction(
    runtime_root: Path,
    old_release_id: str,
    old_index_sha: str,
) -> None:
    resolved = resolve_current_runtime_release(runtime_root)
    assert resolved.release_id == old_release_id
    assert resolved.identity.index_sha256 == old_index_sha
    assert not list((runtime_root / "releases").glob(".building-*"))
    assert {
        path.name for path in (runtime_root / "releases").iterdir()
    } == {old_release_id}
    assert not list(runtime_root.glob(".release_index.json.*"))


def test_first_manual_cnf_update_promotes_complete_immutable_release(tmp_path):
    runtime_root, _, bootstrap = bootstrap_fixture(tmp_path)
    old = resolve_current_runtime_release(runtime_root)
    old_shas = release_file_shas(old.release_dir)
    old_parameter_hash = old.parameter_provenance.parameter_hash
    old_parameter_snapshot = old.parameter_provenance.snapshot
    old_mapping_hash = old.mapping_provenance.mapping_hash
    old_mapping_snapshot = old.mapping_provenance.snapshot
    old_override = old.contract_override_provenance

    result = apply_update(runtime_root, [update(key(), 100.0)])
    current = resolve_current_runtime_release(runtime_root)
    assert current.parameter_provenance.parameter_hash == old_parameter_hash
    assert current.parameter_provenance.snapshot == old_parameter_snapshot
    assert current.mapping_provenance.mapping_hash == old_mapping_hash
    assert current.mapping_provenance.snapshot == old_mapping_snapshot
    assert current.contract_override_provenance == old_override
    assert {
        record.parameter_hash
        for record in load_runtime_release_dataset(runtime_root).dataset.records
    } == {old_parameter_hash}
    assert {
        record.mapping_hash
        for record in load_runtime_release_dataset(runtime_root).dataset.records
    } == {old_mapping_hash}
    assert {
        record.contract_override_hash
        for record in load_runtime_release_dataset(runtime_root).dataset.records
    } == {old_override.contract_override_hash}
    loaded = load_runtime_release_dataset(runtime_root)
    record = loaded.dataset.get(
        business_date=date(2026, 6, 10),
        origin="brazil",
        shipment_year=2026,
        shipment_month=12,
    )
    manual = load_cnf_store(
        loaded.resolved.manual_cnf_path,
        allowed_origins=CONFIG.origin_codes,
    )

    assert result.status == "success"
    assert result.generation == 2
    assert result.previous_release_id == bootstrap.promotion.release_id
    assert result.changed_count == result.inserted_manual_cnf_count == 1
    assert loaded.resolved.previous_release_id == old.release_id
    assert len(manual.records) == 1
    assert manual.records[0].business_key == key()
    assert manual.records[0].source == "manual_ui"
    assert record is not None
    assert record.cnf_cents_per_bushel == 100.0
    assert record.cnf_source == "manual_ui"
    assert record.calculation_status == "success"
    assert record.missing_reasons == ()
    assert record.usd_cost_per_tonne == pytest.approx(477.6681)
    assert release_file_shas(old.release_dir) == old_shas
    assert loaded.dataset.business_key_count == 2
    assert loaded.dataset.success_count == 2
    assert loaded.dataset.incomplete_count == 0


def test_cnf_update_rejects_rebinding_current_release_to_changed_parameters(
    tmp_path,
):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    current = resolve_current_runtime_release(runtime_root)
    changed = replace(
        CONFIG,
        default_parameters=replace(
            CONFIG.default_parameters, tariff_rate=0.20
        ),
    )

    with pytest.raises(RuntimePipelineError, match="parameters do not match"):
        update_runtime_cnf_quotes(
            runtime_root,
            [update(key(), 100.0)],
            config=changed,
            config_path=CONFIG_PATH,
            expected_release_id=current.release_id,
            expected_index_sha256=current.identity.index_sha256,
            expected_manual_cnf_sha256=current.identity.manual_cnf_sha256,
            calculated_at=UPDATED_AT,
            batch_id="batch-001",
            release_id="manual-rebound",
        )

    after = resolve_current_runtime_release(runtime_root)
    assert after.release_id == current.release_id
    assert (
        after.parameter_provenance.parameter_hash
        == current.parameter_provenance.parameter_hash
    )


def test_cnf_update_rejects_rebinding_current_release_to_changed_mapping(
    tmp_path,
):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    current = resolve_current_runtime_release(runtime_root)
    first, *remaining = CONFIG.contract_mapping
    changed = replace(
        CONFIG,
        contract_mapping=(
            replace(first, cbot=replace(first.cbot, contract_month=3)),
            *remaining,
        ),
    )

    with pytest.raises(RuntimePipelineError, match="mapping does not match"):
        update_runtime_cnf_quotes(
            runtime_root,
            [update(key(), 100.0)],
            config=changed,
            config_path=CONFIG_PATH,
            expected_release_id=current.release_id,
            expected_index_sha256=current.identity.index_sha256,
            expected_manual_cnf_sha256=current.identity.manual_cnf_sha256,
            calculated_at=UPDATED_AT,
            batch_id="batch-001",
            release_id="manual-mapping-rebound",
        )

    after = resolve_current_runtime_release(runtime_root)
    assert after.release_id == current.release_id
    assert after.mapping_provenance == current.mapping_provenance


def test_cnf_update_rejects_rebinding_current_release_to_changed_override(
    tmp_path,
):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    current = resolve_current_runtime_release(runtime_root)
    changed = replace(
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

    with pytest.raises(RuntimePipelineError, match="contract override"):
        update_runtime_cnf_quotes(
            runtime_root,
            [update(key(), 100.0)],
            config=changed,
            config_path=CONFIG_PATH,
            expected_release_id=current.release_id,
            expected_index_sha256=current.identity.index_sha256,
            expected_manual_cnf_sha256=current.identity.manual_cnf_sha256,
            calculated_at=UPDATED_AT,
            batch_id="batch-001",
            release_id="manual-override-rebound",
        )

    after = resolve_current_runtime_release(runtime_root)
    assert after.release_id == current.release_id
    assert (
        after.contract_override_provenance
        == current.contract_override_provenance
    )


def test_overwrite_clear_batch_and_partial_no_change(tmp_path):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    first = apply_update(
        runtime_root,
        [
            update(key(), 100.0),
            update(key(origin="us_gulf"), 125.0),
        ],
    )
    current = resolve_current_runtime_release(runtime_root)
    batch = "batch-002"
    result = apply_update(
        runtime_root,
        [
            update(key(), 100.0, batch_id=batch),
            update(key(origin="us_gulf"), None, batch_id=batch),
        ],
        release_id="manual-003",
        batch_id=batch,
    )
    loaded = load_runtime_release_dataset(runtime_root)
    brazil = loaded.dataset.get(
        business_date=date(2026, 6, 10),
        origin="brazil",
        shipment_year=2026,
        shipment_month=12,
    )
    gulf = loaded.dataset.get(
        business_date=date(2026, 6, 10),
        origin="us_gulf",
        shipment_year=2026,
        shipment_month=12,
    )

    assert first.generation == 2
    assert result.generation == 3
    assert result.previous_release_id == current.release_id
    assert result.requested_count == 2
    assert result.changed_count == 1
    assert result.unchanged_count == 1
    assert result.cleared_to_null_count == 1
    assert brazil is not None and brazil.cnf_cents_per_bushel == 100.0
    assert gulf is not None and gulf.cnf_cents_per_bushel is None
    assert gulf.cnf_source == "manual_ui"
    assert gulf.calculation_status == "incomplete"
    assert gulf.missing_reasons == ("missing_cnf",)


def test_no_change_preserves_index_release_mtime_and_calculated_at(tmp_path):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    apply_update(runtime_root, [update(key(), 100.0)])
    current = resolve_current_runtime_release(runtime_root)
    index_path = runtime_root / "release_index.json"
    before_sha = current.identity.index_sha256
    before_mtime = index_path.stat().st_mtime_ns
    before_releases = sorted(
        path.name for path in (runtime_root / "releases").iterdir()
    )
    before_calculated = current.manifest["calculated_at"]

    result = apply_update(
        runtime_root,
        [update(key(), 100.0, batch_id="batch-002")],
        release_id="must-not-exist",
        batch_id="batch-002",
    )
    after = resolve_current_runtime_release(runtime_root)

    assert result.status == "no_change"
    assert result.generation == 2
    assert after.identity.index_sha256 == before_sha
    assert index_path.stat().st_mtime_ns == before_mtime
    assert sorted(
        path.name for path in (runtime_root / "releases").iterdir()
    ) == before_releases
    assert after.manifest["calculated_at"] == before_calculated


@pytest.mark.parametrize(
    ("offset", "match"),
    [
        (("stale", None, None), "Current"),
        ((None, "0" * 64, None), "stale"),
        ((None, None, "0" * 64), "manual CNF"),
    ],
)
def test_stale_expected_identities_reject_before_writes(
    tmp_path, offset, match
):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    current = resolve_current_runtime_release(runtime_root)
    values = list(
        (
            current.release_id,
            current.identity.index_sha256,
            current.identity.manual_cnf_sha256,
        )
    )
    for index, value in enumerate(offset):
        if value is not None:
            values[index] = value

    with pytest.raises(RuntimeConcurrentUpdateError, match=match):
        apply_update(
            runtime_root,
            [update(key(), 100.0)],
            expected=tuple(values),
        )
    assert_clean_failed_transaction(
        runtime_root,
        current.release_id,
        current.identity.index_sha256,
    )


def test_duplicate_and_missing_keys_reject_before_writes(tmp_path):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    current = resolve_current_runtime_release(runtime_root)
    same = update(key(), 100.0)
    with pytest.raises(RuntimePipelineError, match="duplicate"):
        apply_update(runtime_root, [same, same])
    with pytest.raises(RuntimePipelineError, match="must exist"):
        apply_update(
            runtime_root,
            [
                update(
                    key(business_date=date(2026, 6, 11)),
                    100.0,
                )
            ],
        )
    assert_clean_failed_transaction(
        runtime_root,
        current.release_id,
        current.identity.index_sha256,
    )


@pytest.mark.parametrize(
    "timeout", [-1.0, math.inf, math.nan, True, 301.0]
)
def test_lock_timeout_must_be_finite_and_bounded(tmp_path, timeout):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    with pytest.raises(RuntimePipelineError, match="between 0 and 300"):
        apply_update(
            runtime_root,
            [update(key(), 100.0)],
            lock_timeout_seconds=timeout,
        )


def test_occupied_runtime_lock_fails_in_bounded_time(tmp_path):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    lock = FileLock(str(runtime_root / ".runtime.lock"))
    with lock.acquire(timeout=0):
        with pytest.raises(RuntimeLockedError, match="timed out"):
            apply_update(
                runtime_root,
                [update(key(), 100.0)],
                lock_timeout_seconds=0,
            )


@pytest.mark.parametrize(
    "failure_point",
    ["parquet", "manifest", "rename", "index"],
)
def test_transaction_failure_keeps_old_current_and_cleans_artifacts(
    tmp_path, monkeypatch, failure_point
):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    current = resolve_current_runtime_release(runtime_root)
    import agri_research_agent.pipelines.import_profit_runtime as pipeline

    if failure_point == "parquet":
        monkeypatch.setattr(
            pipeline,
            "write_parquet",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("injected parquet failure")
            ),
        )
    elif failure_point == "manifest":
        original = pipeline.write_json_exclusive

        def fail_manifest(path, payload):
            if path.name == MANIFEST_FILENAME:
                raise OSError("injected manifest failure")
            return original(path, payload)

        monkeypatch.setattr(pipeline, "write_json_exclusive", fail_manifest)
    elif failure_point == "rename":
        original = Path.replace

        def fail_building_rename(self, target):
            if self.name.startswith(".building-"):
                raise OSError("injected rename failure")
            return original(self, target)

        monkeypatch.setattr(Path, "replace", fail_building_rename)
    else:
        monkeypatch.setattr(
            pipeline,
            "promote_prepared_runtime_release",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeWriteError("injected index failure")
            ),
        )

    with pytest.raises((OSError, RuntimeWriteError)):
        apply_update(runtime_root, [update(key(), 100.0)])
    assert_clean_failed_transaction(
        runtime_root,
        current.release_id,
        current.identity.index_sha256,
    )
