#!/usr/bin/env python3
"""Bounded, observable readiness polling for spread-dashboard releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit


def interpret_observation(name, *args, **kwargs):
    """Pure shared decoder; transport and bounded readiness remain owned here."""
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "runtime_identity/runtime_observation.py"
    spec = importlib.util.spec_from_file_location("_readiness_runtime_observation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)(*args, **kwargs)


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
LOG_TAIL_LINES = 200
LOG_MAX_BYTES = 64 * 1024
_SENSITIVE_LOG_VALUE_RE = re.compile(
    r"(?i)([A-Za-z0-9_.-]*(?:password|passwd|token|api[_-]?key|secret|"
    r"credential|private[_-]?key)[A-Za-z0-9_.-]*)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_AUTHORIZATION_LOG_VALUE_RE = re.compile(
    r"(?i)\b(authorization)(\s*[:=]\s*)(?:bearer\s+)?([^\s,;]+)"
)
_WARNING_RE = re.compile(r"(?i)\bwarn(?:ing)?\b")
_ERROR_RE = re.compile(r"(?i)\b(?:error|exception|fatal)\b")


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
        payload = interpret_observation("inspect_object", completed.stdout, "readiness container inspect",
            expected_id=container if re.fullmatch(r"[0-9a-f]{64}", container) else None)
        return interpret_observation("readiness_projection", payload)

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
        if completed.returncode != 0:
            raise RuntimeError("docker logs could not be read")
        return completed.stdout + completed.stderr


def _redact_log_text(value: str) -> str:
    redacted = _SENSITIVE_LOG_VALUE_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        value,
    )
    return _AUTHORIZATION_LOG_VALUE_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        redacted,
    )


def build_log_summary(
    log_text: str,
    *,
    collected_at_utc: str,
) -> dict[str, Any]:
    if not isinstance(log_text, str):
        raise TypeError("container logs must be text")
    source_bytes = len(log_text.encode("utf-8", errors="replace"))
    lines = log_text.splitlines(keepends=True)
    line_truncated = len(lines) > LOG_TAIL_LINES
    selected = "".join(lines[-LOG_TAIL_LINES:])
    redacted = _redact_log_text(selected)
    encoded = redacted.encode("utf-8", errors="replace")
    byte_truncated = len(encoded) > LOG_MAX_BYTES
    if byte_truncated:
        encoded = encoded[-LOG_MAX_BYTES:]
        redacted = encoded.decode("utf-8", errors="ignore")
        encoded = redacted.encode("utf-8")
    summary = {
        "collection_status": "captured",
        "collected_at_utc": collected_at_utc,
        "requested_tail_lines": LOG_TAIL_LINES,
        "max_bytes": LOG_MAX_BYTES,
        "source_bytes": source_bytes,
        "stored_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "truncated": line_truncated or byte_truncated,
        "tail_line_count": len(redacted.splitlines()),
        "tail_text": redacted,
        "warning_count": len(_WARNING_RE.findall(redacted)),
        "error_count": len(_ERROR_RE.findall(redacted)),
        "collection_warning": None,
    }
    validate_log_summary(summary)
    return summary


def validate_log_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "collection_status",
        "collected_at_utc",
        "requested_tail_lines",
        "max_bytes",
        "source_bytes",
        "stored_bytes",
        "sha256",
        "truncated",
        "tail_line_count",
        "tail_text",
        "warning_count",
        "error_count",
        "collection_warning",
    }
    if set(summary) != required:
        raise ValueError("log summary fields are incomplete or unexpected")
    status = summary["collection_status"]
    if status not in {"captured", "unavailable"}:
        raise ValueError("invalid log summary collection_status")
    if summary["requested_tail_lines"] != LOG_TAIL_LINES:
        raise ValueError("invalid log summary requested_tail_lines")
    if summary["max_bytes"] != LOG_MAX_BYTES:
        raise ValueError("invalid log summary max_bytes")
    for key in (
        "source_bytes",
        "stored_bytes",
        "tail_line_count",
        "warning_count",
        "error_count",
    ):
        if isinstance(summary[key], bool) or not isinstance(summary[key], int):
            raise ValueError(f"log summary {key} must be an integer")
        if summary[key] < 0:
            raise ValueError(f"log summary {key} must be non-negative")
    tail_text = summary["tail_text"]
    if not isinstance(tail_text, str):
        raise ValueError("log summary tail_text must be text")
    if _redact_log_text(tail_text) != tail_text:
        raise ValueError("log summary contains an unredacted sensitive value")
    encoded = tail_text.encode("utf-8")
    if len(encoded) > LOG_MAX_BYTES or summary["stored_bytes"] != len(encoded):
        raise ValueError("log summary stored byte size mismatch")
    if summary["tail_line_count"] != len(tail_text.splitlines()):
        raise ValueError("log summary tail line count mismatch")
    if summary["tail_line_count"] > LOG_TAIL_LINES:
        raise ValueError("log summary exceeds the line limit")
    if summary["sha256"] != hashlib.sha256(encoded).hexdigest():
        raise ValueError("log summary SHA-256 mismatch")
    if summary["warning_count"] != len(_WARNING_RE.findall(tail_text)):
        raise ValueError("log summary warning count mismatch")
    if summary["error_count"] != len(_ERROR_RE.findall(tail_text)):
        raise ValueError("log summary error count mismatch")
    warning = summary["collection_warning"]
    if status == "captured" and warning is not None:
        raise ValueError("captured log summary must not contain a warning")
    if status == "unavailable":
        if not isinstance(warning, str) or not warning:
            raise ValueError("unavailable log summary requires a warning")
        if any(
            summary[key] != expected
            for key, expected in (
                ("source_bytes", 0),
                ("stored_bytes", 0),
                ("tail_line_count", 0),
                ("tail_text", ""),
                ("warning_count", 0),
                ("error_count", 0),
            )
        ):
            raise ValueError("unavailable log summary must not contain log content")
    collected_at = summary["collected_at_utc"]
    if not isinstance(collected_at, str) or len(collected_at) < 20:
        raise ValueError("log summary collection timestamp is invalid")
    if not isinstance(summary["truncated"], bool):
        raise ValueError("log summary truncated must be boolean")
    return dict(summary)


def collect_log_summary(
    runtime: Runtime,
    container: str,
    *,
    collected_at_utc: str,
) -> dict[str, Any]:
    try:
        log_text = runtime.logs(container, LOG_TAIL_LINES)
    except Exception:
        empty = b""
        summary = {
            "collection_status": "unavailable",
            "collected_at_utc": collected_at_utc,
            "requested_tail_lines": LOG_TAIL_LINES,
            "max_bytes": LOG_MAX_BYTES,
            "source_bytes": 0,
            "stored_bytes": 0,
            "sha256": hashlib.sha256(empty).hexdigest(),
            "truncated": False,
            "tail_line_count": 0,
            "tail_text": "",
            "warning_count": 0,
            "error_count": 0,
            "collection_warning": "docker logs could not be read",
        }
        validate_log_summary(summary)
        return summary
    return build_log_summary(log_text, collected_at_utc=collected_at_utc)


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
        finished_at = now()
        result = {
            "schema_version": "1.1.0",
            "status": "failed",
            "classification": classification,
            "message": message,
            "container": container,
            "health_url": health_url,
            "expected_image_id": expected_image_id,
            "initial_restart_count": initial_restart_count,
            "policy": sealed_policy,
            "started_at_utc": started_at,
            "finished_at_utc": finished_at,
            "elapsed_seconds": total_elapsed,
            "attempt_count": len(attempts),
            "consecutive_successes_observed": streak,
            "first_success_at_utc": first_success_at,
            "first_success_elapsed_seconds": first_success_elapsed,
            "transient_failure_counts": transient_counts,
            "last_inspect_summary": dict(last_inspect),
            "last_http_result": dict(last_request) if last_request is not None else None,
            "attempts": attempts,
            "log_summary": collect_log_summary(
                runtime,
                container,
                collected_at_utc=finished_at,
            ),
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
            ready_at = now()
            return {
                "schema_version": "1.1.0",
                "status": "ready",
                "classification": "ready",
                "container": container,
                "health_url": health_url,
                "expected_image_id": expected_image_id,
                "initial_restart_count": initial_restart_count,
                "final_restart_count": int(last_inspect.get("restart_count") or 0),
                "policy": sealed_policy,
                "started_at_utc": started_at,
                "ready_at_utc": ready_at,
                "elapsed_seconds": elapsed(),
                "attempt_count": len(attempts),
                "consecutive_successes_observed": streak,
                "first_success_at_utc": first_success_at,
                "first_success_elapsed_seconds": first_success_elapsed,
                "transient_failure_counts": transient_counts,
                "last_inspect_summary": dict(last_inspect),
                "last_http_result": dict(last_request),
                "attempts": attempts,
                "log_summary": collect_log_summary(
                    runtime,
                    container,
                    collected_at_utc=ready_at,
                ),
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
            handle.flush()
            os.fsync(handle.fileno())
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
