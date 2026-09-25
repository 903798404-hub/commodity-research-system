"""S1 contract tests. Real Docker evidence belongs to Hosted Linux admission."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("task_runtime_s1", ROOT / "09_deploy/task_runtime/launcher.py")
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)

COMMIT = "a" * 40
TREE = "b" * 40
IMAGE = "sha256:" + "c" * 64
OTHER_IMAGE = "sha256:" + "d" * 64
CID = "e" * 64


def approval(image=IMAGE):
    return dict(schema_version="approved-production-image/1", approved_commit=COMMIT,
                approved_tree=TREE, image_identity_kind="LOCAL_DOCKER_IMAGE_ID", image_identity=image)


def policy(tmp_path, image=IMAGE):
    inputs = tmp_path / "inputs"
    output = tmp_path / "snapshots"
    secret = tmp_path / "tankan.env"
    inputs.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)
    secret.write_text("FAKE=only-for-test", encoding="utf-8")
    return module.TaskPolicy("soybean_capture", "LOCAL_DOCKER_IMAGE_ID", image,
                             ("python", "-B", "/app/probe.py"), 24051, 24051,
                             (module.Mount(inputs, "/runtime/inputs", True),
                              module.Mount(output, "/runtime/snapshots", False)),
                             module.Mount(secret, "/run/secrets/tankan.env", True), "none")


class FakeDocker:
    def __init__(self, *, exit_code=0, fail_on=None):
        self.calls = []
        self.exit_code = exit_code
        self.fail_on = fail_on
        self.policy = None

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == self.fail_on:
            raise RuntimeError("fake Docker failure")
        if args[:2] == ["image", "inspect"]:
            if "--format" in args:
                return IMAGE
            return json.dumps([{"Id": IMAGE, "Config": {"Labels": {
                "org.opencontainers.image.revision": COMMIT, "market-data.git.tree": TREE}}}])
        if args[0] == "create":
            return CID
        if args[0] == "wait":
            return str(self.exit_code)
        if args[0] == "inspect":
            mounts = [{"Source": str(m.source), "Destination": m.target, "RW": not m.read_only}
                      for m in (*self.policy.mounts, self.policy.secret)]
            return json.dumps([{"Image": IMAGE, "HostConfig": {"ReadonlyRootfs": True,
                "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"], "NetworkMode": "none"},
                "Config": {"User": "24051:24051", "Entrypoint": ["python"],
                           "Cmd": ["-B", "/app/probe.py"]}, "Mounts": mounts}])
        return ""


def launcher(tmp_path, *, image=IMAGE, docker=None):
    record = tmp_path / "approved.json"
    record.write_text(json.dumps(approval()), encoding="utf-8")
    fake = docker or FakeDocker()
    task_policy = policy(tmp_path, image)
    fake.policy = task_policy
    result = module.HostLauncher(approval_path=record, policies={"soybean_capture": task_policy},
                                 receipt_root=tmp_path / "receipts", docker=fake)
    return result, fake


def test_approval_contract_distinguishes_local_id_and_registry_digest():
    assert module.ApprovedImage.from_record(approval()).image_identity_kind == "LOCAL_DOCKER_IMAGE_ID"
    with pytest.raises(module.PolicyError):
        module.ApprovedImage.from_record({**approval(), "image_identity_kind": "REGISTRY_DIGEST"})
    digest = {**approval(), "image_identity_kind": "REGISTRY_DIGEST",
              "image_identity": "example.org/market-data@sha256:" + "f" * 64}
    assert module.ApprovedImage.from_record(digest).image_identity_kind == "REGISTRY_DIGEST"
    with pytest.raises(module.PolicyError):
        module.ApprovedImage.from_record({**approval(), "image_identity": "latest"})


def test_success_receipt_security_flags_and_cleanup(tmp_path):
    host, fake = launcher(tmp_path)
    receipt = host.run_task("soybean_capture", {"business_date": "2026-09-25", "session": "AM"})
    assert receipt["result_status"] == "SUCCESS"
    assert receipt["exit_code"] == 0 and receipt["container_id"] == CID
    assert receipt["output_identity"] is None  # no snapshot reconciliation in S1
    assert receipt["approved_commit"] == COMMIT and receipt["approved_tree"] == TREE
    assert receipt["image_identity_kind"] == "LOCAL_DOCKER_IMAGE_ID"
    create = next(call for call in fake.calls if call[0] == "create")
    assert create[-5:] == ["--entrypoint", "python", IMAGE, "-B", "/app/probe.py"]
    assert create[create.index("--entrypoint") + 1] == "python"
    for flag in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pull=never"):
        assert flag in create
    assert create[create.index("--user") + 1] == "24051:24051"
    assert "/var/run/docker.sock" not in " ".join(create)
    assert not any("FAKE=only-for-test" in part for part in create)
    assert ["rm", "-f", CID] in fake.calls
    saved = json.loads((tmp_path / "receipts" / (receipt["task_id"] + ".json")).read_text())
    assert saved == receipt


@pytest.mark.parametrize("exit_code,status", [(7, "TASK_FAILED"), (0, "SUCCESS")])
def test_exit_status(tmp_path, exit_code, status):
    host, _ = launcher(tmp_path, docker=FakeDocker(exit_code=exit_code))
    assert host.run_task("soybean_capture", {"business_date": "2026-09-25", "session": "PM"})["result_status"] == status


def test_unapproved_image_never_starts(tmp_path):
    host, fake = launcher(tmp_path, image=OTHER_IMAGE)
    receipt = host.run_task("soybean_capture", {"business_date": "2026-09-25", "session": "AM"})
    assert receipt["result_status"] == "IMAGE_NOT_APPROVED"
    assert not fake.calls


def test_caller_cannot_set_image_command_mount_uid(tmp_path):
    host, fake = launcher(tmp_path)
    for extra in ("image", "command", "mount", "uid", "secret"):
        identity = {"business_date": "2026-09-25", "session": "AM", extra: "evil"}
        receipt = host.run_task("soybean_capture", identity)
        assert receipt["result_status"] == "POLICY_REJECTED"
        assert receipt["business_identity"] is None and "evil" not in json.dumps(receipt)
    assert not fake.calls


def test_bad_policy_rejected_before_docker(tmp_path):
    host, fake = launcher(tmp_path)
    good = host._policies["soybean_capture"]
    for bad in (module.TaskPolicy(**{**good.__dict__, "uid": 0}),
                module.TaskPolicy(**{**good.__dict__, "image_identity": "latest"}),
                module.TaskPolicy(**{**good.__dict__, "mounts": good.mounts +
                    (module.Mount(Path("/var/run/docker.sock"), "/var/run/docker.sock", False),)})):
        host._policies["soybean_capture"] = bad
        assert host.run_task("soybean_capture", {"business_date": "2026-09-25", "session": "AM"})["result_status"] == "POLICY_REJECTED"
    assert not fake.calls


def test_launch_failure_receipt_and_cleanup(tmp_path):
    host, fake = launcher(tmp_path, docker=FakeDocker(fail_on="start"))
    receipt = host.run_task("soybean_capture", {"business_date": "2026-09-25", "session": "AM"})
    assert receipt["result_status"] == "RUNTIME_FAILED"
    assert ["rm", "-f", CID] in fake.calls
    host, _ = launcher(tmp_path, docker=FakeDocker(fail_on="create"))
    receipt = host.run_task("soybean_capture", {"business_date": "2026-09-25", "session": "AM"})
    assert receipt["result_status"] == "LAUNCH_FAILED"


def test_main_does_not_auto_promote(tmp_path):
    assert "MARKET_DATA_FULL_DAILY_PRODUCTION_COMMIT" not in (ROOT / "09_deploy/task_runtime/launcher.py").read_text()


def test_host_policy_file_is_fixed_and_deterministic(tmp_path):
    configured = policy(tmp_path)
    row = dict(image_identity_kind=configured.image_identity_kind,
               image_identity=configured.image_identity, command=list(configured.command),
               uid=configured.uid, gid=configured.gid, network=configured.network,
               mounts=[dict(source=str(m.source), target=m.target, read_only=m.read_only)
                       for m in configured.mounts],
               secret=dict(source=str(configured.secret.source), target=configured.secret.target,
                           read_only=True), tmpfs=[])
    path = tmp_path / "policies.json"
    path.write_text(json.dumps({"schema_version": "host-task-policies/1",
                                "tasks": {"soybean_capture": row}}))
    first = module.read_host_policies(path)["soybean_capture"]
    second = module.read_host_policies(path)["soybean_capture"]
    assert first == second == configured
    row["command"] = ["/bin/sh", "-c", "evil"]
    assert first.command != tuple(row["command"])  # caller cannot mutate loaded policy


def test_approval_record_is_immutable_for_data_task(tmp_path):
    host, _ = launcher(tmp_path)
    record = tmp_path / "approved.json"
    original = record.read_bytes()
    host.run_task("soybean_capture", {"business_date": "2026-09-25", "session": "AM"})
    assert record.read_bytes() == original


def test_config_drift_rejected_before_start(tmp_path):
    class DriftDocker(FakeDocker):
        def __call__(self, args):
            result = super().__call__(args)
            if args[0] == "inspect":
                payload = json.loads(result)
                payload[0]["HostConfig"]["ReadonlyRootfs"] = False
                return json.dumps(payload)
            return result

    host, fake = launcher(tmp_path, docker=DriftDocker())
    receipt = host.run_task("soybean_capture", {"business_date": "2026-09-25", "session": "AM"})
    assert receipt["result_status"] == "RUNTIME_FAILED"
    assert not any(call[0] == "start" for call in fake.calls)
    assert ["rm", "-f", CID] in fake.calls


def test_runtime_oom_is_not_task_failure(tmp_path):
    class OomDocker(FakeDocker):
        def __call__(self, args):
            result = super().__call__(args)
            if args[0] == "inspect" and any(call[0] == "wait" for call in self.calls):
                payload = json.loads(result)
                payload[0]["State"] = {"OOMKilled": True}
                return json.dumps(payload)
            return result

    host, _ = launcher(tmp_path, docker=OomDocker(exit_code=137))
    receipt = host.run_task("soybean_capture", {"business_date": "2026-09-25", "session": "AM"})
    assert receipt["result_status"] == "RUNTIME_FAILED"
    assert receipt["exit_code"] == 137
