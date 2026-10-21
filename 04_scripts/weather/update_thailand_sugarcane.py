"""Scheduled data-only entry point, isolated from other weather providers."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))
from agri_research_agent.weather.thailand_sugarcane import refresh, atomic_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("THAILAND_SUGARCANE_WEATHER_ROOT", str(ROOT / "01_data/processed/thailand_sugarcane_weather"))))
    parser.add_argument("--config", type=Path, default=ROOT / "02_configs/thailand_sugarcane_weather.json")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--rebuild", action="store_true", help="重新校验并生成快照，保留上一版本")
    args = parser.parse_args()
    try:
        result = refresh(args.root, args.config, publish=args.publish, rebuild=args.rebuild)
    except Exception as exc:
        result = dict(status="FAILED", error_type=type(exc).__name__, message=str(exc), publications=0)
        atomic_json(args.root / "last_attempt.json", result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 1
    atomic_json(args.root / "last_attempt.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
