#!/usr/bin/env python3
"""Atomically apply one environment transition from a sealed deployment plan."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from release_contract import (
    ContractError,
    capture_git_identity,
    load_deployment_plan,
    load_manifest_bundle,
    transition_production_env,
)


SCRIPT_DIR = Path(__file__).resolve().parent
RELEASE_SCHEMA = SCRIPT_DIR / "release.schema.json"
PLAN_SCHEMA = SCRIPT_DIR / "deployment_plan.schema.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply or restore the production env sealed in a deployment plan."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--release-env", type=Path, required=True)
    parser.add_argument("--deployment-plan", type=Path, required=True)
    parser.add_argument("--action", choices=("target", "rollback"), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest, _ = load_manifest_bundle(
            args.manifest.resolve(),
            args.release_env.resolve(),
            RELEASE_SCHEMA,
        )
        plan, _ = load_deployment_plan(
            args.deployment_plan.resolve(),
            manifest,
            PLAN_SCHEMA,
        )
        tool_repo_root = SCRIPT_DIR.parents[1].resolve()
        if Path(str(plan["tool_repo_root"])).resolve() != tool_repo_root:
            raise ContractError("environment transition tool_repo_root changed")
        if capture_git_identity(tool_repo_root) != plan["deployment_tool_revision"]:
            raise ContractError("environment transition tool revision changed")
        evidence = transition_production_env(
            plan,
            to_target=args.action == "target",
        )
    except ContractError as exc:
        print(f"production env transition rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
