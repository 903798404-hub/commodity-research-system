from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from release_contract import (
    ContractError,
    DockerReleaseRuntime,
    capture_formal_container_snapshot,
    formal_evidence_sha256,
    write_formal_container_snapshot,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Capture full USDA and Oil World formal container identities without "
            "recording container environment values."
        )
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        snapshot = capture_formal_container_snapshot(DockerReleaseRuntime())
        target = write_formal_container_snapshot(args.output, snapshot)
    except ContractError as exc:
        print(f"formal container snapshot rejected: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "snapshot": str(target),
                "formal_evidence_sha256": formal_evidence_sha256(snapshot),
                "services": [item["service"] for item in snapshot["containers"]],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
