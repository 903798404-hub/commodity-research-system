"""Prepare a separate regional sugar candidate; no network or production writes."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "03_src"))
from agri_research_agent.pipelines.domestic_sugar_regional_history import prepare_regional_sugar_history


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spot-workbook", type=Path, required=True)
    parser.add_argument("--spot-sha256", required=True)
    parser.add_argument("--main-history-root", type=Path, required=True)
    parser.add_argument("--main-extension", type=Path)
    parser.add_argument("--extension-sha256")
    parser.add_argument("--candidate-root", type=Path, required=True)
    args = parser.parse_args()
    receipt = dict(status="STARTED", started_at=dt.datetime.now(dt.timezone.utc).isoformat())
    folder = ROOT / "06_outputs" / "audits" / "domestic_sugar_regional_history"
    folder.mkdir(parents=True, exist_ok=True)
    receipt_path = folder / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    exit_code = 1
    try:
        permitted = (ROOT / "01_data" / "candidates" / "domestic_sugar_regional_history").resolve()
        if not args.candidate_root.resolve().is_relative_to(permitted):
            raise ValueError("此入口仅准备本项目独立三地白糖候选，不写正式数据目录")
        if bool(args.main_extension) != bool(args.extension_sha256):
            raise ValueError("补充主力行情必须同时提供文件及SHA256")
        receipt.update(prepare_regional_sugar_history(args.spot_workbook, args.spot_sha256,
            args.main_history_root, args.candidate_root, main_extension=args.main_extension,
            extension_sha256=args.extension_sha256))
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
