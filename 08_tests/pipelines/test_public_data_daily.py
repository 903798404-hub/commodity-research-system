from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
import refresh_public_data

from agri_research_agent.pipelines.public_data_daily import (
    DailyBusinessStatus,
    DeliveryAction,
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
    DomainStatus,
    OverallStatus,
    ProviderOutcome,
    ProviderStatus,
    RootFailure,
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


def _clear_lutou_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LUTOU_HOST", "LUTOU_PORT", "LUTOU_USER", "LUTOU_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def test_lutou_settings_falls_back_to_persistent_secret_without_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_lutou_environment(monkeypatch)
    secret = tmp_path / "lutou.env"
    secret.write_text(
        "LUTOU_HOST=file.invalid\nLUTOU_PORT=3306\n"
        "LUTOU_USER=file-reader\nLUTOU_PASSWORD=file-password\n",
        encoding="utf-8",
    )
    loaded = refresh_public_data._lutou_settings(secret)
    assert (loaded.host, loaded.port, loaded.user, loaded.password) == (
        "file.invalid", 3306, "file-reader", "file-password"
    )


def test_lutou_complete_process_environment_overrides_secret_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "lutou.env"
    secret.write_text(
        "LUTOU_HOST=file.invalid\nLUTOU_PORT=3307\n"
        "LUTOU_USER=file-reader\nLUTOU_PASSWORD=file-password\n",
        encoding="utf-8",
    )
    override = {
        "LUTOU_HOST": "environment.invalid",
        "LUTOU_PORT": "3306",
        "LUTOU_USER": "environment-reader",
        "LUTOU_PASSWORD": "environment-password",
    }
    for name, value in override.items():
        monkeypatch.setenv(name, value)
    loaded = refresh_public_data._lutou_settings(secret)
    assert (loaded.host, loaded.port, loaded.user, loaded.password) == (
        "environment.invalid", 3306, "environment-reader", "environment-password"
    )


def test_lutou_partial_or_invalid_process_environment_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_lutou_environment(monkeypatch)
    monkeypatch.setenv("LUTOU_HOST", "partial.invalid")
    with pytest.raises(SystemExit, match="incomplete"):
        refresh_public_data._lutou_settings(tmp_path / "unused.env")
    monkeypatch.setenv("LUTOU_PORT", "invalid")
    monkeypatch.setenv("LUTOU_USER", "reader")
    monkeypatch.setenv("LUTOU_PASSWORD", "password")
    with pytest.raises(SystemExit, match="invalid"):
        refresh_public_data._lutou_settings(tmp_path / "unused.env")


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


def test_daily_manifest_preserves_refresh_root_and_transaction(
    runtime: RuntimeContext,
) -> None:
    root = RootFailure(
        "lutou", "weather", "SOIL_EVIDENCE", "WeatherError", "ValueError", "safe"
    )
    outcome = replace(
        _outcome(ProviderStatus.INGESTION_FAILURE, provider="lutou"),
        root_failure=root,
    )
    transaction = {
        "current_changed_before_rollback": True,
        "rollback": "PASS",
        "rollback_failure": None,
    }
    refresh = UnifiedRunResult(
        "refresh", "start", "end", ("lutou",), (outcome,),
        OverallStatus.FAILED, runtime.runtime_root / "refresh",
        {
            "aggregate_status": "FAILED",
            "root_failure": root.as_dict(),
            "transaction": transaction,
        },
        root,
        transaction,
    )

    result = run_daily_update(
        runtime=runtime,
        run_id="daily-root-evidence",
        refresh_runner=lambda: refresh,
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        server_store_root=runtime.runtime_root / "server",
    )

    assert result.manifest["root_failure"] == root.as_dict()
    assert result.manifest["transaction"] == transaction
    assert result.manifest["aggregate_status"] == "FAILED"
    assert result.manifest["sources"][0]["root_failure"] == root.as_dict()


def test_initial_seed_requires_explicit_sync_target(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        refresh_public_data.parse_args([
            "--runtime-root", str(tmp_path), "--initial-seed",
        ])
    with pytest.raises(SystemExit):
        refresh_public_data.parse_args([
            "--runtime-root", str(tmp_path), "--initial-seed", "--source", "tankan",
            "--sync-target-root", str(tmp_path / "store"),
        ])
    with pytest.raises(SystemExit):
        refresh_public_data.parse_args([
            "--runtime-root", str(tmp_path), "--initial-seed", "--dry-run",
            "--sync-target-root", str(tmp_path / "store"),
        ])


def test_no_change_explicit_initial_seed_builds_without_faking_updated(
    runtime: RuntimeContext,
) -> None:
    artifact = runtime.runtime_root / "historical_spread_database.parquet"
    artifact.write_bytes(b"fixture")
    package_dir = runtime.runtime_root / "fixture-seed-package"
    package_dir.mkdir()
    identity = "d" * 64
    package = ProductionPackage(
        "public-current-seed", package_dir,
        {"delivery_identity_sha256": identity}, True,
    )
    calls: list[str] = []

    def sync(_directory: Path, **kwargs) -> ServerSyncResult:
        assert kwargs["initial_seed"] is True
        calls.append("sync")
        return ServerSyncResult(
            "SYNCED", package.package_id, "PASS", "PASS", "PASS", "PASS", package_dir
        )

    result = run_daily_update(
        runtime=runtime,
        run_id="daily-initial-seed",
        refresh_runner=lambda: _refresh(
            runtime.runtime_root, _outcome(ProviderStatus.NO_CHANGE)
        ),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        server_store_root=runtime.runtime_root / "server",
        delivery_artifact_runner=lambda: {"domestic-spread": artifact},
        consumer_freshness_validator=lambda: ConsumerFreshnessReport(()),
        package_builder=lambda **_kwargs: package,
        syncer=sync,
        initial_seed=True,
    )

    assert result.business_status is DailyBusinessStatus.NO_CHANGE
    assert result.succeeded is True
    assert result.manifest["delivery_action"] == DeliveryAction.INITIAL_SEED.value
    assert result.manifest["delivery_identity"] == identity
    assert result.manifest["production_data_package"]["status"] == "GENERATED"
    assert result.manifest["server_sync"] == "SYNCED"
    assert calls == ["sync"]


def test_normal_daily_after_initial_seed_is_no_change_and_never_syncs(
    runtime: RuntimeContext,
) -> None:
    artifact = runtime.runtime_root / "historical_spread_database.parquet"
    artifact.write_bytes(b"fixture")
    package_dir = runtime.runtime_root / "packages" / "public-current-seeded"
    package_dir.mkdir(parents=True)
    package = ProductionPackage(
        "public-current-seeded", package_dir,
        {"delivery_identity_sha256": "d" * 64}, False,
    )
    calls: list[str] = []
    result = run_daily_update(
        runtime=runtime,
        run_id="daily-after-initial-seed",
        refresh_runner=lambda: _refresh(
            runtime.runtime_root, _outcome(ProviderStatus.NO_CHANGE)
        ),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        server_store_root=runtime.runtime_root / "server",
        delivery_artifact_runner=lambda: {"domestic-spread": artifact},
        consumer_freshness_validator=lambda: ConsumerFreshnessReport(()),
        package_builder=lambda **_kwargs: package,
        syncer=lambda *_args, **_kwargs: calls.append("sync"),
    )
    assert result.business_status is DailyBusinessStatus.NO_CHANGE
    assert result.succeeded is True
    assert result.manifest["delivery_action"] == DeliveryAction.SKIPPED.value
    assert result.manifest["delivery_identity"] == "d" * 64
    assert result.manifest["production_data_package"]["status"] == "SKIPPED"
    assert result.manifest["server_sync"] == "SKIPPED"
    assert calls == []


def test_source_unavailable_cannot_initial_seed(runtime: RuntimeContext) -> None:
    calls: list[str] = []
    result = run_daily_update(
        runtime=runtime,
        run_id="daily-initial-seed-unavailable",
        refresh_runner=lambda: _refresh(
            runtime.runtime_root,
            _outcome(ProviderStatus.SOURCE_UNAVAILABLE, provider="lutou"),
        ),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        server_store_root=runtime.runtime_root / "server",
        delivery_artifact_runner=lambda: calls.append("artifact"),
        package_builder=lambda **_kwargs: calls.append("package"),
        syncer=lambda *_args, **_kwargs: calls.append("sync"),
        initial_seed=True,
    )
    assert result.business_status is DailyBusinessStatus.FAILED
    assert result.manifest["delivery_action"] == DeliveryAction.SKIPPED.value
    assert result.manifest["server_sync"] == "SKIPPED"
    assert calls == []


def test_stale_freshness_cannot_initial_seed(runtime: RuntimeContext) -> None:
    artifact = runtime.runtime_root / "historical_spread_database.parquet"
    artifact.write_bytes(b"fixture")
    freshness = ConsumerFreshnessReport((ConsumerFreshness(
        "Domestic Spread", date(2026, 8, 14), date(2026, 8, 21),
        FreshnessStatus.STALE, FreshnessGate.HARD, "fixture",
    ),))
    calls: list[str] = []
    result = run_daily_update(
        runtime=runtime,
        run_id="daily-initial-seed-stale",
        refresh_runner=lambda: _refresh(
            runtime.runtime_root, _outcome(ProviderStatus.NO_CHANGE)
        ),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        server_store_root=runtime.runtime_root / "server",
        delivery_artifact_runner=lambda: {"domestic-spread": artifact},
        consumer_freshness_validator=lambda: freshness,
        package_builder=lambda **_kwargs: calls.append("package"),
        syncer=lambda *_args, **_kwargs: calls.append("sync"),
        initial_seed=True,
    )
    assert result.business_status is DailyBusinessStatus.NO_CHANGE
    assert result.succeeded is False
    assert result.manifest["consumer_freshness_validation"]["status"] == "STALE"
    assert result.manifest["production_data_package"]["status"] == "SKIPPED"
    assert calls == []


def test_remote_transport_arguments_are_all_required(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        refresh_public_data.parse_args([
            "--runtime-root", str(tmp_path), "--ssh-target", "trusted-host",
        ])


def test_remote_syncer_maps_sealed_activation_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "schema_version": "public-data-transport/1",
        "status": "SYNCED",
        "package_id": "public-current-abc",
        "transport": "PASS",
        "application_runtime_readability": {
            "schema_version": "application-runtime-readability-evidence/1",
            "PRE_SWITCH_RUNTIME_READABILITY": "PASS",
            "POST_SWITCH_RUNTIME_READABILITY": "PASS",
            "MANIFEST_READ": "PASS", "JSON_PARSE": "PASS",
            "DIRECTORY_TRAVERSAL": "PASS", "PARQUET_METADATA_READ": "PASS",
            "ACTIVATED_RUNTIME_RESOLVER": "PASS",
            "DOMESTIC_SPREAD_READER": "PASS", "THREE_OIL_READER": "PASS",
            "DOMESTIC_BASIS_READER": "PASS", "WEATHER_READER": "PASS",
            "ROLLBACK_REQUIRED": "NO",
        },
        "remote_activation": {
            "status": "SYNCED",
            "package_id": "public-current-abc",
            "manifest": "PASS",
            "sha": "PASS",
            "atomic_switch": "PASS",
            "formal_read_validation": "PASS",
            "safe_reason": None,
        },
    }
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(refresh_public_data.subprocess, "run", run)
    sync = refresh_public_data._build_remote_syncer(
        ssh_target="trusted-host", activation_image_id=f"sha256:{'a' * 64}"
    )
    result = sync(
        tmp_path / "package",
        store_root="/home/ubuntu/market-data/01_data/public-data-server-store",
    )

    assert result.status == "SYNCED"
    assert result.formal_read_validation == "PASS"
    assert "--ssh-target" in calls[0]


@pytest.mark.parametrize(
    "readability",
    [
        None,
        {
            "schema_version": "application-runtime-readability-evidence/1",
            "PRE_SWITCH_RUNTIME_READABILITY": "PASS",
            "POST_SWITCH_RUNTIME_READABILITY": "FAIL",
            "MANIFEST_READ": "PASS", "JSON_PARSE": "PASS",
            "DIRECTORY_TRAVERSAL": "PASS", "PARQUET_METADATA_READ": "PASS",
            "ACTIVATED_RUNTIME_RESOLVER": "FAIL",
            "DOMESTIC_SPREAD_READER": "FAIL", "THREE_OIL_READER": "PASS",
            "DOMESTIC_BASIS_READER": "PASS", "WEATHER_READER": "PASS",
            "ROLLBACK_REQUIRED": "YES",
        },
    ],
    ids=["missing-evidence", "post-switch-failure"],
)
def test_remote_syncer_rejects_missing_or_failed_application_readability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    readability: dict[str, str] | None,
) -> None:
    payload = {
        "schema_version": "public-data-transport/1",
        "status": "SYNCED",
        "package_id": "public-current-abc",
        "remote_activation": {
            "status": "SYNCED",
            "package_id": "public-current-abc",
            "manifest": "PASS",
            "sha": "PASS",
            "atomic_switch": "PASS",
            "formal_read_validation": "PASS",
        },
    }
    if readability is not None:
        payload["application_runtime_readability"] = readability
    monkeypatch.setattr(
        refresh_public_data.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps(payload), ""
        ),
    )
    sync = refresh_public_data._build_remote_syncer(
        ssh_target="trusted-host", activation_image_id=f"sha256:{'a' * 64}"
    )

    with pytest.raises(
        RuntimeError, match="production package activation did not succeed"
    ):
        sync(tmp_path / "package", store_root="/safe/store")


def test_remote_syncer_propagates_initial_seed_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "schema_version": "public-data-transport/1", "status": "SYNCED",
        "package_id": "public-current-abc", "remote_activation": {
            "status": "SYNCED", "package_id": "public-current-abc",
            "manifest": "PASS", "sha": "PASS", "atomic_switch": "PASS",
            "formal_read_validation": "PASS",
        },
        "application_runtime_readability": {
            "schema_version": "application-runtime-readability-evidence/1",
            "PRE_SWITCH_RUNTIME_READABILITY": "PASS",
            "POST_SWITCH_RUNTIME_READABILITY": "PASS",
            "MANIFEST_READ": "PASS", "JSON_PARSE": "PASS",
            "DIRECTORY_TRAVERSAL": "PASS", "PARQUET_METADATA_READ": "PASS",
            "ACTIVATED_RUNTIME_RESOLVER": "PASS",
            "DOMESTIC_SPREAD_READER": "PASS", "THREE_OIL_READER": "PASS",
            "DOMESTIC_BASIS_READER": "PASS", "WEATHER_READER": "PASS",
            "ROLLBACK_REQUIRED": "NO",
        },
    }
    calls: list[list[str]] = []
    monkeypatch.setattr(
        refresh_public_data.subprocess, "run",
        lambda command, **_kwargs: (
            calls.append(command) or subprocess.CompletedProcess(
                command, 0, json.dumps(payload), ""
            )
        ),
    )
    sync = refresh_public_data._build_remote_syncer(
        ssh_target="trusted-host", activation_image_id=f"sha256:{'a' * 64}"
    )
    sync(tmp_path / "package", store_root="/safe/store", initial_seed=True)
    assert calls[0][-1] == "--initial-seed"


def test_domestic_spread_change_delivers_when_public_currents_are_unchanged(
    runtime: RuntimeContext,
) -> None:
    artifact = runtime.runtime_root / "historical_spread_database.parquet"
    artifact.write_bytes(b"fixture")
    package_dir = runtime.runtime_root / "fixture-domestic-package"
    package_dir.mkdir()
    package = ProductionPackage("delivery-2", package_dir, {}, True)
    seen_artifacts: dict[str, object] = {}
    (runtime.runtime_root / "packages" / "public-current-prior").mkdir(parents=True)

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
    assert result.manifest["delivery_action"] == DeliveryAction.STANDARD.value
    assert seen_artifacts == {"domestic-spread": artifact}


def test_unchanged_currents_and_unchanged_domestic_spread_are_no_change(
    runtime: RuntimeContext,
) -> None:
    artifact = runtime.runtime_root / "historical_spread_database.parquet"
    artifact.write_bytes(b"fixture")
    package_dir = runtime.runtime_root / "fixture-unchanged-delivery"
    package_dir.mkdir()
    package = ProductionPackage("delivery-1", package_dir, {}, False)
    sync_called = False

    def forbidden_sync(*_args, **_kwargs):
        nonlocal sync_called
        sync_called = True
        raise AssertionError("NO_CHANGE must not contact the server")

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
        syncer=forbidden_sync,
    )

    assert result.business_status is DailyBusinessStatus.NO_CHANGE
    assert result.manifest["production_data_package"]["status"] == "SKIPPED"
    assert result.manifest["server_sync"] == "SKIPPED"
    assert sync_called is False


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
    assert result.business_status is DailyBusinessStatus.FAILED
    assert result.manifest["current_changed"] is False
    assert "publication blocked" in result.manifest["summary"]
    assert called is False


def test_qc_failure_blocks_publication_and_reports_failed_stage(runtime: RuntimeContext) -> None:
    result = run_daily_update(
        runtime=runtime,
        run_id="daily-qc-failure",
        refresh_runner=lambda: _refresh(runtime.runtime_root, _outcome(ProviderStatus.QC_FAILURE)),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        server_store_root=runtime.runtime_root / "server",
        package_builder=lambda **_kwargs: pytest.fail("package must be skipped"),
        initial_seed=True,
    )
    assert result.business_status is DailyBusinessStatus.FAILED
    assert result.manifest["candidate"] == "PASS"
    assert result.manifest["qc"] == "FAIL"
    assert result.manifest["canonical"] == "N/A"
    assert result.manifest["server_sync"] == "SKIPPED"
    assert result.manifest["delivery_action"] == DeliveryAction.SKIPPED.value


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


def test_mixed_updated_and_unavailable_publishes_partial_snapshot(runtime: RuntimeContext) -> None:
    package_dir = runtime.runtime_root / "packages" / "partial-package"
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
        package_builder=lambda **_kwargs: ProductionPackage(
            "partial-package", package_dir, {}, True
        ),
    )
    assert result.business_status is DailyBusinessStatus.PARTIAL_SUCCESS
    assert result.succeeded is True
    assert result.manifest["production_data_package"]["status"] == "GENERATED"
    assert result.manifest["current_changed"] is True
    assert result.manifest["server_sync"] == "SKIPPED"


def test_mixed_updated_and_unavailable_never_publishes_partial_snapshot(
    runtime: RuntimeContext,
) -> None:
    # Keep this legacy name for pytest/admission identity continuity. Here,
    # "partial snapshot" means an incomplete/inconsistent publication, not the
    # valid mixed trusted package supported by Provider Isolation.
    tankan_before = CurrentIdentity(
        "tankan-old",
        "a" * 64,
        {},
        {
            "tankan-market": {"release_id": "market-old", "manifest_sha256": "b" * 64},
            "fx": {"release_id": "fx-old", "manifest_sha256": "c" * 64},
        },
    )
    tankan_after = CurrentIdentity(
        "tankan-new",
        "d" * 64,
        {},
        {
            "tankan-market": {"release_id": "market-new", "manifest_sha256": "e" * 64},
            "fx": {"release_id": "fx-new", "manifest_sha256": "f" * 64},
        },
    )
    lutou_baseline = CurrentIdentity(
        "lutou-baseline",
        "1" * 64,
        {},
        {
            "lutou-three-oil": {"release_id": "oil-old", "manifest_sha256": "2" * 64},
            "lutou-soil-moisture": {"release_id": "soil-old", "manifest_sha256": "3" * 64},
            "lutou-weather": {"release_id": "weather-old", "manifest_sha256": "4" * 64},
        },
    )
    tankan = ProviderOutcome(
        "tankan",
        ProviderStatus.READY,
        ProviderStatus.UPDATED,
        tankan_before,
        tankan_after,
        {},
        {
            "tankan_market": DomainStatus.UPDATED.value,
            "fx": DomainStatus.UPDATED.value,
        },
        None,
    )
    lutou = ProviderOutcome(
        "lutou",
        ProviderStatus.SOURCE_UNAVAILABLE,
        ProviderStatus.SOURCE_UNAVAILABLE,
        lutou_baseline,
        lutou_baseline,
        {},
        {
            "three_oil": DomainStatus.ERROR.value,
            "soil_moisture": DomainStatus.ERROR.value,
            "weather_observation": DomainStatus.ERROR.value,
            "weather_forecast": DomainStatus.ERROR.value,
        },
        "safe",
    )
    package_dir = runtime.runtime_root / "packages" / "mixed-trusted-package"
    result = run_daily_update(
        runtime=runtime,
        run_id="daily-mixed-trusted-legacy-identity",
        refresh_runner=lambda: _refresh(runtime.runtime_root, tankan, lutou),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        server_store_root=runtime.runtime_root / "server",
        package_builder=lambda **_kwargs: ProductionPackage(
            "mixed-trusted-package", package_dir, {}, True
        ),
        syncer=lambda *_args, **_kwargs: ServerSyncResult(
            "SYNCED",
            "mixed-trusted-package",
            "PASS",
            "PASS",
            "PASS",
            "PASS",
            package_dir,
        ),
    )

    assert result.business_status is DailyBusinessStatus.PARTIAL_SUCCESS
    assert result.succeeded is True
    assert result.manifest["production_data_package"]["status"] == "GENERATED"
    assert result.manifest["server_sync"] == "SYNCED"
    assert result.manifest["atomic_current_switch"] == "PASS"
    current = result.manifest["public_current_vector"]
    assert current["tankan"]["dataset_identities"] == tankan_after.dataset_identities
    assert current["lutou"]["dataset_identities"] == lutou_baseline.dataset_identities
    assert current["lutou"]["release_id"] == "lutou-baseline"
    assert all(
        item["release_id"]
        for provider in current.values()
        for item in provider["dataset_identities"].values()
    )


def test_single_lutou_domain_failure_publishes_successful_domains_and_preserves_failed_current(
    runtime: RuntimeContext,
) -> None:
    tankan = replace(
        _outcome(ProviderStatus.NO_CHANGE, provider="tankan"),
        domains={
            "tankan_market": DomainStatus.NO_CHANGE.value,
            "fx": DomainStatus.NO_CHANGE.value,
        },
    )
    old = {
        "lutou-three-oil": {"release_id": "oil-old", "manifest_sha256": "a" * 64},
        "lutou-soil-moisture": {"release_id": "soil-old", "manifest_sha256": "b" * 64},
        "lutou-weather": {"release_id": "weather-old", "manifest_sha256": "c" * 64},
    }
    new = {
        **old,
        "lutou-three-oil": {"release_id": "oil-new", "manifest_sha256": "d" * 64},
    }
    lutou = ProviderOutcome(
        "lutou",
        ProviderStatus.READY,
        ProviderStatus.INGESTION_FAILURE,
        CurrentIdentity("old", "e" * 64, {}, old),
        CurrentIdentity("mixed", "f" * 64, {}, new),
        {},
        {
            "three_oil": DomainStatus.UPDATED.value,
            "soil_moisture": DomainStatus.NO_CHANGE.value,
            "weather_observation": DomainStatus.ERROR.value,
            "weather_forecast": DomainStatus.ERROR.value,
        },
        "weather failed",
    )
    package_dir = runtime.runtime_root / "packages" / "mixed-domain-package"
    result = run_daily_update(
        runtime=runtime,
        run_id="daily-single-lutou-domain-failure",
        refresh_runner=lambda: _refresh(runtime.runtime_root, tankan, lutou),
        public_current_root=runtime.runtime_root / "public-market-data",
        packages_root=runtime.runtime_root / "packages",
        package_builder=lambda **_kwargs: ProductionPackage(
            "mixed-domain-package", package_dir, {}, True
        ),
    )
    assert result.business_status is DailyBusinessStatus.PARTIAL_SUCCESS
    source = result.manifest["sources"][1]
    assert source["current_after"]["dataset_identities"]["lutou-three-oil"] != (
        source["current_before"]["dataset_identities"]["lutou-three-oil"]
    )
    assert source["current_after"]["dataset_identities"]["lutou-weather"] == (
        source["current_before"]["dataset_identities"]["lutou-weather"]
    )


def test_dry_run_is_read_only_and_reports_all_publication_steps_skipped(
    capsys, tmp_path: Path,
) -> None:
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
    evidence = tmp_path / "machine-evidence.json"
    assert refresh_public_data._dry_run(
        [adapter], run_id="dry-run-test", evidence_output=evidence
    ) == 0
    assert capsys.readouterr().out.strip() == "provider preflight evidence written"
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["run_id"] == "dry-run-test"
    assert payload["dry_run"] is True
    assert payload["candidate"] == payload["qc"] == payload["canonical"] == "SKIPPED"
    assert payload["current_changed"] is False
    assert payload["server_sync"] == "SKIPPED"
    assert evidence.read_bytes().endswith(b"\n")
    assert not list(evidence.parent.glob(".*.tmp"))
    assert adapter.refreshed is False
    assert adapter.closed is True
