from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[2]
CONTRACT_DIR = REPOSITORY / "09_deploy" / "spread_release"
sys.path.insert(0, str(CONTRACT_DIR))

import prepare_spread_candidate as candidate_prepare  # noqa: E402
from prepare_spread_candidate import (  # noqa: E402
    CANDIDATE_DATA_CONTAINER_PATH,
    CANDIDATE_LOGS_CONTAINER_PATH,
    CANDIDATE_OUTPUTS_CONTAINER_PATH,
    CANDIDATE_TMP_CONTAINER_PATH,
    CANDIDATE_USER_CACHE_CONTAINER_PATH,
    CANDIDATE_SERVICE,
    CandidateOptions,
    build_candidate_compose,
    candidate_runtime_paths,
    normalized_mount_mode,
    prepare_candidate,
    select_candidate_port,
    validate_candidate_runtime_mounts,
)
from release_contract import CommandRunner, ContractError, DockerReleaseRuntime  # noqa: E402


IMAGE_ID = "sha256:" + "a" * 64
CONTAINER_ID = "b" * 64
SOURCE = "https://github.com/903798404-hub/commodity-research-system"
BUILD_TIME = "2026-07-25T12:00:00Z"
RUNTIME_GIT_COMMIT = "b704a2933fecc667695d5a31065abeea1fda7492"
RUNTIME_IMAGE_REF = "market-data-spread-dashboard:spread-20260723-b704a2933fec-b01"


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


def _formal_compose(
    data_host_root: Path | None = None,
    *,
    data_read_only: bool = True,
    weather_read_only: bool = True,
    include_spread: bool = True,
) -> dict:
    root = (data_host_root or Path("/home/ubuntu/market-data")).resolve()
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
            "labels": {
                "market-data.deployment.role": "production",
                "market-data.deployment.git_sha": "b704a2933fecc667695d5a31065abeea1fda7492",
            },
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
                    "source": str(root / "01_data"),
                    "target": CANDIDATE_DATA_CONTAINER_PATH,
                    "read_only": data_read_only,
                },
                {
                    "type": "bind",
                    "source": str(root / "06_outputs"),
                    "target": CANDIDATE_OUTPUTS_CONTAINER_PATH,
                    "read_only": False,
                },
                {
                    "type": "bind",
                    "source": str(root / "10_logs"),
                    "target": CANDIDATE_LOGS_CONTAINER_PATH,
                    "read_only": False,
                },
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
        self.omit_candidate_rw_read_only = False
        self.commands: list[list[str]] = []
        self.built = False
        self.started = False
        self.container_removed = False
        self.image_removed = False
        self.events: list[str] = []
        self.rendered_candidate: dict | None = None

    def run(self, command, *, cwd=None, env=None):  # type: ignore[override]
        command = list(command)
        self.commands.append(command)
        if command[0] == "git":
            return super().run(command, cwd=cwd, env=env)
        if command[:3] == ["docker", "compose", "--env-file"]:
            return json.dumps(self.compose)
        if command[:2] == ["docker", "compose"] and "config" in command:
            compose_path = Path(command[command.index("-f") + 1])
            rendered = json.loads(compose_path.read_text(encoding="utf-8"))
            if self.omit_candidate_rw_read_only:
                runtime_mount = rendered["services"][CANDIDATE_SERVICE]["volumes"][-1]
                if runtime_mount.get("target") == "/app/runtime/import_profit":
                    runtime_mount.pop("read_only", None)
            self.rendered_candidate = rendered
            return json.dumps(rendered)
        if command[:2] == ["docker", "build"]:
            self.built = True
            self.events.append("docker build")
            return "built"
        if command[:3] == ["docker", "image", "rm"]:
            self.image_removed = True
            self.events.append("candidate image cleanup")
            return "removed-image"
        if command[:2] == ["docker", "compose"] and "up" in command:
            self.started = True
            self.events.append("compose up")
            return "started"
        if command[:3] == ["docker", "inspect", "--format"]:
            return CONTAINER_ID + "\n"
        if command[:3] == ["docker", "exec", command[2]]:
            if command[-2:] == ["id", "-u"]:
                return "1000\n"
            if command[-2:] == ["id", "-g"]:
                return "1000\n"
            if "candidate-runtime-probe" in command:
                return ""
        if command[:3] == ["docker", "rm", "-f"]:
            self.container_removed = True
            self.events.append("candidate container cleanup")
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
        return self.existing_container or (self.runner.started and not self.runner.container_removed)

    def image_record(self, image_ref: str):
        if self.runner.image_removed:
            raise ContractError("image was removed")
        if not self.runner.built and not self.existing_image:
            raise ContractError("image is absent")
        return {"id": IMAGE_ID, "labels": {}}

    def container_record(self, container_name: str):
        if container_name != "spread-dashboard":
            if container_name.startswith("spread-dashboard-candidate-") and self.runner.rendered_candidate:
                service = self.runner.rendered_candidate["services"][CANDIDATE_SERVICE]
                volumes = service["volumes"]
                return {
                    "image_id": IMAGE_ID,
                    "config_image": service["image"],
                    "runtime_git_commit": service["environment"]["MARKET_DATA_GIT_HEAD"],
                    "mounts": [
                        {
                            "type": mount["type"],
                            "source": mount["source"],
                            "destination": mount["target"],
                            "read_only": normalized_mount_mode(mount) == "ro",
                        }
                        for mount in volumes
                    ]
                }
            raise ContractError(f"unexpected container record: {container_name}")
        return {
            "image_id": "sha256:" + "d" * 64,
            "config_image": RUNTIME_IMAGE_REF,
            "runtime_git_commit": RUNTIME_GIT_COMMIT,
        }


class DockerInspectFixtureRunner:
    def __init__(self, container_name: str, payload: dict) -> None:
        self.container_name = container_name
        self.payload = payload

    def run(self, command, *, cwd=None, env=None):
        del cwd, env
        assert command == ["docker", "inspect", self.container_name]
        return json.dumps([self.payload])


def _docker_mount(source: Path | str, destination: str, *, read_only: bool) -> dict:
    return {
        "Type": "bind",
        "Source": str(source.resolve()) if isinstance(source, Path) else source,
        "Destination": destination,
        "Mode": "ro" if read_only else "rw",
        "RW": not read_only,
        "Propagation": "rprivate",
    }


def _real_runtime_mount_fixture(
    tmp_path: Path,
) -> tuple[dict, Path, Path, dict[str, Path]]:
    formal_data = tmp_path / "formal-data" / "01_data"
    weather = tmp_path / "weather"
    formal_data.mkdir(parents=True)
    weather.mkdir()
    runtime_mounts = candidate_runtime_paths(tmp_path / "candidate-output")
    for path in runtime_mounts.values():
        path.mkdir(parents=True)
    mounts = [
        _docker_mount(formal_data, CANDIDATE_DATA_CONTAINER_PATH, read_only=True),
        _docker_mount(weather, "/app/runtime/weather", read_only=True),
        *[
            _docker_mount(path, target, read_only=False)
            for target, path in runtime_mounts.items()
        ],
    ]
    payload = {
        "Image": IMAGE_ID,
        "Config": {
            "Image": RUNTIME_IMAGE_REF,
            "Env": [f"MARKET_DATA_GIT_HEAD={RUNTIME_GIT_COMMIT}"],
        },
        "Mounts": mounts,
    }
    return payload, formal_data, weather, runtime_mounts


def _runtime_record(container_name: str, payload: dict) -> dict:
    return DockerReleaseRuntime(
        runner=DockerInspectFixtureRunner(container_name, payload)
    ).container_record(container_name)


def test_real_docker_inspect_mounts_pass_candidate_runtime_contract(
    tmp_path: Path,
) -> None:
    payload, formal_data, weather, runtime_mounts = _real_runtime_mount_fixture(
        tmp_path
    )

    record = _runtime_record("spread-dashboard-candidate-real-c01", payload)
    evidence = validate_candidate_runtime_mounts(
        record,
        formal_data_host_root=formal_data,
        weather_host_root=weather,
        candidate_runtime_mounts=runtime_mounts,
    )

    assert record["image_id"] == IMAGE_ID
    assert record["config_image"] == RUNTIME_IMAGE_REF
    assert record["runtime_git_commit"] == RUNTIME_GIT_COMMIT
    by_target = {mount["target"]: mount for mount in evidence}
    assert by_target[CANDIDATE_DATA_CONTAINER_PATH]["read_only"] is True
    assert by_target["/app/runtime/weather"]["read_only"] is True
    for target in (
        CANDIDATE_OUTPUTS_CONTAINER_PATH,
        CANDIDATE_LOGS_CONTAINER_PATH,
        CANDIDATE_USER_CACHE_CONTAINER_PATH,
        CANDIDATE_TMP_CONTAINER_PATH,
    ):
        assert by_target[target]["read_only"] is False


def test_real_docker_inspect_writable_production_data_fails(
    tmp_path: Path,
) -> None:
    payload, formal_data, weather, runtime_mounts = _real_runtime_mount_fixture(
        tmp_path
    )
    data_mount = next(
        mount
        for mount in payload["Mounts"]
        if mount["Destination"] == CANDIDATE_DATA_CONTAINER_PATH
    )
    data_mount["Mode"] = "rw"
    data_mount["RW"] = True

    record = _runtime_record("spread-dashboard-candidate-real-c01", payload)
    with pytest.raises(ContractError, match="mount contract failed"):
        validate_candidate_runtime_mounts(
            record,
            formal_data_host_root=formal_data,
            weather_host_root=weather,
            candidate_runtime_mounts=runtime_mounts,
        )


def test_real_docker_inspect_missing_mount_evidence_fails(tmp_path: Path) -> None:
    payload, formal_data, weather, runtime_mounts = _real_runtime_mount_fixture(
        tmp_path
    )
    payload.pop("Mounts")

    record = _runtime_record("spread-dashboard-candidate-real-c01", payload)
    with pytest.raises(ContractError, match="mount evidence is missing"):
        validate_candidate_runtime_mounts(
            record,
            formal_data_host_root=formal_data,
            weather_host_root=weather,
            candidate_runtime_mounts=runtime_mounts,
        )


def test_real_docker_inspect_missing_required_mount_fails(tmp_path: Path) -> None:
    payload, formal_data, weather, runtime_mounts = _real_runtime_mount_fixture(
        tmp_path
    )
    payload["Mounts"] = [
        mount
        for mount in payload["Mounts"]
        if mount["Destination"] != CANDIDATE_LOGS_CONTAINER_PATH
    ]

    record = _runtime_record("spread-dashboard-candidate-real-c01", payload)
    with pytest.raises(ContractError, match="mount set differs"):
        validate_candidate_runtime_mounts(
            record,
            formal_data_host_root=formal_data,
            weather_host_root=weather,
            candidate_runtime_mounts=runtime_mounts,
        )


def test_real_docker_inspect_unknown_writable_mount_fails(tmp_path: Path) -> None:
    payload, formal_data, weather, runtime_mounts = _real_runtime_mount_fixture(
        tmp_path
    )
    unknown_production = tmp_path / "formal-data" / "unexpected"
    unknown_production.mkdir()
    payload["Mounts"].append(
        _docker_mount(
            unknown_production,
            "/app/unapproved-production-data",
            read_only=False,
        )
    )

    record = _runtime_record("spread-dashboard-candidate-real-c01", payload)
    with pytest.raises(ContractError, match="mount set differs"):
        validate_candidate_runtime_mounts(
            record,
            formal_data_host_root=formal_data,
            weather_host_root=weather,
            candidate_runtime_mounts=runtime_mounts,
        )


def test_fake_and_real_runtime_container_records_share_mount_contract(
    tmp_path: Path,
) -> None:
    options, runner = _options(tmp_path)
    candidate, _ = _build_candidate_direct(options, runner)
    runner.rendered_candidate = candidate
    fake_record = FakeRuntime(runner).container_record(
        options.candidate_container_name
    )
    service = candidate["services"][CANDIDATE_SERVICE]
    payload = {
        "Image": IMAGE_ID,
        "Config": {
            "Image": service["image"],
            "Env": [
                f'MARKET_DATA_GIT_HEAD={service["environment"]["MARKET_DATA_GIT_HEAD"]}'
            ],
        },
        "Mounts": [
            _docker_mount(
                mount["source"],
                mount["target"],
                read_only=normalized_mount_mode(mount) == "ro",
            )
            for mount in service["volumes"]
        ],
    }
    real_record = _runtime_record(options.candidate_container_name, payload)

    assert set(fake_record) == set(real_record) == {
        "image_id",
        "config_image",
        "runtime_git_commit",
        "mounts",
    }
    assert {
        (item["type"], item["source"], item["destination"], item["read_only"])
        for item in fake_record["mounts"]
    } == {
        (item["type"], item["source"], item["destination"], item["read_only"])
        for item in real_record["mounts"]
    }


def _options(
    tmp_path: Path,
    *,
    mode: str = "dry-run",
    compose: dict | None = None,
    cleanup_policy: str = "on-failure",
) -> tuple[CandidateOptions, FakeRunner]:
    repository, commit, tree = _copy_contract_repository(tmp_path)
    data_host_root = tmp_path / "formal-data"
    for relative in ("01_data", "06_outputs", "10_logs"):
        (data_host_root / relative).mkdir(parents=True, exist_ok=True)
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
        cleanup_policy=cleanup_policy,
        execute_build=mode == "execute",
        execute_start=mode == "execute",
    )
    selected_compose = json.loads(json.dumps(compose)) if compose is not None else _formal_compose(data_host_root)
    spread = selected_compose.get("services", {}).get("spread-dashboard")
    if isinstance(spread, dict):
        for mount in spread.get("volumes", []):
            target = mount.get("target")
            if target == CANDIDATE_DATA_CONTAINER_PATH:
                mount["source"] = str((data_host_root / "01_data").resolve())
            elif target == CANDIDATE_OUTPUTS_CONTAINER_PATH:
                mount["source"] = str((data_host_root / "06_outputs").resolve())
            elif target == CANDIDATE_LOGS_CONTAINER_PATH:
                mount["source"] = str((data_host_root / "10_logs").resolve())
    return options, FakeRunner(selected_compose)


def _snapshot(_runtime: FakeRuntime, output: Path) -> tuple[Path, dict[str, str]]:
    path = output / "formal_containers.before_candidate.json"
    # The production contract rejects equal timestamps: a formal snapshot must
    # be strictly earlier than candidate preparation.  Keep the successful
    # fixture explicitly one second earlier rather than depending on Windows
    # clock precision between two consecutive datetime.now() calls.
    captured_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps({"captured_at": captured_at}) + "\n", encoding="utf-8")
    return path, {"captured_at": captured_at}


def _with_import_profit_runtime(options: CandidateOptions, tmp_path: Path) -> CandidateOptions:
    approved = tmp_path / "approved-import-profit-runtimes"
    runtime = approved / options.release_id
    runtime.mkdir(parents=True)
    return CandidateOptions(
        **{
            **options.__dict__,
            "import_profit_runtime_host": runtime,
            "import_profit_runtime_approved_root": approved,
            "formal_import_profit_runtime_host": tmp_path / "formal-import-profit-runtime",
            "pending_gate": "real_night_session_close_snapshot",
            "earliest_expected_business_date": "2026-08-03",
        }
    )


def _seal_candidate_result(
    _options: CandidateOptions,
    _runtime: FakeRuntime,
    _environment: dict[str, str],
    release: Path,
    _snapshot_path: Path,
    _output: Path,
    _formal_spread_before: dict[str, str],
    _candidate_health_url: str,
    _candidate_runtime_access: dict[str, object] | None,
) -> dict[str, str]:
    candidate_result = release / "candidate_result.json"
    return {
        "candidate_checks_path": str(release / "candidate_checks.json"),
        "candidate_result_path": str(candidate_result),
        "candidate_result_sha256": "a" * 64,
    }


def _seal_deployment_plan(
    _options: CandidateOptions,
    _runtime: FakeRuntime,
    _environment: dict[str, str],
    release: Path,
    _candidate_result: Path,
) -> dict[str, str]:
    deployment_plan = release / "deployment_plan.json"
    return {
        "deployment_plan_path": str(deployment_plan),
        "deployment_plan_sha256": "b" * 64,
    }


def _build_candidate_direct(
    options: CandidateOptions,
    runner: FakeRunner,
    *,
    image_ref: str | None = None,
) -> tuple[dict, dict[str, Path]]:
    runtime_mounts = candidate_runtime_paths(options.output_directory)
    for path in runtime_mounts.values():
        path.mkdir(parents=True, exist_ok=True)
    environment = candidate_prepare.parse_production_env(options.production_env_file)
    candidate = build_candidate_compose(
        runner.compose,
        environment,
        git_commit=options.git_commit,
        image_ref=image_ref or options.image_ref,
        candidate_container_name=options.candidate_container_name,
        candidate_port=18502,
        formal_data_host_root=(options.data_host_root / "01_data").resolve(),
        candidate_runtime_mounts=runtime_mounts,
        import_profit_runtime_host=(
            options.import_profit_runtime_host.resolve()
            if options.import_profit_runtime_host is not None
            else None
        ),
    )
    return candidate, runtime_mounts


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
    assert service["environment"]["PUBLIC_MARKET_DATA_RUNTIME_ROOT"] == (
        "/app/01_data"
    )
    assert service["labels"]["market-data.deployment.role"] == "candidate"
    assert service["labels"]["market-data.deployment.git_sha"] == options.git_commit
    assert "market-data.release.type=candidate" not in result["build_command"]
    assert "market-data.artifact.origin=candidate" in result["build_command"]
    assert "market-data.artifact.promotable=true" in result["build_command"]


def test_candidate_mounts_formal_data_read_only_and_isolates_all_writable_paths(
    tmp_path: Path,
) -> None:
    options, runner = _options(tmp_path)
    formal_sha = hashlib.sha256(options.production_compose_file.read_bytes()).hexdigest()

    result = prepare_candidate(options, runner=runner, port_probe=lambda _: True)

    compose = json.loads(Path(result["candidate_compose_file"]).read_text(encoding="utf-8"))
    volumes = compose["services"][CANDIDATE_SERVICE]["volumes"]
    by_target = {item["target"]: item for item in volumes}
    data_mount = by_target[CANDIDATE_DATA_CONTAINER_PATH]
    assert data_mount == {
        "type": "bind",
        "source": str((options.data_host_root / "01_data").resolve()),
        "target": CANDIDATE_DATA_CONTAINER_PATH,
        "read_only": True,
    }
    runtime = options.output_directory.resolve() / "runtime"
    assert by_target[CANDIDATE_OUTPUTS_CONTAINER_PATH]["source"] == str(runtime / "06_outputs")
    assert by_target[CANDIDATE_LOGS_CONTAINER_PATH]["source"] == str(runtime / "10_logs")
    assert by_target[CANDIDATE_USER_CACHE_CONTAINER_PATH]["source"] == str(runtime / "user-cache")
    assert by_target[CANDIDATE_TMP_CONTAINER_PATH]["source"] == str(runtime / "tmp")
    for target in (
        CANDIDATE_OUTPUTS_CONTAINER_PATH,
        CANDIDATE_LOGS_CONTAINER_PATH,
        CANDIDATE_USER_CACHE_CONTAINER_PATH,
        CANDIDATE_TMP_CONTAINER_PATH,
    ):
        assert normalized_mount_mode(by_target[target]) == "rw"
        assert Path(by_target[target]["source"]).is_relative_to(runtime)
    assert result["candidate_data_mount"]["mode"] == "ro"
    assert len(result["candidate_writable_mounts"]) == 4
    assert hashlib.sha256(options.production_compose_file.read_bytes()).hexdigest() == formal_sha


def test_candidate_rejects_writable_formal_data_mount(tmp_path: Path) -> None:
    options, runner = _options(
        tmp_path,
        compose=_formal_compose(data_read_only=False),
    )

    with pytest.raises(ContractError, match="formal /app/01_data mount must be read-only"):
        prepare_candidate(options, runner=runner, port_probe=lambda _: True)


def test_candidate_rejects_writable_formal_data_mount_after_generation(tmp_path: Path) -> None:
    options, runner = _options(tmp_path)
    result = prepare_candidate(options, runner=runner, port_probe=lambda _: True)
    candidate = json.loads(Path(result["candidate_compose_file"]).read_text(encoding="utf-8"))
    data_mount = next(
        mount
        for mount in candidate["services"][CANDIDATE_SERVICE]["volumes"]
        if mount["target"] == CANDIDATE_DATA_CONTAINER_PATH
    )
    data_mount["read_only"] = False
    runtime_mounts = candidate_runtime_paths(options.output_directory)
    with pytest.raises(ContractError, match="read-only/runtime contract|must be read-only"):
        candidate_prepare.validate_candidate_compose(
            candidate,
            image_ref=options.image_ref,
            candidate_container_name=options.candidate_container_name,
            candidate_port=18502,
            expected_weather_source="/home/ubuntu/market-data-runtime/weather/processed",
            expected_git_commit=options.git_commit,
            formal_volumes=runner.compose["services"]["spread-dashboard"]["volumes"],
            expected_formal_data_host_root=str((options.data_host_root / "01_data").resolve()),
            expected_candidate_runtime_mounts={
                target: str(path) for target, path in runtime_mounts.items()
            },
        )


def test_candidate_rejects_data_root_identity_mismatch(tmp_path: Path) -> None:
    options, runner = _options(tmp_path)
    wrong_root = tmp_path / "wrong-formal-root"
    (wrong_root / "01_data").mkdir(parents=True)
    changed = CandidateOptions(**{**options.__dict__, "data_host_root": wrong_root})
    with pytest.raises(ContractError, match="must equal"):
        prepare_candidate(changed, runner=runner, port_probe=lambda _: True)


def test_candidate_cli_no_longer_requires_a_copied_data_tree() -> None:
    help_text = candidate_prepare.build_parser().format_help()
    assert "--data-host-root" in help_text
    assert "--candidate-data-host-root" not in help_text
    assert "--candidate-data-approved-root" not in help_text
    assert "--candidate-basis-sha256" not in help_text


def test_candidate_runtime_rejects_production_or_checkout_overlap(tmp_path: Path) -> None:
    options, runner = _options(tmp_path)
    for output in (options.data_host_root / "candidate", options.build_context / "candidate"):
        changed = CandidateOptions(**{**options.__dict__, "output_directory": output})
        with pytest.raises(ContractError, match="must not overlap"):
            prepare_candidate(changed, runner=runner, port_probe=lambda _: True)


def test_live_mount_inspect_rejects_writable_production_data(tmp_path: Path) -> None:
    options, _runner = _options(tmp_path)
    runtime_mounts = candidate_runtime_paths(options.output_directory)
    for path in runtime_mounts.values():
        path.mkdir(parents=True, exist_ok=True)
    weather = tmp_path / "weather"
    weather.mkdir()
    record = {
        "mounts": [
            {
                "type": "bind",
                "source": str((options.data_host_root / "01_data").resolve()),
                "destination": CANDIDATE_DATA_CONTAINER_PATH,
                "read_only": False,
            },
            {
                "type": "bind",
                "source": str(weather.resolve()),
                "destination": "/app/runtime/weather",
                "read_only": True,
            },
            *[
                {
                    "type": "bind",
                    "source": str(path.resolve()),
                    "destination": target,
                    "read_only": False,
                }
                for target, path in runtime_mounts.items()
            ],
        ]
    }
    with pytest.raises(ContractError, match="mount contract failed"):
        validate_candidate_runtime_mounts(
            record,
            formal_data_host_root=options.data_host_root / "01_data",
            weather_host_root=weather,
            candidate_runtime_mounts=runtime_mounts,
        )


def test_candidate_rejects_docker_socket_mount(tmp_path: Path) -> None:
    options, runner = _options(tmp_path)
    runner.compose["services"]["spread-dashboard"]["volumes"].append(
        {
            "type": "bind",
            "source": "/var/run/docker.sock",
            "target": "/var/run/docker.sock",
            "read_only": True,
        }
    )
    with pytest.raises(ContractError, match="Docker socket"):
        prepare_candidate(options, runner=runner, port_probe=lambda _: True)


def test_dry_run_adds_only_the_approved_import_profit_rw_mount(tmp_path: Path) -> None:
    options, runner = _options(tmp_path)
    options = _with_import_profit_runtime(options, tmp_path)
    formal_sha = hashlib.sha256(options.production_compose_file.read_bytes()).hexdigest()

    result = prepare_candidate(options, runner=runner, port_probe=lambda _: True)

    compose = json.loads(Path(result["candidate_compose_file"]).read_text(encoding="utf-8"))
    service = compose["services"][CANDIDATE_SERVICE]
    by_target = {mount["target"]: mount for mount in service["volumes"]}
    assert by_target[CANDIDATE_DATA_CONTAINER_PATH]["read_only"] is True
    assert Path(by_target[CANDIDATE_OUTPUTS_CONTAINER_PATH]["source"]).is_relative_to(
        options.output_directory / "runtime"
    )
    assert Path(by_target[CANDIDATE_LOGS_CONTAINER_PATH]["source"]).is_relative_to(
        options.output_directory / "runtime"
    )
    assert service["volumes"][-1] == {
        "type": "bind",
        "source": str(options.import_profit_runtime_host.resolve()),
        "target": "/app/runtime/import_profit",
        "read_only": False,
    }
    assert service["environment"]["IMPORT_PROFIT_RUNTIME_ROOT"] == "/app/runtime/import_profit"
    assert result["import_profit_runtime_mount"] == {
        "mount_id": "import_profit_candidate_runtime",
        "candidate_batch_id": options.release_id,
        "target": "/app/runtime/import_profit",
        "mode": "rw",
        "environment_variable": "IMPORT_PROFIT_RUNTIME_ROOT",
    }
    assert str(options.import_profit_runtime_host) not in json.dumps(
        result["import_profit_runtime_mount"]
    )
    assert hashlib.sha256(options.production_compose_file.read_bytes()).hexdigest() == formal_sha


def test_compose_v2271_omitted_rw_default_has_identical_normalized_evidence(
    tmp_path: Path,
) -> None:
    explicit_options, explicit_runner = _options(tmp_path / "explicit")
    explicit_options = _with_import_profit_runtime(explicit_options, tmp_path / "explicit")
    explicit = prepare_candidate(explicit_options, runner=explicit_runner, port_probe=lambda _: True)

    omitted_options, omitted_runner = _options(tmp_path / "omitted")
    omitted_options = _with_import_profit_runtime(omitted_options, tmp_path / "omitted")
    omitted_runner.omit_candidate_rw_read_only = True
    omitted = prepare_candidate(omitted_options, runner=omitted_runner, port_probe=lambda _: True)

    explicit_evidence = {**explicit["import_profit_runtime_mount"], "candidate_batch_id": "normalized"}
    omitted_evidence = {**omitted["import_profit_runtime_mount"], "candidate_batch_id": "normalized"}
    assert explicit_evidence == omitted_evidence
    assert omitted_evidence["mode"] == "rw"


@pytest.mark.parametrize("read_only", [True, "false", 0])
def test_import_profit_runtime_rejects_non_rw_or_non_boolean_read_only(
    tmp_path: Path, read_only: object
) -> None:
    options, runner = _options(tmp_path)
    options = _with_import_profit_runtime(options, tmp_path)
    candidate, runtime_mounts = _build_candidate_direct(options, runner)
    candidate["services"][CANDIDATE_SERVICE]["volumes"][-1]["read_only"] = read_only
    with pytest.raises(ContractError):
        candidate_prepare.validate_candidate_compose(
            candidate,
            image_ref=options.image_ref,
            candidate_container_name=options.candidate_container_name,
            candidate_port=18502,
            expected_weather_source="/home/ubuntu/market-data-runtime/weather/processed",
            expected_git_commit=options.git_commit,
            formal_volumes=runner.compose["services"]["spread-dashboard"]["volumes"],
            expected_formal_data_host_root=str((options.data_host_root / "01_data").resolve()),
            expected_candidate_runtime_mounts={
                target: str(path) for target, path in runtime_mounts.items()
            },
            expected_import_profit_runtime_host=str(options.import_profit_runtime_host.resolve()),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("type", "volume"), ("target", "/app/runtime/wrong")],
)
def test_import_profit_runtime_rejects_wrong_mount_identity(
    tmp_path: Path, field: str, value: str
) -> None:
    options, runner = _options(tmp_path)
    options = _with_import_profit_runtime(options, tmp_path)
    candidate, runtime_mounts = _build_candidate_direct(options, runner)
    candidate["services"][CANDIDATE_SERVICE]["volumes"][-1][field] = value
    with pytest.raises(ContractError, match="runtime mount is invalid"):
        candidate_prepare.validate_candidate_compose(
            candidate,
            image_ref=options.image_ref,
            candidate_container_name=options.candidate_container_name,
            candidate_port=18502,
            expected_weather_source="/home/ubuntu/market-data-runtime/weather/processed",
            expected_git_commit=options.git_commit,
            formal_volumes=runner.compose["services"]["spread-dashboard"]["volumes"],
            expected_formal_data_host_root=str((options.data_host_root / "01_data").resolve()),
            expected_candidate_runtime_mounts={
                target: str(path) for target, path in runtime_mounts.items()
            },
            expected_import_profit_runtime_host=str(options.import_profit_runtime_host.resolve()),
        )


def test_import_profit_runtime_rejects_wrong_resolved_source(tmp_path: Path) -> None:
    options, runner = _options(tmp_path)
    options = _with_import_profit_runtime(options, tmp_path)
    wrong_source = tmp_path / "wrong-source"
    wrong_source.mkdir()
    candidate, runtime_mounts = _build_candidate_direct(options, runner)
    candidate["services"][CANDIDATE_SERVICE]["volumes"][-1]["source"] = str(wrong_source)
    with pytest.raises(ContractError, match="runtime mount is invalid"):
        candidate_prepare.validate_candidate_compose(
            candidate,
            image_ref=options.image_ref,
            candidate_container_name=options.candidate_container_name,
            candidate_port=18502,
            expected_weather_source="/home/ubuntu/market-data-runtime/weather/processed",
            expected_git_commit=options.git_commit,
            formal_volumes=runner.compose["services"]["spread-dashboard"]["volumes"],
            expected_formal_data_host_root=str((options.data_host_root / "01_data").resolve()),
            expected_candidate_runtime_mounts={
                target: str(path) for target, path in runtime_mounts.items()
            },
            expected_import_profit_runtime_host=str(options.import_profit_runtime_host.resolve()),
        )


def test_normalized_mount_mode_accepts_only_compose_boolean_semantics() -> None:
    assert normalized_mount_mode({"read_only": True}) == "ro"
    assert normalized_mount_mode({"read_only": False}) == "rw"
    assert normalized_mount_mode({}) == "rw"
    for invalid in ("false", 0, None):
        with pytest.raises(ContractError, match="must be a boolean"):
            normalized_mount_mode({"read_only": invalid})


def test_import_profit_runtime_path_safety_rules(tmp_path: Path) -> None:
    options, runner = _options(tmp_path)
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    base = {
        **options.__dict__,
        "import_profit_runtime_approved_root": approved,
        "formal_import_profit_runtime_host": tmp_path / "formal-runtime",
        "pending_gate": "real_night_session_close_snapshot",
        "earliest_expected_business_date": "2026-08-03",
    }
    for invalid in (tmp_path / "missing", outside, options.build_context):
        changed = CandidateOptions(**{**base, "import_profit_runtime_host": invalid})
        with pytest.raises(ContractError):
            prepare_candidate(changed, runner=runner, port_probe=lambda _: True)

    file_path = approved / "not-a-directory"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ContractError, match="directory"):
        prepare_candidate(
            CandidateOptions(**{**base, "import_profit_runtime_host": file_path}),
            runner=runner,
            port_probe=lambda _: True,
        )


def test_import_profit_runtime_rejects_formal_path_and_symlink_escape(tmp_path: Path) -> None:
    options, runner = _options(tmp_path)
    approved = tmp_path / "approved"
    approved.mkdir()
    formal = approved / "formal"
    formal.mkdir()
    common = {
        **options.__dict__,
        "import_profit_runtime_approved_root": approved,
        "formal_import_profit_runtime_host": formal,
        "pending_gate": "real_night_session_close_snapshot",
        "earliest_expected_business_date": "2026-08-03",
    }
    with pytest.raises(ContractError, match="overlap"):
        prepare_candidate(
            CandidateOptions(**{**common, "import_profit_runtime_host": formal}),
            runner=runner,
            port_probe=lambda _: True,
        )
    outside = tmp_path / "outside"
    outside.mkdir()
    link = approved / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(ContractError, match="approved"):
        prepare_candidate(
            CandidateOptions(**{**common, "import_profit_runtime_host": link}),
            runner=runner,
            port_probe=lambda _: True,
        )


def test_import_profit_runtime_rejects_invalid_container_path(tmp_path: Path) -> None:
    options, runner = _options(tmp_path)
    options = _with_import_profit_runtime(options, tmp_path)
    options = CandidateOptions(
        **{**options.__dict__, "import_profit_runtime_container": "/app/runtime/../01_data"}
    )
    with pytest.raises(ContractError, match="container path"):
        prepare_candidate(options, runner=runner, port_probe=lambda _: True)


def test_candidate_compose_config_failure_leaves_no_candidate_artifact(tmp_path: Path) -> None:
    options, runner = _options(tmp_path)

    original_run = runner.run

    def fail_candidate_config(command, *, cwd=None, env=None):
        if list(command)[:2] == ["docker", "compose"] and "config" in command and "--env-file" not in command:
            raise ContractError("candidate compose config failed")
        return original_run(command, cwd=cwd, env=env)

    runner.run = fail_candidate_config  # type: ignore[method-assign]
    with pytest.raises(ContractError, match="candidate compose config failed"):
        prepare_candidate(options, runner=runner, port_probe=lambda _: True)
    assert not (options.output_directory / "candidate-compose.json").exists()
    assert not (options.output_directory / ".candidate-compose.validating.json").exists()


def test_candidate_rejects_unapproved_formal_mount_before_runtime_overlap(tmp_path: Path) -> None:
    options, runner = _options(tmp_path)
    options = _with_import_profit_runtime(options, tmp_path)
    compose = runner.compose
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    compose["services"]["spread-dashboard"]["volumes"].append(
        {
            "type": "bind",
            "source": str(unrelated),
            "target": "/app/runtime",
            "read_only": True,
        }
    )
    with pytest.raises(ContractError, match="unapproved candidate mount"):
        prepare_candidate(options, runner=runner, port_probe=lambda _: True)


def test_root_compose_declares_future_production_runtime_role() -> None:
    compose_text = (REPOSITORY / "docker-compose.yml").read_text(encoding="utf-8")
    assert "market-data.deployment.role: production" in compose_text
    assert "market-data.deployment.git_sha: ${MARKET_DATA_GIT_HEAD" in compose_text
    assert "./01_data:/app/01_data:ro" in compose_text
    assert "./06_outputs:/app/06_outputs\n" in compose_text
    assert "./10_logs:/app/10_logs\n" in compose_text
    assert (
        "PUBLIC_MARKET_DATA_RUNTIME_ROOT: /app/01_data"
        in compose_text
    )
    assert (
        "${WEATHER_RUNTIME_CURRENT_DIR:?WEATHER_RUNTIME_CURRENT_DIR must be explicitly set}"
        ":/app/runtime/weather:ro"
    ) in compose_text


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

    missing_read_only = _formal_compose()
    weather_mount = next(
        mount
        for mount in missing_read_only["services"]["spread-dashboard"]["volumes"]
        if mount["target"] == "/app/runtime/weather"
    )
    weather_mount.pop("read_only")
    options, omitted_runner = _options(tmp_path / "omitted", compose=missing_read_only)
    with pytest.raises(ContractError, match="read-only"):
        prepare_candidate(options, runner=omitted_runner)


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
        _build_candidate_direct(
            options,
            runner,
            image_ref=production_environment["SPREAD_IMAGE"],
        )
    execute_options, execute_runner = _options(tmp_path / "collision", mode="execute")
    with pytest.raises(ContractError, match="container name already exists"):
        prepare_candidate(
            execute_options,
            runner=execute_runner,
            runtime=FakeRuntime(execute_runner, existing_container=True),
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
        formal_snapshotter=_snapshot,
        candidate_result_sealer=_seal_candidate_result,
        deployment_plan_sealer=_seal_deployment_plan,
    )

    assert result["status"] == "prepared"
    assert result["candidate_image_id"] == IMAGE_ID
    assert result["candidate_container_id"] == CONTAINER_ID
    assert result["candidate_image_removed"] is False
    assert result["candidate_image_retained_for_deployment"] is True
    assert validated == [(options.output_directory / "releases" / options.release_id, result["candidate_health_url"])]
    assert any(command[:2] == ["docker", "build"] for command in runner.commands)
    assert any(command[:2] == ["docker", "compose"] and "up" in command for command in runner.commands)
    inspected = {mount["target"]: mount for mount in result["candidate_runtime_mounts"]}
    assert inspected[CANDIDATE_DATA_CONTAINER_PATH]["read_only"] is True
    assert inspected["/app/runtime/weather"]["read_only"] is True
    for target in (
        CANDIDATE_OUTPUTS_CONTAINER_PATH,
        CANDIDATE_LOGS_CONTAINER_PATH,
        CANDIDATE_USER_CACHE_CONTAINER_PATH,
        CANDIDATE_TMP_CONTAINER_PATH,
    ):
        assert inspected[target]["read_only"] is False
        assert Path(inspected[target]["source"]).is_relative_to(
            options.output_directory / "runtime"
        )


def test_execute_waiting_candidate_probes_runtime_and_never_seals_deployment_plan(
    tmp_path: Path,
) -> None:
    options, runner = _options(tmp_path, mode="execute")
    options = _with_import_profit_runtime(options, tmp_path)
    runtime = FakeRuntime(runner)

    def seal(*_args):
        release = options.output_directory / "releases" / options.release_id
        release.mkdir(parents=True)
        return release

    def forbidden_plan(*_args):
        raise AssertionError("waiting candidate must not seal a deployment plan")

    result = prepare_candidate(
        options,
        runner=runner,
        runtime=runtime,
        port_probe=lambda _: True,
        release_sealer=seal,
        validator=lambda *_args: None,
        formal_snapshotter=_snapshot,
        candidate_result_sealer=_seal_candidate_result,
        deployment_plan_sealer=forbidden_plan,
    )

    assert result["status"] == "waiting-for-gate"
    assert result["candidate_result_status"] == "candidate-waiting-gate"
    assert result["deployment_plan_status"] == "blocked-by-pending-gate"
    assert result["candidate_container_removed"] is True
    assert result["candidate_runtime_access"] == {
        "container_path": "/app/runtime/import_profit",
        "environment_variable": "IMPORT_PROFIT_RUNTIME_ROOT",
        "uid": 1000,
        "gid": 1000,
        "read_write_probe": "passed",
    }
    assert any(command[:2] == ["docker", "exec"] for command in runner.commands)


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
            formal_snapshotter=_snapshot,
        )
    result = json.loads((options.output_directory / "candidate_prepare_result.json").read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["failure_phase"] == "candidate-validation"
    assert any(command[:3] == ["docker", "rm", "-f"] for command in runner.commands)
    assert all(PRODUCTION_NAME not in command for command in runner.commands)


def test_execute_obeys_the_formal_evidence_and_cleanup_order(tmp_path: Path) -> None:
    options, runner = _options(tmp_path, mode="execute", cleanup_policy="after-plan")
    events: list[str] = []

    def snapshot(*args):
        runner.events.append("formal snapshot")
        return _snapshot(*args)

    def seal(*_args):
        runner.events.append("release bundle")
        release = options.output_directory / "releases" / options.release_id
        release.mkdir(parents=True)
        return release

    def validate(*_args):
        runner.events.append("validate")

    def candidate_result(*args):
        runner.events.append("candidate result")
        return _seal_candidate_result(*args)

    def deployment_plan(*args):
        runner.events.append("deployment plan")
        return _seal_deployment_plan(*args)

    result = prepare_candidate(
        options,
        runner=runner,
        runtime=FakeRuntime(runner),
        port_probe=lambda _: True,
        release_sealer=seal,
        validator=validate,
        formal_snapshotter=snapshot,
        candidate_result_sealer=candidate_result,
        deployment_plan_sealer=deployment_plan,
    )
    assert events == []
    assert runner.events == [
        "formal snapshot",
        "docker build",
        "compose up",
        "release bundle",
        "validate",
        "candidate result",
        "candidate container cleanup",
        "deployment plan",
        "candidate image cleanup",
    ]
    assert result["candidate_container_removed"] is True
    assert result["candidate_image_removed"] is True
    assert result["candidate_result_path"].endswith("candidate_result.json")
    assert result["deployment_plan_path"].endswith("deployment_plan.json")


def test_plan_sealer_reads_current_env_and_seals_target_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options, runner = _options(tmp_path, mode="execute")
    release = options.output_directory / "releases" / options.release_id
    release.mkdir(parents=True)
    manifest = {
        "image_ref": options.image_ref,
        "git_commit": options.git_commit,
    }
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        candidate_prepare,
        "load_manifest_bundle",
        lambda *_args: (manifest, {}),
    )

    def fake_create_plan(**kwargs):
        captured.update(kwargs)
        return {
            "plan_status": "deployment_plan_sealed",
            "production_env_baseline_sha256": "a" * 64,
            "production_env_sha256": "b" * 64,
        }

    def fake_write_plan(_plan, path: Path) -> Path:
        path.write_text("{}\n", encoding="utf-8")
        return path

    monkeypatch.setattr(candidate_prepare, "create_deployment_plan", fake_create_plan)
    monkeypatch.setattr(candidate_prepare, "write_deployment_plan", fake_write_plan)

    def fake_load_plan(path: Path, loaded_manifest: dict, schema_path: Path):
        captured["loaded_plan"] = path
        captured["loaded_manifest"] = loaded_manifest
        captured["schema_path"] = schema_path
        return {}, {}

    monkeypatch.setattr(candidate_prepare, "load_deployment_plan", fake_load_plan)

    evidence = candidate_prepare._default_deployment_plan_sealer(
        options,
        FakeRuntime(runner),
        {
            "SPREAD_IMAGE": "market-data-spread-dashboard:spread-20260723-b704a2933fec-b01",
            "MARKET_DATA_GIT_HEAD": "b704a2933fecc667695d5a31065abeea1fda7492",
            "USDA_DASHBOARD_URL": "https://dashboard.example/usda/",
            "OIL_WORLD_DASHBOARD_URL": "https://dashboard.example/oil-world/",
            "WEATHER_RUNTIME_CURRENT_DIR": "/home/ubuntu/market-data-runtime/weather/processed",
            "WEATHER_DATA_DIR": "/app/runtime/weather/current",
        },
        release,
        release / "candidate_result.json",
    )

    production_env = Path(captured["production_env_file"])
    original_text = production_env.read_text(encoding="utf-8")
    assert production_env == options.production_env_file
    assert f"SPREAD_IMAGE={options.image_ref}" not in original_text
    assert f"MARKET_DATA_GIT_HEAD={options.git_commit}" not in original_text
    assert captured["production_compose_file"] == options.production_compose_file
    assert captured["production_project_dir"] == options.production_compose_file.parent
    assert str(options.output_directory / "runtime") not in json.dumps(
        captured, default=str, sort_keys=True
    )
    assert captured["schema_path"].name == "deployment_plan.schema.json"
    assert evidence["production_env_file"] == str(production_env)
    assert evidence["production_env_baseline_sha256"] == "a" * 64
    assert evidence["target_production_env_sha256"] == "b" * 64


def test_snapshot_failure_records_phase_and_never_calls_docker(tmp_path: Path) -> None:
    options, runner = _options(tmp_path, mode="execute")

    def fail_snapshot(*_args):
        raise ContractError("formal snapshot failed")

    with pytest.raises(ContractError, match="formal snapshot failed"):
        prepare_candidate(
            options,
            runner=runner,
            runtime=FakeRuntime(runner),
            port_probe=lambda _: True,
            formal_snapshotter=fail_snapshot,
        )
    result = json.loads((options.output_directory / "candidate_prepare_result.json").read_text(encoding="utf-8"))
    assert result["failure_phase"] == "formal_snapshot"
    assert not any(command[0] == "docker" and command[1] in {"build", "rm"} for command in runner.commands)


def test_rejects_stale_or_external_snapshot_before_build(tmp_path: Path) -> None:
    options, runner = _options(tmp_path, mode="execute")

    def stale_snapshot(_runtime, output):
        path = output / "formal_containers.before_candidate.json"
        path.write_text(json.dumps({"captured_at": BUILD_TIME}) + "\n", encoding="utf-8")
        return path, {"captured_at": BUILD_TIME}

    with pytest.raises(ContractError, match="too old"):
        prepare_candidate(
            options,
            runner=runner,
            runtime=FakeRuntime(runner),
            port_probe=lambda _: True,
            formal_snapshotter=stale_snapshot,
        )
    assert not any(command[:2] == ["docker", "build"] for command in runner.commands)

    external_options, external_runner = _options(tmp_path / "external", mode="execute")

    def external_snapshot(_runtime, _output):
        path = external_options.output_directory.parent / "old-snapshot.json"
        path.write_text(json.dumps({"captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}) + "\n", encoding="utf-8")
        return path, {"captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}

    with pytest.raises(ContractError, match="this candidate output directory"):
        prepare_candidate(
            external_options,
            runner=external_runner,
            runtime=FakeRuntime(external_runner),
            port_probe=lambda _: True,
            formal_snapshotter=external_snapshot,
        )
    assert not any(command[:2] == ["docker", "build"] for command in external_runner.commands)


def test_rejects_snapshot_timestamp_equal_to_preparation_check_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options, runner = _options(tmp_path, mode="execute")
    moments = iter(
        (
            "2026-07-25T11:58:00Z",  # result creation
            "2026-07-25T12:00:00Z",  # snapshot freshness check
        )
    )
    monkeypatch.setattr(candidate_prepare, "_utc_now", lambda: next(moments))

    def equal_snapshot(_runtime, output):
        path = output / "formal_containers.before_candidate.json"
        path.write_text('{"captured_at":"2026-07-25T12:00:00Z"}\n', encoding="utf-8")
        return path, {"captured_at": "2026-07-25T12:00:00Z"}

    with pytest.raises(ContractError, match="must precede candidate preparation"):
        prepare_candidate(
            options,
            runner=runner,
            runtime=FakeRuntime(runner),
            port_probe=lambda _: True,
            formal_snapshotter=equal_snapshot,
        )
    assert not any(command[:2] == ["docker", "build"] for command in runner.commands)


def test_rejects_snapshot_timestamp_later_than_candidate_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options, runner = _options(tmp_path, mode="execute")
    moments = iter(
        (
            "2026-07-25T11:58:00Z",  # result creation
            "2026-07-25T12:01:00Z",  # snapshot freshness check
            "2026-07-25T11:59:00Z",  # attempted candidate startup
        )
    )
    monkeypatch.setattr(candidate_prepare, "_utc_now", lambda: next(moments))

    def snapshot(_runtime, output):
        path = output / "formal_containers.before_candidate.json"
        path.write_text('{"captured_at":"2026-07-25T12:00:00Z"}\n', encoding="utf-8")
        return path, {"captured_at": "2026-07-25T12:00:00Z"}

    def seal(*_args):
        release = options.output_directory / "releases" / options.release_id
        release.mkdir(parents=True)
        return release

    with pytest.raises(ContractError, match="must precede candidate container startup"):
        prepare_candidate(
            options,
            runner=runner,
            runtime=FakeRuntime(runner),
            port_probe=lambda _: True,
            formal_snapshotter=snapshot,
            release_sealer=seal,
        )
    assert not any(command[:2] == ["docker", "compose"] and "up" in command for command in runner.commands)


def test_candidate_result_failure_does_not_return_prepared_and_cleans_candidate(tmp_path: Path) -> None:
    options, runner = _options(tmp_path, mode="execute")

    def seal(*_args):
        release = options.output_directory / "releases" / options.release_id
        release.mkdir(parents=True)
        return release

    def fail_candidate_result(*_args):
        raise ContractError("candidate result schema failed")

    with pytest.raises(ContractError, match="candidate result schema failed"):
        prepare_candidate(
            options,
            runner=runner,
            runtime=FakeRuntime(runner),
            port_probe=lambda _: True,
            release_sealer=seal,
            validator=lambda *_args: None,
            formal_snapshotter=_snapshot,
            candidate_result_sealer=fail_candidate_result,
        )
    result = json.loads((options.output_directory / "candidate_prepare_result.json").read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["failure_phase"] == "candidate-result"
    assert any(command[:3] == ["docker", "rm", "-f"] for command in runner.commands)
    assert any(command[:3] == ["docker", "image", "rm"] for command in runner.commands)


def test_deployment_plan_is_not_attempted_until_candidate_container_is_removed(tmp_path: Path) -> None:
    options, runner = _options(tmp_path, mode="execute")

    def seal(*_args):
        release = options.output_directory / "releases" / options.release_id
        release.mkdir(parents=True)
        return release

    def plan(_options, runtime, *_args):
        assert runtime.container_exists(options.candidate_container_name) is False
        raise ContractError("deployment plan schema failed")

    with pytest.raises(ContractError, match="deployment plan schema failed"):
        prepare_candidate(
            options,
            runner=runner,
            runtime=FakeRuntime(runner),
            port_probe=lambda _: True,
            release_sealer=seal,
            validator=lambda *_args: None,
            formal_snapshotter=_snapshot,
            candidate_result_sealer=_seal_candidate_result,
            deployment_plan_sealer=plan,
        )
    result = json.loads((options.output_directory / "candidate_prepare_result.json").read_text(encoding="utf-8"))
    assert result["failure_phase"] == "deployment-plan"
    assert result["candidate_container_removed"] is True


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
