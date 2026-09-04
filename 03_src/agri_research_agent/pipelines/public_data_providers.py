"""Production adapters for the unified public-data refresh orchestrator."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from time import perf_counter

from agri_research_agent.data_sources.lutou.live import (
    LutouClient,
    LutouClientError,
    LutouConnectionError,
    LutouConnectionSettings,
    LutouPreflightProofContext,
    LutouQuery,
    LutouReadOnlyError,
    LutouSchemaError,
    LutouSourceUnavailableError,
)
from agri_research_agent.data_sources.lutou.domestic_basis import (
    LutouDomesticBasisLiveAdapter,
    load_domestic_basis_catalog,
)
from agri_research_agent.data_sources.lutou.soil_moisture_live import (
    LUTOU_WEATHER_SCHEMA,
    load_soil_moisture_series,
)
from agri_research_agent.data_sources.lutou.three_oil_live import LUTOU_SCHEMA
from agri_research_agent.data_sources.lutou.weather_live import (
    WeatherSourceCatalog,
    load_weather_source_catalog,
)
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
    run_goal_b,
)
from agri_research_agent.pipelines.lutou_goal_b import (
    load_current as load_lutou_current,
)
from agri_research_agent.pipelines.lutou_goal_b_soil import (
    LutouGoalBSoilError,
    load_soil_current,
    run_goal_b_soil,
)
from agri_research_agent.pipelines.lutou_domestic_basis import (
    DomesticBasisPipelineError,
    load_domestic_basis_current,
    load_historical_basis_seed,
    run_domestic_basis_live,
)
from agri_research_agent.pipelines.lutou_weather import (
    LutouWeatherError,
    LutouWeatherStageError,
    load_weather_current,
    run_lutou_weather,
    validate_weather_normal_baselines,
)
from agri_research_agent.pipelines.public_data_refresh import (
    CurrentIdentity,
    ProviderFailure,
    ProviderStatus,
    RefreshResult,
    root_failure_from_exception,
)
from agri_research_agent.pipelines.tankan_goal_a import (
    TankanGoalAError,
    run_goal_a,
)
from agri_research_agent.pipelines.tankan_goal_a import (
    load_current as load_tankan_current,
)
from agri_research_agent.research_data.three_oil_v1 import load_three_oil_v1
from agri_research_agent.shared.runtime_context import RuntimeContext

Connector = Callable[[str, int, float], bool]
NetworkCheck = Callable[[], bool]
LutouRecoveryClientFactory = Callable[[], LutouClient]


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
            domestic_spread = self._client.inspect_relation("market", "futures_spread")
            market_names = {column.column_name for column in market}
            fx_names = {column.column_name for column in fx}
            domestic_spread_names = {column.column_name for column in domestic_spread}
            required_market = {
                "trade_date", "exchange", "product_name", "contract",
                "close_price", "updated_at",
            }
            required_fx = {
                "trade_date", "spot", "updated_at",
                *(f"fx_{month}m" for month in range(1, 13)),
            }
            required_domestic_spread = {
                "trade_date", "product_name", "contract", "close_price", "updated_at",
            }
            if (
                not required_market <= market_names
                or not required_fx <= fx_names
                or not required_domestic_spread <= domestic_spread_names
            ):
                raise TankanSchemaError("approved Tankan relation is unavailable")
            source_max = {
                key: value.isoformat()
                for key, value in self._client.latest_source_dates().items()
            }
            self._preflight_source_max = source_max
            return {"read_only": True, "relations": 3, "source_max_dates": source_max}
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
            if "domestic_spread" in self._preflight_source_max:
                source_max["domestic_spread"] = self._preflight_source_max[
                    "domestic_spread"
                ]
            return RefreshResult(
                result.promoted,
                source_max,
                performance={
                    "async_updates": dict(getattr(result, "async_reports", {})),
                    "domains": {"tankan": dict(getattr(result, "performance", {}))}
                },
            )
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
    weather_policy_path: Path | None = None
    weather_baseline_root: Path | None = None
    connector: Connector = tcp_reachable
    network_check: NetworkCheck = tailscale_ready
    recovery_client_factory: LutouRecoveryClientFactory | None = field(
        default=None, repr=False
    )
    name: str = "lutou"
    _client: LutouClient | None = field(default=None, init=False, repr=False)
    _preflight_source_max: Mapping[str, str] = field(default_factory=dict, init=False, repr=False)
    _preflight_performance: Mapping[str, object] = field(
        default_factory=dict, init=False, repr=False
    )
    _weather_catalog: WeatherSourceCatalog | None = field(default=None, init=False, repr=False)

    def current_identity(self) -> CurrentIdentity:
        oil_root = self.runtime.runtime_root / "public-market-data" / "lutou-three-oil"
        soil_root = self.runtime.runtime_root / "public-market-data" / "lutou-soil-moisture"
        weather_root = self.runtime.runtime_root / "public-market-data" / "lutou-weather"
        oil = load_lutou_current(oil_root)
        soil = load_soil_current(soil_root)
        weather = load_weather_current(weather_root)
        identities: dict[str, object] = {}
        maxima: dict[str, str] = {}
        releases: list[str] = []
        for domain, current, root in (
            ("three_oil", oil, oil_root),
            ("soil_moisture", soil, soil_root),
            ("weather", weather, weather_root),
        ):
            if current is not None:
                pointer = _pointer(root)
                identities[domain] = pointer["manifest_sha256"]
                if domain == "weather":
                    maxima["weather_observation"] = str(
                        current.manifest["source_max_dates"]["observation"]
                    )
                    maxima["weather_forecast_valid"] = str(
                        current.manifest["source_max_dates"]["forecast_valid"]
                    )
                else:
                    maxima[domain] = str(current.manifest["source_max_date"])
                releases.append(f"{domain}:{current.release_id}")
        digest = None
        if identities:
            digest = hashlib.sha256(
                json.dumps(identities, sort_keys=True).encode("utf-8")
            ).hexdigest()
        return CurrentIdentity("|".join(releases) or None, digest, maxima)

    def preflight(self) -> Mapping[str, object]:
        preflight_started = perf_counter()
        self._preflight_performance = {}
        if not self.network_check():
            raise ProviderFailure(ProviderStatus.NETWORK_UNAVAILABLE, "Tailscale network is unavailable")
        if not self.connector(self.settings.host, self.settings.port, 5.0):
            raise ProviderFailure(ProviderStatus.SOURCE_UNAVAILABLE, "Lutou TCP endpoint is unavailable")
        try:
            self._client = LutouClient(self.settings)
            self._client.__enter__()
            proof_context = LutouPreflightProofContext()
            maxima: dict[str, str] = {}
            readiness: dict[str, int] = defaultdict(int)
            queries = list(self._approved_queries())
            if self.weather_policy_path is not None:
                if self.weather_baseline_root is None:
                    raise LutouSchemaError("Weather normal baseline root is missing")
                validate_weather_normal_baselines(
                    self.weather_policy_path, self.weather_baseline_root
                )
                self._weather_catalog = load_weather_source_catalog(
                    self._client, self.weather_policy_path
                )
                queries.extend(
                    (
                        "weather_observation"
                        if item.data_family == "observation"
                        else "weather_forecast_valid",
                        item.query,
                    )
                    for item in self._weather_catalog.tables
                )
            for domain, query in queries:
                proof = self._client.probe_query(
                    query, proof_context=proof_context
                )
                latest = str(proof["latest_date"])
                previous = maxima.get(domain)
                if previous is None or latest > previous:
                    maxima[domain] = latest
                readiness[domain] += 1
            self._preflight_source_max = maxima
            self._preflight_performance = proof_context.safe_telemetry(
                self._client,
                elapsed_seconds=perf_counter() - preflight_started,
            )
            return {
                "read_only": True,
                "source_max_dates": maxima,
                "readiness": {
                    "connectivity": "READY",
                    "required_query": "READY",
                    "required_schema": "READY",
                    "query_counts": dict(sorted(readiness.items())),
                },
                "performance": dict(self._preflight_performance),
            }
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
        except LutouClientError:
            self.close()
            raise ProviderFailure(ProviderStatus.SOURCE_UNAVAILABLE, "Lutou required query readiness failed") from None

    def refresh(self) -> RefreshResult:
        if self._client is None:
            raise ProviderFailure(ProviderStatus.INGESTION_FAILURE, "Lutou preflight client is unavailable")
        domains: dict[str, str] = {}
        maxima: dict[str, str] = {}
        performance_domains: dict[str, object] = {}
        async_reports: dict[str, object] = {}
        promoted = False
        failures: list[ProviderFailure] = []
        try:
            try:
                self._ensure_domain_connection("three-oil")
                oil = run_goal_b(
                    self._client,
                    runtime=self.runtime,
                    run_id=f"{self.run_id}-lutou-oil",
                    end_date=self.end_date,
                    full_load=False,
                    catalog_path=self.three_oil_catalog_path,
                )
                domains["three_oil"] = (ProviderStatus.UPDATED if oil.promoted else ProviderStatus.NO_CHANGE).value
                async_reports.update(getattr(oil, "async_reports", {}))
                maxima["three_oil"] = str(oil.candidate_manifest["source_max_date"])
                performance_domains["three_oil"] = dict(
                    getattr(oil, "performance", {})
                )
                promoted = promoted or oil.promoted
            except ProviderFailure as failure:
                domains["three_oil"] = failure.status.value
                failures.append(failure)
            except LutouGoalBError as exc:
                failure = _pipeline_failure(exc, "Lutou three-oil")
                domains["three_oil"] = failure.status.value
                failures.append(failure)
            try:
                self._ensure_domain_connection("soil-moisture")
                soil = run_goal_b_soil(
                    self._client,
                    runtime=self.runtime,
                    run_id=f"{self.run_id}-lutou-soil",
                    end_date=self.end_date,
                    full_load=False,
                    catalog_path=self.soil_catalog_path,
                )
                domains["soil_moisture"] = (ProviderStatus.UPDATED if soil.promoted else ProviderStatus.NO_CHANGE).value
                async_reports.update(getattr(soil, "async_reports", {}))
                maxima["soil_moisture"] = str(soil.candidate_manifest["source_max_date"])
                performance_domains["soil_moisture"] = dict(
                    getattr(soil, "performance", {})
                )
                promoted = promoted or soil.promoted
            except ProviderFailure as failure:
                domains["soil_moisture"] = failure.status.value
                failures.append(failure)
            except LutouGoalBSoilError as exc:
                failure = _pipeline_failure(exc, "Lutou soil-moisture")
                domains["soil_moisture"] = failure.status.value
                failures.append(failure)
            if self.weather_policy_path is not None:
                try:
                    self._ensure_domain_connection("weather")
                    weather = run_lutou_weather(
                        self._client,
                        runtime=self.runtime,
                        run_id=f"{self.run_id}-lutou-weather",
                        as_of_date=self.end_date,
                        full_load=False,
                        policy_path=self.weather_policy_path,
                        baseline_root=self.weather_baseline_root,
                        source_catalog=self._weather_catalog,
                    )
                    domains["weather"] = (
                        ProviderStatus.UPDATED
                        if weather.promoted
                        else ProviderStatus.NO_CHANGE
                    ).value
                    maxima["weather_observation"] = str(
                        weather.candidate_manifest["source_max_dates"]["observation"]
                    )
                    maxima["weather_forecast_valid"] = str(
                        weather.candidate_manifest["source_max_dates"]["forecast_valid"]
                    )
                    promoted = promoted or weather.promoted
                    async_reports.update(getattr(weather, "async_reports", {}))
                except ProviderFailure as failure:
                    domains["weather"] = failure.status.value
                    failures.append(failure)
                except LutouClientError as exc:
                    failure = ProviderFailure(
                        ProviderStatus.SOURCE_UNAVAILABLE,
                        "Lutou weather extraction failed: LutouClientError; "
                        "root_cause=SOURCE_CONNECTION_FAILURE; query_retried=false",
                        root_failure=root_failure_from_exception(
                            "lutou", "weather", "EXTRACTION", exc
                        ),
                    )
                    domains["weather"] = failure.status.value
                    failures.append(failure)
                except LutouWeatherStageError as exc:
                    failure = ProviderFailure(
                        ProviderStatus.INGESTION_FAILURE,
                        f"Lutou Weather pipeline failed: {type(exc).__name__}",
                        root_failure=root_failure_from_exception(
                            "lutou", "weather", exc.stage, exc
                        ),
                    )
                    domains["weather"] = failure.status.value
                    failures.append(failure)
                except LutouWeatherError as exc:
                    failure = _pipeline_failure(
                        exc, "Lutou Weather", provider_id="lutou",
                        domain="weather", stage="WEATHER",
                    )
                    domains["weather"] = failure.status.value
                    failures.append(failure)
                except Exception as exc:
                    failure = ProviderFailure(
                        ProviderStatus.INGESTION_FAILURE,
                        f"Lutou Weather pipeline failed: {type(exc).__name__}",
                        root_failure=root_failure_from_exception(
                            "lutou", "weather", "WEATHER", exc
                        ),
                    )
                    domains["weather"] = failure.status.value
                    failures.append(failure)
            if failures:
                status = failures[0].status
                return RefreshResult(
                    promoted, maxima, domains, status,
                    failures[0].safe_reason,
                    {
                        "async_updates": async_reports,
                        "preflight": dict(self._preflight_performance),
                        "domains": performance_domains,
                    },
                    failures[0].root_failure,
                )
            return RefreshResult(
                promoted,
                maxima,
                domains,
                performance={
                    "async_updates": async_reports,
                    "preflight": dict(self._preflight_performance),
                    "domains": performance_domains,
                },
            )
        finally:
            self.close()

    def _ensure_domain_connection(self, domain: str) -> None:
        """Validate once at a domain boundary and rebuild one fresh client if stale."""

        if self._client is not None:
            try:
                self._client.ensure_connected()
                return
            except LutouClientError:
                self._client.close()
                self._client = None
        if self.recovery_client_factory is None:
            raise _lutou_connection_validation_failure(domain) from None
        replacement: LutouClient | None = None
        try:
            replacement = self.recovery_client_factory()
            replacement.__enter__()
        except Exception:
            if replacement is not None:
                replacement.close()
            raise _lutou_connection_validation_failure(domain) from None
        self._client = replacement

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self._weather_catalog = None

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


@dataclass(slots=True)
class DomesticBasisPendingAdapter:
    """D3A provider hook that cannot connect, refresh, or claim live evidence."""

    runtime: RuntimeContext
    mapping_path: Path
    name: str = "lutou_domestic_basis"

    def current_identity(self) -> CurrentIdentity:
        root = self.runtime.runtime_root / "public-market-data" / "lutou-domestic-basis"
        current = load_domestic_basis_current(root)
        if current is None:
            return CurrentIdentity(None, None, {})
        pointer = _pointer(root)
        source_max = current.manifest.get("source_max_date")
        maxima = {} if source_max is None else {"domestic_basis": str(source_max)}
        return CurrentIdentity(
            current.release_id,
            str(pointer["manifest_sha256"]),
            maxima,
        )

    def preflight(self) -> Mapping[str, object]:
        catalog = load_domestic_basis_catalog(self.mapping_path)
        if not catalog.live_verified:
            raise ProviderFailure(
                ProviderStatus.LIVE_VERIFICATION_PENDING,
                "Domestic Basis live schema and source mapping verification is pending",
            )
        raise ProviderFailure(
            ProviderStatus.SOURCE_UNAVAILABLE,
            "Domestic Basis live source adapter is not configured",
        )

    def refresh(self) -> RefreshResult:
        raise ProviderFailure(
            ProviderStatus.LIVE_VERIFICATION_PENDING,
            "Domestic Basis live verification must complete before refresh",
        )


@dataclass(slots=True)
class DomesticBasisRefreshAdapter:
    """Independent live Domestic Basis provider using the shared Lutou client contract."""

    settings: LutouConnectionSettings = field(repr=False)
    runtime: RuntimeContext
    run_id: str
    mapping_path: Path
    connector: Connector = tcp_reachable
    recovery_client_factory: LutouRecoveryClientFactory | None = field(
        default=None, repr=False
    )
    name: str = "lutou_domestic_basis"
    _client: LutouClient | None = field(default=None, init=False, repr=False)
    _adapter: LutouDomesticBasisLiveAdapter | None = field(default=None, init=False, repr=False)
    _source_max: date | None = field(default=None, init=False, repr=False)

    def current_identity(self) -> CurrentIdentity:
        root = self.runtime.runtime_root / "public-market-data" / "lutou-domestic-basis"
        current = load_domestic_basis_current(root)
        if current is None:
            return CurrentIdentity(None, None, {})
        pointer = _pointer(root)
        return CurrentIdentity(
            current.release_id,
            str(pointer["manifest_sha256"]),
            {"domestic_basis": str(current.manifest["source_max_date"])},
        )

    def preflight(self) -> Mapping[str, object]:
        catalog = load_domestic_basis_catalog(self.mapping_path)
        if not catalog.live_verified:
            raise ProviderFailure(
                ProviderStatus.LIVE_VERIFICATION_PENDING,
                "Domestic Basis live schema and source mapping verification is pending",
            )
        public_root = self.runtime.runtime_root / "public-market-data" / "lutou-domestic-basis"
        current = load_domestic_basis_current(public_root)
        seed = load_historical_basis_seed(public_root)
        if (
            current is None
            or current.manifest.get("schema_version") != "lutou-domestic-basis-current/3"
            or seed is None
        ):
            raise ProviderFailure(
                ProviderStatus.SOURCE_SCHEMA_FAILURE,
                "Formal Domestic Basis Current alignment and sealed history seed are required",
            )
        if not self.connector(self.settings.host, self.settings.port, 5.0):
            raise ProviderFailure(
                ProviderStatus.NETWORK_UNAVAILABLE,
                "Lutou Domestic Basis TCP endpoint is unavailable",
            )
        try:
            self._client = LutouClient(self.settings)
            self._client.__enter__()
            self._adapter = LutouDomesticBasisLiveAdapter(self._client)
            schema_proof = self._adapter.verify_schema()
            _, self._source_max = self._adapter.date_bounds()
            query_proof = self._client.probe_query(self._adapter.query)
            return {
                "read_only": self._client.proof.transaction_read_only,
                "write_privileges": list(self._client.proof.write_privileges),
                "relations": 1,
                "series": len(catalog.series),
                "schema": schema_proof,
                "readiness": {
                    "connectivity": "READY",
                    "required_query": "READY",
                    "required_schema": "READY",
                    "query_counts": {"domestic_basis": 1},
                    "latest_query_date": query_proof["latest_date"],
                },
                "source_max_dates": {"domestic_basis": self._source_max.isoformat()},
            }
        except LutouReadOnlyError:
            self.close()
            raise ProviderFailure(ProviderStatus.AUTH_FAILURE, "Lutou read-only verification failed") from None
        except LutouSchemaError:
            self.close()
            raise ProviderFailure(ProviderStatus.SOURCE_SCHEMA_FAILURE, "Domestic Basis approved schema check failed") from None
        except LutouSourceUnavailableError:
            self.close()
            raise ProviderFailure(ProviderStatus.SOURCE_UNAVAILABLE, "Domestic Basis approved source is empty") from None
        except LutouConnectionError:
            self.close()
            raise ProviderFailure(ProviderStatus.AUTH_FAILURE, "Lutou authentication failed") from None
        except LutouClientError:
            self.close()
            raise ProviderFailure(ProviderStatus.SOURCE_UNAVAILABLE, "Domestic Basis required query readiness failed") from None

    def refresh(self) -> RefreshResult:
        if self._adapter is None or self._client is None:
            raise ProviderFailure(ProviderStatus.INGESTION_FAILURE, "Domestic Basis preflight client is unavailable")
        try:
            self._client.ensure_connected()
        except LutouClientError:
            stale = self._client
            self._client = None
            self._adapter = None
            stale.close()
            replacement: LutouClient | None = None
            try:
                if self.recovery_client_factory is None:
                    raise LutouConnectionError("fresh client factory is unavailable")
                replacement = self.recovery_client_factory()
                replacement.__enter__()
            except Exception:
                if replacement is not None:
                    replacement.close()
                raise ProviderFailure(
                    ProviderStatus.SOURCE_UNAVAILABLE,
                    "Domestic Basis connection-validation failed after one fresh-client "
                    "recovery attempt: LutouClientError; "
                    "root_cause=SOURCE_CONNECTION_FAILURE",
                ) from None
            self._client = replacement
            self._adapter = LutouDomesticBasisLiveAdapter(replacement)
        try:
            result = run_domestic_basis_live(
                runtime=self.runtime,
                run_id=f"{self.run_id}-lutou-domestic-basis",
                adapter=self._adapter,
                mapping_path=self.mapping_path,
                require_formal_current=True,
            )
            return RefreshResult(
                result.promoted,
                {"domestic_basis": result.query_end_date.isoformat()},
                {"domestic_basis": (ProviderStatus.UPDATED if result.promoted else ProviderStatus.NO_CHANGE).value},
                performance={"async_updates": {"domestic_basis": dict(result.update_summary)} if hasattr(result, "update_summary") else {}},
            )
        except DomesticBasisPipelineError as exc:
            raise _pipeline_failure(exc, "Lutou Domestic Basis") from None
        finally:
            self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = None
        self._adapter = None


def _pipeline_failure(
    exc: Exception,
    provider: str,
    *,
    provider_id: str | None = None,
    domain: str | None = None,
    stage: str | None = None,
) -> ProviderFailure:
    message = str(exc).lower()
    if "quality" in message or "collision" in message or "duplicate" in message:
        status = ProviderStatus.QC_FAILURE
    elif "promot" in message or "pointer" in message or "manifest" in message:
        status = ProviderStatus.PROMOTION_FAILURE
    elif "schema" in message or "column" in message or "relation" in message:
        status = ProviderStatus.SOURCE_SCHEMA_FAILURE
    else:
        status = ProviderStatus.INGESTION_FAILURE
    root_stage = stage or (
        "PROMOTION" if status is ProviderStatus.PROMOTION_FAILURE else "REFRESH"
    )
    return ProviderFailure(
        status,
        f"{provider} pipeline failed: {type(exc).__name__}",
        root_failure=root_failure_from_exception(
            provider_id or provider.lower().replace(" ", "_"),
            domain,
            root_stage,
            exc,
        ),
    )


def _lutou_connection_validation_failure(domain: str) -> ProviderFailure:
    safe_domain = domain.replace("_", "-")
    return ProviderFailure(
        ProviderStatus.SOURCE_UNAVAILABLE,
        f"Lutou {safe_domain} connection-validation failed after one fresh-client "
        "recovery attempt: LutouClientError; root_cause=SOURCE_CONNECTION_FAILURE",
    )


def _pointer(root: Path) -> Mapping[str, object]:
    return json.loads((root / "current.json").read_text(encoding="utf-8"))


__all__ = [
    "DomesticBasisPendingAdapter", "DomesticBasisRefreshAdapter",
    "LutouRefreshAdapter", "TankanRefreshAdapter",
    "tailscale_ready", "tcp_reachable"
]
