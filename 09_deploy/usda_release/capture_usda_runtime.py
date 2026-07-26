#!/usr/bin/env python3
"""Capture a bounded, secret-free identity snapshot for USDA migration gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


FORMAL_CONTAINERS = ("spread-dashboard", "usda-dashboard", "oil-world-dashboard")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_mounts(raw: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    mounts = [
        {
            "type": mount.get("Type"),
            "source": mount.get("Source"),
            "destination": mount.get("Destination"),
            "read_write": bool(mount.get("RW")),
        }
        for mount in raw
    ]
    return sorted(
        mounts,
        key=lambda item: (
            str(item["destination"] or ""),
            str(item["type"] or ""),
            str(item["source"] or ""),
        ),
    )


def _safe_ports(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in sorted(raw.items())}


def sanitize_container(raw: Mapping[str, Any]) -> dict[str, Any]:
    config = raw.get("Config") or {}
    host = raw.get("HostConfig") or {}
    state = raw.get("State") or {}
    labels = config.get("Labels") or {}
    network = raw.get("NetworkSettings") or {}
    attached_networks = network.get("Networks") or {}
    raw_network_mode = host.get("NetworkMode")
    normalized_network_mode = raw_network_mode
    for network_name, details in attached_networks.items():
        if (details or {}).get("NetworkID") == raw_network_mode:
            normalized_network_mode = network_name
            break
    return {
        "container_id": raw.get("Id"),
        "container_name": str(raw.get("Name") or "").lstrip("/"),
        "config_image": config.get("Image"),
        "image_id": raw.get("Image"),
        "created_at": raw.get("Created"),
        "started_at": state.get("StartedAt"),
        "running": bool(state.get("Running")),
        "status": state.get("Status"),
        "restart_count": int(raw.get("RestartCount") or 0),
        "command": config.get("Cmd"),
        "working_dir": config.get("WorkingDir"),
        "network_mode": normalized_network_mode,
        "attached_networks": [
            {"name": name, "network_id": (details or {}).get("NetworkID")}
            for name, details in sorted(attached_networks.items())
        ],
        "restart_policy": (host.get("RestartPolicy") or {}).get("Name"),
        "health_status": (state.get("Health") or {}).get("Status", "none"),
        "ports": _safe_ports(network.get("Ports") or {}),
        "mounts": _safe_mounts(raw.get("Mounts") or []),
        "environment_names": sorted(
            value.split("=", 1)[0] for value in config.get("Env") or []
        ),
        "compose": {
            "project": labels.get("com.docker.compose.project"),
            "working_dir": labels.get("com.docker.compose.project.working_dir"),
            "config_files": labels.get("com.docker.compose.project.config_files"),
            "service": labels.get("com.docker.compose.service"),
        },
    }


def capture_snapshot(*, runner: Any = subprocess.run) -> dict[str, Any]:
    completed = runner(
        ["docker", "inspect", *FORMAL_CONTAINERS],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "docker inspect failed")
    raw_items = json.loads(completed.stdout)
    items = [sanitize_container(item) for item in raw_items]
    services = {str(item["compose"]["service"]): item for item in items}
    if set(services) != set(FORMAL_CONTAINERS):
        raise RuntimeError("formal container snapshot does not contain the expected services")
    if any(not item["running"] or item["status"] != "running" for item in items):
        raise RuntimeError("a formal container is not running")
    return {
        "schema_version": "1.0.0",
        "captured_at_utc": utc_now(),
        "containers": [services[name] for name in FORMAL_CONTAINERS],
    }


def write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_name, path)
    except FileExistsError as exc:
        raise RuntimeError(f"refusing to overwrite evidence: {path}") from exc
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        snapshot = capture_snapshot()
        target = write_json_exclusive(args.output, snapshot)
        print(json.dumps({"output": str(target), "sha256": sha256_file(target)}, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"USDA runtime snapshot failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
