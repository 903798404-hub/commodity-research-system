from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[2]
FGIS_WRAPPER = REPOSITORY / "09_deploy/soybean_exports/run_fgis_yearly_update.sh"
FAS_WRAPPER = REPOSITORY / "09_deploy/soybean_exports/run_fas_export_sales_update.sh"
INSTALLER_PATH = (
    REPOSITORY / "09_deploy/soybean_exports/install_soybean_exports_runtime.py"
)
PROJECT_CONTRACT = REPOSITORY / "07_docs/projects/美豆销售与装船周度更新契约.md"
FAKE_SECRET = "fas-host-wrapper-fixture-secret"
RUNTIME_GIT_HEAD = "c07f2635a86a1c4f3e13167a982e5bb56e391b91"
IMAGE_ID = "sha256:" + "b" * 64


def _load_installer():
    spec = importlib.util.spec_from_file_location("soybean_runtime_installer", INSTALLER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    raise AssertionError("Bash is required to validate the production wrappers")


def _bash_path(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return resolved.as_posix()
    posix = resolved.as_posix()
    drive, remainder = posix.split(":", maxsplit=1)
    return f"/{drive.lower()}{remainder}"


def _write_mock(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _fas_environment(
    tmp_path: Path,
    *,
    secret_content: str | None = f"FAS_EXPORT_SALES_API_KEY={FAKE_SECRET}\n",
    flock_exit_code: int = 0,
    docker_exit_code: int = 0,
    container_running: str = "true",
    runtime_git_head: str = RUNTIME_GIT_HEAD,
    oci_revision: str = RUNTIME_GIT_HEAD,
) -> tuple[dict[str, str], Path, Path]:
    mock_bin = tmp_path / "mock-bin"
    records = tmp_path / "records"
    logs = tmp_path / "logs"
    runtime = tmp_path / "runtime"
    mock_bin.mkdir()
    records.mkdir()
    (runtime / "01_data").mkdir(parents=True)
    secret_file = tmp_path / "fas.env"
    if secret_content is not None:
        secret_file.write_text(secret_content, encoding="utf-8", newline="\n")

    _write_mock(
        mock_bin / "flock",
        """#!/usr/bin/env bash
printf '%s\n' "$*" >"${MOCK_FLOCK_ARGS}"
exit "${MOCK_FLOCK_EXIT_CODE}"
""",
    )
    _write_mock(
        mock_bin / "docker",
        """#!/usr/bin/env bash
printf '%s\n' "$1" >>"${MOCK_DOCKER_CALLS}"
if [[ "$1" == "inspect" ]]; then
    if [[ "$*" == *".State.Running"* ]]; then
        printf '%s\n' "${MOCK_CONTAINER_RUNNING}"
    elif [[ "$*" == *".Image"* ]]; then
        printf '%s\n' "${MOCK_IMAGE_ID}"
    else
        exit 2
    fi
    exit 0
fi
if [[ "$1" == "image" && "${2-}" == "inspect" ]]; then
    printf '%s\n' "${MOCK_OCI_REVISION}"
    exit 0
fi
if [[ "$1" == "exec" && "${2-}" == "spread-dashboard" && "${3-}" == "printenv" ]]; then
    printf '%s\n' "${MOCK_RUNTIME_GIT_HEAD}"
    exit 0
fi
if [[ "$1" == "run" ]]; then
    printf '%s\n' "$@" >"${MOCK_DOCKER_ARGS}"
    printf '%s' "${FAS_EXPORT_SALES_API_KEY-}" >"${MOCK_CONTAINER_SECRET}"
    printf '%s' "${MARKET_DATA_GIT_HEAD-}" >"${MOCK_CONTAINER_GIT_HEAD}"
    exit "${MOCK_DOCKER_EXIT_CODE}"
fi
exit 2
""",
    )

    environment = os.environ.copy()
    environment.update(
        {
            "MARKET_DATA_WRAPPER_PATH": f"{_bash_path(mock_bin)}:/usr/bin:/bin",
            "MARKET_DATA_FAS_ENV_FILE": _bash_path(secret_file),
            "MARKET_DATA_FAS_LOCK_FILE": _bash_path(tmp_path / "fas.lock"),
            "MARKET_DATA_FAS_LOG_ROOT": _bash_path(logs),
            "MARKET_DATA_FAS_RUNTIME_HOST_ROOT": _bash_path(runtime),
            "MOCK_FLOCK_EXIT_CODE": str(flock_exit_code),
            "MOCK_DOCKER_EXIT_CODE": str(docker_exit_code),
            "MOCK_CONTAINER_RUNNING": container_running,
            "MOCK_RUNTIME_GIT_HEAD": runtime_git_head,
            "MOCK_OCI_REVISION": oci_revision,
            "MOCK_IMAGE_ID": IMAGE_ID,
            "MOCK_FLOCK_ARGS": _bash_path(records / "flock-args.txt"),
            "MOCK_DOCKER_CALLS": _bash_path(records / "docker-calls.txt"),
            "MOCK_DOCKER_ARGS": _bash_path(records / "docker-args.txt"),
            "MOCK_CONTAINER_SECRET": _bash_path(records / "container-secret.txt"),
            "MOCK_CONTAINER_GIT_HEAD": _bash_path(records / "container-git-head.txt"),
        }
    )
    return environment, records, logs / "fas_export_sales_update.log"


def _run_fas_wrapper(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_bash_executable(), _bash_path(FAS_WRAPPER)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _trusted_source(tmp_path: Path) -> tuple[Path, str, str]:
    source = tmp_path / "trusted-source"
    wrapper_dir = source / "09_deploy" / "soybean_exports"
    wrapper_dir.mkdir(parents=True)
    for path in (FGIS_WRAPPER, FAS_WRAPPER):
        (wrapper_dir / path.name).write_bytes(path.read_bytes())
    _git(source, "init")
    _git(source, "config", "user.name", "runtime-test")
    _git(source, "config", "user.email", "runtime-test@example.invalid")
    _git(source, "add", "--", "09_deploy/soybean_exports")
    _git(source, "commit", "-m", "runtime wrappers")
    commit = _git(source, "rev-parse", "HEAD")
    tree = _git(source, "rev-parse", "HEAD^{tree}")
    _git(source, "checkout", "--detach", commit)
    return source, commit, tree


def test_shell_wrappers_have_valid_syntax_and_independent_contracts() -> None:
    bash = _bash_executable()
    for wrapper in (FGIS_WRAPPER, FAS_WRAPPER):
        completed = subprocess.run(
            [bash, "-n", _bash_path(wrapper)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert completed.returncode == 0, completed.stderr

    fgis = FGIS_WRAPPER.read_text(encoding="utf-8")
    fas = FAS_WRAPPER.read_text(encoding="utf-8")
    assert "/run/lock/fgis_yearly_update.lock" in fgis
    assert "/run/lock/fas_export_sales_update.lock" in fas
    assert "fgis_yearly_update.log" in fgis
    assert "fas_export_sales_update.log" in fas
    assert "docker run --rm --network none" in fgis
    assert "--source-file" in fgis
    assert "--source-metadata-file" in fgis
    assert "docker run --rm" in fas
    assert "--mount" in fas
    assert '"${CONTAINER_IMAGE_ID}"' in fas
    assert "org.opencontainers.image.revision" in fgis
    assert "org.opencontainers.image.revision" in fas
    assert "run_fas_export_sales.py" in fas
    assert "--allow-usda-api-key-fallback" not in fas
    assert "SPREAD_IMAGE" not in fas
    assert "b40cbb44795b" not in fas
    assert IMAGE_ID not in fas
    assert "flock -n 9" in fgis
    assert "flock -n 9" in fas

    fgis_source = (REPOSITORY / "03_src/agri_research_agent/soybean_exports/fgis.py").read_text(
        encoding="utf-8"
    )
    fas_source = (REPOSITORY / "03_src/agri_research_agent/soybean_exports/fas.py").read_text(
        encoding="utf-8"
    )
    assert "soybean_export_inspections.json" in fgis_source
    assert "soybean_export_sales.json" in fas_source


def test_contract_pins_approved_cron_to_fixed_host_runtime() -> None:
    contract = PROJECT_CONTRACT.read_text(encoding="utf-8")
    runtime = "/home/ubuntu/market-data-runtime/soybean-exports/current"
    assert (
        f"0 5 * * 3 /bin/bash {runtime}/run_fgis_yearly_update.sh" in contract
    )
    assert (
        f"15 6 * * 6 /bin/bash {runtime}/run_fas_export_sales_update.sh" in contract
    )
    assert "/home/ubuntu/market-data/09_deploy/soybean_exports" not in contract
    assert "本契约不表示 Cron 已安装" in contract


def test_fas_wrapper_loads_only_formal_secret_and_propagates_identity(
    tmp_path: Path,
) -> None:
    environment, records, log_file = _fas_environment(tmp_path)

    result = _run_fas_wrapper(environment)

    assert result.returncode == 0, result.stderr
    log = log_file.read_text(encoding="utf-8")
    docker_args = (records / "docker-args.txt").read_text(encoding="utf-8")
    assert "--env\nFAS_EXPORT_SALES_API_KEY\n" in docker_args
    assert "--env\nMARKET_DATA_GIT_HEAD\n" in docker_args
    assert "run_fas_export_sales.py" in docker_args
    assert "--runtime-root\n/runtime\n" in docker_args
    assert f"{IMAGE_ID}\n" in docker_args
    assert "dst=/runtime/01_data" in docker_args
    assert FAKE_SECRET not in docker_args
    assert FAKE_SECRET not in log
    assert FAKE_SECRET not in result.stdout
    assert FAKE_SECRET not in result.stderr
    assert (records / "container-secret.txt").read_text(encoding="utf-8") == FAKE_SECRET
    assert (records / "container-git-head.txt").read_text(encoding="utf-8") == RUNTIME_GIT_HEAD
    assert f"runtime_git_head={RUNTIME_GIT_HEAD}" in log
    assert f"oci_revision={RUNTIME_GIT_HEAD}" in log
    assert f"formal_image_id={IMAGE_ID}" in log


@pytest.mark.parametrize(
    "secret_content",
    [None, "", "FAS_EXPORT_SALES_API_KEY=\n", "FAS_EXPORT_SALES_API_KEY=a\nFAS_EXPORT_SALES_API_KEY=b\n"],
)
def test_fas_wrapper_fails_closed_when_formal_secret_is_invalid(
    tmp_path: Path, secret_content: str | None
) -> None:
    environment, records, log_file = _fas_environment(
        tmp_path, secret_content=secret_content
    )
    environment["USDA_API_KEY"] = "must-not-be-used"

    result = _run_fas_wrapper(environment)

    assert result.returncode == 1
    assert not (records / "docker-calls.txt").exists()
    log = log_file.read_text(encoding="utf-8")
    assert "FAS secret file is absent" in log or "FAS_EXPORT_SALES_API_KEY" in log
    assert "must-not-be-used" not in log


def test_fas_wrapper_lock_contention_skips_before_docker(tmp_path: Path) -> None:
    environment, records, log_file = _fas_environment(tmp_path, flock_exit_code=1)

    result = _run_fas_wrapper(environment)

    assert result.returncode == 0
    assert not (records / "docker-calls.txt").exists()
    assert "already has a running instance" in log_file.read_text(encoding="utf-8")


def test_fgis_wrapper_lock_contention_skips_before_network_or_docker(
    tmp_path: Path,
) -> None:
    mock_bin = tmp_path / "mock-bin"
    runtime = tmp_path / "runtime"
    source = tmp_path / "source"
    logs = tmp_path / "logs"
    records = tmp_path / "records"
    mock_bin.mkdir()
    (runtime / "01_data").mkdir(parents=True)
    source.mkdir()
    records.mkdir()
    _write_mock(mock_bin / "flock", "#!/usr/bin/env bash\nexit 1\n")
    _write_mock(mock_bin / "python3", "#!/usr/bin/env bash\nexit 99\n")
    _write_mock(
        mock_bin / "docker",
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >\"${MOCK_DOCKER_CALLS}\"\nexit 99\n",
    )
    _write_mock(
        mock_bin / "curl",
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >\"${MOCK_CURL_CALLS}\"\nexit 99\n",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "MARKET_DATA_WRAPPER_PATH": f"{_bash_path(mock_bin)}:/usr/bin:/bin",
            "MARKET_DATA_FGIS_LOCK_FILE": _bash_path(tmp_path / "fgis.lock"),
            "MARKET_DATA_FGIS_RUNTIME_HOST_ROOT": _bash_path(runtime),
            "MARKET_DATA_FGIS_SOURCE_ROOT": _bash_path(source),
            "MARKET_DATA_FGIS_LOG_ROOT": _bash_path(logs),
            "MOCK_DOCKER_CALLS": _bash_path(records / "docker-calls.txt"),
            "MOCK_CURL_CALLS": _bash_path(records / "curl-calls.txt"),
        }
    )

    result = subprocess.run(
        [_bash_executable(), _bash_path(FGIS_WRAPPER)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )

    assert result.returncode == 0
    assert not (records / "docker-calls.txt").exists()
    assert not (records / "curl-calls.txt").exists()
    assert "already has a running instance" in (
        logs / "fgis_yearly_update.log"
    ).read_text(encoding="utf-8")


def test_fas_wrapper_preserves_pipeline_exit_code(tmp_path: Path) -> None:
    environment, records, log_file = _fas_environment(tmp_path, docker_exit_code=37)

    result = _run_fas_wrapper(environment)

    assert result.returncode == 37
    assert "exit code 37" in log_file.read_text(encoding="utf-8")
    assert (records / "docker-args.txt").exists()


@pytest.mark.parametrize(
    ("container_running", "runtime_git_head"),
    [("false", RUNTIME_GIT_HEAD), ("true", "not-a-full-git-sha")],
)
def test_fas_wrapper_fails_closed_on_invalid_formal_runtime_identity(
    tmp_path: Path, container_running: str, runtime_git_head: str
) -> None:
    environment, records, log_file = _fas_environment(
        tmp_path,
        container_running=container_running,
        runtime_git_head=runtime_git_head,
    )

    result = _run_fas_wrapper(environment)

    assert result.returncode == 1
    assert not (records / "docker-args.txt").exists()
    log = log_file.read_text(encoding="utf-8")
    assert "not running" in log or "identity is missing or invalid" in log


def test_fas_wrapper_fails_closed_when_oci_revision_disagrees(tmp_path: Path) -> None:
    environment, records, log_file = _fas_environment(
        tmp_path,
        oci_revision="1" * 40,
    )

    result = _run_fas_wrapper(environment)

    assert result.returncode == 1
    assert not (records / "docker-args.txt").exists()
    assert "OCI revision and runtime Git identity disagree" in log_file.read_text(
        encoding="utf-8"
    )


def test_runtime_release_stages_verified_wrappers_and_identity_manifest(
    tmp_path: Path,
) -> None:
    installer = _load_installer()
    source, commit, tree = _trusted_source(tmp_path)
    runtime = tmp_path / "runtime"

    release_dir, manifest = installer.stage_runtime_release(
        source_root=source,
        runtime_root=runtime,
        expected_git_commit=commit,
        expected_git_tree=tree,
        expected_install_user=installer._current_user(),
        installed_at_utc="2026-08-08T08:00:00Z",
    )

    assert release_dir == runtime.resolve() / "releases" / commit
    assert manifest["wrapper_release_git_commit"] == commit
    assert manifest["wrapper_release_git_tree"] == tree
    assert manifest["installed_at_utc"] == "2026-08-08T08:00:00Z"
    assert (runtime / "logs").is_dir()
    for name, relative_path in installer.WRAPPERS.items():
        installed = release_dir / relative_path.name
        record = manifest["wrappers"][name]
        assert installed.read_bytes() == (source / relative_path).read_bytes()
        assert record["source_git_path"] == relative_path.as_posix()
        assert record["sha256"] == installer._sha256_file(installed)
        assert record["current_host_path"].endswith(f"/current/{relative_path.name}") or record[
            "current_host_path"
        ].endswith(f"\\current\\{relative_path.name}")
    assert json.loads((release_dir / "runtime_manifest.json").read_text(encoding="utf-8")) == manifest
    assert not list((runtime / "releases").glob(".staging-*"))


def test_runtime_install_uses_atomic_current_switch_hook(tmp_path: Path) -> None:
    installer = _load_installer()
    source, commit, tree = _trusted_source(tmp_path)
    runtime = tmp_path / "runtime"
    calls: list[tuple[Path, Path]] = []

    manifest = installer.install_runtime(
        source_root=source,
        runtime_root=runtime,
        expected_git_commit=commit,
        expected_git_tree=tree,
        expected_install_user=installer._current_user(),
        installed_at_utc="2026-08-08T08:00:00Z",
        switcher=lambda root, release: calls.append((root, release)),
    )

    assert manifest["wrapper_release_git_commit"] == commit
    assert calls == [(runtime.resolve(), runtime.resolve() / "releases" / commit)]
    source_text = INSTALLER_PATH.read_text(encoding="utf-8")
    assert "os.symlink(relative_target, temporary" in source_text
    assert "os.replace(temporary, current)" in source_text
    assert "if current.exists() and not current.is_symlink()" in source_text


def test_runtime_installer_rejects_attached_or_wrong_git_identity(tmp_path: Path) -> None:
    installer = _load_installer()
    source, commit, tree = _trusted_source(tmp_path)
    _git(source, "checkout", "-B", "attached-test", commit)

    with pytest.raises(installer.InstallError, match="detached HEAD"):
        installer.verify_trusted_source(
            source,
            expected_git_commit=commit,
            expected_git_tree=tree,
        )

    _git(source, "checkout", "--detach", commit)
    with pytest.raises(installer.InstallError, match="approved Git commit"):
        installer.verify_trusted_source(
            source,
            expected_git_commit="0" * 40,
            expected_git_tree=tree,
        )


def test_runtime_installer_rejects_dirty_trusted_source(tmp_path: Path) -> None:
    installer = _load_installer()
    source, commit, tree = _trusted_source(tmp_path)
    wrapper = source / installer.WRAPPERS["fas"]
    wrapper.write_bytes(wrapper.read_bytes() + b"\n")

    with pytest.raises(installer.InstallError, match="not clean"):
        installer.verify_trusted_source(
            source,
            expected_git_commit=commit,
            expected_git_tree=tree,
        )
