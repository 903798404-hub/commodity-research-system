from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from release_contract import (
    ContractError,
    DockerReleaseRuntime,
    artifact_manifest_path,
    create_candidate_result,
    hash_file,
    load_manifest_bundle,
    validate_git_state,
    write_candidate_result,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPOSITORY = SCRIPT_DIR.parents[1]
RELEASE_SCHEMA_PATH = SCRIPT_DIR / "release.schema.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate and seal candidate_result.json after candidate identity, "
            "readiness, HTTP, page, formal-runtime and data checks have passed. "
            "The readiness result must contain the bounded, redacted candidate "
            "log summary. This command does not delete the candidate container."
        )
    )
    parser.add_argument(
        "--tool-repo-root",
        "--repository",
        dest="tool_repo_root",
        type=Path,
        default=DEFAULT_REPOSITORY,
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--release-env", type=Path)
    parser.add_argument("--readiness-result", type=Path, required=True)
    parser.add_argument(
        "--formal-containers-before",
        type=Path,
        required=True,
        help="Snapshot captured before the candidate container was started.",
    )
    parser.add_argument(
        "--checks-file",
        type=Path,
        required=True,
        help=(
            "JSON object containing http, pages, formal_git_unchanged, "
            "data_files_unchanged and "
            "production_switch_performed evidence summaries."
        ),
    )
    parser.add_argument("--output", type=Path)
    return parser


def load_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load {description} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"{description} must be a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest_path = args.manifest.resolve()
    release_env_path = (
        args.release_env.resolve()
        if args.release_env
        else manifest_path.parent / "release.env"
    )
    output_path = (
        args.output.resolve()
        if args.output
        else manifest_path.parent / "candidate_result.json"
    )
    try:
        manifest, _ = load_manifest_bundle(
            manifest_path,
            release_env_path,
            RELEASE_SCHEMA_PATH,
        )
        actual_tree = validate_git_state(
            args.tool_repo_root.resolve(),
            manifest["git_commit"],
        )
        if actual_tree != manifest["git_tree"]:
            raise ContractError("tool repository Tree SHA differs from release.json")
        checks = load_json_object(
            args.checks_file.resolve(),
            "candidate checks",
        )
        checks["formal_containers_before"] = load_json_object(
            args.formal_containers_before.resolve(),
            "before-candidate formal container snapshot",
        )
        result = create_candidate_result(
            manifest=manifest,
            runtime=DockerReleaseRuntime(),
            readiness=load_json_object(
                args.readiness_result.resolve(),
                "candidate readiness result",
            ),
            checks=checks,
        )
        write_candidate_result(result, output_path)
        result_manifest = artifact_manifest_path(output_path, "candidate_result")
    except ContractError as exc:
        print(f"candidate result rejected: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "status": result["status"],
                "candidate_result": str(output_path),
                "candidate_result_sha256": hash_file(output_path),
                "candidate_result_manifest": str(result_manifest),
                "candidate_result_manifest_sha256": hash_file(result_manifest),
                "git_commit": result["git_commit"],
                "git_tree": result["git_tree"],
                "candidate_image_id": result["candidate_image_id"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
