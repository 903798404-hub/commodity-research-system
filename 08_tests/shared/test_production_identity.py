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


def complete_payload(*, grant_id: str = "a" * 32) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "grant_id": grant_id, "issued_at": now.isoformat(), "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "identity_kind": "oci_container", "authorization_mode": "production", "artifact_origin": "candidate", "role": "production",
        "project_id": "fixture-project", "module_id": "fixture-module", "service_id": "fixture-service", "runtime_id": "fixture-runtime",
        "approved_commit": "a" * 40, "approved_tree": "b" * 40, "release_commit": "a" * 40, "release_tree": "b" * 40,
        "image_id": "sha256:" + "c" * 64, "release_sha256": "d" * 64, "runtime_manifest_sha256": "e" * 64,
        "runtime_marker_sha256": "f" * 64, "runtime_root": "/runtime", "writable_roots": ["/runtime/data"],
        "protected_mounts": ["/app", "/runtime"], "rendered_compose_sha256": "1" * 64,
        "mount_contract_sha256": "2" * 64, "actual_config_sha256": "3" * 64,
        "container_id": "4" * 64, "hostname_nonce": "5" * 32,
    }


def test_signed_grant_requires_pinned_domain_key_and_canonical_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    private = Ed25519PrivateKey.generate()
    key_id = "production-key"
    trust = tmp_path / "trust.json"
    trust_file(trust, key_id, private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    monkeypatch.setattr(identity, "TRUST_CONFIG_PATH", trust)
    payload = complete_payload()
    envelope = {"schema_version": "production-execution-grant/1", "algorithm": "ed25519", "key_id": key_id, "payload": payload, "signature": base64.b64encode(private.sign(identity._canonical(payload))).decode("ascii")}
    grant = tmp_path / "grant.json"
    grant.write_text(json.dumps(envelope), encoding="utf-8")
    assert identity._signed_payload(grant, identity.AuthorizationRole.PRODUCTION) == payload
    envelope["payload"]["grant_id"] = "b" * 32
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
    payload = complete_payload()
    grant.write_text(json.dumps({"schema_version": "production-execution-grant/1", "algorithm": "ed25519", "key_id": "missing", "payload": payload, "signature": "A" * 86 + "=="}), encoding="utf-8")
    with pytest.raises(identity.ProductionIdentityError, match="untrusted"):
        identity._signed_payload(grant, identity.AuthorizationRole.PRODUCTION)


def test_committed_v2_trust_has_distinct_role_keys_and_no_private_material() -> None:
    trust_path = Path(__file__).resolve().parents[2] / "02_configs" / "production_runtime_trust.json"
    value = json.loads(trust_path.read_text(encoding="utf-8"))
    assert value["schema_version"] == "production-runtime-trust/1"
    assert value["revoked_key_ids"] == [] and value["revoked_grant_ids"] == []
    assert {(item["key_id"], item["domain"]) for item in value["keys"]} == {
        ("production-runtime-v2-production-20260905", "production"),
        ("production-runtime-v2-candidate-20260905", "candidate_validation"),
    }
    material = [base64.b64decode(item["public_key_base64"], validate=True) for item in value["keys"]]
    assert all(item["algorithm"] == "ed25519" and len(raw) == 32 for item, raw in zip(value["keys"], material))
    assert len(set(material)) == 2
    assert all("private" not in item for item in value["keys"])


@pytest.mark.parametrize("field,value", [
    ("schema_version", "wrong"), ("algorithm", "rsa"), ("key_id", "bad_key"),
    ("payload", []), ("signature", 1),
])
def test_grant_envelope_rejects_schema_confusion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object) -> None:
    private = Ed25519PrivateKey.generate()
    trust = tmp_path / "trust.json"
    trust_file(trust, "production-key", private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    monkeypatch.setattr(identity, "TRUST_CONFIG_PATH", trust)
    payload = complete_payload()
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
    payload = complete_payload()
    envelope = {"schema_version": "production-execution-grant/1", "algorithm": "ed25519", "key_id": "production-key", "payload": payload, "signature": base64.b64encode(private.sign(identity._canonical(payload))).decode("ascii")}
    grant = tmp_path / "grant.json"
    grant.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(identity.ProductionIdentityError, match="revoked"):
        identity._signed_payload(grant, identity.AuthorizationRole.PRODUCTION)


def manifest_v2(root: str) -> dict:
    probes = ["entrypoint_initialization", "runtime_identity", "dependencies", "runtime_paths", "mount_permissions",
              "missing_grant_rejected", "wrong_commit_rejected", "wrong_tree_rejected", "wrong_image_rejected",
              "wrong_service_rejected", "wrong_manifest_rejected", "preview_write_rejected", "release_mismatch_rejected"]
    return {
        "schema_version": "runtime-manifest/2", "project_id": "identity-fixture", "module_id": "shared-runtime",
        "service_id": "fixture-service", "runtime_target": "production_container", "identity_kind": "oci_container",
        "build": {"dockerfile": "Dockerfile", "dockerignore": ".dockerignore", "dependency_contracts": ["requirements.txt"], "compose_sources": ["compose.yml"]},
        "entrypoint": ["python", "app.py"], "working_directory": "/app",
        "runtime_roots": [{"role": "marker", "container_path": root, "access": "ro"}, {"role": "data", "container_path": root + "/data", "access": "rw"}],
        "required_mounts": [{"role": "marker", "container_path": root, "read_only": True}, {"role": "data", "container_path": root + "/data", "read_only": False}],
        "required_environment": [], "secret_references": [], "required_executables": ["python"], "required_python_modules": [],
        "production_policy": {"deployment_role": "production", "write_grant_required": True},
        "preview_policy": {"production_write": False, "production_rw_mounts": False}, "validation_probes": probes,
        "identity_root_role": "marker", "initialization_commands": [{"name": "initialize", "argv": ["python", "init.py"]}],
        "source_inputs": [{"path": "app.py", "role": "entrypoint"}, {"path": "init.py", "role": "initialization"}],
    }


def manifest_v3(root: str) -> dict:
    value = manifest_v2(root)
    value.update({
        "schema_version": "runtime-manifest/3",
        "required_environment": ["MODE", "HISTORY_PATH", "SERVICE_URL", "MARKET_DATA_EXECUTION_GRANT"],
        "environment_bindings": [
            {"name": "MODE", "kind": "literal", "value": "STRICT_RUNTIME"},
            {"name": "HISTORY_PATH", "kind": "runtime_path", "role": "history", "relative_path": "current.json"},
            {"name": "SERVICE_URL", "kind": "deployment", "value_type": "https_url", "candidate_value": "https://candidate.invalid/"},
            {"name": "MARKET_DATA_EXECUTION_GRANT", "kind": "execution_grant"},
        ],
        "forbidden_environment": ["ENABLE_WRITES"],
        "candidate_runtime_inputs": [],
    })
    value["runtime_roots"].append({"role": "history", "container_path": root + "/history", "access": "ro"})
    value["required_mounts"].append({"role": "history", "container_path": root + "/history", "read_only": True})
    return value


def oci_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, role: str = "production", origin: str = "candidate", version: int = 1):
    source = tmp_path / "image"
    runtime = tmp_path / "runtime"
    image_dirs = [source / "02_configs", source / "03_src", source / "04_scripts", source / "auth", source / "manifests", runtime / "data"]
    if version != 2:
        image_dirs.append(source / "05_apps")
    for path in image_dirs:
        path.mkdir(parents=True, exist_ok=True)
    marker = runtime / ".market-data-runtime.json"
    marker.write_text(json.dumps({"schema_version": 1, "runtime_id": "formal-runtime", "module_id": "shared-runtime", "classification": "formal" if role == "production" else "candidate-validation", "created_at": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    release = source / "RELEASE.json"
    manifest = source / "manifests" / "runtime.json"
    commit, tree = "a" * 40, "b" * 40
    release.write_text(json.dumps({"git_commit": commit, "git_tree": tree, "application": "fixture"}), encoding="utf-8")
    logical_root = "/runtime/fixture" if version in (2, 3) else ("/runtime" if role == "production" else "/tmp/runtime")
    manifest_value = (manifest_v3(logical_root) if version == 3 else manifest_v2(logical_root)) if version in (2, 3) else {"project_id": "identity-fixture", "module_id": "shared-runtime", "service_id": "fixture-service"}
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
    if version in (2, 3):
        (source / "app.py").write_text("print('fixture')\n", encoding="utf-8")
        (source / "init.py").write_text("print('initialized')\n", encoding="utf-8")
    private = Ed25519PrivateKey.generate()
    domain = role
    trust = source / "02_configs" / "production_runtime_trust.json"
    trust_file(trust, role.replace("_", "-") + "-key", private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw), domain)
    monkeypatch.setattr(identity, "_ROOT", source)
    monkeypatch.setattr(identity, "TRUST_CONFIG_PATH", trust)
    mapping = {source.resolve(): "/app", runtime.resolve(): logical_root}
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
    monkeypatch.setattr(identity, "_mount_for", lambda _mounts, path: {"rw"} if str(path).replace("\\", "/").endswith("/data") else {"ro"})
    monkeypatch.setattr(identity.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(identity.socket, "gethostname", lambda: "b" * 32)
    root = runtime_path(runtime)
    payload = {
        "grant_id": "a" * 32, "issued_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "identity_kind": "oci_container", "authorization_mode": role, "artifact_origin": origin, "role": role,
        "project_id": "identity-fixture", "module_id": "shared-runtime", "service_id": "fixture-service", "runtime_id": "formal-runtime",
        "approved_commit": commit, "approved_tree": tree, "release_commit": commit, "release_tree": tree,
        "image_id": "sha256:" + "c" * 64, "release_sha256": hashlib.sha256(release.read_bytes()).hexdigest(), "runtime_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(), "runtime_marker_sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
        "runtime_root": root, "writable_roots": [root + "/data"], "protected_mounts": ["/app", *([root, root + "/history"] if version == 3 else [root] if version == 2 else [root + "/.market-data-runtime.json"])],
        "rendered_compose_sha256": "d" * 64, "mount_contract_sha256": "e" * 64, "actual_config_sha256": "f" * 64, "container_id": "1" * 64, "hostname_nonce": "b" * 32,
    }
    if version in (2, 3):
        payload.update(runtime_manifest_schema_version=f"runtime-manifest/{version}", identity_root_role="marker",
                       candidate_scope_id="6" * 32 if role == "candidate_validation" else None,
                       candidate_scope_sha256="7" * 64 if role == "candidate_validation" else None)
    grant = source / "auth" / "grant.json"
    envelope = {"schema_version": f"production-execution-grant/{version}", "algorithm": "ed25519", "key_id": role.replace("_", "-") + "-key", "payload": payload, "signature": base64.b64encode(private.sign(identity._canonical(payload))).decode("ascii")}
    grant.write_text(json.dumps(envelope), encoding="utf-8")
    request = identity.OCIExecutionRequest(grant, release, manifest, runtime, marker)
    return request, payload, private, marker, runtime


def sign_grant(request: identity.OCIExecutionRequest, payload: dict, private: Ed25519PrivateKey) -> None:
    version = int(payload["runtime_manifest_schema_version"].rsplit("/", 1)[1]) if "runtime_manifest_schema_version" in payload else 1
    envelope = {"schema_version": f"production-execution-grant/{version}", "algorithm": "ed25519", "key_id": payload["role"].replace("_", "-") + "-key", "payload": payload, "signature": base64.b64encode(private.sign(identity._canonical(payload))).decode("ascii")}
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


@pytest.mark.parametrize("role", ["production", "candidate_validation"])
def test_v2_uses_manifest_identity_root_for_both_roles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    request, payload, _private, marker, runtime = oci_fixture(tmp_path, monkeypatch, role=role, version=2)
    assert not (request.runtime_manifest_path.parent.parent / "05_apps").exists()
    verified = identity.verify_execution(request, expected_role=role, module_id="shared-runtime", runtime_id="formal-runtime",
                                         runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())
    assert verified.writable_roots == (Path("/runtime/fixture/data"),)
    assert payload["runtime_root"] == "/runtime/fixture"


@pytest.mark.parametrize("role", ["production", "candidate_validation"])
def test_v3_requires_observed_environment_bindings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    request, _payload, _private, marker, runtime = oci_fixture(tmp_path, monkeypatch, role=role, version=3)
    environment = {"MODE": "STRICT_RUNTIME", "HISTORY_PATH": "/runtime/fixture/history/current.json",
                   "SERVICE_URL": "https://candidate.invalid/" if role == "candidate_validation" else "https://production.invalid/",
                   "MARKET_DATA_EXECUTION_GRANT": "/app/auth/grant.json"}
    monkeypatch.setattr(identity.os, "environ", environment)
    verified = identity.verify_execution(request, expected_role=role, module_id="shared-runtime", runtime_id="formal-runtime",
                                         runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())
    assert verified.writable_roots == (Path("/runtime/fixture/data"),)
    environment["ENABLE_WRITES"] = ""
    with pytest.raises(identity.ProductionIdentityError, match="environment"):
        identity.verify_execution(request, expected_role=role, module_id="shared-runtime", runtime_id="formal-runtime",
                                  runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())


@pytest.mark.parametrize("version", [2, 3])
def test_v2_and_v3_reject_missing_manifest_source_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int) -> None:
    request, _payload, _private, marker, runtime = oci_fixture(tmp_path, monkeypatch, version=version)
    if version == 3:
        monkeypatch.setattr(identity.os, "environ", {
            "MODE": "STRICT_RUNTIME", "HISTORY_PATH": "/runtime/fixture/history/current.json",
            "SERVICE_URL": "https://production.invalid/", "MARKET_DATA_EXECUTION_GRANT": "/app/auth/grant.json",
        })
    (request.runtime_manifest_path.parent.parent / "app.py").unlink()
    with pytest.raises(identity.ProductionIdentityError, match="source input"):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime",
                                  runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())


def test_v2_rejects_signed_contract_missing_source_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, payload, private, marker, runtime = oci_fixture(tmp_path, monkeypatch, version=2)
    payload["protected_mounts"] = [value for value in payload["protected_mounts"] if value != "/app"]
    sign_grant(request, payload, private)
    with pytest.raises(identity.ProductionIdentityError, match="protected mount contract"):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime",
                                  runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())


def test_v2_rejects_source_input_mount_that_is_not_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, _payload, _private, marker, runtime = oci_fixture(tmp_path, monkeypatch, version=2)
    monkeypatch.setattr(identity, "_mount_options", lambda: {"/": {"ro"}})
    original_mount_for = identity._mount_for

    def observed_mount(mounts, path: Path):
        if path.name == "app.py":
            return {"rw"}
        return original_mount_for(mounts, path)

    monkeypatch.setattr(identity, "_mount_for", observed_mount)
    with pytest.raises(identity.ProductionIdentityError, match="mount permissions"):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime",
                                  runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())


def test_v2_rejects_source_input_file_resolve_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, _payload, _private, marker, runtime = oci_fixture(tmp_path, monkeypatch, version=2)
    source_root = request.runtime_manifest_path.parent.parent
    original_resolve = Path.resolve

    def aliased_resolve(path: Path, strict: bool = False):
        if path == source_root / "app.py":
            return source_root / "aliased-app.py"
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", aliased_resolve)
    with pytest.raises(identity.ProductionIdentityError, match="missing or aliased"):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime",
                                  runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())


def test_v2_rejects_source_input_ancestor_resolve_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, _payload, _private, marker, runtime = oci_fixture(tmp_path, monkeypatch, version=2)
    source_root = request.runtime_manifest_path.parent.parent
    original_resolve = Path.resolve

    def aliased_resolve(path: Path, strict: bool = False):
        if path == source_root:
            return source_root.parent / "aliased-image"
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", aliased_resolve)
    with pytest.raises((identity.ProductionIdentityError, OSError), match="source input|source image|executing source|outside"):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime",
                                  runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())


@pytest.mark.parametrize("mount_options", [{"ro"}, {"rw"}])
def test_v2_rejects_any_source_overlay_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mount_options: set[str]) -> None:
    request, _payload, _private, marker, runtime = oci_fixture(tmp_path, monkeypatch, version=2)
    monkeypatch.setattr(identity, "_mount_options", lambda: {"/": {"ro"}, "/app/app.py": mount_options})
    with pytest.raises(identity.ProductionIdentityError, match="source overlay"):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime",
                                  runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())


def test_legacy_still_requires_historical_protected_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, _payload, _private, marker, runtime = oci_fixture(tmp_path, monkeypatch, version=1)
    (request.runtime_manifest_path.parent.parent / "05_apps").rmdir()
    with pytest.raises(OSError):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime",
                                  runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())


def test_v2_rejects_manifest_root_drift_and_undeclared_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, payload, private, marker, runtime = oci_fixture(tmp_path, monkeypatch, role="candidate_validation", version=2)
    raw = json.loads(request.runtime_manifest_path.read_text(encoding="utf-8"))
    raw["identity_root_role"] = "data"
    request.runtime_manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    payload["runtime_manifest_sha256"] = hashlib.sha256(request.runtime_manifest_path.read_bytes()).hexdigest()
    payload["identity_root_role"] = "data"
    sign_grant(request, payload, private)
    with pytest.raises(identity.ProductionIdentityError, match="identity root|runtime manifest"):
        identity.verify_execution(request, expected_role="candidate_validation", module_id="shared-runtime", runtime_id="formal-runtime",
                                  runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())

    request, _payload, _private, marker, runtime = oci_fixture(tmp_path / "overlay", monkeypatch, version=2)
    monkeypatch.setattr(identity, "_mount_options", lambda: {"/": {"ro"}, "/runtime/fixture/foreign": {"rw"}})
    with pytest.raises(identity.ProductionIdentityError, match="undeclared overlay"):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime",
                                  runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())


@pytest.mark.parametrize("field,value,error", [
    ("release_commit", "0" * 40, "release identity"), ("approved_tree", "0" * 40, "release identity"),
    ("service_id", "other-service", "runtime manifest"), ("runtime_manifest_sha256", "0" * 64, "manifest identity"),
    ("expires_at", "2000-01-01T00:00:00Z", "not currently valid"), ("image_id", "latest", "execution grant is invalid"),
])
def test_oci_signed_mismatch_and_expiry_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str, error: str) -> None:
    request, payload, private, marker, runtime = oci_fixture(tmp_path, monkeypatch)
    payload[field] = value
    sign_grant(request, payload, private)
    with pytest.raises(identity.ProductionIdentityError, match=error):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime", runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())


def test_oci_rejects_signed_grant_lifetime_longer_than_one_hour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, payload, private, marker, runtime = oci_fixture(tmp_path, monkeypatch)
    issued = datetime.now(timezone.utc) - timedelta(minutes=1)
    payload["issued_at"] = issued.isoformat()
    payload["expires_at"] = (issued + timedelta(hours=2)).isoformat()
    sign_grant(request, payload, private)
    with pytest.raises(identity.ProductionIdentityError, match="not currently valid"):
        identity.verify_execution(request, expected_role="production", module_id="shared-runtime", runtime_id="formal-runtime",
                                  runtime_root=runtime, marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest())


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
        if value.endswith("/data"):
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
