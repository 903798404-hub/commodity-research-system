from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "03_src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from oil_world_data.generator import BuildError  # noqa: E402
from oil_world_data.release_pipeline import ReleasePipelineError, update_release  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="发布或回补 Oil World 季度数据快照")
    parser.add_argument("--release", required=True, help="发布期，格式 YYYY-MM")
    parser.add_argument("--backfill", action="store_true", help="导入早于当前 latest 的历史发布期")
    parser.add_argument("--validate-only", action="store_true", help="只验证和预览，不写正式目录或指针")
    arguments = parser.parse_args()
    try:
        report = update_release(
            PROJECT_ROOT,
            arguments.release,
            backfill=arguments.backfill,
            validate_only=arguments.validate_only,
        )
    except (BuildError, ReleasePipelineError) as error:
        print(f"Oil World 更新失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
