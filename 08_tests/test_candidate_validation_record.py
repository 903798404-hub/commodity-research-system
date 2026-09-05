from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("candidate_validation_record", ROOT / "09_deploy/runtime_identity/candidate_validation_record.py")
assert SPEC and SPEC.loader
record = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(record)


NOW = datetime(2026, 9, 5, 8, tzinfo=timezone.utc)


def evidence():
    source = {"02_configs/runtime.json": "a" * 64, "04_scripts/runtime/validate_target_runtime.py": "b" * 64}
    binding = {"project_id": "demo-runtime", "commit": "c" * 40, "tree": "d" * 40, "source_sha256": source, "validator_version": record._VALIDATOR_VERSION}
    return {"schema_version": "target-runtime-evidence/1", "binding": binding, "TARGET_RUNTIME_STATIC_VALIDATION": "PASS", "TARGET_RUNTIME_CONTAINER_VALIDATION": "PASS", "image_id": "sha256:" + "e" * 64, "rendered_compose_sha256": "f" * 64, "builder": {"builder_id": "linux-fixture", "os": "linux", "execution": "isolated"}, "observed_identity": {"image_id": "sha256:" + "e" * 64, "oci_revision": "c" * 40, "git_tree": "d" * 40, "source_sha256": source, "rendered_compose_sha256": "f" * 64, "authorization_role": "candidate_validation", "git_metadata_present": False, "production_volumes_mounted": False}, "probes": {name: "PASS" for name in record._PROBES}}


def trust(private, *, domain="candidate_validation", revoked_keys=None, revoked_records=None):
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    return {"schema_version": "production-runtime-trust/1", "keys": [{"key_id": "candidate-key", "domain": domain, "algorithm": "ed25519", "public_key_base64": public}], "revoked_key_ids": revoked_keys or [], "revoked_grant_ids": revoked_records or []}


def envelope(private, *, payload=None):
    payload = payload or {"record_id": "1" * 32, "purpose": "target-runtime-validation", "authorization_role": "candidate_validation", "issued_at": (NOW - timedelta(minutes=1)).isoformat(), "expires_at": (NOW + timedelta(hours=1)).isoformat(), "evidence": evidence()}
    payload["evidence_sha256"] = hashlib.sha256(record.canonical(payload["evidence"])).hexdigest()
    unsigned = {"schema_version": "candidate-validation-record/1", "algorithm": "ed25519", "key_id": "candidate-key", "payload": payload}
    return {**unsigned, "signature": base64.b64encode(private.sign(record.canonical(unsigned))).decode()}


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def resign(value, private):
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    value["signature"] = base64.b64encode(private.sign(record.canonical(unsigned))).decode()


def test_valid_record_returns_fresh_payload():
    private = Ed25519PrivateKey.generate()
    value = envelope(private)
    assert record.verify_record(raw(value), trust(private), now=NOW) == value["payload"]


@pytest.mark.parametrize("mutation", ["wrong_signature", "production_domain", "revoked_key", "revoked_record", "expired", "future", "long_ttl", "wrong_hash", "missing_probe", "wrong_observed", "unknown", "nested_unknown", "bad_source", "wrong_role", "wrong_validator"])
def test_record_rejects_security_mutations(mutation):
    private = Ed25519PrivateKey.generate()
    value = envelope(private)
    selected_trust = trust(private)
    if mutation == "wrong_signature":
        value["signature"] = base64.b64encode(Ed25519PrivateKey.generate().sign(record.canonical({k: v for k, v in value.items() if k != "signature"}))).decode()
    elif mutation == "production_domain":
        selected_trust = trust(private, domain="production")
    elif mutation == "revoked_key":
        selected_trust = trust(private, revoked_keys=["candidate-key"])
    elif mutation == "revoked_record":
        selected_trust = trust(private, revoked_records=[value["payload"]["record_id"]])
    elif mutation == "expired":
        value["payload"]["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif mutation == "future":
        value["payload"]["issued_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif mutation == "long_ttl":
        value["payload"]["expires_at"] = (NOW + timedelta(days=8)).isoformat()
    elif mutation == "wrong_hash":
        value["payload"]["evidence_sha256"] = "0" * 64
    elif mutation == "missing_probe":
        value["payload"]["evidence"]["probes"].pop("dependencies")
    elif mutation == "wrong_observed":
        value["payload"]["evidence"]["observed_identity"]["git_metadata_present"] = True
    elif mutation == "unknown":
        value["payload"]["extra"] = True
    elif mutation == "bad_source":
        value["payload"]["evidence"]["binding"]["source_sha256"] = {"../escape": "a" * 64}
    elif mutation == "wrong_role":
        value["payload"]["authorization_role"] = "production"
    elif mutation == "nested_unknown":
        value["payload"]["evidence"]["builder"]["extra"] = True
    elif mutation == "wrong_validator":
        value["payload"]["evidence"]["binding"]["validator_version"] = "target-runtime-validator/2"
    if mutation != "wrong_signature":
        resign(value, private)
    with pytest.raises(record.CandidateValidationRecordError):
        record.verify_record(raw(value), selected_trust, now=NOW)


@pytest.mark.parametrize("text", [
    b'{"schema_version":"candidate-validation-record/1","schema_version":"forged"}',
    b'{"schema_version":"candidate-validation-record/1","x":NaN}',
])
def test_record_rejects_duplicate_or_nonfinite_json(text):
    with pytest.raises(record.CandidateValidationRecordError):
        record.verify_record(text, trust(Ed25519PrivateKey.generate()), now=NOW)


def test_signature_covers_envelope_metadata_not_only_payload():
    private = Ed25519PrivateKey.generate()
    value = envelope(private)
    value["key_id"] = "another-key"
    with pytest.raises(record.CandidateValidationRecordError):
        record.verify_record(raw(value), trust(private), now=NOW)


@pytest.mark.parametrize("bad_path", ["../escape", "has space", "nul\x00path", "drive:path", "glob*.py"])
def test_record_rejects_unsafe_source_paths_after_resigning(bad_path):
    private = Ed25519PrivateKey.generate()
    value = envelope(private)
    value["payload"]["evidence"]["binding"]["source_sha256"] = {bad_path: "a" * 64}
    value["payload"]["evidence_sha256"] = hashlib.sha256(record.canonical(value["payload"]["evidence"])).hexdigest()
    resign(value, private)
    with pytest.raises(record.CandidateValidationRecordError):
        record.verify_record(raw(value), trust(private), now=NOW)


@pytest.mark.parametrize("field,value", [("git_metadata_present", 0), ("production_volumes_mounted", 0)])
def test_record_requires_real_boolean_observation_fields(field, value):
    private = Ed25519PrivateKey.generate()
    item = envelope(private)
    item["payload"]["evidence"]["observed_identity"][field] = value
    item["payload"]["evidence_sha256"] = hashlib.sha256(record.canonical(item["payload"]["evidence"])).hexdigest()
    resign(item, private)
    with pytest.raises(record.CandidateValidationRecordError):
        record.verify_record(raw(item), trust(private), now=NOW)


def test_observed_source_drift_is_rejected_after_resigning():
    private = Ed25519PrivateKey.generate()
    item = envelope(private)
    item["payload"]["evidence"]["observed_identity"]["source_sha256"] = {"02_configs/runtime.json": "0" * 64}
    item["payload"]["evidence_sha256"] = hashlib.sha256(record.canonical(item["payload"]["evidence"])).hexdigest()
    resign(item, private)
    with pytest.raises(record.CandidateValidationRecordError):
        record.verify_record(raw(item), trust(private), now=NOW)


def test_signature_cannot_be_reused_under_a_different_candidate_key_id():
    private, another = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    item = envelope(private)
    public = base64.b64encode(another.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    anchors = trust(private)
    anchors["keys"].append({"key_id": "other-candidate", "domain": "candidate_validation", "algorithm": "ed25519", "public_key_base64": public})
    item["key_id"] = "other-candidate"
    with pytest.raises(record.CandidateValidationRecordError):
        record.verify_record(raw(item), anchors, now=NOW)
