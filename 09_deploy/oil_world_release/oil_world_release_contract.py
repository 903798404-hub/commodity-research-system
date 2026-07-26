"""Shared, application-specific release contract for Oil World.

The module intentionally reuses only the repository's generic JSON Schema
validator.  Oil World service, port, data-mount, image, and artifact rules
remain local to this directory rather than inheriting Spread assumptions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


RELEASE_DIRECTORY = Path(__file__).resolve().parent
REPOSITORY = RELEASE_DIRECTORY.parents[1]
SPREAD_RELEASE_DIRECTORY = REPOSITORY / "09_deploy" / "spread_release"
if str(SPREAD_RELEASE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SPREAD_RELEASE_DIRECTORY))

# This is the reusable non-business schema validator used by the established
# release system.  No Spread service constants or data rules are imported.
from release_contract import (  # noqa: E402
    ContractError,
    load_schema,
    validate_against_schema,
)


APPLICATION = "oil-world-dashboard"
COMPOSE_PROJECT = "market-data-oil-world"
SERVICE = "oil-world-dashboard"
FORMAL_CONTAINERS = ("spread-dashboard", "usda-dashboard", SERVICE)
DATA_CONTAINER_PATH = "/usr/share/nginx/html/oil-world/data/oil_world"
PRODUCTION_COMPOSE = RELEASE_DIRECTORY / "compose.production.yml"
CANDIDATE_COMPOSE = RELEASE_DIRECTORY / "compose.candidate.yml"
FULL_GIT_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RELEASE_ID_RE = re.compile(r"^oil-\d{8}-[0-9a-f]{12}-b\d{2}$")
SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ARTIFACTS = {
    "release": ("release.json", "release.manifest.json", "release.schema.json"),
    "candidate_result": (
        "candidate_result.json",
        "candidate_result.manifest.json",
        "candidate_result.schema.json",
    ),
    "deployment_plan": (
        "deployment_plan.json",
        "deployment_plan.manifest.json",
        "deployment_plan.schema.json",
    ),
    "deployment_result": (
        "deployment_result.json",
        "deployment_result.manifest.json",
        "deployment_result.schema.json",
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_full_git(value: Any, field: str) -> str:
    if not isinstance(value, str) or not FULL_GIT_RE.fullmatch(value):
        raise ContractError(f"{field} must be a full 40-character lowercase Git SHA")
    return value


def _require_image_id(value: Any, field: str = "image_id") -> str:
    if not isinstance(value, str) or not IMAGE_ID_RE.fullmatch(value):
        raise ContractError(f"{field} must be a full sha256 image ID")
    return value


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContractError(f"{field} must be a lowercase SHA-256")
    return value


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _write_exclusive(path: Path, payload: Mapping[str, Any], description: str) -> None:
    """Atomically publish a JSON artifact without ever overwriting evidence."""

    path = path.resolve()
    if path.exists():
        raise ContractError(f"{description} already exists and will not be overwritten: {path}")
    if not path.parent.is_dir():
        raise ContractError(f"{description} parent directory is missing: {path.parent}")
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            if handle.write(encoded) != len(encoded):
                raise OSError("short evidence write")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(0o444)
        os.link(temporary, path)
    except FileExistsError as exc:
        raise ContractError(f"{description} already exists and will not be overwritten: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _schema(name: str) -> dict[str, Any]:
    return load_schema(RELEASE_DIRECTORY / name)


def artifact_paths(directory: Path, artifact_type: str) -> tuple[Path, Path, Path]:
    try:
        target_name, manifest_name, schema_name = ARTIFACTS[artifact_type]
    except KeyError as exc:
        raise ContractError(f"unsupported Oil World artifact type: {artifact_type}") from exc
    directory = directory.resolve()
    return directory / target_name, directory / manifest_name, RELEASE_DIRECTORY / schema_name


def write_artifact(
    directory: Path,
    artifact_type: str,
    payload: Mapping[str, Any],
    *,
    release_id: str,
    git_commit: str,
    git_tree: str,
    image_id: str,
    runtime_git_commit: str | None = None,
) -> tuple[Path, Path]:
    """Validate and seal an Oil World artifact and its independent manifest."""

    target, manifest_path, schema_path = artifact_paths(directory, artifact_type)
    if manifest_path.exists():
        raise ContractError(f"{artifact_type} manifest already exists: {manifest_path}")
    validate_against_schema(dict(payload), _schema(schema_path.name))
    _require_full_git(git_commit, "git_commit")
    _require_full_git(git_tree, "git_tree")
    _require_image_id(image_id)
    if runtime_git_commit is not None:
        _require_full_git(runtime_git_commit, "runtime_git_commit")
        if runtime_git_commit != git_commit:
            raise ContractError("measured runtime_git_commit must equal release git_commit")
    installed = False
    try:
        _write_exclusive(target, payload, f"{artifact_type} artifact")
        installed = True
        formal = payload.get("formal_containers")
        if not isinstance(formal, Mapping):
            raise ContractError(f"{artifact_type} must include formal_containers evidence")
        manifest: dict[str, Any] = {
            "schema_version": "1.0.0",
            "artifact_type": artifact_type,
            "target_file": target.name,
            "target_schema_version": str(payload["schema_version"]),
            "target_sha256": hash_file(target),
            "target_size_bytes": target.stat().st_size,
            "generated_at": utc_now(),
            "release_id": release_id,
            "git_commit": git_commit,
            "git_tree": git_tree,
            "image_id": image_id,
            "formal_evidence_sha256": hash_json(dict(formal)),
        }
        if runtime_git_commit is not None:
            manifest["runtime_git_commit"] = runtime_git_commit
        validate_against_schema(manifest, _schema("artifact_manifest.schema.json"))
        _write_exclusive(manifest_path, manifest, f"{artifact_type} manifest")
        verify_artifact(
            directory,
            artifact_type,
            expected_git_commit=git_commit,
            expected_git_tree=git_tree,
            expected_image_id=image_id,
            expected_runtime_git_commit=runtime_git_commit,
        )
        return target, manifest_path
    except Exception:
        if installed and target.exists() and not manifest_path.exists():
            target.unlink()
        raise


def verify_artifact(
    directory: Path,
    artifact_type: str,
    *,
    expected_git_commit: str | None = None,
    expected_git_tree: str | None = None,
    expected_image_id: str | None = None,
    expected_runtime_git_commit: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    target, manifest_path, schema_path = artifact_paths(directory, artifact_type)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read sealed {artifact_type} evidence: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(manifest, dict):
        raise ContractError(f"sealed {artifact_type} evidence must contain JSON objects")
    validate_against_schema(payload, _schema(schema_path.name))
    validate_against_schema(manifest, _schema("artifact_manifest.schema.json"))
    expected_target, expected_manifest, _ = ARTIFACTS[artifact_type]
    if manifest.get("artifact_type") != artifact_type or manifest.get("target_file") != expected_target:
        raise ContractError(f"{artifact_type} manifest target identity mismatch")
    if manifest_path.name != expected_manifest or manifest.get("target_schema_version") != payload.get("schema_version"):
        raise ContractError(f"{artifact_type} manifest schema identity mismatch")
    if manifest.get("target_sha256") != hash_file(target) or manifest.get("target_size_bytes") != target.stat().st_size:
        raise ContractError(f"{artifact_type} manifest file identity mismatch")
    formal = payload.get("formal_containers")
    if not isinstance(formal, Mapping) or manifest.get("formal_evidence_sha256") != hash_json(dict(formal)):
        raise ContractError(f"{artifact_type} formal container evidence mismatch")
    for field, expected in (
        ("git_commit", expected_git_commit),
        ("git_tree", expected_git_tree),
        ("image_id", expected_image_id),
        ("runtime_git_commit", expected_runtime_git_commit),
    ):
        if expected is not None and manifest.get(field) != expected:
            raise ContractError(f"{artifact_type} manifest {field} mismatch")
    return payload, manifest


def parse_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ContractError(f"invalid environment line {number} in {path}")
        name, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in values:
            raise ContractError(f"invalid or duplicate environment name {name!r}")
        values[name] = value
    return values


def validate_production_environment(values: Mapping[str, str]) -> dict[str, str]:
    allowed = {
        "COMPOSE_PROJECT_NAME",
        "OIL_WORLD_IMAGE",
        "OIL_WORLD_PORT",
        "OIL_WORLD_DATA_ROOT",
        "OIL_WORLD_NETWORK_NAME",
    }
    required = {"OIL_WORLD_IMAGE", "OIL_WORLD_PORT", "OIL_WORLD_DATA_ROOT"}
    missing = required - set(values)
    unknown = set(values) - allowed
    if missing or unknown:
        raise ContractError(f"Oil World production environment missing={sorted(missing)} unknown={sorted(unknown)}")
    normalized = dict(values)
    if normalized.get("COMPOSE_PROJECT_NAME", COMPOSE_PROJECT) != COMPOSE_PROJECT:
        raise ContractError("COMPOSE_PROJECT_NAME must be market-data-oil-world")
    image = validate_immutable_image_reference(normalized["OIL_WORLD_IMAGE"], "OIL_WORLD_IMAGE")
    try:
        port = int(normalized["OIL_WORLD_PORT"])
    except ValueError as exc:
        raise ContractError("OIL_WORLD_PORT must be numeric") from exc
    if port != 8081:
        raise ContractError("Oil World formal port must remain 8081")
    data_root = normalized["OIL_WORLD_DATA_ROOT"].strip()
    # The production value is a Linux path.  Accept a Windows absolute path in
    # local fake-Docker tests so the same contract can be tested without a
    # server; a Windows path cannot resolve on the Linux deployment host.
    if not (data_root.startswith("/") or re.fullmatch(r"[A-Za-z]:[\\/].*", data_root)):
        raise ContractError("OIL_WORLD_DATA_ROOT must be an absolute path")
    normalized["OIL_WORLD_IMAGE"] = image
    normalized["OIL_WORLD_PORT"] = str(port)
    normalized["OIL_WORLD_DATA_ROOT"] = data_root
    normalized["OIL_WORLD_NETWORK_NAME"] = normalized.get(
        "OIL_WORLD_NETWORK_NAME", "market-data-oil-world_default"
    )
    normalized["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT
    return normalized


def validate_immutable_image_reference(value: Any, field: str) -> str:
    """Reject ambiguous and moving image references in release controls."""

    if not isinstance(value, str) or value != value.strip() or not value or value.endswith(":latest") or ":" not in value:
        raise ContractError(f"{field} must be a non-latest immutable tag")
    return value


def candidate_environment(
    production: Mapping[str, str], *, release_id: str, candidate_port: int
) -> dict[str, str]:
    if not RELEASE_ID_RE.fullmatch(release_id):
        raise ContractError("release_id must be oil-YYYYMMDD-<git12>-bNN")
    if not 18081 <= candidate_port <= 18499 or candidate_port in {8080, 8081, 8501}:
        raise ContractError("candidate port must be in 18081-18499 and outside formal ports")
    values = validate_production_environment(production)
    suffix = release_id.replace("oil-", "")
    values.update(
        {
            "OIL_WORLD_CANDIDATE_CONTAINER_NAME": f"oil-world-dashboard-candidate-{suffix}",
            "OIL_WORLD_CANDIDATE_PROJECT_NAME": f"market-data-oil-world-candidate-{suffix}",
            "OIL_WORLD_CANDIDATE_HOST_PORT": str(candidate_port),
        }
    )
    if not SAFE_NAME_RE.fullmatch(values["OIL_WORLD_CANDIDATE_CONTAINER_NAME"]):
        raise ContractError("candidate container name is invalid")
    if not SAFE_NAME_RE.fullmatch(values["OIL_WORLD_CANDIDATE_PROJECT_NAME"]):
        raise ContractError("candidate project name is invalid")
    return values


def git_identity(repository: Path, runner: Callable[..., Any] = subprocess.run) -> tuple[str, str]:
    repository = repository.resolve()
    status = runner(["git", "-C", str(repository), "status", "--porcelain"], capture_output=True, text=True, check=False)
    if status.returncode != 0 or status.stdout.strip():
        raise ContractError("candidate build repository must be clean")
    commit = runner(["git", "-C", str(repository), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    tree = runner(["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True, check=False)
    if commit.returncode or tree.returncode:
        raise ContractError("cannot read candidate repository Git identity")
    return _require_full_git(commit.stdout.strip(), "git_commit"), _require_full_git(tree.stdout.strip(), "git_tree")
