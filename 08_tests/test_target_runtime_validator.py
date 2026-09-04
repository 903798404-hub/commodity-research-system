from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "04_scripts/runtime/validate_target_runtime.py"


def load_engine():
    spec = importlib.util.spec_from_file_location("target_runtime_validator_under_test", ENGINE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def binding():
    return {"project_id": "demo", "commit": "a" * 40, "tree": "b" * 40,
            "source_sha256": {"runtime.json": "c" * 64},
            "validator_version": "target-runtime-validator/1"}


def v2_contract():
    return {
        "identity_root_role": "identity",
        "runtime_roots": [
            {"role": "identity", "container_path": "/runtime", "access": "ro"},
            {"role": "state", "container_path": "/runtime/state", "access": "rw"},
        ],
        "required_mounts": [
            {"role": "identity", "container_path": "/runtime", "read_only": True},
            {"role": "state", "container_path": "/runtime/state", "read_only": False},
        ],
        "required_environment": ["DEMO_MODE"], "entrypoint": ["python", "app.py"],
        "working_directory": "/app", "service_id": "demo", "_numeric_uid": 1000,
        "_container_user": "1000:1000",
    }


def test_cli_exposes_only_gate_controlled_source_arguments(tmp_path):
    engine = load_engine()
    args = engine.parse_args(["--project", "demo", "--runtime-contract", "runtime.json",
                              "--evidence-output", str(tmp_path / "evidence.json")])
    assert vars(args) == {"project": "demo", "runtime_contract": "runtime.json",
                          "evidence_output": tmp_path / "evidence.json"}
    for forbidden in ("--image-id", "--evidence", "--ssh-host", "--docker-socket",
                      "--grant", "--production-volume"):
        with pytest.raises(SystemExit):
            engine.parse_args(["--project", "demo", "--runtime-contract", "runtime.json",
                               "--evidence-output", str(tmp_path / "out.json"), forbidden, "x"])


def test_non_linux_or_nonroot_is_builder_unavailable_not_pass(monkeypatch):
    engine = load_engine()
    monkeypatch.setattr(engine.sys, "platform", "win32")
    with pytest.raises(engine.BuilderUnavailable, match=engine.BLOCKED_REASON):
        engine.require_builder()
    evidence = engine.blocked_evidence(binding())
    assert evidence["TARGET_RUNTIME_STATIC_VALIDATION"] == "PASS"
    assert evidence["TARGET_RUNTIME_CONTAINER_VALIDATION"] == "BLOCKED"
    assert evidence["blocked_reason"] == "LINUX_BUILDER_UNAVAILABLE"
    assert "probes" not in evidence and "image_id" not in evidence


def test_evidence_output_is_new_strict_and_never_overwritten(tmp_path):
    engine = load_engine()
    output = tmp_path / "evidence.json"
    value = engine.blocked_evidence(binding())
    engine.write_evidence(output, value)
    assert json.loads(output.read_text(encoding="utf-8")) == value
    with pytest.raises(engine.ValidationError, match="new|overwrite"):
        engine.write_evidence(output, value)


def test_git_archive_context_excludes_git_and_untracked_files(tmp_path):
    engine = load_engine()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    (repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    context = tmp_path / "context"
    engine.create_archive_context(repo, context)
    assert (context / "tracked.txt").read_text() == "tracked\n"
    assert not (context / "ignored.txt").exists()
    assert not any(path.name == ".git" for path in context.rglob("*"))


def test_candidate_scope_layout_keeps_identity_readonly_and_children_writable():
    engine = load_engine()
    result = engine._runtime_bindings(v2_contract(), 1000, 1000)
    assert result == [
        {"relative_path": "identity", "target": "/runtime", "read_only": True,
         "owner_uid": 0, "owner_gid": 0},
        {"relative_path": "identity/state", "target": "/runtime/state", "read_only": False,
         "owner_uid": 1000, "owner_gid": 1000},
    ]


def test_derived_compose_is_candidate_only_and_hardened(tmp_path):
    engine = load_engine()
    contract = v2_contract()
    mounts = [{"source": "/tmp/candidate/identity", "target": "/runtime", "read_only": True},
              {"source": "/tmp/candidate/identity/state", "target": "/runtime/state", "read_only": False}]
    value = engine._compose_document(contract, "sha256:" + "d" * 64, mounts,
                                     tmp_path / "grants", "e" * 32)
    service = value["services"]["demo"]
    assert service["network_mode"] == "none" and service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["user"] == "1000:1000"
    assert all("production" not in item["source"] for item in service["volumes"])
    assert all("docker.sock" not in item["source"] for item in service["volumes"])


@pytest.mark.parametrize("field,value", [
    ("git_commit", "0" * 40), ("git_tree", "1" * 40),
])
def test_release_mismatch_is_a_failure(field, value):
    engine = load_engine()
    candidate = binding()
    release = {"application": "demo", "git_commit": candidate["commit"],
               "git_tree": candidate["tree"]}
    release[field] = value
    with pytest.raises(engine.ValidationError, match="RELEASE"):
        engine._release_identity(json.dumps(release).encode(), candidate)


def test_strict_json_rejects_duplicate_and_nonfinite_values():
    engine = load_engine()
    for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'[]', b'\xff'):
        with pytest.raises(engine.ValidationError):
            engine._strict_json(raw, "fixture")


def test_probe_evidence_cannot_be_complete_without_every_actual_result():
    engine = load_engine()
    assert engine.REQUIRED_PROBES == {
        "entrypoint_initialization", "runtime_identity", "dependencies", "runtime_paths",
        "mount_permissions", "missing_grant_rejected", "wrong_commit_rejected",
        "wrong_tree_rejected", "wrong_image_rejected", "wrong_service_rejected",
        "wrong_manifest_rejected", "preview_write_rejected", "release_mismatch_rejected",
    }

