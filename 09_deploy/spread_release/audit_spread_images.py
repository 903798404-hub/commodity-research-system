"""Produce a read-only Spread image-governance dry-run report.

The tool never invokes ``docker rm`` or ``docker image rm``.  It can consume an
offline inventory for tests, or collect Docker inspect facts plus sealed release
artifacts on a server.  Deletion remains an explicitly separate operation.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from image_governance import classify_images, evidence_from_artifacts


ARTIFACT_FILES = {
    "deployment_result.json": "deployment_results",
    "deployment_plan.json": "deployment_plans",
    "candidate_result.json": "candidate_results",
    "release.json": "release_manifests",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def discover_artifacts(release_root: Path) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Read known sealed-result filenames, ignoring malformed/unrelated JSON."""

    artifacts = {kind: [] for kind in ARTIFACT_FILES.values()}
    paths: list[str] = []
    if not release_root.is_dir():
        return artifacts, paths
    for path in sorted(release_root.rglob("*.json")):
        kind = ARTIFACT_FILES.get(path.name)
        if kind is None:
            continue
        parsed = _load_json(path)
        if parsed is None:
            continue
        artifacts[kind].append(parsed)
        paths.append(str(path))
    return artifacts, paths


def _run(command: list[str]) -> str:
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return completed.stdout


def docker_inventory() -> dict[str, list[dict[str, Any]]]:
    """Collect only image/container identity facts; this is Docker read-only."""

    image_ids = [line.strip() for line in _run(["docker", "image", "ls", "-q", "--no-trunc"]).splitlines() if line.strip()]
    images: list[dict[str, Any]] = []
    if image_ids:
        inspected = json.loads(_run(["docker", "image", "inspect", *image_ids]))
        for image in inspected:
            if not isinstance(image, Mapping):
                continue
            config = image.get("Config") if isinstance(image.get("Config"), Mapping) else {}
            images.append(
                {
                    "id": image.get("Id"),
                    "repo_tags": image.get("RepoTags") or [],
                    "labels": config.get("Labels") or {},
                    "created_at": image.get("Created"),
                }
            )

    container_ids = [line.strip() for line in _run(["docker", "ps", "-aq", "--filter", "name=spread-dashboard"]).splitlines() if line.strip()]
    containers: list[dict[str, Any]] = []
    if container_ids:
        inspected = json.loads(_run(["docker", "container", "inspect", *container_ids]))
        for container in inspected:
            if not isinstance(container, Mapping):
                continue
            config = container.get("Config") if isinstance(container.get("Config"), Mapping) else {}
            state = container.get("State") if isinstance(container.get("State"), Mapping) else {}
            containers.append(
                {
                    "name": str(container.get("Name", "")).lstrip("/"),
                    "image_id": container.get("Image"),
                    "running": state.get("Running"),
                    "labels": config.get("Labels") or {},
                }
            )
    return {"images": images, "containers": containers}


def build_report(inventory: Mapping[str, Any], artifacts: Mapping[str, Iterable[Mapping[str, Any]]]) -> dict[str, Any]:
    evidence = evidence_from_artifacts(
        containers=inventory.get("containers", []),
        deployment_results=artifacts.get("deployment_results", []),
        deployment_plans=artifacts.get("deployment_plans", []),
        candidate_results=artifacts.get("candidate_results", []),
        release_manifests=artifacts.get("release_manifests", []),
    )
    images = inventory.get("images", [])
    if not isinstance(images, list):
        raise ValueError("inventory images must be a list")
    return {
        "kind": "spread_image_governance_dry_run",
        "generated_at": _utc_now(),
        "mutation_performed": False,
        "images": classify_images(
            [image for image in images if isinstance(image, Mapping)], evidence
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Spread image governance dry-run")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--inventory", type=Path, help="offline JSON inventory for tests/audit")
    source.add_argument("--docker", action="store_true", help="read-only Docker inspect inventory")
    parser.add_argument("--release-root", type=Path, required=True, help="sealed release artifact root")
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.inventory:
        loaded = _load_json(args.inventory)
        if loaded is None:
            raise SystemExit("inventory must be a readable JSON object")
        inventory = loaded
    else:
        inventory = docker_inventory()
    artifacts, artifact_paths = discover_artifacts(args.release_root)
    report = build_report(inventory, artifacts)
    report["artifact_paths"] = artifact_paths
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        if args.output.exists():
            raise SystemExit("refusing to overwrite an existing report")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
