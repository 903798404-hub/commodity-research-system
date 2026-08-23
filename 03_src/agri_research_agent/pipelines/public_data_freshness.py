"""Consumer-level freshness gates evaluated before Public Current packaging."""

from __future__ import annotations

import os
import sys
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import pandas as pd


class FreshnessStatus(StrEnum):
    PASS = "PASS"
    WARNING = "WARNING"
    STALE = "STALE"


class FreshnessGate(StrEnum):
    HARD = "HARD"
    WARNING = "WARNING"


@dataclass(frozen=True, slots=True)
class ConsumerFreshness:
    consumer: str
    latest_business_date: date | None
    expected_latest_date: date | None
    status: FreshnessStatus
    gate: FreshnessGate
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "consumer": self.consumer,
            "latest_business_date": (
                None if self.latest_business_date is None else self.latest_business_date.isoformat()
            ),
            "expected_latest_date": (
                None if self.expected_latest_date is None else self.expected_latest_date.isoformat()
            ),
            "status": self.status.value,
            "gate": self.gate.value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ConsumerFreshnessReport:
    results: tuple[ConsumerFreshness, ...]

    @property
    def hard_pass(self) -> bool:
        return not any(
            item.gate is FreshnessGate.HARD and item.status is FreshnessStatus.STALE
            for item in self.results
        )

    def as_dict(self) -> dict[str, Any]:
        overall = (
            "STALE"
            if not self.hard_pass
            else "WARNING"
            if any(item.status is FreshnessStatus.STALE for item in self.results)
            else "PASS"
        )
        return {
            "status": overall,
            "results": [item.as_dict() for item in self.results],
        }


Probe = Callable[[], tuple[date | None, date | None, str]]


def evaluate_consumer_freshness(
    probes: Mapping[str, tuple[FreshnessGate, Probe]],
) -> ConsumerFreshnessReport:
    """Compare consumer dates with their own upstream/proposed-Current dates.

    This deliberately does not require a date to equal today.  Exchange holidays,
    weekends and provider closure days are represented by the upstream's observed
    business date; only a consumer falling behind that date is stale.
    """

    results: list[ConsumerFreshness] = []
    for name, (gate, probe) in probes.items():
        try:
            latest, expected, detail = probe()
            stale = latest is None or expected is None or latest < expected
            status = FreshnessStatus.STALE if stale else FreshnessStatus.PASS
        except Exception as exc:  # fail closed without leaking source details
            latest = expected = None
            detail = f"probe failed: {type(exc).__name__}"
            status = FreshnessStatus.STALE
        results.append(ConsumerFreshness(name, latest, expected, status, gate, detail))
    return ConsumerFreshnessReport(tuple(results))


def build_consumer_freshness_validator(
    *, project_root: str | Path, runtime_root: str | Path
) -> Callable[[Path | None], ConsumerFreshnessReport]:
    """Build probes for the first four Goal E consumers.

    ``proposed_root`` is accepted by the returned callable so orchestration can
    validate either the freshly promoted local Current or a staged package.
    Domestic Spread uses the proposed package artifact when present; its expected
    date is derived independently from configured source-leg common dates.
    """

    project = Path(project_root).resolve()
    runtime = Path(runtime_root).resolve()
    apps = project / "05_apps"
    if str(apps) not in sys.path:
        sys.path.insert(0, str(apps))
    os.environ["PUBLIC_MARKET_DATA_RUNTIME_ROOT"] = str(runtime)

    def validate(proposed_root: Path | None = None) -> ConsumerFreshnessReport:
        data_root = Path(proposed_root).resolve() if proposed_root is not None else runtime
        public = data_root / "public-market-data"

        def international() -> tuple[date | None, date | None, str]:
            from international_spread_page import load_international_spread_payload

            payload = load_international_spread_payload("palm", project_root=project)
            expected = max(
                (
                    metric.expected_latest_date
                    for section in payload.sections
                    for row in section.rows
                    for metric in row.metrics
                ),
                default=None,
            )
            return payload.as_of_date, expected, "palm payload vs metric inputs"

        def domestic_spread() -> tuple[date | None, date | None, str]:
            packaged_spread = (
                data_root
                / "consumer-artifacts"
                / "domestic-spread"
                / "historical_spread_database.parquet"
            )
            spread_path = (
                packaged_spread
                if packaged_spread.is_file()
                else project / "01_data" / "historical_spread_database.parquet"
            )
            price_path = project / "01_data" / "historical_price_long.xlsx"
            config_path = project / "02_configs" / "historical_spread_config.xlsx"
            spread = pd.read_parquet(spread_path)
            spread["date"] = pd.to_datetime(spread["date"], errors="coerce")
            latest = _date_max(spread.loc[spread["status"].eq("success"), "date"])
            price = pd.read_excel(price_path, sheet_name="price_long")
            config = pd.read_excel(config_path, sheet_name="spread_config")
            expected = _domestic_spread_expected_date(price, config)
            heartbeat = _producer_heartbeat(project / "01_data" / "update_status.json")
            cutoff = date.today() - timedelta(days=3)
            if heartbeat is None or heartbeat < cutoff:
                expected = max((item for item in (expected, cutoff) if item is not None), default=cutoff)
                detail = "producer heartbeat stale; persisted spread also checked against common leg dates"
            else:
                detail = "persisted spread vs common configured leg dates; producer heartbeat current"
            return latest, expected, detail

        def weather() -> tuple[date | None, date | None, str]:
            from agri_research_agent.pipelines.lutou_weather import load_weather_current
            from agri_research_agent.summary_engine.weather_cache import (
                load_weather_current_summary_cached,
            )

            current = load_weather_current(public / "lutou-weather")
            if current is None:
                raise ValueError("Weather Current missing")
            summary = load_weather_current_summary_cached(
                public / "lutou-weather", project / "02_configs" / "soybean_weather_us.yaml"
            )
            expected = date.fromisoformat(str(current.manifest["source_max_dates"]["observation"]))
            latest = None if summary.source_date is None else date.fromisoformat(summary.source_date)
            return latest, expected, "weather summary vs observation Current"

        def domestic_basis() -> tuple[date | None, date | None, str]:
            from agri_research_agent.pipelines.lutou_domestic_basis import (
                load_domestic_basis_current,
            )
            from basis_page import load_basis_page_data

            current = load_domestic_basis_current(public / "lutou-domestic-basis")
            if current is None:
                raise ValueError("Domestic Basis Current missing")
            frame, _ = load_basis_page_data(public / "lutou-domestic-basis")
            return (
                _date_max(frame["date"]),
                date.fromisoformat(str(current.manifest["max_date"])),
                "basis page loader vs Domestic Basis Current",
            )

        return evaluate_consumer_freshness(
            {
                "International Spread": (FreshnessGate.HARD, international),
                "Domestic Spread": (FreshnessGate.HARD, domestic_spread),
                # Weather source tables can close on different provider dates;
                # surface divergence but do not block a complete market package.
                "Weather": (FreshnessGate.WARNING, weather),
                "Domestic Basis": (FreshnessGate.HARD, domestic_basis),
            }
        )

    return validate


def _domestic_spread_expected_date(price: pd.DataFrame, config: pd.DataFrame) -> date | None:
    data = price.copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data["delivery_month"] = pd.to_numeric(data["delivery_month"], errors="coerce")
    data = data[data.get("status", "success").eq("success")]
    enabled = config[config["enabled"].map(_enabled)].copy()
    candidates: list[pd.Timestamp] = []
    for row in enabled.itertuples(index=False):
        leg1 = set(
            data.loc[
                data["instrument"].eq(str(row.leg1_instrument))
                & data["delivery_month"].eq(int(row.leg1_month)),
                "date",
            ].dropna()
        )
        leg2 = set(
            data.loc[
                data["instrument"].eq(str(row.leg2_instrument))
                & data["delivery_month"].eq(int(row.leg2_month)),
                "date",
            ].dropna()
        )
        for value in leg1 & leg2:
            if _inside_window(pd.Timestamp(value), row):
                candidates.append(pd.Timestamp(value))
    return _date_max(pd.Series(candidates, dtype="datetime64[ns]"))


def _inside_window(value: pd.Timestamp, row: Any) -> bool:
    start = (int(row.window_start_month), int(row.window_start_day))
    end = (int(row.window_end_month), int(row.window_end_day))
    current = (value.month, value.day)
    return current >= start or current <= end if start > end else start <= current <= end


def _enabled(value: object) -> bool:
    return value is True or str(value).strip().lower() in {"true", "1", "yes", "y", "是", "启用"}


def _date_max(values: Sequence[Any] | pd.Series) -> date | None:
    parsed = pd.to_datetime(values, errors="coerce")
    maximum = parsed.max() if len(parsed) else pd.NaT
    return None if pd.isna(maximum) else pd.Timestamp(maximum).date()


def _producer_heartbeat(path: Path) -> date | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    for key in ("finished_at", "server_date", "latest_date"):
        value = payload.get(key)
        if value:
            try:
                return date.fromisoformat(str(value)[:10])
            except ValueError:
                continue
    return None


__all__ = [
    "ConsumerFreshness", "ConsumerFreshnessReport", "FreshnessGate",
    "FreshnessStatus", "build_consumer_freshness_validator",
    "evaluate_consumer_freshness",
]
