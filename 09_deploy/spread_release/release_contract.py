from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlparse


APPLICATION = "spread-dashboard"
COMPOSE_PROJECT = "market-data"
COMPOSE_SERVICE = "spread-dashboard"
PRODUCTION_CONTAINER = "spread-dashboard"
SCHEMA_VERSION = "1.0.0"
RELEASE_ENV_KEYS = (
    "RELEASE_ID",
    "SPREAD_IMAGE",
    "EXPECTED_IMAGE_ID",
    "EXPECTED_GIT_COMMIT",
)

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

    def read_candidate_release(self, container_name: str) -> dict[str, Any]: ...

    def candidate_release_sha256(self, container_name: str) -> str: ...

    def read_image_release(self, image_ref: str) -> dict[str, Any]: ...

    def image_release_sha256(self, image_ref: str) -> str: ...

    def read_container_release(self, container_name: str) -> dict[str, Any]: ...

    def container_release_sha256(self, container_name: str) -> str: ...

    def compose_config(
        self, repository: Path, image_ref: str
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
        return {"image_id": image_id, "config_image": config_image}

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
        self, repository: Path, image_ref: str
    ) -> tuple[dict[str, Any], str, list[str]]:
        compose_file = repository / "docker-compose.yml"
        base = [
            "docker",
            "compose",
            "--project-directory",
            str(repository),
            "-f",
            str(compose_file),
        ]
        environment = {"SPREAD_IMAGE": image_ref}
        raw_config = self.runner.run(
            [*base, "config", "--format", "json"],
            cwd=repository,
            env=environment,
        )
        try:
            parsed = json.loads(raw_config)
        except json.JSONDecodeError as exc:
            raise ContractError(f"docker compose config returned invalid JSON: {exc}") from exc
        raw_images = self.runner.run(
            [*base, "config", "--images"],
            cwd=repository,
            env=environment,
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


def _parse_release_json(raw: str, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{description} RELEASE.json is invalid: {exc}") from exc
    required = {
        "application",
        "release_id",
        "git_commit",
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
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("build_time must be a valid RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError("build_time must include an explicit timezone")
    return parsed


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
) -> None:
    validate_full_git_commit(expected_commit)
    command_runner = runner or CommandRunner()
    status = command_runner.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repository
    )
    if status.strip():
        raise ContractError("git worktree must be clean before sealing a release")
    head = command_runner.run(["git", "rev-parse", "HEAD"], cwd=repository).strip()
    validate_full_git_commit(head)
    if head != expected_commit:
        raise ContractError(f"git HEAD mismatch: expected {expected_commit}, got {head}")


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


def validate_repository_static(repository: Path) -> None:
    compose_path = repository / "docker-compose.yml"
    dockerfile_path = repository / "Dockerfile"
    dockerignore_path = repository / ".dockerignore"
    deploy_path = repository / "09_deploy/spread_release/deploy_spread_release.sh"
    rollback_path = repository / "09_deploy/spread_release/rollback_spread_release.sh"
    required_config = repository / "02_configs/historical_spread_config.xlsx"
    for path in (
        compose_path,
        dockerfile_path,
        dockerignore_path,
        deploy_path,
        rollback_path,
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
    if not re.search(
        r"(?ms)^\s{2}spread-dashboard:\s*\n.*?^\s{4}build:\s*$", compose_text
    ):
        raise ContractError("spread Compose service must retain its build definition")

    dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
    for argument in (
        "MARKET_DATA_GIT_HEAD",
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
        if "--no-build" not in script or "--no-deps" not in script:
            raise ContractError(
                f"{description} command must include --no-build and --no-deps"
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
    if expected_type and not _matches_json_type(value, expected_type):
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


def validate_manifest(manifest: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    validate_against_schema(manifest, schema)
    commit = validate_full_git_commit(manifest.get("git_commit"))
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
    validate_sha256(manifest.get("compose_config_sha256"), "compose_config_sha256")
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


def load_manifest_bundle(
    manifest_path: Path,
    env_path: Path,
    schema_path: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load release manifest {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ContractError("release manifest must be a JSON object")
    schema = load_schema(schema_path)
    validate_manifest(manifest, schema)
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


def _validate_embedded_release(
    release: Mapping[str, Any],
    manifest: Mapping[str, Any],
    expected_source: str,
) -> None:
    expected = {
        "application": APPLICATION,
        "release_id": manifest["release_id"],
        "git_commit": manifest["git_commit"],
        "build_time": manifest["build_time"],
        "source": expected_source,
    }
    if dict(release) != expected:
        raise ContractError("image /app/RELEASE.json does not match the release manifest")


def verify_pre_deploy(
    manifest: Mapping[str, Any],
    repository: Path,
    runtime: ReleaseRuntime,
    *,
    git_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    validate_repository_static(repository)
    validate_git_state(repository, manifest["git_commit"], git_runner)
    for path, field in (
        (repository / "Dockerfile", "dockerfile_sha256"),
        (repository / ".dockerignore", "dockerignore_sha256"),
        (
            repository / "02_configs/historical_spread_config.xlsx",
            "required_config_sha256",
        ),
    ):
        actual = hash_file(path)
        if actual != manifest[field]:
            raise ContractError(f"{field} mismatch: expected {manifest[field]}, got {actual}")

    image_evidence = _verify_image_identity(manifest, runtime)
    compose, raw_config, images = runtime.compose_config(
        repository, manifest["image_ref"]
    )
    compose_sha = validate_compose_result(
        compose, raw_config, images, manifest["image_ref"]
    )
    if compose_sha != manifest["compose_config_sha256"]:
        raise ContractError(
            "resolved Compose configuration changed after the manifest was sealed"
        )
    return {
        "phase": "pre-deploy",
        **image_evidence,
        "compose_project": compose["name"],
        "compose_image": compose["services"][COMPOSE_SERVICE]["image"],
        "compose_config_sha256": compose_sha,
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
        "embedded_release": release,
        "embedded_release_sha256": release_sha256,
    }


def verify_post_deploy(
    manifest: Mapping[str, Any],
    runtime: ReleaseRuntime,
) -> dict[str, Any]:
    container = runtime.container_record(PRODUCTION_CONTAINER)
    if container.get("image_id") != manifest["image_id"]:
        raise ContractError(
            "production container Image ID mismatch: "
            f"expected {manifest['image_id']}, got {container.get('image_id')}"
        )
    if container.get("config_image") != manifest["image_ref"]:
        raise ContractError(
            "production container Config.Image mismatch: "
            f"expected {manifest['image_ref']}, got {container.get('config_image')!r}"
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
    return {
        "phase": "post-deploy",
        "container_name": PRODUCTION_CONTAINER,
        "config_image": container["config_image"],
        "actual_image_id": container["image_id"],
        "oci_revision": labels["org.opencontainers.image.revision"],
        "embedded_release": release,
        "embedded_release_sha256": release_sha256,
    }


def verify_pre_rollback(
    manifest: Mapping[str, Any],
    repository: Path,
    runtime: ReleaseRuntime,
) -> dict[str, Any]:
    image_ref = validate_rollback_image_ref(manifest["rollback_image_ref"])
    expected_id = validate_image_id(
        manifest["rollback_image_id"], "rollback_image_id"
    )
    record = runtime.image_record(image_ref)
    if record.get("id") != expected_id:
        raise ContractError(
            f"rollback tag ID mismatch: expected {expected_id}, got {record.get('id')}"
        )
    compose, raw_config, images = runtime.compose_config(repository, image_ref)
    compose_sha = validate_compose_result(
        compose,
        raw_config,
        images,
        image_ref,
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
) -> dict[str, Any]:
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
    return {
        "phase": "post-rollback",
        "container_name": PRODUCTION_CONTAINER,
        "config_image": container["config_image"],
        "actual_image_id": container["image_id"],
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
    runtime: ReleaseRuntime,
    git_runner: CommandRunner | None = None,
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
    if (
        not SAFE_CONTAINER_RE.fullmatch(candidate_container_name)
        or candidate_container_name == PRODUCTION_CONTAINER
    ):
        raise ContractError("candidate container name is invalid")

    validate_repository_static(repository)
    validate_git_state(repository, git_commit, git_runner)

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
        "build_time": build_time,
    }
    _validate_embedded_release(release, identity, source)

    rollback = runtime.image_record(rollback_image_ref)
    if rollback.get("id") != rollback_image_id:
        raise ContractError(
            "rollback tag ID mismatch before manifest creation: "
            f"expected {rollback_image_id}, got {rollback.get('id')}"
        )

    compose, raw_config, images = runtime.compose_config(repository, image_ref)
    compose_sha = validate_compose_result(compose, raw_config, images, image_ref)
    baseline = collect_data_baseline(
        data_host_root,
        PRODUCTION_CONTAINER,
        runtime,
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "application": APPLICATION,
        "release_id": release_id,
        "git_commit": git_commit,
        "image_ref": image_ref,
        "image_id": expected_image_id,
        "image_oci_revision": labels["org.opencontainers.image.revision"],
        "image_release_json_sha256": release_sha256,
        "build_time": build_time,
        "compose_project": COMPOSE_PROJECT,
        "compose_files": ["docker-compose.yml"],
        "compose_config_sha256": compose_sha,
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
        manifest_path.write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        env_path.write_text(render_release_env(manifest), encoding="utf-8")
        checksums_path.write_text(
            f"{hash_file(manifest_path)}  release.json\n"
            f"{hash_file(env_path)}  release.env\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            for path in (manifest_path, env_path, checksums_path):
                path.chmod(0o444)
        os.replace(temp_dir, target)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return target


def write_result(path: Path, result: Mapping[str, Any]) -> None:
    if path.exists():
        raise ContractError(f"result file already exists: {path}")
    payload = {
        **result,
        "verified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    assert_no_sensitive_values(payload, "deployment result")
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
