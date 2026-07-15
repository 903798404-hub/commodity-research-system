from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "03_src"))

from oil_world_data import BuildError, build_release  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="构建Oil World 2026-06审计驱动发布数据")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="仅用于第一版本地开发重建；正常季度发布不得覆盖既有目录。",
    )
    args = parser.parse_args()
    try:
        result = build_release(PROJECT_ROOT, replace=args.replace)
    except BuildError as exc:
        print(f"构建停止：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
