"""Thin, failure-isolated orchestration for public-data provider refreshes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from agri_research_agent.shared.immutable_candidate import seal_immutable_candidate, validate_candidate_id
from agri_research_agent.shared.runtime_context import RuntimeContext, assert_runtime_write


class ProviderStatus(StrEnum):
    READY = "READY"
    LIVE_VERIFICATION_PENDING = "LIVE_VERIFICATION_PENDING"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
    AUTH_FAILURE = "AUTH_FAILURE"
    SOURCE_SCHEMA_FAILURE = "SOURCE_SCHEMA_FAILURE"
    INGESTION_FAILURE = "INGESTION_FAILURE"
    QC_FAILURE = "QC_FAILURE"
    PROMOTION_FAILURE = "PROMOTION_FAILURE"
    NO_CHANGE = "NO_CHANGE"
    UPDATED = "UPDATED"


class OverallStatus(StrEnum):
    SUCCESS = "SUCCESS"
    SUCCESS_WITH_UNAVAILABLE_SOURCE = "SUCCESS_WITH_UNAVAILABLE_SOURCE"
    NO_CHANGE = "NO_CHANGE"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class CurrentIdentity:
    release_id: str | None
    manifest_sha256: str | None
    source_max_dates: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RefreshResult:
    promoted: bool
    source_max_dates: Mapping[str, str]
    domains: Mapping[str, str] = field(default_factory=dict)
    status: ProviderStatus | None = None
    safe_reason: str | None = None


class ProviderAdapter(Protocol):
    name: str

    def current_identity(self) -> CurrentIdentity: ...
    def preflight(self) -> Mapping[str, object]: ...
    def refresh(self) -> RefreshResult: ...


class ProviderFailure(RuntimeError):
    def __init__(self, status: ProviderStatus, safe_reason: str) -> None:
        if status in {ProviderStatus.READY, ProviderStatus.NO_CHANGE, ProviderStatus.UPDATED}:
            raise ValueError("ProviderFailure requires a failure status")
        super().__init__(safe_reason)
        self.status = status
        self.safe_reason = safe_reason


@dataclass(frozen=True, slots=True)
class ProviderOutcome:
    provider: str
    preflight_status: ProviderStatus
    status: ProviderStatus
    current_before: CurrentIdentity
    current_after: CurrentIdentity
    source_max_dates: Mapping[str, str]
    domains: Mapping[str, str]
    safe_reason: str | None = None


@dataclass(frozen=True, slots=True)
class UnifiedRunResult:
    run_id: str
    started_at: str
    completed_at: str
    requested_providers: tuple[str, ...]
    providers: tuple[ProviderOutcome, ...]
    overall_status: OverallStatus
    run_directory: Path
    manifest: Mapping[str, object]


def run_unified_refresh(
    *,
    runtime: RuntimeContext,
    run_id: str,
    adapters: Sequence[ProviderAdapter],
    report_builder: Callable[[Mapping[str, object]], str] | None = None,
) -> UnifiedRunResult:
    """Run providers independently and seal one immutable orchestration report."""

    safe_run_id = validate_candidate_id(run_id)
    if not adapters:
        raise ValueError("at least one provider is required")
    names = tuple(adapter.name for adapter in adapters)
    if len(names) != len(set(names)):
        raise ValueError("provider names must be unique")
    started = datetime.now(timezone.utc)
    outcomes = tuple(_run_provider(adapter) for adapter in adapters)
    completed = datetime.now(timezone.utc)
    overall = _overall(outcomes)
    manifest: dict[str, object] = {
        "schema_version": "unified-public-data-refresh/1",
        "run_id": safe_run_id,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "requested_providers": list(names),
        "providers": [_outcome_payload(item) for item in outcomes],
        "overall_status": overall.value,
    }
    _reject_sensitive_metadata(manifest)
    root = assert_runtime_write(
        runtime, runtime.runtime_root / "public-data-refresh" / "runs"
    )

    def build(directory: Path) -> None:
        (directory / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        render = report_builder or render_report
        (directory / "report.txt").write_text(render(manifest), encoding="utf-8")

    run_directory, _ = seal_immutable_candidate(root, safe_run_id, build)
    return UnifiedRunResult(
        safe_run_id,
        started.isoformat(),
        completed.isoformat(),
        names,
        outcomes,
        overall,
        run_directory,
        manifest,
    )


def _run_provider(adapter: ProviderAdapter) -> ProviderOutcome:
    try:
        before = adapter.current_identity()
    except Exception as exc:
        missing = CurrentIdentity(None, None, {})
        return ProviderOutcome(
            adapter.name,
            ProviderStatus.PROMOTION_FAILURE,
            ProviderStatus.PROMOTION_FAILURE,
            missing,
            missing,
            {},
            {},
            f"Current identity check failed: {type(exc).__name__}",
        )
    preflight_status = ProviderStatus.READY
    try:
        preflight = adapter.preflight()
    except ProviderFailure as exc:
        after = _safe_current_identity(adapter, before)
        return ProviderOutcome(
            adapter.name, exc.status, exc.status, before, after,
            after.source_max_dates, {}, exc.safe_reason,
        )
    except Exception as exc:
        after = _safe_current_identity(adapter, before)
        return ProviderOutcome(
            adapter.name,
            ProviderStatus.SOURCE_UNAVAILABLE,
            ProviderStatus.SOURCE_UNAVAILABLE,
            before,
            after,
            after.source_max_dates,
            {},
            f"provider preflight failed: {type(exc).__name__}",
        )
    preflight_source_max = {
        str(key): str(value)
        for key, value in dict(preflight.get("source_max_dates", {})).items()
    }
    try:
        refreshed = adapter.refresh()
        status = refreshed.status or (
            ProviderStatus.UPDATED if refreshed.promoted else ProviderStatus.NO_CHANGE
        )
        reason = refreshed.safe_reason
        domains = refreshed.domains
        source_max = refreshed.source_max_dates
    except ProviderFailure as exc:
        status = exc.status
        reason = exc.safe_reason
        domains = {}
        source_max = preflight_source_max
    except Exception as exc:
        status = ProviderStatus.INGESTION_FAILURE
        reason = f"provider refresh failed: {type(exc).__name__}"
        domains = {}
        source_max = preflight_source_max
    after = _safe_current_identity(adapter, before)
    if (
        status not in {ProviderStatus.UPDATED, ProviderStatus.NO_CHANGE}
        and after != before
        and not domains
    ):
        status = ProviderStatus.PROMOTION_FAILURE
        reason = "failed provider changed Current identity"
    return ProviderOutcome(
        adapter.name, preflight_status, status, before, after,
        source_max or after.source_max_dates, domains, reason,
    )


def _safe_current_identity(
    adapter: ProviderAdapter, fallback: CurrentIdentity
) -> CurrentIdentity:
    try:
        return adapter.current_identity()
    except Exception:
        return fallback


def _overall(outcomes: Sequence[ProviderOutcome]) -> OverallStatus:
    statuses = [item.status for item in outcomes]
    success = {ProviderStatus.UPDATED, ProviderStatus.NO_CHANGE}
    unavailable = {
        ProviderStatus.SOURCE_UNAVAILABLE,
        ProviderStatus.NETWORK_UNAVAILABLE,
        ProviderStatus.LIVE_VERIFICATION_PENDING,
    }
    if all(item is ProviderStatus.NO_CHANGE for item in statuses):
        return OverallStatus.NO_CHANGE
    if all(item in success for item in statuses):
        return OverallStatus.SUCCESS
    if any(item in success for item in statuses):
        if all(item in success | unavailable for item in statuses):
            return OverallStatus.SUCCESS_WITH_UNAVAILABLE_SOURCE
        return OverallStatus.PARTIAL_FAILURE
    return OverallStatus.FAILED


def _identity_payload(value: CurrentIdentity) -> dict[str, object]:
    return {
        "release_id": value.release_id,
        "manifest_sha256": value.manifest_sha256,
        "source_max_dates": dict(value.source_max_dates),
    }


def _outcome_payload(value: ProviderOutcome) -> dict[str, object]:
    return {
        "provider": value.provider,
        "preflight_status": value.preflight_status.value,
        "status": value.status.value,
        "source_max_dates": dict(value.source_max_dates),
        "current_before": _identity_payload(value.current_before),
        "current_after": _identity_payload(value.current_after),
        "domains": dict(value.domains),
        "safe_reason": value.safe_reason,
    }


def render_report(manifest: Mapping[str, object]) -> str:
    lines = [
        f"Unified public-data refresh: {manifest['run_id']}",
        f"Overall status: {manifest['overall_status']}",
        f"Started: {manifest['started_at']}",
        f"Completed: {manifest['completed_at']}",
    ]
    for provider in manifest["providers"]:  # type: ignore[assignment]
        item = provider  # type: ignore[assignment]
        lines.append(
            f"{item['provider']}: {item['status']} (preflight={item['preflight_status']})"
        )
        source_max = ", ".join(
            f"{key}={value}" for key, value in item["source_max_dates"].items()
        ) or "none"
        before = item["current_before"]
        after = item["current_after"]
        lines.append(f"  source max: {source_max}")
        lines.append(
            f"  Current: {before['release_id']} ({before['manifest_sha256']})"
            f" -> {after['release_id']} ({after['manifest_sha256']})"
        )
        if item["domains"]:
            domains = ", ".join(
                f"{key}={value}" for key, value in item["domains"].items()
            )
            lines.append(f"  domains: {domains}")
        if item["safe_reason"]:
            lines.append(f"  reason: {item['safe_reason']}")
    return "\n".join(lines) + "\n"


def _reject_sensitive_metadata(payload: object) -> None:
    forbidden = ("password", "passwd", "token", "secret", "credential", "private_key", "dsn")

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if any(item in str(key).lower() for item in forbidden):
                    raise ValueError("unified manifest contains a sensitive field")
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
        elif isinstance(value, str) and ("://" in value or Path(value).is_absolute()):
            raise ValueError("unified manifest contains a forbidden locator")

    visit(payload)


__all__ = [
    "CurrentIdentity", "OverallStatus", "ProviderAdapter", "ProviderFailure",
    "ProviderOutcome", "ProviderStatus", "RefreshResult", "UnifiedRunResult",
    "render_report", "run_unified_refresh",
]
