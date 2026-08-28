#!/usr/bin/env python
"""CLI entrypoint for the isolated unified public-data refresh."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
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
    DomesticBasisRefreshAdapter,
    LutouRefreshAdapter,
    TankanRefreshAdapter,
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
)
from agri_research_agent.pipelines.public_data_refresh import (
    ProviderFailure,
    ProviderStatus,
    run_unified_refresh,
)
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


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
                _lutou_settings(args.lutou_secret_file),
                runtime,
                run_id,
                args.end_date,
                ROOT / "02_configs" / "public_research_data_catalog.candidate.json",
                ROOT / "02_configs" / "international_three_oil_v1.sealed.json",
                ROOT / "02_configs" / "lutou_weather_current.yaml",
                weather_baseline_root,
            )
        )
        adapters.append(
            DomesticBasisRefreshAdapter(
                _lutou_settings(args.lutou_secret_file),
                runtime,
                run_id,
                ROOT / "02_configs" / "lutou_domestic_basis.yaml",
            )
        )
    if args.dry_run:
        return _dry_run(adapters)
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
            require_all_sources=True,
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
        initial_seed=args.initial_seed,
    )
    print(f"run_id={result.run_id}")
    print(f"overall_status={result.business_status.value}")
    for provider in result.refresh.providers:
        print(f"provider={provider.provider} status={provider.status.value}")
    print(result.manifest["summary"])
    return 0 if result.succeeded else 1


def _dry_run(adapters: list[object]) -> int:
    sources: list[dict[str, object]] = []
    failed = False
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
            failed = failed or status not in {
                ProviderStatus.SOURCE_UNAVAILABLE,
                ProviderStatus.NETWORK_UNAVAILABLE,
                ProviderStatus.LIVE_VERIFICATION_PENDING,
            }
        except Exception as exc:
            status = ProviderStatus.INGESTION_FAILURE
            reason = f"dry-run preflight failed: {type(exc).__name__}"
            source_max = {}
            failed = True
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
        sources.append(
            {
                "source": name,
                "status": status.value,
                "source_max_dates": source_max,
                "current_identity": None if identity is None else {
                    "release_id": identity.release_id,
                    "manifest_sha256": identity.manifest_sha256,
                },
                "safe_reason": reason,
            }
        )
    payload = {
        "schema_version": "unified-public-data-dry-run/1",
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
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 1 if failed else 0


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
