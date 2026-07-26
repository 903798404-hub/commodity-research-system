#!/usr/bin/env python3
"""Build, validate, seal, and remove an isolated Oil World candidate.

``--execute`` is intentionally required for Docker activity.  The default
dry-run verifies the local Git/Compose/environment contract and renders the
exact candidate commands without contacting Docker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import urlopen

from oil_world_release_contract import (
    APPLICATION,
    CANDIDATE_COMPOSE,
    COMPOSE_PROJECT,
    DATA_CONTAINER_PATH,
    FORMAL_CONTAINERS,
    PRODUCTION_COMPOSE,
    RELEASE_DIRECTORY,
    SERVICE,
    ContractError,
    artifact_paths,
    candidate_environment,
    git_identity,
    hash_file,
    hash_json,
    parse_environment,
    utc_now,
    validate_immutable_image_reference,
    validate_production_environment,
    verify_artifact,
    write_artifact,
)


Runner = Callable[..., Any]
Request = Callable[..., Any]
ERROR_MARKERS = ("Traceback", "FileNotFoundError", "Permission denied", "nginx: [emerg]")


def _run(command: list[str], runner: Runner = subprocess.run) -> Any:
    completed = runner(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "command failed").strip()
        raise ContractError(f"command failed ({command[0]}): {detail[:500]}")
    return completed


def _docker_json(command: list[str], runner: Runner = subprocess.run) -> Any:
    completed = _run(command, runner)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(f"Docker command did not return JSON: {command}") from exc


def _runtime_commit(environment: list[str]) -> str | None:
    matches = [item.split("=", 1)[1] for item in environment if item.startswith("MARKET_DATA_GIT_HEAD=")]
    if not matches:
        return None
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-f]{40}", matches[0]):
        raise ContractError("container MARKET_DATA_GIT_HEAD must be a single full Git SHA")
    return matches[0]


def sanitize_container(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only non-sensitive, deployment-relevant Docker facts."""

    labels = dict(raw.get("Config", {}).get("Labels") or {})
    ports = dict(raw.get("NetworkSettings", {}).get("Ports") or {})
    mounts = [
        {
            "type": item.get("Type"),
            "source": item.get("Source"),
            "destination": item.get("Destination"),
            "read_only": not bool(item.get("RW")),
        }
        for item in raw.get("Mounts") or []
    ]
    mounts.sort(key=lambda item: (str(item["destination"]), str(item["source"])))
    return {
        "name": str(raw.get("Name", "")).lstrip("/"),
        "container_id": raw.get("Id"),
        "config_image": raw.get("Config", {}).get("Image"),
        "image_id": raw.get("Image"),
        "running": bool(raw.get("State", {}).get("Running")),
        "status": raw.get("State", {}).get("Status"),
        "restart_count": raw.get("RestartCount"),
        "health": (raw.get("State", {}).get("Health") or {}).get("Status", "no_healthcheck"),
        "restart_policy": (raw.get("HostConfig", {}).get("RestartPolicy") or {}).get("Name", ""),
        "ports": ports,
        "mounts": mounts,
        "compose": {
            "project": labels.get("com.docker.compose.project"),
            "service": labels.get("com.docker.compose.service"),
            "config_files": labels.get("com.docker.compose.project.config_files"),
            "working_dir": labels.get("com.docker.compose.project.working_dir"),
        },
        "oci_revision": labels.get("org.opencontainers.image.revision"),
        "runtime_git_commit": _runtime_commit(list(raw.get("Config", {}).get("Env") or [])),
    }


def inspect_container(name: str, runner: Runner = subprocess.run) -> dict[str, Any]:
    payload = _docker_json(["docker", "inspect", name], runner)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ContractError(f"docker inspect must return exactly one {name} container")
    return sanitize_container(payload[0])


def wait_for_candidate_health(
    name: str,
    *,
    timeout_seconds: int,
    runner: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Return the candidate only after Docker reports its final healthy state.

    Compose can return before the image's declared healthcheck start period has
    elapsed.  A sealed candidate result must never capture that transient
    ``starting`` state as a release failure or as success.
    """

    deadline = clock() + timeout_seconds
    last_health = "unknown"
    while True:
        candidate = inspect_container(name, runner)
        if not candidate["running"] or candidate["status"] != "running" or candidate["restart_count"] != 0:
            raise ContractError("candidate container stopped or restarted while waiting for health")
        last_health = str(candidate["health"])
        if last_health == "healthy":
            return candidate
        if last_health == "unhealthy":
            raise ContractError("candidate container healthcheck is unhealthy")
        if clock() >= deadline:
            raise ContractError(f"candidate container healthcheck timed out while {last_health}")
        sleep(1.0)


def formal_snapshot(runner: Runner = subprocess.run, phase: str = "before-candidate") -> dict[str, Any]:
    containers = [inspect_container(name, runner) for name in FORMAL_CONTAINERS]
    if {item["name"] for item in containers} != set(FORMAL_CONTAINERS):
        raise ContractError("formal container snapshot is incomplete")
    for item in containers:
        if not item["running"] or item["status"] != "running":
            raise ContractError(f"formal container is not running: {item['name']}")
    containers.sort(key=lambda item: item["name"])
    return {"before_phase": phase, "containers": containers}


def formal_unchanged(before: Mapping[str, Any], after: Mapping[str, Any], *, after_phase: str) -> dict[str, Any]:
    if before.get("containers") != after.get("containers"):
        raise ContractError("formal containers changed during Oil World candidate work")
    return {
        "before_phase": str(before.get("before_phase")),
        "after_phase": after_phase,
        "containers": list(after["containers"]),
        "unchanged": True,
    }


def data_identity(data_root: Path) -> dict[str, Any]:
    data_root = data_root.resolve()
    if not data_root.is_dir():
        raise ContractError(f"Oil World data root is missing: {data_root}")
    entries: list[dict[str, Any]] = []
    for path in sorted(item for item in data_root.rglob("*") if item.is_file()):
        entries.append({"path": path.relative_to(data_root).as_posix(), "size": path.stat().st_size, "sha256": hash_file(path)})
    if not entries:
        raise ContractError("Oil World data root must not be empty")
    required = {"latest.json", "releases.json"}
    if not required.issubset({item["path"] for item in entries}):
        raise ContractError("Oil World data root lacks latest.json or releases.json")
    for filename in required:
        try:
            json.loads((data_root / filename).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"invalid Oil World data JSON: {filename}") from exc
    return {"host_path": str(data_root), "read_only": True, "file_count": len(entries), "tree_sha256": hash_json({"files": entries})}


def image_identity(image_ref: str, runner: Runner = subprocess.run) -> dict[str, str]:
    payload = _docker_json(["docker", "image", "inspect", image_ref], runner)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ContractError("docker image inspect returned an invalid image")
    image = payload[0]
    labels = dict(image.get("Config", {}).get("Labels") or {})
    image_id = image.get("Id")
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ContractError("candidate image has an invalid Image ID")
    return {
        "image_id": image_id,
        "oci_revision": str(labels.get("org.opencontainers.image.revision", "")),
        "oci_version": str(labels.get("org.opencontainers.image.version", "")),
    }


def build_command(repository: Path, image_ref: str, git_commit: str, git_tree: str, release_id: str) -> list[str]:
    project = repository / "11_独立应用" / "OilWorld平衡表"
    return [
        "docker", "build", "--file", str(project / "Dockerfile"), "--tag", image_ref,
        "--build-arg", "OIL_WORLD_BASE_PATH=/oil-world/",
        "--build-arg", f"MARKET_DATA_GIT_HEAD={git_commit}",
        "--build-arg", f"MARKET_DATA_GIT_TREE={git_tree}",
        "--build-arg", f"RELEASE_ID={release_id}", str(project),
    ]


def compose_up_command(environment_file: Path, values: Mapping[str, str]) -> list[str]:
    return [
        "docker", "compose", "--project-name", values["OIL_WORLD_CANDIDATE_PROJECT_NAME"],
        "--env-file", str(environment_file), "-f", str(CANDIDATE_COMPOSE),
        "up", "-d", "--no-build", "--pull", "never", "--no-deps", "--force-recreate", SERVICE,
    ]


def cleanup_command(values: Mapping[str, str]) -> list[str]:
    return ["docker", "rm", "--force", values["OIL_WORLD_CANDIDATE_CONTAINER_NAME"]]


def write_candidate_environment(path: Path, values: Mapping[str, str]) -> None:
    text = "".join(f"{key}={values[key]}\n" for key in sorted(values))
    path.write_text(text, encoding="utf-8", newline="\n")
    if path.stat().st_mode & 0o077:
        path.chmod(0o600)


def ensure_candidate_port_available(port: int) -> None:
    """Fail before a build when the loopback candidate port is already bound."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise ContractError(f"candidate loopback port is unavailable: 127.0.0.1:{port}") from exc


def _read_http(url: str, request: Request) -> tuple[int, str]:
    with request(url, timeout=3) as response:
        return int(response.status), response.read().decode("utf-8", errors="replace")


def _candidate_data_endpoints(root: str, data_root: Path) -> tuple[str, ...]:
    """Return the current release and comparison indexes that the UI reads.

    This reads the already mounted data directory only.  It deliberately does
    not regenerate an index or infer a release from the image filesystem.
    """

    try:
        latest = json.loads((data_root / "latest.json").read_text(encoding="utf-8"))
        releases = json.loads((data_root / "releases.json").read_text(encoding="utf-8"))
        release = latest["release"]
        release_rows = releases["releases"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ContractError("cannot determine current Oil World release data paths") from exc
    if not isinstance(release, str) or not isinstance(release_rows, list):
        raise ContractError("Oil World latest/releases indexes have an invalid shape")
    current = next((row for row in release_rows if isinstance(row, Mapping) and row.get("release") == release), None)
    if current is None:
        raise ContractError("Oil World latest release is absent from releases.json")
    paths = [
        f"{root}data/oil_world/latest.json",
        f"{root}data/oil_world/releases.json",
        f"{root}data/oil_world/releases/{release}/index.json",
    ]
    previous = current.get("previous_release")
    if previous is not None:
        if not isinstance(previous, str):
            raise ContractError("Oil World previous_release has an invalid value")
        comparison = f"{previous}_to_{release}"
        comparison_file = data_root / "comparisons" / comparison / "index.json"
        if not comparison_file.is_file():
            raise ContractError("Oil World comparison index for the latest release is missing")
        paths.append(f"{root}data/oil_world/comparisons/{comparison}/index.json")
    return tuple(paths)


def validate_candidate_http(
    port: int,
    *,
    data_root: Path,
    request: Request = urlopen,
    sleep: Callable[[float], None] = time.sleep,
    timeout: int = 90,
) -> dict[str, Any]:
    root = f"http://127.0.0.1:{port}/oil-world/"
    endpoints = (root, f"{root}presentation", *_candidate_data_endpoints(root, data_root))
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while True:
        try:
            responses = {url: _read_http(url, request) for url in endpoints}
            if all(status == 200 for status, _body in responses.values()):
                assets = re.findall(r'(?:src|href)=["\']([^"\']+\.(?:js|css))(?:\?[^"\']*)?["\']', responses[root][1], flags=re.I)
                if not assets:
                    raise ContractError("candidate root page has no JavaScript or CSS asset reference")
                asset_status = {urljoin(root, item): _read_http(urljoin(root, item), request)[0] for item in sorted(set(assets))}
                if not all(status == 200 for status in asset_status.values()):
                    raise ContractError("candidate static asset check failed")
                release_status, release_body = _read_http(f"{root}RELEASE.json", request)
                if release_status != 200:
                    raise ContractError("candidate RELEASE.json is unavailable")
                release = json.loads(release_body)
                return {
                    "http": {url: status for url, (status, _body) in responses.items()},
                    "assets": asset_status,
                    "embedded_release": release,
                }
            last = {url: status for url, (status, _body) in responses.items()}
        except (URLError, OSError, json.JSONDecodeError, ContractError) as exc:
            last = {"error": type(exc).__name__}
        if time.monotonic() >= deadline:
            raise ContractError(f"candidate readiness timed out: {last}")
        sleep(2)


def log_summary(container: str, runner: Runner = subprocess.run) -> dict[str, Any]:
    completed = _run(["docker", "logs", "--tail", "200", container], runner)
    raw = ((completed.stdout or "") + (completed.stderr or "")).encode("utf-8", errors="replace")
    tail = raw[-65536:]
    rendered = tail.decode("utf-8", errors="replace")
    markers = [marker for marker in ERROR_MARKERS if marker in rendered]
    if markers:
        raise ContractError(f"candidate logs contain error markers: {markers}")
    return {"requested_lines": 200, "sampled_bytes": len(raw), "tail_bytes": len(tail), "tail_sha256": hashlib.sha256(tail).hexdigest(), "error_markers": markers}


def _candidate_result(release: Mapping[str, Any], candidate: Mapping[str, Any], checks: Mapping[str, Any], formal: Mapping[str, Any], data: Mapping[str, Any], log: Mapping[str, Any]) -> dict[str, Any]:
    if not candidate["running"] or candidate["status"] != "running" or candidate["restart_count"] != 0:
        raise ContractError("candidate container is not running with RestartCount 0")
    if candidate["health"] != "healthy":
        raise ContractError("candidate container healthcheck is not healthy")
    if candidate["config_image"] != release["image_ref"]:
        raise ContractError("candidate Config.Image differs from the sealed release image reference")
    if candidate["image_id"] != release["image_id"] or candidate["oci_revision"] != release["git_commit"] or candidate["runtime_git_commit"] != release["git_commit"]:
        raise ContractError("candidate image, OCI revision, or runtime Git identity differs from release")
    mounts = [mount for mount in candidate["mounts"] if mount["destination"] == DATA_CONTAINER_PATH]
    if len(mounts) != 1 or not mounts[0]["read_only"] or mounts[0]["source"] != data["host_path"]:
        raise ContractError("candidate Oil World data mount is not the required read-only production root")
    ports = candidate["ports"].get("80/tcp") or []
    if len(ports) != 1 or ports[0].get("HostIp") != "127.0.0.1":
        raise ContractError("candidate is not bound only to 127.0.0.1")
    embedded = checks["embedded_release"]
    if embedded.get("git_commit") != release["git_commit"] or embedded.get("git_tree") != release["git_tree"]:
        raise ContractError("candidate embedded RELEASE.json differs from the release")
    return {
        "schema_version": "1.0.0", "application": APPLICATION, "release_id": release["release_id"],
        "git_commit": release["git_commit"], "git_tree": release["git_tree"], "candidate_image_id": release["image_id"],
        "candidate_container": candidate["name"],
        "identity": {"actual_image_id": candidate["image_id"], "oci_revision": candidate["oci_revision"], "runtime_git_commit": candidate["runtime_git_commit"], "embedded_git_commit": embedded["git_commit"], "embedded_git_tree": embedded["git_tree"]},
        "formal_containers": formal, "data_identity": {key: data[key] for key in ("host_path", "read_only", "tree_sha256")},
        "checks": {"http": checks["http"], "pages": "passed", "assets": "passed", "data": "unchanged", "restart_count": candidate["restart_count"], "production_switch_performed": False},
        "status": "candidate-validated", "created_at": utc_now(), "log_summary": dict(log),
    }


def _event(events: list[dict[str, str]], name: str) -> None:
    events.append({"name": name, "at": utc_now(), "sequence": str(len(events) + 1)})


def prepare(args: argparse.Namespace, runner: Runner = subprocess.run, request: Request = urlopen, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    repository = args.repository.resolve()
    output = args.output_dir.resolve()
    production_compose = args.production_compose.resolve()
    production_project_dir = args.production_project_dir.resolve()
    production_env = args.production_env.resolve()
    if not production_compose.is_file():
        raise ContractError(f"production Compose file is missing: {production_compose}")
    if not production_project_dir.is_dir():
        raise ContractError(f"production Compose project directory is missing: {production_project_dir}")
    if not production_env.is_file():
        raise ContractError(f"production environment file is missing: {production_env}")
    production = validate_production_environment(parse_environment(production_env))
    git_commit, git_tree = git_identity(repository, runner)
    if git_commit != args.git_commit:
        raise ContractError("--git-commit does not match the clean candidate repository")
    if git_tree != args.git_tree:
        raise ContractError("--git-tree does not match the clean candidate repository")
    formal_image_ref = validate_immutable_image_reference(args.formal_image_ref, "--formal-image-ref")
    data = data_identity(Path(production["OIL_WORLD_DATA_ROOT"]))
    values = candidate_environment(production, release_id=args.release_id, candidate_port=args.candidate_port)
    ensure_candidate_port_available(args.candidate_port)
    image_ref = f"market-data-oil-world-dashboard:{args.release_id}"
    # A candidate must run the image just built for this release, never the
    # formal image reference inherited from the protected production env.
    values["OIL_WORLD_IMAGE"] = image_ref
    plan = {"status": "dry_run", "release_id": args.release_id, "git_commit": git_commit, "git_tree": git_tree, "image_ref": image_ref, "formal_image_ref": formal_image_ref, "candidate_container": values["OIL_WORLD_CANDIDATE_CONTAINER_NAME"], "candidate_project": values["OIL_WORLD_CANDIDATE_PROJECT_NAME"], "candidate_port": args.candidate_port, "build_command": build_command(repository, image_ref, git_commit, git_tree, args.release_id), "candidate_up_command": compose_up_command(output / "candidate.env", values), "candidate_cleanup_command": cleanup_command(values), "data_identity": data}
    if not args.execute:
        return plan
    if output.exists():
        raise ContractError("release output directory must be new and empty")
    output.mkdir(parents=True, mode=0o700)
    events: list[dict[str, str]] = []
    candidate_started = False
    try:
        before = formal_snapshot(runner)
        _event(events, "formal_snapshot")
        _run(build_command(repository, image_ref, git_commit, git_tree, args.release_id), runner)
        _event(events, "docker_build")
        identity = image_identity(image_ref, runner)
        if identity["oci_revision"] != git_commit or identity["oci_version"] != args.release_id:
            raise ContractError("candidate OCI identity does not match requested release")
        release = {"schema_version": "1.0.0", "application": APPLICATION, "release_id": args.release_id, "git_commit": git_commit, "git_tree": git_tree, "image_ref": image_ref, "image_id": identity["image_id"], "oci_revision": identity["oci_revision"], "build_time": utc_now(), "compose_sha256": hash_file(production_compose), "data_identity": data, "formal_containers": before, "rollback": {"image_ref": args.rollback_image_ref, "image_id": args.rollback_image_id, "git_commit": args.rollback_git_commit}, "candidate": {"container_name": values["OIL_WORLD_CANDIDATE_CONTAINER_NAME"], "project_name": values["OIL_WORLD_CANDIDATE_PROJECT_NAME"], "port": args.candidate_port}}
        write_artifact(output, "release", release, release_id=args.release_id, git_commit=git_commit, git_tree=git_tree, image_id=identity["image_id"])
        _event(events, "release_bundle")
        write_candidate_environment(output / "candidate.env", values)
        _run(compose_up_command(output / "candidate.env", values), runner)
        candidate_started = True
        _event(events, "compose_up")
        # Docker reports a newly-created container as "starting" while its
        # healthcheck warms up.  The HTTP validator is the readiness gate and
        # polls that transition; inspect only after it succeeds so the sealed
        # result records the final healthy state, not an initial snapshot.
        checks = validate_candidate_http(
            args.candidate_port,
            data_root=Path(production["OIL_WORLD_DATA_ROOT"]),
            request=request,
            sleep=sleep,
            timeout=args.timeout_seconds,
        )
        _event(events, "validate")
        candidate = wait_for_candidate_health(
            values["OIL_WORLD_CANDIDATE_CONTAINER_NAME"],
            timeout_seconds=args.timeout_seconds,
            runner=runner,
            sleep=sleep,
        )
        after = formal_snapshot(runner, phase="after-candidate")
        formal = formal_unchanged(before, after, after_phase="after-candidate")
        if data_identity(Path(production["OIL_WORLD_DATA_ROOT"])) != data:
            raise ContractError("Oil World production data changed during candidate validation")
        result = _candidate_result(release, candidate, checks, formal, data, log_summary(candidate["name"], runner))
        write_artifact(output, "candidate_result", result, release_id=args.release_id, git_commit=git_commit, git_tree=git_tree, image_id=identity["image_id"], runtime_git_commit=candidate["runtime_git_commit"])
        _event(events, "candidate_result")
        _run(cleanup_command(values), runner)
        missing = runner(["docker", "inspect", values["OIL_WORLD_CANDIDATE_CONTAINER_NAME"]], check=False, capture_output=True, text=True)
        if missing.returncode == 0:
            raise ContractError("candidate container still exists after cleanup")
        candidate_started = False
        _event(events, "candidate_container_cleanup")
        pre_deploy = formal_snapshot(runner, phase="pre-deploy")
        plan_formal = formal_unchanged(after, pre_deploy, after_phase="pre-deploy")
        candidate_file, _ = artifact_paths(output, "candidate_result")[:2]
        deployment = {"schema_version": "1.0.0", "application": APPLICATION, "release_id": args.release_id, "git_commit": git_commit, "git_tree": git_tree, "image_ref": image_ref, "formal_image_ref": formal_image_ref, "candidate_image_id": identity["image_id"], "candidate_result_sha256": hash_file(candidate_file), "candidate_container_removed": True, "formal_containers": plan_formal, "tool_repo_root": str(repository), "production_compose_file": str(production_compose), "production_project_dir": str(production_project_dir), "production_env_file": str(production_env), "production_baseline": {"compose_sha256": hash_file(production_compose), "env_sha256": hash_file(production_env), "image_ref": production["OIL_WORLD_IMAGE"]}, "compose_project": COMPOSE_PROJECT, "service": SERVICE, "data_identity": {key: data[key] for key in ("host_path", "read_only", "tree_sha256")}, "rollback": release["rollback"], "execute_argv": ["docker", "compose", "--env-file", str(production_env), "--project-name", COMPOSE_PROJECT, "--project-directory", str(production_project_dir), "-f", str(production_compose), "up", "-d", "--no-build", "--pull", "never", "--no-deps", "--force-recreate", SERVICE], "status": "deployment_plan_sealed", "created_at": utc_now()}
        write_artifact(output, "deployment_plan", deployment, release_id=args.release_id, git_commit=git_commit, git_tree=git_tree, image_id=identity["image_id"])
        _event(events, "deployment_plan")
        (output / "event_order.json").write_text(json.dumps(events, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"status": "prepared", **plan, "output_dir": str(output), "events": events}
    finally:
        if candidate_started:
            runner(cleanup_command(values), check=False, capture_output=True, text=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repository", type=Path, required=True)
    result.add_argument("--production-compose", type=Path, default=PRODUCTION_COMPOSE)
    result.add_argument("--production-project-dir", type=Path, required=True)
    result.add_argument("--production-env", type=Path, required=True)
    result.add_argument("--output-dir", type=Path, required=True)
    result.add_argument("--release-id", required=True)
    result.add_argument("--git-commit", required=True)
    result.add_argument("--git-tree", required=True)
    result.add_argument("--rollback-image-ref", required=True)
    result.add_argument("--rollback-image-id", required=True)
    result.add_argument("--rollback-git-commit", required=True)
    result.add_argument("--formal-image-ref", required=True)
    result.add_argument("--candidate-port", type=int, default=18081)
    result.add_argument("--timeout-seconds", type=int, default=90)
    result.add_argument("--execute", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        print(json.dumps(prepare(args), ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Oil World candidate preparation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
