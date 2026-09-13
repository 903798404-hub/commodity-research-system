from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    spec = importlib.util.spec_from_file_location(Path(path).stem, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


codec = load("09_deploy/runtime_identity/candidate_evidence.py")
engine = load("04_scripts/runtime/validate_target_runtime.py")
lifecycle = load("04_scripts/runtime/candidate_lifecycle.py")
record = load("09_deploy/runtime_identity/candidate_validation_record.py")


def identity():
    return dict(commit="a"*40, tree="b"*40, image_id="sha256:"+"c"*64,
                release_id="demo-b01", container_id="d"*64, nonce="e"*32)


def image_fixture():
    binding = dict(commit="a"*40, tree="b"*40, project_id="demo",
                   validator_version="target-runtime-validator/1", source_sha256={"app.py": "a"*64})
    labels = {"org.opencontainers.image.revision": binding["commit"], "market-data.git.tree": binding["tree"],
              "market-data.service": "demo", "market-data.artifact.origin": "candidate",
              "market-data.artifact.promotable": "true", "market-data.release.id": "demo-b01",
              "market-data.build.binding": engine._sha(engine._canonical(binding))}
    return binding, dict(Id=identity()["image_id"], Config=dict(Labels=labels, User="65532:65532"))


@pytest.mark.parametrize("field", [None, "org.opencontainers.image.revision", "market-data.git.tree", "market-data.release.id", "market-data.build.binding"])
def test_existing_image_is_observed_and_never_built(monkeypatch, field):
    binding, image = image_fixture()
    if field:
        image["Config"]["Labels"][field] = "wrong"
    monkeypatch.setattr(engine, "inspect_one", lambda *a: image)
    monkeypatch.setattr(engine, "build_image", lambda *a: pytest.fail("existing image was rebuilt"))
    if field:
        with pytest.raises(engine.ValidationError):
            engine.existing_image(image["Id"], {"service_id": "demo"}, binding, "demo-b01")
    else:
        assert engine.existing_image(image["Id"], {"service_id": "demo"}, binding, "demo-b01") == image["Id"]


def test_build_rejects_second_image_for_same_source(monkeypatch, tmp_path):
    binding, image = image_fixture()
    calls = []
    def docker(*args):
        calls.append(args)
        return SimpleNamespace(stdout=image["Id"].encode())
    monkeypatch.setattr(engine, "_docker", docker)
    with pytest.raises(engine.ValidationError, match="ALREADY_BUILT"):
        engine.build_image(tmp_path, tmp_path, {}, binding)
    assert len(calls) == 1 and calls[0][:2] == ("image", "ls")


def bundle_fixture(tmp_path):
    from test_candidate_validation_record import evidence
    evidence = evidence()
    ident = dict(identity(), commit=evidence["binding"]["commit"], tree=evidence["binding"]["tree"], image_id=evidence["image_id"])
    refs = []
    for kind in ("runtime", "health", "browser", "fixture", "consumer", "data", "grant", "instance"):
        raw = codec.canonical({"observation": kind})
        (tmp_path / (kind + ".json")).write_bytes(raw)
        refs.append(dict(name=kind+".json", type=kind, sha256=codec.digest(raw)))
    start = datetime.now(timezone.utc)
    bundle = dict(schema_version="candidate-evidence-bundle/1", identity=ident,
                  grant=dict(grant_id="f"*32, sha256="a"*64, issued_at=start.isoformat(), expires_at=(start+timedelta(minutes=15)).isoformat()),
                  started_at=start.isoformat(), completed_at=(start+timedelta(seconds=1)).isoformat(),
                  results={k: "PASS" for k in ("runtime", "health", "application", "browser", "fixture", "consumer", "production_data_unchanged")}, files=refs)
    payload = dict(evidence=evidence, release_id="demo-b01", evidence_bundle_sha256=codec.digest(codec.canonical(bundle)))
    return bundle, payload


@pytest.mark.parametrize("mutation", [None, "bundle-hash", "file-hash", "image", "release", "extra", "path", "duplicate", "application", "missing-browser", "expired"])
def test_bundle_rejects_tampering_and_incomplete_acceptance(tmp_path, mutation):
    bundle, payload = bundle_fixture(tmp_path)
    if mutation == "file-hash": (tmp_path / "browser.json").write_text("tampered")
    if mutation == "image": bundle["identity"]["image_id"] = "sha256:"+"0"*64
    if mutation == "release": bundle["identity"]["release_id"] = "other"
    if mutation == "extra": bundle["extra"] = True
    if mutation == "path": bundle["files"][0]["name"] = "../secret"
    if mutation == "duplicate": bundle["files"].append(bundle["files"][0])
    if mutation == "application": bundle["results"]["application"] = "FAIL"
    if mutation == "missing-browser": bundle["files"] = [f for f in bundle["files"] if f["type"] != "browser"]
    if mutation == "expired": bundle["grant"]["expires_at"] = bundle["started_at"]
    raw = codec.canonical(bundle)
    payload["evidence_bundle_sha256"] = codec.digest(raw) if mutation != "bundle-hash" else "0"*64
    if mutation:
        with pytest.raises(ValueError): codec.verify_bundle(raw, payload, tmp_path)
    else:
        assert codec.verify_bundle(raw, payload, tmp_path) == bundle


def test_record_v2_signature_binds_bundle_without_inline_dom(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from test_candidate_validation_record import envelope, trust, resign, raw, NOW
    key = Ed25519PrivateKey.generate()
    value = envelope(key)
    value["schema_version"] = "candidate-validation-record/2"
    value["payload"].update(release_id="demo-b01", validation_result="PASS", evidence_bundle_sha256="a"*64)
    resign(value, key)
    assert record.verify_record(raw(value), trust(key), now=NOW)["evidence_bundle_sha256"] == "a"*64
    value["payload"]["evidence_bundle_sha256"] = "b"*64
    with pytest.raises(ValueError, match="signature"):
        record.verify_record(raw(value), trust(key), now=NOW)
    resign(value, key)
    value["payload"]["browser_dom"] = "not allowed"
    resign(value, key)
    with pytest.raises(ValueError): record.verify_record(raw(value), trust(key), now=NOW)


def collector_fixture(tmp_path, monkeypatch, *, hook_failure=False, seal_failure=False):
    events = []
    root = tmp_path / "source"
    root.mkdir()
    plan_dir = root / "02_configs/runtime_acceptance"
    plan_dir.mkdir(parents=True)
    plan = dict(schema_version="candidate-acceptance-plan/1", port=18571, timeout_seconds=30,
                hooks=[dict(category=k, script="hook.py", config="config.json") for k in ("browser", "fixture", "consumer")])
    (plan_dir / "demo.json").write_bytes(codec.canonical(plan))
    (root / "hook.py").write_text("# trusted hook")
    (root / "config.json").write_text("{}")
    ident = identity()
    container = dict(Id=ident["container_id"], Image=ident["image_id"], Config=dict(Hostname=ident["nonce"]), State=dict(Running=True))
    fake = SimpleNamespace(_load=lambda *a: codec, _exact_source=lambda root, p: root / p,
                           _write_new=engine._write_new, _docker=lambda *a: SimpleNamespace(stdout=b""),
                           inspect_one=lambda *a: container)
    def seal(evidence, release, sha, directory):
        events.append("seal")
        assert not (directory / "cleanup.json").exists()
        payload = dict(evidence=evidence, release_id=release, evidence_bundle_sha256=sha)
        codec.verify_bundle((directory / "bundle.json").read_bytes(), payload, directory)
        if seal_failure: raise ValueError("seal failed")
    c = lifecycle.Lifecycle(fake, root, {"project_id": "demo"}, tmp_path / "evidence", seal)
    c.identity, c.mounts, c.data_before = ident, [], {}
    start = datetime.now(timezone.utc)
    grant = dict(grant_id="f"*32, issued_at=start.isoformat(), expires_at=(start+timedelta(minutes=15)).isoformat(),
                 approved_commit=ident["commit"], approved_tree=ident["tree"], image_id=ident["image_id"],
                 container_id=ident["container_id"], hostname_nonce=ident["nonce"], role="candidate_validation")
    c.authorized(codec.canonical({"payload": grant}))
    c.put("instance.json", ident, "instance")
    class Response:
        status = 200
        def read(self, n): return b"ok"
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(lifecycle.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=lambda *a, **k: Response()))
    def run(*args, input, **kwargs):
        request = json.loads(input)
        events.append(request["category"])
        return SimpleNamespace(returncode=int(hook_failure), stdout=codec.canonical(dict(identity=request["identity"], context="CANDIDATE_FIXTURE", timestamp=lifecycle.now(), status="FAIL" if hook_failure else "PASS", observations=[{"rows": 12}])))
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    return c, events, grant


@pytest.mark.parametrize("failure", [None, "application", "seal"])
def test_runtime_then_application_then_seal_then_cleanup(tmp_path, monkeypatch, failure):
    c, events, _ = collector_fixture(tmp_path, monkeypatch, hook_failure=failure == "application", seal_failure=failure == "seal")
    evidence = dict(binding={"commit": c.identity["commit"], "tree": c.identity["tree"]}, image_id=c.identity["image_id"])
    try:
        if failure:
            with pytest.raises(ValueError): c.finish(evidence)
        else:
            c.finish(evidence)
    finally:
        c.before_cleanup()
        assert (c.directory / "lifecycle-result.json").is_file()
        events.append("cleanup")
        c.after_cleanup(True)
    if failure == "application": assert "seal" not in events
    else: assert events.index("consumer") < events.index("seal") < events.index("cleanup")
    assert c.sealed is (failure is None)
    assert json.loads((c.directory / "cleanup.json").read_bytes())["image_retained"] is True


@pytest.mark.parametrize("change", ["expired", "production", "other-instance", "missing"])
def test_fresh_candidate_grant_is_required(tmp_path, monkeypatch, change):
    c, _, grant = collector_fixture(tmp_path, monkeypatch)
    if change == "expired": grant["expires_at"] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    if change == "production": grant["role"] = "production"
    if change == "other-instance": grant["container_id"] = "0"*64
    if change == "missing": grant = {}
    with pytest.raises((ValueError, KeyError)):
        c.authorized(codec.canonical({"payload": grant}))


def test_endpoint_is_internal_loopback_only(tmp_path, monkeypatch):
    c, _, _ = collector_fixture(tmp_path, monkeypatch)
    doc = c.configure({"services": {"demo": {"network_mode": "none"}}})
    assert doc["networks"]["default"]["internal"] is True
    assert doc["services"]["demo"]["ports"][0]["host_ip"] == "127.0.0.1"


@pytest.mark.parametrize("failure", [None, "runtime", "application", "seal"])
@pytest.mark.parametrize("reuse", [False, True])
def test_engine_full_lifecycle_build_count_and_cleanup(tmp_path, monkeypatch, failure, reuse):
    """Exercise the real engine ordering with an inert Docker/host boundary."""
    from contextlib import contextmanager
    events = []
    binding, image = image_fixture()
    contract = dict(project_id="demo", service_id="demo", module_id="demo", entrypoint=["python"],
                    working_directory="/app", runtime_roots=[dict(role="identity", container_path="/runtime", access="ro")],
                    identity_root_role="identity", required_executables=[], required_python_modules=[], initialization_commands=[])
    project = dict(project_id="demo", runtime_contract="runtime.json")
    manifest = b"{}"
    binding["source_sha256"]["runtime.json"] = engine._sha(manifest)
    image["Config"]["Labels"]["market-data.build.binding"] = engine._sha(engine._canonical(binding))
    image["Config"]["Labels"].update({"org.opencontainers.image.source": binding["validator_version"], "org.opencontainers.image.created": "2026-09-13T00:00:00+00:00"})
    container = dict(Id="d"*64, Image=image["Id"], State=dict(Running=False), Config=dict(Hostname="e"*32))
    release = dict(application="demo", release_id="demo-b01", git_commit=binding["commit"], git_tree=binding["tree"],
                   source=binding["validator_version"], build_time="2026-09-13T00:00:00+00:00")
    scope_root = tmp_path / "scope"
    scope_root.mkdir()
    identity_root = scope_root / "identity"
    identity_root.mkdir()
    descriptor = tmp_path / "descriptor"
    scope = dict(candidate_host_root=scope_root.as_posix(), mounts=[dict(source=identity_root.as_posix(), target="/runtime", read_only=True)],
                 candidate_scope=dict(descriptor_path=str(descriptor)))
    # The engine runs as Linux root; emulate removal of its readonly marker on
    # the Windows unit-test host without weakening runtime mount permissions.
    original_remove = engine.shutil.rmtree
    def remove(path, **kwargs):
        for item in Path(path).rglob("*"):
            if item.is_file(): item.chmod(0o600)
        original_remove(path, **kwargs)
    monkeypatch.setattr(engine.shutil, "rmtree", remove)
    @contextmanager
    def work():
        directory = tmp_path / "work"
        directory.mkdir()
        yield directory
    monkeypatch.setattr(engine, "_protected_work", work)
    def build(*a):
        events.append("build")
        return image["Id"]
    monkeypatch.setattr(engine, "build_image", build)
    monkeypatch.setattr(engine, "inspect_one", lambda kind, value: image if kind == "image" else container)
    for name in ("validate_source_compose", "create_archive_context", "_exclude_candidate_inputs", "_actual_host_rejection", "_seed_candidate_runtime_inputs", "_manifest_identity"):
        monkeypatch.setattr(engine, name, lambda *a, **k: None)
    monkeypatch.setattr(engine, "_runtime_bindings", lambda *a: [])
    monkeypatch.setattr(engine, "_compose_document", lambda *a: {"services": {}})
    monkeypatch.setattr(engine, "_render_compose", lambda *a: ({}, "f"*64))
    monkeypatch.setattr(engine, "_copy_bytes", lambda cid, path: manifest if path.endswith("runtime.json") else codec.canonical(release))
    monkeypatch.setattr(engine, "_policy", lambda *a: {"key_id": "test"})
    monkeypatch.setattr(engine, "_negative_observation_probes", lambda *a: {})
    def grant(*args, grant_path, **kwargs):
        assert not container["State"]["Running"]
        events.append("grant")
        grant_path.write_text("{}")
    host = SimpleNamespace(create_candidate_scope=lambda *a: scope, normalize_observation=lambda *a: {},
                           issue_execution_grant=grant, _mounts=lambda *a: scope["mounts"], _validate_v3_runtime=lambda *a: None)
    monkeypatch.setattr(engine, "_load", lambda *a: host)
    def docker(*args, **kwargs):
        if args[0] == "start":
            assert "grant" in events
            events.append("start")
            container["State"]["Running"] = True
        if args[0] == "rm": events.append("cleanup")
        if args[0] in ("rmi", "build"): pytest.fail("unexpected Docker build/image deletion")
        if "ps" in args and "--all" in args: return SimpleNamespace(stdout=(container["Id"]+"\n").encode(), returncode=0)
        return SimpleNamespace(stdout=b"", returncode=1 if args[0] == "cp" else 0)
    monkeypatch.setattr(engine, "_docker", docker)
    monkeypatch.setattr(engine, "_negative_observation_probes", lambda *a: {k: "PASS" for k in engine.REQUIRED_PROBES})
    def probe(*a, **k):
        if failure == "runtime": raise ValueError("runtime failed")
    monkeypatch.setattr(engine, "_exec", probe)
    class Collector:
        def configure(self, doc): return doc
        def created(self, *a): events.append("create")
        def authorized(self, *a): events.append("authorize")
        def started(self, *a): pass
        def finish(self, evidence):
            events.append("application")
            if failure == "application": raise ValueError("application failed")
            events.append("seal")
            if failure == "seal": raise ValueError("seal failed")
        def before_cleanup(self): events.append("result-saved")
        def after_cleanup(self, removed): assert removed
    kwargs = dict(existing_image_id=image["Id"], release_id="demo-b01") if reuse else {}
    if failure:
        with pytest.raises(ValueError): engine.validate_linux(tmp_path, project, contract, binding, "linux", lifecycle=Collector(), **kwargs)
    else:
        engine.validate_linux(tmp_path, project, contract, binding, "linux", lifecycle=Collector(), **kwargs)
        assert events.index("seal") < events.index("cleanup")
    assert events.count("build") == (0 if reuse else 1)
    assert events.index("result-saved") < events.index("cleanup")
    assert not scope_root.exists()


def test_cleanup_cannot_precede_seal_or_failure_evidence(tmp_path, monkeypatch):
    c, _, _ = collector_fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="cleanup before"):
        c.after_cleanup(True)


def test_soybean_fixture_adapter_executes_candidate_presentation(monkeypatch):
    adapter = load("04_scripts/runtime/acceptance_import_profit.py")
    # Same script run by docker exec, with only the local source-root projection
    # substituted for this host test; no production store or Docker is accessed.
    import sys
    monkeypatch.syspath_prepend(str(ROOT / "03_src"))
    monkeypatch.syspath_prepend(str(ROOT / "05_apps"))
    source = adapter.FIXTURES.replace("/app/", ROOT.as_posix() + "/")
    namespace = {}
    exec(compile(source, "candidate-fixture", "exec"), namespace)
    assert len(namespace["observations"]) == 12


@pytest.mark.parametrize("mutation", [None, "rows", "months", "fabricated", "heading", "exception"])
def test_browser_requires_dom_rows_months_and_empty_business_values(mutation):
    browser = load("04_scripts/runtime/acceptance_browser.py")
    config = json.loads((ROOT / "02_configs/runtime_acceptance/import-profit.json").read_text(encoding="utf-8"))
    class Node:
        def __init__(self, kind="tables", index=0): self.kind, self.index = kind, index
        def nth(self, n): return Node("table", n)
        def wait_for(self, **kwargs): pass
        def inner_html(self): return "<tbody>observed</tbody>"
        def count(self):
            if self.kind == "tables": return 2
            if self.kind == "exception": return int(mutation == "exception")
            return 0 if mutation == "rows" else 12
        def locator(self, selector):
            kind = "months" if selector == config["month_selector"] else "values"
            if selector == "tbody tr": kind = "rows"
            if selector.startswith("xpath"): kind = "section"
            if selector == "h2": kind = "heading"
            return Node(kind, self.index)
        def inner_text(self): return "wrong" if mutation == "heading" else config["headings"][self.index]
        def all_text_contents(self):
            if self.kind == "months": return ["2027-01"]*12 if mutation == "months" else ["2027-"+m for m in config["month_sequence"]]
            return ["0"]*12 if mutation == "fabricated" else ["—"]*12
    page = SimpleNamespace(goto=lambda *a, **k: None, locator=lambda selector: Node("exception" if "stException" in selector else "tables"))
    if mutation:
        with pytest.raises(ValueError): browser.inspect_page(page, "http://127.0.0.1:18571", config)
    else:
        assert len(browser.inspect_page(page, "http://127.0.0.1:18571", config)) == 2
