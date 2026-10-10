"""Run one pinned first-batch job with approved Python -I -B."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


def bootstrap(config):
    spec = importlib.util.spec_from_file_location("pinned_delta_entry", ROOT / "04_scripts/automation/run_production_data_delta_windows.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    entry._bootstrap(config["delivery"])
    module = entry.load_module()
    module.verify_clean_detached_clone(ROOT, config["delivery"])
    sys.path.insert(0, str(ROOT / "03_src"))
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    try:
        def closed(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate scheduled configuration key")
                value[key] = item
            return value
        raw = args.config.read_bytes()
        config = json.loads(raw.decode("utf-8"), object_pairs_hook=closed)
        bootstrap(config)
        from agri_research_agent.automation.first_batch_scheduled import run
        result = run(config, publish=args.publish)
        if args.config.read_bytes() != raw:
            raise ValueError("scheduled configuration changed during execution")
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result["status"] == "PARTIAL_FAILURE" else 0
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
