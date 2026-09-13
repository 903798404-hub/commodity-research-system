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

# Production release planning regressions. All Docker/host facts are inert fixtures.
def _risk_gate(**changes):
    values = dict(assets_ready=True, candidate_validated=True, irreversible_state_change="NO",
                  compatibility=True, acceptance_plan_ready=True, rehearsal_validated=False,
                  old_grant_expired=True)
    risk = changes.pop("risk", {"RELEASE_RISK_CLASS": "ROUTINE_STATELESS"})
    values.update(changes)
    return runtime.evaluate_release_gate(risk, **values)


def test_routine_expired_grant_is_not_a_rollback_asset_failure():
    result = _risk_gate()
    assert result["PRODUCTION_RELEASE_PREFLIGHT"] == "PASS"
    assert result["ROLLBACK_REHEARSAL_REQUIRED"] is False
    assert result["production_authorized"] is False
    assert result["EXECUTION_AUTHORIZATION"] == "REQUIRED_AT_FRESH_INSTANCE_START"
    assert _risk_gate(old_grant_expired=False)["PRODUCTION_RELEASE_PREFLIGHT"] == "PASS"


@pytest.mark.parametrize("field", ["assets_ready", "candidate_validated", "compatibility", "acceptance_plan_ready"])
def test_routine_requires_all_release_evidence(field):
    assert _risk_gate(**{field: False})["PRODUCTION_RELEASE_PREFLIGHT"] == "FAIL"


@pytest.mark.parametrize("state", ["YES", "UNKNOWN"])
def test_irreversible_or_unknown_state_cannot_be_low_risk(state):
    result = _risk_gate(irreversible_state_change=state, rehearsal_validated=True)
    assert result["RELEASE_RISK_CLASS"] == "STATEFUL_OR_INFRA"
    assert result["ROLLBACK_REHEARSAL_REQUIRED"] is True
    assert result["PRODUCTION_RELEASE_PREFLIGHT"] == "FAIL"


def test_infrastructure_requires_recovery_and_cannot_use_expired_grant_as_rehearsal():
    risk = {"RELEASE_RISK_CLASS": "STATEFUL_OR_INFRA"}
    assert _risk_gate(risk=risk)["failure_codes"] == ["RECOVERY_REHEARSAL_REQUIRED"]
    assert _risk_gate(risk=risk, rehearsal_validated=True)["PRODUCTION_RELEASE_PREFLIGHT"] == "PASS"


@pytest.mark.parametrize("value", ["NO", 0, None])
def test_release_gate_rejects_truthy_or_untyped_observations(value):
    with pytest.raises(runtime.PreReleaseError):
        _risk_gate(assets_ready=value)


@pytest.fixture
def risk_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-q")
    git("config", "user.name", "Risk Fixture")
    git("config", "user.email", "fixture@example.invalid")
    def put(path, value):
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value, encoding="utf-8")
    put("02_configs/project_registry.json", json.dumps({"projects": [{"project_id": "example", "runtime_contract": "02_configs/runtime.json"}]}))
    put("02_configs/runtime.json", json.dumps({"build": {"dockerfile": "Dockerfile", "compose_sources": ["compose.yml"]}}))
    put("Dockerfile", 'FROM immutable\nCOPY ["05_apps/page.py", "/app/page.py"]\n')
    put("compose.yml", "services: {}\n")
    put("05_apps/page.py", "def present(x):\n    return x + 1\n")
    git("add", "."); git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    return repo, git, put, base


@pytest.mark.parametrize("path,content,expected", [
    ("05_apps/page.py", "def present(x):\n    return x + 2\n", "ROUTINE_STATELESS"),
    ("05_apps/page.py", "def present(x):\n    open('prod', 'w').write(x)\n", "STATEFUL_OR_INFRA"),
    ("05_apps/page.py", "def present(x):\n    x[0] = 3\n    return x\n", "STATEFUL_OR_INFRA"),
    ("migrations/001.sql", "ALTER TABLE production ADD value TEXT;", "STATEFUL_OR_INFRA"),
    ("01_data/current.json", "{}", "STATEFUL_OR_INFRA"),
    ("compose.yml", "services: {different: {}}", "STATEFUL_OR_INFRA"),
    ("Dockerfile", "FROM changed\nCOPY . /app\n", "STATEFUL_OR_INFRA"),
    ("09_deploy/off_image.py", "raise RuntimeError()", "ROUTINE_STATELESS"),
])
def test_classifier_uses_git_and_build_not_caller_low_risk(risk_repo, path, content, expected):
    repo, git, put, base = risk_repo
    put(path, content)
    git("add", "."); git("commit", "-qm", "candidate")
    target = git("rev-parse", "HEAD")
    result = runtime.classify_release(repo, base, target, "example")
    assert result["RELEASE_RISK_CLASS"] == expected
    assert result["target"] == {"commit": target, "tree": git("rev-parse", "HEAD^{tree}")}
    assert result["findings"][0]["path"] == path


def test_classifier_tracks_deletions_and_unknown_builds(risk_repo):
    repo, git, put, base = risk_repo
    (repo / "05_apps/page.py").unlink()
    git("add", "-A"); git("commit", "-qm", "delete")
    assert runtime.classify_release(repo, base, git("rev-parse", "HEAD"), "example")["RELEASE_RISK_CLASS"] == "STATEFUL_OR_INFRA"
    with pytest.raises(runtime.PreReleaseError):
        runtime.classify_release(repo, "HEAD", base, "example")


@pytest.fixture
def rollback_assets(tmp_path, risk_repo):
    repo, git, put, base = risk_repo
    tree = git("rev-parse", "HEAD^{tree}")
    image_id = "sha256:" + "1" * 64
    artifact = dict(commit=base, tree=tree, image_id=image_id)
    def sealed(name, payload):
        path = tmp_path / name
        raw = json.dumps(payload).encode()
        path.write_bytes(raw)
        return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
    assets = dict(artifact, image_location="LOCAL_IMAGE_ONLY", registry_digest=None,
                  release=sealed("release", dict(git_commit=base, git_tree=tree, image_id=image_id, release_id="fixture-release")),
                  runtime_config=sealed("config", dict(artifact=artifact,
                      namespace=dict(compose_project="old", container_name="old", host_ports=[8501]),
                      compose=sealed("compose", {"services": {"old": {}}}), environment=sealed("env", {"MODE": "FORMAL"}))),
                  data_schema=sealed("data", dict(schema_identity="no-migration/1", assets=[])),
                  procedure=sealed("procedure", dict(artifact=artifact, fresh_instance_required=True,
                      fresh_grant_required=True, steps=["create exact old image", "issue fresh grant", "start and accept"])))
    image = {"Id": image_id, "Config": {"Labels": {"org.opencontainers.image.revision": base, "market-data.git.tree": tree}}, "RepoDigests": []}
    host = SimpleNamespace(_json=json.loads, _run_docker=lambda args: json.dumps([image]).encode(), _protected_path=lambda p, **kw: p)
    return repo, assets, host, image


def test_rollback_assets_verify_exact_source_image_and_retained_config(rollback_assets):
    repo, assets, host, image = rollback_assets
    result = runtime.verify_rollback_assets(repo, assets, host)
    assert result["ROLLBACK_ASSETS_READY"] is True
    assert result["retention_risk"] == "HOST_LOSS_NOT_COVERED"
    assert _risk_gate(assets_ready=result["ROLLBACK_ASSETS_READY"])["PRODUCTION_RELEASE_PREFLIGHT"] == "PASS"


@pytest.mark.parametrize("failure", ["image", "tree", "release", "runtime_config", "data_schema", "procedure", "digest", "registry"])
def test_rollback_assets_fail_closed_for_missing_or_mismatched_material(rollback_assets, failure):
    repo, assets, host, image = rollback_assets
    if failure == "image":
        host._run_docker = lambda args: b"[]"
    elif failure == "tree":
        assets["tree"] = "0" * 40
    elif failure in ("release", "runtime_config", "data_schema", "procedure"):
        Path(assets[failure]["path"]).unlink()
    elif failure == "digest":
        Path(assets["runtime_config"]["path"]).write_text("changed")
    else:
        assets.update(image_location="REGISTRY_IMMUTABLE_IMAGE", registry_digest="registry.invalid/image@sha256:" + "a" * 64)
    with pytest.raises((runtime.PreReleaseError, OSError)):
        runtime.verify_rollback_assets(repo, assets, host)


def test_registry_image_requires_observed_digest(rollback_assets):
    repo, assets, host, image = rollback_assets
    digest = "registry.invalid/image@sha256:" + "a" * 64
    image["RepoDigests"] = [digest]
    assets.update(image_location="REGISTRY_IMMUTABLE_IMAGE", registry_digest=digest)
    assert runtime.verify_rollback_assets(repo, assets, host)["registry_digest"] == digest


def test_assessment_cli_cannot_be_combined_with_start_options(tmp_path):
    with pytest.raises(SystemExit):
        runtime.main(["--release-request", str(tmp_path / "r"), "--container-id", "a" * 64])
    with pytest.raises(SystemExit):
        runtime.main(["--classify-release", "a" * 40, "b" * 40, "example", "--production-policy", "policy"])

@pytest.mark.parametrize("mutation,expected", [(None, "PASS"), ("migration", "FAIL"), ("irreversible", "FAIL"), ("candidate", "ERROR"), ("acceptance", "ERROR"), ("extra-field", "ERROR")])
def test_protected_release_assessment_end_to_end(rollback_assets, monkeypatch, tmp_path, mutation, expected):
    import copy
    repo, current, host, image = rollback_assets
    target = dict(commit=current["commit"], tree=current["tree"])
    old = copy.deepcopy(current)
    old["commit"] = "2" * 40
    def ref(name, value):
        raw = json.dumps(value).encode()
        p = tmp_path / name
        p.write_bytes(raw)
        return dict(path=str(p), sha256=hashlib.sha256(raw).hexdigest())
    # These are protected operator observations; signatures are exercised by the
    # existing record tests. Actual artifact hashing remains active in this test.
    state = dict(base=target, target=target, data_schema_sha256=current["data_schema"]["sha256"],
                 irreversible="YES" if mutation == "irreversible" else "NO", compatible=True,
                 database_migration=mutation == "migration", production_data_mutation=False, storage_format_change=False)
    candidate = dict(binding=dict(target, project_id="example"), image_id=image["Id"])
    if mutation == "candidate":
        candidate["binding"]["tree"] = "9" * 40
    request = dict(schema_version="production-release-request/1", project_id="example", current=current, previous=old,
                   target_commit=target["commit"], target_source_root=str(repo), candidate_record=ref("candidate-record", candidate),
                   state_plan=ref("state-plan", state), acceptance_plan=ref("acceptance-plan", dict(target=target,
                   image_id="bad" if mutation == "acceptance" else image["Id"], checks=["actual consumer"])), recovery_evidence=None)
    if mutation == "extra-field":
        request["low_risk"] = True
    path = tmp_path / "request"
    path.write_text(json.dumps(request))
    host.require_protected_authority_source = lambda: None
    host._json = json.loads
    engine = SimpleNamespace(_project=lambda *a: {"runtime_contract": "contract"}, source_contract=lambda *a: (None, {}, dict(target, project_id="example")))
    records = SimpleNamespace(verify_record=lambda raw, trust: {"evidence": json.loads(raw)})
    monkeypatch.setattr(runtime, "ROOT", repo)
    (repo / runtime.TRUST).write_text("{}")
    monkeypatch.setattr(runtime, "_load", lambda name, alias: {runtime.HOST: host, runtime.ENGINE: engine, runtime.RECORD: records}[name])
    monkeypatch.setattr(runtime, "require_source", lambda *a, **kw: (target["commit"], target["tree"]))
    monkeypatch.setattr(runtime, "classify_release", lambda *a: dict(schema_version=runtime.RISK_SCHEMA, base=target, target=target, RELEASE_RISK_CLASS="ROUTINE_STATELESS"))
    monkeypatch.setattr(runtime, "verify_rollback_assets", lambda root, a, h: dict(commit=a["commit"], image_id=image["Id"], ROLLBACK_ASSETS_READY=True))
    # asset negative cases have dedicated real verifier tests above; here test
    # wiring/aggregation and source-bound candidate/state/acceptance checks.
    monkeypatch.setattr(runtime, "_write_new", lambda h, p, raw: p.write_bytes(raw))
    output = tmp_path / "result"
    if expected == "ERROR":
        with pytest.raises(runtime.PreReleaseError):
            runtime.assess_release(path, output)
        assert not output.exists()
    else:
        result = runtime.assess_release(path, output)
        assert result["PRODUCTION_RELEASE_PREFLIGHT"] == expected
        assert result["OLD_GRANT_EXPIRED"] == "NOT_READ_NOT_A_GATE"
        assert result["production_authorized"] is False

@pytest.mark.parametrize("mutation", [None, "expired-start", "wrong-instance", "failed-consumer", "wrong-probe-tree", "signature", "revoked"])
def test_recovery_requires_fresh_signed_grant_and_bound_real_probes(tmp_path, monkeypatch, mutation):
    from datetime import timedelta
    spec = importlib.util.spec_from_file_location("recovery_identity_fixture", ROOT / "08_tests/shared/test_production_identity.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    request, payload, private, marker, runtime_root = fixture.oci_fixture(tmp_path / "fixture", monkeypatch)
    source = request.release_path.parent
    original_parser = runtime._load("03_src/agri_research_agent/shared/production_grant.py", "recovery_parser_test")
    monkeypatch.setattr(runtime, "ROOT", source)
    monkeypatch.setattr(runtime, "_load", lambda *args: original_parser)
    issued = datetime.fromisoformat(payload["issued_at"])
    expires = datetime.fromisoformat(payload["expires_at"])
    started = expires + timedelta(seconds=1) if mutation == "expired-start" else issued + timedelta(seconds=1)
    def ref(name, value):
        raw = json.dumps(value).encode()
        p = tmp_path / name
        p.write_bytes(raw)
        return dict(path=str(p), sha256=hashlib.sha256(raw).hexdigest())
    grant = json.loads(request.grant_path.read_bytes())
    if mutation == "signature":
        grant["signature"] = base64.b64encode(b"0" * 64).decode()
    if mutation == "revoked":
        trust_path = source / runtime.TRUST
        trust = json.loads(trust_path.read_bytes())
        trust["revoked_grant_ids"] = [payload["grant_id"]]
        trust_path.write_text(json.dumps(trust))
    instance = dict(container_id=payload["container_id"], image_id=payload["image_id"], hostname=payload["hostname_nonce"],
                    created_at=(issued-timedelta(seconds=1)).isoformat(), started_at=started.isoformat())
    if mutation == "wrong-instance":
        instance["container_id"] = "f" * 64
    evidence = dict(instance=ref("instance", instance), grant=ref("grant", grant))
    for name in ("preflight", "health", "consumer", "data_unchanged"):
        probe = dict(container_id=payload["container_id"], commit=payload["approved_commit"], tree=payload["approved_tree"],
                     image_id=payload["image_id"], exit_code=0, status="PASS", raw=ref(name+"-raw", {"actual": "fixture"}))
        if name == "consumer" and mutation == "failed-consumer":
            probe["exit_code"] = 1
        if name == "preflight" and mutation == "wrong-probe-tree":
            probe["tree"] = "e" * 40
        evidence[name] = ref(name, probe)
    recovery = dict(base=dict(commit=payload["approved_commit"], tree=payload["approved_tree"]), old_image_id=payload["image_id"],
                    observed_at=(started+timedelta(seconds=1)).isoformat(), evidence=evidence)
    host = SimpleNamespace(_json=json.loads, _protected_path=lambda p, **kw: p)
    if mutation:
        from cryptography.exceptions import InvalidSignature
        with pytest.raises((runtime.PreReleaseError, InvalidSignature)):
            runtime.verify_recovery_observation(recovery, host)
    else:
        runtime.verify_recovery_observation(recovery, host)
