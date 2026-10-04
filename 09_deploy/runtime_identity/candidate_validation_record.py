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


class CandidateValidationRecordExpired(CandidateValidationRecordError):
    """Only raised AFTER strict schema, trust, revocation and signature checks.

    The authenticated payload is historical evidence, never a valid record.
    Preparation may use it to check the exact target before real revalidation.
    """
    def __init__(self, payload: dict):
        super().__init__("candidate validation record is not currently valid")
        self.payload = dict(payload)


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
_PACKAGING_FIELDS = frozenset({"workflow_run_id", "candidate_commit", "candidate_tree", "image_id", "release_commit", "release_tree", "oci_revision", "identity_kind", "role", "production_key_used", "private_key_persisted", "private_key_visible_to_container", "public_key_fingerprint", "grant_binding", "import_closure", "lifecycle_imports", "readonly_initialization", "readonly_exit_code", "initialize_strict_page", "app_test", "probe_stages", "timestamp"})
_SECRET_PROBES = frozenset({"declared_readonly", "undeclared_rejected", "wrong_target_rejected", "writable_rejected"})
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
    # Explicit additive claim. Historical records without packaging stay valid;
    # no other optional or unknown evidence field is accepted.
    if type(evidence) is not dict or set(evidence) not in (_EVIDENCE_FIELDS, _EVIDENCE_FIELDS | {"spread_runtime_packaging"}):
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
    if "spread_runtime_packaging" in evidence:
        _validate_packaging(evidence["spread_runtime_packaging"], evidence)


def _validate_packaging(value: object, evidence: dict) -> None:
    if type(value) is not dict or set(value) not in (_PACKAGING_FIELDS, _PACKAGING_FIELDS | {"service_credential_mounts"}):
        _fail("packaging fields incomplete or unknown")
    binding = evidence["binding"]
    for name, expected in {"candidate_commit": binding["commit"], "release_commit": binding["commit"],
                           "oci_revision": binding["commit"], "candidate_tree": binding["tree"],
                           "release_tree": binding["tree"], "image_id": evidence["image_id"],
                           "role": "candidate_validation", "probe_stages": evidence["probes"]}.items():
        if value[name] != expected:
            _fail("packaging identity or probes differ from evidence")
    run = value["workflow_run_id"]
    if run is not None and (type(run) is not str or not run.isascii() or not run.isdecimal()):
        _fail("invalid packaging workflow run")
    _time(value["timestamp"], "packaging timestamp")
    kind = value["identity_kind"]
    if kind not in {"FIXED_CANDIDATE_VALIDATION_ROOT", "EPHEMERAL_CI_CANDIDATE_VALIDATION_ROOT"}:
        _fail("invalid packaging trust kind")
    if value["production_key_used"] is not False or value["private_key_visible_to_container"] is not False:
        _fail("packaging signer isolation failed")
    if kind == "EPHEMERAL_CI_CANDIDATE_VALIDATION_ROOT":
        if value["private_key_persisted"] is not False:
            _fail("ephemeral candidate key persisted")
        _string(value["public_key_fingerprint"], "candidate public key fingerprint", _HEX64)
    elif value["private_key_persisted"] is not None or value["public_key_fingerprint"] is not None:
        _fail("fixed candidate trust has ephemeral claims")
    grant = value["grant_binding"]
    if type(grant) is not dict or set(grant) != {"commit", "tree", "image_id", "container_id", "runtime_id"}:
        _fail("packaging grant fields incomplete or unknown")
    if any(grant[name] != expected for name, expected in {
            "commit": binding["commit"], "tree": binding["tree"], "image_id": evidence["image_id"],
            "runtime_id": "target-validation"}.items()):
        _fail("packaging grant identity differs")
    _string(grant["container_id"], "packaging container id", _HEX64)
    closure = value["import_closure"]
    counts = {"required_module_count", "manifest_module_count", "dockerfile_module_count"}
    missing = {"missing_from_manifest", "missing_from_dockerfile", "missing_from_final_image"}
    if type(closure) is not dict or set(closure) != counts | missing:
        _fail("import closure fields incomplete or unknown")
    if any(type(closure[name]) is not int or closure[name] <= 0 for name in counts):
        _fail("invalid import closure counts")
    if any(type(closure[name]) is not list or closure[name] for name in missing):
        _fail("import closure has missing modules")
    if any(closure[name] < closure["required_module_count"] for name in counts):
        _fail("import closure counts disagree")
    lifecycle = {name: "PASS" for name in ("lifecycle", "lifecycle_events", "lifecycle_reconciler", "lifecycle_store")
                 if "03_src/agri_research_agent/import_profit/" + name + ".py" in binding["source_sha256"]}
    if value["lifecycle_imports"] != lifecycle:
        _fail("lifecycle import claims incomplete or failed")
    readonly = value["readonly_initialization"]
    if readonly is None:
        if value["readonly_exit_code"] is not None or value["initialize_strict_page"] != "NOT_EXECUTED" or value["app_test"] != "NOT_EXECUTED":
            _fail("unexecuted initialization has success claims")
        if binding["project_id"] == "spread-production-runtime-wiring":
            _fail("spread initialization claim is required")
    else:
        fields = {"schema_version", "status", "mode", "runtime_id", "git_commit", "git_tree", "image_id", "identity_role", "domestic_spread_rows", "soybean_release", "snapshot_status", "snapshot_release", "snapshot_business_date", "capture_executed", "secret_accessed", "network_accessed"}
        if type(readonly) is not dict or set(readonly) != fields:
            _fail("readonly initialization fields incomplete or unknown")
        expected = {"schema_version": "spread-runtime-preflight/1", "status": "PASS", "mode": "CANDIDATE_VALIDATION",
                    "runtime_id": grant["runtime_id"], "git_commit": binding["commit"], "git_tree": binding["tree"],
                    "image_id": evidence["image_id"], "identity_role": "candidate_validation"}
        if any(readonly[name] != expected_value for name, expected_value in expected.items()):
            _fail("readonly initialization identity differs")
        if type(value["readonly_exit_code"]) is not int or value["readonly_exit_code"] != 0 or value["initialize_strict_page"] != "PASS" or value["app_test"] != "PASS":
            _fail("readonly initialization did not pass")
        if any(readonly[name] is not False for name in ("capture_executed", "secret_accessed", "network_accessed")):
            _fail("readonly initialization performed forbidden operations")
        if type(readonly["domestic_spread_rows"]) is not int or readonly["domestic_spread_rows"] <= 0:
            _fail("invalid domestic reader row count")
        if type(readonly["soybean_release"]) is not str or not readonly["soybean_release"]:
            _fail("invalid soybean release identity")
        if readonly["snapshot_status"] == "AVAILABLE":
            if type(readonly["snapshot_release"]) is not str or not readonly["snapshot_release"]:
                _fail("invalid snapshot release identity")
            from datetime import date
            try:
                text = readonly["snapshot_business_date"]
                if type(text) is not str or date.fromisoformat(text).isoformat() != text:
                    _fail("invalid snapshot business date")
            except (TypeError, ValueError):
                _fail("invalid snapshot business date")
        elif readonly["snapshot_status"] != "NOT_YET_AVAILABLE" or readonly["snapshot_release"] is not None or readonly["snapshot_business_date"] is not None:
            _fail("invalid snapshot availability claim")
    if "service_credential_mounts" in value:
        probes = value["service_credential_mounts"]
        if type(probes) is not dict or set(probes) != _SECRET_PROBES or any(status != "PASS" for status in probes.values()):
            _fail("service credential mount probes incomplete or failed")


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
    if issued > current:
        _fail("candidate validation record is not currently valid")
    if expires <= current:
        raise CandidateValidationRecordExpired(payload)
    return dict(payload)
