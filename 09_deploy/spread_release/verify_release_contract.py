from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from release_contract import (
    APPLICATION,
    ContractError,
    DEPLOYMENT_RESULT_BUNDLE_FILENAME,
    DEPLOYMENT_RESULT_SCHEMA_VERSION,
    DockerReleaseRuntime,
    hash_file,
    load_deployment_plan,
    load_manifest_bundle,
    validate_repository_static,
    verify_candidate,
    verify_post_deploy,
    verify_post_rollback,
    verify_pre_deploy,
    verify_pre_rollback,
    validate_deployment_result,
    load_schema,
    write_deployment_result_bundle,
    write_result,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPOSITORY = SCRIPT_DIR.parents[1]
SCHEMA_PATH = SCRIPT_DIR / "release.schema.json"
PLAN_SCHEMA_PATH = SCRIPT_DIR / "deployment_plan.schema.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a sealed spread release before or after an exact switch."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--tool-repo-root",
        "--repository",
        dest="tool_repo_root",
        type=Path,
        default=DEFAULT_REPOSITORY,
    )
    parser.add_argument(
        "--phase",
        choices=(
            "offline",
            "candidate",
            "pre-deploy",
            "post-deploy",
            "record-deployment",
            "pre-rollback",
            "post-rollback",
        ),
        required=True,
    )
    parser.add_argument("--result-path", type=Path)
    parser.add_argument("--readiness-result", type=Path)
    parser.add_argument("--deployment-plan", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest_path = args.manifest.resolve()
    env_path = (
        args.env_file.resolve()
        if args.env_file
        else manifest_path.parent / "release.env"
    )
    tool_repo_root = args.tool_repo_root.resolve()

    try:
        manifest, _ = load_manifest_bundle(manifest_path, env_path, SCHEMA_PATH)
        validate_repository_static(tool_repo_root)
        if args.phase == "offline":
            evidence = {
                "phase": "offline",
                "release_id": manifest["release_id"],
                "image_ref": manifest["image_ref"],
                "image_id": manifest["image_id"],
            }
        elif args.phase == "candidate":
            runtime = DockerReleaseRuntime()
            evidence = verify_candidate(manifest, runtime)
        else:
            if args.deployment_plan is None:
                raise ContractError(f"{args.phase} requires --deployment-plan")
            plan_path = args.deployment_plan.resolve()
            plan, production_environment = load_deployment_plan(
                plan_path,
                manifest,
                PLAN_SCHEMA_PATH,
            )
            plan_sha256 = hash_file(plan_path)
            runtime = DockerReleaseRuntime()
            if args.phase == "pre-deploy":
                if any(
                    (manifest_path.parent / name).exists()
                    for name in (
                        "deployment_result.json",
                        "deployment_result.manifest.json",
                        DEPLOYMENT_RESULT_BUNDLE_FILENAME,
                    )
                ):
                    raise ContractError(
                        "deployment_result.json already exists; this sealed release "
                        "will not be switched again"
                    )
                evidence = verify_pre_deploy(
                    manifest,
                    tool_repo_root,
                    runtime,
                    deployment_plan=plan,
                    production_environment=production_environment,
                )
            elif args.phase in {"post-deploy", "record-deployment"}:
                evidence = verify_post_deploy(
                    manifest,
                    runtime,
                    deployment_plan=plan,
                )
                if args.phase == "record-deployment":
                    if args.readiness_result is None:
                        raise ContractError(
                            "deployment result requires --readiness-result"
                        )
                    readiness_path = args.readiness_result.resolve()
                    try:
                        readiness = json.loads(
                            readiness_path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError) as exc:
                        raise ContractError(
                            f"cannot load readiness result {readiness_path}: {exc}"
                        ) from exc
                    if (
                        not isinstance(readiness, dict)
                        or readiness.get("status") != "ready"
                        or readiness.get("expected_image_id") != manifest["image_id"]
                        or readiness.get("policy") != plan["readiness_policy"]
                        or readiness.get("container") != "spread-dashboard"
                    ):
                        raise ContractError(
                            "deployment readiness result does not match the sealed plan"
                        )
                    result = {
                        "schema_version": DEPLOYMENT_RESULT_SCHEMA_VERSION,
                        "application": APPLICATION,
                        "release_id": manifest["release_id"],
                        "git_commit": manifest["git_commit"],
                        "target_git_commit": manifest["git_commit"],
                        "git_tree": manifest["git_tree"],
                        "image_ref": manifest["image_ref"],
                        "candidate_image_id": plan["candidate_image_id"],
                        "actual_image_id": evidence["actual_image_id"],
                        "oci_revision": evidence["oci_revision"],
                        "runtime_git_commit": evidence["runtime_git_commit"],
                        "runtime_git_commit_verified": evidence[
                            "runtime_git_commit_verified"
                        ],
                        "container_name": evidence["container_name"],
                        "config_image": evidence["config_image"],
                        "compose_project": plan["compose_project"],
                        "production_service": plan["production_service"],
                        "http_status": readiness.get("last_http_result", {}).get(
                            "http_status"
                        ),
                        "readiness": readiness,
                        "readiness_result": str(readiness_path),
                        "readiness_result_sha256": hash_file(readiness_path),
                        "deployment_plan": str(plan_path),
                        "deployment_plan_sha256": plan_sha256,
                        "production_env_file": plan["production_env_file"],
                        "production_env_sha256": plan["production_env_sha256"],
                        "production_compose_sha256": plan[
                            "production_compose_sha256"
                        ],
                        "weather_runtime_contract": plan[
                            "weather_runtime_contract"
                        ],
                        "generated_at": readiness.get("ready_at_utc"),
                        "status": "production_verified",
                    }
                    result_path = (
                        args.result_path.resolve()
                        if args.result_path
                        else manifest_path.parent / "deployment_result.json"
                    )
                    validate_deployment_result(
                        result,
                        manifest,
                        plan,
                        load_schema(
                            SCRIPT_DIR / "deployment_result.schema.json"
                        ),
                    )
                    write_result(result_path, result)
                    bundle_path = write_deployment_result_bundle(
                        result_path,
                        manifest,
                        plan,
                    )
                    evidence = {
                        **result,
                        "result_path": str(result_path),
                        "result_manifest": str(
                            result_path.with_name(
                                "deployment_result.manifest.json"
                            )
                        ),
                        "result_bundle": str(bundle_path),
                    }
            elif args.phase == "pre-rollback":
                evidence = verify_pre_rollback(
                    manifest,
                    tool_repo_root,
                    runtime,
                    deployment_plan=plan,
                    production_environment=production_environment,
                )
            else:
                evidence = verify_post_rollback(manifest, runtime)
                evidence["deployment_plan_sha256"] = plan_sha256
    except ContractError as exc:
        print(f"release contract rejected: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
