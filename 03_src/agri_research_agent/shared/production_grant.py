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


class GrantShapeError(ValueError):
    """An execution grant is not one of the exact supported JSON shapes."""


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
    expected = _V1_PAYLOAD_FIELDS if version == "production-execution-grant/1" else _V2_PAYLOAD_FIELDS
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
    if version == "production-execution-grant/2":
        if payload["runtime_manifest_schema_version"] != "runtime-manifest/2":
            _fail("invalid runtime manifest schema version")
        _string(payload["identity_root_role"], "invalid identity root role", _ID)
        scope_id, scope_hash = payload["candidate_scope_id"], payload["candidate_scope_sha256"]
        if role == "production":
            if scope_id is not None or scope_hash is not None:
                _fail("production grant must not carry a candidate scope")
        else:
            _string(scope_id, "invalid candidate scope id", _HEX32)
            _string(scope_hash, "invalid candidate scope hash", _HEX64)


def validate_execution_grant_envelope(value: object) -> None:
    """Validate a v1 or v2 grant envelope without performing authorization."""
    try:
        primitive = type(value) is dict and _primitive(value)
    except RecursionError:
        _fail("execution grant is too deeply nested")
    if not primitive:
        _fail("execution grant must be a primitive JSON object")
    if set(value) != _ENVELOPE_FIELDS:
        _fail("execution grant envelope fields incomplete or unknown")
    version = value["schema_version"]
    if version not in {"production-execution-grant/1", "production-execution-grant/2"}:
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
