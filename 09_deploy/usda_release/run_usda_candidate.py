#!/usr/bin/env python3
"""Run a USDA-only candidate against the current immutable USDA image."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import urlopen

from capture_usda_runtime import sanitize_container, utc_now, write_json_exclusive


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
CANDIDATE_COMPOSE = SCRIPT_DIRECTORY / "compose.candidate.yml"
FORBIDDEN_NAMES = frozenset(
    {
        "SPREAD_IMAGE",
        "MARKET_DATA_GIT_HEAD",
        "WEATHER_DATA_DIR",
        "WEATHER_RUNTIME_CURRENT_DIR",
        "USDA_DASHBOARD_URL",
        "OIL_WORLD_DASHBOARD_URL",
    }
)
REQUIRED_NAMES = frozenset(
    {
        "USDA_IMAGE",
        "USDA_CANDIDATE_CONTAINER_NAME",
        "USDA_CANDIDATE_HOST_PORT",
        "USDA_CANDIDATE_PROJECT_NAME",
        "USDA_RUNTIME_DATA_ROOT",
    }
)
CONTAINER_RE = re.compile(r"^usda-candidate-[a-z0-9][a-z0-9-]{2,62}$")
PROJECT_RE = re.compile(r"^market-data-usda-candidate-[a-z0-9][a-z0-9-]{2,62}$")
IMAGE_RE = re.compile(r"^[^\s]+:[^\s]+$")


def read_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid environment line in {path}")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"invalid environment name: {key}")
        if key in values:
            raise ValueError(f"duplicate environment name: {key}")
        values[key] = value
    return values


def validate_candidate_environment(values: Mapping[str, str]) -> dict[str, str]:
    missing = REQUIRED_NAMES - set(values)
    if missing:
        raise ValueError(f"candidate environment missing: {sorted(missing)}")
    forbidden = FORBIDDEN_NAMES & set(values)
    if forbidden:
        raise ValueError(f"candidate environment contains non-USDA names: {sorted(forbidden)}")
    unknown = set(values) - REQUIRED_NAMES - {"USDA_NETWORK_NAME"}
    if unknown:
        raise ValueError(f"candidate environment contains unknown names: {sorted(unknown)}")
    image = values["USDA_IMAGE"].strip()
    if not IMAGE_RE.fullmatch(image) or image.endswith(":latest"):
        raise ValueError("USDA_IMAGE must be an immutable non-latest image reference")
    container = values["USDA_CANDIDATE_CONTAINER_NAME"].strip()
    if not CONTAINER_RE.fullmatch(container):
        raise ValueError("candidate container name is invalid")
    project = values["USDA_CANDIDATE_PROJECT_NAME"].strip()
    expected_project = f"market-data-{container}"
    if not PROJECT_RE.fullmatch(project) or project != expected_project:
        raise ValueError("candidate project name must be derived from the candidate container")
    try:
        port = int(values["USDA_CANDIDATE_HOST_PORT"])
    except ValueError as exc:
        raise ValueError("candidate port must be numeric") from exc
    if not 18080 <= port <= 18499 or port in {8080, 8501, 8081}:
        raise ValueError("candidate port must be in 18080-18499 and outside formal ports")
    normalized = dict(values)
    normalized["USDA_IMAGE"] = image
    normalized["USDA_CANDIDATE_CONTAINER_NAME"] = container
    normalized["USDA_CANDIDATE_PROJECT_NAME"] = project
    normalized["USDA_CANDIDATE_HOST_PORT"] = str(port)
    runtime_root = PurePosixPath(values["USDA_RUNTIME_DATA_ROOT"])
    if not runtime_root.is_absolute():
        raise ValueError("USDA_RUNTIME_DATA_ROOT must be absolute")
    normalized["USDA_RUNTIME_DATA_ROOT"] = str(runtime_root)
    return normalized


def compose_command(values: Mapping[str, str], action: str) -> list[str]:
    command = [
        "docker",
        "compose",
        "--project-name",
        values["USDA_CANDIDATE_PROJECT_NAME"],
        "--env-file",
        "<candidate-env-file>",
        "-f",
        str(CANDIDATE_COMPOSE),
    ]
    if action == "up":
        return command + [
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
            "--no-deps",
            "--force-recreate",
            "usda-dashboard",
        ]
    if action == "remove":
        return ["docker", "rm", "-f", values["USDA_CANDIDATE_CONTAINER_NAME"]]
    raise ValueError(f"unsupported candidate action: {action}")


def _run(command: list[str], runner: Callable[..., Any]) -> Any:
    completed = runner(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise RuntimeError(f"{command[0]} command failed: {detail}")
    return completed


def inspect_container(name: str, runner: Callable[..., Any]) -> dict[str, Any]:
    completed = _run(["docker", "inspect", name], runner)
    payload = json.loads(completed.stdout)
    if not isinstance(payload, list) or len(payload) != 1:
        raise RuntimeError("docker inspect did not return exactly one container")
    return sanitize_container(payload[0])


def wait_for_candidate(
    *,
    port: int,
    timeout_seconds: int,
    request: Callable[..., Any] = urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    if timeout_seconds < 10:
        raise ValueError("candidate timeout must be at least 10 seconds")
    root_url = f"http://127.0.0.1:{port}/usda/"
    endpoints = (
        root_url,
        f"http://127.0.0.1:{port}/usda/presentation",
        f"http://127.0.0.1:{port}/usda/app-contract.json",
        f"http://127.0.0.1:{port}/usda/data/current.json",
    )
    deadline = time.monotonic() + timeout_seconds
    checks: list[dict[str, Any]] = []
    while True:
        all_ok = True
        checks = []
        root_body = ""
        for endpoint in endpoints:
            try:
                with request(endpoint, timeout=3) as response:
                    status = int(response.status)
                    checks.append({"url": endpoint, "http_status": status})
                    all_ok = all_ok and status == 200
                    if endpoint == root_url and status == 200:
                        root_body = response.read().decode("utf-8", errors="replace")
            except (URLError, OSError) as exc:
                checks.append({"url": endpoint, "error": type(exc).__name__})
                all_ok = False
        if all_ok:
            asset_paths = re.findall(
                r'(?:src|href)=["\']([^"\']+\.(?:js|css))(?:\?[^"\']*)?["\']',
                root_body,
                flags=re.IGNORECASE,
            )
            if not asset_paths:
                checks.append({"url": root_url, "error": "static_asset_reference_missing"})
                all_ok = False
            else:
                for asset_path in sorted(set(asset_paths)):
                    asset_url = urljoin(root_url, asset_path)
                    try:
                        with request(asset_url, timeout=3) as response:
                            status = int(response.status)
                            checks.append({"url": asset_url, "http_status": status, "kind": "static_asset"})
                            all_ok = all_ok and status == 200
                    except (URLError, OSError) as exc:
                        checks.append({"url": asset_url, "kind": "static_asset", "error": type(exc).__name__})
                        all_ok = False
            if all_ok:
                return checks
        if time.monotonic() >= deadline:
            raise RuntimeError(f"candidate readiness timed out: {checks}")
        sleep(2)


def _candidate_result(
    *,
    values: Mapping[str, str],
    inspected: Mapping[str, Any],
    expected_image_id: str,
    http_checks: list[dict[str, Any]],
    log_summary: Mapping[str, Any],
) -> dict[str, Any]:
    if not inspected["running"] or inspected["status"] != "running":
        raise RuntimeError("candidate container is not running")
    if inspected["restart_count"] != 0:
        raise RuntimeError("candidate RestartCount must be zero")
    if inspected["image_id"] != expected_image_id:
        raise RuntimeError("candidate Image ID differs from the formal USDA image")
    if inspected["restart_policy"] not in {"no", ""}:
        raise RuntimeError("candidate restart policy is not disabled")
    if inspected["compose"]["service"] != "usda-dashboard":
        raise RuntimeError("candidate Compose service is invalid")
    if inspected["compose"]["project"] != values["USDA_CANDIDATE_PROJECT_NAME"]:
        raise RuntimeError("candidate Compose project is invalid")
    if inspected["compose"]["config_files"] != str(CANDIDATE_COMPOSE):
        raise RuntimeError("candidate Compose path is invalid")
    runtime_mounts = [item for item in inspected["mounts"] if item.get("destination") == "/runtime/usda"]
    if len(runtime_mounts) != 1 or runtime_mounts[0].get("source") != values["USDA_RUNTIME_DATA_ROOT"] or runtime_mounts[0].get("read_write") is not False:
        raise RuntimeError("candidate Runtime data parent mount is missing or not read-only")
    ports = inspected["ports"].get("80/tcp") or []
    if not ports or any(item.get("HostIp") not in {"127.0.0.1", "::1"} for item in ports):
        raise RuntimeError("candidate port is not localhost-only")
    return {
        "schema_version": "1.0.0",
        "status": "passed",
        "created_at_utc": utc_now(),
        "candidate_container": inspected,
        "expected_image_id": expected_image_id,
        "candidate_image": values["USDA_IMAGE"],
        "candidate_port": int(values["USDA_CANDIDATE_HOST_PORT"]),
        "http_checks": http_checks,
        "log_summary": dict(log_summary),
        "forbidden_environment_names": sorted(FORBIDDEN_NAMES),
    }


def collect_log_summary(container: str, runner: Callable[..., Any]) -> dict[str, Any]:
    completed = _run(["docker", "logs", "--tail", "200", container], runner)
    raw = (completed.stdout or "") + (completed.stderr or "")
    markers = ("Traceback", "FileNotFoundError", "KeyError", "ConfigurationError")
    present = [marker for marker in markers if marker in raw]
    if present:
        raise RuntimeError(f"candidate logs contain failure markers: {present}")
    encoded = raw.encode("utf-8", errors="replace")
    return {
        "tail_lines_requested": 200,
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "failure_markers": present,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--expected-image-id", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        values = validate_candidate_environment(read_environment(args.env_file))
        plan = {
            "candidate_compose": str(CANDIDATE_COMPOSE),
            "candidate_container": values["USDA_CANDIDATE_CONTAINER_NAME"],
            "candidate_port": int(values["USDA_CANDIDATE_HOST_PORT"]),
            "up_command": compose_command(values, "up"),
            "remove_command": compose_command(values, "remove"),
        }
        if not args.execute:
            print(json.dumps({"status": "dry_run", **plan}, ensure_ascii=False, sort_keys=True))
            return 0

        up = compose_command(values, "up")
        up[up.index("<candidate-env-file>")] = str(args.env_file)
        try:
            _run(up, subprocess.run)
            inspected = inspect_container(values["USDA_CANDIDATE_CONTAINER_NAME"], subprocess.run)
            checks = wait_for_candidate(
                port=int(values["USDA_CANDIDATE_HOST_PORT"]),
                timeout_seconds=args.timeout_seconds,
            )
            log_summary = collect_log_summary(values["USDA_CANDIDATE_CONTAINER_NAME"], subprocess.run)
            result = _candidate_result(
                values=values,
                inspected=inspected,
                expected_image_id=args.expected_image_id,
                http_checks=checks,
                log_summary=log_summary,
            )
            write_json_exclusive(args.evidence_dir / "candidate_result.json", result)
        finally:
            cleanup_attempt = subprocess.run(
                compose_command(values, "remove"),
                check=False,
                capture_output=True,
                text=True,
            )
            if cleanup_attempt.returncode != 0:
                remaining = subprocess.run(
                    ["docker", "inspect", values["USDA_CANDIDATE_CONTAINER_NAME"]],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if remaining.returncode == 0:
                    raise RuntimeError("candidate cleanup failed while the candidate still exists")

        missing = subprocess.run(
            ["docker", "inspect", values["USDA_CANDIDATE_CONTAINER_NAME"]],
            check=False,
            capture_output=True,
            text=True,
        )
        if missing.returncode == 0:
            raise RuntimeError("candidate container still exists after explicit cleanup")
        cleanup = {
            "schema_version": "1.0.0",
            "status": "removed",
            "removed_at_utc": utc_now(),
            "candidate_container_name": values["USDA_CANDIDATE_CONTAINER_NAME"],
            "candidate_container_removed": True,
        }
        write_json_exclusive(args.evidence_dir / "candidate_cleanup.json", cleanup)
        print(json.dumps({"status": "passed", **plan}, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"USDA candidate failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
