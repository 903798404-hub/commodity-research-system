#!/usr/bin/env python
"""Capture one lightweight Tankan-only Public AM/PM snapshot."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "03_src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agri_research_agent.data_sources.tankan import (  # noqa: E402
    TankanClient,
    TankanClientError,
    TankanConnectionSettings,
)
from agri_research_agent.import_profit.config import load_soybean_config  # noqa: E402
from agri_research_agent.import_profit.intraday import (  # noqa: E402
    required_intraday_contracts_for_date,
)
from agri_research_agent.market_data.calendars import WeekdayBusinessDayPolicy  # noqa: E402
from agri_research_agent.market_data.intraday import MarketSession  # noqa: E402
from agri_research_agent.market_data.intraday import IntradaySnapshotError  # noqa: E402
from agri_research_agent.pipelines.public_intraday import (  # noqa: E402
    PublicIntradayCaptureError,
    RequiredIntradayContracts,
    run_public_intraday_capture_job,
)


BEIJING = ZoneInfo("Asia/Shanghai")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--session", required=True, choices=("AM", "PM"))
    value.add_argument("--business-date", type=date.fromisoformat)
    value.add_argument("--snapshot-root", type=Path, required=True)
    value.add_argument(
        "--secret-file",
        type=Path,
        default=Path.home() / ".market-data-secrets" / "tankan.env",
    )
    value.add_argument(
        "--config",
        type=Path,
        default=ROOT / "02_configs" / "import_profit_soybean.yaml",
    )
    value.add_argument(
        "--environment",
        choices=("FORMAL", "TEST_ISOLATED_NON_PRODUCTION"),
        default="FORMAL",
    )
    value.add_argument("--retry-interval-seconds", type=float, default=30.0)
    value.add_argument("--max-attempts", type=int, default=21)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    now = datetime.now(BEIJING)
    business_date = args.business_date or now.date()
    config = load_soybean_config(args.config)
    cbot, meal, oil = required_intraday_contracts_for_date(business_date, config)
    required = RequiredIntradayContracts(cbot, meal, oil)

    def client_factory() -> TankanClient:
        return TankanClient(TankanConnectionSettings.from_secret_file(args.secret_file))

    try:
        result = run_public_intraday_capture_job(
            client_factory,
            store_root=str(args.snapshot_root),
            business_date=business_date,
            session=MarketSession(args.session),
            required=required,
            business_day_policy=WeekdayBusinessDayPolicy(),
            clock=lambda: datetime.now(BEIJING),
            retry_interval_seconds=args.retry_interval_seconds,
            max_attempts=args.max_attempts,
            environment=args.environment,
        )
    except (PublicIntradayCaptureError, IntradaySnapshotError, TankanClientError, OSError) as exc:
        print(
            json.dumps(
                {
                    "status": getattr(exc, "status", "CAPTURE_FAILED"),
                    "error_type": type(exc).__name__,
                    "business_date": business_date.isoformat(),
                    "session": args.session,
                    "environment": args.environment,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "status": result.seal.status.value,
                "release_id": result.seal.release_id,
                "content_sha256": result.seal.content_sha256,
                "attempts": result.attempts,
                "elapsed_seconds": round(result.elapsed_seconds, 6),
                "source_row_count": result.source_row_count,
                "environment": args.environment,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
