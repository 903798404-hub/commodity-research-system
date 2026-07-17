"""Run the safe current-year USDA soybean weekly update."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))

from agri_research_agent.pipelines.soybean_crop_weekly_update import (  # noqa: E402
    SoybeanWeeklyUpdateError,
    run_soybean_crop_weekly_update,
)


def _value(value: object) -> str:
    return "—" if value is None else str(value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch the current USDA soybean reporting year with four official "
            "queries, validate a complete candidate pair, and publish safely."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create Raw, Manifest, candidates, and temporary audits only.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even outside April 1 through November 30 in America/New_York.",
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args()

    try:
        audit = run_soybean_crop_weekly_update(
            project_root=ROOT,
            dry_run=args.dry_run,
            force=args.force,
            timeout=args.timeout,
            retries=args.retries,
        )
    except SoybeanWeeklyUpdateError as exc:
        print(f"美豆周度更新失败：{exc}")
        return 1
    except Exception as exc:
        print(f"美豆周度更新失败：{type(exc).__name__}: {exc}")
        return 1

    print(f"状态：{audit['status']}")
    print(f"运行模式：{audit['run_mode']}")
    print(f"当前年度：{audit['current_year']}")
    if audit.get("request_record_counts"):
        print("四组请求记录数：")
        for name, count in audit["request_record_counts"].items():
            print(f"  {name}: {count}")
    print(f"官方最新周：{_value(audit.get('new_latest_week'))}")
    print(
        "业务变化："
        f"新增 {audit.get('added_records', 0)}，"
        f"修正 {audit.get('corrected_records', 0)}，"
        f"删除 {audit.get('deleted_records', 0)}"
    )
    print(
        "候选记录："
        f"Progress {audit.get('progress_new_rows')}，"
        f"Condition {audit.get('condition_new_rows')}"
    )
    if audit.get("candidate_validation"):
        print(
            "质量检查："
            f"{'通过' if audit['candidate_validation']['passed'] else '失败'}"
        )
    print(f"建议发布：{audit.get('recommended_to_publish', False)}")
    print(f"Raw：{_value(audit.get('raw_path'))}")
    print(f"Manifest：{_value(audit.get('manifest_path'))}")
    print(f"Candidate Progress：{_value(audit.get('candidate_progress_path'))}")
    print(f"Candidate Condition：{_value(audit.get('candidate_condition_path'))}")
    print(f"Candidate SHA-256：{audit.get('candidate_sha256', {})}")
    print(f"审计 JSON：{audit.get('audit_json_path')}")
    print(f"审计 Markdown：{audit.get('audit_markdown_path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
