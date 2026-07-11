from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "03_src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agri_research_agent.data_sources.basis_excel_loader import (  # noqa: E402
    import_basis_excel,
)


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def main() -> int:
    try:
        result = import_basis_excel()
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] 国内现货基差 Excel 导入失败：{exc}", file=sys.stderr)
        return 1

    for summary in result["sheet_summaries"]:
        indicator_text = (
            f"，价差指标数量={summary['spread_indicator_count']}"
            if "spread_indicator_count" in summary
            else ""
        )
        print(
            f"[INFO] {summary['source_sheet']}："
            f"行数={summary['rows']}，"
            f"最早日期={summary['earliest_date'] or '-'}，"
            f"最新日期={summary['latest_date'] or '-'}"
            f"{indicator_text}"
        )

    print("[OK] 国内现货基差 Excel 导入完成")
    print(f"原始文件：{_display_path(result['source_file'])}")
    print(f"标准基差表：{_display_path(result['basis_long'])}")
    print(f"标准一口价表：{_display_path(result['cash_price_long'])}")
    print(f"内盘期货收盘价：{_display_path(result['futures_close'])}")
    print(f"标准价差表：{_display_path(result['spot_spread'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
