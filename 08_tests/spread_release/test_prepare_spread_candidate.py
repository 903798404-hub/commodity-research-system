from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[2]
CONTRACT_DIR = REPOSITORY / "09_deploy" / "spread_release"
sys.path.insert(0, str(CONTRACT_DIR))

from prepare_spread_candidate import (  # noqa: E402
    CANDIDATE_SERVICE,
    CandidateOptions,
    build_candidate_compose,
    prepare_candidate,
    select_candidate_port,
)
from release_contract import CommandRunner, ContractError  # noqa: E402


IMAGE_ID = "sha256:" + "a" * 64
CONTAINER_ID = "b" * 64
SOURCE = "https://github.com/903798404-hub/commodity-research-system"
BUILD_TIME = "2026-07-25T12:00:00Z"


def _run_git(repository: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repository, text=True).strip()


def _copy_contract_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "trusted-checkout"
    repository.mkdir(parents=True)
    for relative in (
        "Dockerfile",
        ".dockerignore",
        "docker-compose.yml",
        "02_configs/historical_spread_config.xlsx",
    ):
        source = REPOSITORY / relative
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copytree(
        REPOSITORY / "09_deploy" / "spread_release",
        repository / "09_deploy" / "spread_release",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    _run_git(repository, "init", "-q")
    _run_git(repository, "config", "user.email", "tests@example.invalid")
    _run_git(repository, "config", "user.name", "Tests")
    _run_git(repository, "add", ".")
    _run_git(repository, "commit", "-qm", "trusted fixture")
    return (
        repository,
        _run_git(repository, "rev-parse", "HEAD"),
        _run_git(repository, "rev-parse", "HEAD^{tree}"),
    )


def _formal_compose(*, weather_read_only: bool = True, include_spread: bool = True) -> dict:
    services: dict[str, object] = {
        "usda-dashboard": {
            "image": "usda:test",
            "ports": [{"published": "8080", "target": 8080}],
        },
        "oil-world-dashboard": {
            "image": "oil:test",
            "ports": [{"published": "8081", "target": 8081}],
        },
    }
    if include_spread:
        services["spread-dashboard"] = {
            "image": "market-data-spread-dashboard:spread-20260723-b704a2933fec-b01",
            "container_name": "spread-dashboard",
            "restart": "unless-stopped",
            "command": ["streamlit", "run", "05_apps/streamlit_app.py"],
            "working_dir": "/app",
            "environment": {
                "MARKET_DATA_GIT_HEAD": "b704a2933fecc667695d5a31065abeea1fda7492",
                "WEATHER_DATA_DIR": "/app/runtime/weather/current",
                "USDA_DASHBOARD_URL": "https://dashboard.example/usda/",
                "OIL_WORLD_DASHBOARD_URL": "https://dashboard.example/oil-world/",
            },
            "volumes": [
                {
                    "type": "bind",
                    "source": "/home/ubuntu/market-data-runtime/weather/processed",
                    "target": "/app/runtime/weather",
                    "read_only": weather_read_only,
                }
            ],
            "depends_on": {"usda-dashboard": {"condition": "service_started"}},
            "networks": ["default"],
        }
    return {
        "name": "market-data",
        "services": services,
        "networks": {"default": {"name": "market-data_default"}},
    }


class FakeRunner(CommandRunner):
    def __init__(self, compose: dict) -> None:
        self.compose = compose
        self.commands: list[list[str]] = []
        self.built = False
        self.started = False

    def run(self, command, *, cwd=None, env=None):  # type: ignore[override]
        command = list(command)
        self.commands.append(command)
        if command[0] == "git":
            return super().run(command, cwd=cwd, env=env)
        if command[:3] == ["docker", "compose", "--env-file"]:
            return json.dumps(self.compose)
        if command[:2] == ["docker", "build"]:
            self.built = True
            return "built"
        if command[:2] == ["docker", "compose"] and "up" in command:
            self.started = True
            return "started"
        if command[:3] == ["docker", "inspect", "--format"]:
            return CONTAINER_ID + "\n"
        if command[:3] == ["docker", "rm", "-f"]:
            return "removed"
        raise AssertionError(f"unexpected command: {command}")


class FakeRuntime:
    def __init__(
        self,
        runner: FakeRunner,
        *,
        existing_container: bool = False,
        existing_image: bool = False,
    ) -> None:
        self.runner = runner
        self.existing_container = existing_container
        self.existing_image = existing_image

    def container_exists(self, name: str) -> bool:
        return self.existing_container or self.runner.started

    def image_record(self, image_ref: str):
        if not self.runner.built and not self.existing_image:
            raise ContractError("image is absent")
        return {"id": IMAGE_ID, "labels": {}}


def _options(tmp_path: Path, *, mode: str = "dry-run", compose: dict | None = None) -> tuple[CandidateOptions, FakeRunner]:
    repository, commit, tree = _copy_contract_repository(tmp_path)
    formal_compose = tmp_path / "formal-compose.yml"
    formal_compose.write_text("name: market-data\nservices: {}\n", encoding="utf-8")
    environment = tmp_path / "spread-production.env"
    environment.write_text(
        "\n".join(
            (
                "SPREAD_IMAGE=market-data-spread-dashboard:spread-20260723-b704a2933fec-b01",
                "MARKET_DATA_GIT_HEAD=b704a2933fecc667695d5a31065abeea1fda7492",
                "USDA_DASHBOARD_URL=https://dashboard.example/usda/",
                "OIL_WORLD_DASHBOARD_URL=https://dashboard.example/oil-world/",
                "WEATHER_RUNTIME_CURRENT_DIR=/home/ubuntu/market-data-runtime/weather/processed",
                "WEATHER_DATA_DIR=/app/runtime/weather/current",
                "",
            )
        ),
        encoding="utf-8",
    )
    release_id = f"spread-20260725-{commit[:12]}-b01"
    data_host_root = tmp_path / "data"
    data_host_root.mkdir()
    options = CandidateOptions(
        mode=mode,
        git_commit=commit,
        git_tree=tree,
        build_context=repository,
        production_compose_file=formal_compose,
        production_env_file=environment,
        image_ref=f"market-data-spread-dashboard:{release_id}",
        release_id=release_id,
        build_time=BUILD_TIME,
        source=SOURCE,
        output_directory=tmp_path / "candidate-output",
        candidate_container_name=f"spread-dashboard-candidate-{commit[:12]}-c01",
        candidate_port=18502,
        auto_port=False,
        data_host_root=data_host_root,
        rollback_image_ref="market-data-spread-dashboard:spread-20260723-b704a2933fec-b01",
        rollback_image_id="sha256:" + "c" * 64,
        formal_git_commit="b704a2933fecc667695d5a31065abeea1fda7492",
        cleanup_policy="on-failure",
        execute_build=mode == "execute",
        execute_start=mode == "execute",
    )
    return options, FakeRunner(compose or _formal_compose())


def test_dry_run_generates_isolated_candidate_without_build_or_start(tmp_path: Path) -> None:
    options, runner = _options(tmp_path)

    result = prepare_candidate(options, runner=runner, port_probe=lambda port: port == 18502)

    assert result["status"] == "planned"
    assert result["candidate_bind"] == "127.0.0.1:18502"
    assert not any(command[:2] == ["docker", "build"] for command in runner.commands)
    assert not any("up" in command for command in runner.commands)
    compose = json.loads((options.output_directory / "candidate-compose.json").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {CANDIDATE_SERVICE}
    service = compose["services"][CANDIDATE_SERVICE]
    assert service["restart"] == "no"
    assert service["ports"][0]["host_ip"] == "127.0.0.1"
    assert service["ports"][0]["target"] == 8501
    assert "depends_on" not in service
    assert service["volumes"][0]["read_only"] is True
    assert service["environment"]["WEATHER_DATA_DIR"] == "/app/runtime/weather/current"


@pytest.mark.parametrize("commit", ["deadbeef", "g" * 40])
def test_rejects_short_or_invalid_commit_before_docker(tmp_path: Path, commit: str) -> None:
    options, runner = _options(tmp_path)
    options = CandidateOptions(**{**options.__dict__, "git_commit": commit})

    with pytest.raises(ContractError):
        prepare_candidate(options, runner=runner)
    assert not any(command[0] == "docker" for command in runner.commands)


def test_rejects_dirty_build_context(tmp_path: Path) -> None:
    options, runner = _options(tmp_path)
    (options.build_context / "untracked.txt").write_text("dirty", encoding="utf-8")

    with pytest.raises(ContractError, match="clean"):
        prepare_candidate(options, runner=runner)


def test_rejects_tree_mismatch_before_docker(tmp_path: Path) -> None:
    options, runner = _options(tmp_path)
    options = CandidateOptions(**{**options.__dict__, "git_tree": "d" * 40})

    with pytest.raises(ContractError, match="Tree SHA"):
        prepare_candidate(options, runner=runner)
    assert not any(command[0] == "docker" for command in runner.commands)


def test_rejects_missing_spread_service_and_writable_weather_mount(tmp_path: Path) -> None:
    options, missing_runner = _options(tmp_path, compose=_formal_compose(include_spread=False))
    with pytest.raises(ContractError, match="spread-dashboard"):
        prepare_candidate(options, runner=missing_runner)

    options, mount_runner = _options(tmp_path / "writable", compose=_formal_compose(weather_read_only=False))
    with pytest.raises(ContractError, match="read-only"):
        prepare_candidate(options, runner=mount_runner)


def test_auto_port_skips_occupied_and_reserved_ports() -> None:
    assert select_candidate_port(None, forbidden_ports={8501, 18501}, probe=lambda port: port >= 18503) == 18503
    with pytest.raises(ContractError):
        select_candidate_port(8501, forbidden_ports={8501}, probe=lambda _: True)


def test_rejects_formal_image_as_candidate_and_candidate_name_collision(tmp_path: Path) -> None:
    options, runner = _options(tmp_path)
    production_environment = {
        "SPREAD_IMAGE": "market-data-spread-dashboard:spread-20260723-b704a2933fec-b01",
        "MARKET_DATA_GIT_HEAD": "b704a2933fecc667695d5a31065abeea1fda7492",
        "USDA_DASHBOARD_URL": "https://dashboard.example/usda/",
        "OIL_WORLD_DASHBOARD_URL": "https://dashboard.example/oil-world/",
        "WEATHER_RUNTIME_CURRENT_DIR": "/home/ubuntu/market-data-runtime/weather/processed",
        "WEATHER_DATA_DIR": "/app/runtime/weather/current",
    }
    with pytest.raises(ContractError, match="must not reuse"):
        build_candidate_compose(
            _formal_compose(),
            production_environment,
            git_commit=options.git_commit,
            image_ref=production_environment["SPREAD_IMAGE"],
            candidate_container_name=options.candidate_container_name,
            candidate_port=18502,
        )
    execute_options = CandidateOptions(
        **{**options.__dict__, "mode": "execute", "execute_build": True, "execute_start": True}
    )
    with pytest.raises(ContractError, match="container name already exists"):
        prepare_candidate(
            execute_options,
            runner=runner,
            runtime=FakeRuntime(runner, existing_container=True),
            port_probe=lambda _: True,
        )


def test_execute_uses_fake_docker_build_start_and_existing_validator(tmp_path: Path) -> None:
    options, runner = _options(tmp_path, mode="execute")
    runtime = FakeRuntime(runner)
    validated: list[tuple[Path, str]] = []

    def seal(*_args):
        release = options.output_directory / "releases" / options.release_id
        release.mkdir(parents=True)
        return release

    def validate(release: Path, health_url: str, _output: Path) -> None:
        validated.append((release, health_url))

    result = prepare_candidate(
        options,
        runner=runner,
        runtime=runtime,
        port_probe=lambda _: True,
        release_sealer=seal,
        validator=validate,
    )

    assert result["status"] == "prepared"
    assert result["candidate_image_id"] == IMAGE_ID
    assert result["candidate_container_id"] == CONTAINER_ID
    assert validated == [(options.output_directory / "releases" / options.release_id, result["candidate_health_url"])]
    assert any(command[:2] == ["docker", "build"] for command in runner.commands)
    assert any(command[:2] == ["docker", "compose"] and "up" in command for command in runner.commands)


def test_execute_validation_failure_cleans_only_candidate_container(tmp_path: Path) -> None:
    options, runner = _options(tmp_path, mode="execute")
    runtime = FakeRuntime(runner, existing_container=False)

    def seal(*_args):
        release = options.output_directory / "releases" / options.release_id
        release.mkdir(parents=True)
        return release

    def fail_validator(*_args) -> None:
        raise ContractError("readiness timed out")

    with pytest.raises(ContractError, match="readiness timed out"):
        prepare_candidate(
            options,
            runner=runner,
            runtime=runtime,
            port_probe=lambda _: True,
            release_sealer=seal,
            validator=fail_validator,
        )
    result = json.loads((options.output_directory / "candidate_prepare_result.json").read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["failure_phase"] == "candidate-validation"
    assert any(command[:3] == ["docker", "rm", "-f"] for command in runner.commands)
    assert all(PRODUCTION_NAME not in command for command in runner.commands)


def test_execute_rejects_preexisting_candidate_image_tag_without_overwrite(tmp_path: Path) -> None:
    options, runner = _options(tmp_path, mode="execute")
    with pytest.raises(ContractError, match="image tag already exists"):
        prepare_candidate(
            options,
            runner=runner,
            runtime=FakeRuntime(runner, existing_image=True),
            port_probe=lambda _: True,
        )
    result = json.loads((options.output_directory / "candidate_prepare_result.json").read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert not any(command[:2] == ["docker", "build"] for command in runner.commands)


PRODUCTION_NAME = "spread-dashboard"
