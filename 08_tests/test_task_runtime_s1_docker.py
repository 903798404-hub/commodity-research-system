"""Opt-in real Linux Docker test: RUN_TASK_RUNTIME_DOCKER_E2E=1 pytest ..."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("task_runtime_s1_e2e", ROOT / "09_deploy/task_runtime/launcher.py")
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


@pytest.mark.skipif(os.name != "posix" or os.environ.get("RUN_TASK_RUNTIME_DOCKER_E2E") != "1",
                    reason="explicit Hosted Linux Docker opt-in required")
def test_real_docker_ephemeral_isolation(tmp_path, monkeypatch):
    fixture = ROOT / "09_deploy/task_runtime/fixtures"
    commit, tree = "a" * 40, "b" * 40
    tag = "market-data-s1-e2e:" + tmp_path.name.lower().replace("_", "-")
    subprocess.run(["docker", "build", "--label", f"org.opencontainers.image.revision={commit}",
                    "--label", f"market-data.git.tree={tree}", "-t", tag, str(fixture)],
                   check=True, timeout=600, capture_output=True)
    try:
        image = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                               check=True, capture_output=True, text=True).stdout.strip()
        inputs, snapshots = tmp_path / "inputs", tmp_path / "snapshots"
        inputs.mkdir()
        snapshots.mkdir()
        snapshots.chmod(0o777)  # isolated fake fixture; production uses dedicated ownership/ACL
        (inputs / "request.json").write_text("fake-request")
        secret = tmp_path / "tankan.env"
        secret.write_text("FAKE_TANKAN=only-for-e2e")
        secret.chmod(0o644)  # fake Hosted secret; real secret uses dedicated-group ACL
        forbidden = tmp_path / "forbidden"
        forbidden.mkdir()
        (forbidden / "marker").write_text("private")
        record = tmp_path / "approved.json"
        record.write_text(json.dumps(dict(schema_version="approved-production-image/1",
             approved_commit=commit, approved_tree=tree,
             image_identity_kind="LOCAL_DOCKER_IMAGE_ID", image_identity=image)))
        # A GitHub runner cannot chown the fixture to root; production reader
        # ownership is tested separately and must not be bypassed on the host.
        monkeypatch.setattr(module, "read_host_record", lambda path:
                            module.ApprovedImage.from_record(json.loads(path.read_text())))
        policy = module.TaskPolicy("soybean_capture", "LOCAL_DOCKER_IMAGE_ID", image,
            ("python", "-B", "/opt/s1/probe.py"), 24051, 24051,
            (module.Mount(inputs, "/runtime/inputs", True),
             module.Mount(snapshots, "/runtime/snapshots", False)),
            module.Mount(secret, "/run/secrets/tankan.env", True), "none")
        launcher = module.HostLauncher(approval_path=record, policies={"soybean_capture": policy},
                                      receipt_root=tmp_path / "receipts")
        receipt = launcher.run_task("soybean_capture", {"business_date": "2026-09-25", "session": "AM"})
        assert receipt["result_status"] == "SUCCESS", receipt
        assert (snapshots / "probe-output").read_text() == "PASS"
        assert not (inputs / "should-not-write").exists()
        inspect = subprocess.run(["docker", "container", "inspect", receipt["container_id"]], capture_output=True)
        assert inspect.returncode != 0  # removed after receipt
    finally:
        subprocess.run(["docker", "image", "rm", tag], capture_output=True, check=True, timeout=120)
