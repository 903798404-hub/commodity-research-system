"""Fetch and validate USDA NASS weekly soybean PLANTED data."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))

from agri_research_agent.pipelines.soybean_crop_progress import (  # noqa: E402
    MISSING_API_KEY_MESSAGE,
    MissingNassApiKeyError,
    run_soybean_planted_pipeline,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch 2021-2026 USDA NASS soybean PLANTED weekly NATIONAL and "
            "STATE records, then build Parquet and audit reports."
        )
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args()

    try:
        audit = run_soybean_planted_pipeline(
            project_root=ROOT,
            timeout=args.timeout,
            retries=args.retries,
        )
    except MissingNassApiKeyError:
        print(MISSING_API_KEY_MESSAGE)
        return 2
    except Exception as exc:
        print(f"USDA NASS 美豆 PLANTED 数据链路失败：{type(exc).__name__}: {exc}")
        return 1

    print("USDA NASS 美豆 PLANTED 数据链路验证完成。")
    print(f"Raw 记录数：{audit['raw_total_records']}")
    print(f"Processed 记录数：{audit['processed_total_records']}")
    print(f"Raw：{audit['files']['raw_snapshot']['path']}")
    print(f"Processed：{audit['files']['processed']['path']}")
    print(
        "审计报告："
        "06_outputs/audits/soybean_crop_progress/"
        "soybeans_planted_api_validation.json 和 .md"
    )
    print(f"可进入下一阶段：{audit['ready_for_next_stage']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
