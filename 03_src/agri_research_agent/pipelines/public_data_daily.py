"""Goal E daily Public Data refresh, package, sync and pre-warm orchestration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from .async_contract_rollout import collect_reports, render_reports, DOMAINS

from agri_research_agent.pipelines.public_data_delivery import (
    PrewarmResult,
    PrewarmStatus,
    PrewarmTarget,
    ProductionPackage,
    ServerSyncResult,
    build_production_package,
    run_prewarm,
    sync_to_local_server_store,
)
from agri_research_agent.pipelines.public_data_refresh import (
    DomainStatus,
    ProviderOutcome,
    ProviderStatus,
    UnifiedRunResult,
)
from agri_research_agent.pipelines.public_data_freshness import ConsumerFreshnessReport
from agri_research_agent.shared.immutable_candidate import (
    seal_immutable_candidate,
    validate_candidate_id,
)
from agri_research_agent.shared.runtime_context import RuntimeContext, assert_runtime_write


class DailyBusinessStatus(StrEnum):
    UPDATED = "UPDATED"
    NO_CHANGE = "NO_CHANGE"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    FAILED = "FAILED"


class DeliveryAction(StrEnum):
    SKIPPED = "SKIPPED"
    STANDARD = "STANDARD"
    INITIAL_SEED = "INITIAL_SEED"


@dataclass(frozen=True, slots=True)
class DailyUpdateResult:
    run_id: str
    business_status: DailyBusinessStatus
    succeeded: bool
    refresh: UnifiedRunResult
    package: ProductionPackage | None
    sync: ServerSyncResult | None
    prewarm: PrewarmResult
    run_directory: Path
    manifest: Mapping[str, Any]


PackageBuilder = Callable[..., ProductionPackage]
Syncer = Callable[..., ServerSyncResult]
Prewarmer = Callable[[Sequence[PrewarmTarget]], PrewarmResult]
FreshnessValidator = Callable[[], ConsumerFreshnessReport]
DeliveryArtifactRunner = Callable[[], Mapping[str, str | Path]]


def run_daily_update(
    *,
    runtime: RuntimeContext,
    run_id: str,
    refresh_runner: Callable[[], UnifiedRunResult],
    public_current_root: str | Path,
    packages_root: str | Path,
    server_store_root: str | Path | None = None,
    required_datasets: Sequence[str] | None = None,
    prewarm_targets: Sequence[PrewarmTarget] = (),
    prewarm_target_factory: Callable[[Path], Sequence[PrewarmTarget]] | None = None,
    pre_switch_validator: Callable[[Path], None] | None = None,
    post_switch_validator: Callable[[Path], None] | None = None,
    consumer_freshness_validator: FreshnessValidator | None = None,
    delivery_artifact_runner: DeliveryArtifactRunner | None = None,
    delivery_artifact_provider: str | None = None,
    preserved_delivery_artifacts: Mapping[str, str | Path] | None = None,
    initial_seed: bool = False,
    package_builder: PackageBuilder = build_production_package,
    syncer: Syncer = sync_to_local_server_store,
    prewarmer: Prewarmer = run_prewarm,
) -> DailyUpdateResult:
    """Execute Goal E and atomically deliver one aggregate data snapshot."""

    safe_run_id = validate_candidate_id(run_id)
    refresh = refresh_runner()
    refresh_status = _business_status(refresh.providers)
    status = refresh_status
    package: ProductionPackage | None = None
    sync: ServerSyncResult | None = None
    prewarm = PrewarmResult(PrewarmStatus.SKIPPED, {})
    package_status = "SKIPPED"
    server_status = "SKIPPED"
    manifest_status = sha_status = switch_status = read_status = "N/A"
    safe_reason: str | None = None
    freshness: ConsumerFreshnessReport | None = None
    freshness_payload: dict[str, Any] = {"status": "SKIPPED", "results": []}
    delivery_artifacts: Mapping[str, str | Path] | None = None
    delivery_artifact_status = "SKIPPED"
    delivery_action = DeliveryAction.SKIPPED
    delivery_identity: str | None = None
    aggregate_unchanged = False

    partial_statuses = {
        DailyBusinessStatus.UPDATED,
        DailyBusinessStatus.NO_CHANGE,
        DailyBusinessStatus.PARTIAL_SUCCESS,
    }
    if refresh_status in partial_statuses:
        if delivery_artifact_runner is not None:
            provider_ready = (
                delivery_artifact_provider is None
                or _provider_completed(refresh.providers, delivery_artifact_provider)
            )
            if not provider_ready:
                delivery_artifacts = dict(preserved_delivery_artifacts or {})
                if delivery_artifacts:
                    delivery_artifact_status = "PRESERVED"
                    status = DailyBusinessStatus.PARTIAL_SUCCESS
                else:
                    status = DailyBusinessStatus.FAILED
                    delivery_artifact_status = "FAIL"
                    safe_reason = "delivery artifact dependency failed and no trusted baseline exists"
            else:
                try:
                    delivery_artifacts = delivery_artifact_runner()
                    if not delivery_artifacts:
                        raise ValueError("delivery artifact runner returned no artifacts")
                    delivery_artifact_status = "PASS"
                except Exception as exc:
                    delivery_artifacts = dict(preserved_delivery_artifacts or {})
                    if delivery_artifacts:
                        status = DailyBusinessStatus.PARTIAL_SUCCESS
                        delivery_artifact_status = "ERROR_PRESERVED"
                        safe_reason = (
                            f"delivery artifact producer failed: {type(exc).__name__}; "
                            "trusted baseline preserved"
                        )
                    else:
                        status = DailyBusinessStatus.FAILED
                        delivery_artifact_status = "FAIL"
                        safe_reason = f"delivery artifact producer failed: {type(exc).__name__}"

    has_delivery_baseline = _has_delivery_package(packages_root)
    refresh_changed = any(
        item.current_before != item.current_after for item in refresh.providers
    )
    should_package = (
        status in {DailyBusinessStatus.UPDATED, DailyBusinessStatus.PARTIAL_SUCCESS}
        or (
            status is DailyBusinessStatus.NO_CHANGE
            and delivery_artifact_runner is not None
            and delivery_artifact_status == "PASS"
            and (initial_seed or has_delivery_baseline)
        )
    )
    if should_package:
        if initial_seed and status is DailyBusinessStatus.NO_CHANGE:
            delivery_action = DeliveryAction.INITIAL_SEED
        if initial_seed and consumer_freshness_validator is None:
            freshness_payload = {"status": "STALE", "results": []}
            safe_reason = "consumer freshness validation is required for initial seed"
        elif consumer_freshness_validator is not None:
            try:
                freshness = consumer_freshness_validator()
                freshness_payload = freshness.as_dict()
                if not freshness.hard_pass:
                    safe_reason = "consumer freshness validation failed"
            except Exception as exc:
                freshness_payload = {"status": "STALE", "results": []}
                safe_reason = f"consumer freshness validation failed: {type(exc).__name__}"
        if freshness_payload["status"] != "STALE":
            try:
                package = package_builder(
                    public_current_root=public_current_root,
                    packages_root=packages_root,
                    source_max_dates=_source_max_dates(refresh.providers),
                    required_datasets=required_datasets,
                    delivery_artifacts=delivery_artifacts,
                )
                package_status = (
                    "GENERATED" if package.created or refresh_changed else "NO_CHANGE"
                )
                delivery_identity = package.manifest.get("delivery_identity_sha256")
                if status in {
                    DailyBusinessStatus.UPDATED,
                    DailyBusinessStatus.PARTIAL_SUCCESS,
                } and package.created:
                    delivery_action = DeliveryAction.STANDARD
                elif initial_seed:
                    package_status = "GENERATED" if package.created else "REUSED"
                elif package.created:
                    delivery_action = DeliveryAction.STANDARD
            except Exception as exc:
                status = DailyBusinessStatus.FAILED
                package_status = "FAILED"
                safe_reason = f"production package failed: {type(exc).__name__}"
        aggregate_unchanged = (
            package is not None
            and not package.created
            and not refresh_changed
            and not initial_seed
        )
        if aggregate_unchanged:
            # A content-addressed delivery aggregate already exists locally;
            # NO_CHANGE must not contact or mutate the production server.
            server_status = "SKIPPED"
            package_status = "SKIPPED"
            package = None
        elif package is not None and server_store_root is not None:
            try:
                seed_action = delivery_action is DeliveryAction.INITIAL_SEED
                sync = syncer(
                    package.directory,
                    store_root=server_store_root,
                    pre_switch_validator=pre_switch_validator,
                    post_switch_validator=post_switch_validator,
                    initial_seed=seed_action,
                )
                server_status = sync.status
                manifest_status = sync.manifest
                sha_status = sync.sha
                switch_status = sync.atomic_switch
                read_status = sync.formal_read_validation
                if sync.status == "SYNCED":
                    if not seed_action and status is not DailyBusinessStatus.PARTIAL_SUCCESS:
                        status = DailyBusinessStatus.UPDATED
                    targets = prewarm_targets
                    if prewarm_target_factory is not None and sync.current_directory is not None:
                        targets = prewarm_target_factory(sync.current_directory / "data")
                    prewarm = prewarmer(targets)
                else:
                    status = DailyBusinessStatus.FAILED
                    safe_reason = sync.safe_reason
            except Exception as exc:
                status = DailyBusinessStatus.FAILED
                server_status = "FAILED"
                safe_reason = f"server sync failed: {type(exc).__name__}"
        elif package is not None:
            server_status = "SKIPPED"
            if package.created and status is not DailyBusinessStatus.PARTIAL_SUCCESS:
                status = DailyBusinessStatus.UPDATED

    if (
        refresh_status is DailyBusinessStatus.NO_CHANGE
        and status is not DailyBusinessStatus.FAILED
        and delivery_action is not DeliveryAction.STANDARD
    ):
        status = DailyBusinessStatus.NO_CHANGE

    seed_action = delivery_action is DeliveryAction.INITIAL_SEED
    succeeded = status not in {DailyBusinessStatus.FAILED} and (
        not should_package
        or aggregate_unchanged
        or (
            freshness_payload["status"] != "STALE"
            and package_status in {"GENERATED", "NO_CHANGE", "REUSED"}
            and server_status != "FAILED"
            and (not seed_action or server_status == "SYNCED")
        )
    )
    now = datetime.now(timezone.utc).isoformat()
    current_vector = _current_vector(refresh.providers)
    current_identity = _json_sha256(current_vector)
    payload: dict[str, Any] = {
        "schema_version": "unified-public-data-daily-update/1",
        "run_id": safe_run_id,
        "completed_at": now,
        "business_status": status.value,
        "aggregate_status": (
            DailyBusinessStatus.PARTIAL_SUCCESS.value
            if status is DailyBusinessStatus.PARTIAL_SUCCESS
            else refresh.manifest.get("aggregate_status", refresh.overall_status.value)
        ),
        "root_failure": (
            refresh.manifest.get("root_failure")
            if "root_failure" in refresh.manifest
            else (
                None
                if refresh.root_failure is None
                else refresh.root_failure.as_dict()
            )
        ),
        "transaction": refresh.manifest.get(
            "transaction",
            refresh.transaction or {
                "current_changed_before_rollback": False,
                "rollback": "NOT_REQUIRED",
                "rollback_failure": None,
            },
        ),
        "succeeded": succeeded,
        "sources": [_source_payload(item) for item in refresh.providers],
        "source_max_dates": _source_max_dates(refresh.providers),
        "candidate": _stage_summary(refresh.providers, "candidate"),
        "qc": _stage_summary(refresh.providers, "qc"),
        "canonical": _stage_summary(refresh.providers, "canonical"),
        "public_current_identity": current_identity,
        "public_current_vector": current_vector,
        "current_changed": refresh_changed,
        "delivery_identity": delivery_identity,
        "delivery_action": delivery_action.value,
        "delivery_changed": bool(
            package
            and (
                package.created
                or (sync is not None and sync.status == "SYNCED")
            )
        ),
        "delivery_artifact_producer": delivery_artifact_status,
        "consumer_freshness_validation": freshness_payload,
        "production_data_package": {
            "status": package_status,
            "package_id": package.package_id if package else None,
        },
        "server_sync": server_status,
        "manifest": manifest_status,
        "sha": sha_status,
        "atomic_current_switch": switch_status,
        "formal_read_validation": read_status,
        "prewarm": {
            "status": prewarm.status.value,
            "targets": dict(prewarm.targets),
        },
        "safe_reason": safe_reason,
    }
    payload["async_updates"] = collect_reports(refresh.providers)
    payload["async_summary_complete"] = set((*DOMAINS, "domestic_basis")) <= set(payload["async_updates"])
    payload["dataset_update_summary"] = {
        name: {"status": report["dataset_status"], **report["summary"]}
        for name, report in payload["async_updates"].items()
    }
    payload["summary"] = render_final_line(payload) + "\n" + "\n".join(render_reports(payload["async_updates"]))
    root = assert_runtime_write(runtime, runtime.runtime_root / "public-data-daily" / "runs")

    def build(directory: Path) -> None:
        (directory / "manifest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (directory / "report.txt").write_text(render_daily_report(payload), encoding="utf-8")

    run_directory, _ = seal_immutable_candidate(root, safe_run_id, build)
    return DailyUpdateResult(
        safe_run_id, status, succeeded, refresh, package, sync, prewarm,
        run_directory, payload,
    )


def render_final_line(manifest: Mapping[str, Any]) -> str:
    status = str(manifest["business_status"])
    if status == DailyBusinessStatus.NO_CHANGE.value:
        return (
            "NO_CHANGE | Delivery aggregate unchanged | "
            f"Delivery action={manifest.get('delivery_action', 'SKIPPED')} | "
            f"Production package={manifest['production_data_package']['status']} | "
            f"Server={manifest['server_sync']}"
        )
    if status == DailyBusinessStatus.SOURCE_UNAVAILABLE.value:
        unavailable = ",".join(
            str(item["source"]) for item in manifest["sources"]
            if item["status"] in {
                ProviderStatus.SOURCE_UNAVAILABLE.value,
                ProviderStatus.NETWORK_UNAVAILABLE.value,
                ProviderStatus.LIVE_VERIFICATION_PENDING.value,
            }
        )
        return f"SOURCE_UNAVAILABLE | {unavailable} unavailable | Current preserved | Server=SKIPPED"
    if status == DailyBusinessStatus.FAILED.value:
        return "FAILED | Current/package publication blocked | Server=SKIPPED"
    if status == DailyBusinessStatus.PARTIAL_SUCCESS.value:
        completed = ",".join(
            str(item["source"])
            for item in manifest["sources"]
            if any(
                value in {DomainStatus.UPDATED.value, DomainStatus.NO_CHANGE.value}
                for value in item.get("domains", {}).values()
            )
        )
        incomplete = ",".join(
            str(item["source"])
            for item in manifest["sources"]
            if any(
                value not in {DomainStatus.UPDATED.value, DomainStatus.NO_CHANGE.value}
                for value in item.get("domains", {}).values()
            )
        )
        return (
            f"PARTIAL_SUCCESS | completed={completed or 'none'} | "
            f"incomplete={incomplete or 'none'} | Server={manifest['server_sync']}"
        )
    latest = " | ".join(
        f"{key} latest={value}" for key, value in manifest["source_max_dates"].items()
    )
    prefix = f"UPDATED | {latest} | " if latest else "UPDATED | "
    freshness = manifest.get("consumer_freshness_validation", {})
    freshness_parts = [
        f"{item['consumer']} latest={item['latest_business_date']} | {item['status']}"
        for item in freshness.get("results", [])
    ]
    freshness_text = (
        "Consumer freshness=SKIPPED"
        if freshness.get("status") == "SKIPPED"
        else " ; ".join(freshness_parts) or f"Consumer freshness={freshness.get('status', 'STALE')}"
    )
    return (
        f"{prefix}{freshness_text} | Current={manifest['public_current_identity']} | "
        f"Delivery={manifest['delivery_identity']} | "
        f"Delivery action={manifest.get('delivery_action', 'SKIPPED')} | "
        f"Server={manifest['server_sync']} | Manifest={manifest['manifest']} | "
        f"Pre-warm={manifest['prewarm']['status']}"
    )


def render_daily_report(manifest: Mapping[str, Any]) -> str:
    lines = [
        f"Overall status: {manifest['business_status']}",
        f"Succeeded: {manifest['succeeded']}",
        f"Candidate/QC/Canonical: {manifest['candidate']}/{manifest['qc']}/{manifest['canonical']}",
        f"Public Current identity: {manifest['public_current_identity']}",
        f"Current changed: {manifest['current_changed']}",
        f"Delivery identity: {manifest['delivery_identity']}",
        f"Delivery action: {manifest.get('delivery_action', 'SKIPPED')}",
        f"Delivery changed: {manifest['delivery_changed']}",
        f"Delivery artifact producer: {manifest['delivery_artifact_producer']}",
        f"Consumer Freshness Validation: {manifest['consumer_freshness_validation']['status']}",
        *[
            f"{item['consumer']} latest={item['latest_business_date']} | {item['status']}"
            for item in manifest["consumer_freshness_validation"]["results"]
        ],
        f"Production Data Package: {manifest['production_data_package']['status']}",
        f"Server sync: {manifest['server_sync']}",
        f"Manifest: {manifest['manifest']}",
        f"SHA: {manifest['sha']}",
        f"Atomic Current Switch: {manifest['atomic_current_switch']}",
        f"Formal read validation: {manifest['formal_read_validation']}",
        f"Pre-warm: {manifest['prewarm']['status']}",
        "",
        str(manifest["summary"]),
    ]
    return "\n".join(lines) + "\n"


def _business_status(outcomes: Sequence[ProviderOutcome]) -> DailyBusinessStatus:
    domain_statuses: list[str] = []
    for outcome in outcomes:
        if outcome.domains:
            domain_statuses.extend(str(value) for value in outcome.domains.values())
        elif outcome.status in {ProviderStatus.UPDATED, ProviderStatus.NO_CHANGE}:
            domain_statuses.append(outcome.status.value)
        else:
            domain_statuses.append(DomainStatus.ERROR.value)
    completed = {
        DomainStatus.UPDATED.value,
        DomainStatus.NO_CHANGE.value,
    }
    completed_count = sum(value in completed for value in domain_statuses)
    incomplete_count = len(domain_statuses) - completed_count
    if completed_count and incomplete_count:
        return DailyBusinessStatus.PARTIAL_SUCCESS
    if not completed_count:
        return DailyBusinessStatus.FAILED
    if DomainStatus.UPDATED.value in domain_statuses:
        return DailyBusinessStatus.UPDATED
    return DailyBusinessStatus.NO_CHANGE


def _provider_completed(outcomes: Sequence[ProviderOutcome], provider: str) -> bool:
    return any(
        item.provider == provider
        and item.status in {ProviderStatus.UPDATED, ProviderStatus.NO_CHANGE}
        for item in outcomes
    )


def _has_delivery_package(packages_root: str | Path) -> bool:
    root = Path(packages_root)
    return root.is_dir() and any(
        path.is_dir() and path.name.startswith("public-current-")
        for path in root.iterdir()
    )


def _source_payload(outcome: ProviderOutcome) -> dict[str, Any]:
    stages = _provider_stages(outcome.status)
    return {
        "source": outcome.provider,
        "read": outcome.preflight_status.value,
        "status": outcome.status.value,
        "source_max_dates": dict(outcome.source_max_dates),
        "candidate": stages[0],
        "qc": stages[1],
        "canonical": stages[2],
        "current_before": _identity_payload(outcome.current_before),
        "current_after": _identity_payload(outcome.current_after),
        "current_changed": outcome.current_before != outcome.current_after,
        "domains": dict(outcome.domains),
        "safe_reason": outcome.safe_reason,
        "root_failure": (
            None if outcome.root_failure is None else outcome.root_failure.as_dict()
        ),
    }


def _provider_stages(status: ProviderStatus) -> tuple[str, str, str]:
    if status in {ProviderStatus.UPDATED, ProviderStatus.NO_CHANGE}:
        return "PASS", "PASS", "PASS"
    if status is ProviderStatus.QC_FAILURE:
        return "PASS", "FAIL", "N/A"
    if status in {ProviderStatus.INGESTION_FAILURE, ProviderStatus.SOURCE_SCHEMA_FAILURE}:
        return "FAIL", "N/A", "N/A"
    if status in {ProviderStatus.PROMOTION_FAILURE}:
        return "PASS", "PASS", "PASS"
    return "N/A", "N/A", "N/A"


def _stage_summary(outcomes: Sequence[ProviderOutcome], stage: str) -> str:
    values = [_source_payload(item)[stage] for item in outcomes]
    if "FAIL" in values:
        return "FAIL"
    if "PASS" in values:
        return "PASS"
    return "N/A"


def _source_max_dates(outcomes: Sequence[ProviderOutcome]) -> dict[str, str]:
    output: dict[str, str] = {}
    for outcome in outcomes:
        for domain, value in outcome.source_max_dates.items():
            output[f"{outcome.provider}.{domain}"] = value
    return dict(sorted(output.items()))


def _current_vector(outcomes: Sequence[ProviderOutcome]) -> dict[str, Any]:
    return {
        item.provider: _formal_identity_payload(item.current_after)
        for item in sorted(outcomes, key=lambda value: value.provider)
    }


def _formal_identity_payload(value: object) -> dict[str, Any]:
    return {
        "release_id": getattr(value, "release_id"),
        "manifest_sha256": getattr(value, "manifest_sha256"),
        "dataset_identities": {
            name: dict(identity)
            for name, identity in sorted(
                getattr(value, "dataset_identities", {}).items()
            )
        },
    }


def _identity_payload(value: object) -> dict[str, Any]:
    return {
        "release_id": getattr(value, "release_id"),
        "manifest_sha256": getattr(value, "manifest_sha256"),
        "source_max_dates": dict(getattr(value, "source_max_dates")),
        "dataset_identities": {
            name: dict(identity)
            for name, identity in sorted(
                getattr(value, "dataset_identities", {}).items()
            )
        },
    }


def _json_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "DailyBusinessStatus", "DailyUpdateResult", "DeliveryAction", "render_daily_report",
    "render_final_line", "run_daily_update",
]
