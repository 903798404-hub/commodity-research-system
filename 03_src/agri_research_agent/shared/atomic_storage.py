"""Small same-directory atomic storage primitives."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .file_identity import FileIdentity, identify_file


Writer = Callable[[Path], None]


def atomic_write_with(
    target: str | Path,
    writer: Writer,
    *,
    schema_fingerprint: str | None = None,
    expected_identity: FileIdentity | None = None,
) -> FileIdentity:
    destination = Path(target)
    parent = destination.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"target directory does not exist: {parent}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        writer(temporary)
        if not temporary.is_file():
            raise RuntimeError("writer did not produce an ordinary file")
        with temporary.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        temporary_identity = identify_file(temporary, schema_fingerprint=schema_fingerprint)
        if expected_identity is not None and temporary_identity != expected_identity:
            raise ValueError("written file identity does not match expected_identity")
        os.replace(temporary, destination)
        final_identity = identify_file(destination, schema_fingerprint=schema_fingerprint)
        if final_identity != temporary_identity:
            raise RuntimeError("post-replace identity verification failed")
        return final_identity
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_bytes(
    target: str | Path,
    payload: bytes,
    *,
    schema_fingerprint: str | None = None,
) -> FileIdentity:
    def write(temporary: Path) -> None:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    return atomic_write_with(target, write, schema_fingerprint=schema_fingerprint)


def atomic_write_json(
    target: str | Path,
    payload: Any,
    *,
    schema_fingerprint: str | None = None,
) -> FileIdentity:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return atomic_write_bytes(target, encoded, schema_fingerprint=schema_fingerprint)
