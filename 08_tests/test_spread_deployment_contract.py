from __future__ import annotations

import copy
import hashlib
import json
import os
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

from release_contract import (  # noqa: E402
    APPLICATION,
    COMPOSE_PROJECT,
    ContractError,
    DATA_SPECS,
    DockerReleaseRuntime,
    collect_data_baseline,
    create_manifest,
    load_manifest_bundle,
    load_schema,
    resolve_spread_image_offline,
    validate_full_git_commit,
    validate_manifest,
    validate_release_image_ref,
    validate_repository_static,
    validate_rollback_image_ref,
    validate_source,
    verify_post_deploy,
    verify_post_rollback,
    verify_pre_deploy,
    verify_pre_rollback,
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
        self.dataset_overrides: dict[str, dict[str, object]] = {}

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
        if container_name == CANDIDATE_CONTAINER:
            return dict(self.candidate_record)
        if container_name == "spread-dashboard":
            return dict(self.production_record)
        raise ContractError(f"unknown container in fixture: {container_name}")

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
        self, repository: Path, image_ref: str
    ) -> tuple[dict[str, object], str, list[str]]:
        assert repository == REPOSITORY
        assert image_ref in {IMAGE_REF, ROLLBACK_REF}
        compose = {
            "name": COMPOSE_PROJECT,
            "services": {
                "spread-dashboard": {
                    "image": self.compose_image,
                    "build": {
                        "context": str(REPOSITORY),
                        "dockerfile": "Dockerfile",
                    },
                },
                "usda-dashboard": {
                    "build": {
                        "context": str(REPOSITORY / "11_独立应用" / "USDA平衡表"),
                        "dockerfile": "Dockerfile",
                    }
                },
            },
        }
        raw = json.dumps(compose, ensure_ascii=False, sort_keys=True) + "\n"
        images = [self.compose_image, "market-data-usda-dashboard"]
        return compose, raw, images

    def dataset_stats(self, container_name, spec) -> dict[str, object]:
        assert container_name == CANDIDATE_CONTAINER
        return self.dataset_overrides.get(
            spec.name,
            {
                "records": 10,
                "latest_business_date": "2026-07-12",
                "duplicate_rows_on_key": 0,
            },
        )


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
    environment = {**os.environ, "SPREAD_IMAGE": IMAGE_REF}
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


def test_manifest_creation_rejects_candidate_image_id_mismatch(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    create_data_files(data_root)
    runtime = FakeReleaseRuntime()
    runtime.candidate_record["image_id"] = ROLLBACK_ID

    with pytest.raises(ContractError, match="candidate container Image ID mismatch"):
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
            runtime=runtime,
            git_runner=FakeGitRunner(),
        )


def test_pre_deploy_accepts_exact_image_oci_release_and_compose(
    tmp_path: Path,
) -> None:
    manifest, runtime, git = build_manifest(tmp_path)

    evidence = verify_pre_deploy(
        manifest,
        REPOSITORY,
        runtime,
        git_runner=git,
    )

    assert evidence["image_ref"] == IMAGE_REF
    assert evidence["image_id"] == IMAGE_ID
    assert evidence["oci_revision"] == GIT_COMMIT
    assert evidence["compose_image"] == IMAGE_REF


def test_pre_deploy_rejects_tag_to_image_id_mismatch(tmp_path: Path) -> None:
    manifest, runtime, git = build_manifest(tmp_path)
    runtime.release_image_id = ROLLBACK_ID

    with pytest.raises(ContractError, match="image tag ID mismatch"):
        verify_pre_deploy(manifest, REPOSITORY, runtime, git_runner=git)


def test_pre_deploy_rejects_oci_revision_mismatch(tmp_path: Path) -> None:
    manifest, runtime, git = build_manifest(tmp_path)
    runtime.labels["org.opencontainers.image.revision"] = OLD_GIT_COMMIT

    with pytest.raises(ContractError, match="image label.*revision mismatch"):
        verify_pre_deploy(manifest, REPOSITORY, runtime, git_runner=git)


def test_pre_deploy_rejects_image_release_json_mismatch(tmp_path: Path) -> None:
    manifest, runtime, git = build_manifest(tmp_path)
    runtime.image_release_json["git_commit"] = OLD_GIT_COMMIT

    with pytest.raises(ContractError, match="/app/RELEASE.json"):
        verify_pre_deploy(manifest, REPOSITORY, runtime, git_runner=git)


def test_regression_compose_must_not_resolve_old_latest_image(
    tmp_path: Path,
) -> None:
    manifest, runtime, git = build_manifest(tmp_path)
    assert runtime.image_record(IMAGE_REF)["id"] == IMAGE_ID
    assert runtime.image_record(LATEST_REF)["id"] == ROLLBACK_ID
    runtime.compose_image = LATEST_REF

    with pytest.raises(ContractError, match="Compose resolved spread image mismatch"):
        verify_pre_deploy(manifest, REPOSITORY, runtime, git_runner=git)


def test_candidate_and_production_must_use_same_image_id(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    assert runtime.container_record(CANDIDATE_CONTAINER)["image_id"] == IMAGE_ID

    evidence = verify_post_deploy(manifest, runtime)

    assert evidence["actual_image_id"] == IMAGE_ID
    assert evidence["config_image"] == IMAGE_REF


def test_post_deploy_rejects_actual_container_image_id_mismatch(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    runtime.production_record["image_id"] = ROLLBACK_ID

    with pytest.raises(ContractError, match="production container Image ID mismatch"):
        verify_post_deploy(manifest, runtime)


def test_post_deploy_rejects_config_image_mismatch(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    runtime.production_record["config_image"] = ROLLBACK_REF

    with pytest.raises(ContractError, match="Config.Image mismatch"):
        verify_post_deploy(manifest, runtime)


def test_post_deploy_rejects_container_release_json_mismatch(
    tmp_path: Path,
) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    runtime.production_release_json["release_id"] = (
        "spread-20260717-aaaaaaaaaaaa-b02"
    )

    with pytest.raises(ContractError, match="/app/RELEASE.json"):
        verify_post_deploy(manifest, runtime)


def test_rollback_requires_explicit_tag_to_image_id_match(tmp_path: Path) -> None:
    manifest, runtime, _ = build_manifest(tmp_path)
    runtime.compose_image = ROLLBACK_REF

    evidence = verify_pre_rollback(manifest, REPOSITORY, runtime)

    assert evidence["rollback_image_ref"] == ROLLBACK_REF
    assert evidence["rollback_image_id"] == ROLLBACK_ID
    assert evidence["compose_image"] == ROLLBACK_REF
    runtime.rollback_image_id = IMAGE_ID
    with pytest.raises(ContractError, match="rollback tag ID mismatch"):
        verify_pre_rollback(manifest, REPOSITORY, runtime)


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
    assert 'SPREAD_IMAGE="${SPREAD_IMAGE}" docker compose' in deploy
    assert 'SPREAD_IMAGE="${rollback_image_ref}" docker compose' in rollback
    assert 'manifest["rollback_image_ref"]' in rollback
    assert 'manifest["rollback_image_id"]' in rollback


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
    http_check = deploy.index("curl --fail")
    production_record = deploy.index("--phase record-deployment")
    assert post_identity < http_check < production_record
    assert "trap rollback_on_failure ERR" in deploy
    assert 'bash "${rollback_script}" "${release_directory}" "${health_url}"' in deploy
    assert deploy.index("trap rollback_on_failure ERR") < deploy.index(
        "docker compose"
    )


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
