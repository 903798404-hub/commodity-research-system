#!/usr/bin/env python
"""Validate one Public Current package from the real application identity."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from agri_research_agent.market_data.activated_runtime import (
    resolve_domestic_spread_path,
    resolve_server_current_data_root,
)
from agri_research_agent.pipelines.public_data_prewarm import (
    validate_activated_public_currents,
    validate_formal_consumer_reads,
)


SCHEMA_VERSION = "application-runtime-readability/1"
_PASS = "PASS"
_FAIL = "FAIL"
_NA = "N/A"


def _initial_evidence(
    *, phase: str, application_container: str, identity_source: str
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "PHASE": phase,
        "STATUS": _FAIL,
        "APPLICATION_CONTAINER": application_container,
        "APPLICATION_RUNTIME_UID": os.getuid(),
        "APPLICATION_RUNTIME_GID": os.getgid(),
        "IDENTITY_SOURCE": identity_source,
        "DIRECTORY_TRAVERSAL": _FAIL,
        "MANIFEST_READ": _FAIL,
        "JSON_PARSE": _FAIL,
        "PARQUET_METADATA_READ": _FAIL,
        "ACTIVATED_RUNTIME_RESOLVER": _NA,
        "DOMESTIC_SPREAD_READER": _FAIL,
        "THREE_OIL_READER": _FAIL,
        "DOMESTIC_BASIS_READER": _FAIL,
        "WEATHER_READER": _FAIL,
        "FILE_COUNT": 0,
        "JSON_FILE_COUNT": 0,
        "PARQUET_FILE_COUNT": 0,
        "PACKAGE_ID": None,
        "SAFE_REASON": None,
    }


def _traverse_and_open(package_root: Path, evidence: dict[str, Any]) -> None:
    directories = [package_root, *sorted(path for path in package_root.rglob("*") if path.is_dir())]
    for directory in directories:
        directory.stat()
        with os.scandir(directory) as entries:
            list(entries)
    evidence["DIRECTORY_TRAVERSAL"] = _PASS

    manifest_path = package_root / "manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    evidence["MANIFEST_READ"] = _PASS
    manifest = json.loads(manifest_text)
    if not isinstance(manifest, dict):
        raise ValueError("package manifest must be a JSON object")
    evidence["PACKAGE_ID"] = manifest.get("package_id")

    files = sorted(path for path in package_root.rglob("*") if path.is_file())
    for path in files:
        with path.open("rb") as handle:
            handle.read(1)
    evidence["FILE_COUNT"] = len(files)

    json_files = sorted(package_root.rglob("*.json"))
    for path in json_files:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, (dict, list)):
            raise ValueError("formal JSON must be an object or array")
    evidence["JSON_FILE_COUNT"] = len(json_files)
    evidence["JSON_PARSE"] = _PASS

    parquet_files = sorted(package_root.rglob("*.parquet"))
    for path in parquet_files:
        metadata = pq.ParquetFile(path).metadata
        if metadata is None:
            raise ValueError("Parquet metadata is unavailable")
    evidence["PARQUET_FILE_COUNT"] = len(parquet_files)
    evidence["PARQUET_METADATA_READ"] = _PASS


def validate_application_runtime_readability(
    *,
    phase: str,
    package_root: str | Path | None,
    store_root: str | Path,
    project_root: str | Path,
    expected_package_id: str,
    expected_uid: int,
    expected_gid: int,
    application_container: str,
    identity_source: str,
) -> dict[str, Any]:
    """Return machine-readable evidence or raise after recording a safe reason."""

    evidence = _initial_evidence(
        phase=phase,
        application_container=application_container,
        identity_source=identity_source,
    )
    try:
        if os.getuid() != expected_uid or os.getgid() != expected_gid:
            raise PermissionError("runtime identity differs from discovered application identity")

        if phase == "PRE_SWITCH":
            if package_root is None:
                raise ValueError("pre-switch package root is required")
            package = Path(package_root).resolve(strict=True)
            data_root = package / "data"
        elif phase in {"POST_SWITCH", "ROLLBACK_READBACK"}:
            data_root = resolve_server_current_data_root(store_root)
            package = data_root.parent
            evidence["ACTIVATED_RUNTIME_RESOLVER"] = _PASS
        else:
            raise ValueError("unsupported application readability phase")

        _traverse_and_open(package, evidence)
        if evidence["PACKAGE_ID"] != expected_package_id:
            raise ValueError("application-visible package identity mismatch")

        validate_activated_public_currents(data_root)
        evidence["THREE_OIL_READER"] = _PASS

        consumer_reads = validate_formal_consumer_reads(
            project_root=project_root,
            runtime_root=data_root,
        )
        targets = dict(consumer_reads.targets)
        required = {
            "domestic_spread": "DOMESTIC_SPREAD_READER",
            "international_spread": "THREE_OIL_READER",
            "domestic_basis": "DOMESTIC_BASIS_READER",
            "weather": "WEATHER_READER",
        }
        for target, field in required.items():
            if targets.get(target) != _PASS:
                raise RuntimeError(f"critical consumer did not pass: {target}")
            evidence[field] = _PASS

        if phase != "PRE_SWITCH":
            spread = resolve_domestic_spread_path(data_root)
            if not spread.is_file() or spread.is_symlink():
                raise RuntimeError("activated Domestic Spread path is unsafe")

        evidence["STATUS"] = _PASS
        return evidence
    except Exception as exc:
        evidence["SAFE_REASON"] = type(exc).__name__
        error = RuntimeError(json.dumps(evidence, sort_keys=True))
        setattr(error, "evidence", evidence)
        raise error from exc


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"required environment is missing: {name}")
    return value


def main() -> int:
    try:
        evidence = validate_application_runtime_readability(
            phase=_required_environment("APPLICATION_READABILITY_PHASE"),
            package_root=os.environ.get("APPLICATION_READABILITY_PACKAGE_ROOT") or None,
            store_root=_required_environment("APPLICATION_READABILITY_STORE_ROOT"),
            project_root=_required_environment("APPLICATION_READABILITY_PROJECT_ROOT"),
            expected_package_id=_required_environment("APPLICATION_READABILITY_PACKAGE_ID"),
            expected_uid=int(_required_environment("APPLICATION_READABILITY_UID")),
            expected_gid=int(_required_environment("APPLICATION_READABILITY_GID")),
            application_container=_required_environment("APPLICATION_READABILITY_CONTAINER"),
            identity_source=_required_environment("APPLICATION_READABILITY_IDENTITY_SOURCE"),
        )
    except Exception as exc:
        evidence = getattr(exc, "evidence", None)
        if not isinstance(evidence, dict):
            evidence = {
                "schema_version": SCHEMA_VERSION,
                "STATUS": _FAIL,
                "SAFE_REASON": type(exc).__name__,
            }
        print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
