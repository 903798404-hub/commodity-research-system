from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from release_contract import (
    ContractError,
    DockerReleaseRuntime,
    artifact_manifest_path,
    create_manifest,
    hash_file,
    parse_production_env,
    write_release_bundle,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPOSITORY = SCRIPT_DIR.parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Seal one already-built and already-validated spread-dashboard image. "
            "This command never builds, tags, starts, deploys, or restarts an image."
        )
    )
    parser.add_argument("--repository", type=Path, default=DEFAULT_REPOSITORY)
    parser.add_argument(
        "--data-host-root",
        type=Path,
        required=True,
        help="Host root whose existing 01_data bind mount is used by the candidate.",
    )
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--expected-image-id", required=True)
    parser.add_argument("--build-time", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--candidate-container-name", required=True)
    parser.add_argument("--rollback-image-ref", required=True)
    parser.add_argument("--rollback-image-id", required=True)
    parser.add_argument(
        "--formal-git-commit",
        required=True,
        help="The full 40-character Git commit currently deployed before this release.",
    )
    parser.add_argument(
        "--production-env-file",
        type=Path,
        required=True,
        help="Validated production environment supplying browser-facing dashboard URLs.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help="Defaults to <repository>/09_deploy/releases.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository = args.repository.resolve()
    output_root = (
        args.output_root.resolve()
        if args.output_root
        else repository / "09_deploy/releases"
    )
    try:
        manifest = create_manifest(
            repository=repository,
            data_host_root=args.data_host_root,
            release_id=args.release_id,
            git_commit=args.git_commit,
            image_ref=args.image_ref,
            expected_image_id=args.expected_image_id,
            build_time=args.build_time,
            source=args.source,
            candidate_container_name=args.candidate_container_name,
            rollback_image_ref=args.rollback_image_ref,
            rollback_image_id=args.rollback_image_id,
            formal_git_commit=args.formal_git_commit,
            production_environment=parse_production_env(
                args.production_env_file.resolve()
            ),
            runtime=DockerReleaseRuntime(),
        )
        release_directory = write_release_bundle(manifest, output_root)
        release_manifest = artifact_manifest_path(
            release_directory / "release.json",
            "release",
        )
    except ContractError as exc:
        print(f"release manifest rejected: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "status": "candidate_sealed",
                "release_directory": str(release_directory),
                "release_id": manifest["release_id"],
                "git_tree": manifest["git_tree"],
                "image_ref": manifest["image_ref"],
                "image_id": manifest["image_id"],
                "release_manifest": str(release_manifest),
                "release_manifest_sha256": hash_file(release_manifest),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
