"""Manually prepare Brazil soybean observations. Production publication is separate."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))

from agri_research_agent.pipelines.brazil_soy import load_bundle, sha256_file, utc_now
from agri_research_agent.pipelines.brazil_soy_update import (
    activate_local, candidate_from_workbook, fetch_report, latest_dates, parse_progress, prepare_update,
)
from agri_research_agent.shared.atomic_storage import atomic_write_json
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=ROOT / "01_data/brazil-soy-dev")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-dev")
    importer = sub.add_parser("import-excel")
    importer.add_argument("--workbook", type=Path, required=True)
    fetch = sub.add_parser("fetch-report")
    fetch.add_argument("--url", required=True)
    fetch.add_argument("--published-at", required=True)
    parse = sub.add_parser("parse-progress")
    parse.add_argument("--source", type=Path, required=True)
    update = sub.add_parser("prepare-update")
    update.add_argument("--baseline", type=Path, required=True)
    update.add_argument("--observations", type=Path, required=True)
    update.add_argument("--allow-revisions", action="store_true")
    for name in ("validate", "activate-local"):
        command = sub.add_parser(name)
        command.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init-dev":
            target = args.runtime_root.resolve()
            if target != (ROOT / "01_data/brazil-soy-dev").resolve() or not (ROOT / ".git").is_file():
                raise ValueError("init-dev requires this linked feature worktree's Brazil development root")
            target.mkdir(parents=True, exist_ok=True)
            with (target / ".market-data-runtime.json").open("x", encoding="utf-8") as handle:
                json.dump({"schema_version": 1, "runtime_id": "brazil-soy-dev", "classification": "isolated-dev",
                           "module_id": "brazil-soy", "created_at": utc_now()}, handle, ensure_ascii=False)
            result = {"status": "PASS", "runtime_root": str(target)}
        elif args.command == "validate":
            bundle = load_bundle(args.candidate)
            result = {"status": "PASS", "sha256": sha256_file(args.candidate), "rows": len(bundle["records"]),
                      "latest_dates": latest_dates(bundle)}
        else:
            context = RuntimeContext(RuntimeMode.ISOLATED_DEV, "brazil-soy", args.runtime_root)
            if args.command == "import-excel":
                path = candidate_from_workbook(context, args.workbook)
            elif args.command == "fetch-report":
                path = fetch_report(context, args.url, args.published_at)
            elif args.command == "parse-progress":
                source_root = (context.runtime_root / "raw/brazil_soy").resolve()
                if source_root not in args.source.resolve().parents:
                    raise ValueError("source must be in this local report archive")
                from agri_research_agent.pipelines.brazil_soy_update import local_write
                path = args.source.parent / "observations.json"
                if path.exists():
                    raise ValueError("parsed observations already exist")
                atomic_write_json(local_write(context, path), parse_progress(args.source))
            elif args.command == "prepare-update":
                path = prepare_update(context, args.baseline, args.observations, allow_revisions=args.allow_revisions)
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
