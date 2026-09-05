from __future__ import annotations

import copy
import base64
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


def test_v2_production_issuer_binds_manifest_identity_root_and_null_scope(tmp_path, monkeypatch):
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
