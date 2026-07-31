"""Bootstrap an immutable import-profit runtime Release."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "03_src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agri_research_agent.import_profit.config import load_soybean_config  # noqa: E402
from agri_research_agent.import_profit.runtime_store import (  # noqa: E402
    parse_utc_text,
)
from agri_research_agent.pipelines.import_profit_runtime import (  # noqa: E402
    bootstrap_import_profit_runtime,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bootstrap an immutable soybean import-profit runtime."
    )
    parser.add_argument("--historical-candidate-dir", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--generated-at", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config_path = Path(args.config)
        result = bootstrap_import_profit_runtime(
            Path(args.historical_candidate_dir),
            Path(args.runtime_root),
            config=load_soybean_config(config_path),
            config_path=config_path,
            release_id=args.release_id,
            generated_at=parse_utc_text(
                args.generated_at, "generated_at"
            ),
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": getattr(exc, "status", "failed"),
                    "error": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": result.status,
                "release_id": result.promotion.release_id,
                "generation": result.promotion.generation,
                "record_count": result.record_count,
                "success_count": result.success_count,
                "incomplete_count": result.incomplete_count,
                "index_sha256": result.promotion.index_sha256,
                "initialization_seconds": (
                    result.initialization_seconds
                ),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
