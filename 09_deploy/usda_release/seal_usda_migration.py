#!/usr/bin/env python3
"""Seal USDA Compose migration evidence without recording environment values."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from capture_usda_runtime import sha256_file, write_json_exclusive


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PRODUCTION_PROJECT = "market-data-usda"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def by_service(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    items = snapshot.get("containers")
    if not isinstance(items, list):
        raise ValueError("snapshot does not contain containers")
    result = {str(item.get("compose", {}).get("service")): item for item in items}
    if set(result) != {"spread-dashboard", "usda-dashboard", "oil-world-dashboard"}:
        raise ValueError("snapshot service set is invalid")
    return result


def environment_identity(path: Path) -> dict[str, Any]:
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            names.append(line.split("=", 1)[0])
    if set(names) - {"USDA_IMAGE", "USDA_HOST_PORT", "USDA_NETWORK_NAME"}:
        raise ValueError("production environment contains non-USDA variables")
    return {"path": str(path), "sha256": sha256_file(path), "variable_names": sorted(names)}


def build_migration_result(
    *,
    git_commit: str,
    git_tree: str,
    compose: Path,
    environment: Path,
    before: Mapping[str, Any],
    candidate: Mapping[str, Any],
    cleanup: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    if not SHA_RE.fullmatch(git_commit) or not SHA_RE.fullmatch(git_tree):
        raise ValueError("Git commit and tree must be complete lowercase SHA-1 values")
    if candidate.get("status") != "passed" or cleanup.get("candidate_container_removed") is not True:
        raise ValueError("candidate evidence is not a passed, explicitly removed candidate")
    pre = by_service(before)
    post = by_service(after)
    for service in ("spread-dashboard", "oil-world-dashboard"):
        if pre[service] != post[service]:
            raise ValueError(f"{service} changed during USDA migration")
    old_usda = pre["usda-dashboard"]
    new_usda = post["usda-dashboard"]
    if old_usda["image_id"] != new_usda["image_id"]:
        raise ValueError("USDA migration changed the business Image ID")
    if old_usda["config_image"] != new_usda["config_image"]:
        raise ValueError("USDA migration changed the business image reference")
    if old_usda["container_id"] == new_usda["container_id"]:
        raise ValueError("USDA formal container was not recreated by the independent Compose")
    if new_usda["compose"]["config_files"] != str(compose):
        raise ValueError("new USDA container does not point to the independent Compose file")
    if new_usda["compose"]["project"] != PRODUCTION_PROJECT:
        raise ValueError("new USDA container does not use the independent Compose project")
    if new_usda["compose"]["working_dir"] != str(compose.parent):
        raise ValueError("new USDA container does not use the stable Compose working directory")
    if candidate.get("expected_image_id") != new_usda["image_id"]:
        raise ValueError("formal USDA Image ID differs from the validated candidate")
    if not new_usda["running"] or new_usda["restart_count"] != 0:
        raise ValueError("new USDA container is not healthy enough to seal")
    return {
        "schema_version": "1.0.0",
        "status": "migration_verified",
        "target_git_commit": git_commit,
        "target_git_tree": git_tree,
        "new_usda_compose": {
            "path": str(compose),
            "sha256": sha256_file(compose),
            "project_name": PRODUCTION_PROJECT,
            "working_dir": str(compose.parent),
        },
        "usda_production_environment": environment_identity(environment),
        "candidate_result": candidate,
        "candidate_cleanup": cleanup,
        "formal_before": before,
        "formal_after": after,
        "rollback": {
            "compose_config_files": old_usda["compose"]["config_files"],
            "image": old_usda["config_image"],
            "image_id": old_usda["image_id"],
            "ports": old_usda["ports"],
            "mounts": old_usda["mounts"],
            "project_name": old_usda["compose"]["project"],
            "working_dir": old_usda["compose"]["working_dir"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--git-tree", required=True)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--cleanup", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = build_migration_result(
            git_commit=args.git_commit,
            git_tree=args.git_tree,
            compose=args.compose,
            environment=args.environment,
            before=load_json(args.before),
            candidate=load_json(args.candidate),
            cleanup=load_json(args.cleanup),
            after=load_json(args.after),
        )
        target = write_json_exclusive(args.output, payload)
        print(json.dumps({"output": str(target), "sha256": sha256_file(target)}, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"USDA migration sealing failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
