"""Thin, failure-isolated orchestration for public-data provider refreshes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Callable, Mapping, Protocol, Sequence

from agri_research_agent.shared.immutable_candidate import seal_immutable_candidate, validate_candidate_id
from agri_research_agent.shared.atomic_storage import atomic_write_bytes
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
class RootFailure:
    provider: str
    domain: str | None
    stage: str
    exception_type: str
    underlying_exception_type: str | None
    safe_message: str
    cleanup_failure: Mapping[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "provider": self.provider,
            "domain": self.domain,
            "stage": self.stage,
            "exception_type": self.exception_type,
            "underlying_exception_type": self.underlying_exception_type,
            "safe_message": self.safe_message,
        }
        if self.cleanup_failure is not None:
            value["cleanup_failure"] = dict(self.cleanup_failure)
        return value


@dataclass(frozen=True, slots=True)
class RefreshResult:
    promoted: bool
    source_max_dates: Mapping[str, str]
    domains: Mapping[str, str] = field(default_factory=dict)
    status: ProviderStatus | None = None
    safe_reason: str | None = None
    performance: Mapping[str, object] = field(default_factory=dict)
    root_failure: RootFailure | None = None


class ProviderAdapter(Protocol):
    name: str

    def current_identity(self) -> CurrentIdentity: ...
    def preflight(self) -> Mapping[str, object]: ...
    def refresh(self) -> RefreshResult: ...


class ProviderFailure(RuntimeError):
    def __init__(
        self,
        status: ProviderStatus,
        safe_reason: str,
        *,
        root_failure: RootFailure | None = None,
    ) -> None:
        if status in {ProviderStatus.READY, ProviderStatus.NO_CHANGE, ProviderStatus.UPDATED}:
            raise ValueError("ProviderFailure requires a failure status")
        super().__init__(safe_reason)
        self.status = status
        self.safe_reason = safe_reason
        self.root_failure = root_failure


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
    performance: Mapping[str, object] = field(default_factory=dict)
    root_failure: RootFailure | None = None


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
    root_failure: RootFailure | None = None
    transaction: Mapping[str, object] = field(default_factory=dict)


def run_unified_refresh(
    *,
    runtime: RuntimeContext,
    run_id: str,
    adapters: Sequence[ProviderAdapter],
    report_builder: Callable[[Mapping[str, object]], str] | None = None,
    require_all_sources: bool = False,
) -> UnifiedRunResult:
    """Run providers independently and seal one immutable orchestration report."""

    safe_run_id = validate_candidate_id(run_id)
    if not adapters:
        raise ValueError("at least one provider is required")
    names = tuple(adapter.name for adapter in adapters)
    if len(names) != len(set(names)):
        raise ValueError("provider names must be unique")
    started = datetime.now(timezone.utc)
    pointer_snapshot = _snapshot_current_pointers(runtime) if require_all_sources else None
    outcomes = (
        _run_all_required(adapters)
        if require_all_sources
        else tuple(_run_provider(adapter) for adapter in adapters)
    )
    root_failure = next(
        (item.root_failure for item in outcomes if item.root_failure is not None), None
    )
    transaction: dict[str, object] = {
        "current_changed_before_rollback": any(
            item.current_before != item.current_after for item in outcomes
        ),
        "rollback": "NOT_REQUIRED",
        "rollback_failure": None,
    }
    if require_all_sources and pointer_snapshot is not None and any(
        item.status not in {ProviderStatus.UPDATED, ProviderStatus.NO_CHANGE}
        for item in outcomes
    ):
        outcomes, rollback_failure = _restore_current_pointers(
            runtime, adapters, outcomes, pointer_snapshot
        )
        transaction["rollback"] = "FAIL" if rollback_failure else "PASS"
        transaction["rollback_failure"] = (
            None if rollback_failure is None else rollback_failure.as_dict()
        )
    completed = datetime.now(timezone.utc)
    overall = _overall(outcomes)
    aggregate = (
        OverallStatus.FAILED
        if require_all_sources
        and any(
            item.status not in {ProviderStatus.UPDATED, ProviderStatus.NO_CHANGE}
            for item in outcomes
        )
        else overall
    )
    manifest: dict[str, object] = {
        "schema_version": "unified-public-data-refresh/1",
        "run_id": safe_run_id,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "requested_providers": list(names),
        "providers": [_outcome_payload(item) for item in outcomes],
        "overall_status": overall.value,
        "aggregate_status": aggregate.value,
        "root_failure": None if root_failure is None else root_failure.as_dict(),
        "transaction": transaction,
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
        root_failure,
        transaction,
    )


def _run_provider(adapter: ProviderAdapter) -> ProviderOutcome:
    try:
        before = adapter.current_identity()
    except Exception as exc:
        missing = CurrentIdentity(None, None, {})
        return ProviderOutcome(
            adapter.name, ProviderStatus.PROMOTION_FAILURE,
            ProviderStatus.PROMOTION_FAILURE, missing, missing, {}, {},
            f"Current identity check failed: {type(exc).__name__}",
            root_failure=root_failure_from_exception(
                adapter.name, None, "PROMOTION", exc
            ),
        )
    preflight_started = perf_counter()
    try:
        preflight = adapter.preflight()
    except ProviderFailure as exc:
        after = _safe_current_identity(adapter, before)
        return ProviderOutcome(
            adapter.name, exc.status, exc.status, before, after,
            after.source_max_dates, {}, exc.safe_reason,
            _provider_performance(perf_counter() - preflight_started),
            exc.root_failure or root_failure_from_exception(
                adapter.name, None, "PROVIDER_PREFLIGHT", exc
            ),
        )
    except Exception as exc:
        after = _safe_current_identity(adapter, before)
        return ProviderOutcome(
            adapter.name, ProviderStatus.SOURCE_UNAVAILABLE,
            ProviderStatus.SOURCE_UNAVAILABLE, before, after,
            after.source_max_dates, {},
            f"provider preflight failed: {type(exc).__name__}",
            _provider_performance(perf_counter() - preflight_started),
            root_failure_from_exception(
                adapter.name, None, "PROVIDER_PREFLIGHT", exc
            ),
        )
    preflight_duration = perf_counter() - preflight_started
    preflight_source_max = {
        str(key): str(value)
        for key, value in dict(preflight.get("source_max_dates", {})).items()
    }
    refresh_started = perf_counter()
    details: Mapping[str, object] = {}
    root_failure: RootFailure | None = None
    try:
        refreshed = adapter.refresh()
        from .async_contract_rollout import validate_provider_reports
        validate_provider_reports(refreshed.performance)
        details = refreshed.performance
        status = refreshed.status or (
            ProviderStatus.UPDATED if refreshed.promoted else ProviderStatus.NO_CHANGE
        )
        reason, domains = refreshed.safe_reason, refreshed.domains
        source_max, root_failure = refreshed.source_max_dates, refreshed.root_failure
    except ProviderFailure as exc:
        status, reason, domains = exc.status, exc.safe_reason, {}
        source_max = preflight_source_max
        root_failure = exc.root_failure or root_failure_from_exception(
            adapter.name, None, "REFRESH", exc
        )
    except Exception as exc:
        status = ProviderStatus.INGESTION_FAILURE
        reason = f"provider refresh failed: {type(exc).__name__}"
        domains, source_max = {}, preflight_source_max
        root_failure = root_failure_from_exception(
            adapter.name, None, "REFRESH", exc
        )
    after = _safe_current_identity(adapter, before)
    status, reason = _reconcile_status_with_identity(status, reason, before, after)
    if status is ProviderStatus.PROMOTION_FAILURE and root_failure is None:
        root_failure = RootFailure(
            adapter.name, None, "PROMOTION", "PromotionContractFailure", None,
            reason or "provider promotion contract failed",
        )
    return ProviderOutcome(
        adapter.name, ProviderStatus.READY, status, before, after,
        source_max or after.source_max_dates, domains, reason,
        _provider_performance(
            preflight_duration, perf_counter() - refresh_started, details
        ),
        root_failure,
    )


def _run_all_required(adapters: Sequence[ProviderAdapter]) -> tuple[ProviderOutcome, ...]:
    """Preflight every source before allowing any Current-producing refresh."""

    prepared: dict[
        str, tuple[ProviderAdapter, CurrentIdentity, Mapping[str, str], float]
    ] = {}
    blocked: dict[str, ProviderOutcome] = {}
    for adapter in adapters:
        try:
            before = adapter.current_identity()
        except Exception as exc:
            missing = CurrentIdentity(None, None, {})
            blocked[adapter.name] = ProviderOutcome(
                adapter.name, ProviderStatus.PROMOTION_FAILURE,
                ProviderStatus.PROMOTION_FAILURE, missing, missing, {}, {},
                f"Current identity check failed: {type(exc).__name__}",
                root_failure=root_failure_from_exception(
                    adapter.name, None, "PROMOTION", exc
                ),
            )
            continue
        preflight_started = perf_counter()
        try:
            preflight = adapter.preflight()
            preflight_duration = perf_counter() - preflight_started
            source_max = {
                str(key): str(value)
                for key, value in dict(preflight.get("source_max_dates", {})).items()
            }
            prepared[adapter.name] = (
                adapter, before, source_max, preflight_duration
            )
        except ProviderFailure as exc:
            after = _safe_current_identity(adapter, before)
            blocked[adapter.name] = ProviderOutcome(
                adapter.name, exc.status, exc.status, before, after,
                after.source_max_dates, {}, exc.safe_reason,
                _provider_performance(perf_counter() - preflight_started),
                exc.root_failure or root_failure_from_exception(
                    adapter.name, None, "PROVIDER_PREFLIGHT", exc
                ),
            )
        except Exception as exc:
            after = _safe_current_identity(adapter, before)
            blocked[adapter.name] = ProviderOutcome(
                adapter.name, ProviderStatus.SOURCE_UNAVAILABLE,
                ProviderStatus.SOURCE_UNAVAILABLE, before, after,
                after.source_max_dates, {},
                f"provider preflight failed: {type(exc).__name__}",
                _provider_performance(perf_counter() - preflight_started),
                root_failure_from_exception(
                    adapter.name, None, "PROVIDER_PREFLIGHT", exc
                ),
            )
    if blocked:
        outcomes: list[ProviderOutcome] = []
        for adapter in adapters:
            if adapter.name in blocked:
                outcomes.append(blocked[adapter.name])
            else:
                _, before, source_max, preflight_duration = prepared[adapter.name]
                outcomes.append(
                    ProviderOutcome(
                        adapter.name, ProviderStatus.READY, ProviderStatus.NO_CHANGE,
                        before, before, source_max or before.source_max_dates, {},
                        "refresh skipped because another required source is unavailable",
                        _provider_performance(preflight_duration),
                    )
                )
            _close_adapter(adapter)
        return tuple(outcomes)
    return tuple(
        _refresh_preflighted(adapter, before, source_max, preflight_duration)
        for adapter, before, source_max, preflight_duration in prepared.values()
    )


def _refresh_preflighted(
    adapter: ProviderAdapter,
    before: CurrentIdentity,
    preflight_source_max: Mapping[str, str],
    preflight_duration: float,
) -> ProviderOutcome:
    refresh_started = perf_counter()
    details: Mapping[str, object] = {}
    root_failure: RootFailure | None = None
    try:
        refreshed = adapter.refresh()
        from .async_contract_rollout import validate_provider_reports
        validate_provider_reports(refreshed.performance)
        details = refreshed.performance
        status = refreshed.status or (
            ProviderStatus.UPDATED if refreshed.promoted else ProviderStatus.NO_CHANGE
        )
        reason = refreshed.safe_reason
        domains = refreshed.domains
        source_max = refreshed.source_max_dates
        root_failure = refreshed.root_failure
    except ProviderFailure as exc:
        status, reason, domains = exc.status, exc.safe_reason, {}
        source_max = preflight_source_max
        root_failure = exc.root_failure or root_failure_from_exception(
            adapter.name, None, "REFRESH", exc
        )
    except Exception as exc:
        status = ProviderStatus.INGESTION_FAILURE
        reason = f"provider refresh failed: {type(exc).__name__}"
        domains, source_max = {}, preflight_source_max
        root_failure = root_failure_from_exception(
            adapter.name, None, "REFRESH", exc
        )
    after = _safe_current_identity(adapter, before)
    status, reason = _reconcile_status_with_identity(status, reason, before, after)
    if status is ProviderStatus.PROMOTION_FAILURE and root_failure is None:
        root_failure = RootFailure(
            adapter.name, None, "PROMOTION", "PromotionContractFailure", None,
            reason or "provider promotion contract failed",
        )
    return ProviderOutcome(
        adapter.name, ProviderStatus.READY, status, before, after,
        source_max or after.source_max_dates, domains, reason,
        _provider_performance(
            preflight_duration, perf_counter() - refresh_started, details
        ),
        root_failure,
    )


def _provider_performance(
    preflight_duration: float,
    refresh_duration: float | None = None,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "provider-performance-telemetry/1",
        "stages": {
            "preflight": {"duration_seconds": round(preflight_duration, 6)},
            "refresh": (
                {"status": "SKIPPED"}
                if refresh_duration is None
                else {"duration_seconds": round(refresh_duration, 6)}
            ),
        },
        "provider_details": dict(details or {}),
    }


def _reconcile_status_with_identity(
    status: ProviderStatus,
    reason: str | None,
    before: CurrentIdentity,
    after: CurrentIdentity,
) -> tuple[ProviderStatus, str | None]:
    if status is ProviderStatus.UPDATED and after == before:
        return ProviderStatus.NO_CHANGE, "provider reported promotion but Current identity is unchanged"
    if status is ProviderStatus.NO_CHANGE and after != before:
        return ProviderStatus.PROMOTION_FAILURE, "provider changed Current while reporting NO_CHANGE"
    return status, reason


def _close_adapter(adapter: ProviderAdapter) -> None:
    close = getattr(adapter, "close", None)
    if callable(close):
        close()


def _snapshot_current_pointers(runtime: RuntimeContext) -> dict[Path, bytes]:
    public_root = runtime.runtime_root / "public-market-data"
    if not public_root.is_dir():
        return {}
    return {
        path.relative_to(runtime.runtime_root): path.read_bytes()
        for path in public_root.glob("*/current.json")
        if path.is_file() and not path.is_symlink()
    }


def _restore_current_pointers(
    runtime: RuntimeContext,
    adapters: Sequence[ProviderAdapter],
    outcomes: tuple[ProviderOutcome, ...],
    snapshot: Mapping[Path, bytes],
) -> tuple[tuple[ProviderOutcome, ...], RootFailure | None]:
    public_root = runtime.runtime_root / "public-market-data"
    current_paths = {
        path.relative_to(runtime.runtime_root): path
        for path in public_root.glob("*/current.json")
        if path.is_file() and not path.is_symlink()
    } if public_root.is_dir() else {}
    try:
        for relative, payload in snapshot.items():
            target = assert_runtime_write(runtime, runtime.runtime_root / relative)
            atomic_write_bytes(target, payload)
        for relative, target in current_paths.items():
            if relative not in snapshot:
                assert_runtime_write(runtime, target).unlink(missing_ok=True)
    except Exception as exc:
        return outcomes, root_failure_from_exception(
            "transaction", None, "ROLLBACK", exc
        )
    refreshed: list[ProviderOutcome] = []
    for adapter, outcome in zip(adapters, outcomes, strict=True):
        after = _safe_current_identity(adapter, outcome.current_before)
        status = outcome.status
        reason = outcome.safe_reason
        if status is ProviderStatus.UPDATED and after == outcome.current_before:
            status = ProviderStatus.NO_CHANGE
            reason = "Current update rolled back because another required source failed"
        refreshed.append(
            replace(outcome, status=status, current_after=after, safe_reason=reason)
        )
    return tuple(refreshed), None


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
        "performance": dict(value.performance),
        "root_failure": (
            None if value.root_failure is None else value.root_failure.as_dict()
        ),
    }


_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:[a-z]:\\[^\s;]+)")
_UNIX_ABSOLUTE_PATH = re.compile(r"(?<!\w)/(?:[^\s;]+/)*[^\s;]+")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|token|secret|credential|private_key|dsn)\s*[:=]\s*\S+"
)


def root_failure_from_exception(
    provider: str,
    domain: str | None,
    stage: str,
    exc: Exception,
) -> RootFailure:
    underlying = exc.__cause__ or exc.__context__
    message = " ".join(str(exc).split()) or type(exc).__name__
    message = _URL.sub("<redacted-url>", message)
    message = _WINDOWS_ABSOLUTE_PATH.sub("<redacted-path>", message)
    message = _UNIX_ABSOLUTE_PATH.sub("<redacted-path>", message)
    message = _SECRET_ASSIGNMENT.sub(r"\1=<redacted>", message)[:500]
    cleanup_failure = getattr(exc, "cleanup_failure", None)
    return RootFailure(
        provider=provider,
        domain=domain,
        stage=stage,
        exception_type=type(exc).__name__,
        underlying_exception_type=(
            None if underlying is None else type(underlying).__name__
        ),
        safe_message=message,
        cleanup_failure=(
            dict(cleanup_failure) if isinstance(cleanup_failure, Mapping) else None
        ),
    )


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
    "ProviderOutcome", "ProviderStatus", "RefreshResult", "RootFailure",
    "UnifiedRunResult", "root_failure_from_exception",
    "render_report", "run_unified_refresh",
]
