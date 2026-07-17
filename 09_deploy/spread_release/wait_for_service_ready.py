#!/usr/bin/env python3
"""Bounded, observable readiness polling for spread-dashboard releases."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit


DEFAULT_READINESS_POLICY: dict[str, Any] = {
    "total_timeout_seconds": 90,
    "poll_interval_seconds": 2,
    "request_timeout_seconds": 3,
    "consecutive_successes": 2,
    "health_endpoint_path": "/_stcore/health",
    "success_http_status": 200,
    "success_body_exact": "ok",
}

_POLICY_KEYS = frozenset(DEFAULT_READINESS_POLICY)
_PERMANENT_CONTAINER_STATES = frozenset({"dead", "exited", "removing"})


class Runtime(Protocol):
    def inspect(self, container: str) -> Mapping[str, Any]:
        ...

    def request(self, url: str, timeout_seconds: int) -> Mapping[str, Any]:
        ...

    def logs(self, container: str, tail: int) -> str:
        ...


class ReadinessFailure(RuntimeError):
    """Raised when the service cannot become ready under the sealed policy."""

    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = dict(result)
        super().__init__(str(self.result.get("message", "service readiness failed")))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_image_id(value: str) -> str:
    value = value.strip()
    return value if value.startswith("sha256:") else f"sha256:{value}"


def validate_readiness_policy(raw: Mapping[str, Any]) -> dict[str, Any]:
    if set(raw) != _POLICY_KEYS:
        missing = sorted(_POLICY_KEYS - set(raw))
        extra = sorted(set(raw) - _POLICY_KEYS)
        raise ValueError(f"invalid readiness policy keys; missing={missing}, extra={extra}")

    policy = dict(raw)
    integer_fields = (
        "total_timeout_seconds",
        "poll_interval_seconds",
        "request_timeout_seconds",
        "consecutive_successes",
        "success_http_status",
    )
    for key in integer_fields:
        value = policy[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")

    if policy["total_timeout_seconds"] < 60:
        raise ValueError("total_timeout_seconds must be at least 60")
    if policy["poll_interval_seconds"] < 1:
        raise ValueError("poll_interval_seconds must be at least 1")
    if policy["request_timeout_seconds"] < 1:
        raise ValueError("request_timeout_seconds must be at least 1")
    if policy["request_timeout_seconds"] > policy["total_timeout_seconds"]:
        raise ValueError("request_timeout_seconds cannot exceed total_timeout_seconds")
    if policy["consecutive_successes"] < 2:
        raise ValueError("consecutive_successes must be at least 2")
    if not 100 <= policy["success_http_status"] <= 599:
        raise ValueError("success_http_status must be a valid HTTP status")

    endpoint = policy["health_endpoint_path"]
    if not isinstance(endpoint, str) or not endpoint.startswith("/") or "?" in endpoint or "#" in endpoint:
        raise ValueError("health_endpoint_path must be an absolute path without query or fragment")
    if not isinstance(policy["success_body_exact"], str) or not policy["success_body_exact"]:
        raise ValueError("success_body_exact must be a non-empty string")
    return policy


def load_readiness_policy(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    policy = payload.get("readiness_policy")
    if not isinstance(policy, dict):
        raise ValueError(f"{path} does not contain readiness_policy")
    return validate_readiness_policy(policy)


def validate_health_url(url: str, endpoint_path: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("health URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("health URL must not contain credentials")
    if parsed.query or parsed.fragment or parsed.path != endpoint_path:
        raise ValueError(f"health URL path must be exactly {endpoint_path}")


class DockerCurlRuntime:
    def inspect(self, container: str) -> Mapping[str, Any]:
        completed = subprocess.run(
            ["docker", "inspect", container],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            return {
                "exists": False,
                "inspect_error": completed.stderr.strip() or completed.stdout.strip(),
            }
        payload = json.loads(completed.stdout)[0]
        state = payload.get("State") or {}
        config = payload.get("Config") or {}
        return {
            "exists": True,
            "container_id": payload.get("Id"),
            "container_name": str(payload.get("Name") or "").lstrip("/"),
            "image_id": payload.get("Image"),
            "config_image": config.get("Image"),
            "status": state.get("Status"),
            "running": bool(state.get("Running")),
            "dead": bool(state.get("Dead")),
            "restarting": bool(state.get("Restarting")),
            "removal_in_progress": bool(state.get("RemovalInProgress")),
            "restart_count": int(payload.get("RestartCount") or 0),
            "created_at": payload.get("Created"),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "exit_code": state.get("ExitCode"),
            "error": state.get("Error"),
        }

    def request(self, url: str, timeout_seconds: int) -> Mapping[str, Any]:
        fd, body_name = tempfile.mkstemp(prefix="spread-readiness-", suffix=".body")
        os.close(fd)
        try:
            completed = subprocess.run(
                [
                    "curl",
                    "--silent",
                    "--show-error",
                    "--output",
                    body_name,
                    "--write-out",
                    "%{http_code}",
                    "--max-time",
                    str(timeout_seconds),
                    url,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            body = Path(body_name).read_text(encoding="utf-8", errors="replace")
            status_text = completed.stdout.strip()
            return {
                "curl_exit_code": completed.returncode,
                "http_status": int(status_text) if status_text.isdigit() else None,
                "body": body,
                "stderr": completed.stderr.strip(),
            }
        finally:
            Path(body_name).unlink(missing_ok=True)

    def logs(self, container: str, tail: int) -> str:
        completed = subprocess.run(
            ["docker", "logs", "--tail", str(tail), container],
            check=False,
            capture_output=True,
            text=True,
        )
        return (completed.stdout + completed.stderr)[-200_000:]


def _container_failure(
    inspect: Mapping[str, Any],
    expected_image_id: str,
    initial_restart_count: int,
) -> tuple[str, str] | None:
    if not inspect.get("exists"):
        return "container_missing", "container does not exist"

    actual_image_id = normalize_image_id(str(inspect.get("image_id") or ""))
    if actual_image_id != expected_image_id:
        return "image_id_mismatch", f"expected {expected_image_id}, got {actual_image_id}"

    restart_count = int(inspect.get("restart_count") or 0)
    if restart_count > initial_restart_count:
        return (
            "restart_count_increased",
            f"RestartCount increased from {initial_restart_count} to {restart_count}",
        )

    status = str(inspect.get("status") or "").lower()
    if inspect.get("dead") or status in _PERMANENT_CONTAINER_STATES:
        return "container_terminal_state", f"container state is {status or 'dead'}"
    if inspect.get("removal_in_progress"):
        return "container_removing", "container removal is in progress"
    if inspect.get("restarting") or status == "restarting":
        return "container_restarting", "container is restarting"
    if not inspect.get("running"):
        return "container_not_running", f"container state is {status or 'unknown'}"
    return None


def _request_classification(
    request: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> tuple[str, bool]:
    exit_code = int(request.get("curl_exit_code") or 0)
    if exit_code:
        return {
            7: "connection_refused",
            28: "request_timeout",
            52: "empty_reply",
            56: "connection_reset",
        }.get(exit_code, "curl_transport_error"), False

    status = request.get("http_status")
    if status != policy["success_http_status"]:
        if status == 502:
            return "http_502", False
        if status == 503:
            return "http_503", False
        return "http_status_not_ready", False

    body = str(request.get("body") or "").strip()
    if body != policy["success_body_exact"]:
        return "content_mismatch", False
    return "ready_success", True


def wait_for_service_ready(
    *,
    runtime: Runtime,
    container: str,
    health_url: str,
    expected_image_id: str,
    initial_restart_count: int,
    policy: Mapping[str, Any],
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], str] = utc_now,
    observer: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    sealed_policy = validate_readiness_policy(policy)
    validate_health_url(health_url, sealed_policy["health_endpoint_path"])
    expected_image_id = normalize_image_id(expected_image_id)
    started_monotonic = monotonic()
    started_at = now()
    attempts: list[dict[str, Any]] = []
    transient_counts: dict[str, int] = {}
    streak = 0
    first_success_elapsed: float | None = None
    first_success_at: str | None = None
    last_inspect: Mapping[str, Any] = {}
    last_request: Mapping[str, Any] | None = None

    def elapsed() -> float:
        return round(monotonic() - started_monotonic, 3)

    def finish_failure(classification: str, message: str) -> None:
        total_elapsed = elapsed()
        result = {
            "schema_version": "1.0.0",
            "status": "failed",
            "classification": classification,
            "message": message,
            "container": container,
            "health_url": health_url,
            "expected_image_id": expected_image_id,
            "initial_restart_count": initial_restart_count,
            "policy": sealed_policy,
            "started_at_utc": started_at,
            "finished_at_utc": now(),
            "elapsed_seconds": total_elapsed,
            "attempt_count": len(attempts),
            "consecutive_successes_observed": streak,
            "first_success_at_utc": first_success_at,
            "first_success_elapsed_seconds": first_success_elapsed,
            "transient_failure_counts": transient_counts,
            "last_inspect_summary": dict(last_inspect),
            "last_http_result": dict(last_request) if last_request is not None else None,
            "attempts": attempts,
            "container_logs_tail_200": runtime.logs(container, 200),
        }
        raise ReadinessFailure(result)

    while True:
        attempt_number = len(attempts) + 1
        last_inspect = dict(runtime.inspect(container))
        permanent = _container_failure(last_inspect, expected_image_id, initial_restart_count)
        if permanent:
            classification, message = permanent
            event = {
                "timestamp_utc": now(),
                "elapsed_seconds": elapsed(),
                "attempt": attempt_number,
                "container_status": last_inspect.get("status"),
                "running": last_inspect.get("running"),
                "restart_count": last_inspect.get("restart_count"),
                "image_id": last_inspect.get("image_id"),
                "classification": classification,
                "consecutive_successes": streak,
            }
            attempts.append(event)
            if observer:
                observer(event)
            finish_failure(classification, message)

        last_request = dict(runtime.request(health_url, sealed_policy["request_timeout_seconds"]))
        classification, success = _request_classification(last_request, sealed_policy)
        if success:
            streak += 1
            if first_success_elapsed is None:
                first_success_elapsed = elapsed()
                first_success_at = now()
        else:
            streak = 0
            transient_counts[classification] = transient_counts.get(classification, 0) + 1

        event = {
            "timestamp_utc": now(),
            "elapsed_seconds": elapsed(),
            "attempt": attempt_number,
            "container_status": last_inspect.get("status"),
            "running": last_inspect.get("running"),
            "restart_count": last_inspect.get("restart_count"),
            "image_id": last_inspect.get("image_id"),
            "curl_exit_code": last_request.get("curl_exit_code"),
            "http_status": last_request.get("http_status"),
            "classification": classification,
            "consecutive_successes": streak,
        }
        attempts.append(event)
        if observer:
            observer(event)

        if streak >= sealed_policy["consecutive_successes"]:
            return {
                "schema_version": "1.0.0",
                "status": "ready",
                "classification": "ready",
                "container": container,
                "health_url": health_url,
                "expected_image_id": expected_image_id,
                "initial_restart_count": initial_restart_count,
                "final_restart_count": int(last_inspect.get("restart_count") or 0),
                "policy": sealed_policy,
                "started_at_utc": started_at,
                "ready_at_utc": now(),
                "elapsed_seconds": elapsed(),
                "attempt_count": len(attempts),
                "consecutive_successes_observed": streak,
                "first_success_at_utc": first_success_at,
                "first_success_elapsed_seconds": first_success_elapsed,
                "transient_failure_counts": transient_counts,
                "last_inspect_summary": dict(last_inspect),
                "last_http_result": dict(last_request),
                "attempts": attempts,
            }

        if elapsed() >= sealed_policy["total_timeout_seconds"]:
            finish_failure("readiness_timeout", "service did not become ready before the total timeout")
        sleep(sealed_policy["poll_interval_seconds"])


def write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except FileExistsError as exc:
        raise RuntimeError(f"refusing to overwrite existing readiness artifact: {path}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--expected-image-id", required=True)
    parser.add_argument("--initial-restart-count", required=True, type=int)
    parser.add_argument("--policy-file", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--failure-artifact", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        policy = load_readiness_policy(args.policy_file)
        result = wait_for_service_ready(
            runtime=DockerCurlRuntime(),
            container=args.container,
            health_url=args.health_url,
            expected_image_id=args.expected_image_id,
            initial_restart_count=args.initial_restart_count,
            policy=policy,
            observer=lambda event: print(
                json.dumps(event, sort_keys=True, ensure_ascii=False),
                flush=True,
            ),
        )
        write_json_exclusive(args.result, result)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except ReadinessFailure as exc:
        write_json_exclusive(args.result, exc.result)
        write_json_exclusive(args.failure_artifact, exc.result)
        print(json.dumps(exc.result, sort_keys=True, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"readiness configuration/runtime error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
