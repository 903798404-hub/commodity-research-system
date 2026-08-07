from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from release_contract import (
    ContractError,
    CommandRunner,
    DockerReleaseRuntime,
    IMPORT_PROFIT_RUNTIME_CONTAINER_PATH,
    IMPORT_PROFIT_RUNTIME_ENV_KEY,
    artifact_manifest_path,
    completed_candidate_gate,
    create_candidate_result,
    hash_file,
    load_manifest_bundle,
    load_candidate_result,
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
    parser.add_argument(
        "--prior-waiting-candidate-result",
        type=Path,
        help="Verified Stage A result whose runtime evidence is carried into a versioned Stage B result.",
    )
    parser.add_argument(
        "--completed-gate-evidence",
        type=Path,
        help="Controlled real_night_session_close_snapshot completion evidence for Stage B.",
    )
    return parser


def load_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load {description} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"{description} must be a JSON object")
    return payload


def measure_import_profit_runtime_access(container_name: str) -> dict[str, Any]:
    runner = CommandRunner()
    uid = runner.run(["docker", "exec", container_name, "id", "-u"]).strip()
    gid = runner.run(["docker", "exec", container_name, "id", "-g"]).strip()
    if not uid.isdigit() or not gid.isdigit():
        raise ContractError("candidate container UID/GID probe returned invalid output")
    probe = f"{IMPORT_PROFIT_RUNTIME_CONTAINER_PATH}/.candidate-stage-b-write-probe"
    runner.run(
        [
            "docker",
            "exec",
            container_name,
            "sh",
            "-c",
            'umask 077; : > "$1"; test -f "$1"; rm -f "$1"',
            "candidate-runtime-probe",
            probe,
        ]
    )
    return {
        "container_path": IMPORT_PROFIT_RUNTIME_CONTAINER_PATH,
        "environment_variable": IMPORT_PROFIT_RUNTIME_ENV_KEY,
        "uid": int(uid),
        "gid": int(gid),
        "read_write_probe": "passed",
    }


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
        if bool(args.prior_waiting_candidate_result) != bool(args.completed_gate_evidence):
            raise ContractError(
                "Stage B requires both prior waiting candidate result and completed gate evidence"
            )
        runtime_mounts: list[dict[str, Any]] = []
        candidate_runtime_access: dict[str, Any] | None = None
        completed_gates: list[dict[str, Any]] = []
        if args.prior_waiting_candidate_result:
            prior = load_candidate_result(
                args.prior_waiting_candidate_result.resolve(), manifest
            )
            if prior.get("status") != "candidate-waiting-gate":
                raise ContractError("Stage B prior candidate result is not waiting for a gate")
            runtime_mounts = list(prior.get("runtime_mounts") or [])
            candidate_runtime_access = measure_import_profit_runtime_access(
                str(manifest["candidate_container_name"])
            )
            completed_gates = [
                completed_candidate_gate(
                    load_json_object(
                        args.completed_gate_evidence.resolve(),
                        "completed candidate gate evidence",
                    )
                )
            ]
        result = create_candidate_result(
            manifest=manifest,
            runtime=DockerReleaseRuntime(),
            readiness=load_json_object(
                args.readiness_result.resolve(),
                "candidate readiness result",
            ),
            checks=checks,
            completed_gates=completed_gates,
            runtime_mounts=runtime_mounts,
            candidate_runtime_access=candidate_runtime_access,
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
