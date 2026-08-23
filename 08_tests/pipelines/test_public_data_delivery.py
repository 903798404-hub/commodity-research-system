from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.pipelines.public_data_delivery import (
    activate_incoming_server_package,
    DeliveryError,
    PrewarmStatus,
    PrewarmTarget,
    build_production_package,
    resolve_server_current,
    run_prewarm,
    sync_to_local_server_store,
    validate_production_package,
)
from agri_research_agent.market_data.activated_runtime import (
    resolve_domestic_spread_path,
    resolve_public_data_root,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _current(root: Path, dataset: str, release_id: str, value: str) -> Path:
    release = root / dataset / "releases" / release_id
    release.mkdir(parents=True)
    (release / "data.txt").write_text(value, encoding="utf-8")
    data_path = release / "data.txt"
    (release / "manifest.json").write_text(
        json.dumps({
            "release_id": release_id,
            "quality_status": "PASS",
            "files": {
                "data.txt": {"sha256": _sha(data_path), "size_bytes": data_path.stat().st_size}
            },
        }),
        encoding="utf-8",
    )
    pointer = {
        "schema_version": 1,
        "release_id": release_id,
        "manifest_sha256": _sha(release / "manifest.json"),
    }
    (root / dataset / "current.json").write_text(json.dumps(pointer), encoding="utf-8")
    return release


def _domestic_spread(path: Path, *, value: float, updated_at: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([
            {
                "date": date(2026, 8, 21),
                "spread_group": "meal",
                "spread_name": "M9-1",
                "leg1_instrument": "M",
                "leg1_month": 9,
                "leg1_price": 3000.0,
                "leg2_instrument": "M",
                "leg2_month": 1,
                "leg2_price": 3000.0 - value,
                "spread_value": value,
                "season": "2026/27",
                "calendar_offset": 0,
                "status": "success",
                "updated_at": updated_at,
            }
        ]),
        path,
    )
    return path


def test_same_current_build_is_content_idempotent_and_mtime_independent(tmp_path: Path) -> None:
    public = tmp_path / "public-market-data"
    release = _current(public, "tankan", "r1", "one")
    one = build_production_package(
        public_current_root=public,
        packages_root=tmp_path / "packages",
        source_max_dates={"tankan.market": "2026-08-21"},
    )
    os.utime(release / "data.txt", None)
    two = build_production_package(
        public_current_root=public,
        packages_root=tmp_path / "packages",
        source_max_dates={"tankan.market": "2026-08-22"},
    )
    assert one.package_id == two.package_id
    assert one.directory == two.directory
    assert one.manifest["current_identity_sha256"] == two.manifest["current_identity_sha256"]


def test_package_contains_only_active_release_and_validates_sha(tmp_path: Path) -> None:
    public = tmp_path / "public-market-data"
    _current(public, "tankan", "old", "old")
    _current(public, "tankan", "new", "new")
    package = build_production_package(
        public_current_root=public,
        packages_root=tmp_path / "packages",
        source_max_dates={},
    )
    packaged = package.directory / "data/public-market-data/tankan/releases"
    assert [item.name for item in packaged.iterdir()] == ["new"]
    target = packaged / "new/data.txt"
    target.write_text("tampered", encoding="utf-8")
    with pytest.raises(DeliveryError, match="files differ"):
        validate_production_package(package.directory)


def test_domestic_spread_business_change_alone_creates_and_switches_aggregate(
    tmp_path: Path,
) -> None:
    public = tmp_path / "public-market-data"
    _current(public, "tankan", "r1", "unchanged")
    artifact = _domestic_spread(
        tmp_path / "source" / "historical_spread_database.parquet",
        value=-38.0,
        updated_at="2026-08-23 08:00:00",
    )
    first = build_production_package(
        public_current_root=public,
        packages_root=tmp_path / "packages",
        source_max_dates={},
        delivery_artifacts={"domestic-spread": artifact},
    )
    store = tmp_path / "server"
    assert sync_to_local_server_store(first.directory, store_root=store).status == "SYNCED"

    _domestic_spread(artifact, value=-42.0, updated_at="2026-08-23 09:00:00")
    second = build_production_package(
        public_current_root=public,
        packages_root=tmp_path / "packages",
        source_max_dates={},
        delivery_artifacts={"domestic-spread": artifact},
    )
    result = sync_to_local_server_store(second.directory, store_root=store)

    assert first.manifest["current_identity_sha256"] == second.manifest["current_identity_sha256"]
    assert first.package_id != second.package_id
    assert result.status == "SYNCED"
    assert resolve_server_current(store).name == second.package_id
    packaged = result.current_directory / "data/consumer-artifacts/domestic-spread/historical_spread_database.parquet"
    assert pq.read_table(packaged)["spread_value"].to_pylist() == [-42.0]


def test_domestic_spread_runtime_timestamp_and_mtime_do_not_change_delivery_identity(
    tmp_path: Path,
) -> None:
    public = tmp_path / "public-market-data"
    _current(public, "tankan", "r1", "unchanged")
    artifact = _domestic_spread(
        tmp_path / "source" / "historical_spread_database.parquet",
        value=-38.0,
        updated_at="2026-08-23 08:00:00",
    )
    first = build_production_package(
        public_current_root=public,
        packages_root=tmp_path / "packages",
        source_max_dates={},
        delivery_artifacts={"domestic-spread": artifact},
    )
    _domestic_spread(artifact, value=-38.0, updated_at="2026-08-23 09:00:00")
    os.utime(artifact, None)
    second = build_production_package(
        public_current_root=public,
        packages_root=tmp_path / "packages",
        source_max_dates={},
        delivery_artifacts={"domestic-spread": artifact},
    )

    assert first.package_id == second.package_id
    assert second.created is False


def test_invalid_domestic_spread_artifact_never_switches_existing_pointer(
    tmp_path: Path,
) -> None:
    public = tmp_path / "public-market-data"
    _current(public, "tankan", "r1", "unchanged")
    first = build_production_package(
        public_current_root=public,
        packages_root=tmp_path / "packages",
        source_max_dates={},
    )
    store = tmp_path / "server"
    sync_to_local_server_store(first.directory, store_root=store)
    before = (store / "current.json").read_bytes()
    broken = tmp_path / "source" / "historical_spread_database.parquet"
    broken.parent.mkdir()
    broken.write_text("not parquet", encoding="utf-8")

    with pytest.raises(DeliveryError, match="unreadable"):
        build_production_package(
            public_current_root=public,
            packages_root=tmp_path / "packages",
            source_max_dates={},
            delivery_artifacts={"domestic-spread": broken},
        )
    assert (store / "current.json").read_bytes() == before


def test_builder_rejects_unsafe_dataset_id_and_undeclared_release_file(tmp_path: Path) -> None:
    public = tmp_path / "public-market-data"
    release = _current(public, "tankan", "r1", "one")
    with pytest.raises(DeliveryError, match="unsafe dataset"):
        build_production_package(
            public_current_root=public,
            packages_root=tmp_path / "packages",
            source_max_dates={},
            required_datasets=["../tankan"],
        )
    (release / "undeclared.txt").write_text("not sealed", encoding="utf-8")
    with pytest.raises(DeliveryError, match="differ from the release manifest"):
        build_production_package(
            public_current_root=public,
            packages_root=tmp_path / "packages",
            source_max_dates={},
        )


def test_server_sync_stages_validates_and_switches_one_pointer(tmp_path: Path) -> None:
    public = tmp_path / "public-market-data"
    _current(public, "tankan", "r1", "one")
    package = build_production_package(
        public_current_root=public,
        packages_root=tmp_path / "packages",
        source_max_dates={},
    )
    seen: list[str] = []
    result = sync_to_local_server_store(
        package.directory,
        store_root=tmp_path / "server",
        pre_switch_validator=lambda data: seen.append(f"pre:{data.name}"),
        post_switch_validator=lambda data: seen.append(f"post:{data.name}"),
    )
    assert result.status == "SYNCED"
    assert (result.manifest, result.sha, result.atomic_switch) == ("PASS", "PASS", "PASS")
    assert result.formal_read_validation == "PASS"
    assert resolve_server_current(tmp_path / "server") == result.current_directory
    assert seen == ["pre:data", "post:data"]


def test_uploaded_package_activates_in_place_and_is_idempotent(tmp_path: Path) -> None:
    public = tmp_path / "public-market-data"
    _current(public, "tankan", "r1", "one")
    package = build_production_package(
        public_current_root=public, packages_root=tmp_path / "packages", source_max_dates={}
    )
    store = tmp_path / "server"
    upload = store / "incoming" / f"{package.package_id}.upload-one"
    upload.parent.mkdir(parents=True)
    shutil.copytree(package.directory, upload)

    result = activate_incoming_server_package(upload, store_root=store)

    assert result.status == "SYNCED"
    assert not upload.exists()
    assert resolve_server_current(store).name == package.package_id
    second_upload = store / "incoming" / f"{package.package_id}.upload-two"
    shutil.copytree(package.directory, second_upload)
    repeated = activate_incoming_server_package(second_upload, store_root=store)
    assert repeated.status == "NO_CHANGE"
    assert not second_upload.exists()


def test_initial_seed_refuses_initialized_local_store(tmp_path: Path) -> None:
    public = tmp_path / "public-market-data"
    _current(public, "tankan", "r1", "one")
    package = build_production_package(
        public_current_root=public, packages_root=tmp_path / "packages", source_max_dates={}
    )
    store = tmp_path / "server"
    sync_to_local_server_store(package.directory, store_root=store)
    before = (store / "current.json").read_bytes()
    with pytest.raises(DeliveryError, match="already initialized"):
        sync_to_local_server_store(
            package.directory, store_root=store, initial_seed=True
        )
    assert (store / "current.json").read_bytes() == before


def test_initial_seed_activation_refuses_initialized_store_without_consuming_upload(
    tmp_path: Path,
) -> None:
    public = tmp_path / "public-market-data"
    _current(public, "tankan", "r1", "one")
    package = build_production_package(
        public_current_root=public, packages_root=tmp_path / "packages", source_max_dates={}
    )
    store = tmp_path / "server"
    sync_to_local_server_store(package.directory, store_root=store)
    upload = store / "incoming" / f"{package.package_id}.upload-seed"
    shutil.copytree(package.directory, upload)
    with pytest.raises(DeliveryError, match="already initialized"):
        activate_incoming_server_package(upload, store_root=store, initial_seed=True)
    assert upload.is_dir()


def test_configured_consumers_resolve_one_activated_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    public = tmp_path / "public-market-data"
    _current(public, "tankan", "r1", "one")
    artifact = _domestic_spread(
        tmp_path / "source" / "historical_spread_database.parquet",
        value=-38.0,
        updated_at="2026-08-23 08:00:00",
    )
    package = build_production_package(
        public_current_root=public,
        packages_root=tmp_path / "packages",
        source_max_dates={},
        delivery_artifacts={"domestic-spread": artifact},
    )
    store = tmp_path / "server"
    sync_to_local_server_store(package.directory, store_root=store)
    monkeypatch.setenv("PUBLIC_DATA_SERVER_STORE_ROOT", str(store))

    data = resolve_public_data_root(tmp_path / "legacy")
    spread = resolve_domestic_spread_path(tmp_path / "legacy")

    assert data == store / "releases" / package.package_id / "data"
    assert spread == data / "consumer-artifacts/domestic-spread/historical_spread_database.parquet"


def test_domestic_spread_formula_mismatch_is_rejected(tmp_path: Path) -> None:
    public = tmp_path / "public-market-data"
    _current(public, "tankan", "r1", "one")
    artifact = _domestic_spread(
        tmp_path / "source" / "historical_spread_database.parquet",
        value=-38.0,
        updated_at="2026-08-23 08:00:00",
    )
    table = pq.read_table(artifact).set_column(
        pq.read_table(artifact).schema.get_field_index("spread_value"),
        "spread_value",
        pa.array([999.0]),
    )
    pq.write_table(table, artifact)

    with pytest.raises(DeliveryError, match="formula validation"):
        build_production_package(
            public_current_root=public,
            packages_root=tmp_path / "packages",
            source_max_dates={},
            delivery_artifacts={"domestic-spread": artifact},
        )


def test_switch_failure_keeps_old_server_current(tmp_path: Path) -> None:
    public = tmp_path / "public-market-data"
    _current(public, "tankan", "r1", "one")
    first = build_production_package(
        public_current_root=public, packages_root=tmp_path / "packages", source_max_dates={}
    )
    store = tmp_path / "server"
    sync_to_local_server_store(first.directory, store_root=store)
    before = (store / "current.json").read_bytes()
    _current(public, "tankan", "r2", "two")
    second = build_production_package(
        public_current_root=public, packages_root=tmp_path / "packages", source_max_dates={}
    )

    def fail() -> None:
        raise OSError("injected")

    result = sync_to_local_server_store(second.directory, store_root=store, switch_hook=fail)
    assert result.status == "FAILED"
    assert result.atomic_switch == "FAIL"
    assert (store / "current.json").read_bytes() == before
    assert resolve_server_current(store).name == first.package_id


def test_sha_validation_failure_never_switches_server_current(tmp_path: Path) -> None:
    public = tmp_path / "public-market-data"
    _current(public, "tankan", "r1", "one")
    first = build_production_package(
        public_current_root=public, packages_root=tmp_path / "packages", source_max_dates={}
    )
    store = tmp_path / "server"
    sync_to_local_server_store(first.directory, store_root=store)
    before = (store / "current.json").read_bytes()
    _current(public, "tankan", "r2", "two")
    second = build_production_package(
        public_current_root=public, packages_root=tmp_path / "packages", source_max_dates={}
    )
    (second.directory / "data/public-market-data/tankan/releases/r2/data.txt").write_text(
        "tampered", encoding="utf-8"
    )
    with pytest.raises(DeliveryError, match="files differ"):
        sync_to_local_server_store(second.directory, store_root=store)
    assert (store / "current.json").read_bytes() == before
    assert resolve_server_current(store).name == first.package_id


def test_post_switch_validation_failure_rolls_back_old_pointer(tmp_path: Path) -> None:
    public = tmp_path / "public-market-data"
    _current(public, "tankan", "r1", "one")
    first = build_production_package(
        public_current_root=public, packages_root=tmp_path / "packages", source_max_dates={}
    )
    store = tmp_path / "server"
    sync_to_local_server_store(first.directory, store_root=store)
    _current(public, "tankan", "r2", "two")
    second = build_production_package(
        public_current_root=public, packages_root=tmp_path / "packages", source_max_dates={}
    )

    def fail(_data: Path) -> None:
        raise ValueError("unreadable")

    result = sync_to_local_server_store(
        second.directory, store_root=store, post_switch_validator=fail
    )
    assert result.status == "FAILED"
    assert result.formal_read_validation == "FAIL"
    assert resolve_server_current(store).name == first.package_id


def test_prewarm_partial_is_distinct_from_data_sync() -> None:
    calls: list[str] = []

    def fail() -> None:
        raise RuntimeError("cold")

    result = run_prewarm(
        [PrewarmTarget("spread", lambda: calls.append("spread")), PrewarmTarget("weather", fail)]
    )
    assert result.status is PrewarmStatus.PARTIAL
    assert result.targets == {"spread": "PASS", "weather": "FAIL:RuntimeError"}
    assert calls == ["spread"]
