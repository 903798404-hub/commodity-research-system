"""Hosted Linux reproduction of the application/tooling UID split.

This fixture uses a disposable Docker container and store.  It never connects
to a deployment host or reads production data.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agri_research_agent.pipelines.public_data_delivery import (
    activate_incoming_server_package,
    resolve_server_current,
    rollback_server_current_after_application_failure,
    sync_to_local_server_store,
)
from agri_research_agent.shared.file_identity import identify_file


ROOT = Path(__file__).resolve().parents[2]
TRANSPORT = ROOT / "04_scripts" / "transfer_public_data_package.py"
FIXTURE = ROOT / "08_tests" / "pipelines" / "application_runtime_accident_fixture.py"


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=120)


def _load_transport():
    spec = importlib.util.spec_from_file_location("readability_docker_transport", TRANSPORT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _set_acl(path: Path, entry: str) -> None:
    _run("setfacl", "-m", entry, str(path))


def _package_paths(package: Path) -> tuple[list[Path], list[Path]]:
    return (
        [package, *(path for path in package.rglob("*") if path.is_dir())],
        [path for path in package.rglob("*") if path.is_file()],
    )


def _restrict_package(package: Path, *, uid: int | None) -> None:
    directories, files = _package_paths(package)
    for path in directories:
        _run("setfacl", "-b", "-k", str(path))
        path.chmod(0o750)
        _set_acl(path, f"u:{0 if uid is None else uid}:r-x")
        _set_acl(path, "m::r-x")
        assert stat.S_IMODE(path.stat().st_mode) == 0o750
    for path in files:
        _run("setfacl", "-b", str(path))
        path.chmod(0o640)
        _set_acl(path, f"u:{0 if uid is None else uid}:r--")
        _set_acl(path, "m::r--")
        assert stat.S_IMODE(path.stat().st_mode) == 0o640


def _identity(package: object) -> dict[str, str]:
    return {
        "id": package.package_id,
        "artifact_sha256": package.manifest["delivery_artifacts"]["domestic-spread"]["sha256"],
        "manifest_sha256": identify_file(package.directory / "manifest.json").sha256,
    }


@pytest.mark.skipif(sys.platform != "linux", reason="Docker/POSIX ACL kernel fixture runs on hosted Linux")
def test_real_docker_runtime_uid_and_root_only_acl_before_current_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A real, independently selected application principal; never derive it
    # from the image default, historical 65532, or the tooling runner UID.
    assert shutil.which("docker") and shutil.which("setfacl") and shutil.which("getfacl")
    application_uid = 30_000 + int(uuid.uuid4().hex[:4], 16) % 20_000
    application_gid = application_uid
    assert application_uid not in {0, os.getuid(), 65532}
    fixture_spec = importlib.util.spec_from_file_location("readability_accident_fixture", FIXTURE)
    assert fixture_spec and fixture_spec.loader
    fixture = importlib.util.module_from_spec(fixture_spec)
    fixture_spec.loader.exec_module(fixture)
    work = tmp_path / "fixture"
    work.mkdir()
    old, candidate = fixture.build_disposable_packages(work)
    store = tmp_path / "server-store"
    store.mkdir()
    store.chmod(0o750)
    _set_acl(store, f"u:{application_uid}:r-x,d:u:{application_uid}:r-x")
    initial = sync_to_local_server_store(old.directory, store_root=store)
    assert initial.status == "SYNCED"
    package = store / "incoming" / candidate.package_id
    shutil.copytree(candidate.directory, package)
    for directory in (store, store / "incoming", store / "releases"):
        directory.chmod(0o755)
    _restrict_package(store / "releases" / old.package_id, uid=application_uid)
    _restrict_package(package, uid=None)
    pointer = store / "current.json"
    pointer.chmod(0o640)
    _set_acl(pointer, f"u:{application_uid}:r--")
    before_pointer = pointer.read_bytes()

    name = f"readability-incident-{uuid.uuid4().hex[:12]}"
    prefix = Path(sys.prefix).resolve()
    assert prefix.is_dir() and (prefix / "bin" / "python").is_file()
    container_store = "/runtime/public-data-server-store"
    docker_args = [
        "docker", "run", "--detach", "--name", name,
        "--label", "market-data.service=spread-dashboard",
        "--user", f"{application_uid}:{application_gid}",
        "--mount", f"type=bind,src={store.resolve()},dst={container_store},readonly",
        "--mount", f"type=bind,src={ROOT.resolve()},dst=/app,readonly",
        "--mount", f"type=bind,src={prefix},dst={prefix},readonly",
        "--env", f"PATH={prefix}/bin:/usr/local/bin:/usr/bin:/bin",
        "--env", "PYTHONPATH=/app/03_src",
        "--env", f"PUBLIC_DATA_SERVER_STORE_ROOT={container_store}",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "MPLCONFIGDIR=/tmp/matplotlib",
    ]
    if Path("/usr/share/fonts").is_dir():
        docker_args.extend(["--mount", "type=bind,src=/usr/share/fonts,dst=/usr/share/fonts,readonly"])
    docker_args.extend(["python:3.12-slim", "sleep", "600"])
    _run(*docker_args)
    evidence: dict[str, object] = {
        "schema_version": "application-runtime-incident-linux/1",
        "root_probe_uid": 0,
        "tooling_uid": None,
        "application_uid": application_uid,
        "application_gid": application_gid,
        "store": "disposable",
        "image": "python:3.12-slim",
    }
    try:
        # Only the SSH transport is adapted to localhost.  The production
        # discovery function still executes real docker ps/inspect/exec.
        transport = _load_transport()
        monkeypatch.setattr(transport, "_ssh", lambda _target, command: _run(*command, check=False))
        discovered = transport._discover_application_runtime("localhost", str(store.resolve()))
        assert discovered["uid"] == application_uid
        assert discovered["gid"] == application_gid
        assert discovered["container_store"] == container_store
        evidence["discovered_uid"] = discovered["uid"]
        evidence["discovered_gid"] = discovered["gid"]
        evidence["container_id"] = discovered["container_id"]

        manifest_in_container = f"{container_store}/incoming/{package.name}/manifest.json"
        probe = (
            "import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); "
            "print(json.dumps({'stat':p.stat().st_size,'read':json.loads(p.read_text())}))"
        )
        root = _run("docker", "exec", "--user", "0:0", name, "python", "-c", probe,
                    manifest_in_container)
        assert json.loads(root.stdout)["read"]["package_id"] == package.name
        denied = _run("docker", "exec", name, "python", "-c", probe,
                      manifest_in_container, check=False)
        assert denied.returncode != 0 and "PermissionError" in denied.stderr
        assert "Errno 13" in denied.stderr
        assert pointer.read_bytes() == before_pointer
        evidence["root_can_read"] = True
        evidence["application_kernel_error"] = "EACCES"
        evidence["root_only_acl"] = _run("getfacl", "-cpn", str(package)).stdout
        evidence["root_only_manifest_acl"] = _run("getfacl", "-cpn", str(package / "manifest.json")).stdout
        evidence["current_before"] = old.package_id

        pre = transport._run_application_runtime_gate(
            "localhost", discovered, phase="PRE_SWITCH",
            package_root=f"{container_store}/incoming/{package.name}",
            expected_package_id=package.name,
        )
        assert pre["STATUS"] == "FAIL" and pre["SAFE_REASON"] == "PermissionError"
        assert pointer.read_bytes() == before_pointer
        evidence["pre_switch_root_only"] = pre
        evidence["current_after"] = resolve_server_current(store).name

        # Correct only the named application ACL.  Traditional owner/group
        # modes remain 750/640, matching the reported incident.
        directories, files = _package_paths(package)
        for path in directories:
            _set_acl(path, f"u:{discovered['uid']}:r-x")
            assert stat.S_IMODE(path.stat().st_mode) == 0o750
        for path in files:
            _set_acl(path, f"u:{discovered['uid']}:r--")
            assert stat.S_IMODE(path.stat().st_mode) == 0o640
        evidence["correct_acl"] = _run("getfacl", "-cpn", str(package)).stdout
        readable = _run("docker", "exec", name, "python", "-c", probe,
                        manifest_in_container)
        assert json.loads(readable.stdout)["read"]["package_id"] == package.name
        evidence["application_manifest_read_after_acl"] = True

        corrected_pre = transport._run_application_runtime_gate(
            "localhost", discovered, phase="PRE_SWITCH",
            package_root=f"{container_store}/incoming/{package.name}",
            expected_package_id=package.name,
        )
        assert corrected_pre["STATUS"] == "PASS", corrected_pre
        assert all(corrected_pre[field] == "PASS" for field in (
            "DIRECTORY_TRAVERSAL", "MANIFEST_READ", "JSON_PARSE",
            "PARQUET_METADATA_READ", "THREE_OIL_READER",
            "DOMESTIC_BASIS_READER", "WEATHER_READER", "DOMESTIC_SPREAD_READER",
        ))
        evidence["pre_switch_correct_acl"] = corrected_pre

        activated = activate_incoming_server_package(
            package,
            store_root=store,
            expected_current=_identity(old),
            expected_candidate=_identity(candidate),
        )
        assert activated.status == "SYNCED"
        assert resolve_server_current(store).name == candidate.package_id
        post = transport._run_application_runtime_gate(
            "localhost", discovered, phase="POST_SWITCH",
            package_root=None, expected_package_id=candidate.package_id,
        )
        evidence["post_switch"] = post
        assert post["STATUS"] == "PASS", post
        assert post["ACTIVATED_RUNTIME_RESOLVER"] == "PASS"
        evidence["post_switch_current"] = candidate.package_id

        def read_restored(_data_root: Path) -> None:
            readback = transport._run_application_runtime_gate(
                "localhost", discovered, phase="ROLLBACK_READBACK",
                package_root=None, expected_package_id=old.package_id,
            )
            assert readback["STATUS"] == "PASS", readback
            evidence["restored_readback"] = readback

        reset = rollback_server_current_after_application_failure(
            store_root=store,
            expected_failed_package_id=candidate.package_id,
            rollback_package_id=old.package_id,
            post_rollback_validator=read_restored,
        )
        assert reset.status == "ROLLED_BACK"
        assert pointer.read_bytes() == before_pointer

        # Replay the external post-switch failure with a real kernel EACCES.
        retry = store / "incoming" / candidate.package_id
        shutil.copytree(candidate.directory, retry)
        _restrict_package(retry, uid=application_uid)
        again = activate_incoming_server_package(
            retry,
            store_root=store,
            expected_current=_identity(old),
            expected_candidate=_identity(candidate),
        )
        assert again.status == "SYNCED"
        active_metadata = (
            store / "releases" / candidate.package_id / "data"
            / "public-market-data" / "lutou-weather" / "current.json"
        )
        _run("setfacl", "-x", f"u:{application_uid}", str(active_metadata))
        denied_after_switch = _run(
            "docker", "exec", name, "python", "-c", probe,
            f"{container_store}/releases/{candidate.package_id}/data/public-market-data/lutou-weather/current.json",
            check=False,
        )
        assert denied_after_switch.returncode != 0 and "PermissionError" in denied_after_switch.stderr
        failed_post = transport._run_application_runtime_gate(
            "localhost", discovered, phase="POST_SWITCH",
            package_root=None, expected_package_id=candidate.package_id,
        )
        assert failed_post["STATUS"] == "FAIL"
        assert failed_post["SAFE_REASON"] == "PermissionError"
        evidence["post_switch_kernel_failure"] = failed_post
        rolled_back = rollback_server_current_after_application_failure(
            store_root=store,
            expected_failed_package_id=candidate.package_id,
            rollback_package_id=old.package_id,
            post_rollback_validator=read_restored,
        )
        assert rolled_back.status == "ROLLED_BACK"
        assert pointer.read_bytes() == before_pointer
        assert resolve_server_current(store).name == old.package_id
        evidence["rollback_status"] = rolled_back.status
        evidence["rollback_current_after"] = old.package_id

        # Exercise the production sealing command on a separate disposable
        # copy: the named-user principal must come from discovery, not root.
        sealed = tmp_path / "sealed-principal-check"
        shutil.copytree(candidate.directory, sealed)
        # sudo is confined to this disposable test directory.  It does not
        # alter runner configuration; it reproduces the root activation helper.
        helper_uid = int(_run("sudo", "-n", "id", "-u").stdout.strip())
        assert helper_uid == 0
        evidence["tooling_uid"] = helper_uid
        command = ["sudo", "-n", "sh", "-c", transport._SEAL_FOR_RUNTIME_READ_SCRIPT,
                   "seal-for-runtime-read", str(sealed), str(discovered["uid"])]
        _run(*command)
        sealed_acl = _run("getfacl", "-cpn", str(sealed / "manifest.json")).stdout
        assert f"user:{application_uid}:r--" in sealed_acl
        assert "user:0:r--" not in sealed_acl
        evidence["sealed_file_acl"] = sealed_acl
        evidence["tooling_uid_used_as_application_principal"] = False
    finally:
        destination = os.environ.get("APPLICATION_RUNTIME_ACCIDENT_EVIDENCE")
        if destination:
            path = Path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _run("docker", "rm", "--force", name, check=False)
