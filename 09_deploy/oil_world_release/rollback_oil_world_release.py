#!/usr/bin/env python3
"""Restore only Oil World's sealed rollback image; never rebuild or touch sidecars."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from deploy_oil_world_release import _assert_sidecars_unchanged, _image_id, _restore_environment, _write_environment_image
from prepare_oil_world_candidate import SERVICE, _run, formal_snapshot, validate_candidate_http
from oil_world_release_contract import ContractError, hash_file, parse_environment, validate_production_environment, verify_artifact


Runner = Callable[..., Any]


def rollback(args: argparse.Namespace, runner: Runner = subprocess.run) -> dict[str, Any]:
    directory = args.release_dir.resolve()
    release, _ = verify_artifact(directory, "release")
    plan, _ = verify_artifact(directory, "deployment_plan", expected_git_commit=release["git_commit"], expected_git_tree=release["git_tree"], expected_image_id=release["image_id"])
    rollback = plan["rollback"]
    if args.dry_run:
        return {"status": "dry_run", "rollback_image_ref": rollback["image_ref"], "rollback_image_id": rollback["image_id"], "execute_argv": plan["execute_argv"]}
    if _image_id(rollback["image_ref"], runner) != rollback["image_id"]:
        raise ContractError("sealed rollback image reference no longer resolves to its sealed Image ID")
    environment_path = Path(plan["production_env_file"])
    if not environment_path.is_file():
        raise ContractError("sealed production environment file is missing")
    before = formal_snapshot(runner, phase="before-rollback")
    original = _write_environment_image(environment_path, rollback["image_ref"])
    try:
        _run(list(plan["execute_argv"]), runner)
        after = formal_snapshot(runner, phase="after-rollback")
        _assert_sidecars_unchanged(before, after)
        oil = next(item for item in after["containers"] if item["name"] == SERVICE)
        if oil["image_id"] != rollback["image_id"]:
            raise ContractError("rollback container did not use the sealed rollback Image ID")
        http = validate_candidate_http(
            8081,
            data_root=Path(plan["data_identity"]["host_path"]),
            timeout=args.timeout_seconds,
        )
        return {"status": "rolled_back", "rollback_image_ref": rollback["image_ref"], "rollback_image_id": rollback["image_id"], "http": http["http"]}
    except Exception:
        _restore_environment(environment_path, original)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    args.dry_run = not args.execute
    try:
        print(json.dumps(rollback(args), ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Oil World rollback failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
