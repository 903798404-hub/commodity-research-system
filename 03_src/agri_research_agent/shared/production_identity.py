"""Fail-closed execution identity verification for Git and OCI runtimes.

This module verifies primary material on every call.  A returned object is
descriptive only; callers must never use it as a cached authorization flag.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
import re
import socket
import subprocess
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .production_grant import GrantShapeError, parse_execution_grant_json
from .runtime_manifest import ManifestValidationError, parse_runtime_manifest


class ProductionIdentityError(RuntimeError):
    pass


class IdentityKind(StrEnum):
    GIT_WORKTREE = "git_worktree"
    OCI_CONTAINER = "oci_container"


class AuthorizationRole(StrEnum):
    PRODUCTION = "production"
    CANDIDATE_VALIDATION = "candidate_validation"


_SHA = re.compile(r"^[0-9a-f]{64}$")
_GIT = re.compile(r"^[0-9a-f]{40}$")
_IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_ROOT = Path(__file__).resolve().parents[3]
TRUST_CONFIG_PATH = _ROOT / "02_configs" / "production_runtime_trust.json"
MARKER_FILENAME = ".market-data-runtime.json"
_GIT_IDENTITY_OVERRIDES = {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_REPLACE_REF_BASE"}


@dataclass(frozen=True, slots=True)
class GitExecutionRequest:
    repository_root: Path
    approved_commit: str
    approved_tree: str


@dataclass(frozen=True, slots=True)
class OCIExecutionRequest:
    grant_path: Path
    release_path: Path
    runtime_manifest_path: Path
    runtime_root: Path
    runtime_marker_path: Path


@dataclass(frozen=True, slots=True)
class VerifiedExecutionIdentity:
    kind: IdentityKind
    role: AuthorizationRole
    project_id: str
    module_id: str
    service_id: str
    runtime_id: str
    approved_commit: str
    approved_tree: str
    image_id: str | None
    grant_id: str | None
    writable_roots: tuple[Path, ...]


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProductionIdentityError("identity JSON is not canonicalizable") from exc


def _json_object(text: str, label: str) -> object:
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=no_duplicates)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProductionIdentityError(f"{label} is invalid") from exc


def _sha_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ProductionIdentityError(f"identity input is missing or unsafe: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ProductionIdentityError(f"invalid {label}")
    return value


def _require_git(value: object, label: str) -> str:
    if not isinstance(value, str) or not _GIT.fullmatch(value):
        raise ProductionIdentityError(f"invalid {label}")
    return value


def _require_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ProductionIdentityError(f"invalid {label}")
    return value


def _git(root: Path, *args: str) -> str:
    if _GIT_IDENTITY_OVERRIDES & set(os.environ):
        raise ProductionIdentityError("Git environment overrides are forbidden")
    try:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env["GIT_NO_REPLACE_OBJECTS"] = "1"
        result = subprocess.run(["git", "--no-replace-objects", "-C", str(root), *args], capture_output=True, text=True, timeout=30, check=False, env=env)
    except OSError as exc:
        raise ProductionIdentityError("Git identity cannot be inspected") from exc
    if result.returncode:
        raise ProductionIdentityError("Git identity check failed")
    return result.stdout.strip()


def _verify_git(request: GitExecutionRequest, role: AuthorizationRole, module_id: str, runtime_id: str, writable_root: Path) -> VerifiedExecutionIdentity:
    root = request.repository_root.resolve(strict=True)
    if root != _ROOT:
        raise ProductionIdentityError("Git request is not bound to the executing source repository")
    commit, tree = _require_git(request.approved_commit, "approved commit"), _require_git(request.approved_tree, "approved tree")
    git_entry = root / ".git"
    if not git_entry.is_dir() or git_entry.is_symlink() or getattr(git_entry, "is_junction", lambda: False)():
        raise ProductionIdentityError("Git execution requires a normal repository; linked worktrees are forbidden")
    if _git(root, "rev-parse", "--is-inside-work-tree") != "true" or Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ProductionIdentityError("Git execution root is not the repository top level")
    common_dir = Path(_git(root, "rev-parse", "--git-common-dir"))
    common_dir = (root / common_dir).resolve() if not common_dir.is_absolute() else common_dir.resolve()
    if common_dir != git_entry.resolve():
        raise ProductionIdentityError("Git execution repository has redirected common directory")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ProductionIdentityError("Git execution repository is dirty")
    tracked = _git(root, "ls-files", "-v", "-z").split("\0")
    if any(record and not record.startswith("H ") for record in tracked):
        raise ProductionIdentityError("Git execution repository has hidden index flags")
    if _git(root, "rev-parse", "HEAD") != commit or _git(root, "rev-parse", "HEAD^{tree}") != tree:
        raise ProductionIdentityError("Git execution identity differs from approved commit/tree")
    return VerifiedExecutionIdentity(IdentityKind.GIT_WORKTREE, role, "git-worktree", module_id, "git-worktree", runtime_id, commit, tree, None, None, (writable_root.resolve(strict=True),))


def _load_trust(domain: str) -> tuple[dict[str, Ed25519PublicKey], set[str], set[str]]:
    try:
        raw = _json_object(TRUST_CONFIG_PATH.read_text(encoding="utf-8"), "production trust configuration")
    except (OSError, UnicodeError, ProductionIdentityError) as exc:
        raise ProductionIdentityError("production trust configuration is unavailable") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "keys", "revoked_key_ids", "revoked_grant_ids"} or raw["schema_version"] != "production-runtime-trust/1":
        raise ProductionIdentityError("production trust configuration is invalid")
    keys: dict[str, Ed25519PublicKey] = {}
    if not isinstance(raw["keys"], list) or not isinstance(raw["revoked_key_ids"], list) or not isinstance(raw["revoked_grant_ids"], list):
        raise ProductionIdentityError("production trust configuration is invalid")
    try:
        revoked_keys, revoked_grants = set(raw["revoked_key_ids"]), set(raw["revoked_grant_ids"])
    except TypeError as exc:
        raise ProductionIdentityError("production trust revocation list is invalid") from exc
    if not all(isinstance(v, str) and v for v in revoked_keys | revoked_grants):
        raise ProductionIdentityError("production trust revocation list is invalid")
    all_key_ids: set[str] = set()
    all_key_material: set[str] = set()
    for item in raw["keys"]:
        if not isinstance(item, dict) or set(item) != {"key_id", "domain", "algorithm", "public_key_base64"} or item.get("domain") not in {"production", "candidate_validation"} or item.get("algorithm") != "ed25519":
            raise ProductionIdentityError("production trust key is invalid")
        key_id = _require_id(item.get("key_id"), "trust key id")
        if key_id in all_key_ids:
            raise ProductionIdentityError("duplicate production trust key")
        all_key_ids.add(key_id)
        if not isinstance(item.get("public_key_base64"), str) or item["public_key_base64"] in all_key_material:
            raise ProductionIdentityError("duplicate production trust key material")
        all_key_material.add(item["public_key_base64"])
        if item["domain"] != domain:
            continue
        try:
            encoded = base64.b64decode(item["public_key_base64"], validate=True)
            keys[key_id] = Ed25519PublicKey.from_public_bytes(encoded)
        except (ValueError, TypeError) as exc:
            raise ProductionIdentityError("production trust public key is invalid") from exc
    return keys, revoked_keys, revoked_grants


def _parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ProductionIdentityError(f"invalid {label}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductionIdentityError(f"invalid {label}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductionIdentityError(f"invalid {label}")
    return parsed.astimezone(timezone.utc)


def _signed_payload(path: Path, role: AuthorizationRole) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ProductionIdentityError("execution grant is missing or unsafe")
    try:
        raw = path.read_bytes()
        payload = parse_execution_grant_json(raw)
        envelope = _json_object(raw.decode("utf-8"), "execution grant")
    except (OSError, UnicodeError, ProductionIdentityError, GrantShapeError) as exc:
        raise ProductionIdentityError("execution grant is invalid") from exc
    key_id = _require_id(envelope.get("key_id"), "grant key id")
    keys, revoked_keys, revoked_grants = _load_trust(role.value)
    if key_id in revoked_keys or key_id not in keys:
        raise ProductionIdentityError("grant signing key is untrusted or revoked")
    try:
        signature = base64.b64decode(envelope["signature"], validate=True)
        keys[key_id].verify(signature, _canonical(payload))
    except (ValueError, InvalidSignature) as exc:
        raise ProductionIdentityError("execution grant signature is invalid") from exc
    if payload.get("grant_id") in revoked_grants:
        raise ProductionIdentityError("execution grant is revoked")
    return payload


def _mount_options() -> dict[str, set[str]]:
    try:
        rows = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ProductionIdentityError("runtime mount table is unavailable") from exc
    mounts: dict[str, set[str]] = {}
    for row in rows:
        pieces = row.split()
        if len(pieces) < 6 or "-" not in pieces:
            continue
        target = re.sub(r"\\(040|011|012|134)", lambda match: {"040": " ", "011": "\t", "012": "\n", "134": "\\"}[match.group(1)], pieces[4])
        mounts[target] = set(pieces[5].split(","))
    return mounts


def _mount_for(mounts: Mapping[str, set[str]], path: Path) -> set[str]:
    target = str(path.resolve(strict=True))
    matches = [(len(mount), options) for mount, options in mounts.items() if target == mount or target.startswith(mount.rstrip("/") + "/")]
    return max(matches, default=(0, set()))[1]


def _runtime_path(path: Path) -> str:
    """The actual absolute in-container path; unit tests may adapt observation only."""
    return str(path.resolve(strict=True))


def _within_mount(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _verify_oci(request: OCIExecutionRequest, role: AuthorizationRole, module_id: str, runtime_id: str, marker_sha256: str) -> VerifiedExecutionIdentity:
    payload = _signed_payload(request.grant_path, role)
    grant_v2 = "runtime_manifest_schema_version" in payload
    try:
        actual_role = AuthorizationRole(payload["role"])
    except (TypeError, ValueError) as exc:
        raise ProductionIdentityError("execution grant role is invalid") from exc
    if actual_role is not role or payload.get("authorization_mode") != actual_role.value:
        raise ProductionIdentityError("execution grant role does not authorize this runtime mode")
    for key in ("project_id", "module_id", "service_id", "runtime_id"):
        _require_id(payload.get(key), key)
    if payload["module_id"] != module_id or payload["runtime_id"] != runtime_id:
        raise ProductionIdentityError("execution grant module/runtime identity mismatch")
    for key in ("approved_commit", "approved_tree", "release_commit", "release_tree"):
        _require_git(payload.get(key), key)
    if payload["approved_commit"] != payload["release_commit"] or payload["approved_tree"] != payload["release_tree"]:
        raise ProductionIdentityError("release identity differs from approved identity")
    if not isinstance(payload.get("image_id"), str) or not _IMAGE.fullmatch(payload["image_id"]):
        raise ProductionIdentityError("execution grant image identity is invalid")
    for key in ("release_sha256", "runtime_manifest_sha256", "runtime_marker_sha256", "rendered_compose_sha256", "mount_contract_sha256", "actual_config_sha256"):
        _require_sha(payload.get(key), key)
    try:
        request.release_path.resolve(strict=True).relative_to(_ROOT)
        request.runtime_manifest_path.resolve(strict=True).relative_to(_ROOT)
    except ValueError as exc:
        raise ProductionIdentityError("release or runtime manifest is outside executing source image") from exc
    if _sha_file(request.release_path) != payload["release_sha256"] or _sha_file(request.runtime_manifest_path) != payload["runtime_manifest_sha256"]:
        raise ProductionIdentityError("release or runtime manifest identity mismatch")
    try:
        release = _json_object(request.release_path.read_text(encoding="utf-8"), "release")
        manifest_raw = _json_object(request.runtime_manifest_path.read_text(encoding="utf-8"), "runtime manifest")
    except (UnicodeError, ProductionIdentityError) as exc:
        raise ProductionIdentityError("release or runtime manifest is invalid") from exc
    if not isinstance(release, dict) or release.get("git_commit") != payload["release_commit"] or release.get("git_tree") != payload["release_tree"]:
        raise ProductionIdentityError("embedded release identity mismatch")
    if not isinstance(manifest_raw, dict) or any(manifest_raw.get(key) != payload[key] for key in ("project_id", "module_id", "service_id")):
        raise ProductionIdentityError("runtime manifest identity mismatch")
    manifest = manifest_raw
    if grant_v2:
        try:
            manifest_object = parse_runtime_manifest(manifest_raw)
            manifest = manifest_object.to_dict()
        except (ManifestValidationError, TypeError, ValueError) as exc:
            raise ProductionIdentityError("runtime manifest violates its source contract") from exc
        if (manifest["schema_version"] != payload["runtime_manifest_schema_version"]
                or manifest["identity_root_role"] != payload["identity_root_role"]):
            raise ProductionIdentityError("runtime manifest version or identity root mismatch")
    if request.runtime_marker_path.is_symlink():
        raise ProductionIdentityError("runtime marker must not be a symbolic link")
    marker_path = request.runtime_marker_path.resolve(strict=True)
    if marker_path != (request.runtime_root.resolve(strict=True) / MARKER_FILENAME) or _sha_file(marker_path) != marker_sha256 or marker_sha256 != payload["runtime_marker_sha256"]:
        raise ProductionIdentityError("runtime marker identity mismatch")
    try:
        marker = _json_object(marker_path.read_text(encoding="utf-8"), "runtime marker")
    except (UnicodeError, ProductionIdentityError) as exc:
        raise ProductionIdentityError("runtime marker is invalid") from exc
    expected_classification = "formal" if role is AuthorizationRole.PRODUCTION else "candidate-validation"
    if (not isinstance(marker, dict) or marker.get("runtime_id") != runtime_id
            or marker.get("module_id") != module_id
            or marker.get("classification") != expected_classification):
        raise ProductionIdentityError("runtime marker classification or identity mismatch")
    root = _runtime_path(request.runtime_root)
    if not root.startswith("/") or payload["runtime_root"] != root or not isinstance(payload["writable_roots"], list) or not isinstance(payload["protected_mounts"], list):
        raise ProductionIdentityError("runtime root contract is invalid")
    writable, protected = set(payload["writable_roots"]), set(payload["protected_mounts"])
    if not writable or writable & protected or any(not isinstance(v, str) or not v.startswith("/") for v in writable | protected) or any(not v.startswith(root + "/") for v in writable):
        raise ProductionIdentityError("runtime mount contract is invalid")
    if grant_v2:
        identity_roots = [item for item in manifest["runtime_roots"] if item["role"] == manifest["identity_root_role"]]
        expected_writable = {item["container_path"] for item in manifest["runtime_roots"] if item["access"] == "rw"}
        expected_readonly = {item["container_path"] for item in manifest["runtime_roots"] if item["access"] == "ro"}
        if (len(identity_roots) != 1 or identity_roots[0]["access"] != "ro"
                or identity_roots[0]["container_path"] != root or writable != expected_writable
                or not expected_readonly.issubset(protected)):
            raise ProductionIdentityError("signed mount roots differ from runtime manifest")
    if not isinstance(payload.get("hostname_nonce"), str) or payload["hostname_nonce"] != socket.gethostname() or not re.fullmatch(r"[0-9a-f]{32}", payload["hostname_nonce"]):
        raise ProductionIdentityError("runtime instance hostname mismatch")
    if not isinstance(payload.get("grant_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", payload["grant_id"]):
        raise ProductionIdentityError("execution grant id is invalid")
    issued, expires = _parse_time(payload["issued_at"], "grant issue time"), _parse_time(payload["expires_at"], "grant expiry")
    now = datetime.now(timezone.utc)
    if issued > now or expires <= now or expires <= issued or expires - issued > timedelta(hours=1):
        raise ProductionIdentityError("execution grant is not currently valid")
    if not isinstance(payload.get("container_id"), str) or not re.fullmatch(r"[0-9a-f]{64}", payload["container_id"]):
        raise ProductionIdentityError("execution grant container identity is invalid")
    mounts = _mount_options()
    if not hasattr(os, "geteuid"):
        raise ProductionIdentityError("OCI execution identity requires Linux runtime support")
    if "ro" not in mounts.get("/", set()) or os.geteuid() == 0:
        raise ProductionIdentityError("OCI runtime is not a non-root read-only filesystem")
    protected_paths = [Path(value) for value in protected]
    required_protected = [request.grant_path, TRUST_CONFIG_PATH, request.release_path, request.runtime_manifest_path, marker_path,
                          _ROOT / "02_configs", _ROOT / "03_src", _ROOT / "04_scripts", _ROOT / "05_apps"]
    if any(not any(_runtime_path(path) == value or _runtime_path(path).startswith(value.rstrip("/") + "/") for value in protected) for path in required_protected):
        raise ProductionIdentityError("protected mount contract omits identity material")
    if (any("ro" not in _mount_for(mounts, path) for path in protected_paths + required_protected)
            or any("rw" not in _mount_for(mounts, Path(value)) for value in writable)):
        raise ProductionIdentityError("OCI runtime mount permissions disagree with signed contract")
    if grant_v2:
        declared_targets = {item["container_path"] for item in manifest["required_mounts"]}
        if any(_within_mount(target, root) and target not in declared_targets for target in mounts):
            raise ProductionIdentityError("OCI runtime contains an undeclared overlay mount")
    if role is AuthorizationRole.CANDIDATE_VALIDATION and (payload["artifact_origin"] != "candidate" or (not grant_v2 and not root.startswith("/tmp/"))):
        raise ProductionIdentityError("candidate validation identity is not isolated")
    if role is AuthorizationRole.PRODUCTION and payload["artifact_origin"] not in {"candidate", "production"}:
        raise ProductionIdentityError("production grant artifact origin is invalid")
    return VerifiedExecutionIdentity(IdentityKind.OCI_CONTAINER, role, payload["project_id"], module_id, payload["service_id"], runtime_id, payload["approved_commit"], payload["approved_tree"], payload["image_id"], payload["grant_id"], tuple(Path(value) for value in writable))


def verify_execution(request: GitExecutionRequest | OCIExecutionRequest, *, expected_role: AuthorizationRole | str, module_id: str, runtime_id: str, runtime_root: Path, marker_sha256: str) -> VerifiedExecutionIdentity:
    """Verify source evidence on each call; no caller-provided 'verified' state exists."""
    try:
        expected_role = AuthorizationRole(expected_role)
    except (TypeError, ValueError) as exc:
        raise ProductionIdentityError("unknown expected authorization role") from exc
    _require_id(module_id, "expected module id")
    _require_id(runtime_id, "expected runtime id")
    _require_sha(marker_sha256, "expected runtime marker sha256")
    if isinstance(request, GitExecutionRequest):
        if expected_role is not AuthorizationRole.PRODUCTION:
            raise ProductionIdentityError("Git worktree identity only authorizes production role")
        return _verify_git(request, expected_role, module_id, runtime_id, runtime_root)
    if isinstance(request, OCIExecutionRequest):
        if request.runtime_root.resolve(strict=True) != runtime_root.resolve(strict=True):
            raise ProductionIdentityError("OCI request runtime root differs from caller runtime root")
        return _verify_oci(request, expected_role, module_id, runtime_id, marker_sha256)
    raise ProductionIdentityError("unknown execution identity request")


def verify_git_worktree(repository_root: Path, approved_commit: str, approved_tree: str) -> VerifiedExecutionIdentity:
    """Testable low-level Git verifier. Runtime authorization must use verify_execution."""
    return _verify_git(GitExecutionRequest(repository_root, approved_commit, approved_tree), AuthorizationRole.PRODUCTION, "runtime", "runtime", repository_root)
