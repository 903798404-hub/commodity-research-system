from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNNER = (
    PROJECT_ROOT
    / "09_deploy"
    / "soybean_crop_progress"
    / "run_soybean_crop_weekly_update.sh"
)
FAKE_SECRET = "runner-fixture-secret"
FAKE_GIT_HEAD = "4444444444444444444444444444444444444444"


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
printf '%s\n' "${MOCK_GIT_HEAD}"
""",
    )
    _write_mock(
        mock_bin / "docker",
        """#!/usr/bin/env bash
printf '%s\n' "$1" >>"${MOCK_DOCKER_CALLS}"
if [[ "$1" == "inspect" ]]; then
    printf '%s\n' "true"
    exit 0
fi
if [[ "$1" == "exec" ]]; then
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
            "MOCK_GIT_HEAD": FAKE_GIT_HEAD,
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
    assert 'git -C "${REPOSITORY_PATH}" rev-parse HEAD' in content
    assert "--env NASS_API_KEY" in content
    assert "--env MARKET_DATA_GIT_HEAD" in content
    assert "--env NASS_API_KEY=" not in content
    assert FAKE_SECRET not in content


def test_runner_reads_secret_and_host_head_without_putting_values_in_arguments(
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
    assert FAKE_GIT_HEAD not in docker_args
    assert FAKE_SECRET not in result.stdout
    assert FAKE_SECRET not in result.stderr
    assert (records / "container-secret.txt").read_text(
        encoding="utf-8"
    ) == FAKE_SECRET
    assert (records / "container-git-head.txt").read_text(
        encoding="utf-8"
    ) == FAKE_GIT_HEAD


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
    ).splitlines() == ["inspect", "exec"]
