"""Unit tests for execution identity materials; no Docker proof is claimed here."""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agri_research_agent.shared import production_identity as identity


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Identity Test")
    git(root, "config", "user.email", "identity@example.invalid")
    (root / "source.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")
    monkeypatch.setattr(identity, "_ROOT", root)
    return root


def test_git_identity_requires_executing_clean_normal_approved_repository(repository: Path) -> None:
    verified = identity.verify_execution(
        identity.GitExecutionRequest(repository, git(repository, "rev-parse", "HEAD"), git(repository, "rev-parse", "HEAD^{tree}")),
        expected_role=identity.AuthorizationRole.PRODUCTION,
        module_id="shared-runtime",
        runtime_id="formal-runtime",
        runtime_root=repository,
        marker_sha256="a" * 64,
    )
    assert verified.kind is identity.IdentityKind.GIT_WORKTREE
    assert verified.writable_roots == (repository,)
    (repository / "untracked.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(identity.ProductionIdentityError, match="dirty"):
        identity.verify_execution(identity.GitExecutionRequest(repository, git(repository, "rev-parse", "HEAD"), git(repository, "rev-parse", "HEAD^{tree}")), expected_role=identity.AuthorizationRole.PRODUCTION, module_id="shared-runtime", runtime_id="formal-runtime", runtime_root=repository, marker_sha256="a" * 64)


def test_git_identity_rejects_wrong_source_root(repository: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-b", "main")
    git(other, "config", "user.name", "Identity Test")
    git(other, "config", "user.email", "identity@example.invalid")
    (other / "x").write_text("x", encoding="utf-8")
    git(other, "add", ".")
    git(other, "commit", "-m", "other")
    with pytest.raises(identity.ProductionIdentityError, match="executing source"):
        identity.verify_execution(identity.GitExecutionRequest(other, git(other, "rev-parse", "HEAD"), git(other, "rev-parse", "HEAD^{tree}")), expected_role=identity.AuthorizationRole.PRODUCTION, module_id="shared-runtime", runtime_id="formal-runtime", runtime_root=repository, marker_sha256="a" * 64)


def trust_file(path: Path, key_id: str, public_key: bytes, domain: str = "production") -> None:
    path.write_text(json.dumps({"schema_version": "production-runtime-trust/1", "keys": [{"key_id": key_id, "domain": domain, "algorithm": "ed25519", "public_key_base64": base64.b64encode(public_key).decode("ascii")}], "revoked_key_ids": [], "revoked_grant_ids": []}), encoding="utf-8")


def test_signed_grant_requires_pinned_domain_key_and_canonical_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    private = Ed25519PrivateKey.generate()
    key_id = "production-key"
    trust = tmp_path / "trust.json"
    trust_file(trust, key_id, private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    monkeypatch.setattr(identity, "TRUST_CONFIG_PATH", trust)
    payload = {"grant_id": "a" * 32}
    envelope = {"schema_version": "production-execution-grant/1", "algorithm": "ed25519", "key_id": key_id, "payload": payload, "signature": base64.b64encode(private.sign(identity._canonical(payload))).decode("ascii")}
    grant = tmp_path / "grant.json"
    grant.write_text(json.dumps(envelope), encoding="utf-8")
    assert identity._signed_payload(grant, identity.AuthorizationRole.PRODUCTION) == payload
    envelope["payload"]["grant_id"] = "h" * 32
    grant.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(identity.ProductionIdentityError, match="signature"):
        identity._signed_payload(grant, identity.AuthorizationRole.PRODUCTION)
    trust_file(trust, key_id, private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw), "candidate_validation")
    with pytest.raises(identity.ProductionIdentityError, match="untrusted"):
        identity._signed_payload(grant, identity.AuthorizationRole.PRODUCTION)


def test_empty_pinned_trust_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trust = tmp_path / "trust.json"
    trust.write_text(json.dumps({"schema_version": "production-runtime-trust/1", "keys": [], "revoked_key_ids": [], "revoked_grant_ids": []}), encoding="utf-8")
    monkeypatch.setattr(identity, "TRUST_CONFIG_PATH", trust)
    grant = tmp_path / "grant.json"
    grant.write_text(json.dumps({"schema_version": "production-execution-grant/1", "algorithm": "ed25519", "key_id": "missing", "payload": {}, "signature": ""}), encoding="utf-8")
    with pytest.raises(identity.ProductionIdentityError, match="untrusted"):
        identity._signed_payload(grant, identity.AuthorizationRole.PRODUCTION)


@pytest.mark.parametrize("field,value", [
    ("schema_version", "wrong"), ("algorithm", "rsa"), ("key_id", "bad_key"),
    ("payload", []), ("signature", 1),
])
def test_grant_envelope_rejects_schema_confusion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object) -> None:
    private = Ed25519PrivateKey.generate()
    trust = tmp_path / "trust.json"
    trust_file(trust, "production-key", private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    monkeypatch.setattr(identity, "TRUST_CONFIG_PATH", trust)
    payload = {"grant_id": "a" * 32}
    envelope = {"schema_version": "production-execution-grant/1", "algorithm": "ed25519", "key_id": "production-key", "payload": payload, "signature": base64.b64encode(private.sign(identity._canonical(payload))).decode("ascii")}
    envelope[field] = value
    grant = tmp_path / "grant.json"
    grant.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(identity.ProductionIdentityError):
        identity._signed_payload(grant, identity.AuthorizationRole.PRODUCTION)


def test_revoked_key_and_grant_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode("ascii")
    trust = tmp_path / "trust.json"
    trust.write_text(json.dumps({"schema_version": "production-runtime-trust/1", "keys": [{"key_id": "production-key", "domain": "production", "algorithm": "ed25519", "public_key_base64": public}], "revoked_key_ids": [], "revoked_grant_ids": ["a" * 32]}), encoding="utf-8")
    monkeypatch.setattr(identity, "TRUST_CONFIG_PATH", trust)
    payload = {"grant_id": "a" * 32}
    envelope = {"schema_version": "production-execution-grant/1", "algorithm": "ed25519", "key_id": "production-key", "payload": payload, "signature": base64.b64encode(private.sign(identity._canonical(payload))).decode("ascii")}
    grant = tmp_path / "grant.json"
    grant.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(identity.ProductionIdentityError, match="revoked"):
        identity._signed_payload(grant, identity.AuthorizationRole.PRODUCTION)


def oci_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, role: str = "production", origin: str = "candidate"):
    source = tmp_path / "image"
    runtime = tmp_path / "runtime"
    for path in (source / "02_configs", source / "03_src", source / "04_scripts", source / "05_apps", source / "auth", source / "manifests", runtime / "data"):
        path.mkdir(parents=True, exist_ok=True)
    marker = runtime / ".market-data-runtime.json"
    marker.write_text(json.dumps({"schema_version": 1, "runtime_id": "formal-runtime", "module_id": "shared-runtime", "classification": "formal" if role == "production" else "candidate-validation", "created_at": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    release = source / "RELEASE.json"
    manifest = source / "manifests" / "runtime.json"
    commit, tree = "a" * 40, "b" * 40
    release.write_text(json.dumps({"git_commit": commit, "git_tree": tree, "application": "fixture"}), encoding="utf-8")
    manifest.write_text(json.dumps({"project_id": "identity-fixture", "module_id": "shared-runtime", "service_id": "fixture-service"}), encoding="utf-8")
    private = Ed25519PrivateKey.generate()
    domain = role
    trust = source / "02_configs" / "production_runtime_trust.json"
    trust_file(trust, role.replace("_", "-") + "-key", private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw), domain)
    monkeypatch.setattr(identity, "_ROOT", source)
    monkeypatch.setattr(identity, "TRUST_CONFIG_PATH", trust)
    mapping = {source.resolve(): "/app", runtime.resolve(): "/runtime" if role == "production" else "/tmp/runtime"}
    def runtime_path(path: Path) -> str:
        resolved = path.resolve(strict=True)
        for base, container in mapping.items():
            try:
                relative = resolved.relative_to(base)
                return container if str(relative) == "." else container + "/" + relative.as_posix()
            except ValueError:
                pass
        raise AssertionError(f"unexpected observed path: {resolved}")
    monkeypatch.setattr(identity, "_runtime_path", runtime_path)
    monkeypatch.setattr(identity, "_mount_options", lambda: {"/": {"ro"}})
    monkeypatch.setattr(identity, "_mount_for", lambda _mounts, path: {"rw"} if str(path).replace("\\", "/").endswith("runtime/data") else {"ro"})
    monkeypatch.setattr(identity.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(identity.socket, "gethostname", lambda: "b" * 32)
    root = runtime_path(runtime)
    payload = {
        "grant_id": "a" * 32, "issued_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "identity_kind": "oci_container", "authorization_mode": role, "artifact_origin": origin, "role": role,
        "project_id": "identity-fixture", "module_id": "shared-runtime", "service_id": "fixture-service", "runtime_id": "formal-runtime",
        "approved_commit": commit, "approved_tree": tree, "release_commit": commit, "release_tree": tree,
        "image_id": "sha256:" + "c" * 64, "release_sha256": hashlib.sha256(release.read_bytes()).hexdigest(), "runtime_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(), "runtime_marker_sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
        "runtime_root": root, "writable_roots": [root + "/data"], "protected_mounts": ["/app", root + "/.market-data-runtime.json"],
        "rendered_compose_sha256": "d" * 64, "mount_contract_sha256": "e" * 64, "actual_config_sha256": "f" * 64, "container_id": "1" * 64, "hostname_nonce": "b" * 32,
    }
    grant = source / "auth" / "grant.json"
    envelope = {"schema_version": "production-execution-grant/1", "algorithm": "ed25519", "key_id": role.replace("_", "-") + "-key", "payload": payload, "signature": base64.b64encode(private.sign(identity._canonical(payload))).decode("ascii")}
    grant.write_text(json.dumps(envelope), encoding="utf-8")
    request = identity.OCIExecutionRequest(grant, release, manifest, runtime, marker)
    return request, payload, private, marker, runtime


def sign_grant(request: identity.OCIExecutionRequest, payload: dict, private: Ed25519PrivateKey) -> None:
    envelope = {"schema_version": "production-execution-grant/1", "algorithm": "ed25519", "key_id": payload["role"].replace("_", "-") + "-key", "payload": payload, "signature": base64.b64encode(private.sign(identity._canonical(payload))).decode("ascii")}
    request.grant_path.write_text(json.dumps(envelope), encoding="utf-8")


def test_oci_production_accepts_promoted_candidate_image_and_returns_only_signed_writable_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, payload, _private, marker, runtime = oci_fixture(tmp_path, monkeypatch)
    verified = identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime", runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())
    assert verified.kind is identity.IdentityKind.OCI_CONTAINER
    assert verified.writable_roots == (Path("/runtime/data"),)
    assert payload["artifact_origin"] == "candidate"


def test_candidate_grant_cannot_authorize_production_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, _payload, _private, marker, runtime = oci_fixture(tmp_path, monkeypatch, role="candidate_validation")
    verified = identity.verify_execution(request, expected_role="candidate_validation", module_id="shared-runtime", runtime_id="formal-runtime", runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())
    assert verified.role is identity.AuthorizationRole.CANDIDATE_VALIDATION
    with pytest.raises(identity.ProductionIdentityError):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime", runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())


@pytest.mark.parametrize("field,value,error", [
    ("release_commit", "0" * 40, "release identity"), ("approved_tree", "0" * 40, "release identity"),
    ("service_id", "other-service", "runtime manifest"), ("runtime_manifest_sha256", "0" * 64, "manifest identity"),
    ("expires_at", "2000-01-01T00:00:00Z", "not currently valid"), ("image_id", "latest", "image identity"),
])
def test_oci_signed_mismatch_and_expiry_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str, error: str) -> None:
    request, payload, private, marker, runtime = oci_fixture(tmp_path, monkeypatch)
    payload[field] = value
    sign_grant(request, payload, private)
    with pytest.raises(identity.ProductionIdentityError, match=error):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime", runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())


def test_oci_marker_change_and_missing_grant_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, _payload, _private, marker, runtime = oci_fixture(tmp_path, monkeypatch)
    marker.write_text("{}", encoding="utf-8")
    with pytest.raises(identity.ProductionIdentityError, match="marker"):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime", runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())
    request.grant_path.unlink()
    with pytest.raises(identity.ProductionIdentityError, match="grant"):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime", runtime_root=runtime, marker_sha256="a" * 64)


def test_oci_rejects_writable_source_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, _payload, _private, marker, runtime = oci_fixture(tmp_path, monkeypatch)
    def observed_mount(_mounts, path: Path):
        value = str(path).replace("\\", "/")
        if value.endswith("runtime/data"):
            return {"rw"}
        if value.endswith("image/03_src"):
            return {"rw"}
        return {"ro"}
    monkeypatch.setattr(identity, "_mount_for", observed_mount)
    with pytest.raises(identity.ProductionIdentityError, match="mount permissions"):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime", runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())


def test_oci_rejects_duplicate_json_keys_before_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, _payload, _private, marker, runtime = oci_fixture(tmp_path, monkeypatch)
    request.grant_path.write_text('{"schema_version":"production-execution-grant/1","schema_version":"production-execution-grant/1"}', encoding="utf-8")
    with pytest.raises(identity.ProductionIdentityError, match="invalid"):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime", runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())
