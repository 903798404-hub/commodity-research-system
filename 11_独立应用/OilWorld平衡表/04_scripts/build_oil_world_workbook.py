from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "03_src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from oil_world_data.workbook_builder import WorkbookBuildError, build_workbook  # noqa: E402


def configure_logging(output_dir: Path) -> logging.Logger:
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("oil_world_workbook_builder")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(output_dir / "build.log", mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(stream)
    logger.addHandler(file_handler)
    return logger


def main() -> int:
    parser = argparse.ArgumentParser(description="从Oil World季度HTML资料安全生成候选Excel工作簿")
    parser.add_argument("--release", required=True, help="发布期，格式YYYY-MM")
    parser.add_argument("--source-dir", required=True, type=Path, help="该发布期原始HTML资料目录")
    parser.add_argument("--output-dir", required=True, type=Path, help="候选工作簿输出目录")
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "02_configs" / "workbook_rebuild.json",
        help="统一生成器配置",
    )
    arguments = parser.parse_args()
    output_dir = arguments.output_dir.resolve()
    logger = configure_logging(output_dir)
    try:
        manifest = build_workbook(
            release=arguments.release,
            source_dir=arguments.source_dir,
            output_dir=output_dir,
            config_path=arguments.config,
            cli_path=Path(__file__),
            logger=logger,
        )
    except WorkbookBuildError as error:
        logger.error("构建停止：%s", error)
        return 1
    except Exception:
        logger.exception("构建出现未处理异常")
        return 1
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
