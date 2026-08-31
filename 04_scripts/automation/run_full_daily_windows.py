#!/usr/bin/env python
"""Stable CLI for the scheduler-neutral Windows FULL DAILY wrapper."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "03_src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agri_research_agent.automation.full_daily_windows import run_wrapper  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Windows FULL DAILY control wrapper")
    parser.add_argument("--trigger-source", required=True, choices=("manual", "scheduled"))
    parser.add_argument("--timeout-seconds", type=float, default=14_400)
    args = parser.parse_args(argv)
    return run_wrapper(args.trigger_source, timeout_seconds=args.timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
