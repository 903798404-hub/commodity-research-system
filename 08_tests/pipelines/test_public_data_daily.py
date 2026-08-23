from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import refresh_public_data

from agri_research_agent.pipelines.public_data_daily import (
    DailyBusinessStatus,
    run_daily_update,
)
from agri_research_agent.pipelines.public_data_delivery import (
    PrewarmResult,
    PrewarmStatus,
    PrewarmTarget,
    ProductionPackage,
    ServerSyncResult,
)
from agri_research_agent.pipelines.public_data_refresh import (
    CurrentIdentity,
    OverallStatus,
    ProviderOutcome,
    ProviderStatus,
    UnifiedRunResult,
)
from agri_research_agent.pipelines.public_data_freshness import (
    ConsumerFreshness,
    ConsumerFreshnessReport,
    FreshnessGate,
    FreshnessStatus,
)
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


@pytest.fixture
def runtime(tmp_path: Path) -> RuntimeContext:
    (tmp_path / ".market-data-runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_id": "goal-e-fixture",
                "classification": "isolated-dev",
                "module_id": "international-spread",
                "created_at": "2026-08-23T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return RuntimeContext(RuntimeMode.ISOLATED_DEV, "international-spread", tmp_path)


def _outcome(status: ProviderStatus, *, provider: str = "tankan") -> ProviderOutcome:
    before = CurrentIdentity("r1", "a" * 64, {"market": "2026-08-21"})
    after = (
        CurrentIdentity("r2", "b" * 64, {"market": "2026-08-22"})
        if status is ProviderStatus.UPDATED else before
    )
    return ProviderOutcome(
        provider,
        ProviderStatus.READY if status not in {
            ProviderStatus.SOURCE_UNAVAILABLE, ProviderStatus.NETWORK_UNAVAILABLE
        } else status,
        status,
        before,
        after,
        after.source_max_dates,
        {},
        "safe" if status not in {ProviderStatus.UPDATED, ProviderStatus.NO_CHANGE} else None,
    )


def _refresh(tmp_path: Path, *outcomes: ProviderOutcome) -> UnifiedRunResult:
    return UnifiedRunResult(
        "refresh", "start", "end", tuple(item.provider for item in outcomes),
        tuple(outcomes), OverallStatus.SUCCESS, tmp_path / "refresh", {},
    )


def test_no_change_short_circuits_package_sync_and_prewarm(runtime: RuntimeContext) -> None:
    calls: list[str] = []

    def forbidden(**_kwargs):
        calls.append("package")
        raise AssertionError

    result = run_daily_update(
        runtime=runtime,
        run_id="daily-no-change",
        refresh_runner=lambda: _refresh(runtime.runtime_root, _outcome(ProviderStatus.NO_CHANGE)),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        server_store_root=runtime.runtime_root / "server",
        package_builder=forbidden,
        syncer=lambda *_args, **_kwargs: calls.append("sync"),
        prewarmer=lambda _targets: calls.append("prewarm"),
    )
    assert result.business_status is DailyBusinessStatus.NO_CHANGE
    assert result.manifest["production_data_package"]["status"] == "SKIPPED"
    assert result.manifest["server_sync"] == "SKIPPED"
    assert result.prewarm.status is PrewarmStatus.SKIPPED
    assert calls == []


def test_domestic_spread_change_delivers_when_public_currents_are_unchanged(
    runtime: RuntimeContext,
) -> None:
    artifact = runtime.runtime_root / "historical_spread_database.parquet"
    artifact.write_bytes(b"fixture")
    package_dir = runtime.runtime_root / "fixture-domestic-package"
    package_dir.mkdir()
    package = ProductionPackage("delivery-2", package_dir, {}, True)
    seen_artifacts: dict[str, object] = {}

    def build(**kwargs) -> ProductionPackage:
        seen_artifacts.update(kwargs["delivery_artifacts"])
        return package

    result = run_daily_update(
        runtime=runtime,
        run_id="daily-domestic-only-change",
        refresh_runner=lambda: _refresh(
            runtime.runtime_root, _outcome(ProviderStatus.NO_CHANGE)
        ),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        server_store_root=runtime.runtime_root / "server",
        delivery_artifact_runner=lambda: {"domestic-spread": artifact},
        package_builder=build,
        syncer=lambda *_args, **_kwargs: ServerSyncResult(
            "SYNCED", "delivery-2", "PASS", "PASS", "PASS", "PASS", package_dir
        ),
    )

    assert result.business_status is DailyBusinessStatus.UPDATED
    assert result.manifest["current_changed"] is False
    assert result.manifest["delivery_artifact_producer"] == "PASS"
    assert result.manifest["production_data_package"]["status"] == "GENERATED"
    assert result.manifest["server_sync"] == "SYNCED"
    assert seen_artifacts == {"domestic-spread": artifact}


def test_unchanged_currents_and_unchanged_domestic_spread_are_no_change(
    runtime: RuntimeContext,
) -> None:
    artifact = runtime.runtime_root / "historical_spread_database.parquet"
    artifact.write_bytes(b"fixture")
    package_dir = runtime.runtime_root / "fixture-unchanged-delivery"
    package_dir.mkdir()
    package = ProductionPackage("delivery-1", package_dir, {}, False)

    result = run_daily_update(
        runtime=runtime,
        run_id="daily-delivery-no-change",
        refresh_runner=lambda: _refresh(
            runtime.runtime_root, _outcome(ProviderStatus.NO_CHANGE)
        ),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        server_store_root=runtime.runtime_root / "server",
        delivery_artifact_runner=lambda: {"domestic-spread": artifact},
        package_builder=lambda **_kwargs: package,
        syncer=lambda *_args, **_kwargs: ServerSyncResult(
            "NO_CHANGE", "delivery-1", "PASS", "PASS", "N/A", "PASS", package_dir
        ),
    )

    assert result.business_status is DailyBusinessStatus.NO_CHANGE
    assert result.manifest["production_data_package"]["status"] == "NO_CHANGE"
    assert result.manifest["server_sync"] == "NO_CHANGE"


def test_domestic_spread_producer_failure_blocks_package_and_switch(
    runtime: RuntimeContext,
) -> None:
    calls: list[str] = []

    def fail() -> dict[str, Path]:
        raise RuntimeError("source unavailable")

    result = run_daily_update(
        runtime=runtime,
        run_id="daily-domestic-producer-failure",
        refresh_runner=lambda: _refresh(
            runtime.runtime_root, _outcome(ProviderStatus.NO_CHANGE)
        ),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        delivery_artifact_runner=fail,
        package_builder=lambda **_kwargs: calls.append("package"),
        syncer=lambda *_args, **_kwargs: calls.append("sync"),
    )

    assert result.business_status is DailyBusinessStatus.FAILED
    assert result.succeeded is False
    assert result.manifest["delivery_artifact_producer"] == "FAIL"
    assert result.manifest["production_data_package"]["status"] == "SKIPPED"
    assert result.manifest["server_sync"] == "SKIPPED"
    assert calls == []


def test_source_unavailable_preserves_current_and_short_circuits(runtime: RuntimeContext) -> None:
    called = False

    def forbidden(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError

    result = run_daily_update(
        runtime=runtime,
        run_id="daily-unavailable",
        refresh_runner=lambda: _refresh(
            runtime.runtime_root, _outcome(ProviderStatus.SOURCE_UNAVAILABLE, provider="lutou")
        ),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        package_builder=forbidden,
    )
    assert result.business_status is DailyBusinessStatus.SOURCE_UNAVAILABLE
    assert result.manifest["current_changed"] is False
    assert "Current preserved" in result.manifest["summary"]
    assert called is False


def test_qc_failure_blocks_publication_and_reports_failed_stage(runtime: RuntimeContext) -> None:
    result = run_daily_update(
        runtime=runtime,
        run_id="daily-qc-failure",
        refresh_runner=lambda: _refresh(runtime.runtime_root, _outcome(ProviderStatus.QC_FAILURE)),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        package_builder=lambda **_kwargs: pytest.fail("package must be skipped"),
    )
    assert result.business_status is DailyBusinessStatus.FAILED
    assert result.manifest["candidate"] == "PASS"
    assert result.manifest["qc"] == "FAIL"
    assert result.manifest["canonical"] == "N/A"
    assert result.manifest["server_sync"] == "SKIPPED"


def test_updated_alone_packages_syncs_then_prewarms(runtime: RuntimeContext) -> None:
    order: list[str] = []
    package_dir = runtime.runtime_root / "fixture-package"
    package_dir.mkdir()
    package = ProductionPackage("p1", package_dir, {})

    def build(**_kwargs) -> ProductionPackage:
        order.append("package")
        return package

    def sync(_directory: Path, **_kwargs) -> ServerSyncResult:
        order.append("sync")
        return ServerSyncResult(
            "SYNCED", "p1", "PASS", "PASS", "PASS", "PASS", package_dir
        )

    def warm(_targets) -> PrewarmResult:
        order.append("prewarm")
        return PrewarmResult(
            PrewarmStatus.PARTIAL,
            {"international_spread": "PASS", "weather": "FAIL:TimeoutError"},
        )

    result = run_daily_update(
        runtime=runtime,
        run_id="daily-updated",
        refresh_runner=lambda: _refresh(runtime.runtime_root, _outcome(ProviderStatus.UPDATED)),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        server_store_root=runtime.runtime_root / "server",
        prewarm_targets=[PrewarmTarget("unused", lambda: None)],
        package_builder=build,
        syncer=sync,
        prewarmer=warm,
    )
    assert order == ["package", "sync", "prewarm"]
    assert result.business_status is DailyBusinessStatus.UPDATED
    assert result.succeeded is True
    assert result.manifest["production_data_package"] == {"status": "GENERATED", "package_id": "p1"}
    assert result.manifest["server_sync"] == "SYNCED"
    assert result.manifest["prewarm"]["status"] == "PARTIAL"
    assert "UPDATED |" in result.manifest["summary"]


def test_stale_hard_consumer_blocks_package_before_sync(runtime: RuntimeContext) -> None:
    freshness = ConsumerFreshnessReport(
        (
            ConsumerFreshness(
                "Domestic Spread",
                date(2026, 8, 14),
                date(2026, 8, 21),
                FreshnessStatus.STALE,
                FreshnessGate.HARD,
                "persisted spread vs common configured leg dates",
            ),
        )
    )
    result = run_daily_update(
        runtime=runtime,
        run_id="daily-stale-consumer",
        refresh_runner=lambda: _refresh(runtime.runtime_root, _outcome(ProviderStatus.UPDATED)),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        consumer_freshness_validator=lambda: freshness,
        package_builder=lambda **_kwargs: pytest.fail("stale consumer must block package"),
    )
    assert result.succeeded is False
    assert result.manifest["consumer_freshness_validation"]["status"] == "STALE"
    assert result.manifest["production_data_package"]["status"] == "SKIPPED"
    assert "Domestic Spread latest=2026-08-14 | STALE" in result.manifest["summary"]


def test_server_no_change_does_not_prewarm(runtime: RuntimeContext) -> None:
    package_dir = runtime.runtime_root / "fixture-package-no-change"
    package_dir.mkdir()
    package = ProductionPackage("p1", package_dir, {})
    warmed = False

    def warm(_targets):
        nonlocal warmed
        warmed = True
        return PrewarmResult(PrewarmStatus.PASS, {})

    result = run_daily_update(
        runtime=runtime,
        run_id="daily-server-no-change",
        refresh_runner=lambda: _refresh(runtime.runtime_root, _outcome(ProviderStatus.UPDATED)),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        server_store_root=runtime.runtime_root / "server",
        package_builder=lambda **_kwargs: package,
        syncer=lambda *_args, **_kwargs: ServerSyncResult(
            "NO_CHANGE", "p1", "PASS", "PASS", "N/A", "PASS", package_dir
        ),
        prewarmer=warm,
    )
    assert result.manifest["server_sync"] == "NO_CHANGE"
    assert result.prewarm.status is PrewarmStatus.SKIPPED
    assert warmed is False


def test_failed_switch_never_prewarm(runtime: RuntimeContext) -> None:
    package_dir = runtime.runtime_root / "fixture-package-failed-switch"
    package_dir.mkdir()
    package = ProductionPackage("p1", package_dir, {})
    warmed = False

    def warm(_targets):
        nonlocal warmed
        warmed = True
        return PrewarmResult(PrewarmStatus.PASS, {})

    result = run_daily_update(
        runtime=runtime,
        run_id="daily-failed-switch",
        refresh_runner=lambda: _refresh(runtime.runtime_root, _outcome(ProviderStatus.UPDATED)),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        server_store_root=runtime.runtime_root / "server",
        package_builder=lambda **_kwargs: package,
        syncer=lambda *_args, **_kwargs: ServerSyncResult(
            "FAILED", "p1", "PASS", "PASS", "FAIL", "N/A", None, "switch failed"
        ),
        prewarmer=warm,
    )
    assert result.succeeded is False
    assert result.manifest["atomic_current_switch"] == "FAIL"
    assert result.prewarm.status is PrewarmStatus.SKIPPED
    assert warmed is False


def test_aggregate_current_identity_excludes_source_dates(runtime: RuntimeContext) -> None:
    base = CurrentIdentity("r1", "a" * 64, {"market": "2026-08-21"})
    later = CurrentIdentity("r1", "a" * 64, {"market": "2026-08-22"})
    one = ProviderOutcome(
        "tankan", ProviderStatus.READY, ProviderStatus.NO_CHANGE,
        base, base, base.source_max_dates, {}, None,
    )
    two = ProviderOutcome(
        "tankan", ProviderStatus.READY, ProviderStatus.NO_CHANGE,
        later, later, later.source_max_dates, {}, None,
    )
    first = run_daily_update(
        runtime=runtime,
        run_id="identity-date-one",
        refresh_runner=lambda: _refresh(runtime.runtime_root, one),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
    )
    second = run_daily_update(
        runtime=runtime,
        run_id="identity-date-two",
        refresh_runner=lambda: _refresh(runtime.runtime_root, two),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
    )
    assert first.manifest["public_current_identity"] == second.manifest["public_current_identity"]


def test_mixed_updated_and_unavailable_never_publishes_partial_snapshot(runtime: RuntimeContext) -> None:
    result = run_daily_update(
        runtime=runtime,
        run_id="daily-mixed-unavailable",
        refresh_runner=lambda: _refresh(
            runtime.runtime_root,
            _outcome(ProviderStatus.UPDATED, provider="tankan"),
            _outcome(ProviderStatus.SOURCE_UNAVAILABLE, provider="lutou"),
        ),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        package_builder=lambda **_kwargs: pytest.fail("partial package must be skipped"),
    )
    assert result.business_status is DailyBusinessStatus.SOURCE_UNAVAILABLE
    assert result.manifest["production_data_package"]["status"] == "SKIPPED"
    assert result.manifest["server_sync"] == "SKIPPED"


def test_dry_run_is_read_only_and_reports_all_publication_steps_skipped(capsys) -> None:
    class Adapter:
        name = "tankan"
        refreshed = False
        closed = False

        def current_identity(self):
            return CurrentIdentity("r1", "a" * 64, {"market": "2026-08-21"})

        def preflight(self):
            return {"read_only": True, "source_max_dates": {"market": "2026-08-21"}}

        def refresh(self):
            self.refreshed = True
            raise AssertionError("dry-run must not refresh")

        def close(self):
            self.closed = True

    adapter = Adapter()
    assert refresh_public_data._dry_run([adapter]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["candidate"] == payload["qc"] == payload["canonical"] == "SKIPPED"
    assert payload["current_changed"] is False
    assert payload["server_sync"] == "SKIPPED"
    assert adapter.refreshed is False
    assert adapter.closed is True
