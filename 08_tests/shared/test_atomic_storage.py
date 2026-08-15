from __future__ import annotations

import json
from pathlib import Path

import pytest

from agri_research_agent.shared.atomic_storage import atomic_write_json, atomic_write_with
from agri_research_agent.shared.file_identity import identify_file


def test_atomic_json_write_replaces_and_verifies_target(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    identity = atomic_write_json(target, {"version": 1}, schema_fingerprint="state-v1")
    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 1}
    assert identity == identify_file(target, schema_fingerprint="state-v1")
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_writer_failure_preserves_existing_target_and_cleans_temp(tmp_path: Path) -> None:
    target = tmp_path / "state.bin"
    target.write_bytes(b"old-content")

    def fail_after_partial_write(temporary: Path) -> None:
        temporary.write_bytes(b"partial-new-content")
        raise RuntimeError("fixture writer failed")

    with pytest.raises(RuntimeError, match="fixture writer failed"):
        atomic_write_with(target, fail_after_partial_write)
    assert target.read_bytes() == b"old-content"
    assert not list(tmp_path.glob(".state.bin.*.tmp"))


def test_identity_mismatch_preserves_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "state.bin"
    expected_source = tmp_path / "expected.bin"
    target.write_bytes(b"old")
    expected_source.write_bytes(b"expected")

    def write_other(temporary: Path) -> None:
        temporary.write_bytes(b"other")

    with pytest.raises(ValueError, match="expected_identity"):
        atomic_write_with(target, write_other, expected_identity=identify_file(expected_source))
    assert target.read_bytes() == b"old"
