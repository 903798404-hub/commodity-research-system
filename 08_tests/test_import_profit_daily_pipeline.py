from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

from filelock import FileLock
import pytest

from agri_research_agent.import_profit.daily_increment import (
    capture_and_store_dce_morning_input,
)
from agri_research_agent.import_profit.morning_external_inputs import (
    store_morning_external_inputs_candidate,
)
from agri_research_agent.import_profit.runtime_store import (
    RuntimeLockedError,
    RuntimeWriteError,
    file_sha256,
    load_runtime_release_dataset,
    resolve_current_runtime_release,
)
from agri_research_agent.pipelines.import_profit_daily import (
    DailyMaterializationError,
    try_materialize_import_profit_business_day,
)
from agri_research_agent.pipelines.import_profit_runtime import (
    RuntimeCnfUpdate,
    update_runtime_cnf_quotes,
)
from test_import_profit_daily_increment import (
    CONFIG_PATH,
    TARGET,
    clock_at,
    frame_for,
)
from test_import_profit_morning_external_inputs import reuters_candidate
from test_import_profit_runtime_store import CONFIG, bootstrap_fixture
from test_import_profit_runtime_pipeline import apply_update, key as historical_key, update
from test_import_profit_components import configured_rows


CALCULATED = datetime(2026, 8, 5, 2, 0, tzinfo=timezone.utc)


def prepare_external(tmp_path, *, candidate_id="external-001"):
    source = reuters_candidate(tmp_path / f"reuters-{candidate_id}")
    return store_morning_external_inputs_candidate(
        tmp_path / "external",
        reuters_candidate_dir=source,
        business_date=TARGET,
        config=CONFIG,
        candidate_id=candidate_id,
        source_file_uploaded_at=datetime(2026, 8, 5, 0, 35, tzinfo=timezone.utc),
        prepared_at=datetime(2026, 8, 5, 0, 40, tzinfo=timezone.utc),
        promoted_at=datetime(2026, 8, 5, 0, 41, tzinfo=timezone.utc),
    )


def prepare_dce(tmp_path, *, failed=False, candidate_id="dce-001"):
    return capture_and_store_dce_morning_input(
        tmp_path / "dce",
        business_date=TARGET,
        config=CONFIG,
        candidate_id=candidate_id,
        snapshot_batch_id=f"snapshot-{candidate_id}",
        fetcher=lambda request: frame_for(request, omit_last=failed),
        clock=clock_at(9, 0, 1),
    )


def materialize(runtime_root, tmp_path, *, release_id="daily-001", timeout=1.0):
    current = resolve_current_runtime_release(runtime_root)
    return try_materialize_import_profit_business_day(
        runtime_root,
        external_input_root=tmp_path / "external",
        dce_input_root=tmp_path / "dce",
        business_date=TARGET,
        config=CONFIG,
        expected_runtime_release_id=current.release_id,
        expected_runtime_index_sha256=current.identity.index_sha256,
        expected_manual_cnf_sha256=current.identity.manual_cnf_sha256,
        release_id=release_id,
        batch_id=f"batch-{release_id}",
        calculated_at=CALCULATED,
        lock_timeout_seconds=timeout,
    )


@pytest.mark.parametrize("first", ["external", "dce"])
def test_both_input_arrival_orders_use_one_idempotent_materializer(tmp_path, first):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    if first == "external":
        prepare_external(tmp_path)
        waiting = materialize(runtime_root, tmp_path)
        assert waiting.status == "waiting_for_dce_capture"
        prepare_dce(tmp_path)
    else:
        prepare_dce(tmp_path)
        waiting = materialize(runtime_root, tmp_path)
        assert waiting.status == "waiting_for_external_inputs"
        prepare_external(tmp_path)

    before = resolve_current_runtime_release(runtime_root)
    result = materialize(runtime_root, tmp_path)
    loaded = load_runtime_release_dataset(runtime_root)
    index_bytes = (runtime_root / "release_index.json").read_bytes()
    repeated = materialize(runtime_root, tmp_path, release_id="daily-002")

    assert result.status == "materialized"
    assert result.appended_business_key_count == 48
    assert result.success_count_delta == 0
    assert result.incomplete_count_delta == 48
    assert loaded.dataset.business_key_count == 50
    assert loaded.resolved.previous_release_id == before.release_id
    assert loaded.resolved.manual_cnf_exists is False
    assert sum(
        record.business_date == TARGET for record in loaded.dataset.records
    ) == 48
    assert all(
        record.cnf_cents_per_bushel is None
        and record.cnf_source == "manual_ui"
        and "missing_cnf" in record.missing_reasons
        for record in loaded.dataset.records
        if record.business_date == TARGET
    )
    assert repeated.status == "already_materialized"
    assert (runtime_root / "release_index.json").read_bytes() == index_bytes


def test_legal_failed_dce_still_materializes_all_keys_with_missing_dce(tmp_path):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    prepare_external(tmp_path)
    prepare_dce(tmp_path, failed=True)

    result = materialize(runtime_root, tmp_path)
    records = [
        record
        for record in load_runtime_release_dataset(runtime_root).dataset.records
        if record.business_date == TARGET
    ]

    assert result.status == "materialized"
    assert len(records) == 48
    assert all(
        "missing_soymeal" in record.missing_reasons
        and "missing_soyoil" in record.missing_reasons
        for record in records
    )
    assert (
        load_runtime_release_dataset(runtime_root)
        .resolved.manifest["morning_open_snapshot_start_date"]
        is None
    )


def test_followup_manual_cnf_uses_frozen_daily_market_and_only_adds_real_key(
    tmp_path,
):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    prepare_external(tmp_path)
    prepare_dce(tmp_path)
    materialize(runtime_root, tmp_path)
    current = resolve_current_runtime_release(runtime_root)
    target = next(
        record
        for record in load_runtime_release_dataset(runtime_root).dataset.records
        if record.business_date == TARGET
        and record.origin == "brazil"
        and record.shipment_month == 1
    )
    from agri_research_agent.import_profit.models import BusinessKey

    key = BusinessKey(
        target.business_date,
        target.commodity,
        target.origin,
        target.shipment_year,
        target.shipment_month,
        CONFIG.origin_codes,
        CONFIG.commodity,
    )
    update_runtime_cnf_quotes(
        runtime_root,
        [
            RuntimeCnfUpdate(
                business_key=key,
                cnf_cents_per_bushel=100.0,
                updated_at=CALCULATED,
                batch_id="manual-after-daily",
            )
        ],
        config=CONFIG,
        config_path=CONFIG_PATH,
        expected_release_id=current.release_id,
        expected_index_sha256=current.identity.index_sha256,
        expected_manual_cnf_sha256=None,
        calculated_at=CALCULATED,
        batch_id="manual-after-daily",
        release_id="manual-after-daily",
    )
    loaded = load_runtime_release_dataset(runtime_root)
    changed = loaded.dataset.get(
        business_date=key.business_date,
        origin=key.origin,
        shipment_year=key.shipment_year,
        shipment_month=key.shipment_month,
    )

    assert changed is not None and changed.calculation_status == "success"
    assert changed.soymeal_price_type == "morning_open_snapshot"
    assert loaded.resolved.manifest["morning_open_snapshot_start_date"] == (
        TARGET.isoformat()
    )
    assert loaded.resolved.manifest["manual_cnf_record_count"] == 1


def test_lock_timeout_and_promotion_failure_leave_current_unchanged(
    tmp_path, monkeypatch
):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    prepare_external(tmp_path)
    prepare_dce(tmp_path)
    current = resolve_current_runtime_release(runtime_root)
    lock = FileLock(str(runtime_root / ".runtime.lock"))
    lock.acquire()
    try:
        with pytest.raises(RuntimeLockedError):
            materialize(runtime_root, tmp_path, timeout=0.01)
    finally:
        lock.release()

    import agri_research_agent.pipelines.import_profit_daily as pipeline

    monkeypatch.setattr(
        pipeline,
        "promote_prepared_runtime_release",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeWriteError("injected promotion failure")
        ),
    )
    with pytest.raises(RuntimeWriteError):
        materialize(runtime_root, tmp_path)
    after = resolve_current_runtime_release(runtime_root)
    assert after.release_id == current.release_id
    assert after.identity.index_sha256 == current.identity.index_sha256
    assert not list((runtime_root / "releases").glob(".building-*"))


def test_existing_manual_cnf_is_byte_identical_and_correction_does_not_rewrite_day(
    tmp_path,
):
    runtime_root, _, _ = bootstrap_fixture(tmp_path)
    apply_update(runtime_root, [update(historical_key(), 100.0)])
    before = resolve_current_runtime_release(runtime_root)
    manual_sha = file_sha256(before.manual_cnf_path)
    prepare_external(tmp_path)
    prepare_dce(tmp_path)
    materialize(runtime_root, tmp_path)
    daily = resolve_current_runtime_release(runtime_root)

    assert file_sha256(daily.manual_cnf_path) == manual_sha
    assert daily.manifest["manual_cnf_record_count"] == 1
    runtime_index_before = (runtime_root / "release_index.json").read_bytes()

    source = reuters_candidate(tmp_path / "reuters-correction")
    source_manifest = json.loads((source / "manifest.json").read_text("utf-8"))
    source_manifest["source_sha256"] = "C" * 64
    (source / "manifest.json").write_text(json.dumps(source_manifest), "utf-8")
    store_morning_external_inputs_candidate(
        tmp_path / "external",
        reuters_candidate_dir=source,
        business_date=TARGET,
        config=CONFIG,
        candidate_id="external-correction",
        source_file_uploaded_at=datetime(2026, 8, 5, 1, 5, tzinfo=timezone.utc),
        prepared_at=datetime(2026, 8, 5, 1, 6, tzinfo=timezone.utc),
        promoted_at=datetime(2026, 8, 5, 1, 7, tzinfo=timezone.utc),
    )
    repeated = materialize(runtime_root, tmp_path, release_id="daily-correction")

    assert repeated.status == "already_materialized"
    assert (runtime_root / "release_index.json").read_bytes() == runtime_index_before
    assert file_sha256(
        resolve_current_runtime_release(runtime_root).manual_cnf_path
    ) == manual_sha


def test_partial_target_date_is_fatal_and_never_auto_filled(tmp_path):
    partial = configured_rows(TARGET, "brazil", 2027, 1, cnf=None)
    runtime_root, _, _ = bootstrap_fixture(tmp_path, records=[partial])
    prepare_external(tmp_path)
    prepare_dce(tmp_path)
    current = resolve_current_runtime_release(runtime_root)

    with pytest.raises(DailyMaterializationError, match="partial"):
        materialize(runtime_root, tmp_path)

    after = resolve_current_runtime_release(runtime_root)
    assert after.release_id == current.release_id
    assert after.identity.index_sha256 == current.identity.index_sha256
