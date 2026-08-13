from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "09_deploy" / "usda_release" / "prepare_usda_candidate_source.py"
SPEC = importlib.util.spec_from_file_location("prepare_usda_candidate_source", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

COMMIT = "a" * 40
TREE = "b" * 40


class FakeRunner:
    def __init__(self, *, head=COMMIT, tree=TREE, status="", promisor="true", filter_="blob:none", main=COMMIT, fail_label=None):
        self.head = head
        self.tree = tree
        self.status = status
        self.promisor = promisor
        self.filter = filter_
        self.main = main
        self.fail_label = fail_label
        self.calls: list[tuple[tuple[str, ...], Path, int]] = []

    def __call__(self, command, cwd, timeout):
        command = tuple(command)
        self.calls.append((command, cwd, timeout))
        stdout = ""
        label = " ".join(command)
        if self.fail_label and self.fail_label in label:
            return MODULE.CommandResult(1, "", "simulated failure", 0.01)
        if len(command) >= 3 and command[1:3] == ("checkout", "--detach"):
            project = cwd / MODULE.USDA_PROJECT
            project.mkdir(parents=True, exist_ok=True)
            (project / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        if command[-2:] == ("rev-parse", "HEAD"):
            stdout = self.head + "\n"
        elif command[-2:] == ("rev-parse", "HEAD^{tree}"):
            stdout = self.tree + "\n"
        elif command[-2:] == ("status", "--porcelain=v1"):
            stdout = self.status
        elif command[-3:] == ("config", "--get", "remote.origin.promisor"):
            stdout = self.promisor + "\n"
        elif command[-3:] == ("config", "--get", "remote.origin.partialclonefilter"):
            stdout = self.filter + "\n"
        elif command[-3:] == ("ls-remote", "origin", "refs/heads/main"):
            stdout = f"{self.main}\trefs/heads/main\n"
        return MODULE.CommandResult(0, stdout, "", 0.01)


def options(tmp_path: Path, **overrides):
    values = {
        "repository_url": "git@github.com:owner/repo.git",
        "git_commit": COMMIT,
        "expected_tree": TREE,
        "expected_main_sha": COMMIT,
        "run_directory": tmp_path / "run-1",
        "ssh_command": "/home/ubuntu/.ssh/github-readonly.sh",
        "fetch_timeout_seconds": 30,
        "checkout_timeout_seconds": 20,
    }
    values.update(overrides)
    return MODULE.Options(**values)


def test_exact_sha_fetch_sparse_paths_persisted_ssh_and_subproject_context(tmp_path):
    runner = FakeRunner()
    opts = options(tmp_path)
    result = MODULE.prepare_source(opts, runner)

    commands = [call[0] for call in runner.calls]
    assert ("git", "config", "core.sshCommand", opts.ssh_command) in commands
    assert (
        "git", "-c", "protocol.version=2", "fetch", "--depth=1",
        "--filter=blob:none", "origin", COMMIT,
    ) in commands
    assert (
        "git", "sparse-checkout", "set", "--no-cone", *MODULE.SPARSE_PATHS,
    ) in commands
    assert ("git", "checkout", "--detach", COMMIT) in commands
    assert result["identity"]["docker_context"].endswith(str(MODULE.USDA_PROJECT))
    assert Path(result["identity"]["docker_context"]) != opts.run_directory / "source"
    assert not any("docker" in part for command in commands for part in command)


def test_wrong_requested_sha_is_rejected_before_run_directory_creation(tmp_path):
    opts = options(tmp_path, git_commit="short")
    with pytest.raises(MODULE.PreparationError, match="full 40-character SHA"):
        MODULE.prepare_source(opts, FakeRunner())
    assert not opts.run_directory.exists()


@pytest.mark.parametrize(
    ("runner", "message"),
    [
        (FakeRunner(head="c" * 40), "HEAD mismatch"),
        (FakeRunner(tree="d" * 40), "tree mismatch"),
        (FakeRunner(status=" M tracked.txt\n"), "not clean"),
        (FakeRunner(promisor="false"), "promisor"),
        (FakeRunner(filter_="blob:limit=1"), "partialclonefilter"),
        (FakeRunner(main="e" * 40), "origin/main mismatch"),
    ],
)
def test_identity_gate_failures_are_audited_and_never_build(tmp_path, runner, message):
    opts = options(tmp_path)
    with pytest.raises(MODULE.PreparationError, match=message):
        MODULE.prepare_source(opts, runner)
    failure = json.loads((opts.run_directory / "evidence" / "checkout_failure.json").read_text(encoding="utf-8"))
    assert failure["docker_build_started"] is False
    assert not any("docker" in part for call in runner.calls for part in call[0])


def test_fsck_connectivity_failure_is_fatal(tmp_path):
    opts = options(tmp_path)
    runner = FakeRunner(fail_label="fsck --connectivity-only")
    with pytest.raises(MODULE.PreparationError, match="connectivity"):
        MODULE.prepare_source(opts, runner)


def test_existing_run_directory_cannot_be_reused_as_candidate_source(tmp_path):
    opts = options(tmp_path)
    opts.run_directory.mkdir()
    with pytest.raises(MODULE.PreparationError, match="cannot be reused"):
        MODULE.prepare_source(opts, FakeRunner())


def test_checkout_failure_records_bounded_failure_without_docker(tmp_path):
    opts = options(tmp_path)
    runner = FakeRunner(fail_label="fetch --depth=1")
    with pytest.raises(MODULE.PreparationError, match="fetch exact SHA"):
        MODULE.prepare_source(opts, runner)
    failure_path = opts.run_directory / "evidence" / "checkout_failure.json"
    assert failure_path.is_file()
    assert json.loads(failure_path.read_text(encoding="utf-8"))["docker_build_started"] is False
    assert not any("docker" in part for call in runner.calls for part in call[0])


def test_success_evidence_records_required_partial_clone_identity(tmp_path):
    opts = options(tmp_path)
    MODULE.prepare_source(opts, FakeRunner())
    evidence = json.loads((opts.run_directory / "evidence" / "checkout_identity.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "success"
    assert evidence["identity"] == {
        "docker_context": str((opts.run_directory / "source" / MODULE.USDA_PROJECT).resolve()),
        "fsck_connectivity": "pass",
        "head": COMMIT,
        "partial_clone_filter": "blob:none",
        "promisor": True,
        "remote_main": COMMIT,
        "tree": TREE,
        "worktree_clean": True,
    }
    assert evidence["sparse_paths"] == list(MODULE.SPARSE_PATHS)
