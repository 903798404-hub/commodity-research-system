#!/usr/bin/env python3
"""Package, validate, promote and roll back immutable USDA Runtime data releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping


MANIFEST_SCHEMA_VERSION = 1
DATA_SCHEMA_VERSION = 1
MINIMUM_APP_CONTRACT_VERSION = 1
RELEASE_ID_RE = re.compile(r"^usda-\d{4}-(?:0[1-9]|1[0-2])-[0-9a-f]{16}$")
MONTH_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
EXTRACTION_METHOD_RE = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
API_SOURCE_TYPE = "usda_psd_api"
SEED_SOURCE_TYPE = "production_image_migration_seed"
REQUIRED_ROOT_FILES = frozenset({"index.json", "report_version.json", "presentation_changes.json"})


class RuntimeDataError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def strict_json(path: Path) -> Any:
    def reject(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject)


def _safe_relative(value: str) -> PurePosixPath:
    if "\\" in value:
        raise RuntimeDataError(f"unsafe manifest path: {value}")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeDataError(f"unsafe manifest path: {value}")
    return path


def validate_release_id(value: str) -> str:
    if not RELEASE_ID_RE.fullmatch(value):
        raise RuntimeDataError(f"invalid release_id: {value}")
    return value


def _iter_regular_files(root: Path) -> Iterable[tuple[str, Path]]:
    seen_inodes: set[tuple[int, int]] = set()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise RuntimeDataError(f"symlink is forbidden: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise RuntimeDataError(f"non-regular file is forbidden: {path}")
        stat = path.stat()
        inode = (stat.st_dev, stat.st_ino)
        if stat.st_nlink > 1 or inode in seen_inodes:
            raise RuntimeDataError(f"hardlink is forbidden: {path}")
        seen_inodes.add(inode)
        yield path.relative_to(root).as_posix(), path


def file_records(data_root: Path) -> list[dict[str, Any]]:
    return [
        {"path": relative, "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for relative, path in _iter_regular_files(data_root)
    ]


def bundle_sha256(records: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(records, key=lambda value: str(value["path"])):
        line = f'{item["path"]}\0{item["size_bytes"]}\0{item["sha256"]}\n'
        digest.update(line.encode("utf-8"))
    return digest.hexdigest()


RELEASE_DIRECTORY_MODE = 0o755
RELEASE_FILE_MODE = 0o644


def validate_release_permissions(release: Path) -> None:
    paths = [release, *sorted(release.rglob("*"), key=lambda item: item.relative_to(release).as_posix())]
    for path in paths:
        if path.is_symlink():
            raise RuntimeDataError(f"symlink is forbidden: {path}")
        stat_result = path.stat()
        mode = stat.S_IMODE(stat_result.st_mode)
        if path.is_dir():
            if os.name != "nt" and mode != RELEASE_DIRECTORY_MODE:
                raise RuntimeDataError(f"release directory mode must be 0755: {path}: {mode:04o}")
        elif path.is_file():
            if stat_result.st_nlink > 1:
                raise RuntimeDataError(f"hardlink is forbidden: {path}")
            if os.name != "nt" and mode != RELEASE_FILE_MODE:
                raise RuntimeDataError(f"release file mode must be 0644: {path}: {mode:04o}")
        else:
            raise RuntimeDataError(f"non-regular release entry is forbidden: {path}")


def prepare_formal_release_permissions(release: Path) -> dict[str, Any]:
    before_hashes = {relative: sha256_file(path) for relative, path in _iter_regular_files(release)}
    before_bundle = bundle_sha256(file_records(release / "data"))
    directories = [release]
    files: list[Path] = []
    for path in sorted(release.rglob("*"), key=lambda item: item.relative_to(release).as_posix()):
        if path.is_symlink():
            raise RuntimeDataError(f"symlink is forbidden: {path}")
        if path.is_dir():
            directories.append(path)
        elif path.is_file():
            if path.stat().st_nlink > 1:
                raise RuntimeDataError(f"hardlink is forbidden: {path}")
            files.append(path)
        else:
            raise RuntimeDataError(f"non-regular release entry is forbidden: {path}")
    try:
        for path in directories:
            path.chmod(RELEASE_DIRECTORY_MODE)
        for path in files:
            path.chmod(RELEASE_FILE_MODE)
    except OSError as exc:
        raise RuntimeDataError(f"failed to normalize release permissions: {exc}") from exc
    validate_release_permissions(release)
    after_hashes = {relative: sha256_file(path) for relative, path in _iter_regular_files(release)}
    after_bundle = bundle_sha256(file_records(release / "data"))
    if after_hashes != before_hashes:
        raise RuntimeDataError("release permission normalization changed file content")
    if after_bundle != before_bundle:
        raise RuntimeDataError("release permission normalization changed bundle identity")
    return {
        "directory_mode": "0755",
        "file_mode": "0644",
        "file_sha256_unchanged": True,
        "bundle_sha256": after_bundle,
    }


def _copy_file(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise RuntimeDataError(f"source must be a regular file: {source}")
    if source.stat().st_nlink > 1:
        raise RuntimeDataError(f"source hardlink is forbidden: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _validate_finite(value: Any, label: str = "JSON") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise RuntimeDataError(f"{label} contains NaN or Infinity")
    if isinstance(value, dict):
        for child in value.values():
            _validate_finite(child, label)
    elif isinstance(value, list):
        for child in value:
            _validate_finite(child, label)


def validate_source_identity(source_type: str, identity: Any) -> dict[str, Any]:
    if not isinstance(identity, dict):
        raise RuntimeDataError("source_batch_identity must be an object")
    if source_type == API_SOURCE_TYPE:
        if set(identity) != {"fetch_run_id", "source_manifest_sha256"}:
            raise RuntimeDataError("USDA API source identity is invalid or mixed with another source type")
        if not identity["fetch_run_id"] or not isinstance(identity["fetch_run_id"], str):
            raise RuntimeDataError("USDA API fetch_run_id is missing")
        if not SHA256_RE.fullmatch(str(identity["source_manifest_sha256"])):
            raise RuntimeDataError("USDA API source manifest SHA-256 is invalid")
    elif source_type == SEED_SOURCE_TYPE:
        required = {"source_image_id", "source_oci_revision", "source_git_sha", "source_git_tree", "extraction_identity"}
        if set(identity) != required:
            raise RuntimeDataError("production image seed source identity is incomplete or mixed")
        if not IMAGE_ID_RE.fullmatch(str(identity["source_image_id"])):
            raise RuntimeDataError("source_image_id must be a complete sha256 image digest")
        for field in ("source_oci_revision", "source_git_sha", "source_git_tree"):
            if not SHA1_RE.fullmatch(str(identity[field])):
                raise RuntimeDataError(f"{field} must be a complete lowercase Git SHA")
        extraction = identity["extraction_identity"]
        extraction_keys = {"extraction_method", "extracted_data_tree_sha256", "extracted_file_count"}
        if not isinstance(extraction, dict) or set(extraction) != extraction_keys:
            raise RuntimeDataError("production image seed extraction identity is incomplete")
        if not EXTRACTION_METHOD_RE.fullmatch(str(extraction["extraction_method"])):
            raise RuntimeDataError("extraction_method is invalid")
        if not SHA256_RE.fullmatch(str(extraction["extracted_data_tree_sha256"])):
            raise RuntimeDataError("extracted_data_tree_sha256 is invalid")
        if type(extraction["extracted_file_count"]) is not int or extraction["extracted_file_count"] <= 0:
            raise RuntimeDataError("extracted_file_count must be a positive integer")
    else:
        raise RuntimeDataError(f"unknown source_type: {source_type}")
    return identity


def extracted_data_identity(source_data: Path) -> dict[str, Any]:
    records = file_records(source_data)
    return {
        "extracted_data_tree_sha256": bundle_sha256(records),
        "extracted_file_count": len(records),
    }


def build_runtime_release(
    *,
    source_data: Path,
    output_parent: Path,
    builder_git_sha: str,
    builder_tree_sha: str,
    fetch_run_id: str,
    source_manifest_sha256: str,
    created_at: str | None = None,
) -> Path:
    source_identity = validate_source_identity(API_SOURCE_TYPE, {
        "fetch_run_id": fetch_run_id,
        "source_manifest_sha256": source_manifest_sha256,
    })
    return _build_runtime_release(
        source_data=source_data,
        output_parent=output_parent,
        builder_git_sha=builder_git_sha,
        builder_tree_sha=builder_tree_sha,
        source_type=API_SOURCE_TYPE,
        source_batch_identity=source_identity,
        created_at=created_at,
    )


def build_runtime_seed_release(
    *,
    source_data: Path,
    output_parent: Path,
    builder_git_sha: str,
    builder_tree_sha: str,
    source_batch_identity: Mapping[str, Any],
    created_at: str | None = None,
) -> Path:
    identity = validate_source_identity(SEED_SOURCE_TYPE, dict(source_batch_identity))
    actual = extracted_data_identity(source_data)
    declared = identity["extraction_identity"]
    if declared["extracted_data_tree_sha256"] != actual["extracted_data_tree_sha256"] or declared["extracted_file_count"] != actual["extracted_file_count"]:
        raise RuntimeDataError("production image seed extraction identity does not match source data")
    return _build_runtime_release(
        source_data=source_data,
        output_parent=output_parent,
        builder_git_sha=builder_git_sha,
        builder_tree_sha=builder_tree_sha,
        source_type=SEED_SOURCE_TYPE,
        source_batch_identity=identity,
        created_at=created_at,
    )


def _build_runtime_release(
    *,
    source_data: Path,
    output_parent: Path,
    builder_git_sha: str,
    builder_tree_sha: str,
    source_type: str,
    source_batch_identity: Mapping[str, Any],
    created_at: str | None = None,
) -> Path:
    if not SHA1_RE.fullmatch(builder_git_sha) or not SHA1_RE.fullmatch(builder_tree_sha):
        raise RuntimeDataError("builder Git commit and tree must be complete lowercase SHA-1 values")
    validate_source_identity(source_type, dict(source_batch_identity))
    output_parent.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_ROOT_FILES:
        if not (source_data / name).is_file():
            raise RuntimeDataError(f"required data file is missing: {name}")
    report = strict_json(source_data / "report_version.json")
    current = str(report.get("currentReportMonth", ""))
    previous = str(report.get("previousReportMonth", ""))
    if not MONTH_RE.fullmatch(current) or not MONTH_RE.fullmatch(previous) or current == previous:
        raise RuntimeDataError("report_version months are invalid")
    index = strict_json(source_data / "index.json")
    matrix_paths = sorted({str(item.get("file", "")) for item in index.get("matrices", [])})
    if not matrix_paths or any(not value.startswith("matrix/") for value in matrix_paths):
        raise RuntimeDataError("index matrix references are invalid")
    previous_index = source_data / "snapshots" / "usda_psd" / previous / "index.json"
    if not previous_index.is_file():
        raise RuntimeDataError(f"previous snapshot index is missing: {previous}")
    previous_catalog = strict_json(previous_index)
    previous_paths = sorted({str(item.get("file", "")) for item in previous_catalog.get("matrices", [])})
    if not previous_paths:
        raise RuntimeDataError("previous snapshot contains no matrices")
    source_records = file_records(source_data)
    source_paths = {str(item["path"]) for item in source_records}
    snapshot_months = sorted({
        parts[2]
        for relative in source_paths
        for parts in [PurePosixPath(relative).parts]
        if len(parts) >= 4 and parts[:2] == ("snapshots", "usda_psd") and MONTH_RE.fullmatch(parts[2])
    })
    temporary = Path(tempfile.mkdtemp(prefix=".usda-runtime-package-", dir=output_parent))
    try:
        data = temporary / "data"
        for item in source_records:
            safe = _safe_relative(str(item["path"]))
            _copy_file(source_data / Path(*safe.parts), data / Path(*safe.parts))
        records = file_records(data)
        if file_records(source_data) != source_records or records != source_records:
            raise RuntimeDataError("source public/data changed during packaging or was not copied exactly")
        identity = bundle_sha256(records)
        release_id = f"usda-{current}-{identity[:16]}"
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "release_id": release_id,
            "report_month": current,
            "previous_report_month": previous,
            "created_at": created_at or utc_now(),
            "builder_git_sha": builder_git_sha,
            "builder_tree_sha": builder_tree_sha,
            "data_schema_version": DATA_SCHEMA_VERSION,
            "minimum_app_contract_version": MINIMUM_APP_CONTRACT_VERSION,
            "source_type": source_type,
            "source_batch_identity": source_batch_identity,
            "file_count": len(records),
            "matrix_count": len(matrix_paths),
            "snapshot_months": snapshot_months,
            "comparison_current": current,
            "comparison_previous": previous,
            "bundle_sha256": identity,
            "files": records,
        }
        (temporary / "release_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        validate_runtime_release(
            temporary,
            app_contract_version=MINIMUM_APP_CONTRACT_VERSION,
            supported_data_schema_version=DATA_SCHEMA_VERSION,
            require_directory_identity=False,
        )
        target = output_parent / release_id
        if target.exists():
            raise RuntimeDataError(f"release already exists: {release_id}")
        temporary.rename(target)
        return target
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def validate_runtime_release(
    release: Path,
    *,
    app_contract_version: int,
    supported_data_schema_version: int,
    require_directory_identity: bool = True,
) -> dict[str, Any]:
    if release.is_symlink() or not release.is_dir():
        raise RuntimeDataError("release must be a real directory")
    manifest_path = release / "release_manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RuntimeDataError("release_manifest.json is missing or unsafe")
    manifest = strict_json(manifest_path)
    required = {
        "schema_version", "release_id", "report_month", "previous_report_month", "created_at",
        "builder_git_sha", "builder_tree_sha", "data_schema_version", "minimum_app_contract_version",
        "source_type", "source_batch_identity", "file_count", "matrix_count", "snapshot_months",
        "comparison_current", "comparison_previous", "bundle_sha256", "files",
    }
    if set(manifest) != required or manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise RuntimeDataError("release manifest schema is invalid")
    validate_source_identity(str(manifest["source_type"]), manifest["source_batch_identity"])
    release_id = validate_release_id(str(manifest["release_id"]))
    if require_directory_identity and release.name != release_id:
        raise RuntimeDataError("release directory does not match release_id")
    if manifest["data_schema_version"] != supported_data_schema_version:
        raise RuntimeDataError("bundle data schema is not supported by the application")
    if int(manifest["minimum_app_contract_version"]) > app_contract_version:
        raise RuntimeDataError("bundle requires a newer app contract")
    if not MONTH_RE.fullmatch(str(manifest["report_month"])) or not MONTH_RE.fullmatch(str(manifest["previous_report_month"])):
        raise RuntimeDataError("manifest report months are invalid")
    if manifest["comparison_current"] != manifest["report_month"] or manifest["comparison_previous"] != manifest["previous_report_month"]:
        raise RuntimeDataError("comparison identity does not match report months")
    if not isinstance(manifest["snapshot_months"], list) or manifest["snapshot_months"] != sorted(set(manifest["snapshot_months"])):
        raise RuntimeDataError("snapshot_months must be a sorted unique list")
    if manifest["previous_report_month"] not in manifest["snapshot_months"]:
        raise RuntimeDataError("snapshot closure must contain the previous report month")
    data = release / "data"
    actual = file_records(data)
    declared = manifest["files"]
    if not isinstance(declared, list) or any(set(item) != {"path", "size_bytes", "sha256"} for item in declared):
        raise RuntimeDataError("manifest files schema is invalid")
    for item in declared:
        _safe_relative(str(item["path"]))
        if not SHA256_RE.fullmatch(str(item["sha256"])):
            raise RuntimeDataError("manifest file SHA-256 is invalid")
    if actual != declared:
        raise RuntimeDataError("release files differ from the sealed manifest")
    if len(actual) != manifest["file_count"] or bundle_sha256(actual) != manifest["bundle_sha256"]:
        raise RuntimeDataError("release file count or bundle identity is invalid")
    expected_release = f'usda-{manifest["report_month"]}-{manifest["bundle_sha256"][:16]}'
    if release_id != expected_release:
        raise RuntimeDataError("release_id does not match the stable bundle identity")
    paths = {item["path"] for item in actual}
    if not REQUIRED_ROOT_FILES.issubset(paths):
        raise RuntimeDataError("required root JSON is missing")
    index = strict_json(data / "index.json")
    indexed_matrices = {str(item.get("file", "")) for item in index.get("matrices", [])}
    current_matrices = {path for path in paths if path.startswith("matrix/") and path.endswith(".json")}
    if indexed_matrices != current_matrices or len(indexed_matrices) != manifest["matrix_count"]:
        raise RuntimeDataError("matrix_count or current index matrix closure is invalid")
    actual_snapshot_months = sorted({
        parts[2]
        for relative in paths
        for parts in [PurePosixPath(relative).parts]
        if len(parts) >= 4 and parts[:2] == ("snapshots", "usda_psd") and MONTH_RE.fullmatch(parts[2])
    })
    if actual_snapshot_months != manifest["snapshot_months"]:
        raise RuntimeDataError("snapshot_months does not match the sealed data tree")
    for month in actual_snapshot_months:
        prefix = f"snapshots/usda_psd/{month}/"
        snapshot_index_path = data / "snapshots" / "usda_psd" / month / "index.json"
        if not snapshot_index_path.is_file():
            raise RuntimeDataError(f"snapshot index is missing: {month}")
        snapshot_index = strict_json(snapshot_index_path)
        indexed = {prefix + str(item.get("file", "")) for item in snapshot_index.get("matrices", [])}
        actual_matrices = {path for path in paths if path.startswith(prefix + "matrix/") and path.endswith(".json")}
        if not indexed or not indexed.issubset(actual_matrices):
            raise RuntimeDataError(f"snapshot matrix closure is incomplete: {month}")
    for relative, path in _iter_regular_files(data):
        if relative.endswith(".json"):
            try:
                value = strict_json(path)
                _validate_finite(value, relative)
            except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise RuntimeDataError(f"invalid JSON file: {relative}: {exc}") from exc
    report = strict_json(data / "report_version.json")
    if report != {"currentReportMonth": manifest["report_month"], "previousReportMonth": manifest["previous_report_month"]}:
        raise RuntimeDataError("report_version does not match release manifest")
    return manifest


def stage_runtime_release(
    *,
    source_release: Path,
    runtime_root: Path,
    app_contract_version: int,
    supported_data_schema_version: int,
) -> Path:
    manifest = validate_runtime_release(source_release, app_contract_version=app_contract_version, supported_data_schema_version=supported_data_schema_version)
    incoming = runtime_root / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    target = incoming / str(manifest["release_id"])
    if target.exists():
        raise RuntimeDataError(f"incoming candidate already exists: {target.name}")
    temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=incoming))
    try:
        for relative, source in _iter_regular_files(source_release):
            destination = temporary / Path(*_safe_relative(relative).parts)
            _copy_file(source, destination)
        validate_runtime_release(temporary, app_contract_version=app_contract_version, supported_data_schema_version=supported_data_schema_version, require_directory_identity=False)
        temporary.rename(target)
        return target
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def compare_json_semantics(expected: Path, actual: Path) -> dict[str, Any]:
    expected_paths = {relative: path for relative, path in _iter_regular_files(expected) if relative.endswith(".json")}
    actual_paths = {relative: path for relative, path in _iter_regular_files(actual) if relative.endswith(".json")}
    missing = sorted(set(expected_paths) - set(actual_paths))
    extra = sorted(set(actual_paths) - set(expected_paths))
    changed: list[str] = []
    for relative in sorted(set(expected_paths) & set(actual_paths)):
        if strict_json(expected_paths[relative]) != strict_json(actual_paths[relative]):
            changed.append(relative)
    return {"status": "equivalent" if not missing and not extra and not changed else "different", "json_file_count": len(expected_paths), "missing": missing, "extra": extra, "changed": changed}


def current_pointer(manifest: Mapping[str, Any], *, updated_at: str | None = None) -> dict[str, Any]:
    return {
        "release_id": manifest["release_id"],
        "report_month": manifest["report_month"],
        "previous_report_month": manifest["previous_report_month"],
        "data_schema_version": manifest["data_schema_version"],
        "minimum_app_contract_version": manifest["minimum_app_contract_version"],
        "updated_at": updated_at or utc_now(),
        "bundle_sha256": manifest["bundle_sha256"],
    }


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _exclusive_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeDataError(f"evidence already exists: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise RuntimeDataError(f"evidence already exists: {path}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(value + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def initialize_legacy_release(
    *,
    runtime_root: Path,
    release_id: str,
    app_contract_version: int,
    supported_data_schema_version: int,
) -> Path:
    """Create the one-time legacy pointer without ever retargeting it."""
    release_id = validate_release_id(release_id)
    target = runtime_root / "releases" / release_id
    validate_runtime_release(
        target,
        app_contract_version=app_contract_version,
        supported_data_schema_version=supported_data_schema_version,
    )
    validate_release_permissions(target)
    legacy = runtime_root / "legacy"
    expected_link = Path("releases") / release_id
    if os.path.lexists(legacy):
        if not legacy.is_symlink():
            raise RuntimeDataError("legacy must be a controlled symbolic link")
        if Path(os.readlink(legacy)) != expected_link or legacy.resolve() != target.resolve():
            raise RuntimeDataError("legacy is immutable and already points to another release")
        return legacy
    temporary = runtime_root / f".legacy.{os.getpid()}.tmp"
    if os.path.lexists(temporary):
        raise RuntimeDataError("temporary legacy pointer already exists")
    try:
        os.symlink(expected_link, temporary, target_is_directory=True)
        if temporary.resolve() != target.resolve():
            raise RuntimeDataError("temporary legacy pointer escaped the validated release")
        os.replace(temporary, legacy)
    except Exception:
        if os.path.lexists(temporary):
            temporary.unlink()
        raise
    return legacy


HttpCheck = Callable[[str], bool]


def promote_runtime_release(
    *,
    runtime_root: Path,
    candidate: Path,
    app_contract_version: int,
    supported_data_schema_version: int,
    http_check: HttpCheck,
    evidence_path: Path,
) -> dict[str, Any]:
    if evidence_path.exists():
        raise RuntimeDataError(f"evidence already exists: {evidence_path}")
    incoming = (runtime_root / "incoming").resolve()
    candidate_resolved = candidate.resolve()
    if candidate_resolved.parent != incoming:
        raise RuntimeDataError("candidate must be a unique direct child of runtime incoming")
    manifest = validate_runtime_release(candidate_resolved, app_contract_version=app_contract_version, supported_data_schema_version=supported_data_schema_version)
    release_id = str(manifest["release_id"])
    releases = runtime_root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    target = releases / release_id
    if target.exists():
        raise RuntimeDataError(f"release_id already exists: {release_id}")
    if candidate_resolved.stat().st_dev != releases.stat().st_dev:
        raise RuntimeDataError("incoming and releases must be on the same filesystem")
    old_pointer_path = runtime_root / "current.json"
    old_pointer = strict_json(old_pointer_path) if old_pointer_path.is_file() else None
    old_current = (runtime_root / "current").read_text(encoding="utf-8").strip() if (runtime_root / "current").is_file() else None
    old_previous = (runtime_root / "previous").read_text(encoding="utf-8").strip() if (runtime_root / "previous").is_file() else None
    prepare_formal_release_permissions(candidate_resolved)
    candidate_resolved.rename(target)
    validate_runtime_release(target, app_contract_version=app_contract_version, supported_data_schema_version=supported_data_schema_version)
    validate_release_permissions(target)
    if not http_check(release_id):
        failed = runtime_root / "failed"
        failed.mkdir(parents=True, exist_ok=True)
        quarantine = failed / f"{release_id}__http-candidate-failed"
        if quarantine.exists():
            raise RuntimeDataError("immutable release HTTP candidate validation failed and quarantine path already exists")
        target.rename(quarantine)
        raise RuntimeDataError("immutable release HTTP candidate validation failed")
    pointer = current_pointer(manifest)
    try:
        if old_pointer:
            _atomic_text(runtime_root / "previous", str(old_pointer["release_id"]))
        _atomic_text(runtime_root / "current", release_id)
        _atomic_json(old_pointer_path, pointer)
        if not http_check(release_id):
            raise RuntimeDataError("post-promote HTTP validation failed")
    except Exception:
        if old_pointer is None:
            for path in (old_pointer_path, runtime_root / "current"):
                path.unlink(missing_ok=True)
        else:
            _atomic_json(old_pointer_path, old_pointer)
            _atomic_text(runtime_root / "current", old_current or str(old_pointer["release_id"]))
        if old_previous is None:
            (runtime_root / "previous").unlink(missing_ok=True)
        else:
            _atomic_text(runtime_root / "previous", old_previous)
        if target.is_dir():
            failed = runtime_root / "failed"
            failed.mkdir(parents=True, exist_ok=True)
            quarantine = failed / f"{release_id}__post-promote-failed"
            if not quarantine.exists():
                target.rename(quarantine)
        raise
    result = {"schema_version": 1, "status": "promoted", "release_id": release_id, "previous_release_id": old_pointer.get("release_id") if old_pointer else None, "bundle_sha256": manifest["bundle_sha256"], "completed_at": utc_now()}
    _exclusive_json(evidence_path, result)
    return result


def rollback_runtime_release(
    *,
    runtime_root: Path,
    app_contract_version: int,
    supported_data_schema_version: int,
    http_check: HttpCheck,
    evidence_path: Path,
) -> dict[str, Any]:
    if evidence_path.exists():
        raise RuntimeDataError(f"evidence already exists: {evidence_path}")
    current_path = runtime_root / "current.json"
    previous_path = runtime_root / "previous"
    if not current_path.is_file() or not previous_path.is_file():
        raise RuntimeDataError("rollback requires current.json and previous")
    original = strict_json(current_path)
    target_id = validate_release_id(previous_path.read_text(encoding="utf-8").strip())
    target_manifest = validate_runtime_release(runtime_root / "releases" / target_id, app_contract_version=app_contract_version, supported_data_schema_version=supported_data_schema_version)
    target_pointer = current_pointer(target_manifest)
    try:
        _atomic_text(runtime_root / "previous", str(original["release_id"]))
        _atomic_text(runtime_root / "current", target_id)
        _atomic_json(current_path, target_pointer)
        if not http_check(target_id):
            raise RuntimeDataError("rollback post-check failed")
    except Exception:
        _atomic_json(current_path, original)
        _atomic_text(runtime_root / "current", str(original["release_id"]))
        _atomic_text(runtime_root / "previous", target_id)
        raise
    result = {"schema_version": 1, "status": "rolled_back", "release_id": target_id, "previous_release_id": original["release_id"], "completed_at": utc_now()}
    _exclusive_json(evidence_path, result)
    return result


def http_release_check(base_url: str, release_id: str) -> bool:
    validate_release_id(release_id)
    url = f"{base_url.rstrip('/')}/usda/data/releases/{release_id}/data/report_version.json"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status == 200 and "application/json" in (response.headers.get("content-type") or "")
    except (urllib.error.URLError, OSError):
        return False


def _app_contract(path: Path) -> tuple[int, int]:
    value = strict_json(path)
    return int(value["app_contract_version"]), int(value["supported_data_schema_version"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    package = sub.add_parser("package")
    package.add_argument("--source-data", type=Path, required=True)
    package.add_argument("--output-parent", type=Path, required=True)
    package.add_argument("--builder-git-sha", required=True)
    package.add_argument("--builder-tree-sha", required=True)
    package.add_argument("--source-fetch-manifest", type=Path, required=True)
    seed = sub.add_parser("package-seed")
    seed.add_argument("--source-data", type=Path, required=True)
    seed.add_argument("--output-parent", type=Path, required=True)
    seed.add_argument("--builder-git-sha", required=True)
    seed.add_argument("--builder-tree-sha", required=True)
    seed.add_argument("--source-identity", type=Path, required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--release", type=Path, required=True)
    validate.add_argument("--app-contract", type=Path, required=True)
    stage = sub.add_parser("stage")
    stage.add_argument("--source-release", type=Path, required=True)
    stage.add_argument("--runtime-root", type=Path, required=True)
    stage.add_argument("--app-contract", type=Path, required=True)
    compare = sub.add_parser("compare-json")
    compare.add_argument("--expected", type=Path, required=True)
    compare.add_argument("--actual", type=Path, required=True)
    legacy = sub.add_parser("initialize-legacy")
    legacy.add_argument("--runtime-root", type=Path, required=True)
    legacy.add_argument("--release-id", required=True)
    legacy.add_argument("--app-contract", type=Path, required=True)
    for name in ("promote", "rollback"):
        command = sub.add_parser(name)
        command.add_argument("--runtime-root", type=Path, required=True)
        command.add_argument("--app-contract", type=Path, required=True)
        command.add_argument("--base-url", required=True)
        command.add_argument("--evidence", type=Path, required=True)
        if name == "promote":
            command.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "package":
            source_batch = strict_json(args.source_fetch_manifest)
            if source_batch.get("status") != "success" or source_batch.get("reportMonth") != strict_json(args.source_data / "report_version.json").get("currentReportMonth"):
                raise RuntimeDataError("source fetch manifest is not a successful batch for the target report month")
            if source_batch.get("expectedMetadataRequests") != 4 or source_batch.get("expectedPsdRequests") != 468:
                raise RuntimeDataError("source fetch manifest request counts are incomplete")
            requests = source_batch.get("requests")
            if not isinstance(requests, list) or len(requests) != 472 or any(item.get("status") != "success" or item.get("success") is not True for item in requests):
                raise RuntimeDataError("source fetch manifest does not prove 472 successful requests")
            result = build_runtime_release(source_data=args.source_data, output_parent=args.output_parent, builder_git_sha=args.builder_git_sha, builder_tree_sha=args.builder_tree_sha, fetch_run_id=str(source_batch.get("runId", "")), source_manifest_sha256=sha256_file(args.source_fetch_manifest))
            manifest = strict_json(result / "release_manifest.json")
            print(json.dumps({"release": str(result), "manifest_sha256": sha256_file(result / "release_manifest.json"), "current_pointer_candidate": current_pointer(manifest)}, sort_keys=True))
        elif args.command == "package-seed":
            result = build_runtime_seed_release(source_data=args.source_data, output_parent=args.output_parent, builder_git_sha=args.builder_git_sha, builder_tree_sha=args.builder_tree_sha, source_batch_identity=strict_json(args.source_identity))
            manifest = strict_json(result / "release_manifest.json")
            print(json.dumps({"release": str(result), "manifest_sha256": sha256_file(result / "release_manifest.json"), "current_pointer_candidate": current_pointer(manifest)}, sort_keys=True))
        elif args.command == "validate":
            app, schema = _app_contract(args.app_contract)
            manifest = validate_runtime_release(args.release, app_contract_version=app, supported_data_schema_version=schema)
            print(json.dumps({"status": "valid", "release_id": manifest["release_id"], "bundle_sha256": manifest["bundle_sha256"]}, sort_keys=True))
        elif args.command == "stage":
            app, schema = _app_contract(args.app_contract)
            result = stage_runtime_release(source_release=args.source_release, runtime_root=args.runtime_root, app_contract_version=app, supported_data_schema_version=schema)
            print(json.dumps({"status": "staged", "candidate": str(result)}, sort_keys=True))
        elif args.command == "compare-json":
            result = compare_json_semantics(args.expected, args.actual)
            print(json.dumps(result, sort_keys=True))
            if result["status"] != "equivalent":
                return 2
        elif args.command == "initialize-legacy":
            app, schema = _app_contract(args.app_contract)
            result = initialize_legacy_release(
                runtime_root=args.runtime_root,
                release_id=args.release_id,
                app_contract_version=app,
                supported_data_schema_version=schema,
            )
            print(json.dumps({"status": "initialized", "legacy": str(result), "release_id": args.release_id}, sort_keys=True))
        elif args.command == "promote":
            app, schema = _app_contract(args.app_contract)
            result = promote_runtime_release(runtime_root=args.runtime_root, candidate=args.candidate, app_contract_version=app, supported_data_schema_version=schema, http_check=lambda release_id: http_release_check(args.base_url, release_id), evidence_path=args.evidence)
            print(json.dumps(result, sort_keys=True))
        else:
            app, schema = _app_contract(args.app_contract)
            result = rollback_runtime_release(runtime_root=args.runtime_root, app_contract_version=app, supported_data_schema_version=schema, http_check=lambda release_id: http_release_check(args.base_url, release_id), evidence_path=args.evidence)
            print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"USDA Runtime data operation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
