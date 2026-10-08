"""Validate all visited contract rankings and publish a local disclosed summary."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))
from agri_research_agent.oilseed_positions.aggregation import parse_browser_capture
from agri_research_agent.oilseed_positions.model import config, preview_root
from agri_research_agent.sugar_positions.storage import publish


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", choices=("soybean", "palm"), required=True)
    parser.add_argument("--captures", type=Path, nargs="+", required=True)
    args = parser.parse_args(argv)
    if os.getenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", "").strip():
        parser.error("浏览器采集导入仅允许独立本地预览，不接受正式runtime配置")
    root = preview_root(ROOT, args.domain)
    domestic, captures, attempts = [], [], []
    for path in args.captures:
        raw = path.read_bytes()
        rows, excluded = parse_browser_capture(raw, tuple(config(ROOT)[args.domain]["domestic"]))
        domestic.extend(rows)
        day = json.loads(raw.decode("utf-8"))["report_date"]
        captures.append((f"browser_contracts_{args.domain}_{day.replace('-', '')}", raw,
            "https://qhweb.eastmoney.com/lhb/dkcc/dce", "json"))
        attempts.append(dict(source_id=captures[-1][0], status="ok", rows=len(rows),
            acquisition="browser_dom", excluded_contracts=excluded))
    payload = publish(root, [], domestic, captures, attempts)
    from agri_research_agent.shared.atomic_storage import atomic_write_json
    atomic_write_json(root / "last_attempt.json", dict(
        attempted_at=datetime.now(timezone.utc).isoformat(), attempts=attempts))
    print(json.dumps(dict(root=str(root), release_id=payload["release_id"],
        imported_rows=len(domestic), acquisition="browser_dom", automatic_collection=False), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
