"""Pure contract tests for production execution grants; no Docker or signing."""
from __future__ import annotations

import copy
import ast
import json
import re
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from agri_research_agent.shared import production_grant as grant


GIT = "a" * 40
TREE = "b" * 40
SHA = "c" * 64
IMAGE = "sha256:" + "d" * 64
CID = "e" * 64


def schema_validator() -> Draft202012Validator:
    schema_path = Path(__file__).resolve().parents[2] / "09_deploy" / "runtime_identity" / "production_authorization.schema.json"
    checker = FormatChecker()

    @checker.checks("date-time", raises=(TypeError, ValueError))
    def strict_datetime(value: object) -> bool:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value):
            return False
        datetime.fromisoformat(value[:-1] + "+00:00" if value[-1:] in {"Z", "z"} else value.replace("t", "T", 1))
        return True

    return Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")), format_checker=checker)


def payload(version: int = 1, role: str = "production") -> dict:
    candidate = role == "candidate_validation"
    value = {
        "grant_id": "f" * 32, "issued_at": "2026-01-01T00:00:00Z", "expires_at": "2026-01-01T01:00:00+00:00",
        "identity_kind": "oci_container", "authorization_mode": role,
        "artifact_origin": "candidate" if candidate else "production", "role": role,
        "project_id": "fixture-project", "module_id": "fixture-module", "service_id": "fixture-service",
        "runtime_id": "fixture-runtime", "approved_commit": GIT, "approved_tree": TREE,
        "release_commit": GIT, "release_tree": TREE, "image_id": IMAGE,
        "release_sha256": SHA, "runtime_manifest_sha256": SHA, "runtime_marker_sha256": SHA,
        "runtime_root": "/tmp/fixture-runtime" if candidate else "/runtime/fixture",
        "writable_roots": ["/tmp/fixture-runtime/data"] if candidate else ["/runtime/fixture/data"],
        "protected_mounts": ["/tmp/fixture-runtime" if candidate else "/runtime/fixture"],
        "rendered_compose_sha256": SHA, "mount_contract_sha256": SHA, "actual_config_sha256": SHA,
        "container_id": CID, "hostname_nonce": "1" * 32,
    }
    if version in (2, 3):
        value.update({"runtime_manifest_schema_version": f"runtime-manifest/{version}",
                      "identity_root_role": "marker",
                      "candidate_scope_id": "2" * 32 if candidate else None,
                      "candidate_scope_sha256": SHA if candidate else None})
    return value


def envelope(version: int = 1, role: str = "production") -> dict:
    return {"schema_version": f"production-execution-grant/{version}", "algorithm": "ed25519",
            "key_id": "fixture-key", "payload": payload(version, role), "signature": "A" * 86 + "=="}


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("role", ["production", "candidate_validation"])
def test_complete_grant_versions_and_roles(version: int, role: str):
    value = envelope(version, role)
    assert grant.validate_execution_grant_envelope(value) is None
    assert grant.parse_execution_grant_json(json.dumps(value, sort_keys=True).encode()) == value["payload"]


def test_json_schema_and_shared_validator_accept_the_same_complete_versions():
    validator = schema_validator()
    for version in (1, 2, 3):
        for role in ("production", "candidate_validation"):
            value = envelope(version, role)
            grant.validate_execution_grant_envelope(value)
            validator.validate(value)


@pytest.mark.parametrize("mutation", ["unknown", "scope", "path", "calendar", "role"])
def test_json_schema_and_shared_validator_reject_the_same_v2_shape_faults(mutation: str):
    validator = schema_validator()
    value = envelope(2, "candidate_validation")
    if mutation == "unknown": value["payload"]["unknown"] = True
    elif mutation == "scope": value["payload"]["candidate_scope_id"] = None
    elif mutation == "path": value["payload"]["runtime_root"] = "/runtime/../escape"
    elif mutation == "calendar": value["payload"]["issued_at"] = "2026-02-30T00:00:00Z"
    else: value["payload"]["authorization_mode"] = "production"
    with pytest.raises(grant.GrantShapeError): grant.validate_execution_grant_envelope(value)
    assert list(validator.iter_errors(value))


def test_production_grant_module_has_only_standard_library_imports():
    path = Path(grant.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = {node.names[0].name.split(".")[0] for node in tree.body if isinstance(node, ast.Import)}
    roots |= {str(node.module).split(".")[0] for node in tree.body if isinstance(node, ast.ImportFrom) and node.module != "__future__"}
    assert roots <= {"datetime", "json", "math", "re", "typing", "urllib"}


@pytest.mark.parametrize("field", ["schema_version", "algorithm", "key_id", "payload", "signature"])
def test_envelope_missing_required_field_rejected(field: str):
    value = envelope()
    del value[field]
    with pytest.raises(grant.GrantShapeError):
        grant.validate_execution_grant_envelope(value)


@pytest.mark.parametrize("field", ["grant_id", "issued_at", "identity_kind", "authorization_mode", "artifact_origin",
                                   "role", "project_id", "module_id", "service_id", "runtime_id", "approved_commit",
                                   "approved_tree", "release_commit", "release_tree", "image_id", "release_sha256",
                                   "runtime_manifest_sha256", "runtime_marker_sha256", "runtime_root", "writable_roots",
                                   "protected_mounts", "rendered_compose_sha256", "mount_contract_sha256",
                                   "actual_config_sha256", "container_id", "hostname_nonce"])
def test_v1_payload_missing_required_field_rejected(field: str):
    value = envelope()
    del value["payload"][field]
    with pytest.raises(grant.GrantShapeError):
        grant.validate_execution_grant_envelope(value)


@pytest.mark.parametrize("field,value", [
    ("schema_version", "production-execution-grant/9"), ("algorithm", "rsa"), ("signature", "not base64 !!!"),
    ("issued_at", "2026-01-01T00:00:00"), ("expires_at", "2026-02-30T00:00:00Z"),
    ("approved_commit", "A" * 40), ("image_id", "latest"), ("writable_roots", "/runtime/data"),
    ("protected_mounts", ["/runtime", "/runtime"]), ("runtime_root", "/runtime/../outside"),
    ("container_id", "short"),
])
def test_shape_type_pattern_time_and_unique_array_rejected(field: str, value):
    item = envelope()
    target = item if field in {"schema_version", "algorithm", "signature"} else item["payload"]
    target[field] = value
    with pytest.raises(grant.GrantShapeError):
        grant.validate_execution_grant_envelope(item)


def test_v2_requires_exact_extensions_and_rejects_version_mixing():
    item = envelope(2)
    del item["payload"]["candidate_scope_id"]
    with pytest.raises(grant.GrantShapeError):
        grant.validate_execution_grant_envelope(item)
    item = envelope(1)
    item["payload"]["runtime_manifest_schema_version"] = "runtime-manifest/2"
    with pytest.raises(grant.GrantShapeError):
        grant.validate_execution_grant_envelope(item)


def test_v3_uses_the_v2_field_shape_and_pins_the_v3_manifest():
    v2, v3 = envelope(2), envelope(3)
    assert set(v2["payload"]) == set(v3["payload"])
    v3["payload"]["runtime_manifest_schema_version"] = "runtime-manifest/2"
    with pytest.raises(grant.GrantShapeError):
        grant.validate_execution_grant_envelope(v3)


def v3_environment_manifest() -> dict:
    return {
        "schema_version": "runtime-manifest/3",
        "required_environment": ["MODE", "HISTORY_PATH", "SERVICE_URL", "MARKET_DATA_EXECUTION_GRANT"],
        "forbidden_environment": ["ENABLE_WRITES", "BUSINESS_DATE"],
        "runtime_roots": [
            {"role": "marker", "container_path": "/runtime", "access": "ro"},
            {"role": "history", "container_path": "/runtime/history", "access": "ro"},
        ],
        "environment_bindings": [
            {"name": "MODE", "kind": "literal", "value": "STRICT_RUNTIME"},
            {"name": "HISTORY_PATH", "kind": "runtime_path", "role": "history", "relative_path": "current.json"},
            {"name": "SERVICE_URL", "kind": "deployment", "value_type": "https_url", "candidate_value": "https://candidate.invalid/"},
            {"name": "MARKET_DATA_EXECUTION_GRANT", "kind": "execution_grant"},
        ],
    }


def test_v3_runtime_environment_binds_all_declared_values_by_role():
    manifest = v3_environment_manifest()
    candidate = {"MODE": "STRICT_RUNTIME", "HISTORY_PATH": "/runtime/history/current.json",
                 "SERVICE_URL": "https://candidate.invalid/", "MARKET_DATA_EXECUTION_GRANT": "/run/grants/grant.json"}
    assert grant.validate_runtime_environment(manifest, candidate, role="candidate_validation",
                                              grant_path="/run/grants/grant.json") is None
    production = {**candidate, "SERVICE_URL": "https://production.invalid/"}
    assert grant.validate_runtime_environment(manifest, production, role="production",
                                              grant_path="/run/grants/grant.json") is None
    candidate["SERVICE_URL"] = "https://production.invalid/"
    with pytest.raises(grant.GrantValidationError, match="candidate deployment"):
        grant.validate_runtime_environment(manifest, candidate, role="candidate_validation", grant_path="/run/grants/grant.json")


@pytest.mark.parametrize("mutation", ["host_path", "wrong_grant", "forbidden", "missing"])
def test_v3_runtime_environment_rejects_mismatches_and_forbidden_empty_values(mutation: str):
    environment = {"MODE": "STRICT_RUNTIME", "HISTORY_PATH": "/runtime/history/current.json",
                   "SERVICE_URL": "https://candidate.invalid/", "MARKET_DATA_EXECUTION_GRANT": "/run/grants/grant.json"}
    if mutation == "host_path":
        environment["HISTORY_PATH"] = "C:/host/history/current.json"
    elif mutation == "wrong_grant":
        environment["MARKET_DATA_EXECUTION_GRANT"] = "/tmp/grant.json"
    elif mutation == "forbidden":
        environment["ENABLE_WRITES"] = ""
    else:
        del environment["MODE"]
    with pytest.raises(grant.GrantValidationError):
        grant.validate_runtime_environment(v3_environment_manifest(), environment,
                                           role="candidate_validation", grant_path="/run/grants/grant.json")


@pytest.mark.parametrize("role,field,value", [
    ("candidate_validation", "artifact_origin", "production"),
    ("production", "artifact_origin", "unexpected"),
    ("production", "candidate_scope_id", "candidate-1"),
    ("production", "candidate_scope_sha256", SHA),
    ("candidate_validation", "candidate_scope_id", None),
    ("candidate_validation", "candidate_scope_sha256", None),
])
def test_cross_role_and_scope_contract_rejected(role: str, field: str, value):
    item = envelope(2, role)
    item["payload"][field] = value
    with pytest.raises(grant.GrantShapeError):
        grant.validate_execution_grant_envelope(item)


def test_duplicate_json_keys_rejected_for_all_versions(tmp_path: Path):
    for version in (1, 2, 3):
        raw = (b'{"schema_version":"production-execution-grant/' + str(version).encode() +
               b'","schema_version":"production-execution-grant/' + str(version).encode() + b'"}')
        with pytest.raises(grant.GrantShapeError):
            grant.parse_execution_grant_json(raw)


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
@pytest.mark.parametrize("version", [1, 2])
def test_nonfinite_json_numbers_are_rejected_before_shape_validation(version: int, constant: bytes):
    raw = (b'{"schema_version":"production-execution-grant/' + str(version).encode()
           + b'","algorithm":"ed25519","key_id":"fixture-key","payload":{"value":'
           + constant + b'},"signature":"' + b"A" * 86 + b'=="}')
    with pytest.raises(grant.GrantShapeError, match="non-finite"):
        grant.parse_execution_grant_json(raw)


def test_nested_mutation_does_not_change_fixture_source():
    item = envelope(2, "candidate_validation")
    original = copy.deepcopy(item)
    parsed = grant.parse_execution_grant_json(json.dumps(item).encode())
    parsed["writable_roots"].append("/tmp/escape")
    assert item == original
