from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "03_src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agri_research_agent.data_sources.basis_database import (  # noqa: E402
    build_basis_database,
)


def _display_path(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()


def main() -> int:
    try:
        result = build_basis_database()
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] 正式基差数据库导入失败：{exc}", file=sys.stderr)
        return 1

    for summary in result["sheet_summaries"]:
        print(
            f"[INFO] {summary['source_sheet']}："
            f"源数据={summary['source_rows']}，"
            f"有效输出={summary['output_rows']}，"
            f"缺失剔除={summary['dropped_missing_rows']}，"
            f"重复剔除={summary['duplicate_rows']}，"
            f"自动计算基差={summary['calculated_basis_rows']}"
        )
    print("[OK] 正式基差数据库已生成")
    print(f"原始 Excel：{_display_path(result['source_file'])}")
    print(f"正式数据库：{_display_path(result['output_file'])}")
    print(
        f"总行数={result['rows']}，基差行数={result['basis_rows']}，"
        f"一口价行数={result['cash_price_rows']}，"
        f"日期范围={result['earliest_date']:%Y-%m-%d}"
        f" 至 {result['latest_date']:%Y-%m-%d}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
