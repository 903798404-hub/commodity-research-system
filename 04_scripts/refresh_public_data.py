#!/usr/bin/env python
"""CLI entrypoint for the isolated unified public-data refresh."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "03_src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agri_research_agent.data_sources.lutou.live import LutouConnectionSettings
from agri_research_agent.data_sources.tankan.client import TankanConnectionSettings
from agri_research_agent.pipelines.public_data_providers import (
    LutouRefreshAdapter,
    TankanRefreshAdapter,
)
from agri_research_agent.pipelines.public_data_refresh import run_unified_refresh
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh isolated public data Currents")
    parser.add_argument("--source", action="append", choices=("tankan", "lutou"), dest="sources")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--weather-baseline-root", type=Path)
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--run-id")
    parser.add_argument(
        "--tankan-secret-file",
        type=Path,
        default=Path.home() / ".market-data-secrets" / "tankan.env",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sources = tuple(dict.fromkeys(args.sources or ("tankan", "lutou")))
    run_id = args.run_id or datetime.now(timezone.utc).strftime("refresh-%Y%m%dT%H%M%SZ")
    runtime = RuntimeContext(
        mode=RuntimeMode.ISOLATED_DEV,
        module_id="international-spread",
        runtime_root=args.runtime_root,
    )
    weather_baseline_root = args.weather_baseline_root or Path(
        os.environ.get(
            "WEATHER_DATA_DIR",
            str(ROOT / "01_data" / "processed" / "weather"),
        )
    )
    adapters = []
    if "tankan" in sources:
        adapters.append(
            TankanRefreshAdapter(
                TankanConnectionSettings.from_secret_file(args.tankan_secret_file),
                runtime,
                run_id,
                args.end_date,
                ROOT / "02_configs" / "tankan_goal_a_market_price.yaml",
                ROOT / "02_configs" / "tankan_fx.yaml",
            )
        )
    if "lutou" in sources:
        adapters.append(
            LutouRefreshAdapter(
                _lutou_settings(),
                runtime,
                run_id,
                args.end_date,
                ROOT / "02_configs" / "public_research_data_catalog.candidate.json",
                ROOT / "02_configs" / "international_three_oil_v1.sealed.json",
                ROOT / "02_configs" / "lutou_weather_current.yaml",
                weather_baseline_root,
            )
        )
    result = run_unified_refresh(
        runtime=runtime,
        run_id=run_id,
        adapters=adapters,
    )
    print(f"run_id={result.run_id}")
    print(f"overall_status={result.overall_status.value}")
    for provider in result.providers:
        print(f"provider={provider.provider} status={provider.status.value}")
    return 0 if result.overall_status.value in {
        "SUCCESS", "SUCCESS_WITH_UNAVAILABLE_SOURCE", "NO_CHANGE"
    } else 1


def _lutou_settings() -> LutouConnectionSettings:
    required = ("LUTOU_HOST", "LUTOU_PORT", "LUTOU_USER", "LUTOU_PASSWORD")
    values = {name: os.environ.get(name, "") for name in required}
    if any(not values[name] for name in required):
        raise SystemExit("Lutou process-local connection settings are incomplete")
    try:
        return LutouConnectionSettings(
            host=values["LUTOU_HOST"],
            port=int(values["LUTOU_PORT"]),
            user=values["LUTOU_USER"],
            password=values["LUTOU_PASSWORD"],
        )
    finally:
        for name in values:
            values[name] = ""


if __name__ == "__main__":
    raise SystemExit(main())
