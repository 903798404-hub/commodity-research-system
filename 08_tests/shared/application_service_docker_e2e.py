"""Required Hosted Linux evidence for the real application-service Docker path.

This runs from a root-owned copy of the candidate on an ephemeral GitHub runner.
All Docker objects and bind sources are test-only; no production authority is read.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))
HOST_PATH = ROOT / "09_deploy/runtime_identity/host_authorization.py"
spec = importlib.util.spec_from_file_location("hosted_service_authorization", HOST_PATH)
assert spec and spec.loader
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)

SERVICE = "hosted-service-evidence"
MODULE = "shared-intraday"
RUNTIME = "hosted-runtime"
UID = GID = 65532
CONTAINER_ROOT = "/runtime/candidate"
SECRET_TARGET = "/run/secrets/market-data-service.json"
GRANT_TARGET = "/run/market-data-grants"


def run(*args: str, timeout: int = 120, check: bool = True) -> str:
    value = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    if check and value.returncode:
        raise RuntimeError(f"command failed ({value.returncode}): {args[:3]}: {value.stderr[-1200:]}")
    return value.stdout.strip()


def write(path: Path, raw: bytes, mode: int = 0o600) -> None:
    path.write_bytes(raw)
    path.chmod(mode)


def canonical(value: object) -> bytes:
    return host._canonical(value)


def scoped_directory(path: Path, mode: int, uid: int = 0, gid: int = 0) -> None:
    path.mkdir(mode=mode)
    os.chown(path, uid, gid)
    path.chmod(mode)


def make_scope() -> tuple[Path, dict, list[dict], Path]:
    parent = Path(host.CANDIDATE_SCOPE_PARENT)
    parent.mkdir(mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    descriptor_dir = parent / ".candidate-scope-descriptors"
    descriptor_dir.mkdir(mode=0o700, exist_ok=True)
    descriptor_dir.chmod(0o700)
    scope_id = uuid.uuid4().hex
    root = parent / f"candidate-{scope_id}"
    scoped_directory(root, 0o700)
    identity = root / "identity"
    scoped_directory(identity, 0o755)
    allowed = root / "authorized"
    denied = root / "unauthorized"
    scoped_directory(allowed, 0o770, UID, GID)
    scoped_directory(denied, 0o770, UID, GID)
    for name in ("public-current", "full-daily", "other-commodity"):
        scoped_directory(denied / name, 0o770, UID, GID)
    private = root / "service-private"
    scoped_directory(private, 0o700)
    secret = private / "service.json"
    write(secret, b"", 0o440)
    os.chown(secret, 0, GID)
    marker = {"schema_version": 1, "runtime_id": RUNTIME,
              "classification": "candidate-validation", "module_id": MODULE,
              "created_at": datetime.now(timezone.utc).isoformat()}
    write(identity / ".market-data-runtime.json", canonical(marker), 0o644)
    bindings = [
        (identity / ".market-data-runtime.json", CONTAINER_ROOT + "/.market-data-runtime.json", True),
        (allowed, CONTAINER_ROOT + "/authorized", False),
        (denied, CONTAINER_ROOT + "/unauthorized", False),
        (secret, SECRET_TARGET, True),
    ]
    records = []
    for source, target, readonly in bindings:
        item = source.stat()
        records.append({"source": str(source), "device": item.st_dev,
                        "inode": item.st_ino, "target": target, "read_only": readonly})
    now = datetime.now(timezone.utc)
    state = root.stat()
    descriptor = {"schema_version": "candidate-scope/1", "scope_id": scope_id,
                  "created_at": now.isoformat(),
                  "expires_at": (now + timedelta(minutes=45)).isoformat(),
                  "root": {"path": str(root), "device": state.st_dev, "inode": state.st_ino},
                  "binds": records}
    descriptor_path = descriptor_dir / f"{scope_id}.json"
    raw = canonical(descriptor)
    write(descriptor_path, raw)
    scope = {"descriptor_path": str(descriptor_path),
             "descriptor_sha256": hashlib.sha256(raw).hexdigest(), "scope_id": scope_id}
    mounts = [{"source": str(source), "target": target, "read_only": readonly}
              for source, target, readonly in bindings]
    return root, scope, mounts, secret


def build_image(work: Path, commit: str, tree: str) -> tuple[str, str, str]:
    shutil.copytree(ROOT / "03_src", work / "03_src")
    release = {"release_id": f"hosted-{uuid.uuid4().hex}", "git_commit": commit,
               "git_tree": tree, "application": "hosted-service-evidence"}
    release_raw = canonical(release)
    write(work / "RELEASE.json", release_raw, 0o644)
    # The issuer binds this exact in-image manifest hash; application write
    # authorization remains the unmodified production runtime_context module.
    manifest_raw = canonical({"schema_version": "runtime-manifest/3",
                              "module_id": MODULE, "service_id": SERVICE})
    write(work / "runtime-manifest.json", manifest_raw, 0o644)
    dockerfile = f"""FROM python:3.12-slim
RUN pip install --no-cache-dir cryptography==46.0.3
RUN mkdir -p /runtime/candidate/authorized /runtime/candidate/unauthorized /run/secrets /run/market-data-grants
RUN touch /runtime/candidate/.market-data-runtime.json /run/secrets/market-data-service.json
COPY 03_src/ /app/03_src/
COPY RELEASE.json /app/RELEASE.json
COPY runtime-manifest.json /app/runtime-manifest.json
WORKDIR /app
ENV PYTHONPATH=/app/03_src
LABEL org.opencontainers.image.revision={commit} market-data.git.tree={tree} market-data.service={SERVICE} market-data.artifact.promotable=true market-data.artifact.origin=candidate market-data.release.id={release['release_id']}
USER {UID}:{GID}
ENTRYPOINT ["python", "-c", "import time; time.sleep(600)"]
"""
    write(work / "Dockerfile.service-evidence", dockerfile.encode(), 0o644)
    image_name = f"hosted-service-identity-{uuid.uuid4().hex}:evidence"
    run("docker", "build", "--pull", "--file", str(work / "Dockerfile.service-evidence"),
        "--tag", image_name, str(work), timeout=900)
    image_id = run("docker", "image", "inspect", "--format", "{{.Id}}", image_name)
    assert image_id.startswith("sha256:") and len(image_id) == 71
    return image_id, hashlib.sha256(release_raw).hexdigest(), hashlib.sha256(manifest_raw).hexdigest()


def compose_instance(work: Path, image: str, mounts: list[dict], grant_dir: Path,
                     *, suffix: str) -> tuple[str, Path, Path, str]:
    hostname = uuid.uuid4().hex
    source = work / f"compose-{suffix}.json"
    env_file = work / f"compose-{suffix}.env"
    write(env_file, b"", 0o600)
    volumes = [{"type": "bind", "source": item["source"], "target": item["target"],
                "read_only": item["read_only"]} for item in mounts]
    volumes.append({"type": "bind", "source": str(grant_dir), "target": GRANT_TARGET,
                    "read_only": True})
    compose = {"services": {SERVICE: {"image": image, "user": f"{UID}:{GID}",
               "hostname": hostname, "working_dir": "/app", "read_only": True,
               "entrypoint": ["python", "-c", "import time; time.sleep(600)"],
               "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
               "environment": {"PYTHONDONTWRITEBYTECODE": "1",
                               "MARKET_DATA_EXECUTION_GRANT": GRANT_TARGET + "/grant.json"},
               "volumes": volumes}}}
    write(source, canonical(compose), 0o644)
    project = f"hosted-service-{uuid.uuid4().hex[:12]}"
    command = ["docker", "compose", "--project-name", project,
               "--project-directory", str(work), "--env-file", str(env_file),
               "-f", str(source)]
    run(*command, "up", "--no-start", "--no-build", "--pull", "never", SERVICE)
    cid = run(*command, "ps", "-aq", SERVICE)
    assert len(cid) == 64 and run("docker", "inspect", "--format", "{{.State.Status}}", cid) == "created"
    return cid, source, env_file, project


def policy_for(cid: str, source: Path, env_file: Path, scope_root: Path, scope: dict,
               image: str, release_sha: str, manifest_sha: str, commit: str, tree: str,
               work: Path, grant_dir: Path) -> tuple[dict, Path]:
    container = host.docker_inspect(cid)
    image_data = host.docker_image_inspect(image)
    release = host.copy_container_json(cid)
    observation = host.normalize_observation(container, image_data, release)
    rendered = host._json(host._run_docker(["compose", "--project-directory", str(work),
        "--env-file", str(env_file), "-f", str(source), "config", "--format", "json"]))
    rendered_service = rendered["services"][SERVICE]
    for rendered_key, inspect_key in (("entrypoint", "Entrypoint"), ("command", "Cmd"),
                                      ("working_dir", "WorkingDir"), ("user", "User"),
                                      ("hostname", "Hostname")):
        expected_value = rendered_service.get(rendered_key, image_data["Config"].get(inspect_key))
        actual_value = container["Config"].get(inspect_key)
        if expected_value != actual_value:
            raise RuntimeError(f"Compose fixture field {rendered_key} differs: "
                               f"rendered={expected_value!r}, inspect={actual_value!r}")
    marker = host.copy_container_bytes(cid, CONTAINER_ROOT + "/.market-data-runtime.json")
    policy = {"schema_version": "host-runtime-policy/4", "role": "candidate_validation",
              "key_id": "hosted-key", "project_id": "hosted-service-evidence", "module_id": MODULE,
              "service_id": SERVICE, "runtime_id": RUNTIME, "approved_commit": commit,
              "approved_tree": tree, "image_id": image, "artifact_service": SERVICE,
              "release_application": "hosted-service-evidence", "source_root": "/app",
              "runtime_root": CONTAINER_ROOT, "runtime_manifest_path": "/app/runtime-manifest.json",
              "runtime_manifest_sha256": manifest_sha,
              "runtime_marker_sha256": hashlib.sha256(marker).hexdigest(),
              "release_sha256": release_sha,
              "actual_config_sha256": observation["actual_config_sha256"],
              "mounts": observation["mounts"],
              "compose_sources": [{"path": str(source),
                                   "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}],
              "compose_project_directory": str(work), "compose_environment_file": str(env_file),
              "rendered_compose_sha256": host._digest(rendered),
              "grant_container_directory": GRANT_TARGET,
              "candidate_host_root": str(scope_root), "candidate_scope": scope,
              "application_service": {"service_id": SERVICE, "runtime_id": RUNTIME,
                                      "allowed_writable_roots": [CONTAINER_ROOT + "/authorized"]}}
    path = work / f"policy-{cid[:12]}.json"
    write(path, canonical(policy))
    assert {"source": str(grant_dir), "target": GRANT_TARGET,
            "read_only": True} in observation["mounts"]
    host.validate_policy(policy, "candidate_validation")
    host._candidate_descriptor(policy, consume=False)
    host.observe_and_validate(cid, policy, role="candidate_validation")
    return policy, path


PROBE = """import json, os
from pathlib import Path
from agri_research_agent.shared.runtime_context import (
    ApplicationServiceContext, RuntimeAuthorizationError,
    assert_runtime_write, establish_application_service_context)
root = Path('/runtime/candidate')
def rejected(call):
    try: call()
    except RuntimeAuthorizationError: return True
    return False
assert rejected(lambda: ApplicationServiceContext())
assert rejected(lambda: assert_runtime_write(None, root / 'authorized' / 'x'))
ctx = establish_application_service_context(
    service_id='hosted-service-evidence', module_id='shared-intraday', runtime_root=root)
assert assert_runtime_write(ctx, root / 'authorized' / 'x') == root / 'authorized' / 'x'
for name in ('unauthorized', 'unauthorized/public-current',
             'unauthorized/full-daily', 'unauthorized/other-commodity'):
    assert rejected(lambda n=name: assert_runtime_write(ctx, root / n / 'x'))
assert rejected(lambda: establish_application_service_context(
    service_id='another-service', module_id='shared-intraday', runtime_root=root))
print(json.dumps({'context': 'PASS', 'authorized': 'PASS', 'unauthorized': 'PASS',
                  'cross_service': 'PASS', 'constructor': 'REJECTED',
                  'uid': os.geteuid(), 'gid': os.getegid()}))
"""


NEGATIVE = """import sys
from pathlib import Path
from agri_research_agent.shared.runtime_context import establish_application_service_context,RuntimeAuthorizationError
try:
    establish_application_service_context(service_id='hosted-service-evidence',
        module_id='shared-intraday',runtime_root=Path('/runtime/candidate'))
except RuntimeAuthorizationError:
    print('REJECTED')
else:
    raise SystemExit('credential unexpectedly accepted')
"""


def container_probe(cid: str, code: str) -> str:
    return run("docker", "exec", cid, "python", "-B", "-c", code)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--candidate-tree", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert sys.platform == "linux" and os.geteuid() == 0
    assert shutil.which("docker") and run("docker", "info", "--format", "{{.ServerVersion}}")
    assert len(args.candidate_commit) == len(args.candidate_tree) == 40
    work = ROOT / f"hosted-evidence-{uuid.uuid4().hex}"
    scoped_directory(work, 0o700)
    grant_dir = work / "grants"
    scoped_directory(grant_dir, 0o755)
    cid_a = cid_b = None
    image = None
    evidence = {"schema_version": "application-service-identity-docker-evidence/1",
                "candidate_commit": args.candidate_commit,
                "candidate_tree": args.candidate_tree,
                "docker_version": run("docker", "version", "--format", "{{.Server.Version}}"),
                "service_id": SERVICE, "runtime_id": RUNTIME,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "result": "STARTED"}
    try:
        authority_paths = (
            ("issuer", HOST_PATH),
            ("grant_contract", host.GRANT_CONTRACT_PATH),
            ("manifest_contract", host.MANIFEST_CONTRACT_PATH),
            ("trust_config", host.TRUST_CONFIG_PATH),
            ("recovery_contract", ROOT / "09_deploy/runtime_identity/recovery_namespace.py"),
        )
        evidence["authority_source_preflight"] = []
        for label, path in authority_paths:
            state = path.stat()
            item = {"label": label, "owner": state.st_uid,
                    "mode": oct(stat.S_IMODE(state.st_mode))}
            evidence["authority_source_preflight"].append(item)
            host._protected_path(path)
            item["result"] = "PASS"
        host.require_protected_authority_source()
        scope_root, scope, mounts, secret = make_scope()
        image, release_sha, manifest_sha = build_image(work, args.candidate_commit, args.candidate_tree)
        cid_a, source_a, env_a, project_a = compose_instance(work, image, mounts, grant_dir, suffix="a")
        evidence["container_a_id"] = cid_a
        policy_a, policy_path_a = policy_for(cid_a, source_a, env_a, scope_root, scope,
            image, release_sha, manifest_sha, args.candidate_commit, args.candidate_tree, work, grant_dir)
        secret_state = secret.stat()
        evidence["credential_path_evidence"] = {
            "candidate_scope_root": str(scope_root),
            "credential_destination": str(secret),
            "resolved_real_path": str(secret.resolve(strict=True)),
            "owner": secret_state.st_uid,
            "group": secret_state.st_gid,
            "mode": oct(stat.S_IMODE(secret_state.st_mode)),
            "temporary_path_branch": "candidate_validation_bound_scope",
            "container_a_id": cid_a,
            "credential_readonly_mount": {"target": SECRET_TARGET, "read_only": True},
        }
        assert host._application_service_credential_path(secret, policy_a) == secret
        host.issue_application_service_credential(cid_a, expected_policy_path=policy_path_a,
            credential_path=secret, role="candidate_validation")
        raw_a = secret.read_bytes()
        payload_a = host._json(raw_a)
        evidence["credential_sha256"] = hashlib.sha256(raw_a).hexdigest()
        evidence["credential_mount_read_only"] = True
        state = secret.stat()
        evidence.update(credential_host_owner=state.st_uid, credential_host_group=state.st_gid,
                        credential_host_mode=oct(stat.S_IMODE(state.st_mode)),
                        credential_container_path=SECRET_TARGET)
        assert state.st_uid == 0 and state.st_gid == GID and stat.S_IMODE(state.st_mode) == 0o440
        assert payload_a["container_id"] == cid_a
        run("docker", "start", cid_a)
        positive = json.loads(container_probe(cid_a, PROBE))
        assert positive["uid"] == UID and positive["gid"] == GID
        evidence.update(application_uid=UID, application_gid=GID,
                        container_a_context_valid=True,
                        authorized_root_result=positive["authorized"],
                        unauthorized_root_result=positive["unauthorized"],
                        cross_service_result=positive["cross_service"],
                        unvalidated_context_result=positive["constructor"])
        # The kernel denial is confirmed with the actual process exit status.
        other = subprocess.run(["docker", "exec", "--user", "65533:65533", cid_a,
            "python", "-c", f"open({SECRET_TARGET!r},'rb').read()"], capture_output=True, text=True)
        assert other.returncode != 0 and "PermissionError" in other.stderr
        evidence["other_identity_secret_read_rejected"] = True
        for key, mutation in (
            ("invalid_credential_result", os.urandom(32).hex().encode()),
            ("service_id_mismatch_result", canonical({**payload_a, "service_id": "other-service"})),
            ("runtime_id_mismatch_result", canonical({**payload_a, "runtime_id": "other-runtime"})),
            ("production_identity_mismatch_result", canonical({**payload_a, "role": "production"})),
        ):
            secret.chmod(0o640)
            write(secret, mutation, 0o440)
            assert container_probe(cid_a, NEGATIVE) == "REJECTED"
            evidence[key] = "PASS"
        secret.chmod(0o640)
        write(secret, raw_a, 0o440)
        assert json.loads(container_probe(cid_a, PROBE))["context"] == "PASS"
        secret.chmod(0o640)
        write(secret, b"", 0o440)
        assert container_probe(cid_a, NEGATIVE) == "REJECTED"
        evidence["no_credential_result"] = "PASS"
        secret.chmod(0o640)
        write(secret, raw_a, 0o440)
        run("docker", "rm", "--force", cid_a)
        cid_a_removed = cid_a
        cid_a = None
        cid_b, source_b, env_b, _project_b = compose_instance(
            work, image, mounts, grant_dir, suffix="b")
        evidence["container_b_id"] = cid_b
        assert cid_b != cid_a_removed
        policy_b, _path_b = policy_for(cid_b, source_b, env_b, scope_root, scope,
            image, release_sha, manifest_sha, args.candidate_commit, args.candidate_tree, work, grant_dir)
        observed_b = host.observe_and_validate(cid_b, policy_b, role="candidate_validation")
        try:
            host._validate_application_service_credential(cid_b, policy_b, observed_b)
        except host.HostAuthorizationError as error:
            assert "another deployment" in str(error)
        else:
            raise AssertionError("Container B accepted Container A credential")
        evidence["container_b_old_credential_rejected"] = True
        evidence["result"] = "PASS"
    except Exception as exc:
        evidence["result"] = "FAIL"
        evidence["failure_type"] = type(exc).__name__
        evidence["failure_summary"] = str(exc)[-1200:]
        raise
    finally:
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        if cid_a:
            run("docker", "rm", "--force", cid_a, check=False)
        if cid_b:
            run("docker", "rm", "--force", cid_b, check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
