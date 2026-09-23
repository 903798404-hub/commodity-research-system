#!/usr/bin/env python
"""CLI entrypoint for the isolated unified public-data refresh."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "03_src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agri_research_agent.data_sources.lutou.live import (
    LutouClient,
    LutouConnectionSettings,
)
from agri_research_agent.data_sources.tankan.client import TankanConnectionSettings
from agri_research_agent.data_sources.tankan.client import TankanClient
from agri_research_agent.pipelines.public_data_providers import (
    DomesticBasisRefreshAdapter,
    LutouRefreshAdapter,
    TankanRefreshAdapter,
    domestic_basis_current_identity,
    lutou_current_identity,
    tailscale_ready,
    tankan_current_identity,
    tcp_reachable,
)
from agri_research_agent.pipelines.public_data_daily import run_daily_update
from agri_research_agent.pipelines.public_data_freshness import (
    build_consumer_freshness_validator,
)
from agri_research_agent.pipelines.public_data_prewarm import (
    build_consumer_prewarm_targets,
    validate_activated_public_currents,
)
from agri_research_agent.pipelines.public_data_delivery import (
    ServerSyncResult,
    sync_to_local_server_store,
    validate_production_package,
)
from agri_research_agent.pipelines.public_data_refresh import (
    CurrentIdentity,
    DomainStatus,
    ProviderFailure,
    ProviderOutcome,
    ProviderStatus,
    run_unified_refresh,
)
from agri_research_agent.shared.atomic_storage import atomic_write_json
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


class ServerTransportTimeout(TimeoutError):
    """The bounded transport subprocess exhausted its total deadline."""


class ServerActivationFailure(RuntimeError):
    """Transport reached the remote activation stage, which then failed."""


PROVIDER_ORDER = ("tankan", "lutou", "lutou_domestic_basis")
PROVIDER_DOMAINS = {
    "tankan": ("tankan_market", "fx"),
    "lutou": (
        "three_oil", "soil_moisture", "weather_observation", "weather_forecast",
    ),
    "lutou_domestic_basis": ("domestic_basis",),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh isolated public data Currents")
    parser.add_argument("--source", action="append", choices=("tankan", "lutou"), dest="sources")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--weather-baseline-root", type=Path)
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--run-id")
    parser.add_argument(
        "--packages-root",
        type=Path,
        help="Immutable production data package root (defaults inside runtime root)",
    )
    parser.add_argument(
        "--sync-target-root",
        type=Path,
        help="Local filesystem server-store backend used for Goal E validation",
    )
    parser.add_argument(
        "--ssh-target",
        help="Trusted OpenSSH config alias for the production data host",
    )
    parser.add_argument(
        "--remote-store-root",
        help="Absolute Ubuntu public-data server-store root",
    )
    parser.add_argument(
        "--activation-image-id",
        help="Immutable sha256 Image ID containing the remote activation code",
    )
    parser.add_argument(
        "--prewarm",
        action="store_true",
        help="Run the four consumer loader warmers after a successful local switch",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read-only source/current preflight; do not build Candidate or change Current",
    )
    parser.add_argument(
        "--evidence-output",
        type=Path,
        help="Explicit UTF-8 machine evidence file for --dry-run",
    )
    parser.add_argument(
        "--provider-preflight-evidence",
        type=Path,
        help="Validated provider-scoped preflight evidence from the Production Wrapper",
    )
    parser.add_argument(
        "--initial-seed",
        action="store_true",
        help="Explicitly initialize an empty server store from validated unchanged data",
    )
    parser.add_argument(
        "--tankan-secret-file",
        type=Path,
        default=Path.home() / ".market-data-secrets" / "tankan.env",
    )
    parser.add_argument(
        "--lutou-secret-file",
        type=Path,
        default=Path.home() / ".market-data-secrets" / "lutou.env",
    )
    args = parser.parse_args(argv)
    remote = (args.ssh_target, args.remote_store_root, args.activation_image_id)
    if any(remote) and not all(remote):
        parser.error(
            "--ssh-target, --remote-store-root and --activation-image-id are required together"
        )
    if all(remote) and args.sync_target_root is not None:
        parser.error("remote transport and --sync-target-root are mutually exclusive")
    if args.initial_seed and args.dry_run:
        parser.error("--initial-seed and --dry-run are mutually exclusive")
    if args.evidence_output is not None and not args.dry_run:
        parser.error("--evidence-output requires --dry-run")
    if args.provider_preflight_evidence is not None and args.dry_run:
        parser.error("--provider-preflight-evidence cannot be used with --dry-run")
    if all(remote) and not args.dry_run and args.provider_preflight_evidence is None:
        parser.error("formal remote execution requires --provider-preflight-evidence")
    if args.initial_seed and not (all(remote) or args.sync_target_root is not None):
        parser.error("--initial-seed requires a remote transport or --sync-target-root")
    if args.initial_seed and set(args.sources or ("tankan", "lutou")) != {"tankan", "lutou"}:
        parser.error("--initial-seed requires the complete tankan and lutou source set")
    return args


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
    if args.dry_run:
        adapters, blocked = _dry_run_plan(
            args=args,
            runtime=runtime,
            run_id=run_id,
            sources=sources,
            weather_baseline_root=weather_baseline_root,
        )
        return _dry_run(
            adapters,
            run_id=run_id,
            evidence_output=args.evidence_output,
            preset_sources=blocked,
            provider_order=_provider_order(sources),
        )
    if args.provider_preflight_evidence is not None:
        evidence = _load_provider_preflight_evidence(
            args.provider_preflight_evidence,
            run_id=run_id,
            provider_order=_provider_order(sources),
        )
        adapters, preset_outcomes = _execution_plan_from_evidence(
            args=args,
            runtime=runtime,
            run_id=run_id,
            sources=sources,
            weather_baseline_root=weather_baseline_root,
            evidence=evidence,
        )
    else:
        adapters = _build_adapters(
            args=args,
            runtime=runtime,
            run_id=run_id,
            sources=sources,
            weather_baseline_root=weather_baseline_root,
        )
        preset_outcomes = ()
    packages_root = args.packages_root or runtime.runtime_root / "public-data-packages"
    required_datasets = []
    if "tankan" in sources:
        required_datasets.append("tankan")
    if "lutou" in sources:
        required_datasets.extend(
            ("lutou-three-oil", "lutou-soil-moisture", "lutou-weather", "lutou-domestic-basis")
        )
    remote_transport = bool(args.ssh_target)
    result = run_daily_update(
        runtime=runtime,
        run_id=run_id,
        refresh_runner=lambda: run_unified_refresh(
            runtime=runtime,
            run_id=f"{run_id}-refresh",
            adapters=adapters,
            require_all_sources=False,
            preset_outcomes=preset_outcomes,
            provider_order=_provider_order(sources),
        ),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=packages_root,
        server_store_root=(
            args.remote_store_root if remote_transport else args.sync_target_root
        ),
        syncer=(
            _build_remote_syncer(
                ssh_target=args.ssh_target,
                activation_image_id=args.activation_image_id,
            )
            if remote_transport
            else sync_to_local_server_store
        ),
        required_datasets=required_datasets,
        prewarm_target_factory=(
            lambda activated_runtime: build_consumer_prewarm_targets(
                project_root=ROOT, runtime_root=activated_runtime
            )
        ) if args.prewarm else None,
        pre_switch_validator=validate_activated_public_currents,
        post_switch_validator=validate_activated_public_currents,
        consumer_freshness_validator=build_consumer_freshness_validator(
            project_root=ROOT, runtime_root=runtime.runtime_root
        ),
        delivery_artifact_runner=(
            (
                lambda: _refresh_domestic_spread_artifact(
                    tankan_secret_file=args.tankan_secret_file,
                    end_date=args.end_date,
                )
            )
            if "tankan" in sources
            else None
        ),
        delivery_artifact_provider=("tankan" if "tankan" in sources else None),
        preserved_delivery_artifacts=_baseline_delivery_artifacts(packages_root),
        initial_seed=args.initial_seed,
    )
    print(f"run_id={result.run_id}")
    print(f"overall_status={result.business_status.value}")
    for provider in result.refresh.providers:
        print(f"provider={provider.provider} status={provider.status.value}")
    print(result.manifest["summary"])
    return 0 if result.succeeded else 1


def _provider_order(sources: tuple[str, ...]) -> tuple[str, ...]:
    requested: list[str] = []
    if "tankan" in sources:
        requested.append("tankan")
    if "lutou" in sources:
        requested.extend(("lutou", "lutou_domestic_basis"))
    return tuple(requested)


def _build_adapters(
    *,
    args: argparse.Namespace,
    runtime: RuntimeContext,
    run_id: str,
    sources: tuple[str, ...],
    weather_baseline_root: Path,
    allowed_providers: set[str] | None = None,
) -> list[object]:
    allowed = set(_provider_order(sources)) if allowed_providers is None else allowed_providers
    adapters: list[object] = []
    if "tankan" in allowed:
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
    if {"lutou", "lutou_domestic_basis"} & allowed:
        if "lutou" in allowed:
            adapters.append(
                LutouRefreshAdapter(
                    _lutou_settings(args.lutou_secret_file),
                    runtime,
                    run_id,
                    args.end_date,
                    ROOT / "02_configs" / "public_research_data_catalog.candidate.json",
                    ROOT / "02_configs" / "international_three_oil_v1.sealed.json",
                    ROOT / "02_configs" / "lutou_weather_current.yaml",
                    weather_baseline_root,
                    recovery_client_factory=lambda: LutouClient(
                        _lutou_settings(args.lutou_secret_file)
                    ),
                )
            )
        if "lutou_domestic_basis" in allowed:
            adapters.append(
                DomesticBasisRefreshAdapter(
                    _lutou_settings(args.lutou_secret_file),
                    runtime,
                    run_id,
                    ROOT / "02_configs" / "lutou_domestic_basis.yaml",
                    recovery_client_factory=lambda: LutouClient(
                        _lutou_settings(args.lutou_secret_file)
                    ),
                )
            )
    return adapters


def _dry_run_plan(
    *,
    args: argparse.Namespace,
    runtime: RuntimeContext,
    run_id: str,
    sources: tuple[str, ...],
    weather_baseline_root: Path,
) -> tuple[list[object], list[dict[str, object]]]:
    ready: set[str] = set()
    blocked: list[dict[str, object]] = []
    if "tankan" in sources:
        identity = tankan_current_identity(runtime)
        try:
            settings = TankanConnectionSettings.from_secret_file(args.tankan_secret_file)
            if not tcp_reachable(settings.host, settings.port, 5.0):
                raise ConnectionError("Tankan TCP endpoint is unavailable")
            with TankanClient(settings):
                pass
            ready.add("tankan")
        except (Exception, SystemExit) as exc:
            blocked.append(
                _dependency_unavailable_source(
                    "tankan", identity, f"Tankan dependency unavailable: {type(exc).__name__}"
                )
            )
    if "lutou" in sources:
        identities = {
            "lutou": lutou_current_identity(runtime),
            "lutou_domestic_basis": domestic_basis_current_identity(runtime),
        }
        try:
            settings = _lutou_settings(args.lutou_secret_file)
            if not tailscale_ready():
                raise ConnectionError("Lutou network dependency is unavailable")
            if not tcp_reachable(settings.host, settings.port, 5.0):
                raise ConnectionError("Lutou TCP endpoint is unavailable")
            with LutouClient(settings):
                pass
            ready.update(("lutou", "lutou_domestic_basis"))
        except (Exception, SystemExit) as exc:
            for provider in ("lutou", "lutou_domestic_basis"):
                blocked.append(
                    _dependency_unavailable_source(
                        provider,
                        identities[provider],
                        f"Lutou dependency unavailable: {type(exc).__name__}",
                    )
                )
    adapters = _build_adapters(
        args=args,
        runtime=runtime,
        run_id=run_id,
        sources=sources,
        weather_baseline_root=weather_baseline_root,
        allowed_providers=ready,
    )
    return adapters, blocked


def _identity_payload(identity: CurrentIdentity) -> dict[str, object]:
    return {
        "release_id": identity.release_id,
        "manifest_sha256": identity.manifest_sha256,
        "source_max_dates": dict(identity.source_max_dates),
        "dataset_identities": {
            name: dict(value)
            for name, value in sorted(identity.dataset_identities.items())
        },
    }


def _dependency_unavailable_source(
    provider: str, identity: CurrentIdentity, safe_reason: str,
) -> dict[str, object]:
    return {
        "source": provider,
        "status": ProviderStatus.DEPENDENCY_UNAVAILABLE.value,
        "source_max_dates": dict(identity.source_max_dates),
        "current_identity": _identity_payload(identity),
        "domains": {
            domain: DomainStatus.SKIPPED_DEPENDENCY_UNAVAILABLE.value
            for domain in PROVIDER_DOMAINS[provider]
        },
        "safe_reason": safe_reason,
    }


def _domain_preflight_status(status: ProviderStatus) -> str:
    if status is ProviderStatus.READY:
        return ProviderStatus.READY.value
    if status is ProviderStatus.DEPENDENCY_UNAVAILABLE:
        return DomainStatus.SKIPPED_DEPENDENCY_UNAVAILABLE.value
    if status is ProviderStatus.SOURCE_UNAVAILABLE:
        return DomainStatus.MISSING.value
    return DomainStatus.ERROR.value


def _load_provider_preflight_evidence(
    path: Path, *, run_id: str, provider_order: tuple[str, ...],
) -> dict[str, object]:
    value = json.loads(path.resolve().read_text(encoding="utf-8"))
    sources = value.get("sources") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "unified-public-data-dry-run/1"
        or value.get("run_id") != f"{run_id}-preflight"
        or value.get("dry_run") is not True
        or not isinstance(sources, list)
        or [item.get("source") for item in sources if isinstance(item, dict)]
        != list(provider_order)
    ):
        raise ValueError("provider preflight evidence identity is invalid")
    for item in sources:
        if not isinstance(item, dict):
            raise ValueError("provider preflight evidence status is invalid")
        try:
            ProviderStatus(str(item.get("status")))
        except ValueError as exc:
            raise ValueError("provider preflight evidence status is invalid") from exc
        identity = item.get("current_identity")
        if not isinstance(identity, dict):
            raise ValueError("provider preflight Current identity is invalid")
    return value


def _current_identity_from_payload(value: Mapping[str, object]) -> CurrentIdentity:
    maxima = value.get("source_max_dates", {})
    datasets = value.get("dataset_identities", {})
    if not isinstance(maxima, Mapping) or not isinstance(datasets, Mapping):
        raise ValueError("provider preflight identity payload is invalid")
    return CurrentIdentity(
        value.get("release_id") if isinstance(value.get("release_id"), str) else None,
        (
            value.get("manifest_sha256")
            if isinstance(value.get("manifest_sha256"), str)
            else None
        ),
        {str(key): str(item) for key, item in maxima.items()},
        {
            str(name): {
                str(key): (str(item) if item is not None else None)
                for key, item in identity.items()
            }
            for name, identity in datasets.items()
            if isinstance(identity, Mapping)
        },
    )


def _preset_outcome_from_source(item: Mapping[str, object]) -> ProviderOutcome:
    provider = str(item["source"])
    status = ProviderStatus(str(item["status"]))
    identity_value = item["current_identity"]
    if not isinstance(identity_value, Mapping):
        raise ValueError("provider preflight Current identity is invalid")
    identity = _current_identity_from_payload(identity_value)
    domains = item.get("domains")
    if not isinstance(domains, Mapping):
        domains = {
            domain: _domain_preflight_status(status)
            for domain in PROVIDER_DOMAINS[provider]
        }
    return ProviderOutcome(
        provider,
        status,
        status,
        identity,
        identity,
        identity.source_max_dates,
        {str(key): str(value) for key, value in domains.items()},
        str(item.get("safe_reason") or "provider preflight did not pass"),
    )


def _execution_plan_from_evidence(
    *,
    args: argparse.Namespace,
    runtime: RuntimeContext,
    run_id: str,
    sources: tuple[str, ...],
    weather_baseline_root: Path,
    evidence: Mapping[str, object],
) -> tuple[list[object], tuple[ProviderOutcome, ...]]:
    source_items = {
        str(item["source"]): item
        for item in evidence["sources"]  # type: ignore[index]
        if isinstance(item, Mapping)
    }
    ready = {
        name for name, item in source_items.items()
        if item.get("status") == ProviderStatus.READY.value
    }
    preset: list[ProviderOutcome] = [
        _preset_outcome_from_source(source_items[name])
        for name in _provider_order(sources)
        if name not in ready
    ]
    adapters: list[object] = []
    for provider in _provider_order(sources):
        if provider not in ready:
            continue
        try:
            adapters.extend(
                _build_adapters(
                    args=args,
                    runtime=runtime,
                    run_id=run_id,
                    sources=sources,
                    weather_baseline_root=weather_baseline_root,
                    allowed_providers={provider},
                )
            )
        except (Exception, SystemExit) as exc:
            changed = dict(source_items[provider])
            changed["status"] = ProviderStatus.DEPENDENCY_UNAVAILABLE.value
            changed["domains"] = {
                domain: DomainStatus.SKIPPED_DEPENDENCY_UNAVAILABLE.value
                for domain in PROVIDER_DOMAINS[provider]
            }
            changed["safe_reason"] = (
                "provider dependency changed after preflight: " + type(exc).__name__
            )
            preset.append(_preset_outcome_from_source(changed))
    return adapters, tuple(preset)


def _baseline_delivery_artifacts(packages_root: str | Path) -> dict[str, Path]:
    root = Path(packages_root)
    candidates = sorted(
        path for path in root.glob("public-current-*") if path.is_dir()
    ) if root.is_dir() else []
    if len(candidates) != 1:
        return {}
    package = validate_production_package(candidates[0])
    artifacts = package.manifest.get("delivery_artifacts", {})
    if not isinstance(artifacts, Mapping):
        return {}
    output: dict[str, Path] = {}
    for name, identity in artifacts.items():
        if not isinstance(identity, Mapping):
            continue
        package_path = identity.get("package_path")
        if not isinstance(package_path, str):
            continue
        path = package.directory / "data" / package_path
        if path.is_file():
            output[str(name)] = path
    return output


def _dry_run(
    adapters: list[object],
    *,
    run_id: str = "dry-run",
    evidence_output: Path | None = None,
    preset_sources: list[dict[str, object]] | None = None,
    provider_order: tuple[str, ...] | None = None,
) -> int:
    sources: list[dict[str, object]] = list(preset_sources or [])
    for adapter in adapters:
        name = str(getattr(adapter, "name", type(adapter).__name__))
        identity = None
        try:
            identity = adapter.current_identity()
            preflight = adapter.preflight()
            status = ProviderStatus.READY
            reason = None
            source_max = dict(preflight.get("source_max_dates", {}))
        except ProviderFailure as exc:
            try:
                identity = getattr(adapter, "current_identity")()
            except Exception:
                identity = None
            status = exc.status
            reason = exc.safe_reason
            source_max = {} if identity is None else dict(identity.source_max_dates)
        except Exception as exc:
            if identity is None:
                try:
                    identity = getattr(adapter, "current_identity")()
                except Exception:
                    identity = CurrentIdentity(None, None, {})
            status = ProviderStatus.INGESTION_FAILURE
            reason = f"dry-run preflight failed: {type(exc).__name__}"
            source_max = dict(identity.source_max_dates)
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
        sources.append(
            {
                "source": name,
                "status": status.value,
                "source_max_dates": source_max,
                "current_identity": _identity_payload(
                    identity or CurrentIdentity(None, None, {})
                ),
                "domains": {
                    domain: _domain_preflight_status(status)
                    for domain in PROVIDER_DOMAINS.get(name, ())
                },
                "safe_reason": reason,
            }
        )
    if provider_order is not None:
        by_name = {str(item["source"]): item for item in sources}
        if set(by_name) != set(provider_order):
            raise ValueError("provider preflight source set is incomplete")
        sources = [by_name[name] for name in provider_order]
    payload = {
        "schema_version": "unified-public-data-dry-run/1",
        "run_id": run_id,
        "dry_run": True,
        "sources": sources,
        "candidate": "SKIPPED",
        "qc": "SKIPPED",
        "canonical": "SKIPPED",
        "current_changed": False,
        "production_data_package": "SKIPPED",
        "server_sync": "SKIPPED",
        "prewarm": "SKIPPED",
    }
    if evidence_output is not None:
        atomic_write_json(evidence_output.resolve(), payload)
        print("provider preflight evidence written")
    else:
        # Preserve the existing interactive dry-run contract when an explicit
        # machine channel was not requested.
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    # A classified provider failure is machine evidence, not a process failure.
    # The Production Wrapper decides whether at least one independent provider
    # remains executable; malformed or unwritable evidence still raises.
    return 0


def _lutou_settings(secret_file: str | Path) -> LutouConnectionSettings:
    """Prefer a complete process override, then the persistent local secret."""

    required = ("LUTOU_HOST", "LUTOU_PORT", "LUTOU_USER", "LUTOU_PASSWORD")
    values = {name: os.environ.get(name, "") for name in required}
    try:
        present = tuple(name for name in required if values[name])
        if present:
            if len(present) != len(required):
                raise SystemExit("Lutou process-local connection settings are incomplete")
            try:
                return LutouConnectionSettings(
                    host=values["LUTOU_HOST"],
                    port=int(values["LUTOU_PORT"]),
                    user=values["LUTOU_USER"],
                    password=values["LUTOU_PASSWORD"],
                )
            except (TypeError, ValueError):
                raise SystemExit("Lutou process-local connection settings are invalid") from None
        return LutouConnectionSettings.from_secret_file(secret_file)
    finally:
        for name in values:
            values[name] = ""


def _refresh_domestic_spread_artifact(
    *,
    tankan_secret_file: Path,
    end_date: date,
) -> dict[str, Path]:
    """Run the formal Domestic Spread producer and return its sealed input."""

    command = [
        sys.executable,
        str(ROOT / "04_scripts" / "server_update_spreads.py"),
        "--update-from-tankan",
        "--tankan-secret-file",
        str(tankan_secret_file),
        "--end-date",
        end_date.isoformat(),
    ]
    completed = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise RuntimeError(f"Domestic Spread producer failed{suffix}")
    artifact = ROOT / "01_data" / "historical_spread_database.parquet"
    if not artifact.is_file():
        raise FileNotFoundError("Domestic Spread producer did not create Parquet")
    return {"domestic-spread": artifact}


def _build_remote_syncer(*, ssh_target: str, activation_image_id: str):
    """Adapt the OpenSSH transport result to the daily orchestration contract."""

    def sync(
        package_directory: str | Path,
        *,
        store_root: str | Path,
        pre_switch_validator=None,
        post_switch_validator=None,
        initial_seed: bool = False,
    ) -> ServerSyncResult:
        del pre_switch_validator, post_switch_validator
        command = [
            sys.executable,
            str(ROOT / "04_scripts" / "transfer_public_data_package.py"),
            "--package", str(package_directory),
            "--ssh-target", ssh_target,
            "--remote-store-root", str(store_root),
            "--activation-image-id", activation_image_id,
        ]
        if initial_seed:
            command.append("--initial-seed")
        completed = subprocess.run(
            command, cwd=ROOT, text=True, capture_output=True, check=False
        )
        if completed.returncode != 0:
            if completed.returncode == 124:
                raise ServerTransportTimeout("production package transport timed out")
            if "remote activation failed" in completed.stderr:
                raise ServerActivationFailure("production package remote activation failed")
            raise RuntimeError("production package transport or activation failed")
        try:
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError) as exc:
            raise RuntimeError("production package transport result is invalid") from exc
        status = str(payload.get("status"))
        package_id = str(payload.get("package_id"))
        if payload.get("schema_version") != "public-data-transport/1":
            raise RuntimeError("production package transport schema is invalid")
        if status == "NO_CHANGE":
            return ServerSyncResult(
                status, package_id, "PASS", "PASS", "N/A", "PASS", None
            )
        activation = payload.get("remote_activation")
        if (
            status != "SYNCED"
            or not isinstance(activation, dict)
            or activation.get("status") != "SYNCED"
            or activation.get("package_id") != package_id
            or any(
                activation.get(key) != "PASS"
                for key in (
                    "manifest", "sha", "atomic_switch", "formal_read_validation"
                )
            )
        ):
            raise RuntimeError("production package activation did not succeed")
        return ServerSyncResult(
            status,
            package_id,
            str(activation.get("manifest")),
            str(activation.get("sha")),
            str(activation.get("atomic_switch")),
            str(activation.get("formal_read_validation")),
            None,
            activation.get("safe_reason"),
        )

    return sync


if __name__ == "__main__":
    raise SystemExit(main())
