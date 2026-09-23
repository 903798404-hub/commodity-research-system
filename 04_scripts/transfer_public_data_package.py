#!/usr/bin/env python
"""Transport one sealed Goal E package with system OpenSSH and SCP.

The command intentionally accepts no password, private-key path, or option for
disabling host verification.  SSH trust stays in the operator's existing config.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "03_src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agri_research_agent.pipelines.public_data_delivery import (  # noqa: E402
    validate_production_package,
)
from agri_research_agent.pipelines.public_data_prewarm import (  # noqa: E402
    validate_formal_consumer_reads,
)


_SSH_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,199}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9_./-]+$")
_REMOTE_DETAIL_LIMIT = 512
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(?:lutou_(?:host|port|user|password)|password|passwd|pwd|secret|"
    r"token|api[_-]?key|authorization|user\s*(?:name|id)?|host|server|"
    r"data\s+source|"
    r"private[_-]?key)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_CONNECTION_URI = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s]+")
_SSH_AUTHORITY = re.compile(r"\b[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\b")
_COMMAND_LINE = re.compile(r"(?i)(?:^|\s)(?:docker|ssh|scp)\s+\S+.*--\S+")
_PRIVATE_KEY_PATH = re.compile(
    r"(?i)(?:[A-Za-z]:\\|/)[^\s\"'<>]*(?:id_(?:rsa|dsa|ecdsa|ed25519)|"
    r"[^/\\\s]+\.(?:pem|key))\b"
)
_SSH_OPTIONS = (
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
)
_DEFAULT_TRANSPORT_TIMEOUT_SECONDS = 3600.0
_transport_deadline: float | None = None


class TransportTimeoutError(TimeoutError):
    """The bounded SSH/SCP transport budget was exhausted."""

_PREPARE_PERMISSION_SCRIPT = (
    'set -eu; install -d --mode=0750 -- "$1" "$2" "$3"; '
    'setfacl -m "u:$4:r-x,d:u:$4:r-x" -- "$1"; '
    'setfacl -k -- "$2"; setfacl -m "u:$4:--x" -- "$2"; '
    'if getfacl -cp -- "$2" | grep -q "^default:"; then exit 66; fi; '
    'setfacl -m "u:$4:r-x" -- "$3"'
)

_CREATE_PRIVATE_UPLOAD_SCRIPT = (
    'set -eu; test ! -e "$1"; test ! -L "$1"; '
    'install -d --mode=0700 -- "$1"; setfacl -b -k -- "$1"; '
    'chmod 0700 -- "$1"; '
    'test "$(stat -c %u -- "$1")" = "$2"; '
    'test "$(stat -c %g -- "$1")" = "$3"'
)

_VERIFY_COMPLETED_UPLOAD_SCRIPT = (
    'set -eu; test -d "$1"; test ! -L "$1"; '
    'test -f "$1/manifest.json"; test ! -L "$1/manifest.json"; '
    'test -d "$1/data"; test ! -L "$1/data"; '
    'test "$(find "$1" -mindepth 1 -maxdepth 1 -printf x | wc -c)" -eq 2'
)

_SEAL_FOR_RUNTIME_READ_SCRIPT = (
    'set -eu; test -d "$1"; test ! -L "$1"; '
    'test -z "$(find "$1" -mindepth 1 ! -type d ! -type f -print -quit)"; '
    'find "$1" -type d -exec setfacl -b -k -- {} +; '
    'find "$1" -type f -exec setfacl -b -- {} +; '
    'find "$1" -type d -exec chmod 0700 -- {} +; '
    'find "$1" -type f -exec chmod 0600 -- {} +; '
    'find "$1" -type d -exec setfacl -m "u:$2:r-x" -- {} +; '
    'find "$1" -type f -exec setfacl -m "u:$2:r--" -- {} +; '
    'directory_count=$(find "$1" -type d -printf x | wc -c); '
    'file_count=$(find "$1" -type f -printf x | wc -c); '
    'test "$directory_count" -gt 0; test "$file_count" -gt 0; '
    'directory_acls=$(find "$1" -type d -exec getfacl -cpn -- {} +); '
    'file_acls=$(find "$1" -type f -exec getfacl -cpn -- {} +); '
    'count=$(printf "%s\\n" "$directory_acls" | grep -c "^user::rwx$" || true); '
    'test "$count" -eq "$directory_count"; '
    'count=$(printf "%s\\n" "$directory_acls" | grep -c "^user:$2:r-x$" || true); '
    'test "$count" -eq "$directory_count"; '
    'count=$(printf "%s\\n" "$directory_acls" | grep -c "^group::---$" || true); '
    'test "$count" -eq "$directory_count"; '
    'count=$(printf "%s\\n" "$directory_acls" | grep -c "^mask::r-x$" || true); '
    'test "$count" -eq "$directory_count"; '
    'count=$(printf "%s\\n" "$directory_acls" | grep -c "^other::---$" || true); '
    'test "$count" -eq "$directory_count"; '
    'count=$(printf "%s\\n" "$directory_acls" | grep -c "^[^[:space:]]" || true); '
    'test "$count" -eq "$((directory_count * 5))"; '
    'if printf "%s\\n" "$directory_acls" | grep -q "^default:"; then exit 67; fi; '
    'count=$(printf "%s\\n" "$file_acls" | grep -c "^user::rw-$" || true); '
    'test "$count" -eq "$file_count"; '
    'count=$(printf "%s\\n" "$file_acls" | grep -c "^user:$2:r--$" || true); '
    'test "$count" -eq "$file_count"; '
    'count=$(printf "%s\\n" "$file_acls" | grep -c "^group::---$" || true); '
    'test "$count" -eq "$file_count"; '
    'count=$(printf "%s\\n" "$file_acls" | grep -c "^mask::r--$" || true); '
    'test "$count" -eq "$file_count"; '
    'count=$(printf "%s\\n" "$file_acls" | grep -c "^other::---$" || true); '
    'test "$count" -eq "$file_count"; '
    'count=$(printf "%s\\n" "$file_acls" | grep -c "^[^[:space:]]" || true); '
    'test "$count" -eq "$((file_count * 5))"; '
    'test -z "$(find "$1" -mindepth 1 -perm /0007 -print -quit)"'
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload, remotely validate, and atomically activate a Goal E package"
    )
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--ssh-target", required=True)
    parser.add_argument("--remote-store-root", required=True)
    parser.add_argument("--activation-image-id", required=True)
    parser.add_argument("--initial-seed", action="store_true")
    parser.add_argument("--expected-current-id")
    parser.add_argument("--expected-current-artifact-sha256")
    parser.add_argument("--expected-current-manifest-sha256")
    parser.add_argument("--candidate-artifact-sha256")
    parser.add_argument("--candidate-manifest-sha256")
    parser.add_argument("--reconciliation-manifest-sha256")
    parser.add_argument(
        "--transport-timeout-seconds",
        type=float,
        default=_DEFAULT_TRANSPORT_TIMEOUT_SECONDS,
        help="Total wall-clock budget shared by all SSH and SCP subprocesses",
    )
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
    timeout = None
    if _transport_deadline is not None:
        timeout = _transport_deadline - time.monotonic()
        if timeout <= 0:
            raise TransportTimeoutError("SSH/SCP transport total timeout exhausted")
    try:
        return subprocess.run(
            command, text=True, capture_output=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise TransportTimeoutError("SSH/SCP transport total timeout exhausted") from exc


def _ssh(target: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return _run(["ssh", *_SSH_OPTIONS, "-T", target, shlex.join(arguments)])


def _bounded_remote_detail(result: subprocess.CompletedProcess[str]) -> str:
    detail = result.stderr.strip() or result.stdout.strip()
    detail = "\n".join(
        "<redacted-command>" if _COMMAND_LINE.search(line) else line
        for line in detail.splitlines()
    )
    detail = _CONNECTION_URI.sub("<redacted-connection-string>", detail)
    detail = _SSH_AUTHORITY.sub("<redacted-ssh-authority>", detail)
    detail = _SENSITIVE_ASSIGNMENT.sub("<redacted-credential>", detail)
    detail = _PRIVATE_KEY_PATH.sub("<redacted-private-key-path>", detail)
    detail = " ".join(detail.split()) or "<no diagnostic output>"
    if len(detail) > _REMOTE_DETAIL_LIMIT:
        detail = f"{detail[:_REMOTE_DETAIL_LIMIT - 3]}..."
    return detail


def _remote_integer(target: str, arguments: list[str], label: str) -> int:
    result = _ssh(target, arguments)
    try:
        value = int(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"remote {label} probe returned an invalid value") from exc
    if result.returncode != 0 or value < 0:
        raise RuntimeError(f"remote {label} probe failed")
    return value


def _candidate_runtime_identity(target: str, image: str) -> dict[str, object]:
    code = (
        "import json,os;"
        "print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),"
        "'groups':os.getgroups()},sort_keys=True))"
    )
    command = [
        "docker", "run", "--rm", "--pull", "never", "--network", "none",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m",
        image, "python", "-c", code,
    ]
    result = _ssh(target, command)
    if result.returncode != 0:
        raise RuntimeError("exact Candidate runtime identity probe failed")
    try:
        identity = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("exact Candidate runtime identity is invalid") from exc
    if (
        not isinstance(identity, dict)
        or set(identity) != {"uid", "gid", "groups"}
        or not isinstance(identity["uid"], int)
        or not isinstance(identity["gid"], int)
        or not isinstance(identity["groups"], list)
        or not all(isinstance(item, int) for item in identity["groups"])
    ):
        raise RuntimeError("exact Candidate runtime identity schema is invalid")
    return identity


def _quarantine_upload(
    target: str, store: str, remote_upload: str, upload_name: str
) -> str:
    quarantine_name = f"{upload_name}.failed-{uuid.uuid4().hex}"
    quarantine_root = f"{store}/quarantine"
    quarantine_path = f"{quarantine_root}/{quarantine_name}"
    script = (
        'set -eu; install -d --mode=0700 -- "$1"; '
        'if [ -e "$2" ] || [ -L "$2" ]; then mv -- "$2" "$3"; fi'
    )
    result = _ssh(target, [
        "sh", "-c", script, "public-data-quarantine",
        quarantine_root, remote_upload, quarantine_path,
    ])
    if result.returncode != 0:
        raise RuntimeError("failed staging package could not be quarantined")
    return quarantine_path


def main(argv: list[str] | None = None) -> int:
    global _transport_deadline
    args = parse_args(argv)
    if args.transport_timeout_seconds <= 0:
        raise ValueError("transport timeout must be positive")
    _transport_deadline = time.monotonic() + args.transport_timeout_seconds
    target, store, image = _checked_inputs(args)
    package = validate_production_package(args.package)
    promotion_values = (
        args.expected_current_id, args.expected_current_artifact_sha256,
        args.expected_current_manifest_sha256, args.candidate_artifact_sha256,
        args.candidate_manifest_sha256, args.reconciliation_manifest_sha256,
    )
    promotion = any(value is not None for value in promotion_values)
    if promotion:
        if args.initial_seed or not all(value is not None for value in promotion_values):
            raise ValueError("complete promotion and expected Current identity required")
        if not re.fullmatch(r"public-current-[0-9a-f]{24}", args.expected_current_id):
            raise ValueError("expected Current id invalid")
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in promotion_values[1:]):
            raise ValueError("promotion SHA-256 identity invalid")
        artifact = package.manifest["delivery_artifacts"]["domestic-spread"]
        if (hashlib.sha256((package.directory / "manifest.json").read_bytes()).hexdigest()
                != args.candidate_manifest_sha256 or
                artifact["sha256"] != args.candidate_artifact_sha256):
            raise ValueError("candidate identity changed before transfer")
    validate_formal_consumer_reads(
        project_root=ROOT,
        runtime_root=package.directory / "data",
    )
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
        if args.initial_seed:
            print(json.dumps({
                "schema_version": "public-data-transport/1",
                "status": "ALREADY_INITIALIZED",
                "package_id": package.package_id,
                "transport": "SKIPPED",
                "remote_activation": "SKIPPED",
            }, sort_keys=True))
            return 2
        if pointer.get("package_id") == package.package_id and not promotion:
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
    runtime_identity = _candidate_runtime_identity(target, image)
    runtime_uid = int(runtime_identity["uid"])
    transport_uid = _remote_integer(target, ["id", "-u"], "transport UID")
    transport_gid = _remote_integer(target, ["id", "-g"], "transport GID")
    if runtime_uid == transport_uid:
        raise RuntimeError("runtime and transport owner identities must be distinct")

    acl_probe = _ssh(target, [
        "sh", "-c",
        "command -v getfacl >/dev/null 2>&1 && command -v setfacl >/dev/null 2>&1",
        "public-data-acl-probe",
    ])
    if acl_probe.returncode != 0:
        raise RuntimeError("remote POSIX ACL tools are unavailable")

    incoming_root = f"{store}/incoming"
    releases_root = f"{store}/releases"
    prepare = _ssh(target, [
        "sh", "-c", _PREPARE_PERMISSION_SCRIPT, "public-data-permission-setup",
        store, incoming_root, releases_root, str(runtime_uid),
    ])
    if prepare.returncode != 0:
        raise RuntimeError("remote permission contract preparation failed")
    upload_name = f"{package.package_id}.upload-{uuid.uuid4().hex}"
    remote_upload = f"{incoming_root}/{upload_name}"
    created = _ssh(target, [
        "sh", "-c", _CREATE_PRIVATE_UPLOAD_SCRIPT, "create-private-upload",
        remote_upload, str(transport_uid), str(transport_gid),
    ])
    if created.returncode != 0:
        _quarantine_upload(target, store, remote_upload, upload_name)
        raise RuntimeError("private staging creation failed")
    copied = _run([
        "scp", *_SSH_OPTIONS, "-r", "--",
        str(package.directory / "manifest.json"), str(package.directory / "data"),
        f"{target}:{remote_upload}/",
    ])
    if copied.returncode != 0:
        _quarantine_upload(target, store, remote_upload, upload_name)
        raise RuntimeError("SCP package transport failed")

    completed = _ssh(target, [
        "sh", "-c", _VERIFY_COMPLETED_UPLOAD_SCRIPT, "verify-completed-upload",
        remote_upload,
    ])
    if completed.returncode != 0:
        _quarantine_upload(target, store, remote_upload, upload_name)
        raise RuntimeError("uploaded staging package is structurally incomplete")

    container_store = "/runtime/public-data-server-store"
    sealed = _ssh(target, [
        "sh", "-c", _SEAL_FOR_RUNTIME_READ_SCRIPT, "seal-for-runtime-read",
        remote_upload, str(runtime_uid),
    ])
    if sealed.returncode != 0:
        _quarantine_upload(target, store, remote_upload, upload_name)
        raise RuntimeError("SEAL_FOR_RUNTIME_READ failed")

    validation_arguments = [
        "docker", "run", "--rm", "--pull", "never", "--network", "none",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m",
        "--mount", f"type=bind,src={store},dst={container_store},readonly",
        image, "python", "/app/04_scripts/activate_public_data_package.py",
        "--incoming-package", f"{container_store}/incoming/{upload_name}",
        "--store-root", container_store, "--validate-only",
    ]
    validation = _ssh(target, validation_arguments)
    if validation.returncode != 0:
        quarantine = _quarantine_upload(target, store, remote_upload, upload_name)
        raise RuntimeError(f"remote sealed-package validation failed; quarantined={quarantine}")
    try:
        validation_result = json.loads(validation.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        _quarantine_upload(target, store, remote_upload, upload_name)
        raise RuntimeError("remote sealed-package validation result is invalid") from exc
    if (
        not isinstance(validation_result, dict)
        or validation_result.get("status") != "VALIDATED"
        or validation_result.get("package_id") != package.package_id
    ):
        _quarantine_upload(target, store, remote_upload, upload_name)
        raise RuntimeError("remote sealed-package validation identity mismatch")
    if promotion and (
        "public-current-server-cas/1" not in validation_result.get("activation_capabilities", [])
        or
        validation_result.get("manifest_sha256") != args.candidate_manifest_sha256
        or validation_result.get("domestic_spread_artifact_sha256") != args.candidate_artifact_sha256
    ):
        _quarantine_upload(target, store, remote_upload, upload_name)
        raise RuntimeError("activation image lacks CAS or transferred candidate identity differs")

    activation_arguments = [
        "docker", "run", "--rm", "--pull", "never", "--network", "none",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m",
        "--user", f"{transport_uid}:{transport_gid}",
        "--mount", f"type=bind,src={store},dst={container_store}",
        "--env", f"PUBLIC_DATA_SERVER_STORE_ROOT={container_store}",
        image, "python", "/app/04_scripts/activate_public_data_package.py",
        "--incoming-package", f"{container_store}/incoming/{upload_name}",
        "--store-root", container_store,
    ]
    if args.initial_seed:
        activation_arguments.append("--initial-seed")
    if promotion:
        activation_arguments.extend([
            "--expected-current-id", args.expected_current_id,
            "--expected-current-artifact-sha256", args.expected_current_artifact_sha256,
            "--expected-current-manifest-sha256", args.expected_current_manifest_sha256,
            "--expected-candidate-id", package.package_id,
            "--expected-candidate-artifact-sha256", args.candidate_artifact_sha256,
            "--expected-candidate-manifest-sha256", args.candidate_manifest_sha256,
        ])
    activation = _ssh(target, activation_arguments)
    if activation.returncode != 0:
        if promotion:
            try:
                stale = json.loads(activation.stdout.strip().splitlines()[-1])
            except (IndexError, ValueError):
                stale = None
            if isinstance(stale, dict) and stale.get("status") == "FAIL_STALE_BASE" and (
                stale.get("cas") or {}).get("result") == "FAIL_STALE_BASE":
                print(json.dumps({"schema_version": "public-data-transport/1",
                                  "status": "FAIL_STALE_BASE", "cas": stale["cas"],
                                  "package_id": package.package_id}, sort_keys=True))
                return 3
        detail = _bounded_remote_detail(activation)
        raise RuntimeError(
            f"remote activation failed; exit_code={activation.returncode}; detail={detail}"
        )
    try:
        result = json.loads(activation.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("remote activation result is invalid") from exc
    if result.get("package_id") != package.package_id:
        raise RuntimeError("remote activation package identity mismatch")
    receipt = {
        "schema_version": "public-data-transport/1",
        "status": result.get("status"),
        "package_id": package.package_id,
        "transport": "PASS",
        "remote_validation": validation_result,
        "remote_activation": result,
    }
    if promotion:
        cas = result.get("cas") or {}
        if (cas.get("result") != "PASS" or
            (cas.get("actual_old_current") or {}).get("id") != args.expected_current_id or
            cas.get("new_current_id") != package.package_id):
            raise RuntimeError("server CAS receipt identity differs")
        receipt["promotion"] = {
            "promoted_candidate_id": package.package_id,
            "promoted_artifact_sha256": args.candidate_artifact_sha256,
            "promoted_manifest_sha256": args.candidate_manifest_sha256,
            "reconciliation_manifest_sha256": args.reconciliation_manifest_sha256,
            "expected_old_current_id": args.expected_current_id,
            "actual_old_current_id": cas["actual_old_current"]["id"],
            "expected_old_artifact_sha256": args.expected_current_artifact_sha256,
            "actual_old_artifact_sha256": cas["actual_old_current"]["artifact_sha256"],
            "expected_old_manifest_sha256": args.expected_current_manifest_sha256,
            "actual_old_manifest_sha256": cas["actual_old_current"]["manifest_sha256"],
            "new_current_id": cas["new_current_id"],
            "cas_result": cas["result"], "switch_timestamp": cas["switch_timestamp"],
        }
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"SYNCED", "NO_CHANGE"} else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TransportTimeoutError:
        print("SSH/SCP transport total timeout exhausted", file=sys.stderr)
        raise SystemExit(124) from None
