"""Fail-closed Windows FULL DAILY launcher control plane.

This module deliberately knows nothing about provider update policy.  It prepares
an isolated, identity-pinned workspace and invokes the existing business CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import shlex
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from filelock import FileLock, Timeout as FileLockTimeout


SCHEMA_VERSION = "windows-full-daily-wrapper/2"
DAILY_SCHEMA = "unified-public-data-daily-update/1"
TRIGGERS = {"manual", "scheduled"}
REQUIRED_PROVIDERS = {
    "tankan", "lutou", "lutou_domestic_basis",
}
UNAVAILABLE = {"SOURCE_UNAVAILABLE", "NETWORK_UNAVAILABLE", "LIVE_VERIFICATION_PENDING"}
SUCCESS_BUSINESS = {"UPDATED", "NO_CHANGE"}
ACCEPTED_BUSINESS = {*SUCCESS_BUSINESS, "PARTIAL_SUCCESS"}
DOMAIN_SUCCESS = {"UPDATED", "NO_CHANGE"}
DOMAIN_INCOMPLETE = {"MISSING", "ERROR", "SKIPPED_DEPENDENCY_UNAVAILABLE"}
PROVIDER_DOMAINS = {
    "tankan": {"tankan_market", "fx"},
    "lutou": {
        "three_oil", "soil_moisture", "weather_observation", "weather_forecast",
    },
    "lutou_domestic_basis": {"domestic_basis"},
}
DOMAIN_DATASETS = {
    "tankan_market": "tankan",
    "fx": "tankan",
    "three_oil": "lutou-three-oil",
    "soil_moisture": "lutou-soil-moisture",
    "weather_observation": "lutou-weather",
    "weather_forecast": "lutou-weather",
    "domestic_basis": "lutou-domestic-basis",
}
PREWARM_WARNINGS = {"SKIPPED", "PARTIAL", "FAIL"}
FAILED_STAGES = {
    "LOCK", "WINDOWS_ENV", "RUNTIME_FILESYSTEM", "REPOSITORY", "PYTHON_RUNTIME", "CREDENTIALS",
    "TAILSCALE", "NETWORK", "TANKAN_TCP", "TANKAN_AUTH", "LUTOU_TCP",
    "LUTOU_AUTH", "SSH", "RUNTIME_BASELINE", "LOCAL_DISK", "REMOTE_IDENTITY",
    "REMOTE_DISK", "PROVIDER_PREFLIGHT", "TANKAN_REFRESH", "THREE_OIL",
    "SOIL_MOISTURE", "WEATHER", "WEATHER_TABLE_PARTITION_CLEANUP",
    "DOMESTIC_BASIS", "DOMESTIC_SPREAD",
    "PROMOTION", "ROLLBACK",
    "CONSUMER_FRESHNESS", "PRODUCTION_PACKAGE", "SERVER_TRANSPORT",
    "SERVER_ACTIVATION", "FORMAL_READ", "PREWARM", "ENTRYPOINT_EXCEPTION",
    "TIMEOUT", "MANIFEST_VALIDATION", "STATUS_SEAL",
}
DEFAULT_REPOSITORY = Path(
    r"C:\Users\xx202\Desktop\codex自动更新\codex-projects\market-data"
)
DEFAULT_PYTHON = DEFAULT_REPOSITORY / ".venv-py312" / "Scripts" / "python.exe"
_SSH_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,199}$")
_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9_./-]+$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_CLOUD_REPARSE_BASE = 0x9000001A
_CLOUD_REPARSE_MASK = 0xFFFF0FFF
_WINDOWS_TOOLS = {
    "git": (Path(r"C:\Program Files\Git\cmd\git.exe"),),
    "ssh": (Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe",),
    "scp": (Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "OpenSSH" / "scp.exe",),
    "tailscale": (Path(r"C:\Program Files\Tailscale\tailscale.exe"),),
}


class WrapperFailure(RuntimeError):
    def __init__(self, stage: str, safe_reason: str):
        if stage not in FAILED_STAGES:
            raise ValueError(f"unknown failed_stage: {stage}")
        super().__init__(safe_reason)
        self.stage, self.safe_reason = stage, safe_reason


def default_runtime_root(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    local_app_data = values.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise WrapperFailure(
            "RUNTIME_FILESYSTEM", "LOCALAPPDATA is unavailable for production runtime",
        )
    root = Path(local_app_data)
    if not root.is_absolute():
        raise WrapperFailure(
            "RUNTIME_FILESYSTEM", "LOCALAPPDATA is not an absolute Windows path",
        )
    return root / "market-data-runtime" / "automation"




def _reparse_tag(path: Path) -> int | None:
    value = path.lstat()
    tag = getattr(value, "st_reparse_tag", 0)
    return int(tag) if tag else None


def _is_cloud_reparse_tag(tag: int | None) -> bool:
    return tag is not None and tag & _CLOUD_REPARSE_MASK == _CLOUD_REPARSE_BASE


def validate_runtime_filesystem(
    automation_root: Path,
    repository: Path,
    *,
    expected_root: Path | None = None,
) -> Mapping[str, object]:
    """Create and validate an owned non-Cloud-Files automation root."""

    requested = automation_root.absolute()
    expected = expected_root.absolute() if expected_root is not None else None
    if expected is not None and requested != expected:
        raise WrapperFailure(
            "RUNTIME_FILESYSTEM", "production runtime is not the expected LocalAppData root",
        )
    repository_resolved = repository.resolve(strict=False)
    requested_resolved = requested.resolve(strict=False)
    if requested_resolved == repository_resolved or requested_resolved.is_relative_to(
        repository_resolved
    ):
        raise WrapperFailure(
            "RUNTIME_FILESYSTEM", "production runtime must not be inside the repository",
        )

    known_sync_roots = {
        Path(value).resolve(strict=False)
        for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
        if (value := os.environ.get(name, "").strip())
    }
    user_profile = os.environ.get("USERPROFILE", "").strip()
    if user_profile:
        profile = Path(user_profile)
        known_sync_roots.update(
            (profile / name).resolve(strict=False) for name in ("Desktop", "Documents")
        )
    if any(
        requested_resolved == root or requested_resolved.is_relative_to(root)
        for root in known_sync_roots
    ):
        raise WrapperFailure(
            "RUNTIME_FILESYSTEM", "production runtime is located in a known synchronized user folder",
        )

    requested.mkdir(parents=True, exist_ok=True)
    inspected: list[dict[str, object]] = []
    current = requested
    stop = Path(current.anchor)
    while True:
        if current.exists():
            tag = _reparse_tag(current)
            inspected.append(
                {
                    "relative_level": len(requested.parts) - len(current.parts),
                    "reparse_tag": None if tag is None else f"0x{tag:08X}",
                    "cloud_files": _is_cloud_reparse_tag(tag),
                }
            )
            if _is_cloud_reparse_tag(tag):
                raise WrapperFailure(
                    "RUNTIME_FILESYSTEM",
                    "production runtime is located on Cloud Files/reparse storage",
                )
        if current == stop:
            break
        current = current.parent
    if requested.is_symlink() or not requested.is_dir():
        raise WrapperFailure(
            "RUNTIME_FILESYSTEM", "production runtime must be a real directory",
        )
    return {
        "runtime_class": "LOCALAPPDATA_LOCAL_FILESYSTEM",
        "cloud_files": False,
        "inspected_path_count": len(inspected),
        "reparse_points": [item for item in inspected if item["reparse_tag"] is not None],
    }


@dataclass(frozen=True)
class Invocation:
    run_id: str
    trigger_source: str
    started_at: str
    repository_main_head: str
    repository_main_tree: str
    production_commit: str
    production_tree: str
    repository_branch: str
    python_executable: str


@dataclass(frozen=True)
class RepositoryIdentity:
    repository_main_head: str
    repository_main_tree: str
    production_commit: str
    production_tree: str
    repository_branch: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_run_id(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return f"full-daily-{moment:%Y%m%dT%H%M%S}.{moment.microsecond:06d}Z-{uuid.uuid4().hex[:8]}"


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def persist_completed_status(
    automation_root: Path,
    final: Mapping[str, Any],
    *,
    full_success: bool,
) -> None:
    """Advance current status; reserve last-success for complete provider success."""

    atomic_write_json(automation_root / "current-status.json", final)
    if full_success:
        atomic_write_json(automation_root / "last-success.json", final)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tool_path(name: str) -> str:
    for candidate in _WINDOWS_TOOLS.get(name, ()):
        if candidate.is_file():
            return str(candidate)
    resolved = shutil.which(name)
    if resolved:
        return resolved
    raise WrapperFailure("WINDOWS_ENV", f"required executable is unavailable: {name}")


def runtime_environment() -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "LUTOU_HOST", "LUTOU_PORT", "LUTOU_USER", "LUTOU_PASSWORD",
        "PYTHONHOME", "PYTHONPATH",
    ):
        env.pop(name, None)
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONNOUSERSITE": "1"})
    tool_directories = [str(Path(tool_path(name)).parent) for name in ("git", "ssh", "scp", "tailscale")]
    env["PATH"] = os.pathsep.join(dict.fromkeys([*tool_directories, env.get("PATH", "")]))
    return env


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        [tool_path("git"), "-C", str(repository), *args], text=True, encoding="utf-8",
        capture_output=True, check=False, timeout=30,
    )
    if result.returncode:
        raise WrapperFailure("REPOSITORY", "Git identity probe failed")
    return result.stdout.strip()


def trusted_remote_main(repository: Path) -> str:
    advertised = _git(repository, "ls-remote", "--exit-code", "origin", "refs/heads/main")
    fields = advertised.split()
    if len(fields) != 2 or fields[1] != "refs/heads/main" or not _COMMIT_SHA.fullmatch(fields[0]):
        raise WrapperFailure("REPOSITORY", "origin did not advertise one valid main SHA")
    remote_main = fields[0]
    _git(repository, "fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
    if _git(repository, "rev-parse", "refs/remotes/origin/main") != remote_main:
        raise WrapperFailure("REPOSITORY", "fetched origin/main differs from live remote advertisement")
    return remote_main


def repository_identity(
    repository: Path, production_commit: str, *, remote_main: str | None = None,
) -> RepositoryIdentity:
    if not (repository / ".git").exists():
        raise WrapperFailure("REPOSITORY", "formal repository is not a Git worktree")
    if not _COMMIT_SHA.fullmatch(production_commit):
        raise WrapperFailure("REPOSITORY", "approved production commit must be a full lowercase Git SHA")
    trusted_main = remote_main or trusted_remote_main(repository)
    if not _COMMIT_SHA.fullmatch(trusted_main):
        raise WrapperFailure("REPOSITORY", "trusted origin/main must be a full lowercase Git SHA")
    object_probe = subprocess.run(
        [tool_path("git"), "-C", str(repository), "cat-file", "-t", production_commit],
        text=True, encoding="utf-8", capture_output=True, check=False, timeout=30,
    )
    if object_probe.returncode or object_probe.stdout.strip() != "commit":
        raise WrapperFailure("REPOSITORY", "approved production commit is not a known local commit")
    ancestor_probe = subprocess.run(
        [tool_path("git"), "-C", str(repository), "merge-base", "--is-ancestor", production_commit, trusted_main],
        text=True, encoding="utf-8", capture_output=True, check=False, timeout=30,
    )
    if ancestor_probe.returncode == 1:
        raise WrapperFailure("REPOSITORY", "approved production commit is not an ancestor of trusted origin/main")
    if ancestor_probe.returncode:
        raise WrapperFailure("REPOSITORY", "production ancestry probe failed")
    production_tree = _git(repository, "rev-parse", f"{production_commit}^{{tree}}")
    trusted_main_tree = _git(repository, "rev-parse", f"{trusted_main}^{{tree}}")
    return RepositoryIdentity(
        trusted_main, trusted_main_tree, production_commit, production_tree, "NOT_APPLICABLE"
    )


def validate_tool_repo_identity(destination: Path, head: str, tree: str) -> None:
    if _git(destination, "rev-parse", "HEAD") != head:
        raise WrapperFailure("REPOSITORY", "trusted workspace HEAD mismatch")
    if _git(destination, "rev-parse", "HEAD^{tree}") != tree:
        raise WrapperFailure("REPOSITORY", "trusted workspace tree mismatch")
    if _git(destination, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD":
        raise WrapperFailure("REPOSITORY", "trusted workspace is not detached")
    if _git(destination, "status", "--porcelain=v1", "--untracked-files=all"):
        raise WrapperFailure("REPOSITORY", "trusted workspace is not clean")


def create_trusted_tool_repo(source: Path, destination: Path, head: str, tree: str) -> None:
    result = subprocess.run(
        [
            tool_path("git"), "clone", "-c", "core.longpaths=true",
            "--local", "--no-hardlinks", "--no-checkout", str(source), str(destination),
        ],
        text=True, encoding="utf-8", capture_output=True, check=False, timeout=300,
    )
    if result.returncode:
        raise WrapperFailure("REPOSITORY", "local trusted clone failed")
    checkout = subprocess.run(
        [tool_path("git"), "-C", str(destination), "checkout", "--detach", head],
        text=True, encoding="utf-8", capture_output=True, check=False, timeout=120,
    )
    if checkout.returncode:
        raise WrapperFailure("REPOSITORY", "trusted workspace checkout failed")
    validate_tool_repo_identity(destination, head, tree)


def write_runtime_marker(runtime: Path, run_id: str) -> None:
    runtime.mkdir(parents=True, exist_ok=False)
    atomic_write_json(runtime / ".market-data-runtime.json", {
        "schema_version": 1, "runtime_id": run_id, "classification": "isolated-dev",
        "module_id": "international-spread", "created_at": utc_now(),
    })


def seed_from_production_package(package: Path, runtime: Path, tool_repo: Path) -> dict[str, Any]:
    from agri_research_agent.pipelines.public_data_delivery import validate_production_package

    validated = validate_production_package(package)
    source = validated.directory / "data" / "public-market-data"
    shutil.copytree(source, runtime / "public-market-data")
    packages = runtime / "public-data-packages"
    packages.mkdir()
    shutil.copytree(validated.directory, packages / validated.package_id)
    local_seeds: dict[str, str] = {}
    for filename in (
        "historical_spread_database.parquet", "historical_spread_database.xlsx",
        "historical_price_long.xlsx",
    ):
        candidate = DEFAULT_REPOSITORY / "01_data" / filename
        if candidate.is_file():
            target = tool_repo / "01_data" / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, target)
            local_seeds[filename] = sha256_file(candidate)
    weather = DEFAULT_REPOSITORY / "01_data" / "processed" / "weather"
    if weather.is_dir():
        shutil.copytree(weather, tool_repo / "01_data" / "processed" / "weather")
        for item in sorted(weather.rglob("*")):
            if item.is_file():
                local_seeds[f"processed/weather/{item.relative_to(weather).as_posix()}"] = sha256_file(item)
    return {
        "package_id": validated.package_id,
        "bundle_sha256": validated.manifest["bundle_sha256"],
        "delivery_identity_sha256": validated.manifest["delivery_identity_sha256"],
        "manifest_sha256": sha256_file(validated.directory / "manifest.json"),
        "local_seed_files": local_seeds,
    }


def load_env_file_structure(path: Path, required: Sequence[str]) -> dict[str, str]:
    if not path.is_file():
        raise WrapperFailure("CREDENTIALS", f"credential file is missing: {path.name}")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip() in required and value.strip():
            values[key.strip()] = value.strip()
    missing = sorted(set(required) - values.keys())
    if missing:
        raise WrapperFailure("CREDENTIALS", f"credential file lacks required fields: {','.join(missing)}")
    return values


def tcp_probe(host: str, port: int, *, stage: str, timeout: float = 10) -> None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return
    except OSError as exc:
        raise WrapperFailure(stage, f"TCP endpoint unavailable: {type(exc).__name__}") from exc


def ssh_command(target: str, remote_command: str, *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    command = [
        tool_path("ssh"), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3", "-T", target, remote_command,
    ]
    try:
        return subprocess.run(
            command, text=True, encoding="utf-8", capture_output=True,
            check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WrapperFailure("TIMEOUT", "SSH preflight total timeout exhausted") from exc


def download_runtime_baseline(
    target: str, remote_store_root: str, package_id: str, destination: Path,
    *, timeout: float = 1800,
) -> None:
    if not package_id.startswith("public-current-") or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in package_id):
        raise WrapperFailure("REMOTE_IDENTITY", "remote package_id is unsafe")
    remote_package = f"{remote_store_root.rstrip('/')}/releases/{package_id}"
    destination.mkdir(parents=True, exist_ok=False)
    command = [
        tool_path("scp"), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3", "-r", "--",
        f"{target}:{remote_package}/manifest.json", f"{target}:{remote_package}/data",
        str(destination),
    ]
    try:
        result = subprocess.run(
            command, text=True, encoding="utf-8", capture_output=True,
            check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WrapperFailure("TIMEOUT", "runtime baseline SCP total timeout exhausted") from exc
    if result.returncode:
        raise WrapperFailure("RUNTIME_BASELINE", "runtime baseline download failed")


def require_baseline_matches(local_identity: Mapping[str, Any], remote_pointer: Mapping[str, Any]) -> None:
    if local_identity.get("package_id") != remote_pointer.get("package_id"):
        raise WrapperFailure("RUNTIME_BASELINE", "local baseline package does not match remote Current")


def gate(name: str, action: Callable[[], str | Mapping[str, Any] | None]) -> dict[str, Any]:
    started = utc_now()
    try:
        detail = action()
        return {"gate": name, "started_at": started, "completed_at": utc_now(), "status": "PASS", "safe_reason": detail or "PASS"}
    except WrapperFailure:
        raise
    except Exception as exc:
        raise WrapperFailure(name, f"{name} preflight failed: {type(exc).__name__}") from exc


def validate_daily_manifest(path: Path, run_id: str, process_exit_code: int) -> dict[str, Any]:
    if not path.is_file():
        stage = "ENTRYPOINT_EXCEPTION" if process_exit_code else "MANIFEST_VALIDATION"
        raise WrapperFailure(stage, "FULL DAILY failed without a Daily Manifest" if process_exit_code else "Daily Manifest is missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WrapperFailure("MANIFEST_VALIDATION", "Daily Manifest is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != DAILY_SCHEMA:
        raise WrapperFailure("MANIFEST_VALIDATION", "Daily Manifest schema is invalid")
    if value.get("run_id") != run_id:
        raise WrapperFailure("MANIFEST_VALIDATION", "Daily Manifest run_id mismatch")
    if process_exit_code != 0:
        raise WrapperFailure(classify_manifest_failure(value), "FULL DAILY process and Daily Manifest report failure")
    business = value.get("business_status")
    if value.get("succeeded") is not True or business not in ACCEPTED_BUSINESS:
        raise WrapperFailure("MANIFEST_VALIDATION", "Daily Manifest reports an incomplete business outcome")
    sources = value.get("sources")
    if not isinstance(sources, list):
        raise WrapperFailure("MANIFEST_VALIDATION", "Daily Manifest source results are invalid")
    provider_status = {
        str(item.get("source")): str(item.get("status"))
        for item in sources if isinstance(item, dict)
    }
    if set(provider_status) != REQUIRED_PROVIDERS:
        raise WrapperFailure("MANIFEST_VALIDATION", "Daily Manifest required provider set is incomplete")
    domain_completed = 0
    domain_incomplete = 0
    for item in sources:
        if not isinstance(item, Mapping):
            raise WrapperFailure("MANIFEST_VALIDATION", "Daily Manifest source result is invalid")
        domains = item.get("domains")
        if not isinstance(domains, Mapping) or not domains:
            if business in SUCCESS_BUSINESS and str(item.get("status")) in SUCCESS_BUSINESS:
                # Historical complete-success manifests predate domain-level evidence.
                # They remain valid inputs for downstream audit/alert consumers, while
                # PARTIAL_SUCCESS always requires the stricter dataset-preservation proof.
                domain_completed += 1
                continue
            raise WrapperFailure("MANIFEST_VALIDATION", "Daily Manifest domain evidence is incomplete")
        provider = str(item.get("source"))
        if set(map(str, domains)) != PROVIDER_DOMAINS[provider]:
            raise WrapperFailure("MANIFEST_VALIDATION", "Daily Manifest domain set is incomplete")
        before = item.get("current_before")
        after = item.get("current_after")
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            raise WrapperFailure("MANIFEST_VALIDATION", "Daily Manifest Current evidence is invalid")
        before_datasets = before.get("dataset_identities")
        after_datasets = after.get("dataset_identities")
        if not isinstance(before_datasets, Mapping) or not isinstance(after_datasets, Mapping):
            raise WrapperFailure("MANIFEST_VALIDATION", "Daily Manifest dataset Current evidence is invalid")
        item_incomplete = 0
        for domain, raw_status in domains.items():
            status = str(raw_status)
            if status in DOMAIN_SUCCESS:
                domain_completed += 1
                continue
            if status not in DOMAIN_INCOMPLETE:
                raise WrapperFailure("MANIFEST_VALIDATION", "Daily Manifest domain status is invalid")
            domain_incomplete += 1
            item_incomplete += 1
            dataset = DOMAIN_DATASETS.get(str(domain))
            if dataset is None or before_datasets.get(dataset) != after_datasets.get(dataset):
                raise WrapperFailure(
                    "MANIFEST_VALIDATION",
                    "incomplete domain changed its trusted Public Current identity",
                )
        item_status = str(item.get("status"))
        if (item_incomplete == 0) != (item_status in SUCCESS_BUSINESS):
            raise WrapperFailure(
                "MANIFEST_VALIDATION", "provider status conflicts with domain evidence"
            )
    if business in SUCCESS_BUSINESS:
        if domain_incomplete or any(
            status not in SUCCESS_BUSINESS for status in provider_status.values()
        ):
            raise WrapperFailure("MANIFEST_VALIDATION", "complete success contains an incomplete provider")
    elif business == "PARTIAL_SUCCESS":
        if not domain_completed or not domain_incomplete:
            raise WrapperFailure("MANIFEST_VALIDATION", "partial success domain accounting is invalid")
    freshness = value.get("consumer_freshness_validation")
    if not isinstance(freshness, dict) or freshness.get("status") in {"STALE", "FAIL", None}:
        raise WrapperFailure("CONSUMER_FRESHNESS", "consumer freshness contract did not pass")
    package = value.get("production_data_package")
    if not isinstance(package, dict) or package.get("status") not in {"GENERATED", "NO_CHANGE", "REUSED", "SKIPPED"}:
        raise WrapperFailure("PRODUCTION_PACKAGE", "production package evidence is invalid")
    server = str(value.get("server_sync"))
    if server not in {"SYNCED", "NO_CHANGE", "SKIPPED"}:
        raise WrapperFailure("SERVER_TRANSPORT", "server transport did not succeed")
    if server == "SYNCED" and any(value.get(key) != "PASS" for key in ("manifest", "sha", "atomic_current_switch", "formal_read_validation")):
        raise WrapperFailure("FORMAL_READ", "server activation/formal-read evidence is incomplete")
    warnings: list[str] = []
    prewarm = value.get("prewarm", {})
    prewarm_status = str(prewarm.get("status", "SKIPPED")) if isinstance(prewarm, dict) else "SKIPPED"
    if prewarm_status in PREWARM_WARNINGS:
        warnings.append(f"PREWARM_{prewarm_status}")
    return {"manifest": value, "prewarm_status": prewarm_status, "warnings": warnings}


def classify_manifest_failure(manifest: Mapping[str, Any]) -> str:
    root_failure = manifest.get("root_failure")
    if isinstance(root_failure, Mapping):
        structured = _structured_failure_stage(root_failure)
        if structured is not None:
            return structured
    transaction = manifest.get("transaction")
    if isinstance(transaction, Mapping) and transaction.get("rollback") == "FAIL":
        return "ROLLBACK"
    safe_reason = str(manifest.get("safe_reason", ""))
    if "ServerTransportTimeout" in safe_reason:
        return "TIMEOUT"
    if "ServerActivationFailure" in safe_reason:
        return "SERVER_ACTIVATION"
    if manifest.get("formal_read_validation") == "FAIL":
        return "FORMAL_READ"
    if manifest.get("atomic_current_switch") == "FAIL":
        return "SERVER_ACTIVATION"
    if manifest.get("server_sync") == "FAILED":
        return "SERVER_TRANSPORT"
    package = manifest.get("production_data_package")
    if isinstance(package, Mapping) and package.get("status") == "FAILED":
        return "PRODUCTION_PACKAGE"
    if manifest.get("delivery_artifact_producer") == "FAIL":
        return "DOMESTIC_SPREAD"
    freshness = manifest.get("consumer_freshness_validation")
    if isinstance(freshness, Mapping) and freshness.get("status") in {"STALE", "FAIL"}:
        return "CONSUMER_FRESHNESS"
    sources = manifest.get("sources")
    if isinstance(sources, list):
        failed = [item for item in sources if isinstance(item, Mapping) and item.get("status") not in SUCCESS_BUSINESS]
        for item in failed:
            source_root = item.get("root_failure")
            if isinstance(source_root, Mapping):
                structured = _structured_failure_stage(source_root)
                if structured is not None:
                    return structured
        if any(str(item.get("read")) != "READY" for item in failed):
            return "PROVIDER_PREFLIGHT"
        names = {str(item.get("source")) for item in failed}
        if "tankan" in names:
            return "TANKAN_REFRESH"
        if "lutou_domestic_basis" in names:
            return "DOMESTIC_BASIS"
        if "lutou" in names:
            reasons = " ".join(str(item.get("safe_reason", "")) for item in failed).lower()
            if "three" in reasons and "oil" in reasons:
                return "THREE_OIL"
            if "soil" in reasons:
                return "SOIL_MOISTURE"
            if "weather" in reasons:
                return "WEATHER"
            return "ENTRYPOINT_EXCEPTION"
    return "ENTRYPOINT_EXCEPTION"


def _structured_failure_stage(root_failure: Mapping[str, Any]) -> str | None:
    stage = str(root_failure.get("stage", "")).upper()
    domain = str(root_failure.get("domain", "")).lower()
    if stage in {"PROVIDER_PREFLIGHT", "READINESS", "PREFLIGHT"}:
        return "PROVIDER_PREFLIGHT"
    if stage == "PROMOTION":
        return "PROMOTION"
    if stage == "ROLLBACK":
        return "ROLLBACK"
    if stage == "TIMEOUT":
        return "TIMEOUT"
    if stage == "WEATHER_TABLE_PARTITION_CLEANUP":
        return "WEATHER_TABLE_PARTITION_CLEANUP"
    if domain == "three_oil" or stage == "THREE_OIL":
        return "THREE_OIL"
    if domain == "soil_moisture" or stage == "SOIL_MOISTURE":
        return "SOIL_MOISTURE"
    if domain == "weather" or stage in {"WEATHER", "SOIL_EVIDENCE"}:
        return "WEATHER"
    if domain == "domestic_basis" or stage == "DOMESTIC_BASIS":
        return "DOMESTIC_BASIS"
    return None


def make_final_status(
    invocation: Invocation, *, status: str, completed_at: str, failed_stage: str | None,
    safe_reason: str, process_exit_code: int | None = None,
    manifest_path: Path | None = None, manifest: Mapping[str, Any] | None = None,
    warnings: Sequence[str] = (), prewarm_status: str | None = None,
) -> dict[str, Any]:
    started = datetime.fromisoformat(invocation.started_at.replace("Z", "+00:00"))
    completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    package = manifest.get("production_data_package", {}) if manifest else {}
    return {
        "schema_version": SCHEMA_VERSION, "run_id": invocation.run_id,
        "trigger_source": invocation.trigger_source, "status": status,
        "failed_stage": failed_stage, "safe_reason": safe_reason,
        "process_exit_code": process_exit_code,
        "business_status": manifest.get("business_status") if manifest else None,
        "daily_succeeded": manifest.get("succeeded") if manifest else None,
        "daily_manifest_path": str(manifest_path) if manifest_path else None,
        "daily_manifest_sha256": sha256_file(manifest_path) if manifest_path and manifest_path.is_file() else None,
        "server_sync": manifest.get("server_sync") if manifest else None,
        "production_package_id": package.get("package_id") if isinstance(package, dict) else None,
        "prewarm_status": prewarm_status, "warnings": list(warnings),
        "repository_main_head": invocation.repository_main_head,
        "repository_main_tree": invocation.repository_main_tree,
        "production_commit": invocation.production_commit,
        "production_tree": invocation.production_tree,
        "started_at": invocation.started_at, "completed_at": completed_at,
        "duration_seconds": round((completed - started).total_seconds(), 3),
    }


@contextmanager
def lifecycle_lock(lock_path: Path, active_path: Path, run_id: str) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(lock_path, timeout=0)
    try:
        lock.acquire()
    except FileLockTimeout as exc:
        raise WrapperFailure("LOCK", "another FULL DAILY wrapper owns the lifecycle lock") from exc
    try:
        atomic_write_json(active_path, {"schema_version": SCHEMA_VERSION, "active_run_id": run_id, "acquired_at": utc_now()})
        yield
    finally:
        active_path.unlink(missing_ok=True)
        lock.release()


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        process.kill()


def run_logged(command: Sequence[str], cwd: Path, env: Mapping[str, str], log_path: Path, timeout: float) -> int:
    with log_path.open("w", encoding="utf-8", newline="\n") as log:
        process = subprocess.Popen(
            list(command), cwd=cwd, env=dict(env), stdout=log, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8",
        )
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_tree(process)
            process.wait(timeout=30)
            raise WrapperFailure("TIMEOUT", "FULL DAILY total timeout exhausted") from exc


def business_command(
    python: Path, tool_repo: Path, runtime: Path, run_id: str,
    ssh_target: str, remote_store_root: str, activation_image_id: str,
    provider_preflight_evidence: Path,
) -> list[str]:
    return [
        str(python), "-I", str(tool_repo / "04_scripts" / "refresh_public_data.py"),
        "--runtime-root", str(runtime), "--weather-baseline-root",
        str(tool_repo / "01_data" / "processed" / "weather"),
        "--packages-root", str(runtime / "public-data-packages"), "--run-id", run_id,
        "--ssh-target", ssh_target, "--remote-store-root", remote_store_root,
        "--activation-image-id", activation_image_id, "--prewarm",
        "--provider-preflight-evidence", str(provider_preflight_evidence),
    ]


def run_provider_preflight(
    python: Path, tool_repo: Path, runtime: Path, run_id: str,
    tankan_secret: Path, lutou_secret: Path, *, timeout: float = 900,
) -> Mapping[str, Any]:
    runtime.mkdir(parents=True, exist_ok=True)
    evidence_path = runtime / "provider-preflight-evidence.json"
    human_stdout_path = runtime / "provider-preflight.stdout.log"
    human_stderr_path = runtime / "provider-preflight.stderr.log"
    evidence_path.unlink(missing_ok=True)
    preflight_run_id = f"{run_id}-preflight"
    command = [
        str(python), "-I", str(tool_repo / "04_scripts" / "refresh_public_data.py"),
        "--runtime-root", str(runtime), "--weather-baseline-root",
        str(tool_repo / "01_data" / "processed" / "weather"), "--run-id",
        preflight_run_id, "--tankan-secret-file", str(tankan_secret),
        "--lutou-secret-file", str(lutou_secret), "--dry-run",
        "--evidence-output", str(evidence_path),
    ]
    env = runtime_environment()
    try:
        with human_stdout_path.open("wb") as human_stdout, human_stderr_path.open("wb") as human_stderr:
            result = subprocess.run(
                command, cwd=tool_repo, env=env, stdout=human_stdout,
                stderr=human_stderr, check=False, timeout=timeout,
            )
    except subprocess.TimeoutExpired as exc:
        raise WrapperFailure("TIMEOUT", "provider preflight total timeout exhausted") from exc
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise WrapperFailure("PROVIDER_PREFLIGHT", "provider preflight returned invalid evidence") from exc
    if not isinstance(payload, dict):
        raise WrapperFailure("PROVIDER_PREFLIGHT", "provider preflight returned invalid evidence")
    sources = payload.get("sources")
    valid_sources = isinstance(sources, list) and all(
        isinstance(item, dict)
        and isinstance(item.get("source"), str)
        and isinstance(item.get("status"), str)
        for item in sources
    )
    source_names = [item["source"] for item in sources] if valid_sources else []
    valid_identity = (
        payload.get("schema_version") == "unified-public-data-dry-run/1"
        and payload.get("dry_run") is True
        and payload.get("run_id") == preflight_run_id
        and valid_sources
        and len(source_names) == len(set(source_names))
        and set(source_names) == REQUIRED_PROVIDERS
    )
    allowed_statuses = {
        "READY", "DEPENDENCY_UNAVAILABLE", "LIVE_VERIFICATION_PENDING",
        "SOURCE_UNAVAILABLE", "NETWORK_UNAVAILABLE", "AUTH_FAILURE",
        "SOURCE_SCHEMA_FAILURE", "INGESTION_FAILURE", "QC_FAILURE",
        "PROMOTION_FAILURE",
    }
    valid_statuses = valid_sources and all(
        item.get("status") in allowed_statuses for item in sources
        if isinstance(item, dict)
    )
    ready_count = sum(
        item.get("status") == "READY" for item in sources if isinstance(item, dict)
    )
    valid_details = valid_sources and all(
        isinstance(item.get("current_identity"), Mapping)
        and isinstance(item["current_identity"].get("dataset_identities"), Mapping)
        and isinstance(item.get("domains"), Mapping)
        and set(map(str, item["domains"]))
        == PROVIDER_DOMAINS.get(str(item.get("source")), set())
        and all(
            str(status) in {"READY", *DOMAIN_SUCCESS, *DOMAIN_INCOMPLETE}
            for status in item["domains"].values()
        )
        and (
            all(str(status) == "READY" for status in item["domains"].values())
            == (item.get("status") == "READY")
        )
        for item in sources if isinstance(item, dict)
    )
    if result.returncode or not valid_identity or not valid_statuses or not valid_details:
        raise WrapperFailure("PROVIDER_PREFLIGHT", "provider preflight returned invalid evidence")
    if ready_count == 0:
        raise WrapperFailure("PROVIDER_PREFLIGHT", "no independent provider passed read-only preflight")
    return {
        "sources": [
            {
                "source": item.get("source"),
                "status": item.get("status"),
                "domains": item.get("domains", {}),
                "safe_reason": item.get("safe_reason"),
            }
            for item in sources
        ],
        "ready_provider_count": ready_count,
        "evidence_path": str(evidence_path),
    }


def run_wrapper(trigger_source: str, *, repository: Path = DEFAULT_REPOSITORY, automation_root: Path | None = None, python: Path = DEFAULT_PYTHON, timeout_seconds: float = 14400) -> int:
    if trigger_source not in TRIGGERS:
        raise ValueError("trigger_source must be manual or scheduled")
    formal_runtime = automation_root is None
    automation_root = default_runtime_root() if formal_runtime else automation_root
    assert automation_root is not None
    run_id, started = new_run_id(), utc_now()
    try:
        filesystem_evidence = validate_runtime_filesystem(
            automation_root,
            repository,
            expected_root=default_runtime_root() if formal_runtime else None,
        )
    except WrapperFailure as exc:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": run_id,
                    "status": "FAILED",
                    "failed_stage": exc.stage,
                    "safe_reason": exc.safe_reason,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    run_dir = automation_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    lock_path, active_path = automation_root / "full-daily.lock", automation_root / "active-run.json"
    invocation = Invocation(
        run_id, trigger_source, started, "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", str(python)
    )
    process_exit_code: int | None = None
    daily_manifest_path: Path | None = None
    daily_manifest: Mapping[str, Any] | None = None
    atomic_write_json(run_dir / "invocation.json", {**invocation.__dict__, "schema_version": SCHEMA_VERSION})
    try:
        with lifecycle_lock(lock_path, active_path, run_id):
            production_commit = os.environ.get("MARKET_DATA_FULL_DAILY_PRODUCTION_COMMIT", "").strip()
            identity = repository_identity(repository, production_commit)
            invocation = Invocation(
                run_id, trigger_source, started,
                identity.repository_main_head, identity.repository_main_tree,
                identity.production_commit, identity.production_tree,
                identity.repository_branch, str(python),
            )
            atomic_write_json(run_dir / "invocation.json", {**invocation.__dict__, "schema_version": SCHEMA_VERSION})
            gates: list[dict[str, Any]] = []
            def record_gate(name: str, action: Callable[[], str | Mapping[str, Any] | None]) -> None:
                try:
                    gates.append(gate(name, action))
                except WrapperFailure as exc:
                    gates.append({"gate": name, "started_at": utc_now(), "completed_at": utc_now(), "status": "FAIL", "safe_reason": exc.safe_reason})
                    atomic_write_json(run_dir / "preflight.json", {"schema_version": SCHEMA_VERSION, "run_id": run_id, "gates": gates})
                    raise
                atomic_write_json(run_dir / "preflight.json", {"schema_version": SCHEMA_VERSION, "run_id": run_id, "gates": gates})
            record_gate("RUNTIME_FILESYSTEM", lambda: filesystem_evidence)
            record_gate("LOCK", lambda: "exclusive OS lock acquired")
            def windows_gate() -> Mapping[str, str]:
                if platform.system() != "Windows":
                    raise WrapperFailure("WINDOWS_ENV", "wrapper requires Windows")
                return {name: tool_path(name) for name in ("git", "ssh", "scp", "tailscale")}
            record_gate("WINDOWS_ENV", windows_gate)
            record_gate("REPOSITORY", lambda: {
                "repository_main_head": identity.repository_main_head,
                "production_commit": identity.production_commit,
            })
            def python_gate() -> str:
                if python.resolve() != DEFAULT_PYTHON.resolve() or not python.is_file():
                    raise WrapperFailure("PYTHON_RUNTIME", "exact project Python executable is unavailable")
                probe = subprocess.run([str(python), "-c", "import sys,pyarrow,pandas,yaml,filelock; assert sys.version_info[:2] == (3,12)"], capture_output=True, check=False, timeout=30)
                if probe.returncode:
                    raise WrapperFailure("PYTHON_RUNTIME", "Python 3.12 dependency probe failed")
                return "exact Python 3.12 runtime and required imports passed"
            record_gate("PYTHON_RUNTIME", python_gate)
            home = Path.home()
            tankan_file, lutou_file = home / ".market-data-secrets" / "tankan.env", home / ".market-data-secrets" / "lutou.env"
            ssh_target = os.environ.get("MARKET_DATA_SSH_TARGET", "").strip()
            remote_store = os.environ.get("MARKET_DATA_REMOTE_STORE_ROOT", "").strip()
            image_id = os.environ.get("MARKET_DATA_ACTIVATION_IMAGE_ID", "").strip()
            if not all((ssh_target, remote_store, image_id)):
                raise WrapperFailure("CREDENTIALS", "required non-secret FULL DAILY configuration is incomplete")
            if not _SSH_TARGET.fullmatch(ssh_target) or not _REMOTE_PATH.fullmatch(remote_store) or ".." in Path(remote_store).parts or not _IMAGE_ID.fullmatch(image_id):
                raise WrapperFailure("CREDENTIALS", "FULL DAILY configuration contains an unsafe identity")
            record_gate("CREDENTIALS", lambda: "shared non-secret production configuration is complete")
            remote = ssh_command(ssh_target, "true")
            if remote.returncode:
                raise WrapperFailure("SSH", "strict non-interactive SSH preflight failed")
            record_gate("SSH", lambda: "strict non-interactive SSH passed")
            pointer_path = f"{remote_store.rstrip('/')}/current.json"
            pointer_probe = ssh_command(ssh_target, f"cat -- {shlex.quote(pointer_path)}", timeout=45)
            if pointer_probe.returncode:
                raise WrapperFailure("REMOTE_IDENTITY", "remote Current identity probe failed")
            try:
                remote_pointer = json.loads(pointer_probe.stdout)
            except ValueError as exc:
                raise WrapperFailure("REMOTE_IDENTITY", "remote Current identity is invalid JSON") from exc
            if remote_pointer.get("schema_version") != "public-current-server-pointer/2":
                raise WrapperFailure("REMOTE_IDENTITY", "remote Current identity schema is invalid")
            record_gate("REMOTE_IDENTITY", lambda: {"package_id": remote_pointer.get("package_id")})
            disk_probe = ssh_command(ssh_target, f"df -Pk -- {shlex.quote(remote_store)} | tail -1", timeout=45)
            if disk_probe.returncode:
                raise WrapperFailure("REMOTE_DISK", "remote disk probe failed")
            try:
                remote_free_kib = int(disk_probe.stdout.split()[-3])
            except (IndexError, ValueError) as exc:
                raise WrapperFailure("REMOTE_DISK", "remote disk probe returned invalid output") from exc
            if remote_free_kib < 5 * 1024 * 1024:
                raise WrapperFailure("REMOTE_DISK", "less than 5 GiB remote disk space remains")
            record_gate("REMOTE_DISK", lambda: f"free_kib={remote_free_kib}")
            usage = shutil.disk_usage(automation_root)
            if usage.free < 10 * 1024**3:
                raise WrapperFailure("LOCAL_DISK", "less than 10 GiB local disk space remains")
            record_gate("LOCAL_DISK", lambda: f"free_bytes={usage.free}")
            tool_repo, runtime = run_dir / "tool-repo", run_dir / "runtime"
            create_trusted_tool_repo(
                repository, tool_repo, identity.production_commit, identity.production_tree
            )
            write_runtime_marker(runtime, run_id)
            baseline = run_dir / "runtime-baseline" / str(remote_pointer["package_id"])
            download_runtime_baseline(ssh_target, remote_store, str(remote_pointer["package_id"]), baseline)
            baseline_identity = seed_from_production_package(baseline, runtime, tool_repo)
            require_baseline_matches(baseline_identity, remote_pointer)
            record_gate("RUNTIME_BASELINE", lambda: baseline_identity)
            record_gate("PROVIDER_PREFLIGHT", lambda: run_provider_preflight(
                python, tool_repo, runtime, run_id, tankan_file, lutou_file
            ))
            env = runtime_environment()
            command = business_command(
                python, tool_repo, runtime, run_id, ssh_target, remote_store, image_id,
                runtime / "provider-preflight-evidence.json",
            )
            process_exit_code = run_logged(command, tool_repo, env, run_dir / "run.log", timeout_seconds)
            (run_dir / "process-exit-code.txt").write_text(f"{process_exit_code}\n", encoding="utf-8")
            daily_manifest_path = runtime / "public-data-daily" / "runs" / run_id / "manifest.json"
            if daily_manifest_path.is_file():
                try:
                    loaded_manifest = json.loads(daily_manifest_path.read_text(encoding="utf-8"))
                    if isinstance(loaded_manifest, dict):
                        daily_manifest = loaded_manifest
                except (OSError, UnicodeError, ValueError):
                    pass
            evaluation = validate_daily_manifest(daily_manifest_path, run_id, process_exit_code)
            business_status = str(evaluation["manifest"]["business_status"])
            wrapper_status = (
                "PARTIAL_SUCCESS"
                if business_status == "PARTIAL_SUCCESS"
                else "SUCCESS"
            )
            final = make_final_status(invocation, status=wrapper_status, completed_at=utc_now(), failed_stage=None, safe_reason="FULL DAILY completed and manifest contract passed", process_exit_code=process_exit_code, manifest_path=daily_manifest_path, manifest=evaluation["manifest"], warnings=evaluation["warnings"], prewarm_status=evaluation["prewarm_status"])
            atomic_write_json(run_dir / "final-status.json", final)
            persist_completed_status(
                automation_root, final, full_success=wrapper_status == "SUCCESS"
            )
            return 0
    except WrapperFailure as exc:
        active_id = None
        if exc.stage == "LOCK" and active_path.is_file():
            try:
                active_id = json.loads(active_path.read_text(encoding="utf-8")).get("active_run_id")
            except (OSError, ValueError):
                pass
        status = "SKIPPED_ALREADY_RUNNING" if exc.stage == "LOCK" else "FAILED"
        reason = exc.safe_reason + (f"; active_run_id={active_id}" if active_id else "")
        preflight_path = run_dir / "preflight.json"
        if exc.stage != "LOCK":
            try:
                preflight = json.loads(preflight_path.read_text(encoding="utf-8")) if preflight_path.is_file() else {"schema_version": SCHEMA_VERSION, "run_id": run_id, "gates": []}
                gates_value = preflight.get("gates", [])
                if not gates_value or gates_value[-1].get("gate") != exc.stage or gates_value[-1].get("status") != "FAIL":
                    gates_value.append({"gate": exc.stage, "started_at": utc_now(), "completed_at": utc_now(), "status": "FAIL", "safe_reason": exc.safe_reason})
                preflight["gates"] = gates_value
                atomic_write_json(preflight_path, preflight)
            except Exception:
                pass
        prewarm_value = daily_manifest.get("prewarm", {}) if daily_manifest else {}
        final = make_final_status(
            invocation, status=status, completed_at=utc_now(), failed_stage=exc.stage,
            safe_reason=reason, process_exit_code=process_exit_code,
            manifest_path=daily_manifest_path, manifest=daily_manifest,
            prewarm_status=str(prewarm_value.get("status")) if isinstance(prewarm_value, Mapping) and prewarm_value.get("status") is not None else None,
        )
        atomic_write_json(run_dir / "final-status.json", final)
        if exc.stage != "LOCK":
            atomic_write_json(automation_root / "current-status.json", final)
        return 3 if exc.stage == "LOCK" else 1
    except Exception as exc:
        final = make_final_status(invocation, status="FAILED", completed_at=utc_now(), failed_stage="ENTRYPOINT_EXCEPTION", safe_reason=f"wrapper exception: {type(exc).__name__}")
        atomic_write_json(run_dir / "final-status.json", final)
        atomic_write_json(automation_root / "current-status.json", final)
        return 1


__all__ = [
    "Invocation", "RepositoryIdentity", "WrapperFailure", "atomic_write_json", "business_command",
    "create_trusted_tool_repo", "download_runtime_baseline", "lifecycle_lock", "make_final_status", "new_run_id", "require_baseline_matches",
    "default_runtime_root", "repository_identity", "run_logged", "run_provider_preflight", "run_wrapper", "ssh_command", "trusted_remote_main", "validate_daily_manifest",
    "validate_runtime_filesystem", "validate_tool_repo_identity",
]
