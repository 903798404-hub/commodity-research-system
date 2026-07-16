from __future__ import annotations

import json
import os
from pathlib import Path


RAW_DATA_ROOT_ENV = "OILWORLD_RAW_DATA_ROOT"
LOCAL_PATHS_FILE = "local_paths.json"
LEGACY_RAW_DIRECTORY = "01_原始资料"


class RawDataPathError(ValueError):
    """Raised when a local Oil World raw-data path override is invalid."""


def _resolve_override(project_root: Path, value: object, source: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RawDataPathError(f"{source} must contain a non-empty path string")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def resolve_raw_data_root(project_root: Path) -> Path:
    """Resolve licensed raw inputs without storing a machine path in Git."""

    project_root = project_root.resolve()
    environment_value = os.environ.get(RAW_DATA_ROOT_ENV)
    if environment_value:
        return _resolve_override(project_root, environment_value, RAW_DATA_ROOT_ENV)

    local_config = project_root / "02_configs" / LOCAL_PATHS_FILE
    if local_config.is_file():
        try:
            payload = json.loads(local_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RawDataPathError(f"cannot read {local_config.name}: {error}") from error
        if not isinstance(payload, dict):
            raise RawDataPathError(f"{local_config.name} must contain a JSON object")
        if RAW_DATA_ROOT_ENV in payload:
            return _resolve_override(project_root, payload[RAW_DATA_ROOT_ENV], local_config.name)

    return (project_root / LEGACY_RAW_DIRECTORY).resolve()


def resolve_configured_source_workbook(project_root: Path, configured_path: str) -> Path:
    """Resolve legacy baseline configs while keeping sandbox-relative paths intact."""

    project_root = project_root.resolve()
    path = Path(configured_path)
    if path.is_absolute():
        return path.resolve()
    if path.parts and path.parts[0] == LEGACY_RAW_DIRECTORY:
        return (resolve_raw_data_root(project_root).joinpath(*path.parts[1:])).resolve()
    return (project_root / path).resolve()


def raw_source_reference(project_root: Path, workbook_path: Path) -> str:
    """Return a portable release reference without leaking a local absolute path."""

    raw_root = resolve_raw_data_root(project_root)
    try:
        relative = workbook_path.resolve().relative_to(raw_root)
    except ValueError as error:
        message = f"source workbook is outside the configured raw-data root: {workbook_path.name}"
        raise RawDataPathError(message) from error
    return f"raw/{relative.as_posix()}"
