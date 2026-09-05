"""Strict verifier for host-signed target-runtime validation records.

This module verifies a candidate-validation *fact*.  It does not issue grants,
select an Approved commit, or authorize production execution.  ``revoked_grant_ids``
from the shared trust configuration is deliberately also the revocation namespace
for ``record_id`` values in this signed-record domain.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class CandidateValidationRecordError(ValueError):
    """The record, trust anchor, or signed validation fact is invalid."""


_ID = re.compile(r"[a-z][a-z0-9-]*\Z")
_KEY_ID = re.compile(r"[a-z][a-z0-9-]{0,127}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})\Z")
_ENVELOPE_FIELDS = frozenset({"schema_version", "algorithm", "key_id", "payload", "signature"})
_PAYLOAD_FIELDS = frozenset({"record_id", "purpose", "authorization_role", "issued_at", "expires_at", "evidence_sha256", "evidence"})
_EVIDENCE_FIELDS = frozenset({"schema_version", "binding", "TARGET_RUNTIME_STATIC_VALIDATION", "TARGET_RUNTIME_CONTAINER_VALIDATION", "image_id", "rendered_compose_sha256", "builder", "observed_identity", "probes"})
_BINDING_FIELDS = frozenset({"project_id", "commit", "tree", "source_sha256", "validator_version"})
_BUILDER_FIELDS = frozenset({"builder_id", "os", "execution"})
_OBSERVED_FIELDS = frozenset({"image_id", "oci_revision", "git_tree", "source_sha256", "rendered_compose_sha256", "authorization_role", "git_metadata_present", "production_volumes_mounted"})
_TRUST_FIELDS = frozenset({"schema_version", "keys", "revoked_key_ids", "revoked_grant_ids"})
_TRUST_KEY_FIELDS = frozenset({"key_id", "domain", "algorithm", "public_key_base64"})
_PROBES = frozenset({"entrypoint_initialization", "runtime_identity", "dependencies", "runtime_paths", "mount_permissions", "missing_grant_rejected", "wrong_commit_rejected", "wrong_tree_rejected", "wrong_image_rejected", "wrong_service_rejected", "wrong_manifest_rejected", "preview_write_rejected", "release_mismatch_rejected"})
# Keep the historical singular constant for callers that pin the original
# validator, while explicitly allowing the v2 validator binding in the same
# signed record schema.  The binding remains an opaque, exact version value;
# no other versions are accepted.
_VALIDATOR_VERSION = "target-runtime-validator/1"
_VALIDATOR_VERSIONS = frozenset({_VALIDATOR_VERSION, "target-runtime-validator/2"})


def _fail(message: str) -> None:
    raise CandidateValidationRecordError(message)


def canonical(value: object) -> bytes:
    """Return the only bytes accepted for evidence hashes and signatures."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise CandidateValidationRecordError("record value is not canonicalizable") from exc


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


def _string(value: object, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if type(value) is not str or (pattern is not None and pattern.fullmatch(value) is None):
        _fail(f"invalid {label}")
    return value


def _time(value: object, label: str) -> datetime:
    text = _string(value, label)
    if _RFC3339.fullmatch(text) is None:
        _fail(f"invalid {label}")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text[-1:] in {"Z", "z"} else text)
    except ValueError as exc:
        raise CandidateValidationRecordError(f"invalid {label}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(f"invalid {label}")
    return parsed.astimezone(timezone.utc)


def _source_path(value: object) -> str:
    path = _string(value, "source input path")
    if (not path or path.startswith("/") or "\\" in path or ":" in path
            or any(character in path for character in "*?[]")
            or any(ord(character) < 32 or character.isspace() for character in path)
            or any(part in {"", ".", ".."} for part in path.split("/"))):
        _fail("invalid source input path")
    return path


def _validate_evidence(evidence: object) -> None:
    if type(evidence) is not dict or set(evidence) != _EVIDENCE_FIELDS:
        _fail("target runtime evidence fields incomplete or unknown")
    if evidence["schema_version"] != "target-runtime-evidence/1":
        _fail("unsupported target runtime evidence schema")
    binding = evidence["binding"]
    if type(binding) is not dict or set(binding) != _BINDING_FIELDS:
        _fail("target runtime binding fields incomplete or unknown")
    _string(binding["project_id"], "project id", _ID)
    _string(binding["commit"], "candidate commit", _HEX40)
    _string(binding["tree"], "candidate tree", _HEX40)
    if (type(binding["validator_version"]) is not str
            or binding["validator_version"] not in _VALIDATOR_VERSIONS):
        _fail("unsupported validator version")
    source = binding["source_sha256"]
    if type(source) is not dict or not source:
        _fail("candidate source hashes are invalid")
    paths = [_source_path(key) for key in source]
    if len(paths) != len({path.casefold() for path in paths}):
        _fail("candidate source hashes contain aliases")
    for value in source.values():
        _string(value, "candidate source hash", _HEX64)
    if evidence["TARGET_RUNTIME_STATIC_VALIDATION"] != "PASS" or evidence["TARGET_RUNTIME_CONTAINER_VALIDATION"] != "PASS":
        _fail("target runtime validation did not pass")
    _string(evidence["image_id"], "candidate image id", _IMAGE)
    _string(evidence["rendered_compose_sha256"], "candidate rendered compose hash", _HEX64)
    builder = evidence["builder"]
    if type(builder) is not dict or set(builder) != _BUILDER_FIELDS or builder.get("os") != "linux" or builder.get("execution") != "isolated":
        _fail("candidate builder identity is invalid")
    if not isinstance(builder["builder_id"], str) or not builder["builder_id"].strip():
        _fail("candidate builder identity is invalid")
    observed = evidence["observed_identity"]
    expected_observed = {"image_id": evidence["image_id"], "oci_revision": binding["commit"], "git_tree": binding["tree"], "source_sha256": source, "rendered_compose_sha256": evidence["rendered_compose_sha256"], "authorization_role": "candidate_validation", "git_metadata_present": False, "production_volumes_mounted": False}
    if (type(observed) is not dict or type(observed.get("git_metadata_present")) is not bool
            or type(observed.get("production_volumes_mounted")) is not bool
            or observed != expected_observed):
        _fail("observed candidate identity differs from evidence binding")
    probes = evidence["probes"]
    if type(probes) is not dict or set(probes) != _PROBES or any(value != "PASS" for value in probes.values()):
        _fail("candidate probes are incomplete or failed")


def validate_payload(payload: object) -> None:
    """Validate the exact signed validation-fact payload without trusting it."""
    if type(payload) is not dict or not _primitive(payload) or set(payload) != _PAYLOAD_FIELDS:
        _fail("candidate validation record payload fields incomplete or unknown")
    _string(payload["record_id"], "record id", _HEX32)
    if payload["purpose"] != "target-runtime-validation" or payload["authorization_role"] != "candidate_validation":
        _fail("candidate validation record purpose or role is invalid")
    issued, expires = _time(payload["issued_at"], "record issue time"), _time(payload["expires_at"], "record expiry time")
    if expires <= issued or expires - issued > timedelta(days=7):
        _fail("candidate validation record lifetime is invalid")
    evidence = payload["evidence"]
    _validate_evidence(evidence)
    expected_hash = hashlib.sha256(canonical(evidence)).hexdigest()
    if _string(payload["evidence_sha256"], "evidence sha256", _HEX64) != expected_hash:
        _fail("candidate evidence hash differs from evidence")


def _key_for_record(trust: object, key_id: str, record_id: str) -> Ed25519PublicKey:
    if type(trust) is not dict or set(trust) != _TRUST_FIELDS or trust.get("schema_version") != "production-runtime-trust/1":
        _fail("candidate validation trust configuration is invalid")
    keys, revoked_keys, revoked_records = trust["keys"], trust["revoked_key_ids"], trust["revoked_grant_ids"]
    if type(keys) is not list or type(revoked_keys) is not list or type(revoked_records) is not list:
        _fail("candidate validation trust configuration is invalid")
    if any(type(value) is not str or not value for value in [*revoked_keys, *revoked_records]):
        _fail("candidate validation trust revocation list is invalid")
    if key_id in revoked_keys or record_id in revoked_records:
        _fail("candidate validation record is revoked")
    found: Ed25519PublicKey | None = None
    seen_ids: set[str] = set()
    seen_material: set[str] = set()
    for item in keys:
        if type(item) is not dict or set(item) != _TRUST_KEY_FIELDS:
            _fail("candidate validation trust key is invalid")
        item_id = _string(item.get("key_id"), "trust key id", _KEY_ID)
        if item_id in seen_ids:
            _fail("duplicate candidate validation trust key")
        seen_ids.add(item_id)
        if item.get("domain") not in {"production", "candidate_validation"} or item.get("algorithm") != "ed25519":
            _fail("candidate validation trust key is invalid")
        material = _string(item.get("public_key_base64"), "trust public key")
        if material in seen_material:
            _fail("duplicate candidate validation trust key material")
        seen_material.add(material)
        try:
            decoded = base64.b64decode(material, validate=True)
            public = Ed25519PublicKey.from_public_bytes(decoded)
        except (ValueError, TypeError) as exc:
            raise CandidateValidationRecordError("candidate validation trust public key is invalid") from exc
        if item_id == key_id:
            if item["domain"] != "candidate_validation":
                _fail("candidate validation record uses wrong key domain")
            found = public
    if found is None:
        _fail("candidate validation signing key is untrusted")
    return found


def verify_record(raw: bytes, trust: dict, *, now: datetime | None = None) -> dict:
    """Strictly verify a signed candidate-validation record and return its payload."""
    if type(raw) is not bytes:
        _fail("candidate validation record input must be bytes")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                _fail("duplicate JSON object key")
            result[key] = value
        return result

    def nonfinite(_: str) -> object:
        _fail("non-finite JSON number is not allowed")

    try:
        envelope = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CandidateValidationRecordError("candidate validation record is not valid UTF-8 JSON") from exc
    if type(envelope) is not dict or not _primitive(envelope) or set(envelope) != _ENVELOPE_FIELDS:
        _fail("candidate validation record envelope fields incomplete or unknown")
    if envelope["schema_version"] != "candidate-validation-record/1" or envelope["algorithm"] != "ed25519":
        _fail("unsupported candidate validation record envelope")
    key_id = _string(envelope["key_id"], "record key id", _KEY_ID)
    signature_text = _string(envelope["signature"], "record signature")
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except (ValueError, TypeError) as exc:
        raise CandidateValidationRecordError("record signature is invalid") from exc
    if len(signature) != 64:
        _fail("record signature is invalid")
    validate_payload(envelope["payload"])
    payload = envelope["payload"]
    public = _key_for_record(trust, key_id, payload["record_id"])
    unsigned = {key: value for key, value in envelope.items() if key != "signature"}
    try:
        public.verify(signature, canonical(unsigned))
    except InvalidSignature as exc:
        raise CandidateValidationRecordError("candidate validation record signature is invalid") from exc
    if now is None:
        current = datetime.now(timezone.utc)
    elif isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None:
        current = now.astimezone(timezone.utc)
    else:
        _fail("record verification time is invalid")
    issued, expires = _time(payload["issued_at"], "record issue time"), _time(payload["expires_at"], "record expiry time")
    if issued > current or expires <= current:
        _fail("candidate validation record is not currently valid")
    return dict(payload)
