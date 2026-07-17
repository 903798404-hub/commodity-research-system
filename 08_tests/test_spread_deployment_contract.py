from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pandas as pd
import pytest
import yaml


REPOSITORY = Path(__file__).resolve().parents[1]
CONTRACT_DIR = REPOSITORY / "09_deploy" / "spread_release"
sys.path.insert(0, str(CONTRACT_DIR))

from release_contract import (  # noqa: E402
    APPLICATION,
    CANDIDATE_RUNTIME_ENVIRONMENT,
    COMPOSE_PROJECT,
    ContractError,
    DATA_SPECS,
    DEFAULT_READINESS_POLICY,
    DockerReleaseRuntime,
    PRODUCTION_ENV_KEYS,
    PRODUCTION_CONTAINER,
    SPREAD_PRIMARY_KEY,
    collect_data_baseline,
    create_deployment_plan,
    create_manifest,
    load_deployment_plan,
    load_manifest_bundle,
    load_schema,
    parse_production_env,
    resolve_spread_image_offline,
    validate_deployment_plan,
    validate_full_git_commit,
    validate_manifest,
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
    write_deployment_plan,
    write_release_bundle,
    write_result,
)


GIT_COMMIT = "a" * 40
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


class FakeGitRunner:
    def __init__(self, *, head: str = GIT_COMMIT, status: str = "") -> None:
        self.head = head
        self.status = status

    def run(self, command, *, cwd=None, env=None) -> str:
        del cwd, env
        if command[:2] == ["git", "status"]:
            return self.status
        if command == ["git", "rev-parse", "HEAD"]:
            return self.head + "\n"
        raise AssertionError(f"unexpected git command: {command}")


class MultipleImageInspectRunner:
    def run(self, command, *, cwd=None, env=None) -> str:
        del cwd, env
        assert command[:3] == ["docker", "image", "inspect"]
        return json.dumps([{"Id": IMAGE_ID}, {"Id": ROLLBACK_ID}])


class FakeReleaseRuntime:
    def __init__(self) -> None:
        self.release_image_id = IMAGE_ID
        self.rollback_image_id = ROLLBACK_ID
        self.labels = {
            "org.opencontainers.image.revision": GIT_COMMIT,
            "org.opencontainers.image.version": RELEASE_ID,
            "org.opencontainers.image.created": BUILD_TIME,
            "org.opencontainers.image.source": SOURCE,
        }
        self.release_json = {
            "application": APPLICATION,
            "release_id": RELEASE_ID,
            "git_commit": GIT_COMMIT,
            "build_time": BUILD_TIME,
            "source": SOURCE,
        }
        self.image_release_json = dict(self.release_json)
        self.production_release_json = dict(self.release_json)
        self.candidate_record = {
            "image_id": IMAGE_ID,
            "config_image": IMAGE_REF,
        }
        self.production_record = {
            "image_id": IMAGE_ID,
            "config_image": IMAGE_REF,
        }
        self.compose_image = IMAGE_REF
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

    def image_record(self, image_ref: str) -> dict[str, object]:
        if image_ref == IMAGE_REF:
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
        assert image_ref == IMAGE_REF
        return dict(self.image_release_json)

    def image_release_sha256(self, image_ref: str) -> str:
        assert image_ref == IMAGE_REF
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
        assert repository == REPOSITORY
        assert image_ref in {IMAGE_REF, ROLLBACK_REF}
        project_root = (project_directory or repository).resolve()
        assert (compose_file or repository / "docker-compose.yml").resolve() == (
            repository / "docker-compose.yml"
        ).resolve()
        resolved_environment = {
            **CANDIDATE_RUNTIME_ENVIRONMENT,
            **(environment or {}),
        }
        is_candidate = all(
            resolved_environment[key] == CANDIDATE_RUNTIME_ENVIRONMENT[key]
            for key in CANDIDATE_RUNTIME_ENVIRONMENT
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
        compose = {
            "name": COMPOSE_PROJECT,
            "services": {
                "spread-dashboard": {
                    "image": self.compose_image,
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
                            "USDA_DASHBOARD_URL",
                            "OIL_WORLD_DASHBOARD_URL",
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
        images = [self.compose_image, "market-data-usda-dashboard"]
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
        runtime=runtime,
        git_runner=git,
    )
    return manifest, runtime, git


def create_production_env(
    tmp_path: Path,
    manifest: dict[str, object],
    *,
    usda_url: str = PRODUCTION_USDA_URL,
    oil_world_url: str = PRODUCTION_OIL_WORLD_URL,
) -> tuple[Path, dict[str, str]]:
    environment = {
        "SPREAD_IMAGE": str(manifest["image_ref"]),
        "MARKET_DATA_GIT_HEAD": str(manifest["git_commit"]),
        "USDA_DASHBOARD_URL": usda_url,
        "OIL_WORLD_DASHBOARD_URL": oil_world_url,
    }
    path = (tmp_path / "spread-production.env").resolve()
    path.write_text(
        "".join(f"{key}={environment[key]}\n" for key in PRODUCTION_ENV_KEYS),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path, environment


def create_candidate_result(
    tmp_path: Path,
    manifest: dict[str, object],
) -> Path:
    path = (tmp_path / "candidate_result.json").resolve()
    path.write_text(
        json.dumps(
            {
                "schema_version": "candidate-result-v1",
                "application": APPLICATION,
                "release_id": manifest["release_id"],
                "git_commit": manifest["git_commit"],
                "image_ref": manifest["image_ref"],
                "image_id": manifest["image_id"],
                "status": "candidate-validated",
                "three_way_image_id_equal": True,
                "formal_containers_unchanged": True,
                "formal_git_unchanged": True,
                "data_files_unchanged": True,
                "production_switch_performed": False,
                "identity": {
                    "config_image": manifest["image_ref"],
                    "actual_image_id": manifest["image_id"],
                    "tag_image_id": manifest["image_id"],
                    "manifest_image_id": manifest["image_id"],
                    "oci_revision": manifest["git_commit"],
                },
                "http": {
                    "health": 200,
                    "host_config": 200,
                    "root": 200,
                },
                "pages": {"status": "passed"},
                "readiness": {
                    "status": "ready",
                    "expected_image_id": manifest["image_id"],
                    "policy": manifest["readiness_policy"],
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def build_deployment_plan(
    tmp_path: Path,
    manifest: dict[str, object],
    runtime: FakeReleaseRuntime,
) -> tuple[dict[str, object], Path, dict[str, str]]:
    production_env_file, production_environment = create_production_env(
        tmp_path,
        manifest,
    )
    candidate_result_file = create_candidate_result(tmp_path, manifest)
    plan = create_deployment_plan(
        repository=REPOSITORY,
        production_project_directory=REPOSITORY,
        candidate_result_file=candidate_result_file,
        production_env_file=production_env_file,
        manifest=manifest,
        runtime=runtime,
        schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
        created_at=BUILD_TIME,
    )
    return plan, production_env_file, production_environment


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


@pytest.mark.skipif(
    not docker_compose_is_available(), reason="local Docker Compose is unavailable"
)
def test_real_compose_config_fails_without_spread_image() -> None:
    environment = os.environ.copy()
    environment.pop("SPREAD_IMAGE", None)
    environment["USDA_DASHBOARD_URL"] = PRODUCTION_USDA_URL
    environment["OIL_WORLD_DASHBOARD_URL"] = PRODUCTION_OIL_WORLD_URL
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
def test_real_compose_config_resolves_exact_spread_image() -> None:
    environment = {
        **os.environ,
        "SPREAD_IMAGE": IMAGE_REF,
        "USDA_DASHBOARD_URL": PRODUCTION_USDA_URL,
        "OIL_WORLD_DASHBOARD_URL": PRODUCTION_OIL_WORLD_URL,
    }
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
    manifest, _, _ = build_manifest(tmp_path)
    release_directory = write_release_bundle(manifest, tmp_path / "releases")
    manifest_path = release_directory / "release.json"
    original = manifest_path.read_bytes()
    result_path = release_directory / "deployment_result.json"

    write_result(
        result_path,
        {
            "phase": "deployment-result",
            "release_id": RELEASE_ID,
            "actual_image_id": IMAGE_ID,
            "status": "deployed-and-verified",
        },
    )

    assert result_path.is_file()
    assert manifest_path.read_bytes() == original
    with pytest.raises(ContractError, match="already exists"):
        write_result(result_path, {"status": "replacement"})


def test_candidate_and_production_url_difference_seals_same_image_plan(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)

    plan, _, production_environment = build_deployment_plan(
        tmp_path,
        manifest,
        runtime,
    )

    assert production_environment["USDA_DASHBOARD_URL"] != (
        CANDIDATE_RUNTIME_ENVIRONMENT["USDA_DASHBOARD_URL"]
    )
    assert production_environment["OIL_WORLD_DASHBOARD_URL"] != (
        CANDIDATE_RUNTIME_ENVIRONMENT["OIL_WORLD_DASHBOARD_URL"]
    )
    assert plan["image_ref"] == IMAGE_REF
    assert plan["expected_image_id"] == IMAGE_ID
    assert plan["candidate_compose_sha256"] != plan["production_compose_sha256"]
    assert plan["allowed_candidate_production_differences"] == [
        "USDA_DASHBOARD_URL",
        "OIL_WORLD_DASHBOARD_URL",
    ]
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
    ["USDA_DASHBOARD_URL", "OIL_WORLD_DASHBOARD_URL"],
)
def test_production_environment_missing_url_hard_fails(
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


def test_explicit_production_urls_pass_validation(tmp_path: Path) -> None:
    manifest, _, _ = build_manifest(tmp_path)
    path, expected = create_production_env(tmp_path, manifest)

    parsed = parse_production_env(path)
    validate_production_env(parsed, manifest)

    assert parsed == expected


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
    _, environment = create_production_env(tmp_path, manifest)
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
    _, environment = create_production_env(tmp_path, manifest)
    environment["USDA_DASHBOARD_URL"] = url

    with pytest.raises(ContractError, match="must not use"):
        validate_production_env(environment, manifest)


def test_deployment_plan_rejects_compose_template_identity_change(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    production_env_file, _ = create_production_env(tmp_path, manifest)
    candidate_result_file = create_candidate_result(tmp_path, manifest)
    manifest["compose_template_sha256"] = "f" * 64

    with pytest.raises(ContractError, match="template SHA-256"):
        create_deployment_plan(
            repository=REPOSITORY,
            production_project_directory=REPOSITORY,
            candidate_result_file=candidate_result_file,
            production_env_file=production_env_file,
            manifest=manifest,
            runtime=runtime,
            schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
        )


def test_deployment_plan_rejects_mount_difference(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    production_env_file, _ = create_production_env(tmp_path, manifest)
    candidate_result_file = create_candidate_result(tmp_path, manifest)
    runtime.production_mounts_override = [
        {
            "type": "bind",
            "source": str((REPOSITORY / "wrong-data").resolve()),
            "target": "/app/01_data",
        }
    ]

    with pytest.raises(ContractError, match="mount contract changed"):
        create_deployment_plan(
            repository=REPOSITORY,
            production_project_directory=REPOSITORY,
            candidate_result_file=candidate_result_file,
            production_env_file=production_env_file,
            manifest=manifest,
            runtime=runtime,
            schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
        )


def test_deployment_plan_rejects_command_difference(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    production_env_file, _ = create_production_env(tmp_path, manifest)
    candidate_result_file = create_candidate_result(tmp_path, manifest)
    runtime.production_command_override = ["python", "-m", "unexpected"]

    with pytest.raises(ContractError, match="outside the runtime URL allowlist"):
        create_deployment_plan(
            repository=REPOSITORY,
            production_project_directory=REPOSITORY,
            candidate_result_file=candidate_result_file,
            production_env_file=production_env_file,
            manifest=manifest,
            runtime=runtime,
            schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
        )


def test_deployment_plan_rejects_unexpected_production_port(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    production_env_file, _ = create_production_env(tmp_path, manifest)
    candidate_result_file = create_candidate_result(tmp_path, manifest)
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
            repository=REPOSITORY,
            production_project_directory=REPOSITORY,
            candidate_result_file=candidate_result_file,
            production_env_file=production_env_file,
            manifest=manifest,
            runtime=runtime,
            schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
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


def test_deployment_plan_requires_validated_candidate_result(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    production_env_file, _ = create_production_env(tmp_path, manifest)
    candidate_result_file = create_candidate_result(tmp_path, manifest)
    candidate_result = json.loads(
        candidate_result_file.read_text(encoding="utf-8")
    )
    candidate_result["pages"]["status"] = "failed"
    candidate_result_file.write_text(
        json.dumps(candidate_result, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="page validation did not pass"):
        create_deployment_plan(
            repository=REPOSITORY,
            production_project_directory=REPOSITORY,
            candidate_result_file=candidate_result_file,
            production_env_file=production_env_file,
            manifest=manifest,
            runtime=runtime,
            schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
        )


def test_deployment_plan_requires_candidate_container_removed(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    production_env_file, _ = create_production_env(tmp_path, manifest)
    candidate_result_file = create_candidate_result(tmp_path, manifest)
    runtime.candidate_container_exists = True

    with pytest.raises(ContractError, match="candidate container still exists"):
        create_deployment_plan(
            repository=REPOSITORY,
            production_project_directory=REPOSITORY,
            candidate_result_file=candidate_result_file,
            production_env_file=production_env_file,
            manifest=manifest,
            runtime=runtime,
            schema=load_schema(CONTRACT_DIR / "deployment_plan.schema.json"),
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

    with pytest.raises(ContractError, match="worktree must be clean"):
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
        git_runner=git,
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
            git_runner=git,
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
            git_runner=git,
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
            git_runner=git,
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
            git_runner=git,
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

    evidence = verify_post_deploy(manifest, runtime, deployment_plan=plan)

    assert evidence["actual_image_id"] == IMAGE_ID
    assert evidence["config_image"] == IMAGE_REF


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


def test_post_deploy_rejects_actual_container_image_id_mismatch(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    runtime.production_record["image_id"] = ROLLBACK_ID

    with pytest.raises(ContractError, match="production container Image ID mismatch"):
        verify_post_deploy(manifest, runtime, deployment_plan=plan)


def test_post_deploy_rejects_config_image_mismatch(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    runtime.production_record["config_image"] = ROLLBACK_REF

    with pytest.raises(ContractError, match="Config.Image mismatch"):
        verify_post_deploy(manifest, runtime, deployment_plan=plan)


def test_post_deploy_rejects_container_release_json_mismatch(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    plan, _, _ = build_deployment_plan(tmp_path, manifest, runtime)
    runtime.production_release_json["release_id"] = (
        "spread-20260717-aaaaaaaaaaaa-b02"
    )

    with pytest.raises(ContractError, match="/app/RELEASE.json"):
        verify_post_deploy(manifest, runtime, deployment_plan=plan)


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
    )

    assert evidence["rollback_image_ref"] == ROLLBACK_REF
    assert evidence["rollback_image_id"] == ROLLBACK_ID
    assert evidence["compose_image"] == ROLLBACK_REF
    runtime.rollback_image_id = IMAGE_ID
    with pytest.raises(ContractError, match="rollback tag ID mismatch"):
        verify_pre_rollback(
            manifest,
            REPOSITORY,
            runtime,
            deployment_plan=plan,
            production_environment=production_environment,
        )


def test_post_rollback_verifies_actual_container_image_id(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    runtime.production_record = {
        "image_id": ROLLBACK_ID,
        "config_image": ROLLBACK_REF,
    }

    evidence = verify_post_rollback(manifest, runtime)

    assert evidence["actual_image_id"] == ROLLBACK_ID
    runtime.production_record["image_id"] = IMAGE_ID
    with pytest.raises(ContractError, match="rolled-back container Image ID mismatch"):
        verify_post_rollback(manifest, runtime)


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
        assert "up -d --no-build --no-deps spread-dashboard" in script
        assert "up -d --no-build --no-deps usda-dashboard" not in script
        assert "up -d --no-build --no-deps oil-world" not in script
    assert 'production_env_file="${plan_identity[2]}"' in deploy
    assert 'SPREAD_IMAGE="${rollback_image_ref}"' in rollback
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
        "09_deploy/spread_release/deployment_plan.schema.json",
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
        "07_docs/农产品研究系统更新手册.md",
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
