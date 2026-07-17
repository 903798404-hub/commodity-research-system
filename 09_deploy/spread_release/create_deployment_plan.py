from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from release_contract import (
    ContractError,
    DockerReleaseRuntime,
    create_deployment_plan,
    hash_file,
    load_manifest_bundle,
    load_schema,
    write_deployment_plan,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPOSITORY = SCRIPT_DIR.parents[1]
RELEASE_SCHEMA_PATH = SCRIPT_DIR / "release.schema.json"
PLAN_SCHEMA_PATH = SCRIPT_DIR / "deployment_plan.schema.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Seal the explicit non-sensitive production environment and semantic "
            "Compose contract for one already sealed and candidate-validated release."
        )
    )
    parser.add_argument("--repository", type=Path, default=DEFAULT_REPOSITORY)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--release-env", type=Path)
    parser.add_argument(
        "--production-project-directory",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--candidate-result",
        type=Path,
        help="Defaults to candidate_result.json beside release.json.",
    )
    parser.add_argument("--production-env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


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
        else manifest_path.parent / "deployment_plan.json"
    )
    try:
        manifest, _ = load_manifest_bundle(
            manifest_path,
            release_env_path,
            RELEASE_SCHEMA_PATH,
        )
        if (manifest_path.parent / "deployment_result.json").exists():
            raise ContractError(
                "deployment_result.json already exists; a new plan cannot be sealed"
            )
        plan = create_deployment_plan(
            repository=args.repository,
            production_project_directory=args.production_project_directory,
            candidate_result_file=(
                args.candidate_result.resolve()
                if args.candidate_result
                else manifest_path.parent / "candidate_result.json"
            ),
            production_env_file=args.production_env_file,
            manifest=manifest,
            runtime=DockerReleaseRuntime(),
            schema=load_schema(PLAN_SCHEMA_PATH),
        )
        write_deployment_plan(plan, output_path)
    except ContractError as exc:
        print(f"deployment plan rejected: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "plan_status": plan["plan_status"],
                "deployment_plan": str(output_path),
                "deployment_plan_sha256": hash_file(output_path),
                "production_compose_sha256": plan[
                    "production_compose_sha256"
                ],
                "allowed_candidate_production_differences": plan[
                    "allowed_candidate_production_differences"
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
