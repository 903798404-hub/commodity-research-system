from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path

from filelock import FileLock
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.import_profit.cnf_store import (
    CNF_FIELDS,
    CNF_SCHEMA,
    CnfConcurrentUpdateError,
    CnfDuplicateKeyError,
    CnfQuoteUpdate,
    CnfStoreLockedError,
    CnfStoreSchemaError,
    CnfStoreValidationError,
    CnfStoreWriteError,
    empty_cnf_dataframe,
    load_cnf_store,
    records_as_dataframe,
    upsert_cnf_quotes,
)
from agri_research_agent.import_profit.models import BusinessKey, BusinessKeyError


ORIGINS = ("brazil", "us_gulf", "us_pnw", "argentina")
UTC_TIME = datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc)


def key(
    *,
    business_date: date = date(2026, 7, 28),
    commodity: str = "soybean",
    origin: str = "brazil",
    shipment_year: int = 2027,
    shipment_month: int = 1,
    allowed_origins: tuple[str, ...] = ORIGINS,
    expected_commodity: str = "soybean",
) -> BusinessKey:
    return BusinessKey(
        business_date,
        commodity,
        origin,
        shipment_year,
        shipment_month,
        allowed_origins,
        expected_commodity,
    )


def update(
    *,
    business_key: BusinessKey | None = None,
    cnf: object = 100.0,
    source: str = "manual_ui",
    updated_at: datetime = UTC_TIME,
    batch_id: str = "batch-001",
) -> CnfQuoteUpdate:
    return CnfQuoteUpdate(
        business_key or key(),
        cnf,  # type: ignore[arg-type]
        source,
        updated_at,
        batch_id,
    )


def create_store(
    path: Path,
    updates: list[CnfQuoteUpdate] | None = None,
    *,
    backup_dir: Path | None = None,
):
    return upsert_cnf_quotes(
        path,
        updates or [update()],
        allowed_origins=ORIGINS,
        expected_store_sha256=None,
        backup_dir=backup_dir,
    )


def write_raw(path: Path, rows: list[dict], schema: pa.Schema = CNF_SCHEMA) -> None:
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def raw_row(
    *,
    business_date: date = date(2026, 7, 28),
    commodity: str = "soybean",
    origin: str = "brazil",
    shipment_year: int = 2027,
    shipment_month: int = 1,
    cnf: float | None = 100.0,
    source: str = "manual_ui",
    updated_at: datetime = UTC_TIME,
    batch_id: str = "batch-001",
) -> dict:
    return {
        "business_date": business_date,
        "commodity": commodity,
        "origin": origin,
        "shipment_year": shipment_year,
        "shipment_month": shipment_month,
        "cnf_cents_per_bushel": cnf,
        "source": source,
        "updated_at": updated_at,
        "batch_id": batch_id,
    }


def test_missing_file_returns_immutable_empty_snapshot_without_creating_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cnf.parquet"
    snapshot = load_cnf_store(path, allowed_origins=ORIGINS)
    assert snapshot.store_exists is False
    assert snapshot.record_count == 0
    assert snapshot.store_sha256 is None
    assert snapshot.records == ()
    assert not path.exists()
    assert list(empty_cnf_dataframe().columns) == list(CNF_FIELDS)
    with pytest.raises(FrozenInstanceError):
        snapshot.record_count = 1  # type: ignore[misc]


def test_normal_empty_parquet_and_fixed_schema(tmp_path: Path) -> None:
    path = tmp_path / "empty.parquet"
    pq.write_table(pa.Table.from_pylist([], schema=CNF_SCHEMA), path)
    snapshot = load_cnf_store(path, allowed_origins=ORIGINS)
    assert snapshot.store_exists is True
    assert snapshot.record_count == 0
    assert snapshot.store_sha256
    assert pq.read_table(path).schema == CNF_SCHEMA
    assert CNF_SCHEMA.names == list(CNF_FIELDS)


def test_normal_multirow_read_is_stably_sorted_and_caller_owned(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cnf.parquet"
    updates = [
        update(
            business_key=key(origin=origin),
            cnf=index - 2,
            batch_id="batch-four",
        )
        for index, origin in enumerate(sorted(ORIGINS))
    ]
    create_store(path, updates)
    snapshot = load_cnf_store(path, allowed_origins=ORIGINS)
    assert [record.business_key.origin for record in snapshot.records] == sorted(ORIGINS)
    frame = records_as_dataframe(snapshot.records)
    frame.loc[0, "origin"] = "mutated"
    assert snapshot.records[0].business_key.origin != "mutated"


@pytest.mark.parametrize("origin", ORIGINS)
def test_all_four_configured_origins_are_valid(tmp_path: Path, origin: str) -> None:
    path = tmp_path / f"{origin}.parquet"
    result = create_store(path, [update(business_key=key(origin=origin))])
    assert result.status == "success"


def test_unknown_origin_and_non_soybean_are_rejected(tmp_path: Path) -> None:
    unknown_key = key(origin="unknown", allowed_origins=(*ORIGINS, "unknown"))
    rapeseed_key = key(
        commodity="rapeseed",
        expected_commodity="rapeseed",
    )
    with pytest.raises(CnfStoreValidationError):
        create_store(tmp_path / "unknown.parquet", [update(business_key=unknown_key)])
    with pytest.raises(CnfStoreValidationError):
        create_store(tmp_path / "commodity.parquet", [update(business_key=rapeseed_key)])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"shipment_month": 0},
        {"shipment_month": 13},
        {"shipment_year": 1899},
        {"shipment_year": 2200},
    ],
)
def test_invalid_year_or_month_is_rejected_by_shared_business_key(
    kwargs: dict,
) -> None:
    with pytest.raises(BusinessKeyError):
        key(**kwargs)


@pytest.mark.parametrize("value", [10, -10, 0, None, 1.25, -2.75])
def test_valid_cnf_values_round_trip(tmp_path: Path, value: object) -> None:
    path = tmp_path / "value.parquet"
    create_store(path, [update(cnf=value)])
    stored = load_cnf_store(path, allowed_origins=ORIGINS).records[0]
    assert stored.cnf_cents_per_bushel == (
        None if value is None else float(value)  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "value",
    [True, False, float("nan"), float("inf"), float("-inf"), "", "10", [], {}],
)
def test_invalid_cnf_values_are_rejected(value: object) -> None:
    with pytest.raises(CnfStoreValidationError):
        update(cnf=value)


def test_source_time_and_batch_validation_and_utc_conversion() -> None:
    with pytest.raises(CnfStoreValidationError, match="source"):
        update(source="excel")
    with pytest.raises(CnfStoreValidationError, match="timezone"):
        update(updated_at=datetime(2026, 7, 28, 8, 0))
    for batch_id in ("", " ", "../secret", "a/b"):
        with pytest.raises(CnfStoreValidationError, match="batch_id"):
            update(batch_id=batch_id)
    local_time = datetime(
        2026, 7, 28, 16, 0, tzinfo=timezone(timedelta(hours=8))
    )
    actual = update(updated_at=local_time)
    assert actual.updated_at == UTC_TIME
    assert actual.updated_at.tzinfo is timezone.utc


def test_new_key_insert_and_exact_existing_key_update(tmp_path: Path) -> None:
    path = tmp_path / "cnf.parquet"
    first = create_store(path)
    result = upsert_cnf_quotes(
        path,
        [update(cnf=125.0, updated_at=UTC_TIME + timedelta(hours=1), batch_id="batch-002")],
        allowed_origins=ORIGINS,
        expected_store_sha256=first.new_sha256,
    )
    assert result.status == "success"
    assert result.inserted_count == 0
    assert result.updated_count == 1
    assert result.unchanged_count == 0
    snapshot = load_cnf_store(path, allowed_origins=ORIGINS)
    assert snapshot.record_count == 1
    assert snapshot.records[0].cnf_cents_per_bushel == 125.0
    assert snapshot.records[0].batch_id == "batch-002"


def test_null_zero_and_number_transitions_are_distinct(tmp_path: Path) -> None:
    path = tmp_path / "cnf.parquet"
    created = create_store(path, [update(cnf=10.0)])
    cleared = upsert_cnf_quotes(
        path,
        [update(cnf=None, batch_id="batch-002")],
        allowed_origins=ORIGINS,
        expected_store_sha256=created.new_sha256,
    )
    assert cleared.cleared_to_null_count == 1
    assert load_cnf_store(path, allowed_origins=ORIGINS).records[0].cnf_cents_per_bushel is None
    zeroed = upsert_cnf_quotes(
        path,
        [update(cnf=0, batch_id="batch-003")],
        allowed_origins=ORIGINS,
        expected_store_sha256=cleared.new_sha256,
    )
    assert zeroed.updated_count == 1
    assert load_cnf_store(path, allowed_origins=ORIGINS).records[0].cnf_cents_per_bushel == 0
    cleared_again = upsert_cnf_quotes(
        path,
        [update(cnf=None, batch_id="batch-004")],
        allowed_origins=ORIGINS,
        expected_store_sha256=zeroed.new_sha256,
    )
    assert cleared_again.cleared_to_null_count == 1


def test_new_null_key_is_retained_not_deleted(tmp_path: Path) -> None:
    path = tmp_path / "cnf.parquet"
    result = create_store(path, [update(cnf=None)])
    assert result.inserted_count == 1
    snapshot = load_cnf_store(path, allowed_origins=ORIGINS)
    assert snapshot.record_count == 1
    assert snapshot.records[0].cnf_cents_per_bushel is None


def test_multi_key_batch_is_atomic_and_counts_are_correct(tmp_path: Path) -> None:
    path = tmp_path / "cnf.parquet"
    updates = [
        update(
            business_key=key(origin=origin, shipment_month=index + 1),
            cnf=float(index),
            batch_id="batch-multi",
        )
        for index, origin in enumerate(ORIGINS)
    ]
    result = create_store(path, updates)
    assert result.inserted_count == 4
    assert result.updated_count == 0
    assert result.new_record_count == 4
    assert result.updated_keys == tuple(sorted(result.updated_keys))


def test_duplicate_batch_key_rejects_whole_batch(tmp_path: Path) -> None:
    path = tmp_path / "cnf.parquet"
    with pytest.raises(CnfDuplicateKeyError):
        create_store(path, [update(cnf=1), update(cnf=2)])
    assert not path.exists()


def test_formal_store_duplicate_keys_and_unsorted_rows_are_rejected(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.parquet"
    write_raw(duplicate, [raw_row(), raw_row(cnf=2)])
    with pytest.raises(CnfDuplicateKeyError):
        load_cnf_store(duplicate, allowed_origins=ORIGINS)

    unsorted = tmp_path / "unsorted.parquet"
    write_raw(
        unsorted,
        [
            raw_row(origin="us_gulf"),
            raw_row(origin="brazil", shipment_month=2),
        ],
    )
    with pytest.raises(CnfStoreValidationError, match="sorted"):
        load_cnf_store(unsorted, allowed_origins=ORIGINS)


def test_schema_type_and_order_corruption_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad-schema.parquet"
    wrong = pa.schema(
        [
            pa.field("commodity", pa.string(), nullable=False),
            pa.field("business_date", pa.date32(), nullable=False),
        ]
    )
    write_raw(
        path,
        [{"commodity": "soybean", "business_date": date(2026, 7, 28)}],
        wrong,
    )
    with pytest.raises(CnfStoreSchemaError):
        load_cnf_store(path, allowed_origins=ORIGINS)


def test_update_one_key_does_not_affect_other_origin_date_or_year(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cnf.parquet"
    keys = [
        key(origin="brazil", shipment_year=2026, shipment_month=12),
        key(origin="us_gulf", shipment_year=2027, shipment_month=12),
        key(business_date=date(2026, 7, 29), origin="brazil", shipment_year=2027, shipment_month=12),
    ]
    created = create_store(
        path,
        [
            update(business_key=item, cnf=index + 1, batch_id="batch-base")
            for index, item in enumerate(keys)
        ],
    )
    result = upsert_cnf_quotes(
        path,
        [
            update(
                business_key=keys[0],
                cnf=99,
                batch_id="batch-change",
            )
        ],
        allowed_origins=ORIGINS,
        expected_store_sha256=created.new_sha256,
    )
    assert result.updated_count == 1
    values = {
        record.key: record.cnf_cents_per_bushel
        for record in load_cnf_store(path, allowed_origins=ORIGINS).records
    }
    assert values[(date(2026, 7, 28), "soybean", "brazil", 2026, 12)] == 99
    assert values[(date(2026, 7, 28), "soybean", "us_gulf", 2027, 12)] == 2
    assert values[(date(2026, 7, 29), "soybean", "brazil", 2027, 12)] == 3


def test_expected_sha_guards_all_existence_transitions(tmp_path: Path) -> None:
    path = tmp_path / "cnf.parquet"
    created = create_store(path)
    with pytest.raises(CnfConcurrentUpdateError):
        upsert_cnf_quotes(
            path,
            [update(cnf=2, batch_id="stale")],
            allowed_origins=ORIGINS,
            expected_store_sha256="0" * 64,
        )
    with pytest.raises(CnfConcurrentUpdateError):
        upsert_cnf_quotes(
            path,
            [update(cnf=2, batch_id="expected-missing")],
            allowed_origins=ORIGINS,
            expected_store_sha256=None,
        )
    path.unlink()
    with pytest.raises(CnfConcurrentUpdateError):
        upsert_cnf_quotes(
            path,
            [update(cnf=2, batch_id="expected-present")],
            allowed_origins=ORIGINS,
            expected_store_sha256=created.new_sha256,
        )
    assert not path.exists()


def test_lock_contention_fails_in_finite_time(tmp_path: Path) -> None:
    path = tmp_path / "cnf.parquet"
    lock = FileLock(f"{path}.lock")
    lock.acquire(timeout=0)
    try:
        with pytest.raises(CnfStoreLockedError):
            upsert_cnf_quotes(
                path,
                [update()],
                allowed_origins=ORIGINS,
                expected_store_sha256=None,
                lock_timeout_seconds=0,
            )
    finally:
        lock.release()


def test_no_change_ignores_new_audit_metadata_and_does_not_write_or_backup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cnf.parquet"
    backup = tmp_path / "backups"
    created = create_store(path)
    before_stat = path.stat()
    result = upsert_cnf_quotes(
        path,
        [
            update(
                cnf=100.0,
                updated_at=UTC_TIME + timedelta(days=1),
                batch_id="different-batch",
            )
        ],
        allowed_origins=ORIGINS,
        expected_store_sha256=created.new_sha256,
        backup_dir=backup,
    )
    after_stat = path.stat()
    assert result.status == "no_change"
    assert result.previous_sha256 == result.new_sha256 == created.new_sha256
    assert result.unchanged_count == 1
    assert result.updated_keys == ()
    assert result.backup_created is False
    assert before_stat.st_mtime_ns == after_stat.st_mtime_ns
    assert before_stat.st_size == after_stat.st_size
    assert not backup.exists()
    stored = load_cnf_store(path, allowed_origins=ORIGINS).records[0]
    assert stored.updated_at == UTC_TIME
    assert stored.batch_id == "batch-001"


def test_success_changes_sha_and_creates_verified_backup(tmp_path: Path) -> None:
    path = tmp_path / "cnf.parquet"
    backup_dir = tmp_path / "backups"
    created = create_store(path)
    result = upsert_cnf_quotes(
        path,
        [update(cnf=200, batch_id="batch-002")],
        allowed_origins=ORIGINS,
        expected_store_sha256=created.new_sha256,
        backup_dir=backup_dir,
    )
    assert result.status == "success"
    assert result.new_sha256 != result.previous_sha256
    assert result.backup_created is True
    assert result.backup_filename is not None
    assert "/" not in result.backup_filename
    assert "\\" not in result.backup_filename
    backup = backup_dir / result.backup_filename
    assert backup.exists()
    assert load_cnf_store(backup, allowed_origins=ORIGINS).store_sha256 == created.new_sha256


def test_backup_collision_and_failure_prevent_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cnf.parquet"
    created = create_store(path)
    original_bytes = path.read_bytes()
    real_copy = __import__("agri_research_agent.import_profit.cnf_store", fromlist=["shutil"]).shutil.copy2
    calls = 0

    def fail_backup(source: Path, destination: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("backup failed")
        return real_copy(source, destination)

    import agri_research_agent.import_profit.cnf_store as module

    monkeypatch.setattr(module.shutil, "copy2", fail_backup)
    with pytest.raises(CnfStoreWriteError):
        upsert_cnf_quotes(
            path,
            [update(cnf=200, batch_id="batch-002")],
            allowed_origins=ORIGINS,
            expected_store_sha256=created.new_sha256,
            backup_dir=tmp_path / "backups",
        )
    assert path.read_bytes() == original_bytes


def test_parquet_write_and_replace_failures_preserve_original_and_clean_temps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agri_research_agent.import_profit.cnf_store as module

    path = tmp_path / "cnf.parquet"
    created = create_store(path)
    original = path.read_bytes()
    real_write = module.pq.write_table
    monkeypatch.setattr(
        module.pq,
        "write_table",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )
    with pytest.raises(CnfStoreWriteError):
        upsert_cnf_quotes(
            path,
            [update(cnf=2, batch_id="write-fail")],
            allowed_origins=ORIGINS,
            expected_store_sha256=created.new_sha256,
        )
    assert path.read_bytes() == original
    monkeypatch.setattr(module.pq, "write_table", real_write)
    real_replace = module.os.replace
    monkeypatch.setattr(
        module.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(CnfStoreWriteError):
        upsert_cnf_quotes(
            path,
            [update(cnf=3, batch_id="replace-fail")],
            allowed_origins=ORIGINS,
            expected_store_sha256=created.new_sha256,
        )
    monkeypatch.setattr(module.os, "replace", real_replace)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".*.tmp"))
    assert not list(tmp_path.glob(".*.rollback"))


def test_invalid_mixed_batch_changes_nothing_and_inputs_are_not_mutated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cnf.parquet"
    created = create_store(path)
    original = path.read_bytes()
    first = update(cnf=2, batch_id="batch-good")
    inputs = [first, object()]
    before = list(inputs)
    with pytest.raises(CnfStoreValidationError):
        upsert_cnf_quotes(
            path,
            inputs,  # type: ignore[arg-type]
            allowed_origins=ORIGINS,
            expected_store_sha256=created.new_sha256,
        )
    assert inputs == before
    assert path.read_bytes() == original


def test_schema_contains_no_history_or_sensitive_fields() -> None:
    assert CNF_SCHEMA.names == [
        "business_date",
        "commodity",
        "origin",
        "shipment_year",
        "shipment_month",
        "cnf_cents_per_bushel",
        "source",
        "updated_at",
        "batch_id",
    ]
    forbidden = {
        "old_value",
        "previous_value",
        "revision",
        "reason",
        "username",
        "password",
        "token",
        "server",
        "path",
        "session",
    }
    assert forbidden.isdisjoint(CNF_SCHEMA.names)


def test_repeated_fixed_input_is_stable(tmp_path: Path) -> None:
    path = tmp_path / "cnf.parquet"
    created = create_store(path)
    first_bytes = path.read_bytes()
    repeated = upsert_cnf_quotes(
        path,
        [update()],
        allowed_origins=ORIGINS,
        expected_store_sha256=created.new_sha256,
    )
    assert repeated.status == "no_change"
    assert path.read_bytes() == first_bytes
    assert not list(tmp_path.glob(".*.tmp"))
    assert not list(tmp_path.glob(".*.rollback"))
    assert Path(f"{path}.lock").name == "cnf.parquet.lock"
