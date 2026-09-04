from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agri_research_agent.shared import production_identity
from agri_research_agent.shared.production_identity import GitExecutionRequest
from agri_research_agent.shared.runtime_context import (
    MARKER_FILENAME,
    RuntimeAuthorizationError,
    RuntimeClassification,
    RuntimeContext,
    RuntimeMode,
    assert_runtime_write,
    load_runtime_identity,
)


def marked_runtime(tmp_path: Path, classification: str, module_id: str = "architecture-foundation") -> Path:
    root = tmp_path / f"runtime-{classification}"
    root.mkdir()
    (root / MARKER_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_id": f"fixture-{classification}",
                "classification": classification,
                "module_id": module_id,
                "created_at": "2026-08-15T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return root


def test_fixture_and_isolated_contexts_write_only_inside_own_root(tmp_path: Path) -> None:
    fixture_root = marked_runtime(tmp_path, "fixture")
    isolated_root = marked_runtime(tmp_path, "isolated-dev")
    fixture = RuntimeContext(RuntimeMode.FIXTURE, "architecture-foundation", fixture_root)
    isolated = RuntimeContext(RuntimeMode.ISOLATED_DEV, "architecture-foundation", isolated_root)
    assert fixture.write_allowed and isolated.write_allowed
    assert assert_runtime_write(fixture, fixture_root / "candidate" / "result.json").is_absolute()
    assert assert_runtime_write(isolated, isolated_root / "logs" / "run.log").is_absolute()
    with pytest.raises(RuntimeAuthorizationError, match="outside"):
        assert_runtime_write(isolated, tmp_path / "outside.txt")


def test_no_context_and_path_traversal_fail_closed(tmp_path: Path) -> None:
    root = marked_runtime(tmp_path, "isolated-dev")
    context = RuntimeContext(RuntimeMode.ISOLATED_DEV, "architecture-foundation", root)
    with pytest.raises(RuntimeAuthorizationError, match="required"):
        assert_runtime_write(None, root / "state.json")
    with pytest.raises(RuntimeAuthorizationError, match="outside"):
        assert_runtime_write(context, root / ".." / "escape.json")
    with pytest.raises(RuntimeAuthorizationError, match="marker cannot be changed"):
        assert_runtime_write(context, root / MARKER_FILENAME)


def test_missing_marker_module_mismatch_and_classification_mismatch_reject(tmp_path: Path) -> None:
    unmarked = tmp_path / "unmarked"
    unmarked.mkdir()
    with pytest.raises(RuntimeAuthorizationError, match="marker is missing"):
        RuntimeContext(RuntimeMode.ISOLATED_DEV, "architecture-foundation", unmarked)

    root = marked_runtime(tmp_path, "isolated-dev", module_id="another-module")
    with pytest.raises(RuntimeAuthorizationError, match="module_id mismatch"):
        RuntimeContext(RuntimeMode.ISOLATED_DEV, "architecture-foundation", root)
    with pytest.raises(RuntimeAuthorizationError, match="classification mismatch"):
        RuntimeContext(RuntimeMode.FIXTURE, "another-module", root)


def test_formal_runtime_is_read_only(tmp_path: Path) -> None:
    root = marked_runtime(tmp_path, "formal")
    identity = load_runtime_identity(root)
    context = RuntimeContext(
        RuntimeMode.FORMAL_READONLY,
        "architecture-foundation",
        root,
        formal_identity=identity,
    )
    assert context.write_allowed is False
    with pytest.raises(RuntimeAuthorizationError, match="read-only"):
        assert_runtime_write(context, root / "state.json")


def test_formal_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    first = marked_runtime(tmp_path, "formal")
    second = tmp_path / "second-formal"
    second.mkdir()
    (second / MARKER_FILENAME).write_text(
        (first / MARKER_FILENAME).read_text(encoding="utf-8").replace("fixture-formal", "another-formal"),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeAuthorizationError, match="formal runtime identity mismatch"):
        RuntimeContext(
            RuntimeMode.FORMAL_READONLY,
            "architecture-foundation",
            first,
            formal_identity=load_runtime_identity(second),
        )


def test_production_write_rejects_linked_worktree(tmp_path: Path) -> None:
    root = marked_runtime(tmp_path, "formal")
    identity = load_runtime_identity(root)
    repository = tmp_path / "linked-worktree"
    repository.mkdir()
    (repository / ".git").write_text("gitdir: ../repo/.git/worktrees/linked\n", encoding="utf-8")
    with pytest.raises(RuntimeAuthorizationError, match="linked worktree"):
        RuntimeContext(
            RuntimeMode.PRODUCTION_WRITE,
            "architecture-foundation",
            root,
            formal_identity=identity,
            expected_runtime_id=identity.runtime_id,
            repository_root=repository,
        )


def test_production_write_requires_expected_runtime_id(tmp_path: Path) -> None:
    root = marked_runtime(tmp_path, "formal")
    identity = load_runtime_identity(root)
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    with pytest.raises(RuntimeAuthorizationError, match="runtime_id mismatch"):
        RuntimeContext(
            RuntimeMode.PRODUCTION_WRITE,
            "architecture-foundation",
            root,
            formal_identity=identity,
            expected_runtime_id="wrong",
            repository_root=repository,
        )


def test_empty_git_directory_cannot_authorize_production(tmp_path: Path) -> None:
    root = marked_runtime(tmp_path, "formal")
    identity = load_runtime_identity(root)
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    with pytest.raises(RuntimeAuthorizationError, match="explicit execution_request"):
        RuntimeContext(
            RuntimeMode.PRODUCTION_WRITE,
            "architecture-foundation",
            root,
            formal_identity=identity,
            expected_runtime_id=identity.runtime_id,
            repository_root=repository,
        )


def test_marker_alone_cannot_authorize_production(tmp_path: Path) -> None:
    root = marked_runtime(tmp_path, "formal")
    identity = load_runtime_identity(root)
    with pytest.raises(RuntimeAuthorizationError, match="execution_request is required"):
        RuntimeContext(
            RuntimeMode.PRODUCTION_WRITE, "architecture-foundation", root,
            formal_identity=identity, expected_runtime_id=identity.runtime_id,
        )


def test_candidate_validation_requires_separate_identity(tmp_path: Path) -> None:
    root = marked_runtime(tmp_path, "candidate-validation")
    identity = load_runtime_identity(root)
    with pytest.raises(RuntimeAuthorizationError, match="execution_request is required"):
        RuntimeContext(
            RuntimeMode.CANDIDATE_VALIDATION, "architecture-foundation", root,
            expected_runtime_id=identity.runtime_id,
        )
    with pytest.raises(RuntimeAuthorizationError, match="classification mismatch"):
        RuntimeContext(
            RuntimeMode.PRODUCTION_WRITE, "architecture-foundation", root,
            formal_identity=identity, expected_runtime_id=identity.runtime_id,
        )


def test_runtime_marker_change_invalidates_existing_context(tmp_path: Path) -> None:
    root = marked_runtime(tmp_path, "isolated-dev")
    context = RuntimeContext(RuntimeMode.ISOLATED_DEV, "architecture-foundation", root)
    marker = root / MARKER_FILENAME
    marker.write_text(marker.read_text(encoding="utf-8").replace("fixture-isolated-dev", "replaced"), encoding="utf-8")
    with pytest.raises(RuntimeAuthorizationError, match="identity changed"):
        assert_runtime_write(context, root / "state.json")


def test_symbolic_marker_is_rejected_before_identity_read(tmp_path: Path, monkeypatch) -> None:
    root = marked_runtime(tmp_path, "formal")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == root / MARKER_FILENAME or original(self))
    with pytest.raises(RuntimeAuthorizationError, match="symbolic link"):
        load_runtime_identity(root)


def test_production_git_identity_is_rechecked_before_each_write(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    def git(*arguments: str) -> str:
        return subprocess.check_output(["git", "-C", str(repository), *arguments], text=True).strip()
    git("init", "--quiet")
    (repository / "source.txt").write_text("approved source\n", encoding="utf-8")
    git("add", "source.txt")
    git("-c", "user.name=Runtime Test", "-c", "user.email=runtime@example.invalid", "commit", "--quiet", "-m", "fixture")
    commit, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    # Simulate the source location only; Git itself and its clean checks are real.
    monkeypatch.setattr(production_identity, "_ROOT", repository)
    root = marked_runtime(tmp_path, "formal")
    identity = load_runtime_identity(root)
    context = RuntimeContext(
        RuntimeMode.PRODUCTION_WRITE, "architecture-foundation", root,
        formal_identity=identity, expected_runtime_id=identity.runtime_id,
        execution_request=GitExecutionRequest(repository, commit, tree),
    )
    assert assert_runtime_write(context, root / "result.json") == root / "result.json"
    (repository / "source.txt").write_text("modified after authorization\n", encoding="utf-8")
    with pytest.raises(RuntimeAuthorizationError, match="dirty"):
        assert_runtime_write(context, root / "result.json")


def test_symlink_or_junction_escape_is_rejected(tmp_path: Path) -> None:
    root = marked_runtime(tmp_path, "isolated-dev")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "linked-outside"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    else:
        os.symlink(outside, link, target_is_directory=True)
    try:
        context = RuntimeContext(RuntimeMode.ISOLATED_DEV, "architecture-foundation", root)
        with pytest.raises(RuntimeAuthorizationError, match="outside"):
            assert_runtime_write(context, link / "escape.json")
    finally:
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()
