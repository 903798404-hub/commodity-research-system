"""Host-side observation and validation for production execution grants.

This module deliberately does not trust container supplied identity claims.  All
observed values come from Docker's inspect API (and the immutable RELEASE file
copied from the inspected container).  Signing is kept out of the pure
validators so a caller cannot turn a hand-crafted mapping into authorization.
"""
from __future__ import annotations

import io
import importlib.util
import json
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import base64
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence


class HostAuthorizationError(ValueError):
    """Raised whenever host observations cannot establish the contract."""


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
TRUST_CONFIG_PATH = Path(__file__).resolve().parents[2] / "02_configs" / "production_runtime_trust.json"
SOURCE_ROOT = Path(__file__).resolve().parents[2]
GRANT_CONTRACT_PATH = SOURCE_ROOT / "03_src" / "agri_research_agent" / "shared" / "production_grant.py"
MANIFEST_CONTRACT_PATH = SOURCE_ROOT / "03_src" / "agri_research_agent" / "shared" / "runtime_manifest.py"
CANDIDATE_SCOPE_PARENT = "/tmp/market-data-candidate-scopes"


def _run_docker(args: Sequence[str], *, runner=None) -> bytes:
    command = ["docker", *args]
    if runner is not None:
        result = runner(command)
        if isinstance(result, str):
            return result.encode("utf-8")
        return bytes(result)
    try:
        result = subprocess.run(command, check=False, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostAuthorizationError("Docker command unavailable") from exc
    if result.returncode != 0:
        raise HostAuthorizationError(result.stderr.decode("utf-8", "replace").strip() or "Docker command failed")
    return result.stdout


def _container_id(value: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise HostAuthorizationError("container ID must be a complete 64-hex ID")
    return value


def docker_inspect(container_id: str, *, runner=None) -> dict[str, Any]:
    """Return exactly one actual Docker container inspection."""
    cid = _container_id(container_id)
    raw = _run_docker(["container", "inspect", cid], runner=runner)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HostAuthorizationError("docker inspect returned invalid JSON") from exc
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise HostAuthorizationError("docker inspect must return exactly one container")
    actual = value[0]
    if actual.get("Id") != cid:
        raise HostAuthorizationError("docker inspect container ID mismatch")
    return actual


def docker_image_inspect(image_id: str, *, runner=None) -> dict[str, Any]:
    """Inspect an immutable image ID, never a mutable tag."""
    if not isinstance(image_id, str) or not _IMAGE.fullmatch(image_id):
        raise HostAuthorizationError("image must be an immutable sha256 ID")
    raw = _run_docker(["image", "inspect", image_id], runner=runner)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HostAuthorizationError("docker image inspect returned invalid JSON") from exc
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise HostAuthorizationError("docker image inspect must return exactly one image")
    if value[0].get("Id") != image_id:
        raise HostAuthorizationError("image inspect ID mismatch")
    return value[0]


def copy_container_bytes(container_id: str, path: str = "/app/RELEASE.json", *, runner=None) -> bytes:
    """Read JSON from a real container via ``docker cp`` tar output.

    Using an archive avoids executing a container process and makes it
    impossible for a caller to substitute a host-side file.
    """
    cid = _container_id(container_id)
    if not isinstance(path, str) or not path.startswith("/") or ".." in path.split("/"):
        raise HostAuthorizationError("container path is unsafe")
    raw = _run_docker(["cp", f"{cid}:{path}", "-"], runner=runner)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
            members = [m for m in archive.getmembers() if m.isfile()]
            if len(members) != 1:
                raise HostAuthorizationError("docker cp archive must contain exactly one file")
            member = members[0]
            stream = archive.extractfile(member)
            if stream is None:
                raise HostAuthorizationError("cannot read copied container file")
            return stream.read()
    except tarfile.TarError as exc:
        raise HostAuthorizationError("container archive is invalid") from exc


def copy_container_json(container_id: str, path: str = "/app/RELEASE.json", *, runner=None) -> dict[str, Any]:
    try:
        value = json.loads(copy_container_bytes(container_id, path, runner=runner).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HostAuthorizationError("container RELEASE.json is invalid") from exc
    if not isinstance(value, dict):
        raise HostAuthorizationError("container RELEASE.json must be an object")
    return value



def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _json(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise HostAuthorizationError("duplicate JSON field")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise HostAuthorizationError("invalid identity JSON") from exc
    if not isinstance(value, dict):
        raise HostAuthorizationError("identity JSON must be an object")
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _absolute(value: str) -> str:
    from pathlib import PurePosixPath
    if not isinstance(value, str) or not value.startswith("/") or str(PurePosixPath(value)) != value or ".." in value.split("/") or "\\" in value:
        raise HostAuthorizationError("non-canonical container path")
    return value


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _env(values: list[str]) -> dict[str, str]:
    if not isinstance(values, list):
        raise HostAuthorizationError("invalid environment")
    result = {}
    for value in values:
        if not isinstance(value, str) or "=" not in value:
            raise HostAuthorizationError("invalid environment entry")
        key, content = value.split("=", 1)
        if not key or key in result:
            raise HostAuthorizationError("duplicate environment key")
        result[key] = content
    return result


def _mounts(container: Mapping) -> list[dict]:
    mounts = container.get("Mounts")
    if not isinstance(mounts, list):
        raise HostAuthorizationError("mount observations missing")
    result = []
    for item in mounts:
        if not isinstance(item, dict) or item.get("Type") != "bind" or type(item.get("RW")) is not bool:
            raise HostAuthorizationError("only explicit bind mounts are supported by authorization v1")
        result.append({"source": _absolute(item.get("Source")), "target": _absolute(item.get("Destination")), "read_only": not item["RW"]})
    if len({m["target"] for m in result}) != len(result):
        raise HostAuthorizationError("duplicate mount target")
    return sorted(result, key=lambda m: m["target"])


def normalize_observation(container: Mapping, image: Mapping, release: Mapping) -> dict:
    config, host = container.get("Config"), container.get("HostConfig")
    if not isinstance(config, dict) or not isinstance(host, dict):
        raise HostAuthorizationError("Docker configuration missing")
    labels = image.get("Config", {}).get("Labels") or {}
    return {
        "container_id": container.get("Id"), "image_id": container.get("Image"),
        "config": config, "host_config": host, "image_config": image.get("Config", {}),
        "image_labels": labels, "release_manifest": release,
        "mounts": _mounts(container), "state": container.get("State", {}),
        "actual_config_sha256": _digest({"config": config, "host_config": host, "path": container.get("Path"), "args": container.get("Args")}),
    }


_POLICY_V1_FIELDS = {
    "schema_version", "role", "key_id", "project_id", "module_id", "service_id", "runtime_id",
    "approved_commit", "approved_tree", "image_id", "artifact_service", "release_application",
    "source_root", "runtime_root", "runtime_manifest_path", "runtime_manifest_sha256",
    "runtime_marker_sha256", "release_sha256", "actual_config_sha256", "mounts",
    "compose_sources", "compose_project_directory", "compose_environment_file", "rendered_compose_sha256",
    "grant_container_directory", "candidate_host_root",
}
_POLICY_V2_FIELDS = _POLICY_V1_FIELDS | {"candidate_scope"}
_POLICY_V3_FIELDS = _POLICY_V2_FIELDS | {"candidate_record", "approved_source_root", "production_storage_root"}
_CANDIDATE_POLICIES = {"host-runtime-policy/2", "host-runtime-policy/4"}
_PRODUCTION_POLICIES = {"host-runtime-policy/3", "host-runtime-policy/5"}
_SOURCE_POLICIES = _CANDIDATE_POLICIES | _PRODUCTION_POLICIES
_V3_POLICIES = {"host-runtime-policy/4", "host-runtime-policy/5"}


def _manifest_version(version: str) -> str:
    return {"host-runtime-policy/1": "runtime-manifest/1",
            "host-runtime-policy/2": "runtime-manifest/2",
            "host-runtime-policy/3": "runtime-manifest/2",
            "host-runtime-policy/4": "runtime-manifest/3",
            "host-runtime-policy/5": "runtime-manifest/3"}[version]


def _contract_module(path: Path, name: str):
    """Load a trusted source contract without importing the application package."""
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise HostAuthorizationError("source contract cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        previous = sys.modules.get(name)
        sys.modules[name] = module
        try:
            exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        finally:
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        return module
    except (OSError, ImportError, AttributeError, TypeError, ValueError) as exc:
        raise HostAuthorizationError("source contract cannot be loaded") from exc


def validate_policy(policy: Mapping, role: str) -> None:
    version = policy.get("schema_version")
    expected_fields = {"host-runtime-policy/1": _POLICY_V1_FIELDS,
                       "host-runtime-policy/2": _POLICY_V2_FIELDS,
                       "host-runtime-policy/3": _POLICY_V3_FIELDS,
                       "host-runtime-policy/4": _POLICY_V2_FIELDS,
                       "host-runtime-policy/5": _POLICY_V3_FIELDS}.get(version, set())
    if set(policy) != expected_fields or policy.get("role") != role:
        raise HostAuthorizationError("protected policy schema or role mismatch")
    if role not in {"production", "candidate_validation"}:
        raise HostAuthorizationError("unknown authorization role")
    if version in _V3_POLICIES and policy["grant_container_directory"] != "/run/market-data-grants":
        raise HostAuthorizationError("v3 policy must use the reserved execution grant directory")
    if version in _CANDIDATE_POLICIES and role != "candidate_validation":
        raise HostAuthorizationError("policy/2 candidate scope cannot authorize production; policy/3 record required")
    if version in _PRODUCTION_POLICIES:
        if role != "production" or policy["candidate_scope"] is not None:
            raise HostAuthorizationError("policy/3 requires production without a candidate scope")
        record = policy["candidate_record"]
        if (type(record) is not dict or set(record) != {"path", "sha256"}
                or not isinstance(record["sha256"], str) or not _HEX64.fullmatch(record["sha256"])):
            raise HostAuthorizationError("production candidate record binding missing")
        _absolute(record["path"])
        _absolute(policy["approved_source_root"])
        storage = _absolute(policy["production_storage_root"])
        if not _within(storage, "/var/lib/market-data/production-runtime") or storage == "/var/lib/market-data/production-runtime":
            raise HostAuthorizationError("production storage must have an explicit isolated runtime allocation")
    for name in ("project_id", "module_id", "service_id", "runtime_id", "key_id", "artifact_service", "release_application"):
        if not isinstance(policy[name], str) or not re.fullmatch(r"[a-z][a-z0-9-]*", policy[name]):
            raise HostAuthorizationError("invalid policy identity")
    for name in ("approved_commit", "approved_tree"):
        if not isinstance(policy[name], str) or not _COMMIT.fullmatch(policy[name]):
            raise HostAuthorizationError("invalid Approved Git identity")
    for name in ("runtime_manifest_sha256", "runtime_marker_sha256", "release_sha256", "actual_config_sha256", "rendered_compose_sha256"):
        if not isinstance(policy[name], str) or not _HEX64.fullmatch(policy[name]):
            raise HostAuthorizationError("invalid expected identity hash")
    if not isinstance(policy["image_id"], str) or not _IMAGE.fullmatch(policy["image_id"]):
        raise HostAuthorizationError("policy must bind immutable image ID")
    for name in ("source_root", "runtime_root", "runtime_manifest_path", "grant_container_directory", "compose_project_directory", "compose_environment_file"):
        _absolute(policy[name])
    if not _within(policy["runtime_manifest_path"], policy["source_root"]):
        raise HostAuthorizationError("runtime manifest is outside source image")
    if not isinstance(policy["mounts"], list) or not isinstance(policy["compose_sources"], list) or not policy["compose_sources"]:
        raise HostAuthorizationError("deployment contract missing")
    mount_targets = set()
    for item in policy["mounts"]:
        if not isinstance(item, dict) or set(item) != {"source", "target", "read_only"} or type(item["read_only"]) is not bool:
            raise HostAuthorizationError("invalid mount policy")
        _absolute(item["source"])
        target = _absolute(item["target"])
        if target in mount_targets:
            raise HostAuthorizationError("duplicate mount target")
        mount_targets.add(target)
    source_paths = []
    for item in policy["compose_sources"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise HostAuthorizationError("invalid ordered Compose source")
        source_paths.append(_absolute(item["path"]))
        if not isinstance(item["sha256"], str) or not _HEX64.fullmatch(item["sha256"]):
            raise HostAuthorizationError("Compose source identity missing")
    if len(set(source_paths)) != len(source_paths):
        raise HostAuthorizationError("duplicate Compose source")
    if role == "candidate_validation" and version == "host-runtime-policy/1":
        if not _within(policy["runtime_root"], "/tmp") or policy["runtime_root"] == "/tmp":
            raise HostAuthorizationError("candidate container root is not temporary")
        _absolute(policy["candidate_host_root"])
        if not _within(policy["candidate_host_root"], "/tmp") or policy["candidate_host_root"] == "/tmp":
            raise HostAuthorizationError("candidate host root is not temporary")
    elif role == "production" and policy["candidate_host_root"] is not None:
        raise HostAuthorizationError("production cannot use a candidate root")
    if version in _CANDIDATE_POLICIES:
        scope = policy["candidate_scope"]
        if role == "production":
            if scope is not None:
                raise HostAuthorizationError("production cannot use a candidate scope")
        else:
            if not isinstance(scope, dict) or set(scope) != {"descriptor_path", "descriptor_sha256", "scope_id"}:
                raise HostAuthorizationError("candidate scope binding is incomplete")
            _absolute(scope["descriptor_path"])
            if not isinstance(scope["descriptor_sha256"], str) or not _HEX64.fullmatch(scope["descriptor_sha256"]):
                raise HostAuthorizationError("candidate scope descriptor identity is invalid")
            if not isinstance(scope["scope_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", scope["scope_id"]):
                raise HostAuthorizationError("candidate scope identity is invalid")
            _absolute(policy["candidate_host_root"])
            parent = CANDIDATE_SCOPE_PARENT
            descriptor_root = parent + "/.candidate-scope-descriptors"
            if (not _within(policy["candidate_host_root"], parent) or policy["candidate_host_root"] == parent
                    or not _within(scope["descriptor_path"], descriptor_root)):
                raise HostAuthorizationError("candidate scope is outside the fixed temporary authority root")


def validate_observation(observed: Mapping, expected: Mapping, *, role: str) -> dict:
    """Validate actual metadata without signing; mappings are never authority."""
    validate_policy(expected, role)
    config, host = observed["config"], observed["host_config"]
    if observed["image_id"] != expected["image_id"] or not _HEX64.fullmatch(str(observed["container_id"])):
        raise HostAuthorizationError("actual container/image identity mismatch")
    if observed["actual_config_sha256"] != expected["actual_config_sha256"]:
        raise HostAuthorizationError("actual configuration differs from approved deployment")
    if observed["state"].get("Status") != "created" or observed["state"].get("Running") is not False:
        raise HostAuthorizationError("grant requires a fresh, unstarted container")
    if not re.fullmatch(r"[1-9][0-9]*(?::[1-9][0-9]*)?", str(config.get("User", ""))):
        raise HostAuthorizationError("explicit non-root numeric user is required")
    if not re.fullmatch(r"[0-9a-f]{32}", str(config.get("Hostname", ""))):
        raise HostAuthorizationError("host-assigned random container nonce is required")
    if host.get("ReadonlyRootfs") is not True or host.get("Privileged") is not False:
        raise HostAuthorizationError("root filesystem or privilege policy rejected")
    security = host.get("SecurityOpt") or []
    if (host.get("CapAdd") or set(host.get("CapDrop") or []) != {"ALL"}
            or not any(s in security for s in ("no-new-privileges", "no-new-privileges:true"))
            or any("unconfined" in str(s) for s in security)
            or host.get("Devices") or host.get("DeviceRequests") or host.get("DeviceCgroupRules")):
        raise HostAuthorizationError("container capabilities/devices policy rejected")
    if (host.get("PidMode") not in (None, "") or host.get("IpcMode") not in (None, "", "private")
            or host.get("NetworkMode") == "host" or str(host.get("NetworkMode", "")).startswith("container:")
            or host.get("UTSMode") not in (None, "") or host.get("UsernsMode") not in (None, "")
            or host.get("VolumesFrom") or host.get("CgroupnsMode") == "host"):
        raise HostAuthorizationError("shared host/container namespace rejected")
    labels, release = observed["image_labels"], observed["release_manifest"]
    for label, value in {"org.opencontainers.image.revision": expected["approved_commit"], "market-data.git.tree": expected["approved_tree"], "market-data.service": expected["artifact_service"], "market-data.artifact.promotable": "true", "market-data.release.id": release.get("release_id")}.items():
        if not value or labels.get(label) != value:
            raise HostAuthorizationError("actual OCI release label mismatch")
    origin = labels.get("market-data.artifact.origin")
    if origin not in ({"candidate"} if role == "candidate_validation" else {"candidate", "production"}):
        raise HostAuthorizationError("artifact origin cannot authorize deployment role")
    if any(release.get(k) != v for k, v in {"git_commit": expected["approved_commit"], "git_tree": expected["approved_tree"], "application": expected["release_application"]}.items()):
        raise HostAuthorizationError("actual RELEASE differs from Approved identity")
    if observed["mounts"] != expected["mounts"]:
        raise HostAuthorizationError("actual mounts differ from deployment contract")
    source = expected["source_root"]
    # v2 source closures can intentionally omit legacy top-level directories.
    # Its signed source root is therefore the whole immutable image boundary;
    # even a read-only child bind could replace code that the image ID bound.
    immutable = ([source] if expected["schema_version"] in _SOURCE_POLICIES
                 else [source + "/" + name for name in ("03_src", "02_configs", "04_scripts", "05_apps", "RELEASE.json")])
    for mount in observed["mounts"]:
        target = mount["target"]
        if target == "/" or target == source or any(_within(target, path) or _within(path, target) for path in immutable):
            raise HostAuthorizationError("mount shadows immutable source identity")
    return dict(observed)


def _require_linux_root() -> None:
    if sys.platform != "linux" or os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise HostAuthorizationError("host authorization requires Linux root")


def _protected_path(path: Path, *, directory: bool = False, private: bool = False, temporary: bool = False) -> Path:
    _require_linux_root()
    if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path:
        raise HostAuthorizationError("protected path is missing, aliased or relative")
    for item in (path, *path.parents):
        state = item.stat()
        if temporary and item == Path("/tmp") and state.st_uid == 0 and stat.S_IMODE(state.st_mode) == 0o1777:
            continue
        if item.is_symlink() or state.st_uid != 0 or state.st_mode & 0o022:
            raise HostAuthorizationError("authorization path has an unprotected ancestor")
    state = path.stat()
    if directory != path.is_dir() or (not directory and not path.is_file()):
        raise HostAuthorizationError("authorization path has wrong type")
    if private and stat.S_IMODE(state.st_mode) != 0o600:
        raise HostAuthorizationError("private authorization file must have mode 0600")
    return path


def require_protected_key_and_grant_dirs(key_path: str | Path, grant_dir: str | Path) -> None:
    key = _protected_path(Path(key_path), private=True)
    grants = _protected_path(Path(grant_dir), directory=True)
    if stat.S_IMODE(grants.stat().st_mode) != 0o755 or grants in key.parents:
        raise HostAuthorizationError("grant directory must be public-readable, root-controlled and contain no private key")


def require_protected_authority_source() -> None:
    """Require the root-run signer and source contracts to be immutable to non-root users."""
    for path in (Path(__file__).resolve(strict=True), GRANT_CONTRACT_PATH, MANIFEST_CONTRACT_PATH, TRUST_CONFIG_PATH):
        _protected_path(path)


def _rfc3339(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value
    ):
        raise HostAuthorizationError(f"invalid {label}")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value[-1:] in {"Z", "z"} else value)
    except ValueError as exc:
        raise HostAuthorizationError(f"invalid {label}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HostAuthorizationError(f"invalid {label}")
    return parsed.astimezone(timezone.utc)


def _host_mount_points() -> tuple[str, ...]:
    def unescape(value: str) -> str:
        return re.sub(r"\\(040|011|012|134)", lambda match: {"040": " ", "011": "\t", "012": "\n", "134": "\\"}[match.group(1)], value)
    try:
        rows = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise HostAuthorizationError("host mount table is unavailable") from exc
    result = []
    for row in rows:
        pieces = row.split()
        if len(pieces) < 6 or "-" not in pieces:
            raise HostAuthorizationError("host mount table is invalid")
        result.append(_absolute(unescape(pieces[4])))
    return tuple(result)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_scope_tree(root: Path) -> None:
    """Reject aliases, special files, hardlinks, and nested host mounts."""
    resolved = root.resolve(strict=True)
    if resolved != root or root.is_symlink() or not root.is_dir():
        raise HostAuthorizationError("candidate scope root is missing or aliased")
    state = root.stat()
    if state.st_uid != 0 or stat.S_IMODE(state.st_mode) != 0o700:
        raise HostAuthorizationError("candidate scope root must be root-owned mode 0700")
    root_text = str(root)
    if any(_within(point, root_text) for point in _host_mount_points()):
        raise HostAuthorizationError("candidate scope contains a nested host mount")
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        for name in [*names, *files]:
            path = Path(directory) / name
            item = path.lstat()
            if stat.S_ISLNK(item.st_mode) or not (stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode)):
                raise HostAuthorizationError("candidate scope contains an alias or special file")
            if stat.S_ISREG(item.st_mode) and item.st_nlink != 1:
                raise HostAuthorizationError("candidate scope contains a hard-linked file")


def _candidate_descriptor(policy: Mapping, *, consume: bool) -> dict:
    binding = policy.get("candidate_scope")
    if policy.get("schema_version") not in _CANDIDATE_POLICIES or policy.get("role") != "candidate_validation" or not isinstance(binding, dict):
        raise HostAuthorizationError("candidate scope is unavailable")
    descriptor_path = _protected_path(Path(binding["descriptor_path"]), private=True, temporary=True)
    raw = descriptor_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != binding["descriptor_sha256"]:
        raise HostAuthorizationError("candidate scope descriptor differs from policy")
    descriptor = _json(raw)
    expected = {"schema_version", "scope_id", "created_at", "expires_at", "root", "binds"}
    if set(descriptor) != expected or descriptor.get("schema_version") != "candidate-scope/1" or descriptor.get("scope_id") != binding["scope_id"]:
        raise HostAuthorizationError("candidate scope descriptor schema is invalid")
    created, expires = _rfc3339(descriptor["created_at"], "candidate scope creation time"), _rfc3339(descriptor["expires_at"], "candidate scope expiry")
    now = datetime.now(timezone.utc)
    if created > now or expires <= now or expires - created > timedelta(hours=1):
        raise HostAuthorizationError("candidate scope is expired or exceeds its lifetime")
    root_record = descriptor.get("root")
    if not isinstance(root_record, dict) or set(root_record) != {"path", "device", "inode"}:
        raise HostAuthorizationError("candidate scope root identity is invalid")
    root = Path(_absolute(root_record["path"]))
    if str(root) != policy["candidate_host_root"]:
        raise HostAuthorizationError("candidate scope root differs from policy")
    _safe_scope_tree(root)
    root_state = root.stat()
    if type(root_record["device"]) is not int or type(root_record["inode"]) is not int or (root_record["device"], root_record["inode"]) != (root_state.st_dev, root_state.st_ino):
        raise HostAuthorizationError("candidate scope root inode changed")
    binds = descriptor.get("binds")
    if not isinstance(binds, list) or not binds:
        raise HostAuthorizationError("candidate scope bind identity is missing")
    actual = []
    targets = set()
    for item in binds:
        if not isinstance(item, dict) or set(item) != {"source", "device", "inode", "target", "read_only"} or type(item["read_only"]) is not bool:
            raise HostAuthorizationError("candidate scope bind identity is invalid")
        source = Path(_absolute(item["source"]))
        target = _absolute(item["target"])
        if target in targets or not _within(str(source), str(root)):
            raise HostAuthorizationError("candidate scope bind is duplicate or outside its root")
        targets.add(target)
        if source.is_symlink() or source.resolve(strict=True) != source or not (source.is_dir() or source.is_file()):
            raise HostAuthorizationError("candidate scope bind source is unsafe")
        source_state = source.stat()
        if type(item["device"]) is not int or type(item["inode"]) is not int or (item["device"], item["inode"]) != (source_state.st_dev, source_state.st_ino):
            raise HostAuthorizationError("candidate scope bind inode changed")
        if source.is_file() and source_state.st_nlink != 1:
            raise HostAuthorizationError("candidate scope bind is hard-linked")
        actual.append({"source": str(source), "target": target, "read_only": item["read_only"]})
    expected_mounts = [m for m in policy["mounts"] if m["target"] != policy["grant_container_directory"]]
    if sorted(actual, key=lambda item: item["target"]) != sorted(expected_mounts, key=lambda item: item["target"]):
        raise HostAuthorizationError("candidate scope descriptor differs from mount policy")
    if consume:
        receipt = descriptor_path.with_name(descriptor_path.name + ".consumed")
        try:
            fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(_canonical({"scope_id": binding["scope_id"], "consumed_at": now.isoformat()}))
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_directory(receipt.parent)
        except FileExistsError as exc:
            raise HostAuthorizationError("candidate scope was already consumed") from exc
    return descriptor


def create_candidate_scope(bindings: Sequence[Mapping], *, ttl_seconds: int = 3600) -> dict:
    """Create the only supported candidate bind-source scope as Linux root.

    ``bindings`` names directories to create below the fresh scope.  The
    returned values are policy inputs; callers cannot nominate pre-existing
    sources.  Mutable leaves may be owned by the numeric container user while
    the enclosing scope remains root-owned mode 0700.
    """
    from pathlib import PurePosixPath
    _require_linux_root()
    try:
        os.mkdir(CANDIDATE_SCOPE_PARENT, 0o700)
    except FileExistsError:
        pass
    parent = _protected_path(Path(CANDIDATE_SCOPE_PARENT), directory=True, temporary=True)
    if stat.S_IMODE(parent.stat().st_mode) != 0o700 or type(ttl_seconds) is not int or not 0 < ttl_seconds <= 3600:
        raise HostAuthorizationError("candidate scope parent or lifetime is invalid")
    if not isinstance(bindings, Sequence) or isinstance(bindings, (str, bytes)) or not bindings:
        raise HostAuthorizationError("candidate scope bindings are missing")
    scope_id = os.urandom(16).hex()
    root = Path(tempfile.mkdtemp(prefix=f"candidate-{scope_id}-", dir=parent))
    os.chmod(root, 0o700)
    records = []
    relative_names, targets = set(), set()
    try:
        for binding in bindings:
            required = {"relative_path", "target", "read_only", "owner_uid", "owner_gid"}
            if not isinstance(binding, Mapping) or set(binding) != required or type(binding["read_only"]) is not bool:
                raise HostAuthorizationError("candidate scope binding is invalid")
            relative = binding["relative_path"]
            pure = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath("/")
            if (not isinstance(relative, str) or not relative or pure.is_absolute() or str(pure) != relative
                    or ".." in pure.parts or relative in relative_names):
                raise HostAuthorizationError("candidate scope relative path is unsafe")
            uid, gid = binding["owner_uid"], binding["owner_gid"]
            if type(uid) is not int or type(gid) is not int or uid < 0 or gid < 0:
                raise HostAuthorizationError("candidate scope owner is invalid")
            target = _absolute(binding["target"])
            if target in targets:
                raise HostAuthorizationError("candidate scope target is duplicated")
            relative_names.add(relative)
            targets.add(target)
            source = root if relative == "." else root.joinpath(*pure.parts)
            if relative == ".":
                if (uid, gid) != (0, 0):
                    raise HostAuthorizationError("candidate scope root ownership cannot change")
            else:
                source.mkdir(parents=True, exist_ok=False)
                os.chown(source, uid, gid)
            state = source.stat()
            records.append({"source": str(source), "device": state.st_dev, "inode": state.st_ino, "target": target, "read_only": binding["read_only"]})
        descriptor_dir = parent / ".candidate-scope-descriptors"
        try:
            descriptor_dir.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _protected_path(descriptor_dir, directory=True, temporary=True)
        if stat.S_IMODE(descriptor_dir.stat().st_mode) != 0o700:
            raise HostAuthorizationError("candidate scope descriptor directory is unprotected")
        now = datetime.now(timezone.utc)
        root_state = root.stat()
        descriptor = {
            "schema_version": "candidate-scope/1", "scope_id": scope_id,
            "created_at": now.isoformat(), "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "root": {"path": str(root), "device": root_state.st_dev, "inode": root_state.st_ino},
            "binds": records,
        }
        raw = _canonical(descriptor)
        descriptor_path = descriptor_dir / f"{scope_id}.json"
        fd = os.open(descriptor_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(descriptor_path.parent)
        return {
            "candidate_host_root": str(root),
            "candidate_scope": {"descriptor_path": str(descriptor_path), "descriptor_sha256": hashlib.sha256(raw).hexdigest(), "scope_id": scope_id},
            "mounts": [{"source": item["source"], "target": item["target"], "read_only": item["read_only"]} for item in records],
        }
    except Exception:
        # The parent is protected and freshly allocated, so recursive cleanup
        # cannot escape it.  Failed creation never yields a usable descriptor.
        import shutil
        shutil.rmtree(root, ignore_errors=True)
        raise


def _load_policy(path: str | Path) -> dict:
    return _json(_protected_path(Path(path), private=True).read_bytes())


def _load_private_key(path: str | Path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    try:
        key = load_pem_private_key(_protected_path(Path(path), private=True).read_bytes(), password=None)
    except (ValueError, TypeError) as exc:
        raise HostAuthorizationError("invalid private signing key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise HostAuthorizationError("only Ed25519 signing keys are supported")
    return key


def _validate_mount_sources(observed: Mapping, policy: Mapping, grant_dir: Path) -> None:
    grant_target = policy["grant_container_directory"]
    if {"source": str(grant_dir), "target": grant_target, "read_only": True} not in observed["mounts"]:
        raise HostAuthorizationError("protected grant directory is not injected read-only")
    for mount in observed["mounts"]:
        source = Path(mount["source"])
        if not source.exists() or source.is_symlink() or source.resolve(strict=True) != source or (not source.is_dir() and not source.is_file()):
            raise HostAuthorizationError("mount source is aliased, missing or a control socket/device")
        if source == grant_dir:
            if mount["target"] != grant_target or mount["read_only"] is not True:
                raise HostAuthorizationError("grant source cannot be mounted under another role")
            continue
        if policy["role"] == "candidate_validation" and not _within(str(source), policy["candidate_host_root"]):
            raise HostAuthorizationError("candidate mount source is outside protected temporary root")
        if policy["schema_version"] in _PRODUCTION_POLICIES:
            storage = _protected_path(Path(policy["production_storage_root"]), directory=True)
            if not mount["target"].startswith("/run/secrets/") and not _within(str(source), str(storage)):
                raise HostAuthorizationError("production mount is outside its approved storage allocation")
            if any((parent / ".git").exists() for parent in (source, *source.parents) if parent.is_dir()):
                raise HostAuthorizationError("production cannot mount a development Git checkout")
        if source == Path("/") or any(_within(str(source), root) or _within(root, str(source)) for root in ("/var/run", "/run", "/var/lib/docker", "/proc", "/sys", "/dev", "/root")):
            raise HostAuthorizationError("control or host system directory mount rejected")


def _validated_candidate_record(policy: Mapping) -> tuple[dict, dict]:
    """Bind an authenticated validation fact to this exact Approved source."""
    from types import SimpleNamespace
    if policy["approved_source_root"] != str(SOURCE_ROOT):
        raise HostAuthorizationError("signer must run from the exact Approved source")
    for relative in ("04_scripts/runtime/pre_release_runtime.py", "04_scripts/runtime/validate_target_runtime.py",
                     "09_deploy/runtime_identity/candidate_validation_record.py"):
        _protected_path(SOURCE_ROOT / relative)
    entry = _contract_module(SOURCE_ROOT / "04_scripts/runtime/pre_release_runtime.py", "_host_pre_release")
    engine = _contract_module(SOURCE_ROOT / "04_scripts/runtime/validate_target_runtime.py", "_host_release_engine")
    identity = entry.require_source(SimpleNamespace(_require_linux_root=_require_linux_root,
                                                    _protected_path=_protected_path), engine)
    if identity != (policy["approved_commit"], policy["approved_tree"]):
        raise HostAuthorizationError("Approved source Commit/Tree differs")
    engine.require_builder()
    record_binding = policy["candidate_record"]
    raw = _protected_path(Path(record_binding["path"]), private=True).read_bytes()
    if hashlib.sha256(raw).hexdigest() != record_binding["sha256"]:
        raise HostAuthorizationError("candidate record content differs from approval")
    parser = _contract_module(SOURCE_ROOT / "09_deploy/runtime_identity/candidate_validation_record.py", "_host_candidate_record")
    payload = parser.verify_record(raw, _json(TRUST_CONFIG_PATH.read_bytes()))
    if "evidence_bundle_sha256" in payload:
        codec = _contract_module(SOURCE_ROOT / "09_deploy/runtime_identity/candidate_evidence.py", "_host_candidate_bundle")
        directory = Path(record_binding["path"]).with_name(Path(record_binding["path"]).name + ".evidence")
        bundle = codec.verify_bundle(_protected_path(directory / "bundle.json", private=True).read_bytes(),
                                     payload, directory, protected=lambda p: _protected_path(p, private=True))
        cleanup = _json(_protected_path(directory / "cleanup.json", private=True).read_bytes())
        result = _json(_protected_path(directory / "lifecycle-result.json", private=True).read_bytes())
        if (set(result) != {"status", "identity", "sealed", "timestamp", "failure_type"}
                or result["status"] != "PASS" or result["sealed"] is not True
                or result["identity"] != bundle["identity"] or result["failure_type"] is not None):
            raise HostAuthorizationError("candidate lifecycle did not complete successfully")
        if (set(cleanup) != {"status", "identity", "image_retained", "timestamp"} or cleanup["status"] != "PASS"
                or cleanup["identity"] != bundle["identity"] or cleanup["image_retained"] is not True):
            raise HostAuthorizationError("candidate cleanup not complete")
        image = docker_image_inspect(payload["evidence"]["image_id"])
        if image["Config"]["Labels"].get("market-data.release.id") != payload["release_id"]:
            raise HostAuthorizationError("candidate release image differs")
    project = engine._project(SOURCE_ROOT, policy["project_id"])
    _, manifest, binding = engine.source_contract(SOURCE_ROOT, policy["project_id"], project["runtime_contract"])
    if manifest.get("schema_version") != _manifest_version(policy["schema_version"]):
        raise HostAuthorizationError("candidate source contract manifest version differs from policy")
    evidence = payload["evidence"]
    if (evidence["binding"] != binding or evidence["image_id"] != policy["image_id"]
            or binding["source_sha256"][project["runtime_contract"]] != policy["runtime_manifest_sha256"]
            or policy["runtime_manifest_path"] != policy["source_root"] + "/" + project["runtime_contract"]):
        raise HostAuthorizationError("candidate validation does not bind the production target")
    engine.validate_source_compose(SOURCE_ROOT, manifest)
    return payload, manifest


def _production_compose_bridge(rendered: Mapping, policy: Mapping, manifest: Mapping) -> list[dict]:
    """Resolve approved source Compose with production inputs; allow no hidden overrides."""
    name = rendered.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise HostAuthorizationError("actual Compose project name is invalid")
    command = ["compose", "--project-name", name, "--project-directory", str(SOURCE_ROOT),
               "--env-file", policy["compose_environment_file"]]
    for relative in manifest["build"]["compose_sources"]:
        command.extend(["-f", str(_protected_path(SOURCE_ROOT / relative))])
    desired = _json(_run_docker([*command, "config", "--format", "json"]))
    # A deployment may remove build metadata and add its host nonce. Every
    # other rendered field must equal the committed contract with the same env.
    actual = _json(_canonical(rendered))
    for document in (desired, actual):
        if set(document.get("services", {})) != {policy["service_id"]}:
            raise HostAuthorizationError("production Compose must contain only the target service")
    wanted_service = desired["services"][policy["service_id"]]
    actual_service = actual["services"][policy["service_id"]]
    if actual_service.get("build") not in (None, wanted_service.get("build")):
        raise HostAuthorizationError("production build metadata differs from source contract")
    nonce = actual_service.get("hostname")
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise HostAuthorizationError("production Compose must bind the host nonce")
    if "hostname" in wanted_service and wanted_service["hostname"] != nonce:
        raise HostAuthorizationError("source hostname differs from production nonce")
    for service in (wanted_service, actual_service):
        service.pop("build", None)
        service.pop("hostname", None)
    if desired != actual:
        raise HostAuthorizationError("production Compose contains undeclared source-contract differences")
    if (actual_service.get("entrypoint") != manifest["entrypoint"]
            or actual_service.get("working_dir") != manifest["working_directory"]
            or not set(manifest["required_environment"]).issubset(actual_service.get("environment", {}))):
        raise HostAuthorizationError("production entrypoint/environment differs from manifest")
    references = actual_service.get("secrets") or []
    definitions = actual.get("secrets") or {}
    if (type(references) is not list or any(type(item) is not dict for item in references)
            or {item.get("source") for item in references} != set(manifest["secret_references"])
            or len(references) != len(manifest["secret_references"])
            or type(definitions) is not dict or set(definitions) != set(manifest["secret_references"])):
        raise HostAuthorizationError("production secrets differ from manifest")
    mounts = []
    for item in references:
        target = item.get("target")
        definition = definitions[item["source"]]
        if (not isinstance(target, str) or not re.fullmatch(r"/run/secrets/[A-Za-z0-9][A-Za-z0-9._-]*", target)
                or set(item) != {"source", "target"} or type(definition) is not dict
                or set(definition) - {"file", "name"} or not isinstance(definition.get("file"), str)):
            raise HostAuthorizationError("only exact file-secret references are supported")
        source = _protected_path(Path(definition["file"]))
        _secret_file_identity(source, actual_service.get("user"))
        mounts.append({"source": str(source), "target": target, "read_only": True})
    if len({item["target"] for item in mounts}) != len(mounts):
        raise HostAuthorizationError("duplicate production secret target")
    return mounts


def _secret_file_identity(source: Path, user: str) -> dict:
    """Observe a protected file without publishing its contents or credentials."""
    _protected_path(source)
    if not isinstance(user, str) or not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", user):
        raise HostAuthorizationError("file-secret access requires explicit numeric user and group")
    uid, gid = map(int, user.split(":"))
    before = source.stat()
    mode = stat.S_IMODE(before.st_mode)
    readable = bool(mode & (0o400 if before.st_uid == uid else 0o040 if before.st_gid == gid else 0o004))
    if not readable or mode & 0o007:
        raise HostAuthorizationError("secret must be readable by its runtime identity, not world accessible")
    raw = source.read_bytes()
    after = source.stat()
    fields = ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    identity = {name: getattr(before, name) for name in fields}
    if identity != {name: getattr(after, name) for name in fields}:
        raise HostAuthorizationError("secret file changed while observed")
    return {**identity, "sha256": hashlib.sha256(raw).hexdigest()}


def _secret_state(observed: Mapping) -> dict:
    return {mount["target"]: _secret_file_identity(Path(mount["source"]), observed["config"].get("User"))
            for mount in observed["mounts"] if mount["target"].startswith("/run/secrets/")}


def _render_actual_compose(container: Mapping, policy: Mapping, image: Mapping) -> str:
    labels = container["Config"].get("Labels") or {}
    paths = labels.get("com.docker.compose.project.config_files", "").split(",")
    if paths != [item["path"] for item in policy["compose_sources"]] or labels.get("com.docker.compose.project.working_dir") != policy["compose_project_directory"] or labels.get("com.docker.compose.service") != policy["service_id"]:
        raise HostAuthorizationError("actual Compose source differs from protected deployment policy")
    command = ["compose", "--project-directory", policy["compose_project_directory"], "--env-file", policy["compose_environment_file"]]
    _protected_path(Path(policy["compose_environment_file"]), private=True)
    if policy["schema_version"] in _SOURCE_POLICIES:
        _protected_path(Path(policy["compose_project_directory"]), directory=True)
    for path, expected_source in zip(paths, policy["compose_sources"]):
        file = Path(path)
        if policy["schema_version"] in _SOURCE_POLICIES:
            _protected_path(file)
        if file.is_symlink() or file.resolve(strict=True) != file or hashlib.sha256(file.read_bytes()).hexdigest() != expected_source["sha256"]:
            raise HostAuthorizationError("actual Compose source content changed")
        command.extend(["-f", path])
    rendered = _json(_run_docker([*command, "config", "--format", "json"]))
    digest = _digest(rendered)
    if digest != policy["rendered_compose_sha256"]:
        raise HostAuthorizationError("actual rendered Compose differs from approval")
    service = rendered.get("services", {}).get(policy["service_id"])
    if not isinstance(service, dict) or service.get("image") != policy["image_id"]:
        raise HostAuthorizationError("rendered service must use the approved immutable image")
    actual, defaults = container["Config"], image["Config"]
    for rendered_key, inspect_key in (("entrypoint", "Entrypoint"), ("command", "Cmd"), ("working_dir", "WorkingDir"), ("user", "User"), ("hostname", "Hostname")):
        value = service.get(rendered_key, defaults.get(inspect_key))
        if value != actual.get(inspect_key):
            raise HostAuthorizationError("rendered entrypoint/working directory/user/hostname differs from actual container")
    environment = _env(defaults.get("Env") or [])
    extra = service.get("environment") or {}
    if not isinstance(extra, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in extra.items()):
        raise HostAuthorizationError("rendered environment is unresolved")
    environment.update(extra)
    if environment != _env(actual.get("Env") or []):
        raise HostAuthorizationError("rendered environment differs from actual container")
    volumes = service.get("volumes") or []
    if any(not isinstance(v, dict) or v.get("type") != "bind" for v in volumes):
        raise HostAuthorizationError("rendered mounts must be explicit bind contracts")
    expected_mounts = sorted([{"source": v.get("source"), "target": v.get("target"), "read_only": v.get("read_only", False)} for v in volumes], key=lambda m: m["target"])
    if policy["schema_version"] in _PRODUCTION_POLICIES:
        _, manifest = _validated_candidate_record(policy)
        expected_mounts = sorted([*expected_mounts, *_production_compose_bridge(rendered, policy, manifest)], key=lambda m: m["target"])
    if expected_mounts != _mounts(container):
        raise HostAuthorizationError("rendered mounts differ from actual container")
    return digest


def _validate_container_layer(container_id: str, mounts: list[dict], *, runner=None) -> None:
    # ReadonlyRootfs governs execution, but Docker can modify a stopped
    # container's writable layer. Image ID alone cannot detect that mutation.
    raw = _run_docker(["diff", container_id], runner=runner).decode("utf-8", "strict")
    for line in raw.splitlines():
        if len(line) < 3 or line[1] != " " or line[0] not in {"A", "C"}:
            raise HostAuthorizationError("container layer differs from the approved image")
        path = _absolute(line[2:])
        if not any(_within(path, item["target"]) or _within(item["target"], path) for item in mounts):
            raise HostAuthorizationError("container layer modifies immutable image content")


def observe_and_validate(container_id: str, expected: Mapping, *, role: str, runner=None) -> dict:
    container = docker_inspect(container_id, runner=runner)
    image = docker_image_inspect(container.get("Image"), runner=runner)
    raw = copy_container_bytes(container_id, expected["source_root"] + "/RELEASE.json", runner=runner)
    observed = normalize_observation(container, image, _json(raw))
    _validate_container_layer(container_id, observed["mounts"], runner=runner)
    observed["release_sha256"] = hashlib.sha256(raw).hexdigest()
    if observed["release_sha256"] != expected["release_sha256"]:
        raise HostAuthorizationError("actual RELEASE bytes differ from approval")
    return validate_observation(observed, expected, role=role)


def _validate_runtime_mounts(manifest: Mapping, mounts: list[dict], policy: Mapping) -> None:
    required = manifest.get("required_mounts")
    roots = manifest.get("runtime_roots")
    if not isinstance(required, list) or not isinstance(roots, list):
        raise HostAuthorizationError("runtime manifest mount contract missing")
    by_role = {}
    for item in roots:
        if not isinstance(item, dict) or set(item) != {"role", "container_path", "access"} or item["role"] in by_role or item["access"] not in {"ro", "rw"}:
            raise HostAuthorizationError("invalid runtime root contract")
        by_role[item["role"]] = item
    targets = set()
    for item in required:
        if not isinstance(item, dict) or set(item) != {"role", "container_path", "read_only"} or type(item["read_only"]) is not bool:
            raise HostAuthorizationError("invalid required mount contract")
        root = by_role.pop(item["role"], None)
        target = _absolute(item["container_path"])
        if root is None or root["container_path"] != target or (root["access"] == "ro") != item["read_only"] or target in targets:
            raise HostAuthorizationError("runtime roots and required mounts disagree")
        targets.add(target)
        actual = [m for m in mounts if m["target"] == target]
        if len(actual) != 1 or actual[0]["read_only"] != item["read_only"]:
            raise HostAuthorizationError("actual mount permission differs from runtime manifest")
    # For policy/3 the source/render bridge has already proved the exact secret
    # names, file sources and targets against the Approved manifest and inspect.
    secrets = [m for m in mounts if m["target"].startswith("/run/secrets/")] if policy["schema_version"] in _PRODUCTION_POLICIES else []
    if secrets and (len(secrets) != len(manifest["secret_references"]) or any(m["read_only"] is not True for m in secrets)):
        raise HostAuthorizationError("production secret mount permissions differ")
    allowed = targets | {policy["grant_container_directory"]} | {m["target"] for m in secrets}
    if by_role or any(m["target"] not in allowed for m in mounts):
        raise HostAuthorizationError("undeclared runtime mount")


def _validate_v3_runtime(manifest: Mapping, observed: Mapping, policy: Mapping, container_id: str) -> None:
    if manifest.get("schema_version") != _manifest_version(policy["schema_version"]):
        raise HostAuthorizationError("policy and executing manifest versions differ")
    if policy["schema_version"] not in _V3_POLICIES:
        return
    grant_contract = _contract_module(GRANT_CONTRACT_PATH, "_host_runtime_environment_contract")
    try:
        grant_contract.validate_runtime_environment(
            manifest, _env(observed["config"].get("Env") or []), role=policy["role"],
            grant_path=policy["grant_container_directory"] + "/grant.json")
    except (AttributeError, TypeError, ValueError) as exc:
        raise HostAuthorizationError("actual runtime environment violates manifest") from exc
    # Only candidate temporary mounts may contain test seed bytes. Production
    # storage is independently approved and must never be compared to fixtures.
    if policy["role"] != "candidate_validation":
        return
    roots = {item["role"]: item for item in manifest["runtime_roots"]}
    total = 0
    for item in manifest["candidate_runtime_inputs"]:
        target_root = roots[item["role"]]["container_path"]
        mounts = [mount for mount in observed["mounts"] if mount["target"] == target_root]
        if len(mounts) != 1 or mounts[0]["read_only"] is not True:
            raise HostAuthorizationError("candidate seed must have its exact readonly mount")
        mount_source = _protected_path(Path(mounts[0]["source"]), directory=True, temporary=True)
        source = _protected_path(mount_source / item["relative_path"], temporary=True)
        try:
            source.relative_to(mount_source)
        except ValueError as exc:
            raise HostAuthorizationError("candidate seed escapes its declared mount source") from exc
        total += source.stat().st_size
        if total > 64 * 1024 * 1024:
            raise HostAuthorizationError("candidate seed inputs exceed size limit")
        raw = source.read_bytes()
        if (hashlib.sha256(raw).hexdigest() != item["sha256"]
                or copy_container_bytes(container_id, target_root + "/" + item["relative_path"]) != raw):
            raise HostAuthorizationError("candidate readonly seed identity differs")


def revalidate_production(container_id: str, *, expected_policy_path: str | Path) -> dict:
    """Read-only pre-approval validation; never sign a grant or start a container."""
    _require_linux_root()
    require_protected_authority_source()
    policy = _load_policy(expected_policy_path)
    validate_policy(policy, "production")
    if policy["schema_version"] not in _PRODUCTION_POLICIES:
        raise HostAuthorizationError("pre-release validation requires production policy/3")
    record, source_manifest = _validated_candidate_record(policy)
    observed = observe_and_validate(container_id, policy, role="production")
    grants = [m for m in observed["mounts"] if m["target"] == policy["grant_container_directory"]]
    if len(grants) != 1 or grants[0]["read_only"] is not True:
        raise HostAuthorizationError("pre-release grant mount is missing or writable")
    grant_dir = _protected_path(Path(grants[0]["source"]), directory=True)
    _validate_mount_sources(observed, policy, grant_dir)
    container = docker_inspect(container_id)
    image = docker_image_inspect(policy["image_id"])
    rendered = _render_actual_compose(container, policy, image)
    manifest_raw = copy_container_bytes(container_id, policy["runtime_manifest_path"])
    manifest = _json(manifest_raw)
    marker_path = policy["runtime_root"] + "/.market-data-runtime.json"
    marker_raw = copy_container_bytes(container_id, marker_path)
    marker = _json(marker_raw)
    trust_raw = TRUST_CONFIG_PATH.read_bytes()
    if (manifest != source_manifest or hashlib.sha256(manifest_raw).hexdigest() != policy["runtime_manifest_sha256"]
            or hashlib.sha256(marker_raw).hexdigest() != policy["runtime_marker_sha256"]
            or copy_container_bytes(container_id, policy["source_root"] + "/02_configs/production_runtime_trust.json") != trust_raw):
        raise HostAuthorizationError("pre-release executing identity material differs from approval")
    for field in ("project_id", "module_id", "service_id"):
        if manifest[field] != policy[field]:
            raise HostAuthorizationError("pre-release manifest role identity differs")
    identity_roots = [item for item in manifest["runtime_roots"] if item["role"] == manifest["identity_root_role"]]
    if (len(identity_roots) != 1 or identity_roots[0]["access"] != "ro"
            or identity_roots[0]["container_path"] != policy["runtime_root"]
            or marker.get("classification") != "formal" or marker.get("module_id") != policy["module_id"]
            or marker.get("runtime_id") != policy["runtime_id"]):
        raise HostAuthorizationError("pre-release runtime identity root is not the approved formal root")
    _validate_runtime_mounts(manifest, observed["mounts"], policy)
    _validate_v3_runtime(manifest, observed, policy, container_id)
    writable = [m["target"] for m in observed["mounts"] if not m["read_only"] and _within(m["target"], policy["runtime_root"])]
    if not writable or policy["runtime_root"] in writable:
        raise HostAuthorizationError("pre-release requires explicit writable children under a read-only identity root")
    secrets = _secret_state(observed)
    if observe_and_validate(container_id, policy, role="production") != observed:
        raise HostAuthorizationError("production instance changed during pre-release validation")
    _validate_mount_sources(observed, policy, grant_dir)
    if _render_actual_compose(docker_inspect(container_id), policy, docker_image_inspect(policy["image_id"])) != rendered:
        raise HostAuthorizationError("production Compose changed during pre-release validation")
    if (_load_policy(expected_policy_path) != policy or _validated_candidate_record(policy) != (record, source_manifest)
            or TRUST_CONFIG_PATH.read_bytes() != trust_raw
            or copy_container_bytes(container_id, policy["runtime_manifest_path"]) != manifest_raw
            or copy_container_bytes(container_id, marker_path) != marker_raw
            or copy_container_bytes(container_id, policy["source_root"] + "/02_configs/production_runtime_trust.json") != trust_raw
            or _secret_state(observed) != secrets):
        raise HostAuthorizationError("pre-release authority material changed")
    return {"schema_version": "production-pre-release-validation/1", "PRE_RELEASE_VALIDATION": "PASS",
            "production_write_granted": False, "container_started": False,
            "validated_at": datetime.now(timezone.utc).isoformat(), "container_id": container_id,
            "project_id": policy["project_id"], "approved_commit": policy["approved_commit"],
            "approved_tree": policy["approved_tree"], "image_id": policy["image_id"],
            "policy_sha256": _digest(policy), "candidate_record_sha256": policy["candidate_record"]["sha256"],
            "candidate_rendered_compose_sha256": record["evidence"]["rendered_compose_sha256"],
            "production_rendered_compose_sha256": rendered,
            "runtime_manifest_sha256": policy["runtime_manifest_sha256"], "release_sha256": observed["release_sha256"],
            "mount_contract_sha256": _digest(observed["mounts"]), "actual_config_sha256": observed["actual_config_sha256"],
            "secret_file_identity_sha256": _digest(secrets)}


def issue_execution_grant(container_id: str, *, expected_policy_path: str | Path, key_path: str | Path, grant_path: str | Path, grant_dir: str | Path, role: str, ttl_seconds: int = 900) -> dict:
    """Observe a fresh container, sign a bounded grant, and leave it unstarted."""
    _require_linux_root()
    require_protected_authority_source()
    policy = _load_policy(expected_policy_path)
    validate_policy(policy, role)
    require_protected_key_and_grant_dirs(key_path, grant_dir)
    if type(ttl_seconds) is not int or not 0 < ttl_seconds <= 3600:
        raise HostAuthorizationError("invalid grant lifetime")
    grant_dir, destination = Path(grant_dir), Path(grant_path)
    if not destination.is_absolute() or destination.parent != grant_dir or destination.exists() or destination.is_symlink():
        raise HostAuthorizationError("grant path must be new and inside its protected directory")
    policy_version = policy["schema_version"]
    candidate_record = _validated_candidate_record(policy) if policy_version in _PRODUCTION_POLICIES else None
    if policy_version in _CANDIDATE_POLICIES and role == "candidate_validation":
        # Consumption deliberately precedes every fallible Docker observation.
        # A failed attempt is not replayable with a mutated scope.
        _candidate_descriptor(policy, consume=True)
    observed = observe_and_validate(container_id, policy, role=role)
    secret_state = _secret_state(observed) if policy_version in _PRODUCTION_POLICIES else None
    _validate_mount_sources(observed, policy, grant_dir)
    if role == "candidate_validation":
        _protected_path(Path(policy["candidate_host_root"]), directory=True, temporary=True)
    container = docker_inspect(container_id)
    image = docker_image_inspect(policy["image_id"])
    rendered_digest = _render_actual_compose(container, policy, image)
    marker_path = policy["runtime_root"] + "/.market-data-runtime.json"
    manifest_raw = copy_container_bytes(container_id, policy["runtime_manifest_path"])
    marker_raw = copy_container_bytes(container_id, marker_path)
    marker = _json(marker_raw)
    if hashlib.sha256(manifest_raw).hexdigest() != policy["runtime_manifest_sha256"] or hashlib.sha256(marker_raw).hexdigest() != policy["runtime_marker_sha256"]:
        raise HostAuthorizationError("actual runtime manifest/marker differs from approval")
    manifest = _json(manifest_raw)
    if policy_version in _SOURCE_POLICIES:
        try:
            manifest_contract = _contract_module(MANIFEST_CONTRACT_PATH, "_market_data_runtime_manifest_host_contract")
            manifest_object = manifest_contract.parse_runtime_manifest(manifest)
            manifest = manifest_object.to_dict()
        except (AttributeError, TypeError, ValueError) as exc:
            raise HostAuthorizationError("runtime manifest violates its source contract") from exc
    for key in ("project_id", "module_id", "service_id"):
        if manifest.get(key) != policy[key]:
            raise HostAuthorizationError("runtime manifest identity mismatch")
    expected_manifest_version = _manifest_version(policy_version)
    if manifest.get("identity_kind") != "oci_container" or manifest.get("runtime_target") != "production_container" or manifest.get("schema_version") != expected_manifest_version:
        raise HostAuthorizationError("runtime manifest identity contract is missing")
    identity_root_role = manifest.get("identity_root_role")
    if policy_version in _SOURCE_POLICIES:
        identity_roots = [item for item in manifest["runtime_roots"] if item["role"] == identity_root_role]
        if len(identity_roots) != 1 or identity_roots[0]["access"] != "ro" or identity_roots[0]["container_path"] != policy["runtime_root"]:
            raise HostAuthorizationError("policy runtime root differs from manifest identity root")
    classification = "formal" if role == "production" else "candidate-validation"
    if marker.get("classification") != classification or marker.get("module_id") != policy["module_id"] or marker.get("runtime_id") != policy["runtime_id"]:
        raise HostAuthorizationError("runtime marker cannot authorize this deployment role")
    mounts = observed["mounts"]
    _validate_runtime_mounts(manifest, mounts, policy)
    _validate_v3_runtime(manifest, observed, policy, container_id)
    writable = [m["target"] for m in mounts if not m["read_only"] and _within(m["target"], policy["runtime_root"])]
    if not writable or any(target == policy["runtime_root"] for target in writable):
        raise HostAuthorizationError("runtime marker root must remain read-only with explicit writable children")
    protected = ([policy["source_root"], policy["grant_container_directory"], marker_path]
                 if policy_version == "host-runtime-policy/1"
                 else [policy["source_root"], policy["grant_container_directory"],
                       *[item["container_path"] for item in manifest["runtime_roots"] if item["access"] == "ro"]])
    if policy_version in _PRODUCTION_POLICIES:
        protected.extend(m["target"] for m in mounts if m["target"].startswith("/run/secrets/"))
    def writable_cover(path):
        matches = [m for m in mounts if _within(path, m["target"])]
        return bool(matches and not max(matches, key=lambda m: len(m["target"]))["read_only"])
    if any(writable_cover(path) for path in protected):
        raise HostAuthorizationError("identity material is writable")
    trust_raw = TRUST_CONFIG_PATH.read_bytes()
    if copy_container_bytes(container_id, policy["source_root"] + "/02_configs/production_runtime_trust.json") != trust_raw:
        raise HostAuthorizationError("host and executing image trust anchors differ")
    trust = _json(trust_raw)
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    key = _load_private_key(key_path)
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode("ascii")
    matches = [item for item in trust.get("keys", []) if item.get("key_id") == policy["key_id"] and item.get("domain") == role and item.get("algorithm") == "ed25519" and item.get("public_key_base64") == public]
    if len(matches) != 1 or policy["key_id"] in trust.get("revoked_key_ids", []):
        raise HostAuthorizationError("host signing key is not pinned for this authorization role")
    now = datetime.now(timezone.utc)
    payload = {key: policy[key] for key in ("project_id", "module_id", "service_id", "runtime_id", "approved_commit", "approved_tree", "image_id", "runtime_root", "runtime_manifest_sha256", "runtime_marker_sha256")}
    payload.update(grant_id=os.urandom(16).hex(), issued_at=now.isoformat(), expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(), identity_kind="oci_container", authorization_mode=role, role=role, artifact_origin=observed["image_labels"]["market-data.artifact.origin"], release_commit=policy["approved_commit"], release_tree=policy["approved_tree"], release_sha256=observed["release_sha256"], rendered_compose_sha256=rendered_digest, mount_contract_sha256=_digest(mounts), actual_config_sha256=observed["actual_config_sha256"], container_id=container_id, hostname_nonce=observed["config"]["Hostname"], writable_roots=writable, protected_mounts=protected)
    grant_version = ("production-execution-grant/3" if policy_version in _V3_POLICIES else
                     "production-execution-grant/1" if policy_version == "host-runtime-policy/1" else "production-execution-grant/2")
    if grant_version in {"production-execution-grant/2", "production-execution-grant/3"}:
        scope = policy["candidate_scope"]
        payload.update(
            runtime_manifest_schema_version=expected_manifest_version,
            identity_root_role=identity_root_role,
            candidate_scope_id=scope["scope_id"] if scope is not None else None,
            candidate_scope_sha256=scope["descriptor_sha256"] if scope is not None else None,
        )
    envelope = {"schema_version": grant_version, "algorithm": "ed25519", "key_id": policy["key_id"], "payload": payload, "signature": base64.b64encode(key.sign(_canonical(payload))).decode("ascii")}
    try:
        grant_contract = _contract_module(GRANT_CONTRACT_PATH, "_market_data_production_grant_host_contract")
        grant_contract.validate_execution_grant_envelope(envelope)
    except (AttributeError, TypeError, ValueError) as exc:
        raise HostAuthorizationError("issued envelope violates the grant schema") from exc
    if observe_and_validate(container_id, policy, role=role) != observed:
        raise HostAuthorizationError("container changed before grant sealing")
    _validate_mount_sources(observed, policy, grant_dir)
    final_container = docker_inspect(container_id)
    final_image = docker_image_inspect(policy["image_id"])
    if _render_actual_compose(final_container, policy, final_image) != rendered_digest:
        raise HostAuthorizationError("Compose deployment changed before grant sealing")
    if (copy_container_bytes(container_id, policy["runtime_manifest_path"]) != manifest_raw
            or copy_container_bytes(container_id, marker_path) != marker_raw
            or copy_container_bytes(container_id, policy["source_root"] + "/02_configs/production_runtime_trust.json") != trust_raw):
        raise HostAuthorizationError("identity material changed before grant sealing")
    _validate_v3_runtime(manifest, observed, policy, container_id)
    if policy_version in _CANDIDATE_POLICIES and role == "candidate_validation":
        _candidate_descriptor(policy, consume=False)
    if _load_policy(expected_policy_path) != policy or TRUST_CONFIG_PATH.read_bytes() != trust_raw:
        raise HostAuthorizationError("host approval or trust changed before grant sealing")
    if policy_version in _PRODUCTION_POLICIES and _validated_candidate_record(policy) != candidate_record:
        raise HostAuthorizationError("candidate record changed before grant sealing")
    if policy_version in _PRODUCTION_POLICIES and _secret_state(observed) != secret_state:
        raise HostAuthorizationError("secret file changed before grant sealing")
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(envelope))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(destination, 0o444)
        _fsync_directory(destination.parent)
    except FileExistsError as exc:
        raise HostAuthorizationError("grant already exists; refusing overwrite") from exc
    return envelope


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--grant-directory", required=True)
    parser.add_argument("--grant", required=True)
    parser.add_argument("--role", choices=("production", "candidate_validation"), required=True)
    args = parser.parse_args(argv)
    try:
        result = issue_execution_grant(args.container_id, expected_policy_path=args.policy, key_path=args.key, grant_path=args.grant, grant_dir=args.grant_directory, role=args.role)
        print(json.dumps({"HOST_AUTHORIZATION": "PASS", "grant_id": result["payload"]["grant_id"], "container_id": args.container_id}))
        return 0
    except (HostAuthorizationError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"HOST_AUTHORIZATION": "FAIL", "reason": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
