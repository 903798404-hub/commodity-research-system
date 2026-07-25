"""Prepare one isolated spread-dashboard candidate before existing release gates.

This is deliberately the *front* of the existing release chain.  It builds no
parallel manifest format and delegates immutable release sealing and readiness
validation to the established spread_release contract implementation.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import socket
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from release_contract import (
    COMPOSE_SERVICE,
    PRODUCTION_CONTAINER,
    WEATHER_CONTAINER_CURRENT_PATH,
    WEATHER_CONTAINER_PATH,
    ContractError,
    CommandRunner,
    DockerReleaseRuntime,
    ReleaseRuntime,
    candidate_compose_environment,
    capture_formal_container_snapshot,
    collect_data_baseline,
    create_candidate_result,
    create_deployment_plan,
    create_manifest,
    hash_file,
    load_candidate_result,
    load_deployment_plan,
    load_manifest_bundle,
    load_schema,
    parse_production_env,
    validate_build_time,
    validate_full_git_commit,
    validate_release_id,
    validate_release_image_ref,
    validate_repository_static,
    validate_image_id,
    validate_rollback_image_ref,
    validate_source,
    validate_weather_runtime_dir,
    validate_git_state,
    validate_formal_container_snapshot,
    write_candidate_result,
    write_deployment_plan,
    write_formal_container_snapshot,
    write_release_bundle,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPOSITORY = SCRIPT_DIR.parents[1]
DEFAULT_PORT_START = 18501
DEFAULT_PORT_END = 18999
CANDIDATE_SERVICE = "spread-dashboard-candidate"
DEFAULT_CLEANUP_POLICY = "on-failure"
AFTER_PLAN_CLEANUP_POLICY = "after-plan"
SNAPSHOT_MAX_AGE_SECONDS = 300
TARGET_PRODUCTION_ENV_FILENAME = "production-target.env"


class CandidateValidator(Protocol):
    def __call__(self, release_directory: Path, health_url: str, output_directory: Path) -> None: ...


class FormalSnapshotter(Protocol):
    def __call__(self, runtime: ReleaseRuntime, output_directory: Path) -> tuple[Path, Mapping[str, Any]]: ...


class CandidateResultSealer(Protocol):
    def __call__(
        self,
        options: "CandidateOptions",
        runtime: ReleaseRuntime,
        production_environment: Mapping[str, str],
        release_directory: Path,
        formal_snapshot_path: Path,
        output_directory: Path,
        formal_spread_before: Mapping[str, Any],
        candidate_health_url: str,
    ) -> Mapping[str, str]: ...


class DeploymentPlanSealer(Protocol):
    def __call__(
        self,
        options: "CandidateOptions",
        runtime: ReleaseRuntime,
        production_environment: Mapping[str, str],
        release_directory: Path,
        candidate_result_path: Path,
    ) -> Mapping[str, str]: ...


@dataclass(frozen=True)
class CandidateOptions:
    mode: str
    git_commit: str
    git_tree: str
    build_context: Path
    production_compose_file: Path
    production_env_file: Path
    image_ref: str
    release_id: str
    build_time: str
    source: str
    output_directory: Path
    candidate_container_name: str
    candidate_port: int | None
    auto_port: bool
    data_host_root: Path | None
    rollback_image_ref: str | None
    rollback_image_id: str | None
    formal_git_commit: str | None
    cleanup_policy: str
    execute_build: bool
    execute_start: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
    except FileExistsError as exc:
        raise ContractError(f"candidate artifact already exists: {path}") from exc
    return path


def _validate_output_parent(path: Path) -> None:
    """Reject an output destination that the deployment user cannot own or write."""
    ancestor = path.parent.resolve()
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
        raise ContractError("candidate output parent is not writable")
    if os.name != "nt" and ancestor.stat().st_uid == 0 and os.geteuid() != 0:
        raise ContractError("candidate output parent must not be a root-owned deployment path")


def _normalise_port(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise ContractError(f"{field} must be a TCP port")
    return value


def _port_is_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def select_candidate_port(
    requested_port: int | None,
    *,
    forbidden_ports: set[int],
    probe: Callable[[int], bool] = _port_is_available,
) -> int:
    """Select a free loopback-only port from the fixed candidate range."""
    if requested_port is not None:
        port = _normalise_port(requested_port, "candidate port")
        if port in forbidden_ports or not DEFAULT_PORT_START <= port <= DEFAULT_PORT_END:
            raise ContractError("candidate port is reserved or outside the approved range")
        if not probe(port):
            raise ContractError("requested candidate port is already in use")
        return port
    for port in range(DEFAULT_PORT_START, DEFAULT_PORT_END + 1):
        if port not in forbidden_ports and probe(port):
            return port
    raise ContractError("no free loopback candidate port is available")


def _published_ports(compose: Mapping[str, Any]) -> set[int]:
    ports: set[int] = {8501, 8080, 8081}
    services = compose.get("services")
    if not isinstance(services, dict):
        raise ContractError("formal Compose services are missing")
    for service in services.values():
        if not isinstance(service, dict):
            raise ContractError("formal Compose service is invalid")
        for port in service.get("ports") or []:
            if isinstance(port, dict):
                published = port.get("published")
                if str(published).isdigit():
                    ports.add(int(published))
    return ports


def _formal_networks(
    formal_compose: Mapping[str, Any], service: Mapping[str, Any]
) -> dict[str, Any]:
    declared = formal_compose.get("networks") or {}
    service_networks = service.get("networks") or ["default"]
    if isinstance(service_networks, dict):
        names = list(service_networks)
    elif isinstance(service_networks, list) and all(isinstance(item, str) for item in service_networks):
        names = list(service_networks)
    else:
        raise ContractError("formal spread networks are invalid")
    project_name = formal_compose.get("name")
    if not isinstance(project_name, str) or not project_name:
        raise ContractError("formal Compose project name is missing")
    result: dict[str, Any] = {}
    for name in names:
        specification = declared.get(name, {}) if isinstance(declared, dict) else {}
        if specification is None:
            specification = {}
        if not isinstance(specification, dict):
            raise ContractError("formal Compose network is invalid")
        network_name = specification.get("name") or f"{project_name}_{name}"
        if not isinstance(network_name, str) or not network_name:
            raise ContractError("formal Compose network name is invalid")
        # Joining the existing formal network is explicit; the candidate project
        # must not create, rename, or remove a production network.
        result[name] = {"external": True, "name": network_name}
    return result


def _weather_mount(service: Mapping[str, Any]) -> Mapping[str, Any]:
    matches = [
        volume
        for volume in service.get("volumes") or []
        if isinstance(volume, dict) and volume.get("target") == WEATHER_CONTAINER_PATH
    ]
    if len(matches) != 1:
        raise ContractError("formal spread Compose must contain exactly one weather mount")
    mount = matches[0]
    if mount.get("type") != "bind" or mount.get("read_only") is not True:
        raise ContractError("weather runtime mount must be a read-only bind mount")
    source = mount.get("source")
    if not isinstance(source, str):
        raise ContractError("weather runtime mount source is invalid")
    validate_weather_runtime_dir(source)
    return mount


def build_candidate_compose(
    formal_compose: Mapping[str, Any],
    production_environment: Mapping[str, str],
    *,
    git_commit: str,
    image_ref: str,
    candidate_container_name: str,
    candidate_port: int,
) -> dict[str, Any]:
    """Derive a one-service candidate Compose document from formal Compose facts."""
    services = formal_compose.get("services")
    if not isinstance(services, dict) or COMPOSE_SERVICE not in services:
        raise ContractError("formal Compose is missing spread-dashboard")
    formal_service = services[COMPOSE_SERVICE]
    if not isinstance(formal_service, dict):
        raise ContractError("formal spread-dashboard service is invalid")
    if image_ref == formal_service.get("image"):
        raise ContractError("candidate image must not reuse the formal spread image tag")
    if candidate_container_name == PRODUCTION_CONTAINER:
        raise ContractError("candidate container name must not equal spread-dashboard")
    weather_mount = _weather_mount(formal_service)
    expected_weather_source = production_environment["WEATHER_RUNTIME_CURRENT_DIR"]
    if weather_mount.get("source") != expected_weather_source:
        raise ContractError("formal weather mount does not match the formal environment")
    formal_environment = formal_service.get("environment") or {}
    if not isinstance(formal_environment, dict):
        raise ContractError("formal spread environment is invalid")
    candidate_environment = dict(formal_environment)
    candidate_environment.update(
        candidate_compose_environment(git_commit, production_environment, "current")
    )
    # WEATHER_RUNTIME_CURRENT_DIR is an interpolation input, never an application
    # environment variable.  Its value is represented by the resolved bind source.
    candidate_environment.pop("WEATHER_RUNTIME_CURRENT_DIR", None)
    if candidate_environment.get("WEATHER_DATA_DIR") != WEATHER_CONTAINER_CURRENT_PATH:
        raise ContractError("candidate WEATHER_DATA_DIR must use weather/current")

    candidate_service = copy.deepcopy(formal_service)
    candidate_service["image"] = image_ref
    candidate_service["container_name"] = candidate_container_name
    candidate_service["restart"] = "no"
    candidate_service["ports"] = [
        {
            "mode": "ingress",
            "protocol": "tcp",
            "published": str(candidate_port),
            "target": 8501,
            "host_ip": "127.0.0.1",
        }
    ]
    candidate_service["environment"] = candidate_environment
    # Candidate Compose contains no USDA/Oil World service, so retaining this
    # production-only relationship would be invalid and could create siblings.
    candidate_service.pop("depends_on", None)
    candidate_project = f"spread-candidate-{git_commit[:12]}-c01"
    candidate = {
        "name": candidate_project,
        "services": {CANDIDATE_SERVICE: candidate_service},
        "networks": _formal_networks(formal_compose, formal_service),
    }
    validate_candidate_compose(
        candidate,
        image_ref=image_ref,
        candidate_container_name=candidate_container_name,
        candidate_port=candidate_port,
        expected_weather_source=expected_weather_source,
        expected_git_commit=git_commit,
    )
    return candidate


def validate_candidate_compose(
    candidate: Mapping[str, Any],
    *,
    image_ref: str,
    candidate_container_name: str,
    candidate_port: int,
    expected_weather_source: str,
    expected_git_commit: str,
) -> None:
    services = candidate.get("services")
    if not isinstance(services, dict) or set(services) != {CANDIDATE_SERVICE}:
        raise ContractError("candidate Compose must contain exactly one candidate spread service")
    service = services[CANDIDATE_SERVICE]
    if not isinstance(service, dict):
        raise ContractError("candidate spread service is invalid")
    if service.get("image") != image_ref:
        raise ContractError("candidate image does not match the requested image")
    if service.get("container_name") != candidate_container_name:
        raise ContractError("candidate container name does not match the requested name")
    if service.get("container_name") == PRODUCTION_CONTAINER:
        raise ContractError("candidate Compose targets the production container")
    if service.get("restart") not in {"no", "none"}:
        raise ContractError("candidate restart policy must be disabled")
    if "depends_on" in service:
        raise ContractError("candidate Compose must not retain production depends_on")
    if service.get("ports") != [
        {
            "mode": "ingress",
            "protocol": "tcp",
            "published": str(candidate_port),
            "target": 8501,
            "host_ip": "127.0.0.1",
        }
    ]:
        raise ContractError("candidate port must be exactly 127.0.0.1:<temporary>:8501")
    environment = service.get("environment")
    if not isinstance(environment, dict):
        raise ContractError("candidate environment is invalid")
    if environment.get("MARKET_DATA_GIT_HEAD") != expected_git_commit:
        raise ContractError("candidate MARKET_DATA_GIT_HEAD does not match the target commit")
    if environment.get("WEATHER_DATA_DIR") != WEATHER_CONTAINER_CURRENT_PATH:
        raise ContractError("candidate WEATHER_DATA_DIR is invalid")
    mounts = [
        mount
        for mount in service.get("volumes") or []
        if isinstance(mount, dict) and mount.get("target") == WEATHER_CONTAINER_PATH
    ]
    if len(mounts) != 1 or mounts[0].get("source") != expected_weather_source:
        raise ContractError("candidate weather mount source is invalid")
    if mounts[0].get("type") != "bind" or mounts[0].get("read_only") is not True:
        raise ContractError("candidate weather mount must remain read-only")
    networks = candidate.get("networks")
    if not isinstance(networks, dict) or not networks:
        raise ContractError("candidate must explicitly join the formal network")
    if any(not isinstance(value, dict) or value.get("external") is not True for value in networks.values()):
        raise ContractError("candidate networks must be external formal networks")


def _compose_command(options: CandidateOptions, compose_path: Path, project_name: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        project_name,
        "-f",
        str(compose_path),
        "up",
        "-d",
        "--no-deps",
        CANDIDATE_SERVICE,
    ]


def _build_command(options: CandidateOptions) -> list[str]:
    return [
        "docker",
        "build",
        "--file",
        str(options.build_context / "Dockerfile"),
        "--tag",
        options.image_ref,
        "--build-arg",
        f"MARKET_DATA_GIT_HEAD={options.git_commit}",
        "--build-arg",
        f"MARKET_DATA_GIT_TREE={options.git_tree}",
        "--build-arg",
        f"MARKET_DATA_RELEASE_ID={options.release_id}",
        "--build-arg",
        f"MARKET_DATA_BUILD_TIME={options.build_time}",
        "--build-arg",
        f"MARKET_DATA_SOURCE={options.source}",
        "--label",
        f"org.opencontainers.image.revision={options.git_commit}",
        "--label",
        f"org.opencontainers.image.created={options.build_time}",
        "--label",
        f"org.opencontainers.image.source={options.source}",
        "--label",
        f"market-data.git.tree={options.git_tree}",
        "--label",
        "market-data.release.type=candidate",
        str(options.build_context),
    ]


def _load_formal_compose(options: CandidateOptions, runner: CommandRunner) -> dict[str, Any]:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(options.production_env_file),
        "--project-directory",
        str(options.production_compose_file.parent),
        "-f",
        str(options.production_compose_file),
        "config",
        "--format",
        "json",
    ]
    raw = runner.run(command, cwd=options.production_compose_file.parent)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractError(f"formal docker compose config returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ContractError("formal docker compose config must be an object")
    return parsed


def _ensure_candidate_name(value: str, git_commit: str) -> str:
    if (
        not value
        or value == PRODUCTION_CONTAINER
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", value)
    ):
        raise ContractError("candidate container name is unsafe")
    expected_prefix = f"spread-dashboard-candidate-{git_commit[:12]}-"
    if not value.startswith(expected_prefix):
        raise ContractError("candidate container name must be derived from the target Git SHA")
    return value


def _validate_options(options: CandidateOptions, runner: CommandRunner) -> tuple[dict[str, str], dict[str, Any]]:
    if options.mode not in {"dry-run", "execute"}:
        raise ContractError("mode must be dry-run or execute")
    if options.mode == "dry-run" and (options.execute_build or options.execute_start):
        raise ContractError("dry-run must not request Docker build or Docker start")
    if options.mode == "execute" and not (options.execute_build and options.execute_start):
        raise ContractError("execute mode requires explicit --execute-build and --execute-start")
    if options.mode == "execute":
        if not all(
            [
                options.data_host_root,
                options.rollback_image_ref,
                options.rollback_image_id,
                options.formal_git_commit,
            ]
        ):
            raise ContractError(
                "execute mode requires data host root, rollback image identity, and formal Git commit"
            )
        if not options.data_host_root or not options.data_host_root.is_dir():
            raise ContractError("execute data host root is missing")
        validate_rollback_image_ref(options.rollback_image_ref)
        validate_image_id(options.rollback_image_id, "rollback_image_id")
        validate_full_git_commit(options.formal_git_commit)
    if options.cleanup_policy not in {
        DEFAULT_CLEANUP_POLICY,
        AFTER_PLAN_CLEANUP_POLICY,
    }:
        raise ContractError("candidate cleanup policy is invalid")
    validate_full_git_commit(options.git_commit)
    validate_full_git_commit(options.git_tree)
    validate_build_time(options.build_time)
    validate_source(options.source)
    validate_release_id(options.release_id, options.git_commit, options.build_time)
    validate_release_image_ref(options.image_ref, options.release_id)
    _ensure_candidate_name(options.candidate_container_name, options.git_commit)
    if not options.build_context.is_dir():
        raise ContractError("build context directory is missing")
    if not options.production_compose_file.is_file():
        raise ContractError("production Compose file is missing")
    validate_repository_static(options.build_context)
    actual_tree = validate_git_state(options.build_context, options.git_commit, runner)
    if actual_tree != options.git_tree:
        raise ContractError("build context Tree SHA does not match the requested Tree SHA")
    production_environment = parse_production_env(options.production_env_file)
    formal_compose = _load_formal_compose(options, runner)
    if not options.auto_port and options.candidate_port is None:
        raise ContractError("provide --candidate-port or --auto-port")
    if options.auto_port and options.candidate_port is not None:
        raise ContractError("--candidate-port and --auto-port are mutually exclusive")
    _validate_output_parent(options.output_directory)
    return production_environment, formal_compose


def _default_release_sealer(
    options: CandidateOptions,
    runtime: ReleaseRuntime,
    production_environment: Mapping[str, str],
) -> Path:
    if not all(
        [
            options.data_host_root,
            options.rollback_image_ref,
            options.rollback_image_id,
            options.formal_git_commit,
        ]
    ):
        raise ContractError(
            "execute mode requires data host root, rollback image identity, and formal Git commit"
        )
    manifest = create_manifest(
        repository=options.build_context,
        data_host_root=options.data_host_root,
        release_id=options.release_id,
        git_commit=options.git_commit,
        image_ref=options.image_ref,
        expected_image_id=runtime.image_record(options.image_ref)["id"],
        build_time=options.build_time,
        source=options.source,
        candidate_container_name=options.candidate_container_name,
        rollback_image_ref=options.rollback_image_ref,
        rollback_image_id=options.rollback_image_id,
        formal_git_commit=options.formal_git_commit,
        production_environment=production_environment,
        runtime=runtime,
        weather_candidate_mode="current",
    )
    return write_release_bundle(manifest, options.output_directory / "releases")


def _default_validator(release_directory: Path, health_url: str, output_directory: Path) -> None:
    runner = CommandRunner()
    runner.run(
        [
            "bash",
            str(SCRIPT_DIR / "validate_spread_candidate.sh"),
            str(release_directory),
            health_url,
            str(output_directory / "candidate_readiness.json"),
            str(output_directory / "candidate_readiness.failure.json"),
        ]
    )


def _default_formal_snapshotter(
    runtime: ReleaseRuntime, output_directory: Path
) -> tuple[Path, Mapping[str, Any]]:
    """Seal the existing formal-container snapshot before candidate startup."""
    snapshot = capture_formal_container_snapshot(runtime)
    path = write_formal_container_snapshot(
        output_directory / "formal_containers.before_candidate.json", snapshot
    )
    # Re-load through the same validation boundary used by downstream artifacts.
    parsed = json.loads(path.read_text(encoding="utf-8"))
    return path, validate_formal_container_snapshot(parsed)


def _load_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load {description}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{description} must be a JSON object")
    return value


def _http_status(url: str) -> int:
    """Return an actual loopback HTTP status for the isolated candidate."""
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - loopback URL is constructed here.
            return int(response.status)
    except HTTPError as exc:
        return int(exc.code)
    except URLError as exc:
        raise ContractError(f"candidate HTTP request failed for {url}: {exc.reason}") from exc


def _candidate_check_evidence(
    health_url: str,
    readiness: Mapping[str, Any],
    *,
    formal_spread_before: Mapping[str, Any],
    formal_spread_after: Mapping[str, Any],
    data_baseline_before: Mapping[str, Any],
    data_baseline_after: Mapping[str, Any],
) -> dict[str, Any]:
    """Build candidate-result checks from live evidence, never operator defaults."""
    last_http = readiness.get("last_http_result") or {}
    if last_http.get("http_status") != 200:
        raise ContractError("candidate readiness HTTP status is not 200")
    base_url = health_url.removesuffix("/_stcore/health")
    if not base_url.startswith("http://127.0.0.1:"):
        raise ContractError("candidate HTTP checks require a loopback candidate URL")
    root_status = _http_status(f"{base_url}/")
    host_config_status = _http_status(f"{base_url}/_stcore/host-config")
    if root_status != 200 or host_config_status != 200:
        raise ContractError(
            "candidate HTTP validation failed: "
            f"root={root_status}, host_config={host_config_status}"
        )
    if dict(formal_spread_before) != dict(formal_spread_after):
        raise ContractError("formal spread-dashboard identity changed during candidate validation")
    if data_baseline_before.get("datasets") != data_baseline_after.get("datasets"):
        raise ContractError("formal data baseline changed during candidate validation")
    return {
        "http": {"health": 200, "host_config": host_config_status, "root": root_status},
        # The Streamlit entry point and its host configuration were fetched from
        # the running candidate.  Detailed visual acceptance remains a separate
        # human gate; this is the contract's machine-readable page entry check.
        "pages": {"status": "passed"},
        "formal_git_unchanged": True,
        "data_files_unchanged": True,
        "production_switch_performed": False,
    }


def _default_candidate_result_sealer(
    options: CandidateOptions,
    runtime: ReleaseRuntime,
    production_environment: Mapping[str, str],
    release_directory: Path,
    formal_snapshot_path: Path,
    output_directory: Path,
    formal_spread_before: Mapping[str, Any],
    candidate_health_url: str,
) -> Mapping[str, str]:
    """Seal candidate_result with the existing generator and schema boundary."""
    del production_environment
    manifest_path = release_directory / "release.json"
    manifest, _ = load_manifest_bundle(
        manifest_path,
        release_directory / "release.env",
        SCRIPT_DIR / "release.schema.json",
    )
    readiness_path = output_directory / "candidate_readiness.json"
    readiness = _load_json_object(readiness_path, "candidate readiness result")
    if readiness.get("status") != "ready":
        raise ContractError("candidate readiness result is not ready")
    formal_spread_after = runtime.container_record(PRODUCTION_CONTAINER)
    if options.data_host_root is None:
        raise ContractError("candidate result requires a data host root")
    data_baseline_after = collect_data_baseline(
        options.data_host_root,
        PRODUCTION_CONTAINER,
        runtime,
    )
    checks = _candidate_check_evidence(
        candidate_health_url,
        readiness,
        formal_spread_before=formal_spread_before,
        formal_spread_after=formal_spread_after,
        data_baseline_before=manifest["data_baseline"],
        data_baseline_after=data_baseline_after,
    )
    checks_path = output_directory / "candidate_checks.json"
    _write_json_exclusive(checks_path, checks)
    formal_snapshot = _load_json_object(formal_snapshot_path, "formal snapshot")
    candidate_result = create_candidate_result(
        manifest=manifest,
        runtime=runtime,
        readiness=readiness,
        checks={**checks, "formal_containers_before": formal_snapshot},
    )
    candidate_result_path = write_candidate_result(
        candidate_result, release_directory / "candidate_result.json"
    )
    # Loading validates both the schema and its artifact manifest.
    load_candidate_result(candidate_result_path, manifest)
    return {
        "candidate_checks_path": str(checks_path),
        "candidate_result_path": str(candidate_result_path),
        "candidate_result_sha256": hash_file(candidate_result_path),
    }


def _default_deployment_plan_sealer(
    options: CandidateOptions,
    runtime: ReleaseRuntime,
    production_environment: Mapping[str, str],
    release_directory: Path,
    candidate_result_path: Path,
) -> Mapping[str, str]:
    """Seal deployment_plan only after the candidate container is gone.

    The supplied production environment identifies the *currently running*
    formal service.  A candidate plan instead needs the immutable target
    environment that Compose will use for the eventual one-service switch.
    Seal that small derived environment in the release bundle rather than
    mutating the operator-managed production environment file during a
    candidate gate.
    """
    manifest_path = release_directory / "release.json"
    manifest, _ = load_manifest_bundle(
        manifest_path,
        release_directory / "release.env",
        SCRIPT_DIR / "release.schema.json",
    )
    target_environment = _target_production_environment(production_environment, manifest)
    target_environment_path = release_directory / TARGET_PRODUCTION_ENV_FILENAME
    _write_target_production_environment(target_environment_path, target_environment)
    plan = create_deployment_plan(
        tool_repo_root=options.build_context,
        production_compose_file=options.production_compose_file,
        production_project_dir=options.production_compose_file.parent,
        candidate_result_file=candidate_result_path,
        production_env_file=target_environment_path,
        manifest=manifest,
        runtime=runtime,
        schema=load_schema(SCRIPT_DIR / "deployment_plan.schema.json"),
    )
    deployment_plan_path = write_deployment_plan(
        plan, release_directory / "deployment_plan.json"
    )
    load_deployment_plan(deployment_plan_path, manifest)
    return {
        "deployment_plan_path": str(deployment_plan_path),
        "deployment_plan_sha256": hash_file(deployment_plan_path),
        "target_production_env_file": str(target_environment_path),
        "target_production_env_sha256": hash_file(target_environment_path),
    }


def _write_target_production_environment(
    path: Path, environment: Mapping[str, str]
) -> None:
    """Write and re-parse the sealed deployment environment without secrets."""
    if path.exists():
        raise ContractError("target production environment already exists")
    content = "".join(f"{key}={value}\n" for key, value in environment.items())
    path.write_text(content, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    if parse_production_env(path) != dict(environment):
        raise ContractError("sealed target production environment did not round-trip")


def _target_production_environment(
    production_environment: Mapping[str, str], manifest: Mapping[str, Any]
) -> dict[str, str]:
    """Derive the target-only overlay while preserving formal URL/data inputs."""
    target = dict(production_environment)
    target["SPREAD_IMAGE"] = str(manifest["image_ref"])
    target["MARKET_DATA_GIT_HEAD"] = str(manifest["git_commit"])
    return target


def _remove_candidate_container(
    runtime: ReleaseRuntime, runner: CommandRunner, container_name: str
) -> list[str]:
    """Remove and re-check only this named candidate container."""
    if not runtime.container_exists(container_name):
        raise ContractError("candidate container is already absent before cleanup")
    command = ["docker", "rm", "-f", container_name]
    runner.run(command)
    if runtime.container_exists(container_name):
        raise ContractError("candidate container still exists after cleanup")
    return command


def _remove_candidate_image(
    runtime: ReleaseRuntime, runner: CommandRunner, image_ref: str
) -> list[str]:
    """Remove only the validated candidate tag after plan sealing."""
    record = runtime.image_record(image_ref)
    if not isinstance(record.get("id"), str):
        raise ContractError("candidate image identity is missing before cleanup")
    command = ["docker", "image", "rm", image_ref]
    runner.run(command)
    try:
        runtime.image_record(image_ref)
    except ContractError:
        return command
    raise ContractError("candidate image tag still resolves after cleanup")


def _best_effort_cleanup(
    runtime: ReleaseRuntime, runner: CommandRunner, container_name: str, image_ref: str
) -> list[list[str]]:
    """Failure-path cleanup records only candidate actions and never masks the cause."""
    commands: list[list[str]] = []
    try:
        if runtime.container_exists(container_name):
            command = ["docker", "rm", "-f", container_name]
            runner.run(command)
            commands.append(command)
    except ContractError:
        commands.append(["docker", "rm", "-f", container_name, "# cleanup-failed"])
    try:
        command = ["docker", "image", "rm", image_ref]
        runner.run(command)
        commands.append(command)
    except ContractError:
        commands.append(["docker", "image", "rm", image_ref, "# cleanup-failed"])
    return commands


def prepare_candidate(
    options: CandidateOptions,
    *,
    runner: CommandRunner | None = None,
    runtime: ReleaseRuntime | None = None,
    port_probe: Callable[[int], bool] = _port_is_available,
    release_sealer: Callable[[CandidateOptions, ReleaseRuntime, Mapping[str, str]], Path] = _default_release_sealer,
    validator: CandidateValidator = _default_validator,
    formal_snapshotter: FormalSnapshotter = _default_formal_snapshotter,
    candidate_result_sealer: CandidateResultSealer = _default_candidate_result_sealer,
    deployment_plan_sealer: DeploymentPlanSealer = _default_deployment_plan_sealer,
) -> dict[str, Any]:
    """Generate a dry plan or execute the candidate-only preflight sequence."""
    command_runner = runner or CommandRunner()
    production_environment, formal_compose = _validate_options(options, command_runner)
    port = select_candidate_port(
        options.candidate_port,
        forbidden_ports=_published_ports(formal_compose),
        probe=port_probe,
    )
    candidate_compose = build_candidate_compose(
        formal_compose,
        production_environment,
        git_commit=options.git_commit,
        image_ref=options.image_ref,
        candidate_container_name=options.candidate_container_name,
        candidate_port=port,
    )
    if options.output_directory.exists():
        raise ContractError("candidate output directory must not already exist")
    options.output_directory.mkdir(parents=True)
    compose_path = options.output_directory / "candidate-compose.json"
    _write_json_exclusive(compose_path, candidate_compose)
    project_name = str(candidate_compose["name"])
    build_command = _build_command(options)
    start_command = _compose_command(options, compose_path, project_name)
    snapshot_path = options.output_directory / "formal_containers.before_candidate.json"
    cleanup_commands: list[list[str]] = [
        ["docker", "rm", "-f", options.candidate_container_name],
        ["docker", "image", "rm", options.image_ref],
    ]
    result: dict[str, Any] = {
        "kind": "candidate_prepare_result",
        "generated_at": _utc_now(),
        "status": "planned",
        "mode": options.mode,
        "git_commit": options.git_commit,
        "git_tree": options.git_tree,
        "build_context": str(options.build_context),
        "production_compose_file": str(options.production_compose_file),
        "production_env_file": str(options.production_env_file),
        "candidate_image_ref": options.image_ref,
        "candidate_container_name": options.candidate_container_name,
        "candidate_project_name": project_name,
        "candidate_service": CANDIDATE_SERVICE,
        "candidate_port": port,
        "candidate_bind": f"127.0.0.1:{port}",
        "candidate_health_url": f"http://127.0.0.1:{port}/_stcore/health",
        "candidate_compose_file": str(compose_path),
        "build_command": build_command,
        "start_command": start_command,
        "formal_snapshot_command": [sys.executable, str(SCRIPT_DIR / "capture_formal_container_snapshot.py"), "--output", str(snapshot_path)],
        "candidate_result_command": [sys.executable, str(SCRIPT_DIR / "create_candidate_result.py")],
        "deployment_plan_command": [sys.executable, str(SCRIPT_DIR / "create_deployment_plan.py")],
        "cleanup_commands": cleanup_commands,
        "weather_mount": {
            "source": production_environment["WEATHER_RUNTIME_CURRENT_DIR"],
            "target": WEATHER_CONTAINER_PATH,
            "read_only": True,
            "weather_data_dir": WEATHER_CONTAINER_CURRENT_PATH,
        },
        "candidate_image_id": None,
        "candidate_container_id": None,
        "formal_snapshot_path": str(snapshot_path),
        "formal_snapshot_sha256": None,
        "formal_snapshot_captured_at": None,
        "candidate_started_at": None,
        "candidate_result_path": None,
        "deployment_plan_path": None,
        "candidate_container_removed": False,
        "candidate_image_removed": False,
        "formal_spread_before": None,
        "formal_spread_after_candidate_cleanup": None,
        "failure_phase": None,
    }
    result_path = options.output_directory / "candidate_prepare_result.json"
    if options.mode == "dry-run":
        result["formal_snapshot_status"] = "not-executed"
        result["candidate_result_status"] = "not-generated"
        result["deployment_plan_status"] = "not-generated"
        _write_json_exclusive(result_path, result)
        return result

    candidate_runtime = runtime or DockerReleaseRuntime(command_runner)
    started = False
    built = False
    phase = "preflight"
    try:
        if candidate_runtime.container_exists(options.candidate_container_name):
            raise ContractError("candidate container name already exists")
        try:
            candidate_runtime.image_record(options.image_ref)
        except ContractError:
            pass
        else:
            raise ContractError("candidate image tag already exists; refusing to overwrite it")
        phase = "formal_snapshot"
        formal_snapshot_path, formal_snapshot = formal_snapshotter(
            candidate_runtime, options.output_directory
        )
        if formal_snapshot_path.resolve().parent != options.output_directory.resolve():
            raise ContractError("formal snapshot must be created in this candidate output directory")
        result["formal_snapshot_path"] = str(formal_snapshot_path)
        result["formal_snapshot_sha256"] = hash_file(formal_snapshot_path)
        captured_at = formal_snapshot.get("captured_at")
        captured_time = validate_build_time(captured_at)
        result["formal_snapshot_captured_at"] = captured_at
        snapshot_checked_at = validate_build_time(_utc_now())
        if captured_time >= snapshot_checked_at:
            raise ContractError("formal snapshot must precede candidate preparation")
        if (snapshot_checked_at - captured_time).total_seconds() > SNAPSHOT_MAX_AGE_SECONDS:
            raise ContractError("formal snapshot is too old for this candidate startup")
        formal_spread_before = candidate_runtime.container_record(PRODUCTION_CONTAINER)
        result["formal_spread_before"] = formal_spread_before
        phase = "build"
        built = True
        command_runner.run(build_command, cwd=options.build_context)
        image = candidate_runtime.image_record(options.image_ref)
        result["candidate_image_id"] = image["id"]
        phase = "seal-release"
        release_directory = release_sealer(options, candidate_runtime, production_environment)
        result["release_directory"] = str(release_directory)
        # Compose may create a container before reporting an error, so cleanup
        # begins from the attempted-start boundary rather than its exit status.
        started = True
        phase = "compose-start"
        candidate_started_at = _utc_now()
        candidate_started_time = validate_build_time(candidate_started_at)
        if captured_time >= candidate_started_time:
            raise ContractError("formal snapshot must precede candidate container startup")
        result["candidate_started_at"] = candidate_started_at
        command_runner.run(start_command, cwd=options.output_directory)
        container_id = command_runner.run(
            ["docker", "inspect", "--format", "{{.Id}}", options.candidate_container_name]
        ).strip()
        if len(container_id) != 64 or any(character not in "0123456789abcdef" for character in container_id):
            raise ContractError("candidate container did not return a full container ID")
        result["candidate_container_id"] = container_id
        phase = "candidate-validation"
        validator(release_directory, str(result["candidate_health_url"]), options.output_directory)
        phase = "candidate-result"
        candidate_evidence = candidate_result_sealer(
            options, candidate_runtime, production_environment, release_directory,
            formal_snapshot_path, options.output_directory, formal_spread_before,
            str(result["candidate_health_url"]),
        )
        result.update(candidate_evidence)
        result["candidate_result_path"] = candidate_evidence["candidate_result_path"]
        phase = "candidate-container-cleanup"
        result["candidate_container_cleanup_command"] = _remove_candidate_container(
            candidate_runtime, command_runner, options.candidate_container_name
        )
        result["candidate_container_removed"] = True
        formal_spread_after_cleanup = candidate_runtime.container_record(PRODUCTION_CONTAINER)
        if formal_spread_after_cleanup != formal_spread_before:
            raise ContractError("formal spread-dashboard changed during candidate cleanup")
        result["formal_spread_after_candidate_cleanup"] = formal_spread_after_cleanup
        image_after_container_cleanup = candidate_runtime.image_record(options.image_ref)
        if image_after_container_cleanup.get("id") != result["candidate_image_id"]:
            raise ContractError("candidate image identity changed before deployment plan sealing")
        phase = "deployment-plan"
        plan_evidence = deployment_plan_sealer(
            options,
            candidate_runtime,
            production_environment,
            release_directory,
            Path(str(result["candidate_result_path"])),
        )
        result.update(plan_evidence)
        result["deployment_plan_path"] = plan_evidence["deployment_plan_path"]
        if options.cleanup_policy == AFTER_PLAN_CLEANUP_POLICY:
            phase = "candidate-image-cleanup"
            result["candidate_image_cleanup_command"] = _remove_candidate_image(
                candidate_runtime, command_runner, options.image_ref
            )
            result["candidate_image_removed"] = True
        else:
            result["candidate_image_retained_for_deployment"] = True
        result["status"] = "prepared"
    except ContractError as exc:
        result["status"] = "failed"
        result["failure_phase"] = phase
        match = re.search(r"command failed \((\d+)\):", str(exc))
        result["failure_exit_code"] = int(match.group(1)) if match else None
        result["failure"] = str(exc)
        if started or built:
            result["cleanup_commands_executed"] = _best_effort_cleanup(
                candidate_runtime, command_runner, options.candidate_container_name, options.image_ref
            )
        _write_json_exclusive(result_path, result)
        raise
    _write_json_exclusive(result_path, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare an isolated spread candidate before existing release sealing and result gates."
    )
    parser.add_argument("--mode", choices=("dry-run", "execute"), default="dry-run")
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--git-tree", required=True)
    parser.add_argument("--build-context", type=Path, required=True)
    parser.add_argument("--production-compose-file", type=Path, required=True)
    parser.add_argument("--production-env-file", type=Path, required=True)
    parser.add_argument("--candidate-image", dest="image_ref", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--build-time", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--candidate-output-dir", type=Path, required=True)
    parser.add_argument("--candidate-container-name")
    port_group = parser.add_mutually_exclusive_group(required=True)
    port_group.add_argument("--candidate-port", type=int)
    port_group.add_argument("--auto-port", action="store_true")
    parser.add_argument(
        "--cleanup-policy",
        choices=(DEFAULT_CLEANUP_POLICY, AFTER_PLAN_CLEANUP_POLICY),
        default=DEFAULT_CLEANUP_POLICY,
        help=(
            "Retain the validated image for a later formal deployment, or remove "
            "it only after deployment_plan sealing during a validation-only run."
        ),
    )
    parser.add_argument("--execute-build", action="store_true")
    parser.add_argument("--execute-start", action="store_true")
    # Required only when execute mode seals the existing release manifest.
    parser.add_argument("--data-host-root", type=Path)
    parser.add_argument("--rollback-image-ref")
    parser.add_argument("--rollback-image-id")
    parser.add_argument("--formal-git-commit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    git_commit = args.git_commit.lower()
    name = args.candidate_container_name or f"spread-dashboard-candidate-{git_commit[:12]}-c01"
    options = CandidateOptions(
        mode=args.mode,
        git_commit=git_commit,
        git_tree=args.git_tree.lower(),
        build_context=args.build_context.resolve(),
        production_compose_file=args.production_compose_file.resolve(),
        production_env_file=args.production_env_file.resolve(),
        image_ref=args.image_ref,
        release_id=args.release_id,
        build_time=args.build_time,
        source=args.source,
        output_directory=args.candidate_output_dir.resolve(),
        candidate_container_name=name,
        candidate_port=args.candidate_port,
        auto_port=args.auto_port,
        data_host_root=args.data_host_root.resolve() if args.data_host_root else None,
        rollback_image_ref=args.rollback_image_ref,
        rollback_image_id=args.rollback_image_id,
        formal_git_commit=args.formal_git_commit.lower() if args.formal_git_commit else None,
        cleanup_policy=args.cleanup_policy,
        execute_build=args.execute_build,
        execute_start=args.execute_start,
    )
    try:
        result = prepare_candidate(options)
    except ContractError as exc:
        print(f"candidate preparation rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
