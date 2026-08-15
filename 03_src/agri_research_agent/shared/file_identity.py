"""Stable file identity based on content, never modification time."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileIdentity:
    sha256: str
    size_bytes: int
    schema_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("sha256 must be a lowercase hexadecimal digest")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")
        if self.schema_fingerprint is not None and not self.schema_fingerprint.strip():
            raise ValueError("schema_fingerprint cannot be blank")


def identify_file(
    path: str | Path,
    *,
    schema_fingerprint: str | None = None,
    chunk_size: int = 1024 * 1024,
) -> FileIdentity:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"ordinary file required: {source}")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
            size += len(chunk)
    return FileIdentity(digest.hexdigest(), size, schema_fingerprint)
