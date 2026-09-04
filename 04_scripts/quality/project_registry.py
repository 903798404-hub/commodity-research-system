"""Small, fail-closed development project registry (not an authorization service)."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path, PurePosixPath

REGISTRY_PATH = "02_configs/project_registry.json"


def relative_path(value: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or "\\" in value or ":" in value or any(c in value for c in "*?[]\x00\r\n")
            or value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/"))):
        raise ValueError(f"Invalid registry relative path: {value!r}")
    return PurePosixPath(value).as_posix()


def validate_registry(data: dict, root: Path) -> dict:
    if not isinstance(data, dict) or data.get("schema_version") != "project-registry/1":
        raise ValueError("Invalid project registry schema")
    if set(data) != {"schema_version", "protected_paths", "projects"}:
        raise ValueError("Unexpected registry fields")
    if not isinstance(data["projects"], list) or not data["projects"]:
        raise ValueError("Registry projects must be nonempty")
    def paths(values):
        if not isinstance(values, list) or not all(isinstance(v,str) for v in values) or len(values) != len(set(values)):
            raise ValueError("Registry paths must be a unique list")
        for value in values:
            relative_path(value)
            resolved = (root / value).resolve()
            if root.resolve() not in resolved.parents or not resolved.exists():
                raise ValueError(f"Registry path missing or outside repository: {value}")
    paths(data["protected_paths"])
    seen = set()
    keys = {"project_id", "change_class", "status", "owned_paths", "shared_dependencies",
            "forbidden_paths", "required_tests", "capabilities", "boundary_notes"}
    for item in data["projects"]:
        if not isinstance(item, dict) or set(item) != keys:
            raise ValueError("Invalid project fields")
        pid = item["project_id"]
        if not isinstance(pid, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", pid) or pid in seen:
            raise ValueError("Invalid or duplicate project_id")
        seen.add(pid)
        if item["change_class"] not in ("business", "shared"):
            raise ValueError("Invalid project change_class")
        if item["status"] not in ("ready", "frozen", "needs-boundary-review"):
            raise ValueError("Invalid project status")
        for key in ("owned_paths", "shared_dependencies", "forbidden_paths", "required_tests"):
            paths(item[key])
        if item["status"] == "ready" and (not item["owned_paths"] or not item["required_tests"]):
            raise ValueError("Ready project needs owned paths and tests")
        if not isinstance(item["capabilities"], list) or not all(isinstance(x,str) and x for x in item["capabilities"]):
            raise ValueError("Invalid capabilities")
        if not isinstance(item["boundary_notes"], str) or not item["boundary_notes"]:
            raise ValueError("Missing boundary evidence/limitations")
    return data


def load_registry(root: Path) -> dict:
    try:
        data = json.loads((root / REGISTRY_PATH).read_text(encoding="utf-8"))
        return validate_registry(data, root)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"Project registry unavailable/invalid: {exc}") from exc


def select_project(root: Path, project_id: str) -> tuple[dict, dict]:
    registry = load_registry(root)
    for project in registry["projects"]:
        if project["project_id"] == project_id:
            return registry, project
    raise ValueError(f"Unknown project_id: {project_id}")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=90)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout.decode("utf-8").strip()


def assert_main_mirror(root: Path) -> dict:
    """Read every main checkout; never checkout, merge or reset main."""
    remote = git(root, "rev-parse", "origin/main")
    if git(root, "rev-parse", "refs/heads/main") != remote:
        raise ValueError("LOCAL_MAIN_NOT_MIRROR")
    mains = []
    for block in git(root, "worktree", "list", "--porcelain").split("\n\n"):
        lines = block.splitlines()
        if "branch refs/heads/main" in lines:
            path = Path(next(line[9:] for line in lines if line.startswith("worktree ")))
            if git(path, "status", "--porcelain=v1", "--untracked-files=all"):
                raise ValueError("LOCAL_MAIN_NOT_CLEAN")
            mains.append(str(path))
    if not mains:
        raise ValueError("MAIN_CHECKOUT_NOT_FOUND")
    return {"main_head": remote, "main_clean_mirror": True, "main_checkouts": mains}
