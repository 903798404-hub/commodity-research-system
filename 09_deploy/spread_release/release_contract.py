from __future__ import annotations

import base64
import copy
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from wait_for_service_ready import (
    DEFAULT_READINESS_POLICY,
    validate_log_summary,
    validate_readiness_policy,
)

APPLICATION = "spread-dashboard"
COMPOSE_PROJECT = "market-data"
COMPOSE_SERVICE = "spread-dashboard"
PRODUCTION_CONTAINER = "spread-dashboard"
SCHEMA_VERSION = "2.6.0"
CANDIDATE_RESULT_SCHEMA_VERSION = "1.6.0"
LEGACY_CANDIDATE_RESULT_SCHEMA_VERSION = "1.5.0"
SUPPORTED_CANDIDATE_RESULT_SCHEMA_VERSIONS = {
    LEGACY_CANDIDATE_RESULT_SCHEMA_VERSION,
    CANDIDATE_RESULT_SCHEMA_VERSION,
}
DEPLOYMENT_PLAN_SCHEMA_VERSION = "1.6.0"
LEGACY_DEPLOYMENT_PLAN_SCHEMA_VERSION = "1.5.0"
DEPLOYMENT_RESULT_SCHEMA_VERSION = "1.5.0"
LEGACY_DEPLOYMENT_RESULT_SCHEMA_VERSION = "1.4.0"
DEPLOYMENT_RESULT_BUNDLE_SCHEMA_VERSION = "1.0.0"
DEPLOYMENT_RESULT_BUNDLE_FILENAME = "deployment_result.bundle.manifest.json"
DEPLOYMENT_RESULT_BUNDLE_MEMBER_FILENAMES = (
    "deployment_result.json",
    "deployment_result.manifest.json",
)
ARTIFACT_MANIFEST_SCHEMA_VERSION = "1.5.0"
LEGACY_ARTIFACT_MANIFEST_SCHEMA_VERSION = "1.4.0"
ARTIFACT_MANIFEST_FILENAMES = {
    "release": "release.manifest.json",
    "candidate_result": "candidate_result.manifest.json",
    "deployment_plan": "deployment_plan.manifest.json",
    "deployment_result": "deployment_result.manifest.json",
}
ARTIFACT_TARGET_FILENAMES = {
    "release": "release.json",
    "candidate_result": "candidate_result.json",
    "deployment_plan": "deployment_plan.json",
    "deployment_result": "deployment_result.json",
}
ARTIFACT_TARGET_SCHEMA_VERSIONS = {
    "release": SCHEMA_VERSION,
    "candidate_result": CANDIDATE_RESULT_SCHEMA_VERSION,
    "deployment_plan": DEPLOYMENT_PLAN_SCHEMA_VERSION,
    "deployment_result": DEPLOYMENT_RESULT_SCHEMA_VERSION,
}
ARTIFACT_TARGET_SCHEMA_FILENAMES = {
    "release": "release.schema.json",
    "candidate_result": "candidate_result.schema.json",
    "deployment_plan": "deployment_plan.schema.json",
    "deployment_result": "deployment_result.schema.json",
}
RELEASE_ENV_KEYS = (
    "RELEASE_ID",
    "SPREAD_IMAGE",
    "EXPECTED_IMAGE_ID",
    "EXPECTED_GIT_COMMIT",
)
PRODUCTION_ENV_KEYS = (
    "SPREAD_IMAGE",
    "MARKET_DATA_GIT_HEAD",
    "USDA_DASHBOARD_URL",
    "OIL_WORLD_DASHBOARD_URL",
    "WEATHER_RUNTIME_CURRENT_DIR",
    "WEATHER_DATA_DIR",
)
RUNTIME_URL_KEYS = (
    "USDA_DASHBOARD_URL",
    "OIL_WORLD_DASHBOARD_URL",
)
WEATHER_RUNTIME_ENV_KEY = "WEATHER_RUNTIME_CURRENT_DIR"
WEATHER_DATA_DIR_ENV_KEY = "WEATHER_DATA_DIR"
PUBLIC_MARKET_DATA_RUNTIME_ENV_KEY = "PUBLIC_MARKET_DATA_RUNTIME_ROOT"
PUBLIC_MARKET_DATA_CONTAINER_ROOT = "/app/01_data/public-market-data"
WEATHER_CONTAINER_PATH = "/app/runtime/weather"
WEATHER_CONTAINER_CURRENT_PATH = f"{WEATHER_CONTAINER_PATH}/current"
WEATHER_CONTAINER_NEXT_PATH = f"{WEATHER_CONTAINER_PATH}/next"
PRODUCTION_WEATHER_RUNTIME_DIR = "/home/ubuntu/market-data-runtime/weather/processed"
WEATHER_CANDIDATE_MODE_CURRENT = "current"
WEATHER_CANDIDATE_MODE_NEXT = "next"
WEATHER_CANDIDATE_MODES = (
    WEATHER_CANDIDATE_MODE_CURRENT,
    WEATHER_CANDIDATE_MODE_NEXT,
)
WEATHER_CANDIDATE_RUNTIME_DIRS = {
    WEATHER_CANDIDATE_MODE_CURRENT: PRODUCTION_WEATHER_RUNTIME_DIR,
    WEATHER_CANDIDATE_MODE_NEXT: PRODUCTION_WEATHER_RUNTIME_DIR,
}
WEATHER_CANDIDATE_DATA_DIRS = {
    WEATHER_CANDIDATE_MODE_CURRENT: WEATHER_CONTAINER_CURRENT_PATH,
    WEATHER_CANDIDATE_MODE_NEXT: WEATHER_CONTAINER_NEXT_PATH,
}
WEATHER_RUNTIME_MOUNT_CONTRACT = {
    "environment_variable": WEATHER_RUNTIME_ENV_KEY,
    "production_host_path": PRODUCTION_WEATHER_RUNTIME_DIR,
    "allowed_candidate_host_paths": list(
        dict.fromkeys(WEATHER_CANDIDATE_RUNTIME_DIRS.values())
    ),
    "container_path": WEATHER_CONTAINER_PATH,
    "read_only": True,
    "weather_data_dir": WEATHER_CONTAINER_CURRENT_PATH,
}
RUNTIME_ENVIRONMENT_CONTRACT = {
    "allowed_production_variables": list(PRODUCTION_ENV_KEYS),
    "required_production_variables": list(PRODUCTION_ENV_KEYS),
    "release_bound_variables": {
        "SPREAD_IMAGE": "image_ref",
        "MARKET_DATA_GIT_HEAD": "git_commit",
    },
    "allowed_candidate_production_differences": [],
    "undeclared_variables_forbidden": True,
    "weather_runtime_mount": copy.deepcopy(WEATHER_RUNTIME_MOUNT_CONTRACT),
}
PRODUCTION_SERVICE_SCOPE = (COMPOSE_SERVICE,)
FORMAL_CONTAINER_NAMES = ("usda-dashboard", "oil-world-dashboard")
FORMAL_CONTAINER_IDENTITY_SCHEMA_VERSION = "1.0.0"
FORMAL_CONTAINER_IDENTITY_FIELDS = (
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
FORMAL_CONTAINER_IDENTITY_CONTRACT = {
    "schema_version": FORMAL_CONTAINER_IDENTITY_SCHEMA_VERSION,
    "services": list(FORMAL_CONTAINER_NAMES),
    "required_fields": list(FORMAL_CONTAINER_IDENTITY_FIELDS),
    "comparison": "exact_after_canonical_normalization",
    "fail_closed": True,
}
PRODUCTION_DATA_MOUNTS = {
    "01_data": "/app/01_data",
    "06_outputs": "/app/06_outputs",
    "10_logs": "/app/10_logs",
}
PRODUCTION_DATA_MOUNT_READ_ONLY = {
    "/app/01_data": True,
    "/app/06_outputs": False,
    "/app/10_logs": False,
}
IMPORT_PROFIT_RUNTIME_ENV_KEY = "IMPORT_PROFIT_RUNTIME_ROOT"
IMPORT_PROFIT_RUNTIME_CONTAINER_PATH = "/app/runtime/import_profit"
IMPORT_PROFIT_RUNTIME_MOUNT_ID = "import_profit_candidate_runtime"
REAL_NIGHT_SESSION_CLOSE_GATE_ID = "real_night_session_close_snapshot"
CANDIDATE_VALIDATED_STATUS = "candidate-validated"
CANDIDATE_WAITING_STATUS = "candidate-waiting-gate"


def candidate_compose_environment(
    git_commit: str,
    production_environment: Mapping[str, str],
    weather_candidate_mode: str = WEATHER_CANDIDATE_MODE_NEXT,
) -> dict[str, str]:
    """Build candidate settings from validated browser-facing production URLs.

    Candidate and production dashboards link users to the same independent USDA and
    Oil World services.  Host-loopback addresses are reserved for server health
    probes and must never be propagated into browser-facing candidate pages.
    """
    candidate_user_urls = candidate_user_url_environment(production_environment)
    candidate_weather_dir = weather_candidate_source(weather_candidate_mode)
    return {
        **candidate_user_urls,
        "MARKET_DATA_GIT_HEAD": validate_full_git_commit(git_commit),
        WEATHER_RUNTIME_ENV_KEY: candidate_weather_dir,
        WEATHER_DATA_DIR_ENV_KEY: weather_candidate_data_dir(weather_candidate_mode),
    }


FULL_GIT_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RELEASE_ID_RE = re.compile(
    r"^spread-(?P<date>\d{8})-(?P<commit>[0-9a-f]{12,40})-b(?P<build>\d{2,})$"
)
SAFE_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]+$")
FORBIDDEN_TAGS = {"latest", "new"}
SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|token|api[_-]?key|secret|credential|private[_-]?key)",
    re.IGNORECASE,
)
SENSITIVE_VALUE_RE = re.compile(
    r"(^|[/\\])(\.env(?:[./\\]|$)|id_rsa(?:[./\\]|$)|id_ed25519(?:[./\\]|$)|\.ssh(?:[./\\]|$))",
    re.IGNORECASE,
)


class ContractError(RuntimeError):
    """Raised when a release fact is missing, ambiguous, or inconsistent."""


@dataclass(frozen=True)
class DataSpec:
    name: str
    relative_host_path: str
    container_path: str
    date_column: str
    primary_key: tuple[str, ...]
    update_task: str


SPREAD_PRIMARY_KEY = ("date", "spread_name")


DATA_SPECS = (
    DataSpec(
        name="spread",
        relative_host_path="01_data/historical_spread_database.parquet",
        container_path="/app/01_data/historical_spread_database.parquet",
        date_column="date",
        primary_key=SPREAD_PRIMARY_KEY,
        update_task="server_update_spreads.py scheduled update",
    ),
    DataSpec(
        name="basis",
        relative_host_path="01_data/database/basis/basis_quotes.parquet",
        container_path="/app/01_data/database/basis/basis_quotes.parquet",
        date_column="date",
        primary_key=(
            "date",
            "commodity",
            "region",
            "quote_type",
            "delivery_month",
            "futures_contract",
        ),
        update_task="validated update_basis_data.py update",
    ),
    DataSpec(
        name="soybean_progress",
        relative_host_path=(
            "01_data/processed/soybean_crop_progress/"
            "soybeans_crop_progress_weekly.parquet"
        ),
        container_path=(
            "/app/01_data/processed/soybean_crop_progress/"
            "soybeans_crop_progress_weekly.parquet"
        ),
        date_column="week_ending",
        primary_key=(
            "system",
            "commodity",
            "metric_family",
            "metric",
            "geography_level",
            "region_code",
            "week_ending",
            "unit",
        ),
        update_task="run_soybean_crop_weekly_update.sh scheduled update",
    ),
    DataSpec(
        name="soybean_condition",
        relative_host_path=(
            "01_data/processed/soybean_crop_progress/"
            "soybeans_crop_condition_weekly.parquet"
        ),
        container_path=(
            "/app/01_data/processed/soybean_crop_progress/"
            "soybeans_crop_condition_weekly.parquet"
        ),
        date_column="week_ending",
        primary_key=(
            "system",
            "commodity",
            "metric_family",
            "metric",
            "geography_level",
            "region_code",
            "week_ending",
            "unit",
        ),
        update_task="run_soybean_crop_weekly_update.sh scheduled update",
    ),
)


class ReleaseRuntime(Protocol):
    def image_record(self, image_ref: str) -> dict[str, Any]: ...

    def container_record(self, container_name: str) -> dict[str, Any]: ...

    def formal_container_identity(self, container_name: str) -> dict[str, Any]: ...

    def container_exists(self, container_name: str) -> bool: ...

    def read_candidate_release(self, container_name: str) -> dict[str, Any]: ...

    def candidate_release_sha256(self, container_name: str) -> str: ...

    def read_image_release(self, image_ref: str) -> dict[str, Any]: ...

    def image_release_sha256(self, image_ref: str) -> str: ...

    def read_container_release(self, container_name: str) -> dict[str, Any]: ...

    def container_release_sha256(self, container_name: str) -> str: ...

    def compose_config(
        self,
        repository: Path,
        image_ref: str,
        *,
        environment: Mapping[str, str] | None = None,
        project_directory: Path | None = None,
        compose_file: Path | None = None,
    ) -> tuple[dict[str, Any], str, list[str]]: ...

    def dataset_stats(
        self, container_name: str, spec: DataSpec
    ) -> dict[str, Any]: ...


class CommandRunner:
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        completed = subprocess.run(
            list(command),
            cwd=str(cwd) if cwd else None,
            env=process_env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ContractError(
                f"command failed ({completed.returncode}): {' '.join(command)}"
                + (f": {detail}" if detail else "")
            )
        return completed.stdout


class DockerReleaseRuntime:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or CommandRunner()

    def image_record(self, image_ref: str) -> dict[str, Any]:
        payload = _load_docker_array(
            self.runner.run(["docker", "image", "inspect", image_ref]),
            f"image {image_ref}",
        )
        image_id = payload.get("Id")
        validate_image_id(image_id, f"image {image_ref} ID")
        labels = payload.get("Config", {}).get("Labels") or {}
        return {"id": image_id, "labels": labels}

    def container_record(self, container_name: str) -> dict[str, Any]:
        payload = _load_docker_array(
            self.runner.run(["docker", "inspect", container_name]),
            f"container {container_name}",
        )
        image_id = payload.get("Image")
        validate_image_id(image_id, f"container {container_name} Image")
        config_image = payload.get("Config", {}).get("Image")
        if not isinstance(config_image, str) or not config_image:
            raise ContractError(f"container {container_name} has no Config.Image")
        runtime_git_commit = _runtime_git_commit_from_inspect(
            payload,
            f"container {container_name}",
        )
        record = {
            "image_id": image_id,
            "config_image": config_image,
            "runtime_git_commit": runtime_git_commit,
        }
        if "Mounts" not in payload:
            return record
        raw_mounts = payload["Mounts"]
        if not isinstance(raw_mounts, list):
            raise ContractError(f"container {container_name} mounts are invalid")
        mounts: list[dict[str, Any]] = []
        for mount in raw_mounts:
            if not isinstance(mount, dict):
                raise ContractError(f"container {container_name} mount is invalid")
            mount_type = mount.get("Type")
            source = mount.get("Source")
            destination = mount.get("Destination")
            rw = mount.get("RW")
            if (
                mount_type not in {"bind", "volume", "tmpfs", "npipe", "cluster"}
                or not isinstance(source, str)
                or not isinstance(destination, str)
                or not isinstance(rw, bool)
            ):
                raise ContractError(f"container {container_name} mount fields are invalid")
            mounts.append(
                {
                    "type": mount_type,
                    "source": source,
                    "destination": destination,
                    "read_only": not rw,
                }
            )
        record["mounts"] = sorted(
            mounts,
            key=lambda item: (
                item["type"],
                item["source"],
                item["destination"],
                item["read_only"],
            ),
        )
        return record

    def formal_container_identity(self, container_name: str) -> dict[str, Any]:
        if container_name not in FORMAL_CONTAINER_NAMES:
            raise ContractError(f"unsupported formal container: {container_name}")
        payload = _load_docker_array(
            self.runner.run(["docker", "inspect", container_name]),
            f"container {container_name}",
        )
        container_id = payload.get("Id")
        if not isinstance(container_id, str) or not re.fullmatch(
            r"[0-9a-f]{64}", container_id
        ):
            raise ContractError(f"container {container_name} has no full container ID")
        image_id = payload.get("Image")
        validate_image_id(image_id, f"container {container_name} Image")
        image_ref = payload.get("Config", {}).get("Image")
        if not isinstance(image_ref, str) or not image_ref:
            raise ContractError(f"container {container_name} has no Config.Image")
        last_component = image_ref.rsplit("/", 1)[-1]
        if ":" not in last_component:
            raise ContractError(
                f"container {container_name} image reference must have an explicit tag"
            )
        tag = last_component.rsplit(":", 1)[-1].casefold()
        if tag in {"latest", "new"}:
            raise ContractError(
                f"container {container_name} image reference uses an ambiguous tag"
            )
        image = self.image_record(image_ref)
        if image.get("id") != image_id:
            raise ContractError(
                f"container {container_name} image tag does not resolve to its Image ID"
            )
        revision = (image.get("labels") or {}).get(
            "org.opencontainers.image.revision"
        )
        validate_full_git_commit(revision)
        created_at = payload.get("Created")
        state = payload.get("State") or {}
        started_at = state.get("StartedAt")
        validate_build_time(created_at)
        validate_build_time(started_at)
        restart_count = payload.get("RestartCount")
        if not isinstance(restart_count, int) or isinstance(restart_count, bool):
            raise ContractError(f"container {container_name} RestartCount is invalid")
        if restart_count < 0:
            raise ContractError(f"container {container_name} RestartCount is negative")
        status = state.get("Status")
        if status not in {
            "created",
            "running",
            "restarting",
            "removing",
            "paused",
            "exited",
            "dead",
        }:
            raise ContractError(f"container {container_name} status is invalid")
        health = state.get("Health")
        health_status = (
            health.get("Status") if isinstance(health, dict) else "not-configured"
        )
        if health_status not in {
            "healthy",
            "unhealthy",
            "starting",
            "not-configured",
        }:
            raise ContractError(f"container {container_name} health status is invalid")
        ports: list[dict[str, Any]] = []
        network_ports = (payload.get("NetworkSettings") or {}).get("Ports") or {}
        if not isinstance(network_ports, dict):
            raise ContractError(f"container {container_name} ports are invalid")
        for container_port, bindings in network_ports.items():
            match = re.fullmatch(r"([0-9]{1,5})/(tcp|udp|sctp)", str(container_port))
            if not match:
                raise ContractError(f"container {container_name} port key is invalid")
            normalized_bindings = bindings if isinstance(bindings, list) and bindings else [None]
            for binding in normalized_bindings:
                host_ip = None
                host_port = None
                if binding is not None:
                    if not isinstance(binding, dict):
                        raise ContractError(
                            f"container {container_name} port binding is invalid"
                        )
                    host_ip = binding.get("HostIp") or None
                    raw_host_port = binding.get("HostPort") or None
                    if raw_host_port is not None:
                        if not str(raw_host_port).isdigit():
                            raise ContractError(
                                f"container {container_name} host port is invalid"
                            )
                        host_port = int(raw_host_port)
                ports.append(
                    {
                        "container_port": int(match.group(1)),
                        "protocol": match.group(2),
                        "host_ip": host_ip,
                        "host_port": host_port,
                    }
                )
        ports.sort(
            key=lambda item: (
                item["container_port"],
                item["protocol"],
                item["host_ip"] or "",
                item["host_port"] if item["host_port"] is not None else -1,
            )
        )
        mounts: list[dict[str, Any]] = []
        raw_mounts = payload.get("Mounts") or []
        if not isinstance(raw_mounts, list):
            raise ContractError(f"container {container_name} mounts are invalid")
        for mount in raw_mounts:
            if not isinstance(mount, dict):
                raise ContractError(f"container {container_name} mount is invalid")
            mount_type = mount.get("Type")
            source = mount.get("Source")
            destination = mount.get("Destination")
            rw = mount.get("RW")
            if (
                mount_type not in {"bind", "volume", "tmpfs", "npipe", "cluster"}
                or not isinstance(source, str)
                or not isinstance(destination, str)
                or not isinstance(rw, bool)
            ):
                raise ContractError(f"container {container_name} mount fields are invalid")
            mounts.append(
                {
                    "type": mount_type,
                    "source": source,
                    "destination": destination,
                    "read_only": not rw,
                }
            )
        mounts.sort(
            key=lambda item: (
                item["type"],
                item["source"],
                item["destination"],
                item["read_only"],
            )
        )
        return {
            "service": container_name,
            "container_id": container_id,
            "image_id": image_id,
            "image_ref": image_ref,
            "oci_revision": revision,
            "created_at": created_at,
            "started_at": started_at,
            "restart_count": restart_count,
            "status": status,
            "health_status": health_status,
            "ports": ports,
            "mounts": mounts,
        }

    def container_exists(self, container_name: str) -> bool:
        if not SAFE_CONTAINER_RE.fullmatch(container_name):
            raise ContractError("container name is invalid")
        output = self.runner.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"name=^/{container_name}$",
                "--format",
                "{{.ID}}",
            ]
        )
        return bool(output.strip())

    def read_candidate_release(self, container_name: str) -> dict[str, Any]:
        return _parse_release_json(
            self.runner.run(
                ["docker", "exec", container_name, "cat", "/app/RELEASE.json"]
            ),
            f"candidate container {container_name}",
        )

    def candidate_release_sha256(self, container_name: str) -> str:
        return self._container_file_sha256(container_name, "/app/RELEASE.json")

    def read_container_release(self, container_name: str) -> dict[str, Any]:
        return _parse_release_json(
            self.runner.run(
                ["docker", "exec", container_name, "cat", "/app/RELEASE.json"]
            ),
            f"container {container_name}",
        )

    def container_release_sha256(self, container_name: str) -> str:
        return self._container_file_sha256(container_name, "/app/RELEASE.json")

    def _container_file_sha256(self, container_name: str, path: str) -> str:
        raw = self.runner.run(
            ["docker", "exec", container_name, "sha256sum", path]
        ).strip()
        digest = raw.split(maxsplit=1)[0] if raw else ""
        return validate_sha256(digest, f"{container_name}:{path} SHA-256")

    def read_image_release(self, image_ref: str) -> dict[str, Any]:
        release, _ = self._read_image_release_file(image_ref)
        return release

    def image_release_sha256(self, image_ref: str) -> str:
        _, digest = self._read_image_release_file(image_ref)
        return digest

    def _read_image_release_file(
        self, image_ref: str
    ) -> tuple[dict[str, Any], str]:
        temporary_name = f"spread-release-inspect-{uuid.uuid4().hex[:12]}"
        with tempfile.TemporaryDirectory(prefix="spread-release-json-") as folder:
            destination = Path(folder) / "RELEASE.json"
            created = False
            try:
                self.runner.run(
                    [
                        "docker",
                        "create",
                        "--name",
                        temporary_name,
                        "--entrypoint",
                        "/bin/sh",
                        image_ref,
                        "-c",
                        "true",
                    ]
                )
                created = True
                self.runner.run(
                    [
                        "docker",
                        "cp",
                        f"{temporary_name}:/app/RELEASE.json",
                        str(destination),
                    ]
                )
                if not destination.is_file():
                    raise ContractError(
                        f"image {image_ref} does not contain /app/RELEASE.json"
                    )
                return (
                    _parse_release_json(
                        destination.read_text(encoding="utf-8"),
                        f"image {image_ref}",
                    ),
                    hash_file(destination),
                )
            finally:
                if created:
                    self.runner.run(["docker", "rm", temporary_name])

    def compose_config(
        self,
        repository: Path,
        image_ref: str,
        *,
        environment: Mapping[str, str] | None = None,
        project_directory: Path | None = None,
        compose_file: Path | None = None,
    ) -> tuple[dict[str, Any], str, list[str]]:
        repository = repository.resolve()
        resolved_project_directory = (project_directory or repository).resolve()
        resolved_compose_file = (compose_file or repository / "docker-compose.yml").resolve()
        base = [
            "docker",
            "compose",
            "--project-directory",
            str(resolved_project_directory),
            "-f",
            str(resolved_compose_file),
        ]
        compose_environment = {"SPREAD_IMAGE": image_ref}
        if environment:
            compose_environment.update(environment)
        raw_config = self.runner.run(
            [*base, "config", "--format", "json"],
            cwd=resolved_project_directory,
            env=compose_environment,
        )
        try:
            parsed = json.loads(raw_config)
        except json.JSONDecodeError as exc:
            raise ContractError(f"docker compose config returned invalid JSON: {exc}") from exc
        raw_images = self.runner.run(
            [*base, "config", "--images"],
            cwd=resolved_project_directory,
            env=compose_environment,
        )
        images = [line.strip() for line in raw_images.splitlines() if line.strip()]
        return parsed, raw_config, images

    def dataset_stats(
        self, container_name: str, spec: DataSpec
    ) -> dict[str, Any]:
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "path": spec.container_path,
                    "date_column": spec.date_column,
                    "primary_key": spec.primary_key,
                    "strict_primary_key": spec.name == "spread",
                },
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        program = """
import base64
import json
import sys

import pandas as pd

payload = json.loads(base64.urlsafe_b64decode(sys.argv[1]).decode())
data = pd.read_parquet(payload["path"])
required = [payload["date_column"], *payload["primary_key"]]
missing = [column for column in required if column not in data.columns]
if missing:
    raise ValueError(f"missing columns: {missing}")

key_frame = data[payload["primary_key"]]
blank_key = (
    key_frame.astype("string")
    .apply(lambda column: column.str.strip().eq(""))
    .fillna(True)
    .any(axis=1)
)
null_key = key_frame.isna().any(axis=1) | blank_key
primary_key_null_rows = int(null_key.sum())
if payload["strict_primary_key"] and primary_key_null_rows:
    raise ValueError(
        f"primary key contains null or blank values: {primary_key_null_rows}"
    )

dates = pd.to_datetime(data[payload["date_column"]], errors="coerce")
invalid_date_rows = int(dates.isna().sum())
if payload["strict_primary_key"] and invalid_date_rows:
    raise ValueError(f"date column contains invalid values: {invalid_date_rows}")

duplicate_rows = int(data.duplicated(payload["primary_key"], keep=False).sum())
if payload["strict_primary_key"] and duplicate_rows:
    raise ValueError(f"duplicate primary key rows: {duplicate_rows}")

latest = dates.max()
if pd.isna(latest):
    raise ValueError("latest business date is empty")

print(
    json.dumps(
        {
            "records": int(len(data)),
            "latest_business_date": latest.date().isoformat(),
            "primary_key_null_rows": primary_key_null_rows,
            "duplicate_rows_on_key": duplicate_rows,
            "invalid_date_rows": invalid_date_rows,
        }
    )
)
"""
        raw = self.runner.run(
            [
                "docker",
                "exec",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                container_name,
                "python",
                "-c",
                program,
                payload,
            ]
        )
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractError(
                f"deployment data inspection failed for {spec.name}: {exc}"
            ) from exc
        return result


def _load_docker_array(raw: str, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{description} inspect output is not JSON: {exc}") from exc
    if not isinstance(payload, list) or len(payload) != 1:
        count = len(payload) if isinstance(payload, list) else "non-list"
        raise ContractError(
            f"{description} must resolve to exactly one object, got {count}"
        )
    if not isinstance(payload[0], dict):
        raise ContractError(f"{description} inspect object is invalid")
    return payload[0]


def _runtime_git_commit_from_inspect(
    payload: Mapping[str, Any],
    description: str,
) -> str:
    config = payload.get("Config")
    environment = config.get("Env") if isinstance(config, dict) else None
    if not isinstance(environment, list):
        raise ContractError(
            f"{description} is missing runtime MARKET_DATA_GIT_HEAD"
        )
    values: list[str] = []
    for entry in environment:
        if not isinstance(entry, str):
            continue
        key, separator, value = entry.partition("=")
        if separator and key == "MARKET_DATA_GIT_HEAD":
            values.append(value)
    if not values:
        raise ContractError(
            f"{description} is missing runtime MARKET_DATA_GIT_HEAD"
        )
    if any(not value for value in values):
        raise ContractError(
            f"{description} runtime MARKET_DATA_GIT_HEAD is empty"
        )
    if len(set(values)) != 1:
        raise ContractError(
            f"{description} has conflicting runtime MARKET_DATA_GIT_HEAD entries"
        )
    try:
        return validate_full_git_commit(values[0])
    except ContractError as exc:
        raise ContractError(
            f"{description} runtime MARKET_DATA_GIT_HEAD is invalid"
        ) from exc


def _parse_release_json(raw: str, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{description} RELEASE.json is invalid: {exc}") from exc
    required = {
        "application",
        "release_id",
        "git_commit",
        "git_tree",
        "build_time",
        "source",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ContractError(
            f"{description} RELEASE.json must contain exactly {sorted(required)}"
        )
    if payload["application"] != APPLICATION:
        raise ContractError(f"{description} RELEASE.json application mismatch")
    validate_full_git_commit(payload["git_commit"])
    validate_full_git_commit(payload["git_tree"])
    if payload["git_tree"] == payload["git_commit"]:
        raise ContractError(
            f"{description} RELEASE.json git_tree must differ from git_commit"
        )
    validate_release_id(payload["release_id"], payload["git_commit"], payload["build_time"])
    validate_build_time(payload["build_time"])
    validate_source(payload["source"])
    assert_no_sensitive_values(payload, f"{description} RELEASE.json")
    return payload


def validate_full_git_commit(value: Any) -> str:
    if not isinstance(value, str) or not FULL_GIT_RE.fullmatch(value):
        raise ContractError("git commit must be a full 40-character lowercase SHA")
    return value


def validate_image_id(value: Any, field: str = "image ID") -> str:
    if not isinstance(value, str) or not IMAGE_ID_RE.fullmatch(value):
        raise ContractError(f"{field} must be sha256:<64 lowercase hex>")
    return value


def validate_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContractError(f"{field} must be a 64-character lowercase SHA-256")
    return value


def validate_build_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ContractError("build_time must be a non-empty RFC 3339 timestamp")
    normalized = re.sub(
        r"(\.\d{6})\d+(Z|[+-]\d{2}:\d{2})$",
        r"\1\2",
        value,
    )
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("build_time must be a valid RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError("build_time must include an explicit timezone")
    return parsed


def formal_evidence_sha256(value: Mapping[str, Any]) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_formal_container_snapshot(
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise ContractError("formal container snapshot must be an object")
    validate_against_schema(
        snapshot,
        load_schema(Path(__file__).with_name("formal_container_identity.schema.json")),
    )
    if snapshot.get("schema_version") != FORMAL_CONTAINER_IDENTITY_SCHEMA_VERSION:
        raise ContractError("formal container snapshot schema_version mismatch")
    validate_build_time(snapshot.get("captured_at"))
    containers = snapshot.get("containers")
    if not isinstance(containers, list):
        raise ContractError("formal container snapshot containers are missing")
    services = [item.get("service") for item in containers if isinstance(item, dict)]
    if services != list(FORMAL_CONTAINER_NAMES):
        raise ContractError(
            "formal container snapshot must contain USDA and Oil World in canonical order"
        )
    for item in containers:
        missing_fields = [
            field for field in FORMAL_CONTAINER_IDENTITY_FIELDS if field not in item
        ]
        if missing_fields:
            raise ContractError(
                "formal container identity fields are missing: "
                + ", ".join(missing_fields)
            )
        container_id = item.get("container_id")
        if not isinstance(container_id, str) or not re.fullmatch(
            r"[0-9a-f]{64}", container_id
        ):
            raise ContractError("formal container ID must be full lowercase hex")
        validate_image_id(item.get("image_id"), "formal container Image ID")
        validate_full_git_commit(item.get("oci_revision"))
        validate_build_time(item.get("created_at"))
        validate_build_time(item.get("started_at"))
        image_ref = item.get("image_ref")
        if not isinstance(image_ref, str) or not image_ref:
            raise ContractError("formal container image_ref is missing")
        last_component = image_ref.rsplit("/", 1)[-1]
        if ":" not in last_component or last_component.rsplit(":", 1)[-1].casefold() in {
            "latest",
            "new",
        }:
            raise ContractError("formal container image_ref must use an explicit stable tag")
        restart_count = item.get("restart_count")
        if (
            not isinstance(restart_count, int)
            or isinstance(restart_count, bool)
            or restart_count < 0
        ):
            raise ContractError("formal container RestartCount is invalid")
        ports = item.get("ports")
        mounts = item.get("mounts")
        if ports != sorted(
            ports,
            key=lambda entry: (
                entry["container_port"],
                entry["protocol"],
                entry["host_ip"] or "",
                entry["host_port"] if entry["host_port"] is not None else -1,
            ),
        ):
            raise ContractError("formal container ports are not in canonical order")
        if mounts != sorted(
            mounts,
            key=lambda entry: (
                entry["type"],
                entry["source"],
                entry["destination"],
                entry["read_only"],
            ),
        ):
            raise ContractError("formal container mounts are not in canonical order")
    return copy.deepcopy(dict(snapshot))


def capture_formal_container_snapshot(
    runtime: ReleaseRuntime,
    *,
    captured_at: str | None = None,
) -> dict[str, Any]:
    timestamp = captured_at or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    snapshot = {
        "schema_version": FORMAL_CONTAINER_IDENTITY_SCHEMA_VERSION,
        "captured_at": timestamp,
        "containers": [
            runtime.formal_container_identity(name) for name in FORMAL_CONTAINER_NAMES
        ],
    }
    return validate_formal_container_snapshot(snapshot)


def compare_formal_container_snapshots(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    before_phase: str,
    after_phase: str,
) -> dict[str, Any]:
    validated_before = validate_formal_container_snapshot(before)
    validated_after = validate_formal_container_snapshot(after)
    differences: list[dict[str, Any]] = []
    before_by_service = {
        item["service"]: item for item in validated_before["containers"]
    }
    after_by_service = {
        item["service"]: item for item in validated_after["containers"]
    }
    for service in FORMAL_CONTAINER_NAMES:
        for field in FORMAL_CONTAINER_IDENTITY_FIELDS:
            before_value = before_by_service[service][field]
            after_value = after_by_service[service][field]
            if before_value != after_value:
                differences.append(
                    {
                        "service": service,
                        "field": field,
                        "before": copy.deepcopy(before_value),
                        "after": copy.deepcopy(after_value),
                    }
                )
    evidence = {
        "before_phase": before_phase,
        "after_phase": after_phase,
        "before": validated_before,
        "after": validated_after,
        "compared_fields": list(FORMAL_CONTAINER_IDENTITY_FIELDS),
        "differences": differences,
        "formal_containers_unchanged": not differences,
    }
    assert_no_sensitive_values(evidence, "formal container identity evidence")
    return evidence


def require_formal_containers_unchanged(
    evidence: Mapping[str, Any], description: str
) -> None:
    if not isinstance(evidence, dict):
        raise ContractError(f"{description} formal container evidence is missing")
    validate_formal_container_snapshot(evidence.get("before") or {})
    validate_formal_container_snapshot(evidence.get("after") or {})
    recomputed = compare_formal_container_snapshots(
        evidence["before"],
        evidence["after"],
        before_phase=str(evidence.get("before_phase")),
        after_phase=str(evidence.get("after_phase")),
    )
    if dict(evidence) != recomputed:
        raise ContractError(f"{description} formal container comparison is not derived")
    if recomputed["formal_containers_unchanged"] is not True:
        raise ContractError(f"{description} formal containers changed")


def write_formal_container_snapshot(
    path: Path, snapshot: Mapping[str, Any]
) -> Path:
    target = path.resolve()
    validated = validate_formal_container_snapshot(snapshot)
    _write_json_exclusive(
        target,
        validated,
        description="formal container identity snapshot",
    )
    return target


def validate_source(value: Any) -> str:
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise ContractError("source must be one non-empty HTTPS repository URL")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.path.strip("/")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ContractError(
            "source must be an HTTPS repository URL without credentials, query, or fragment"
        )
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise ContractError("source must be a repository URL, not a server IP address")
    return value


def validate_release_id(
    release_id: Any,
    git_commit: str | None = None,
    build_time: str | None = None,
) -> re.Match[str]:
    if not isinstance(release_id, str):
        raise ContractError("release_id must be a string")
    match = RELEASE_ID_RE.fullmatch(release_id)
    if not match:
        raise ContractError(
            "release_id must match spread-YYYYMMDD-<12-40 lowercase git hex>-bNN"
        )
    if git_commit is not None:
        validate_full_git_commit(git_commit)
        if not git_commit.startswith(match.group("commit")):
            raise ContractError("release_id git fragment does not identify git_commit")
    if build_time is not None:
        parsed = validate_build_time(build_time)
        if parsed.strftime("%Y%m%d") != match.group("date"):
            raise ContractError("release_id date does not match build_time")
    return match


def split_tagged_image_ref(image_ref: Any) -> tuple[str, str]:
    if not isinstance(image_ref, str) or not image_ref or any(
        char.isspace() for char in image_ref
    ):
        raise ContractError("image reference must be a non-empty string without whitespace")
    if "@" in image_ref:
        raise ContractError("digest-only image references are not the release tag contract")
    last_slash = image_ref.rfind("/")
    last_colon = image_ref.rfind(":")
    if last_colon <= last_slash:
        raise ContractError("image reference must include an explicit tag")
    repository, tag = image_ref[:last_colon], image_ref[last_colon + 1 :]
    if not repository or not tag:
        raise ContractError("image reference must include repository and tag")
    if tag.lower() in FORBIDDEN_TAGS:
        raise ContractError(f"forbidden mutable image tag: {tag}")
    return repository, tag


def validate_release_image_ref(image_ref: Any, release_id: str) -> str:
    repository, tag = split_tagged_image_ref(image_ref)
    if repository.rsplit("/", 1)[-1] != "market-data-spread-dashboard":
        raise ContractError(
            "spread release image repository must be market-data-spread-dashboard"
        )
    if tag != release_id:
        raise ContractError("spread release image tag must exactly equal release_id")
    return str(image_ref)


def validate_rollback_image_ref(image_ref: Any) -> str:
    repository, _ = split_tagged_image_ref(image_ref)
    if repository.rsplit("/", 1)[-1] != "market-data-spread-dashboard":
        raise ContractError(
            "rollback image repository must be market-data-spread-dashboard"
        )
    return str(image_ref)


def assert_no_sensitive_values(value: Any, description: str = "payload") -> None:
    def walk(current: Any, path: str) -> None:
        if isinstance(current, dict):
            for key, item in current.items():
                key_text = str(key)
                if SENSITIVE_KEY_RE.search(key_text):
                    raise ContractError(
                        f"{description} contains a sensitive key at {path}.{key_text}"
                    )
                walk(item, f"{path}.{key_text}")
        elif isinstance(current, list):
            for index, item in enumerate(current):
                walk(item, f"{path}[{index}]")
        elif isinstance(current, str) and SENSITIVE_VALUE_RE.search(current):
            raise ContractError(
                f"{description} contains a sensitive file reference at {path}"
            )

    walk(value, "$")


def hash_file(path: Path) -> str:
    if not path.is_file():
        raise ContractError(f"required file is missing: {path}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ContractError(f"cannot hash required file {path}: {exc}") from exc
    return digest.hexdigest()


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_git_state(
    repository: Path,
    expected_commit: str,
    runner: CommandRunner | None = None,
) -> str:
    validate_full_git_commit(expected_commit)
    command_runner = runner or CommandRunner()
    status = command_runner.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repository
    )
    if status.strip():
        raise ContractError("git checkout must be clean before sealing a release")
    head = command_runner.run(["git", "rev-parse", "HEAD"], cwd=repository).strip()
    validate_full_git_commit(head)
    if head != expected_commit:
        raise ContractError(f"git HEAD mismatch: expected {expected_commit}, got {head}")
    tree = command_runner.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repository,
    ).strip()
    return validate_full_git_commit(tree)


def capture_git_identity(
    repository: Path,
    runner: CommandRunner | None = None,
) -> dict[str, str]:
    """Capture a clean deployment-tool checkout independently of the app release."""
    command_runner = runner or CommandRunner()
    status = command_runner.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repository
    )
    if status.strip():
        raise ContractError("deployment tool checkout must be clean before sealing a plan")
    commit = command_runner.run(
        ["git", "rev-parse", "HEAD"], cwd=repository
    ).strip()
    tree = command_runner.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repository
    ).strip()
    return {
        "git_commit": validate_full_git_commit(commit),
        "git_tree": validate_full_git_commit(tree),
    }


def validate_compose_result(
    compose: Mapping[str, Any],
    raw_config: str,
    images: Sequence[str],
    expected_image_ref: str,
) -> str:
    if compose.get("name") != COMPOSE_PROJECT:
        raise ContractError(
            f"Compose project must resolve to {COMPOSE_PROJECT}, got {compose.get('name')!r}"
        )
    services = compose.get("services")
    if not isinstance(services, dict) or COMPOSE_SERVICE not in services:
        raise ContractError(f"Compose service {COMPOSE_SERVICE} is missing")
    service = services[COMPOSE_SERVICE]
    if not isinstance(service, dict):
        raise ContractError(f"Compose service {COMPOSE_SERVICE} is invalid")
    resolved_image = service.get("image")
    if resolved_image != expected_image_ref:
        raise ContractError(
            "Compose resolved spread image mismatch: "
            f"expected {expected_image_ref}, got {resolved_image!r}"
        )
    _, tag = split_tagged_image_ref(resolved_image)
    if tag.lower() in FORBIDDEN_TAGS:
        raise ContractError("Compose resolved a forbidden mutable spread tag")
    if expected_image_ref not in images:
        raise ContractError(
            "docker compose config --images does not contain the sealed spread image"
        )
    if not raw_config.strip():
        raise ContractError("docker compose config output is empty")
    _validate_weather_runtime_compose(compose)
    return hash_text(raw_config)


def resolve_spread_image_offline(
    compose_text: str, environment: Mapping[str, str]
) -> str:
    marker = (
        "${SPREAD_IMAGE:?SPREAD_IMAGE must be set to an immutable release tag}"
    )
    if marker not in compose_text:
        raise ContractError("Compose does not contain the required SPREAD_IMAGE marker")
    image_ref = environment.get("SPREAD_IMAGE")
    if not image_ref:
        raise ContractError("SPREAD_IMAGE must be set to an immutable release tag")
    split_tagged_image_ref(image_ref)
    return image_ref


def _validate_weather_runtime_compose(
    compose: Mapping[str, Any],
    *,
    expected_source: str | None = None,
    expected_weather_data_dir: str | None = None,
) -> None:
    services = compose.get("services")
    if not isinstance(services, dict):
        raise ContractError("Compose services are missing")
    spread = services.get(COMPOSE_SERVICE)
    if not isinstance(spread, dict):
        raise ContractError("spread Compose service is missing")
    environment = spread.get("environment")
    if (
        not isinstance(environment, dict)
        or environment.get(WEATHER_DATA_DIR_ENV_KEY)
        not in (
            (expected_weather_data_dir,)
            if expected_weather_data_dir is not None
            else tuple(WEATHER_CANDIDATE_DATA_DIRS.values())
        )
    ):
        raise ContractError("spread WEATHER_DATA_DIR does not match the sealed runtime directory")

    weather_mounts: list[Mapping[str, Any]] = []
    for service_name, service in services.items():
        if not isinstance(service, dict):
            raise ContractError(f"Compose service {service_name!r} is invalid")
        volumes = service.get("volumes") or []
        if not isinstance(volumes, list):
            raise ContractError(f"Compose service {service_name!r} volumes are invalid")
        for volume in volumes:
            if not isinstance(volume, dict):
                raise ContractError(f"Compose service {service_name!r} volume is invalid")
            source = volume.get("source")
            target = volume.get("target")
            if isinstance(source, str) and source.lower().endswith(".sql"):
                raise ContractError("raw SQL files must not be mounted into containers")
            if target != WEATHER_CONTAINER_PATH:
                continue
            if service_name != COMPOSE_SERVICE:
                raise ContractError("only spread-dashboard may mount the weather runtime")
            weather_mounts.append(volume)

    if len(weather_mounts) != 1:
        raise ContractError("spread must declare exactly one weather runtime mount")
    weather_mount = weather_mounts[0]
    source = weather_mount.get("source")
    if not isinstance(source, str):
        raise ContractError("weather runtime mount source is invalid")
    validate_weather_runtime_dir(source)
    if weather_mount.get("type") != "bind" or weather_mount.get("read_only") is not True:
        raise ContractError("weather runtime mount must be an exact read-only bind mount")
    if expected_source is not None and source != expected_source:
        raise ContractError("weather runtime mount source does not match the sealed environment")


def validate_repository_static(repository: Path) -> None:
    compose_path = repository / "docker-compose.yml"
    dockerfile_path = repository / "Dockerfile"
    dockerignore_path = repository / ".dockerignore"
    deploy_path = repository / "09_deploy/spread_release/deploy_spread_release.sh"
    rollback_path = repository / "09_deploy/spread_release/rollback_spread_release.sh"
    plan_creator_path = (
        repository / "09_deploy/spread_release/create_deployment_plan.py"
    )
    env_transition_path = (
        repository / "09_deploy/spread_release/transition_production_env.py"
    )
    plan_schema_path = (
        repository / "09_deploy/spread_release/deployment_plan.schema.json"
    )
    candidate_creator_path = (
        repository / "09_deploy/spread_release/create_candidate_result.py"
    )
    candidate_schema_path = (
        repository / "09_deploy/spread_release/candidate_result.schema.json"
    )
    artifact_manifest_schema_path = (
        repository / "09_deploy/spread_release/artifact_manifest.schema.json"
    )
    deployment_result_schema_path = (
        repository / "09_deploy/spread_release/deployment_result.schema.json"
    )
    deployment_result_bundle_schema_path = (
        repository
        / "09_deploy/spread_release/deployment_result_bundle.schema.json"
    )
    required_config = repository / "02_configs/historical_spread_config.xlsx"
    for path in (
        compose_path,
        dockerfile_path,
        dockerignore_path,
        deploy_path,
        rollback_path,
        plan_creator_path,
        env_transition_path,
        plan_schema_path,
        candidate_creator_path,
        candidate_schema_path,
        artifact_manifest_schema_path,
        deployment_result_schema_path,
        deployment_result_bundle_schema_path,
        required_config,
    ):
        if not path.is_file():
            raise ContractError(f"required contract file is missing: {path}")

    compose_text = compose_path.read_text(encoding="utf-8")
    required_image_line = (
        "image: ${SPREAD_IMAGE:?SPREAD_IMAGE must be set to an immutable release tag}"
    )
    if not re.search(r"(?m)^name:\s*market-data\s*$", compose_text):
        raise ContractError("docker-compose.yml must declare name: market-data")
    if required_image_line not in compose_text:
        raise ContractError("spread Compose image must be a required SPREAD_IMAGE")
    for variable in ("MARKET_DATA_GIT_HEAD", *RUNTIME_URL_KEYS):
        marker = f"${{{variable}:?{variable} must be explicitly set}}"
        if marker not in compose_text:
            raise ContractError(
                f"spread Compose production environment must require {variable}"
            )
    weather_data_marker = (
        f"${{{WEATHER_DATA_DIR_ENV_KEY}:?{WEATHER_DATA_DIR_ENV_KEY} must be explicitly set}}"
    )
    if weather_data_marker not in compose_text:
        raise ContractError("spread Compose WEATHER_DATA_DIR must be explicitly required")
    public_market_data_marker = (
        f"{PUBLIC_MARKET_DATA_RUNTIME_ENV_KEY}: {PUBLIC_MARKET_DATA_CONTAINER_ROOT}"
    )
    if public_market_data_marker not in compose_text:
        raise ContractError(
            "spread Compose must declare the fixed Public Market Data runtime root"
        )
    weather_mount_marker = (
        f"${{{WEATHER_RUNTIME_ENV_KEY}:?{WEATHER_RUNTIME_ENV_KEY} must be explicitly set}}:"
        f"{WEATHER_CONTAINER_PATH}:ro"
    )
    if weather_mount_marker not in compose_text:
        raise ContractError("spread Compose must require the read-only weather runtime mount")
    if "./01_data:/app/01_data:ro" not in compose_text:
        raise ContractError("spread Compose must mount /app/01_data read-only")
    if not re.search(
        r"(?ms)^\s{2}spread-dashboard:\s*\n.*?^\s{4}build:\s*$", compose_text
    ):
        raise ContractError("spread Compose service must retain its build definition")

    dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
    for argument in (
        "MARKET_DATA_GIT_HEAD",
        "MARKET_DATA_GIT_TREE",
        "MARKET_DATA_RELEASE_ID",
        "MARKET_DATA_BUILD_TIME",
        "MARKET_DATA_SOURCE",
    ):
        if f"ARG {argument}" not in dockerfile_text:
            raise ContractError(f"Dockerfile is missing ARG {argument}")
    for label in (
        "org.opencontainers.image.revision",
        "org.opencontainers.image.version",
        "org.opencontainers.image.created",
        "org.opencontainers.image.source",
    ):
        if label not in dockerfile_text:
            raise ContractError(f"Dockerfile is missing OCI label {label}")
    if "/app/RELEASE.json" not in dockerfile_text:
        raise ContractError("Dockerfile must create /app/RELEASE.json")
    if "COPY 01_data " in dockerfile_text or "COPY 06_outputs " in dockerfile_text:
        raise ContractError("dynamic runtime data must not be copied into the image")

    dockerignore_rules = {
        line.strip()
        for line in dockerignore_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    for required_rule in (
        ".env",
        ".env.*",
        "**/.env",
        "**/.env.*",
        "01_data",
        "06_outputs",
        "10_logs",
        "*.xlsx",
        "!02_configs/historical_spread_config.xlsx",
    ):
        if required_rule not in dockerignore_rules:
            raise ContractError(f".dockerignore is missing {required_rule}")

    deploy_text = deploy_path.read_text(encoding="utf-8")
    rollback_text = rollback_path.read_text(encoding="utf-8")
    for description, script in (
        ("deploy", deploy_text),
        ("rollback", rollback_text),
    ):
        if any(
            flag not in script
            for flag in ("--no-build", "--pull never", "--no-deps", "--force-recreate")
        ):
            raise ContractError(
                f"{description} command must include the sealed no-build switch flags"
            )
        if "--env-file" not in script or "--deployment-plan" not in script:
            raise ContractError(
                f"{description} command must require the sealed production environment "
                "and deployment plan"
            )
        if 'plan["production_service"]' not in script:
            raise ContractError(
                f"{description} command must read production_service from the "
                "verified deployment plan"
            )
        if not re.search(
            r'up\s+-d\s+--no-build\s+--pull\s+never\s+--no-deps\s+'
            r'--force-recreate\s+"\$\{production_service\}"',
            script,
        ):
            raise ContractError(
                f"{description} command service scope must come from the verified "
                "deployment plan"
            )
        if re.search(
            r"(?is)\bup\b[^\n]*(?:usda-dashboard|oil-world|oil_world)",
            script,
        ):
            raise ContractError(
                f"{description} command must not switch USDA or Oil World services"
            )
        if re.search(r"\bdocker\s+(?:image\s+)?(?:build|tag)\b", script):
            raise ContractError(f"{description} script must not build or retag images")
        if ".Config.Image" in script:
            raise ContractError(
                f"{description} script must not derive a target from Config.Image"
            )
        if re.search(r"(?i)(?::|=)(latest|new)(?:\s|['\"]|$)", script):
            raise ContractError(
                f"{description} script must not use latest or new image tags"
            )
        if "transition_production_env.py" not in script:
            raise ContractError(
                f"{description} script must use the governed production env transition"
            )


def _resolve_schema_ref(root_schema: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise ContractError(f"unsupported schema reference: {ref}")
    current: Any = root_schema
    for part in ref[2:].split("/"):
        if not isinstance(current, dict) or part not in current:
            raise ContractError(f"invalid schema reference: {ref}")
        current = current[part]
    if not isinstance(current, dict):
        raise ContractError(f"schema reference is not an object: {ref}")
    return current


def _matches_json_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ContractError(f"unsupported schema type: {expected}")


def validate_against_schema(
    value: Any,
    schema: Mapping[str, Any],
    *,
    root_schema: Mapping[str, Any] | None = None,
    path: str = "$",
) -> None:
    root = root_schema or schema
    if "$ref" in schema:
        validate_against_schema(
            value,
            _resolve_schema_ref(root, schema["$ref"]),
            root_schema=root,
            path=path,
        )
        return

    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        if not any(_matches_json_type(value, item) for item in expected_type):
            raise ContractError(f"{path} must be one of JSON types {expected_type}")
    elif expected_type and not _matches_json_type(value, expected_type):
        raise ContractError(f"{path} must be JSON type {expected_type}")
    if "const" in schema and value != schema["const"]:
        raise ContractError(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ContractError(f"{path} must be one of {schema['enum']!r}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ContractError(f"{path} is shorter than minLength")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ContractError(f"{path} does not match required pattern")

    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ContractError(f"{path} is below minimum")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ContractError(f"{path} has too few items")
        if schema.get("uniqueItems") and len({json.dumps(i, sort_keys=True) for i in value}) != len(value):
            raise ContractError(f"{path} items must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                validate_against_schema(
                    item,
                    item_schema,
                    root_schema=root,
                    path=f"{path}[{index}]",
                )

    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise ContractError(f"{path}.{key} is required")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ContractError(f"{path} contains unexpected keys: {extras}")
        for key, item in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                validate_against_schema(
                    item,
                    child_schema,
                    root_schema=root,
                    path=f"{path}.{key}",
                )


def load_schema(schema_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load release schema {schema_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError("release schema must be a JSON object")
    return payload


def artifact_manifest_path(target_path: Path, artifact_type: str) -> Path:
    try:
        filename = ARTIFACT_MANIFEST_FILENAMES[artifact_type]
    except KeyError as exc:
        raise ContractError(f"unsupported artifact type: {artifact_type}") from exc
    return target_path.resolve().with_name(filename)


def create_artifact_manifest(
    target_path: Path,
    *,
    artifact_type: str,
    target_schema_version: str,
    release_id: str,
    git_commit: str,
    git_tree: str,
    image_id: str,
    runtime_git_commit: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    target_path = target_path.resolve()
    expected_target = ARTIFACT_TARGET_FILENAMES.get(artifact_type)
    if expected_target is None:
        raise ContractError(f"unsupported artifact type: {artifact_type}")
    if target_path.name != expected_target:
        raise ContractError(
            f"{artifact_type} manifest target must be {expected_target}, "
            f"got {target_path.name}"
        )
    expected_schema_version = ARTIFACT_TARGET_SCHEMA_VERSIONS[artifact_type]
    accepted_target_versions = {expected_schema_version}
    if artifact_type == "candidate_result":
        accepted_target_versions.add(LEGACY_CANDIDATE_RESULT_SCHEMA_VERSION)
    elif artifact_type == "deployment_plan":
        accepted_target_versions.add(LEGACY_DEPLOYMENT_PLAN_SCHEMA_VERSION)
    elif artifact_type == "deployment_result":
        accepted_target_versions.add(LEGACY_DEPLOYMENT_RESULT_SCHEMA_VERSION)
    if target_schema_version not in accepted_target_versions:
        raise ContractError(
            f"{artifact_type} manifest target schema version must be "
            f"{expected_schema_version}, got {target_schema_version}"
        )
    validate_full_git_commit(git_commit)
    validate_full_git_commit(git_tree)
    validate_image_id(image_id)
    timestamp = generated_at or datetime.now(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )
    validate_build_time(timestamp)
    try:
        target_payload = json.loads(target_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read {artifact_type} target evidence: {exc}") from exc
    evidence_key = (
        "formal_container_identity_contract"
        if artifact_type == "release"
        else "formal_containers"
    )
    formal_evidence = target_payload.get(evidence_key)
    if not isinstance(formal_evidence, dict):
        raise ContractError(f"{artifact_type} target is missing {evidence_key}")
    manifest = {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
        "artifact_type": artifact_type,
        "target_file": target_path.name,
        "target_sha256": hash_file(target_path),
        "target_size_bytes": target_path.stat().st_size,
        "target_schema_version": target_schema_version,
        "generated_at": timestamp,
        "release_id": release_id,
        "git_commit": git_commit,
        "git_tree": git_tree,
        "image_id": image_id,
        "formal_evidence_sha256": formal_evidence_sha256(formal_evidence),
    }
    runtime_mounts = target_payload.get("runtime_mounts")
    if artifact_type == "candidate_result" and runtime_mounts:
        manifest["runtime_mounts"] = validate_runtime_mount_evidence(runtime_mounts)
    measured_artifact = artifact_type in {"candidate_result", "deployment_result"}
    if measured_artifact:
        if runtime_git_commit is None:
            raise ContractError(
                f"{artifact_type} manifest requires measured runtime_git_commit"
            )
        measured_commit = validate_full_git_commit(runtime_git_commit)
        if measured_commit != git_commit:
            raise ContractError(
                f"{artifact_type} manifest runtime_git_commit mismatch: "
                "must equal git_commit"
            )
        manifest["runtime_git_commit"] = measured_commit
    elif runtime_git_commit is not None:
        raise ContractError(
            f"{artifact_type} manifest must not contain runtime_git_commit"
        )
    validate_against_schema(
        manifest,
        load_schema(Path(__file__).with_name("artifact_manifest.schema.json")),
    )
    return manifest


def validate_artifact_manifest(
    target_path: Path,
    manifest_path: Path,
    *,
    artifact_type: str,
    expected_release_id: str | None = None,
    expected_git_commit: str | None = None,
    expected_git_tree: str | None = None,
    expected_image_id: str | None = None,
    expected_runtime_git_commit: str | None = None,
) -> dict[str, Any]:
    target_path = target_path.resolve()
    manifest_path = manifest_path.resolve()
    expected_manifest_path = artifact_manifest_path(target_path, artifact_type)
    if manifest_path != expected_manifest_path:
        raise ContractError(
            f"{artifact_type} manifest must be {expected_manifest_path.name}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(
            f"cannot load {artifact_type} manifest {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise ContractError(f"{artifact_type} manifest must be a JSON object")
    validate_against_schema(
        manifest,
        load_schema(Path(__file__).with_name("artifact_manifest.schema.json")),
    )
    if manifest.get("artifact_type") != artifact_type:
        raise ContractError(f"{artifact_type} manifest artifact_type mismatch")
    expected_target = ARTIFACT_TARGET_FILENAMES.get(artifact_type)
    if expected_target is None:
        raise ContractError(f"unsupported artifact type: {artifact_type}")
    if target_path.name != expected_target:
        raise ContractError(
            f"{artifact_type} manifest target must be {expected_target}, "
            f"got {target_path.name}"
        )
    expected_schema_version = ARTIFACT_TARGET_SCHEMA_VERSIONS[artifact_type]
    accepted_target_versions = {expected_schema_version}
    if artifact_type == "candidate_result":
        accepted_target_versions.add(LEGACY_CANDIDATE_RESULT_SCHEMA_VERSION)
    elif artifact_type == "deployment_plan":
        accepted_target_versions.add(LEGACY_DEPLOYMENT_PLAN_SCHEMA_VERSION)
    elif artifact_type == "deployment_result":
        accepted_target_versions.add(LEGACY_DEPLOYMENT_RESULT_SCHEMA_VERSION)
    if manifest.get("target_schema_version") not in accepted_target_versions:
        raise ContractError(
            f"{artifact_type} manifest target_schema_version mismatch"
        )
    measured_artifact = artifact_type in {
        "candidate_result",
        "deployment_result",
    }
    runtime_git_commit = manifest.get("runtime_git_commit")
    if measured_artifact:
        if runtime_git_commit is None:
            raise ContractError(
                f"{artifact_type} manifest is missing runtime_git_commit"
            )
        measured_commit = validate_full_git_commit(runtime_git_commit)
        if measured_commit != manifest.get("git_commit"):
            raise ContractError(
                f"{artifact_type} manifest runtime_git_commit mismatch: "
                "must equal git_commit"
            )
    elif runtime_git_commit is not None:
        raise ContractError(
            f"{artifact_type} manifest must not contain runtime_git_commit"
        )
    if manifest.get("target_file") != target_path.name:
        raise ContractError(f"{artifact_type} manifest target filename mismatch")
    if manifest.get("target_sha256") != hash_file(target_path):
        raise ContractError(f"{artifact_type} artifact SHA-256 mismatch")
    if manifest.get("target_size_bytes") != target_path.stat().st_size:
        raise ContractError(f"{artifact_type} artifact byte size mismatch")
    try:
        target_payload = json.loads(target_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read {artifact_type} target evidence: {exc}") from exc
    evidence_key = (
        "formal_container_identity_contract"
        if artifact_type == "release"
        else "formal_containers"
    )
    formal_evidence = target_payload.get(evidence_key)
    if not isinstance(formal_evidence, dict):
        raise ContractError(f"{artifact_type} target is missing {evidence_key}")
    if manifest.get("formal_evidence_sha256") != formal_evidence_sha256(
        formal_evidence
    ):
        raise ContractError(f"{artifact_type} formal evidence SHA-256 mismatch")
    target_runtime_mounts = target_payload.get("runtime_mounts", [])
    manifest_runtime_mounts = manifest.get("runtime_mounts", [])
    if artifact_type == "candidate_result":
        if validate_runtime_mount_evidence(manifest_runtime_mounts) != validate_runtime_mount_evidence(
            target_runtime_mounts
        ):
            raise ContractError("candidate_result runtime mount manifest mismatch")
    elif manifest_runtime_mounts:
        raise ContractError(f"{artifact_type} manifest must not contain runtime mounts")
    expected_identity = {
        "release_id": expected_release_id,
        "git_commit": expected_git_commit,
        "git_tree": expected_git_tree,
        "image_id": expected_image_id,
        "runtime_git_commit": expected_runtime_git_commit,
    }
    for key, expected in expected_identity.items():
        if expected is not None and manifest.get(key) != expected:
            raise ContractError(f"{artifact_type} manifest {key} mismatch")
    validate_build_time(manifest.get("generated_at"))
    return manifest


def load_verified_json_artifact(
    target_path: Path,
    *,
    artifact_type: str,
    expected_release_id: str | None = None,
    expected_git_commit: str | None = None,
    expected_git_tree: str | None = None,
    expected_image_id: str | None = None,
    expected_runtime_git_commit: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    target_path = target_path.resolve()
    artifact_manifest = validate_artifact_manifest(
        target_path,
        artifact_manifest_path(target_path, artifact_type),
        artifact_type=artifact_type,
        expected_release_id=expected_release_id,
        expected_git_commit=expected_git_commit,
        expected_git_tree=expected_git_tree,
        expected_image_id=expected_image_id,
        expected_runtime_git_commit=expected_runtime_git_commit,
    )
    try:
        payload = json.loads(target_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(
            f"cannot load verified {artifact_type} artifact {target_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ContractError(f"{artifact_type} artifact must be a JSON object")
    if payload.get("schema_version") != artifact_manifest["target_schema_version"]:
        raise ContractError(f"{artifact_type} target schema_version mismatch")
    return payload, artifact_manifest


def _serialize_json_payload(payload: Mapping[str, Any]) -> bytes:
    try:
        text = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(f"JSON payload cannot be serialized: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ContractError("JSON artifact must serialize to an object")
    return text.encode("utf-8")


def _write_bytes_exclusive(path: Path, content: bytes, *, description: str) -> None:
    path = path.resolve()
    if not path.parent.is_dir():
        raise ContractError(f"{description} parent directory is missing: {path.parent}")
    temp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temp_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            written = handle.write(content)
            if written != len(content):
                raise OSError(
                    f"short write: expected {len(content)} bytes, wrote {written}"
                )
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temp_path.chmod(0o444)
        try:
            os.link(temp_path, path)
        except FileExistsError as exc:
            raise ContractError(
                f"{description} already exists and will not be overwritten: {path}"
            ) from exc
        except OSError as exc:
            raise ContractError(
                f"cannot publish {description} exclusively at {path}: {exc}"
            ) from exc
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError(f"cannot write {description} {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temp_path.exists():
            temp_path.unlink()


def _write_json_exclusive(
    path: Path,
    payload: Mapping[str, Any],
    *,
    description: str,
) -> None:
    content = _serialize_json_payload(payload)
    _write_bytes_exclusive(path, content, description=description)


def write_artifact_manifest(
    target_path: Path,
    *,
    artifact_type: str,
    target_schema_version: str,
    release_id: str,
    git_commit: str,
    git_tree: str,
    image_id: str,
    runtime_git_commit: str | None = None,
) -> Path:
    target_path = target_path.resolve()
    output = artifact_manifest_path(target_path, artifact_type)
    manifest = create_artifact_manifest(
        target_path,
        artifact_type=artifact_type,
        target_schema_version=target_schema_version,
        release_id=release_id,
        git_commit=git_commit,
        git_tree=git_tree,
        image_id=image_id,
        runtime_git_commit=runtime_git_commit,
    )
    _write_json_exclusive(
        output,
        manifest,
        description=f"{artifact_type} manifest",
    )
    validate_artifact_manifest(
        target_path,
        output,
        artifact_type=artifact_type,
        expected_release_id=release_id,
        expected_git_commit=git_commit,
        expected_git_tree=git_tree,
        expected_image_id=image_id,
        expected_runtime_git_commit=runtime_git_commit,
    )
    return output


def write_json_artifact(
    path: Path,
    payload: Mapping[str, Any],
    *,
    artifact_type: str,
    release_id: str,
    git_commit: str,
    git_tree: str,
    image_id: str,
    runtime_git_commit: str | None = None,
) -> tuple[Path, Path]:
    path = path.resolve()
    manifest_path = artifact_manifest_path(path, artifact_type)
    if manifest_path.exists():
        raise ContractError(
            f"{artifact_type} manifest already exists and will not be overwritten: "
            f"{manifest_path}"
        )
    if not path.parent.is_dir():
        raise ContractError(f"{artifact_type} parent directory is missing: {path.parent}")
    schema_filename = ARTIFACT_TARGET_SCHEMA_FILENAMES.get(artifact_type)
    if schema_filename is None:
        raise ContractError(f"unsupported artifact type: {artifact_type}")
    validate_against_schema(
        payload,
        load_schema(Path(__file__).with_name(schema_filename)),
    )
    installed_target = False
    try:
        _write_json_exclusive(
            path,
            payload,
            description=f"{artifact_type} artifact",
        )
        installed_target = True
        output_manifest = write_artifact_manifest(
            path,
            artifact_type=artifact_type,
            target_schema_version=str(payload.get("schema_version", "")),
            release_id=release_id,
            git_commit=git_commit,
            git_tree=git_tree,
            image_id=image_id,
            runtime_git_commit=runtime_git_commit,
        )
        return path, output_manifest
    except Exception:
        if installed_target and path.exists() and not manifest_path.exists():
            path.unlink()
        raise


def validate_manifest(manifest: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    validate_against_schema(manifest, schema)
    schema_version = manifest.get("schema_version")
    if schema_version not in {"1.0.0", "2.0.0", "2.1.0", SCHEMA_VERSION}:
        raise ContractError(f"unsupported release schema_version: {schema_version!r}")
    commit = validate_full_git_commit(manifest.get("git_commit"))
    tree = validate_full_git_commit(manifest.get("git_tree"))
    if tree == commit:
        raise ContractError("git_tree must identify a tree object, not the commit object")
    release_id = str(manifest.get("release_id", ""))
    validate_release_id(release_id, commit, str(manifest.get("build_time", "")))
    validate_release_image_ref(manifest.get("image_ref"), release_id)
    validate_image_id(manifest.get("image_id"))
    validate_full_git_commit(manifest.get("image_oci_revision"))
    if manifest["image_oci_revision"] != commit:
        raise ContractError("image_oci_revision must exactly equal git_commit")
    validate_sha256(
        manifest.get("image_release_json_sha256"),
        "image_release_json_sha256",
    )
    if manifest.get("compose_project") != COMPOSE_PROJECT:
        raise ContractError(f"compose_project must be {COMPOSE_PROJECT}")
    if manifest.get("compose_files") != ["docker-compose.yml"]:
        raise ContractError("compose_files must be exactly ['docker-compose.yml']")
    if schema_version == SCHEMA_VERSION:
        try:
            readiness_policy = validate_readiness_policy(
                manifest.get("readiness_policy") or {}
            )
        except ValueError as exc:
            raise ContractError(f"invalid readiness_policy: {exc}") from exc
        if readiness_policy != DEFAULT_READINESS_POLICY:
            raise ContractError(
                "readiness_policy must exactly equal the versioned deployment default"
            )
        if (
            manifest.get("formal_container_identity_contract")
            != FORMAL_CONTAINER_IDENTITY_CONTRACT
        ):
            raise ContractError("formal container identity contract mismatch")
    if schema_version == "1.0.0":
        validate_sha256(
            manifest.get("compose_config_sha256"),
            "compose_config_sha256",
        )
        if (
            "compose_template_sha256" in manifest
            or "runtime_environment_contract" in manifest
        ):
            raise ContractError(
                "legacy release manifest cannot contain version 2 environment fields"
            )
    else:
        if "compose_config_sha256" in manifest:
            raise ContractError(
                "release schema version 2 must not seal a rendered Compose hash"
            )
        validate_sha256(
            manifest.get("compose_template_sha256"),
            "compose_template_sha256",
        )
        if manifest.get("runtime_environment_contract") != RUNTIME_ENVIRONMENT_CONTRACT:
            raise ContractError(
                "runtime_environment_contract does not match the fixed contract"
            )
        candidate_user_url_environment(
            manifest.get("candidate_user_url_environment") or {}
        )
        weather_facts = weather_candidate_facts(
            manifest.get("weather_candidate_mode")
        )
        for key, expected_value in weather_facts.items():
            if manifest.get(key) != expected_value:
                raise ContractError(f"release manifest {key} mismatch")
        if manifest.get("weather_runtime_contract") != weather_runtime_contract(
            PRODUCTION_WEATHER_RUNTIME_DIR,
            weather_facts["weather_candidate_mode"],
        ):
            raise ContractError("release manifest weather runtime contract mismatch")
    validate_sha256(manifest.get("dockerfile_sha256"), "dockerfile_sha256")
    validate_sha256(manifest.get("dockerignore_sha256"), "dockerignore_sha256")
    validate_sha256(manifest.get("required_config_sha256"), "required_config_sha256")
    candidate = manifest.get("candidate_container_name")
    if (
        not isinstance(candidate, str)
        or not SAFE_CONTAINER_RE.fullmatch(candidate)
        or candidate == PRODUCTION_CONTAINER
    ):
        raise ContractError(
            "candidate_container_name must be safe and distinct from production"
        )
    validate_rollback_image_ref(manifest.get("rollback_image_ref"))
    validate_image_id(manifest.get("rollback_image_id"), "rollback_image_id")
    formal_git_commit = validate_full_git_commit(manifest.get("formal_git_commit"))
    if formal_git_commit == commit:
        raise ContractError("formal_git_commit must identify the previous production code")
    if manifest["rollback_image_ref"] == manifest["image_ref"]:
        raise ContractError("rollback_image_ref must differ from the release image")
    if manifest["rollback_image_id"] == manifest["image_id"]:
        raise ContractError("rollback_image_id must differ from the release image ID")

    baseline = manifest.get("data_baseline", {})
    if baseline.get("kind") != "deployment-time":
        raise ContractError("data_baseline.kind must be deployment-time")
    if baseline.get("mutable_after_deployment") is not True:
        raise ContractError("data baseline must allow later scheduled data changes")
    datasets = baseline.get("datasets", [])
    names = [item.get("name") for item in datasets if isinstance(item, dict)]
    required_names = [spec.name for spec in DATA_SPECS]
    if sorted(names) != sorted(required_names):
        raise ContractError(
            f"data_baseline must contain exactly these datasets: {required_names}"
        )
    validate_build_time(baseline.get("captured_at"))
    by_name = {item["name"]: item for item in datasets}
    for spec in DATA_SPECS:
        item = by_name[spec.name]
        if item.get("delivery") != "bind_mount" or item.get("persistence") != "host":
            raise ContractError(
                f"data baseline {item.get('name')} must be host bind-mounted"
            )
        normalized_host_path = str(item.get("host_path", "")).replace("\\", "/")
        if not normalized_host_path.endswith("/" + spec.relative_host_path):
            raise ContractError(
                f"data baseline {spec.name} host path does not match the fixed contract"
            )
        if item.get("container_path") != spec.container_path:
            raise ContractError(
                f"data baseline {spec.name} container path does not match the fixed contract"
            )
        if item.get("filename") != Path(spec.relative_host_path).name:
            raise ContractError(f"data baseline {spec.name} filename mismatch")
        if item.get("primary_key") != list(spec.primary_key):
            raise ContractError(f"data baseline {spec.name} primary key mismatch")
        if spec.name == "spread" and item.get("primary_key_null_rows") != 0:
            raise ContractError(
                f"data baseline {spec.name} primary key contains null values"
            )
        if spec.name == "spread" and item.get("duplicate_rows_on_key") != 0:
            raise ContractError(
                f"data baseline {spec.name} contains duplicate primary keys"
            )
        if item.get("update_task") != spec.update_task:
            raise ContractError(f"data baseline {spec.name} update task mismatch")
        if item.get("writes_to") != "host_through_bind_mount":
            raise ContractError(f"data baseline {spec.name} write target mismatch")
        validate_build_time(item.get("mtime"))
        validate_sha256(item.get("sha256"), f"{item.get('name')} data sha256")
    assert_no_sensitive_values(manifest, "release manifest")


def parse_release_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ContractError(f"release.env is missing: {path}")
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line:
            continue
        if raw_line.startswith("#") or "=" not in raw_line:
            raise ContractError(f"release.env line {number} is not a KEY=value entry")
        key, value = raw_line.split("=", 1)
        if key not in RELEASE_ENV_KEYS or key in values:
            raise ContractError(f"release.env contains invalid or duplicate key {key!r}")
        if not value or any(char.isspace() for char in value):
            raise ContractError(f"release.env value for {key} is empty or unsafe")
        values[key] = value
    if set(values) != set(RELEASE_ENV_KEYS):
        raise ContractError(
            f"release.env must contain exactly {list(RELEASE_ENV_KEYS)}"
        )
    assert_no_sensitive_values(values, "release.env")
    return values


def validate_release_env(
    environment: Mapping[str, str], manifest: Mapping[str, Any]
) -> None:
    expected = {
        "RELEASE_ID": manifest["release_id"],
        "SPREAD_IMAGE": manifest["image_ref"],
        "EXPECTED_IMAGE_ID": manifest["image_id"],
        "EXPECTED_GIT_COMMIT": manifest["git_commit"],
    }
    if dict(environment) != expected:
        raise ContractError("release.env does not exactly match release.json")


def validate_production_url(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ContractError(f"{field} must be a non-empty URL without whitespace")
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ContractError(
            f"{field} must be an HTTP(S) URL without credentials, query, or fragment"
        )
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "localhost.localdomain"}:
        raise ContractError(f"{field} must not use localhost in production")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if address.is_loopback or address.is_unspecified:
            raise ContractError(f"{field} must not use a local address in production")
    return value


def candidate_user_url_environment(
    production_environment: Mapping[str, str],
) -> dict[str, str]:
    """Extract the browser-facing URLs approved for a sealed candidate.

    This deliberately validates the values again at the boundary where they enter
    the candidate.  Internal localhost health endpoints are separate inputs and
    are never part of this mapping.
    """
    values: dict[str, str] = {}
    for key in RUNTIME_URL_KEYS:
        try:
            value = production_environment[key]
        except KeyError as exc:
            raise ContractError(f"production environment is missing {key}") from exc
        values[key] = validate_production_url(value, key)
    return values


def validate_manifest_production_environment(
    production_environment: Mapping[str, str]
) -> None:
    """Validate the production environment before it becomes a candidate URL source."""
    if set(production_environment) != set(PRODUCTION_ENV_KEYS):
        raise ContractError(
            f"production environment must contain exactly {list(PRODUCTION_ENV_KEYS)}"
        )
    candidate_user_url_environment(production_environment)
    validate_weather_runtime_dir(
        production_environment[WEATHER_RUNTIME_ENV_KEY],
        expected=PRODUCTION_WEATHER_RUNTIME_DIR,
    )


def validate_weather_runtime_dir(value: Any, *, expected: str | None = None) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ContractError("WEATHER_RUNTIME_CURRENT_DIR is empty or unsafe")
    if not value.startswith("/") or "\\" in value:
        raise ContractError("WEATHER_RUNTIME_CURRENT_DIR must be an absolute POSIX path")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", value):
        raise ContractError("WEATHER_RUNTIME_CURRENT_DIR contains unsafe characters")
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components[1:]):
        raise ContractError("WEATHER_RUNTIME_CURRENT_DIR must not contain path traversal")
    if value.lower().endswith(".sql"):
        raise ContractError("WEATHER_RUNTIME_CURRENT_DIR must not reference a raw SQL file")
    if expected is not None and value != expected:
        raise ContractError(
            "WEATHER_RUNTIME_CURRENT_DIR does not match the fixed production runtime path"
        )
    return value


def validate_weather_candidate_mode(value: Any) -> str:
    if value not in WEATHER_CANDIDATE_MODES:
        raise ContractError(
            "weather_candidate_mode must be exactly 'current' or 'next'"
        )
    return str(value)


def weather_candidate_source(weather_candidate_mode: Any) -> str:
    mode = validate_weather_candidate_mode(weather_candidate_mode)
    return WEATHER_CANDIDATE_RUNTIME_DIRS[mode]


def weather_candidate_data_dir(weather_candidate_mode: Any) -> str:
    mode = validate_weather_candidate_mode(weather_candidate_mode)
    return WEATHER_CANDIDATE_DATA_DIRS[mode]


def weather_candidate_facts(weather_candidate_mode: Any) -> dict[str, Any]:
    """Return sealed, non-user-configurable weather release facts.

    The release tooling never creates or mutates weather data directories.  A
    `next` candidate therefore records that a separately validated data
    promotion is required; a `current` candidate is explicitly code-only.
    """
    mode = validate_weather_candidate_mode(weather_candidate_mode)
    promotion_required = mode == WEATHER_CANDIDATE_MODE_NEXT
    return {
        "weather_candidate_mode": mode,
        "weather_candidate_source": weather_candidate_source(mode),
        "weather_candidate_data_dir": weather_candidate_data_dir(mode),
        "weather_runtime_current_dir": PRODUCTION_WEATHER_RUNTIME_DIR,
        "weather_data_promotion_required": promotion_required,
        "weather_data_changed": promotion_required,
        "processed_next_created": False,
        "processed_current_modified": False,
    }


def weather_runtime_contract(
    production_host_path: str,
    weather_candidate_mode: Any,
) -> dict[str, Any]:
    validate_weather_runtime_dir(production_host_path, expected=PRODUCTION_WEATHER_RUNTIME_DIR)
    validate_weather_candidate_mode(weather_candidate_mode)
    return {
        **copy.deepcopy(WEATHER_RUNTIME_MOUNT_CONTRACT),
        "production_host_path": production_host_path,
    }


def parse_production_env(path: Path) -> dict[str, str]:
    if not path.is_absolute():
        raise ContractError("production environment file path must be absolute")
    if not path.is_file():
        raise ContractError(f"production environment file is missing: {path}")
    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise ContractError(
                "production environment file must not be accessible by group or others"
            )
        if path.stat().st_uid != os.geteuid():
            raise ContractError(
                "production environment file must be owned by the deployment user"
            )
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line:
            continue
        if raw_line.startswith("#") or "=" not in raw_line:
            raise ContractError(
                f"production environment line {number} is not a KEY=value entry"
            )
        key, value = raw_line.split("=", 1)
        if key not in PRODUCTION_ENV_KEYS or key in values:
            raise ContractError(
                f"production environment contains invalid or duplicate key {key!r}"
            )
        if not value or any(char.isspace() for char in value):
            raise ContractError(
                f"production environment value for {key} is empty or unsafe"
            )
        values[key] = value
    if set(values) != set(PRODUCTION_ENV_KEYS):
        raise ContractError(
            f"production environment must contain exactly {list(PRODUCTION_ENV_KEYS)}"
        )
    assert_no_sensitive_values(values, "production environment")
    validate_weather_runtime_dir(
        values[WEATHER_RUNTIME_ENV_KEY], expected=PRODUCTION_WEATHER_RUNTIME_DIR
    )
    return values


def validate_production_env(
    environment: Mapping[str, str],
    manifest: Mapping[str, Any],
) -> None:
    validate_current_production_env(environment)
    if environment["SPREAD_IMAGE"] != manifest["image_ref"]:
        raise ContractError("production SPREAD_IMAGE does not match release image_ref")
    if environment["MARKET_DATA_GIT_HEAD"] != manifest["git_commit"]:
        raise ContractError(
            "production MARKET_DATA_GIT_HEAD does not match release git_commit"
        )


def validate_current_production_env(
    environment: Mapping[str, str],
) -> dict[str, str]:
    """Validate a production env without pretending it is already the target release."""
    if set(environment) != set(PRODUCTION_ENV_KEYS):
        raise ContractError(
            f"production environment must contain exactly {list(PRODUCTION_ENV_KEYS)}"
        )
    validate_rollback_image_ref(environment["SPREAD_IMAGE"])
    validate_full_git_commit(environment["MARKET_DATA_GIT_HEAD"])
    validate_production_url(
        environment["USDA_DASHBOARD_URL"],
        "USDA_DASHBOARD_URL",
    )
    validate_production_url(
        environment["OIL_WORLD_DASHBOARD_URL"],
        "OIL_WORLD_DASHBOARD_URL",
    )
    validate_weather_runtime_dir(
        environment[WEATHER_RUNTIME_ENV_KEY], expected=PRODUCTION_WEATHER_RUNTIME_DIR
    )
    if environment[WEATHER_DATA_DIR_ENV_KEY] != WEATHER_CONTAINER_CURRENT_PATH:
        raise ContractError(
            f"{WEATHER_DATA_DIR_ENV_KEY} must equal {WEATHER_CONTAINER_CURRENT_PATH}"
        )
    assert_no_sensitive_values(environment, "production environment")
    return dict(environment)


def target_production_environment(
    current_environment: Mapping[str, str],
    manifest: Mapping[str, Any],
) -> dict[str, str]:
    target = validate_current_production_env(current_environment)
    target["SPREAD_IMAGE"] = str(manifest["image_ref"])
    target["MARKET_DATA_GIT_HEAD"] = str(manifest["git_commit"])
    validate_production_env(target, manifest)
    return target


def render_production_env(environment: Mapping[str, str]) -> str:
    values = validate_current_production_env(environment)
    return "".join(f"{key}={values[key]}\n" for key in PRODUCTION_ENV_KEYS)


def production_env_sha256(environment: Mapping[str, str]) -> str:
    return hash_text(render_production_env(environment))


def _replace_production_env_atomically(
    path: Path,
    environment: Mapping[str, str],
) -> None:
    """Replace the production env in-place while preserving owner and mode."""
    path = path.resolve()
    try:
        original = path.stat()
    except OSError as exc:
        raise ContractError(f"cannot stat production environment {path}: {exc}") from exc
    rendered = render_production_env(environment)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(original.st_mode))
        if os.name == "posix":
            os.chown(temporary, original.st_uid, original.st_gid)
        os.replace(temporary, path)
        temporary = None
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise ContractError(f"cannot atomically replace production environment: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def transition_production_env(
    deployment_plan: Mapping[str, Any],
    *,
    to_target: bool,
) -> dict[str, Any]:
    """Apply one sealed env transition, rejecting drift in either direction."""
    path = Path(str(deployment_plan.get("production_env_file", ""))).resolve()
    current = deployment_plan.get("current_production")
    target = deployment_plan.get("target_release")
    if not isinstance(current, dict) or not isinstance(target, dict):
        raise ContractError("deployment plan does not contain a sealed env transition")
    source = current if to_target else target
    destination = target if to_target else current
    source_environment = source.get("environment")
    destination_environment = destination.get("environment")
    if not isinstance(source_environment, dict) or not isinstance(
        destination_environment, dict
    ):
        raise ContractError("deployment plan env transition is incomplete")
    actual_sha256 = hash_file(path)
    destination_sha256 = str(destination.get("env_sha256", ""))
    if actual_sha256 == destination_sha256:
        if parse_production_env(path) != destination_environment:
            raise ContractError("production env hash matched but sealed values changed")
        return {
            "status": "already-target" if to_target else "already-rollback",
            "production_env_file": str(path),
            "production_env_sha256": actual_sha256,
        }
    if actual_sha256 != source.get("env_sha256"):
        raise ContractError(
            "production environment drifted from the sealed transition baseline"
        )
    if parse_production_env(path) != source_environment:
        raise ContractError("production environment values drifted from the sealed baseline")
    _replace_production_env_atomically(path, destination_environment)
    if hash_file(path) != destination_sha256:
        raise ContractError("production environment transition SHA-256 mismatch")
    if parse_production_env(path) != destination_environment:
        raise ContractError("production environment transition values mismatch")
    return {
        "status": "target-applied" if to_target else "rollback-restored",
        "production_env_file": str(path),
        "production_env_sha256": destination_sha256,
    }


def _normalize_project_path(value: Any, project_root: Path) -> Any:
    if not isinstance(value, str):
        return value
    normalized = value.replace("\\", "/").rstrip("/")
    root = str(project_root.resolve()).replace("\\", "/").rstrip("/")
    if normalized == root:
        return "$PROJECT_ROOT"
    if normalized.startswith(root + "/"):
        return "$PROJECT_ROOT/" + normalized[len(root) + 1 :]
    return value


def _sorted_json_values(values: Any) -> Any:
    if isinstance(values, list):
        return sorted(
            values,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    return values


def _compose_spread_semantics(
    compose: Mapping[str, Any],
    project_root: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    services = compose.get("services")
    if not isinstance(services, dict):
        raise ContractError("Compose services are missing")
    service = services.get(COMPOSE_SERVICE)
    if not isinstance(service, dict):
        raise ContractError(f"Compose service {COMPOSE_SERVICE} is missing")
    normalized_service = copy.deepcopy(service)
    environment = normalized_service.get("environment") or {}
    if not isinstance(environment, dict):
        raise ContractError("spread Compose environment must be an object")
    runtime_environment: dict[str, str] = {}
    for key in RUNTIME_URL_KEYS:
        value = environment.get(key)
        if not isinstance(value, str) or not value:
            raise ContractError(f"spread Compose environment is missing {key}")
        runtime_environment[key] = value
    weather_data_dir = environment.get(WEATHER_DATA_DIR_ENV_KEY)
    if weather_data_dir not in WEATHER_CANDIDATE_DATA_DIRS.values():
        raise ContractError("spread Compose WEATHER_DATA_DIR is not a permitted runtime directory")
    runtime_environment[WEATHER_DATA_DIR_ENV_KEY] = weather_data_dir
    other_environment = {
        key: value
        for key, value in environment.items()
        if key not in (*RUNTIME_URL_KEYS, WEATHER_DATA_DIR_ENV_KEY)
    }

    build = normalized_service.get("build")
    normalized_build: Any = build
    if isinstance(build, dict):
        normalized_build = {
            **build,
            "context": _normalize_project_path(build.get("context"), project_root),
        }
        normalized_service["build"] = normalized_build

    volumes = normalized_service.get("volumes") or []
    if not isinstance(volumes, list):
        raise ContractError("spread Compose volumes must be an array")
    normalized_volumes: list[Any] = []
    for volume in volumes:
        if not isinstance(volume, dict):
            raise ContractError("spread Compose volume entries must be objects")
        normalized_source = _normalize_project_path(volume.get("source"), project_root)
        if volume.get("target") == WEATHER_CONTAINER_PATH:
            normalized_source = "$WEATHER_RUNTIME_CURRENT_DIR"
        normalized_volumes.append({**volume, "source": normalized_source})

    normalized_service["volumes"] = _sorted_json_values(normalized_volumes)
    normalized_service["ports"] = _sorted_json_values(
        normalized_service.get("ports") or []
    )
    normalized_service["environment"] = other_environment
    return normalized_service, runtime_environment


def _validate_formal_spread_semantics(
    compose: Mapping[str, Any],
    project_root: Path,
    expected_image_ref: str,
    expected_git_commit: str,
    expected_weather_runtime_dir: str,
) -> None:
    service = compose["services"][COMPOSE_SERVICE]
    if service.get("image") != expected_image_ref:
        raise ContractError("formal Compose image does not match the release image_ref")
    if service.get("container_name") != PRODUCTION_CONTAINER:
        raise ContractError("formal spread container_name changed")
    if service.get("restart") != "unless-stopped":
        raise ContractError("formal spread restart policy changed")
    environment = service.get("environment")
    if (
        not isinstance(environment, dict)
        or environment.get("MARKET_DATA_GIT_HEAD") != expected_git_commit
    ):
        raise ContractError(
            "formal spread runtime MARKET_DATA_GIT_HEAD does not match the release"
        )
    if (
        environment.get(PUBLIC_MARKET_DATA_RUNTIME_ENV_KEY)
        != PUBLIC_MARKET_DATA_CONTAINER_ROOT
    ):
        raise ContractError("formal Public Market Data runtime root is invalid")
    _validate_weather_runtime_compose(
        compose,
        expected_source=expected_weather_runtime_dir,
        expected_weather_data_dir=WEATHER_CONTAINER_CURRENT_PATH,
    )

    ports = service.get("ports") or []
    expected_port = {
        "mode": "ingress",
        "protocol": "tcp",
        "published": "8501",
        "target": 8501,
    }
    if ports != [expected_port]:
        raise ContractError("formal spread port mapping changed")

    volumes = service.get("volumes") or []
    actual_mounts: dict[str, tuple[str, str, bool]] = {}
    for volume in volumes:
        if not isinstance(volume, dict):
            raise ContractError("formal spread volume entry is invalid")
        target = volume.get("target")
        source = volume.get("source")
        if isinstance(target, str) and isinstance(source, str):
            actual_mounts[target] = (
                str(Path(source).resolve()),
                str(volume.get("type")),
                bool(volume.get("read_only", False)),
            )
    expected_mounts = {
        target: (
            str((project_root / relative).resolve()),
            "bind",
            PRODUCTION_DATA_MOUNT_READ_ONLY[target],
        )
        for relative, target in PRODUCTION_DATA_MOUNTS.items()
    }
    expected_mounts[WEATHER_CONTAINER_PATH] = (
        str(Path(expected_weather_runtime_dir).resolve()),
        "bind",
        True,
    )
    if actual_mounts != expected_mounts:
        raise ContractError("formal spread data mount contract changed")


def _semantic_sha256(value: Mapping[str, Any]) -> str:
    return hash_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def load_manifest_bundle(
    manifest_path: Path,
    env_path: Path,
    schema_path: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    manifest, _ = load_verified_json_artifact(
        manifest_path,
        artifact_type="release",
    )
    schema = load_schema(schema_path)
    validate_manifest(manifest, schema)
    validate_artifact_manifest(
        manifest_path,
        artifact_manifest_path(manifest_path, "release"),
        artifact_type="release",
        expected_release_id=manifest["release_id"],
        expected_git_commit=manifest["git_commit"],
        expected_git_tree=manifest["git_tree"],
        expected_image_id=manifest["image_id"],
    )
    environment = parse_release_env(env_path)
    validate_release_env(environment, manifest)
    validate_checksums(manifest_path.parent / "checksums.sha256", manifest_path, env_path)
    return manifest, environment


def validate_checksums(
    checksums_path: Path, manifest_path: Path, env_path: Path
) -> None:
    if not checksums_path.is_file():
        raise ContractError(f"checksums file is missing: {checksums_path}")
    recorded: dict[str, str] = {}
    for number, line in enumerate(
        checksums_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        match = re.fullmatch(r"([0-9a-f]{64})  (release\.json|release\.env)", line)
        if not match or match.group(2) in recorded:
            raise ContractError(f"invalid checksums.sha256 line {number}")
        recorded[match.group(2)] = match.group(1)
    if set(recorded) != {"release.json", "release.env"}:
        raise ContractError("checksums.sha256 must seal release.json and release.env")
    actual = {
        "release.json": hash_file(manifest_path),
        "release.env": hash_file(env_path),
    }
    if recorded != actual:
        raise ContractError("release bundle checksum mismatch")


def load_candidate_result(
    path: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    if not path.is_absolute():
        raise ContractError("candidate result path must be absolute")
    result, artifact_manifest = load_verified_json_artifact(
        path,
        artifact_type="candidate_result",
        expected_release_id=str(manifest["release_id"]),
        expected_git_commit=str(manifest["git_commit"]),
        expected_git_tree=str(manifest["git_tree"]),
        expected_image_id=str(manifest["image_id"]),
    )
    validate_candidate_result(
        result,
        manifest,
        load_schema(Path(__file__).with_name("candidate_result.schema.json")),
    )
    runtime_git_commit = result["identity"]["runtime_git_commit"]
    if artifact_manifest.get("runtime_git_commit") != runtime_git_commit:
        raise ContractError(
            "candidate_result manifest runtime_git_commit mismatch"
        )
    return result


def pending_candidate_gate(gate_id: str, earliest_business_date: str) -> dict[str, Any]:
    if gate_id != REAL_NIGHT_SESSION_CLOSE_GATE_ID:
        raise ContractError(f"unsupported candidate gate: {gate_id}")
    try:
        parsed_date = datetime.strptime(earliest_business_date, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ContractError("candidate gate earliest business date must be YYYY-MM-DD") from exc
    if parsed_date.weekday() >= 5:
        raise ContractError("candidate gate earliest business date must be a weekday")
    return {
        "gate_id": REAL_NIGHT_SESSION_CLOSE_GATE_ID,
        "status": "pending",
        "required_business_timezone": "Asia/Shanghai",
        "required_window_start": "08:30:00",
        "required_window_end_exclusive": "08:33:00",
        "earliest_expected_business_date": parsed_date.isoformat(),
        "blocks_production_promotion": True,
        "description": "Real AkShare night_session_close validation is pending.",
    }


def completed_candidate_gate(evidence: Mapping[str, Any]) -> dict[str, Any]:
    if evidence.get("gate_id") != REAL_NIGHT_SESSION_CLOSE_GATE_ID or evidence.get("status") != "completed":
        raise ContractError("completed candidate gate identity is invalid")
    business_date = evidence.get("business_date")
    try:
        parsed_date = datetime.strptime(str(business_date), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ContractError("completed gate business date must be YYYY-MM-DD") from exc
    if parsed_date.weekday() >= 5:
        raise ContractError("completed gate business date must be a weekday")
    captured_at = evidence.get("captured_at")
    captured = validate_build_time(captured_at)
    captured_local = captured.astimezone(ZoneInfo("Asia/Shanghai"))
    if captured_local.date() != parsed_date or not (
        (8, 30, 0) <= (
            captured_local.hour,
            captured_local.minute,
            captured_local.second,
        ) < (8, 33, 0)
    ):
        raise ContractError(
            "completed gate captured_at must be inside the target business date 08:30 window"
        )
    contracts = evidence.get("target_contracts")
    if (
        not isinstance(contracts, list)
        or not contracts
        or len(set(contracts)) != len(contracts)
        or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", item) for item in contracts)
    ):
        raise ContractError("completed gate target contracts are invalid")
    available = evidence.get("available_contracts")
    missing = evidence.get("missing_contracts")
    if (
        not isinstance(available, list)
        or not isinstance(missing, list)
        or not available
        or len(set(available)) != len(available)
        or len(set(missing)) != len(missing)
        or set(available) & set(missing)
        or set(available) | set(missing) != set(contracts)
    ):
        raise ContractError("completed gate contract availability partition is invalid")
    capture_status = evidence.get("capture_status")
    expected_status = "success" if not missing else "passed_with_incomplete"
    if capture_status != expected_status:
        raise ContractError("completed gate capture status does not match contract availability")
    if (
        evidence.get("business_date_validation") != "passed"
        or evidence.get("source_time_validation") != "passed"
        or evidence.get("snapshot_freeze_validation") != "passed"
        or evidence.get("cnf_refetch_validation") != "passed"
    ):
        raise ContractError("completed gate validation evidence is incomplete")
    snapshot_batch_id = evidence.get("snapshot_batch_id")
    if not isinstance(snapshot_batch_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", snapshot_batch_id):
        raise ContractError("completed gate snapshot batch identity is invalid")
    candidate_sha256 = evidence.get("candidate_sha256")
    if not isinstance(candidate_sha256, str) or not SHA256_RE.fullmatch(candidate_sha256):
        raise ContractError("completed gate candidate SHA-256 is invalid")
    return {
        "gate_id": REAL_NIGHT_SESSION_CLOSE_GATE_ID,
        "status": "completed",
        "business_date": parsed_date.isoformat(),
        "captured_at": str(captured_at),
        "target_contracts": list(contracts),
        "available_contracts": list(available),
        "missing_contracts": list(missing),
        "capture_status": capture_status,
        "business_date_validation": "passed",
        "source_time_validation": "passed",
        "snapshot_freeze_validation": "passed",
        "cnf_refetch_validation": "passed",
        "snapshot_batch_id": snapshot_batch_id,
        "candidate_sha256": candidate_sha256,
    }


def validate_candidate_gate_state(result: Mapping[str, Any]) -> None:
    version = result.get("schema_version")
    if version not in SUPPORTED_CANDIDATE_RESULT_SCHEMA_VERSIONS:
        raise ContractError("candidate result schema version is unsupported")
    status = result.get("status")
    blocking = result.get("blocking_gates", [])
    completed = result.get("completed_gates", [])
    if version == LEGACY_CANDIDATE_RESULT_SCHEMA_VERSION:
        if status != CANDIDATE_VALIDATED_STATUS or blocking or completed:
            raise ContractError("legacy candidate result gate state is invalid")
        return
    if not isinstance(blocking, list) or not isinstance(completed, list):
        raise ContractError("candidate gate collections must be arrays")
    if len(completed) > 1 or any(
        not isinstance(gate, dict) or completed_candidate_gate(gate) != gate
        for gate in completed
    ):
        raise ContractError("candidate completed gate evidence is invalid")
    if status == CANDIDATE_WAITING_STATUS:
        if len(blocking) != 1 or completed:
            raise ContractError("waiting candidate must contain exactly one blocking gate")
        gate = blocking[0]
        if not isinstance(gate, dict):
            raise ContractError("candidate blocking gate is invalid")
        expected = pending_candidate_gate(
            str(gate.get("gate_id")), str(gate.get("earliest_expected_business_date"))
        )
        if gate != expected:
            raise ContractError("candidate blocking gate does not match the controlled contract")
    elif status == CANDIDATE_VALIDATED_STATUS:
        if blocking:
            raise ContractError("validated candidate must not contain blocking gates")
    else:
        raise ContractError("candidate result status is not supported")


def validate_runtime_mount_evidence(mounts: Any) -> list[dict[str, Any]]:
    if mounts is None:
        return []
    if not isinstance(mounts, list) or len(mounts) > 1:
        raise ContractError("candidate runtime mounts must contain at most one approved mount")
    validated: list[dict[str, Any]] = []
    for item in mounts:
        if not isinstance(item, dict):
            raise ContractError("candidate runtime mount evidence is invalid")
        expected = {
            "mount_id": IMPORT_PROFIT_RUNTIME_MOUNT_ID,
            "container_path": IMPORT_PROFIT_RUNTIME_CONTAINER_PATH,
            "mode": "rw",
            "runtime_kind": "import_profit_candidate",
            "candidate_batch_id": item.get("candidate_batch_id"),
            "required_environment_variable": IMPORT_PROFIT_RUNTIME_ENV_KEY,
        }
        batch = expected["candidate_batch_id"]
        if not isinstance(batch, str) or not RELEASE_ID_RE.fullmatch(batch):
            raise ContractError("candidate runtime mount batch identity is invalid")
        if item != expected:
            raise ContractError("candidate runtime mount evidence does not match the approved contract")
        validated.append(expected)
    return validated


def require_deployable_candidate_result(result: Mapping[str, Any]) -> None:
    validate_candidate_gate_state(result)
    if result.get("status") != CANDIDATE_VALIDATED_STATUS:
        raise ContractError("candidate result is not eligible for production promotion")
    if result.get("blocking_gates", []):
        raise ContractError("candidate result has blocking production gates")
    mounts = validate_runtime_mount_evidence(result.get("runtime_mounts", []))
    if mounts:
        completed = result.get("completed_gates", [])
        if not isinstance(completed, list) or not any(
            isinstance(gate, dict)
            and gate.get("gate_id") == REAL_NIGHT_SESSION_CLOSE_GATE_ID
            and gate.get("status") == "completed"
            for gate in completed
        ):
            raise ContractError("import profit candidate is missing the completed real 08:30 night-session gate")


def validate_candidate_result(
    result: Mapping[str, Any],
    manifest: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> None:
    validate_against_schema(result, schema)
    expected = {
        "application": APPLICATION,
        "release_id": manifest["release_id"],
        "git_commit": manifest["git_commit"],
        "git_tree": manifest["git_tree"],
        "image_ref": manifest["image_ref"],
        "candidate_image_id": manifest["image_id"],
        "candidate_container_name": manifest["candidate_container_name"],
    }
    for key, expected_value in expected.items():
        if result.get(key) != expected_value:
            raise ContractError(
                f"candidate result {key} mismatch: expected {expected_value!r}, "
                f"got {result.get(key)!r}"
            )
    validate_candidate_gate_state(result)
    runtime_mounts = validate_runtime_mount_evidence(result.get("runtime_mounts", []))
    access = result.get("candidate_runtime_access")
    if runtime_mounts:
        if not isinstance(access, dict) or access != {
            "container_path": IMPORT_PROFIT_RUNTIME_CONTAINER_PATH,
            "environment_variable": IMPORT_PROFIT_RUNTIME_ENV_KEY,
            "uid": access.get("uid"),
            "gid": access.get("gid"),
            "read_write_probe": "passed",
        }:
            raise ContractError("candidate runtime access evidence is invalid")
        for field in ("uid", "gid"):
            value = access.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ContractError(f"candidate runtime access {field} is invalid")
    elif access is not None:
        raise ContractError("candidate runtime access must be absent without a runtime mount")
    weather_facts = weather_candidate_facts(manifest.get("weather_candidate_mode"))
    for key, expected_value in weather_facts.items():
        if result.get(key) != expected_value:
            raise ContractError(f"candidate result {key} mismatch")
    if result.get("weather_runtime_contract") != {
        **weather_runtime_contract(
            PRODUCTION_WEATHER_RUNTIME_DIR,
            weather_facts["weather_candidate_mode"],
        ),
        "host_path": weather_facts["weather_candidate_source"],
        "candidate_weather_data_dir": weather_facts["weather_candidate_data_dir"],
    }:
        raise ContractError("candidate result weather runtime contract mismatch")
    identity = result.get("identity")
    if not isinstance(identity, dict):
        raise ContractError("candidate result identity is missing")
    for key, expected_value in (
        ("config_image", manifest["image_ref"]),
        ("actual_image_id", manifest["image_id"]),
        ("tag_image_id", manifest["image_id"]),
        ("release_image_id", manifest["image_id"]),
        ("oci_revision", manifest["git_commit"]),
        ("runtime_git_commit", manifest["git_commit"]),
        ("embedded_release_sha256", manifest["image_release_json_sha256"]),
    ):
        if identity.get(key) != expected_value:
            raise ContractError(f"candidate result identity {key} mismatch")
    formal_containers = result.get("formal_containers")
    require_formal_containers_unchanged(
        formal_containers, "candidate result"
    )
    if formal_containers.get("before_phase") != "before-candidate" or formal_containers.get(
        "after_phase"
    ) != "after-candidate":
        raise ContractError("candidate result formal container phases mismatch")
    checks = result.get("checks")
    if not isinstance(checks, dict):
        raise ContractError("candidate result checks are missing")
    http = checks.get("http")
    if not isinstance(http, dict) or any(
        http.get(key) != 200 for key in ("health", "host_config", "root")
    ):
        raise ContractError("candidate result HTTP validation did not fully pass")
    pages = checks.get("pages")
    if not isinstance(pages, dict) or pages.get("status") != "passed":
        raise ContractError("candidate result page validation did not pass")
    for key, expected_value in (
        ("formal_git_unchanged", True),
        ("data_files_unchanged", True),
        ("production_switch_performed", False),
    ):
        if checks.get(key) is not expected_value:
            raise ContractError(f"candidate result check {key} mismatch")
    if checks.get("formal_containers_unchanged") is not formal_containers.get(
        "formal_containers_unchanged"
    ):
        raise ContractError(
            "candidate result formal_containers_unchanged is not derived"
        )
    readiness = checks.get("readiness")
    if not isinstance(readiness, dict) or readiness.get("status") != "ready":
        raise ContractError("candidate result readiness validation did not pass")
    if readiness.get("expected_image_id") != manifest["image_id"]:
        raise ContractError("candidate result readiness Image ID mismatch")
    if readiness.get("policy") != manifest["readiness_policy"]:
        raise ContractError("candidate result readiness policy mismatch")
    if readiness.get("container") != manifest["candidate_container_name"]:
        raise ContractError("candidate result readiness container mismatch")
    readiness_log_summary = readiness.get("log_summary")
    if not isinstance(readiness_log_summary, dict):
        raise ContractError("candidate result readiness log summary is missing")
    try:
        validated_log_summary = validate_log_summary(readiness_log_summary)
    except ValueError as exc:
        raise ContractError(f"candidate result log summary is invalid: {exc}") from exc
    if result.get("log_summary") != validated_log_summary:
        raise ContractError("candidate result log summary differs from readiness evidence")
    validate_build_time(result.get("generated_at"))
    assert_no_sensitive_values(result, "candidate result")


def create_candidate_result(
    *,
    manifest: Mapping[str, Any],
    runtime: ReleaseRuntime,
    readiness: Mapping[str, Any],
    checks: Mapping[str, Any],
    blocking_gates: Sequence[Mapping[str, Any]] = (),
    completed_gates: Sequence[Mapping[str, Any]] = (),
    runtime_mounts: Sequence[Mapping[str, Any]] = (),
    candidate_runtime_access: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    candidate_identity = verify_candidate(manifest, runtime)
    formal_before = checks.get("formal_containers_before")
    if not isinstance(formal_before, dict):
        raise ContractError(
            "candidate checks must include a before-candidate formal container snapshot"
        )
    formal_after = capture_formal_container_snapshot(runtime)
    formal_containers = compare_formal_container_snapshots(
        formal_before,
        formal_after,
        before_phase="before-candidate",
        after_phase="after-candidate",
    )
    require_formal_containers_unchanged(formal_containers, "candidate result")
    timestamp = generated_at or datetime.now(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )
    blocking_gate_list = [copy.deepcopy(dict(gate)) for gate in blocking_gates]
    completed_gate_list = [copy.deepcopy(dict(gate)) for gate in completed_gates]
    status = CANDIDATE_WAITING_STATUS if blocking_gate_list else CANDIDATE_VALIDATED_STATUS
    runtime_mount_list = validate_runtime_mount_evidence(
        [copy.deepcopy(dict(mount)) for mount in runtime_mounts]
    )
    if runtime_mount_list and candidate_runtime_access is None:
        raise ContractError("import profit candidate requires measured runtime access evidence")
    result = {
        "schema_version": CANDIDATE_RESULT_SCHEMA_VERSION,
        "application": APPLICATION,
        "release_id": manifest["release_id"],
        "git_commit": manifest["git_commit"],
        "git_tree": manifest["git_tree"],
        "image_ref": manifest["image_ref"],
        "candidate_image_id": manifest["image_id"],
        "candidate_container_name": manifest["candidate_container_name"],
        "generated_at": timestamp,
        "status": status,
        "blocking_gates": blocking_gate_list,
        "completed_gates": completed_gate_list,
        "runtime_mounts": runtime_mount_list,
        "candidate_runtime_access": (
            copy.deepcopy(dict(candidate_runtime_access))
            if candidate_runtime_access is not None
            else None
        ),
        "weather_runtime_contract": {
            **weather_runtime_contract(
                PRODUCTION_WEATHER_RUNTIME_DIR,
                manifest["weather_candidate_mode"],
            ),
            "host_path": manifest["weather_candidate_source"],
            "candidate_weather_data_dir": manifest["weather_candidate_data_dir"],
        },
        **weather_candidate_facts(manifest["weather_candidate_mode"]),
        "identity": {
            "config_image": candidate_identity["config_image"],
            "actual_image_id": candidate_identity["actual_image_id"],
            "tag_image_id": candidate_identity["tag_image_id"],
            "release_image_id": candidate_identity["manifest_image_id"],
            "oci_revision": candidate_identity["oci_revision"],
            "runtime_git_commit": candidate_identity["runtime_git_commit"],
            "embedded_release_sha256": candidate_identity[
                "embedded_release_sha256"
            ],
        },
        "formal_containers": formal_containers,
        "log_summary": copy.deepcopy(dict(readiness.get("log_summary") or {})),
        "checks": {
            "readiness": copy.deepcopy(dict(readiness)),
            "http": copy.deepcopy(dict(checks.get("http") or {})),
            "pages": copy.deepcopy(dict(checks.get("pages") or {})),
            "formal_containers_unchanged": formal_containers[
                "formal_containers_unchanged"
            ],
            "formal_git_unchanged": checks.get("formal_git_unchanged"),
            "data_files_unchanged": checks.get("data_files_unchanged"),
            "production_switch_performed": checks.get(
                "production_switch_performed"
            ),
        },
    }
    validate_candidate_result(
        result,
        manifest,
        load_schema(Path(__file__).with_name("candidate_result.schema.json")),
    )
    return result


def write_candidate_result(result: Mapping[str, Any], path: Path) -> Path:
    validate_against_schema(
        result,
        load_schema(Path(__file__).with_name("candidate_result.schema.json")),
    )
    target, _ = write_json_artifact(
        path,
        result,
        artifact_type="candidate_result",
        release_id=str(result["release_id"]),
        git_commit=str(result["git_commit"]),
        git_tree=str(result["git_tree"]),
        image_id=str(result["candidate_image_id"]),
        runtime_git_commit=str(result["identity"]["runtime_git_commit"]),
    )
    return target


def _compose_switch_argv(
    production_env_file: Path,
    production_project_dir: Path,
    production_compose_file: Path,
) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(production_env_file),
        "--project-name",
        COMPOSE_PROJECT,
        "--project-directory",
        str(production_project_dir),
        "-f",
        str(production_compose_file),
        "up",
        "-d",
        "--no-build",
        "--pull",
        "never",
        "--no-deps",
        "--force-recreate",
        COMPOSE_SERVICE,
    ]


def _validate_sealed_environment_identity(
    identity: Mapping[str, Any],
    *,
    description: str,
) -> dict[str, str]:
    environment = identity.get("environment")
    if not isinstance(environment, dict):
        raise ContractError(f"deployment plan {description} environment is missing")
    values = validate_current_production_env(environment)
    if identity.get("env_sha256") != production_env_sha256(values):
        raise ContractError(f"deployment plan {description} env SHA-256 mismatch")
    if identity.get("image_ref") != values["SPREAD_IMAGE"]:
        raise ContractError(f"deployment plan {description} image_ref mismatch")
    if identity.get("git_commit") != values["MARKET_DATA_GIT_HEAD"]:
        raise ContractError(f"deployment plan {description} git_commit mismatch")
    validate_image_id(identity.get("image_id"), f"{description} image_id")
    validate_sha256(identity.get("compose_sha256"), f"{description} compose_sha256")
    return values


def validate_deployment_plan(
    plan: Mapping[str, Any],
    manifest: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    production_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    validate_against_schema(plan, schema)
    expected_release_fields = {
        "release_id": manifest["release_id"],
        "git_commit": manifest["git_commit"],
        "git_tree": manifest["git_tree"],
        "image_ref": manifest["image_ref"],
        "expected_image_id": manifest["image_id"],
        "candidate_image_id": manifest["image_id"],
        "compose_project": COMPOSE_PROJECT,
        "production_service": COMPOSE_SERVICE,
        "compose_template_sha256": manifest["compose_template_sha256"],
        "rollback_git_commit": manifest["formal_git_commit"],
        "rollback_image_ref": manifest["rollback_image_ref"],
        "rollback_image_id": manifest["rollback_image_id"],
        "readiness_policy": manifest["readiness_policy"],
    }
    for key, expected in expected_release_fields.items():
        if plan.get(key) != expected:
            raise ContractError(
                f"deployment plan {key} mismatch: expected {expected!r}, "
                f"got {plan.get(key)!r}"
            )
    if plan.get("schema_version") != DEPLOYMENT_PLAN_SCHEMA_VERSION:
        raise ContractError("unsupported deployment plan schema_version")
    try:
        plan_readiness_policy = validate_readiness_policy(
            plan.get("readiness_policy") or {}
        )
    except ValueError as exc:
        raise ContractError(f"invalid deployment plan readiness_policy: {exc}") from exc
    if plan_readiness_policy != manifest.get("readiness_policy"):
        raise ContractError("deployment plan readiness_policy differs from release.json")
    if plan.get("plan_status") != "deployment_plan_sealed":
        raise ContractError("deployment plan is not sealed")
    if plan.get("production_service_scope") != list(PRODUCTION_SERVICE_SCOPE):
        raise ContractError(
            "deployment plan service scope must contain only spread-dashboard"
        )
    tool_repo_root = Path(str(plan.get("tool_repo_root", "")))
    if not tool_repo_root.is_absolute() or not tool_repo_root.is_dir():
        raise ContractError("deployment plan tool_repo_root must be an existing absolute directory")
    production_compose_file = Path(str(plan.get("production_compose_file", "")))
    if (
        not production_compose_file.is_absolute()
        or production_compose_file.name != "docker-compose.yml"
        or not production_compose_file.is_file()
    ):
        raise ContractError(
            "deployment plan production_compose_file must be an existing absolute "
            "docker-compose.yml path"
        )
    production_project_dir = Path(str(plan.get("production_project_dir", "")))
    if not production_project_dir.is_absolute() or not production_project_dir.is_dir():
        raise ContractError(
            "deployment plan production_project_dir must be an existing absolute directory"
        )
    if production_project_dir.resolve() == tool_repo_root.resolve():
        raise ContractError(
            "deployment plan tool_repo_root and production_project_dir must be "
            "different directories"
        )
    if hash_file(production_compose_file) != plan.get("compose_template_sha256"):
        raise ContractError("deployment plan production Compose file changed")
    candidate_result_file = Path(str(plan.get("candidate_result_file", "")))
    if not candidate_result_file.is_absolute():
        raise ContractError("deployment plan candidate_result_file must be absolute")
    candidate_result = load_candidate_result(candidate_result_file, manifest)
    require_deployable_candidate_result(candidate_result)
    if hash_file(candidate_result_file) != plan.get("candidate_result_sha256"):
        raise ContractError("candidate result changed after deployment plan sealing")
    formal_containers = plan.get("formal_containers")
    require_formal_containers_unchanged(formal_containers, "deployment plan")
    if (
        formal_containers.get("before_phase") != "after-candidate"
        or formal_containers.get("after_phase") != "pre-deploy"
        or formal_containers.get("before")
        != candidate_result.get("formal_containers", {}).get("after")
    ):
        raise ContractError("deployment plan formal container baseline mismatch")
    if plan.get("candidate_container_removed") is not True:
        raise ContractError("deployment plan requires the candidate container to be removed")
    allowed_differences = plan.get("allowed_candidate_production_differences")
    if (
        not isinstance(allowed_differences, list)
        or len(set(allowed_differences)) != len(allowed_differences)
        or not set(allowed_differences).issubset(
            (*RUNTIME_URL_KEYS, WEATHER_DATA_DIR_ENV_KEY)
        )
    ):
        raise ContractError(
            "deployment plan contains an undeclared candidate/production difference"
        )
    semantic = plan.get("semantic_comparison")
    if not isinstance(semantic, dict):
        raise ContractError("deployment plan semantic comparison is missing")
    if semantic.get("base_semantics_equal") is not True:
        raise ContractError("candidate and production Compose semantics differ")
    candidate_semantic = validate_sha256(
        semantic.get("candidate_semantic_sha256"),
        "candidate_semantic_sha256",
    )
    production_semantic = validate_sha256(
        semantic.get("production_semantic_sha256"),
        "production_semantic_sha256",
    )
    if candidate_semantic != production_semantic:
        raise ContractError("candidate and production semantic hashes differ")
    candidate_runtime = plan.get("candidate_runtime_environment")
    production_runtime = plan.get("production_runtime_environment")
    if not isinstance(production_runtime, dict):
        raise ContractError(
            "deployment plan production runtime environment is missing"
        )
    validate_sha256(
        plan.get("candidate_compose_sha256"),
        "candidate_compose_sha256",
    )
    validate_sha256(
        plan.get("production_compose_sha256"),
        "production_compose_sha256",
    )
    validate_build_time(plan.get("created_at"))

    production_env_file = Path(str(plan.get("production_env_file", "")))
    if not production_env_file.is_absolute():
        raise ContractError("deployment plan production_env_file must be absolute")
    current_identity = plan.get("current_production")
    target_identity = plan.get("target_release")
    if not isinstance(current_identity, dict) or not isinstance(target_identity, dict):
        raise ContractError("deployment plan current/target identities are missing")
    current_values = _validate_sealed_environment_identity(
        current_identity,
        description="current_production",
    )
    target_values = _validate_sealed_environment_identity(
        target_identity,
        description="target_release",
    )
    if current_identity.get("image_ref") != manifest["rollback_image_ref"]:
        raise ContractError("deployment plan current production image_ref changed")
    if current_identity.get("image_id") != manifest["rollback_image_id"]:
        raise ContractError("deployment plan current production Image ID changed")
    if current_identity.get("git_commit") != manifest["formal_git_commit"]:
        raise ContractError("deployment plan current production Git commit changed")
    expected_target_identity = {
        "release_id": manifest["release_id"],
        "image_ref": manifest["image_ref"],
        "image_id": manifest["image_id"],
        "git_commit": manifest["git_commit"],
        "git_tree": manifest["git_tree"],
        "oci_revision": manifest["git_commit"],
    }
    for key, expected in expected_target_identity.items():
        if target_identity.get(key) != expected:
            raise ContractError(f"deployment plan target_release {key} mismatch")
    validate_production_env(target_values, manifest)
    if current_values == target_values:
        raise ContractError("deployment plan cannot seal an already-deployed no-op")
    if plan.get("production_env_baseline_sha256") != current_identity["env_sha256"]:
        raise ContractError("deployment plan production env baseline SHA-256 mismatch")
    if plan.get("production_env_sha256") != target_identity["env_sha256"]:
        raise ContractError("deployment plan target production env SHA-256 mismatch")
    environment = (
        dict(production_environment)
        if production_environment is not None
        else parse_production_env(production_env_file)
    )
    validate_current_production_env(environment)
    actual_env_sha256 = hash_file(production_env_file)
    if environment == current_values:
        expected_env_sha256 = current_identity["env_sha256"]
    elif environment == target_values:
        expected_env_sha256 = target_identity["env_sha256"]
    else:
        raise ContractError("production environment drifted outside the sealed transition")
    if actual_env_sha256 != expected_env_sha256:
        raise ContractError("production environment file does not match its sealed state")
    expected_candidate_runtime = {
        **candidate_user_url_environment(target_values),
        WEATHER_DATA_DIR_ENV_KEY: weather_candidate_data_dir(
            manifest["weather_candidate_mode"]
        ),
    }
    if candidate_runtime != expected_candidate_runtime:
        raise ContractError(
            "deployment plan candidate runtime environment does not inherit production URLs"
        )
    weather_facts = weather_candidate_facts(manifest.get("weather_candidate_mode"))
    for key, expected_value in weather_facts.items():
        if plan.get(key) != expected_value:
            raise ContractError(f"deployment plan {key} mismatch")
    expected_weather_contract = weather_runtime_contract(
        target_values[WEATHER_RUNTIME_ENV_KEY],
        weather_facts["weather_candidate_mode"],
    )
    if plan.get("weather_runtime_contract") != expected_weather_contract:
        raise ContractError("deployment plan weather runtime contract changed")
    for key in RUNTIME_URL_KEYS:
        if plan.get(key) != target_values[key]:
            raise ContractError(f"deployment plan {key} does not match target env")
        if production_runtime.get(key) != target_values[key]:
            raise ContractError(
                f"deployment plan production runtime {key} does not match target env"
            )
    if production_runtime.get(WEATHER_DATA_DIR_ENV_KEY) != target_values[
        WEATHER_DATA_DIR_ENV_KEY
    ]:
        raise ContractError(
            "deployment plan production weather runtime does not match production env"
        )
    actual_differences = [
        key
        for key in (*RUNTIME_URL_KEYS, WEATHER_DATA_DIR_ENV_KEY)
        if candidate_runtime[key] != production_runtime[key]
    ]
    if allowed_differences != actual_differences:
        raise ContractError(
            "deployment plan runtime differences do not match the allowlisted facts"
        )
    if plan.get("MARKET_DATA_GIT_HEAD") != target_values["MARKET_DATA_GIT_HEAD"]:
        raise ContractError(
            "deployment plan MARKET_DATA_GIT_HEAD does not match target env"
        )
    if plan.get("current_production_compose_sha256") != current_identity[
        "compose_sha256"
    ]:
        raise ContractError("deployment plan current production Compose SHA mismatch")
    if plan.get("production_compose_sha256") != target_identity["compose_sha256"]:
        raise ContractError("deployment plan target production Compose SHA mismatch")
    tool_revision = plan.get("deployment_tool_revision")
    if not isinstance(tool_revision, dict):
        raise ContractError("deployment plan deployment_tool_revision is missing")
    validate_full_git_commit(tool_revision.get("git_commit"))
    validate_full_git_commit(tool_revision.get("git_tree"))
    expected_argv = _compose_switch_argv(
        production_env_file,
        production_project_dir,
        production_compose_file,
    )
    if plan.get("deployment_argv") != expected_argv:
        raise ContractError("deployment plan deployment argv changed")
    if plan.get("rollback_argv") != expected_argv:
        raise ContractError("deployment plan rollback argv changed")
    if plan.get("deployment_action") != "upgrade":
        raise ContractError("deployment plan action must be upgrade")
    assert_no_sensitive_values(plan, "deployment plan")
    return environment


def create_deployment_plan(
    *,
    tool_repo_root: Path,
    production_compose_file: Path,
    production_project_dir: Path,
    candidate_result_file: Path,
    production_env_file: Path,
    manifest: Mapping[str, Any],
    runtime: ReleaseRuntime,
    schema: Mapping[str, Any],
    created_at: str | None = None,
    deployment_tool_git_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    tool_repo_root = tool_repo_root.resolve()
    production_compose_file = production_compose_file.resolve()
    production_project_dir = production_project_dir.resolve()
    candidate_result_file = candidate_result_file.resolve()
    production_env_file = production_env_file.resolve()
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(
            f"deployment plans require a release manifest using schema {SCHEMA_VERSION}"
        )
    validate_repository_static(tool_repo_root)
    if tool_repo_root == production_project_dir:
        raise ContractError(
            "tool_repo_root and production_project_dir must be different directories"
        )
    if not production_project_dir.is_dir():
        raise ContractError(
            f"production project directory is missing: {production_project_dir}"
        )
    if not production_compose_file.is_file():
        raise ContractError(
            f"production Compose file is missing: {production_compose_file}"
        )
    compose_template = tool_repo_root / "docker-compose.yml"
    template_sha = hash_file(production_compose_file)
    if template_sha != manifest.get("compose_template_sha256"):
        raise ContractError(
            "production Compose file SHA-256 does not match release.json"
        )
    if hash_file(compose_template) != template_sha:
        raise ContractError(
            "tool repository Compose and production Compose file identities differ"
        )

    deployment_tool_revision = capture_git_identity(
        tool_repo_root, deployment_tool_git_runner
    )
    current_environment = parse_production_env(production_env_file)
    validate_current_production_env(current_environment)
    target_environment = target_production_environment(
        current_environment, manifest
    )
    candidate_result = load_candidate_result(candidate_result_file, manifest)
    if runtime.container_exists(manifest["candidate_container_name"]):
        raise ContractError(
            "candidate container still exists; remove it before sealing deployment plan"
        )
    _verify_image_identity(manifest, runtime)
    current_container = runtime.container_record(PRODUCTION_CONTAINER)
    if current_environment == target_environment:
        if (
            current_container.get("image_id") == manifest["image_id"]
            and current_container.get("config_image") == manifest["image_ref"]
            and current_container.get("runtime_git_commit") == manifest["git_commit"]
        ):
            raise ContractError(
                "production already matches target release; deployment is already-deployed/no-op"
            )
        raise ContractError(
            "production env names the target release but the running container identity differs"
        )
    if current_environment["SPREAD_IMAGE"] != manifest["rollback_image_ref"]:
        raise ContractError(
            "current production SPREAD_IMAGE does not match the candidate-sealed rollback ref"
        )
    if current_environment["MARKET_DATA_GIT_HEAD"] != manifest["formal_git_commit"]:
        raise ContractError(
            "current production Git commit does not match the candidate-sealed baseline"
        )
    if current_container.get("image_id") != manifest["rollback_image_id"]:
        raise ContractError(
            "current production container Image ID does not match the sealed baseline"
        )
    if current_container.get("config_image") != manifest["rollback_image_ref"]:
        raise ContractError(
            "current production container image ref does not match the sealed baseline"
        )
    if current_container.get("runtime_git_commit") != manifest["formal_git_commit"]:
        raise ContractError(
            "current production runtime Git commit does not match the sealed baseline"
        )
    rollback_image = runtime.image_record(manifest["rollback_image_ref"])
    if rollback_image.get("id") != manifest["rollback_image_id"]:
        raise ContractError("sealed rollback tag no longer resolves to its Image ID")
    pre_deploy_formal_snapshot = capture_formal_container_snapshot(runtime)
    formal_containers = compare_formal_container_snapshots(
        candidate_result["formal_containers"]["after"],
        pre_deploy_formal_snapshot,
        before_phase="after-candidate",
        after_phase="pre-deploy",
    )
    require_formal_containers_unchanged(formal_containers, "deployment plan")

    candidate_compose, candidate_raw, candidate_images = runtime.compose_config(
        tool_repo_root,
        manifest["image_ref"],
        environment=candidate_compose_environment(
            manifest["git_commit"],
            target_environment,
            manifest["weather_candidate_mode"],
        ),
        project_directory=tool_repo_root,
        compose_file=compose_template,
    )
    candidate_sha = validate_compose_result(
        candidate_compose,
        candidate_raw,
        candidate_images,
        manifest["image_ref"],
    )

    current_compose, current_raw, current_images = runtime.compose_config(
        tool_repo_root,
        current_environment["SPREAD_IMAGE"],
        environment=current_environment,
        project_directory=production_project_dir,
        compose_file=production_compose_file,
    )
    current_compose_sha = validate_compose_result(
        current_compose,
        current_raw,
        current_images,
        current_environment["SPREAD_IMAGE"],
    )
    _validate_formal_spread_semantics(
        current_compose,
        production_project_dir,
        current_environment["SPREAD_IMAGE"],
        current_environment["MARKET_DATA_GIT_HEAD"],
        current_environment[WEATHER_RUNTIME_ENV_KEY],
    )

    production_compose, production_raw, production_images = runtime.compose_config(
        tool_repo_root,
        manifest["image_ref"],
        environment=target_environment,
        project_directory=production_project_dir,
        compose_file=production_compose_file,
    )
    production_sha = validate_compose_result(
        production_compose,
        production_raw,
        production_images,
        manifest["image_ref"],
    )
    _validate_formal_spread_semantics(
        production_compose,
        production_project_dir,
        manifest["image_ref"],
        manifest["git_commit"],
        target_environment[WEATHER_RUNTIME_ENV_KEY],
    )
    _validate_weather_runtime_compose(
        candidate_compose,
        expected_source=manifest["weather_candidate_source"],
        expected_weather_data_dir=manifest["weather_candidate_data_dir"],
    )

    candidate_semantics, candidate_runtime = _compose_spread_semantics(
        candidate_compose,
        tool_repo_root,
    )
    production_semantics, production_runtime = _compose_spread_semantics(
        production_compose,
        production_project_dir,
    )
    if candidate_semantics != production_semantics:
        raise ContractError(
            "candidate and production Compose differ outside the runtime URL allowlist"
        )
    allowed_differences = [
        key
        for key in (*RUNTIME_URL_KEYS, WEATHER_DATA_DIR_ENV_KEY)
        if candidate_runtime[key] != production_runtime[key]
    ]
    for key in (*RUNTIME_URL_KEYS, WEATHER_DATA_DIR_ENV_KEY):
        if production_runtime[key] != target_environment[key]:
            raise ContractError(
                f"production Compose did not render the explicit {key} value"
            )
    semantic_sha = _semantic_sha256(candidate_semantics)
    timestamp = created_at or datetime.now(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )
    validate_build_time(timestamp)
    plan = {
        "schema_version": DEPLOYMENT_PLAN_SCHEMA_VERSION,
        "release_id": manifest["release_id"],
        "git_commit": manifest["git_commit"],
        "git_tree": manifest["git_tree"],
        "image_ref": manifest["image_ref"],
        "expected_image_id": manifest["image_id"],
        "candidate_image_id": manifest["image_id"],
        "candidate_result_file": str(candidate_result_file),
        "candidate_result_sha256": hash_file(candidate_result_file),
        "candidate_container_removed": True,
        "formal_containers": formal_containers,
        "production_env_file": str(production_env_file),
        "production_env_baseline_sha256": hash_file(production_env_file),
        "production_env_sha256": production_env_sha256(target_environment),
        "USDA_DASHBOARD_URL": target_environment["USDA_DASHBOARD_URL"],
        "OIL_WORLD_DASHBOARD_URL": target_environment[
            "OIL_WORLD_DASHBOARD_URL"
        ],
        "MARKET_DATA_GIT_HEAD": target_environment["MARKET_DATA_GIT_HEAD"],
        "weather_runtime_contract": weather_runtime_contract(
            target_environment[WEATHER_RUNTIME_ENV_KEY],
            manifest["weather_candidate_mode"],
        ),
        **weather_candidate_facts(manifest["weather_candidate_mode"]),
        "tool_repo_root": str(tool_repo_root),
        "deployment_tool_revision": deployment_tool_revision,
        "compose_project": COMPOSE_PROJECT,
        "production_service": COMPOSE_SERVICE,
        "production_compose_file": str(production_compose_file),
        "production_project_dir": str(production_project_dir),
        "compose_template_sha256": template_sha,
        "candidate_compose_sha256": candidate_sha,
        "current_production_compose_sha256": current_compose_sha,
        "production_compose_sha256": production_sha,
        "candidate_runtime_environment": dict(candidate_runtime),
        "production_runtime_environment": dict(production_runtime),
        "allowed_candidate_production_differences": allowed_differences,
        "semantic_comparison": {
            "base_semantics_equal": True,
            "candidate_semantic_sha256": semantic_sha,
            "production_semantic_sha256": semantic_sha,
        },
        "production_service_scope": list(PRODUCTION_SERVICE_SCOPE),
        "current_production": {
            "environment": dict(current_environment),
            "env_sha256": hash_file(production_env_file),
            "image_ref": current_environment["SPREAD_IMAGE"],
            "image_id": manifest["rollback_image_id"],
            "git_commit": current_environment["MARKET_DATA_GIT_HEAD"],
            "compose_sha256": current_compose_sha,
        },
        "target_release": {
            "environment": dict(target_environment),
            "env_sha256": production_env_sha256(target_environment),
            "release_id": manifest["release_id"],
            "image_ref": manifest["image_ref"],
            "image_id": manifest["image_id"],
            "git_commit": manifest["git_commit"],
            "git_tree": manifest["git_tree"],
            "oci_revision": manifest["git_commit"],
            "compose_sha256": production_sha,
        },
        "deployment_action": "upgrade",
        "deployment_argv": _compose_switch_argv(
            production_env_file,
            production_project_dir,
            production_compose_file,
        ),
        "rollback_argv": _compose_switch_argv(
            production_env_file,
            production_project_dir,
            production_compose_file,
        ),
        "rollback_git_commit": manifest["formal_git_commit"],
        "rollback_image_ref": manifest["rollback_image_ref"],
        "rollback_image_id": manifest["rollback_image_id"],
        "readiness_policy": copy.deepcopy(manifest["readiness_policy"]),
        "created_at": timestamp,
        "plan_status": "deployment_plan_sealed",
    }
    validate_deployment_plan(
        plan,
        manifest,
        schema,
        production_environment=current_environment,
    )
    return plan


def write_deployment_plan(plan: Mapping[str, Any], path: Path) -> Path:
    target, _ = write_json_artifact(
        path,
        plan,
        artifact_type="deployment_plan",
        release_id=str(plan["release_id"]),
        git_commit=str(plan["git_commit"]),
        git_tree=str(plan["git_tree"]),
        image_id=str(plan["candidate_image_id"]),
    )
    return target


def load_deployment_plan(
    plan_path: Path,
    manifest: Mapping[str, Any],
    schema_path: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    plan, _ = load_verified_json_artifact(
        plan_path,
        artifact_type="deployment_plan",
        expected_release_id=str(manifest["release_id"]),
        expected_git_commit=str(manifest["git_commit"]),
        expected_git_tree=str(manifest["git_tree"]),
        expected_image_id=str(manifest["image_id"]),
    )
    environment = validate_deployment_plan(
        plan,
        manifest,
        load_schema(schema_path),
    )
    return plan, environment


def _verify_image_identity(
    manifest: Mapping[str, Any],
    runtime: ReleaseRuntime,
) -> dict[str, Any]:
    image_ref = manifest["image_ref"]
    record = runtime.image_record(image_ref)
    if record.get("id") != manifest["image_id"]:
        raise ContractError(
            f"image tag ID mismatch: expected {manifest['image_id']}, got {record.get('id')}"
        )
    labels = record.get("labels")
    if not isinstance(labels, dict):
        raise ContractError("image labels are missing")
    expected_labels = {
        "org.opencontainers.image.revision": manifest["git_commit"],
        "org.opencontainers.image.version": manifest["release_id"],
        "org.opencontainers.image.created": manifest["build_time"],
    }
    for key, expected in expected_labels.items():
        if labels.get(key) != expected:
            raise ContractError(
                f"image label {key} mismatch: expected {expected}, got {labels.get(key)!r}"
            )
    source = labels.get("org.opencontainers.image.source")
    validate_source(source)
    release = runtime.read_image_release(image_ref)
    _validate_embedded_release(release, manifest, source)
    release_sha256 = runtime.image_release_sha256(image_ref)
    if release_sha256 != manifest["image_release_json_sha256"]:
        raise ContractError(
            "image /app/RELEASE.json SHA-256 does not match the release manifest"
        )
    return {
        "image_ref": image_ref,
        "image_id": record["id"],
        "oci_revision": labels["org.opencontainers.image.revision"],
        "embedded_release": release,
        "embedded_release_sha256": release_sha256,
    }


def _verified_runtime_git_commit(
    container: Mapping[str, Any],
    expected_git_commit: str,
    description: str,
) -> str:
    value = container.get("runtime_git_commit")
    if value is None:
        raise ContractError(
            f"{description} is missing runtime MARKET_DATA_GIT_HEAD"
        )
    if value == "":
        raise ContractError(
            f"{description} runtime MARKET_DATA_GIT_HEAD is empty"
        )
    try:
        runtime_git_commit = validate_full_git_commit(value)
    except ContractError as exc:
        raise ContractError(
            f"{description} runtime MARKET_DATA_GIT_HEAD is invalid"
        ) from exc
    if runtime_git_commit != expected_git_commit:
        raise ContractError(
            f"{description} runtime MARKET_DATA_GIT_HEAD does not match the release"
        )
    return runtime_git_commit


def _validate_embedded_release(
    release: Mapping[str, Any],
    manifest: Mapping[str, Any],
    expected_source: str,
) -> None:
    expected = {
        "application": APPLICATION,
        "release_id": manifest["release_id"],
        "git_commit": manifest["git_commit"],
        "git_tree": manifest["git_tree"],
        "build_time": manifest["build_time"],
        "source": expected_source,
    }
    if dict(release) != expected:
        raise ContractError("image /app/RELEASE.json does not match the release manifest")


def verify_pre_deploy(
    manifest: Mapping[str, Any],
    tool_repo_root: Path,
    runtime: ReleaseRuntime,
    *,
    deployment_plan: Mapping[str, Any] | None = None,
    production_environment: Mapping[str, str] | None = None,
    git_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or deployment_plan is None
        or production_environment is None
    ):
        raise ContractError(
            f"production deployment requires a schema {SCHEMA_VERSION} release and "
            "a sealed deployment_plan"
        )
    tool_repo_root = tool_repo_root.resolve()
    validate_repository_static(tool_repo_root)
    actual_tool_revision = capture_git_identity(tool_repo_root, git_runner)
    if actual_tool_revision != deployment_plan.get("deployment_tool_revision"):
        raise ContractError("deployment tool revision changed after plan sealing")
    for path, field in (
        (tool_repo_root / "Dockerfile", "dockerfile_sha256"),
        (tool_repo_root / ".dockerignore", "dockerignore_sha256"),
        (tool_repo_root / "docker-compose.yml", "compose_template_sha256"),
        (
            tool_repo_root / "02_configs/historical_spread_config.xlsx",
            "required_config_sha256",
        ),
    ):
        actual = hash_file(path)
        if actual != manifest[field]:
            raise ContractError(f"{field} mismatch: expected {manifest[field]}, got {actual}")

    validate_current_production_env(production_environment)
    current_identity = deployment_plan.get("current_production") or {}
    target_identity = deployment_plan.get("target_release") or {}
    if production_environment != current_identity.get("environment"):
        raise ContractError("production environment drifted after plan sealing")
    production_env_file = Path(deployment_plan["production_env_file"])
    if hash_file(production_env_file) != current_identity.get("env_sha256"):
        raise ContractError("production environment SHA-256 drifted after plan sealing")
    if deployment_plan.get("plan_status") != "deployment_plan_sealed":
        raise ContractError("production deployment requires a sealed deployment_plan")
    if deployment_plan.get("production_service_scope") != list(
        PRODUCTION_SERVICE_SCOPE
    ):
        raise ContractError("deployment plan service scope changed")
    if Path(str(deployment_plan.get("tool_repo_root", ""))).resolve() != tool_repo_root:
        raise ContractError("deployment plan tool_repo_root changed")
    production_compose_file = Path(
        str(deployment_plan.get("production_compose_file", ""))
    ).resolve()
    production_project_dir = Path(
        str(deployment_plan.get("production_project_dir", ""))
    ).resolve()
    if production_project_dir == tool_repo_root:
        raise ContractError(
            "pre-deploy tool_repo_root and production_project_dir must be "
            "different directories"
        )
    if hash_file(production_compose_file) != manifest["compose_template_sha256"]:
        raise ContractError("deployment plan production Compose file changed")
    for key, expected in (
        ("release_id", manifest["release_id"]),
        ("git_commit", manifest["git_commit"]),
        ("git_tree", manifest["git_tree"]),
        ("image_ref", manifest["image_ref"]),
        ("expected_image_id", manifest["image_id"]),
        ("candidate_image_id", manifest["image_id"]),
        ("compose_project", COMPOSE_PROJECT),
        ("production_service", COMPOSE_SERVICE),
        ("production_env_baseline_sha256", hash_file(production_env_file)),
    ):
        if deployment_plan.get(key) != expected:
            raise ContractError(f"deployment plan {key} changed before deployment")

    image_evidence = _verify_image_identity(manifest, runtime)
    current_container = runtime.container_record(PRODUCTION_CONTAINER)
    for key, expected in (
        ("config_image", current_identity.get("image_ref")),
        ("image_id", current_identity.get("image_id")),
        ("runtime_git_commit", current_identity.get("git_commit")),
    ):
        if current_container.get(key) != expected:
            raise ContractError(f"current production container {key} drifted")
    current_image = runtime.image_record(str(current_identity.get("image_ref", "")))
    if current_image.get("id") != current_identity.get("image_id"):
        raise ContractError("current production image tag drifted from its sealed Image ID")
    target_environment = target_identity.get("environment")
    if not isinstance(target_environment, dict):
        raise ContractError("deployment plan target environment is missing")
    compose, raw_config, images = runtime.compose_config(
        tool_repo_root,
        manifest["image_ref"],
        environment=target_environment,
        project_directory=production_project_dir,
        compose_file=production_compose_file,
    )
    compose_sha = validate_compose_result(
        compose, raw_config, images, manifest["image_ref"]
    )
    if compose_sha != deployment_plan["production_compose_sha256"]:
        raise ContractError(
            "production Compose configuration changed after deployment plan sealing"
        )
    _validate_formal_spread_semantics(
        compose,
        production_project_dir,
        manifest["image_ref"],
        manifest["git_commit"],
        target_environment[WEATHER_RUNTIME_ENV_KEY],
    )
    semantics, rendered_environment = _compose_spread_semantics(
        compose,
        production_project_dir,
    )
    if _semantic_sha256(semantics) != deployment_plan["semantic_comparison"][
        "production_semantic_sha256"
    ]:
        raise ContractError("production Compose semantics changed after plan sealing")
    for key in RUNTIME_URL_KEYS:
        if rendered_environment[key] != target_environment[key]:
            raise ContractError(f"production Compose rendered unexpected {key}")
    formal_containers = compare_formal_container_snapshots(
        deployment_plan["formal_containers"]["after"],
        capture_formal_container_snapshot(runtime),
        before_phase="pre-deploy",
        after_phase="pre-switch",
    )
    require_formal_containers_unchanged(formal_containers, "pre-deploy")
    return {
        "phase": "pre-deploy",
        **image_evidence,
        "compose_project": compose["name"],
        "compose_image": compose["services"][COMPOSE_SERVICE]["image"],
        "compose_config_sha256": compose_sha,
        "deployment_plan_status": deployment_plan["plan_status"],
        "production_service_scope": deployment_plan["production_service_scope"],
        "tool_repo_root": str(tool_repo_root),
        "production_compose_file": str(production_compose_file),
        "production_project_dir": str(production_project_dir),
        "formal_containers": formal_containers,
    }


def verify_candidate(
    manifest: Mapping[str, Any],
    runtime: ReleaseRuntime,
) -> dict[str, Any]:
    container_name = manifest["candidate_container_name"]
    container = runtime.container_record(container_name)
    if container.get("image_id") != manifest["image_id"]:
        raise ContractError(
            "candidate container Image ID mismatch: "
            f"expected {manifest['image_id']}, got {container.get('image_id')}"
        )
    if container.get("config_image") != manifest["image_ref"]:
        raise ContractError(
            "candidate container Config.Image mismatch: "
            f"expected {manifest['image_ref']}, got {container.get('config_image')!r}"
        )
    runtime_git_commit = _verified_runtime_git_commit(
        container,
        manifest["git_commit"],
        "candidate container",
    )

    image = runtime.image_record(manifest["image_ref"])
    if image.get("id") != manifest["image_id"]:
        raise ContractError(
            "candidate image tag ID mismatch: "
            f"expected {manifest['image_id']}, got {image.get('id')}"
        )
    labels = image.get("labels") or {}
    expected_labels = {
        "org.opencontainers.image.revision": manifest["git_commit"],
        "org.opencontainers.image.version": manifest["release_id"],
        "org.opencontainers.image.created": manifest["build_time"],
    }
    for key, expected in expected_labels.items():
        if labels.get(key) != expected:
            raise ContractError(
                f"candidate image label {key} mismatch: "
                f"expected {expected}, got {labels.get(key)!r}"
            )

    source = labels.get("org.opencontainers.image.source")
    validate_source(source)
    release = runtime.read_candidate_release(container_name)
    _validate_embedded_release(release, manifest, source)
    release_sha256 = runtime.candidate_release_sha256(container_name)
    if release_sha256 != manifest["image_release_json_sha256"]:
        raise ContractError(
            "candidate container /app/RELEASE.json SHA-256 mismatch"
        )
    return {
        "phase": "candidate",
        "container_name": container_name,
        "config_image": container["config_image"],
        "actual_image_id": container["image_id"],
        "tag_image_id": image["id"],
        "manifest_image_id": manifest["image_id"],
        "oci_revision": labels["org.opencontainers.image.revision"],
        "runtime_git_commit": runtime_git_commit,
        "embedded_release": release,
        "embedded_release_sha256": release_sha256,
    }


def verify_post_deploy(
    manifest: Mapping[str, Any],
    runtime: ReleaseRuntime,
    *,
    deployment_plan: Mapping[str, Any] | None = None,
    production_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or deployment_plan is None
        or deployment_plan.get("plan_status") != "deployment_plan_sealed"
        or production_environment is None
    ):
        raise ContractError(
            "post-deploy verification requires a sealed deployment_plan"
        )
    target_environment = deployment_plan.get("target_release", {}).get("environment")
    if production_environment != target_environment:
        raise ContractError("post-deploy production env does not match the sealed target")
    if hash_file(Path(deployment_plan["production_env_file"])) != deployment_plan[
        "production_env_sha256"
    ]:
        raise ContractError("post-deploy production env SHA-256 does not match target")
    for key, expected in (
        ("git_commit", manifest["git_commit"]),
        ("git_tree", manifest["git_tree"]),
        ("candidate_image_id", manifest["image_id"]),
        ("production_service", COMPOSE_SERVICE),
    ):
        if deployment_plan.get(key) != expected:
            raise ContractError(f"post-deploy deployment plan {key} mismatch")
    container = runtime.container_record(PRODUCTION_CONTAINER)
    if container.get("image_id") != deployment_plan["candidate_image_id"]:
        raise ContractError(
            "production container Image ID mismatch: "
            f"expected {deployment_plan['candidate_image_id']}, "
            f"got {container.get('image_id')}"
        )
    if container.get("config_image") != manifest["image_ref"]:
        raise ContractError(
            "production container Config.Image mismatch: "
            f"expected {manifest['image_ref']}, got {container.get('config_image')!r}"
        )
    runtime_git_commit = _verified_runtime_git_commit(
        container,
        manifest["git_commit"],
        "production container",
    )
    record = runtime.image_record(manifest["image_ref"])
    labels = record.get("labels") or {}
    if labels.get("org.opencontainers.image.revision") != manifest["git_commit"]:
        raise ContractError("production image OCI revision mismatch")
    release = runtime.read_container_release(PRODUCTION_CONTAINER)
    release_sha256 = runtime.container_release_sha256(PRODUCTION_CONTAINER)
    if release_sha256 != manifest["image_release_json_sha256"]:
        raise ContractError(
            "production container /app/RELEASE.json SHA-256 mismatch"
        )
    source = labels.get("org.opencontainers.image.source")
    validate_source(source)
    _validate_embedded_release(release, manifest, source)
    formal_containers = compare_formal_container_snapshots(
        deployment_plan["formal_containers"]["after"],
        capture_formal_container_snapshot(runtime),
        before_phase="pre-deploy",
        after_phase="post-deploy",
    )
    require_formal_containers_unchanged(formal_containers, "post-deploy")
    return {
        "phase": "post-deploy",
        "container_name": PRODUCTION_CONTAINER,
        "config_image": container["config_image"],
        "actual_image_id": container["image_id"],
        "oci_revision": labels["org.opencontainers.image.revision"],
        "runtime_git_commit": runtime_git_commit,
        "runtime_git_commit_verified": True,
        "embedded_release": release,
        "embedded_release_sha256": release_sha256,
        "deployment_plan_status": deployment_plan["plan_status"],
        "formal_containers": formal_containers,
    }


def verify_pre_rollback(
    manifest: Mapping[str, Any],
    tool_repo_root: Path,
    runtime: ReleaseRuntime,
    *,
    deployment_plan: Mapping[str, Any] | None = None,
    production_environment: Mapping[str, str] | None = None,
    git_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    if deployment_plan is None or production_environment is None:
        raise ContractError("rollback requires a sealed deployment_plan")
    if deployment_plan.get("plan_status") != "deployment_plan_sealed":
        raise ContractError("rollback deployment_plan is not sealed")
    if deployment_plan.get("production_service_scope") != list(
        PRODUCTION_SERVICE_SCOPE
    ):
        raise ContractError("rollback service scope changed")
    validate_current_production_env(production_environment)
    current_environment = deployment_plan.get("current_production", {}).get(
        "environment"
    )
    target_environment = deployment_plan.get("target_release", {}).get(
        "environment"
    )
    if production_environment == target_environment:
        expected_env_sha256 = deployment_plan["production_env_sha256"]
    elif production_environment == current_environment:
        expected_env_sha256 = deployment_plan["production_env_baseline_sha256"]
    else:
        raise ContractError("rollback production env drifted outside the sealed transition")
    if hash_file(Path(deployment_plan["production_env_file"])) != expected_env_sha256:
        raise ContractError("rollback production env SHA-256 drifted")
    tool_repo_root = tool_repo_root.resolve()
    if Path(str(deployment_plan.get("tool_repo_root", ""))).resolve() != tool_repo_root:
        raise ContractError("rollback tool_repo_root changed")
    if capture_git_identity(tool_repo_root, git_runner) != deployment_plan.get(
        "deployment_tool_revision"
    ):
        raise ContractError("rollback deployment tool revision changed")
    production_compose_file = Path(
        str(deployment_plan.get("production_compose_file", ""))
    ).resolve()
    production_project_dir = Path(
        str(deployment_plan.get("production_project_dir", ""))
    ).resolve()
    if production_project_dir == tool_repo_root:
        raise ContractError(
            "rollback tool_repo_root and production_project_dir must be "
            "different directories"
        )
    image_ref = validate_rollback_image_ref(manifest["rollback_image_ref"])
    expected_id = validate_image_id(
        manifest["rollback_image_id"], "rollback_image_id"
    )
    record = runtime.image_record(image_ref)
    if record.get("id") != expected_id:
        raise ContractError(
            f"rollback tag ID mismatch: expected {expected_id}, got {record.get('id')}"
        )
    rollback_environment = dict(current_environment)
    compose, raw_config, images = runtime.compose_config(
        tool_repo_root,
        image_ref,
        environment=rollback_environment,
        project_directory=production_project_dir,
        compose_file=production_compose_file,
    )
    compose_sha = validate_compose_result(
        compose,
        raw_config,
        images,
        image_ref,
    )
    _validate_formal_spread_semantics(
        compose,
        production_project_dir,
        image_ref,
        manifest["formal_git_commit"],
        production_environment[WEATHER_RUNTIME_ENV_KEY],
    )
    return {
        "phase": "pre-rollback",
        "rollback_image_ref": image_ref,
        "rollback_image_id": expected_id,
        "compose_project": compose["name"],
        "compose_image": compose["services"][COMPOSE_SERVICE]["image"],
        "compose_config_sha256": compose_sha,
    }


def verify_post_rollback(
    manifest: Mapping[str, Any],
    runtime: ReleaseRuntime,
    *,
    deployment_plan: Mapping[str, Any] | None = None,
    production_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if deployment_plan is None or production_environment is None:
        raise ContractError("post-rollback verification requires a sealed deployment plan")
    current_environment = deployment_plan.get("current_production", {}).get(
        "environment"
    )
    if production_environment != current_environment:
        raise ContractError("rolled-back production env does not match the sealed baseline")
    if hash_file(Path(deployment_plan["production_env_file"])) != deployment_plan[
        "production_env_baseline_sha256"
    ]:
        raise ContractError("rolled-back production env SHA-256 mismatch")
    expected_ref = validate_rollback_image_ref(manifest["rollback_image_ref"])
    expected_id = validate_image_id(
        manifest["rollback_image_id"], "rollback_image_id"
    )
    container = runtime.container_record(PRODUCTION_CONTAINER)
    if container.get("image_id") != expected_id:
        raise ContractError(
            "rolled-back container Image ID mismatch: "
            f"expected {expected_id}, got {container.get('image_id')}"
        )
    if container.get("config_image") != expected_ref:
        raise ContractError(
            "rolled-back container Config.Image mismatch: "
            f"expected {expected_ref}, got {container.get('config_image')!r}"
        )
    runtime_git_commit = _verified_runtime_git_commit(
        container,
        manifest["formal_git_commit"],
        "rolled-back container",
    )
    return {
        "phase": "post-rollback",
        "container_name": PRODUCTION_CONTAINER,
        "config_image": container["config_image"],
        "actual_image_id": container["image_id"],
        "runtime_git_commit": runtime_git_commit,
        "runtime_git_commit_verified": True,
    }


def collect_data_baseline(
    data_host_root: Path,
    inspection_container_name: str,
    runtime: ReleaseRuntime,
    *,
    captured_at: str | None = None,
) -> dict[str, Any]:
    if not SAFE_CONTAINER_RE.fullmatch(inspection_container_name):
        raise ContractError("data inspection container name is unsafe")
    root = data_host_root.resolve()
    datasets: list[dict[str, Any]] = []
    for spec in DATA_SPECS:
        host_path = (root / Path(spec.relative_host_path)).resolve()
        try:
            host_path.relative_to(root)
        except ValueError as exc:
            raise ContractError(f"data path escapes host root: {host_path}") from exc
        if SENSITIVE_VALUE_RE.search(str(host_path)):
            raise ContractError(f"sensitive data path is not allowed: {host_path}")
        digest = hash_file(host_path)
        stat = host_path.stat()
        stats = runtime.dataset_stats(inspection_container_name, spec)
        records = stats.get("records")
        latest = stats.get("latest_business_date")
        primary_key_null_rows = stats.get("primary_key_null_rows")
        duplicate_rows = stats.get("duplicate_rows_on_key")
        invalid_date_rows = stats.get("invalid_date_rows")
        if not isinstance(records, int) or records < 1:
            raise ContractError(f"{spec.name} record count is invalid")
        if not isinstance(latest, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", latest):
            raise ContractError(f"{spec.name} latest business date is invalid")
        if not isinstance(primary_key_null_rows, int) or primary_key_null_rows < 0:
            raise ContractError(f"{spec.name} primary key null count is invalid")
        if spec.name == "spread" and primary_key_null_rows != 0:
            raise ContractError(f"{spec.name} primary key contains null values")
        if not isinstance(duplicate_rows, int) or duplicate_rows < 0:
            raise ContractError(f"{spec.name} duplicate count is invalid")
        if spec.name == "spread" and duplicate_rows != 0:
            raise ContractError(f"{spec.name} contains duplicate primary keys")
        if not isinstance(invalid_date_rows, int) or invalid_date_rows < 0:
            raise ContractError(f"{spec.name} invalid date count is invalid")
        if spec.name == "spread" and invalid_date_rows != 0:
            raise ContractError(f"{spec.name} contains invalid business dates")
        datasets.append(
            {
                "name": spec.name,
                "host_path": str(host_path),
                "container_path": spec.container_path,
                "delivery": "bind_mount",
                "persistence": "host",
                "filename": host_path.name,
                "sha256": digest,
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).isoformat().replace("+00:00", "Z"),
                "records": records,
                "latest_business_date": latest,
                "primary_key": list(spec.primary_key),
                "primary_key_null_rows": primary_key_null_rows,
                "duplicate_rows_on_key": duplicate_rows,
                "update_task": spec.update_task,
                "writes_to": "host_through_bind_mount",
            }
        )
    timestamp = captured_at or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    validate_build_time(timestamp)
    return {
        "kind": "deployment-time",
        "captured_at": timestamp,
        "mutable_after_deployment": True,
        "datasets": datasets,
    }


def create_manifest(
    *,
    repository: Path,
    data_host_root: Path,
    release_id: str,
    git_commit: str,
    image_ref: str,
    expected_image_id: str,
    build_time: str,
    source: str,
    candidate_container_name: str,
    rollback_image_ref: str,
    rollback_image_id: str,
    formal_git_commit: str,
    production_environment: Mapping[str, str],
    runtime: ReleaseRuntime,
    git_runner: CommandRunner | None = None,
    weather_candidate_mode: str = WEATHER_CANDIDATE_MODE_NEXT,
    data_inspection_container_name: str = PRODUCTION_CONTAINER,
) -> dict[str, Any]:
    repository = repository.resolve()
    validate_full_git_commit(git_commit)
    validate_release_id(release_id, git_commit, build_time)
    validate_release_image_ref(image_ref, release_id)
    validate_image_id(expected_image_id)
    validate_source(source)
    validate_rollback_image_ref(rollback_image_ref)
    validate_image_id(rollback_image_id, "rollback_image_id")
    validate_full_git_commit(formal_git_commit)
    if formal_git_commit == git_commit:
        raise ContractError("formal_git_commit must differ from the candidate commit")
    validate_manifest_production_environment(production_environment)
    weather_facts = weather_candidate_facts(weather_candidate_mode)
    if (
        not SAFE_CONTAINER_RE.fullmatch(candidate_container_name)
        or candidate_container_name == PRODUCTION_CONTAINER
    ):
        raise ContractError("candidate container name is invalid")

    validate_repository_static(repository)
    git_tree = validate_git_state(repository, git_commit, git_runner)

    image = runtime.image_record(image_ref)
    if image.get("id") != expected_image_id:
        raise ContractError(
            f"release tag ID mismatch: expected {expected_image_id}, got {image.get('id')}"
        )
    labels = image.get("labels") or {}
    expected_labels = {
        "org.opencontainers.image.revision": git_commit,
        "org.opencontainers.image.version": release_id,
        "org.opencontainers.image.created": build_time,
        "org.opencontainers.image.source": source,
    }
    for key, expected in expected_labels.items():
        if labels.get(key) != expected:
            raise ContractError(
                f"candidate image label {key} mismatch: "
                f"expected {expected}, got {labels.get(key)!r}"
            )
    release = runtime.read_image_release(image_ref)
    release_sha256 = runtime.image_release_sha256(image_ref)
    identity = {
        "release_id": release_id,
        "git_commit": git_commit,
        "git_tree": git_tree,
        "build_time": build_time,
    }
    _validate_embedded_release(release, identity, source)

    rollback = runtime.image_record(rollback_image_ref)
    if rollback.get("id") != rollback_image_id:
        raise ContractError(
            "rollback tag ID mismatch before manifest creation: "
            f"expected {rollback_image_id}, got {rollback.get('id')}"
        )

    compose, raw_config, images = runtime.compose_config(
        repository,
        image_ref,
        environment=candidate_compose_environment(
            git_commit,
            production_environment,
            weather_facts["weather_candidate_mode"],
        ),
    )
    validate_compose_result(compose, raw_config, images, image_ref)
    baseline = collect_data_baseline(
        data_host_root,
        data_inspection_container_name,
        runtime,
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "application": APPLICATION,
        "release_id": release_id,
        "git_commit": git_commit,
        "git_tree": git_tree,
        "image_ref": image_ref,
        "image_id": expected_image_id,
        "image_oci_revision": labels["org.opencontainers.image.revision"],
        "image_release_json_sha256": release_sha256,
        "build_time": build_time,
        "compose_project": COMPOSE_PROJECT,
        "compose_files": ["docker-compose.yml"],
        "compose_template_sha256": hash_file(repository / "docker-compose.yml"),
        "runtime_environment_contract": copy.deepcopy(RUNTIME_ENVIRONMENT_CONTRACT),
        "weather_runtime_contract": weather_runtime_contract(
            production_environment[WEATHER_RUNTIME_ENV_KEY],
            weather_facts["weather_candidate_mode"],
        ),
        **weather_facts,
        "candidate_user_url_environment": candidate_user_url_environment(
            production_environment
        ),
        "readiness_policy": copy.deepcopy(DEFAULT_READINESS_POLICY),
        "formal_container_identity_contract": copy.deepcopy(
            FORMAL_CONTAINER_IDENTITY_CONTRACT
        ),
        "dockerfile_sha256": hash_file(repository / "Dockerfile"),
        "dockerignore_sha256": hash_file(repository / ".dockerignore"),
        "required_config_sha256": hash_file(
            repository / "02_configs/historical_spread_config.xlsx"
        ),
        "candidate_container_name": candidate_container_name,
        "rollback_image_ref": rollback_image_ref,
        "rollback_image_id": rollback_image_id,
        "formal_git_commit": formal_git_commit,
        "data_baseline": baseline,
        "status": "candidate_sealed",
    }
    schema = load_schema(
        repository / "09_deploy/spread_release/release.schema.json"
    )
    validate_manifest(manifest, schema)
    return manifest


def render_release_env(manifest: Mapping[str, Any]) -> str:
    values = {
        "RELEASE_ID": manifest["release_id"],
        "SPREAD_IMAGE": manifest["image_ref"],
        "EXPECTED_IMAGE_ID": manifest["image_id"],
        "EXPECTED_GIT_COMMIT": manifest["git_commit"],
    }
    validate_release_env(values, manifest)
    return "".join(f"{key}={values[key]}\n" for key in RELEASE_ENV_KEYS)


def write_release_bundle(
    manifest: Mapping[str, Any],
    output_root: Path,
) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    release_id = str(manifest["release_id"])
    target = output_root / release_id
    if target.exists():
        raise ContractError(
            f"release directory already exists and will not be overwritten: {target}"
        )
    temp_dir = Path(
        tempfile.mkdtemp(prefix=f".{release_id}.", dir=str(output_root))
    )
    try:
        manifest_path = temp_dir / "release.json"
        env_path = temp_dir / "release.env"
        checksums_path = temp_dir / "checksums.sha256"
        _write_json_exclusive(
            manifest_path,
            manifest,
            description="release artifact",
        )
        env_path.write_text(render_release_env(manifest), encoding="utf-8")
        checksums_path.write_text(
            f"{hash_file(manifest_path)}  release.json\n"
            f"{hash_file(env_path)}  release.env\n",
            encoding="utf-8",
        )
        release_artifact_manifest = write_artifact_manifest(
            manifest_path,
            artifact_type="release",
            target_schema_version=str(manifest["schema_version"]),
            release_id=str(manifest["release_id"]),
            git_commit=str(manifest["git_commit"]),
            git_tree=str(manifest["git_tree"]),
            image_id=str(manifest["image_id"]),
        )
        if os.name != "nt":
            for path in (
                manifest_path,
                env_path,
                checksums_path,
                release_artifact_manifest,
            ):
                path.chmod(0o444)
        os.replace(temp_dir, target)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return target


def validate_deployment_result(
    result: Mapping[str, Any],
    manifest: Mapping[str, Any],
    deployment_plan: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> None:
    validate_against_schema(result, schema)
    expected = {
        "schema_version": DEPLOYMENT_RESULT_SCHEMA_VERSION,
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
        "compose_project": deployment_plan["compose_project"],
        "production_service": deployment_plan["production_service"],
        "production_env_file": deployment_plan["production_env_file"],
        "production_env_before_sha256": deployment_plan[
            "production_env_baseline_sha256"
        ],
        "production_env_after_sha256": deployment_plan["production_env_sha256"],
        "production_env_sha256": deployment_plan["production_env_sha256"],
        "production_compose_sha256": deployment_plan[
            "production_compose_sha256"
        ],
        "deployment_tool_revision": deployment_plan["deployment_tool_revision"],
        "current_production": deployment_plan["current_production"],
        "target_release": deployment_plan["target_release"],
        "status": "production_verified",
    }
    for key, expected_value in expected.items():
        if result.get(key) != expected_value:
            raise ContractError(f"deployment result {key} mismatch")
    formal_containers = result.get("formal_containers")
    require_formal_containers_unchanged(
        formal_containers, "deployment result"
    )
    if (
        formal_containers.get("before_phase") != "pre-deploy"
        or formal_containers.get("after_phase") != "post-deploy"
        or formal_containers.get("before")
        != deployment_plan.get("formal_containers", {}).get("after")
    ):
        raise ContractError("deployment result formal container baseline mismatch")
    weather_facts = weather_candidate_facts(manifest.get("weather_candidate_mode"))
    for key, expected_value in weather_facts.items():
        if result.get(key) != expected_value:
            raise ContractError(f"deployment result {key} mismatch")
    if result.get("weather_runtime_contract") != deployment_plan.get(
        "weather_runtime_contract"
    ):
        raise ContractError("deployment result weather runtime contract mismatch")
    if result.get("readiness", {}).get("expected_image_id") != manifest["image_id"]:
        raise ContractError("deployment result readiness Image ID mismatch")
    if result.get("readiness", {}).get("policy") != deployment_plan[
        "readiness_policy"
    ]:
        raise ContractError("deployment result readiness policy mismatch")
    if result.get("http_status") != 200:
        raise ContractError("deployment result HTTP status is not successful")
    validate_build_time(result.get("generated_at"))
    assert_no_sensitive_values(result, "deployment result")


def write_result(path: Path, result: Mapping[str, Any]) -> None:
    validate_against_schema(
        result,
        load_schema(Path(__file__).with_name("deployment_result.schema.json")),
    )
    write_json_artifact(
        path,
        result,
        artifact_type="deployment_result",
        release_id=str(result["release_id"]),
        git_commit=str(result["git_commit"]),
        git_tree=str(result["git_tree"]),
        image_id=str(result["candidate_image_id"]),
        runtime_git_commit=str(result["runtime_git_commit"]),
    )


def load_deployment_result(
    path: Path,
    manifest: Mapping[str, Any],
    deployment_plan: Mapping[str, Any],
) -> dict[str, Any]:
    result, artifact_manifest = load_verified_json_artifact(
        path,
        artifact_type="deployment_result",
        expected_release_id=str(manifest["release_id"]),
        expected_git_commit=str(manifest["git_commit"]),
        expected_git_tree=str(manifest["git_tree"]),
        expected_image_id=str(manifest["image_id"]),
    )
    validate_deployment_result(
        result,
        manifest,
        deployment_plan,
        load_schema(Path(__file__).with_name("deployment_result.schema.json")),
    )
    if artifact_manifest.get("runtime_git_commit") != result.get(
        "runtime_git_commit"
    ):
        raise ContractError(
            "deployment_result manifest runtime_git_commit mismatch"
        )
    return result


def deployment_result_bundle_path(result_path: Path) -> Path:
    result_path = result_path.resolve()
    expected_result = DEPLOYMENT_RESULT_BUNDLE_MEMBER_FILENAMES[0]
    if result_path.name != expected_result:
        raise ContractError(
            f"deployment result bundle target must be {expected_result}, "
            f"got {result_path.name}"
        )
    return result_path.with_name(DEPLOYMENT_RESULT_BUNDLE_FILENAME)


def create_deployment_result_bundle(
    result_path: Path,
    manifest: Mapping[str, Any],
    deployment_plan: Mapping[str, Any],
) -> dict[str, Any]:
    result_path = result_path.resolve()
    result = load_deployment_result(result_path, manifest, deployment_plan)
    members = []
    for filename in DEPLOYMENT_RESULT_BUNDLE_MEMBER_FILENAMES:
        member_path = result_path.with_name(filename)
        if not member_path.is_file():
            raise ContractError(
                f"deployment result bundle member is missing: {member_path}"
            )
        members.append(
            {
                "target_file": filename,
                "target_sha256": hash_file(member_path),
                "target_size_bytes": member_path.stat().st_size,
            }
        )
    bundle = {
        "schema_version": DEPLOYMENT_RESULT_BUNDLE_SCHEMA_VERSION,
        "release_id": result["release_id"],
        "git_commit": result["git_commit"],
        "git_tree": result["git_tree"],
        "image_id": result["candidate_image_id"],
        "runtime_git_commit": result["runtime_git_commit"],
        "generated_at": result["generated_at"],
        "members": members,
    }
    validate_against_schema(
        bundle,
        load_schema(
            Path(__file__).with_name("deployment_result_bundle.schema.json")
        ),
    )
    return bundle


def validate_deployment_result_bundle(
    bundle_path: Path,
    manifest: Mapping[str, Any],
    deployment_plan: Mapping[str, Any],
) -> dict[str, Any]:
    bundle_path = bundle_path.resolve()
    if bundle_path.name != DEPLOYMENT_RESULT_BUNDLE_FILENAME:
        raise ContractError(
            "deployment result bundle must be "
            f"{DEPLOYMENT_RESULT_BUNDLE_FILENAME}"
        )
    result_path = bundle_path.with_name(
        DEPLOYMENT_RESULT_BUNDLE_MEMBER_FILENAMES[0]
    )
    result = load_deployment_result(result_path, manifest, deployment_plan)
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(
            f"cannot load deployment result bundle {bundle_path}: {exc}"
        ) from exc
    if not isinstance(bundle, dict):
        raise ContractError("deployment result bundle must be a JSON object")
    validate_against_schema(
        bundle,
        load_schema(
            Path(__file__).with_name("deployment_result_bundle.schema.json")
        ),
    )
    members = bundle["members"]
    member_filenames = [member["target_file"] for member in members]
    if tuple(member_filenames) != DEPLOYMENT_RESULT_BUNDLE_MEMBER_FILENAMES:
        raise ContractError(
            "deployment result bundle members must exactly and deterministically "
            f"equal {list(DEPLOYMENT_RESULT_BUNDLE_MEMBER_FILENAMES)!r}"
        )
    for member in members:
        member_path = result_path.with_name(member["target_file"])
        if not member_path.is_file():
            raise ContractError(
                f"deployment result bundle member is missing: {member_path}"
            )
        if member["target_sha256"] != hash_file(member_path):
            raise ContractError(
                "deployment result bundle member SHA-256 mismatch: "
                f"{member['target_file']}"
            )
        if member["target_size_bytes"] != member_path.stat().st_size:
            raise ContractError(
                "deployment result bundle member byte size mismatch: "
                f"{member['target_file']}"
            )
    expected_identity = {
        "release_id": result["release_id"],
        "git_commit": result["git_commit"],
        "git_tree": result["git_tree"],
        "image_id": result["candidate_image_id"],
        "runtime_git_commit": result["runtime_git_commit"],
        "generated_at": result["generated_at"],
    }
    for key, expected in expected_identity.items():
        if bundle.get(key) != expected:
            raise ContractError(f"deployment result bundle {key} mismatch")
    return bundle


def load_deployment_result_bundle(
    bundle_path: Path,
    manifest: Mapping[str, Any],
    deployment_plan: Mapping[str, Any],
) -> dict[str, Any]:
    return validate_deployment_result_bundle(
        bundle_path,
        manifest,
        deployment_plan,
    )


def write_deployment_result_bundle(
    result_path: Path,
    manifest: Mapping[str, Any],
    deployment_plan: Mapping[str, Any],
) -> Path:
    result_path = result_path.resolve()
    output = deployment_result_bundle_path(result_path)
    bundle = create_deployment_result_bundle(result_path, manifest, deployment_plan)
    installed_bundle = False
    try:
        _write_json_exclusive(
            output,
            bundle,
            description="deployment result bundle",
        )
        installed_bundle = True
        validate_deployment_result_bundle(output, manifest, deployment_plan)
    except Exception:
        if installed_bundle and output.exists():
            output.unlink()
        raise
    return output
