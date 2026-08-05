from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "03_src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agri_research_agent.pipelines.update_basis_from_sql import (  # noqa: E402
    apply_basis_sql_update,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="流式读取 basis_price，校验候选并原子更新正式基差数据。"
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="已停用；正式源固定为 basis_sql.yaml 中的 SQL。",
    )
    args = parser.parse_args()
    try:
        if args.input is not None:
            raise ValueError("正式基差源已固定为 basis_price SQL，不接受 Excel 输入")
        result = apply_basis_sql_update()
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] 基差更新被拒绝：{exc}", file=sys.stderr)
        return 1

    print("[OK] basis_price SQL 候选已验证并原子切换正式 Parquet")
    print(f"当前总行数：{result['rows']}")
    print(f"排除远月合约记录：{result['far_contract_excluded_rows']}")
    print(f"最新业务日期：{result['latest_date']:%Y-%m-%d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
