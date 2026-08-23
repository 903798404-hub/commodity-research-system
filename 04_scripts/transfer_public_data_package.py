#!/usr/bin/env python
"""Transport one sealed Goal E package with system OpenSSH and SCP.

The command intentionally accepts no password, private-key path, or option for
disabling host verification.  SSH trust stays in the operator's existing config.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "03_src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agri_research_agent.pipelines.public_data_delivery import (  # noqa: E402
    validate_production_package,
)


_SSH_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,199}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9_./-]+$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload, remotely validate, and atomically activate a Goal E package"
    )
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--ssh-target", required=True)
    parser.add_argument("--remote-store-root", required=True)
    parser.add_argument("--activation-image-id", required=True)
    return parser.parse_args(argv)


def _checked_inputs(args: argparse.Namespace) -> tuple[str, str, str]:
    target = str(args.ssh_target)
    store = str(args.remote_store_root)
    image = str(args.activation_image_id)
    if not _SSH_TARGET.fullmatch(target):
        raise ValueError("unsafe SSH target")
    if not _REMOTE_PATH.fullmatch(store) or ".." in PurePosixPath(store).parts:
        raise ValueError("unsafe remote store root")
    if not _IMAGE_ID.fullmatch(image):
        raise ValueError("activation image must be an immutable sha256 Image ID")
    return target, store.rstrip("/"), image


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _ssh(target: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return _run(["ssh", "-T", target, shlex.join(arguments)])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    target, store, image = _checked_inputs(args)
    package = validate_production_package(args.package)
    pointer_path = f"{store}/current.json"
    pointer_probe = (
        'if [ ! -e "$1" ]; then printf "__MISSING__"; '
        'elif [ -f "$1" ] && [ ! -L "$1" ]; then cat -- "$1"; else exit 65; fi'
    )
    current = _ssh(target, ["sh", "-c", pointer_probe, "public-data-probe", pointer_path])
    if current.returncode != 0:
        raise RuntimeError("remote Current preflight failed")
    if current.stdout != "__MISSING__":
        try:
            pointer = json.loads(current.stdout)
        except ValueError as exc:
            raise RuntimeError("remote Current pointer is invalid JSON") from exc
        required_pointer = {
            "schema_version", "package_id", "current_identity_sha256",
            "delivery_identity_sha256", "bundle_sha256",
        }
        if (
            not isinstance(pointer, dict)
            or set(pointer) != required_pointer
            or pointer["schema_version"] != "public-current-server-pointer/2"
        ):
            raise RuntimeError("remote Current pointer schema is invalid")
        if pointer.get("package_id") == package.package_id:
            expected = package.manifest
            if any(
                pointer[key] != expected[key]
                for key in (
                    "current_identity_sha256", "delivery_identity_sha256",
                    "bundle_sha256",
                )
            ):
                raise RuntimeError("remote Current package identity mismatch")
            print(json.dumps({
                "schema_version": "public-data-transport/1",
                "status": "NO_CHANGE",
                "package_id": package.package_id,
                "transport": "SKIPPED",
                "remote_activation": "SKIPPED",
            }, sort_keys=True))
            return 0
    incoming_root = f"{store}/incoming"
    prepare = _ssh(target, ["install", "-d", "--mode=0750", "--", incoming_root])
    if prepare.returncode != 0:
        raise RuntimeError("remote incoming preparation failed")
    upload_name = f"{package.package_id}.upload-{uuid.uuid4().hex}"
    remote_upload = f"{incoming_root}/{upload_name}"
    copied = _run(["scp", "-r", "--", str(package.directory), f"{target}:{remote_upload}"])
    if copied.returncode != 0:
        raise RuntimeError("SCP package transport failed")

    container_store = "/runtime/public-data-server-store"
    activation = _ssh(target, [
        "docker", "run", "--rm", "--pull", "never", "--network", "none",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m",
        "--mount", f"type=bind,src={store},dst={container_store}",
        "--env", f"PUBLIC_DATA_SERVER_STORE_ROOT={container_store}",
        image, "python", "/app/04_scripts/activate_public_data_package.py",
        "--incoming-package", f"{container_store}/incoming/{upload_name}",
        "--store-root", container_store,
    ])
    if activation.returncode != 0:
        raise RuntimeError("remote validation or activation failed")
    try:
        result = json.loads(activation.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("remote activation result is invalid") from exc
    print(json.dumps({
        "schema_version": "public-data-transport/1",
        "status": result.get("status"),
        "package_id": package.package_id,
        "transport": "PASS",
        "remote_activation": result,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"SYNCED", "NO_CHANGE"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
