from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "04_scripts/runtime/validate_target_runtime.py"
BASE_IMAGE = "python:3.12-slim@sha256:" + "a" * 64


def test_spread_source_contract_preserves_full_entrypoint_and_readonly_page_policy():
    engine = load_engine()
    parser = engine._load(ROOT / engine._MANIFEST_PARSER, "spread_source_manifest_test")
    path = ROOT / "02_configs/runtime_contracts/spread-production-runtime.json"
    contract = parser.load_runtime_manifest(path).to_dict()
    assert contract["project_id"] == "spread-production-runtime-wiring"
    assert contract["runtime_target"] == "production_container"
    assert contract["entrypoint"] == ["streamlit", "run", "05_apps/streamlit_app.py",
                                      "--server.address=0.0.0.0", "--server.port=8501"]
    assert contract["service_id"] == "spread-dashboard"
    assert contract["identity_kind"] == "oci_container"
    environment = engine._candidate_environment(contract)
    assert environment["IMPORT_PROFIT_INTRADAY_PAGE_MODE"] == "STRICT_RUNTIME"
    assert environment["IMPORT_PROFIT_INTRADAY_ENVIRONMENT"] == "FORMAL"
    assert environment["IMPORT_PROFIT_INTRADAY_ALLOW_CNF_SAVE"] == "1"
    assert environment["IMPORT_PROFIT_INTRADAY_CNF_STORE_PATH"] == "/runtime/import-profit/operational/cnf/manual_cnf_quotes.parquet"
    assert environment["IMPORT_PROFIT_INTRADAY_AM_RESULT_ROOT"] == "/runtime/import-profit/operational/am-results"
    roles = {item['role']: item['access'] for item in contract['runtime_roots']}
    assert all(roles[role] == 'ro' for role in ('history','cnf','results','snapshots','data','weather'))
    assert roles['manual-cnf'] == roles['am-results'] == 'rw'
    assert {"IMPORT_PROFIT_INTRADAY_BUSINESS_DATE",
            "IMPORT_PROFIT_INTRADAY_WRITE_RUNTIME_ROOT", "IMPORT_PROFIT_INTRADAY_WRITE_MODE"
            } <= set(contract["forbidden_environment"])
    inputs = {item["path"] for item in contract["source_inputs"]}
    assert {"05_apps/streamlit_app.py", "05_apps/import_profit_intraday_runtime_page.py",
            "05_apps/weather_research_page.py", "05_apps/basis_page.py",
            "02_configs/historical_spread_config.xlsx", ".streamlit/config.toml"} <= inputs
    build = contract["build"]
    tracked = set(subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0"))
    for name in inputs | {build["dockerfile"], build["dockerignore"],
                          *build["dependency_contracts"], *build["compose_sources"]}:
        assert (ROOT / name).is_file() and not (ROOT / name).is_symlink()
        assert name in tracked


def load_engine():
    spec = importlib.util.spec_from_file_location("target_runtime_validator_under_test", ENGINE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_candidate_secret_abi_keeps_current_declared_credentials(monkeypatch):
    engine = load_engine()
    declarations = {"service": "/run/secrets/market-data-service.json"}
    host = SimpleNamespace(declared_secret_targets=lambda contract, rendered: declarations)
    monkeypatch.setattr(engine, "_load", lambda *a: pytest.fail("current ABI must not load a legacy validator"))
    assert engine._candidate_secret_declarations(ROOT, host, {}, {}, existing_image_id=None) == declarations


def test_retained_provider_only_image_reuses_original_compose_validation(tmp_path, monkeypatch):
    engine = load_engine()
    seen = []
    contract, rendered = {"old": "contract"}, {"old": "compose"}
    monkeypatch.setattr(engine, "_interpret", lambda *a: {"tankan": "/run/secrets/tankan.env"})
    monkeypatch.setattr(engine, "_exact_source", lambda root, path: root / path)
    def original(root, actual):
        assert root == tmp_path and actual is contract
        seen.append(actual)
    monkeypatch.setattr(engine, "_load", lambda *a: SimpleNamespace(validate_source_compose=original))
    assert engine._candidate_secret_declarations(tmp_path, SimpleNamespace(), contract, rendered,
        existing_image_id="sha256:" + "a" * 64) == {}
    assert seen == [contract]


@pytest.mark.parametrize("image,declarations", [
    (None, {"tankan": "/run/secrets/tankan.env"}),
    ("sha256:" + "a" * 64, {"service": "/run/secrets/market-data-service.json"}),
    ("sha256:" + "a" * 64, {"unknown": "/run/secrets/unknown.json"}),
    ("sha256:" + "a" * 64, {"one": "/run/secrets/tankan.env", "two": "/run/secrets/tankan.env"}),
])
def test_legacy_candidate_abi_cannot_drop_new_credentials_or_enable_build(monkeypatch, image, declarations):
    engine = load_engine()
    monkeypatch.setattr(engine, "_interpret", lambda *a: declarations)
    monkeypatch.setattr(engine, "_load", lambda *a: pytest.fail("invalid legacy ABI must stop first"))
    with pytest.raises(engine.ValidationError, match="existing provider-only image"):
        engine._candidate_secret_declarations(ROOT, SimpleNamespace(), {}, {}, existing_image_id=image)


def test_retained_candidate_abi_preserves_original_compose_rejection(monkeypatch):
    engine = load_engine()
    monkeypatch.setattr(engine, "_interpret", lambda *a: {})
    monkeypatch.setattr(engine, "_exact_source", lambda root, path: root / path)
    def original(*a):
        raise RuntimeError("original compose rejected")
    monkeypatch.setattr(engine, "_load", lambda *a: SimpleNamespace(validate_source_compose=original))
    with pytest.raises(RuntimeError, match="original compose rejected"):
        engine._candidate_secret_declarations(ROOT, SimpleNamespace(), {}, {},
            existing_image_id="sha256:" + "a" * 64)


def test_candidate_grant_omits_absent_optional_trust_for_original_issuer():
    engine = load_engine()
    seen = []
    def original(container_id, *, role, key_path):
        seen.append((container_id, role, key_path))
        return "issued"
    host = SimpleNamespace(issue_execution_grant=original)
    assert engine._issue_candidate_grant(host, "created", external_trust=None,
        role="candidate_validation", key_path="fixed-key") == "issued"
    assert seen == [("created", "candidate_validation", "fixed-key")]
    with pytest.raises(TypeError, match="external_candidate_trust_path"):
        engine._issue_candidate_grant(host, "created", external_trust="supplied",
            role="candidate_validation", key_path="fixed-key")
    assert len(seen) == 1


def test_candidate_grant_never_discards_supplied_external_trust():
    engine = load_engine()
    seen = []
    host = SimpleNamespace(issue_execution_grant=lambda cid, **kw: seen.append((cid, kw)))
    engine._issue_candidate_grant(host, "created", external_trust="bound-public-trust",
        role="candidate_validation")
    assert seen == [("created", {"role": "candidate_validation",
        "external_candidate_trust_path": "bound-public-trust"})]


def test_current_image_keeps_all_declared_lifecycle_probes():
    engine = load_engine()
    contract = json.loads((ROOT / "02_configs/runtime_contracts/spread-production-runtime.json").read_text())
    assert engine._lifecycle_probe_names(contract) == (
        "lifecycle", "lifecycle_events", "lifecycle_reconciler", "lifecycle_store")


def test_pre_lifecycle_image_uses_its_own_source_contract():
    engine = load_engine()
    assert engine._lifecycle_probe_names({"source_inputs": [
        {"path": "03_src/agri_research_agent/import_profit/soybean.py"}]}) == ()


def test_declared_lifecycle_module_cannot_be_omitted_from_probes():
    engine = load_engine()
    assert engine._lifecycle_probe_names({"source_inputs": [
        {"path": "03_src/agri_research_agent/import_profit/lifecycle_store.py"}]}) == ("lifecycle_store",)


@pytest.mark.parametrize('command', [('docker', 'build', '.'), ('docker', 'buildx', 'build', '.')])
def test_existing_image_transport_rejects_build_before_spawning_process(monkeypatch, command):
    engine = load_engine()
    engine._BUILD_ALLOWED = False
    calls = []
    monkeypatch.setattr(engine.subprocess, 'run', lambda *a, **kw: calls.append(a))
    with pytest.raises(engine.ValidationError, match='must never invoke build'):
        engine._run(command)
    assert not calls and engine._BUILD_INVOCATIONS == 1


@pytest.mark.parametrize("mutation", [None, "config", "dependency", "source_changed"])
def test_image_bound_inputs_verify_non_python_copied_bytes(tmp_path, monkeypatch, mutation):
    engine = load_engine()
    files = {"config.json": b'{"enabled":true}', "requirements.txt": b"example==1\n"}
    dockerfile = ("FROM " + BASE_IMAGE + "\n"
                  'COPY ["config.json", "/app/config.json"]\n'
                  "COPY requirements.txt /app/\n")
    (tmp_path / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    for name, raw in files.items():
        (tmp_path / name).write_bytes(raw)
    contract = {"build": {"dockerfile": "Dockerfile", "dockerignore": ".dockerignore",
                           "compose_sources": []}}
    binding = {"source_sha256": {name: engine._sha(raw) for name, raw in files.items()}}
    observed = {"/app/" + name: raw for name, raw in files.items()}
    if mutation == "config":
        observed["/app/config.json"] = b'{"enabled":false}'
    elif mutation == "dependency":
        observed["/app/requirements.txt"] = b"example==2\n"
    elif mutation == "source_changed":
        (tmp_path / "config.json").write_bytes(b"changed")
    reads = []

    def copy(container, path):
        assert container == "created-unstarted-candidate"
        reads.append(path)
        return observed[path]

    monkeypatch.setattr(engine, "_copy_bytes", copy)
    if mutation:
        with pytest.raises(engine.ValidationError, match="bound source|source changed"):
            engine._image_bound_inputs(tmp_path, contract, binding, "created-unstarted-candidate")
    else:
        engine._image_bound_inputs(tmp_path, contract, binding, "created-unstarted-candidate")
        assert set(reads) == set(observed)


def test_ephemeral_candidate_trust_is_public_only_and_not_mounted(tmp_path):
    engine = load_engine()
    contract = {"required_environment": [], "schema_version": "runtime-manifest/2"}
    engine._ephemeral_candidate_identity(tmp_path, contract)
    private = contract["_ephemeral_candidate_private_key"]
    grants = tmp_path / "grants"
    grants.mkdir()
    public = engine._install_candidate_public_trust(grants, contract)
    from agri_research_agent.shared.production_grant import parse_external_candidate_trust
    key_id, raw = parse_external_candidate_trust(public.read_bytes())
    assert key_id == contract["_ephemeral_candidate_key_id"]
    assert hashlib.sha256(raw).hexdigest() == contract["_ephemeral_candidate_public_fingerprint"]
    assert engine._candidate_signing_key(contract, ROOT) == private
    assert "PRIVATE KEY" in private.read_text(encoding="ascii")
    assert "PRIVATE KEY" not in public.read_text(encoding="utf-8")
    assert engine._candidate_environment(contract)["MARKET_DATA_CANDIDATE_EXTERNAL_TRUST"] == "1"
    assert str(private) not in json.dumps(engine._candidate_environment(contract))


def test_final_image_import_closure_detects_missing_module(monkeypatch):
    engine = load_engine()
    contract = json.loads((ROOT / "02_configs/runtime_contracts/spread-production-runtime.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(engine, "_copy_bytes", lambda container, path: (ROOT / path.removeprefix("/app/")).read_bytes())
    complete = engine._image_import_closure(ROOT, contract, "test-container")
    assert complete["required_module_count"] > 0
    assert complete["missing_from_final_image"] == []
    original = engine._copy_bytes
    def missing(container, path):
        if path.endswith("/soybean_margin/store.py"):
            raise engine.ValidationError("module absent")
        return original(container, path)
    monkeypatch.setattr(engine, "_copy_bytes", missing)
    with pytest.raises(engine.ValidationError, match="final image runtime import closure"):
        engine._image_import_closure(ROOT, contract, "test-container")


def test_existing_admission_routes_spread_runtime_changes_to_real_docker():
    path = ROOT / "04_scripts/quality/platform_ci.py"
    spec = importlib.util.spec_from_file_location("platform_ci_spread_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def report(changed):
        return {"changed_paths": [{"path": name} for name in changed]}
    assert module.requires_spread_runtime_docker(report(["03_src/agri_research_agent/shared/production_identity.py"]), ROOT)
    assert module.requires_spread_runtime_docker(report(["09_deploy/spread_runtime/Dockerfile.spread-runtime"]), ROOT)
    for source in ("04_scripts/runtime/routine_release.py",
                   "09_deploy/spread_release/high_risk_execution.py",
                   "08_tests/shared/high_risk_execution_docker_e2e.py",
                   "08_tests/test_release_stabilize.py"):
        assert module.requires_spread_runtime_docker(report([source]), ROOT)
    assert not module.requires_spread_runtime_docker(report(["07_docs/unrelated.md"]), ROOT)


def load_ci_router():
    spec = importlib.util.spec_from_file_location('platform_ci_depth_test', ROOT / '04_scripts/quality/platform_ci.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('path', ['04_scripts/runtime/说明.md', '09_deploy/runtime_identity/说明.md'])
def test_ci_tooling_documentation_uses_both_committed_runtime_inventories(monkeypatch, path):
    module = load_ci_router()
    calls = []
    raw = (ROOT / module.RUNTIME_CONTRACT).read_bytes()
    def blob(repo, commit, name):
        calls.append((commit, name))
        return raw
    monkeypatch.setattr(module.admission, 'blob', blob)
    report = {'changed_paths': [{'path': path}],
              'trusted_main': {'commit': 'base'}, 'candidate': {'commit': 'candidate'}}
    assert not module.requires_spread_release_e2e(report, ROOT)
    assert calls == [('base', module.RUNTIME_CONTRACT), ('candidate', module.RUNTIME_CONTRACT)]
    assert not module.requires_spread_runtime_docker(report, ROOT)
    report['changed_paths'].append({'path': '09_deploy/spread_release/high_risk_execution.py'})
    assert module.requires_spread_release_e2e(report, ROOT)


@pytest.mark.parametrize('change', ['base-packaged', 'candidate-packaged', 'missing-identity',
                                  'invalid-json', 'empty-inputs', 'invalid-input'])
def test_ci_markdown_suffix_cannot_hide_runtime_inputs_or_unknown_inventory(monkeypatch, change):
    module = load_ci_router()
    path = '09_deploy/runtime_identity/说明.md'
    manifest = json.loads((ROOT / module.RUNTIME_CONTRACT).read_text(encoding='utf-8'))
    def blob(repo, commit, name):
        import copy
        value = copy.deepcopy(manifest)
        if change == commit + '-packaged':
            value['source_inputs'].append({'path': path, 'role': 'resource'})
        elif change == 'invalid-json':
            return b'{'
        elif change == 'empty-inputs':
            value['source_inputs'] = []
        elif change == 'invalid-input':
            value['source_inputs'].append({'path': path, 'role': None})
        return json.dumps(value).encode()
    monkeypatch.setattr(module.admission, 'blob', blob)
    report = {'changed_paths': [{'path': path}],
              'trusted_main': {'commit': 'base'}, 'candidate': {'commit': 'candidate'}}
    if change == 'missing-identity':
        report.pop('trusted_main')
    assert module.requires_spread_release_e2e(report, ROOT)


@pytest.mark.parametrize('path,expected', [
    ('05_apps/soybean_margin_page.py', False),
    ('05_apps/soybean_margin_assets/tables.css', False),
    ('03_src/agri_research_agent/soybean_margin/model.py', False),
    ('03_src/agri_research_agent/soybean_margin/store.py', True),
    ('03_src/agri_research_agent/soybean_margin/runtime.py', True),
    ('03_src/agri_research_agent/import_profit/operational_runtime.py', True),
    ('07_docs/04_开发与发布检查清单.md', False),
    ('09_deploy/spread_release/high_risk_execution.py', True),
    ('04_scripts/runtime/routine_release.py', True),
    ('04_scripts/quality/platform_ci.py', True),
    ('04_scripts/quality/main_admission.py', True),
    ('03_src/agri_research_agent/shared/production_identity.py', True),
    ('08_tests/shared/high_risk_execution_docker_e2e.py', True),
    ('08_tests/test_release_stabilize.py', True),
    ('.github/workflows/trusted-main-admission.yml', True),
    ('Dockerfile', True), ('requirements.txt', True), ('requirements-dev.in', True),
    ('02_configs/production_runtime_trust.json', True),
    ('02_configs/runtime_contracts/public-intraday-runtime.json', True),
])
def test_ci_image_and_lifecycle_depth_follow_changed_behavior(path, expected):
    module = load_ci_router()
    report = {'changed_paths': [{'path': path}]}
    assert module.requires_spread_release_e2e(report, ROOT) is expected
    if path.startswith(('05_apps/', '03_src/agri_research_agent/soybean_margin/')):
        assert module.requires_spread_runtime_docker(report, ROOT)


@pytest.mark.parametrize('change,expected', [
    ('formatting-only', False), ('write-permission', True), ('entrypoint', True),
    ('input-added', True), ('input-removed', True), ('invalid-input', True),
    ('missing-contract', True), ('invalid-json', True),
])
def test_ci_runtime_contract_formatting_cannot_hide_permission_or_path_changes(monkeypatch, change, expected):
    import copy
    module = load_ci_router()
    before = json.loads((ROOT / module.RUNTIME_CONTRACT).read_text(encoding='utf-8'))
    after = copy.deepcopy(before)
    if change == 'write-permission':
        after['runtime_roots'][0]['access'] = 'rw'
    elif change == 'entrypoint':
        after['entrypoint'].append('--new-argument')
    elif change == 'input-added':
        after['source_inputs'].append({'path': 'new.py', 'sha256': 'a' * 64})
    elif change == 'input-removed':
        after['source_inputs'].pop()
    elif change == 'invalid-input':
        after['source_inputs'][0]['access'] = 'rw'
    def blob(repo, commit, path):
        assert path == module.RUNTIME_CONTRACT
        if commit == 'candidate' and change == 'missing-contract':
            raise subprocess.CalledProcessError(128, ['git', 'show'])
        if commit == 'candidate' and change == 'invalid-json':
            return b'{'
        return json.dumps(before if commit == 'base' else after, indent=None if commit == 'base' else 2).encode()
    monkeypatch.setattr(module.admission, 'blob', blob)
    report = {'changed_paths': [{'path': module.RUNTIME_CONTRACT}],
              'trusted_main': {'commit': 'base'}, 'candidate': {'commit': 'candidate'}}
    assert module.requires_spread_release_e2e(report, ROOT) is expected
    # Formatting-only changes still require a real application image check.
    assert module.requires_spread_runtime_docker(report, ROOT)


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


def v3_contract():
    contract = v2_contract()
    contract.update({
        "schema_version": "runtime-manifest/3",
        "required_environment": ["DEMO_MODE", "RUNTIME_PATH", "DEPLOYMENT", "MARKET_DATA_EXECUTION_GRANT"],
        "environment_bindings": [
            {"name": "DEMO_MODE", "kind": "literal", "value": "candidate"},
            {"name": "RUNTIME_PATH", "kind": "runtime_path", "role": "state", "relative_path": "input.txt"},
            {"name": "DEPLOYMENT", "kind": "deployment", "candidate_value": "candidate-value", "production_value": "production-value"},
            {"name": "MARKET_DATA_EXECUTION_GRANT", "kind": "execution_grant"},
        ],
    })
    return contract


def test_v3_candidate_environment_is_typed_and_fixed_bindings_are_exact():
    engine = load_engine()
    value = engine._candidate_environment(v3_contract())
    assert all(type(item) is str and item for item in value.values())
    assert value == {
        "DEMO_MODE": "candidate", "RUNTIME_PATH": "/runtime/state/input.txt",
        "DEPLOYMENT": "candidate-value", "MARKET_DATA_EXECUTION_GRANT": "/run/market-data-grants/grant.json",
    }
    broken = v3_contract()
    broken["environment_bindings"][0]["kind"] = "unknown"
    with pytest.raises(engine.ValidationError, match="unknown environment"):
        engine._candidate_environment(broken)


def test_v3_source_compose_rejects_fixed_environment_binding_mismatch(monkeypatch, tmp_path):
    engine = load_engine()
    contract = v3_contract()
    contract["build"] = {"compose_sources": ["compose.yml"], "dockerfile": "Dockerfile"}
    service = {"image": "demo@sha256:" + "d" * 64, "build": {"context": str(tmp_path.resolve()), "dockerfile": "Dockerfile"},
               "entrypoint": contract["entrypoint"], "working_dir": "/app",
               "environment": engine._candidate_environment(contract),
               "volumes": [{"type": "bind", "source": "${IDENTITY}", "target": "/runtime", "read_only": True},
                           {"type": "bind", "source": "${STATE}", "target": "/runtime/state", "read_only": False},
                           {"type": "bind", "source": "${GRANTS}", "target": "/run/market-data-grants", "read_only": True}]}
    service["environment"]["DEMO_MODE"] = "forged"
    rendered = {"services": {"demo": service}}
    monkeypatch.setattr(engine, "_docker", lambda *args: SimpleNamespace(stdout=json.dumps(rendered).encode()))
    (tmp_path / "Dockerfile").write_text("FROM " + BASE_IMAGE + "\n", encoding="utf-8")
    with pytest.raises(engine.ValidationError, match="fixed environment"):
        engine.validate_source_compose(tmp_path, contract)


def test_v3_fixture_is_removed_from_context_and_cannot_be_copied_into_image(tmp_path):
    engine = load_engine()
    raw = b"fixture bytes\n"
    fixture = tmp_path / "fixtures" / "input.txt"
    fixture.parent.mkdir()
    fixture.write_bytes(raw)
    contract = {"schema_version": "runtime-manifest/3",
                "candidate_runtime_inputs": [{"source_path": "fixtures/input.txt", "relative_path": "input.txt",
                                                "role": "state", "sha256": hashlib.sha256(raw).hexdigest()}],
                "build": {"dockerfile": "Dockerfile", "dockerignore": ".dockerignore",
                           "compose_sources": [], "dependency_contracts": []}}
    (tmp_path / "Dockerfile").write_text("FROM " + BASE_IMAGE + "\nCOPY fixtures/input.txt /app/input.txt\n", encoding="utf-8")
    with pytest.raises(engine.ValidationError, match="COPY source|runtime input is missing"):
        engine.validate_dockerfile_inputs(tmp_path, contract,
                                          {"source_sha256": {"fixtures/input.txt": hashlib.sha256(raw).hexdigest()}})
    engine._exclude_candidate_inputs(tmp_path, contract)
    assert not fixture.exists()


def test_v3_fixture_seeding_uses_committed_bytes_hash_readonly_and_new_only(monkeypatch, tmp_path):
    engine = load_engine()
    raw = b"canonical committed fixture\n"
    digest = hashlib.sha256(raw).hexdigest()
    contract = {"schema_version": "runtime-manifest/3", "identity_root_role": "identity",
                "runtime_roots": [{"role": "identity", "container_path": "/runtime", "access": "ro"},
                                   {"role": "state", "container_path": "/runtime/state", "access": "ro"}],
                "candidate_runtime_inputs": [{"source_path": "fixtures/input.txt", "relative_path": "input.txt",
                                                "role": "state", "sha256": digest}]}
    candidate_root = tmp_path / "candidate"
    base = candidate_root / "state"
    base.mkdir(parents=True)
    scope = {"candidate_host_root": str(candidate_root),
             "mounts": [{"source": str(base), "target": "/runtime/state", "read_only": True}]}
    host = SimpleNamespace(_protected_path=lambda path, **kwargs: Path(path))
    def git_blob(_root, *args, **kwargs):
        assert args == ("show", "a" * 40 + ":fixtures/input.txt")
        assert kwargs == {"binary": True}
        return raw
    monkeypatch.setattr(engine, "_git", git_blob)
    binding_value = {"commit": "a" * 40, "source_sha256": {"fixtures/input.txt": digest}}
    engine._seed_candidate_runtime_inputs(tmp_path, contract, binding_value, scope, host)
    seeded = base / "input.txt"
    assert seeded.read_bytes() == raw
    assert hashlib.sha256(seeded.read_bytes()).hexdigest() == digest
    assert seeded.stat().st_mode & 0o777 == 0o444
    with pytest.raises(engine.ValidationError, match="new absolute"):
        engine._seed_candidate_runtime_inputs(tmp_path, contract, binding_value, scope, host)


@pytest.mark.parametrize("mutation", ["identity-root", "writable", "outside", "unsafe"])
def test_v3_fixture_seeding_rejects_unsafe_mount_or_target(mutation, tmp_path):
    engine = load_engine()
    raw = b"x"
    digest = hashlib.sha256(raw).hexdigest()
    contract = {"schema_version": "runtime-manifest/3", "identity_root_role": "identity",
                "runtime_roots": [{"role": "identity", "container_path": "/runtime", "access": "ro"},
                                   {"role": "state", "container_path": "/runtime/state", "access": "ro"}],
                "candidate_runtime_inputs": [{"source_path": "fixture", "relative_path": "../bad" if mutation == "unsafe" else "x",
                                                "role": "identity" if mutation == "identity-root" else "state", "sha256": digest}]}
    base = tmp_path / "candidate" / "state"; base.mkdir(parents=True)
    source = tmp_path / "outside"; source.mkdir()
    target = str(source if mutation == "outside" else base)
    scope = {"candidate_host_root": str(tmp_path / "candidate"),
             "mounts": [{"source": target, "target": "/runtime/state", "read_only": mutation != "writable"}]}
    host = SimpleNamespace(_protected_path=lambda path, **kwargs: Path(path))
    with pytest.raises(engine.ValidationError):
        engine._seed_candidate_runtime_inputs(tmp_path, contract,
            {"source_sha256": {"fixture": digest}}, scope, host)


def test_cli_exposes_only_gate_controlled_source_arguments(tmp_path):
    engine = load_engine()
    args = engine.parse_args(["--project", "demo", "--runtime-contract", "runtime.json",
                              "--evidence-output", str(tmp_path / "evidence.json")])
    assert vars(args) == {"project": "demo", "runtime_contract": "runtime.json",
                          "evidence_output": tmp_path / "evidence.json",
                            "ephemeral_candidate_trust": False,
                            "existing_image_id": None, "application_source_root": None}
    assert engine.parse_args(["--project", "demo", "--runtime-contract", "runtime.json",
                              "--evidence-output", str(tmp_path / "evidence.json"),
                              "--ephemeral-candidate-trust"]).ephemeral_candidate_trust is True
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
                           "dockerfile": "Dockerfile", "dockerignore": ".dockerignore",
                           "dependency_contracts": ["requirements.txt"]}, secret_references=[])
    rendered = {"services": {"demo": {
        "image": "demo@sha256:" + "d" * 64, "entrypoint": contract["entrypoint"],
        "build": {"context": str(tmp_path.resolve()), "dockerfile": "Dockerfile"},
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
    contract.update(build={"compose_sources": ["compose.yml"], "dockerfile": "Dockerfile",
                           "dockerignore": ".dockerignore", "dependency_contracts": ["requirements.txt"]},
                    secret_references=["tankan-secret"])
    rendered = {"secrets": {"tankan-secret": {"file": "${TANKAN_SECRET_FILE:?required}"}},
                "services": {"demo": {
        "image": "demo@sha256:" + "d" * 64, "entrypoint": contract["entrypoint"],
        "build": {"context": str(tmp_path.resolve()), "dockerfile": "Dockerfile"},
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

    rendered["services"]["demo"]["secrets"].append({"source": "undeclared-secret", "target": "/run/secrets/undeclared"})
    with pytest.raises(engine.ValidationError, match="secret declarations differ from runtime contract"):
        engine.validate_source_compose(tmp_path, contract)
    rendered["services"]["demo"]["secrets"] = [
        {"source": "tankan-secret", "target": "relative-secret"}]
    with pytest.raises(engine.ValidationError, match="secret declarations differ from runtime contract"):
        engine.validate_source_compose(tmp_path, contract)
    rendered["services"]["demo"]["secrets"][0]["target"] = "/app/RELEASE.json"
    with pytest.raises(engine.ValidationError, match="secret declarations differ from runtime contract"):
        engine.validate_source_compose(tmp_path, contract)
    rendered["services"]["demo"]["secrets"][0]["target"] = "/run/secrets/tankan.env"
    rendered["secrets"]["unused-secret"] = {"file": "${UNUSED_SECRET_FILE:?required}"}
    with pytest.raises(engine.ValidationError, match="secret declarations differ from runtime contract"):
        engine.validate_source_compose(tmp_path, contract)
    rendered["secrets"].pop("unused-secret")
    rendered["secrets"]["tankan-secret"] = {"external": True}
    with pytest.raises(engine.ValidationError, match="only declared file secrets are supported"):
        engine.validate_source_compose(tmp_path, contract)
    rendered["secrets"].clear()
    with pytest.raises(engine.ValidationError, match="secret declarations differ from runtime contract"):
        engine.validate_source_compose(tmp_path, contract)


def test_source_compose_requires_readonly_host_grant_injection(monkeypatch, tmp_path):
    engine = load_engine()
    contract = v2_contract()
    contract.update(build={"compose_sources": ["compose.yml"], "dockerfile": "Dockerfile",
                           "dockerignore": ".dockerignore", "dependency_contracts": ["requirements.txt"]},
                    secret_references=[])
    service = {
        "image": "demo@sha256:" + "d" * 64,
        "build": {"context": str(tmp_path.resolve()), "dockerfile": "Dockerfile"},
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
    with pytest.raises(engine.ValidationError, match="duplicate Compose mount target"):
        engine.validate_source_compose(tmp_path, contract)


@pytest.mark.parametrize("mutation", [
    "missing", "string", "parent", "child", "relative", "alias", "wrong-dockerfile",
    "dockerfile-inline", "additional-contexts", "args", "target", "network",
    "secrets", "ssh",
])
def test_source_compose_build_is_exact_candidate_root(monkeypatch, tmp_path, mutation):
    engine = load_engine()
    contract = v2_contract()
    contract.update(build={"compose_sources": ["compose.yml"], "dockerfile": "Dockerfile",
                           "dockerignore": ".dockerignore", "dependency_contracts": ["requirements.txt"]},
                    secret_references=[])
    build = {"context": str(tmp_path.resolve()), "dockerfile": "Dockerfile"}
    if mutation == "missing":
        build = None
    elif mutation == "string":
        build = str(tmp_path)
    elif mutation == "parent":
        build["context"] = str(tmp_path.parent.resolve())
    elif mutation == "child":
        child = tmp_path / "child"
        child.mkdir()
        build["context"] = str(child.resolve())
    elif mutation == "relative":
        build["context"] = "."
    elif mutation == "alias":
        build["context"] = str(tmp_path / "child" / "..")
    elif mutation == "wrong-dockerfile":
        build["dockerfile"] = "OtherDockerfile"
    else:
        key = mutation.replace("-", "_")
        build[key] = "unexpected"
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
    if build is not None:
        service["build"] = build
    from types import SimpleNamespace
    monkeypatch.setattr(engine, "_docker", lambda *args: SimpleNamespace(
        stdout=json.dumps({"services": {"demo": service}}).encode()))
    with pytest.raises(engine.ValidationError, match="build contract|build context|Dockerfile"):
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


def test_public_dockerfile_inputs_are_all_declared_and_bound(monkeypatch):
    engine = load_engine()
    parser = engine._load(ROOT / engine._MANIFEST_PARSER, "manifest_parser_for_dockerfile_test")
    contract = parser.load_runtime_manifest(
        ROOT / "02_configs/runtime_contracts/public-intraday-runtime.json").to_dict()
    names = ["02_configs/runtime_contracts/public-intraday-runtime.json",
             contract["build"]["dockerfile"], contract["build"]["dockerignore"],
             *contract["build"]["dependency_contracts"],
             *contract["build"]["compose_sources"],
             *(item["path"] for item in contract["source_inputs"]),
             engine._MANIFEST_PARSER, engine._MANIFEST_SCHEMA]
    binding = {"source_sha256": {
        name: engine._sha(engine._git(ROOT, "show", "HEAD:" + name, binary=True))
        for name in names}}
    engine.validate_dockerfile_inputs(ROOT, contract, binding)


@pytest.mark.parametrize("dockerfile,files,expected", [
    ("FROM python:3.12-slim\nCOPY app.py /app/app.py\n", {"app.py"}, True),
    ("FROM python:3.12-slim\nCOPY [\"app.py\", \"/app/app.py\"]\n", {"app.py"}, True),
    ("FROM python:3.12-slim\nCOPY app.py init.py /app/\n", {"app.py", "init.py"}, True),
    ("FROM python:3.12-slim\nCOPY app.py \\\n     init.py /app/\n", {"app.py", "init.py"}, True),
    ("FROM python:3.12-slim\nCOPY src /app/src\n", {"src"}, False),
    ("FROM python:3.12-slim\nCOPY *.py /app/\n", {"*.py"}, False),
    ("FROM python:3.12-slim\nCOPY $SOURCE /app/app.py\n", {"$SOURCE"}, False),
    ("FROM python:3.12-slim\nCOPY ./app.py /app/app.py\n", {"./app.py"}, False),
    ("FROM python:3.12-slim\nADD app.py /app/app.py\n", {"app.py"}, False),
    ("FROM python:3.12-slim\nONBUILD COPY app.py /app/app.py\n", {"app.py"}, False),
    ("# syntax=docker/dockerfile:1\nFROM python:3.12-slim\nCOPY app.py /app/app.py\n", {"app.py"}, False),
    ("# escape=\\\\\nFROM python:3.12-slim\nCOPY app.py /app/app.py\n", {"app.py"}, False),
    ("FROM python:3.12-slim\nRUN --mount=type=bind,target=/src true\nCOPY app.py /app/app.py\n", {"app.py"}, False),
    ("FROM builder\nCOPY --from=builder /out/app.py /app/app.py\n", {"app.py"}, False),
    ("FROM python:3.12-slim AS base\nFROM base\nCOPY app.py /app/app.py\n", {"app.py"}, False),
])
def test_validate_dockerfile_inputs_rejects_unbound_or_ambiguous_sources(
        tmp_path, dockerfile, files, expected):
    engine = load_engine()
    dockerfile = dockerfile.replace("FROM python:3.12-slim", "FROM " + BASE_IMAGE)
    dockerfile_path = tmp_path / "Dockerfile"
    dockerfile_path.write_text(dockerfile, encoding="utf-8")
    for name in files:
        if name == "src":
            (tmp_path / name).mkdir()
        elif name in {"app.py", "init.py"}:
            (tmp_path / name).write_text("fixture\n", encoding="utf-8")
    contract = {"build": {"dockerfile": "Dockerfile", "dockerignore": ".dockerignore",
                           "compose_sources": ["compose.yml"],
                           "dependency_contracts": ["requirements.txt"]}}
    binding = {"source_sha256": {
        name: __import__("hashlib").sha256((tmp_path / name).read_bytes()).hexdigest()
        for name in files if (tmp_path / name).is_file()}}
    if expected:
        engine.validate_dockerfile_inputs(tmp_path, contract, binding)
    else:
        with pytest.raises(engine.ValidationError):
            engine.validate_dockerfile_inputs(tmp_path, contract, binding)


def test_base_image_onbuild_metadata_is_rejected(monkeypatch):
    engine = load_engine()
    monkeypatch.setattr(engine, "_docker", lambda *args, **kwargs: type(
        "Result", (), {"stdout": ("sha256:" + "b" * 64).encode(), "stderr": b"", "returncode": 0})())
    monkeypatch.setattr(engine, "inspect_one", lambda kind, identity: {
        "Config": {"OnBuild": ["COPY hidden /app/hidden"]}})
    with pytest.raises(engine.ValidationError, match="ONBUILD"):
        engine.require_base_image(BASE_IMAGE)


@pytest.mark.parametrize("onbuild", [None, []])
def test_base_image_explicitly_allows_empty_onbuild_metadata(monkeypatch, onbuild):
    engine = load_engine()
    monkeypatch.setattr(engine, "_docker", lambda *args, **kwargs: type(
        "Result", (), {"stdout": ("sha256:" + "b" * 64).encode(), "stderr": b"", "returncode": 0})())
    monkeypatch.setattr(engine, "inspect_one", lambda kind, identity: {
        "Config": {"OnBuild": onbuild}})
    engine.require_base_image(BASE_IMAGE)


@pytest.mark.parametrize("config", [{}, None])
def test_base_image_metadata_must_be_observed(monkeypatch, config):
    engine = load_engine()
    monkeypatch.setattr(engine, "_docker", lambda *args, **kwargs: type(
        "Result", (), {"stdout": ("sha256:" + "b" * 64).encode(), "stderr": b"", "returncode": 0})())
    monkeypatch.setattr(engine, "inspect_one", lambda kind, identity: {"Config": config})
    with pytest.raises(engine.ValidationError, match="ONBUILD"):
        engine.require_base_image(BASE_IMAGE)


def test_source_contract_connects_dockerfile_closure_to_main_validation(monkeypatch):
    engine = load_engine()
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(engine, "_candidate_binding", lambda *args: {"source_sha256": {}})
    with pytest.raises(engine.ValidationError, match="COPY source"):
        engine.source_contract(ROOT, "public-intraday-runtime",
                               "02_configs/runtime_contracts/public-intraday-runtime.json")


def test_dockerfile_copy_requires_existing_bound_regular_file(tmp_path):
    engine = load_engine()
    (tmp_path / "Dockerfile").write_text(
        f"FROM {BASE_IMAGE}\nCOPY app.py /app/app.py\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("fixture\n", encoding="utf-8")
    with pytest.raises(engine.ValidationError, match="COPY source"):
        engine.validate_dockerfile_inputs(
            tmp_path, {"build": {"dockerfile": "Dockerfile", "dockerignore": ".dockerignore",
                                  "compose_sources": ["compose.yml"],
                                  "dependency_contracts": ["requirements.txt"]}},
            {"source_sha256": {}})


def test_dockerfile_copy_rejects_bound_directory(tmp_path):
    engine = load_engine()
    (tmp_path / "Dockerfile").write_text(
        f"FROM {BASE_IMAGE}\nCOPY src /app/src\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    with pytest.raises(engine.ValidationError, match="missing|regular file|COPY source"):
        engine.validate_dockerfile_inputs(
            tmp_path, {"build": {"dockerfile": "Dockerfile", "dockerignore": ".dockerignore",
                                  "compose_sources": ["compose.yml"],
                                  "dependency_contracts": ["requirements.txt"]}},
            {"source_sha256": {"src": "a" * 64}})


@pytest.mark.parametrize("dockerfile", [
    f"FROM python:3.12-slim\nCOPY app.py /app/app.py\n",
    f"FROM {BASE_IMAGE}\nRUN --network=host true\nCOPY app.py /app/app.py\n",
    f"FROM {BASE_IMAGE}\nCOPY --chown=1000:1000 app.py /app/app.py\n",
])
def test_dockerfile_rejects_unpinned_or_flagged_input_semantics(tmp_path, dockerfile):
    engine = load_engine()
    (tmp_path / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    (tmp_path / "app.py").write_text("fixture\n", encoding="utf-8")
    with pytest.raises(engine.ValidationError):
        engine.validate_dockerfile_inputs(
            tmp_path, {"build": {"dockerfile": "Dockerfile", "dockerignore": ".dockerignore",
                                  "compose_sources": ["compose.yml"],
                                  "dependency_contracts": ["requirements.txt"]}},
            {"source_sha256": {"app.py": "a" * 64}})


@pytest.mark.parametrize("forbidden", [
    "Dockerfile", ".dockerignore", "compose.yml",
    "04_scripts/runtime/validate_target_runtime.py",
])
def test_dockerfile_cannot_copy_build_or_validation_inputs(tmp_path, forbidden):
    engine = load_engine()
    (tmp_path / "Dockerfile").write_text(
        f"FROM {BASE_IMAGE}\nCOPY {forbidden} /app/input\n", encoding="utf-8")
    source = tmp_path / forbidden
    source.parent.mkdir(parents=True, exist_ok=True)
    if source != tmp_path / "Dockerfile":
        source.write_text("fixture\n", encoding="utf-8")
    binding = {"source_sha256": {
        forbidden: __import__("hashlib").sha256(source.read_bytes()).hexdigest()}}
    contract = {"build": {"dockerfile": "Dockerfile", "dockerignore": ".dockerignore",
                           "compose_sources": ["compose.yml"],
                           "dependency_contracts": ["requirements.txt"]}}
    with pytest.raises(engine.ValidationError, match="COPY source|runtime COPY"):
        engine.validate_dockerfile_inputs(tmp_path, contract, binding)


def test_dockerfile_must_copy_every_required_runtime_input(tmp_path):
    engine = load_engine()
    (tmp_path / "Dockerfile").write_text(
        f"FROM {BASE_IMAGE}\nCOPY app.py /app/app.py\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("fixture\n", encoding="utf-8")
    (tmp_path / "init.py").write_text("fixture\n", encoding="utf-8")
    contract = {"build": {"dockerfile": "Dockerfile", "dockerignore": ".dockerignore",
                           "compose_sources": ["compose.yml"],
                           "dependency_contracts": ["requirements.txt"]}}
    binding = {"source_sha256": {
        name: __import__("hashlib").sha256((tmp_path / name).read_bytes()).hexdigest()
        for name in ("app.py", "init.py")}}
    with pytest.raises(engine.ValidationError, match="required runtime COPY"):
        engine.validate_dockerfile_inputs(tmp_path, contract, binding)


def test_dependency_contract_must_also_be_copied_for_closed_inputs(tmp_path):
    engine = load_engine()
    (tmp_path / "Dockerfile").write_text(
        f"FROM {BASE_IMAGE}\nCOPY app.py /app/app.py\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("fixture\n", encoding="utf-8")
    contract = {"build": {"dockerfile": "Dockerfile", "dockerignore": ".dockerignore",
                           "compose_sources": ["compose.yml"],
                           "dependency_contracts": ["requirements.txt"]}}
    binding = {"source_sha256": {
        "app.py": __import__("hashlib").sha256((tmp_path / "app.py").read_bytes()).hexdigest(),
        "requirements.txt": "a" * 64}}
    with pytest.raises(engine.ValidationError, match="required runtime COPY"):
        engine.validate_dockerfile_inputs(tmp_path, contract, binding)


@pytest.mark.parametrize("module_name,body,returncode", [
    ("healthy_module", "VALUE = 1\n", 0),
    ("import_failure", "raise ImportError('fixture failure')\n", 1),
    ("os_failure", "raise OSError('fixture failure')\n", 1),
])
def test_python_module_probe_imports_and_reports_runtime_failures(
        tmp_path, module_name, body, returncode):
    engine = load_engine()
    (tmp_path / (module_name + ".py")).write_text(body, encoding="utf-8")
    argv = list(engine._python_module_probe_argv(module_name))
    argv[0] = sys.executable
    environment = {**os.environ, "PYTHONPATH": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(argv, cwd=tmp_path, env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False)
    assert (result.returncode == 0) is (returncode == 0), result.stderr.decode()
