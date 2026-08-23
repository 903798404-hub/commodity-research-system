from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from agri_research_agent.pipelines.public_data_refresh import (
    CurrentIdentity,
    OverallStatus,
    ProviderFailure,
    ProviderStatus,
    RefreshResult,
    run_unified_refresh,
)
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


@pytest.fixture
def runtime(tmp_path: Path) -> RuntimeContext:
    (tmp_path / ".market-data-runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_id": "goal-c-fixture",
                "classification": "isolated-dev",
                "module_id": "international-spread",
                "created_at": "2026-08-19T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return RuntimeContext(RuntimeMode.ISOLATED_DEV, "international-spread", tmp_path)


@dataclass
class FakeAdapter:
    name: str
    refresh_result: RefreshResult | None = None
    preflight_failure: ProviderFailure | None = None
    refresh_failure: ProviderFailure | None = None
    revision: int = 1
    identity_failure: bool = False
    unexpected_preflight_failure: bool = False

    def current_identity(self) -> CurrentIdentity:
        if self.identity_failure:
            raise ValueError("broken Current")
        return CurrentIdentity(f"r{self.revision}", f"sha{self.revision}", {"data": "2026-08-18"})

    def preflight(self):
        if self.unexpected_preflight_failure:
            raise OSError("unexpected")
        if self.preflight_failure:
            raise self.preflight_failure
        return {"read_only": True}

    def refresh(self) -> RefreshResult:
        if self.refresh_failure:
            raise self.refresh_failure
        result = self.refresh_result or RefreshResult(False, {"data": "2026-08-18"})
        if result.promoted:
            self.revision += 1
        return result


def test_all_noop_seals_machine_manifest_and_human_report(runtime: RuntimeContext) -> None:
    result = run_unified_refresh(
        runtime=runtime,
        run_id="noop",
        adapters=[FakeAdapter("tankan"), FakeAdapter("lutou")],
    )
    assert result.overall_status is OverallStatus.NO_CHANGE
    assert [item.status for item in result.providers] == [ProviderStatus.NO_CHANGE] * 2
    assert (result.run_directory / "manifest.json").is_file()
    assert "Overall status: NO_CHANGE" in (result.run_directory / "report.txt").read_text()


def test_mixed_noop_and_update_is_success(runtime: RuntimeContext) -> None:
    result = run_unified_refresh(
        runtime=runtime,
        run_id="mixed",
        adapters=[
            FakeAdapter("tankan"),
            FakeAdapter("lutou", RefreshResult(True, {"data": "2026-08-19"})),
        ],
    )
    assert result.overall_status is OverallStatus.SUCCESS
    assert result.providers[1].current_before != result.providers[1].current_after


def test_reported_update_requires_formal_current_identity_change(runtime: RuntimeContext) -> None:
    class FalsePromotion(FakeAdapter):
        def refresh(self) -> RefreshResult:
            return RefreshResult(True, {"data": "2026-08-19"})

    result = run_unified_refresh(
        runtime=runtime, run_id="false-promotion", adapters=[FalsePromotion("tankan")]
    )
    assert result.providers[0].status is ProviderStatus.NO_CHANGE
    assert result.providers[0].current_before == result.providers[0].current_after


def test_no_change_with_identity_mutation_fails_closed(runtime: RuntimeContext) -> None:
    class HiddenMutation(FakeAdapter):
        def refresh(self) -> RefreshResult:
            self.revision += 1
            return RefreshResult(False, {"data": "2026-08-19"})

    result = run_unified_refresh(
        runtime=runtime, run_id="hidden-mutation", adapters=[HiddenMutation("tankan")]
    )
    assert result.providers[0].status is ProviderStatus.PROMOTION_FAILURE
    assert result.overall_status is OverallStatus.FAILED


@pytest.mark.parametrize(
    "status",
    [
        ProviderStatus.SOURCE_UNAVAILABLE,
        ProviderStatus.NETWORK_UNAVAILABLE,
        ProviderStatus.AUTH_FAILURE,
        ProviderStatus.SOURCE_SCHEMA_FAILURE,
    ],
)
def test_preflight_failures_do_not_run_or_change_current(runtime: RuntimeContext, status: ProviderStatus) -> None:
    adapter = FakeAdapter("tankan", preflight_failure=ProviderFailure(status, "safe"))
    result = run_unified_refresh(runtime=runtime, run_id=f"preflight-{status.value.lower()}", adapters=[adapter])
    outcome = result.providers[0]
    assert outcome.status is status
    assert outcome.current_before == outcome.current_after


@pytest.mark.parametrize(
    "status",
    [ProviderStatus.INGESTION_FAILURE, ProviderStatus.QC_FAILURE, ProviderStatus.PROMOTION_FAILURE],
)
def test_pipeline_failures_preserve_current(runtime: RuntimeContext, status: ProviderStatus) -> None:
    adapter = FakeAdapter("lutou", refresh_failure=ProviderFailure(status, "safe"))
    result = run_unified_refresh(runtime=runtime, run_id=f"failure-{status.value.lower()}", adapters=[adapter])
    outcome = result.providers[0]
    assert outcome.preflight_status is ProviderStatus.READY
    assert outcome.status is status
    assert outcome.current_before == outcome.current_after


def test_unavailable_provider_does_not_block_other_provider(runtime: RuntimeContext) -> None:
    result = run_unified_refresh(
        runtime=runtime,
        run_id="independent",
        adapters=[
            FakeAdapter("tankan", preflight_failure=ProviderFailure(ProviderStatus.NETWORK_UNAVAILABLE, "safe")),
            FakeAdapter("lutou", RefreshResult(True, {"data": "2026-08-19"})),
        ],
    )
    assert result.overall_status is OverallStatus.SUCCESS_WITH_UNAVAILABLE_SOURCE
    assert result.providers[1].status is ProviderStatus.UPDATED


def test_required_source_mode_preflights_all_and_preserves_every_current(
    runtime: RuntimeContext,
) -> None:
    tankan = FakeAdapter("tankan", RefreshResult(True, {"data": "2026-08-19"}))
    lutou = FakeAdapter(
        "lutou",
        preflight_failure=ProviderFailure(ProviderStatus.SOURCE_UNAVAILABLE, "safe"),
    )
    result = run_unified_refresh(
        runtime=runtime,
        run_id="all-required",
        adapters=[tankan, lutou],
        require_all_sources=True,
    )
    assert [item.status for item in result.providers] == [
        ProviderStatus.NO_CHANGE,
        ProviderStatus.SOURCE_UNAVAILABLE,
    ]
    assert all(item.current_before == item.current_after for item in result.providers)
    assert tankan.revision == 1


def test_required_source_mode_rolls_back_prior_update_when_later_qc_fails(
    runtime: RuntimeContext,
) -> None:
    @dataclass
    class PointerAdapter:
        name: str
        fail_qc: bool = False

        @property
        def pointer(self) -> Path:
            return runtime.runtime_root / "public-market-data" / self.name / "current.json"

        def current_identity(self) -> CurrentIdentity:
            value = json.loads(self.pointer.read_text(encoding="utf-8"))
            return CurrentIdentity(value["release_id"], value["manifest_sha256"], {})

        def preflight(self):
            return {"read_only": True}

        def refresh(self) -> RefreshResult:
            if self.fail_qc:
                raise ProviderFailure(ProviderStatus.QC_FAILURE, "safe")
            self.pointer.write_text(
                json.dumps({"release_id": "r2", "manifest_sha256": "b" * 64}),
                encoding="utf-8",
            )
            return RefreshResult(True, {})

    adapters = [PointerAdapter("tankan"), PointerAdapter("lutou", fail_qc=True)]
    for adapter in adapters:
        adapter.pointer.parent.mkdir(parents=True)
        adapter.pointer.write_text(
            json.dumps({"release_id": "r1", "manifest_sha256": "a" * 64}),
            encoding="utf-8",
        )
    before = {adapter.name: adapter.pointer.read_bytes() for adapter in adapters}
    result = run_unified_refresh(
        runtime=runtime,
        run_id="rollback-after-qc",
        adapters=adapters,
        require_all_sources=True,
    )
    assert result.providers[1].status is ProviderStatus.QC_FAILURE
    assert all(item.current_before == item.current_after for item in result.providers)
    assert {adapter.name: adapter.pointer.read_bytes() for adapter in adapters} == before


def test_failed_provider_does_not_block_other_provider(runtime: RuntimeContext) -> None:
    result = run_unified_refresh(
        runtime=runtime,
        run_id="partial-failure",
        adapters=[
            FakeAdapter("tankan", refresh_failure=ProviderFailure(ProviderStatus.QC_FAILURE, "safe")),
            FakeAdapter("lutou", RefreshResult(True, {"data": "2026-08-19"})),
        ],
    )
    assert result.overall_status is OverallStatus.PARTIAL_FAILURE
    assert result.providers[1].status is ProviderStatus.UPDATED


def test_all_failed_is_failed(runtime: RuntimeContext) -> None:
    result = run_unified_refresh(
        runtime=runtime,
        run_id="all-failed",
        adapters=[
            FakeAdapter("tankan", refresh_failure=ProviderFailure(ProviderStatus.QC_FAILURE, "safe")),
            FakeAdapter("lutou", preflight_failure=ProviderFailure(ProviderStatus.AUTH_FAILURE, "safe")),
        ],
    )
    assert result.overall_status is OverallStatus.FAILED


def test_unexpected_preflight_and_current_identity_failures_are_isolated(runtime: RuntimeContext) -> None:
    result = run_unified_refresh(
        runtime=runtime,
        run_id="unexpected-isolated",
        adapters=[
            FakeAdapter("tankan", unexpected_preflight_failure=True),
            FakeAdapter("lutou", identity_failure=True),
            FakeAdapter("healthy"),
        ],
    )
    assert [item.status for item in result.providers] == [
        ProviderStatus.SOURCE_UNAVAILABLE,
        ProviderStatus.PROMOTION_FAILURE,
        ProviderStatus.NO_CHANGE,
    ]
    assert result.overall_status is OverallStatus.PARTIAL_FAILURE


def test_report_failure_never_rolls_back_updated_current(runtime: RuntimeContext) -> None:
    adapter = FakeAdapter("tankan", RefreshResult(True, {"data": "2026-08-19"}))

    def broken_report(_manifest):
        raise OSError("injected report failure")

    with pytest.raises(OSError, match="report failure"):
        run_unified_refresh(
            runtime=runtime,
            run_id="report-failure",
            adapters=[adapter],
            report_builder=broken_report,
        )
    assert adapter.revision == 2
    assert not (runtime.runtime_root / "public-data-refresh" / "runs" / "report-failure").exists()


def test_run_report_is_immutable(runtime: RuntimeContext) -> None:
    adapter = FakeAdapter("tankan")
    run_unified_refresh(runtime=runtime, run_id="same", adapters=[adapter])
    with pytest.raises(FileExistsError):
        run_unified_refresh(runtime=runtime, run_id="same", adapters=[adapter])


def test_manifest_rejects_sensitive_or_absolute_metadata(runtime: RuntimeContext) -> None:
    adapter = FakeAdapter("tankan", RefreshResult(False, {"password": "not-allowed"}))
    with pytest.raises(ValueError, match="sensitive"):
        run_unified_refresh(runtime=runtime, run_id="sensitive", adapters=[adapter])
