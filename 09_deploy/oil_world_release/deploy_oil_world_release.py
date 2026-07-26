#!/usr/bin/env python3
"""Promote a validated Oil World candidate Image ID without rebuilding it."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.request import urlopen

from prepare_oil_world_candidate import (
    FORMAL_CONTAINERS,
    SERVICE,
    _docker_json,
    _run,
    formal_snapshot,
    sanitize_container,
    validate_candidate_http,
)
from oil_world_release_contract import (
    APPLICATION,
    COMPOSE_PROJECT,
    ContractError,
    artifact_paths,
    hash_file,
    git_identity,
    parse_environment,
    utc_now,
    validate_production_environment,
    validate_immutable_image_reference,
    verify_artifact,
    write_artifact,
)


Runner = Callable[..., Any]


def _image_id(image_ref: str, runner: Runner) -> str:
    payload = _docker_json(["docker", "image", "inspect", image_ref], runner)
    if not isinstance(payload, list) or len(payload) != 1:
        raise ContractError("docker image inspect did not return exactly one image")
    image_id = payload[0].get("Id")
    if not isinstance(image_id, str):
        raise ContractError("docker image inspect has no Image ID")
    return image_id


def _write_environment_image(path: Path, image_ref: str) -> bytes:
    original = path.read_bytes()
    lines = original.decode("utf-8").splitlines()
    changed = False
    rendered: list[str] = []
    for line in lines:
        if line.startswith("OIL_WORLD_IMAGE="):
            rendered.append(f"OIL_WORLD_IMAGE={image_ref}")
            changed = True
        else:
            rendered.append(line)
    if not changed:
        raise ContractError("production environment has no OIL_WORLD_IMAGE entry")
    temporary = path.with_name(f".{path.name}.oil-world-release.tmp")
    if temporary.exists():
        raise ContractError(f"refusing to overwrite existing temporary environment file: {temporary}")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(("\n".join(rendered) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
    return original


def _restore_environment(path: Path, original: bytes) -> None:
    temporary = path.with_name(f".{path.name}.oil-world-rollback.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _assert_sidecars_unchanged(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    before_by_name = {item["name"]: item for item in before["containers"]}
    after_by_name = {item["name"]: item for item in after["containers"]}
    for name in ("spread-dashboard", "usda-dashboard"):
        if before_by_name.get(name) != after_by_name.get(name):
            raise ContractError(f"unrelated formal container changed: {name}")


def deploy(
    args: argparse.Namespace,
    runner: Runner = subprocess.run,
    request: Callable[..., Any] = urlopen,
    sleep: Callable[[float], None] = __import__("time").sleep,
) -> dict[str, Any]:
    release_dir = args.release_dir.resolve()
    release, _ = verify_artifact(release_dir, "release")
    plan, _ = verify_artifact(
        release_dir,
        "deployment_plan",
        expected_git_commit=release["git_commit"],
        expected_git_tree=release["git_tree"],
        expected_image_id=release["image_id"],
    )
    candidate_result, _ = verify_artifact(
        release_dir,
        "candidate_result",
        expected_git_commit=release["git_commit"],
        expected_git_tree=release["git_tree"],
        expected_image_id=release["image_id"],
        expected_runtime_git_commit=release["git_commit"],
    )
    if plan["candidate_image_id"] != release["image_id"] or candidate_result["candidate_image_id"] != release["image_id"]:
        raise ContractError("plan or candidate result does not use the sealed candidate Image ID")
    candidate_result_file, _, _ = artifact_paths(release_dir, "candidate_result")
    if hash_file(candidate_result_file) != plan["candidate_result_sha256"]:
        raise ContractError("deployment plan does not match the sealed candidate result")
    if release["compose_sha256"] != plan["production_baseline"]["compose_sha256"]:
        raise ContractError("release bundle and deployment plan use different production Compose files")
    validate_immutable_image_reference(plan["formal_image_ref"], "formal_image_ref")
    if not plan["candidate_container_removed"]:
        raise ContractError("candidate container removal is required before deployment")
    tool_commit, tool_tree = git_identity(Path(plan["tool_repo_root"]), runner)
    if (tool_commit, tool_tree) != (release["git_commit"], release["git_tree"]):
        raise ContractError("deployment tool repository no longer matches the sealed release Git identity")
    if args.dry_run:
        return {"status": "dry_run", "release_id": release["release_id"], "image_id": release["image_id"], "formal_image_ref": plan["formal_image_ref"], "execute_argv": plan["execute_argv"]}

    environment_path = Path(plan["production_env_file"])
    if hash_file(environment_path) != plan["production_baseline"]["env_sha256"]:
        raise ContractError("production environment changed after plan sealing")
    if hash_file(Path(plan["production_compose_file"])) != plan["production_baseline"]["compose_sha256"]:
        raise ContractError("production Compose changed after plan sealing")
    environment = validate_production_environment(parse_environment(environment_path))
    if environment["OIL_WORLD_IMAGE"] != plan["production_baseline"]["image_ref"]:
        raise ContractError("production environment image differs from sealed rollback baseline")
    before = formal_snapshot(runner, phase="pre-deploy")
    if before["containers"] != plan["formal_containers"]["containers"]:
        raise ContractError("formal container baseline changed after deployment plan sealing")
    if _image_id(release["image_ref"], runner) != release["image_id"]:
        raise ContractError("candidate image tag no longer resolves to its sealed Image ID")
    _run(["docker", "tag", release["image_ref"], plan["formal_image_ref"]], runner)
    if _image_id(plan["formal_image_ref"], runner) != release["image_id"]:
        raise ContractError("formal image tag did not retain the candidate Image ID")
    original_environment: bytes | None = None
    try:
        original_environment = _write_environment_image(environment_path, plan["formal_image_ref"])
        _run(list(plan["execute_argv"]), runner)
        after = formal_snapshot(runner, phase="after-deploy")
        _assert_sidecars_unchanged(before, after)
        oil = next(item for item in after["containers"] if item["name"] == SERVICE)
        if oil["image_id"] != release["image_id"] or oil["config_image"] != plan["formal_image_ref"] or oil["runtime_git_commit"] != release["git_commit"] or oil["oci_revision"] != release["git_commit"]:
            raise ContractError("formal Oil World container does not match the sealed candidate identity")
        http = validate_candidate_http(
            8081,
            data_root=Path(plan["data_identity"]["host_path"]),
            request=request,
            sleep=sleep,
            timeout=args.timeout_seconds,
        )
        result = {"schema_version": "1.0.0", "application": APPLICATION, "release_id": release["release_id"], "git_commit": release["git_commit"], "git_tree": release["git_tree"], "image_id": release["image_id"], "runtime_git_commit": oil["runtime_git_commit"], "formal_containers": {"before_phase": "pre-deploy", "after_phase": "after-deploy", "containers": after["containers"]}, "http": http["http"], "rollback_performed": False, "status": "deployment-succeeded", "created_at": utc_now()}
        write_artifact(release_dir, "deployment_result", result, release_id=release["release_id"], git_commit=release["git_commit"], git_tree=release["git_tree"], image_id=release["image_id"], runtime_git_commit=oil["runtime_git_commit"])
        _run(["docker", "image", "rm", release["image_ref"]], runner)
        return {"status": "deployed", "result": result}
    except Exception:
        if original_environment is not None:
            _restore_environment(environment_path, original_environment)
            _run(list(plan["execute_argv"]), runner)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    args.dry_run = not args.execute
    try:
        print(json.dumps(deploy(args), ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Oil World deployment failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
