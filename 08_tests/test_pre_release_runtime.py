from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pre_release_runtime", ROOT / "04_scripts/runtime/pre_release_runtime.py")
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)

RECORD_SPEC = importlib.util.spec_from_file_location("candidate_record_for_pre_release", ROOT / "09_deploy/runtime_identity/candidate_validation_record.py")
assert RECORD_SPEC and RECORD_SPEC.loader
record = importlib.util.module_from_spec(RECORD_SPEC)
RECORD_SPEC.loader.exec_module(record)


def test_cli_rejects_caller_evidence_and_identity_inputs(tmp_path):
    for option in ("--evidence", "--image-id", "--container-id", "--probe", "--source-binding"):
        with pytest.raises(SystemExit):
            runtime.main(["--project", "demo", "--record-output", str(tmp_path / "r"),
                          "--candidate-key", str(tmp_path / "k"), option, "x"])


def test_production_cli_calls_host_and_reports_host_digest(monkeypatch, tmp_path, capsys):
    policy = tmp_path / "policy.json"
    output = tmp_path / "report.json"
    calls = {}
    report = {"schema_version": "production-pre-release-validation/1", "PRE_RELEASE_VALIDATION": "PASS",
              "production_write_granted": False, "container_started": False,
              "container_id": "a" * 64}

    def revalidate(container_id, policy_path, destination):
        calls.update(container_id=container_id, policy_path=policy_path, destination=destination)
        return report

    monkeypatch.setattr(runtime, "revalidate_production", revalidate)
    assert runtime.main(["--production-policy", str(policy), "--container-id", "a" * 64,
                         "--report-output", str(output)]) == 0
    assert calls == {"container_id": "a" * 64, "policy_path": policy, "destination": output}
    assert json.loads(capsys.readouterr().out)["PRODUCTION_REVALIDATION"] == "PASS"


def test_production_cli_rejects_candidate_and_caller_fields(tmp_path):
    with pytest.raises(SystemExit):
        runtime.main(["--production-policy", str(tmp_path / "policy"), "--container-id", "a" * 64,
                      "--report-output", str(tmp_path / "out"), "--candidate-key", str(tmp_path / "key")])


def _production_host(monkeypatch, tmp_path, result):
    source = tmp_path / "source"
    source.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    destination = tmp_path / "report.json"
    class Host:
        def require_protected_authority_source(self):
            return None
        def _protected_path(self, path, **kwargs):
            return path
        def _fsync_directory(self, path):
            return None
        def revalidate_production(self, container_id, *, expected_policy_path):
            return result
    host = Host()
    monkeypatch.setattr(runtime, "ROOT", source)
    monkeypatch.setattr(runtime, "_load", lambda relative, name: host if relative == runtime.HOST else object())
    monkeypatch.setattr(runtime, "require_source", lambda host, engine: ("c", "t"))
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 0, raising=False)
    return host, policy, destination


def test_production_revalidation_is_readonly_and_does_not_overwrite(monkeypatch, tmp_path):
    result = {"schema_version": "production-pre-release-validation/1", "PRE_RELEASE_VALIDATION": "PASS",
              "production_write_granted": False, "container_started": False,
              "container_id": "a" * 64}
    host, policy, destination = _production_host(monkeypatch, tmp_path, result)
    wrapped = runtime.revalidate_production("a" * 64, policy, destination)
    assert wrapped["report"] == result
    assert json.loads(destination.read_text()) == wrapped
    with pytest.raises(runtime.PreReleaseError, match="new absolute"):
        runtime.revalidate_production("a" * 64, policy, destination)


def test_production_policy_v5_uses_the_same_strict_revalidation_contract(monkeypatch, tmp_path):
    result = {"schema_version": "production-pre-release-validation/1", "PRE_RELEASE_VALIDATION": "PASS",
              "production_write_granted": False, "container_started": False,
              "container_id": "a" * 64}
    host, policy, destination = _production_host(monkeypatch, tmp_path, result)
    policy.write_text(json.dumps({"schema_version": "host-runtime-policy/5"}), encoding="utf-8")
    wrapped = runtime.revalidate_production("a" * 64, policy, destination)
    assert wrapped["report"] == result


def test_production_revalidation_propagates_host_failure_without_output(monkeypatch, tmp_path):
    host, policy, destination = _production_host(monkeypatch, tmp_path, {})
    def fail(*args, **kwargs):
        raise RuntimeError("engine failed")
    host.revalidate_production = fail
    with pytest.raises(RuntimeError, match="engine failed"):
        runtime.revalidate_production("a" * 64, policy, destination)
    assert not destination.exists()


def test_validation_is_blocked_on_non_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    with pytest.raises(runtime.ValidationBlocked, match="LINUX_BUILDER_UNAVAILABLE"):
        runtime.validate_candidate("demo", tmp_path / "record.json", tmp_path / "key.pem")


def test_new_output_is_absolute_outside_source_and_non_overwriting(monkeypatch, tmp_path):
    host = SimpleNamespace(_protected_path=lambda path, **kwargs: path)
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(runtime, "ROOT", source)
    destination = tmp_path / "record.json"
    runtime._new_output(host, destination)
    destination.write_bytes(b"existing")
    with pytest.raises(runtime.PreReleaseError, match="new absolute"):
        runtime._new_output(host, destination)
    with pytest.raises(runtime.PreReleaseError, match="outside source"):
        runtime._new_output(host, source / "nested.json")


def test_execute_validation_uses_isolated_engine_without_caller_evidence(monkeypatch, tmp_path):
    seen = {}

    def run(command, **kwargs):
        seen["command"] = command
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime.subprocess, "run", run)
    project = {"project_id": "demo", "runtime_contract": "contract.json"}
    output = tmp_path / "evidence.json"
    assert runtime._execute_validation(project, output) == 0
    assert "-I" in seen["command"] and "--evidence" not in seen["command"]
    assert seen["command"][-1] == str(output)


@pytest.mark.parametrize("mutation,expected", [
    ("hidden", "hidden Git index"), ("dirty", "dirty"),
    ("detached", "detached"), ("bytes", "Approved Git object"),
])
def test_require_source_rejects_hidden_index_dirty_detached_or_source_bytes(
        monkeypatch, tmp_path, mutation, expected):
    source = tmp_path / "repo"
    source.mkdir()
    (source / ".git").mkdir()
    (source / "tracked.py").write_bytes(b"working" if mutation == "bytes" else b"tracked")
    monkeypatch.setattr(runtime, "ROOT", source)
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)
    host = SimpleNamespace(_require_linux_root=lambda: None,
                           _protected_path=lambda path, **kwargs: path)
    values = {"--show-toplevel": str(source), "--abbrev-ref": "HEAD", "status": "", "for-each-ref": ""}

    def git(root, *args, binary=False):
        if args[:2] == ("ls-files", "-v"):
            return (b"S tracked.py\0" if mutation == "hidden" else b"H tracked.py\0") if binary else ""
        if args[0] == "show":
            return b"different" if mutation == "bytes" else b"tracked"
        if args[0] == "archive":
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as archive:
                info = tarfile.TarInfo("tracked.py")
                info.size = len(b"tracked")
                archive.addfile(info, io.BytesIO(b"tracked"))
            return stream.getvalue()
        if args[0] == "status" and mutation == "dirty":
            return "M tracked.py"
        if args[0] == "rev-parse" and args[1] == "--abbrev-ref":
            return "main" if mutation == "detached" else "HEAD"
        return values.get(args[0] if args[0] in values else args[1], "")

    engine = SimpleNamespace(_git=git)
    with pytest.raises(runtime.PreReleaseError, match=expected):
        runtime.require_source(host, engine)


def _evidence(binding):
    image = "sha256:" + "e" * 64
    rendered = "f" * 64
    return {"schema_version": "target-runtime-evidence/1", "binding": binding,
            "TARGET_RUNTIME_STATIC_VALIDATION": "PASS", "TARGET_RUNTIME_CONTAINER_VALIDATION": "PASS",
            "image_id": image, "rendered_compose_sha256": rendered,
            "builder": {"builder_id": "linux-fixture", "os": "linux", "execution": "isolated"},
            "observed_identity": {"image_id": image, "oci_revision": binding["commit"],
                "git_tree": binding["tree"], "source_sha256": binding["source_sha256"],
                "rendered_compose_sha256": rendered, "authorization_role": "candidate_validation",
                "git_metadata_present": False, "production_volumes_mounted": False},
            "probes": {name: "PASS" for name in record._PROBES}}


@pytest.mark.parametrize("failure", [None, "nonzero", "blocked", "no-output", "binding", "probe", "source-drift", "key-domain"])
def test_candidate_validation_engine_and_record_fail_closed(monkeypatch, tmp_path, failure):
    private = Ed25519PrivateKey.generate()
    key_path = tmp_path / "candidate.pem"
    trust_path = tmp_path / "trust.json"
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    key_path.write_bytes(private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    trust_path.write_text(json.dumps({"schema_version": "production-runtime-trust/1", "keys": [{
        "key_id": "candidate-key", "domain": "production" if failure == "key-domain" else "candidate_validation", "algorithm": "ed25519",
        "public_key_base64": public}], "revoked_key_ids": [], "revoked_grant_ids": []}), encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(runtime, "ROOT", source)
    monkeypatch.setattr(runtime, "TRUST", str(trust_path))
    monkeypatch.setattr(runtime, "KEY_DIRECTORY", tmp_path)
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 0, raising=False)
    binding = {"project_id": "demo", "commit": "a" * 40, "tree": "b" * 40,
               "source_sha256": {"app.py": "c" * 64}, "validator_version": "target-runtime-validator/1"}
    evidence = _evidence(binding)
    project = {"project_id": "demo", "runtime_contract": "contract.json"}
    host = SimpleNamespace(require_protected_authority_source=lambda: None,
                           _protected_path=lambda path, **kwargs: path,
                           _load_private_key=lambda path: private,
                           _json=lambda raw: json.loads(raw),
                           _fsync_directory=lambda path: None)
    engine = SimpleNamespace(_project=lambda root, project_id: project,
                             source_contract=lambda *args: (project, {}, binding))
    def load(path, name):
        if path == runtime.HOST:
            return host
        if path == runtime.ENGINE:
            return engine
        return record
    monkeypatch.setattr(runtime, "_load", load)
    source_results = iter([(binding["commit"], binding["tree"]), ("x" * 40, "y" * 40)])
    monkeypatch.setattr(runtime, "require_source", lambda *args: next(source_results) if failure == "source-drift" else (binding["commit"], binding["tree"]))
    def execute(project_value, output):
        if failure == "nonzero":
            return 2
        if failure == "blocked":
            return 3
        if failure == "no-output":
            return 0
        selected = evidence
        if failure == "binding":
            selected = dict(evidence, binding=dict(binding, commit="f" * 40))
        elif failure == "probe":
            selected = dict(evidence, probes=dict(evidence["probes"], dependencies="FAIL"))
        output.write_bytes(record.canonical(selected))
        return 0
    monkeypatch.setattr(runtime, "_execute_validation", execute)
    output = tmp_path / "record.json"
    if failure is None:
        payload = runtime.validate_candidate("demo", output, key_path, ttl_seconds=3600)
        assert payload["authorization_role"] == "candidate_validation"
        assert record.verify_record(output.read_bytes(), json.loads(trust_path.read_text()), now=datetime.now(timezone.utc))["record_id"] == payload["record_id"]
    else:
        error = runtime.ValidationBlocked if failure == "blocked" else (runtime.PreReleaseError, ValueError)
        with pytest.raises(error):
            runtime.validate_candidate("demo", output, key_path, ttl_seconds=3600)
        assert not output.exists()
