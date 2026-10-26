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


def test_candidate_can_seal_readonly_history_without_changing_its_bytes_or_mode(tmp_path):
    import shutil
    source = tmp_path/'source.txt'
    source.write_bytes(b'immutable source evidence')
    source.chmod(0o444)
    def build(directory):
        shutil.copy2(source,directory/'history.txt')
    final, _ = seal_immutable_candidate(tmp_path/'candidates','readonly-history',build)
    assert (final/'history.txt').read_bytes() == source.read_bytes()
    assert not (final/'history.txt').stat().st_mode & 0o222
    assert not source.stat().st_mode & 0o222


def test_readonly_aliases_are_allowed_only_when_attribute_changes_stay_inside_candidate(tmp_path):
    import os
    from agri_research_agent.shared.immutable_candidate import ImmutableCandidateError
    def private(directory):
        first=directory/'one';first.write_bytes(b'private immutable object')
        os.link(first,directory/'two');first.chmod(0o444)
    final,_=seal_immutable_candidate(tmp_path/'candidates','private-links',private)
    assert (final/'one').read_bytes()==(final/'two').read_bytes()
    assert not (final/'one').stat().st_mode & 0o222
    external=tmp_path/'external';external.write_bytes(b'external history');external.chmod(0o444)
    def shared(directory): os.link(external,directory/'history')
    if os.name=='nt':
        with pytest.raises(ImmutableCandidateError,match='shared read-only'):
            seal_immutable_candidate(tmp_path/'candidates','external-link',shared)
    else:
        seal_immutable_candidate(tmp_path/'candidates','external-link',shared)
    assert external.read_bytes()==b'external history' and not external.stat().st_mode & 0o222
