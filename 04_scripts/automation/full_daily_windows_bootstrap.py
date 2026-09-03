#!/usr/bin/env python
"""Start FULL DAILY from an approved, isolated Git checkout."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


CANONICAL_REPOSITORY = Path(r"C:\Users\xx202\Desktop\codex自动更新\codex-projects\market-data")
PRODUCTION_COMMIT_ENV = "MARKET_DATA_FULL_DAILY_PRODUCTION_COMMIT"
FULL_SHA = re.compile(r"[0-9a-f]{40}")


class BootstrapFailure(RuntimeError):
    """Raised when the approved production control plane cannot be proved."""


@dataclass(frozen=True)
class ApprovedIdentity:
    commit: str
    tree: str
    remote_main: str
    remote_main_tree: str


def _git(repository: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(repository), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip()
        raise BootstrapFailure(f"git {' '.join(args)} failed: {detail}")
    return process.stdout.strip()


def resolve_approved_identity(repository: Path, production_commit: str) -> ApprovedIdentity:
    repository = repository.resolve()
    if not (repository / ".git").exists():
        raise BootstrapFailure(f"canonical repository is unavailable: {repository}")
    if not FULL_SHA.fullmatch(production_commit):
        raise BootstrapFailure("approved production commit must be a full lowercase SHA")

    advertised = _git(repository, "ls-remote", "--exit-code", "origin", "refs/heads/main")
    fields = advertised.split()
    if len(fields) != 2 or fields[1] != "refs/heads/main" or not FULL_SHA.fullmatch(fields[0]):
        raise BootstrapFailure("origin did not advertise one valid refs/heads/main SHA")
    remote_main = fields[0]
    _git(
        repository,
        "fetch",
        "--no-tags",
        "origin",
        "+refs/heads/main:refs/remotes/origin/main",
    )
    fetched_main = _git(repository, "rev-parse", "refs/remotes/origin/main")
    if fetched_main != remote_main:
        raise BootstrapFailure("fetched origin/main does not match the live remote advertisement")

    _git(repository, "cat-file", "-e", f"{production_commit}^{{commit}}")
    ancestry = subprocess.run(
        ["git", "-C", str(repository), "merge-base", "--is-ancestor", production_commit, remote_main],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if ancestry.returncode != 0:
        raise BootstrapFailure("approved production commit is not on trusted origin/main")
    return ApprovedIdentity(
        commit=production_commit,
        tree=_git(repository, "rev-parse", f"{production_commit}^{{tree}}"),
        remote_main=remote_main,
        remote_main_tree=_git(repository, "rev-parse", f"{remote_main}^{{tree}}"),
    )


def validate_detached_checkout(repository: Path, identity: ApprovedIdentity) -> None:
    if _git(repository, "rev-parse", "HEAD") != identity.commit:
        raise BootstrapFailure("approved control-plane checkout HEAD mismatch")
    if _git(repository, "rev-parse", "HEAD^{tree}") != identity.tree:
        raise BootstrapFailure("approved control-plane checkout tree mismatch")
    branch = _git(repository, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "HEAD":
        raise BootstrapFailure("approved control-plane checkout is not detached")
    if _git(repository, "status", "--porcelain", "--untracked-files=all"):
        raise BootstrapFailure("approved control-plane checkout is dirty")
    _git(repository, "fsck", "--no-dangling", identity.commit)


def create_detached_checkout(source: Path, destination: Path, identity: ApprovedIdentity) -> None:
    process = subprocess.run(
        ["git", "clone", "--no-hardlinks", "--no-checkout", str(source), str(destination)],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode:
        raise BootstrapFailure(f"control-plane clone failed: {process.stderr.strip()}")
    _git(destination, "checkout", "--detach", identity.commit)
    validate_detached_checkout(destination, identity)


def isolated_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def run_approved_control_plane(
    python: Path,
    source_repository: Path,
    control_plane: Path,
    identity: ApprovedIdentity,
    trigger_source: str,
    timeout_seconds: float,
) -> int:
    runner = control_plane / "04_scripts" / "automation" / "run_full_daily_windows.py"
    if not runner.is_file():
        raise BootstrapFailure("approved control-plane runner is unavailable")
    command = [
        str(python),
        "-I",
        str(runner),
        "--trigger-source",
        trigger_source,
        "--timeout-seconds",
        str(timeout_seconds),
        "--source-repository",
        str(source_repository.resolve()),
        "--approved-control-plane-commit",
        identity.commit,
        "--approved-control-plane-tree",
        identity.tree,
    ]
    return subprocess.run(command, cwd=control_plane, env=isolated_environment(), check=False).returncode


def launch(trigger_source: str, source_repository: Path, timeout_seconds: float) -> int:
    production_commit = os.environ.get(PRODUCTION_COMMIT_ENV, "").strip()
    identity = resolve_approved_identity(source_repository, production_commit)
    python = source_repository / ".venv-py312" / "Scripts" / "python.exe"
    if not python.is_file():
        raise BootstrapFailure("exact market-data Python executable is unavailable")
    temp_root = Path(tempfile.mkdtemp(prefix="market-data-approved-control-plane-"))
    control_plane = temp_root / "repository"
    try:
        create_detached_checkout(source_repository, control_plane, identity)
        return run_approved_control_plane(
            python, source_repository, control_plane, identity, trigger_source, timeout_seconds
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the approved FULL DAILY control plane")
    parser.add_argument("--trigger-source", required=True, choices=("manual", "scheduled"))
    parser.add_argument("--source-repository", type=Path, default=CANONICAL_REPOSITORY)
    parser.add_argument("--timeout-seconds", type=float, default=14_400)
    args = parser.parse_args(argv)
    try:
        return launch(args.trigger_source, args.source_repository, args.timeout_seconds)
    except BootstrapFailure as exc:
        print(f"FULL DAILY bootstrap failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
