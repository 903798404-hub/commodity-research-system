#!/usr/bin/env python
"""Approved-checkout CLI for the scheduler-neutral Windows FULL DAILY wrapper."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(f"approved control-plane Git check failed: {detail}")
    return process.stdout.strip()


def validate_control_plane_root(commit: str, tree: str) -> None:
    if _git("rev-parse", "HEAD") != commit:
        raise RuntimeError("approved control-plane HEAD mismatch")
    if _git("rev-parse", "HEAD^{tree}") != tree:
        raise RuntimeError("approved control-plane tree mismatch")
    if _git("rev-parse", "--abbrev-ref", "HEAD") != "HEAD":
        raise RuntimeError("approved control-plane checkout is not detached")
    if _git("status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("approved control-plane checkout is dirty")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Windows FULL DAILY control wrapper")
    parser.add_argument("--trigger-source", required=True, choices=("manual", "scheduled"))
    parser.add_argument("--timeout-seconds", type=float, default=14_400)
    parser.add_argument("--source-repository", required=True, type=Path)
    parser.add_argument("--approved-control-plane-commit", required=True)
    parser.add_argument("--approved-control-plane-tree", required=True)
    args = parser.parse_args(argv)
    validate_control_plane_root(args.approved_control_plane_commit, args.approved_control_plane_tree)
    configured_commit = os.environ.get("MARKET_DATA_FULL_DAILY_PRODUCTION_COMMIT", "").strip()
    if configured_commit != args.approved_control_plane_commit:
        raise RuntimeError("approved production commit changed after bootstrap")

    src = ROOT / "03_src"
    sys.path.insert(0, str(src))
    from agri_research_agent.automation.full_daily_windows import run_wrapper

    return run_wrapper(
        args.trigger_source,
        repository=args.source_repository,
        python=Path(sys.executable),
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
