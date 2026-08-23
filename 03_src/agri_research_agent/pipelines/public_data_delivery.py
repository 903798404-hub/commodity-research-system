"""Sealed Public Current packages and a locally testable server data store.

This module deliberately contains no SSH, Docker, or code-release operations.
The filesystem store models the server-side receive/validate/switch contract so
the data release flow can be proved locally before a transport is authorized.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.ipc as ipc
import pyarrow.parquet as pq

from agri_research_agent.shared.atomic_storage import atomic_write_json


PACKAGE_SCHEMA = "public-current-production-package/2"
SERVER_POINTER_SCHEMA = "public-current-server-pointer/2"
DOMESTIC_SPREAD_ARTIFACT = "domestic-spread"
DOMESTIC_SPREAD_FILENAME = "historical_spread_database.parquet"
DOMESTIC_SPREAD_REQUIRED_COLUMNS = frozenset({
    "date", "spread_group", "spread_name", "leg1_instrument", "leg1_month",
    "leg1_price", "leg2_instrument", "leg2_month", "leg2_price",
    "spread_value", "season", "calendar_offset", "status", "updated_at",
})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class DeliveryError(RuntimeError):
    """A fail-closed package or server-store validation error."""


class PrewarmStatus(StrEnum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class ProductionPackage:
    package_id: str
    directory: Path
    manifest: Mapping[str, Any]
    created: bool = False


@dataclass(frozen=True, slots=True)
class ServerSyncResult:
    status: str
    package_id: str
    manifest: str
    sha: str
    atomic_switch: str
    formal_read_validation: str
    current_directory: Path | None
    safe_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PrewarmTarget:
    name: str
    loader: Callable[[], object]


@dataclass(frozen=True, slots=True)
class PrewarmResult:
    status: PrewarmStatus
    targets: Mapping[str, str]


def build_production_package(
    *,
    public_current_root: str | Path,
    packages_root: str | Path,
    source_max_dates: Mapping[str, str],
    required_datasets: Sequence[str] | None = None,
    delivery_artifacts: Mapping[str, str | Path] | None = None,
) -> ProductionPackage:
    """Seal active Currents and consumer artifacts into one immutable package."""

    source = Path(public_current_root).resolve()
    if not source.is_dir():
        raise DeliveryError("Public Current root is missing")
    dataset_names = _dataset_names(source, required_datasets)
    identities = _current_identities(source, dataset_names)
    identity_sha = _json_sha256(identities)
    artifact_identities = _delivery_artifact_identities(delivery_artifacts or {})
    delivery_identity_sha = _json_sha256({
        "current_identity_sha256": identity_sha,
        "delivery_artifacts": {
            name: identity["business_sha256"]
            for name, identity in sorted(artifact_identities.items())
        },
    })
    package_id = f"public-current-{delivery_identity_sha[:24]}"
    root = Path(packages_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / package_id
    if destination.exists():
        package = validate_production_package(destination)
        if package.manifest["delivery_identity_sha256"] != delivery_identity_sha:
            raise DeliveryError("existing package identity differs from delivery aggregate")
        return package

    temporary = Path(tempfile.mkdtemp(prefix=f".{package_id}.", dir=root))
    try:
        data_root = temporary / "data" / "public-market-data"
        for dataset in dataset_names:
            _copy_active_dataset(source / dataset, data_root / dataset)
        for name, identity in artifact_identities.items():
            artifact_root = temporary / "data" / "consumer-artifacts" / name
            artifact_root.mkdir(parents=True)
            shutil.copy2(Path(identity["source_path"]), artifact_root / identity["filename"])
        records = _file_records(temporary / "data")
        sealed_artifacts = {
            name: {key: value for key, value in identity.items() if key != "source_path"}
            for name, identity in artifact_identities.items()
        }
        manifest: dict[str, Any] = {
            "schema_version": PACKAGE_SCHEMA,
            "package_id": package_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "current_identity_sha256": identity_sha,
            "current_identities": identities,
            "delivery_identity_sha256": delivery_identity_sha,
            "delivery_artifacts": sealed_artifacts,
            "source_max_dates": dict(sorted(source_max_dates.items())),
            "file_count": len(records),
            "bundle_sha256": _bundle_sha256(records),
            "files": records,
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        validate_production_package(temporary, require_directory_name=False)
        os.replace(temporary, destination)
    except Exception:
        _safe_remove_tree(temporary, root, ignore_errors=True)
        raise
    package = validate_production_package(destination)
    return ProductionPackage(package.package_id, package.directory, package.manifest, True)


def validate_production_package(
    directory: str | Path, *, require_directory_name: bool = True
) -> ProductionPackage:
    root = Path(directory).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise DeliveryError("production package manifest is missing or unsafe")
    manifest = _strict_json(manifest_path)
    required = {
        "schema_version", "package_id", "created_at", "current_identity_sha256",
        "current_identities", "delivery_identity_sha256", "delivery_artifacts",
        "source_max_dates", "file_count", "bundle_sha256", "files",
    }
    if set(manifest) != required or manifest["schema_version"] != PACKAGE_SCHEMA:
        raise DeliveryError("production package manifest schema is invalid")
    identities = manifest["current_identities"]
    if not isinstance(identities, dict) or _json_sha256(identities) != manifest["current_identity_sha256"]:
        raise DeliveryError("production package Current identity is invalid")
    artifacts = manifest["delivery_artifacts"]
    if not isinstance(artifacts, dict):
        raise DeliveryError("production package delivery artifacts are invalid")
    expected_delivery_sha = _json_sha256({
        "current_identity_sha256": manifest["current_identity_sha256"],
        "delivery_artifacts": {
            name: identity.get("business_sha256")
            for name, identity in sorted(artifacts.items())
            if isinstance(identity, dict)
        },
    })
    if manifest["delivery_identity_sha256"] != expected_delivery_sha:
        raise DeliveryError("production package delivery identity is invalid")
    expected_id = f"public-current-{str(manifest['delivery_identity_sha256'])[:24]}"
    if manifest["package_id"] != expected_id or (
        require_directory_name and root.name != expected_id
    ):
        raise DeliveryError("production package id is invalid")
    declared = manifest["files"]
    if not isinstance(declared, list):
        raise DeliveryError("production package file list is invalid")
    actual = _file_records(root / "data")
    if declared != actual or manifest["file_count"] != len(actual):
        raise DeliveryError("production package files differ from manifest")
    if manifest["bundle_sha256"] != _bundle_sha256(actual):
        raise DeliveryError("production package SHA bundle is invalid")
    _validate_packaged_currents(root / "data" / "public-market-data", identities)
    _validate_packaged_delivery_artifacts(root / "data", artifacts)
    return ProductionPackage(str(manifest["package_id"]), root, manifest)


def sync_to_local_server_store(
    package_directory: str | Path,
    *,
    store_root: str | Path,
    pre_switch_validator: Callable[[Path], None] | None = None,
    post_switch_validator: Callable[[Path], None] | None = None,
    switch_hook: Callable[[], None] | None = None,
) -> ServerSyncResult:
    """Stage, validate and atomically switch one package-store Current pointer."""

    package = validate_production_package(package_directory)
    configured_root = Path(store_root)
    if configured_root.is_symlink():
        raise DeliveryError("server store root must not be a symbolic link")
    root = configured_root.resolve()
    incoming = root / "incoming"
    releases = root / "releases"
    incoming.mkdir(parents=True, exist_ok=True)
    releases.mkdir(parents=True, exist_ok=True)
    _reject_symlink(incoming, "server incoming root")
    _reject_symlink(releases, "server releases root")
    pointer_path = root / "current.json"
    old_pointer = _strict_json(pointer_path) if pointer_path.is_file() else None
    if old_pointer and old_pointer.get("package_id") == package.package_id:
        current = resolve_server_current(root)
        return ServerSyncResult(
            "NO_CHANGE", package.package_id, "PASS", "PASS", "N/A", "PASS", current
        )

    staged = incoming / package.package_id
    if staged.exists():
        _safe_remove_tree(staged, incoming)
    shutil.copytree(package.directory, staged, copy_function=shutil.copy2)
    staged_package = validate_production_package(staged)
    if pre_switch_validator is not None:
        pre_switch_validator(staged_package.directory / "data")
    formal = releases / package.package_id
    if formal.exists():
        existing = validate_production_package(formal)
        if existing.manifest["bundle_sha256"] != package.manifest["bundle_sha256"]:
            raise DeliveryError("server release id collision")
        _safe_remove_tree(staged, incoming)
    else:
        os.replace(staged, formal)
    new_pointer = {
        "schema_version": SERVER_POINTER_SCHEMA,
        "package_id": package.package_id,
        "current_identity_sha256": package.manifest["current_identity_sha256"],
        "delivery_identity_sha256": package.manifest["delivery_identity_sha256"],
        "bundle_sha256": package.manifest["bundle_sha256"],
    }
    try:
        if switch_hook is not None:
            switch_hook()
        atomic_write_json(pointer_path, new_pointer)
    except Exception as exc:
        return ServerSyncResult(
            "FAILED", package.package_id, "PASS", "PASS", "FAIL", "N/A", None,
            f"atomic Current switch failed: {type(exc).__name__}",
        )
    try:
        current = resolve_server_current(root)
        if post_switch_validator is not None:
            post_switch_validator(current / "data")
    except Exception as exc:
        if old_pointer is None:
            pointer_path.unlink(missing_ok=True)
        else:
            atomic_write_json(pointer_path, old_pointer)
        return ServerSyncResult(
            "FAILED", package.package_id, "PASS", "PASS", "PASS", "FAIL",
            resolve_server_current(root) if old_pointer is not None else None,
            f"formal read validation failed: {type(exc).__name__}",
        )
    return ServerSyncResult(
        "SYNCED", package.package_id, "PASS", "PASS", "PASS", "PASS", current
    )


def activate_incoming_server_package(
    incoming_directory: str | Path,
    *,
    store_root: str | Path,
    pre_switch_validator: Callable[[Path], None] | None = None,
    post_switch_validator: Callable[[Path], None] | None = None,
    switch_hook: Callable[[], None] | None = None,
) -> ServerSyncResult:
    """Validate one uploaded directory and atomically activate it in-place.

    Unlike the local simulation helper, this entrypoint never copies a package:
    transport owns ``incoming/`` and activation renames the validated directory
    into the immutable ``releases/`` store on the same filesystem.
    """

    configured_root = Path(store_root)
    if configured_root.is_symlink():
        raise DeliveryError("server store root must not be a symbolic link")
    root = configured_root.resolve()
    incoming_root = root / "incoming"
    releases = root / "releases"
    incoming_root.mkdir(parents=True, exist_ok=True)
    releases.mkdir(parents=True, exist_ok=True)
    _reject_symlink(incoming_root, "server incoming root")
    _reject_symlink(releases, "server releases root")
    upload_path = Path(incoming_directory)
    if upload_path.is_symlink():
        raise DeliveryError("uploaded package is outside server incoming root")
    uploaded = upload_path.resolve(strict=True)
    if uploaded.parent != incoming_root.resolve():
        raise DeliveryError("uploaded package is outside server incoming root")
    _safe_id(uploaded.name, "uploaded package directory")
    package = validate_production_package(uploaded, require_directory_name=False)
    if pre_switch_validator is not None:
        pre_switch_validator(package.directory / "data")

    pointer_path = root / "current.json"
    old_pointer = _strict_json(pointer_path) if pointer_path.is_file() else None
    if old_pointer and old_pointer.get("package_id") == package.package_id:
        current = resolve_server_current(root)
        _safe_remove_tree(uploaded, incoming_root)
        return ServerSyncResult(
            "NO_CHANGE", package.package_id, "PASS", "PASS", "N/A", "PASS", current
        )

    formal = releases / package.package_id
    if formal.exists():
        existing = validate_production_package(formal)
        if existing.manifest["bundle_sha256"] != package.manifest["bundle_sha256"]:
            raise DeliveryError("server release id collision")
        _safe_remove_tree(uploaded, incoming_root)
    else:
        os.replace(uploaded, formal)
    new_pointer = {
        "schema_version": SERVER_POINTER_SCHEMA,
        "package_id": package.package_id,
        "current_identity_sha256": package.manifest["current_identity_sha256"],
        "delivery_identity_sha256": package.manifest["delivery_identity_sha256"],
        "bundle_sha256": package.manifest["bundle_sha256"],
    }
    try:
        if switch_hook is not None:
            switch_hook()
        atomic_write_json(pointer_path, new_pointer)
    except Exception as exc:
        return ServerSyncResult(
            "FAILED", package.package_id, "PASS", "PASS", "FAIL", "N/A", None,
            f"atomic Current switch failed: {type(exc).__name__}",
        )
    try:
        current = resolve_server_current(root)
        if post_switch_validator is not None:
            post_switch_validator(current / "data")
    except Exception as exc:
        if old_pointer is None:
            pointer_path.unlink(missing_ok=True)
        else:
            atomic_write_json(pointer_path, old_pointer)
        return ServerSyncResult(
            "FAILED", package.package_id, "PASS", "PASS", "PASS", "FAIL",
            resolve_server_current(root) if old_pointer is not None else None,
            f"formal read validation failed: {type(exc).__name__}",
        )
    return ServerSyncResult(
        "SYNCED", package.package_id, "PASS", "PASS", "PASS", "PASS", current
    )


def resolve_server_current(store_root: str | Path) -> Path:
    root = Path(store_root).resolve()
    pointer = _strict_json(root / "current.json")
    required = {
        "schema_version", "package_id", "current_identity_sha256",
        "delivery_identity_sha256", "bundle_sha256",
    }
    if set(pointer) != required or pointer["schema_version"] != SERVER_POINTER_SCHEMA:
        raise DeliveryError("server Current pointer is invalid")
    package_id = _safe_id(str(pointer["package_id"]), "server package_id")
    release = root / "releases" / package_id
    package = validate_production_package(release)
    if (
        pointer["current_identity_sha256"] != package.manifest["current_identity_sha256"]
        or pointer["delivery_identity_sha256"] != package.manifest["delivery_identity_sha256"]
        or pointer["bundle_sha256"] != package.manifest["bundle_sha256"]
    ):
        raise DeliveryError("server Current pointer identity mismatch")
    return package.directory


def run_prewarm(targets: Sequence[PrewarmTarget]) -> PrewarmResult:
    if not targets:
        return PrewarmResult(PrewarmStatus.SKIPPED, {})
    results: dict[str, str] = {}
    passed = 0
    for target in targets:
        try:
            target.loader()
            results[target.name] = "PASS"
            passed += 1
        except Exception as exc:
            results[target.name] = f"FAIL:{type(exc).__name__}"
    status = (
        PrewarmStatus.PASS if passed == len(targets)
        else PrewarmStatus.FAIL if passed == 0
        else PrewarmStatus.PARTIAL
    )
    return PrewarmResult(status, results)


def _dataset_names(source: Path, required: Sequence[str] | None) -> tuple[str, ...]:
    available = tuple(sorted(path.name for path in source.iterdir() if (path / "current.json").is_file()))
    names = tuple(
        _safe_id(str(name), "dataset")
        for name in (dict.fromkeys(required) if required is not None else available)
    )
    if not names:
        raise DeliveryError("no Public Current datasets were found")
    missing = [name for name in names if not (source / name / "current.json").is_file()]
    if missing:
        raise DeliveryError(f"required Public Current dataset is missing: {','.join(missing)}")
    return names


def _current_identities(source: Path, datasets: Sequence[str]) -> dict[str, Any]:
    identities: dict[str, Any] = {}
    for dataset in datasets:
        _reject_symlink(source / dataset, "Public Current dataset")
        pointer = _current_pointer(source / dataset)
        identities[dataset] = {
            "release_id": pointer["release_id"],
            "manifest_sha256": pointer["manifest_sha256"],
        }
        seed_pointer = source / dataset / "historical-seed.json"
        if seed_pointer.is_file():
            identities[dataset]["historical_seed"] = _historical_seed_identity(
                source / dataset
            )
    return identities


def _copy_active_dataset(source: Path, destination: Path) -> None:
    _reject_symlink(source, "Public Current dataset")
    pointer = _current_pointer(source)
    destination.mkdir(parents=True)
    shutil.copy2(source / "current.json", destination / "current.json")
    release_id = _safe_id(str(pointer["release_id"]), "release_id")
    _assert_no_symlinks(source / "releases" / release_id)
    shutil.copytree(
        source / "releases" / release_id,
        destination / "releases" / release_id,
        copy_function=shutil.copy2,
    )
    seed_pointer = source / "historical-seed.json"
    if seed_pointer.is_file():
        seed = _historical_seed_identity(source)
        seed_id = _safe_id(str(seed.get("seed_id", "")), "historical seed_id")
        _assert_no_symlinks(source / "historical-seeds" / seed_id)
        shutil.copy2(seed_pointer, destination / "historical-seed.json")
        shutil.copytree(
            source / "historical-seeds" / seed_id,
            destination / "historical-seeds" / seed_id,
            copy_function=shutil.copy2,
        )


def _current_pointer(dataset_root: Path) -> dict[str, Any]:
    pointer = _strict_json(dataset_root / "current.json")
    required = {"schema_version", "release_id", "manifest_sha256"}
    if set(pointer) != required or pointer["schema_version"] != 1:
        raise DeliveryError(f"Public Current pointer is invalid: {dataset_root.name}")
    release_id = _safe_id(str(pointer["release_id"]), "release_id")
    release = dataset_root / "releases" / release_id
    _reject_symlink(release, "Public Current release")
    manifest = release / "manifest.json"
    if not release.is_dir() or _sha256_file(manifest) != pointer["manifest_sha256"]:
        raise DeliveryError(f"Public Current manifest identity mismatch: {dataset_root.name}")
    payload = _strict_json(manifest)
    if str(payload.get("release_id")) != str(pointer["release_id"]):
        raise DeliveryError(f"Public Current release identity mismatch: {dataset_root.name}")
    if payload.get("quality_status") != "PASS":
        raise DeliveryError(f"Public Current quality status is not PASS: {dataset_root.name}")
    _validate_declared_files(release, payload)
    return pointer


def _validate_declared_files(directory: Path, manifest: Mapping[str, Any]) -> None:
    declared = manifest.get("files")
    if not isinstance(declared, dict) or not declared:
        raise DeliveryError(f"Public Current file manifest is invalid: {directory.parent.parent.name}")
    children = list(directory.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in children):
        raise DeliveryError("Public Current release contains an unsafe entry")
    actual_names = {path.name for path in children if path.name != "manifest.json"}
    if actual_names != set(declared):
        raise DeliveryError("Public Current files differ from the release manifest")
    for filename, identity in declared.items():
        safe = PurePosixPath(str(filename))
        if safe.is_absolute() or len(safe.parts) != 1 or ".." in safe.parts:
            raise DeliveryError("Public Current manifest contains an unsafe path")
        if not isinstance(identity, dict) or "sha256" not in identity:
            raise DeliveryError("Public Current file identity is invalid")
        path = directory / str(filename)
        if _sha256_file(path) != identity["sha256"]:
            raise DeliveryError(f"Public Current data SHA mismatch: {filename}")
        expected_size = identity.get("size_bytes", identity.get("size"))
        if expected_size is not None and path.stat().st_size != expected_size:
            raise DeliveryError(f"Public Current data size mismatch: {filename}")


def _historical_seed_identity(dataset_root: Path) -> dict[str, Any]:
    pointer = _strict_json(dataset_root / "historical-seed.json")
    required = {"schema_version", "seed_id", "manifest_sha256"}
    if set(pointer) != required or pointer["schema_version"] != 1:
        raise DeliveryError("historical seed pointer is invalid")
    seed_id = _safe_id(str(pointer["seed_id"]), "historical seed_id")
    directory = dataset_root / "historical-seeds" / seed_id
    _reject_symlink(directory, "historical seed release")
    manifest_path = directory / "manifest.json"
    if not directory.is_dir() or _sha256_file(manifest_path) != pointer["manifest_sha256"]:
        raise DeliveryError("historical seed manifest identity mismatch")
    manifest = _strict_json(manifest_path)
    if manifest.get("seed_id") != pointer["seed_id"] or manifest.get("quality_status") != "PASS":
        raise DeliveryError("historical seed contract is invalid")
    _validate_declared_files(directory, manifest)
    return pointer


def _validate_packaged_currents(root: Path, identities: Mapping[str, Any]) -> None:
    if set(path.name for path in root.iterdir() if path.is_dir()) != set(identities):
        raise DeliveryError("packaged dataset inventory differs from Current identity")
    for dataset, expected in identities.items():
        pointer = _current_pointer(root / dataset)
        actual = {"release_id": pointer["release_id"], "manifest_sha256": pointer["manifest_sha256"]}
        if "historical_seed" in expected:
            actual["historical_seed"] = _historical_seed_identity(root / dataset)
        if actual != expected:
            raise DeliveryError(f"packaged Current identity mismatch: {dataset}")


def _delivery_artifact_identities(
    artifacts: Mapping[str, str | Path],
) -> dict[str, dict[str, Any]]:
    identities: dict[str, dict[str, Any]] = {}
    for raw_name, raw_path in sorted(artifacts.items()):
        name = _safe_id(str(raw_name), "delivery artifact")
        if name != DOMESTIC_SPREAD_ARTIFACT:
            raise DeliveryError(f"unsupported delivery artifact: {name}")
        path = Path(raw_path).resolve()
        details = _domestic_spread_identity(path)
        identities[name] = {
            "source_path": str(path),
            "filename": DOMESTIC_SPREAD_FILENAME,
            "package_path": (
                f"consumer-artifacts/{name}/{DOMESTIC_SPREAD_FILENAME}"
            ),
            **details,
        }
    return identities


def _domestic_spread_identity(path: Path) -> dict[str, Any]:
    if path.name != DOMESTIC_SPREAD_FILENAME or not path.is_file() or path.is_symlink():
        raise DeliveryError("Domestic Spread formal Parquet artifact is missing or unsafe")
    try:
        table = pq.read_table(path)
    except Exception as exc:
        raise DeliveryError("Domestic Spread Parquet is unreadable") from exc
    missing = DOMESTIC_SPREAD_REQUIRED_COLUMNS - set(table.column_names)
    if missing:
        raise DeliveryError(
            f"Domestic Spread Parquet schema is missing: {','.join(sorted(missing))}"
        )
    if table.num_rows == 0:
        raise DeliveryError("Domestic Spread Parquet contains no rows")
    successful = table.filter(pc.equal(table["status"], "success"))
    if successful.num_rows == 0:
        raise DeliveryError("Domestic Spread Parquet contains no successful rows")
    for leg1, leg2, spread in zip(
        successful["leg1_price"].to_pylist(),
        successful["leg2_price"].to_pylist(),
        successful["spread_value"].to_pylist(),
        strict=True,
    ):
        try:
            values = (float(leg1), float(leg2), float(spread))
        except (TypeError, ValueError) as exc:
            raise DeliveryError("Domestic Spread formula validation failed") from exc
        if not all(math.isfinite(value) for value in values) or not math.isclose(
            values[0] - values[1], values[2], rel_tol=1e-9, abs_tol=1e-8
        ):
            raise DeliveryError("Domestic Spread formula validation failed")
    latest = pc.max(successful["date"]).as_py()
    latest_text = (
        latest.date().isoformat() if isinstance(latest, datetime) else latest.isoformat()
    )
    business_columns = sorted(
        column for column in table.column_names if column != "updated_at"
    )
    business = table.select(business_columns).replace_schema_metadata(None)
    business = business.sort_by(
        [(column, "ascending") for column in business.column_names]
    )
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, business.schema) as writer:
        writer.write_table(business.combine_chunks())
    business_sha = hashlib.sha256(sink.getvalue().to_pybytes()).hexdigest()
    return {
        "size_bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
        "business_sha256": business_sha,
        "latest_business_date": latest_text,
        "row_count": table.num_rows,
    }


def _validate_packaged_delivery_artifacts(
    data_root: Path, artifacts: Mapping[str, Any]
) -> None:
    artifact_root = data_root / "consumer-artifacts"
    if not artifacts:
        if artifact_root.exists():
            raise DeliveryError("undeclared delivery artifact directory exists")
        return
    for name, expected in artifacts.items():
        name = _safe_id(str(name), "delivery artifact")
        if not isinstance(expected, dict):
            raise DeliveryError("delivery artifact identity is invalid")
        filename = _safe_id(str(expected.get("filename", "")), "delivery filename")
        relative = f"consumer-artifacts/{name}/{filename}"
        if expected.get("package_path") != relative:
            raise DeliveryError("delivery artifact package path is invalid")
        actual = _domestic_spread_identity(data_root / relative)
        for key in ("size_bytes", "sha256", "business_sha256", "latest_business_date", "row_count"):
            if actual[key] != expected.get(key):
                raise DeliveryError(f"packaged delivery artifact identity mismatch: {name}")


def _file_records(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        raise DeliveryError("production package data directory is missing")
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise DeliveryError("production package contains a symbolic link")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        safe = PurePosixPath(relative)
        if safe.is_absolute() or ".." in safe.parts:
            raise DeliveryError("production package contains an unsafe path")
        records.append({"path": relative, "size_bytes": path.stat().st_size, "sha256": _sha256_file(path)})
    return records


def _bundle_sha256(records: Sequence[Mapping[str, Any]]) -> str:
    return _json_sha256(list(records))


def _json_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise DeliveryError(f"required regular file is missing: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _strict_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeliveryError(f"invalid JSON file: {path.name}") from exc
    if not isinstance(value, dict):
        raise DeliveryError(f"JSON object required: {path.name}")
    return value


def _safe_id(value: str, label: str) -> str:
    if value in {".", ".."} or _SAFE_ID.fullmatch(value) is None:
        raise DeliveryError(f"unsafe {label}")
    return value


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise DeliveryError(f"{label} must not be a symbolic link")


def _assert_no_symlinks(directory: Path) -> None:
    if not directory.is_dir() or directory.is_symlink():
        raise DeliveryError("source release directory is missing or unsafe")
    if any(path.is_symlink() for path in directory.rglob("*")):
        raise DeliveryError("source release contains a symbolic link")


def _safe_remove_tree(path: Path, expected_parent: Path, *, ignore_errors: bool = False) -> None:
    parent = expected_parent.resolve()
    target = path.resolve(strict=False)
    if target.parent != parent or path.is_symlink():
        raise DeliveryError("refusing to remove an unsafe staging directory")
    shutil.rmtree(target, ignore_errors=ignore_errors)


__all__ = [
    "DeliveryError", "PrewarmResult", "PrewarmStatus", "PrewarmTarget",
    "ProductionPackage", "ServerSyncResult", "build_production_package",
    "activate_incoming_server_package", "resolve_server_current", "run_prewarm",
    "sync_to_local_server_store", "validate_production_package",
]
