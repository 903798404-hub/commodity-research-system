import hashlib
import importlib.util
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.pipelines import public_data_delivery as delivery
from agri_research_agent.market_data.activated_runtime import resolve_domestic_spread_path
from agri_research_agent.application.domestic_spreads import load_domestic_spread_database
from test_public_data_delivery import _current, _domestic_spread

ROOT = Path(__file__).resolve().parents[2]


def build(tmp_path, *, partitioned=True):
    public = tmp_path / "public"; _current(public, "tankan", "r1", "same immutable market")
    source = _domestic_spread(tmp_path / "source/historical_spread_database.parquet", value=10, updated_at="old")
    rows = pq.read_table(source).to_pylist()
    second = dict(rows[0]); second["date"] = second["date"].replace(month=9); rows.append(second)
    pq.write_table(pa.Table.from_pylist(rows), source)
    package = delivery.build_production_package(public_current_root=public, packages_root=tmp_path / "packages",
        source_max_dates={}, delivery_artifacts={"domestic-spread": source}, partitioned_delivery_artifacts=partitioned)
    return public, source, package


def test_only_changed_month_gets_new_object_and_all_business_history_stays_readable(tmp_path, monkeypatch):
    public, source, first = build(tmp_path)
    artifact = first.directory / "data" / first.manifest["delivery_artifacts"]["domestic-spread"]["package_path"]
    old = {p.name[5:12]: p for p in artifact.iterdir()}
    original = {p: p.read_bytes() for p in first.directory.rglob("*") if p.is_file()}
    rows = pq.read_table(source).to_pylist(); rows[1].update(leg2_price=2980.0, spread_value=20.0, updated_at="new")
    pq.write_table(pa.Table.from_pylist(rows), source)
    second = delivery.build_production_package(public_current_root=public, packages_root=tmp_path / "packages",
        source_max_dates={}, delivery_artifacts={"domestic-spread": source}, partitioned_delivery_artifacts=True)
    next_root = second.directory / "data" / second.manifest["delivery_artifacts"]["domestic-spread"]["package_path"]
    new = {p.name[5:12]: p for p in next_root.iterdir()}
    assert old["2026-08"].name == new["2026-08"].name
    assert old["2026-08"].stat().st_ino == new["2026-08"].stat().st_ino
    assert old["2026-09"].name != new["2026-09"].name
    assert len(list((tmp_path / "packages/.monthly-objects").iterdir())) == 3
    for p, raw in original.items(): assert p.read_bytes() == raw
    delivery.validate_production_package(first.directory); delivery.validate_production_package(second.directory)
    store = tmp_path / "store"; delivery.sync_to_local_server_store(second.directory, store_root=store)
    monkeypatch.setenv("PUBLIC_DATA_SERVER_STORE_ROOT", str(store))
    loaded = load_domestic_spread_database(resolve_domestic_spread_path(tmp_path / "unused"))
    assert loaded["date"].dt.strftime("%Y-%m").tolist() == ["2026-08", "2026-09"]
    assert loaded.spread_value.tolist() == [10, 20]


def test_old_and_new_package_storage_identity_are_distinct_and_rollback_remains_valid(tmp_path):
    _, _, old = build(tmp_path, partitioned=False)
    source = tmp_path / "source/historical_spread_database.parquet"
    new = delivery.build_production_package(public_current_root=tmp_path / "public", packages_root=tmp_path / "packages",
        source_max_dates={}, delivery_artifacts={"domestic-spread": source}, partitioned_delivery_artifacts=True)
    assert old.package_id != new.package_id
    old_rows = pq.read_table(old.directory / "data" / old.manifest["delivery_artifacts"]["domestic-spread"]["package_path"]).to_pandas()
    new_rows = pq.read_table(new.directory / "data" / new.manifest["delivery_artifacts"]["domestic-spread"]["package_path"]).to_pandas()
    assert old_rows.spread_value.tolist() == new_rows.spread_value.tolist()
    assert old.manifest["schema_version"].endswith("/2") and new.manifest["schema_version"].endswith("/3")
    spec = importlib.util.spec_from_file_location("partition_retention", ROOT / "04_scripts/maintenance/public_package_retention.py")
    retention = importlib.util.module_from_spec(spec); spec.loader.exec_module(retention)
    assert retention.validate(old.directory)[0]["package_id"] == old.package_id
    assert retention.validate(new.directory)[0]["package_id"] == new.package_id
    gate_spec = importlib.util.spec_from_file_location("partition_readability", ROOT / "04_scripts/application_runtime_readability_gate.py")
    gate = importlib.util.module_from_spec(gate_spec); gate_spec.loader.exec_module(gate)
    evidence = {}; gate._traverse_and_open(new.directory, evidence)
    assert evidence["PARQUET_METADATA_READ"] == "PASS" and evidence["PARQUET_FILE_COUNT"] == 2


def test_mutable_cached_object_and_corrupt_partition_refuse_new_publication(tmp_path):
    public, source, old = build(tmp_path)
    artifact = old.directory / "data" / old.manifest["delivery_artifacts"]["domestic-spread"]["package_path"]
    august = next(p for p in artifact.iterdir() if p.name.startswith("part-2026-08-"))
    obj = tmp_path / "packages/.monthly-objects" / august.name[13:]; obj.chmod(0o644)
    rows = pq.read_table(source).to_pylist(); rows[1].update(leg2_price=2980.0, spread_value=20.0)
    pq.write_table(pa.Table.from_pylist(rows), source)
    with pytest.raises(delivery.DeliveryError, match="mutable"):
        delivery.build_production_package(public_current_root=public, packages_root=tmp_path / "packages",
            source_max_dates={}, delivery_artifacts={"domestic-spread": source}, partitioned_delivery_artifacts=True)
    obj.chmod(0o444)
    assert delivery.validate_production_package(old.directory).package_id == old.package_id


def test_partition_conversion_refuses_numeric_precision_loss_without_rewriting_old_package(tmp_path):
    public, source, old = build(tmp_path, partitioned=False)
    unchanged = (old.directory / "manifest.json").read_bytes()
    rows = pq.read_table(source).to_pylist()
    for row in rows:row.update(leg1_price=2**53+1, leg2_price=2**53+1, spread_value=0)
    pq.write_table(pa.Table.from_pylist(rows), source)
    with pytest.raises(delivery.DeliveryError, match="historical values"):
        delivery.build_production_package(public_current_root=public, packages_root=tmp_path / "packages",
            source_max_dates={}, delivery_artifacts={"domestic-spread": source}, partitioned_delivery_artifacts=True)
    assert (old.directory / "manifest.json").read_bytes() == unchanged
    assert delivery.validate_production_package(old.directory).package_id == old.package_id


def test_new_partitioned_current_rolls_back_to_the_exact_old_single_file_package(tmp_path):
    public, source, old = build(tmp_path, partitioned=False)
    new = delivery.build_production_package(public_current_root=public, packages_root=tmp_path / "packages",
        source_max_dates={}, delivery_artifacts={"domestic-spread": source}, partitioned_delivery_artifacts=True)
    store = tmp_path / "store"; delivery.sync_to_local_server_store(old.directory, store_root=store)
    before = (store / "current.json").read_bytes()
    upload = store / "incoming" / (new.package_id + ".upload-test")
    shutil.copytree(new.directory, upload)
    assert delivery.activate_incoming_server_package(upload, store_root=store).status == "SYNCED"
    assert delivery.resolve_server_current(store).name == new.package_id
    restored = delivery.rollback_server_current_after_application_failure(store_root=store,
        expected_failed_package_id=new.package_id, rollback_package_id=old.package_id)
    assert restored.status == "ROLLED_BACK" and (store / "current.json").read_bytes() == before
    assert delivery.resolve_server_current(store).name == old.package_id
    assert delivery.validate_production_package(store / "releases" / new.package_id).package_id == new.package_id
