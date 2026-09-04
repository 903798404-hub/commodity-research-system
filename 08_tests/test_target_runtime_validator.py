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
        "required_environment": ["DEMO_MODE", "MARKET_DATA_EXECUTION_GRANT"],
        "entrypoint": ["python", "app.py"],
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


def test_caller_selected_docker_endpoint_is_never_a_builder(monkeypatch):
    engine = load_engine()
    monkeypatch.setattr(engine.sys, "platform", "linux")
    monkeypatch.setattr(engine.os, "name", "posix")
    monkeypatch.setattr(engine.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setenv("DOCKER_HOST", "tcp://caller.example:2375")
    with pytest.raises(engine.BuilderUnavailable):
        engine.require_builder()


def test_caller_git_environment_is_rejected(monkeypatch, tmp_path):
    engine = load_engine()
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/replace/")
    with pytest.raises(engine.ValidationError, match="Git environment"):
        engine.source_contract(tmp_path, "demo", "runtime.json")


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
    exact = {"commit": engine._git(repo, "rev-parse", "HEAD"),
             "tree": engine._git(repo, "rev-parse", "HEAD^{tree}")}
    engine.create_archive_context(repo, context, exact)
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


def test_source_compose_must_match_manifest_mount_and_entrypoint(monkeypatch, tmp_path):
    engine = load_engine()
    contract = v2_contract()
    contract.update(build={"compose_sources": ["compose.yml"],
                           "dockerfile": "Dockerfile"}, secret_references=[])
    rendered = {"services": {"demo": {
        "image": "demo@sha256:" + "d" * 64, "entrypoint": contract["entrypoint"],
        "working_dir": "/app", "environment": {
            "DEMO_MODE": "${DEMO_MODE}",
            "MARKET_DATA_EXECUTION_GRANT": "/run/market-data-grants/grant.json"},
        "volumes": [
            {"type": "bind", "source": "${IDENTITY}", "target": "/runtime", "read_only": True},
            {"type": "bind", "source": "${STATE}", "target": "/runtime/state", "read_only": False},
            {"type": "bind", "source": "${GRANTS}", "target": "/run/market-data-grants", "read_only": True},
        ]}}}
    from types import SimpleNamespace
    monkeypatch.setattr(engine, "_docker", lambda *args: SimpleNamespace(
        stdout=json.dumps(rendered).encode()))
    assert engine.validate_source_compose(tmp_path, contract) == rendered
    rendered["services"]["demo"]["volumes"].pop()
    with pytest.raises(engine.ValidationError, match="mounts"):
        engine.validate_source_compose(tmp_path, contract)


def test_secret_file_reference_is_static_only_and_never_enters_candidate(tmp_path, monkeypatch):
    engine = load_engine()
    contract = v2_contract()
    contract.update(build={"compose_sources": ["compose.yml"], "dockerfile": "Dockerfile"},
                    secret_references=["tankan-secret"])
    rendered = {"secrets": {"tankan-secret": {"file": "${TANKAN_SECRET_FILE:?required}"}},
                "services": {"demo": {
        "image": "demo@sha256:" + "d" * 64, "entrypoint": contract["entrypoint"],
        "working_dir": "/app", "environment": {
            "DEMO_MODE": "candidate",
            "MARKET_DATA_EXECUTION_GRANT": "/run/market-data-grants/grant.json"},
        "secrets": [{"source": "tankan-secret", "target": "/run/secrets/tankan.env"}],
        "volumes": [
            {"type": "bind", "source": "${IDENTITY}", "target": "/runtime", "read_only": True},
            {"type": "bind", "source": "${STATE}", "target": "/runtime/state", "read_only": False},
            {"type": "bind", "source": "${GRANTS}", "target": "/run/market-data-grants", "read_only": True},
        ]}}}
    from types import SimpleNamespace
    monkeypatch.setattr(engine, "_docker", lambda *args: SimpleNamespace(
        stdout=json.dumps(rendered).encode()))
    assert engine.validate_source_compose(tmp_path, contract) == rendered
    candidate = engine._compose_document(
        contract, "sha256:" + "e" * 64,
        [{"source": "/tmp/candidate/identity", "target": "/runtime", "read_only": True},
         {"source": "/tmp/candidate/identity/state", "target": "/runtime/state", "read_only": False}],
        tmp_path / "grants", "f" * 32)["services"]["demo"]
    assert "secrets" not in candidate
    assert "/run/secrets/tankan.env" not in json.dumps(candidate)
    assert set(candidate["environment"]) == {"DEMO_MODE", "MARKET_DATA_EXECUTION_GRANT"}
    assert {item["target"] for item in candidate["volumes"]} == {
        "/runtime", "/runtime/state", "/run/market-data-grants"}

    rendered["services"]["demo"]["secrets"].append({"source": "undeclared-secret"})
    with pytest.raises(engine.ValidationError, match="differ from runtime manifest"):
        engine.validate_source_compose(tmp_path, contract)
    rendered["services"]["demo"]["secrets"] = [
        {"source": "tankan-secret", "target": "relative-secret"}]
    with pytest.raises(engine.ValidationError, match="secret targets are invalid"):
        engine.validate_source_compose(tmp_path, contract)
    rendered["services"]["demo"]["secrets"][0]["target"] = "/run/secrets/tankan.env"
    rendered["secrets"]["tankan-secret"] = {"external": True}
    with pytest.raises(engine.ValidationError, match="must be file-backed"):
        engine.validate_source_compose(tmp_path, contract)
    rendered["secrets"].clear()
    with pytest.raises(engine.ValidationError, match="must be file-backed"):
        engine.validate_source_compose(tmp_path, contract)


def test_source_compose_requires_readonly_host_grant_injection(monkeypatch, tmp_path):
    engine = load_engine()
    contract = v2_contract()
    contract.update(build={"compose_sources": ["compose.yml"], "dockerfile": "Dockerfile"},
                    secret_references=[])
    service = {
        "image": "demo@sha256:" + "d" * 64,
        "entrypoint": contract["entrypoint"], "working_dir": "/app",
        "environment": {"DEMO_MODE": "candidate",
                        "MARKET_DATA_EXECUTION_GRANT": "/run/market-data-grants/grant.json"},
        "volumes": [
            {"type": "bind", "source": "${IDENTITY}", "target": "/runtime", "read_only": True},
            {"type": "bind", "source": "${STATE}", "target": "/runtime/state", "read_only": False},
            {"type": "bind", "source": "${GRANTS}", "target": "/run/market-data-grants", "read_only": True},
        ],
    }
    from types import SimpleNamespace
    monkeypatch.setattr(engine, "_docker", lambda *args: SimpleNamespace(
        stdout=json.dumps({"services": {"demo": service}}).encode()))
    assert engine.validate_source_compose(tmp_path, contract)
    service["volumes"].pop()
    with pytest.raises(engine.ValidationError, match="mounts"):
        engine.validate_source_compose(tmp_path, contract)
    service["volumes"].append({"type": "bind", "source": "${GRANTS}",
                               "target": "/run/market-data-grants", "read_only": False})
    with pytest.raises(engine.ValidationError, match="mounts|grant mount"):
        engine.validate_source_compose(tmp_path, contract)
    service["volumes"][-1]["read_only"] = True
    service["environment"]["MARKET_DATA_EXECUTION_GRANT"] = "/tmp/grant.json"
    with pytest.raises(engine.ValidationError, match="grant environment"):
        engine.validate_source_compose(tmp_path, contract)
    service["environment"]["MARKET_DATA_EXECUTION_GRANT"] = "/run/market-data-grants/grant.json"
    service["volumes"].append(dict(service["volumes"][0]))
    with pytest.raises(engine.ValidationError, match="duplicate mount target"):
        engine.validate_source_compose(tmp_path, contract)


@pytest.mark.parametrize("field,value", [
    ("git_commit", "0" * 40), ("git_tree", "1" * 40),
])
def test_release_mismatch_is_a_failure(field, value):
    engine = load_engine()
    candidate = binding()
    release = {"application": "demo", "release_id": "demo-release",
               "git_commit": candidate["commit"], "git_tree": candidate["tree"],
               "build_time": "2026-01-01T00:00:00+00:00",
               "source": "target-runtime-validator/1"}
    release[field] = value
    with pytest.raises(engine.ValidationError, match="RELEASE"):
        engine._release_identity(json.dumps(release).encode(), candidate)


def test_release_application_and_oci_release_id_are_bound():
    engine = load_engine()
    candidate = binding()
    release = {"application": "demo", "release_id": "demo-release",
               "git_commit": candidate["commit"], "git_tree": candidate["tree"],
               "build_time": "2026-01-01T00:00:00+00:00",
               "source": "target-runtime-validator/1"}
    assert engine._release_identity(json.dumps(release).encode(), candidate,
                                    "demo", "demo-release") == release
    for application, release_id in (("other", "demo-release"), ("demo", "other")):
        with pytest.raises(engine.ValidationError, match="RELEASE"):
            engine._release_identity(json.dumps(release).encode(), candidate,
                                     application, release_id)


@pytest.mark.parametrize("mutation", ["missing-source", "wrong-source", "wrong-time", "label-time"])
def test_release_build_origin_is_bound_to_oci_labels(mutation):
    engine = load_engine()
    candidate = binding()
    release = {"application": "demo", "release_id": "demo-release",
               "git_commit": candidate["commit"], "git_tree": candidate["tree"],
               "build_time": "2026-01-01T00:00:00+00:00",
               "source": "target-runtime-validator/1"}
    labels = {"org.opencontainers.image.created": release["build_time"],
              "org.opencontainers.image.source": release["source"]}
    if mutation == "missing-source":
        del release["source"]
    elif mutation == "wrong-source":
        release["source"] = "caller"
    elif mutation == "wrong-time":
        release["build_time"] = "not-a-time"
        labels["org.opencontainers.image.created"] = "not-a-time"
    else:
        labels["org.opencontainers.image.created"] = "2026-01-02T00:00:00+00:00"
    with pytest.raises(engine.ValidationError, match="RELEASE"):
        engine._release_identity(json.dumps(release).encode(), candidate,
                                 "demo", "demo-release", labels)


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


def test_engine_binding_matches_completion_gate_for_same_v2_candidate(tmp_path):
    engine = load_engine()
    sys.path.insert(0, str(ROOT / "04_scripts"))
    from quality import target_runtime_gate as gate
    # The infrastructure project itself is library_only, so compare the exact
    # hashing algorithm over a minimal v2-shaped selection of its owned files.
    contract = {
        "build": {"dockerfile": "Dockerfile", "dockerignore": ".dockerignore",
                  "dependency_contracts": ["requirements.txt"],
                  "compose_sources": ["docker-compose.yml"]},
        "source_inputs": [{"path": "03_src/agri_research_agent/shared/production_identity.py"}],
    }
    selected = {"project_id": "shared-production-infrastructure",
                "runtime_contract": "02_configs/production_runtime_trust.json"}
    paths = [selected["runtime_contract"], "Dockerfile", ".dockerignore", "requirements.txt",
             "docker-compose.yml", gate.ENGINE,
             "03_src/agri_research_agent/shared/production_identity.py",
             gate.MANIFEST_PARSER, gate.MANIFEST_SCHEMA]
    repo = tmp_path / "binding-repo"
    import shutil
    for name in paths:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "binding"], check=True)
    actual = engine._candidate_binding(repo, selected, contract)
    expected_hashes = {name: engine._sha(engine._git(repo, "show", "HEAD:" + name, binary=True))
                       for name in paths}
    assert actual == {"project_id": selected["project_id"],
                      "commit": engine._git(repo, "rev-parse", "HEAD"),
                      "tree": engine._git(repo, "rev-parse", "HEAD^{tree}"),
                      "source_sha256": expected_hashes,
                      "validator_version": gate.VALIDATOR_VERSION}
