from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNNER = (
    PROJECT_ROOT
    / "09_deploy"
    / "soybean_crop_progress"
    / "run_soybean_crop_weekly_update.sh"
)
FAKE_SECRET = "runner-fixture-secret"
CONTROL_REPO_GIT_HEAD = "426aa28ec815bb3d2c95855430d2bd3dde2031c0"
RUNTIME_GIT_HEAD = "b704a2933fecc667695d5a31065abeea1fda7492"


def _bash_executable() -> Path:
    discovered = shutil.which("bash")
    candidates = [
        Path(discovered) if discovered else None,
        Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        / "Git"
        / "bin"
        / "bash.exe",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    raise AssertionError("Bash is required to validate the production runner")


def _bash_path(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return resolved.as_posix()
    posix = resolved.as_posix()
    drive, remainder = posix.split(":", maxsplit=1)
    return f"/{drive.lower()}{remainder}"


def _write_mock(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _runner_environment(
    tmp_path: Path,
    *,
    flock_exit_code: int = 0,
    docker_exit_code: int = 0,
    container_running: str = "true",
    runtime_git_head: str = RUNTIME_GIT_HEAD,
    control_repo_git_head: str = RUNTIME_GIT_HEAD,
    inspect_exit_code: int = 0,
) -> tuple[dict[str, str], Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    secret_file = tmp_path / "nass.env"
    secret_file.write_text(
        f"NASS_API_KEY={FAKE_SECRET}\n",
        encoding="utf-8",
        newline="\n",
    )
    mock_bin = tmp_path / "mock-bin"
    mock_bin.mkdir()
    records = tmp_path / "records"
    records.mkdir()

    _write_mock(
        mock_bin / "flock",
        """#!/usr/bin/env bash
printf '%s\n' "$*" >"${MOCK_FLOCK_ARGS}"
exit "${MOCK_FLOCK_EXIT_CODE}"
""",
    )
    _write_mock(
        mock_bin / "git",
        """#!/usr/bin/env bash
printf '%s\n' "$*" >"${MOCK_GIT_ARGS}"
printf '%s\n' "${MOCK_CONTROL_REPO_GIT_HEAD}"
""",
    )
    _write_mock(
        mock_bin / "docker",
        """#!/usr/bin/env bash
printf '%s\n' "$1" >>"${MOCK_DOCKER_CALLS}"
if [[ "$1" == "inspect" ]]; then
    if [[ "${MOCK_INSPECT_EXIT_CODE}" != "0" ]]; then
    exit "${MOCK_INSPECT_EXIT_CODE}"
    fi
    printf '%s\n' "${MOCK_CONTAINER_RUNNING}"
    exit 0
fi
if [[ "$1" == "exec" ]]; then
    if [[ "${2-}" == "spread-dashboard" && "${3-}" == "printenv" && "${4-}" == "MARKET_DATA_GIT_HEAD" ]]; then
        printf '%s\n' "${MOCK_RUNTIME_GIT_HEAD}"
        exit 0
    fi
    printf '%s\n' "$@" >"${MOCK_DOCKER_ARGS}"
    printf '%s' "${NASS_API_KEY-}" >"${MOCK_CONTAINER_SECRET}"
    printf '%s' "${MARKET_DATA_GIT_HEAD-}" >"${MOCK_CONTAINER_GIT_HEAD}"
    exit "${MOCK_DOCKER_EXIT_CODE}"
fi
exit 2
""",
    )

    environment = os.environ.copy()
    environment.update(
        {
            "RUNNER_PATH": _bash_path(RUNNER),
            "MOCK_BIN": _bash_path(mock_bin),
            "MARKET_DATA_REPOSITORY_PATH": _bash_path(repository),
            "MARKET_DATA_NASS_ENV_FILE": _bash_path(secret_file),
            "MARKET_DATA_SOYBEAN_LOCK_FILE": _bash_path(
                tmp_path / "weekly.lock"
            ),
            "MOCK_FLOCK_EXIT_CODE": str(flock_exit_code),
            "MOCK_DOCKER_EXIT_CODE": str(docker_exit_code),
            "MOCK_INSPECT_EXIT_CODE": str(inspect_exit_code),
            "MOCK_CONTAINER_RUNNING": container_running,
            "MOCK_CONTROL_REPO_GIT_HEAD": control_repo_git_head,
            "MOCK_RUNTIME_GIT_HEAD": runtime_git_head,
            "MOCK_FLOCK_ARGS": _bash_path(records / "flock-args.txt"),
            "MOCK_GIT_ARGS": _bash_path(records / "git-args.txt"),
            "MOCK_DOCKER_CALLS": _bash_path(records / "docker-calls.txt"),
            "MOCK_DOCKER_ARGS": _bash_path(records / "docker-args.txt"),
            "MOCK_CONTAINER_SECRET": _bash_path(
                records / "container-secret.txt"
            ),
            "MOCK_CONTAINER_GIT_HEAD": _bash_path(
                records / "container-git-head.txt"
            ),
        }
    )
    return environment, records


def _run_wrapper(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _bash_executable(),
            "-c",
            'PATH="${MOCK_BIN}:/usr/bin:/bin"; '
            'export PATH; exec bash "${RUNNER_PATH}"',
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )


def test_runner_static_syntax_and_environment_only_secret_injection() -> None:
    bash = _bash_executable()
    result = subprocess.run(
        [bash, "-n", _bash_path(RUNNER)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    content = RUNNER.read_text(encoding="utf-8")

    assert result.returncode == 0, result.stderr
    assert "/home/ubuntu/market-data" in content
    assert "/home/ubuntu/.config/market-data/nass.env" in content
    assert "/run/lock/soybean_crop_progress_update.lock" in content
    assert 'docker exec "${CONTAINER_NAME}" printenv MARKET_DATA_GIT_HEAD' in content
    assert 'git -C "${REPOSITORY_PATH}" rev-parse HEAD' in content
    assert "--env NASS_API_KEY" in content
    assert "--env MARKET_DATA_GIT_HEAD" in content
    assert "--env NASS_API_KEY=" not in content
    assert FAKE_SECRET not in content


def test_runner_uses_matching_runtime_identity_without_putting_values_in_arguments(
    tmp_path: Path,
) -> None:
    environment, records = _runner_environment(tmp_path)

    result = _run_wrapper(environment)

    assert result.returncode == 0, result.stderr
    git_args = (records / "git-args.txt").read_text(encoding="utf-8")
    docker_args = (records / "docker-args.txt").read_text(encoding="utf-8")
    assert "rev-parse HEAD" in git_args
    assert "--env\nNASS_API_KEY\n" in docker_args
    assert "--env\nMARKET_DATA_GIT_HEAD\n" in docker_args
    assert "spread-dashboard" in docker_args
    assert (
        "/app/04_scripts/soybean_crop_progress/"
        "update_soybeans_crop_weekly.py"
    ) in docker_args
    assert FAKE_SECRET not in docker_args
    assert CONTROL_REPO_GIT_HEAD not in docker_args
    assert FAKE_SECRET not in result.stdout
    assert FAKE_SECRET not in result.stderr
    assert (records / "container-secret.txt").read_text(
        encoding="utf-8"
    ) == FAKE_SECRET
    assert (records / "container-git-head.txt").read_text(
        encoding="utf-8"
    ) == RUNTIME_GIT_HEAD
    assert f"runtime_git_head={RUNTIME_GIT_HEAD}" in result.stdout
    assert f"control_repo_git_head={RUNTIME_GIT_HEAD}" in result.stdout
    assert "identity_match=true" in result.stdout


def test_runner_uses_runtime_identity_when_control_repository_differs(
    tmp_path: Path,
) -> None:
    environment, records = _runner_environment(
        tmp_path,
        control_repo_git_head=CONTROL_REPO_GIT_HEAD,
        runtime_git_head=RUNTIME_GIT_HEAD,
    )

    result = _run_wrapper(environment)

    assert result.returncode == 0, result.stderr
    assert f"runtime_git_head={RUNTIME_GIT_HEAD}" in result.stdout
    assert f"control_repo_git_head={CONTROL_REPO_GIT_HEAD}" in result.stdout
    assert "identity_match=false" in result.stdout
    assert (records / "container-git-head.txt").read_text(
        encoding="utf-8"
    ) == RUNTIME_GIT_HEAD
    assert (records / "docker-args.txt").read_text(encoding="utf-8").splitlines().count(
        "MARKET_DATA_GIT_HEAD"
    ) == 1


@pytest.mark.parametrize(
    "runtime_git_head",
    ["", "short-sha", "g" * 40, f"{RUNTIME_GIT_HEAD} extra", f"{RUNTIME_GIT_HEAD}\nextra"],
)
def test_runner_rejects_missing_or_invalid_runtime_identity(
    tmp_path: Path, runtime_git_head: str
) -> None:
    environment, records = _runner_environment(
        tmp_path,
        runtime_git_head=runtime_git_head,
        control_repo_git_head=CONTROL_REPO_GIT_HEAD,
    )

    result = _run_wrapper(environment)

    assert result.returncode == 1
    assert "运行容器Git身份" in result.stderr
    assert not (records / "docker-args.txt").exists()


def test_runner_rejects_missing_container(tmp_path: Path) -> None:
    environment, records = _runner_environment(tmp_path, inspect_exit_code=1)

    result = _run_wrapper(environment)

    assert result.returncode == 1
    assert "容器不存在或无法检查" in result.stderr
    assert (records / "docker-calls.txt").read_text(encoding="utf-8").splitlines() == [
        "inspect"
    ]


def test_runner_rejects_stopped_container(tmp_path: Path) -> None:
    environment, records = _runner_environment(tmp_path, container_running="false")

    result = _run_wrapper(environment)

    assert result.returncode == 1
    assert "容器未运行" in result.stderr
    assert (records / "docker-calls.txt").read_text(encoding="utf-8").splitlines() == [
        "inspect"
    ]


def test_runner_lock_contention_skips_docker(tmp_path: Path) -> None:
    environment, records = _runner_environment(tmp_path, flock_exit_code=1)

    result = _run_wrapper(environment)

    assert result.returncode == 0
    assert "已有实例运行" in result.stdout
    assert not (records / "docker-calls.txt").exists()


def test_runner_preserves_docker_exec_failure_exit_code(tmp_path: Path) -> None:
    environment, records = _runner_environment(
        tmp_path,
        docker_exit_code=37,
    )

    result = _run_wrapper(environment)

    assert result.returncode == 37
    assert (records / "docker-calls.txt").read_text(
        encoding="utf-8"
    ).splitlines() == ["inspect", "exec", "exec", "exec"]
