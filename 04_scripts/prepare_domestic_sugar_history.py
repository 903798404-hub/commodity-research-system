"""One-off local historical candidate preparation; no refresh or publication."""
from __future__ import annotations

import argparse
import json
import sys
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "03_src"))
from agri_research_agent.pipelines.domestic_sugar_history import prepare_sugar_history


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-directory", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    receipt_path = args.receipt or ROOT / "06_outputs" / "audits" / "domestic_sugar_history" / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = dict(status="STARTED", started_at=dt.datetime.now(dt.timezone.utc).isoformat())
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    exit_code = 1
    try:
        for path in (args.source_directory, args.candidate_root):
            if "\x00" in str(path) or str(path).encode("utf-8").decode("utf-8") != str(path):
                raise ValueError("输入路径编码无效")
        if not args.source_directory.is_dir():
            raise ValueError("历史来源目录不存在")
        permitted = (ROOT / "01_data" / "candidates" / "domestic_sugar_history").resolve()
        if not args.candidate_root.resolve().is_relative_to(permitted):
            raise ValueError("此入口仅准备本项目独立白糖候选，不写正式数据目录")
        receipt.update(prepare_sugar_history(args.source_directory, args.candidate_root))
        exit_code = 0
    except Exception as exc:
        receipt.update(status="FAILED", error_type=type(exc).__name__, error=str(exc)[:300])
    finally:
        receipt.update(exit_code=exit_code, finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(receipt, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
