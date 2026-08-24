from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "04_scripts" / "transfer_public_data_package.py"
SPEC = importlib.util.spec_from_file_location("public_data_transport_test", SCRIPT)
assert SPEC and SPEC.loader
transport = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transport)


@pytest.fixture(autouse=True)
def _formal_consumer_validation_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        transport,
        "validate_formal_consumer_reads",
        lambda **_kwargs: SimpleNamespace(targets={"weather": "PASS"}),
    )


def _completed(command: list[str], *, code: int = 0, stdout: str = ""):
    return subprocess.CompletedProcess(command, code, stdout, "")


def _successful_delivery_run(calls: list[list[str]], command: list[str]):
    calls.append(command)
    joined = " ".join(command)
    if len(calls) == 1:
        return _completed(command, stdout="__MISSING__")
    if command[0] == "scp":
        return _completed(command)
    if "os.getuid" in joined:
        return _completed(command, stdout=json.dumps({"uid": 0, "gid": 0, "groups": [0]}))
    if command[-1] == "id -u":
        return _completed(command, stdout="1000\n")
    if command[-1] == "id -g":
        return _completed(command, stdout="1000\n")
    if "--validate-only" in joined:
        return _completed(command, stdout=json.dumps({
            "status": "VALIDATED", "package_id": "public-current-abc",
        }))
    if "activate_public_data_package.py" in joined:
        return _completed(command, stdout=json.dumps({
            "status": "SYNCED", "package_id": "public-current-abc",
        }))
    return _completed(command)


def test_same_remote_identity_skips_scp_and_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = {
        "current_identity_sha256": "a" * 64,
        "delivery_identity_sha256": "b" * 64,
        "bundle_sha256": "c" * 64,
    }
    package = SimpleNamespace(
        package_id="public-current-abc", directory=tmp_path, manifest=manifest
    )
    monkeypatch.setattr(transport, "validate_production_package", lambda _path: package)
    calls: list[list[str]] = []

    def run(command: list[str]):
        calls.append(command)
        return _completed(command, stdout=json.dumps({
            "schema_version": "public-current-server-pointer/2",
            "package_id": package.package_id,
            **manifest,
        }))

    monkeypatch.setattr(transport, "_run", run)
    code = transport.main([
        "--package", str(tmp_path),
        "--ssh-target", "trusted-host",
        "--remote-store-root", "/home/ubuntu/market-data/01_data/public-data-server-store",
        "--activation-image-id", f"sha256:{'a' * 64}",
    ])

    assert code == 0
    assert len(calls) == 1 and calls[0][0] == "ssh"
    assert json.loads(capsys.readouterr().out)["transport"] == "SKIPPED"


def test_formal_consumer_failure_stops_before_any_server_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = SimpleNamespace(
        package_id="public-current-abc", directory=tmp_path, manifest={}
    )
    monkeypatch.setattr(transport, "validate_production_package", lambda _path: package)
    monkeypatch.setattr(
        transport,
        "validate_formal_consumer_reads",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("formal consumer validation failed: weather=FAIL")
        ),
    )
    monkeypatch.setattr(
        transport,
        "_run",
        lambda _command: pytest.fail("formal validation must precede SSH and SCP"),
    )

    with pytest.raises(RuntimeError, match="weather=FAIL"):
        transport.main([
            "--package", str(tmp_path), "--ssh-target", "trusted-host",
            "--remote-store-root", "/safe/store",
            "--activation-image-id", f"sha256:{'a' * 64}",
        ])


def test_updated_package_uses_scp_then_immutable_image_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = SimpleNamespace(package_id="public-current-abc", directory=tmp_path, manifest={})
    monkeypatch.setattr(transport, "validate_production_package", lambda _path: package)
    calls: list[list[str]] = []

    def run(command: list[str]):
        return _successful_delivery_run(calls, command)

    monkeypatch.setattr(transport, "_run", run)
    image_id = f"sha256:{'b' * 64}"
    code = transport.main([
        "--package", str(tmp_path),
        "--ssh-target", "trusted-host",
        "--remote-store-root", "/home/ubuntu/market-data/01_data/public-data-server-store",
        "--activation-image-id", image_id,
    ])

    assert code == 0
    joined_calls = [" ".join(command) for command in calls]
    assert [command[0] for command in calls].count("scp") == 1
    setup_index = next(
        i for i, item in enumerate(joined_calls)
        if "public-data-permission-setup" in item
    )
    create_index = next(
        i for i, item in enumerate(joined_calls) if "create-private-upload" in item
    )
    scp_index = next(i for i, command in enumerate(calls) if command[0] == "scp")
    complete_index = next(
        i for i, item in enumerate(joined_calls) if "verify-completed-upload" in item
    )
    setup = next(item for item in joined_calls if "public-data-permission-setup" in item)
    seal_index = next(i for i, item in enumerate(joined_calls) if "seal-for-runtime-read" in item)
    validate_index = next(i for i, item in enumerate(joined_calls) if "--validate-only" in item)
    activate_index = max(
        i for i, item in enumerate(joined_calls)
        if "activate_public_data_package.py" in item
    )
    assert setup_index < create_index < scp_index < complete_index < seal_index
    assert seal_index < validate_index < activate_index
    assert 'd:u:$4:r-x' in setup and 'u:$4:--x' in setup
    assert 'setfacl -k -- "$2"' in setup
    assert '^default:' in setup
    create = joined_calls[create_index]
    assert "--mode=0700" in create
    assert "setfacl -b -k" in create
    assert "chmod 0700" in create
    assert "u:$" not in create
    scp = calls[scp_index]
    assert str(tmp_path / "manifest.json") in scp
    assert str(tmp_path / "data") in scp
    seal = joined_calls[seal_index]
    assert 'u:$2:r-x' in seal and 'u:$2:r--' in seal
    assert "setfacl -b -k" in seal and "setfacl -b" in seal
    assert "chmod 0700" in seal and "chmod 0600" in seal
    assert '^other::---$' in seal and '^group::---$' in seal
    assert "-perm /0007" in seal
    validation = joined_calls[validate_index]
    activation = joined_calls[activate_index]
    assert image_id in activation
    assert "activate_public_data_package.py" in activation
    assert "--cap-drop ALL" in validation and ",readonly" in validation
    assert "--user 1000:1000" not in validation
    assert "--cap-drop ALL" in activation and "--user 1000:1000" in activation
    assert ",rw" in activation
    assert "CAP_DAC_OVERRIDE" not in " ".join(joined_calls)
    assert "StrictHostKeyChecking" not in " ".join(" ".join(item) for item in calls)


def test_initial_seed_refuses_existing_remote_current_before_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    package = SimpleNamespace(
        package_id="public-current-abc", directory=tmp_path,
        manifest={"current_identity_sha256": "a" * 64,
                  "delivery_identity_sha256": "b" * 64,
                  "bundle_sha256": "c" * 64},
    )
    monkeypatch.setattr(transport, "validate_production_package", lambda _path: package)
    calls: list[list[str]] = []
    monkeypatch.setattr(transport, "_run", lambda command: (
        calls.append(command) or _completed(command, stdout=json.dumps({
            "schema_version": "public-current-server-pointer/2",
            "package_id": "another-package", **package.manifest,
        }))
    ))
    code = transport.main([
        "--package", str(tmp_path), "--ssh-target", "trusted-host",
        "--remote-store-root", "/safe/store",
        "--activation-image-id", f"sha256:{'a' * 64}", "--initial-seed",
    ])
    assert code == 2
    assert len(calls) == 1 and calls[0][0] == "ssh"
    assert json.loads(capsys.readouterr().out)["status"] == "ALREADY_INITIALIZED"


def test_initial_seed_propagates_to_remote_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = SimpleNamespace(package_id="public-current-abc", directory=tmp_path, manifest={})
    monkeypatch.setattr(transport, "validate_production_package", lambda _path: package)
    calls: list[list[str]] = []
    def run(command: list[str]):
        return _successful_delivery_run(calls, command)
    monkeypatch.setattr(transport, "_run", run)
    assert transport.main([
        "--package", str(tmp_path), "--ssh-target", "trusted-host",
        "--remote-store-root", "/safe/store",
        "--activation-image-id", f"sha256:{'a' * 64}", "--initial-seed",
    ]) == 0
    assert "--initial-seed" in calls[-1][-1]


def test_missing_acl_tools_fails_before_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = SimpleNamespace(package_id="public-current-abc", directory=tmp_path, manifest={})
    monkeypatch.setattr(transport, "validate_production_package", lambda _path: package)
    calls: list[list[str]] = []

    def run(command: list[str]):
        calls.append(command)
        joined = " ".join(command)
        if len(calls) == 1:
            return _completed(command, stdout="__MISSING__")
        if "os.getuid" in joined:
            return _completed(command, stdout='{"gid":0,"groups":[0],"uid":0}')
        if command[-1] in {"id -u", "id -g"}:
            return _completed(command, stdout="1000\n")
        if "public-data-acl-probe" in joined:
            return _completed(command, code=1)
        return _completed(command)

    monkeypatch.setattr(transport, "_run", run)
    with pytest.raises(RuntimeError, match="POSIX ACL tools"):
        transport.main([
            "--package", str(tmp_path), "--ssh-target", "trusted-host",
            "--remote-store-root", "/safe/store",
            "--activation-image-id", f"sha256:{'a' * 64}",
        ])
    assert all(command[0] != "scp" for command in calls)


def test_seal_failure_quarantines_and_never_validates_or_activates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = SimpleNamespace(package_id="public-current-abc", directory=tmp_path, manifest={})
    monkeypatch.setattr(transport, "validate_production_package", lambda _path: package)
    calls: list[list[str]] = []

    def run(command: list[str]):
        result = _successful_delivery_run(calls, command)
        if "seal-for-runtime-read" in " ".join(command):
            return _completed(command, code=1)
        return result

    monkeypatch.setattr(transport, "_run", run)
    with pytest.raises(RuntimeError, match="SEAL_FOR_RUNTIME_READ"):
        transport.main([
            "--package", str(tmp_path), "--ssh-target", "trusted-host",
            "--remote-store-root", "/safe/store",
            "--activation-image-id", f"sha256:{'a' * 64}",
        ])
    joined = [" ".join(command) for command in calls]
    assert any("public-data-quarantine" in item for item in joined)
    assert not any("--validate-only" in item for item in joined)
    assert not any("--user 1000:1000" in item for item in joined)


def test_incomplete_uploaded_structure_quarantines_before_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = SimpleNamespace(package_id="public-current-abc", directory=tmp_path, manifest={})
    monkeypatch.setattr(transport, "validate_production_package", lambda _path: package)
    calls: list[list[str]] = []

    def run(command: list[str]):
        result = _successful_delivery_run(calls, command)
        if "verify-completed-upload" in " ".join(command):
            return _completed(command, code=1)
        return result

    monkeypatch.setattr(transport, "_run", run)
    with pytest.raises(RuntimeError, match="structurally incomplete"):
        transport.main([
            "--package", str(tmp_path), "--ssh-target", "trusted-host",
            "--remote-store-root", "/safe/store",
            "--activation-image-id", f"sha256:{'a' * 64}",
        ])
    joined = [" ".join(command) for command in calls]
    assert any("public-data-quarantine" in item for item in joined)
    assert not any("seal-for-runtime-read" in item for item in joined)
    assert not any("--validate-only" in item for item in joined)


@pytest.mark.skipif(
    os.name != "posix"
    or shutil.which("sh") is None
    or shutil.which("setfacl") is None
    or shutil.which("getfacl") is None,
    reason="requires a POSIX ACL runtime",
)
def test_real_scp_0664_modes_are_normalized_before_runtime_acl(tmp_path: Path) -> None:
    staging = tmp_path / "public-current-abc.upload-scp"
    nested = staging / "data" / "nested"
    nested.mkdir(parents=True)
    manifest = staging / "manifest.json"
    payload = nested / "payload.txt"
    manifest.write_text("ordinary manifest", encoding="utf-8")
    payload.write_text("ordinary payload", encoding="utf-8")
    for directory in (staging, staging / "data", nested):
        directory.chmod(0o775)
    for path in (manifest, payload):
        path.chmod(0o664)
        subprocess.run(
            ["setfacl", "-m", "u:65533:rw-", "--", str(path)], check=True
        )
    subprocess.run(
        ["setfacl", "-m", "d:u:65533:rwx", "--", str(staging / "data")],
        check=True,
    )
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o664
    assert stat.S_IMODE(staging.stat().st_mode) == 0o775

    runtime_uid = 65534
    result = subprocess.run(
        [
            "sh", "-c", transport._SEAL_FOR_RUNTIME_READ_SCRIPT,
            "seal-for-runtime-read", str(staging), str(runtime_uid),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    for directory in (staging, staging / "data", nested):
        acl = subprocess.run(
            ["getfacl", "-cpn", "--", str(directory)],
            text=True, capture_output=True, check=True,
        ).stdout.splitlines()
        assert "user::rwx" in acl
        assert f"user:{runtime_uid}:r-x" in acl
        assert "group::---" in acl
        assert "mask::r-x" in acl
        assert "other::---" in acl
        assert not any(line.startswith("default:") for line in acl)
        assert not any(line.startswith("user:65533:") for line in acl)
    for path in (manifest, payload):
        acl = subprocess.run(
            ["getfacl", "-cpn", "--", str(path)],
            text=True, capture_output=True, check=True,
        ).stdout.splitlines()
        assert "user::rw-" in acl
        assert f"user:{runtime_uid}:r--" in acl
        assert "group::---" in acl
        assert "mask::r--" in acl
        assert "other::---" in acl
        assert not any(line.startswith("user:65533:") for line in acl)
        assert not any(
            line.startswith(f"user:{runtime_uid}:") and "w" in line
            for line in acl
        )
    manifest.write_text("transport still manages", encoding="utf-8")
    created = staging / "transport-created.txt"
    created.write_text("owner write", encoding="utf-8")
    created.unlink()


@pytest.mark.skipif(
    os.name != "posix"
    or shutil.which("sh") is None
    or shutil.which("setfacl") is None
    or shutil.which("getfacl") is None,
    reason="requires a POSIX ACL runtime",
)
def test_permission_seal_rejects_symlink_before_recursive_changes(tmp_path: Path) -> None:
    staging = tmp_path / "public-current-abc.upload-symlink"
    staging.mkdir()
    regular = staging / "manifest.json"
    regular.write_text("unchanged", encoding="utf-8")
    regular.chmod(0o664)
    (staging / "escape").symlink_to(tmp_path / "outside")

    result = subprocess.run(
        [
            "sh", "-c", transport._SEAL_FOR_RUNTIME_READ_SCRIPT,
            "seal-for-runtime-read", str(staging), "65534",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert stat.S_IMODE(regular.stat().st_mode) == 0o664


def test_validation_failure_quarantines_without_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = SimpleNamespace(package_id="public-current-abc", directory=tmp_path, manifest={})
    monkeypatch.setattr(transport, "validate_production_package", lambda _path: package)
    calls: list[list[str]] = []

    def run(command: list[str]):
        result = _successful_delivery_run(calls, command)
        if "--validate-only" in " ".join(command):
            return _completed(command, code=1)
        return result

    monkeypatch.setattr(transport, "_run", run)
    with pytest.raises(RuntimeError, match="sealed-package validation failed"):
        transport.main([
            "--package", str(tmp_path), "--ssh-target", "trusted-host",
            "--remote-store-root", "/safe/store",
            "--activation-image-id", f"sha256:{'a' * 64}",
        ])
    joined = [" ".join(command) for command in calls]
    assert any("public-data-quarantine" in item for item in joined)
    assert not any("--user 1000:1000" in item for item in joined)


def test_runtime_and_transport_owner_must_be_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = SimpleNamespace(package_id="public-current-abc", directory=tmp_path, manifest={})
    monkeypatch.setattr(transport, "validate_production_package", lambda _path: package)
    calls: list[list[str]] = []

    def run(command: list[str]):
        calls.append(command)
        joined = " ".join(command)
        if len(calls) == 1:
            return _completed(command, stdout="__MISSING__")
        if "os.getuid" in joined:
            return _completed(command, stdout='{"gid":1000,"groups":[1000],"uid":1000}')
        if command[-1] in {"id -u", "id -g"}:
            return _completed(command, stdout="1000\n")
        return _completed(command)

    monkeypatch.setattr(transport, "_run", run)
    with pytest.raises(RuntimeError, match="must be distinct"):
        transport.main([
            "--package", str(tmp_path), "--ssh-target", "trusted-host",
            "--remote-store-root", "/safe/store",
            "--activation-image-id", f"sha256:{'a' * 64}",
        ])


@pytest.mark.parametrize(
    ("target", "store", "image"),
    [
        ("-unsafe", "/safe", f"sha256:{'a' * 64}"),
        ("trusted", "/safe/../escape", f"sha256:{'a' * 64}"),
        ("trusted", "/safe", "latest"),
    ],
)
def test_transport_rejects_unsafe_remote_identity(target: str, store: str, image: str) -> None:
    with pytest.raises(ValueError):
        transport._checked_inputs(SimpleNamespace(
            ssh_target=target, remote_store_root=store, activation_image_id=image,
        ))
