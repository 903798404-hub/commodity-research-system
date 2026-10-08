"""Prepare CGC canola exports and activate isolated local data, without deployment."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))

from agri_research_agent.canola_exports.data import MODULE_ID, source_url
from agri_research_agent.canola_exports.update import activate_local, discover_years, missing_reports, now, official_bytes, prepare, record_failure
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=ROOT / "01_data")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-dev")
    importer = commands.add_parser("prepare")
    importer.add_argument("--years", type=int, default=2)
    importer.add_argument("--crop-year")
    importer.add_argument("--source-file", type=Path)
    local = commands.add_parser("activate-local")
    local.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare" and (not 1 <= args.years <= 20 or args.source_file and not args.crop_year):
        parser.error("years must be 1..20; source-file requires crop-year")
    context = None
    try:
        if args.command == "init-dev":
            root = args.runtime_root.resolve()
            if root != (ROOT / "01_data").resolve() or not (ROOT / ".git").is_file():
                raise ValueError("init-dev only initializes this feature worktree's 01_data")
            root.mkdir(exist_ok=True)
            with (root / ".market-data-runtime.json").open("x", encoding="utf-8") as handle:
                json.dump({"schema_version": 1, "runtime_id": MODULE_ID + "-dev", "classification": "isolated-dev",
                           "module_id": MODULE_ID, "created_at": now()}, handle)
            path = root
        else:
            context = RuntimeContext(RuntimeMode.ISOLATED_DEV, MODULE_ID, args.runtime_root)
            if args.command == "prepare":
                years = [args.crop_year] if args.crop_year else discover_years()[:args.years]
                if args.source_file:
                    downloads = {args.crop_year: args.source_file.resolve(strict=True).read_bytes()}
                else:
                    downloads = {year: official_bytes(source_url(year)) for year in years}
                reports = {} if args.source_file else missing_reports(downloads)
                path = prepare(context, downloads, reports)
            else:
                path = activate_local(context, args.candidate)
        print(json.dumps({"status": "PASS", "path": str(path)}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        result = {"status": "FAIL", "reason": str(exc)}
        if context is not None:
            try:
                record_failure(context, str(exc))
            except (OSError, ValueError, RuntimeError) as status_error:
                result["status_write_error"] = str(status_error)
        print(json.dumps(result, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
