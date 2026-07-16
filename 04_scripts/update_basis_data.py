from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "03_src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agri_research_agent.pipelines.update_basis_data import (  # noqa: E402
    apply_basis_update,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验用户上传的国内现货基差 Excel，并原子更新正式数据。"
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="待更新 Excel；省略时使用配置中的固定上传路径。",
    )
    args = parser.parse_args()
    try:
        result = apply_basis_update(args.input)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] 基差更新被拒绝：{exc}", file=sys.stderr)
        return 1

    print("[OK] 基差 Excel、Parquet 和更新状态已完成一致性切换")
    print(f"当前总行数：{result['candidate_rows']}")
    print(f"本次新增：{result['added_rows']}")
    print(f"最新业务日期：{result['latest_date']:%Y-%m-%d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
