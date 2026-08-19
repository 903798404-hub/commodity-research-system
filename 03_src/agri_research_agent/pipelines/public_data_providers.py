"""Production adapters for the unified public-data refresh orchestrator."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Mapping

from agri_research_agent.data_sources.lutou.live import (
    LutouClient,
    LutouConnectionError,
    LutouConnectionSettings,
    LutouReadOnlyError,
    LutouSchemaError,
    LutouSourceUnavailableError,
    LutouQuery,
)
from agri_research_agent.data_sources.lutou.soil_moisture_live import (
    LUTOU_WEATHER_SCHEMA,
    load_soil_moisture_series,
)
from agri_research_agent.data_sources.lutou.three_oil_live import LUTOU_SCHEMA
from agri_research_agent.data_sources.tankan.client import (
    TankanClient,
    TankanConnectionError,
    TankanConnectionSettings,
    TankanReadOnlyError,
    TankanSchemaError,
    TankanSourceUnavailableError,
)
from agri_research_agent.pipelines.lutou_goal_b import (
    LutouGoalBError,
    load_current as load_lutou_current,
    run_goal_b,
)
from agri_research_agent.pipelines.lutou_goal_b_soil import (
    LutouGoalBSoilError,
    load_soil_current,
    run_goal_b_soil,
)
from agri_research_agent.pipelines.public_data_refresh import (
    CurrentIdentity,
    ProviderFailure,
    ProviderStatus,
    RefreshResult,
)
from agri_research_agent.pipelines.tankan_goal_a import (
    TankanGoalAError,
    load_current as load_tankan_current,
    run_goal_a,
)
from agri_research_agent.research_data.three_oil_v1 import load_three_oil_v1
from agri_research_agent.shared.runtime_context import RuntimeContext


Connector = Callable[[str, int, float], bool]
NetworkCheck = Callable[[], bool]


def tcp_reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def tailscale_ready() -> bool:
    """Return only a safe connectivity boolean; never expose peer inventory."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return False
        payload = json.loads(result.stdout)
        return payload.get("BackendState") == "Running"
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return False


@dataclass(slots=True)
class TankanRefreshAdapter:
    settings: TankanConnectionSettings = field(repr=False)
    runtime: RuntimeContext
    run_id: str
    end_date: date
    market_config_path: Path
    fx_config_path: Path
    connector: Connector = tcp_reachable
    name: str = "tankan"
    _client: TankanClient | None = field(default=None, init=False, repr=False)
    _preflight_source_max: Mapping[str, str] = field(default_factory=dict, init=False, repr=False)

    def current_identity(self) -> CurrentIdentity:
        current = load_tankan_current(
            self.runtime.runtime_root / "public-market-data" / "tankan"
        )
        if current is None:
            return CurrentIdentity(None, None, {})
        pointer = _pointer(self.runtime.runtime_root / "public-market-data" / "tankan")
        return CurrentIdentity(
            current.release_id,
            str(pointer["manifest_sha256"]),
            {
                "market": str(current.manifest["market"]["source_max_date"]),
                "fx": str(current.manifest["fx"]["source_max_date"]),
            },
        )

    def preflight(self) -> Mapping[str, object]:
        if not self.connector(self.settings.host, self.settings.port, 5.0):
            raise ProviderFailure(ProviderStatus.NETWORK_UNAVAILABLE, "Tankan TCP endpoint is unavailable")
        try:
            self._client = TankanClient(self.settings)
            self._client.__enter__()
            market = self._client.inspect_relation("market", "foreign_futures_price_raw")
            fx = self._client.inspect_relation("market", "exchange_rate")
            market_names = {column.column_name for column in market}
            fx_names = {column.column_name for column in fx}
            required_market = {
                "trade_date", "exchange", "product_name", "contract",
                "close_price", "updated_at",
            }
            required_fx = {
                "trade_date", "spot", "updated_at",
                *(f"fx_{month}m" for month in range(1, 13)),
            }
            if not required_market <= market_names or not required_fx <= fx_names:
                raise TankanSchemaError("approved Tankan relation is unavailable")
            source_max = {
                key: value.isoformat()
                for key, value in self._client.latest_source_dates().items()
            }
            self._preflight_source_max = source_max
            return {"read_only": True, "relations": 2, "source_max_dates": source_max}
        except TankanSourceUnavailableError:
            self.close()
            raise ProviderFailure(ProviderStatus.SOURCE_UNAVAILABLE, "Tankan approved source is empty") from None
        except TankanReadOnlyError:
            self.close()
            raise ProviderFailure(ProviderStatus.AUTH_FAILURE, "Tankan read-only verification failed") from None
        except TankanSchemaError:
            self.close()
            raise ProviderFailure(ProviderStatus.SOURCE_SCHEMA_FAILURE, "Tankan approved schema check failed") from None
        except TankanConnectionError:
            self.close()
            raise ProviderFailure(ProviderStatus.AUTH_FAILURE, "Tankan authentication failed") from None

    def refresh(self) -> RefreshResult:
        if self._client is None:
            raise ProviderFailure(ProviderStatus.INGESTION_FAILURE, "Tankan preflight client is unavailable")
        try:
            result = run_goal_a(
                self._client,
                runtime=self.runtime,
                run_id=f"{self.run_id}-tankan",
                market_config_path=self.market_config_path,
                fx_config_path=self.fx_config_path,
                end_date=self.end_date,
                full_load=False,
            )
            source_max = {
                "market": str(result.candidate_manifest["market"]["source_max_date"]),
                "fx": str(result.candidate_manifest["fx"]["source_max_date"]),
            }
            return RefreshResult(result.promoted, source_max)
        except TankanGoalAError as exc:
            raise _pipeline_failure(exc, "Tankan") from None
        finally:
            self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


@dataclass(slots=True)
class LutouRefreshAdapter:
    settings: LutouConnectionSettings = field(repr=False)
    runtime: RuntimeContext
    run_id: str
    end_date: date
    soil_catalog_path: Path
    three_oil_catalog_path: Path | None = None
    connector: Connector = tcp_reachable
    network_check: NetworkCheck = tailscale_ready
    name: str = "lutou"
    _client: LutouClient | None = field(default=None, init=False, repr=False)
    _preflight_source_max: Mapping[str, str] = field(default_factory=dict, init=False, repr=False)

    def current_identity(self) -> CurrentIdentity:
        oil_root = self.runtime.runtime_root / "public-market-data" / "lutou-three-oil"
        soil_root = self.runtime.runtime_root / "public-market-data" / "lutou-soil-moisture"
        oil = load_lutou_current(oil_root)
        soil = load_soil_current(soil_root)
        identities: dict[str, object] = {}
        maxima: dict[str, str] = {}
        releases: list[str] = []
        for domain, current, root in (("three_oil", oil, oil_root), ("soil_moisture", soil, soil_root)):
            if current is not None:
                pointer = _pointer(root)
                identities[domain] = pointer["manifest_sha256"]
                maxima[domain] = str(current.manifest["source_max_date"])
                releases.append(f"{domain}:{current.release_id}")
        digest = None
        if identities:
            digest = hashlib.sha256(
                json.dumps(identities, sort_keys=True).encode("utf-8")
            ).hexdigest()
        return CurrentIdentity("|".join(releases) or None, digest, maxima)

    def preflight(self) -> Mapping[str, object]:
        if not self.network_check():
            raise ProviderFailure(ProviderStatus.NETWORK_UNAVAILABLE, "Tailscale network is unavailable")
        if not self.connector(self.settings.host, self.settings.port, 5.0):
            raise ProviderFailure(ProviderStatus.SOURCE_UNAVAILABLE, "Lutou TCP endpoint is unavailable")
        try:
            self._client = LutouClient(self.settings)
            self._client.__enter__()
            maxima: dict[str, str] = {}
            for domain, query in self._approved_queries():
                latest = self._client.latest_date(query).isoformat()
                previous = maxima.get(domain)
                if previous is None or latest > previous:
                    maxima[domain] = latest
            self._preflight_source_max = maxima
            return {"read_only": True, "source_max_dates": maxima}
        except LutouSourceUnavailableError:
            self.close()
            raise ProviderFailure(ProviderStatus.SOURCE_UNAVAILABLE, "Lutou approved source is empty") from None
        except LutouReadOnlyError:
            self.close()
            raise ProviderFailure(ProviderStatus.AUTH_FAILURE, "Lutou read-only verification failed") from None
        except LutouSchemaError:
            self.close()
            raise ProviderFailure(ProviderStatus.SOURCE_SCHEMA_FAILURE, "Lutou approved schema check failed") from None
        except LutouConnectionError:
            self.close()
            raise ProviderFailure(ProviderStatus.AUTH_FAILURE, "Lutou authentication failed") from None

    def refresh(self) -> RefreshResult:
        if self._client is None:
            raise ProviderFailure(ProviderStatus.INGESTION_FAILURE, "Lutou preflight client is unavailable")
        domains: dict[str, str] = {}
        maxima: dict[str, str] = {}
        promoted = False
        failures: list[ProviderFailure] = []
        try:
            try:
                oil = run_goal_b(
                    self._client,
                    runtime=self.runtime,
                    run_id=f"{self.run_id}-lutou-oil",
                    end_date=self.end_date,
                    full_load=False,
                    catalog_path=self.three_oil_catalog_path,
                )
                domains["three_oil"] = (ProviderStatus.UPDATED if oil.promoted else ProviderStatus.NO_CHANGE).value
                maxima["three_oil"] = str(oil.candidate_manifest["source_max_date"])
                promoted = promoted or oil.promoted
            except LutouGoalBError as exc:
                failure = _pipeline_failure(exc, "Lutou three-oil")
                domains["three_oil"] = failure.status.value
                failures.append(failure)
            try:
                soil = run_goal_b_soil(
                    self._client,
                    runtime=self.runtime,
                    run_id=f"{self.run_id}-lutou-soil",
                    end_date=self.end_date,
                    full_load=False,
                    catalog_path=self.soil_catalog_path,
                )
                domains["soil_moisture"] = (ProviderStatus.UPDATED if soil.promoted else ProviderStatus.NO_CHANGE).value
                maxima["soil_moisture"] = str(soil.candidate_manifest["source_max_date"])
                promoted = promoted or soil.promoted
            except LutouGoalBSoilError as exc:
                failure = _pipeline_failure(exc, "Lutou soil-moisture")
                domains["soil_moisture"] = failure.status.value
                failures.append(failure)
            if failures:
                status = failures[0].status
                return RefreshResult(
                    promoted, maxima, domains, status,
                    "one or more Lutou domains failed",
                )
            return RefreshResult(promoted, maxima, domains)
        finally:
            self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _approved_queries(self) -> tuple[tuple[str, LutouQuery], ...]:
        output: list[tuple[str, LutouQuery]] = []
        oil_by_table: dict[str, list[str]] = defaultdict(list)
        for contract in load_three_oil_v1(self.three_oil_catalog_path).series:
            oil_by_table[contract.source_native_table].append(contract.source_native_series)
        for table, columns in sorted(oil_by_table.items()):
            output.append(
                (
                    "three_oil",
                    LutouQuery(
                        schema=LUTOU_SCHEMA,
                        table=table,
                        date_column="Date",
                        value_columns=tuple(sorted(columns)),
                    ),
                )
            )
        soil_by_table: dict[tuple[str, str], list[str]] = defaultdict(list)
        for contract in load_soil_moisture_series(self.soil_catalog_path):
            soil_by_table[(contract.source_table, contract.date_column)].append(
                contract.source_column
            )
        for (table, date_column), columns in sorted(soil_by_table.items()):
            output.append(
                (
                    "soil_moisture",
                    LutouQuery(
                        schema=LUTOU_WEATHER_SCHEMA,
                        table=table,
                        date_column=date_column,
                        value_columns=tuple(sorted(columns)),
                        version="lutou-soil-moisture-0-100cm/1",
                        max_plan_rows=20_000,
                    ),
                )
            )
        return tuple(output)


def _pipeline_failure(exc: Exception, provider: str) -> ProviderFailure:
    message = str(exc).lower()
    if "quality" in message or "collision" in message or "duplicate" in message:
        status = ProviderStatus.QC_FAILURE
    elif "promot" in message or "pointer" in message or "manifest" in message:
        status = ProviderStatus.PROMOTION_FAILURE
    elif "schema" in message or "column" in message or "relation" in message:
        status = ProviderStatus.SOURCE_SCHEMA_FAILURE
    else:
        status = ProviderStatus.INGESTION_FAILURE
    return ProviderFailure(status, f"{provider} pipeline failed: {type(exc).__name__}")


def _pointer(root: Path) -> Mapping[str, object]:
    return json.loads((root / "current.json").read_text(encoding="utf-8"))


__all__ = [
    "LutouRefreshAdapter", "TankanRefreshAdapter", "tailscale_ready", "tcp_reachable"
]
