from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from release_contract import (
    ContractError,
    DockerReleaseRuntime,
    load_manifest_bundle,
    validate_repository_static,
    verify_post_deploy,
    verify_post_rollback,
    verify_pre_deploy,
    verify_pre_rollback,
    write_result,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPOSITORY = SCRIPT_DIR.parents[1]
SCHEMA_PATH = SCRIPT_DIR / "release.schema.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a sealed spread release before or after an exact switch."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--repository", type=Path, default=DEFAULT_REPOSITORY)
    parser.add_argument(
        "--phase",
        choices=(
            "offline",
            "pre-deploy",
            "post-deploy",
            "record-deployment",
            "pre-rollback",
            "post-rollback",
        ),
        required=True,
    )
    parser.add_argument("--result-path", type=Path)
    parser.add_argument("--http-status", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest_path = args.manifest.resolve()
    env_path = (
        args.env_file.resolve()
        if args.env_file
        else manifest_path.parent / "release.env"
    )
    repository = args.repository.resolve()

    try:
        manifest, _ = load_manifest_bundle(manifest_path, env_path, SCHEMA_PATH)
        validate_repository_static(repository)
        if args.phase == "offline":
            evidence = {
                "phase": "offline",
                "release_id": manifest["release_id"],
                "image_ref": manifest["image_ref"],
                "image_id": manifest["image_id"],
            }
        else:
            runtime = DockerReleaseRuntime()
            if args.phase == "pre-deploy":
                if (manifest_path.parent / "deployment_result.json").exists():
                    raise ContractError(
                        "deployment_result.json already exists; this sealed release "
                        "will not be switched again"
                    )
                evidence = verify_pre_deploy(manifest, repository, runtime)
            elif args.phase in {"post-deploy", "record-deployment"}:
                evidence = verify_post_deploy(manifest, runtime)
                if args.phase == "record-deployment":
                    if args.http_status != 200:
                        raise ContractError(
                            "deployment result may be recorded only after HTTP 200"
                        )
                    evidence = {
                        **evidence,
                        "phase": "deployment-result",
                        "release_id": manifest["release_id"],
                        "http_status": args.http_status,
                        "status": "deployed-and-verified",
                    }
                    result_path = (
                        args.result_path.resolve()
                        if args.result_path
                        else manifest_path.parent / "deployment_result.json"
                    )
                    write_result(result_path, evidence)
                    evidence["result_path"] = str(result_path)
            elif args.phase == "pre-rollback":
                evidence = verify_pre_rollback(manifest, repository, runtime)
            else:
                evidence = verify_post_rollback(manifest, runtime)
    except ContractError as exc:
        print(f"release contract rejected: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
