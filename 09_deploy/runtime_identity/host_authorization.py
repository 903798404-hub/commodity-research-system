"""Host-side observation and validation for production execution grants.

This module deliberately does not trust container supplied identity claims.  All
observed values come from Docker's inspect API (and the immutable RELEASE file
copied from the inspected container).  Signing is kept out of the pure
validators so a caller cannot turn a hand-crafted mapping into authorization.
"""
from __future__ import annotations

import io
from functools import lru_cache
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


@lru_cache(maxsize=1)
def _observation():
    return _contract_module(Path(__file__).with_name("runtime_observation.py"), "_host_runtime_observation")


def _observe(name, *args, **kwargs):
    module = _observation()
    try:
        return getattr(module, name)(*args, **kwargs)
    except module.ObservationError as exc:
        raise HostAuthorizationError(str(exc)) from exc


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
    cid = _container_id(container_id)
    return _observe("inspect_object", _run_docker(["container", "inspect", cid], runner=runner), "docker container inspect", expected_id=cid)


def docker_image_inspect(image_id: str, *, runner=None) -> dict[str, Any]:
    if not isinstance(image_id, str) or not _IMAGE.fullmatch(image_id):
        raise HostAuthorizationError("image must be an immutable sha256 ID")
    return _observe("inspect_object", _run_docker(["image", "inspect", image_id], runner=runner), "docker image inspect", expected_id=image_id)


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
    return _observe("strict_object", raw, "identity")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _absolute(value: str) -> str:
    return _observe("absolute", value)


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _env(values: list[str]) -> dict[str, str]:
    return _observe("environment", values)


def _mounts(container: Mapping) -> list[dict]:
    return _observe("mount_projection", container.get("Mounts"), purpose="authorization")


def normalize_observation(container: Mapping, image: Mapping, release: Mapping) -> dict:
    payload = _observe("config_payload", container)
    config, host = payload["config"], payload["host_config"]
    labels = image.get("Config", {}).get("Labels") or {}
    return {
        "container_id": container.get("Id"), "image_id": container.get("Image"),
        "config": config, "host_config": host, "image_config": image.get("Config", {}),
        "image_labels": labels, "release_manifest": release,
        "mounts": _mounts(container), "state": container.get("State", {}),
        "path": container.get("Path"), "args": container.get("Args"),
        "actual_config_sha256": _digest(payload),
    }


def compare_config_payloads(policy_payload: Mapping, actual_payload: Mapping, *,
                            policy_raw_sha256: str, actual_raw_sha256: str,
                            platform: Mapping | None = None) -> dict:
    """Keep raw evidence; normalize only exact observed Docker transitions."""
    if _digest(policy_payload) != policy_raw_sha256 or _digest(actual_payload) != actual_raw_sha256:
        raise HostAuthorizationError("config payload does not reproduce raw hash")
    evidence = dict(policy_raw_sha256=policy_raw_sha256, actual_raw_sha256=actual_raw_sha256,
                    raw_hash_match=policy_raw_sha256 == actual_raw_sha256,
                    semantic_config_match=True, compatibility_rule=None, platform=dict(platform or {}))
    if evidence["raw_hash_match"]:
        return evidence
    version = (platform or {}).get("ServerVersion")
    if not platform or version not in {"26.1.3", "28.0.4"} or platform.get("CgroupVersion") != "2":
        raise HostAuthorizationError("config mismatch: unsupported Docker/cgroup compatibility evidence")
    left, right = _json(_canonical(policy_payload)), _json(_canonical(actual_payload))
    dns_fields = ('Dns', 'DnsOptions', 'DnsSearch')
    before, after = left.get('host_config', {}), right.get('host_config', {})
    dns_restore = (version == '26.1.3' and before.get('OomKillDisable') is False
                   and after.get('OomKillDisable') is None
                   and 'OomKillDisable' in after
                   and all(k in before and before[k] is None and k in after
                           and type(after[k]) is list and not after[k] for k in dns_fields))
    if dns_restore:
        for key in dns_fields:
            after[key] = None
    if version == "28.0.4":
        before, after = left.get("host_config", {}), right.get("host_config", {})
        if ("OomKillDisable" not in before or "OomKillDisable" not in after
                or before["OomKillDisable"] is not False or after["OomKillDisable"] is not None):
            raise HostAuthorizationError("config mismatch: 28.0.4 requires created false to running explicit null")
    for payload in (left, right):
        host = payload.get("host_config", {})
        if "OomKillDisable" not in host or not (host["OomKillDisable"] is False or host["OomKillDisable"] is None):
            raise HostAuthorizationError("config mismatch: OomKillDisable is not false/null")
        host["OomKillDisable"] = False
    if _canonical(left) != _canonical(right):
        raise HostAuthorizationError("config mismatch outside exact supported defaults")
    evidence["compatibility_rule"] = ("DOCKER26_RESTORE_OOM_DNS_DEFAULTS" if dns_restore
                                      else "OOM_KILL_DISABLE_FALSE_NULL_EQUIVALENCE")
    if dns_restore:
        evidence.update(matched_platform='Docker 26.1.3 / cgroup 2',
                        normalized_fields=['HostConfig.OomKillDisable',
                                           *('HostConfig.' + k for k in dns_fields)])
    if version == "28.0.4":
        evidence.update(matched_platform="Docker 28.0.4 / cgroup 2",
                        normalized_field="HostConfig.OomKillDisable")
    return evidence


def compare_observed_config(observed: Mapping, policy_raw_sha256: str) -> dict:
    """Reconstruct bounded defaults; the sealed raw digest proves every byte."""
    actual = {k: observed.get(k) for k in ("config", "host_config", "path", "args")}
    actual_hash = observed["actual_config_sha256"]
    if _digest(actual) != actual_hash:
        raise HostAuthorizationError("actual config payload does not reproduce raw hash")
    if actual_hash == policy_raw_sha256:
        return compare_config_payloads(actual, actual, policy_raw_sha256=policy_raw_sha256,
                                       actual_raw_sha256=actual_hash)
    reconstructed = _json(_canonical(actual))
    host = reconstructed["host_config"]
    if "OomKillDisable" not in host or not (host["OomKillDisable"] is False or host["OomKillDisable"] is None):
        raise HostAuthorizationError("actual container config differs from approval")
    host["OomKillDisable"] = None if host["OomKillDisable"] is False else False
    dns_restore = False
    if _digest(reconstructed) != policy_raw_sha256:
        # Docker 26.1.3 restores three unset DNS slices as empty arrays. Require
        # the exact simultaneous false->null OOM transition observed on host.
        dns_fields = ('Dns', 'DnsOptions', 'DnsSearch')
        if (actual['host_config']['OomKillDisable'] is None and
                all(k in host and type(host[k]) is list and not host[k] for k in dns_fields)):
            for key in dns_fields:
                host[key] = None
            dns_restore = _digest(reconstructed) == policy_raw_sha256
        if not dns_restore:
            raise HostAuthorizationError("reconstructed config does not reproduce sealed raw hash")
    info = _json(_run_docker(["info", "--format", "json"]))
    platform = {key: info.get(key) for key in ("ServerVersion", "CgroupVersion", "CgroupDriver")}
    if dns_restore and (platform.get('ServerVersion') != '26.1.3' or platform.get('CgroupVersion') != '2'):
        raise HostAuthorizationError('DNS restore requires Docker 26.1.3 / cgroup 2')
    if dns_restore or platform.get("ServerVersion") == "28.0.4":
        state = observed.get("state", {})
        if (state.get("Status") != "running" or state.get("Running") is not True
                or not isinstance(observed.get("container_id"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", observed["container_id"])):
            raise HostAuthorizationError("default compatibility requires the bound running instance")
    return compare_config_payloads(reconstructed, actual, policy_raw_sha256=policy_raw_sha256,
                                   actual_raw_sha256=actual_hash, platform=platform)


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


def _recovery_call(name, *args):
    from types import SimpleNamespace
    module = _contract_module(SOURCE_ROOT / '09_deploy/runtime_identity/recovery_namespace.py', '_recovery_namespace')
    return getattr(module, name)(SimpleNamespace(**globals()), *args)


def validate_policy(policy: Mapping, role: str) -> None:
    version = policy.get("schema_version")
    expected_fields = {"host-runtime-policy/1": _POLICY_V1_FIELDS,
                       "host-runtime-policy/2": _POLICY_V2_FIELDS,
                       "host-runtime-policy/3": _POLICY_V3_FIELDS,
                       "host-runtime-policy/4": _POLICY_V2_FIELDS,
                       "host-runtime-policy/5": _POLICY_V3_FIELDS}.get(version, set())
    if 'recovery' in policy:
        if version not in _PRODUCTION_POLICIES or role != 'production':
            raise HostAuthorizationError('recovery requires the existing production role')
        expected_fields = expected_fields | {'recovery'}
        _recovery_call('validate', policy)
    application_fields = (expected_fields | {"application_service"}
                          if version in {"host-runtime-policy/4", "host-runtime-policy/5"}
                          else set())
    if set(policy) not in (expected_fields, application_fields) or policy.get("role") != role:
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
    if "application_service" in policy:
        service = policy["application_service"]
        if (type(service) is not dict or set(service) != {"service_id", "runtime_id", "allowed_writable_roots"}
                or service["service_id"] != policy["service_id"]
                or service["runtime_id"] != policy["runtime_id"]
                or type(service["allowed_writable_roots"]) is not list
                or not service["allowed_writable_roots"]
                or any(type(root) is not str for root in service["allowed_writable_roots"])
                or len(set(service["allowed_writable_roots"])) != len(service["allowed_writable_roots"])):
            raise HostAuthorizationError("application service policy identity or roots are invalid")
        for root in service["allowed_writable_roots"]:
            _absolute(root)
            if not _within(root, policy["runtime_root"]) or root == policy["runtime_root"]:
                raise HostAuthorizationError("application service root is outside the runtime")
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
    comparison = compare_observed_config(observed, expected["actual_config_sha256"])
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
    result = dict(observed)
    if not comparison["raw_hash_match"]:
        result["config_comparison"] = comparison
    return result


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
    for path in (Path(__file__).resolve(strict=True), GRANT_CONTRACT_PATH, MANIFEST_CONTRACT_PATH, TRUST_CONFIG_PATH,
                 SOURCE_ROOT / "09_deploy/runtime_identity/recovery_namespace.py",
                 Path(__file__).with_name("runtime_observation.py")):
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


def _production_readonly_binding(binding: Mapping) -> tuple[dict, dict]:
    """Resolve a read-only input through an existing protected production policy."""
    if set(binding) != {"policy_path", "source", "target"}:
        raise HostAuthorizationError("production readonly binding is invalid")
    path = _protected_path(Path(_absolute(binding["policy_path"])), private=True)
    raw = path.read_bytes()
    production = _json(raw)
    validate_policy(production, "production")
    if production["schema_version"] not in _PRODUCTION_POLICIES:
        raise HostAuthorizationError("approved production storage identity required")
    source = Path(_absolute(binding["source"]))
    if not source.exists() or source.is_symlink() or source.resolve(strict=True) != source:
        raise HostAuthorizationError("production readonly source is missing or aliased")
    storage = _protected_path(Path(production["production_storage_root"]), directory=True)
    target = _absolute(binding["target"])
    mount = {"source": str(source), "target": target, "read_only": True}
    if not _within(str(source), str(storage)) or mount not in production["mounts"]:
        raise HostAuthorizationError("source is not an approved production readonly mount")
    state = source.stat()
    if not (source.is_dir() or source.is_file()) or (source.is_file() and state.st_nlink != 1):
        raise HostAuthorizationError("production readonly source identity is unsafe")
    return {**binding, "policy_sha256": hashlib.sha256(raw).hexdigest(),
            "device": state.st_dev, "inode": state.st_ino, "read_only": True}, production


def _readonly_snapshot(source: Path, target: str) -> dict:
    """Observe all bytes and inodes of an immutable, root-owned candidate tree."""
    records, seen, total, files_count = [], set(), 0, 0
    if not source.is_dir() or source.is_symlink() or source.resolve(strict=True) != source:
        raise HostAuthorizationError("candidate readonly snapshot source is unsafe")
    for directory, names, files in os.walk(source, topdown=True, followlinks=False):
        for path in [Path(directory), *[Path(directory) / name for name in sorted([*names, *files])]]:
            relative = path.relative_to(source).as_posix()
            if relative in seen:
                continue  # A child directory is observed again by os.walk.
            state = path.lstat()
            if (state.st_uid != 0 or state.st_mode & 0o222 or path.is_symlink()
                    or path.resolve(strict=True) != path
                    or not (stat.S_ISDIR(state.st_mode) or stat.S_ISREG(state.st_mode))):
                raise HostAuthorizationError("candidate readonly snapshot is writable or aliased")
            item = {'path': relative, 'device': state.st_dev, 'inode': state.st_ino,
                    'mode': stat.S_IMODE(state.st_mode), 'kind': 'directory' if path.is_dir() else 'file'}
            if item['kind'] == 'file':
                if state.st_nlink != 1 or state.st_size > 1024 * 1024 * 1024:
                    raise HostAuthorizationError("candidate readonly snapshot file is linked or oversized")
                files_count += 1; total += state.st_size
                if files_count > 20000 or total > 8 * 1024 * 1024 * 1024:
                    raise HostAuthorizationError("candidate readonly snapshot exceeds size limit")
                digest, size = hashlib.sha256(), 0
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        size += len(chunk); digest.update(chunk)
                        if size > state.st_size:
                            raise HostAuthorizationError("candidate readonly snapshot changed while read")
                after = path.lstat()
                if (size != state.st_size or (after.st_dev, after.st_ino, after.st_size, after.st_mode,
                        after.st_uid, after.st_nlink) != (state.st_dev, state.st_ino, state.st_size,
                        state.st_mode, state.st_uid, state.st_nlink)):
                    raise HostAuthorizationError("candidate readonly snapshot changed while read")
                item.update(size_bytes=size, sha256=digest.hexdigest())
            records.append(item)
            seen.add(relative)
            if len(records) > 40000:
                raise HostAuthorizationError("candidate readonly snapshot exceeds entry limit")
    if not records or not files_count:
        raise HostAuthorizationError("candidate readonly snapshot is empty")
    return {'source': str(source), 'target': target, 'tree_sha256': _digest(sorted(records, key=lambda x: x['path'])),
            'file_count': files_count, 'total_bytes': total}


def _candidate_descriptor(policy: Mapping, *, consume: bool, check_expiry: bool = True) -> dict:
    binding = policy.get("candidate_scope")
    if policy.get("schema_version") not in _CANDIDATE_POLICIES or policy.get("role") != "candidate_validation" or not isinstance(binding, dict):
        raise HostAuthorizationError("candidate scope is unavailable")
    descriptor_path = _protected_path(Path(binding["descriptor_path"]), private=True, temporary=True)
    raw = descriptor_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != binding["descriptor_sha256"]:
        raise HostAuthorizationError("candidate scope descriptor differs from policy")
    descriptor = _json(raw)
    expected = {"schema_version", "scope_id", "created_at", "expires_at", "root", "binds"}
    version = descriptor.get("schema_version")
    allowed = (expected, expected | {"production_readonly"}) if version == 'candidate-scope/1' else (
        expected | {'readonly_snapshots'}, expected | {'production_readonly', 'readonly_snapshots'})
    if (set(descriptor) not in allowed or version not in {'candidate-scope/1', 'candidate-scope/2'}
            or descriptor.get("scope_id") != binding["scope_id"]):
        raise HostAuthorizationError("candidate scope descriptor schema is invalid")
    created, expires = _rfc3339(descriptor["created_at"], "candidate scope creation time"), _rfc3339(descriptor["expires_at"], "candidate scope expiry")
    now = datetime.now(timezone.utc)
    if created > now or (expires <= now and (check_expiry or consume)) or expires <= created or expires - created > timedelta(hours=1):
        raise HostAuthorizationError("candidate scope is expired or exceeds its lifetime")
    root_record = descriptor.get("root")
    if not isinstance(root_record, dict) or set(root_record) != {"path", "device", "inode"}:
        raise HostAuthorizationError("candidate scope root identity is invalid")
    root = Path(_absolute(root_record["path"]))
    if not _within(str(root), CANDIDATE_SCOPE_PARENT) or str(root) == CANDIDATE_SCOPE_PARENT:
        raise HostAuthorizationError("candidate scope is outside the fixed temporary authority root")
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
    readonly = descriptor.get("production_readonly", [])
    if not isinstance(readonly, list):
        raise HostAuthorizationError("production readonly identities are invalid")
    for item in readonly:
        if not isinstance(item, dict) or set(item) != {"policy_path", "source", "target", "policy_sha256", "device", "inode", "read_only"}:
            raise HostAuthorizationError("production readonly identity is invalid")
        observed, production = _production_readonly_binding({k: item[k] for k in ("policy_path", "source", "target")})
        if observed != item or any(production[k] != policy[k] for k in ("project_id", "module_id", "service_id")):
            raise HostAuthorizationError("production readonly identity changed")
        if item["target"] in targets:
            raise HostAuthorizationError("duplicate candidate mount target")
        sources = [production["production_storage_root"], *[m["source"] for m in production["mounts"]]]
        if any(_within(str(root), s) or _within(s, str(root)) for s in sources):
            raise HostAuthorizationError("candidate scope overlaps production storage")
        targets.add(item["target"])
        actual.append({k: item[k] for k in ("source", "target", "read_only")})
    expected_mounts = [m for m in policy["mounts"] if m["target"] != policy["grant_container_directory"]]
    if sorted(actual, key=lambda item: item["target"]) != sorted(expected_mounts, key=lambda item: item["target"]):
        raise HostAuthorizationError("candidate scope descriptor differs from mount policy")
    if version == 'candidate-scope/2':
        snapshots = descriptor['readonly_snapshots']
        if (not isinstance(snapshots, list) or not snapshots
                or any(not isinstance(item, dict) or set(item) != {'source', 'target', 'tree_sha256', 'file_count', 'total_bytes'} for item in snapshots)
                or len({item['target'] for item in snapshots}) != len(snapshots)):
            raise HostAuthorizationError("candidate readonly snapshot descriptor is invalid")
        for item in snapshots:
            if (type(item['source']) is not str or type(item['target']) is not str
                    or type(item['tree_sha256']) is not str or not _HEX64.fullmatch(item['tree_sha256'])
                    or type(item['file_count']) is not int or not 0 < item['file_count'] <= 20000
                    or type(item['total_bytes']) is not int or not 0 <= item['total_bytes'] <= 8 * 1024 * 1024 * 1024):
                raise HostAuthorizationError("candidate readonly snapshot descriptor is invalid")
            bind = next((b for b in binds if b['source'] == item['source'] and b['target'] == item['target']), None)
            if (bind is None or bind['read_only'] is not True or item['source'] == str(root)
                    or not _within(item['source'], str(root))
                    or any(not b['read_only'] and (_within(b['source'], item['source'])
                           or _within(item['source'], b['source'])) for b in binds)
                    or _readonly_snapshot(Path(item['source']), item['target']) != item):
                raise HostAuthorizationError("candidate readonly snapshot identity changed")
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


def create_candidate_scope(bindings: Sequence[Mapping], *, ttl_seconds: int = 3600,
                           production_readonly: Sequence[Mapping] = ()) -> dict:
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
        readonly_records = [_production_readonly_binding(item)[0] for item in production_readonly]
        if len({item['target'] for item in readonly_records}) != len(readonly_records):
            raise HostAuthorizationError("duplicate production readonly target")
        targets.update(item['target'] for item in readonly_records)
        for binding in bindings:
            required = {"relative_path", "target", "read_only", "owner_uid", "owner_gid"}
            if not isinstance(binding, Mapping) or set(binding) not in (required, required | {"kind"}) or type(binding["read_only"]) is not bool:
                raise HostAuthorizationError("candidate scope binding is invalid")
            kind = binding.get("kind", "directory")
            if kind not in {"directory", "file"} or (kind == "file" and (binding["read_only"] is not True or binding["owner_uid"] != 0)):
                raise HostAuthorizationError("candidate file binding must be root-owned and readonly")
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
                if kind == "file":
                    source.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                    fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o440)
                    os.close(fd)
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
        if readonly_records:
            descriptor["production_readonly"] = readonly_records
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
            "mounts": [{"source": item["source"], "target": item["target"], "read_only": item["read_only"]} for item in [*records, *readonly_records]],
        }
    except Exception:
        # The parent is protected and freshly allocated, so recursive cleanup
        # cannot escape it.  Failed creation never yields a usable descriptor.
        import shutil
        shutil.rmtree(root, ignore_errors=True)
        raise


def seal_candidate_readonly_snapshots(scope: Mapping, targets: Sequence[str]) -> dict:
    """Seal only fresh scope-owned readonly directories before issuing a grant.

    This does not approve production storage or grant production execution.
    The returned descriptor binds complete bytes, modes and inodes, rechecked
    by mount preflight and the fresh candidate issuer. Original CI fixture
    rules continue to apply to every unsealed candidate input.
    """
    _require_linux_root()
    if (set(scope) != {'candidate_host_root', 'candidate_scope', 'mounts'}
            or not isinstance(targets, Sequence) or isinstance(targets, (str, bytes))
            or not targets or any(type(t) is not str for t in targets) or len(set(targets)) != len(targets)):
        raise HostAuthorizationError("candidate readonly snapshot request is invalid")
    if (type(scope['candidate_host_root']) is not str or not isinstance(scope['mounts'], list)
            or any(not isinstance(m, dict) or set(m) != {'source', 'target', 'read_only'}
                   or type(m['source']) is not str or type(m['target']) is not str
                   or type(m['read_only']) is not bool for m in scope['mounts'])):
        raise HostAuthorizationError("candidate readonly snapshot scope is invalid")
    if any(not _within(m['source'], scope['candidate_host_root']) for m in scope['mounts']):
        raise HostAuthorizationError("candidate snapshot requires a fresh wholly isolated scope")
    policy = {**scope, 'schema_version': 'host-runtime-policy/4', 'role': 'candidate_validation',
              'grant_container_directory': '/run/market-data-grants'}
    descriptor = _candidate_descriptor(policy, consume=False)
    path = Path(scope['candidate_scope']['descriptor_path'])
    if (descriptor['schema_version'] != 'candidate-scope/1' or descriptor.get('production_readonly')
            or path.with_name(path.name + '.consumed').exists()):
        raise HostAuthorizationError("candidate snapshot requires a fresh wholly isolated scope")
    snapshots = []
    for target in targets:
        bind = next((b for b in descriptor['binds'] if b['target'] == target), None)
        if (bind is None or bind['read_only'] is not True or bind['source'] == scope['candidate_host_root']
                or not Path(bind['source']).is_dir()
                or any(not b['read_only'] and (_within(b['source'], bind['source'])
                       or _within(bind['source'], b['source'])) for b in descriptor['binds'])):
            raise HostAuthorizationError("candidate snapshot must be a declared readonly child directory")
        snapshots.append(_readonly_snapshot(Path(bind['source']), target))
    sealed = {**descriptor, 'schema_version': 'candidate-scope/2', 'readonly_snapshots': snapshots}
    raw = _canonical(sealed)
    destination = path.with_name(path.name + '.readonly-snapshots.json')
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    _fsync_directory(destination.parent)
    result = {**scope, 'candidate_scope': {**scope['candidate_scope'], 'descriptor_path': str(destination),
              'descriptor_sha256': hashlib.sha256(raw).hexdigest()}}
    _candidate_descriptor({**policy, 'candidate_scope': result['candidate_scope']}, consume=False)
    return result


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


def _reject_other_writable_sources(root: str, own_container: str | None) -> None:
    """Observe other containers, including stopped production recovery instances."""
    ids = _run_docker(['ps', '-aq', '--no-trunc']).decode('ascii').split()
    for cid in ids:
        _container_id(cid)
        if cid == own_container:
            continue
        container = docker_inspect(cid)
        if not isinstance(container.get('Mounts'), list):
            raise HostAuthorizationError('container mount observation is incomplete')
        for mount in container['Mounts']:
            if not isinstance(mount, dict) or type(mount.get('RW')) is not bool:
                raise HostAuthorizationError('container mount mode is unknown')
            if mount['RW'] is not True:
                continue
            source = Path(_absolute(mount.get('Source'))).resolve(strict=True)
            if _within(str(source), root) or _within(root, str(source)):
                raise HostAuthorizationError('candidate storage overlaps another container writable source')


def _validate_mount_sources(observed: Mapping, policy: Mapping, grant_dir: Path, *, check_expiry: bool = True) -> None:
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
            if mount["read_only"] is not True:
                raise HostAuthorizationError("production source RW is forbidden for candidate")
            descriptor = _candidate_descriptor(policy, consume=False, check_expiry=check_expiry)
            allowed = [{k: item[k] for k in ("source", "target", "read_only")}
                       for item in descriptor.get("production_readonly", [])]
            if mount not in allowed:
                raise HostAuthorizationError("candidate source has no approved readonly identity")
        if policy["schema_version"] in _PRODUCTION_POLICIES:
            storage = _protected_path(Path(policy["production_storage_root"]), directory=True)
            sandbox_mount = False
            if 'sandbox' in policy.get('recovery', {}):
                old, _ = _recovery_call('retained', policy)
                sandbox_mount = mount in _recovery_call('sandbox_mounts', policy, old) and any(
                    item['sandbox_source'] == str(source) for item in policy['recovery']['sandbox']['roots'])
                if sandbox_mount:
                    _reject_other_writable_sources(policy['recovery']['sandbox']['root'], observed.get('container_id'))
            if not mount["target"].startswith("/run/secrets/") and not _within(str(source), str(storage)) and not sandbox_mount:
                raise HostAuthorizationError("production mount is outside its approved storage allocation")
            if any((parent / ".git").exists() for parent in (source, *source.parents) if parent.is_dir()):
                raise HostAuthorizationError("production cannot mount a development Git checkout")
        if source == Path("/") or any(_within(str(source), root) or _within(root, str(source)) for root in ("/var/run", "/run", "/var/lib/docker", "/proc", "/sys", "/dev", "/root")):
            raise HostAuthorizationError("control or host system directory mount rejected")
    if policy['role'] == 'candidate_validation' and policy['schema_version'] in _CANDIDATE_POLICIES:
        _reject_other_writable_sources(policy['candidate_host_root'], observed.get('container_id'))


def validate_candidate_mounts(manifest: Mapping, mounts: list[dict], policy: Mapping,
                              grant_dir: Path, *, live: bool = False, container_id: str | None = None) -> None:
    """The routine preflight and issuer share the existing host source identities."""
    validate_policy(policy, "candidate_validation")
    if any(manifest.get(key) != policy[key] for key in ('project_id', 'module_id', 'service_id')):
        raise HostAuthorizationError("candidate manifest identity differs from policy")
    _candidate_descriptor(policy, consume=False, check_expiry=not live)
    if sorted(mounts, key=lambda m: m['target']) != sorted(policy['mounts'], key=lambda m: m['target']):
        raise HostAuthorizationError("candidate mounts differ from protected policy")
    _validate_runtime_mounts(manifest, mounts, policy)
    _validate_mount_sources({'mounts': mounts, 'container_id': container_id}, policy, grant_dir, check_expiry=not live)


def _validated_candidate_record(policy: Mapping, *, primary_rollback_context=None) -> tuple[dict, dict]:
    """Bind an authenticated validation fact to this exact Approved source."""
    from types import SimpleNamespace
    # Routine collector JSON is protected by the same administrator-owned file
    # boundary, but does not need an additional evidence-signing hierarchy.
    if 'recovery' in policy:
        if primary_rollback_context is not None:
            raise HostAuthorizationError('historical acceptance cannot authorize recovery rehearsal')
        return _recovery_call('baseline', policy)
    record_binding = policy["candidate_record"]
    raw = _protected_path(Path(record_binding["path"]), private=True).read_bytes()
    if hashlib.sha256(raw).hexdigest() != record_binding["sha256"]:
        raise HostAuthorizationError("candidate record content differs from approval")
    record = _json(raw)
    if record.get("schema_version") in {"routine-candidate-acceptance/1", "routine-rollback-assets/1"}:
        routine = _contract_module(SOURCE_ROOT / "04_scripts/runtime/routine_release.py", "_host_routine_release")
        entry = _contract_module(SOURCE_ROOT / "04_scripts/runtime/pre_release_runtime.py", "_host_routine_pre_release")
        engine = _contract_module(SOURCE_ROOT / "04_scripts/runtime/validate_target_runtime.py", "_host_routine_engine")
        source = _protected_path(Path(policy["approved_source_root"]), directory=True)
        identity = entry.require_source(SimpleNamespace(_require_linux_root=_require_linux_root,
            _protected_path=_protected_path), engine, source_root=source)
        if identity != (policy["approved_commit"], policy["approved_tree"]):
            raise HostAuthorizationError("Approved source Commit/Tree differs")
        if primary_rollback_context is None:
            routine.verify_routine_record(record, policy, source)
        else:
            primary_rollback_context.verify(record, policy, source)
        project = engine._project(source, policy["project_id"])
        _, manifest, binding = engine.source_contract(source, policy["project_id"], project["runtime_contract"])
        if (manifest["schema_version"] != _manifest_version(policy["schema_version"])
                or binding["source_sha256"][project["runtime_contract"]] != policy["runtime_manifest_sha256"]
                or policy["runtime_manifest_path"] != policy["source_root"] + "/" + project["runtime_contract"]):
            raise HostAuthorizationError("routine source contract differs from policy")
        engine.validate_source_compose(source, manifest)
        return record, manifest
    if primary_rollback_context is not None:
        raise HostAuthorizationError('historical context requires the original Routine primary rollback fact')
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
    source_root = Path(policy.get("approved_source_root", SOURCE_ROOT))
    command = ["compose", "--project-name", name, "--project-directory", str(source_root),
               "--env-file", policy["compose_environment_file"]]
    for relative in manifest["build"]["compose_sources"]:
        command.extend(["-f", str(_protected_path(source_root / relative))])
    desired = _json(_run_docker([*command, "config", "--format", "json"]))
    if 'recovery' in policy:
        desired = _recovery_call('project_compose', desired, policy)
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
    if 'recovery' in policy:
        if _recovery_call('validate_projected_network', desired, actual, policy) is not True:
            raise HostAuthorizationError("recovery network semantic validation did not pass")
        # Recovery network identity is owned by the semantic validator above.
        # The rest of the Compose document remains an exact structural match.
        desired.pop('networks')
        actual.pop('networks')
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


def _secret_file_identity(source: Path, user: str, *, temporary: bool = False) -> dict:
    """Observe a protected file without publishing its contents or credentials."""
    if temporary:
        _protected_path(source, temporary=True)
    else:
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


def declared_secret_targets(manifest: Mapping, rendered: Mapping) -> dict[str, str]:
    return _observe("declared_secret_targets", manifest, rendered)


def _candidate_secret_mounts(manifest: Mapping, policy: Mapping, rendered: Mapping | None = None) -> list[dict]:
    source_command = ["compose", "--project-directory", str(SOURCE_ROOT)]
    for relative in manifest["build"]["compose_sources"]:
        source_command.extend(["-f", str(_protected_path(SOURCE_ROOT / relative))])
    declarations = declared_secret_targets(manifest, _json(_run_docker(
        [*source_command, "config", "--no-interpolate", "--format", "json"])))
    if rendered is None:
        command = ["compose", "--project-directory", policy["compose_project_directory"],
                   "--env-file", policy["compose_environment_file"]]
        for item in policy["compose_sources"]:
            path = _protected_path(Path(item["path"]))
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise HostAuthorizationError("candidate secret Compose source changed")
            command.extend(["-f", str(path)])
        rendered = _json(_run_docker([*command, "config", "--format", "json"]))
        if _digest(rendered) != policy["rendered_compose_sha256"]:
            raise HostAuthorizationError("candidate secret Compose differs from policy")
    service = rendered.get("services", {}).get(policy["service_id"], {})
    refs, definitions = service.get("secrets") or [], rendered.get("secrets") or {}
    if (type(refs) is not list or type(definitions) is not dict
            or any(type(item) is not dict or set(item) != {"source", "target"} for item in refs)):
        raise HostAuthorizationError("invalid candidate file-secret references")
    names = [item["source"] for item in refs]
    if any(type(name) is not str for name in names) or len(names) != len(set(names)) or set(definitions) != set(names):
        raise HostAuthorizationError("duplicate or undeclared candidate secret")
    mounts = []
    for item in refs:
        name = item["source"]
        if name not in declarations or item["target"] != declarations[name]:
            raise HostAuthorizationError("undeclared candidate secret or wrong target")
        definition = definitions[name]
        if type(definition) is not dict or set(definition) - {"file", "name"} or type(definition.get("file")) is not str:
            raise HostAuthorizationError("candidate secret must be a declared file")
        source = _protected_path(Path(definition["file"]), temporary=True)
        if not _within(str(source), policy["candidate_host_root"]):
            raise HostAuthorizationError("candidate secret is outside its isolated scope")
        _secret_file_identity(source, service.get("user"), temporary=True)
        mounts.append({"source": str(source), "target": item["target"], "read_only": True})
    return mounts


def _acceptance_for_instance(policy, context):
    # Preserve the ordinary issuer ABI and strict current-time semantics.
    if context is None:
        return _validated_candidate_record(policy)
    return _validated_candidate_record(policy, primary_rollback_context=context)


def _render_actual_compose(container: Mapping, policy: Mapping, image: Mapping,
                           *, recovery_phase: str = 'pre_start', primary_rollback_context=None) -> str:
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
    expected_mounts = _observe("compose_mounts", volumes)
    if policy["schema_version"] in _PRODUCTION_POLICIES:
        _, manifest = _acceptance_for_instance(policy, primary_rollback_context)
        expected_mounts = sorted([*expected_mounts, *_production_compose_bridge(rendered, policy, manifest)], key=lambda m: m["target"])
    elif policy["schema_version"] in _CANDIDATE_POLICIES and service.get("secrets"):
        manifest = _json(copy_container_bytes(container["Id"], policy["runtime_manifest_path"]))
        expected_mounts = sorted([*expected_mounts, *_candidate_secret_mounts(manifest, policy, rendered)], key=lambda m: m["target"])
    if expected_mounts != _mounts(container):
        raise HostAuthorizationError("rendered mounts differ from actual container")
    if 'recovery' in policy:
        _recovery_call('validate_instance', container, policy, recovery_phase)
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
    # For policy/3 the source/render bridge has already proved the exact secret
    # names, file sources and targets against the Approved manifest and inspect.
    secrets = [m for m in mounts if m["target"].startswith("/run/secrets/")] if policy["schema_version"] in _PRODUCTION_POLICIES else []
    if secrets and (len(secrets) != len(manifest["secret_references"]) or any(m["read_only"] is not True for m in secrets)):
        raise HostAuthorizationError("production secret mount permissions differ")
    if policy["schema_version"] in _CANDIDATE_POLICIES:
        secrets = [m for m in mounts if m["target"].startswith("/run/secrets/")]
        if secrets:
            declared = _candidate_secret_mounts(manifest, policy)
            if sorted(secrets, key=lambda m: m["target"]) != sorted(declared, key=lambda m: m["target"]):
                raise HostAuthorizationError("candidate secret mounts differ from exact readonly declarations")
    _observe("resolve_mount_interpretation", manifest, policy, mounts, secrets)
    # Operational business state must not alias sealed inputs or each other.
    operational = {'manual-cnf', 'am-results'}
    sources = {item['role']: Path(next(m['source'] for m in mounts
               if m['target'] == item['container_path'])).resolve(strict=True)
               for item in required} if any(item['role'] in operational for item in required) else {}
    protected = {item['role'] for item in required if item['read_only'] and item['role'] != manifest.get('identity_root_role')}
    for role in operational & sources.keys():
        for other in ((protected | operational) & sources.keys()) - {role}:
            a, b = str(sources[role]), str(sources[other])
            if _within(a, b) or _within(b, a):
                raise HostAuthorizationError('operational write source overlaps historical or other operational source')


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
    if not manifest['candidate_runtime_inputs']:
        return
    roots = {item["role"]: item for item in manifest["runtime_roots"]}
    # Approved production-backed inputs are real readonly data, not CI seed
    # fixtures. All other inputs retain their exact fixture-byte checks.
    production_targets = set()
    descriptor = _candidate_descriptor(policy, consume=False)
    snapshot_targets = {item['target'] for item in descriptor.get('readonly_snapshots', [])}
    if any(m['target'] != policy['grant_container_directory'] and not _within(m['source'], policy['candidate_host_root'])
           for m in observed['mounts']):
        production_targets = {item['target'] for item in descriptor.get('production_readonly', [])}
    total = 0
    for item in manifest["candidate_runtime_inputs"]:
        target_root = roots[item["role"]]["container_path"]
        mounts = [mount for mount in observed["mounts"] if mount["target"] == target_root]
        if len(mounts) != 1 or mounts[0]["read_only"] is not True:
            raise HostAuthorizationError("candidate seed must have its exact readonly mount")
        if target_root in production_targets or target_root in snapshot_targets:
            continue
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


def revalidate_production(container_id: str, *, expected_policy_path: str | Path, primary_rollback_context=None) -> dict:
    """Read-only pre-approval validation; never sign a grant or start a container."""
    _require_linux_root()
    require_protected_authority_source()
    policy = _load_policy(expected_policy_path)
    validate_policy(policy, "production")
    if policy["schema_version"] not in _PRODUCTION_POLICIES:
        raise HostAuthorizationError("pre-release validation requires production policy/3")
    record, source_manifest = _acceptance_for_instance(policy, primary_rollback_context)
    observed = observe_and_validate(container_id, policy, role="production")
    grants = [m for m in observed["mounts"] if m["target"] == policy["grant_container_directory"]]
    if len(grants) != 1 or grants[0]["read_only"] is not True:
        raise HostAuthorizationError("pre-release grant mount is missing or writable")
    grant_dir = _protected_path(Path(grants[0]["source"]), directory=True)
    _validate_mount_sources(observed, policy, grant_dir)
    container = docker_inspect(container_id)
    image = docker_image_inspect(policy["image_id"])
    rendered = (_render_actual_compose(container, policy, image) if primary_rollback_context is None else
                _render_actual_compose(container, policy, image, primary_rollback_context=primary_rollback_context))
    recovery_network = (_recovery_call('validate_instance', container, policy, 'pre_start')
                        if 'recovery' in policy else None)
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
    if (_render_actual_compose(docker_inspect(container_id), policy, docker_image_inspect(policy["image_id"]))
            if primary_rollback_context is None else _render_actual_compose(docker_inspect(container_id), policy,
            docker_image_inspect(policy["image_id"]), primary_rollback_context=primary_rollback_context)) != rendered:
        raise HostAuthorizationError("production Compose changed during pre-release validation")
    if (_load_policy(expected_policy_path) != policy or _acceptance_for_instance(policy, primary_rollback_context) != (record, source_manifest)
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
            "candidate_rendered_compose_sha256": record.get("rendered_compose_sha256") if "recovery" in policy or record.get("schema_version", "").startswith("routine-") else record["evidence"]["rendered_compose_sha256"],
            "production_rendered_compose_sha256": rendered,
            "runtime_manifest_sha256": policy["runtime_manifest_sha256"], "release_sha256": observed["release_sha256"],
            "mount_contract_sha256": _digest(observed["mounts"]), "actual_config_sha256": observed["actual_config_sha256"],
            "secret_file_identity_sha256": _digest(secrets),
            **({"RUNTIME_NETWORK_IDENTITY_ASSERTION":
                recovery_network['RUNTIME_NETWORK_IDENTITY_ASSERTION'],
                "expected_recovery_network_id": recovery_network['expected_network_id']}
                if recovery_network is not None else {}),
            **({"recovery_config_comparison": record["config_comparison"]} if "config_comparison" in record else {}),
            **({"config_comparison": observed["config_comparison"]} if "config_comparison" in observed else {})}


def issue_application_service_credential(
    container_id: str, *, expected_policy_path: str | Path,
    credential_path: str | Path, role: str,
) -> dict[str, str]:
    """Provision one deployment-bound bearer into an already approved, unstarted container.

    The dedicated Docker file-secret mount is the transport; neither the
    credential nor its allowed roots come from an application-supplied request.
    The existing grant issuer still validates the same instance before start.
    """
    _require_linux_root()
    require_protected_authority_source()
    policy = _load_policy(expected_policy_path)
    validate_policy(policy, role)
    service = policy.get("application_service")
    if service is None:
        raise HostAuthorizationError("application service policy is not approved")
    observed = observe_and_validate(container_id, policy, role=role)
    if observed["state"].get("Status") != "created":
        raise HostAuthorizationError("application service credential requires an unstarted container")
    image = docker_image_inspect(policy["image_id"])
    container = docker_inspect(container_id)
    _render_actual_compose(container, policy, image)
    mounts = observed["mounts"]
    allowed = service["allowed_writable_roots"]
    writable = {item["target"] for item in mounts if item["read_only"] is False}
    if any(root not in writable for root in allowed):
        raise HostAuthorizationError("application service root is not an exact approved writable mount")
    destination = Path(credential_path)
    matched = [item for item in mounts if item["target"] == "/run/secrets/market-data-service.json"]
    if (len(matched) != 1 or matched[0]["read_only"] is not True
            or destination != Path(matched[0]["source"])):
        raise HostAuthorizationError("application service credential mount is missing or mismatched")
    source = _application_service_credential_path(destination, policy)
    if stat.S_IMODE(source.parent.stat().st_mode) != 0o700:
        raise HostAuthorizationError("application service credential host directory must be root-private")
    if source.stat().st_size != 0:
        raise HostAuthorizationError("application service credential must be newly allocated")
    # The existing Docker file-secret contract verifies that only the service
    # UID/GID can read this root-owned file; the host parent remains protected.
    _application_service_secret_identity(source, observed["config"].get("User"), policy)
    credential = {
        "schema_version": "application-service-credential/1",
        "role": role,
        "service_id": policy["service_id"],
        "module_id": policy["module_id"],
        "runtime_id": policy["runtime_id"],
        "runtime_marker_sha256": policy["runtime_marker_sha256"],
        "container_id": container_id,
        "deployment_id": os.urandom(16).hex(),
        "credential": os.urandom(32).hex(),
        "allowed_writable_roots": allowed,
    }
    # Open the exact mounted inode without following a replacement symlink.
    before = source.stat()
    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        current = os.fstat(descriptor)
        if ((current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                or current.st_size != 0):
            raise HostAuthorizationError("application service credential source changed")
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(_canonical(credential))
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    _fsync_directory(source.parent)
    final_observed = observe_and_validate(container_id, policy, role=role)
    if (_load_policy(expected_policy_path) != policy
            or final_observed["container_id"] != container_id
            or final_observed["state"].get("Status") != "created"
            or copy_container_bytes(container_id, "/run/secrets/market-data-service.json") != _canonical(credential)
            or _application_service_secret_identity(source, observed["config"].get("User"), policy)["sha256"] != hashlib.sha256(_canonical(credential)).hexdigest()):
        raise HostAuthorizationError("application service identity changed during issuance")
    return {"service_id": policy["service_id"], "runtime_id": policy["runtime_id"],
            "deployment_id": credential["deployment_id"]}


def _application_service_credential_path(source: Path, policy: Mapping) -> Path:
    """Use the existing temporary-path exception only inside a bound candidate scope."""
    if policy["role"] != "candidate_validation":
        return _protected_path(source)
    _absolute(str(source))
    _candidate_descriptor(policy, consume=False)
    if not _within(str(source), policy["candidate_host_root"]):
        raise HostAuthorizationError("application service credential is outside candidate scope")
    return _protected_path(source, temporary=True)


def _application_service_secret_identity(source: Path, user: str, policy: Mapping) -> dict:
    if policy["role"] == "candidate_validation":
        return _secret_file_identity(source, user, temporary=True)
    return _secret_file_identity(source, user)


def _validate_application_service_credential(container_id: str, policy: Mapping, observed: Mapping) -> None:
    """Before any legacy grant is signed, reject a credential from another deployment."""
    service = policy.get("application_service")
    if service is None:
        return
    matches = [item for item in observed["mounts"]
               if item["target"] == "/run/secrets/market-data-service.json"]
    if len(matches) != 1 or matches[0]["read_only"] is not True:
        raise HostAuthorizationError("application service secret mount is missing")
    source = _application_service_credential_path(Path(matches[0]["source"]), policy)
    if stat.S_IMODE(source.parent.stat().st_mode) != 0o700:
        raise HostAuthorizationError("application service credential host directory must be root-private")
    _application_service_secret_identity(source, observed["config"].get("User"), policy)
    payload = _json(source.read_bytes())
    required = {"schema_version", "role", "service_id", "module_id", "runtime_id",
                "runtime_marker_sha256", "container_id", "deployment_id", "credential",
                "allowed_writable_roots"}
    if (set(payload) != required or payload["schema_version"] != "application-service-credential/1"
            or payload["role"] != policy["role"]
            or any(payload[name] != policy[name] for name in ("service_id", "module_id", "runtime_id", "runtime_marker_sha256"))
            or payload["container_id"] != container_id
            or not isinstance(payload["deployment_id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", payload["deployment_id"])
            or not isinstance(payload["credential"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", payload["credential"])
            or payload["allowed_writable_roots"] != service["allowed_writable_roots"]):
        raise HostAuthorizationError("application service credential belongs to another deployment or policy")
    if copy_container_bytes(container_id, "/run/secrets/market-data-service.json") != _canonical(payload):
        raise HostAuthorizationError("application service credential injection differs from approved source")


def issue_execution_grant(container_id: str, *, expected_policy_path: str | Path, key_path: str | Path, grant_path: str | Path, grant_dir: str | Path, role: str, ttl_seconds: int = 900, external_candidate_trust_path: str | Path | None = None, primary_rollback_context=None) -> dict:
    """Observe a fresh container, sign a bounded grant, and leave it unstarted."""
    _require_linux_root()
    require_protected_authority_source()
    policy = _load_policy(expected_policy_path)
    validate_policy(policy, role)
    if external_candidate_trust_path is not None and role != "candidate_validation":
        raise HostAuthorizationError("external candidate trust cannot authorize production")
    require_protected_key_and_grant_dirs(key_path, grant_dir)
    if type(ttl_seconds) is not int or not 0 < ttl_seconds <= 3600:
        raise HostAuthorizationError("invalid grant lifetime")
    grant_dir, destination = Path(grant_dir), Path(grant_path)
    if not destination.is_absolute() or destination.parent != grant_dir or destination.exists() or destination.is_symlink():
        raise HostAuthorizationError("grant path must be new and inside its protected directory")
    policy_version = policy["schema_version"]
    if primary_rollback_context is not None and role != 'production':
        raise HostAuthorizationError('historical context cannot authorize a candidate instance')
    candidate_record = _acceptance_for_instance(policy, primary_rollback_context) if policy_version in _PRODUCTION_POLICIES else None
    if policy_version in _CANDIDATE_POLICIES and role == "candidate_validation":
        # Consumption deliberately precedes every fallible Docker observation.
        # A failed attempt is not replayable with a mutated scope.
        _candidate_descriptor(policy, consume=True)
    observed = observe_and_validate(container_id, policy, role=role)
    external_enabled = external_candidate_trust_path is not None
    environment = _env(observed["config"]["Env"])
    external_flag = environment.get("MARKET_DATA_CANDIDATE_EXTERNAL_TRUST")
    if external_flag not in (None, "1") or external_enabled != (external_flag == "1"):
        raise HostAuthorizationError("external candidate trust enablement differs from container")
    _validate_application_service_credential(container_id, policy, observed)
    secret_state = _secret_state(observed) if policy_version in _PRODUCTION_POLICIES else None
    _validate_mount_sources(observed, policy, grant_dir)
    if role == "candidate_validation":
        _protected_path(Path(policy["candidate_host_root"]), directory=True, temporary=True)
    container = docker_inspect(container_id)
    image = docker_image_inspect(policy["image_id"])
    rendered_digest = (_render_actual_compose(container, policy, image) if primary_rollback_context is None else
        _render_actual_compose(container, policy, image, primary_rollback_context=primary_rollback_context))
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
    external_raw = None
    if external_enabled:
        grant_contract = _contract_module(GRANT_CONTRACT_PATH, "_external_candidate_trust_contract")
        path = Path(external_candidate_trust_path)
        expected = grant_dir / grant_contract.EXTERNAL_CANDIDATE_TRUST_NAME
        if path != expected or path.is_symlink():
            raise HostAuthorizationError("external candidate trust path is not fixed")
        _protected_path(path)
        if stat.S_IMODE(path.stat().st_mode) != 0o444:
            raise HostAuthorizationError("external candidate trust must be public read-only")
        external_raw = path.read_bytes()
        try:
            external_key_id, external_public = grant_contract.parse_external_candidate_trust(external_raw)
        except (ValueError, TypeError) as exc:
            raise HostAuthorizationError("external candidate trust contract is invalid") from exc
        if external_key_id != policy["key_id"] or base64.b64encode(external_public).decode("ascii") != public:
            raise HostAuthorizationError("external candidate signing key does not match public trust")
    else:
        matches = [item for item in trust.get("keys", []) if item.get("key_id") == policy["key_id"] and item.get("domain") == role and item.get("algorithm") == "ed25519" and item.get("public_key_base64") == public]
        if len(matches) != 1 or policy["key_id"] in trust.get("revoked_key_ids", []):
            raise HostAuthorizationError("host signing key is not pinned for this authorization role")
    now = datetime.now(timezone.utc)
    payload = {key: policy[key] for key in ("project_id", "module_id", "service_id", "runtime_id", "approved_commit", "approved_tree", "image_id", "runtime_root", "runtime_manifest_sha256", "runtime_marker_sha256")}
    grant_id = (_recovery_call('grant_id', policy, container_id)
                if 'recovery' in policy else os.urandom(16).hex())
    payload.update(grant_id=grant_id, issued_at=now.isoformat(), expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(), identity_kind="oci_container", authorization_mode=role, role=role, artifact_origin=observed["image_labels"]["market-data.artifact.origin"], release_commit=policy["approved_commit"], release_tree=policy["approved_tree"], release_sha256=observed["release_sha256"], rendered_compose_sha256=rendered_digest, mount_contract_sha256=_digest(mounts), actual_config_sha256=observed["actual_config_sha256"], container_id=container_id, hostname_nonce=observed["config"]["Hostname"], writable_roots=writable, protected_mounts=protected)
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
    if (_render_actual_compose(final_container, policy, final_image) if primary_rollback_context is None else
        _render_actual_compose(final_container, policy, final_image, primary_rollback_context=primary_rollback_context)) != rendered_digest:
        raise HostAuthorizationError("Compose deployment changed before grant sealing")
    if (copy_container_bytes(container_id, policy["runtime_manifest_path"]) != manifest_raw
            or copy_container_bytes(container_id, marker_path) != marker_raw
            or copy_container_bytes(container_id, policy["source_root"] + "/02_configs/production_runtime_trust.json") != trust_raw):
        raise HostAuthorizationError("identity material changed before grant sealing")
    _validate_v3_runtime(manifest, observed, policy, container_id)
    if policy_version in _CANDIDATE_POLICIES and role == "candidate_validation":
        _candidate_descriptor(policy, consume=False)
    if (_load_policy(expected_policy_path) != policy or TRUST_CONFIG_PATH.read_bytes() != trust_raw
            or (external_enabled and Path(external_candidate_trust_path).read_bytes() != external_raw)):
        raise HostAuthorizationError("host approval or trust changed before grant sealing")
    if policy_version in _PRODUCTION_POLICIES and _acceptance_for_instance(policy, primary_rollback_context) != candidate_record:
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


def validate_recovery_post_start(container_id: str, *, expected_policy_path: str | Path,
                                 grant_path: str | Path, key_path: str | Path) -> dict:
    """Check the signed grant and materialized endpoint before health or HTTP."""
    _require_linux_root()
    require_protected_authority_source()
    policy = _load_policy(expected_policy_path)
    validate_policy(policy, 'production')
    if 'recovery' not in policy:
        raise HostAuthorizationError('post-start network validation requires recovery policy')
    grant_source = next(m['source'] for m in policy['mounts']
                        if m['target'] == policy['grant_container_directory'])
    path = Path(grant_path)
    if path.parent != Path(grant_source):
        raise HostAuthorizationError('post-start grant is outside the fresh grant directory')
    raw = _protected_path(path).read_bytes()
    envelope = _json(raw)
    from cryptography.exceptions import InvalidSignature
    try:
        contract = _contract_module(GRANT_CONTRACT_PATH, '_recovery_post_start_grant')
        contract.validate_execution_grant_envelope(envelope)
        key = _load_private_key(key_path)
        trust = _json(TRUST_CONFIG_PATH.read_bytes())
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode('ascii')
        matches = [item for item in trust.get('keys', [])
                   if item.get('key_id') == policy['key_id'] and item.get('domain') == 'production'
                   and item.get('algorithm') == 'ed25519' and item.get('public_key_base64') == public]
        if (len(matches) != 1 or envelope['key_id'] != policy['key_id']
                or policy['key_id'] in trust.get('revoked_key_ids', [])
                or envelope['payload']['grant_id'] in trust.get('revoked_grant_ids', [])):
            raise HostAuthorizationError('post-start grant signing authority differs')
        key.public_key().verify(base64.b64decode(envelope['signature'], validate=True),
                                _canonical(envelope['payload']))
    except (AttributeError, TypeError, ValueError, KeyError, InvalidSignature) as exc:
        raise HostAuthorizationError('post-start recovery grant is invalid') from exc
    container = docker_inspect(container_id)
    image = docker_image_inspect(policy['image_id'])
    if container.get('Image') != policy['image_id'] or image.get('Id') != policy['image_id']:
        raise HostAuthorizationError('post-start recovery image differs')
    rendered = _render_actual_compose(container, policy, image, recovery_phase='post_start')
    network = _recovery_call('validate_instance', container, policy, 'post_start')
    release_raw = copy_container_bytes(container_id, policy['source_root'] + '/RELEASE.json')
    observed = normalize_observation(container, image, _json(release_raw))
    payload = envelope['payload']
    expected_grant_id = _recovery_call('grant_id', policy, container_id)
    now = datetime.now(timezone.utc)
    if (payload['grant_id'] != expected_grant_id or payload['role'] != 'production'
            or payload['container_id'] != container_id or payload['image_id'] != policy['image_id']
            or payload['approved_commit'] != policy['approved_commit']
            or payload['approved_tree'] != policy['approved_tree']
            or payload['hostname_nonce'] != policy['recovery']['nonce']
            or payload['rendered_compose_sha256'] != rendered
            or payload['mount_contract_sha256'] != _digest(observed['mounts'])
            or payload['release_sha256'] != hashlib.sha256(release_raw).hexdigest()
            or payload['release_sha256'] != policy['release_sha256']
            or payload['actual_config_sha256'] != policy['actual_config_sha256']
            or not datetime.fromisoformat(payload['issued_at']) <= now <
                   datetime.fromisoformat(payload['expires_at'])):
        raise HostAuthorizationError('post-start grant does not bind the actual recovery instance')
    comparison = compare_observed_config(observed, payload['actual_config_sha256'])
    if (_load_policy(expected_policy_path) != policy or _protected_path(path).read_bytes() != raw
            or _recovery_call('validate_instance', docker_inspect(container_id), policy, 'post_start') != network):
        raise HostAuthorizationError('recovery identity changed during post-start validation')
    return {'POST_START_NETWORK_IDENTITY_VALIDATION': 'PASS', **network,
            'grant_id': payload['grant_id'], 'config_comparison': comparison}


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--grant-directory", required=True)
    parser.add_argument("--grant", required=True)
    parser.add_argument("--role", choices=("production", "candidate_validation"), required=True)
    parser.add_argument("--validate-recovery-post-start", action="store_true")
    parser.add_argument("--issue-application-service-credential", action="store_true")
    parser.add_argument("--application-service-credential")
    args = parser.parse_args(argv)
    try:
        if args.issue_application_service_credential:
            if args.validate_recovery_post_start or not args.application_service_credential:
                raise HostAuthorizationError("application service credential action is incomplete")
            result = issue_application_service_credential(
                args.container_id, expected_policy_path=args.policy,
                credential_path=args.application_service_credential, role=args.role)
            print(json.dumps({"APPLICATION_SERVICE_CREDENTIAL": "ISSUED", **result}))
            return 0
        if args.validate_recovery_post_start:
            if args.role != "production":
                raise HostAuthorizationError("post-start recovery validation requires production role")
            result = validate_recovery_post_start(args.container_id, expected_policy_path=args.policy,
                                                  grant_path=args.grant, key_path=args.key)
            print(json.dumps(result))
            return 0
        result = issue_execution_grant(args.container_id, expected_policy_path=args.policy, key_path=args.key, grant_path=args.grant, grant_dir=args.grant_directory, role=args.role)
        print(json.dumps({"HOST_AUTHORIZATION": "PASS", "grant_id": result["payload"]["grant_id"], "container_id": args.container_id}))
        return 0
    except (HostAuthorizationError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"HOST_AUTHORIZATION": "FAIL", "reason": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
