"""Root-controlled candidate validation records for same-image release validation.

This entrypoint accepts a registered project, never caller-provided probe results
or raw evidence. Candidate records attest validation, not production approval.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[2]
ENGINE = "04_scripts/runtime/validate_target_runtime.py"
RECORD = "09_deploy/runtime_identity/candidate_validation_record.py"
HOST = "09_deploy/runtime_identity/host_authorization.py"
TRUST = "02_configs/production_runtime_trust.json"
KEY_DIRECTORY = Path("/etc/market-data/runtime-identity")


class PreReleaseError(ValueError):
    """Candidate validation or release identity could not be established."""


class ValidationBlocked(PreReleaseError):
    """The authorized Linux builder is unavailable."""


def _load(relative: str, name: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PreReleaseError("trusted source cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        # Never execute ignored bytecode, even if it has a matching timestamp.
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


def require_source(host, engine) -> tuple[str, str]:
    """Require this exact detached source and its Git database to be protected."""
    host._require_linux_root()
    if any(key.startswith("GIT_") for key in os.environ):
        raise PreReleaseError("caller Git environment is forbidden")
    host._protected_path(ROOT, directory=True)
    host._protected_path(ROOT / ".git", directory=True)
    # Check all files, including Git config/hooks and ignored files, before any
    # Git subprocess. Execution contracts below additionally use committed bytes.
    for path in ROOT.rglob("*"):
        host._protected_path(path, directory=path.is_dir())
    if engine._git(ROOT, "rev-parse", "--show-toplevel") != str(ROOT):
        raise PreReleaseError("source repository root differs")
    if engine._git(ROOT, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD":
        raise PreReleaseError("release source must be detached")
    if engine._git(ROOT, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PreReleaseError("release source is dirty")
    if engine._git(ROOT, "for-each-ref", "refs/replace"):
        raise PreReleaseError("Git replacement refs are forbidden")
    flags = engine._git(ROOT, "ls-files", "-v", "-z", binary=True).split(b"\0")
    if any(row and not row.startswith(b"H ") for row in flags):
        raise PreReleaseError("hidden Git index changes are forbidden")
    tracked = {row[2:].decode("utf-8", "strict") for row in flags if row}
    archived = set()
    raw = engine._git(ROOT, "archive", "--format=tar", "HEAD", binary=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile() or member.name not in tracked or member.name in archived:
                raise PreReleaseError("source archive contains an undeclared or aliased file")
            archived.add(member.name)
            stream = archive.extractfile(member)
            if stream is None or (ROOT / member.name).read_bytes() != stream.read():
                raise PreReleaseError("source bytes differ from Approved Git object")
    if archived != tracked:
        raise PreReleaseError("source archive omits tracked files")
    return engine._git(ROOT, "rev-parse", "HEAD"), engine._git(ROOT, "rev-parse", "HEAD^{tree}")


def _new_output(host, destination: Path) -> None:
    if not destination.is_absolute() or destination.exists() or destination.is_symlink():
        raise PreReleaseError("record output must be a new absolute file")
    host._protected_path(destination.parent, directory=True)
    if destination == ROOT or ROOT in destination.parents:
        raise PreReleaseError("record output must be outside source repository")


def _write_new(host, destination: Path, raw: bytes) -> None:
    _new_output(host, destination)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    host._fsync_directory(destination.parent)


def _execute_validation(project: dict, output: Path) -> int:
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTHONHOME"}}
    return subprocess.run(
        [sys.executable, "-I", "-B", str(ROOT / ENGINE), "--project", project["project_id"],
         "--runtime-contract", project["runtime_contract"], "--evidence-output", str(output)],
        cwd=ROOT, env=env, check=False, timeout=3900,
    ).returncode


def validate_candidate(project_id: str, destination: Path, key_path: Path,
                       *, ttl_seconds: int = 86400) -> dict:
    """Run the actual engine, verify its result, and exclusively seal a record."""
    if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ValidationBlocked("LINUX_BUILDER_UNAVAILABLE")
    host = _load(HOST, "_pre_release_host")
    host.require_protected_authority_source()
    for name in (ENGINE, RECORD, "04_scripts/runtime/pre_release_runtime.py"):
        host._protected_path(ROOT / name)
    engine = _load(ENGINE, "_pre_release_engine")
    before = require_source(host, engine)
    _new_output(host, destination)
    if type(ttl_seconds) is not int or not 0 < ttl_seconds <= 7 * 86400:
        raise PreReleaseError("invalid candidate record lifetime")
    if key_path.parent != KEY_DIRECTORY:
        raise PreReleaseError("candidate signing key must use the fixed host key directory")
    key = host._load_private_key(key_path)
    trust_raw = (ROOT / TRUST).read_bytes()
    trust = host._json(trust_raw)
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode("ascii")
    matches = [item for item in trust.get("keys", []) if item.get("domain") == "candidate_validation"
               and item.get("algorithm") == "ed25519" and item.get("public_key_base64") == public]
    if len(matches) != 1 or matches[0]["key_id"] in trust.get("revoked_key_ids", []):
        raise PreReleaseError("candidate key is untrusted or revoked")
    project = engine._project(ROOT, project_id)
    _, _, binding = engine.source_contract(ROOT, project_id, project["runtime_contract"])
    if (binding["commit"], binding["tree"]) != before:
        raise PreReleaseError("candidate identity changed before validation")
    record = _load(RECORD, "_pre_release_record")
    # Root-private, uniquely allocated evidence location: the CLI never imports
    # an externally supplied evidence file and does not expose this path as input.
    with tempfile.TemporaryDirectory(prefix="candidate-validation-", dir=destination.parent) as folder:
        output = Path(folder) / "evidence.json"
        code = _execute_validation(project, output)
        if code == 3:
            raise ValidationBlocked("LINUX_BUILDER_UNAVAILABLE")
        if code != 0 or not output.is_file() or output.is_symlink():
            raise PreReleaseError("candidate engine did not complete successfully")
        evidence = host._json(output.read_bytes())
    if evidence.get("binding") != binding:
        raise PreReleaseError("candidate evidence binding differs")
    now = datetime.now(timezone.utc)
    payload = {"record_id": os.urandom(16).hex(), "purpose": "target-runtime-validation",
               "authorization_role": "candidate_validation", "issued_at": now.isoformat(),
               "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
               "evidence_sha256": hashlib.sha256(record.canonical(evidence)).hexdigest(),
               "evidence": evidence}
    record.validate_payload(payload)
    envelope = {"schema_version": "candidate-validation-record/1", "algorithm": "ed25519",
                "key_id": matches[0]["key_id"], "payload": payload}
    envelope["signature"] = base64.b64encode(key.sign(record.canonical(envelope))).decode("ascii")
    raw = record.canonical(envelope)
    record.verify_record(raw, trust, now=now)
    if require_source(host, engine) != before or (ROOT / TRUST).read_bytes() != trust_raw:
        raise PreReleaseError("candidate source changed during validation")
    if engine.source_contract(ROOT, project_id, project["runtime_contract"])[2] != binding:
        raise PreReleaseError("candidate binding changed during validation")
    _write_new(host, destination, raw)
    return payload


def _validate_production_revalidation_report(report: object, container_id: str) -> dict:
    """Apply the common strict report contract for production policy v3/v5."""
    if not isinstance(report, dict):
        raise PreReleaseError("host returned an invalid revalidation report")
    required = {"schema_version", "PRE_RELEASE_VALIDATION", "production_write_granted",
                "container_started", "container_id"}
    if (not required.issubset(report) or report["schema_version"] != "production-pre-release-validation/1"
            or report["PRE_RELEASE_VALIDATION"] != "PASS"
            or report["production_write_granted"] is not False
            or report["container_started"] is not False
            or report["container_id"] != container_id):
        raise PreReleaseError("host returned an invalid revalidation report")
    return report


def revalidate_production(container_id: str, policy_path: Path, destination: Path) -> dict:
    """Revalidate a fresh, unstarted production instance using host authority.

    This deliberately has no signing-key or grant-writing path.  The host owns
    policy parsing, Docker observation, and report generation; this wrapper only
    establishes protected source state and atomically publishes the host report.
    """
    if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ValidationBlocked("LINUX_BUILDER_UNAVAILABLE")
    if type(container_id) is not str or not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise PreReleaseError("invalid container id")
    if not isinstance(policy_path, Path):
        policy_path = Path(policy_path)
    if not isinstance(destination, Path):
        destination = Path(destination)
    host = _load(HOST, "_pre_release_host_revalidation")
    host.require_protected_authority_source()
    for name in (ENGINE, RECORD, "04_scripts/runtime/pre_release_runtime.py"):
        host._protected_path(ROOT / name)
    # A caller-supplied policy is data only; it must be an existing protected
    # root-owned file and is subsequently re-read/validated by the host.
    engine = _load(ENGINE, "_pre_release_engine_revalidation")
    before = require_source(host, engine)
    _new_output(host, destination)
    report = host.revalidate_production(container_id, expected_policy_path=policy_path)
    report = _validate_production_revalidation_report(report, container_id)
    digest = hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    result = {"report": report, "report_sha256": digest}
    raw = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if require_source(host, engine) != before:
        raise PreReleaseError("production source changed during revalidation")
    _write_new(host, destination, raw)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--project")
    parser.add_argument("--record-output", type=Path)
    parser.add_argument("--candidate-key", type=Path)
    parser.add_argument("--ttl-seconds", type=int, default=86400)
    parser.add_argument("--production-policy", type=Path)
    parser.add_argument("--container-id")
    parser.add_argument("--report-output", type=Path)
    args = parser.parse_args(argv)
    try:
        production = args.production_policy is not None
        if production:
            if (args.container_id is None or args.report_output is None or args.project is not None
                    or args.record_output is not None or args.candidate_key is not None
                    or args.ttl_seconds != 86400):
                parser.error("production mode cannot be combined with candidate options")
            result = revalidate_production(args.container_id, args.production_policy, args.report_output)
            print(json.dumps({"PRODUCTION_REVALIDATION": "PASS", **result}, sort_keys=True))
        else:
            if (args.project is None or args.record_output is None or args.candidate_key is None
                    or args.container_id is not None or args.report_output is not None):
                parser.error("candidate mode requires candidate options")
            result = validate_candidate(args.project, args.record_output, args.candidate_key,
                                        ttl_seconds=args.ttl_seconds)
            print(json.dumps({"CANDIDATE_VALIDATION_RECORD": "PASS", "record_id": result["record_id"],
                              "image_id": result["evidence"]["image_id"]}))
        return 0
    except ValidationBlocked:
        print(json.dumps({"CANDIDATE_VALIDATION_RECORD": "BLOCKED", "reason": "LINUX_BUILDER_UNAVAILABLE"}))
        return 3
    except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"CANDIDATE_VALIDATION_RECORD": "FAIL", "reason": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
