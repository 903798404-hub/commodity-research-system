from __future__ import annotations

import copy
import base64
from contextlib import nullcontext
import hashlib
import io
import json
import sys
import stat
import tarfile
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "09_deploy"))
from runtime_identity import host_authorization as host


CID = "a" * 64
IMAGE = "sha256:" + "b" * 64
COMMIT, TREE = "c" * 40, "d" * 40


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def policy(role: str = "candidate_validation") -> dict:
    root = "/tmp/candidate" if role == "candidate_validation" else "/runtime"
    return {"schema_version":"host-runtime-policy/1","role":role,"key_id":"candidate-key","project_id":"project","module_id":"shared-runtime","service_id":"svc","runtime_id":"runtime","approved_commit":COMMIT,"approved_tree":TREE,"image_id":IMAGE,"artifact_service":"svc","release_application":"app","source_root":"/app","runtime_root":root,"runtime_manifest_path":"/app/runtime.json","runtime_manifest_sha256":"d"*64,"runtime_marker_sha256":"e"*64,"release_sha256":"f"*64,"actual_config_sha256":"0"*64,"mounts":[{"source":"/tmp/grants","target":"/run/grants","read_only":True},{"source":"/tmp/candidate","target":root,"read_only":True},{"source":"/tmp/candidate/data","target":root+"/data","read_only":False}],"compose_sources":[{"path":"/tmp/compose.yml","sha256":"1"*64}],"compose_project_directory":"/tmp","compose_environment_file":"/tmp/env","rendered_compose_sha256":"2"*64,"grant_container_directory":"/run/grants","candidate_host_root":"/tmp/candidate" if role=="candidate_validation" else None}


def observed(expected: dict) -> dict:
    config={"User":"10001:10001","Hostname":"1"*32,"Env":["PATH=/bin"],"Entrypoint":["python"],"Cmd":["app.py"],"WorkingDir":"/app"}
    host_config={"ReadonlyRootfs":True,"Privileged":False,"SecurityOpt":["no-new-privileges:true"],"CapAdd":[],"CapDrop":["ALL"],"Devices":[],"DeviceRequests":[],"DeviceCgroupRules":[],"PidMode":"","IpcMode":"private","NetworkMode":"bridge","UTSMode":"","UsernsMode":"","VolumesFrom":[],"CgroupnsMode":"private"}
    release={"git_commit":COMMIT,"git_tree":TREE,"application":"app","release_id":"release"}
    labels={"org.opencontainers.image.revision":COMMIT,"market-data.git.tree":TREE,"market-data.service":"svc","market-data.artifact.promotable":"true","market-data.artifact.origin":"candidate","market-data.release.id":"release"}
    value={"container_id":CID,"image_id":IMAGE,"config":config,"host_config":host_config,"image_labels":labels,"release_manifest":release,"mounts":copy.deepcopy(expected["mounts"]),"state":{"Status":"created","Running":False}}
    value["actual_config_sha256"]=digest({"config":config,"host_config":host_config,"path":None,"args":None})
    expected["actual_config_sha256"]=value["actual_config_sha256"]
    return value


def test_policy_rejects_non_temporary_candidate_and_unknown_fields():
    value=policy(); host.validate_policy(value,"candidate_validation")
    broken=copy.deepcopy(value); broken["runtime_root"]="/runtime"
    with pytest.raises(host.HostAuthorizationError): host.validate_policy(broken,"candidate_validation")


def test_v2_policy_uses_explicit_candidate_scope_instead_of_container_path_heuristic():
    value = policy()
    value["schema_version"] = "host-runtime-policy/2"
    value["runtime_root"] = "/runtime/candidate"
    value["candidate_host_root"] = "/tmp/market-data-candidate-scopes/candidate-2"
    value["candidate_scope"] = {"descriptor_path":"/tmp/market-data-candidate-scopes/.candidate-scope-descriptors/candidate.json","descriptor_sha256":"1"*64,"scope_id":"2"*32}
    host.validate_policy(value, "candidate_validation")
    broken = copy.deepcopy(value); broken["candidate_scope"] = None
    with pytest.raises(host.HostAuthorizationError, match="scope"): host.validate_policy(broken, "candidate_validation")
    production = policy("production"); production["schema_version"] = "host-runtime-policy/2"; production["candidate_scope"] = value["candidate_scope"]
    with pytest.raises(host.HostAuthorizationError, match="scope"): host.validate_policy(production, "production")
    broken=copy.deepcopy(value); broken["extra"]=True
    with pytest.raises(host.HostAuthorizationError): host.validate_policy(broken,"candidate_validation")


def v3_policy(role: str) -> dict:
    value = policy(role)
    value["schema_version"] = "host-runtime-policy/4" if role == "candidate_validation" else "host-runtime-policy/5"
    value["grant_container_directory"] = "/run/market-data-grants"
    value["mounts"][0]["target"] = value["grant_container_directory"]
    if role == "candidate_validation":
        value["runtime_root"] = "/runtime/candidate"
        value["mounts"][1]["target"] = value["runtime_root"]
        value["mounts"][2]["target"] = value["runtime_root"] + "/data"
        value["candidate_host_root"] = "/tmp/market-data-candidate-scopes/candidate-4"
        value["mounts"][1]["source"] = value["candidate_host_root"]
        value["mounts"][2]["source"] = value["candidate_host_root"] + "/data"
        value["candidate_scope"] = {"descriptor_path":"/tmp/market-data-candidate-scopes/.candidate-scope-descriptors/scope.json", "descriptor_sha256":"1" * 64, "scope_id":"2" * 32}
    else:
        value.update(candidate_scope=None, approved_source_root="/var/lib/approved-source",
                     production_storage_root="/var/lib/market-data/production-runtime/project",
                     candidate_record={"path":"/var/lib/validation/record.json", "sha256":"3" * 64})
    return value


@pytest.mark.parametrize("role", ["candidate_validation", "production"])
def test_policy4_and_policy5_bind_v3_manifest_and_reserved_grant_directory(role: str):
    value = v3_policy(role)
    host.validate_policy(value, role)
    broken = copy.deepcopy(value)
    broken["grant_container_directory"] = "/run/grants"
    with pytest.raises(host.HostAuthorizationError, match="reserved"):
        host.validate_policy(broken, role)
    assert host._manifest_version(value["schema_version"]) == "runtime-manifest/3"


def v3_runtime_manifest() -> dict:
    return {
        "schema_version": "runtime-manifest/3",
        "required_environment": ["MODE", "HISTORY_PATH", "SERVICE_URL", "MARKET_DATA_EXECUTION_GRANT"],
        "forbidden_environment": ["ENABLE_WRITES"],
        "runtime_roots": [
            {"role":"marker", "container_path":"/runtime/candidate", "access":"ro"},
            {"role":"history", "container_path":"/runtime/candidate/history", "access":"ro"},
        ],
        "environment_bindings": [
            {"name":"MODE", "kind":"literal", "value":"STRICT"},
            {"name":"HISTORY_PATH", "kind":"runtime_path", "role":"history", "relative_path":"current.json"},
            {"name":"SERVICE_URL", "kind":"deployment", "value_type":"https_url", "candidate_value":"https://candidate.invalid/"},
            {"name":"MARKET_DATA_EXECUTION_GRANT", "kind":"execution_grant"},
        ],
        "candidate_runtime_inputs": [],
    }


@pytest.mark.parametrize("role,mutation", [("candidate_validation", None), ("production", None),
                                             ("candidate_validation", "literal"), ("candidate_validation", "forbidden"),
                                             ("candidate_validation", "candidate-url"), ("production", "deployment-invalid")])
def test_v3_host_runtime_revalidates_actual_environment(monkeypatch, role, mutation):
    runtime = v3_runtime_manifest()
    actual = {"MODE":"STRICT", "HISTORY_PATH":"/runtime/candidate/history/current.json",
              "SERVICE_URL":"https://candidate.invalid/" if role == "candidate_validation" else "https://production.invalid/", "MARKET_DATA_EXECUTION_GRANT":"/run/market-data-grants/grant.json"}
    if mutation == "literal": actual["MODE"] = "PREVIEW"
    elif mutation == "forbidden": actual["ENABLE_WRITES"] = ""
    elif mutation == "candidate-url": actual["SERVICE_URL"] = "https://production.invalid/"
    elif mutation == "deployment-invalid": actual["SERVICE_URL"] = "http://production.invalid/"
    observed_value = {"config":{"Env":[f"{key}={value}" for key, value in actual.items()]}, "mounts":[]}
    context = pytest.raises(host.HostAuthorizationError) if mutation else nullcontext()
    with context:
        host._validate_v3_runtime(runtime, observed_value, v3_policy(role), CID)


def test_v3_candidate_seed_requires_host_and_container_bytes_to_match(tmp_path, monkeypatch):
    runtime = v3_runtime_manifest()
    runtime["candidate_runtime_inputs"] = [{"role":"history", "relative_path":"current.json", "sha256":hashlib.sha256(b"fixture").hexdigest(), "source_path":"08_tests/fixtures/runtime/current.json"}]
    source_root = tmp_path / "history"; source_root.mkdir()
    (source_root / "current.json").write_bytes(b"fixture")
    observed_value = {"config":{"Env":[]}, "mounts":[{"source":str(source_root), "target":"/runtime/candidate/history", "read_only":True}]}
    monkeypatch.setattr(host, "_contract_module", lambda *args: SimpleNamespace(validate_runtime_environment=lambda *args, **kwargs: None))
    monkeypatch.setattr(host, "_protected_path", lambda path, **kwargs:path)
    monkeypatch.setattr(host, "copy_container_bytes", lambda *_args: b"fixture")
    expected = v3_policy("candidate_validation"); expected["candidate_host_root"] = str(source_root)
    host._validate_v3_runtime(runtime, observed_value, expected, CID)
    monkeypatch.setattr(host, "copy_container_bytes", lambda *_args: b"changed")
    with pytest.raises(host.HostAuthorizationError, match="seed identity"):
        host._validate_v3_runtime(runtime, observed_value, expected, CID)


@pytest.mark.parametrize("mutation", ["image","root","socket","namespace","mount"])
def test_observation_fails_closed_for_actual_runtime_drift(mutation: str):
    expected=policy(); value=observed(expected)
    if mutation=="image": value["image_id"]="sha256:"+"9"*64
    elif mutation=="root": value["config"]["User"]="0"
    elif mutation=="socket": value["host_config"]["Devices"]=[{"PathOnHost":"/var/run/docker.sock"}]
    elif mutation=="namespace": value["host_config"]["NetworkMode"]="host"
    else: value["mounts"][1]["read_only"]=False
    with pytest.raises(host.HostAuthorizationError): host.validate_observation(value,expected,role="candidate_validation")


def test_candidate_image_can_be_promoted_but_candidate_policy_cannot_be_production():
    prod=policy("production"); value=observed(prod)
    assert host.validate_observation(value,prod,role="production")["image_id"]==IMAGE
    candidate=policy("candidate_validation")
    with pytest.raises(host.HostAuthorizationError): host.validate_policy(candidate,"production")


@pytest.mark.parametrize("role", ["production", "candidate_validation"])
@pytest.mark.parametrize("read_only", [True, False])
def test_v2_rejects_any_readonly_or_writable_source_root_child_overlay(tmp_path, monkeypatch, role: str, read_only: bool):
    expected, container, _image, _rendered, _policy_file, _key, _grants, _manifest, _marker, _release, _identity = signing_fixture(tmp_path, monkeypatch, role, 2)
    value = observed(expected)
    value["mounts"].append({"source":"/tmp/injected-source", "target":"/app/unlisted.py", "read_only":read_only})
    expected["mounts"].append({"source":"/tmp/injected-source", "target":"/app/unlisted.py", "read_only":read_only})
    with pytest.raises(host.HostAuthorizationError, match="immutable source"):
        host.validate_observation(value, expected, role=role)


@pytest.mark.parametrize("role", ["production", "candidate_validation"])
def test_v1_retains_legacy_unlisted_source_child_mount_behavior(role: str):
    expected = policy(role)
    value = observed(expected)
    overlay = {"source":"/tmp/legacy-overlay", "target":"/app/unlisted.py", "read_only":True}
    value["mounts"].append(overlay)
    expected["mounts"].append(overlay)
    assert host.validate_observation(value, expected, role=role)["image_id"] == IMAGE


def signing_fixture(tmp_path, monkeypatch, role="candidate_validation", version=1):
    """Model Linux mounts/ownership and Docker transport, never the verifier or signer."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption
    from agri_research_agent.shared import production_identity as identity

    expected = policy(role)
    if version == 2:
        expected["schema_version"] = "host-runtime-policy/2"
        expected["candidate_scope"] = None if role == "production" else {"descriptor_path":"/tmp/protected/descriptors/scope.json","descriptor_sha256":"3"*64,"scope_id":"4"*32}
        if role == "production":
            expected.update(schema_version="host-runtime-policy/3", approved_source_root="/var/lib/approved-source",
                            production_storage_root="/var/lib/market-data/production-runtime/project",
                            candidate_record={"path":"/var/lib/validation/record.json", "sha256":"9"*64})
        if role == "candidate_validation":
            old_root = expected["candidate_host_root"]
            expected["candidate_host_root"] = "/tmp/market-data-candidate-scopes/candidate-4"
            for mount in expected["mounts"]:
                if mount["source"] == old_root: mount["source"] = expected["candidate_host_root"]
                elif mount["source"].startswith(old_root + "/"): mount["source"] = expected["candidate_host_root"] + mount["source"][len(old_root):]
            expected["candidate_scope"]["descriptor_path"] = "/tmp/market-data-candidate-scopes/.candidate-scope-descriptors/scope.json"
            expected["runtime_root"] = "/runtime/candidate"
            expected["mounts"][1]["target"] = expected["runtime_root"]
            expected["mounts"][2]["target"] = expected["runtime_root"] + "/data"
    image_root, runtime, grants = tmp_path / "image", tmp_path / "runtime", tmp_path / "grants"
    for folder in (image_root / "02_configs", image_root / "03_src", image_root / "04_scripts", image_root / "05_apps", runtime / "data", grants):
        folder.mkdir(parents=True)
    private = Ed25519PrivateKey.generate()
    key = tmp_path / "host-key.pem"
    key.write_bytes(private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    trust = image_root / "02_configs" / "production_runtime_trust.json"
    trust.write_text(json.dumps({"schema_version":"production-runtime-trust/1", "keys":[{"key_id":expected["key_id"], "domain":role, "algorithm":"ed25519", "public_key_base64":base64.b64encode(private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()}],"revoked_key_ids":[],"revoked_grant_ids":[]}),encoding="utf-8")
    manifest = {"schema_version":"runtime-manifest/1", "runtime_target":"production_container", "identity_kind":"oci_container", **{name:expected[name] for name in ("project_id","module_id","service_id")}, "runtime_roots":[{"role":"marker", "container_path":expected["runtime_root"], "access":"ro"},{"role":"data", "container_path":expected["runtime_root"]+"/data", "access":"rw"}],"required_mounts":[{"role":"marker","container_path":expected["runtime_root"],"read_only":True},{"role":"data","container_path":expected["runtime_root"]+"/data","read_only":False}]}
    if version == 2:
        manifest.update({
            "schema_version":"runtime-manifest/2", "build":{"dockerfile":"Dockerfile","dockerignore":".dockerignore","dependency_contracts":["requirements.txt"],"compose_sources":["compose.yml"]},
            "entrypoint":["python","app.py"], "working_directory":"/app", "required_environment":[], "secret_references":[],
            "required_executables":["python"], "required_python_modules":[],
            "production_policy":{"deployment_role":"production","write_grant_required":True},
            "preview_policy":{"production_write":False,"production_rw_mounts":False},
            "validation_probes":["entrypoint_initialization","runtime_identity","dependencies","runtime_paths","mount_permissions","missing_grant_rejected","wrong_commit_rejected","wrong_tree_rejected","wrong_image_rejected","wrong_service_rejected","wrong_manifest_rejected","preview_write_rejected","release_mismatch_rejected"],
            "identity_root_role":"marker", "initialization_commands":[{"name":"initialize","argv":["python","init.py"]}],
            "source_inputs":[{"path":"app.py","role":"entrypoint"},{"path":"init.py","role":"initialization"}],
        })
        (image_root / "app.py").write_text("print('runtime')\n", encoding="utf-8")
        (image_root / "init.py").write_text("print('initialize')\n", encoding="utf-8")
    marker = {"schema_version":1,"runtime_id":"runtime","module_id":"shared-runtime","classification":"candidate-validation" if role=="candidate_validation" else "formal", "created_at":"2026-01-01T00:00:00Z"}
    release = {"git_commit":COMMIT,"git_tree":TREE,"application":"app","release_id":"release"}
    manifest_file, marker_file, release_file = image_root / "runtime.json", runtime / ".market-data-runtime.json", image_root / "RELEASE.json"
    for file, value in ((manifest_file,manifest),(marker_file,marker),(release_file,release)):
        file.write_text(json.dumps(value,indent=2)+"\n",encoding="utf-8")
    expected.update(runtime_manifest_sha256=hashlib.sha256(manifest_file.read_bytes()).hexdigest(),runtime_marker_sha256=hashlib.sha256(marker_file.read_bytes()).hexdigest(),release_sha256=hashlib.sha256(release_file.read_bytes()).hexdigest())
    expected["mounts"] = sorted(expected["mounts"], key=lambda item:item["target"])
    value = observed(expected)
    config = value["config"]
    config["Labels"] = {"com.docker.compose.project.config_files":"/tmp/compose.yml","com.docker.compose.project.working_dir":"/tmp","com.docker.compose.service":"svc"}
    container = {"Id":CID,"Image":IMAGE,"Config":config,"HostConfig":value["host_config"],"State":value["state"],"Path":"python","Args":["app.py"],"Mounts":[{"Type":"bind","Source":m["source"],"Destination":m["target"],"RW":not m["read_only"]} for m in expected["mounts"]]}
    image = {"Id":IMAGE,"Config":{**copy.deepcopy(config),"Labels":value["image_labels"]}}
    expected["actual_config_sha256"] = host.normalize_observation(container,image,release)["actual_config_sha256"]
    rendered = {"services":{"svc":{"image":IMAGE,"entrypoint":config["Entrypoint"],"command":config["Cmd"],"working_dir":"/app","user":config["User"],"hostname":config["Hostname"],"environment":{},"volumes":[{"type":"bind",**m} for m in expected["mounts"]]}}}
    expected["rendered_compose_sha256"] = digest(rendered)
    compose_file, env_file = tmp_path / "compose.yml", tmp_path / "env"
    compose_file.write_text("services: {}\n",encoding="utf-8")
    env_file.write_text("",encoding="utf-8")
    expected["compose_sources"][0]["sha256"] = hashlib.sha256(compose_file.read_bytes()).hexdigest()
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(expected),encoding="utf-8")
    container_files = {"/app/RELEASE.json":release_file,"/app/runtime.json":manifest_file,expected["runtime_root"]+"/.market-data-runtime.json":marker_file,"/app/02_configs/production_runtime_trust.json":trust}
    def transport(args, **kwargs):
        if args[:2] == ["container","inspect"]:
            return json.dumps([container]).encode()
        if args[:2] == ["image","inspect"]:
            return json.dumps([image]).encode()
        if args[0] == "compose":
            assert "--env-file" in args and args[-3:] == ["config","--format","json"]
            return json.dumps(rendered).encode()
        if args[0] == "diff":
            return container.get("_test_layer_diff", "").encode()
        if args[0] == "cp":
            raw = container_files[args[1].split(":",1)[1]].read_bytes()
            result = io.BytesIO()
            with tarfile.open(fileobj=result,mode="w") as archive:
                item=tarfile.TarInfo("identity.json"); item.size=len(raw)
                archive.addfile(item,io.BytesIO(raw))
            return result.getvalue()
        raise AssertionError(args)
    original_path = Path
    host_paths = {"/tmp/compose.yml":compose_file,"/tmp/env":env_file}
    monkeypatch.setattr(host,"Path",lambda value:host_paths.get(str(value),original_path(value)))
    monkeypatch.setattr(host,"TRUST_CONFIG_PATH",trust)
    monkeypatch.setattr(host,"_run_docker",transport)
    # Windows does not expose Linux UID/mode/bind-source facts. These are tested
    # separately; only this OS boundary is simulated for the signature roundtrip.
    monkeypatch.setattr(host,"_require_linux_root",lambda:None)
    monkeypatch.setattr(host,"_protected_path",lambda path,**kwargs:path)
    monkeypatch.setattr(host,"require_protected_key_and_grant_dirs",lambda *args:None)
    monkeypatch.setattr(host,"_validate_mount_sources",lambda *args:None)
    monkeypatch.setattr(host,"_candidate_descriptor",lambda *args,**kwargs:{"scope_id":"4"*32})
    if version == 2 and role == "production":
        # This roundtrip isolates grant compatibility after pre-release succeeds.
        # Record verification and source/production bridges have separate tests.
        monkeypatch.setattr(host,"_validated_candidate_record",lambda policy:({"record_id":"5"*32}, manifest))
        monkeypatch.setattr(host,"_production_compose_bridge",lambda *args:[])
    monkeypatch.setattr(host,"_fsync_directory",lambda *args:None)
    monkeypatch.setattr(identity,"_ROOT",image_root)
    monkeypatch.setattr(identity,"TRUST_CONFIG_PATH",trust)
    mapping={image_root:"/app",runtime:expected["runtime_root"],grants:"/run/grants"}
    def runtime_path(path):
        for root,target in mapping.items():
            try:
                suffix=path.relative_to(root).as_posix()
                return target if suffix=="." else target+"/"+suffix
            except ValueError:
                pass
        return str(path).replace("\\","/")
    monkeypatch.setattr(identity,"_runtime_path",runtime_path)
    monkeypatch.setattr(identity,"_mount_options",lambda:{"/":{"ro"}})
    monkeypatch.setattr(identity,"_mount_for",lambda mounts,path:{"rw"} if runtime_path(path)==expected["runtime_root"]+"/data" else {"ro"})
    monkeypatch.setattr(identity.os,"geteuid",lambda:1000,raising=False)
    monkeypatch.setattr(identity.socket,"gethostname",lambda:config["Hostname"])
    return expected,container,image,rendered,policy_file,key,grants,manifest_file,marker_file,release_file,identity


@pytest.mark.parametrize("role",["production","candidate_validation"])
def test_host_signature_roundtrip_is_accepted_only_for_its_role(tmp_path,monkeypatch,role):
    expected,container,image,rendered,policy_file,key,grants,manifest,marker,release,identity = signing_fixture(tmp_path,monkeypatch,role)
    grant=grants/"grant.json"
    envelope=host.issue_execution_grant(CID,expected_policy_path=policy_file,key_path=key,grant_path=grant,grant_dir=grants,role=role)
    request=identity.OCIExecutionRequest(grant,release,manifest,marker.parent,marker)
    result=identity.verify_execution(request,expected_role=role,module_id="shared-runtime",runtime_id="runtime",runtime_root=marker.parent,marker_sha256=expected["runtime_marker_sha256"])
    assert result.image_id==IMAGE and envelope["payload"]["artifact_origin"]=="candidate"
    assert envelope["payload"]["rendered_compose_sha256"]==digest(rendered)
    with pytest.raises(identity.ProductionIdentityError):
        identity.verify_execution(request,expected_role="production" if role=="candidate_validation" else "candidate_validation",module_id="shared-runtime",runtime_id="runtime",runtime_root=marker.parent,marker_sha256=expected["runtime_marker_sha256"])
    with pytest.raises(host.HostAuthorizationError,match="new"):
        host.issue_execution_grant(CID,expected_policy_path=policy_file,key_path=key,grant_path=grant,grant_dir=grants,role=role)


def test_v3_production_issuer_keeps_grant_v2_identity_root_and_null_scope(tmp_path, monkeypatch):
    expected,container,image,rendered,policy_file,key,grants,manifest,marker,release,identity = signing_fixture(tmp_path,monkeypatch,"production",2)
    grant = grants / "grant-v2.json"
    envelope = host.issue_execution_grant(CID,expected_policy_path=policy_file,key_path=key,grant_path=grant,grant_dir=grants,role="production")
    assert envelope["schema_version"] == "production-execution-grant/2"
    assert envelope["payload"]["runtime_manifest_schema_version"] == "runtime-manifest/2"
    assert envelope["payload"]["identity_root_role"] == "marker"
    assert envelope["payload"]["candidate_scope_id"] is None
    request=identity.OCIExecutionRequest(grant,release,manifest,marker.parent,marker)
    assert identity.verify_execution(request,expected_role="production",module_id="shared-runtime",runtime_id="runtime",
                                     runtime_root=marker.parent,marker_sha256=expected["runtime_marker_sha256"]).image_id == IMAGE


def test_policy_v2_cannot_bypass_candidate_record_with_null_scope():
    expected = policy("production")
    expected.update(schema_version="host-runtime-policy/2", candidate_scope=None)
    with pytest.raises(host.HostAuthorizationError, match="policy/3 record"):
        host.validate_policy(expected, "production")


@pytest.mark.parametrize("mutation", [None, "record", "marker", "manifest", "secret-drift", "policy-drift"])
def test_readonly_production_revalidation_never_signs_or_starts(tmp_path, monkeypatch, mutation):
    expected, container, image, rendered, policy_file, key, grants, manifest, marker, release, identity = signing_fixture(tmp_path, monkeypatch, "production", 2)
    source_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    candidate = {"record_id":"5"*32, "evidence":{"rendered_compose_sha256":"6"*64}}
    monkeypatch.setattr(host, "_validated_candidate_record", lambda policy:(candidate, source_manifest))
    monkeypatch.setattr(host, "_load_private_key", lambda *args: (_ for _ in ()).throw(AssertionError("must not sign")))
    if mutation == "record":
        monkeypatch.setattr(host, "_validated_candidate_record", lambda policy: (_ for _ in ()).throw(host.HostAuthorizationError("invalid record")))
    elif mutation == "marker":
        value = json.loads(marker.read_text(encoding="utf-8")); value["classification"] = "preview"
        marker.write_text(json.dumps(value), encoding="utf-8")
        expected["runtime_marker_sha256"] = hashlib.sha256(marker.read_bytes()).hexdigest()
        policy_file.write_text(json.dumps(expected), encoding="utf-8")
    elif mutation == "manifest":
        manifest.write_text("{}", encoding="utf-8")
    elif mutation == "secret-drift":
        count = iter([{"secret":"initial"}, {"secret":"changed"}])
        monkeypatch.setattr(host, "_secret_state", lambda _:next(count))
    elif mutation == "policy-drift":
        count = iter([expected, {**expected, "runtime_id":"changed"}])
        monkeypatch.setattr(host, "_load_policy", lambda _:next(count))
    if mutation:
        with pytest.raises(host.HostAuthorizationError):
            host.revalidate_production(CID, expected_policy_path=policy_file)
    else:
        report = host.revalidate_production(CID, expected_policy_path=policy_file)
        assert report["PRE_RELEASE_VALIDATION"] == "PASS"
        assert report["production_write_granted"] is False and report["container_started"] is False
        assert report["candidate_rendered_compose_sha256"] != report["production_rendered_compose_sha256"]
        assert report["image_id"] == IMAGE
    assert not list(grants.iterdir())


@pytest.mark.parametrize("mode,gid,user,accepted", [(0o640,65532,"65532:65532",True), (0o600,0,"65532:65532",False),
                                                  (0o644,65532,"65532:65532",False), (0o640,22,"65532:65532",False),
                                                  (0o640,65532,"65532",False)])
def test_secret_access_uses_actual_numeric_runtime_identity(monkeypatch, mode, gid, user, accepted):
    state = SimpleNamespace(st_dev=1, st_ino=2, st_uid=0, st_gid=gid, st_mode=stat.S_IFREG|mode,
                            st_size=4, st_mtime_ns=10, st_ctime_ns=11)
    source = SimpleNamespace(stat=lambda:state, read_bytes=lambda:b"test")
    monkeypatch.setattr(host, "_protected_path", lambda path: path)
    if accepted:
        result = host._secret_file_identity(source, user)
        assert result["sha256"] == hashlib.sha256(b"test").hexdigest()
    else:
        with pytest.raises(host.HostAuthorizationError):
            host._secret_file_identity(source, user)


@pytest.mark.parametrize("mutation", [None, "environment", "command", "secret-target", "extra-service", "image", "build"])
def test_production_compose_bridge_allows_only_explicit_deployment_differences(tmp_path, monkeypatch, mutation):
    expected = policy("production")
    manifest = {"build":{"compose_sources":["compose.yml"]}, "service_id":"svc", "entrypoint":["python", "app.py"],
                "working_directory":"/app", "required_environment":["MODE"], "secret_references":["reader"]}
    secret = tmp_path / "secret.env"; secret.write_bytes(b"synthetic")
    desired = {"name":"production-project", "services":{"svc":{"image":IMAGE, "entrypoint":["python", "app.py"],
               "working_dir":"/app", "user":"65532:65532", "environment":{"MODE":"FORMAL"},
               "build":{"context":"/approved", "dockerfile":"Dockerfile"},
               "secrets":[{"source":"reader", "target":"/run/secrets/reader.env"}]}},
               "secrets":{"reader":{"name":"production-project_reader", "file":str(secret)}}}
    actual = copy.deepcopy(desired)
    service = actual["services"]["svc"]
    service.pop("build"); service["hostname"] = "7"*32
    if mutation == "environment": service["environment"]["MODE"] = "PREVIEW"
    elif mutation == "command": service["command"] = ["sh"]
    elif mutation == "secret-target": service["secrets"][0]["target"] = "/run/secrets/other.env"
    elif mutation == "extra-service": actual["services"]["other"] = copy.deepcopy(service)
    elif mutation == "image": service["image"] = "mutable:latest"
    elif mutation == "build": service["build"] = {"context":"/dev-worktree"}
    monkeypatch.setattr(host, "_run_docker", lambda args:json.dumps(desired).encode())
    monkeypatch.setattr(host, "_protected_path", lambda path, **kwargs:path)
    monkeypatch.setattr(host, "_secret_file_identity", lambda path, user:{"synthetic":True})
    if mutation:
        with pytest.raises(host.HostAuthorizationError):
            host._production_compose_bridge(actual, expected, manifest)
    else:
        assert host._production_compose_bridge(actual, expected, manifest) == [
            {"source":str(secret), "target":"/run/secrets/reader.env", "read_only":True}]


@pytest.mark.parametrize("policy_version,manifest_version", [(3, 2), (5, 3)])
@pytest.mark.parametrize("mutation", [None, "image", "commit", "source", "record-hash", "record-signature", "manifest", "source-root", "manifest-version"])
def test_host_consumes_real_signed_candidate_record(tmp_path, monkeypatch, policy_version, manifest_version, mutation):
    from datetime import datetime, timedelta, timezone
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import importlib.util
    spec = importlib.util.spec_from_file_location("record_test_fixtures", Path(__file__).with_name("test_candidate_validation_record.py"))
    helpers = importlib.util.module_from_spec(spec); spec.loader.exec_module(helpers)
    private = Ed25519PrivateKey.generate()
    value = helpers.envelope(private)
    now = datetime.now(timezone.utc)
    value["payload"].update(issued_at=(now-timedelta(seconds=1)).isoformat(), expires_at=(now+timedelta(hours=1)).isoformat())
    unsigned = {k:v for k,v in value.items() if k != "signature"}
    value["signature"] = base64.b64encode(private.sign(helpers.record.canonical(unsigned))).decode()
    raw = helpers.record.canonical(value)
    record_file = tmp_path / "record.json"; record_file.write_bytes(raw)
    trust_file = tmp_path / "trust.json"; trust_file.write_text(json.dumps(helpers.trust(private)), encoding="utf-8")
    evidence = value["payload"]["evidence"]
    binding = copy.deepcopy(evidence["binding"])
    expected = policy("production")
    expected.update(schema_version=f"host-runtime-policy/{policy_version}", candidate_scope=None,
                    approved_source_root=str(host.SOURCE_ROOT), approved_commit=binding["commit"], approved_tree=binding["tree"],
                    image_id=evidence["image_id"], runtime_manifest_path="/app/02_configs/runtime.json",
                    runtime_manifest_sha256=binding["source_sha256"]["02_configs/runtime.json"],
                    candidate_record={"path":str(record_file), "sha256":hashlib.sha256(raw).hexdigest()})
    manifest = {"schema_version":f"runtime-manifest/{manifest_version}", "project_id":expected["project_id"]}
    entry = SimpleNamespace(require_source=lambda *args:(binding["commit"], binding["tree"]))
    engine = SimpleNamespace(require_builder=lambda:None, _project=lambda *args:{"runtime_contract":"02_configs/runtime.json"},
                             source_contract=lambda *args:({},manifest,binding), validate_source_compose=lambda *args:{})
    original_loader = host._contract_module
    def load(path, name):
        if str(path).endswith("pre_release_runtime.py"): return entry
        if str(path).endswith("validate_target_runtime.py"): return engine
        return original_loader(path, name)
    monkeypatch.setattr(host, "_contract_module", load)
    monkeypatch.setattr(host, "_protected_path", lambda path, **kwargs:path)
    monkeypatch.setattr(host, "TRUST_CONFIG_PATH", trust_file)
    if mutation == "image": expected["image_id"] = IMAGE
    elif mutation == "commit": expected["approved_commit"] = "0"*40
    elif mutation == "source": binding["source_sha256"]["02_configs/runtime.json"] = "0"*64
    elif mutation == "record-hash": expected["candidate_record"]["sha256"] = "0"*64
    elif mutation == "record-signature":
        value["signature"] = base64.b64encode(b"x"*64).decode()
        record_file.write_bytes(helpers.record.canonical(value))
        expected["candidate_record"]["sha256"] = hashlib.sha256(record_file.read_bytes()).hexdigest()
    elif mutation == "manifest": expected["runtime_manifest_sha256"] = "0"*64
    elif mutation == "source-root": expected["approved_source_root"] = "/different/source"
    elif mutation == "manifest-version": manifest["schema_version"] = "runtime-manifest/2" if manifest_version == 3 else "runtime-manifest/3"
    if mutation:
        with pytest.raises(ValueError): host._validated_candidate_record(expected)
    else:
        assert host._validated_candidate_record(expected) == (value["payload"], manifest)


def test_v2_issuer_requires_protected_authority_source_before_docker_observation(tmp_path, monkeypatch):
    expected,container,image,rendered,policy_file,key,grants,*_ = signing_fixture(tmp_path,monkeypatch,"production",2)
    monkeypatch.setattr(host,"require_protected_authority_source",lambda:(_ for _ in ()).throw(host.HostAuthorizationError("unprotected authority")))
    monkeypatch.setattr(host,"_run_docker",lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError("Docker must not run")))
    with pytest.raises(host.HostAuthorizationError, match="unprotected authority"):
        host.issue_execution_grant(CID,expected_policy_path=policy_file,key_path=key,grant_path=grants/"grant.json",grant_dir=grants,role="production")


def test_v2_candidate_issuer_uses_signed_host_scope_with_logical_container_root(tmp_path, monkeypatch):
    expected,container,image,rendered,policy_file,key,grants,manifest,marker,release,identity = signing_fixture(tmp_path,monkeypatch,"candidate_validation",2)
    calls = []
    monkeypatch.setattr(host,"_candidate_descriptor",lambda policy,consume:calls.append(consume) or {"scope_id":"4"*32})
    grant = grants / "candidate-v2.json"
    envelope = host.issue_execution_grant(CID,expected_policy_path=policy_file,key_path=key,grant_path=grant,grant_dir=grants,role="candidate_validation")
    assert calls == [True, False]
    assert envelope["payload"]["runtime_root"] == "/runtime/candidate"
    assert envelope["payload"]["candidate_scope_id"] == "4" * 32
    assert envelope["payload"]["candidate_scope_sha256"] == "3" * 64
    request=identity.OCIExecutionRequest(grant,release,manifest,marker.parent,marker)
    assert identity.verify_execution(request,expected_role="candidate_validation",module_id="shared-runtime",runtime_id="runtime",
                                     runtime_root=marker.parent,marker_sha256=expected["runtime_marker_sha256"]).role.value == "candidate_validation"


@pytest.mark.parametrize("mutation",["image","tree","commit","service","manifest","marker","compose","environment","key-domain","container-layer"])
def test_host_cannot_sign_mismatched_actual_material(tmp_path,monkeypatch,mutation):
    expected,container,image,rendered,policy_file,key,grants,manifest,marker,release,identity=signing_fixture(tmp_path,monkeypatch)
    if mutation=="image": container["Image"]="sha256:"+"0"*64
    elif mutation=="tree": image["Config"]["Labels"]["market-data.git.tree"]="0"*40
    elif mutation=="commit": image["Config"]["Labels"]["org.opencontainers.image.revision"]="0"*40
    elif mutation=="service": image["Config"]["Labels"]["market-data.service"]="other-service"
    elif mutation=="manifest": manifest.write_text("{}",encoding="utf-8")
    elif mutation=="marker": marker.write_text("{}",encoding="utf-8")
    elif mutation=="compose": rendered["services"]["svc"]["image"]="sha256:"+"0"*64
    elif mutation=="environment": container["Config"]["Env"].append("PYTHONPATH=/tmp/foreign")
    elif mutation=="container-layer": container["_test_layer_diff"]="C /app/03_src/foreign.py\n"
    else:
        trust=json.loads(host.TRUST_CONFIG_PATH.read_text(encoding="utf-8"));trust["keys"][0]["domain"]="production"
        host.TRUST_CONFIG_PATH.write_text(json.dumps(trust),encoding="utf-8")
    with pytest.raises(host.HostAuthorizationError):
        host.issue_execution_grant(CID,expected_policy_path=policy_file,key_path=key,grant_path=grants/"grant.json",grant_dir=grants,role="candidate_validation")
    assert not (grants/"grant.json").exists()


class ObservedHostPath:
    """Small filesystem observation double; production path policy remains real."""
    def __init__(self, value, *, owner=0, mode=0o755, kind="directory", resolved=None, parent=None):
        self.value,self.owner,self.mode,self.kind=str(value),owner,mode,kind
        self.resolved,self.parent_path=resolved,parent
    def __str__(self): return self.value
    def __eq__(self,other): return self.value==str(other)
    @property
    def parents(self): return (self.parent_path,) if self.parent_path is not None else ()
    def is_absolute(self): return self.value.startswith("/")
    def exists(self): return True
    def is_symlink(self): return self.kind=="symlink"
    def is_dir(self): return self.kind=="directory"
    def is_file(self): return self.kind=="file"
    def resolve(self,strict=True): return self if self.resolved is None else ObservedHostPath(self.resolved)
    def stat(self): return SimpleNamespace(st_uid=self.owner,st_mode=self.mode | (stat.S_IFDIR if self.is_dir() else stat.S_IFREG))


@pytest.mark.parametrize("fault",["owner","mode","ancestor","alias"])
def test_host_protected_path_rejects_untrusted_filesystem_observations(monkeypatch,fault):
    monkeypatch.setattr(host,"_require_linux_root",lambda:None)
    path=ObservedHostPath("/etc/runtime/policy.json",kind="file",mode=0o600)
    assert host._protected_path(path,private=True)==path
    if fault=="owner": path.owner=1000
    elif fault=="mode": path.mode=0o666
    elif fault=="ancestor": path.parent_path=ObservedHostPath("/etc/runtime",mode=0o777)
    else: path.resolved="/tmp/other-policy.json"
    with pytest.raises(host.HostAuthorizationError): host._protected_path(path,private=True)


@pytest.mark.parametrize("source,kind,resolved",[("/production/data","directory",None),("/tmp/candidate/socket","socket",None),("/tmp/candidate/link","directory","/production/data")])
def test_candidate_mount_source_is_observed_not_self_declared(monkeypatch,source,kind,resolved):
    grants=ObservedHostPath("/run/grants")
    def path(value):
        return ObservedHostPath(value,kind=kind,resolved=resolved) if str(value)==source else ObservedHostPath(value)
    monkeypatch.setattr(host,"Path",path)
    mounts=[{"source":"/run/grants","target":"/run/grants","read_only":True},{"source":source,"target":"/tmp/candidate/data","read_only":False}]
    with pytest.raises(host.HostAuthorizationError):
        host._validate_mount_sources({"mounts":mounts},policy(),grants)


def test_native_non_linux_host_cannot_issue_grants(monkeypatch):
    monkeypatch.setattr(host,"sys",SimpleNamespace(platform="win32"))
    with pytest.raises(host.HostAuthorizationError,match="Linux root"):
        host._require_linux_root()


def isolation_fixture(monkeypatch):
    """Real validators over explicit POSIX inode/mount observations; no host IO."""
    from pathlib import PurePosixPath
    from datetime import datetime,timezone,timedelta
    nodes={}
    def add(path, *, file=None, mode=None):
        for parent in reversed(PurePosixPath(path).parents):
            nodes.setdefault(str(parent),dict(kind='dir',mode=0o755,ino=len(nodes)+1,raw=b''))
        nodes[path]=dict(kind='file' if file is not None else 'dir',mode=mode or (0o600 if file is not None else 0o700),ino=len(nodes)+1,raw=file or b'')
    class P(PurePosixPath):
        def exists(self): return str(self) in nodes
        def is_symlink(self): return nodes.get(str(self),{}).get('kind')=='symlink'
        def is_dir(self): return nodes.get(str(self),{}).get('kind')=='dir'
        def is_file(self): return nodes.get(str(self),{}).get('kind')=='file'
        def resolve(self,strict=True):
            if strict and not self.exists():raise FileNotFoundError(str(self))
            for parent in (self,*self.parents):
                if 'resolved' in nodes.get(str(parent),{}):return P(nodes[str(parent)]['resolved'])/self.relative_to(parent)
            return self
        def stat(self):
            n=nodes[str(self)];return SimpleNamespace(st_uid=n.get('uid',0),st_gid=0,st_dev=1,st_ino=n['ino'],st_nlink=n.get('links',1),st_mode=n['mode']|(stat.S_IFREG if self.is_file() else stat.S_IFLNK if self.is_symlink() else stat.S_IFDIR),st_size=len(n['raw']))
        lstat=stat
        def read_bytes(self): return nodes[str(self)]['raw']
    root='/tmp/market-data-candidate-scopes/candidate-test';grants='/var/lib/grants'
    add(root);add(grants)
    manifest=json.loads((Path(__file__).resolve().parents[1]/'02_configs/runtime_contracts/spread-production-runtime.json').read_text(encoding='utf-8'))
    mounts=[];binds=[]
    for m in manifest['required_mounts']:
        source=root+'/'+m['role'];add(source);mount=dict(source=source,target=m['container_path'],read_only=m['read_only']);mounts.append(mount)
        binds.append({**mount,'device':1,'inode':nodes[source]['ino']})
    mounts.append(dict(source=grants,target='/run/market-data-grants',read_only=True))
    production=v3_policy('production');production.update(project_id=manifest['project_id'],module_id=manifest['module_id'],service_id=manifest['service_id'])
    prodroot=production['production_storage_root'];add(prodroot);add(prodroot+'/snapshots');add(prodroot+'/snapshots/child')
    production['mounts']=[dict(source=prodroot+'/snapshots',target='/runtime/import-profit/snapshots',read_only=True),dict(source=prodroot+'/snapshots',target='/runtime/capture-snapshots',read_only=False)]
    prodfile='/etc/market-data/production.json';add(prodfile,file=host._canonical(production))
    now=datetime.now(timezone.utc)
    descriptor=dict(schema_version='candidate-scope/1',scope_id='2'*32,created_at=now.isoformat(),expires_at=(now+timedelta(minutes=30)).isoformat(),root=dict(path=root,device=1,inode=nodes[root]['ino']),binds=binds)
    descriptor_path='/tmp/market-data-candidate-scopes/.candidate-scope-descriptors/scope.json';add(descriptor_path,file=host._canonical(descriptor))
    p=v3_policy('candidate_validation');p.update(project_id=manifest['project_id'],module_id=manifest['module_id'],service_id=manifest['service_id'],candidate_host_root=root,mounts=mounts)
    p['candidate_scope']=dict(descriptor_path=descriptor_path,descriptor_sha256=hashlib.sha256(nodes[descriptor_path]['raw']).hexdigest(),scope_id='2'*32)
    monkeypatch.setattr(host,'Path',P);monkeypatch.setattr(host,'_require_linux_root',lambda:None);monkeypatch.setattr(host,'_host_mount_points',lambda:())
    monkeypatch.setattr(host,'_run_docker',lambda args: b'' if args==['ps','-aq','--no-trunc'] else pytest.fail('unexpected Docker observation'))
    # Simulate only OS enumeration, preserving the real ownership/alias checks.
    def walk(path,**kwargs):
        for name,n in list(nodes.items()):
            if n['kind']=='dir' and (name==str(path) or name.startswith(str(path)+'/')):
                children=[k for k in nodes if str(P(k).parent)==name and k!=name]
                yield name,[P(k).name for k in children if nodes[k]['kind']=='dir'],[P(k).name for k in children if nodes[k]['kind']!='dir']
    monkeypatch.setattr(host.os,'walk',walk)
    def seal():
        nodes[descriptor_path]['raw']=host._canonical(descriptor);p['candidate_scope']['descriptor_sha256']=hashlib.sha256(nodes[descriptor_path]['raw']).hexdigest()
    return SimpleNamespace(nodes=nodes,P=P,root=root,grants=grants,manifest=manifest,policy=p,descriptor=descriptor,seal=seal,production=production,prodfile=prodfile,prodroot=prodroot)


def test_soybean_isolated_required_rw_passes_routine_and_grant_dry_check(monkeypatch):
    f=isolation_fixture(monkeypatch)
    host.validate_candidate_mounts(f.manifest,f.policy['mounts'],f.policy,f.P(f.grants))
    import importlib.util
    path=Path(__file__).resolve().parents[1]/'04_scripts/runtime/routine_release.py'
    spec=importlib.util.spec_from_file_location('_routine_isolation_dry',path);r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
    monkeypatch.setattr(r,'Path',f.P)
    b=r.DockerSession({},None,host,{},f.manifest);b.role='candidate_validation';b.spec={'policy_template':'policy','grant_directory':f.grants}
    b.protected_json=lambda _:f.policy
    b._check_mounts([dict(type='bind',**m) for m in f.policy['mounts']])


@pytest.mark.parametrize('fault',['ro','missing','target','production','production-child','unknown','symlink','junction','parent-alias','inode','root-inode','root-owner','nested-bind','hardlink'])
def test_candidate_mount_negative_source_and_manifest_matrix(monkeypatch,fault):
    _check_candidate_mount_negative(monkeypatch, fault, '/runtime/capture-snapshots')


@pytest.mark.parametrize('fault',['ro','missing','target','production','production-child','unknown','symlink','junction','parent-alias','inode','root-inode','root-owner','nested-bind','hardlink'])
@pytest.mark.parametrize('target',['/runtime/import-profit/operational/cnf','/runtime/import-profit/operational/am-results'])
def test_operational_candidate_mount_negative_source_and_manifest_matrix(monkeypatch,fault,target):
    _check_candidate_mount_negative(monkeypatch, fault, target)


def _check_candidate_mount_negative(monkeypatch,fault,target):
    f=isolation_fixture(monkeypatch);mount=next(m for m in f.policy['mounts'] if m['target']==target);source=mount['source']
    if fault=='ro':mount['read_only']=True;next(b for b in f.descriptor['binds'] if b['source']==source)['read_only']=True;f.seal()
    elif fault=='missing':f.policy['mounts'].remove(mount)
    elif fault=='target':mount['target']='/runtime/wrong'
    elif fault in ['production','production-child','unknown']:mount['source']=f.prodroot+'/snapshots'+('/child' if fault=='production-child' else '') if fault!='unknown' else '/unknown'
    elif fault in ['symlink','junction','parent-alias']:
        n=f.nodes[source if fault!='parent-alias' else f.root];n['resolved']=f.prodroot+'/snapshots'
        if fault=='symlink':n['kind']='symlink'
    elif fault=='inode':f.nodes[source]['ino']+=100
    elif fault=='root-inode':f.nodes[f.root]['ino']+=100
    elif fault=='root-owner':f.nodes[f.root]['uid']=1000
    elif fault=='nested-bind':monkeypatch.setattr(host,'_host_mount_points',lambda:(source,))
    elif fault=='hardlink':f.nodes[source].update(kind='file',links=2)
    with pytest.raises((host.HostAuthorizationError,FileNotFoundError)):
        host.validate_candidate_mounts(f.manifest,f.policy['mounts'],f.policy,f.P(f.grants))


def add_production_readonly(f):
    target='/runtime/import-profit/snapshots'
    f.descriptor['binds']=[b for b in f.descriptor['binds'] if b['target']!=target]
    item,_=host._production_readonly_binding(dict(policy_path=f.prodfile,source=f.prodroot+'/snapshots',target=target))
    f.descriptor['production_readonly']=[item]
    next(m for m in f.policy['mounts'] if m['target']==target)['source']=item['source'];f.seal()


@pytest.mark.parametrize('fault',['none','rw','inode','policy','overlap','ancestor-overlap','unapproved-target','readonly-alias'])
def test_production_readonly_requires_approved_source_identity(monkeypatch,fault):
    f=isolation_fixture(monkeypatch);add_production_readonly(f)
    if fault=='rw':next(m for m in f.policy['mounts'] if m['target']=='/runtime/import-profit/snapshots')['read_only']=False
    elif fault=='inode':f.nodes[f.prodroot+'/snapshots']['ino']+=1
    elif fault=='policy':f.nodes[f.prodfile]['raw']+=b' '
    elif fault in ['overlap','ancestor-overlap']:
        f.production['mounts'].append(dict(source=f.root if fault=='overlap' else str(f.P(f.root).parent),target='/runtime/other',read_only=False))
        f.nodes[f.prodfile]['raw']=host._canonical(f.production)
        item=f.descriptor['production_readonly'][0];item['policy_sha256']=hashlib.sha256(f.nodes[f.prodfile]['raw']).hexdigest();f.seal()
    elif fault=='unapproved-target':f.descriptor['production_readonly'][0]['target']='/unknown';f.seal()
    elif fault=='readonly-alias':f.nodes[f.prodroot+'/snapshots']['resolved']='/other'
    if fault=='none':host.validate_candidate_mounts(f.manifest,f.policy['mounts'],f.policy,f.P(f.grants))
    else:
        with pytest.raises(host.HostAuthorizationError):host.validate_candidate_mounts(f.manifest,f.policy['mounts'],f.policy,f.P(f.grants))


def test_fresh_grant_rechecks_source_inode_before_any_docker_or_grant_write(monkeypatch):
    f=isolation_fixture(monkeypatch)
    f.nodes[f.root+'/capture-snapshots']['ino']+=1
    monkeypatch.setattr(host,'require_protected_authority_source',lambda:None)
    monkeypatch.setattr(host,'_load_policy',lambda _:f.policy)
    monkeypatch.setattr(host,'require_protected_key_and_grant_dirs',lambda *a:None)
    monkeypatch.setattr(host,'observe_and_validate',lambda *a,**k:pytest.fail('must reject before Docker'))
    with pytest.raises(host.HostAuthorizationError,match='inode changed'):
        host.issue_execution_grant(CID,expected_policy_path='/etc/policy',key_path='/etc/key',grant_path=f.grants+'/grant.json',grant_dir=f.grants,role='candidate_validation')
    assert f.grants+'/grant.json' not in f.nodes


def test_approved_production_readonly_does_not_require_fake_fixture_bytes(monkeypatch):
    f=isolation_fixture(monkeypatch);add_production_readonly(f)
    runtime=copy.deepcopy(f.manifest)
    runtime['candidate_runtime_inputs']=[dict(role='snapshots',relative_path='absent-ci-seed.json',sha256='0'*64,source_path='08_tests/fixture.json')]
    monkeypatch.setattr(host,'_contract_module',lambda *a:SimpleNamespace(validate_runtime_environment=lambda *a,**k:None))
    monkeypatch.setattr(host,'copy_container_bytes',lambda *a:pytest.fail('production readonly input is not a CI fixture'))
    host._validate_v3_runtime(runtime,{'config':{'Env':[]},'mounts':f.policy['mounts']},f.policy,CID)
    f.nodes[f.prodroot+'/snapshots']['ino']+=1
    with pytest.raises(host.HostAuthorizationError,match='identity changed'):
        host._validate_v3_runtime(runtime,{'config':{'Env':[]},'mounts':f.policy['mounts']},f.policy,CID)


@pytest.mark.parametrize('relation',['same','ancestor','child','disjoint','readonly','own'])
def test_actual_other_container_writable_overlap(monkeypatch,relation):
    f=isolation_fixture(monkeypatch)
    source=f.root if relation in ('same','readonly','own') else str(f.P(f.root).parent) if relation=='ancestor' else f.root+'/capture-snapshots' if relation=='child' else f.prodroot+'/snapshots'
    c=dict(Id=CID,Mounts=[dict(Source=source,RW=relation!='readonly',Type='bind')])
    monkeypatch.setattr(host,'_run_docker',lambda args,**kwargs:(CID+'\n').encode() if args[0]=='ps' else json.dumps([c]).encode())
    args=(f.manifest,f.policy['mounts'],f.policy,f.P(f.grants))
    if relation in ('disjoint','readonly','own'):
        host.validate_candidate_mounts(*args,container_id=CID if relation=='own' else None)
    else:
        with pytest.raises(host.HostAuthorizationError,match='overlaps another container'):
            host.validate_candidate_mounts(*args)
