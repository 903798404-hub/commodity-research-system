"""Resolve the one production data package selected by the server pointer."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


PUBLIC_DATA_SERVER_STORE_ROOT_ENV = "PUBLIC_DATA_SERVER_STORE_ROOT"
SERVER_POINTER_SCHEMA = "public-current-server-pointer/2"
PACKAGE_SCHEMA = "public-current-production-package/2"
DOMESTIC_SPREAD_RELATIVE_PATH = Path(
    "consumer-artifacts/domestic-spread/historical_spread_database.parquet"
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class ActivatedRuntimeError(RuntimeError):
    """The configured production package pointer is absent or inconsistent."""


def resolve_server_current_data_root(store_root: str | Path) -> Path:
    """Resolve Current without silently falling back to loose production files.

    Package SHA validation happens during server activation.  Consumers repeat the
    pointer/manifest identity check and then use each domain's strict reader.
    """

    configured_root = Path(store_root)
    if configured_root.is_symlink():
        raise ActivatedRuntimeError("server store root is unsafe")
    root = configured_root.resolve(strict=True)
    if not root.is_dir():
        raise ActivatedRuntimeError("server store root is unsafe")
    pointer = _strict_json(root / "current.json")
    required = {
        "schema_version", "package_id", "current_identity_sha256",
        "delivery_identity_sha256", "bundle_sha256",
    }
    if set(pointer) != required or pointer["schema_version"] != SERVER_POINTER_SCHEMA:
        raise ActivatedRuntimeError("server Current pointer is invalid")
    package_id = str(pointer["package_id"])
    if not _SAFE_ID.fullmatch(package_id):
        raise ActivatedRuntimeError("server package id is unsafe")
    releases_path = root / "releases"
    if releases_path.is_symlink():
        raise ActivatedRuntimeError("server releases root is unsafe")
    releases_root = releases_path.resolve(strict=True)
    if not releases_root.is_relative_to(root):
        raise ActivatedRuntimeError("server releases root is unsafe")
    release_path = releases_path / package_id
    if release_path.is_symlink():
        raise ActivatedRuntimeError("server release path is unsafe")
    release = release_path.resolve(strict=True)
    if not release.is_relative_to(releases_root):
        raise ActivatedRuntimeError("server release path is unsafe")
    manifest = _strict_json(release / "manifest.json")
    if (
        manifest.get("schema_version") != PACKAGE_SCHEMA
        or manifest.get("package_id") != package_id
        or manifest.get("current_identity_sha256") != pointer["current_identity_sha256"]
        or manifest.get("delivery_identity_sha256") != pointer["delivery_identity_sha256"]
        or manifest.get("bundle_sha256") != pointer["bundle_sha256"]
    ):
        raise ActivatedRuntimeError("server Current package identity mismatch")
    data_path = release / "data"
    if data_path.is_symlink():
        raise ActivatedRuntimeError("server Current data root is unsafe")
    data = data_path.resolve(strict=True)
    if not data.is_dir() or not data.is_relative_to(release):
        raise ActivatedRuntimeError("server Current data root is unsafe")
    return data


def resolve_public_data_root(default_root: str | Path) -> Path:
    """Use the package store when explicitly configured, otherwise a local root."""

    configured = os.getenv(PUBLIC_DATA_SERVER_STORE_ROOT_ENV, "").strip()
    if configured:
        return resolve_server_current_data_root(configured)
    return Path(default_root)


def resolve_domestic_spread_path(default_data_root: str | Path) -> Path:
    configured = os.getenv(PUBLIC_DATA_SERVER_STORE_ROOT_ENV, "").strip()
    if configured:
        path = resolve_server_current_data_root(configured) / DOMESTIC_SPREAD_RELATIVE_PATH
        if not path.is_file() or path.is_symlink():
            raise ActivatedRuntimeError("activated Domestic Spread artifact is missing")
        return path
    root = Path(default_data_root).resolve()
    parquet = root / "historical_spread_database.parquet"
    return parquet if parquet.exists() else root / "historical_spread_database.xlsx"


def _strict_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ActivatedRuntimeError("server Current metadata is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ActivatedRuntimeError("server Current metadata is unreadable") from exc
    if not isinstance(value, dict):
        raise ActivatedRuntimeError("server Current metadata is invalid")
    return value


__all__ = [
    "ActivatedRuntimeError", "DOMESTIC_SPREAD_RELATIVE_PATH",
    "PUBLIC_DATA_SERVER_STORE_ROOT_ENV", "resolve_domestic_spread_path",
    "resolve_public_data_root", "resolve_server_current_data_root",
]
