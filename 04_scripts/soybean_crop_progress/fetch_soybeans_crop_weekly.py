"""Fetch all official USDA soybean crop progress and condition metrics."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))

from agri_research_agent.pipelines.soybean_crop_progress import (  # noqa: E402
    MISSING_API_KEY_MESSAGE,
    MissingNassApiKeyError,
    run_soybean_crop_weekly_pipeline,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch 2021-2026 USDA NASS soybean weekly PROGRESS and CONDITION "
            "data, derive GOOD_EXCELLENT, and publish audited Parquet files."
        )
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args()
    try:
        audit = run_soybean_crop_weekly_pipeline(
            project_root=ROOT,
            timeout=args.timeout,
            retries=args.retries,
        )
    except MissingNassApiKeyError:
        print(MISSING_API_KEY_MESSAGE)
        return 2
    except Exception as exc:
        print(
            "USDA NASS 美豆生长阶段与作物状况链路失败："
            f"{type(exc).__name__}: {exc}"
        )
        return 1

    print("USDA NASS 美豆生长阶段与作物状况链路验证完成。")
    print(f"PROGRESS Raw：{audit['raw_record_counts']['PROGRESS']}")
    print(f"CONDITION Raw：{audit['raw_record_counts']['CONDITION']}")
    print(
        "PROGRESS 标准记录："
        f"{audit['standardized_record_counts']['PROGRESS']}"
    )
    print(
        "CONDITION（含派生）标准记录："
        f"{audit['standardized_record_counts']['CONDITION_WITH_DERIVED']}"
    )
    print(f"GOOD_EXCELLENT 派生：{audit['good_excellent']['derived_count']}")
    print(f"Raw：{audit['files']['raw_snapshot']['path']}")
    print(f"Progress：{audit['files']['progress']['path']}")
    print(f"Condition：{audit['files']['condition']['path']}")
    print(
        "审计：06_outputs/audits/soybean_crop_progress/"
        "soybeans_crop_weekly_validation.json 和 .md"
    )
    print(f"PLANTED 新旧一致：{audit['planted_compatibility']['business_fields_exact_match']}")
    print(f"可进入下一阶段：{audit['ready_for_next_stage']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
