from __future__ import annotations

from pathlib import Path

import pytest

from agri_research_agent.shared.immutable_candidate import seal_immutable_candidate


def test_candidate_is_built_in_isolation_and_sealed_once(tmp_path: Path) -> None:
    root = tmp_path / "candidates"

    def build(directory: Path) -> str:
        assert directory.name.startswith(".building-batch-1-")
        (directory / "data.txt").write_text("verified", encoding="utf-8")
        return "result"

    final, result = seal_immutable_candidate(root, "batch-1", build)
    assert result == "result"
    assert final.name == "batch-1"
    assert (final / "data.txt").read_text(encoding="utf-8") == "verified"
    assert not list(root.glob(".building-*"))
    with pytest.raises(FileExistsError):
        seal_immutable_candidate(root, "batch-1", build)


def test_builder_failure_leaves_no_candidate_or_building_directory(tmp_path: Path) -> None:
    root = tmp_path / "candidates"

    def fail(directory: Path) -> None:
        (directory / "partial").write_text("partial", encoding="utf-8")
        raise RuntimeError("fixture failure")

    with pytest.raises(RuntimeError, match="fixture failure"):
        seal_immutable_candidate(root, "failed", fail)
    assert not (root / "failed").exists()
    assert not list(root.glob(".building-*"))


def test_unsafe_candidate_id_is_rejected_before_writing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        seal_immutable_candidate(tmp_path, "../escape", lambda _: None)
