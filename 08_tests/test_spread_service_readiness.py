from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
CONTRACT_DIR = REPOSITORY / "09_deploy" / "spread_release"
sys.path.insert(0, str(CONTRACT_DIR))

from wait_for_service_ready import (  # noqa: E402
    DEFAULT_READINESS_POLICY,
    LOG_MAX_BYTES,
    ReadinessFailure,
    build_log_summary,
    validate_readiness_policy,
    wait_for_service_ready,
)


IMAGE_ID = "sha256:" + "1" * 64
HEALTH_URL = "http://127.0.0.1:18501/_stcore/health"


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeRuntime:
    def __init__(
        self,
        requests: list[dict[str, object]],
        *,
        inspect_records: list[dict[str, object]] | None = None,
        clock: FakeClock | None = None,
        request_durations: list[float] | None = None,
        log_text: str = "last safe application log",
        log_error: bool = False,
    ) -> None:
        self.requests = list(requests)
        self.inspect_records = list(inspect_records or [])
        self.clock = clock
        self.request_durations = list(request_durations or [])
        self.log_text = log_text
        self.log_error = log_error
        self.request_calls = 0
        self.inspect_calls = 0
        self.log_calls = 0

    def inspect(self, container: str) -> dict[str, object]:
        del container
        self.inspect_calls += 1
        if self.inspect_records:
            return self.inspect_records.pop(0)
        return running_inspect()

    def request(self, url: str, timeout_seconds: int) -> dict[str, object]:
        assert url == HEALTH_URL
        assert timeout_seconds == DEFAULT_READINESS_POLICY["request_timeout_seconds"]
        self.request_calls += 1
        if self.request_durations and self.clock:
            self.clock.advance(self.request_durations.pop(0))
        if not self.requests:
            raise AssertionError("unexpected readiness request")
        return self.requests.pop(0)

    def logs(self, container: str, tail: int) -> str:
        assert container == "spread-candidate"
        assert tail == 200
        self.log_calls += 1
        if self.log_error:
            raise RuntimeError("simulated docker log failure with SECRET_TOKEN=hidden")
        return self.log_text


def running_inspect(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "exists": True,
        "container_id": "a" * 64,
        "container_name": "spread-candidate",
        "image_id": IMAGE_ID,
        "config_image": "market-data-spread-dashboard:sealed",
        "status": "running",
        "running": True,
        "dead": False,
        "restarting": False,
        "removal_in_progress": False,
        "restart_count": 0,
    }
    result.update(overrides)
    return result


def response(
    *,
    exit_code: int = 0,
    status: int | None = 200,
    body: str = "ok",
) -> dict[str, object]:
    return {
        "curl_exit_code": exit_code,
        "http_status": status,
        "body": body,
        "stderr": "",
    }


def run(runtime: FakeRuntime, clock: FakeClock | None = None) -> dict[str, object]:
    clock = clock or FakeClock()
    return wait_for_service_ready(
        runtime=runtime,
        container="spread-candidate",
        health_url=HEALTH_URL,
        expected_image_id=IMAGE_ID,
        initial_restart_count=0,
        policy=DEFAULT_READINESS_POLICY,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        now=lambda: "2026-07-17T00:00:00Z",
    )


def test_transient_startup_failures_then_two_consecutive_successes() -> None:
    runtime = FakeRuntime(
        [
            response(exit_code=7, status=None),
            response(exit_code=56, status=None),
            response(exit_code=52, status=None),
            response(status=502, body=""),
            response(status=503, body="starting"),
            response(status=200, body="not ready"),
            response(),
            response(),
        ]
    )
    clock = FakeClock()

    result = run(runtime, clock)

    assert result["status"] == "ready"
    assert result["attempt_count"] == 8
    assert result["consecutive_successes_observed"] == 2
    assert result["transient_failure_counts"] == {
        "connection_refused": 1,
        "connection_reset": 1,
        "empty_reply": 1,
        "http_502": 1,
        "http_503": 1,
        "content_mismatch": 1,
    }
    assert clock.sleeps == [2] * 7


def test_real_regression_connection_reset_is_retryable() -> None:
    runtime = FakeRuntime(
        [
            response(exit_code=56, status=None),
            response(),
            response(),
        ]
    )

    result = run(runtime)

    assert result["status"] == "ready"
    assert result["transient_failure_counts"] == {"connection_reset": 1}
    assert [item["classification"] for item in result["attempts"]] == [
        "connection_reset",
        "ready_success",
        "ready_success",
    ]


@pytest.mark.parametrize(
    ("failure", "classification"),
    [
        (response(exit_code=56, status=None), "connection_reset"),
        (response(status=503, body="starting"), "http_503"),
    ],
)
def test_three_injected_transients_then_success(
    failure: dict[str, object],
    classification: str,
) -> None:
    runtime = FakeRuntime([failure, failure, failure, response(), response()])

    result = run(runtime)

    assert result["status"] == "ready"
    assert result["attempt_count"] == 5
    assert result["transient_failure_counts"] == {classification: 3}


def test_success_streak_resets_after_transient_failure() -> None:
    runtime = FakeRuntime(
        [response(), response(status=503), response(), response()]
    )

    result = run(runtime)

    assert result["attempt_count"] == 4
    assert result["first_success_elapsed_seconds"] == 0.0
    assert result["consecutive_successes_observed"] == 2


def test_request_timeout_consumes_total_timeout_and_writes_diagnostics() -> None:
    clock = FakeClock()
    runtime = FakeRuntime(
        [response(exit_code=28, status=None)],
        clock=clock,
        request_durations=[91],
    )

    with pytest.raises(ReadinessFailure) as captured:
        run(runtime, clock)

    result = captured.value.result
    assert result["classification"] == "readiness_timeout"
    assert result["transient_failure_counts"] == {"request_timeout": 1}
    assert result["elapsed_seconds"] == 91.0
    assert result["log_summary"]["tail_text"] == "last safe application log"
    assert result["log_summary"]["collection_status"] == "captured"
    assert "env" not in json.dumps(result).lower()


def test_persistent_503_reaches_bounded_total_timeout() -> None:
    runtime = FakeRuntime([response(status=503)] * 46)
    clock = FakeClock()

    with pytest.raises(ReadinessFailure) as captured:
        run(runtime, clock)

    result = captured.value.result
    assert result["classification"] == "readiness_timeout"
    assert result["elapsed_seconds"] == 90.0
    assert result["attempt_count"] == 46
    assert result["transient_failure_counts"] == {"http_503": 46}


def test_persistent_connection_failure_reaches_bounded_total_timeout() -> None:
    runtime = FakeRuntime([response(exit_code=7, status=None)] * 46)
    clock = FakeClock()

    with pytest.raises(ReadinessFailure) as captured:
        run(runtime, clock)

    result = captured.value.result
    assert result["classification"] == "readiness_timeout"
    assert result["elapsed_seconds"] == 90.0
    assert result["transient_failure_counts"] == {"connection_refused": 46}


@pytest.mark.parametrize(
    ("inspect", "classification"),
    [
        (running_inspect(status="exited", running=False), "container_terminal_state"),
        (
            running_inspect(status="dead", running=False, dead=True),
            "container_terminal_state",
        ),
        (running_inspect(status="restarting", restarting=True), "container_restarting"),
        (running_inspect(restart_count=1), "restart_count_increased"),
        (
            running_inspect(image_id="sha256:" + "2" * 64),
            "image_id_mismatch",
        ),
        ({"exists": False, "inspect_error": "not found"}, "container_missing"),
    ],
)
def test_permanent_container_failures_stop_before_http(
    inspect: dict[str, object],
    classification: str,
) -> None:
    runtime = FakeRuntime([], inspect_records=[inspect])

    with pytest.raises(ReadinessFailure) as captured:
        run(runtime)

    assert captured.value.result["classification"] == classification
    assert runtime.request_calls == 0
    assert runtime.log_calls == 1


def test_restart_increase_after_transient_failure_is_permanent() -> None:
    runtime = FakeRuntime(
        [response(exit_code=7, status=None)],
        inspect_records=[running_inspect(), running_inspect(restart_count=1)],
    )

    with pytest.raises(ReadinessFailure, match="RestartCount increased"):
        run(runtime)

    assert runtime.request_calls == 1


def test_policy_is_bounded_and_rejects_weak_or_extra_values() -> None:
    assert validate_readiness_policy(DEFAULT_READINESS_POLICY) == DEFAULT_READINESS_POLICY
    weak = copy.deepcopy(DEFAULT_READINESS_POLICY)
    weak["total_timeout_seconds"] = 59
    with pytest.raises(ValueError, match="at least 60"):
        validate_readiness_policy(weak)
    extra = copy.deepcopy(DEFAULT_READINESS_POLICY)
    extra["unsealed_override"] = True
    with pytest.raises(ValueError, match="extra"):
        validate_readiness_policy(extra)


def test_health_url_must_be_exact_and_must_not_contain_credentials() -> None:
    runtime = FakeRuntime([])
    for url in (
        "http://user:secret@127.0.0.1:18501/_stcore/health",
        "http://127.0.0.1:18501/",
        "http://127.0.0.1:18501/_stcore/health?token=secret",
    ):
        with pytest.raises(ValueError):
            wait_for_service_ready(
                runtime=runtime,
                container="spread-candidate",
                health_url=url,
                expected_image_id=IMAGE_ID,
                initial_restart_count=0,
                policy=DEFAULT_READINESS_POLICY,
            )


def test_success_and_failure_use_the_same_bounded_redacted_log_summary() -> None:
    sensitive_logs = (
        "WARNING startup delay\n"
        "SECRET_TOKEN=do-not-record\n"
        "Authorization: Bearer also-secret\n"
        "ERROR final diagnostic\n"
    )
    success_runtime = FakeRuntime(
        [response(), response()],
        log_text=sensitive_logs,
    )

    success = run(success_runtime)
    success_summary = success["log_summary"]

    assert success_summary["collection_status"] == "captured"
    assert success_summary["source_bytes"] == len(sensitive_logs.encode("utf-8"))
    assert success_summary["warning_count"] == 1
    assert success_summary["error_count"] == 1
    assert success_summary["sha256"] == hashlib.sha256(
        success_summary["tail_text"].encode("utf-8")
    ).hexdigest()
    serialized = json.dumps(success_summary)
    assert "do-not-record" not in serialized
    assert "also-secret" not in serialized
    assert serialized.count("[REDACTED]") == 2

    clock = FakeClock()
    failure_runtime = FakeRuntime(
        [response(exit_code=28, status=None)],
        clock=clock,
        request_durations=[91],
        log_text=sensitive_logs,
    )
    with pytest.raises(ReadinessFailure) as captured:
        run(failure_runtime, clock)
    failure_summary = captured.value.result["log_summary"]
    assert set(failure_summary) == set(success_summary)
    assert failure_summary["sha256"] == success_summary["sha256"]


def test_log_summary_enforces_line_and_byte_limits() -> None:
    logs = "".join(f"line-{index:03d}-{'x' * 1000}\n" for index in range(250))

    summary = build_log_summary(
        logs,
        collected_at_utc="2026-07-17T00:00:00Z",
    )

    assert summary["truncated"] is True
    assert summary["tail_line_count"] <= 200
    assert summary["stored_bytes"] <= LOG_MAX_BYTES
    assert len(summary["tail_text"].encode("utf-8")) <= LOG_MAX_BYTES
    assert summary["sha256"] == hashlib.sha256(
        summary["tail_text"].encode("utf-8")
    ).hexdigest()


def test_log_collection_failure_is_a_controlled_non_sensitive_warning() -> None:
    runtime = FakeRuntime(
        [response(), response()],
        log_error=True,
    )

    result = run(runtime)

    summary = result["log_summary"]
    assert summary["collection_status"] == "unavailable"
    assert summary["collection_warning"] == "docker logs could not be read"
    assert summary["tail_text"] == ""
    assert "SECRET_TOKEN" not in json.dumps(summary)
    assert "hidden" not in json.dumps(summary)
