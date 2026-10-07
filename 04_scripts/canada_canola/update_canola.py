"""Manually import/fetch/prepare Canadian canola data; never build images or deploy."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))

from agri_research_agent.pipelines.canada_canola import load_bundle, sha256_file, utc_now
from agri_research_agent.pipelines.canada_canola_update import (
    activate_local, candidate_from_workbook, fetch_report, latest_dates, prepare_update,
)
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=ROOT / "01_data")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-dev")
    importer = sub.add_parser("import-excel")
    importer.add_argument("--workbook", type=Path, required=True)
    fetch = sub.add_parser("fetch-report")
    fetch.add_argument("--province", choices=("SK", "AB", "MB"), required=True)
    fetch.add_argument("--url", required=True)
    update = sub.add_parser("prepare-update")
    update.add_argument("--baseline", type=Path, required=True)
    update.add_argument("--observations", type=Path, required=True)
    update.add_argument("--allow-revisions", action="store_true")
    local = sub.add_parser("activate-local")
    local.add_argument("--candidate", type=Path, required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            data = load_bundle(args.candidate)
            result = {"status": "PASS", "sha256": sha256_file(args.candidate),
                      "rows": len(data["records"]), "latest_dates": latest_dates(data)}
        else:
            if args.command == "init-dev":
                # Only this feature checkout's ignored data directory may be initialized.
                target = args.runtime_root.resolve()
                if target != (ROOT / "01_data").resolve() or not (ROOT / ".git").is_file():
                    raise ValueError("init-dev requires this linked feature worktree's 01_data")
                target.mkdir(exist_ok=True)
                marker = target / ".market-data-runtime.json"
                with marker.open("x", encoding="utf-8") as handle:
                    json.dump({"schema_version": 1, "runtime_id": "canada-canola-dev",
                               "classification": "isolated-dev", "module_id": "canada-canola",
                               "created_at": utc_now()}, handle, ensure_ascii=False)
                result = {"status": "PASS", "runtime_root": str(target)}
            else:
                context = RuntimeContext(RuntimeMode.ISOLATED_DEV, "canada-canola", args.runtime_root)
                if args.command == "import-excel":
                    path = candidate_from_workbook(context, args.workbook)
                elif args.command == "fetch-report":
                    path = fetch_report(context, args.province, args.url)
                elif args.command == "prepare-update":
                    path = prepare_update(context, args.baseline, args.observations,
                                          allow_revisions=args.allow_revisions)
                else:
                    path = activate_local(context, args.candidate)
                result = {"status": "PASS", "path": str(path), "sha256": sha256_file(path)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
