#!/usr/bin/env python3
"""Prepare an isolated, exact-SHA sparse checkout for a USDA candidate release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


USDA_PROJECT = Path("11_独立应用/USDA平衡表")
RELEASE_TOOLS = Path("09_deploy/usda_release")
SPARSE_PATHS = (
    "/11_独立应用/USDA平衡表/",
    "/09_deploy/usda_release/",
)
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
OUTPUT_LIMIT = 8_192


class PreparationError(RuntimeError):
    """A release source preparation gate failed."""


@dataclass(frozen=True)
class Options:
    repository_url: str
    git_commit: str
    expected_tree: str
    expected_main_sha: str
    run_directory: Path
    ssh_command: str
    fetch_timeout_seconds: int = 180
    checkout_timeout_seconds: int = 180


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float


Runner = Callable[[Sequence[str], Path, int], CommandResult]


def _trim(value: str) -> str:
    return value[-OUTPUT_LIMIT:]


def run_bounded(command: Sequence[str], cwd: Path, timeout_seconds: int) -> CommandResult:
    started = time.monotonic()
    popen_kwargs: dict[str, object] = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    process = subprocess.Popen(list(command), **popen_kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        stdout, stderr = process.communicate()
        raise PreparationError(
            f"command timed out after {timeout_seconds}s: {' '.join(command[:3])}; "
            f"stdout={_trim(stdout or str(exc.stdout or ''))!r}; "
            f"stderr={_trim(stderr or str(exc.stderr or ''))!r}"
        ) from exc
    return CommandResult(
        returncode=process.returncode,
        stdout=_trim(stdout),
        stderr=_trim(stderr),
        elapsed_seconds=round(time.monotonic() - started, 3),
    )


def _run_checked(
    runner: Runner,
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: int,
    label: str,
) -> CommandResult:
    result = runner(command, cwd, timeout_seconds)
    if result.returncode != 0:
        raise PreparationError(
            f"{label} failed with exit code {result.returncode}; "
            f"stdout={result.stdout!r}; stderr={result.stderr!r}"
        )
    return result


def validate_options(options: Options) -> None:
    for label, value in (
        ("git commit", options.git_commit),
        ("expected tree", options.expected_tree),
        ("expected main SHA", options.expected_main_sha),
    ):
        if not FULL_SHA.fullmatch(value):
            raise PreparationError(f"{label} must be a lowercase full 40-character SHA")
    if options.git_commit != options.expected_main_sha:
        raise PreparationError("exact candidate commit must equal the approved main SHA")
    if not options.repository_url.strip():
        raise PreparationError("repository URL is required")
    if not options.ssh_command.strip():
        raise PreparationError("readonly SSH command is required")
    if options.fetch_timeout_seconds <= 0 or options.checkout_timeout_seconds <= 0:
        raise PreparationError("timeouts must be positive")
    if options.run_directory.exists():
        raise PreparationError(
            f"run directory already exists and cannot be reused: {options.run_directory}"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json_exclusive(path: Path, payload: dict[str, object]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    return _sha256(path)


def _git(
    runner: Runner,
    repo: Path,
    timeout: int,
    *args: str,
    label: str,
) -> CommandResult:
    return _run_checked(runner, ("git", *args), repo, timeout, label)


def _record_step(
    steps: list[dict[str, object]], label: str, result: CommandResult
) -> None:
    steps.append(
        {
            "label": label,
            "exit_code": result.returncode,
            "elapsed_seconds": result.elapsed_seconds,
        }
    )


def verify_checkout(
    options: Options,
    source_repo: Path,
    runner: Runner,
    steps: list[dict[str, object]],
) -> dict[str, object]:
    timeout = options.checkout_timeout_seconds

    def query(label: str, *args: str) -> str:
        result = _git(runner, source_repo, timeout, *args, label=label)
        _record_step(steps, label, result)
        return result.stdout.strip()

    head = query("verify HEAD", "rev-parse", "HEAD")
    tree = query("verify tree", "rev-parse", "HEAD^{tree}")
    status = query("verify clean worktree", "status", "--porcelain=v1")
    promisor = query("verify promisor", "config", "--get", "remote.origin.promisor")
    partial_filter = query(
        "verify partial clone filter", "config", "--get", "remote.origin.partialclonefilter"
    )
    remote_main = query("verify remote main", "ls-remote", "origin", "refs/heads/main")
    fsck = _git(
        runner,
        source_repo,
        timeout,
        "fsck",
        "--connectivity-only",
        label="verify connectivity",
    )
    _record_step(steps, "verify connectivity", fsck)

    remote_fields = remote_main.split()
    actual_main = remote_fields[0] if len(remote_fields) == 2 else ""
    failures: list[str] = []
    if head != options.git_commit:
        failures.append(f"HEAD mismatch: expected {options.git_commit}, got {head or '<empty>'}")
    if tree != options.expected_tree:
        failures.append(f"tree mismatch: expected {options.expected_tree}, got {tree or '<empty>'}")
    if status:
        failures.append("checkout worktree is not clean")
    if promisor.lower() != "true":
        failures.append(f"remote.origin.promisor is {promisor!r}, expected 'true'")
    if partial_filter.lower() != "blob:none":
        failures.append(
            f"remote.origin.partialclonefilter is {partial_filter!r}, expected 'blob:none'"
        )
    if actual_main != options.expected_main_sha:
        failures.append(
            f"origin/main mismatch: expected {options.expected_main_sha}, "
            f"got {actual_main or '<empty>'}"
        )
    if failures:
        raise PreparationError("; ".join(failures))

    docker_context = source_repo / USDA_PROJECT
    if docker_context == source_repo or not docker_context.is_dir():
        raise PreparationError("USDA Docker context is missing or resolves to repository root")
    if not (docker_context / "Dockerfile").is_file():
        raise PreparationError("USDA Dockerfile is missing from the sparse checkout")

    return {
        "head": head,
        "tree": tree,
        "worktree_clean": True,
        "promisor": True,
        "partial_clone_filter": partial_filter,
        "remote_main": actual_main,
        "fsck_connectivity": "pass",
        "docker_context": str(docker_context.resolve()),
    }


def prepare_source(options: Options, runner: Runner = run_bounded) -> dict[str, object]:
    validate_options(options)
    run_dir = options.run_directory.resolve()
    source_repo = run_dir / "source"
    evidence_dir = run_dir / "evidence"
    steps: list[dict[str, object]] = []
    run_dir.mkdir(parents=True, exist_ok=False)
    source_repo.mkdir()
    evidence_dir.mkdir()
    started = time.time()

    try:
        commands: tuple[tuple[str, tuple[str, ...], int], ...] = (
            ("initialize isolated repository", ("init", "."), options.checkout_timeout_seconds),
            (
                "persist readonly SSH command",
                ("config", "core.sshCommand", options.ssh_command),
                options.checkout_timeout_seconds,
            ),
            (
                "add origin",
                ("remote", "add", "origin", options.repository_url),
                options.checkout_timeout_seconds,
            ),
            (
                "fetch exact SHA",
                (
                    "-c",
                    "protocol.version=2",
                    "fetch",
                    "--depth=1",
                    "--filter=blob:none",
                    "origin",
                    options.git_commit,
                ),
                options.fetch_timeout_seconds,
            ),
            (
                "initialize sparse checkout",
                ("sparse-checkout", "init", "--no-cone"),
                options.checkout_timeout_seconds,
            ),
            (
                "set approved sparse paths",
                ("sparse-checkout", "set", "--no-cone", *SPARSE_PATHS),
                options.checkout_timeout_seconds,
            ),
            (
                "checkout exact SHA",
                ("checkout", "--detach", options.git_commit),
                options.checkout_timeout_seconds,
            ),
        )
        for label, arguments, timeout in commands:
            result = _git(runner, source_repo, timeout, *arguments, label=label)
            _record_step(steps, label, result)

        identity = verify_checkout(options, source_repo, runner, steps)
        payload: dict[str, object] = {
            "schema_version": 1,
            "status": "success",
            "repository_url": options.repository_url,
            "requested_commit": options.git_commit,
            "expected_tree": options.expected_tree,
            "expected_main_sha": options.expected_main_sha,
            "sparse_paths": list(SPARSE_PATHS),
            "source_repository": str(source_repo),
            "identity": identity,
            "steps": steps,
            "elapsed_seconds": round(time.time() - started, 3),
        }
        evidence_path = evidence_dir / "checkout_identity.json"
        payload["evidence_sha256"] = _write_json_exclusive(evidence_path, payload)
        return payload
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "status": "failed",
            "repository_url": options.repository_url,
            "requested_commit": options.git_commit,
            "expected_tree": options.expected_tree,
            "expected_main_sha": options.expected_main_sha,
            "sparse_paths": list(SPARSE_PATHS),
            "source_repository": str(source_repo),
            "steps": steps,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "elapsed_seconds": round(time.time() - started, 3),
            "docker_build_started": False,
        }
        _write_json_exclusive(evidence_dir / "checkout_failure.json", failure)
        if isinstance(exc, PreparationError):
            raise
        raise PreparationError(str(exc)) from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--expected-tree", required=True)
    parser.add_argument("--expected-main-sha", required=True)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--ssh-command", required=True)
    parser.add_argument("--fetch-timeout-seconds", type=int, default=180)
    parser.add_argument("--checkout-timeout-seconds", type=int, default=180)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.execute:
        print("Refusing to prepare source without --execute", file=sys.stderr)
        return 2
    options = Options(
        repository_url=args.repository_url,
        git_commit=args.git_commit,
        expected_tree=args.expected_tree,
        expected_main_sha=args.expected_main_sha,
        run_directory=args.run_directory,
        ssh_command=args.ssh_command,
        fetch_timeout_seconds=args.fetch_timeout_seconds,
        checkout_timeout_seconds=args.checkout_timeout_seconds,
    )
    try:
        result = prepare_source(options)
    except PreparationError as exc:
        print(f"USDA candidate source preparation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
