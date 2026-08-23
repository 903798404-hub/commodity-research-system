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


def _completed(command: list[str], *, code: int = 0, stdout: str = ""):
    return subprocess.CompletedProcess(command, code, stdout, "")


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


def test_updated_package_uses_scp_then_immutable_image_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = SimpleNamespace(package_id="public-current-abc", directory=tmp_path, manifest={})
    monkeypatch.setattr(transport, "validate_production_package", lambda _path: package)
    calls: list[list[str]] = []

    def run(command: list[str]):
        calls.append(command)
        if len(calls) == 1:
            return _completed(command, stdout="__MISSING__")
        if command[0] == "scp" or len(calls) == 2:
            return _completed(command)
        return _completed(command, stdout=json.dumps({
            "status": "SYNCED", "package_id": package.package_id,
        }))

    monkeypatch.setattr(transport, "_run", run)
    image_id = f"sha256:{'b' * 64}"
    code = transport.main([
        "--package", str(tmp_path),
        "--ssh-target", "trusted-host",
        "--remote-store-root", "/home/ubuntu/market-data/01_data/public-data-server-store",
        "--activation-image-id", image_id,
    ])

    assert code == 0
    assert [command[0] for command in calls] == ["ssh", "ssh", "scp", "ssh"]
    activation = calls[-1][-1]
    assert image_id in activation
    assert "activate_public_data_package.py" in activation
    assert "StrictHostKeyChecking" not in " ".join(" ".join(item) for item in calls)


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
