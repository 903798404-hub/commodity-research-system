#!/usr/bin/env python
"""Validate and activate one already-uploaded public-data package."""

from __future__ import annotations

import argparse
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
    build_consumer_prewarm_targets,
    validate_activated_public_currents,
)
from agri_research_agent.pipelines.public_data_delivery import (  # noqa: E402
    PrewarmStatus,
    run_prewarm,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate incoming package and atomically switch server Current"
    )
    parser.add_argument("--incoming-package", type=Path, required=True)
    parser.add_argument("--store-root", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    def post_switch_validate(data_root: Path) -> None:
        validate_activated_public_currents(data_root)
        consumer_reads = run_prewarm(
            build_consumer_prewarm_targets(project_root=ROOT, runtime_root=data_root)
        )
        if consumer_reads.status is not PrewarmStatus.PASS:
            raise RuntimeError("formal consumer read validation failed")

    result = activate_incoming_server_package(
        args.incoming_package,
        store_root=args.store_root,
        pre_switch_validator=validate_activated_public_currents,
        post_switch_validator=post_switch_validate,
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
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if result.status in {"SYNCED", "NO_CHANGE"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
