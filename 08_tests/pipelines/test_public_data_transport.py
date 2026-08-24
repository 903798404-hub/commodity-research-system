from __future__ import annotations

import importlib.util
import json
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
    scp_index = next(i for i, command in enumerate(calls) if command[0] == "scp")
    setup = next(item for item in joined_calls if "public-data-permission-setup" in item)
    seal_index = next(i for i, item in enumerate(joined_calls) if "seal-for-runtime-read" in item)
    validate_index = next(i for i, item in enumerate(joined_calls) if "--validate-only" in item)
    activate_index = max(
        i for i, item in enumerate(joined_calls)
        if "activate_public_data_package.py" in item
    )
    assert scp_index < seal_index < validate_index < activate_index
    assert 'd:u:$4:r-x' in setup and 'u:$4:--x' in setup
    seal = joined_calls[seal_index]
    assert 'u:$2:r-x' in seal and 'u:$2:r--' in seal
    assert "chmod" not in setup + seal
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
