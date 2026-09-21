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


@pytest.mark.parametrize('state,destructive,expected', [
    ('ADDITIVE_REVERSIBLE', False, 'PASS'),
    ('HIGH_RISK', False, 'FAIL'),
    ('NEEDS_MAINTAINER_RISK_REVIEW', False, 'FAIL'),
    ('UNKNOWN', False, 'FAIL'),
    ('ADDITIVE_REVERSIBLE', True, 'FAIL'),
])
def test_stateful_release_treatment_never_uses_additive_to_downgrade_risk(state, destructive, expected):
    risk = dict(RELEASE_RISK_CLASS='STATEFUL_OR_INFRA', STATE_CHANGE_CLASS=state,
                MACHINE_DESTRUCTIVE_EVIDENCE=destructive)
    result = _risk_gate(risk=risk)
    assert result['PRODUCTION_RELEASE_PREFLIGHT'] == expected
    assert result['TARGETED_RECOVERY_VALIDATION_REQUIRED'] is (state != 'ADDITIVE_REVERSIBLE' or destructive)
    if expected == 'PASS':
        assert result['FULL_ROLLBACK_REHEARSAL_REQUIRED'] is False
        assert result['production_authorized'] is False


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
    result = _risk_gate(risk=risk)
    assert 'RECOVERY_REHEARSAL_REQUIRED' in result['failure_codes']
    assert 'STATE_COMPATIBILITY_UNPROVEN' in result['failure_codes']
    assert _risk_gate(risk=risk, rehearsal_validated=True)["PRODUCTION_RELEASE_PREFLIGHT"] == "FAIL"


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
    put("02_configs/runtime.json", json.dumps({"entrypoint": ["python", "05_apps/page.py"], "build": {"dockerfile": "Dockerfile", "compose_sources": ["compose.yml"]}}))
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


@pytest.mark.parametrize('activation', ['inactive', 'import', 'relative', 'literal-loader', 'dynamic-loader', 'compose-reference'])
def test_packaged_tool_risk_depends_on_runtime_reachability(risk_repo, activation):
    repo, git, put, _ = risk_repo
    put('Dockerfile', 'FROM immutable\nCOPY . /app\n')
    put('04_scripts/tool.py', 'def run():\n    return 1\n')
    put('04_scripts/__init__.py', '')
    if activation == 'import':
        put('05_apps/page.py', 'import tool\ndef present(x):\n    return x\n')
    elif activation == 'relative':
        put('05_apps/page.py', 'import package\n')
        put('05_apps/package/__init__.py', 'from . import child\n')
        put('05_apps/package/child.py', 'import tool\n')
    elif activation == 'literal-loader':
        put('05_apps/page.py', "import importlib\nimportlib.import_module('tool')\n")
    elif activation == 'dynamic-loader':
        put('05_apps/page.py', "import importlib,os\nimportlib.import_module(os.environ['PLUGIN'])\n")
    elif activation == 'compose-reference':
        put('compose.yml', 'services:\n  app:\n    command: python /app/04_scripts/tool.py\n')
    git('add', '.'); git('commit', '-qm', 'runtime topology')
    base = git('rev-parse', 'HEAD')
    put('04_scripts/tool.py', "def run():\n    open('production', 'w').write('changed')\n")
    git('add', '.'); git('commit', '-qm', 'tool delta')
    report = runtime.classify_release(repo, base, git('rev-parse', 'HEAD'), 'example')
    assert report['RELEASE_RISK_CLASS'] == ('ROUTINE_STATELESS' if activation == 'inactive' else 'STATEFUL_OR_INFRA')
    finding = report['findings'][0]
    assert finding['image_input'] is True
    if activation == 'inactive':
        assert finding['reason'] == 'PACKAGED_INACTIVE_CHANGE'
    elif activation != 'dynamic-loader':
        assert finding['runtime_effect'] == 'RUNTIME_ACTIVE_CHANGE'
    else:
        assert not report['RUNTIME_EFFECTIVE_DELTA']['target']['complete']


@pytest.mark.parametrize('body,expected', [
    ('rows = []\n    for month in range(1, 13):\n        rows.append({"month": month, "value": x.get(month)})\n    return rows', 'ROUTINE_STATELESS'),
    ('x[0] = 1\n    return x', 'STATEFUL_OR_INFRA'),
    ('x.append(1)\n    return x', 'STATEFUL_OR_INFRA'),
    ('open("production", "w").write(str(x))\n    return x', 'STATEFUL_OR_INFRA'),
    ('unknown_provider(x)\n    return x', 'STATEFUL_OR_INFRA'),
])
def test_presentation_effect_delta_retains_month_shaping_but_rejects_writes(risk_repo, body, expected):
    repo, git, put, base = risk_repo
    put('05_apps/page.py', 'def present(x):\n    '+body+'\n')
    git('add', '.'); git('commit', '-qm', 'presentation effects')
    result = runtime.classify_release(repo, base, git('rev-parse', 'HEAD'), 'example')
    assert result['RELEASE_RISK_CLASS'] == expected


def test_application_authorization_guard_is_not_a_state_contract_change(risk_repo):
    repo, git, put, _ = risk_repo
    put('05_apps/page.py', 'def save(x):\n    return x\n\ndef present(x):\n    return save(x)\n')
    git('add', '.'); git('commit', '-qm', 'guard baseline')
    base = git('rev-parse', 'HEAD')
    put('05_apps/page.py', '''from typing import Callable

def save(x, authorize_write: Callable[[], None] | None = None):
    if authorize_write is not None:
        authorize_write()
    return x

def present(x, authorize_write: Callable[[], None] | None = None):
    return save(x, authorize_write=authorize_write)
''')
    git('add', '.'); git('commit', '-qm', 'revalidate authorization')
    result = runtime.classify_release(repo, base, git('rev-parse', 'HEAD'), 'example')
    assert result['RELEASE_RISK_CLASS'] == 'ROUTINE_STATELESS'
    assert result['findings'][0]['reason'] == 'STATELESS_APPLICATION_EXECUTABLE_DELTA'
    assert result['MACHINE_DESTRUCTIVE_EVIDENCE'] is False


@pytest.mark.parametrize('change', [
    'persistent-path', 'schema-migration', 'compose-mount', 'destructive-write',
    'changed-storage-root', 'unknown-provider',
])
def test_application_executable_delta_does_not_hide_state_or_unknown_risk(risk_repo, change):
    repo, git, put, base = risk_repo
    put('05_apps/page.py', 'def present(x):\n    return x + 2\n')
    if change == 'persistent-path':
        put('05_apps/page.py', 'def present(x):\n    return open("/new/operational-store.json", "w").write(str(x))\n')
    elif change == 'schema-migration':
        put('migrations/001.sql', 'ALTER TABLE production ADD value TEXT;\n')
    elif change == 'compose-mount':
        put('compose.yml', 'services:\n  app:\n    volumes: ["/new/data:/runtime/data:rw"]\n')
    elif change == 'destructive-write':
        put('05_apps/page.py', 'def present(x):\n    open("history", "w").write(str(x))\n    return x\n')
    elif change == 'changed-storage-root':
        put('05_apps/page.py', 'def present(x):\n    result_root = "/new/operational-store"\n    return x + 2\n')
    else:
        put('05_apps/page.py', 'def present(x):\n    return unknown_provider(x)\n')
    git('add', '.'); git('commit', '-qm', 'state or unknown change')
    result = runtime.classify_release(repo, base, git('rev-parse', 'HEAD'), 'example')
    assert result['RELEASE_RISK_CLASS'] == 'STATEFUL_OR_INFRA'
    assert result['FULL_ROLLBACK_REHEARSAL_REQUIRED'] is True
    if change in ('schema-migration', 'compose-mount'):
        assert any(f['high_risk'] for f in result['findings'] if f['path'] != '05_apps/page.py')
    else:
        assert result['findings'][0]['reason'] == 'UNKNOWN_EXECUTABLE_CHANGE'


def test_application_business_calculation_and_docs_only_keep_routine_class(risk_repo):
    repo, git, put, base = risk_repo
    put('05_apps/page.py', 'def present(x):\n    return max(0, x * 2)\n')
    put('07_docs/contract.md', 'Calculation display contract.\n')
    put('08_tests/test_page.py', 'def test_example(): assert True\n')
    git('add', '.'); git('commit', '-qm', 'calculation and contract')
    result = runtime.classify_release(repo, base, git('rev-parse', 'HEAD'), 'example')
    assert result['RELEASE_RISK_CLASS'] == 'ROUTINE_STATELESS'
    assert result['MACHINE_DESTRUCTIVE_EVIDENCE'] is False
    assert all(not finding['high_risk'] for finding in result['findings'])


def test_opaque_runtime_graph_blocks_application_routine_proof(risk_repo):
    repo, git, put, _ = risk_repo
    put('05_apps/page.py', 'import importlib\nimportlib.import_module(runtime_plugin)\ndef present(x):\n    return x + 1\n')
    git('add', '.'); git('commit', '-qm', 'dynamic baseline')
    base = git('rev-parse', 'HEAD')
    put('05_apps/page.py', 'import importlib\nimportlib.import_module(runtime_plugin)\ndef present(x):\n    return x + 2\n')
    git('add', '.'); git('commit', '-qm', 'dynamic application change')
    result = runtime.classify_release(repo, base, git('rev-parse', 'HEAD'), 'example')
    assert result['RUNTIME_EFFECTIVE_DELTA']['target']['complete'] is False
    assert result['RELEASE_RISK_CLASS'] == 'STATEFUL_OR_INFRA'
    assert result['findings'][0]['reason'] == 'UNKNOWN_EXECUTABLE_CHANGE'


def test_exact_soybean_read_write_delta_is_routine_without_state_footprint():
    base = 'b12e878e33d16dcfa7a43f86ee8357b542cd41cf'
    target = '7845c31610ffa74cc52dc699dc9ece9910fe330f'
    result = runtime.classify_release(ROOT, base, target, 'spread-production-runtime-wiring')
    assert result['base']['commit'] == base
    assert result['target'] == {'commit': target, 'tree': '2ff9da714e81799a293ee601715a1fa47c7003ac'}
    assert result['RELEASE_RISK_CLASS'] == 'ROUTINE_STATELESS'
    assert result['MACHINE_DESTRUCTIVE_EVIDENCE'] is False
    assert result['TARGETED_RECOVERY_VALIDATION_REQUIRED'] is False
    assert result['FULL_ROLLBACK_REHEARSAL_REQUIRED'] is False
    assert result['build_projection_complete'] is True
    assert result['RUNTIME_EFFECTIVE_DELTA']['base']['complete'] is True
    assert result['RUNTIME_EFFECTIVE_DELTA']['target']['complete'] is True
    findings = {finding['path']: finding for finding in result['findings']}
    assert {path for path, finding in findings.items() if finding['runtime_effect'] == 'RUNTIME_ACTIVE_CHANGE'} == {
        '03_src/agri_research_agent/pipelines/soybean_intraday.py',
        '05_apps/import_profit_intraday_runtime_page.py',
    }
    assert all(finding['reason'] == 'STATELESS_APPLICATION_EXECUTABLE_DELTA'
               for finding in findings.values() if finding['runtime_effect'] == 'RUNTIME_ACTIVE_CHANGE')


def test_new_runtime_import_is_not_packaged_inactive(risk_repo):
    repo, git, put, _ = risk_repo
    put('Dockerfile', 'FROM immutable\nCOPY . /app\n')
    put('04_scripts/tool.py', 'def run():\n    return 1\n')
    git('add', '.'); git('commit', '-qm', 'packaged tool')
    base = git('rev-parse', 'HEAD')
    put('05_apps/page.py', 'import tool\ndef present(x):\n    return x + 2\n')
    git('add', '.'); git('commit', '-qm', 'activate tool')
    result = runtime.classify_release(repo, base, git('rev-parse', 'HEAD'), 'example')
    assert result['RELEASE_RISK_CLASS'] == 'STATEFUL_OR_INFRA'
    assert '04_scripts/tool.py' in result['RUNTIME_EFFECTIVE_DELTA']['target']['active_paths']


def test_real_soybean_target_uses_effective_delta_not_sha_allowlist():
    base = 'e42a61e390ca4f9be6aff30025c640447d3f8fdd'
    target = '2c67e156c7bcfa812a8f733b9fdba0b61b1c0091'
    result = runtime.classify_release(ROOT, base, target, 'spread-production-runtime-wiring')
    assert result['RELEASE_RISK_CLASS'] == 'ROUTINE_STATELESS'
    assert result['ROLLBACK_REHEARSAL_REQUIRED'] is False
    findings = {f['path']: f for f in result['findings']}
    assert findings['03_src/agri_research_agent/automation/full_daily_windows.py']['reason'] == 'PACKAGED_INACTIVE_CHANGE'
    assert findings['05_apps/import_profit_intraday_page.py']['reason'] == 'STATELESS_PRESENTATION_EFFECT_DELTA'
    assert result['target']['tree'] == '3fd94bc4ec5f781ff4f79afe6490244479d8d506'


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

@pytest.mark.parametrize("mutation,expected", [(None, "PASS"), ("migration", "FAIL"), ("irreversible", "FAIL"), ("candidate", "ERROR"), ("acceptance", "ERROR"), ("extra-field", "ERROR"), ("target-output", "ERROR")])
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
    output = repo / "result" if mutation == "target-output" else tmp_path / "result"
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

# Reversibility classification uses real temporary Git trees, not caller labels.
@pytest.fixture
def additive_repo(risk_repo):
    import yaml
    repo, git, put, _ = risk_repo
    contract = dict(service_id='app', identity_root_role='identity', entrypoint=['python','05_apps/page.py'],
        build=dict(dockerfile='Dockerfile',compose_sources=['compose.yml']),
        runtime_roots=[dict(role='history',container_path='/history',access='ro')],
        required_mounts=[dict(role='history',container_path='/history',read_only=True)],
        required_environment=[],environment_bindings=[],forbidden_environment=[],source_inputs=[])
    compose = dict(services=dict(app=dict(image='immutable',volumes=[dict(type='bind',source='${HISTORY:?required}',
        target='/history',read_only=True)], environment={})))
    put('02_configs/runtime.json',json.dumps(contract))
    put('compose.yml',yaml.safe_dump(compose))
    put('05_apps/page.py','from pathlib import Path\ndef history():\n    return Path("/history/old.txt").read_text()\n')
    git('add','.');git('commit','-qm','unchanged runtime')
    base=git('rev-parse','HEAD')
    contract['runtime_roots'].append(dict(role='entries',container_path='/operational/entries',access='rw'))
    contract['required_mounts'].append(dict(role='entries',container_path='/operational/entries',read_only=False))
    compose['services']['app']['volumes'].append(dict(type='bind',source='${ENTRIES:?required}',
        target='/operational/entries',read_only=False,bind=dict(create_host_path=False)))
    put('02_configs/runtime.json',json.dumps(contract))
    put('compose.yml',yaml.safe_dump(compose))
    code=(repo/'05_apps/page.py').read_text()+('def save(value):\n    p=Path("/operational/entries/new.txt")\n    p.write_text(value)\n'
        'def read_new():\n    p=Path("/operational/entries/new.txt")\n    return p.read_text()\n')
    put('05_apps/page.py',code)
    return repo,git,put,base,contract,compose,code


def additive_report(fixture):
    repo,git,put,base,*_=fixture
    git('add','.');git('commit','-qm','state delta')
    return runtime.classify_release(repo,base,git('rev-parse','HEAD'),'example')


def test_independent_added_store_requires_targeted_only(additive_repo):
    # Keep the historical node identity for the baseline ratchet. The policy
    # changed: proven additive state now requires neither recovery rehearsal.
    r=additive_report(additive_repo)
    assert r['RELEASE_RISK_CLASS']=='STATEFUL_OR_INFRA'
    assert r['STATE_CHANGE_CLASS']=='ADDITIVE_REVERSIBLE', r
    assert r['FULL_ROLLBACK_REHEARSAL_REQUIRED'] is False
    assert r['TARGETED_RECOVERY_VALIDATION_REQUIRED'] is False
    assert _risk_gate(risk=r)['PRODUCTION_RELEASE_PREFLIGHT']=='PASS'
    assert _risk_gate(risk=r)['production_authorized'] is False


@pytest.mark.parametrize('fault', ['db-migration','history-rewrite','format-conversion','old-delete',
    'mount-replaced','mount-overlap','ephemeral','autocreate','old-source','unproven-write',
    'missing-reader','old-reader-change','authorization','lifecycle','env-claim','path-shadow'])
def test_additive_proof_rejects_unsafe_or_unknown_delta(additive_repo,fault):
    import yaml
    repo,git,put,base,contract,compose,code=additive_repo
    if fault=='db-migration':put('migrations/01.sql','ALTER TABLE state ADD x TEXT;')
    elif fault=='history-rewrite':put('01_data/history.parquet','rewritten')
    elif fault=='format-conversion':put('storage/convert.py','def convert():\n    pass\n')
    elif fault=='old-delete':(repo/'05_apps/page.py').unlink()
    elif fault=='mount-replaced':compose['services']['app']['volumes'][0]['read_only']=False
    elif fault=='mount-overlap':contract['runtime_roots'][-1]['container_path']='/history/entries'
    elif fault=='ephemeral':compose['services']['app']['volumes'][-1]['type']='volume'
    elif fault=='autocreate':compose['services']['app']['volumes'][-1]['bind']['create_host_path']=True
    elif fault=='old-source':compose['services']['app']['volumes'][-1]['source']='${HISTORY:?required}'
    elif fault=='unproven-write':put('05_apps/page.py',code+'def other():\n    external_database.migrate()\n')
    elif fault=='missing-reader':put('05_apps/page.py',code[:code.index('def read_new')])
    elif fault=='old-reader-change':put('05_apps/page.py',code.replace('Path("/history/old.txt").read_text()', 'Path("/operational/entries/new.txt").write_text("overwrite")'))
    elif fault=='authorization':contract['identity_kind']='new-grant-system'
    elif fault=='lifecycle':compose['services']['app']['command']='migrate-and-start'
    elif fault=='env-claim':contract['STATE_CHANGE_CLASS']='ADDITIVE_REVERSIBLE'
    elif fault=='path-shadow':put('05_apps/page.py',code+'Path=unsafe_writer\n')
    put('02_configs/runtime.json',json.dumps(contract));put('compose.yml',yaml.safe_dump(compose))
    r=additive_report(additive_repo)
    assert r['STATE_CHANGE_CLASS'] in ('NEEDS_MAINTAINER_RISK_REVIEW','IRREVERSIBLE_OR_DESTRUCTIVE'),r
    assert r['FULL_ROLLBACK_REHEARSAL_REQUIRED'] is True
    assert r['TARGETED_RECOVERY_VALIDATION_REQUIRED'] is True
    assert _risk_gate(risk=r,rehearsal_validated=True,targeted_recovery_validated=True)['PRODUCTION_RELEASE_PREFLIGHT']=='FAIL'


def test_stateless_recovery_requirements_unchanged(risk_repo):
    repo,git,put,base=risk_repo
    put('05_apps/page.py','def present(x):\n    return x+3\n')
    git('add','.');git('commit','-qm','presentation')
    r=runtime.classify_release(repo,base,git('rev-parse','HEAD'),'example')
    assert not r['FULL_ROLLBACK_REHEARSAL_REQUIRED'] and not r['TARGETED_RECOVERY_VALIDATION_REQUIRED']
    assert _risk_gate(risk=r)['PRODUCTION_RELEASE_PREFLIGHT']=='PASS'


def test_destructive_operator_facts_cannot_be_overridden_by_additive_label():
    r=dict(RELEASE_RISK_CLASS='STATEFUL_OR_INFRA',STATE_CHANGE_CLASS='ADDITIVE_REVERSIBLE')
    result=_risk_gate(risk=r,irreversible_state_change='YES',rehearsal_validated=True,targeted_recovery_validated=True)
    assert result['STATE_CHANGE_CLASS']=='IRREVERSIBLE_OR_DESTRUCTIVE'
    assert result['FULL_ROLLBACK_REHEARSAL_REQUIRED'] is True
    assert result['PRODUCTION_RELEASE_PREFLIGHT']=='FAIL'


@pytest.mark.parametrize('fault',[None,'http','history','new-data','removed-directory','wrong-source',
    'old-writer','missing-surface','missing-mounts','invalid-hash','empty-history'])
def test_targeted_recovery_retains_history_and_new_bind_sources(tmp_path,fault):
    import copy
    def ref(name,value):
        p=tmp_path/name;raw=json.dumps(value).encode();p.write_bytes(raw)
        return dict(path=str(p),sha256=hashlib.sha256(raw).hexdigest())
    store=dict(container_path='/operational/entries',source='/srv/new-entries',device=1,inode=44,files={'entry':'a'*64})
    data=dict(historical_before={'/srv/history/file':'b'*64},historical_after={'/srv/history/file':'b'*64},
        operational_before=[store],operational_after=[copy.deepcopy(store)],
        old_mounts=[dict(source='/srv/history',target='/history',read_only=True)])
    if fault=='history':data['historical_after']['/srv/history/file']='c'*64
    if fault=='new-data':data['operational_after'][0]['files']['entry']='c'*64
    if fault=='removed-directory':data['operational_after']=[]
    if fault=='wrong-source':data['operational_after'][0]['source']='/srv/replaced'
    if fault=='old-writer':data['old_mounts'].append(dict(source='/srv',target='/all',read_only=False))
    if fault=='missing-surface':data['operational_before']=data['operational_after']=[]
    if fault=='missing-mounts':data['old_mounts']=[]
    if fault=='invalid-hash':data['historical_before']=data['historical_after']={'/srv/history/file':'bad'}
    if fault=='empty-history':data['historical_before']=data['historical_after']={}
    observations=dict(consumer=dict(raw=ref('http',dict(url='http://127.0.0.1:18502/',status_code=503 if fault=='http' else 200))),
        data_unchanged=dict(raw=ref('data',data)))
    host=SimpleNamespace(_json=json.loads,_protected_path=lambda p,**kw:p)
    roots=[dict(container_path='/operational/entries')]
    if fault:
        with pytest.raises(runtime.PreReleaseError):runtime.verify_targeted_preservation(observations,host,roots)
    else:runtime.verify_targeted_preservation(observations,host,roots)

@pytest.mark.parametrize('body',[
    'def hidden(Path):\n    p=Path("/operational/entries/new.txt")\n    return p.read_text()\n',
    'def hidden(x: destroy()):\n    return x\n',
    'def hidden():\n    global Path\n    Path=destroy\n',
    'def hidden():\n    p=Path("/history/old.txt")\n    p.write_text("lost")\n',
])
def test_additive_proof_rejects_shadowed_or_side_effecting_python(additive_repo,body):
    repo,git,put,base,contract,compose,code=additive_repo
    put('05_apps/page.py',code+body)
    assert additive_report(additive_repo)['STATE_CHANGE_CLASS']=='NEEDS_MAINTAINER_RISK_REVIEW'


def review_for(risk, decision='ADDITIVE_REVERSIBLE'):
    module=runtime._load('04_scripts/runtime/release_reversibility.py','_review_test')
    return dict(reviewer='fixture Maintainer',timestamp=datetime.now(timezone.utc).isoformat(),
        base=risk['base'],target=risk['target'],authoritative_main=risk['base'],
        machine_findings=module.machine_findings(risk),maintainer_classification=decision,
        reason='Reviewed code, tests, independent storage paths, no migration, retained new data and old configuration rollback.')


def test_unsupported_business_semantics_allows_bound_maintainer_review(additive_repo):
    repo,git,put,base,contract,compose,code=additive_repo
    put('05_apps/page.py',code+'def custom():\n    custom_read_only_adapter()\n')
    r=additive_report(additive_repo)
    assert r['MACHINE_DESTRUCTIVE_EVIDENCE'] is False
    assert r['MACHINE_STATE_CHANGE_CLASS']=='NEEDS_MAINTAINER_RISK_REVIEW'
    assert 'MAINTAINER_RISK_REVIEW_REQUIRED' in _risk_gate(risk=r)['failure_codes']
    review=review_for(r)
    accepted=runtime.apply_maintainer_review(r,review,r['base'])
    assert accepted['MACHINE_STATE_CHANGE_CLASS']=='NEEDS_MAINTAINER_RISK_REVIEW'
    assert accepted['STATE_CHANGE_CLASS']=='ADDITIVE_REVERSIBLE'
    assert accepted['FULL_ROLLBACK_REHEARSAL_REQUIRED'] is False
    assert accepted['TARGETED_RECOVERY_VALIDATION_REQUIRED'] is False
    assert _risk_gate(risk=accepted)['PRODUCTION_RELEASE_PREFLIGHT']=='PASS'
    assert accepted['maintainer_risk_review']==review


@pytest.mark.parametrize('mutation',['base-commit','base-tree','target-commit','target-tree','main-commit',
    'main-tree','findings','reviewer','reason','future','unknown-field'])
def test_maintainer_risk_review_invalidated_by_identity_or_findings_change(additive_repo,mutation):
    import copy
    r=additive_report(additive_repo);review=copy.deepcopy(review_for(r));main=copy.deepcopy(r['base'])
    if mutation.startswith('main-'):main[mutation.split('-')[1]]='d'*40
    elif mutation.startswith(('base-','target-')):
        key,field=mutation.split('-');review[key][field]='d'*40
    elif mutation=='findings':review['machine_findings']['findings']=[]
    elif mutation in ('reviewer','reason'):review[mutation]=' '
    elif mutation=='future':review['timestamp']='2999-01-01T00:00:00+00:00'
    else:review['low_risk']=True
    with pytest.raises(ValueError):runtime.apply_maintainer_review(r,review,main)


@pytest.mark.parametrize('operation',['schema-migration','delete-history','rewrite-history','one-way-conversion','direct-delete','direct-rewrite'])
def test_machine_destructive_evidence_cannot_be_reviewed_away(risk_repo,operation):
    repo,git,put,_=risk_repo
    # Existing persistent bytes, not a new file mislabelled as a rewrite.
    put('01_data/history.parquet','old history')
    contract=dict(service_id='app',entrypoint=['python','05_apps/page.py'],
        runtime_roots=[dict(role='history',container_path='/history',access='ro')],
        build=dict(dockerfile='Dockerfile',compose_sources=['compose.yml']))
    put('02_configs/runtime.json',json.dumps(contract));git('add','.');git('commit','-qm','old history')
    base=git('rev-parse','HEAD')
    if operation=='schema-migration':put('migrations/001.sql','ALTER TABLE quotes ADD value TEXT;')
    elif operation=='delete-history':(repo/'01_data/history.parquet').unlink()
    elif operation=='rewrite-history':put('01_data/history.parquet','overwritten')
    elif operation=='one-way-conversion':put('migrations/002.sql','ALTER TABLE quotes ALTER COLUMN value TYPE INTEGER;')
    else:
        call='unlink()' if operation=='direct-delete' else 'write_text("replacement")'
        put('05_apps/page.py','from pathlib import Path\ndef mutate():\n    Path("/history/old.txt").'+call+'\n')
    git('add','-A');git('commit','-qm','destructive')
    r=runtime.classify_release(repo,base,git('rev-parse','HEAD'),'example')
    assert r['MACHINE_DESTRUCTIVE_EVIDENCE'] is True,r
    assert r['DESTRUCTIVE_FINDINGS']
    with pytest.raises(ValueError,match='CANNOT_BE_DOWNGRADED'):
        runtime.apply_maintainer_review(r,review_for(r),r['base'])
    accepted=runtime.apply_maintainer_review(r,review_for(r,'HIGH_RISK'),r['base'])
    assert accepted['STATE_CHANGE_CLASS']=='IRREVERSIBLE_OR_DESTRUCTIVE'
    assert accepted['FULL_ROLLBACK_REHEARSAL_REQUIRED'] is True
    # Even a malformed trusted aggregation input cannot suppress machine findings.
    accepted['STATE_CHANGE_CLASS']='ADDITIVE_REVERSIBLE'
    result=_risk_gate(risk=accepted,rehearsal_validated=True,targeted_recovery_validated=True)
    assert result['PRODUCTION_RELEASE_PREFLIGHT']=='FAIL'


def test_maintainer_high_risk_retains_full_recovery(additive_repo):
    r=additive_report(additive_repo)
    reviewed=runtime.apply_maintainer_review(r,review_for(r,'HIGH_RISK'),r['base'])
    assert reviewed['FULL_ROLLBACK_REHEARSAL_REQUIRED'] is True
    assert _risk_gate(risk=reviewed,targeted_recovery_validated=True)['PRODUCTION_RELEASE_PREFLIGHT']=='FAIL'
    assert _risk_gate(risk=reviewed,rehearsal_validated=True)['PRODUCTION_RELEASE_PREFLIGHT']=='PASS'


def test_routine_requires_no_maintainer_risk_review(risk_repo):
    repo,git,put,base=risk_repo
    r=runtime.classify_release(repo,base,base,'example')
    assert runtime.apply_maintainer_review(r,None,r['base'])==r
    assert r['MACHINE_DESTRUCTIVE_EVIDENCE'] is False
    assert not r['FULL_ROLLBACK_REHEARSAL_REQUIRED']
    assert not r['TARGETED_RECOVERY_VALIDATION_REQUIRED']


def test_review_cli_is_not_an_execution_override(tmp_path):
    with pytest.raises(SystemExit):runtime.main(['--maintainer-risk-review',str(tmp_path/'review')])
    with pytest.raises(SystemExit):runtime.main(['--project','example','--maintainer-risk-review',str(tmp_path/'review')])

@pytest.mark.parametrize('fault',[None,'main-moved','target-mismatch','destructive','no-review','no-recovery'])
def test_release_assessment_consumes_plain_review_and_targeted_evidence(rollback_assets,monkeypatch,tmp_path,fault):
    import copy
    repo,current,host,image=rollback_assets
    identity=dict(commit=current['commit'],tree=current['tree'])
    module=runtime._load('04_scripts/runtime/release_reversibility.py','_assessment_review')
    risk=dict(base=identity,target=identity,RELEASE_RISK_CLASS='STATEFUL_OR_INFRA',
        STATE_CHANGE_CLASS='NEEDS_MAINTAINER_RISK_REVIEW',MACHINE_STATE_CHANGE_CLASS='NEEDS_MAINTAINER_RISK_REVIEW',
        MACHINE_DESTRUCTIVE_EVIDENCE=False,DESTRUCTIVE_FINDINGS=[],findings=[dict(path='example.py',reason='unsupported')],
        REVERSIBILITY_EVIDENCE=dict(new_roots=[dict(container_path='/operational/new')],failure_codes=['UNSUPPORTED']))
    review=copy.deepcopy(review_for(risk))
    if fault=='target-mismatch':review['target']['commit']='d'*40
    if fault=='destructive':risk.update(MACHINE_DESTRUCTIVE_EVIDENCE=True,MACHINE_STATE_CHANGE_CLASS='IRREVERSIBLE_OR_DESTRUCTIVE',STATE_CHANGE_CLASS='IRREVERSIBLE_OR_DESTRUCTIVE')
    def ref(name,value):
        raw=json.dumps(value).encode();p=tmp_path/name;p.write_bytes(raw)
        return dict(path=str(p),sha256=hashlib.sha256(raw).hexdigest())
    state=dict(base=identity,target=identity,data_schema_sha256=current['data_schema']['sha256'],
        irreversible='NO',compatible=True,database_migration=False,production_data_mutation=False,storage_format_change=False)
    binding=dict(identity,project_id='example')
    candidate=dict(binding=binding,image_id=image['Id'])
    recovery=dict(schema_version='production-recovery-observation/1',base=identity,target=identity,old_image_id=image['Id'],
        runtime_config_sha256=current['runtime_config']['sha256'],data_schema_sha256=current['data_schema']['sha256'],
        method='targeted-recovery',observed_at=datetime.now(timezone.utc).isoformat(),result='PASS',
        evidence={k:ref(k,{}) for k in ('instance','grant','preflight','health','consumer','data_unchanged')})
    request=dict(schema_version='production-release-request/1',project_id='example',current=current,previous=current,
        target_commit=identity['commit'],target_source_root=str(repo),candidate_record=ref('candidate',candidate),
        state_plan=ref('state',state),acceptance_plan=ref('acceptance',dict(target=identity,image_id=image['Id'],checks=['manual UI'])),
        recovery_evidence=ref('recovery',recovery),maintainer_risk_review=review)
    if fault=='no-review':request.pop('maintainer_risk_review');request['recovery_evidence']=None
    if fault=='no-recovery':request['recovery_evidence']=None
    p=tmp_path/'request.json';p.write_text(json.dumps(request))
    host.require_protected_authority_source=lambda:None
    engine=SimpleNamespace(_project=lambda *a:{'runtime_contract':'contract'},source_contract=lambda *a:(None,{},binding))
    records=SimpleNamespace(verify_record=lambda raw,trust:{'evidence':json.loads(raw)})
    monkeypatch.setattr(runtime,'ROOT',repo);(repo/runtime.TRUST).write_text('{}')
    monkeypatch.setattr(runtime,'_load',lambda path,name:{runtime.HOST:host,runtime.ENGINE:engine,runtime.RECORD:records,
        '04_scripts/runtime/release_reversibility.py':module}[path])
    monkeypatch.setattr(runtime,'require_source',lambda *a,**k:(identity['commit'],identity['tree']))
    monkeypatch.setattr(runtime,'classify_release',lambda *a:copy.deepcopy(risk))
    calls=[]
    def main(_):
        calls.append(True)
        return dict(identity,commit='e'*40) if fault=='main-moved' and len(calls)>1 else identity
    monkeypatch.setattr(runtime,'current_main_identity',main)
    observed=[]
    def verify(recovery,host,*,targeted_roots=None):
        observed.append(targeted_roots)
        assert targeted_roots==[dict(container_path='/operational/new')]
    monkeypatch.setattr(runtime,'verify_recovery_observation',verify)
    monkeypatch.setattr(runtime,'_write_new',lambda h,p,raw:p.write_bytes(raw))
    output=tmp_path/'assessment.json'
    if fault in ('main-moved','target-mismatch','destructive'):
        with pytest.raises(runtime.PreReleaseError):runtime.assess_release(p,output)
        assert not output.exists()
    else:
        r=runtime.assess_release(p,output)
        assert r['PRODUCTION_RELEASE_PREFLIGHT']==('FAIL' if fault=='no-review' else 'PASS')
        assert r['production_authorized'] is False
        if fault in (None,'no-recovery'):
            assert len(observed)==(0 if fault=='no-recovery' else 2)
            assert r['MAINTAINER_STATE_CHANGE_CLASS']=='ADDITIVE_REVERSIBLE'
            assert r['MACHINE_STATE_CHANGE_CLASS']=='NEEDS_MAINTAINER_RISK_REVIEW'
            assert not r['FULL_ROLLBACK_REHEARSAL_REQUIRED']
            assert not r['TARGETED_RECOVERY_VALIDATION_REQUIRED']
        else:assert not calls and not observed


def test_risk_review_json_rejects_duplicate_decisions():
    with pytest.raises(runtime.PreReleaseError,match='duplicate'):
        runtime.read_maintainer_review(b'{"maintainer_classification":"HIGH_RISK","maintainer_classification":"ADDITIVE_REVERSIBLE"}')
