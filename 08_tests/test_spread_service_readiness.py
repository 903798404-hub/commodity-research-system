from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
CONTRACT_DIR = REPOSITORY / "09_deploy" / "spread_release"
sys.path.insert(0, str(CONTRACT_DIR))

from wait_for_service_ready import (  # noqa: E402
    DEFAULT_READINESS_POLICY,
    ReadinessFailure,
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
    ) -> None:
        self.requests = list(requests)
        self.inspect_records = list(inspect_records or [])
        self.clock = clock
        self.request_durations = list(request_durations or [])
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
        return "last safe application log"


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
    assert result["container_logs_tail_200"] == "last safe application log"
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
