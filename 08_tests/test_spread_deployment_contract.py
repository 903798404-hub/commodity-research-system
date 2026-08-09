from __future__ import annotations

import base64
import concurrent.futures
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml


REPOSITORY = Path(__file__).resolve().parents[1]
CONTRACT_DIR = REPOSITORY / "09_deploy" / "spread_release"
sys.path.insert(0, str(CONTRACT_DIR))

import release_contract as release_contract_module  # noqa: E402
import verify_release_contract as verifier_module  # noqa: E402
from release_contract import (  # noqa: E402
    APPLICATION,
    COMPOSE_PROJECT,
    CommandRunner,
    ContractError,
    DATA_SPECS,
    DEFAULT_READINESS_POLICY,
    DEPLOYMENT_RESULT_BUNDLE_FILENAME,
    DockerReleaseRuntime,
    PRODUCTION_ENV_KEYS,
    PRODUCTION_CONTAINER,
    SPREAD_PRIMARY_KEY,
    collect_data_baseline,
    capture_formal_container_snapshot,
    compare_formal_container_snapshots,
    candidate_compose_environment,
    create_candidate_result,
    create_deployment_plan,
    create_deployment_result_bundle,
    create_manifest,
    load_deployment_plan,
    load_manifest_bundle,
    load_candidate_result,
    load_deployment_result,
    load_deployment_result_bundle,
    load_schema,
    parse_production_env,
    pending_candidate_gate,
    require_deployable_candidate_result,
    resolve_spread_image_offline,
    require_formal_containers_unchanged,
    validate_deployment_plan,
    validate_deployment_result_bundle,
    validate_full_git_commit,
    validate_formal_container_snapshot,
    validate_manifest,
    validate_candidate_result,
    validate_artifact_manifest,
    validate_production_env,
    validate_release_image_ref,
    validate_repository_static,
    validate_rollback_image_ref,
    validate_source,
    verify_candidate,
    verify_post_deploy,
    verify_post_rollback,
    verify_pre_deploy,
    verify_pre_rollback,
    transition_production_env,
    write_deployment_plan,
    write_deployment_result_bundle,
    write_artifact_manifest,
    write_candidate_result,
    write_release_bundle,
    write_result,
)
from wait_for_service_ready import build_log_summary  # noqa: E402


GIT_COMMIT = "a" * 40
GIT_TREE = "c" * 40
TOOL_GIT_COMMIT = "d" * 40
TOOL_GIT_TREE = "e" * 40
OLD_GIT_COMMIT = "b" * 40
RELEASE_ID = "spread-20260717-aaaaaaaaaaaa-b01"
IMAGE_REF = f"market-data-spread-dashboard:{RELEASE_ID}"
IMAGE_ID = "sha256:" + "1" * 64
ROLLBACK_REF = "market-data-spread-dashboard:rollback-e0f7d859"
ROLLBACK_ID = "sha256:" + "2" * 64
LATEST_REF = "market-data-spread-dashboard:latest"
UNTAGGED_REF = "market-data-spread-dashboard"
BUILD_TIME = "2026-07-17T08:00:00Z"
SOURCE = "https://github.com/example/commodity-research-system"
CANDIDATE_CONTAINER = "spread-candidate-release"
PRODUCTION_USDA_URL = "https://dashboards.example.com/usda/"
PRODUCTION_OIL_WORLD_URL = "https://dashboards.example.com/oil-world/"
PRODUCTION_WEATHER_RUNTIME_DIR = "/home/ubuntu/market-data-runtime/weather/processed"
CANDIDATE_WEATHER_RUNTIME_DIR = PRODUCTION_WEATHER_RUNTIME_DIR
PRODUCTION_WEATHER_DATA_DIR = "/app/runtime/weather/current"
CANDIDATE_WEATHER_DATA_DIR = "/app/runtime/weather/next"
USDA_CONTAINER_ID = "3" * 64
OIL_WORLD_CONTAINER_ID = "4" * 64
USDA_IMAGE_ID = "sha256:" + "5" * 64
OIL_WORLD_IMAGE_ID = "sha256:" + "6" * 64
USDA_IMAGE_REF = f"market-data-usda-dashboard:{GIT_COMMIT}"
OIL_WORLD_IMAGE_REF = f"market-data-oil-world-dashboard:{GIT_COMMIT}"


class FakeGitRunner:
    def __init__(
        self,
        *,
        head: str = GIT_COMMIT,
        tree: str = GIT_TREE,
        status: str = "",
    ) -> None:
        self.head = head
        self.tree = tree
        self.status = status
        self.commands: list[tuple[list[str], Path | None]] = []

    def run(self, command, *, cwd=None, env=None) -> str:
        del env
        self.commands.append((list(command), cwd))
        if command[:2] == ["git", "status"]:
            return self.status
        if command == ["git", "rev-parse", "HEAD"]:
            return self.head + "\n"
        if command == ["git", "rev-parse", "HEAD^{tree}"]:
            return self.tree + "\n"
        raise AssertionError(f"unexpected git command: {command}")


class RecordingRealGitRunner(CommandRunner):
    def __init__(self) -> None:
        self.commands: list[tuple[list[str], Path | None]] = []

    def run(self, command, *, cwd=None, env=None) -> str:
        self.commands.append((list(command), cwd))
        return super().run(command, cwd=cwd, env=env)


class MultipleImageInspectRunner:
    def run(self, command, *, cwd=None, env=None) -> str:
        del cwd, env
        assert command[:3] == ["docker", "image", "inspect"]
        return json.dumps([{"Id": IMAGE_ID}, {"Id": ROLLBACK_ID}])


class ContainerInspectRunner:
    def __init__(self, environment: list[str] | None) -> None:
        self.environment = environment

    def run(self, command, *, cwd=None, env=None) -> str:
        del cwd, env
        assert command == ["docker", "inspect", CANDIDATE_CONTAINER]
        return json.dumps(
            [
                {
                    "Image": IMAGE_ID,
                    "Config": {
                        "Image": IMAGE_REF,
                        "Env": self.environment,
                    },
                }
            ]
        )


class FormalContainerInspectRunner:
    def run(self, command, *, cwd=None, env=None) -> str:
        del cwd, env
        if command == ["docker", "inspect", "usda-dashboard"]:
            return json.dumps([self._container_payload()])
        if command == ["docker", "image", "inspect", USDA_IMAGE_REF]:
            return json.dumps(
                [{
                    "Id": USDA_IMAGE_ID,
                    "Config": {
                        "Labels": {"org.opencontainers.image.revision": GIT_COMMIT}
                    },
                }]
            )
        raise AssertionError(f"unexpected command: {command}")

    @staticmethod
    def _container_payload() -> dict[str, object]:
        return {
            "Id": USDA_CONTAINER_ID,
            "Image": USDA_IMAGE_ID,
            "Created": BUILD_TIME,
            "RestartCount": 2,
            "Config": {"Image": USDA_IMAGE_REF, "Env": ["SECRET=must-not-leak"]},
            "State": {
                "Status": "running",
                "StartedAt": BUILD_TIME,
                "Health": {"Status": "healthy"},
            },
            "NetworkSettings": {
                "Ports": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}
            },
            "Mounts": [{
                "Type": "bind",
                "Source": "/srv/usda/data",
                "Destination": "/usr/share/nginx/html/data",
                "RW": False,
            }],
        }


class FakeReleaseRuntime:
    def __init__(
        self,
        *,
        git_commit: str = GIT_COMMIT,
        git_tree: str = GIT_TREE,
        release_id: str = RELEASE_ID,
        image_ref: str = IMAGE_REF,
        repository_root: Path = REPOSITORY,
    ) -> None:
        self.git_commit = git_commit
        self.git_tree = git_tree
        self.release_id = release_id
        self.image_ref = image_ref
        self.repository_root = repository_root.resolve()
        self.release_image_id = IMAGE_ID
        self.rollback_image_id = ROLLBACK_ID
        self.labels = {
            "org.opencontainers.image.revision": git_commit,
            "org.opencontainers.image.version": release_id,
            "org.opencontainers.image.created": BUILD_TIME,
            "org.opencontainers.image.source": SOURCE,
        }
        self.release_json = {
            "application": APPLICATION,
            "release_id": release_id,
            "git_commit": git_commit,
            "git_tree": git_tree,
            "build_time": BUILD_TIME,
            "source": SOURCE,
        }
        self.image_release_json = dict(self.release_json)
        self.production_release_json = dict(self.release_json)
        self.candidate_record = {
            "image_id": IMAGE_ID,
            "config_image": image_ref,
            "runtime_git_commit": git_commit,
        }
        self.production_record = {
            "image_id": ROLLBACK_ID,
            "config_image": ROLLBACK_REF,
            "runtime_git_commit": OLD_GIT_COMMIT,
        }
        self.compose_image: str | None = None
        self.compose_command: object = [
            "streamlit",
            "run",
            "05_apps/streamlit_app.py",
            "--server.address=0.0.0.0",
            "--server.port=8501",
        ]
        self.compose_ports: object = [
            {
                "mode": "ingress",
                "protocol": "tcp",
                "published": "8501",
                "target": 8501,
            }
        ]
        self.compose_mounts: object | None = None
        self.compose_template_variant = "release-template"
        self.candidate_command_override: object | None = None
        self.production_command_override: object | None = None
        self.candidate_mounts_override: object | None = None
        self.production_mounts_override: object | None = None
        self.production_ports_override: object | None = None
        self.dataset_overrides: dict[str, dict[str, object]] = {}
        self.container_record_calls: list[str] = []
        self.dataset_container_names: list[str] = []
        self.candidate_container_exists = False
        self.formal_records = {
            "usda-dashboard": {
                "service": "usda-dashboard",
                "container_id": USDA_CONTAINER_ID,
                "image_id": USDA_IMAGE_ID,
                "image_ref": USDA_IMAGE_REF,
                "oci_revision": GIT_COMMIT,
                "created_at": BUILD_TIME,
                "started_at": BUILD_TIME,
                "restart_count": 0,
                "status": "running",
                "health_status": "healthy",
                "ports": [{
                    "container_port": 80,
                    "protocol": "tcp",
                    "host_ip": "0.0.0.0",
                    "host_port": 8502,
                }],
                "mounts": [{
                    "type": "bind",
                    "source": "/home/ubuntu/market-data/11_apps/usda/public/data",
                    "destination": "/usr/share/nginx/html/data",
                    "read_only": True,
                }],
            },
            "oil-world-dashboard": {
                "service": "oil-world-dashboard",
                "container_id": OIL_WORLD_CONTAINER_ID,
                "image_id": OIL_WORLD_IMAGE_ID,
                "image_ref": OIL_WORLD_IMAGE_REF,
                "oci_revision": GIT_COMMIT,
                "created_at": BUILD_TIME,
                "started_at": BUILD_TIME,
                "restart_count": 0,
                "status": "running",
                "health_status": "not-configured",
                "ports": [{
                    "container_port": 80,
                    "protocol": "tcp",
                    "host_ip": "0.0.0.0",
                    "host_port": 8503,
                }],
                "mounts": [{
                    "type": "bind",
                    "source": "/home/ubuntu/market-data/11_apps/oil-world/public/data",
                    "destination": "/usr/share/nginx/html/data",
                    "read_only": True,
                }],
            },
        }

    def image_record(self, image_ref: str) -> dict[str, object]:
        if image_ref == self.image_ref:
            return {"id": self.release_image_id, "labels": dict(self.labels)}
        if image_ref in {ROLLBACK_REF, LATEST_REF}:
            return {
                "id": self.rollback_image_id,
                "labels": {
                    "org.opencontainers.image.revision": OLD_GIT_COMMIT,
                },
            }
        raise ContractError(f"unknown image reference in fixture: {image_ref}")

    def container_record(self, container_name: str) -> dict[str, str]:
        self.container_record_calls.append(container_name)
        if container_name == CANDIDATE_CONTAINER:
            return dict(self.candidate_record)
        if container_name == "spread-dashboard":
            return dict(self.production_record)
        raise ContractError(f"unknown container in fixture: {container_name}")

    def formal_container_identity(self, container_name: str) -> dict[str, object]:
        try:
            return copy.deepcopy(self.formal_records[container_name])
        except KeyError as exc:
            raise ContractError(
                f"unknown formal container in fixture: {container_name}"
            ) from exc

    def container_exists(self, container_name: str) -> bool:
        assert container_name == CANDIDATE_CONTAINER
        return self.candidate_container_exists

    def read_candidate_release(self, container_name: str) -> dict[str, str]:
        assert container_name == CANDIDATE_CONTAINER
        return dict(self.release_json)

    def candidate_release_sha256(self, container_name: str) -> str:
        assert container_name == CANDIDATE_CONTAINER
        return self._release_sha256(self.release_json)

    def read_image_release(self, image_ref: str) -> dict[str, str]:
        assert image_ref == self.image_ref
        return dict(self.image_release_json)

    def image_release_sha256(self, image_ref: str) -> str:
        assert image_ref == self.image_ref
        return self._release_sha256(self.image_release_json)

    def read_container_release(self, container_name: str) -> dict[str, str]:
        assert container_name == "spread-dashboard"
        return dict(self.production_release_json)

    def container_release_sha256(self, container_name: str) -> str:
        assert container_name == "spread-dashboard"
        return self._release_sha256(self.production_release_json)

    @staticmethod
    def _release_sha256(payload: dict[str, str]) -> str:
        raw = (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def compose_config(
        self,
        repository: Path,
        image_ref: str,
        *,
        environment: dict[str, str] | None = None,
        project_directory: Path | None = None,
        compose_file: Path | None = None,
    ) -> tuple[dict[str, object], str, list[str]]:
        assert repository.resolve() == self.repository_root
        assert image_ref in {self.image_ref, ROLLBACK_REF}
        project_root = (project_directory or repository).resolve()
        assert (compose_file or repository / "docker-compose.yml").is_file()
        resolved_environment = {
            "USDA_DASHBOARD_URL": PRODUCTION_USDA_URL,
            "OIL_WORLD_DASHBOARD_URL": PRODUCTION_OIL_WORLD_URL,
            "MARKET_DATA_GIT_HEAD": self.git_commit,
            "WEATHER_RUNTIME_CURRENT_DIR": CANDIDATE_WEATHER_RUNTIME_DIR,
            "WEATHER_DATA_DIR": CANDIDATE_WEATHER_DATA_DIR,
            **(environment or {}),
        }
        is_candidate = (
            resolved_environment["WEATHER_DATA_DIR"] == CANDIDATE_WEATHER_DATA_DIR
        )
        volumes = self.compose_mounts
        if is_candidate and self.candidate_mounts_override is not None:
            volumes = self.candidate_mounts_override
        if not is_candidate and self.production_mounts_override is not None:
            volumes = self.production_mounts_override
        if volumes is None:
            volumes = [
                {
                    "type": "bind",
                    "source": str((project_root / relative).resolve()),
                    "target": target,
                    "bind": {"create_host_path": True},
                }
                for relative, target in (
                    ("01_data", "/app/01_data"),
                    ("06_outputs", "/app/06_outputs"),
                    ("10_logs", "/app/10_logs"),
                )
            ]
            volumes.append(
                {
                    "type": "bind",
                    "source": resolved_environment["WEATHER_RUNTIME_CURRENT_DIR"],
                    "target": "/app/runtime/weather",
                    "read_only": True,
                }
            )
        compose = {
            "name": COMPOSE_PROJECT,
            "services": {
                "spread-dashboard": {
                    "image": self.compose_image or image_ref,
                    "build": {
                        "context": str(project_root),
                        "dockerfile": "Dockerfile",
                    },
                    "container_name": PRODUCTION_CONTAINER,
                    "command": (
                        self.candidate_command_override
                        if is_candidate and self.candidate_command_override is not None
                        else self.production_command_override
                        if not is_candidate
                        and self.production_command_override is not None
                        else self.compose_command
                    ),
                    "ports": (
                        self.production_ports_override
                        if not is_candidate
                        and self.production_ports_override is not None
                        else self.compose_ports
                    ),
                    "environment": {
                        key: resolved_environment[key]
                        for key in (
                            "MARKET_DATA_GIT_HEAD",
                            "USDA_DASHBOARD_URL",
                            "OIL_WORLD_DASHBOARD_URL",
                            "WEATHER_DATA_DIR",
                        )
                    },
                    "volumes": volumes,
                    "restart": "unless-stopped",
                    "x-test-template-variant": self.compose_template_variant,
                },
                "usda-dashboard": {
                    "build": {
                        "context": str(
                            project_root / "11_独立应用" / "USDA平衡表"
                        ),
                        "dockerfile": "Dockerfile",
                    }
                },
            },
        }
        raw = json.dumps(compose, ensure_ascii=False, sort_keys=True) + "\n"
        images = [self.compose_image or image_ref, "market-data-usda-dashboard"]
        return compose, raw, images

    def dataset_stats(self, container_name, spec) -> dict[str, object]:
        self.dataset_container_names.append(container_name)
        assert container_name in {CANDIDATE_CONTAINER, PRODUCTION_CONTAINER}
        return self.dataset_overrides.get(
            spec.name,
            {
                "records": 10,
                "latest_business_date": "2026-07-12",
                "primary_key_null_rows": 0,
                "duplicate_rows_on_key": 0,
                "invalid_date_rows": 0,
            },
        )


class LocalDatasetRunner:
    def __init__(self, parquet_path: Path) -> None:
        self.parquet_path = parquet_path

    def run(self, command, *, cwd=None, env=None) -> str:
        del cwd, env
        assert command[:5] == [
            "docker",
            "exec",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            CANDIDATE_CONTAINER,
        ]
        assert command[5:7] == ["python", "-c"]
        program = command[-2]
        payload = json.loads(
            base64.urlsafe_b64decode(command[-1]).decode("utf-8")
        )
        payload["path"] = str(self.parquet_path)
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        result = subprocess.run(
            [sys.executable, "-c", program, encoded],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ContractError(detail)
        return result.stdout


def create_data_files(root: Path) -> None:
    for index, spec in enumerate(DATA_SPECS, 1):
        path = root / spec.relative_host_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"parquet-fixture-{index}".encode())


def build_manifest(
    tmp_path: Path,
    *,
    weather_candidate_mode: str = "next",
) -> tuple[dict[str, object], FakeReleaseRuntime, FakeGitRunner]:
    data_root = tmp_path / "host-data"
    create_data_files(data_root)
    runtime = FakeReleaseRuntime()
    git = FakeGitRunner()
    manifest = create_manifest(
        repository=REPOSITORY,
        data_host_root=data_root,
        release_id=RELEASE_ID,
        git_commit=GIT_COMMIT,
        image_ref=IMAGE_REF,
        expected_image_id=IMAGE_ID,
        build_time=BUILD_TIME,
        source=SOURCE,
        candidate_container_name=CANDIDATE_CONTAINER,
        rollback_image_ref=ROLLBACK_REF,
        rollback_image_id=ROLLBACK_ID,
        formal_git_commit=OLD_GIT_COMMIT,
        production_environment=production_environment_for(),
        runtime=runtime,
        git_runner=git,
        weather_candidate_mode=weather_candidate_mode,
    )
    return manifest, runtime, git


def test_manifest_can_inspect_isolated_candidate_data_through_candidate_container(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "candidate-host-data"
    create_data_files(data_root)
    runtime = FakeReleaseRuntime()
    create_manifest(
        repository=REPOSITORY,
        data_host_root=data_root,
        release_id=RELEASE_ID,
        git_commit=GIT_COMMIT,
        image_ref=IMAGE_REF,
        expected_image_id=IMAGE_ID,
        build_time=BUILD_TIME,
        source=SOURCE,
        candidate_container_name=CANDIDATE_CONTAINER,
        rollback_image_ref=ROLLBACK_REF,
        rollback_image_id=ROLLBACK_ID,
        formal_git_commit=OLD_GIT_COMMIT,
        production_environment=production_environment_for(),
        runtime=runtime,
        git_runner=FakeGitRunner(),
        data_inspection_container_name=CANDIDATE_CONTAINER,
    )
    assert runtime.dataset_container_names == [CANDIDATE_CONTAINER] * len(DATA_SPECS)


def production_environment_for(
    *, image_ref: str = IMAGE_REF, git_commit: str = GIT_COMMIT,
    usda_url: str = PRODUCTION_USDA_URL,
    oil_world_url: str = PRODUCTION_OIL_WORLD_URL,
) -> dict[str, str]:
    return {
        "SPREAD_IMAGE": image_ref,
        "MARKET_DATA_GIT_HEAD": git_commit,
        "USDA_DASHBOARD_URL": usda_url,
        "OIL_WORLD_DASHBOARD_URL": oil_world_url,
        "WEATHER_RUNTIME_CURRENT_DIR": PRODUCTION_WEATHER_RUNTIME_DIR,
        "WEATHER_DATA_DIR": PRODUCTION_WEATHER_DATA_DIR,
    }


def create_production_env(
    tmp_path: Path,
    manifest: dict[str, object],
    *,
    usda_url: str = PRODUCTION_USDA_URL,
    oil_world_url: str = PRODUCTION_OIL_WORLD_URL,
    target: bool = False,
) -> tuple[Path, dict[str, str]]:
    environment = production_environment_for(
        image_ref=str(
            manifest["image_ref"] if target else manifest["rollback_image_ref"]
        ),
        git_commit=str(
            manifest["git_commit"] if target else manifest["formal_git_commit"]
        ),
        usda_url=usda_url,
        oil_world_url=oil_world_url,
    )
    path = (tmp_path / "spread-production.env").resolve()
    path.write_bytes(
        "".join(
            f"{key}={environment[key]}\n" for key in PRODUCTION_ENV_KEYS
        ).encode("utf-8")
    )
    path.chmod(0o600)
    return path, environment


def build_candidate_result_fixture(
    manifest: dict[str, object],
    runtime: FakeReleaseRuntime,
    *,
    page_status: str = "passed",
    blocking_gates: tuple[dict[str, object], ...] = (),
    completed_gates: tuple[dict[str, object], ...] = (),
    runtime_mounts: tuple[dict[str, object], ...] = (),
    candidate_runtime_access: dict[str, object] | None = None,
) -> dict[str, object]:
    return create_candidate_result(
        manifest=manifest,
        runtime=runtime,
        readiness={
            "schema_version": "1.1.0",
            "status": "ready",
            "container": manifest["candidate_container_name"],
            "expected_image_id": manifest["image_id"],
            "policy": manifest["readiness_policy"],
            "log_summary": build_log_summary(
                "candidate fixture WARNING\ncandidate fixture ready\n",
                collected_at_utc=BUILD_TIME,
            ),
        },
        checks={
            "http": {
                "health": 200,
                "host_config": 200,
                "root": 200,
            },
            "pages": {"status": page_status},
            "formal_containers_before": capture_formal_container_snapshot(
                runtime, captured_at=BUILD_TIME
            ),
            "formal_git_unchanged": True,
            "data_files_unchanged": True,
            "production_switch_performed": False,
        },
        generated_at=BUILD_TIME,
        blocking_gates=blocking_gates,
        completed_gates=completed_gates,
        runtime_mounts=runtime_mounts,
        candidate_runtime_access=candidate_runtime_access,
    )


def test_validate_build_time_accepts_docker_nanosecond_timestamps() -> None:
    parsed = release_contract_module.validate_build_time(
        "2026-07-16T14:19:22.640338145Z"
    )
    assert parsed.isoformat() == "2026-07-16T14:19:22.640338+00:00"


def write_candidate_result_fixture(
    tmp_path: Path,
    manifest: dict[str, object],
    runtime: FakeReleaseRuntime,
    *,
    page_status: str = "passed",
    **result_kwargs,
) -> Path:
    path = (tmp_path / "candidate_result.json").resolve()
    result = build_candidate_result_fixture(
        manifest,
        runtime,
        page_status=page_status,
        **result_kwargs,
    )
    write_candidate_result(result, path)
    return path


def create_production_project(tmp_path: Path) -> tuple[Path, Path]:
    production_project_dir = (tmp_path / "production-project").resolve()
    production_project_dir.mkdir()
    production_compose_file = production_project_dir / "docker-compose.yml"
    shutil.copyfile(REPOSITORY / "docker-compose.yml", production_compose_file)
    for name in ("01_data", "06_outputs", "10_logs"):
        (production_project_dir / name).mkdir()
    return production_project_dir, production_compose_file


def build_deployment_plan(
    tmp_path: Path,
    manifest: dict[str, object],
    runtime: FakeReleaseRuntime,
) -> tuple[dict[str, object], Path, dict[str, str]]:
    production_env_file, production_environment = create_production_env(
        tmp_path,
        manifest,
    )
    candidate_result_file = write_candidate_result_fixture(
        tmp_path,
        manifest,
        runtime,
    )
    production_project_dir, production_compose_file = create_production_project(
        tmp_path
    )
    plan = create_deployment_plan(
        tool_repo_root=REPOSITORY,
        production_compose_file=production_compose_file,
        production_project_dir=production_project_dir,
        candidate_result_file=candidate_result_file,
        production_env_file=production_env_file,
        manifest=manifest,
        runtime=runtime,
        schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
        created_at=BUILD_TIME,
        deployment_tool_git_runner=FakeGitRunner(
            head=TOOL_GIT_COMMIT,
            tree=TOOL_GIT_TREE,
        ),
    )
    return plan, production_env_file, production_environment


def apply_target_transition(
    plan: dict[str, object],
    runtime: FakeReleaseRuntime,
) -> dict[str, str]:
    transition_production_env(plan, to_target=True)
    runtime.production_record = {
        "image_id": IMAGE_ID,
        "config_image": IMAGE_REF,
        "runtime_git_commit": GIT_COMMIT,
    }
    return dict(plan["target_release"]["environment"])


def build_deployment_result(
    tmp_path: Path,
    manifest: dict[str, object],
    plan: dict[str, object],
) -> dict[str, object]:
    readiness_path = (tmp_path / "production_readiness.json").resolve()
    readiness = {
        "status": "ready",
        "container": PRODUCTION_CONTAINER,
        "expected_image_id": manifest["image_id"],
        "policy": manifest["readiness_policy"],
        "last_http_result": {"http_status": 200},
    }
    readiness_path.write_text(
        json.dumps(readiness, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    plan_path = (tmp_path / "sealed-plan-reference.json").resolve()
    plan_path.write_text(
        json.dumps(plan, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": "1.5.0",
        "application": APPLICATION,
        "release_id": manifest["release_id"],
        "git_commit": manifest["git_commit"],
        "target_git_commit": manifest["git_commit"],
        "git_tree": manifest["git_tree"],
        "image_ref": manifest["image_ref"],
        "candidate_image_id": manifest["image_id"],
        "actual_image_id": manifest["image_id"],
        "oci_revision": manifest["git_commit"],
        "runtime_git_commit": manifest["git_commit"],
        "runtime_git_commit_verified": True,
        "container_name": PRODUCTION_CONTAINER,
        "config_image": manifest["image_ref"],
        "formal_containers": compare_formal_container_snapshots(
            plan["formal_containers"]["after"],
            plan["formal_containers"]["after"],
            before_phase="pre-deploy",
            after_phase="post-deploy",
        ),
        "compose_project": plan["compose_project"],
        "production_service": plan["production_service"],
        "deployment_plan": str(plan_path),
        "deployment_plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "production_env_file": plan["production_env_file"],
        "production_env_before_sha256": plan[
            "production_env_baseline_sha256"
        ],
        "production_env_after_sha256": plan["production_env_sha256"],
        "production_env_sha256": plan["production_env_sha256"],
        "production_compose_sha256": plan["production_compose_sha256"],
        "deployment_tool_revision": plan["deployment_tool_revision"],
        "current_production": plan["current_production"],
        "target_release": plan["target_release"],
        "weather_runtime_contract": plan["weather_runtime_contract"],
        "weather_candidate_mode": plan["weather_candidate_mode"],
        "weather_candidate_source": plan["weather_candidate_source"],
        "weather_candidate_data_dir": plan["weather_candidate_data_dir"],
        "weather_runtime_current_dir": plan["weather_runtime_current_dir"],
        "weather_data_promotion_required": plan[
            "weather_data_promotion_required"
        ],
        "weather_data_changed": plan["weather_data_changed"],
        "processed_next_created": plan["processed_next_created"],
        "processed_current_modified": plan["processed_current_modified"],
        "readiness": readiness,
        "readiness_result": str(readiness_path),
        "readiness_result_sha256": hashlib.sha256(
            readiness_path.read_bytes()
        ).hexdigest(),
        "http_status": 200,
        "generated_at": BUILD_TIME,
        "status": "production_verified",
    }


def docker_compose_is_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "compose", "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def test_compose_declares_fixed_project_and_required_explicit_image() -> None:
    compose = yaml.safe_load(
        (REPOSITORY / "docker-compose.yml").read_text(encoding="utf-8")
    )
    spread = compose["services"]["spread-dashboard"]

    assert compose["name"] == COMPOSE_PROJECT
    assert (
        spread["image"]
        == "${SPREAD_IMAGE:?SPREAD_IMAGE must be set to an immutable release tag}"
    )
    assert "build" in spread
    assert spread["environment"]["MARKET_DATA_GIT_HEAD"] == (
        "${MARKET_DATA_GIT_HEAD:?"
        "MARKET_DATA_GIT_HEAD must be explicitly set}"
    )
    assert set(compose["services"]) == {"spread-dashboard"}


def test_offline_compose_resolution_rejects_missing_spread_image() -> None:
    compose_text = (REPOSITORY / "docker-compose.yml").read_text(encoding="utf-8")

    with pytest.raises(ContractError, match="SPREAD_IMAGE must be set"):
        resolve_spread_image_offline(compose_text, {})


def test_offline_compose_resolution_returns_exact_versioned_image() -> None:
    compose_text = (REPOSITORY / "docker-compose.yml").read_text(encoding="utf-8")

    assert (
        resolve_spread_image_offline(compose_text, {"SPREAD_IMAGE": IMAGE_REF})
        == IMAGE_REF
    )


def test_offline_compose_resolution_rejects_old_latest_fallback() -> None:
    compose_text = (REPOSITORY / "docker-compose.yml").read_text(encoding="utf-8")

    with pytest.raises(ContractError, match="forbidden mutable image tag"):
        resolve_spread_image_offline(compose_text, {"SPREAD_IMAGE": LATEST_REF})


def test_real_compose_environment_fixture_covers_required_variables() -> None:
    compose_text = (REPOSITORY / "docker-compose.yml").read_text(encoding="utf-8")
    required_variables = set(re.findall(r"\$\{([A-Z0-9_]+):\?", compose_text))
    complete_environment = production_environment_for()

    assert required_variables <= complete_environment.keys()
    assert complete_environment["WEATHER_DATA_DIR"] == PRODUCTION_WEATHER_DATA_DIR


@pytest.mark.skipif(
    not docker_compose_is_available(), reason="local Docker Compose is unavailable"
)
def test_real_compose_config_fails_without_spread_image() -> None:
    environment = {**os.environ, **production_environment_for()}
    environment.pop("SPREAD_IMAGE", None)
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(REPOSITORY),
            "-f",
            str(REPOSITORY / "docker-compose.yml"),
            "config",
        ],
        cwd=REPOSITORY,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "SPREAD_IMAGE must be set to an immutable release tag" in (
        result.stderr + result.stdout
    )


@pytest.mark.skipif(
    not docker_compose_is_available(), reason="local Docker Compose is unavailable"
)
def test_real_compose_config_fails_without_runtime_git_head() -> None:
    environment = {**os.environ, **production_environment_for()}
    environment.pop("MARKET_DATA_GIT_HEAD", None)
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(REPOSITORY),
            "-f",
            str(REPOSITORY / "docker-compose.yml"),
            "config",
        ],
        cwd=REPOSITORY,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "MARKET_DATA_GIT_HEAD must be explicitly set" in (
        result.stderr + result.stdout
    )


@pytest.mark.skipif(
    not docker_compose_is_available(), reason="local Docker Compose is unavailable"
)
def test_real_compose_config_resolves_exact_spread_image() -> None:
    environment = {**os.environ, **production_environment_for()}
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(REPOSITORY),
            "-f",
            str(REPOSITORY / "docker-compose.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=REPOSITORY,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["name"] == COMPOSE_PROJECT
    assert parsed["services"]["spread-dashboard"]["image"] == IMAGE_REF


@pytest.mark.parametrize(
    "commit",
    [
        "a" * 39,
        "A" * 40,
        "8249f3e01b4f",
        "not-a-commit",
    ],
)
def test_full_git_commit_is_mandatory(commit: str) -> None:
    with pytest.raises(ContractError, match="full 40-character"):
        validate_full_git_commit(commit)


@pytest.mark.parametrize(
    "image_ref",
    [
        LATEST_REF,
        "market-data-spread-dashboard:new",
        UNTAGGED_REF,
        "market-data-spread-dashboard@sha256:" + "3" * 64,
    ],
)
def test_formal_image_rejects_mutable_or_untagged_references(
    image_ref: str,
) -> None:
    with pytest.raises(ContractError):
        validate_release_image_ref(image_ref, RELEASE_ID)


def test_release_tag_must_exactly_equal_release_id() -> None:
    with pytest.raises(ContractError, match="exactly equal"):
        validate_release_image_ref(
            "market-data-spread-dashboard:spread-20260717-aaaaaaaaaaaa-b02",
            RELEASE_ID,
        )


def test_image_reference_must_resolve_to_exactly_one_image_object() -> None:
    runtime = DockerReleaseRuntime(runner=MultipleImageInspectRunner())

    with pytest.raises(ContractError, match="exactly one object"):
        runtime.image_record(IMAGE_REF)


def test_docker_container_inspect_returns_only_validated_runtime_git_head() -> None:
    runtime = DockerReleaseRuntime(
        runner=ContainerInspectRunner(
            [
                "PATH=/usr/local/bin",
                f"MARKET_DATA_GIT_HEAD={GIT_COMMIT}",
                "SECRET_TOKEN=do-not-record",
            ]
        )
    )

    assert runtime.container_record(CANDIDATE_CONTAINER) == {
        "image_id": IMAGE_ID,
        "config_image": IMAGE_REF,
        "runtime_git_commit": GIT_COMMIT,
    }


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        (None, "missing runtime MARKET_DATA_GIT_HEAD"),
        (["PATH=/bin"], "missing runtime MARKET_DATA_GIT_HEAD"),
        (["MARKET_DATA_GIT_HEAD="], "runtime MARKET_DATA_GIT_HEAD is empty"),
        (
            [
                f"MARKET_DATA_GIT_HEAD={GIT_COMMIT}",
                f"MARKET_DATA_GIT_HEAD={OLD_GIT_COMMIT}",
            ],
            "conflicting runtime MARKET_DATA_GIT_HEAD",
        ),
    ],
)
def test_docker_container_inspect_rejects_invalid_runtime_git_without_leaking_env(
    environment: list[str] | None,
    message: str,
) -> None:
    runtime = DockerReleaseRuntime(runner=ContainerInspectRunner(environment))

    with pytest.raises(ContractError, match=message) as exc_info:
        runtime.container_record(CANDIDATE_CONTAINER)

    error = str(exc_info.value)
    assert "SECRET_TOKEN" not in error
    assert "do-not-record" not in error
    assert GIT_COMMIT not in error
    assert OLD_GIT_COMMIT not in error


@pytest.mark.parametrize(
    "rollback_ref",
    [LATEST_REF, "market-data-spread-dashboard:new", UNTAGGED_REF],
)
def test_rollback_rejects_ambiguous_tags(rollback_ref: str) -> None:
    with pytest.raises(ContractError):
        validate_rollback_image_ref(rollback_ref)


def test_oci_source_rejects_server_ip_or_embedded_credentials() -> None:
    with pytest.raises(ContractError, match="server IP"):
        validate_source("https://192.0.2.10/market-data")
    with pytest.raises(ContractError, match="without credentials"):
        validate_source("https://user:token@example.com/market-data")


def test_manifest_is_valid_draft_2020_12_schema(tmp_path: Path) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    schema = load_schema(CONTRACT_DIR / "release.schema.json")

    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(manifest)
    validate_manifest(manifest, schema)
    assert manifest["status"] == "candidate_sealed"
    assert manifest["formal_git_commit"] == OLD_GIT_COMMIT
    assert len(manifest["image_release_json_sha256"]) == 64
    assert manifest["readiness_policy"] == DEFAULT_READINESS_POLICY


@pytest.mark.parametrize(
    "schema_name",
    [
        "release.schema.json",
        "candidate_result.schema.json",
        "artifact_manifest.schema.json",
        "deployment_plan.schema.json",
        "deployment_result.schema.json",
        "deployment_result_bundle.schema.json",
        "formal_container_identity.schema.json",
    ],
)
def test_release_contract_schemas_are_valid_draft_2020_12(
    schema_name: str,
) -> None:
    jsonschema.Draft202012Validator.check_schema(
        load_schema(CONTRACT_DIR / schema_name)
    )


def test_formal_container_snapshot_requires_every_identity_field() -> None:
    runtime = FakeReleaseRuntime()
    snapshot = capture_formal_container_snapshot(runtime, captured_at=BUILD_TIME)
    validate_formal_container_snapshot(snapshot)

    required_fields = (
        "container_id",
        "image_id",
        "image_ref",
        "oci_revision",
        "created_at",
        "started_at",
        "restart_count",
        "status",
        "health_status",
        "ports",
        "mounts",
    )
    for field in required_fields:
        missing = copy.deepcopy(snapshot)
        missing["containers"][0].pop(field)
        with pytest.raises(ContractError):
            validate_formal_container_snapshot(missing)


def test_docker_formal_identity_records_full_fields_without_environment_values() -> None:
    identity = DockerReleaseRuntime(
        runner=FormalContainerInspectRunner()
    ).formal_container_identity("usda-dashboard")

    assert identity == {
        "service": "usda-dashboard",
        "container_id": USDA_CONTAINER_ID,
        "image_id": USDA_IMAGE_ID,
        "image_ref": USDA_IMAGE_REF,
        "oci_revision": GIT_COMMIT,
        "created_at": BUILD_TIME,
        "started_at": BUILD_TIME,
        "restart_count": 2,
        "status": "running",
        "health_status": "healthy",
        "ports": [{
            "container_port": 80,
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
            "host_port": 8080,
        }],
        "mounts": [{
            "type": "bind",
            "source": "/srv/usda/data",
            "destination": "/usr/share/nginx/html/data",
            "read_only": True,
        }],
    }
    assert "SECRET" not in json.dumps(identity)


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("container_id", "7" * 64),
        ("image_id", "sha256:" + "8" * 64),
        ("image_ref", f"market-data-usda-dashboard:{OLD_GIT_COMMIT}"),
        ("oci_revision", OLD_GIT_COMMIT),
        ("created_at", "2026-07-17T08:00:01Z"),
        ("started_at", "2026-07-17T08:00:01Z"),
        ("restart_count", 1),
        ("status", "restarting"),
        ("health_status", "unhealthy"),
        ("ports", []),
        ("mounts", []),
    ],
)
def test_each_formal_container_identity_change_fails_closed(
    field: str, changed_value: object
) -> None:
    runtime = FakeReleaseRuntime()
    before = capture_formal_container_snapshot(runtime, captured_at=BUILD_TIME)
    after = copy.deepcopy(before)
    after["captured_at"] = "2026-07-17T08:00:02Z"
    after["containers"][0][field] = changed_value

    evidence = compare_formal_container_snapshots(
        before,
        after,
        before_phase="before-candidate",
        after_phase="after-candidate",
    )
    assert evidence["formal_containers_unchanged"] is False
    assert evidence["differences"] == [
        {
            "service": "usda-dashboard",
            "field": field,
            "before": before["containers"][0][field],
            "after": changed_value,
        }
    ]
    with pytest.raises(ContractError, match="formal containers changed"):
        require_formal_containers_unchanged(evidence, "test")


def test_candidate_result_rejects_changed_formal_container_identity(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    before = capture_formal_container_snapshot(runtime, captured_at=BUILD_TIME)
    runtime.formal_records["oil-world-dashboard"]["restart_count"] = 1

    with pytest.raises(ContractError, match="formal containers changed"):
        create_candidate_result(
            manifest=manifest,
            runtime=runtime,
            readiness={
                "schema_version": "1.1.0",
                "status": "ready",
                "container": manifest["candidate_container_name"],
                "expected_image_id": manifest["image_id"],
                "policy": manifest["readiness_policy"],
                "log_summary": build_log_summary("ready\n", collected_at_utc=BUILD_TIME),
            },
            checks={
                "http": {"health": 200, "host_config": 200, "root": 200},
                "pages": {"status": "passed"},
                "formal_containers_before": before,
                "formal_containers_unchanged": True,
                "formal_git_unchanged": True,
                "data_files_unchanged": True,
                "production_switch_performed": False,
            },
            generated_at=BUILD_TIME,
        )


def test_artifact_manifest_schema_independently_binds_type_file_and_version() -> None:
    schema = load_schema(CONTRACT_DIR / "artifact_manifest.schema.json")
    validator = jsonschema.Draft202012Validator(schema)
    candidate = {
        "schema_version": "1.4.0",
        "artifact_type": "candidate_result",
        "target_file": "candidate_result.json",
        "target_sha256": "d" * 64,
        "target_size_bytes": 123,
        "target_schema_version": "1.5.0",
        "generated_at": BUILD_TIME,
        "release_id": RELEASE_ID,
        "git_commit": GIT_COMMIT,
        "git_tree": GIT_TREE,
        "image_id": IMAGE_ID,
        "formal_evidence_sha256": "e" * 64,
        "runtime_git_commit": GIT_COMMIT,
    }
    validator.validate(candidate)

    mismatches = []
    wrong_type_file = copy.deepcopy(candidate)
    wrong_type_file["artifact_type"] = "release"
    mismatches.append(wrong_type_file)
    wrong_version = copy.deepcopy(candidate)
    wrong_version["target_schema_version"] = "1.1.0"
    mismatches.append(wrong_version)
    candidate_with_result_filename = copy.deepcopy(candidate)
    candidate_with_result_filename["target_file"] = "deployment_result.json"
    mismatches.append(candidate_with_result_filename)

    for payload in mismatches:
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(payload)

    release = copy.deepcopy(candidate)
    release.update(
        artifact_type="release",
        target_file="release.json",
        target_schema_version="2.6.0",
    )
    release.pop("runtime_git_commit")
    validator.validate(release)
    forged_release = copy.deepcopy(release)
    forged_release["runtime_git_commit"] = GIT_COMMIT
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(forged_release)

    deployment_result = copy.deepcopy(candidate)
    deployment_result.update(
        artifact_type="deployment_result",
        target_file="deployment_result.json",
        target_schema_version="1.4.0",
    )
    validator.validate(deployment_result)
    deployment_result.pop("runtime_git_commit")
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(deployment_result)

    deployment_plan = copy.deepcopy(release)
    deployment_plan.update(
        artifact_type="deployment_plan",
        target_file="deployment_plan.json",
        target_schema_version="1.5.0",
    )
    validator.validate(deployment_plan)
    deployment_plan["target_schema_version"] = "1.6.0"
    validator.validate(deployment_plan)


def test_schema_rejects_missing_required_field(tmp_path: Path) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    manifest.pop("image_id")

    with pytest.raises(ContractError, match="image_id is required"):
        validate_manifest(manifest, load_schema(CONTRACT_DIR / "release.schema.json"))


def test_manifest_rejects_sensitive_file_reference(tmp_path: Path) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    manifest["data_baseline"]["datasets"][0]["host_path"] = (
        "/tmp/.env/01_data/historical_spread_database.parquet"
    )

    with pytest.raises(ContractError, match="sensitive file reference"):
        validate_manifest(manifest, load_schema(CONTRACT_DIR / "release.schema.json"))


def test_manifest_requires_a_distinct_rollback_image_id(tmp_path: Path) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    manifest["rollback_image_id"] = IMAGE_ID

    with pytest.raises(ContractError, match="rollback_image_id must differ"):
        validate_manifest(manifest, load_schema(CONTRACT_DIR / "release.schema.json"))


def test_release_bundle_has_exact_env_and_valid_checksums(tmp_path: Path) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    release_directory = write_release_bundle(manifest, tmp_path / "releases")

    loaded, environment = load_manifest_bundle(
        release_directory / "release.json",
        release_directory / "release.env",
        CONTRACT_DIR / "release.schema.json",
    )

    assert loaded == manifest
    assert environment == {
        "RELEASE_ID": RELEASE_ID,
        "SPREAD_IMAGE": IMAGE_REF,
        "EXPECTED_IMAGE_ID": IMAGE_ID,
        "EXPECTED_GIT_COMMIT": GIT_COMMIT,
    }
    assert set(
        line.split("=", 1)[0]
        for line in (release_directory / "release.env")
        .read_text(encoding="utf-8")
        .splitlines()
    ) == {
        "RELEASE_ID",
        "SPREAD_IMAGE",
        "EXPECTED_IMAGE_ID",
        "EXPECTED_GIT_COMMIT",
    }


def test_release_bundle_is_not_silently_overwritten(tmp_path: Path) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    output_root = tmp_path / "releases"
    write_release_bundle(manifest, output_root)

    with pytest.raises(ContractError, match="will not be overwritten"):
        write_release_bundle(manifest, output_root)


def test_production_result_is_separate_and_does_not_rewrite_manifest(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    release_directory = write_release_bundle(manifest, tmp_path / "releases")
    manifest_path = release_directory / "release.json"
    original = manifest_path.read_bytes()
    result_path = release_directory / "deployment_result.json"

    result = build_deployment_result(tmp_path, manifest, plan)
    write_result(result_path, result)

    assert result_path.is_file()
    assert (release_directory / "deployment_result.manifest.json").is_file()
    assert load_deployment_result(result_path, manifest, plan) == result
    assert result["target_git_commit"] == GIT_COMMIT
    assert result["runtime_git_commit"] == GIT_COMMIT
    assert result["runtime_git_commit_verified"] is True
    assert manifest_path.read_bytes() == original
    with pytest.raises(ContractError, match="already exists"):
        write_result(result_path, result)


def test_deployment_result_bundle_is_sealed_and_reverified(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    result_path = (tmp_path / "deployment_result.json").resolve()
    result = build_deployment_result(tmp_path, manifest, plan)
    write_result(result_path, result)

    bundle_path = write_deployment_result_bundle(result_path, manifest, plan)
    bundle = load_deployment_result_bundle(bundle_path, manifest, plan)
    expected_members = (
        "deployment_result.json",
        "deployment_result.manifest.json",
    )

    assert bundle_path.name == DEPLOYMENT_RESULT_BUNDLE_FILENAME
    assert tuple(member["target_file"] for member in bundle["members"]) == (
        expected_members
    )
    assert bundle["release_id"] == result["release_id"]
    assert bundle["git_commit"] == result["git_commit"]
    assert bundle["git_tree"] == result["git_tree"]
    assert bundle["image_id"] == result["candidate_image_id"]
    assert bundle["runtime_git_commit"] == result["runtime_git_commit"]
    for member in bundle["members"]:
        member_path = result_path.with_name(member["target_file"])
        assert member["target_sha256"] == hashlib.sha256(
            member_path.read_bytes()
        ).hexdigest()
        assert member["target_size_bytes"] == member_path.stat().st_size
    jsonschema.Draft202012Validator(
        load_schema(CONTRACT_DIR / "deployment_result_bundle.schema.json")
    ).validate(bundle)


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("missing_member", "has too few items"),
        ("extra_member", "must exactly and deterministically equal"),
        ("duplicate_member", "must exactly and deterministically equal"),
        ("reordered_member", "must exactly and deterministically equal"),
        ("substituted_member", "must be one of"),
        ("sha256", "member SHA-256 mismatch"),
        ("size", "member byte size mismatch"),
        ("release_id", "release_id mismatch"),
        ("git_commit", "git_commit mismatch"),
        ("git_tree", "git_tree mismatch"),
        ("image_id", "image_id mismatch"),
        ("runtime_git_commit", "runtime_git_commit mismatch"),
    ],
)
def test_deployment_result_bundle_rejects_member_and_identity_tampering(
    tmp_path: Path,
    tamper: str,
    message: str,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    result_path = (tmp_path / "deployment_result.json").resolve()
    write_result(result_path, build_deployment_result(tmp_path, manifest, plan))
    bundle_path = write_deployment_result_bundle(result_path, manifest, plan)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))

    if tamper == "missing_member":
        bundle["members"].pop()
    elif tamper == "extra_member":
        bundle["members"].append(copy.deepcopy(bundle["members"][0]))
    elif tamper == "duplicate_member":
        bundle["members"][1]["target_file"] = "deployment_result.json"
    elif tamper == "reordered_member":
        bundle["members"].reverse()
    elif tamper == "substituted_member":
        bundle["members"][0]["target_file"] = "candidate_result.json"
    elif tamper == "sha256":
        bundle["members"][0]["target_sha256"] = "f" * 64
    elif tamper == "size":
        bundle["members"][0]["target_size_bytes"] += 1
    elif tamper == "release_id":
        bundle["release_id"] = "spread-20260717-bbbbbbbbbbbb-b01"
    elif tamper == "image_id":
        bundle["image_id"] = "sha256:" + "f" * 64
    else:
        bundle[tamper] = OLD_GIT_COMMIT
    bundle_path.write_text(json.dumps(bundle, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ContractError, match=message):
        validate_deployment_result_bundle(bundle_path, manifest, plan)


@pytest.mark.parametrize(
    "filename",
    [
        "../deployment_result.json",
        "/deployment_result.json",
        "C:\\release\\deployment_result.json",
        "results/deployment_result.json",
        "Deployment_Result.json",
    ],
)
def test_deployment_result_bundle_rejects_uncontrolled_member_filename(
    tmp_path: Path,
    filename: str,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    result_path = (tmp_path / "deployment_result.json").resolve()
    write_result(result_path, build_deployment_result(tmp_path, manifest, plan))
    bundle_path = write_deployment_result_bundle(result_path, manifest, plan)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["members"][0]["target_file"] = filename
    bundle_path.write_text(json.dumps(bundle, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ContractError, match="must be one of"):
        validate_deployment_result_bundle(bundle_path, manifest, plan)


def test_deployment_result_bundle_rejects_malformed_or_incomplete_json(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    result_path = (tmp_path / "deployment_result.json").resolve()
    write_result(result_path, build_deployment_result(tmp_path, manifest, plan))
    bundle_path = write_deployment_result_bundle(result_path, manifest, plan)

    bundle_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ContractError, match="cannot load deployment result bundle"):
        validate_deployment_result_bundle(bundle_path, manifest, plan)

    bundle = create_deployment_result_bundle(result_path, manifest, plan)
    bundle.pop("members")
    bundle_path.write_text(json.dumps(bundle, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ContractError, match="members is required"):
        validate_deployment_result_bundle(bundle_path, manifest, plan)


@pytest.mark.parametrize(
    "member_filename",
    [
        "deployment_result.json",
        "deployment_result.manifest.json",
    ],
)
def test_deployment_result_bundle_rejects_missing_member_file(
    tmp_path: Path,
    member_filename: str,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    result_path = (tmp_path / "deployment_result.json").resolve()
    write_result(result_path, build_deployment_result(tmp_path, manifest, plan))
    bundle_path = write_deployment_result_bundle(result_path, manifest, plan)
    result_path.with_name(member_filename).unlink()

    with pytest.raises(ContractError):
        validate_deployment_result_bundle(bundle_path, manifest, plan)


def test_deployment_result_bundle_write_failure_preserves_result_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    result_path = (tmp_path / "deployment_result.json").resolve()
    write_result(result_path, build_deployment_result(tmp_path, manifest, plan))
    original_result = result_path.read_bytes()
    original_manifest = result_path.with_name(
        "deployment_result.manifest.json"
    ).read_bytes()
    original_link = release_contract_module.os.link

    def fail_bundle_link(source, target, *args, **kwargs) -> None:
        if Path(target).name == DEPLOYMENT_RESULT_BUNDLE_FILENAME:
            raise OSError("simulated deployment result bundle write failure")
        original_link(source, target, *args, **kwargs)

    monkeypatch.setattr(release_contract_module.os, "link", fail_bundle_link)

    with pytest.raises(ContractError, match="bundle write failure"):
        write_deployment_result_bundle(result_path, manifest, plan)

    assert result_path.read_bytes() == original_result
    assert result_path.with_name("deployment_result.manifest.json").read_bytes() == (
        original_manifest
    )
    assert not result_path.with_name(DEPLOYMENT_RESULT_BUNDLE_FILENAME).exists()
    assert not list(tmp_path.glob(".deployment_result.bundle.manifest.json.*.tmp"))


def test_deployment_result_bundle_reverification_failure_removes_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    result_path = (tmp_path / "deployment_result.json").resolve()
    write_result(result_path, build_deployment_result(tmp_path, manifest, plan))
    original_result = result_path.read_bytes()
    original_manifest = result_path.with_name(
        "deployment_result.manifest.json"
    ).read_bytes()
    monkeypatch.setattr(
        release_contract_module,
        "validate_deployment_result_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ContractError("simulated deployment result bundle re-verification failure")
        ),
    )

    with pytest.raises(ContractError, match="bundle re-verification failure"):
        write_deployment_result_bundle(result_path, manifest, plan)

    assert result_path.read_bytes() == original_result
    assert result_path.with_name("deployment_result.manifest.json").read_bytes() == (
        original_manifest
    )
    assert not result_path.with_name(DEPLOYMENT_RESULT_BUNDLE_FILENAME).exists()


def test_deployment_runtime_git_tamper_in_json_or_manifest_is_rejected(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    result_path = tmp_path / "deployment_result.json"
    result = build_deployment_result(tmp_path, manifest, plan)
    write_result(result_path, result)
    artifact_manifest_path = tmp_path / "deployment_result.manifest.json"

    result["runtime_git_commit"] = OLD_GIT_COMMIT
    result_path.write_text(
        json.dumps(result, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="artifact SHA-256 mismatch"):
        load_deployment_result(result_path, manifest, plan)

    result["runtime_git_commit"] = GIT_COMMIT
    result_path.write_text(
        json.dumps(result, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    artifact_manifest = json.loads(
        artifact_manifest_path.read_text(encoding="utf-8")
    )
    artifact_manifest["target_sha256"] = hashlib.sha256(
        result_path.read_bytes()
    ).hexdigest()
    artifact_manifest["target_size_bytes"] = result_path.stat().st_size
    artifact_manifest["runtime_git_commit"] = OLD_GIT_COMMIT
    artifact_manifest_path.write_text(
        json.dumps(artifact_manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="runtime_git_commit mismatch"):
        load_deployment_result(result_path, manifest, plan)


def test_all_four_json_artifacts_have_exact_verified_manifest_names(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    release_directory = write_release_bundle(manifest, tmp_path / "releases")
    candidate_path = write_candidate_result_fixture(
        tmp_path,
        manifest,
        runtime,
    )
    plan_root = tmp_path / "plan-fixture"
    plan_root.mkdir()
    plan, _, _ = build_deployment_plan(plan_root, manifest, runtime)
    plan_path = tmp_path / "deployment_plan.json"
    write_deployment_plan(plan, plan_path)
    result_path = tmp_path / "deployment_result.json"
    write_result(result_path, build_deployment_result(tmp_path, manifest, plan))

    artifacts = [
        (
            release_directory / "release.json",
            release_directory / "release.manifest.json",
            "release",
        ),
        (
            candidate_path,
            tmp_path / "candidate_result.manifest.json",
            "candidate_result",
        ),
        (
            plan_path,
            tmp_path / "deployment_plan.manifest.json",
            "deployment_plan",
        ),
        (
            result_path,
            tmp_path / "deployment_result.manifest.json",
            "deployment_result",
        ),
    ]
    for target, artifact_manifest, artifact_type in artifacts:
        verified = validate_artifact_manifest(
            target,
            artifact_manifest,
            artifact_type=artifact_type,
            expected_release_id=RELEASE_ID,
            expected_git_commit=GIT_COMMIT,
            expected_git_tree=GIT_TREE,
            expected_image_id=IMAGE_ID,
        )
        assert verified["target_file"] == target.name
        assert verified["target_size_bytes"] == target.stat().st_size
        assert verified["target_sha256"] == hashlib.sha256(
            target.read_bytes()
        ).hexdigest()
        if artifact_type in {"candidate_result", "deployment_result"}:
            assert verified["runtime_git_commit"] == GIT_COMMIT
        else:
            assert "runtime_git_commit" not in verified


def test_candidate_result_is_formally_generated_and_write_once(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    path = write_candidate_result_fixture(tmp_path, manifest, runtime)
    loaded = load_candidate_result(path, manifest)

    assert loaded["git_commit"] == GIT_COMMIT
    assert loaded["git_tree"] == GIT_TREE
    assert loaded["candidate_image_id"] == IMAGE_ID
    assert loaded["candidate_container_name"] == CANDIDATE_CONTAINER
    assert loaded["status"] == "candidate-validated"
    assert loaded["identity"]["runtime_git_commit"] == GIT_COMMIT
    assert loaded["log_summary"] == loaded["checks"]["readiness"]["log_summary"]
    assert loaded["log_summary"]["stored_bytes"] <= 65536
    assert loaded["log_summary"]["sha256"] == hashlib.sha256(
        loaded["log_summary"]["tail_text"].encode("utf-8")
    ).hexdigest()
    assert (CONTRACT_DIR / "create_candidate_result.py").is_file()
    with pytest.raises(ContractError, match="will not be overwritten"):
        write_candidate_result(loaded, path)


def _import_profit_runtime_mount() -> dict[str, object]:
    return {
        "mount_id": "import_profit_candidate_runtime",
        "container_path": "/app/runtime/import_profit",
        "mode": "rw",
        "runtime_kind": "import_profit_candidate",
        "candidate_batch_id": RELEASE_ID,
        "required_environment_variable": "IMPORT_PROFIT_RUNTIME_ROOT",
    }


def _candidate_runtime_access() -> dict[str, object]:
    return {
        "container_path": "/app/runtime/import_profit",
        "environment_variable": "IMPORT_PROFIT_RUNTIME_ROOT",
        "uid": 1000,
        "gid": 1000,
        "read_write_probe": "passed",
    }


def test_candidate_waiting_gate_is_schema_valid_but_not_deployable(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    gate = pending_candidate_gate("real_night_session_close_snapshot", "2026-08-03")
    result = build_candidate_result_fixture(
        manifest,
        runtime,
        blocking_gates=(gate,),
        runtime_mounts=(_import_profit_runtime_mount(),),
        candidate_runtime_access=_candidate_runtime_access(),
    )

    validate_candidate_result(
        result,
        manifest,
        load_schema(CONTRACT_DIR / "candidate_result.schema.json"),
    )
    assert result["status"] == "candidate-waiting-gate"
    with pytest.raises(ContractError, match="not eligible"):
        require_deployable_candidate_result(result)

    path = (tmp_path / "candidate_result.json").resolve()
    write_candidate_result(result, path)
    artifact = json.loads(
        (tmp_path / "candidate_result.manifest.json").read_text(encoding="utf-8")
    )
    assert artifact["schema_version"] == "1.5.0"
    assert artifact["target_schema_version"] == "1.6.0"
    assert artifact["runtime_mounts"] == [_import_profit_runtime_mount()]
    assert "host" not in json.dumps(artifact).lower()


def test_legacy_validated_candidate_sample_remains_schema_compatible(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    result = build_candidate_result_fixture(manifest, runtime)
    result["schema_version"] = "1.5.0"
    for key in (
        "blocking_gates",
        "completed_gates",
        "runtime_mounts",
        "candidate_runtime_access",
    ):
        result.pop(key)
    validate_candidate_result(
        result,
        manifest,
        load_schema(CONTRACT_DIR / "candidate_result.schema.json"),
    )


def test_candidate_gate_schema_rejects_unknown_failed_and_malformed_states(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    schema = load_schema(CONTRACT_DIR / "candidate_result.schema.json")
    valid = build_candidate_result_fixture(manifest, runtime)
    for status in ("candidate-failed", "passed", "unknown"):
        changed = {**valid, "status": status}
        with pytest.raises(ContractError):
            validate_candidate_result(changed, manifest, schema)
    waiting_without_gate = {**valid, "status": "candidate-waiting-gate"}
    with pytest.raises(ContractError):
        validate_candidate_result(waiting_without_gate, manifest, schema)
    validated_with_gate = {
        **valid,
        "blocking_gates": [pending_candidate_gate("real_night_session_close_snapshot", "2026-08-03")],
    }
    with pytest.raises(ContractError):
        validate_candidate_result(validated_with_gate, manifest, schema)


def test_import_profit_validated_candidate_requires_completed_real_gate(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    result = build_candidate_result_fixture(
        manifest,
        runtime,
        runtime_mounts=(_import_profit_runtime_mount(),),
        candidate_runtime_access=_candidate_runtime_access(),
    )
    with pytest.raises(ContractError, match="completed real 08:30 night-session gate"):
        require_deployable_candidate_result(result)

    result["completed_gates"] = [
        {
            "gate_id": "real_night_session_close_snapshot",
            "status": "completed",
            "business_date": "2026-08-03",
            "captured_at": "2026-08-03T08:30:30+08:00",
            "target_contracts": ["m2609", "y2609"],
            "available_contracts": ["m2609", "y2609"],
            "missing_contracts": [],
            "capture_status": "success",
            "business_date_validation": "passed",
            "source_time_validation": "passed",
            "snapshot_freeze_validation": "passed",
            "cnf_refetch_validation": "passed",
            "snapshot_batch_id": "dce-20260803-083030",
            "candidate_sha256": "f" * 64,
        }
    ]
    require_deployable_candidate_result(result)
    result["completed_gates"][0]["available_contracts"] = ["m2609"]
    result["completed_gates"][0]["missing_contracts"] = ["y2609"]
    result["completed_gates"][0]["capture_status"] = "passed_with_incomplete"
    require_deployable_candidate_result(result)


def test_exclusive_artifact_write_preserves_existing_content_and_hash(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    result = build_candidate_result_fixture(manifest, runtime)
    path = (tmp_path / "candidate_result.json").resolve()
    original = b'{"existing":"must-survive"}\n'
    path.write_bytes(original)
    original_hash = hashlib.sha256(original).hexdigest()

    with pytest.raises(ContractError, match="already exists"):
        write_candidate_result(result, path)

    assert path.read_bytes() == original
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
    assert not (tmp_path / "candidate_result.manifest.json").exists()


def test_exclusive_manifest_write_preserves_existing_manifest(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    path = write_candidate_result_fixture(tmp_path, manifest, runtime)
    sealed_path = tmp_path / "candidate_result.manifest.json"
    original = sealed_path.read_bytes()
    original_hash = hashlib.sha256(original).hexdigest()

    with pytest.raises(ContractError, match="already exists"):
        write_artifact_manifest(
            path,
            artifact_type="candidate_result",
            target_schema_version="1.5.0",
            release_id=RELEASE_ID,
            git_commit=GIT_COMMIT,
            git_tree=GIT_TREE,
            image_id=IMAGE_ID,
            runtime_git_commit=GIT_COMMIT,
        )

    assert sealed_path.read_bytes() == original
    assert hashlib.sha256(sealed_path.read_bytes()).hexdigest() == original_hash


def test_competing_exclusive_writers_allow_exactly_one_complete_chain(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    result = build_candidate_result_fixture(manifest, runtime)
    path = (tmp_path / "candidate_result.json").resolve()

    def write_once() -> str:
        try:
            write_candidate_result(copy.deepcopy(result), path)
        except ContractError:
            return "rejected"
        return "written"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: write_once(), range(2)))

    assert outcomes.count("written") == 1
    assert outcomes.count("rejected") == 1
    assert load_candidate_result(path, manifest) == result


def test_manifest_write_failure_removes_unsealed_json_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    result = build_candidate_result_fixture(manifest, runtime)
    path = (tmp_path / "candidate_result.json").resolve()
    original_writer = release_contract_module._write_json_exclusive

    def fail_manifest(target: Path, payload, *, description: str) -> None:
        if target.name == "candidate_result.manifest.json":
            raise ContractError("simulated manifest publication failure")
        original_writer(target, payload, description=description)

    monkeypatch.setattr(
        release_contract_module,
        "_write_json_exclusive",
        fail_manifest,
    )

    with pytest.raises(ContractError, match="manifest publication failure"):
        write_candidate_result(result, path)

    assert not path.exists()
    assert not (tmp_path / "candidate_result.manifest.json").exists()


def test_candidate_result_content_tamper_is_rejected_before_read(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    path = write_candidate_result_fixture(tmp_path, manifest, runtime)
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(ContractError, match="artifact SHA-256 mismatch"):
        load_candidate_result(path, manifest)


def test_artifact_manifest_hash_and_identity_tamper_are_rejected(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    path = write_candidate_result_fixture(tmp_path, manifest, runtime)
    artifact_manifest = tmp_path / "candidate_result.manifest.json"
    sealed = json.loads(artifact_manifest.read_text(encoding="utf-8"))
    sealed["target_sha256"] = "f" * 64
    artifact_manifest.write_text(
        json.dumps(sealed, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="artifact SHA-256 mismatch"):
        load_candidate_result(path, manifest)

    sealed["target_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    sealed["git_tree"] = OLD_GIT_COMMIT
    artifact_manifest.write_text(
        json.dumps(sealed, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="manifest git_tree mismatch"):
        load_candidate_result(path, manifest)


def test_formal_container_evidence_is_independently_sealed_by_manifest(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    path = write_candidate_result_fixture(tmp_path, manifest, runtime)
    artifact_manifest_path = tmp_path / "candidate_result.manifest.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    sealed = json.loads(artifact_manifest_path.read_text(encoding="utf-8"))

    result["formal_containers"]["after"]["captured_at"] = (
        "2026-07-17T08:00:03Z"
    )
    path.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    sealed["target_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    sealed["target_size_bytes"] = path.stat().st_size
    artifact_manifest_path.write_text(
        json.dumps(sealed, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(ContractError, match="formal evidence SHA-256 mismatch"):
        load_candidate_result(path, manifest)


def test_candidate_runtime_git_tamper_in_json_or_manifest_is_rejected(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    path = write_candidate_result_fixture(tmp_path, manifest, runtime)
    artifact_manifest_path = tmp_path / "candidate_result.manifest.json"

    result = json.loads(path.read_text(encoding="utf-8"))
    result["identity"]["runtime_git_commit"] = OLD_GIT_COMMIT
    path.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ContractError, match="artifact SHA-256 mismatch"):
        load_candidate_result(path, manifest)

    result["identity"]["runtime_git_commit"] = GIT_COMMIT
    path.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    artifact_manifest = json.loads(
        artifact_manifest_path.read_text(encoding="utf-8")
    )
    artifact_manifest["target_sha256"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    artifact_manifest["target_size_bytes"] = path.stat().st_size
    artifact_manifest["runtime_git_commit"] = OLD_GIT_COMMIT
    artifact_manifest_path.write_text(
        json.dumps(artifact_manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="runtime_git_commit mismatch"):
        load_candidate_result(path, manifest)


def test_tool_repository_and_formal_project_are_separate_and_formal_head_stays_old(
    tmp_path: Path,
) -> None:
    def git(cwd: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout.strip()

    def initialize_repository(path: Path) -> tuple[str, str]:
        git(path, "init")
        git(path, "config", "user.name", "Release Contract Test")
        git(path, "config", "user.email", "release-contract@example.invalid")
        git(path, "add", "--all")
        git(path, "commit", "-m", "test fixture")
        return git(path, "rev-parse", "HEAD"), git(path, "rev-parse", "HEAD^{tree}")

    tool_repo_root = (tmp_path / "tool-repository").resolve()
    tool_repo_root.mkdir()
    for relative_path in ("docker-compose.yml", "Dockerfile", ".dockerignore"):
        shutil.copy2(REPOSITORY / relative_path, tool_repo_root / relative_path)
    config = tool_repo_root / "02_configs" / "historical_spread_config.xlsx"
    config.parent.mkdir(parents=True)
    shutil.copy2(
        REPOSITORY / "02_configs" / "historical_spread_config.xlsx",
        config,
    )
    shutil.copytree(
        CONTRACT_DIR,
        tool_repo_root / "09_deploy" / "spread_release",
    )
    tool_head, tool_tree = initialize_repository(tool_repo_root)

    production_project_dir = (tmp_path / "production-project").resolve()
    production_project_dir.mkdir()
    production_compose_file = production_project_dir / "docker-compose.yml"
    shutil.copy2(REPOSITORY / "docker-compose.yml", production_compose_file)
    production_head, production_tree = initialize_repository(
        production_project_dir
    )
    for name in ("01_data", "06_outputs", "10_logs"):
        (production_project_dir / name).mkdir()

    assert tool_head != production_head
    assert tool_tree != production_tree

    release_id = f"spread-20260717-{tool_head[:12]}-b01"
    image_ref = f"market-data-spread-dashboard:{release_id}"
    runtime = FakeReleaseRuntime(
        git_commit=tool_head,
        git_tree=tool_tree,
        release_id=release_id,
        image_ref=image_ref,
        repository_root=tool_repo_root,
    )
    runtime.production_record["runtime_git_commit"] = production_head
    data_root = tmp_path / "host-data"
    create_data_files(data_root)
    recording_git = RecordingRealGitRunner()
    manifest = create_manifest(
        repository=tool_repo_root,
        data_host_root=data_root,
        release_id=release_id,
        git_commit=tool_head,
        image_ref=image_ref,
        expected_image_id=IMAGE_ID,
        build_time=BUILD_TIME,
        source=SOURCE,
        candidate_container_name=CANDIDATE_CONTAINER,
        rollback_image_ref=ROLLBACK_REF,
        rollback_image_id=ROLLBACK_ID,
        formal_git_commit=production_head,
        production_environment=production_environment_for(
            image_ref=image_ref, git_commit=tool_head
        ),
        runtime=runtime,
        git_runner=recording_git,
    )
    production_env_file, production_environment = create_production_env(
        tmp_path,
        manifest,
    )
    candidate_result_file = write_candidate_result_fixture(
        tmp_path,
        manifest,
        runtime,
    )
    plan = create_deployment_plan(
        tool_repo_root=tool_repo_root,
        production_compose_file=production_compose_file,
        production_project_dir=production_project_dir,
        candidate_result_file=candidate_result_file,
        production_env_file=production_env_file,
        manifest=manifest,
        runtime=runtime,
        schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
        created_at=BUILD_TIME,
        deployment_tool_git_runner=recording_git,
    )

    evidence = verify_pre_deploy(
        manifest,
        tool_repo_root,
        runtime,
        git_runner=recording_git,
        deployment_plan=plan,
        production_environment=production_environment,
    )

    assert Path(plan["tool_repo_root"]) == tool_repo_root
    assert Path(plan["production_project_dir"]) == production_project_dir
    assert Path(plan["production_project_dir"]) != tool_repo_root
    assert Path(plan["production_compose_file"]).parent == Path(
        plan["production_project_dir"]
    )
    assert git(tool_repo_root, "rev-parse", "HEAD") == tool_head
    assert git(tool_repo_root, "rev-parse", "HEAD^{tree}") == tool_tree
    assert git(production_project_dir, "rev-parse", "HEAD") == production_head
    assert git(production_project_dir, "rev-parse", "HEAD^{tree}") == production_tree
    assert git(production_project_dir, "status", "--porcelain") == ""
    assert evidence["tool_repo_root"] == str(tool_repo_root)
    assert all(cwd == tool_repo_root for _, cwd in recording_git.commands)
    forbidden_git_writes = {"checkout", "reset", "clean", "switch", "restore"}
    assert all(
        not forbidden_git_writes.intersection(command)
        for command, _ in recording_git.commands
    )


def test_deployment_plan_path_tamper_is_rejected_by_manifest(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    path = tmp_path / "deployment_plan.json"
    write_deployment_plan(plan, path)
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["production_compose_file"] = str(
        (tmp_path / "attacker" / "docker-compose.yml").resolve()
    )
    path.write_text(
        json.dumps(tampered, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="artifact SHA-256 mismatch"):
        load_deployment_plan(
            path,
            manifest,
            CONTRACT_DIR / "deployment_plan.schema.json",
        )


def test_git_tree_and_image_identity_must_propagate_to_plan_and_result(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, production_environment = build_deployment_plan(
        tmp_path,
        manifest,
        runtime,
    )
    assert plan["git_tree"] == GIT_TREE
    assert plan["candidate_image_id"] == IMAGE_ID

    altered_plan = copy.deepcopy(plan)
    altered_plan["git_tree"] = OLD_GIT_COMMIT
    with pytest.raises(ContractError, match="deployment plan git_tree mismatch"):
        validate_deployment_plan(
            altered_plan,
            manifest,
            load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
            production_environment=production_environment,
        )

    bad_result = build_deployment_result(tmp_path, manifest, plan)
    bad_result["git_tree"] = OLD_GIT_COMMIT
    result_path = tmp_path / "deployment_result.json"
    write_result(result_path, bad_result)
    with pytest.raises(ContractError, match="manifest git_tree mismatch"):
        load_deployment_result(result_path, manifest, plan)


def test_candidate_inherits_production_user_urls_in_same_image_plan(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)

    plan, _, production_environment = build_deployment_plan(
        tmp_path,
        manifest,
        runtime,
    )

    assert plan["candidate_runtime_environment"] == {
        "USDA_DASHBOARD_URL": production_environment["USDA_DASHBOARD_URL"],
        "OIL_WORLD_DASHBOARD_URL": production_environment["OIL_WORLD_DASHBOARD_URL"],
        "WEATHER_DATA_DIR": CANDIDATE_WEATHER_DATA_DIR,
    }
    assert plan["image_ref"] == IMAGE_REF
    assert plan["expected_image_id"] == IMAGE_ID
    assert plan["allowed_candidate_production_differences"] == ["WEATHER_DATA_DIR"]
    assert plan["semantic_comparison"]["base_semantics_equal"] is True
    assert plan["readiness_policy"] == manifest["readiness_policy"]
    assert plan["plan_status"] == "deployment_plan_sealed"


def test_deployment_plan_rejects_readiness_policy_drift(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, production_environment = build_deployment_plan(
        tmp_path,
        manifest,
        runtime,
    )
    plan["readiness_policy"]["poll_interval_seconds"] = 3

    with pytest.raises(ContractError, match="readiness_policy"):
        validate_deployment_plan(
            plan,
            manifest,
            load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
            production_environment=production_environment,
        )


@pytest.mark.parametrize(
    "missing_key",
    [
        "USDA_DASHBOARD_URL",
        "OIL_WORLD_DASHBOARD_URL",
        "WEATHER_RUNTIME_CURRENT_DIR",
    ],
)
def test_production_environment_missing_required_key_hard_fails(
    tmp_path: Path,
    missing_key: str,
) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    path, environment = create_production_env(tmp_path, manifest)
    environment.pop(missing_key)
    path.write_text(
        "".join(
            f"{key}={environment[key]}\n"
            for key in PRODUCTION_ENV_KEYS
            if key in environment
        ),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="must contain exactly"):
        parse_production_env(path)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "empty or unsafe"),
        ("weather/processed/current", "absolute POSIX path"),
        ("/home/ubuntu/weather/processed/current", "fixed production runtime path"),
        ("/home/ubuntu/weather\nvalue", "not a KEY=value entry"),
    ],
)
def test_production_weather_runtime_dir_is_strictly_validated(
    tmp_path: Path, value: str, message: str
) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    path, environment = create_production_env(tmp_path, manifest)
    environment["WEATHER_RUNTIME_CURRENT_DIR"] = value
    path.write_text(
        "".join(f"{key}={environment[key]}\n" for key in PRODUCTION_ENV_KEYS),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match=message):
        parse_production_env(path)


def test_production_environment_rejects_unknown_key(tmp_path: Path) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    path, environment = create_production_env(tmp_path, manifest)
    path.write_text(
        "".join(f"{key}={environment[key]}\n" for key in PRODUCTION_ENV_KEYS)
        + "UNAPPROVED_VALUE=1\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="invalid or duplicate key"):
        parse_production_env(path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_mount", "exactly one weather runtime mount"),
        ("read_write_mount", "exact read-only bind mount"),
        ("wrong_target", "exactly one weather runtime mount"),
        ("raw_sql", "raw SQL files must not be mounted"),
        ("missing_weather_data_dir", "WEATHER_DATA_DIR"),
        ("wrong_weather_data_dir", "WEATHER_DATA_DIR"),
        ("usda_mount", "only spread-dashboard may mount"),
    ],
)
def test_compose_weather_runtime_contract_rejects_invalid_shapes(
    mutation: str, message: str
) -> None:
    runtime = FakeReleaseRuntime()
    compose, raw, images = runtime.compose_config(
        REPOSITORY,
        IMAGE_REF,
        environment=candidate_compose_environment(
            GIT_COMMIT, production_environment_for()
        ),
    )
    spread = compose["services"]["spread-dashboard"]
    if mutation == "missing_mount":
        spread["volumes"] = spread["volumes"][:-1]
    elif mutation == "read_write_mount":
        spread["volumes"][-1]["read_only"] = False
    elif mutation == "wrong_target":
        spread["volumes"][-1]["target"] = "/app/runtime/not-weather"
    elif mutation == "raw_sql":
        spread["volumes"][-1]["source"] = "/safe/weather_latest.sql"
    elif mutation == "missing_weather_data_dir":
        del spread["environment"]["WEATHER_DATA_DIR"]
    elif mutation == "wrong_weather_data_dir":
        spread["environment"]["WEATHER_DATA_DIR"] = "/tmp/weather"
    elif mutation == "usda_mount":
        compose["services"]["usda-dashboard"]["volumes"] = [
            {
                "type": "bind",
                "source": CANDIDATE_WEATHER_RUNTIME_DIR,
                "target": "/app/runtime/weather",
                "read_only": True,
            }
        ]
    else:
        raise AssertionError(mutation)

    with pytest.raises(ContractError, match=message):
        release_contract_module.validate_compose_result(compose, raw, images, IMAGE_REF)


def test_weather_runtime_contract_is_recorded_in_all_sealed_artifacts(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    assert manifest["weather_candidate_mode"] == "next"
    assert manifest["weather_candidate_source"] == CANDIDATE_WEATHER_RUNTIME_DIR
    assert manifest["weather_data_promotion_required"] is True
    assert manifest["weather_data_changed"] is True
    candidate = build_candidate_result_fixture(manifest, runtime)
    candidate_path = write_candidate_result_fixture(tmp_path, manifest, runtime)
    plan_root = tmp_path / "plan"
    plan_root.mkdir()
    plan, _, _ = build_deployment_plan(plan_root, manifest, runtime)
    result = build_deployment_result(tmp_path, manifest, plan)

    assert manifest["runtime_environment_contract"]["weather_runtime_mount"][
        "production_host_path"
    ] == PRODUCTION_WEATHER_RUNTIME_DIR
    assert candidate["weather_runtime_contract"]["host_path"] == CANDIDATE_WEATHER_RUNTIME_DIR
    assert load_candidate_result(candidate_path, manifest)["weather_runtime_contract"] == candidate[
        "weather_runtime_contract"
    ]
    assert plan["weather_runtime_contract"]["production_host_path"] == PRODUCTION_WEATHER_RUNTIME_DIR
    assert plan["weather_candidate_mode"] == "next"
    assert plan["weather_data_promotion_required"] is True
    assert result["weather_runtime_contract"] == plan["weather_runtime_contract"]


def test_code_only_weather_candidate_uses_current_without_data_promotion(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(
        tmp_path,
        weather_candidate_mode="current",
    )
    assert manifest["weather_candidate_mode"] == "current"
    assert manifest["weather_candidate_source"] == PRODUCTION_WEATHER_RUNTIME_DIR
    assert manifest["weather_data_promotion_required"] is False
    assert manifest["weather_data_changed"] is False
    assert manifest["processed_next_created"] is False
    assert manifest["processed_current_modified"] is False

    environment = candidate_compose_environment(
        GIT_COMMIT,
        production_environment_for(),
        "current",
    )
    assert environment["WEATHER_RUNTIME_CURRENT_DIR"] == PRODUCTION_WEATHER_RUNTIME_DIR

    candidate = build_candidate_result_fixture(manifest, runtime)
    assert candidate["weather_runtime_contract"]["host_path"] == PRODUCTION_WEATHER_RUNTIME_DIR
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    assert plan["weather_candidate_mode"] == "current"
    assert plan["weather_data_promotion_required"] is False
    assert plan["weather_runtime_contract"]["production_host_path"] == (
        PRODUCTION_WEATHER_RUNTIME_DIR
    )


@pytest.mark.parametrize(
    "mode",
    ("", "previous", "/tmp/weather", "CURRENT"),
)
def test_weather_candidate_mode_rejects_anything_except_current_or_next(mode: str) -> None:
    with pytest.raises(ContractError, match="weather_candidate_mode"):
        candidate_compose_environment(GIT_COMMIT, production_environment_for(), mode)


def test_explicit_production_urls_pass_validation(tmp_path: Path) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    path, expected = create_production_env(tmp_path, manifest, target=True)

    parsed = parse_production_env(path)
    validate_production_env(parsed, manifest)

    assert parsed == expected


def test_candidate_environment_inherits_validated_public_urls(tmp_path: Path) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    _, production_environment = create_production_env(tmp_path, manifest)

    candidate_environment = candidate_compose_environment(
        GIT_COMMIT, production_environment
    )

    assert candidate_environment["USDA_DASHBOARD_URL"] == PRODUCTION_USDA_URL
    assert candidate_environment["OIL_WORLD_DASHBOARD_URL"] == PRODUCTION_OIL_WORLD_URL
    assert "127.0.0.1:5175" not in candidate_environment.values()
    assert candidate_environment["MARKET_DATA_GIT_HEAD"] == GIT_COMMIT
    assert candidate_environment["WEATHER_RUNTIME_CURRENT_DIR"] == (
        CANDIDATE_WEATHER_RUNTIME_DIR
    )
    assert manifest["candidate_user_url_environment"] == {
        "USDA_DASHBOARD_URL": PRODUCTION_USDA_URL,
        "OIL_WORLD_DASHBOARD_URL": PRODUCTION_OIL_WORLD_URL,
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8081/oil-world/",
        "http://127.0.0.1:8081/oil-world/",
        "http://0.0.0.0:8081/oil-world/",
    ],
)
def test_candidate_rejects_loopback_browser_urls(url: str) -> None:
    environment = production_environment_for(oil_world_url=url)

    with pytest.raises(ContractError, match="must not use|local address"):
        candidate_compose_environment(GIT_COMMIT, environment)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("SPREAD_IMAGE", ROLLBACK_REF, "SPREAD_IMAGE"),
        ("MARKET_DATA_GIT_HEAD", OLD_GIT_COMMIT, "MARKET_DATA_GIT_HEAD"),
    ],
)
def test_production_release_bound_identity_mismatch_fails(
    tmp_path: Path,
    key: str,
    value: str,
    message: str,
) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    _, environment = create_production_env(tmp_path, manifest, target=True)
    environment[key] = value

    with pytest.raises(ContractError, match=message):
        validate_production_env(environment, manifest)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/usda/",
        "http://127.0.0.1:5175/",
        "http://0.0.0.0:8080/",
    ],
)
def test_production_url_rejects_local_defaults(tmp_path: Path, url: str) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    _, environment = create_production_env(tmp_path, manifest, target=True)
    environment["USDA_DASHBOARD_URL"] = url

    with pytest.raises(ContractError, match="must not use"):
        validate_production_env(environment, manifest)


def test_deployment_plan_rejects_compose_template_identity_change(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    production_env_file, _ = create_production_env(tmp_path, manifest)
    candidate_result_file = write_candidate_result_fixture(
        tmp_path, manifest, runtime
    )
    production_project_dir, production_compose_file = create_production_project(
        tmp_path
    )
    manifest["compose_template_sha256"] = "f" * 64

    with pytest.raises(ContractError, match="Compose file SHA-256"):
        create_deployment_plan(
            tool_repo_root=REPOSITORY,
            production_compose_file=production_compose_file,
            production_project_dir=production_project_dir,
            candidate_result_file=candidate_result_file,
            production_env_file=production_env_file,
            manifest=manifest,
            runtime=runtime,
            schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
            deployment_tool_git_runner=FakeGitRunner(
                head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE
            ),
        )


def test_deployment_plan_rejects_tool_and_production_directory_alias(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    production_env_file, _ = create_production_env(tmp_path, manifest)
    candidate_result_file = write_candidate_result_fixture(
        tmp_path,
        manifest,
        runtime,
    )

    with pytest.raises(ContractError, match="must be different directories"):
        create_deployment_plan(
            tool_repo_root=REPOSITORY,
            production_compose_file=REPOSITORY / "docker-compose.yml",
            production_project_dir=REPOSITORY,
            candidate_result_file=candidate_result_file,
            production_env_file=production_env_file,
            manifest=manifest,
            runtime=runtime,
            schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
            deployment_tool_git_runner=FakeGitRunner(
                head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE
            ),
        )


def test_deployment_plan_rejects_mount_difference(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    production_env_file, _ = create_production_env(tmp_path, manifest)
    candidate_result_file = write_candidate_result_fixture(
        tmp_path, manifest, runtime
    )
    production_project_dir, production_compose_file = create_production_project(
        tmp_path
    )
    runtime.production_mounts_override = [
        {
            "type": "bind",
            "source": str((REPOSITORY / "wrong-data").resolve()),
            "target": "/app/01_data",
        },
        {
            "type": "bind",
            "source": PRODUCTION_WEATHER_RUNTIME_DIR,
            "target": "/app/runtime/weather",
            "read_only": True,
        },
    ]

    with pytest.raises(ContractError, match="mount contract changed"):
        create_deployment_plan(
            tool_repo_root=REPOSITORY,
            production_compose_file=production_compose_file,
            production_project_dir=production_project_dir,
            candidate_result_file=candidate_result_file,
            production_env_file=production_env_file,
            manifest=manifest,
            runtime=runtime,
            schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
            deployment_tool_git_runner=FakeGitRunner(
                head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE
            ),
        )


def test_deployment_plan_rejects_command_difference(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    production_env_file, _ = create_production_env(tmp_path, manifest)
    candidate_result_file = write_candidate_result_fixture(
        tmp_path, manifest, runtime
    )
    production_project_dir, production_compose_file = create_production_project(
        tmp_path
    )
    runtime.production_command_override = ["python", "-m", "unexpected"]

    with pytest.raises(ContractError, match="outside the runtime URL allowlist"):
        create_deployment_plan(
            tool_repo_root=REPOSITORY,
            production_compose_file=production_compose_file,
            production_project_dir=production_project_dir,
            candidate_result_file=candidate_result_file,
            production_env_file=production_env_file,
            manifest=manifest,
            runtime=runtime,
            schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
            deployment_tool_git_runner=FakeGitRunner(
                head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE
            ),
        )


def test_deployment_plan_rejects_unexpected_production_port(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    production_env_file, _ = create_production_env(tmp_path, manifest)
    candidate_result_file = write_candidate_result_fixture(
        tmp_path, manifest, runtime
    )
    production_project_dir, production_compose_file = create_production_project(
        tmp_path
    )
    runtime.production_ports_override = [
        {
            "mode": "ingress",
            "protocol": "tcp",
            "published": "9501",
            "target": 8501,
        }
    ]

    with pytest.raises(ContractError, match="port mapping changed"):
        create_deployment_plan(
            tool_repo_root=REPOSITORY,
            production_compose_file=production_compose_file,
            production_project_dir=production_project_dir,
            candidate_result_file=candidate_result_file,
            production_env_file=production_env_file,
            manifest=manifest,
            runtime=runtime,
            schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
            deployment_tool_git_runner=FakeGitRunner(
                head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE
            ),
        )


def test_deployment_plan_is_write_once_and_loads_with_environment(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, expected_environment = build_deployment_plan(
        tmp_path,
        manifest,
        runtime,
    )
    plan_path = tmp_path / "deployment_plan.json"

    write_deployment_plan(plan, plan_path)
    loaded, environment = load_deployment_plan(
        plan_path,
        manifest,
        CONTRACT_DIR / "deployment_plan.schema.json",
    )

    assert loaded == plan
    assert environment == expected_environment
    with pytest.raises(ContractError, match="will not be overwritten"):
        write_deployment_plan(plan, plan_path)


def test_deployment_plan_seals_distinct_current_and_target_read_only(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    before_container = copy.deepcopy(runtime.production_record)
    before_side_services = copy.deepcopy(runtime.formal_records)
    plan, production_env_file, current_environment = build_deployment_plan(
        tmp_path, manifest, runtime
    )

    assert plan["current_production"]["environment"] == current_environment
    assert plan["current_production"]["image_ref"] == ROLLBACK_REF
    assert plan["target_release"]["image_ref"] == IMAGE_REF
    assert plan["current_production"]["image_ref"] != plan["target_release"][
        "image_ref"
    ]
    assert plan["deployment_tool_revision"] == {
        "git_commit": TOOL_GIT_COMMIT,
        "git_tree": TOOL_GIT_TREE,
    }
    assert plan["git_commit"] == GIT_COMMIT
    assert parse_production_env(production_env_file) == current_environment
    assert runtime.production_record == before_container
    assert runtime.formal_records == before_side_services


def test_deployment_plan_returns_clear_already_deployed_no_op(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, production_env_file, _ = build_deployment_plan(tmp_path, manifest, runtime)
    target_environment = apply_target_transition(plan, runtime)
    candidate_result_file = tmp_path / "candidate_result.json"
    production_compose_file = Path(plan["production_compose_file"])
    production_project_dir = Path(plan["production_project_dir"])

    with pytest.raises(ContractError, match="already-deployed/no-op"):
        create_deployment_plan(
            tool_repo_root=REPOSITORY,
            production_compose_file=production_compose_file,
            production_project_dir=production_project_dir,
            candidate_result_file=candidate_result_file,
            production_env_file=production_env_file,
            manifest=manifest,
            runtime=runtime,
            schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
            deployment_tool_git_runner=FakeGitRunner(
                head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE
            ),
        )
    assert parse_production_env(production_env_file) == target_environment


def test_existing_homepage_candidate_identity_is_a_legal_target(
    tmp_path: Path,
) -> None:
    release_id = "spread-20260808-feb1596f89da-b01"
    git_commit = "feb1596f89da09655ea89e068b87136ebc39c09f"
    git_tree = "df9d92d4a63a522a7aaf69c55f0a7dc606817244"
    image_ref = f"market-data-spread-dashboard:{release_id}"
    image_id = (
        "sha256:33ae88c2c5aa5fa5bf03aa061bf53fa0981a067dfce7ba72628f53ffc489e752"
    )
    build_time = "2026-08-08T08:00:00Z"
    data_root = tmp_path / "existing-candidate-data"
    create_data_files(data_root)
    runtime = FakeReleaseRuntime(
        git_commit=git_commit,
        git_tree=git_tree,
        release_id=release_id,
        image_ref=image_ref,
    )
    runtime.release_image_id = image_id
    runtime.candidate_record["image_id"] = image_id
    runtime.labels["org.opencontainers.image.created"] = build_time
    for release_payload in (
        runtime.release_json,
        runtime.image_release_json,
        runtime.production_release_json,
    ):
        release_payload["build_time"] = build_time
    manifest = create_manifest(
        repository=REPOSITORY,
        data_host_root=data_root,
        release_id=release_id,
        git_commit=git_commit,
        image_ref=image_ref,
        expected_image_id=image_id,
        build_time=build_time,
        source=SOURCE,
        candidate_container_name=CANDIDATE_CONTAINER,
        rollback_image_ref=ROLLBACK_REF,
        rollback_image_id=ROLLBACK_ID,
        formal_git_commit=OLD_GIT_COMMIT,
        production_environment=production_environment_for(
            image_ref=image_ref, git_commit=git_commit
        ),
        runtime=runtime,
        git_runner=FakeGitRunner(head=git_commit, tree=git_tree),
    )

    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)

    assert {
        key: plan["target_release"][key]
        for key in (
            "release_id",
            "image_ref",
            "image_id",
            "git_commit",
            "git_tree",
            "oci_revision",
        )
    } == {
        "release_id": release_id,
        "image_ref": image_ref,
        "image_id": image_id,
        "git_commit": git_commit,
        "git_tree": git_tree,
        "oci_revision": git_commit,
    }


def test_pre_deploy_rejects_env_or_container_drift_after_plan_sealing(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, production_env_file, current_environment = build_deployment_plan(
        tmp_path, manifest, runtime
    )
    production_env_file.write_bytes(
        release_contract_module.render_production_env(
            plan["target_release"]["environment"]
        ).encode("utf-8")
    )
    with pytest.raises(ContractError, match="environment drifted"):
        verify_pre_deploy(
            manifest,
            REPOSITORY,
            runtime,
            deployment_plan=plan,
            production_environment=parse_production_env(production_env_file),
            git_runner=FakeGitRunner(
                head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE
            ),
        )

    production_env_file.write_bytes(
        release_contract_module.render_production_env(current_environment).encode(
            "utf-8"
        )
    )
    runtime.production_record["image_id"] = IMAGE_ID
    with pytest.raises(ContractError, match="container image_id drifted"):
        verify_pre_deploy(
            manifest,
            REPOSITORY,
            runtime,
            deployment_plan=plan,
            production_environment=current_environment,
            git_runner=FakeGitRunner(
                head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE
            ),
        )


def test_execution_transition_is_atomic_idempotent_and_rollback_restores_a(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, production_env_file, current_environment = build_deployment_plan(
        tmp_path, manifest, runtime
    )
    original_mode = production_env_file.stat().st_mode
    side_services = copy.deepcopy(runtime.formal_records)

    applied = transition_production_env(plan, to_target=True)
    repeated = transition_production_env(plan, to_target=True)
    assert applied["status"] == "target-applied"
    assert repeated["status"] == "already-target"
    assert parse_production_env(production_env_file) == plan["target_release"][
        "environment"
    ]
    assert production_env_file.stat().st_mode == original_mode

    restored = transition_production_env(plan, to_target=False)
    assert restored["status"] == "rollback-restored"
    assert parse_production_env(production_env_file) == current_environment
    assert runtime.formal_records == side_services


def test_deployment_plan_requires_validated_candidate_result(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)

    with pytest.raises(ContractError, match="checks.pages.status"):
        write_candidate_result_fixture(
            tmp_path,
            manifest,
            runtime,
            page_status="failed",
        )


def test_deployment_plan_rejects_schema_valid_waiting_candidate(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    production_env_file, _ = create_production_env(tmp_path, manifest)
    candidate_result_file = write_candidate_result_fixture(
        tmp_path,
        manifest,
        runtime,
        blocking_gates=(
            pending_candidate_gate("real_night_session_close_snapshot", "2026-08-03"),
        ),
        runtime_mounts=(_import_profit_runtime_mount(),),
        candidate_runtime_access=_candidate_runtime_access(),
    )
    production_project_dir, production_compose_file = create_production_project(tmp_path)

    with pytest.raises(ContractError, match="not eligible"):
        create_deployment_plan(
            tool_repo_root=REPOSITORY,
            production_compose_file=production_compose_file,
            production_project_dir=production_project_dir,
            candidate_result_file=candidate_result_file,
            production_env_file=production_env_file,
            manifest=manifest,
            runtime=runtime,
            schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
            deployment_tool_git_runner=FakeGitRunner(
                head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE
            ),
        )


def test_deployment_plan_requires_candidate_container_removed(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    production_env_file, _ = create_production_env(tmp_path, manifest)
    candidate_result_file = write_candidate_result_fixture(
        tmp_path, manifest, runtime
    )
    production_project_dir, production_compose_file = create_production_project(
        tmp_path
    )
    runtime.candidate_container_exists = True

    with pytest.raises(ContractError, match="candidate container still exists"):
        create_deployment_plan(
            tool_repo_root=REPOSITORY,
            production_compose_file=production_compose_file,
            production_project_dir=production_project_dir,
            candidate_result_file=candidate_result_file,
            production_env_file=production_env_file,
            manifest=manifest,
            runtime=runtime,
            schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
            deployment_tool_git_runner=FakeGitRunner(
                head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE
            ),
        )


def test_deployment_plan_rejects_undeclared_service_scope(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, production_environment = build_deployment_plan(
        tmp_path,
        manifest,
        runtime,
    )
    plan["production_service_scope"] = [
        "spread-dashboard",
        "usda-dashboard",
    ]

    with pytest.raises(ContractError):
        validate_deployment_plan(
            plan,
            manifest,
            load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
            production_environment=production_environment,
        )


def test_non_offline_verifier_rejects_missing_deployment_plan(
    tmp_path: Path,
) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    release_directory = write_release_bundle(manifest, tmp_path / "releases")

    result = subprocess.run(
        [
            sys.executable,
            str(CONTRACT_DIR / "verify_release_contract.py"),
            "--phase",
            "pre-deploy",
            "--repository",
            str(REPOSITORY),
            "--manifest",
            str(release_directory / "release.json"),
        ],
        cwd=REPOSITORY,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "requires --deployment-plan" in result.stderr


def test_offline_verifier_accepts_a_valid_sealed_bundle(tmp_path: Path) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    release_directory = write_release_bundle(manifest, tmp_path / "releases")
    result = subprocess.run(
        [
            sys.executable,
            str(CONTRACT_DIR / "verify_release_contract.py"),
            "--phase",
            "offline",
            "--repository",
            str(REPOSITORY),
            "--manifest",
            str(release_directory / "release.json"),
        ],
        cwd=REPOSITORY,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["image_id"] == IMAGE_ID


def test_record_deployment_returns_zero_only_after_bundle_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    release_directory = write_release_bundle(manifest, tmp_path / "releases")
    plan_path = (tmp_path / "deployment_plan.json").resolve()
    write_deployment_plan(plan, plan_path)
    readiness_path = (tmp_path / "production_readiness.json").resolve()
    readiness_path.write_text(
        json.dumps(
            {
                "status": "ready",
                "container": PRODUCTION_CONTAINER,
                "expected_image_id": IMAGE_ID,
                "policy": plan["readiness_policy"],
                "ready_at_utc": BUILD_TIME,
                "last_http_result": {"http_status": 200},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        verifier_module,
        "verify_post_deploy",
        lambda *_args, **_kwargs: {
            "actual_image_id": IMAGE_ID,
            "oci_revision": GIT_COMMIT,
            "runtime_git_commit": GIT_COMMIT,
            "runtime_git_commit_verified": True,
            "container_name": PRODUCTION_CONTAINER,
            "config_image": IMAGE_REF,
            "formal_containers": compare_formal_container_snapshots(
                plan["formal_containers"]["after"],
                plan["formal_containers"]["after"],
                before_phase="pre-deploy",
                after_phase="post-deploy",
            ),
        },
    )
    result_path = release_directory / "deployment_result.json"
    argv = [
        "--phase",
        "record-deployment",
        "--repository",
        str(REPOSITORY),
        "--manifest",
        str(release_directory / "release.json"),
        "--env-file",
        str(release_directory / "release.env"),
        "--deployment-plan",
        str(plan_path),
        "--readiness-result",
        str(readiness_path),
        "--result-path",
        str(result_path),
    ]

    assert verifier_module.main(argv) == 0
    evidence = json.loads(capsys.readouterr().out)
    assert evidence["result_bundle"] == str(
        release_directory / DEPLOYMENT_RESULT_BUNDLE_FILENAME
    )
    assert (release_directory / DEPLOYMENT_RESULT_BUNDLE_FILENAME).is_file()


def test_record_deployment_returns_nonzero_when_bundle_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    release_directory = write_release_bundle(manifest, tmp_path / "releases")
    plan_path = (tmp_path / "deployment_plan.json").resolve()
    write_deployment_plan(plan, plan_path)
    readiness_path = (tmp_path / "production_readiness.json").resolve()
    readiness_path.write_text(
        json.dumps(
            {
                "status": "ready",
                "container": PRODUCTION_CONTAINER,
                "expected_image_id": IMAGE_ID,
                "policy": plan["readiness_policy"],
                "ready_at_utc": BUILD_TIME,
                "last_http_result": {"http_status": 200},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        verifier_module,
        "verify_post_deploy",
        lambda *_args, **_kwargs: {
            "actual_image_id": IMAGE_ID,
            "oci_revision": GIT_COMMIT,
            "runtime_git_commit": GIT_COMMIT,
            "runtime_git_commit_verified": True,
            "container_name": PRODUCTION_CONTAINER,
            "config_image": IMAGE_REF,
            "formal_containers": compare_formal_container_snapshots(
                plan["formal_containers"]["after"],
                plan["formal_containers"]["after"],
                before_phase="pre-deploy",
                after_phase="post-deploy",
            ),
        },
    )
    monkeypatch.setattr(
        verifier_module,
        "write_deployment_result_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ContractError("simulated bundle re-verification failure")
        ),
    )

    assert verifier_module.main(
        [
            "--phase",
            "record-deployment",
            "--repository",
            str(REPOSITORY),
            "--manifest",
            str(release_directory / "release.json"),
            "--env-file",
            str(release_directory / "release.env"),
            "--deployment-plan",
            str(plan_path),
            "--readiness-result",
            str(readiness_path),
        ]
    ) == 2
    assert not (release_directory / DEPLOYMENT_RESULT_BUNDLE_FILENAME).exists()


def test_generated_release_facts_do_not_make_git_worktree_dirty() -> None:
    ignored = subprocess.run(
        [
            "git",
            "check-ignore",
            "-q",
            "09_deploy/releases/spread-20260717-aaaaaaaaaaaa-b01/release.json",
        ],
        cwd=REPOSITORY,
        check=False,
    )
    policy = subprocess.run(
        ["git", "check-ignore", "-q", "09_deploy/releases/说明.md"],
        cwd=REPOSITORY,
        check=False,
    )

    assert ignored.returncode == 0
    assert policy.returncode != 0


def spread_spec():
    return next(spec for spec in DATA_SPECS if spec.name == "spread")


def inspect_spread_fixture(
    tmp_path: Path,
    rows: list[dict[str, object]],
) -> dict[str, object]:
    import pandas as pd

    parquet_path = tmp_path / "historical_spread_database.parquet"
    pd.DataFrame(rows).to_parquet(parquet_path, index=False)
    runtime = DockerReleaseRuntime(
        runner=LocalDatasetRunner(parquet_path)
    )
    return runtime.dataset_stats(CANDIDATE_CONTAINER, spread_spec())


def test_spread_primary_key_is_one_authoritative_real_schema_definition() -> None:
    assert SPREAD_PRIMARY_KEY == ("date", "spread_name")
    assert spread_spec().primary_key is SPREAD_PRIMARY_KEY
    assert not {
        "exchange",
        "product1",
        "contract1",
        "product2",
        "contract2",
    }.intersection(SPREAD_PRIMARY_KEY)


def test_spread_unique_date_and_name_passes(tmp_path: Path) -> None:
    stats = inspect_spread_fixture(
        tmp_path,
        [
            {"date": "2026-07-16", "spread_name": "M09-M01"},
            {"date": "2026-07-17", "spread_name": "Y09-Y01"},
        ],
    )

    assert stats["records"] == 2
    assert stats["latest_business_date"] == "2026-07-17"
    assert stats["primary_key_null_rows"] == 0
    assert stats["duplicate_rows_on_key"] == 0


def test_spread_same_date_different_name_passes(tmp_path: Path) -> None:
    stats = inspect_spread_fixture(
        tmp_path,
        [
            {"date": "2026-07-17", "spread_name": "M09-M01"},
            {"date": "2026-07-17", "spread_name": "Y09-Y01"},
        ],
    )

    assert stats["duplicate_rows_on_key"] == 0


def test_spread_same_name_different_date_passes(tmp_path: Path) -> None:
    stats = inspect_spread_fixture(
        tmp_path,
        [
            {"date": "2026-07-16", "spread_name": "M09-M01"},
            {"date": "2026-07-17", "spread_name": "M09-M01"},
        ],
    )

    assert stats["duplicate_rows_on_key"] == 0


def test_spread_duplicate_date_and_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="duplicate primary key rows"):
        inspect_spread_fixture(
            tmp_path,
            [
                {"date": "2026-07-17", "spread_name": "M09-M01"},
                {"date": "2026-07-17", "spread_name": "M09-M01"},
            ],
        )


@pytest.mark.parametrize(
    "rows",
    [
        [
            {"date": None, "spread_name": "M09-M01"},
            {"date": "2026-07-17", "spread_name": "Y09-Y01"},
        ],
        [
            {"date": "2026-07-16", "spread_name": None},
            {"date": "2026-07-17", "spread_name": "Y09-Y01"},
        ],
        [
            {"date": "2026-07-16", "spread_name": "   "},
            {"date": "2026-07-17", "spread_name": "Y09-Y01"},
        ],
    ],
    ids=["null-date", "null-spread-name", "blank-spread-name"],
)
def test_spread_primary_key_null_or_blank_is_rejected(
    tmp_path: Path,
    rows: list[dict[str, object]],
) -> None:
    with pytest.raises(ContractError, match="primary key contains null or blank"):
        inspect_spread_fixture(tmp_path, rows)


@pytest.mark.parametrize("missing_column", ["date", "spread_name"])
def test_spread_missing_required_primary_key_column_is_rejected(
    tmp_path: Path,
    missing_column: str,
) -> None:
    row = {"date": "2026-07-17", "spread_name": "M09-M01"}
    row.pop(missing_column)

    with pytest.raises(ContractError, match="missing columns"):
        inspect_spread_fixture(tmp_path, [row])


def test_spread_unparseable_date_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="date column contains invalid values"):
        inspect_spread_fixture(
            tmp_path,
            [{"date": "not-a-date", "spread_name": "M09-M01"}],
        )


def test_data_baseline_is_deployment_time_and_host_persistent(
    tmp_path: Path,
) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    baseline = manifest["data_baseline"]

    assert baseline["kind"] == "deployment-time"
    assert baseline["mutable_after_deployment"] is True
    assert {item["name"] for item in baseline["datasets"]} == {
        "spread",
        "basis",
        "soybean_progress",
        "soybean_condition",
    }
    for item in baseline["datasets"]:
        assert item["delivery"] == "bind_mount"
        assert item["persistence"] == "host"
        assert item["container_path"].startswith("/app/01_data/")
        assert item["writes_to"] == "host_through_bind_mount"
        assert len(item["sha256"]) == 64
        assert item["records"] == 10
        assert item["primary_key_null_rows"] == 0
        assert item["duplicate_rows_on_key"] == 0

    spread = next(item for item in baseline["datasets"] if item["name"] == "spread")
    spread_path = tmp_path / "host-data" / spread_spec().relative_host_path
    assert spread["primary_key"] == list(SPREAD_PRIMARY_KEY)
    assert spread["sha256"] == hashlib.sha256(spread_path.read_bytes()).hexdigest()


def test_sealed_data_baseline_is_not_retroactively_changed(
    tmp_path: Path,
) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    sealed = copy.deepcopy(manifest["data_baseline"])
    spread_path = tmp_path / "host-data" / spread_spec().relative_host_path

    spread_path.write_bytes(b"changed-after-seal")
    os.utime(spread_path, (spread_path.stat().st_atime, spread_path.stat().st_mtime + 60))

    assert manifest["data_baseline"] == sealed
    spread = next(
        item
        for item in manifest["data_baseline"]["datasets"]
        if item["name"] == "spread"
    )
    assert spread["sha256"] != hashlib.sha256(spread_path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("primary_key_null_rows", 1, "primary key contains null values"),
        ("duplicate_rows_on_key", 2, "contains duplicate primary keys"),
        ("invalid_date_rows", 1, "contains invalid business dates"),
    ],
)
def test_data_baseline_rejects_invalid_spread_key_statistics(
    tmp_path: Path,
    field: str,
    value: int,
    message: str,
) -> None:
    data_root = tmp_path / "host-data"
    create_data_files(data_root)
    runtime = FakeReleaseRuntime()
    runtime.dataset_overrides["spread"] = {
        "records": 10,
        "latest_business_date": "2026-07-12",
        "primary_key_null_rows": 0,
        "duplicate_rows_on_key": 0,
        "invalid_date_rows": 0,
        field: value,
    }

    with pytest.raises(ContractError, match=message):
        collect_data_baseline(data_root, PRODUCTION_CONTAINER, runtime)


def test_non_spread_baseline_records_nullable_business_key_fields(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "host-data"
    create_data_files(data_root)
    runtime = FakeReleaseRuntime()
    runtime.dataset_overrides["basis"] = {
        "records": 10,
        "latest_business_date": "2026-07-12",
        "primary_key_null_rows": 3,
        "duplicate_rows_on_key": 2,
        "invalid_date_rows": 0,
    }

    baseline = collect_data_baseline(data_root, PRODUCTION_CONTAINER, runtime)
    basis = next(item for item in baseline["datasets"] if item["name"] == "basis")

    assert basis["primary_key_null_rows"] == 3
    assert basis["duplicate_rows_on_key"] == 2


def test_data_baseline_rejects_a_missing_required_file(tmp_path: Path) -> None:
    runtime = FakeReleaseRuntime()

    with pytest.raises(ContractError, match="required file is missing"):
        collect_data_baseline(tmp_path, CANDIDATE_CONTAINER, runtime)


def test_manifest_creation_rejects_dirty_git(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    create_data_files(data_root)

    with pytest.raises(ContractError, match="checkout must be clean"):
        create_manifest(
            repository=REPOSITORY,
            data_host_root=data_root,
            release_id=RELEASE_ID,
            git_commit=GIT_COMMIT,
            image_ref=IMAGE_REF,
            expected_image_id=IMAGE_ID,
            build_time=BUILD_TIME,
            source=SOURCE,
            candidate_container_name=CANDIDATE_CONTAINER,
            rollback_image_ref=ROLLBACK_REF,
            rollback_image_id=ROLLBACK_ID,
            formal_git_commit=OLD_GIT_COMMIT,
            production_environment=production_environment_for(),
            runtime=FakeReleaseRuntime(),
            git_runner=FakeGitRunner(status=" M Dockerfile\n"),
        )


def test_manifest_creation_rejects_git_head_mismatch(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    create_data_files(data_root)

    with pytest.raises(ContractError, match="git HEAD mismatch"):
        create_manifest(
            repository=REPOSITORY,
            data_host_root=data_root,
            release_id=RELEASE_ID,
            git_commit=GIT_COMMIT,
            image_ref=IMAGE_REF,
            expected_image_id=IMAGE_ID,
            build_time=BUILD_TIME,
            source=SOURCE,
            candidate_container_name=CANDIDATE_CONTAINER,
            rollback_image_ref=ROLLBACK_REF,
            rollback_image_id=ROLLBACK_ID,
            formal_git_commit=OLD_GIT_COMMIT,
            production_environment=production_environment_for(),
            runtime=FakeReleaseRuntime(),
            git_runner=FakeGitRunner(head=OLD_GIT_COMMIT),
        )


def test_manifest_seals_before_candidate_container_exists(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    create_data_files(data_root)
    runtime = FakeReleaseRuntime()
    runtime.candidate_record["image_id"] = ROLLBACK_ID

    manifest = create_manifest(
        repository=REPOSITORY,
        data_host_root=data_root,
        release_id=RELEASE_ID,
        git_commit=GIT_COMMIT,
        image_ref=IMAGE_REF,
        expected_image_id=IMAGE_ID,
        build_time=BUILD_TIME,
        source=SOURCE,
        candidate_container_name=CANDIDATE_CONTAINER,
        rollback_image_ref=ROLLBACK_REF,
        rollback_image_id=ROLLBACK_ID,
        formal_git_commit=OLD_GIT_COMMIT,
        production_environment=production_environment_for(),
        runtime=runtime,
        git_runner=FakeGitRunner(),
    )

    assert manifest["status"] == "candidate_sealed"
    assert CANDIDATE_CONTAINER not in runtime.container_record_calls
    assert set(runtime.dataset_container_names) == {PRODUCTION_CONTAINER}


def test_pre_deploy_accepts_exact_image_oci_release_and_compose(
    tmp_path: Path,
) -> None:
    manifest, runtime, git = build_manifest(tmp_path)
    plan, _, production_environment = build_deployment_plan(
        tmp_path, manifest, runtime
    )

    evidence = verify_pre_deploy(
        manifest,
        REPOSITORY,
        runtime,
        git_runner=FakeGitRunner(head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE),
        deployment_plan=plan,
        production_environment=production_environment,
    )

    assert evidence["image_ref"] == IMAGE_REF
    assert evidence["image_id"] == IMAGE_ID
    assert evidence["oci_revision"] == GIT_COMMIT
    assert evidence["compose_image"] == IMAGE_REF


def test_pre_deploy_rejects_tag_to_image_id_mismatch(tmp_path: Path) -> None:
    manifest, runtime, git = build_manifest(tmp_path)
    plan, _, production_environment = build_deployment_plan(
        tmp_path, manifest, runtime
    )
    runtime.release_image_id = ROLLBACK_ID

    with pytest.raises(ContractError, match="image tag ID mismatch"):
        verify_pre_deploy(
            manifest,
            REPOSITORY,
            runtime,
            git_runner=FakeGitRunner(head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE),
            deployment_plan=plan,
            production_environment=production_environment,
        )


def test_pre_deploy_rejects_oci_revision_mismatch(tmp_path: Path) -> None:
    manifest, runtime, git = build_manifest(tmp_path)
    plan, _, production_environment = build_deployment_plan(
        tmp_path, manifest, runtime
    )
    runtime.labels["org.opencontainers.image.revision"] = OLD_GIT_COMMIT

    with pytest.raises(ContractError, match="image label.*revision mismatch"):
        verify_pre_deploy(
            manifest,
            REPOSITORY,
            runtime,
            git_runner=FakeGitRunner(head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE),
            deployment_plan=plan,
            production_environment=production_environment,
        )


def test_pre_deploy_rejects_image_release_json_mismatch(tmp_path: Path) -> None:
    manifest, runtime, git = build_manifest(tmp_path)
    plan, _, production_environment = build_deployment_plan(
        tmp_path, manifest, runtime
    )
    runtime.image_release_json["git_commit"] = OLD_GIT_COMMIT

    with pytest.raises(ContractError, match="/app/RELEASE.json"):
        verify_pre_deploy(
            manifest,
            REPOSITORY,
            runtime,
            git_runner=FakeGitRunner(head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE),
            deployment_plan=plan,
            production_environment=production_environment,
        )


def test_regression_compose_must_not_resolve_old_latest_image(
    tmp_path: Path,
) -> None:
    manifest, runtime, git = build_manifest(tmp_path)
    plan, _, production_environment = build_deployment_plan(
        tmp_path, manifest, runtime
    )
    assert runtime.image_record(IMAGE_REF)["id"] == IMAGE_ID
    assert runtime.image_record(LATEST_REF)["id"] == ROLLBACK_ID
    runtime.compose_image = LATEST_REF

    with pytest.raises(ContractError, match="Compose resolved spread image mismatch"):
        verify_pre_deploy(
            manifest,
            REPOSITORY,
            runtime,
            git_runner=FakeGitRunner(head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE),
            deployment_plan=plan,
            production_environment=production_environment,
        )


def test_candidate_and_production_must_use_same_image_id(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    candidate = verify_candidate(manifest, runtime)
    assert candidate["actual_image_id"] == IMAGE_ID
    assert candidate["tag_image_id"] == IMAGE_ID
    assert candidate["manifest_image_id"] == IMAGE_ID
    assert candidate["runtime_git_commit"] == GIT_COMMIT
    production_environment = apply_target_transition(plan, runtime)

    evidence = verify_post_deploy(
        manifest,
        runtime,
        deployment_plan=plan,
        production_environment=production_environment,
    )

    assert evidence["actual_image_id"] == IMAGE_ID
    assert evidence["config_image"] == IMAGE_REF
    assert evidence["runtime_git_commit"] == GIT_COMMIT
    assert evidence["runtime_git_commit_verified"] is True


@pytest.mark.parametrize(
    ("runtime_git_commit", "message"),
    [
        (None, "missing runtime MARKET_DATA_GIT_HEAD"),
        ("", "runtime MARKET_DATA_GIT_HEAD is empty"),
        (OLD_GIT_COMMIT, "does not match the release"),
    ],
)
def test_candidate_runtime_git_head_is_required_and_exact(
    tmp_path: Path,
    runtime_git_commit: str | None,
    message: str,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    if runtime_git_commit is None:
        runtime.candidate_record.pop("runtime_git_commit")
    else:
        runtime.candidate_record["runtime_git_commit"] = runtime_git_commit

    with pytest.raises(ContractError, match=message):
        verify_candidate(manifest, runtime)


@pytest.mark.parametrize(
    ("runtime_git_commit", "message"),
    [
        (None, "missing runtime MARKET_DATA_GIT_HEAD"),
        (OLD_GIT_COMMIT, "does not match the release"),
    ],
)
def test_production_runtime_git_head_is_required_and_exact(
    tmp_path: Path,
    runtime_git_commit: str | None,
    message: str,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    production_environment = apply_target_transition(plan, runtime)
    if runtime_git_commit is None:
        runtime.production_record.pop("runtime_git_commit")
    else:
        runtime.production_record["runtime_git_commit"] = runtime_git_commit

    with pytest.raises(ContractError, match=message):
        verify_post_deploy(
            manifest,
            runtime,
            deployment_plan=plan,
            production_environment=production_environment,
        )


def test_candidate_identity_rejects_container_image_id_mismatch(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    runtime.candidate_record["image_id"] = ROLLBACK_ID

    with pytest.raises(ContractError, match="candidate container Image ID mismatch"):
        verify_candidate(manifest, runtime)


def test_candidate_release_json_mismatch_rejects_before_readiness(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    runtime.release_json = {
        **runtime.release_json,
        "git_commit": OLD_GIT_COMMIT,
    }

    with pytest.raises(ContractError, match="/app/RELEASE.json"):
        verify_candidate(manifest, runtime)


def test_candidate_release_json_tree_mismatch_rejects_before_readiness(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    runtime.release_json = {
        **runtime.release_json,
        "git_tree": OLD_GIT_COMMIT,
    }

    with pytest.raises(ContractError, match="/app/RELEASE.json"):
        verify_candidate(manifest, runtime)


def test_post_deploy_rejects_actual_container_image_id_mismatch(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    production_environment = apply_target_transition(plan, runtime)
    runtime.production_record["image_id"] = ROLLBACK_ID

    with pytest.raises(ContractError, match="production container Image ID mismatch"):
        verify_post_deploy(
            manifest,
            runtime,
            deployment_plan=plan,
            production_environment=production_environment,
        )


def test_post_deploy_rejects_config_image_mismatch(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    production_environment = apply_target_transition(plan, runtime)
    runtime.production_record["config_image"] = ROLLBACK_REF

    with pytest.raises(ContractError, match="Config.Image mismatch"):
        verify_post_deploy(
            manifest,
            runtime,
            deployment_plan=plan,
            production_environment=production_environment,
        )


def test_post_deploy_rejects_container_release_json_mismatch(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    production_environment = apply_target_transition(plan, runtime)
    runtime.production_release_json["release_id"] = (
        "spread-20260717-aaaaaaaaaaaa-b02"
    )

    with pytest.raises(ContractError, match="/app/RELEASE.json"):
        verify_post_deploy(
            manifest,
            runtime,
            deployment_plan=plan,
            production_environment=production_environment,
        )


def test_post_deploy_rejects_container_release_json_tree_mismatch(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    production_environment = apply_target_transition(plan, runtime)
    runtime.production_release_json["git_tree"] = OLD_GIT_COMMIT

    with pytest.raises(ContractError, match="/app/RELEASE.json"):
        verify_post_deploy(
            manifest,
            runtime,
            deployment_plan=plan,
            production_environment=production_environment,
        )


def test_rollback_requires_explicit_tag_to_image_id_match(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, production_environment = build_deployment_plan(
        tmp_path, manifest, runtime
    )
    runtime.compose_image = ROLLBACK_REF

    evidence = verify_pre_rollback(
        manifest,
        REPOSITORY,
        runtime,
        deployment_plan=plan,
        production_environment=production_environment,
        git_runner=FakeGitRunner(head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE),
    )

    assert evidence["rollback_image_ref"] == ROLLBACK_REF
    assert evidence["rollback_image_id"] == ROLLBACK_ID
    assert evidence["compose_image"] == ROLLBACK_REF
    with pytest.raises(ContractError, match="deployment tool revision changed"):
        verify_pre_rollback(
            manifest,
            REPOSITORY,
            runtime,
            deployment_plan=plan,
            production_environment=production_environment,
            git_runner=FakeGitRunner(head=GIT_COMMIT, tree=GIT_TREE),
        )
    runtime.rollback_image_id = IMAGE_ID
    with pytest.raises(ContractError, match="rollback tag ID mismatch"):
        verify_pre_rollback(
            manifest,
            REPOSITORY,
            runtime,
            deployment_plan=plan,
            production_environment=production_environment,
            git_runner=FakeGitRunner(
                head=TOOL_GIT_COMMIT, tree=TOOL_GIT_TREE
            ),
        )


def test_post_rollback_verifies_actual_container_image_id(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, production_environment = build_deployment_plan(
        tmp_path, manifest, runtime
    )
    runtime.production_record = {
        "image_id": ROLLBACK_ID,
        "config_image": ROLLBACK_REF,
        "runtime_git_commit": OLD_GIT_COMMIT,
    }

    evidence = verify_post_rollback(
        manifest,
        runtime,
        deployment_plan=plan,
        production_environment=production_environment,
    )

    assert evidence["actual_image_id"] == ROLLBACK_ID
    runtime.production_record["image_id"] = IMAGE_ID
    with pytest.raises(ContractError, match="rolled-back container Image ID mismatch"):
        verify_post_rollback(
            manifest,
            runtime,
            deployment_plan=plan,
            production_environment=production_environment,
        )


def test_formal_scripts_never_build_retag_or_derive_from_config_image() -> None:
    deploy = (CONTRACT_DIR / "deploy_spread_release.sh").read_text(encoding="utf-8")
    rollback = (CONTRACT_DIR / "rollback_spread_release.sh").read_text(
        encoding="utf-8"
    )

    for script in (deploy, rollback):
        assert "--no-build" in script
        assert "--no-deps" in script
        assert "docker build" not in script
        assert "docker tag" not in script
        assert ".Config.Image" not in script
        assert ":latest" not in script
        assert ":new" not in script
        assert "--env-file" in script
        assert "--deployment-plan" in script
        assert (
            'up -d --no-build --pull never --no-deps --force-recreate '
            '"${production_service}"' in script
        )
        assert "transition_production_env.py" in script
        assert "up -d --no-build --no-deps usda-dashboard" not in script
        assert "up -d --no-build --no-deps oil-world" not in script
    assert 'production_env_file="${plan_identity[2]}"' in deploy
    assert 'production_compose_file="${plan_identity[3]}"' in deploy
    assert 'production_project_dir="${plan_identity[4]}"' in deploy
    assert '-f "${production_compose_file}"' in deploy
    assert '--project-directory "${production_project_dir}"' in deploy
    assert 'SPREAD_IMAGE="${rollback_image_ref}"' not in rollback
    assert 'manifest["rollback_image_ref"]' in rollback
    assert 'manifest["rollback_image_id"]' in rollback


def test_static_contract_rejects_usda_in_formal_switch_scope(
    tmp_path: Path,
) -> None:
    fixture_repository = tmp_path / "repository"
    required_paths = (
        "docker-compose.yml",
        "Dockerfile",
        ".dockerignore",
        "02_configs/historical_spread_config.xlsx",
        "09_deploy/spread_release/deploy_spread_release.sh",
        "09_deploy/spread_release/rollback_spread_release.sh",
            "09_deploy/spread_release/create_deployment_plan.py",
            "09_deploy/spread_release/transition_production_env.py",
        "09_deploy/spread_release/create_candidate_result.py",
        "09_deploy/spread_release/deployment_plan.schema.json",
        "09_deploy/spread_release/candidate_result.schema.json",
        "09_deploy/spread_release/artifact_manifest.schema.json",
        "09_deploy/spread_release/deployment_result.schema.json",
        "09_deploy/spread_release/deployment_result_bundle.schema.json",
    )
    for relative_path in required_paths:
        source = REPOSITORY / relative_path
        destination = fixture_repository / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    deploy_path = (
        fixture_repository
        / "09_deploy"
        / "spread_release"
        / "deploy_spread_release.sh"
    )
    deploy_path.write_text(
        deploy_path.read_text(encoding="utf-8")
        + "\ndocker compose up -d --no-build --no-deps usda-dashboard\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="must not switch USDA"):
        validate_repository_static(fixture_repository)


def test_operator_docs_do_not_offer_legacy_spread_build_switch() -> None:
    documents = (
        "07_docs/06_日常运行与数据更新手册.md",
        "09_deploy/SERVER_DAILY_UPDATE.md",
        "09_deploy/README_TENCENT_LIGHTHOUSE.md",
    )
    for relative_path in documents:
        content = (REPOSITORY / relative_path).read_text(encoding="utf-8")
        assert "docker compose build spread-dashboard" not in content
        assert "docker compose up -d --build" not in content
    handbook = (REPOSITORY / documents[0]).read_text(encoding="utf-8")
    assert "deploy_spread_release.sh" in handbook
    assert "rollback_spread_release.sh" in handbook


def test_deploy_checks_identity_before_http_and_auto_rolls_back() -> None:
    deploy = (CONTRACT_DIR / "deploy_spread_release.sh").read_text(encoding="utf-8")

    post_identity = deploy.index("--phase post-deploy")
    readiness_check = deploy.index('"${readiness_waiter}"')
    production_record = deploy.index("--phase record-deployment")
    assert post_identity < readiness_check < production_record
    assert "curl --fail" not in deploy
    assert "--policy-file \"${deployment_plan}\"" in deploy
    assert "--readiness-result \"${readiness_result}\"" in deploy
    assert "trap rollback_on_failure ERR" in deploy
    assert (
        'bash "${rollback_script}" "${release_directory}" '
        '"${deployment_plan}" "${health_url}"'
    ) in deploy
    assert deploy.count('bash "${rollback_script}"') == 1
    assert deploy.count("docker compose") == 1
    assert deploy.index("trap rollback_on_failure ERR") < deploy.index(
        "docker compose"
    )
    assert "DEPLOYMENT_RESULT_BUNDLE_FILENAME" in deploy
    assert production_record < deploy.index('if [[ ! -f "${deployment_result_bundle}" ]]')
    assert deploy.index('if [[ ! -f "${deployment_result_bundle}" ]]') < deploy.index(
        "deployment_succeeded=1"
    )
    assert "exit 66" not in deploy
    assert "    false\nfi" in deploy
    assert deploy.index("deployment_succeeded=1") < deploy.index(
        'echo "spread release ${RELEASE_ID} deployed'
    )


def test_candidate_deploy_and_rollback_share_one_readiness_tool() -> None:
    scripts = {
        name: (CONTRACT_DIR / name).read_text(encoding="utf-8")
        for name in (
            "validate_spread_candidate.sh",
            "deploy_spread_release.sh",
            "rollback_spread_release.sh",
        )
    }
    for content in scripts.values():
        assert 'readiness_waiter="${script_dir}/wait_for_service_ready.py"' in content
        assert '"${readiness_waiter}"' in content
        assert "curl --fail" not in content
        assert "--initial-restart-count" in content
        assert "--expected-image-id" in content
        assert "--policy-file" in content

    assert scripts["validate_spread_candidate.sh"].index("--phase candidate") < scripts[
        "validate_spread_candidate.sh"
    ].index('"${readiness_waiter}"')
    assert scripts["rollback_spread_release.sh"].index("--phase post-rollback") < scripts[
        "rollback_spread_release.sh"
    ].index('"${readiness_waiter}"')


def test_static_repository_contract_includes_required_config_and_sensitive_exclusions() -> None:
    validate_repository_static(REPOSITORY)
    dockerignore = (REPOSITORY / ".dockerignore").read_text(encoding="utf-8")
    dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")

    assert (REPOSITORY / "02_configs/historical_spread_config.xlsx").is_file()
    assert "!02_configs/historical_spread_config.xlsx" in dockerignore
    assert "**/.env" in dockerignore
    assert "**/*.key" in dockerignore
    assert "COPY 02_configs /app/02_configs" in dockerfile
    assert "COPY 05_apps /app/05_apps" in dockerfile
    assert "COPY 01_data " not in dockerfile
    assert "COPY 06_outputs " not in dockerfile
