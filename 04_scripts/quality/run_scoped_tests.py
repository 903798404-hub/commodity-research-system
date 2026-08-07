"""Run an explicitly declared test scope; never infer a module or deploy action."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODES = ("special", "impact", "full")


def _repo_test_path(project_root: Path, value: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"测试路径必须是仓库内相对路径: {value}")
    resolved = (project_root / candidate).resolve()
    if not resolved.is_file() or project_root.resolve() not in resolved.parents:
        raise ValueError(f"测试文件不存在或不在仓库内: {value}")
    return candidate.as_posix()


def build_test_plan(mode: str, tests: Sequence[str], project_root: Path = PROJECT_ROOT) -> dict[str, object]:
    """Create a plan without executing it."""
    if mode not in MODES:
        raise ValueError(f"未知测试模式: {mode}")
    if mode == "full":
        if tests:
            raise ValueError("完整回归不接受局部测试路径；请使用 special 或 impact")
        selected_tests: list[str] = []
    else:
        if not tests:
            raise ValueError(f"{mode} 模式必须显式提供至少一个 --tests 路径；不会猜测执行范围")
        selected_tests = [_repo_test_path(project_root, value) for value in tests]

    command = [sys.executable, "-m", "pytest", *selected_tests]
    return {
        "mode": mode,
        "selected_tests": selected_tests,
        "command": command,
        "full_regression": mode == "full",
        "side_effects": "仅在非 dry-run 时执行 pytest；不会执行 commit、push 或部署",
    }


def execute_plan(plan: dict[str, object], project_root: Path = PROJECT_ROOT, *, dry_run: bool = False) -> int:
    print(json.dumps({"dry_run": dry_run, **plan}, ensure_ascii=False, indent=2))
    if dry_run:
        return 0
    return subprocess.run(plan["command"], cwd=project_root, check=False).returncode  # type: ignore[arg-type]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, required=True, help="special、impact 或 full")
    parser.add_argument("--tests", nargs="*", default=[], metavar="PATH", help="显式 pytest 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只显示 pytest 计划")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = build_test_plan(args.mode, args.tests)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    return execute_plan(plan, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
