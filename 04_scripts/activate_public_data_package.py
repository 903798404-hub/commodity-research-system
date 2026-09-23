#!/usr/bin/env python
"""Validate and activate one already-uploaded public-data package."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "03_src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agri_research_agent.pipelines.public_data_delivery import (  # noqa: E402
    activate_incoming_server_package,
)
from agri_research_agent.pipelines.public_data_prewarm import (  # noqa: E402
    validate_activated_public_currents,
    validate_formal_consumer_reads,
)
from agri_research_agent.pipelines.public_data_delivery import (  # noqa: E402
    validate_production_package,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate incoming package and atomically switch server Current"
    )
    parser.add_argument("--incoming-package", type=Path, required=True)
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument("--initial-seed", action="store_true")
    parser.add_argument("--expected-current-id")
    parser.add_argument("--expected-current-artifact-sha256")
    parser.add_argument("--expected-current-manifest-sha256")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate a sealed staging package without writing server-store state",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    expected_values = (args.expected_current_id, args.expected_current_artifact_sha256,
                       args.expected_current_manifest_sha256)
    if any(value is not None for value in expected_values) and (
        not all(value is not None for value in expected_values)
        or args.initial_seed or args.validate_only
    ):
        raise ValueError("complete expected Current identity is required for activation")
    expected_current = (
        dict(zip(("id", "artifact_sha256", "manifest_sha256"), expected_values))
        if all(value is not None for value in expected_values) else None
    )

    if args.validate_only:
        package = validate_production_package(
            args.incoming_package, require_directory_name=False
        )
        data_root = package.directory / "data"
        consumer_reads = validate_formal_consumer_reads(
            project_root=ROOT,
            runtime_root=data_root,
        )
        print(json.dumps({
            "schema_version": "public-data-remote-validation/1",
            "status": "VALIDATED",
            "package_id": package.package_id,
            "manifest": "PASS",
            "sha": "PASS",
            "formal_read_validation": "PASS",
            "consumer_reads": dict(consumer_reads.targets),
            "activation_capabilities": ["public-current-server-cas/1"],
            "manifest_sha256": hashlib.sha256((package.directory / "manifest.json").read_bytes()).hexdigest(),
            "domestic_spread_artifact_sha256": package.manifest["delivery_artifacts"].get(
                "domestic-spread", {}).get("sha256"),
        }, ensure_ascii=False, sort_keys=True))
        return 0

    def post_switch_validate(data_root: Path) -> None:
        validate_formal_consumer_reads(project_root=ROOT, runtime_root=data_root)

    result = activate_incoming_server_package(
        args.incoming_package,
        store_root=args.store_root,
        pre_switch_validator=validate_activated_public_currents,
        post_switch_validator=post_switch_validate,
        initial_seed=args.initial_seed,
        expected_current=expected_current,
    )
    payload = {
        "schema_version": "public-data-remote-activation/1",
        "status": result.status,
        "package_id": result.package_id,
        "manifest": result.manifest,
        "sha": result.sha,
        "atomic_switch": result.atomic_switch,
        "formal_read_validation": result.formal_read_validation,
        "safe_reason": result.safe_reason,
        "cas": result.cas,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if result.status in {"SYNCED", "NO_CHANGE"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
