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
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

from release_contract import (
    COMPOSE_SERVICE,
    CANDIDATE_WAITING_STATUS,
    IMPORT_PROFIT_RUNTIME_CONTAINER_PATH,
    IMPORT_PROFIT_RUNTIME_ENV_KEY,
    IMPORT_PROFIT_RUNTIME_MOUNT_ID,
    PRODUCTION_CONTAINER,
    REAL_NIGHT_SESSION_CLOSE_GATE_ID,
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
    pending_candidate_gate,
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
CANDIDATE_DATA_CONTAINER_PATH = "/app/01_data"
CANDIDATE_OUTPUTS_CONTAINER_PATH = "/app/06_outputs"
CANDIDATE_LOGS_CONTAINER_PATH = "/app/10_logs"
CANDIDATE_USER_CACHE_CONTAINER_PATH = "/root/.cache"
CANDIDATE_TMP_CONTAINER_PATH = "/tmp"
CANDIDATE_RUNTIME_LAYOUT = {
    CANDIDATE_OUTPUTS_CONTAINER_PATH: "06_outputs",
    CANDIDATE_LOGS_CONTAINER_PATH: "10_logs",
    CANDIDATE_USER_CACHE_CONTAINER_PATH: "user-cache",
    CANDIDATE_TMP_CONTAINER_PATH: "tmp",
}
CANDIDATE_REPLACED_FORMAL_TARGETS = {
    CANDIDATE_OUTPUTS_CONTAINER_PATH,
    CANDIDATE_LOGS_CONTAINER_PATH,
}
DOCKER_SOCKET_PATH = "/var/run/docker.sock"


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
        candidate_runtime_access: Mapping[str, Any] | None,
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
    import_profit_runtime_host: Path | None = None
    import_profit_runtime_approved_root: Path | None = None
    formal_import_profit_runtime_host: Path | None = None
    import_profit_runtime_container: str = IMPORT_PROFIT_RUNTIME_CONTAINER_PATH
    pending_gate: str | None = None
    earliest_expected_business_date: str | None = None


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


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_import_profit_runtime_paths(options: CandidateOptions) -> Path | None:
    supplied = (
        options.import_profit_runtime_host,
        options.import_profit_runtime_approved_root,
        options.formal_import_profit_runtime_host,
    )
    if not any(supplied):
        if options.pending_gate or options.earliest_expected_business_date:
            raise ContractError("candidate gate requires an import profit runtime")
        return None
    if not all(supplied):
        raise ContractError(
            "import profit runtime requires host, approved root, and formal runtime paths"
        )
    runtime = options.import_profit_runtime_host
    approved_root = options.import_profit_runtime_approved_root
    formal_runtime = options.formal_import_profit_runtime_host
    assert runtime is not None and approved_root is not None and formal_runtime is not None
    if not runtime.is_absolute() or not approved_root.is_absolute() or not formal_runtime.is_absolute():
        raise ContractError("import profit runtime paths must be absolute")
    if not runtime.exists() or not runtime.is_dir():
        raise ContractError("import profit candidate runtime must be an existing directory")
    if not approved_root.exists() or not approved_root.is_dir():
        raise ContractError("approved import profit candidate root must be an existing directory")
    resolved_runtime = runtime.resolve(strict=True)
    resolved_root = approved_root.resolve(strict=True)
    resolved_formal = formal_runtime.resolve(strict=False)
    if resolved_runtime == resolved_root or not _is_relative_to(resolved_runtime, resolved_root):
        raise ContractError("import profit runtime must be below the approved candidate root")
    if (
        resolved_runtime == resolved_formal
        or _is_relative_to(resolved_runtime, resolved_formal)
        or _is_relative_to(resolved_formal, resolved_runtime)
    ):
        raise ContractError("candidate and formal import profit runtimes must not overlap")
    build_context = options.build_context.resolve(strict=True)
    if resolved_runtime == build_context or _is_relative_to(resolved_runtime, build_context):
        raise ContractError("import profit runtime must not be inside the Git checkout")
    container_path = options.import_profit_runtime_container
    pure = PurePosixPath(container_path)
    if (
        not container_path.startswith("/")
        or "\\" in container_path
        or any(part in {".", ".."} for part in pure.parts)
        or str(pure) != container_path
        or container_path != IMPORT_PROFIT_RUNTIME_CONTAINER_PATH
    ):
        raise ContractError("import profit runtime container path is invalid")
    if options.pending_gate != REAL_NIGHT_SESSION_CLOSE_GATE_ID:
        raise ContractError("import profit Stage A requires the controlled real 08:30 night-session pending gate")
    if not options.earliest_expected_business_date:
        raise ContractError("import profit pending gate requires an earliest business date")
    pending_candidate_gate(options.pending_gate, options.earliest_expected_business_date)
    return resolved_runtime


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


def normalized_mount_mode(mount: Mapping[str, object]) -> Literal["ro", "rw"]:
    """Return the Compose mount mode without coercing non-boolean values."""
    if "read_only" not in mount:
        return "rw"
    read_only = mount["read_only"]
    if not isinstance(read_only, bool):
        raise ContractError("candidate mount read_only must be a boolean when present")
    return "ro" if read_only is True else "rw"


def _formal_candidate_data_mount(
    formal_service: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    matches = [
        mount
        for mount in formal_service.get("volumes") or []
        if isinstance(mount, dict)
        and mount.get("target") == CANDIDATE_DATA_CONTAINER_PATH
    ]
    if len(matches) > 1:
        raise ContractError("formal Compose contains multiple /app/01_data mounts")
    if not matches:
        return None
    mount = matches[0]
    if mount.get("type") != "bind" or not isinstance(mount.get("source"), str):
        raise ContractError("formal /app/01_data mount must be a host bind")
    normalized_mount_mode(mount)
    return mount


def validate_candidate_data_path(
    options: CandidateOptions,
    formal_service: Mapping[str, Any],
) -> tuple[Path, Mapping[str, Any]]:
    """Resolve the formal data source that the candidate may mount only read-only."""
    formal_mount = _formal_candidate_data_mount(formal_service)
    if formal_mount is None:
        raise ContractError("formal Compose is missing the required /app/01_data mount")
    if options.data_host_root is None or not options.data_host_root.is_dir():
        raise ContractError("candidate requires the existing formal data host root")
    formal_source = Path(str(formal_mount["source"]))
    if not formal_source.is_absolute():
        raise ContractError("formal /app/01_data source must be absolute after Compose rendering")
    try:
        resolved_formal = formal_source.resolve(strict=True)
    except OSError as exc:
        raise ContractError("formal /app/01_data source is missing") from exc
    manifest_root = options.data_host_root.resolve(strict=True)
    try:
        expected_formal = (manifest_root / "01_data").resolve(strict=True)
    except OSError as exc:
        raise ContractError("formal data host root is missing 01_data") from exc
    if resolved_formal != expected_formal:
        raise ContractError(
            "formal /app/01_data source must equal <data-host-root>/01_data"
        )
    return resolved_formal, formal_mount


def candidate_runtime_paths(output_directory: Path) -> dict[str, Path]:
    """Return the fixed candidate-only writable mount layout."""
    runtime_root = output_directory.resolve() / "runtime"
    return {
        target: runtime_root / relative
        for target, relative in CANDIDATE_RUNTIME_LAYOUT.items()
    }


def _create_candidate_runtime(paths: Mapping[str, Path]) -> None:
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=False)


def validate_candidate_runtime_location(
    options: CandidateOptions, formal_service: Mapping[str, Any]
) -> dict[str, Path]:
    """Keep every candidate-writable host path outside production and the checkout."""
    output = options.output_directory.resolve()
    protected = [options.build_context.resolve(strict=True)]
    if options.data_host_root is not None:
        protected.append(options.data_host_root.resolve(strict=True))
    for mount in formal_service.get("volumes") or []:
        if not isinstance(mount, dict) or normalized_mount_mode(mount) != "rw":
            continue
        source = mount.get("source")
        if isinstance(source, str) and Path(source).is_absolute():
            protected.append(Path(source).resolve(strict=True))
    for protected_path in protected:
        if (
            output == protected_path
            or _is_relative_to(output, protected_path)
            or _is_relative_to(protected_path, output)
        ):
            raise ContractError("candidate output/runtime must not overlap production or the Git checkout")
    return candidate_runtime_paths(output)


def build_candidate_compose(
    formal_compose: Mapping[str, Any],
    production_environment: Mapping[str, str],
    *,
    git_commit: str,
    image_ref: str,
    candidate_container_name: str,
    candidate_port: int,
    formal_data_host_root: Path,
    candidate_runtime_mounts: Mapping[str, Path],
    import_profit_runtime_host: Path | None = None,
    import_profit_runtime_container: str = IMPORT_PROFIT_RUNTIME_CONTAINER_PATH,
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
    if import_profit_runtime_host is not None:
        candidate_environment[IMPORT_PROFIT_RUNTIME_ENV_KEY] = import_profit_runtime_container
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
    formal_volumes = copy.deepcopy(formal_service.get("volumes") or [])
    expected_runtime_targets = set(CANDIDATE_RUNTIME_LAYOUT)
    if set(candidate_runtime_mounts) != expected_runtime_targets:
        raise ContractError("candidate writable runtime mount set is incomplete")
    candidate_volumes: list[dict[str, Any]] = []
    seen_formal_targets: set[str] = set()
    for formal_mount in formal_volumes:
        if not isinstance(formal_mount, dict):
            raise ContractError("formal spread volume entry is invalid")
        source = formal_mount.get("source")
        target = formal_mount.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            raise ContractError("formal spread volume identity is invalid")
        if source == DOCKER_SOCKET_PATH or target == DOCKER_SOCKET_PATH:
            raise ContractError("candidate must not mount the Docker socket")
        if target in seen_formal_targets:
            raise ContractError("formal spread Compose contains duplicate mount targets")
        seen_formal_targets.add(target)
        candidate_mount = copy.deepcopy(formal_mount)
        if target == CANDIDATE_DATA_CONTAINER_PATH:
            candidate_mount["source"] = str(formal_data_host_root)
            candidate_mount["read_only"] = True
        elif target in CANDIDATE_REPLACED_FORMAL_TARGETS:
            candidate_mount["source"] = str(candidate_runtime_mounts[target])
            candidate_mount["read_only"] = False
        elif target != WEATHER_CONTAINER_PATH:
            raise ContractError("formal spread Compose contains an unapproved candidate mount")
        candidate_volumes.append(candidate_mount)
    required_formal_targets = {
        CANDIDATE_DATA_CONTAINER_PATH,
        CANDIDATE_OUTPUTS_CONTAINER_PATH,
        CANDIDATE_LOGS_CONTAINER_PATH,
        WEATHER_CONTAINER_PATH,
    }
    if seen_formal_targets != required_formal_targets:
        raise ContractError("formal spread mount set is incomplete or unexpected")
    for target in (
        CANDIDATE_USER_CACHE_CONTAINER_PATH,
        CANDIDATE_TMP_CONTAINER_PATH,
    ):
        candidate_volumes.append(
            {
                "type": "bind",
                "source": str(candidate_runtime_mounts[target]),
                "target": target,
                "read_only": False,
            }
        )
    if import_profit_runtime_host is not None:
        targets = {
            volume.get("target")
            for volume in candidate_volumes
            if isinstance(volume, dict)
        }
        candidate_target = PurePosixPath(import_profit_runtime_container)
        if any(
            isinstance(target, str)
            and (
                PurePosixPath(target) == candidate_target
                or PurePosixPath(target) in candidate_target.parents
                or candidate_target in PurePosixPath(target).parents
            )
            for target in targets
        ):
            raise ContractError("import profit runtime target conflicts with an existing mount")
        candidate_volumes.append(
            {
                "type": "bind",
                "source": str(import_profit_runtime_host),
                "target": import_profit_runtime_container,
                "read_only": False,
            }
        )
    candidate_service["volumes"] = candidate_volumes
    # The formal Compose service is explicitly labelled as production.  A
    # candidate must not inherit that runtime role when its one-service Compose
    # document is derived from the formal service.
    candidate_labels = dict(candidate_service.get("labels") or {})
    candidate_labels.update(
        {
            "market-data.deployment.role": "candidate",
            "market-data.deployment.git_sha": git_commit,
        }
    )
    candidate_service["labels"] = candidate_labels
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
        formal_volumes=formal_volumes,
        expected_formal_data_host_root=str(formal_data_host_root),
        expected_candidate_runtime_mounts={
            target: str(path) for target, path in candidate_runtime_mounts.items()
        },
        expected_import_profit_runtime_host=(
            str(import_profit_runtime_host) if import_profit_runtime_host is not None else None
        ),
        expected_import_profit_runtime_container=import_profit_runtime_container,
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
    formal_volumes: Sequence[Any],
    expected_formal_data_host_root: str,
    expected_candidate_runtime_mounts: Mapping[str, str],
    expected_import_profit_runtime_host: str | None = None,
    expected_import_profit_runtime_container: str = IMPORT_PROFIT_RUNTIME_CONTAINER_PATH,
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
    expected_runtime_environment = (
        expected_import_profit_runtime_container
        if expected_import_profit_runtime_host is not None
        else None
    )
    if environment.get(IMPORT_PROFIT_RUNTIME_ENV_KEY) != expected_runtime_environment:
        raise ContractError("candidate IMPORT_PROFIT_RUNTIME_ROOT is invalid")
    labels = service.get("labels")
    if not isinstance(labels, dict):
        raise ContractError("candidate runtime labels are invalid")
    if labels.get("market-data.deployment.role") != "candidate":
        raise ContractError("candidate must have the candidate deployment role")
    if labels.get("market-data.deployment.git_sha") != expected_git_commit:
        raise ContractError("candidate deployment Git SHA does not match the target commit")
    mounts = [
        mount
        for mount in service.get("volumes") or []
        if isinstance(mount, dict) and mount.get("target") == WEATHER_CONTAINER_PATH
    ]
    if len(mounts) != 1 or mounts[0].get("source") != expected_weather_source:
        raise ContractError("candidate weather mount source is invalid")
    if mounts[0].get("type") != "bind" or mounts[0].get("read_only") is not True:
        raise ContractError("candidate weather mount must remain read-only")
    volumes = service.get("volumes") or []
    expected_count = len(formal_volumes) + 2 + (1 if expected_import_profit_runtime_host else 0)
    if not isinstance(volumes, list) or len(volumes) != expected_count:
        raise ContractError("candidate mount set differs from the approved formal mount set")
    inherited = volumes[: len(formal_volumes)]
    for formal_mount, candidate_mount in zip(formal_volumes, inherited, strict=True):
        if not isinstance(formal_mount, dict) or not isinstance(candidate_mount, dict):
            raise ContractError("candidate mount entries must be objects")
        target = formal_mount.get("target")
        expected_mount = copy.deepcopy(formal_mount)
        if target == CANDIDATE_DATA_CONTAINER_PATH:
            expected_mount["source"] = expected_formal_data_host_root
            expected_mount["read_only"] = True
        elif target in CANDIDATE_REPLACED_FORMAL_TARGETS:
            expected_mount["source"] = expected_candidate_runtime_mounts[target]
            expected_mount["read_only"] = False
        expected_mode = normalized_mount_mode(expected_mount)
        candidate_mode = normalized_mount_mode(candidate_mount)
        expected_comparable = {
            key: value for key, value in expected_mount.items() if key != "read_only"
        }
        candidate_comparable = {
            key: value for key, value in candidate_mount.items() if key != "read_only"
        }
        if candidate_comparable != expected_comparable or candidate_mode != expected_mode:
            raise ContractError("candidate mount differs from the approved read-only/runtime contract")
        if target == CANDIDATE_DATA_CONTAINER_PATH and candidate_mode != "ro":
            raise ContractError("candidate /app/01_data mount must be read-only")
        if target in CANDIDATE_REPLACED_FORMAL_TARGETS and candidate_mode != "rw":
            raise ContractError("candidate outputs and logs mounts must be read-write")
    next_index = len(formal_volumes)
    for target in (
        CANDIDATE_USER_CACHE_CONTAINER_PATH,
        CANDIDATE_TMP_CONTAINER_PATH,
    ):
        runtime_mount = volumes[next_index]
        if not isinstance(runtime_mount, dict) or runtime_mount.get("type") != "bind":
            raise ContractError("candidate cache/tmp runtime mount is invalid")
        if (
            runtime_mount.get("source") != expected_candidate_runtime_mounts[target]
            or runtime_mount.get("target") != target
            or normalized_mount_mode(runtime_mount) != "rw"
        ):
            raise ContractError("candidate cache/tmp runtime mount is invalid")
        next_index += 1
    if expected_import_profit_runtime_host is not None:
        runtime_mount = volumes[next_index]
        if not isinstance(runtime_mount, dict):
            raise ContractError("candidate import profit runtime mount is invalid")
        source = runtime_mount.get("source")
        try:
            source_path = Path(source).resolve(strict=True) if isinstance(source, str) else None
            expected_source_path = Path(expected_import_profit_runtime_host).resolve(strict=True)
        except OSError as exc:
            raise ContractError("candidate import profit runtime mount is invalid") from exc
        if (
            runtime_mount.get("type") != "bind"
            or source_path != expected_source_path
            or runtime_mount.get("target") != expected_import_profit_runtime_container
            or normalized_mount_mode(runtime_mount) != "rw"
        ):
            raise ContractError("candidate import profit runtime mount is invalid")
    for mount in volumes:
        if not isinstance(mount, dict):
            raise ContractError("candidate mount entry is invalid")
        if mount.get("source") == DOCKER_SOCKET_PATH or mount.get("target") == DOCKER_SOCKET_PATH:
            raise ContractError("candidate must not mount the Docker socket")
    networks = candidate.get("networks")
    if not isinstance(networks, dict) or not networks:
        raise ContractError("candidate must explicitly join the formal network")
    if any(not isinstance(value, dict) or value.get("external") is not True for value in networks.values()):
        raise ContractError("candidate networks must be external formal networks")


def validate_candidate_runtime_mounts(
    container_record: Mapping[str, Any],
    *,
    formal_data_host_root: Path,
    weather_host_root: Path,
    candidate_runtime_mounts: Mapping[str, Path],
    import_profit_runtime_host: Path | None = None,
    import_profit_runtime_container: str = IMPORT_PROFIT_RUNTIME_CONTAINER_PATH,
) -> list[dict[str, Any]]:
    """Re-prove the candidate mount contract from live Docker inspect evidence."""
    raw_mounts = container_record.get("mounts")
    if not isinstance(raw_mounts, list):
        raise ContractError("candidate container mount evidence is missing")
    actual: dict[str, Mapping[str, Any]] = {}
    for mount in raw_mounts:
        if not isinstance(mount, dict):
            raise ContractError("candidate container mount evidence is invalid")
        source = mount.get("source")
        destination = mount.get("destination")
        if source == DOCKER_SOCKET_PATH or destination == DOCKER_SOCKET_PATH:
            raise ContractError("candidate must not mount the Docker socket")
        if not isinstance(destination, str) or destination in actual:
            raise ContractError("candidate container mount targets are invalid")
        actual[destination] = mount
    expected: dict[str, tuple[Path, bool]] = {
        CANDIDATE_DATA_CONTAINER_PATH: (formal_data_host_root, True),
        WEATHER_CONTAINER_PATH: (weather_host_root, True),
        **{
            target: (path, False)
            for target, path in candidate_runtime_mounts.items()
        },
    }
    if import_profit_runtime_host is not None:
        expected[import_profit_runtime_container] = (import_profit_runtime_host, False)
    if set(actual) != set(expected):
        raise ContractError("candidate container mount set differs from the sealed contract")
    evidence: list[dict[str, Any]] = []
    for destination, (expected_source, expected_read_only) in expected.items():
        mount = actual[destination]
        source = mount.get("source")
        if (
            mount.get("type") != "bind"
            or not isinstance(source, str)
            or Path(source).resolve(strict=False) != expected_source.resolve(strict=False)
            or mount.get("read_only") is not expected_read_only
        ):
            raise ContractError(f"candidate container mount contract failed for {destination}")
        evidence.append(
            {
                "target": destination,
                "source": str(expected_source.resolve(strict=False)),
                "read_only": expected_read_only,
            }
        )
    return sorted(evidence, key=lambda item: item["target"])


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


def _validate_generated_candidate_compose(
    options: CandidateOptions,
    runner: CommandRunner,
    compose_path: Path,
    candidate_compose: Mapping[str, Any],
    formal_compose: Mapping[str, Any],
    formal_data_host_root: Path,
    candidate_runtime_mounts: Mapping[str, Path],
    runtime_host: Path | None,
    candidate_port: int,
) -> None:
    project_name = str(candidate_compose["name"])
    raw = runner.run(
        [
            "docker",
            "compose",
            "--project-name",
            project_name,
            "-f",
            str(compose_path),
            "config",
            "--format",
            "json",
        ],
        cwd=options.output_directory,
    )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractError(f"candidate docker compose config returned invalid JSON: {exc}") from exc
    formal_service = formal_compose["services"][COMPOSE_SERVICE]
    validate_candidate_compose(
        parsed,
        image_ref=options.image_ref,
        candidate_container_name=options.candidate_container_name,
        candidate_port=candidate_port,
        expected_weather_source=str(
            _weather_mount(candidate_compose["services"][CANDIDATE_SERVICE])["source"]
        ),
        expected_git_commit=options.git_commit,
        formal_volumes=copy.deepcopy(formal_service.get("volumes") or []),
        expected_formal_data_host_root=str(formal_data_host_root),
        expected_candidate_runtime_mounts={
            target: str(path) for target, path in candidate_runtime_mounts.items()
        },
        expected_import_profit_runtime_host=(str(runtime_host) if runtime_host else None),
        expected_import_profit_runtime_container=options.import_profit_runtime_container,
    )


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
        f"market-data.git.commit={options.git_commit}",
        "--label",
        "market-data.artifact.origin=candidate",
        "--label",
        "market-data.artifact.promotable=true",
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
    validate_import_profit_runtime_paths(options)
    return production_environment, formal_compose


def _probe_import_profit_runtime(
    options: CandidateOptions, runner: CommandRunner
) -> dict[str, Any] | None:
    if options.import_profit_runtime_host is None:
        return None
    uid_text = runner.run(["docker", "exec", options.candidate_container_name, "id", "-u"]).strip()
    gid_text = runner.run(["docker", "exec", options.candidate_container_name, "id", "-g"]).strip()
    if not uid_text.isdigit() or not gid_text.isdigit():
        raise ContractError("candidate container UID/GID probe returned invalid output")
    probe_name = f".candidate-write-probe-{options.release_id}"
    probe_path = f"{options.import_profit_runtime_container}/{probe_name}"
    runner.run(
        [
            "docker",
            "exec",
            options.candidate_container_name,
            "sh",
            "-c",
            'umask 077; : > "$1"; test -f "$1"; rm -f "$1"',
            "candidate-runtime-probe",
            probe_path,
        ]
    )
    return {
        "container_path": options.import_profit_runtime_container,
        "environment_variable": IMPORT_PROFIT_RUNTIME_ENV_KEY,
        "uid": int(uid_text),
        "gid": int(gid_text),
        "read_write_probe": "passed",
    }


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
        data_inspection_container_name=options.candidate_container_name,
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
    candidate_runtime_access: Mapping[str, Any] | None,
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
        options.candidate_container_name,
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
    blocking_gates = []
    runtime_mounts = []
    if options.import_profit_runtime_host is not None:
        blocking_gates.append(
            pending_candidate_gate(
                str(options.pending_gate), str(options.earliest_expected_business_date)
            )
        )
        runtime_mounts.append(
            {
                "mount_id": IMPORT_PROFIT_RUNTIME_MOUNT_ID,
                "container_path": options.import_profit_runtime_container,
                "mode": "rw",
                "runtime_kind": "import_profit_candidate",
                "candidate_batch_id": options.release_id,
                "required_environment_variable": IMPORT_PROFIT_RUNTIME_ENV_KEY,
            }
        )
    candidate_result = create_candidate_result(
        manifest=manifest,
        runtime=runtime,
        readiness=readiness,
        checks={**checks, "formal_containers_before": formal_snapshot},
        blocking_gates=blocking_gates,
        runtime_mounts=runtime_mounts,
        candidate_runtime_access=candidate_runtime_access,
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

    The supplied environment is the read-only current production baseline.
    create_deployment_plan derives and seals the target environment without
    changing the operator-managed production file.
    """
    manifest_path = release_directory / "release.json"
    manifest, _ = load_manifest_bundle(
        manifest_path,
        release_directory / "release.env",
        SCRIPT_DIR / "release.schema.json",
    )
    del production_environment
    plan = create_deployment_plan(
        tool_repo_root=options.build_context,
        production_compose_file=options.production_compose_file,
        production_project_dir=options.production_compose_file.parent,
        candidate_result_file=candidate_result_path,
        production_env_file=options.production_env_file,
        manifest=manifest,
        runtime=runtime,
        schema=load_schema(SCRIPT_DIR / "deployment_plan.schema.json"),
    )
    deployment_plan_path = write_deployment_plan(
        plan, release_directory / "deployment_plan.json"
    )
    load_deployment_plan(
        deployment_plan_path,
        manifest,
        SCRIPT_DIR / "deployment_plan.schema.json",
    )
    return {
        "deployment_plan_path": str(deployment_plan_path),
        "deployment_plan_sha256": hash_file(deployment_plan_path),
        "production_env_file": str(options.production_env_file),
        "production_env_baseline_sha256": plan[
            "production_env_baseline_sha256"
        ],
        "target_production_env_sha256": plan["production_env_sha256"],
    }


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
    services = formal_compose.get("services")
    if not isinstance(services, dict) or COMPOSE_SERVICE not in services:
        raise ContractError("formal Compose is missing spread-dashboard")
    formal_service = services[COMPOSE_SERVICE]
    if not isinstance(formal_service, dict):
        raise ContractError("formal spread-dashboard service is invalid")
    formal_data_host_root, formal_data_mount = validate_candidate_data_path(
        options, formal_service
    )
    runtime_mounts = validate_candidate_runtime_location(options, formal_service)
    import_profit_runtime_host = validate_import_profit_runtime_paths(options)
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
        formal_data_host_root=formal_data_host_root,
        candidate_runtime_mounts=runtime_mounts,
        import_profit_runtime_host=import_profit_runtime_host,
        import_profit_runtime_container=options.import_profit_runtime_container,
    )
    if options.output_directory.exists():
        raise ContractError("candidate output directory must not already exist")
    options.output_directory.mkdir(parents=True)
    _create_candidate_runtime(runtime_mounts)
    compose_path = options.output_directory / "candidate-compose.json"
    validating_compose_path = options.output_directory / ".candidate-compose.validating.json"
    _write_json_exclusive(validating_compose_path, candidate_compose)
    try:
        _validate_generated_candidate_compose(
            options,
            command_runner,
            validating_compose_path,
            candidate_compose,
            formal_compose,
            formal_data_host_root,
            runtime_mounts,
            import_profit_runtime_host,
            port,
        )
        os.replace(validating_compose_path, compose_path)
    finally:
        if validating_compose_path.exists():
            validating_compose_path.unlink()
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
        "candidate_data_mount": (
            {
                "source": str(formal_data_host_root),
                "target": CANDIDATE_DATA_CONTAINER_PATH,
                "production_source": str(formal_data_mount["source"]),
                "mode": "ro",
            }
        ),
        "candidate_writable_mounts": [
            {
                "source": str(runtime_mounts[target]),
                "target": target,
                "mode": "rw",
            }
            for target in sorted(runtime_mounts)
        ],
        "import_profit_runtime_mount": (
            {
                "mount_id": IMPORT_PROFIT_RUNTIME_MOUNT_ID,
                "candidate_batch_id": options.release_id,
                "target": options.import_profit_runtime_container,
                "mode": "rw",
                "environment_variable": IMPORT_PROFIT_RUNTIME_ENV_KEY,
            }
            if import_profit_runtime_host is not None
            else None
        ),
        "candidate_image_id": None,
        "candidate_container_id": None,
        "candidate_runtime_mounts": None,
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
        phase = "seal-release"
        phase = "candidate-mount-validation"
        revalidated_data_root, _ = validate_candidate_data_path(options, formal_service)
        if revalidated_data_root != formal_data_host_root:
            raise ContractError("formal data identity changed before release sealing")
        candidate_record = candidate_runtime.container_record(options.candidate_container_name)
        result["candidate_runtime_mounts"] = validate_candidate_runtime_mounts(
            candidate_record,
            formal_data_host_root=formal_data_host_root,
            weather_host_root=Path(production_environment["WEATHER_RUNTIME_CURRENT_DIR"]),
            candidate_runtime_mounts=runtime_mounts,
            import_profit_runtime_host=import_profit_runtime_host,
            import_profit_runtime_container=options.import_profit_runtime_container,
        )
        phase = "seal-release"
        release_directory = release_sealer(options, candidate_runtime, production_environment)
        result["release_directory"] = str(release_directory)
        phase = "candidate-validation"
        validator(release_directory, str(result["candidate_health_url"]), options.output_directory)
        phase = "candidate-runtime-access"
        candidate_runtime_access = _probe_import_profit_runtime(options, command_runner)
        result["candidate_runtime_access"] = candidate_runtime_access
        phase = "candidate-result"
        candidate_evidence = candidate_result_sealer(
            options, candidate_runtime, production_environment, release_directory,
            formal_snapshot_path, options.output_directory, formal_spread_before,
            str(result["candidate_health_url"]),
            candidate_runtime_access,
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
        if options.pending_gate:
            result["candidate_result_status"] = CANDIDATE_WAITING_STATUS
            result["deployment_plan_status"] = "blocked-by-pending-gate"
        else:
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
        if options.cleanup_policy == AFTER_PLAN_CLEANUP_POLICY and not options.pending_gate:
            phase = "candidate-image-cleanup"
            result["candidate_image_cleanup_command"] = _remove_candidate_image(
                candidate_runtime, command_runner, options.image_ref
            )
            result["candidate_image_removed"] = True
        else:
            result["candidate_image_retained_for_deployment"] = True
        result["status"] = "waiting-for-gate" if options.pending_gate else "prepared"
    except Exception as exc:
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
    parser.add_argument(
        "--data-host-root",
        type=Path,
        help=(
            "Existing formal project root whose 01_data is mounted read-only into "
            "the candidate and inspected for the release Manifest."
        ),
    )
    parser.add_argument("--rollback-image-ref")
    parser.add_argument("--rollback-image-id")
    parser.add_argument("--formal-git-commit")
    parser.add_argument("--import-profit-runtime-host", type=Path)
    parser.add_argument("--import-profit-runtime-approved-root", type=Path)
    parser.add_argument("--formal-import-profit-runtime-host", type=Path)
    parser.add_argument(
        "--import-profit-runtime-container",
        default=IMPORT_PROFIT_RUNTIME_CONTAINER_PATH,
    )
    parser.add_argument("--pending-gate", choices=(REAL_NIGHT_SESSION_CLOSE_GATE_ID,))
    parser.add_argument("--earliest-expected-business-date")
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
        import_profit_runtime_host=(
            args.import_profit_runtime_host.resolve()
            if args.import_profit_runtime_host
            else None
        ),
        import_profit_runtime_approved_root=(
            args.import_profit_runtime_approved_root.resolve()
            if args.import_profit_runtime_approved_root
            else None
        ),
        formal_import_profit_runtime_host=(
            args.formal_import_profit_runtime_host.resolve()
            if args.formal_import_profit_runtime_host
            else None
        ),
        import_profit_runtime_container=args.import_profit_runtime_container,
        pending_gate=args.pending_gate,
        earliest_expected_business_date=args.earliest_expected_business_date,
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
