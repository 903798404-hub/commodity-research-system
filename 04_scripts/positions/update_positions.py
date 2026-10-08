"""Explicit local collection/export entry; does not install a schedule or deploy."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))

from agri_research_agent.positions.bundle import export_bundle
from agri_research_agent.positions.workspace import DOMAINS, collection_plan, local_root


def main(argv=None):
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domains", nargs="+", choices=tuple(DOMAINS), default=list(DOMAINS))
    parser.add_argument("--markets", choices=("all", "domestic", "foreign"), default="all")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--start-year", type=int, default=today.year - 1)
    parser.add_argument("--recheck-domestic", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="仅显示执行计划，不采集或写入")
    mode.add_argument("--export-only", action="store_true", help="校验现有快照并导出数据包，不联网")
    args = parser.parse_args(argv)
    if os.getenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", "").strip():
        parser.error("采集和导出仅适用于独立本地 worktree，不接受正式 runtime 配置")
    try:
        for domain in args.domains:
            local_root(ROOT, domain)
        plan = collection_plan(ROOT, args.domains, args.markets, today, args.days,
                               args.start_year, recheck=args.recheck_domestic)
    except ValueError as exc:
        parser.error(str(exc))
    if args.plan:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if args.export_only:
        print(json.dumps({"bundle": str(export_bundle(ROOT, args.domains))}, ensure_ascii=False))
        return 0
    output = ROOT / "06_outputs/commodity_positions"
    if output.resolve() != output:
        parser.error("采集结果目录不能指向其他位置")
    output.mkdir(parents=True, exist_ok=True)
    lock = output / "collection.lock"
    receipts = []
    # Reject concurrent orchestrators and abandoned locks; never steal a lock.
    with lock.open("x", encoding="utf-8") as file:
        file.write(datetime.now(ZoneInfo("Asia/Shanghai")).isoformat())
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        for item in plan:
            result = subprocess.run(item["argv"], cwd=ROOT, env=env, check=False)
            receipts.append({"domain": item["domain"], "exit_code": result.returncode})
            if result.returncode not in {0, 2}:
                break
    finally:
        (output / "last_update.json").write_text(json.dumps({
            "attempted_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
            "markets": args.markets, "results": receipts}, ensure_ascii=False, indent=2), encoding="utf-8")
        lock.unlink()
    return 0 if len(receipts) == len(plan) and all(r["exit_code"] == 0 for r in receipts) else 2


if __name__ == "__main__":
    raise SystemExit(main())
