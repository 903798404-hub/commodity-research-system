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
SEMANTIC_PLATFORM = dict(ServerVersion='26.1.3', CgroupVersion='2', CgroupDriver='systemd')
IMAGE = "sha256:" + "b" * 64
COMMIT, TREE = "c" * 40, "d" * 40


def real_config_payloads():
    actual = json.loads((Path(__file__).parent / 'fixtures/runtime_config/production-e42-running.json').read_bytes())
    sealed = copy.deepcopy(actual)
    sealed['host_config']['OomKillDisable'] = False
    return sealed, actual


def test_real_recovery_payload_replay_preserves_both_historical_raw_hashes(monkeypatch):
    sealed, actual = real_config_payloads()
    old_hash = '6fdd5f00fb76b6ffa5112f9ecd2b5e525886484022570b44109a2bac7eb9fe88'
    new_hash = 'fd0dd335dce809264b779b2af915e4305aabd3e9f9922b56b94ad70c64ed2d85'
    assert host._digest(sealed) == old_hash
    assert host._digest(actual) == new_hash
    monkeypatch.setattr(host, '_run_docker', lambda args: host._canonical(SEMANTIC_PLATFORM))
    result = host.compare_observed_config(dict(actual, actual_config_sha256=new_hash), old_hash)
    assert result == dict(policy_raw_sha256=old_hash, actual_raw_sha256=new_hash,
                         raw_hash_match=False, semantic_config_match=True,
                         compatibility_rule='OOM_KILL_DISABLE_FALSE_NULL_EQUIVALENCE', platform=SEMANTIC_PLATFORM)
    assert actual['host_config']['OomKillDisable'] is None
    assert sealed['host_config']['OomKillDisable'] is False


@pytest.mark.parametrize('left,right,passes',[(False,False,True),(None,None,True),(False,None,True),
    (None,False,True),(True,None,False),(True,False,False),(None,True,False),(False,True,False),
    (0,None,False),('',False,False)])
def test_config_default_equivalence_is_typed_and_bounded(left,right,passes):
    sealed,actual=real_config_payloads()
    sealed['host_config']['OomKillDisable']=left;actual['host_config']['OomKillDisable']=right
    call=lambda:host.compare_config_payloads(sealed,actual,policy_raw_sha256=host._digest(sealed),
        actual_raw_sha256=host._digest(actual),platform=SEMANTIC_PLATFORM)
    if passes:assert call()['semantic_config_match']
    else:
        with pytest.raises(host.HostAuthorizationError):call()


@pytest.mark.parametrize('field,value',[
    ('Image','sha256:'+'f'*64),('Entrypoint',['different']),('Cmd',['different']),('Env',['BUSINESS=changed']),
    ('User','0:0'),('WorkingDir','/other'),('Hostname','f'*32),('Labels',{'com.docker.compose.project':'other'})])
def test_semantic_rule_rejects_every_other_config_difference(monkeypatch,field,value):
    sealed,actual=real_config_payloads();actual['config'][field]=value
    monkeypatch.setattr(host,'_run_docker',lambda args:host._canonical(SEMANTIC_PLATFORM))
    with pytest.raises(host.HostAuthorizationError):
        host.compare_observed_config(dict(actual,actual_config_sha256=host._digest(actual)),host._digest(sealed))


@pytest.mark.parametrize('field',['Source','Target','ReadOnly'])
def test_semantic_rule_keeps_mount_identity_strict(field):
    sealed,actual=real_config_payloads();actual['host_config']['Mounts'][0][field]=False if field=='ReadOnly' else '/changed'
    with pytest.raises(host.HostAuthorizationError):
        host.compare_config_payloads(sealed,actual,policy_raw_sha256=host._digest(sealed),
            actual_raw_sha256=host._digest(actual),platform=SEMANTIC_PLATFORM)


@pytest.mark.parametrize('field',['NetworkMode','PortBindings','ReadonlyRootfs','SecurityOpt','CapAdd','CapDrop',
    'RestartPolicy','Memory','Privileged','Dns','ExtraHosts','PidsLimit'])
def test_semantic_rule_keeps_other_host_fields_strict(field):
    sealed,actual=real_config_payloads();actual['host_config'][field]='changed'
    with pytest.raises(host.HostAuthorizationError):
        host.compare_config_payloads(sealed,actual,policy_raw_sha256=host._digest(sealed),
            actual_raw_sha256=host._digest(actual),platform=SEMANTIC_PLATFORM)


@pytest.mark.parametrize('side',['policy','actual'])
def test_semantic_rule_rejects_forged_raw_binding(side):
    sealed,actual=real_config_payloads()
    with pytest.raises(host.HostAuthorizationError,match='raw hash'):
        host.compare_config_payloads(sealed,actual,
            policy_raw_sha256='0'*64 if side=='policy' else host._digest(sealed),
            actual_raw_sha256='0'*64 if side=='actual' else host._digest(actual),platform=SEMANTIC_PLATFORM)


@pytest.mark.parametrize('platform',[None,{},dict(ServerVersion='26.1.3',CgroupVersion='1'),
    dict(ServerVersion='27.0.0',CgroupVersion='2')])
def test_semantic_rule_requires_verified_platform(platform):
    sealed,actual=real_config_payloads()
    with pytest.raises(host.HostAuthorizationError,match='Docker/cgroup'):
        host.compare_config_payloads(sealed,actual,policy_raw_sha256=host._digest(sealed),
            actual_raw_sha256=host._digest(actual),platform=platform)


def test_raw_equal_needs_no_platform_and_bad_reconstruction_never_queries_docker(monkeypatch):
    sealed,actual=real_config_payloads()
    monkeypatch.setattr(host,'_run_docker',lambda args:pytest.fail('unexpected Docker query'))
    assert host.compare_observed_config(dict(actual,actual_config_sha256=host._digest(actual)),host._digest(actual))['raw_hash_match']
    with pytest.raises(host.HostAuthorizationError,match='sealed raw hash'):
        host.compare_observed_config(dict(actual,actual_config_sha256=host._digest(actual)),'0'*64)


def test_same_instance_create_to_start_default_transition_preserves_raw_evidence(monkeypatch):
    sealed,actual=real_config_payloads()
    monkeypatch.setattr(host,'_run_docker',lambda args:host._canonical(SEMANTIC_PLATFORM))
    image={'Config':{}};release={}
    def observe(payload,state):
        return host.normalize_observation(dict(Id=CID,Image=IMAGE,Config=payload['config'],HostConfig=payload['host_config'],
            Path=payload['path'],Args=payload['args'],Mounts=[],State=state),image,release)
    created=observe(sealed,dict(Status='created',Running=False));running=observe(actual,dict(Status='running',Running=True))
    assert created['container_id']==running['container_id']
    assert not host.compare_observed_config(running,created['actual_config_sha256'])['raw_hash_match']
    assert running['actual_config_sha256']==host._digest(actual)


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


def test_application_service_policy_is_exact_and_restricted():
    value = v3_policy("production")
    value["application_service"] = {"service_id": "svc", "runtime_id": "runtime",
                                    "allowed_writable_roots": ["/runtime/data"]}
    host.validate_policy(value, "production")
    for change in ({"service_id": "other"}, {"runtime_id": "other"},
                   {"allowed_writable_roots": ["/runtime"]},
                   {"allowed_writable_roots": ["/other"]},
                   {"allowed_writable_roots": ["/runtime/data", "/runtime/data"]}):
        broken = copy.deepcopy(value)
        broken["application_service"].update(change)
        with pytest.raises(host.HostAuthorizationError):
            host.validate_policy(broken, "production")


def test_trusted_launcher_issues_once_after_exact_image_and_mount_validation(tmp_path, monkeypatch):
    private = tmp_path / "service-private"
    private.mkdir()
    secret = private / "service.json"
    secret.touch()
    value = v3_policy("production")
    value["application_service"] = {"service_id": "svc", "runtime_id": "runtime",
                                    "allowed_writable_roots": ["/runtime/data"]}
    value["mounts"].append({"source": str(secret),
        "target": "/run/secrets/market-data-service.json", "read_only": True})
    actual = observed(value)
    calls = []
    monkeypatch.setattr(host, "_require_linux_root", lambda: None)
    monkeypatch.setattr(host, "require_protected_authority_source", lambda: None)
    monkeypatch.setattr(host, "_load_policy", lambda path: value)
    # The issuer is Linux-only; policy shape is exercised separately above.
    monkeypatch.setattr(host, "validate_policy", lambda value, role: None)
    monkeypatch.setattr(host, "_protected_path", lambda path, **kwargs: Path(path))
    monkeypatch.setattr(host.stat, "S_IMODE", lambda mode: 0o700)
    monkeypatch.setattr(host, "_fsync_directory", lambda path: None)
    monkeypatch.setattr(host, "_secret_file_identity", lambda path, user: {
        "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()})
    monkeypatch.setattr(host, "copy_container_bytes", lambda cid, path: secret.read_bytes())
    monkeypatch.setattr(host, "docker_inspect", lambda cid: {"Id": cid})
    monkeypatch.setattr(host, "docker_image_inspect", lambda image: {"Id": image})
    monkeypatch.setattr(host, "_render_actual_compose", lambda *args: calls.append("compose") or "approved")
    def verify(cid, expected, *, role):
        calls.append("approved-image-and-container")
        if cid not in {CID, "e" * 64} or expected["image_id"] != IMAGE:
            raise host.HostAuthorizationError("unapproved image or container")
        return {**actual, "container_id": cid}
    monkeypatch.setattr(host, "observe_and_validate", verify)
    result = host.issue_application_service_credential(CID, expected_policy_path=tmp_path / "policy.json",
        credential_path=secret, role="production")
    payload = json.loads(secret.read_bytes())
    assert calls[:2] == ["approved-image-and-container", "compose"]
    assert result == {"service_id": "svc", "runtime_id": "runtime",
                      "deployment_id": payload["deployment_id"]}
    assert len(payload["credential"]) == 64 and payload["allowed_writable_roots"] == ["/runtime/data"]
    assert "credential" not in result
    host._validate_application_service_credential(CID, value, actual)
    with pytest.raises(host.HostAuthorizationError, match="another deployment"):
        host._validate_application_service_credential("e" * 64, value, actual)
    with pytest.raises(host.HostAuthorizationError, match="newly allocated"):
        host.issue_application_service_credential("e" * 64, expected_policy_path=tmp_path / "policy.json",
            credential_path=secret, role="production")
    secret.write_bytes(b"")
    value["image_id"] = "sha256:" + "f" * 64
    with pytest.raises(host.HostAuthorizationError, match="unapproved image"):
        host.issue_application_service_credential(CID, expected_policy_path=tmp_path / "policy.json",
            credential_path=secret, role="production")
    assert secret.read_bytes() == b""
    value["image_id"] = IMAGE
    value["application_service"]["allowed_writable_roots"] = ["/runtime/01_data"]
    with pytest.raises(host.HostAuthorizationError, match="exact approved writable mount"):
        host.issue_application_service_credential(CID, expected_policy_path=tmp_path / "policy.json",
            credential_path=secret, role="production")
    assert secret.read_bytes() == b""


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


def test_ephemeral_candidate_public_trust_cannot_authorize_production(tmp_path, monkeypatch):
    expected, container, image, rendered, policy_file, key, grants, manifest, marker, release, identity = signing_fixture(
        tmp_path, monkeypatch, "production")
    external = grants / "candidate-validation-trust.json"
    external.write_text("{}", encoding="utf-8")
    with pytest.raises(host.HostAuthorizationError, match="cannot authorize production"):
        host.issue_execution_grant(
            CID, expected_policy_path=policy_file, key_path=key,
            grant_path=grants / "grant.json", grant_dir=grants, role="production",
            external_candidate_trust_path=external)
    assert not (grants / "grant.json").exists()


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


def test_application_service_candidate_credential_uses_only_bound_temporary_scope(monkeypatch):
    f = isolation_fixture(monkeypatch)
    f.nodes['/tmp']['mode'] = 0o1777
    secret = f.root + '/service-private/service.json'
    f.nodes[f.root + '/service-private'] = dict(kind='dir', mode=0o700, ino=9001, raw=b'')
    f.nodes[secret] = dict(kind='file', mode=0o440, ino=9002, raw=b'')
    mount = dict(source=secret, target='/run/secrets/market-data-service.json', read_only=True)
    f.policy['mounts'].append(mount)
    f.descriptor['binds'].append({**mount, 'device':1, 'inode':9002})
    f.seal()
    assert host._application_service_credential_path(f.P(secret), f.policy) == f.P(secret)
    for outside in ('/tmp/foo', f.root + '/../outside'):
        with pytest.raises(host.HostAuthorizationError):
            host._application_service_credential_path(f.P(outside), f.policy)
    f.nodes[secret]['kind'] = 'symlink'
    f.nodes[secret]['resolved'] = '/tmp/outside'
    with pytest.raises(host.HostAuthorizationError):
        host._application_service_credential_path(f.P(secret), f.policy)


def test_application_service_production_credential_keeps_original_protected_path(monkeypatch):
    f = isolation_fixture(monkeypatch)
    f.nodes['/tmp']['mode'] = 0o1777
    protected = '/var/lib/service-private/service.json'
    f.nodes['/var/lib/service-private'] = dict(kind='dir', mode=0o700, ino=9010, raw=b'')
    f.nodes[protected] = dict(kind='file', mode=0o440, ino=9011, raw=b'')
    assert host._application_service_credential_path(f.P(protected), f.production) == f.P(protected)
    with pytest.raises(host.HostAuthorizationError, match='unprotected ancestor'):
        host._application_service_credential_path(f.P('/tmp/market-data-candidate-scopes/candidate-test'), f.production)
    f.nodes['/var/lib/service-private']['mode'] = 0o777
    with pytest.raises(host.HostAuthorizationError, match='unprotected ancestor'):
        host._application_service_credential_path(f.P(protected), f.production)


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


# Recovery uses the existing production signing domain; only protected host
# policy opts into namespace projection. These fixtures simulate Docker I/O.
def recovery_policy():
    p = v3_policy('production')
    n = '7' * 32
    p['recovery'] = dict(purpose='recovery-validation', baseline_policy=dict(path='/etc/recovery/old.json',sha256='a'*64),
        production_container_id='b'*64, production_observation_sha256='c'*64, nonce=n,
        project='spread-recovery-'+n, container=p['service_id']+'-recovery-'+n,
        host_port=18509, network='spread-recovery-'+n+'-net', expected_network_id='e'*64,
        preserved_store_sources=['/var/lib/operational/cnf','/var/lib/operational/am'])
    return p


def recovery_module():
    return host._contract_module(host.SOURCE_ROOT/'09_deploy/runtime_identity/recovery_namespace.py','_recovery_test')


def test_real_old_compose_empty_ipam_replays_as_no_custom_ipam():
    m=recovery_module()
    # Exact network portion observed from the old production Compose resolution.
    old={'default':{'name':'spread-e42a61e-20260908-b01_default','ipam':{}}}
    same_without_ipam={'name':old['default']['name']}
    assert m.semantic_network(host,old['default'])==m.semantic_network(host,same_without_ipam)
    assert m.semantic_network(host,old['default'])==dict(
        name=old['default']['name'],driver='bridge',internal=False)
    assert 'ipam' not in m.semantic_network(host,old['default'])


@pytest.mark.parametrize('left,right,equal',[
    ({}, {}, True), ({}, {'ipam':{}}, True), ({'ipam':{}}, {}, True),
    ({'ipam':{}}, {'ipam':{}}, True),
    # Keep the baseline node identities while updating their effective-default contract.
    pytest.param({}, {'driver':'bridge'}, True, id='left4-right4-False'),
    pytest.param({}, {'internal':False}, True, id='left5-right5-False'),
    ({}, {'ipam':{'config':[{'subnet':'172.20.0.0/16'}]}}, False),
    ({'ipam':{}}, {'ipam':{'config':[{'subnet':'172.20.0.0/16'}]}}, False),
    ({'ipam':{}}, {'ipam':{'driver':'default'}}, False),
    ({'ipam':{}}, {'ipam':{'options':{'custom':'yes'}}}, False),
])
def test_only_empty_network_ipam_is_semantically_absent(left,right,equal):
    m=recovery_module();base={'name':'isolated_default'}
    actual=m.semantic_network(host,base|left)==m.semantic_network(host,base|right)
    assert actual is equal


@pytest.mark.parametrize('field,value',[
    ('ipam',None),('ipam',[]),('options',{}),('attachable',False),('enable_ipv6',False),
    ('driver','overlay'),('internal',True)])
def test_network_semantic_normalization_does_not_ignore_other_fields(field,value):
    m=recovery_module();network={'name':'isolated_default','ipam':{}}
    network[field]=value
    if field in ('driver','internal'):
        assert m.semantic_network(host,network)!=m.semantic_network(host,{'name':'isolated_default'})
    else:
        with pytest.raises(host.HostAuthorizationError):m.semantic_network(host,network)


@pytest.mark.parametrize('fault',[None,'role','purpose','name','port','extra','unprotected-flag'])
def test_recovery_projection_requires_explicit_protected_policy(fault):
    p=recovery_policy()
    if fault=='role':p['role']='candidate_validation'
    elif fault=='purpose':p['recovery']['purpose']='production'
    elif fault=='name':p['recovery']['container']=p['service_id']
    elif fault=='port':p['recovery']['host_port']=8501
    elif fault=='extra':p['recovery']['safe']=True
    elif fault=='unprotected-flag':p['safe_projection']=p.pop('recovery')
    if fault:
        with pytest.raises(host.HostAuthorizationError):host.validate_policy(p,p['role'])
    else:host.validate_policy(p,'production')


@pytest.fixture
def recovery_compose(tmp_path,monkeypatch):
    m=recovery_module();p=recovery_policy();r=p['recovery'];sid=p['service_id']
    baseline=dict(name='production',services={sid:dict(image=IMAGE,container_name=sid,
        entrypoint=['streamlit','run','page.py'],environment={'BUSINESS':'unchanged'},
        ports=[dict(target=8501,published='8501',protocol='tcp',mode='ingress')],
        networks={'default':None},volumes=[dict(type='bind',source='/old/history',target='/history',read_only=True),
            dict(type='bind',source='/old/grants',target=p['grant_container_directory'],read_only=True)])},
        networks={'default':dict(name='production_default')})
    old=copy.deepcopy(p);old.pop('recovery');old['rendered_compose_sha256']=digest(baseline)
    f=tmp_path/'old-compose.json';f.write_bytes(host._canonical(baseline));old['compose_sources']=[dict(path=str(f),sha256=hashlib.sha256(f.read_bytes()).hexdigest())]
    p['mounts']=[dict(source='/new/grants',target=p['grant_container_directory'],read_only=True)]
    production=dict(Id='b'*64,NetworkSettings={'Networks':{'production_default':{'NetworkID':'d'*64}}})
    monkeypatch.setattr(m,'retained',lambda h,policy:(old,production))
    monkeypatch.setattr(host,'_protected_path',lambda path,**kw:path)
    monkeypatch.setattr(host,'_run_docker',lambda args:host._canonical(baseline))
    desired=copy.deepcopy(baseline);desired['name']=r['project'];desired['networks']['default']['name']=r['project']+'_default'
    desired['services'][sid]['volumes'][1]['source']='/new/grants'
    return m,p,desired,old,production


@pytest.mark.parametrize('fault',[None,'image','env','command','history-source','history-rw','extra'])
def test_recovery_only_projects_namespace_from_exact_old_configuration(recovery_compose,fault):
    m,p,d,old,prod=recovery_compose;s=d['services'][p['service_id']]
    if fault=='image':s['image']='sha256:'+'d'*64
    elif fault=='env':s['environment']['BUSINESS']='different'
    elif fault=='command':s['entrypoint']=['shell']
    elif fault=='history-source':s['volumes'][0]['source']='/other/history'
    elif fault=='history-rw':s['volumes'][0]['read_only']=False
    elif fault=='extra':s['privileged']=True
    if fault:
        with pytest.raises(host.HostAuthorizationError):m.project_compose(host,d,p)
    else:
        before=copy.deepcopy(prod);actual=m.project_compose(host,d,p);s=actual['services'][p['service_id']]
        assert s['container_name']==p['recovery']['container']
        assert s['ports'][0]['host_ip']=='127.0.0.1' and s['ports'][0]['published']=='18509'
        assert actual['name']==p['recovery']['project']
        assert actual['networks']['default']==dict(name=p['recovery']['network'],driver='bridge',internal=False)
        assert s['volumes']==d['services'][p['service_id']]['volumes'] and prod==before


@pytest.mark.parametrize('old_ipam,desired_ipam,old_change,desired_change,passes',[
    ({},None,None,None,True),
    ({},{},None,None,True),
    (None,{},None,None,True),
    ({'config':[{'subnet':'172.20.0.0/16'}]},None,None,None,False),
    ({},{'config':[{'subnet':'172.20.0.0/16'}]},None,None,False),
    ({'driver':'default'},None,None,None,False),
    ({},{'options':{'custom':'yes'}},None,None,False),
    ({},None,('internal',True),None,False),
    ({},None,None,('internal',True),False),
    ({},None,('driver','overlay'),None,False),
    ({},None,None,('driver','overlay'),False),
    ({},None,('options',{}),None,False),
    ({},None,None,('attachable',False),False),
])
def test_recovery_project_compose_preserves_network_behavior_and_projects_name(
        recovery_compose,monkeypatch,old_ipam,desired_ipam,old_change,desired_change,passes):
    m,p,desired,old,production=recovery_compose
    baseline=host._json(host._run_docker(['compose','config','--format','json']))
    old_network=baseline['networks']['default'];new_network=desired['networks']['default']
    if old_ipam is not None:old_network['ipam']=old_ipam
    if desired_ipam is not None:new_network['ipam']=desired_ipam
    if old_change:old_network[old_change[0]]=old_change[1]
    if desired_change:new_network[desired_change[0]]=desired_change[1]
    old['rendered_compose_sha256']=host._digest(baseline)
    monkeypatch.setattr(host,'_run_docker',lambda args:host._canonical(baseline))
    if passes:
        result=m.project_compose(host,desired,p)
        assert result['networks']['default']==dict(name=p['recovery']['network'],driver='bridge',internal=False)
        assert result['services'][p['service_id']]['ports'][0]['host_ip']=='127.0.0.1'
        assert result['services'][p['service_id']]['container_name']==p['recovery']['container']
        assert p['recovery']['network'] not in production['NetworkSettings']['Networks']
    else:
        with pytest.raises(host.HostAuthorizationError):m.project_compose(host,desired,p)


@pytest.mark.parametrize('fault',[None,'production-network','public-bind','wrong-name','wrong-nonce','other-endpoint','internal','network-id','wrong-port'])
def test_recovery_actual_docker_namespace_isolated_with_production_still_present(monkeypatch,fault):
    m=recovery_module();p=recovery_policy();r=p['recovery']
    prod=dict(Id='b'*64,NetworkSettings={'Networks':{'production_default':{'NetworkID':'d'*64}}})
    c=dict(Id=CID,Name='/'+r['container'],Config=dict(Hostname=r['nonce'],Labels={'com.docker.compose.project':r['project']}),
        State=dict(Status='created',Running=False),
        HostConfig=dict(PortBindings={'8501/tcp':[dict(HostIp='127.0.0.1',HostPort='18509')]},NetworkMode=r['network']),
        NetworkSettings={'Networks':{r['network']:{'NetworkID':''}}})
    net=dict(Id='e'*64,Name=r['network'],Driver='bridge',Internal=False,EnableIPv6=False,
        Labels={'com.docker.compose.project':r['project']},Containers={CID:{}})
    if fault=='production-network':c['NetworkSettings']['Networks']['production_default']={}
    elif fault=='public-bind':c['HostConfig']['PortBindings']['8501/tcp'][0]['HostIp']='0.0.0.0'
    elif fault=='wrong-name':c['Name']='/'+p['service_id']
    elif fault=='wrong-nonce':c['Config']['Hostname']='f'*32
    elif fault=='other-endpoint':net['Containers']['b'*64]={}
    elif fault=='internal':net['Internal']=True
    elif fault=='network-id':net['Id']='d'*64
    elif fault=='wrong-port':c['HostConfig']['PortBindings']['8501/tcp'][0]['HostPort']='8501'
    monkeypatch.setattr(m,'retained',lambda *args:({},prod));monkeypatch.setattr(m,'preserved',lambda *args:[])
    monkeypatch.setattr(host,'_run_docker',lambda args:host._canonical([net]))
    if fault:
        with pytest.raises(host.HostAuthorizationError):m.validate_instance(host,c,p,'pre_start')
    else:assert m.validate_instance(host,c,p,'pre_start')['RUNTIME_NETWORK_IDENTITY_ASSERTION']=='DEFERRED_UNTIL_POST_START'


@pytest.fixture
def recovery_network_lifecycle(monkeypatch):
    m=recovery_module();p=recovery_policy();r=p['recovery']
    evidence=json.loads((Path(__file__).resolve().parent/'fixtures/runtime_config/recovery-created-e42-network.json').read_text(encoding='utf-8'))
    p['service_id']='spread-dashboard'
    r.update(nonce=evidence['nonce'],project=evidence['project'],container=evidence['container'],
        network=evidence['network'],host_port=evidence['host_port'])
    assert evidence['network_object_id_in_original_evidence'] is None
    assert evidence['declared_network_id']==evidence['declared_endpoint_id']==''
    # The real created inspect was retained, but network inspect was not. Its
    # object ID and simulated post-start endpoint are synthetic, never evidence.
    c=dict(Id=evidence['container_id'],Name='/'+r['container'],
        State=dict(Status=evidence['state'],Running=evidence['running']),
        Config=dict(Hostname=r['nonce'],Labels={'com.docker.compose.project':r['project']}),
        HostConfig=dict(PortBindings={'8501/tcp':[dict(HostIp='127.0.0.1',
            HostPort=str(r['host_port']))]},NetworkMode=r['network']),
        NetworkSettings={'Networks':{r['network']:{'NetworkID':evidence['declared_network_id'],
            'EndpointID':evidence['declared_endpoint_id']}}})
    net=dict(Id=r['expected_network_id'],Name=r['network'],Driver='bridge',Internal=False,
        EnableIPv6=False,Labels={'com.docker.compose.project':r['project']},Containers={})
    prod=dict(Id='b'*64,NetworkSettings={'Networks':{'production_default':{'NetworkID':'d'*64}}})
    monkeypatch.setattr(m,'retained',lambda *args:({},prod))
    monkeypatch.setattr(m,'preserved',lambda *args:[])
    monkeypatch.setattr(host,'_run_docker',lambda args:host._canonical([net]))
    return m,p,c,net


@pytest.mark.parametrize('network_id,passes,deferred',[
    ('',True,True),('expected',True,False),('wrong',False,False),
])
def test_created_network_identity_is_strict_but_runtime_endpoint_can_be_deferred(
        recovery_network_lifecycle,network_id,passes,deferred):
    m,p,c,net=recovery_network_lifecycle
    endpoint=c['NetworkSettings']['Networks'][p['recovery']['network']]
    endpoint['NetworkID']=net['Id'] if network_id=='expected' else 'f'*64 if network_id=='wrong' else ''
    if not passes:
        with pytest.raises(host.HostAuthorizationError):m.validate_instance(host,c,p,'pre_start')
    else:
        result=m.validate_instance(host,c,p,'pre_start')
        assert result['expected_network_id']==net['Id']
        assert result['RUNTIME_NETWORK_IDENTITY_ASSERTION']==(
            'DEFERRED_UNTIL_POST_START' if deferred else 'PASS')


@pytest.mark.parametrize('fault',[None,'empty-id','wrong-id','empty-endpoint',
    'production-network','extra-network','public-bind','not-running','other-endpoint',
    'wrong-network-object'])
def test_post_start_network_identity_requires_materialized_exact_isolated_endpoint(
        recovery_network_lifecycle,fault):
    m,p,c,net=recovery_network_lifecycle
    r=p['recovery'];c['State']=dict(Status='running',Running=True)
    endpoint=c['NetworkSettings']['Networks'][r['network']]
    endpoint.update(NetworkID=net['Id'],EndpointID='a'*64)
    net['Containers'][c['Id']]={}
    if fault=='empty-id':endpoint['NetworkID']=''
    elif fault=='wrong-id':endpoint['NetworkID']='f'*64
    elif fault=='empty-endpoint':endpoint['EndpointID']=''
    elif fault=='production-network':c['NetworkSettings']['Networks']['production_default']={'NetworkID':'d'*64}
    elif fault=='extra-network':c['NetworkSettings']['Networks']['other']={'NetworkID':'f'*64}
    elif fault=='public-bind':c['HostConfig']['PortBindings']['8501/tcp'][0]['HostIp']='0.0.0.0'
    elif fault=='not-running':c['State']=dict(Status='created',Running=False)
    elif fault=='other-endpoint':net['Containers']['f'*64]={}
    elif fault=='wrong-network-object':net['Id']='f'*64
    if fault:
        with pytest.raises(host.HostAuthorizationError):m.validate_instance(host,c,p,'post_start')
    else:
        assert m.validate_instance(host,c,p,'post_start')['RUNTIME_NETWORK_IDENTITY_ASSERTION']=='PASS'


@pytest.mark.parametrize('fault',['production-network','extra-network','running','wrong-object'])
def test_pre_start_deferred_endpoint_never_weakens_declared_network(recovery_network_lifecycle,fault):
    m,p,c,net=recovery_network_lifecycle
    if fault=='production-network':c['NetworkSettings']['Networks']['production_default']={}
    elif fault=='extra-network':c['NetworkSettings']['Networks']['other']={}
    elif fault=='running':c['State']=dict(Status='running',Running=True)
    elif fault=='wrong-object':net['Id']='f'*64
    with pytest.raises(host.HostAuthorizationError):m.validate_instance(host,c,p,'pre_start')


def test_recovery_grant_id_commits_to_exact_protected_network_object(recovery_network_lifecycle):
    m,p,c,_=recovery_network_lifecycle
    original=m.grant_id(host,p,c['Id'])
    assert len(original)==32
    p['recovery']['expected_network_id']='f'*64
    assert m.grant_id(host,p,c['Id'])!=original
    p['recovery'].pop('expected_network_id')
    with pytest.raises(host.HostAuthorizationError,match='requires the expected network object ID'):
        m.grant_id(host,p,c['Id'])


@pytest.mark.parametrize('fault',[None,'commit','tree','image','mount','reuse-grant','production-changed','oom-default'])
def test_recovery_retained_policy_and_live_artifact_remain_exact(tmp_path,monkeypatch,fault):
    m=recovery_module();p=recovery_policy();old=copy.deepcopy(p);old.pop('recovery')
    value=observed(old)
    if fault == 'oom-default':
        value['host_config']['OomKillDisable'] = False
        old['actual_config_sha256'] = host._digest(dict(config=value['config'],host_config=value['host_config'],path=None,args=None))
    c=dict(Id='b'*64,Name='/'+old['service_id'],Image=IMAGE,Config=value['config'],HostConfig=value['host_config'],
        Mounts=[dict(Type='bind',Source=x['source'],Destination=x['target'],RW=not x['read_only']) for x in old['mounts']],
        State=dict(Running=True,StartedAt='2026-09-01T00:00:00Z'),NetworkSettings={'Networks':{}},Created='old',RestartCount=0)
    p['actual_config_sha256']=old['actual_config_sha256']
    p['mounts'][0]['source']='/var/lib/market-data/production-runtime/project/recovery-grants'
    f=tmp_path/'old.json';f.write_bytes(host._canonical(old));p['recovery']['baseline_policy']['sha256']=hashlib.sha256(f.read_bytes()).hexdigest()
    p['recovery']['production_observation_sha256']=m.production_identity(host,c)
    image=dict(Id=IMAGE,Config={'Labels':value['image_labels']})
    monkeypatch.setattr(host,'_protected_path',lambda *a,**kw:f)
    monkeypatch.setattr(host,'docker_inspect',lambda cid:c);monkeypatch.setattr(host,'docker_image_inspect',lambda image_id:image)
    monkeypatch.setattr(host,'copy_container_json',lambda *a:value['release_manifest'])
    if fault == 'oom-default':
        c['HostConfig']['OomKillDisable'] = None
        p['recovery']['production_observation_sha256'] = m.production_identity(host,c)
        monkeypatch.setattr(host, '_run_docker', lambda args: host._canonical(SEMANTIC_PLATFORM))
    if fault in ('commit','tree'):p['approved_'+fault]='e'*40
    elif fault=='image':p['image_id']='sha256:'+'d'*64
    elif fault=='mount':p['mounts'][1]['source']='/different'
    elif fault=='reuse-grant':p['mounts']=copy.deepcopy(old['mounts'])
    elif fault=='production-changed':c['RestartCount']=1
    if fault and fault != 'oom-default':
        with pytest.raises(host.HostAuthorizationError):m.retained(host,p)
    else:assert m.retained(host,p)[0]==old


@pytest.mark.parametrize('mount_mode',[None,True,False])
def test_recovery_never_mounts_operational_stores_and_preserves_bytes(tmp_path,mount_mode):
    m=recovery_module();cnf=tmp_path/'cnf';am=tmp_path/'am'
    cnf.mkdir();am.mkdir();(cnf/'quotes').write_bytes(b'retained');(am/'result').write_bytes(b'retained AM')
    p=dict(recovery=dict(preserved_store_sources=[str(cnf),str(am)]),mounts=[])
    if mount_mode is not None:p['mounts']=[dict(source=str(cnf),target='/new',read_only=mount_mode)]
    if mount_mode is not None:
        with pytest.raises(host.HostAuthorizationError):m.preserved(host,p)
    else:
        before=m.preserved(host,p);assert before==m.preserved(host,p)
        (cnf/'quotes').write_bytes(b'changed');assert before!=m.preserved(host,p)


def test_recovery_fresh_grant_preserves_old_image_protocol_and_instance_binding(tmp_path,monkeypatch):
    p,c,image,rendered,file,key,grants,manifest,marker,release,identity=signing_fixture(tmp_path,monkeypatch,'production',2)
    p['recovery']=recovery_policy()['recovery'];p['recovery']['container']=p['service_id']+'-recovery-'+p['recovery']['nonce']
    # Namespace transport is covered independently above. Keep the real signer,
    # grant schema and old image verifier, including nonce and actual instance.
    original=host._recovery_call
    monkeypatch.setattr(host,'_recovery_call',lambda name,*args:original(name,*args) if name in ('validate','grant_id') else None)
    file.write_bytes(host._canonical(p))
    envelope=host.issue_execution_grant(CID,expected_policy_path=file,key_path=key,grant_path=grants/'grant.json',grant_dir=grants,role='production')
    assert envelope['schema_version']=='production-execution-grant/2'
    assert envelope['payload']['container_id']==CID and envelope['payload']['image_id']==IMAGE
    assert envelope['payload']['role']=='production'
    assert envelope['payload']['grant_id']==original('grant_id',p,CID)
    request=identity.OCIExecutionRequest(grants/'grant.json',release,manifest,marker.parent,marker)
    identity.verify_execution(request,expected_role='production',module_id='shared-runtime',runtime_id='runtime',runtime_root=marker.parent,marker_sha256=p['runtime_marker_sha256'])
    monkeypatch.setattr(identity.socket,'gethostname',lambda:'f'*32)
    with pytest.raises(identity.ProductionIdentityError):
        identity.verify_execution(request,expected_role='production',module_id='shared-runtime',runtime_id='runtime',runtime_root=marker.parent,marker_sha256=p['runtime_marker_sha256'])
    with pytest.raises(host.HostAuthorizationError,match='new'):
        host.issue_execution_grant(CID,expected_policy_path=file,key_path=key,grant_path=grants/'grant.json',grant_dir=grants,role='production')
    p['recovery'].pop('expected_network_id')
    file.write_bytes(host._canonical(p))
    with pytest.raises(host.HostAuthorizationError,match='expected network object ID'):
        host.issue_execution_grant(CID,expected_policy_path=file,key_path=key,
            grant_path=grants/'missing-network.json',grant_dir=grants,role='production')


@pytest.mark.parametrize('fault',[None,'network-policy-changed','grant-tampered','not-running'])
def test_recovery_post_start_requires_signed_grant_network_commitment(tmp_path,monkeypatch,fault):
    p,c,image,rendered,file,key,grants,manifest,marker,release,identity=signing_fixture(
        tmp_path,monkeypatch,'production',2)
    p['recovery']=recovery_policy()['recovery']
    p['recovery']['container']=p['service_id']+'-recovery-'+p['recovery']['nonce']
    c['Config']['Hostname']=p['recovery']['nonce']
    rendered['services'][p['service_id']]['hostname']=p['recovery']['nonce']
    p['rendered_compose_sha256']=digest(rendered)
    p['actual_config_sha256']=host.normalize_observation(c,image,json.loads(release.read_bytes()))['actual_config_sha256']
    original=host._recovery_call
    monkeypatch.setattr(host,'_recovery_call',lambda name,*args:
        original(name,*args) if name in ('validate','grant_id') else None)
    file.write_bytes(host._canonical(p))
    grant_file=grants/'grant.json'
    envelope=host.issue_execution_grant(CID,expected_policy_path=file,key_path=key,
        grant_path=grant_file,grant_dir=grants,role='production')
    assert envelope['payload']['grant_id']==original('grant_id',p,CID)
    grant_source=next(m['source'] for m in p['mounts'] if m['target']==p['grant_container_directory'])
    previous_path=host.Path
    monkeypatch.setattr(host,'Path',lambda value:grants if str(value)==grant_source else previous_path(value))
    c['State']=dict(Status='running',Running=True)
    def network(name,*args):
        if name in ('validate','grant_id'):return original(name,*args)
        if name=='validate_instance':
            if fault=='not-running':raise host.HostAuthorizationError('post-start requires running')
            return dict(RUNTIME_NETWORK_IDENTITY_ASSERTION='PASS',
                expected_network_id=p['recovery']['expected_network_id'],network=p['recovery']['network'],
                container_id=CID)
        return None
    monkeypatch.setattr(host,'_recovery_call',network)
    monkeypatch.setattr(host,'_render_actual_compose',lambda *args,**kwargs:p['rendered_compose_sha256'])
    if fault=='network-policy-changed':
        p['recovery']['expected_network_id']='f'*64
        file.write_bytes(host._canonical(p))
    elif fault=='grant-tampered':
        broken=json.loads(grant_file.read_bytes());broken['payload']['grant_id']='f'*32
        grant_file.chmod(0o600)
        grant_file.write_bytes(host._canonical(broken))
    if fault:
        with pytest.raises(host.HostAuthorizationError):
            host.validate_recovery_post_start(CID,expected_policy_path=file,grant_path=grant_file,key_path=key)
    else:
        result=host.validate_recovery_post_start(CID,expected_policy_path=file,
            grant_path=grant_file,key_path=key)
        assert result['POST_START_NETWORK_IDENTITY_VALIDATION']=='PASS'
        assert result['expected_network_id']==p['recovery']['expected_network_id']


def test_post_start_cli_validates_existing_grant_without_issuing_another(monkeypatch,capsys):
    calls=[]
    monkeypatch.setattr(host,'validate_recovery_post_start',lambda cid,**kwargs:
        calls.append((cid,kwargs)) or {'POST_START_NETWORK_IDENTITY_VALIDATION':'PASS'})
    monkeypatch.setattr(host,'issue_execution_grant',lambda *args,**kwargs:
        pytest.fail('post-start validation must not issue a new grant'))
    code=host.main(['--validate-recovery-post-start','--container-id',CID,'--policy','/policy.json',
        '--key','/key','--grant-directory','/grants','--grant','/grants/grant.json',
        '--role','production'])
    assert code==0 and calls==[(CID,dict(expected_policy_path='/policy.json',
        grant_path='/grants/grant.json',key_path='/key'))]
    assert json.loads(capsys.readouterr().out)['POST_START_NETWORK_IDENTITY_VALIDATION']=='PASS'


@pytest.fixture
def recovery_bridge(recovery_compose,monkeypatch):
    m,p,desired,old,prod=recovery_compose
    # Exact source Compose render and retained render come from separate calls.
    source=copy.deepcopy(desired)
    source['services'][p['service_id']]['working_dir']='/app'
    # Add matching working dir to the retained fixture through its transport.
    raw=host._run_docker([]);baseline=json.loads(raw)
    baseline['services'][p['service_id']]['working_dir']='/app'
    old['rendered_compose_sha256']=digest(baseline)
    monkeypatch.setattr(host,'_run_docker',lambda args:host._canonical(source if '--project-name' in args else baseline))
    monkeypatch.setattr(host,'_recovery_call',lambda name,*args:getattr(m,name)(host,*args))
    actual=m.project_compose(host,source,p)
    actual['services'][p['service_id']]['hostname']=p['recovery']['nonce']
    manifest=dict(build={'compose_sources':['compose.yml']},entrypoint=['streamlit','run','page.py'],working_directory='/app',required_environment=['BUSINESS'],secret_references=[])
    return m,p,actual,manifest


def test_recovery_projection_is_used_by_production_compose_bridge(recovery_bridge):
    _,p,actual,manifest=recovery_bridge
    assert host._production_compose_bridge(actual,p,manifest)==[]
    actual['services'][p['service_id']]['environment']['BUSINESS']='override'
    with pytest.raises(host.HostAuthorizationError):host._production_compose_bridge(actual,p,manifest)


@pytest.mark.parametrize('mutation',['internal-absent','ipam-empty','both'])
def test_recovery_final_compose_delegates_effective_network_defaults(recovery_bridge,mutation):
    _,p,actual,manifest=recovery_bridge
    network=actual['networks']['default']
    if mutation in ('internal-absent','both'):network.pop('internal')
    if mutation in ('ipam-empty','both'):network['ipam']={}
    assert host._production_compose_bridge(actual,p,manifest)==[]


@pytest.mark.parametrize('mutation',[
    'custom-subnet','custom-ipam-options','internal-true','custom-driver',
    'production-network-name','production-network-join','extra-network',
    'public-bind','environment','command','mount-source','mount-rw','security-option',
])
def test_recovery_final_compose_rejects_semantic_or_nonnetwork_change(recovery_bridge,mutation):
    _,p,actual,manifest=recovery_bridge
    network=actual['networks']['default'];service=actual['services'][p['service_id']]
    if mutation=='custom-subnet':network['ipam']={'config':[{'subnet':'172.20.0.0/16'}]}
    elif mutation=='custom-ipam-options':network['ipam']={'options':{'custom':'yes'}}
    elif mutation=='internal-true':network['internal']=True
    elif mutation=='custom-driver':network['driver']='overlay'
    elif mutation=='production-network-name':network['name']='production_default'
    elif mutation=='production-network-join':service['networks']['production_default']=None
    elif mutation=='extra-network':actual['networks']['other']={'name':'other'}
    elif mutation=='public-bind':service['ports'][0]['host_ip']='0.0.0.0'
    elif mutation=='environment':service['environment']['BUSINESS']='different'
    elif mutation=='command':service['command']=['shell']
    elif mutation=='mount-source':service['volumes'][0]['source']='/other/history'
    elif mutation=='mount-rw':service['volumes'][0]['read_only']=False
    elif mutation=='security-option':service['security_opt']=['no-new-privileges:false']
    with pytest.raises(host.HostAuthorizationError):host._production_compose_bridge(actual,p,manifest)


@pytest.mark.parametrize('validator_result',[None,False])
def test_recovery_final_compose_requires_explicit_network_validator_pass(
        recovery_bridge,monkeypatch,validator_result):
    _,p,actual,manifest=recovery_bridge
    original=host._recovery_call
    monkeypatch.setattr(host,'_recovery_call',lambda name,*args:
        validator_result if name=='validate_projected_network' else original(name,*args))
    with pytest.raises(host.HostAuthorizationError,match='network semantic validation did not pass'):
        host._production_compose_bridge(actual,p,manifest)


def test_real_recovery_compose_network_representation_replay(recovery_compose,monkeypatch):
    m,p,desired,old,_=recovery_compose
    old_network_name='spread-e42a61e-20260908-b01_default'
    baseline=json.loads(host._run_docker([]))
    baseline['name']='spread-e42a61e-20260908-b01'
    baseline['networks']['default']={'name':old_network_name,'ipam':{}}
    baseline['services'][p['service_id']]['working_dir']='/app'
    old['rendered_compose_sha256']=digest(baseline)
    source=copy.deepcopy(desired)
    source['networks']['default']['ipam']={}
    source['services'][p['service_id']]['working_dir']='/app'
    monkeypatch.setattr(host,'_run_docker',lambda args:host._canonical(source if '--project-name' in args else baseline))
    monkeypatch.setattr(host,'_recovery_call',lambda name,*args:getattr(m,name)(host,*args))
    actual=m.project_compose(host,source,p)
    assert m.semantic_network(host,actual['networks']['default'])==dict(
        name=p['recovery']['network'],driver='bridge',internal=False)
    actual['networks']['default'].pop('internal')
    actual['networks']['default']['ipam']={}
    actual['services'][p['service_id']]['hostname']=p['recovery']['nonce']
    manifest=dict(build={'compose_sources':['compose.yml']},
        entrypoint=['streamlit','run','page.py'],working_directory='/app',
        required_environment=['BUSINESS'],secret_references=[])
    non_network_expected=m.project_compose(host,source,p)
    non_network_actual=copy.deepcopy(actual)
    for document in (non_network_expected,non_network_actual):
        document.pop('networks')
        document['services'][p['service_id']].pop('hostname',None)
    assert non_network_expected==non_network_actual  # zero non-network differences
    assert host._production_compose_bridge(actual,p,manifest)==[]


def test_recovery_changed_preservation_or_production_observation_prevents_sealing(tmp_path,monkeypatch):
    p,c,image,rendered,file,key,grants,manifest,marker,release,identity=signing_fixture(tmp_path,monkeypatch,'production',2)
    p['recovery']=recovery_policy()['recovery'];p['recovery']['container']=p['service_id']+'-recovery-'+p['recovery']['nonce']
    original=host._recovery_call
    monkeypatch.setattr(host,'_recovery_call',lambda name,*args:original(name,*args) if name in ('validate','grant_id') else None)
    calls=[]
    manifest_data=json.loads(manifest.read_bytes())
    def baseline(policy):
        calls.append(True)
        return ({'preserved_stores':'before' if len(calls)==1 else 'changed'},manifest_data)
    monkeypatch.setattr(host,'_validated_candidate_record',baseline)
    file.write_bytes(host._canonical(p))
    with pytest.raises(host.HostAuthorizationError,match='record changed'):
        host.issue_execution_grant(CID,expected_policy_path=file,key_path=key,grant_path=grants/'grant.json',grant_dir=grants,role='production')
    assert not (grants/'grant.json').exists()
