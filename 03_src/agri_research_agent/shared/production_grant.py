"""Pure structural validation for signed production execution grants.

This module does not load trust anchors, verify signatures, inspect a host or
container, or grant authority.  It deliberately owns only the common JSON
shape consumed by the host signer and the in-container verifier.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import re
from typing import Mapping
from urllib.parse import urlsplit


class GrantShapeError(ValueError):
    """An execution grant is not one of the exact supported JSON shapes."""


class GrantValidationError(ValueError):
    """A runtime instance does not satisfy its signed v3 environment contract."""


_ENVELOPE_FIELDS = frozenset({"schema_version", "algorithm", "key_id", "payload", "signature"})
_V1_PAYLOAD_FIELDS = frozenset({
    "grant_id", "issued_at", "expires_at", "identity_kind", "authorization_mode",
    "artifact_origin", "role", "project_id", "module_id", "service_id", "runtime_id",
    "approved_commit", "approved_tree", "release_commit", "release_tree", "image_id",
    "release_sha256", "runtime_manifest_sha256", "runtime_marker_sha256", "runtime_root",
    "writable_roots", "protected_mounts", "rendered_compose_sha256",
    "mount_contract_sha256", "actual_config_sha256", "container_id", "hostname_nonce",
})
_V2_EXTRA_FIELDS = frozenset({
    "runtime_manifest_schema_version", "identity_root_role", "candidate_scope_id",
    "candidate_scope_sha256",
})
_V2_PAYLOAD_FIELDS = _V1_PAYLOAD_FIELDS | _V2_EXTRA_FIELDS
_V4_EXTRA_FIELDS = frozenset({
    "task_id", "task_request_id", "task_request_sha256",
    "runtime_observation_sha256", "approved_read_roots",
    "approved_secret_targets",
})
_V4_PAYLOAD_FIELDS = _V2_PAYLOAD_FIELDS | _V4_EXTRA_FIELDS
_ID = re.compile(r"[a-z][a-z0-9-]*\Z")
_KEY_ID = re.compile(r"[a-z][a-z0-9-]{0,127}\Z")
_HEX32 = re.compile(r"[a-f0-9]{32}\Z")
_HEX40 = re.compile(r"[a-f0-9]{40}\Z")
_HEX64 = re.compile(r"[a-f0-9]{64}\Z")
_IMAGE = re.compile(r"sha256:[a-f0-9]{64}\Z")
_SIGNATURE = re.compile(r"[A-Za-z0-9+/]{86}==\Z")
_PATH = re.compile(r"/[^\\]*\Z")
_RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})\Z"
)


def _fail(message: str) -> None:
    raise GrantShapeError(message)


def _primitive(value: object) -> bool:
    if value is None or type(value) in (str, int, bool):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_primitive(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _primitive(item) for key, item in value.items())
    return False


def _string(value: object, message: str, pattern: re.Pattern[str] | None = None) -> str:
    if type(value) is not str or (pattern is not None and pattern.fullmatch(value) is None):
        _fail(message)
    return value


def _path(value: object, message: str) -> str:
    result = _string(value, message, _PATH)
    if len(result) < 2 or any(part in {"", ".", ".."} for part in result[1:].split("/")):
        _fail(message)
    return result


def _date_time(value: object, message: str) -> None:
    text = _string(value, message)
    if _RFC3339.fullmatch(text) is None:
        _fail(message)
    # Python 3.10 does not parse a trailing Z itself.  RFC3339 permits both
    # upper and lower forms; normalize only for calendar/offset validation.
    normalized = text[:-1] + "+00:00" if text[-1:] in {"Z", "z"} else text
    try:
        datetime.fromisoformat(normalized.replace("t", "T", 1))
    except ValueError as exc:
        raise GrantShapeError(message) from exc


def _paths(value: object, message: str) -> None:
    if type(value) is not list or not value:
        _fail(message)
    paths = tuple(_path(item, message) for item in value)
    if len(paths) != len(set(paths)):
        _fail(message)


def _validate_payload(payload: object, version: str) -> None:
    if type(payload) is not dict:
        _fail("execution grant payload must be an object")
    expected = (_V1_PAYLOAD_FIELDS if version == "production-execution-grant/1" else
                _V4_PAYLOAD_FIELDS if version == "production-execution-grant/4" else
                _V2_PAYLOAD_FIELDS)
    if set(payload) != expected:
        _fail("execution grant payload fields incomplete or unknown")
    _string(payload["grant_id"], "invalid grant id", _HEX32)
    _date_time(payload["issued_at"], "invalid grant issue time")
    _date_time(payload["expires_at"], "invalid grant expiry time")
    if payload["identity_kind"] != "oci_container":
        _fail("invalid execution identity kind")
    role = payload["role"]
    if role not in {"production", "candidate_validation"}:
        _fail("invalid execution authorization role")
    if payload["authorization_mode"] != role:
        _fail("authorization mode does not match role")
    origin = payload["artifact_origin"]
    if origin not in {"candidate", "production"}:
        _fail("invalid artifact origin")
    if role == "candidate_validation" and origin != "candidate":
        _fail("candidate validation requires a candidate artifact")
    for name in ("project_id", "module_id", "service_id", "runtime_id"):
        _string(payload[name], f"invalid {name}", _ID)
    for name in ("approved_commit", "approved_tree", "release_commit", "release_tree"):
        _string(payload[name], f"invalid {name}", _HEX40)
    _string(payload["image_id"], "invalid image id", _IMAGE)
    for name in ("release_sha256", "runtime_manifest_sha256", "runtime_marker_sha256",
                 "rendered_compose_sha256", "mount_contract_sha256", "actual_config_sha256"):
        _string(payload[name], f"invalid {name}", _HEX64)
    _path(payload["runtime_root"], "invalid runtime root")
    _paths(payload["writable_roots"], "invalid writable roots")
    _paths(payload["protected_mounts"], "invalid protected mounts")
    _string(payload["container_id"], "invalid container id", _HEX64)
    _string(payload["hostname_nonce"], "invalid hostname nonce", _HEX32)
    if version in {"production-execution-grant/2", "production-execution-grant/3",
                   "production-execution-grant/4"}:
        expected_manifest = "runtime-manifest/2" if version.endswith("/2") else "runtime-manifest/3"
        if payload["runtime_manifest_schema_version"] != expected_manifest:
            _fail("invalid runtime manifest schema version")
        _string(payload["identity_root_role"], "invalid identity root role", _ID)
        scope_id, scope_hash = payload["candidate_scope_id"], payload["candidate_scope_sha256"]
        if role == "production":
            if scope_id is not None or scope_hash is not None:
                _fail("production grant must not carry a candidate scope")
        else:
            _string(scope_id, "invalid candidate scope id", _HEX32)
            _string(scope_hash, "invalid candidate scope hash", _HEX64)
    if version == "production-execution-grant/4":
        _string(payload["task_id"], "invalid task id", _ID)
        _string(payload["task_request_id"], "invalid task request id", _HEX32)
        for name in ("task_request_sha256", "runtime_observation_sha256"):
            _string(payload[name], f"invalid {name}", _HEX64)
        reads = payload["approved_read_roots"]
        if type(reads) is not list:
            _fail("invalid approved read roots")
        read_paths = tuple(_path(item, "invalid approved read roots") for item in reads)
        if len(read_paths) != len(set(read_paths)):
            _fail("invalid approved read roots")
        secrets = payload["approved_secret_targets"]
        if type(secrets) is not list:
            _fail("invalid approved secret targets")
        secret_paths = tuple(_path(item, "invalid approved secret targets") for item in secrets)
        if len(secret_paths) != len(set(secret_paths)) or any(
                not path.startswith("/run/secrets/") for path in secret_paths):
            _fail("invalid approved secret targets")


def validate_execution_grant_envelope(value: object) -> None:
    """Validate a v1-v4 grant envelope without authorization."""
    try:
        primitive = type(value) is dict and _primitive(value)
    except RecursionError:
        _fail("execution grant is too deeply nested")
    if not primitive:
        _fail("execution grant must be a primitive JSON object")
    if set(value) != _ENVELOPE_FIELDS:
        _fail("execution grant envelope fields incomplete or unknown")
    version = value["schema_version"]
    if version not in {"production-execution-grant/1", "production-execution-grant/2",
                       "production-execution-grant/3", "production-execution-grant/4"}:
        _fail("unsupported execution grant schema")
    if value["algorithm"] != "ed25519":
        _fail("unsupported execution grant algorithm")
    _string(value["key_id"], "invalid grant key id", _KEY_ID)
    _string(value["signature"], "invalid grant signature", _SIGNATURE)
    _validate_payload(value["payload"], version)


def _duplicate_rejecting_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            _fail("duplicate JSON object key")
        value[key] = item
    return value


def _reject_constant(_: str) -> object:
    _fail("non-finite JSON number is not allowed")


def parse_execution_grant_json(raw: bytes) -> dict:
    """Strictly parse a grant and return its validated, fresh payload mapping."""
    if type(raw) is not bytes:
        _fail("execution grant input must be bytes")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_rejecting_object,
                           parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise GrantShapeError("execution grant is not valid UTF-8 JSON") from exc
    validate_execution_grant_envelope(value)
    # json.loads returns a new object; callers receive no reference to internal state.
    return value["payload"]


_ENVIRONMENT_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
_LOGICAL_PATH = re.compile(r"/[^\\]*\Z")


def _runtime_environment_error(message: str) -> None:
    raise GrantValidationError(message)


def _logical_path(value: object, message: str) -> str:
    if type(value) is not str or _LOGICAL_PATH.fullmatch(value) is None or len(value) < 2:
        _runtime_environment_error(message)
    if any(part in {"", ".", ".."} for part in value[1:].split("/")):
        _runtime_environment_error(message)
    return value


def _environment_name(value: object, message: str) -> str:
    if type(value) is not str or _ENVIRONMENT_NAME.fullmatch(value) is None:
        _runtime_environment_error(message)
    return value


def _deployment_value(value: object, value_type: object) -> str:
    # This is deliberately kept consistent with runtime_manifest._deployment_value.
    if type(value) is not str or not value or value.strip() != value or "\x00" in value:
        _runtime_environment_error("invalid deployment environment value")
    if len(value) > 2048:
        _runtime_environment_error("deployment environment value is too long")
    if value_type == "nonempty":
        if re.fullmatch(r"[A-Za-z0-9_./:+-]+", value) is None:
            _runtime_environment_error("invalid deployment environment value")
        return value
    if value_type not in {"http_url", "https_url"}:
        _runtime_environment_error("unsupported deployment environment value type")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (parsed.scheme in ({"http", "https"} if value_type == "http_url" else {"https"})
                 and parsed.hostname and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment and not any(char.isspace() for char in value)
                 and "\\" not in value and (port is None or 0 < port < 65536))
    except ValueError:
        valid = False
    if not valid:
        _runtime_environment_error("invalid deployment environment URL")
    return value


def validate_runtime_environment(manifest: dict, environment: Mapping[str, str], *, role: str,
                                 grant_path: str) -> None:
    """Validate v3's declared environment against an observed runtime mapping.

    This is intentionally a pure consumer: it accepts only logical container
    paths and does not import runtime_manifest private helpers, so host code
    can load it independently of the application package.
    """
    if type(manifest) is not dict or manifest.get("schema_version") != "runtime-manifest/3":
        _runtime_environment_error("runtime environment requires a v3 manifest")
    if role not in {"production", "candidate_validation"}:
        _runtime_environment_error("invalid runtime environment role")
    if not isinstance(environment, Mapping) or any(type(key) is not str or type(value) is not str
                                                    for key, value in environment.items()):
        _runtime_environment_error("runtime environment must be a string mapping")
    trusted_grant = _logical_path(grant_path, "invalid trusted execution grant path")
    required_raw, bindings = manifest.get("required_environment"), manifest.get("environment_bindings")
    forbidden_raw, roots_raw = manifest.get("forbidden_environment"), manifest.get("runtime_roots")
    if type(required_raw) is not list or type(bindings) is not list or type(forbidden_raw) is not list or type(roots_raw) is not list:
        _runtime_environment_error("runtime environment manifest declarations are invalid")
    required = {_environment_name(item, "invalid required environment name") for item in required_raw}
    if len(required) != len(required_raw):
        _runtime_environment_error("duplicate required environment name")
    forbidden = {_environment_name(item, "invalid forbidden environment name") for item in forbidden_raw}
    if len(forbidden) != len(forbidden_raw) or required & forbidden:
        _runtime_environment_error("invalid forbidden environment declaration")
    roots: dict[str, str] = {}
    for item in roots_raw:
        if type(item) is not dict or set(item) != {"role", "container_path", "access"}:
            _runtime_environment_error("invalid runtime root declaration")
        root_role = item["role"]
        if type(root_role) is not str or _ID.fullmatch(root_role) is None or root_role in roots:
            _runtime_environment_error("invalid runtime root declaration")
        roots[root_role] = _logical_path(item["container_path"], "invalid runtime root path")
    shapes = {"literal": {"name", "kind", "value"}, "runtime_path": {"name", "kind", "role", "relative_path"},
              "deployment": {"name", "kind", "value_type", "candidate_value"}, "execution_grant": {"name", "kind"}}
    names: set[str] = set()
    for binding in bindings:
        if type(binding) is not dict or type(binding.get("kind")) is not str:
            _runtime_environment_error("invalid environment binding")
        kind = binding["kind"]
        if kind not in shapes or set(binding) != shapes[kind]:
            _runtime_environment_error("environment binding fields incomplete or unknown")
        name = _environment_name(binding.get("name"), "invalid environment binding name")
        if (name == "MARKET_DATA_EXECUTION_GRANT") != (kind == "execution_grant"):
            _runtime_environment_error("execution grant environment binding is invalid")
        if name in names or name not in required:
            _runtime_environment_error("duplicate or undeclared environment binding")
        names.add(name)
        actual = environment.get(name)
        if actual is None:
            _runtime_environment_error("required runtime environment is missing")
        if kind == "literal":
            if type(binding["value"]) is not str or actual != binding["value"]:
                _runtime_environment_error("literal runtime environment differs from manifest")
        elif kind == "runtime_path":
            binding_role = binding["role"]
            if type(binding_role) is not str or _ID.fullmatch(binding_role) is None:
                _runtime_environment_error("invalid runtime path binding")
            root = roots.get(binding_role)
            relative = binding["relative_path"]
            if root is None or type(relative) is not str or "\\" in relative or relative.startswith("/") or any(part in {"", ".", ".."} for part in relative.split("/")) and relative != "":
                _runtime_environment_error("invalid runtime path binding")
            expected = root if relative == "" else root + "/" + relative
            if actual != expected:
                _runtime_environment_error("runtime path environment differs from manifest")
        elif kind == "deployment":
            candidate = _deployment_value(binding["candidate_value"], binding["value_type"])
            if role == "candidate_validation" and actual != candidate:
                _runtime_environment_error("candidate deployment environment differs from manifest")
            _deployment_value(actual, binding["value_type"])
        elif actual != trusted_grant:
            _runtime_environment_error("execution grant environment differs from trusted grant path")
    if names != required:
        _runtime_environment_error("environment bindings do not cover required environment")
    if any(name in environment for name in forbidden):
        _runtime_environment_error("forbidden runtime environment is present")
