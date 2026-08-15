from __future__ import annotations

import os
from pathlib import Path

from agri_research_agent.shared.file_identity import identify_file


def test_file_identity_uses_sha_size_and_optional_schema(tmp_path: Path) -> None:
    path = tmp_path / "sample.bin"
    path.write_bytes(b"market-data")
    identity = identify_file(path, schema_fingerprint="schema-v1")
    assert identity.sha256 == "f58ff5e1e3acb63bc454ea3bef0336117d231f55e34ca93cf5be6960fb1e34f8"
    assert identity.size_bytes == 11
    assert identity.schema_fingerprint == "schema-v1"


def test_mtime_is_not_part_of_file_identity(tmp_path: Path) -> None:
    path = tmp_path / "sample.bin"
    path.write_bytes(b"same content")
    before = identify_file(path)
    os.utime(path, (1_700_000_000, 1_700_000_000))
    assert identify_file(path) == before


def test_content_change_changes_identity(tmp_path: Path) -> None:
    path = tmp_path / "sample.bin"
    path.write_bytes(b"one")
    before = identify_file(path)
    path.write_bytes(b"two")
    assert identify_file(path) != before
