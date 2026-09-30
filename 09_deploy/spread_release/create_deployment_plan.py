from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from release_contract import (
    ContractError,
    DockerReleaseRuntime,
    artifact_manifest_path,
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
    parser.add_argument(
        "--tool-repo-root",
        "--repository",
        dest="tool_repo_root",
        type=Path,
        default=DEFAULT_REPOSITORY,
        help="Independent read-only checkout containing the target Release SHA.",
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--release-env", type=Path)
    parser.add_argument(
        "--production-project-dir",
        "--production-project-directory",
        dest="production_project_dir",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--production-compose-file",
        type=Path,
        required=True,
        help=(
            "Absolute current production Compose A used for baseline validation and "
            "rollback; the target switch uses docker-compose.yml from tool_repo_root."
        ),
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
    arguments = sys.argv[1:] if argv is None else argv
    if '--high-risk-input' in arguments:
        parser = argparse.ArgumentParser(allow_abbrev=False)
        parser.add_argument('--high-risk-input', type=Path, required=True)
        parser.add_argument('--output', type=Path, required=True)
        args = parser.parse_args(arguments)
        from high_risk_execution import HostBackend, seal_plan
        backend = HostBackend(args.output.parent)
        backend.host._require_linux_root()
        backend.host.require_protected_authority_source()
        raw = backend.host._protected_path(args.high_risk_input, private=True).read_bytes()
        plan = backend.host._json(raw)
        seal_plan(plan, args.output)
        print(json.dumps({'deployment_plan': str(args.output), 'sha256': hash_file(args.output),
                          'production_authorized': False}, sort_keys=True))
        return 0
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
        if any(
            (manifest_path.parent / name).exists()
            for name in (
                "deployment_result.json",
                "deployment_result.manifest.json",
            )
        ):
            raise ContractError(
                "deployment_result.json already exists; a new plan cannot be sealed"
            )
        plan = create_deployment_plan(
            tool_repo_root=args.tool_repo_root,
            production_compose_file=args.production_compose_file,
            production_project_dir=args.production_project_dir,
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
        plan_manifest = artifact_manifest_path(output_path, "deployment_plan")
    except ContractError as exc:
        print(f"deployment plan rejected: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "plan_status": plan["plan_status"],
                "deployment_plan": str(output_path),
                "deployment_plan_sha256": hash_file(output_path),
                "deployment_plan_manifest": str(plan_manifest),
                "deployment_plan_manifest_sha256": hash_file(plan_manifest),
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
