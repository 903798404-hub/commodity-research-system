from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agri_research_agent.data_sources.lutou.live import (
    LutouClientError,
    LutouConnectionSettings,
)
from agri_research_agent.pipelines.public_data_providers import (
    DomesticBasisPendingAdapter,
    DomesticBasisRefreshAdapter,
)
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
                "runtime_id": "goal-d3a-provider-fixture",
                "classification": "isolated-dev",
                "module_id": "international-spread",
                "created_at": "2026-08-20T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return RuntimeContext(RuntimeMode.ISOLATED_DEV, "international-spread", tmp_path)


@dataclass
class NoChangeAdapter:
    name: str = "tankan"

    def current_identity(self) -> CurrentIdentity:
        return CurrentIdentity("existing", "manifest", {"market": "2026-08-19"})

    def preflight(self) -> dict[str, object]:
        return {"read_only": True}

    def refresh(self) -> RefreshResult:
        return RefreshResult(False, {"market": "2026-08-19"})


def test_pending_provider_reports_explicit_state_without_connection_or_current_change(
    runtime: RuntimeContext, tmp_path: Path,
) -> None:
    payload = yaml.safe_load(Path("02_configs/lutou_domestic_basis.yaml").read_text(encoding="utf-8"))
    payload["source_contract"]["database_schema"] = None
    payload["source_contract"]["table_status"] = "EXPECTED_FROM_LEGACY_CONTRACT"
    payload["source_contract"]["query_identity_status"] = "LIVE_CONFIRMATION_PENDING"
    payload["source_contract"]["snapshot_identity_status"] = "LIVE_CONFIRMATION_PENDING"
    for item in payload["series"]:
        item["live_status"] = "LIVE_CONFIRMATION_PENDING"
        item["expected_source_status"] = "EXPECTED_FROM_LEGACY_CONTRACT"
    mapping = tmp_path / "pending-mapping.yaml"
    mapping.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    adapter = DomesticBasisPendingAdapter(
        runtime=runtime,
        mapping_path=mapping,
    )
    before = adapter.current_identity()
    result = run_unified_refresh(
        runtime=runtime,
        run_id="d3a-provider-pending",
        adapters=[NoChangeAdapter(), adapter],
    )
    outcome = result.providers[1]
    assert result.overall_status is OverallStatus.SUCCESS_WITH_UNAVAILABLE_SOURCE
    assert outcome.preflight_status is ProviderStatus.LIVE_VERIFICATION_PENDING
    assert outcome.status is ProviderStatus.LIVE_VERIFICATION_PENDING
    assert outcome.current_before == outcome.current_after == before
    assert outcome.safe_reason == "Domestic Basis live schema and source mapping verification is pending"
    assert outcome.domains == {}
    assert "LIVE_VERIFICATION_PENDING" in (result.run_directory / "report.txt").read_text()
    assert not (runtime.runtime_root / "public-market-data" / "lutou-domestic-basis").exists()


def test_live_provider_reports_independent_no_change(monkeypatch, runtime: RuntimeContext) -> None:
    import agri_research_agent.pipelines.public_data_providers as providers

    class Client:
        proof = SimpleNamespace(transaction_read_only=True, write_privileges=())
        def __init__(self, settings): pass
        def __enter__(self): return self
        def probe_query(self, query): return {"latest_date": "2026-08-19"}
        def ensure_connected(self): pass
        def close(self): pass

    class Live:
        def __init__(self, client): self.query = object()
        def verify_schema(self): return {"column_count": 19, "date_indexed": True}
        def date_bounds(self): return __import__("datetime").date(2022, 6, 15), __import__("datetime").date(2026, 8, 19)

    monkeypatch.setattr(providers, "load_domestic_basis_catalog", lambda path: SimpleNamespace(live_verified=True, series=tuple(range(21))))
    monkeypatch.setattr(
        providers,
        "load_domestic_basis_current",
        lambda root: SimpleNamespace(
            release_id="formal-current",
            manifest={"schema_version": "lutou-domestic-basis-current/3", "source_max_date": "2026-08-19"},
        ),
    )
    monkeypatch.setattr(providers, "_pointer", lambda root: {"manifest_sha256": "a" * 64})
    monkeypatch.setattr(providers, "load_historical_basis_seed", lambda root: SimpleNamespace(seed_id="sealed"))
    monkeypatch.setattr(providers, "LutouClient", Client)
    monkeypatch.setattr(providers, "LutouDomesticBasisLiveAdapter", Live)
    calls = []
    def run(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(promoted=False, query_end_date=__import__("datetime").date(2026, 8, 19))
    monkeypatch.setattr(providers, "run_domestic_basis_live", run)
    adapter = DomesticBasisRefreshAdapter(
        LutouConnectionSettings("safe-host", 3306, "readonly", "process-only"),
        runtime, "unified", Path("mapping.yaml"), connector=lambda *args: True,
    )
    result = run_unified_refresh(runtime=runtime, run_id="provider-live", adapters=[NoChangeAdapter(), adapter])
    outcome = result.providers[1]
    assert outcome.preflight_status is ProviderStatus.READY
    assert outcome.status is ProviderStatus.NO_CHANGE
    assert outcome.domains == {"domestic_basis": "NO_CHANGE"}
    assert outcome.source_max_dates == {"domestic_basis": "2026-08-19"}
    assert calls[0]["require_formal_current"] is True


def test_live_provider_fails_before_network_when_formal_alignment_is_missing(
    monkeypatch, runtime: RuntimeContext,
) -> None:
    import agri_research_agent.pipelines.public_data_providers as providers

    monkeypatch.setattr(
        providers,
        "load_domestic_basis_catalog",
        lambda path: SimpleNamespace(live_verified=True, series=tuple(range(21))),
    )
    network_calls = []
    adapter = DomesticBasisRefreshAdapter(
        LutouConnectionSettings("safe-host", 3306, "readonly", "process-only"),
        runtime,
        "unified",
        Path("mapping.yaml"),
        connector=lambda *args: network_calls.append(args) or True,
    )
    result = run_unified_refresh(
        runtime=runtime,
        run_id="provider-formal-missing",
        adapters=[adapter],
    )
    outcome = result.providers[0]
    assert outcome.preflight_status is ProviderStatus.SOURCE_SCHEMA_FAILURE
    assert outcome.status is ProviderStatus.SOURCE_SCHEMA_FAILURE
    assert network_calls == []


def test_live_provider_required_query_failure_is_not_ready(
    monkeypatch, runtime: RuntimeContext,
) -> None:
    import agri_research_agent.pipelines.public_data_providers as providers

    class Client:
        proof = SimpleNamespace(transaction_read_only=True, write_privileges=())
        def __init__(self, settings): pass
        def __enter__(self): return self
        def probe_query(self, query): raise LutouClientError("fixture query failure")
        def close(self): pass

    class Live:
        def __init__(self, client): self.query = object()
        def verify_schema(self): return {"column_count": 19, "date_indexed": True}
        def date_bounds(self):
            return __import__("datetime").date(2022, 6, 15), __import__("datetime").date(2026, 8, 19)

    monkeypatch.setattr(providers, "load_domestic_basis_catalog", lambda path: SimpleNamespace(live_verified=True, series=tuple(range(21))))
    monkeypatch.setattr(
        providers,
        "load_domestic_basis_current",
        lambda root: SimpleNamespace(
            release_id="formal-current",
            manifest={"schema_version": "lutou-domestic-basis-current/3", "source_max_date": "2026-08-19"},
        ),
    )
    monkeypatch.setattr(providers, "_pointer", lambda root: {"manifest_sha256": "a" * 64})
    monkeypatch.setattr(providers, "load_historical_basis_seed", lambda root: SimpleNamespace(seed_id="sealed"))
    monkeypatch.setattr(providers, "LutouClient", Client)
    monkeypatch.setattr(providers, "LutouDomesticBasisLiveAdapter", Live)
    adapter = DomesticBasisRefreshAdapter(
        LutouConnectionSettings("safe-host", 3306, "readonly", "process-only"),
        runtime, "unified", Path("mapping.yaml"), connector=lambda *args: True,
    )
    result = run_unified_refresh(runtime=runtime, run_id="provider-query-not-ready", adapters=[adapter])
    outcome = result.providers[0]
    assert outcome.preflight_status is ProviderStatus.INGESTION_FAILURE
    assert outcome.status is ProviderStatus.INGESTION_FAILURE
    assert outcome.current_before == outcome.current_after


def test_domestic_basis_rebuilds_stale_preflight_client_before_extraction(
    monkeypatch, runtime: RuntimeContext,
) -> None:  # type: ignore[no-untyped-def]
    import agri_research_agent.pipelines.public_data_providers as providers

    class StaleClient:
        close_count = 0
        def ensure_connected(self):
            raise LutouClientError("fixture stale session")
        def close(self):
            self.close_count += 1

    class FreshClient:
        def __init__(self):
            self.enter_count = 0
            self.close_count = 0
            self.read_only_proved = False
        def __enter__(self):
            self.enter_count += 1
            self.read_only_proved = True
            return self
        def close(self):
            self.close_count += 1

    class Live:
        def __init__(self, client):
            self.client = client
            self.query = object()

    stale = StaleClient()
    fresh = FreshClient()
    recovery_calls = []
    extraction_clients = []

    def run(**kwargs):  # type: ignore[no-untyped-def]
        extraction_clients.append(kwargs["adapter"].client)
        assert kwargs["adapter"].client.read_only_proved is True
        return SimpleNamespace(
            promoted=False,
            query_end_date=__import__("datetime").date(2026, 8, 19),
        )

    monkeypatch.setattr(providers, "LutouDomesticBasisLiveAdapter", Live)
    monkeypatch.setattr(providers, "run_domestic_basis_live", run)
    adapter = DomesticBasisRefreshAdapter(
        LutouConnectionSettings("safe-host", 3306, "readonly", "process-only"),
        runtime, "stale-basis", Path("mapping.yaml"),
        recovery_client_factory=lambda: recovery_calls.append("fresh") or fresh,
    )
    adapter._client = stale  # type: ignore[assignment]
    adapter._adapter = Live(stale)  # type: ignore[assignment]

    result = adapter.refresh()

    assert result.status is None
    assert extraction_clients == [fresh]
    assert recovery_calls == ["fresh"]
    assert fresh.enter_count == 1
    assert stale.close_count == 1


def test_domestic_basis_fresh_reconnect_failure_fails_closed_once(
    monkeypatch, runtime: RuntimeContext,
) -> None:  # type: ignore[no-untyped-def]
    import agri_research_agent.pipelines.public_data_providers as providers

    class StaleClient:
        def ensure_connected(self):
            raise LutouClientError("fixture stale session")
        def close(self):
            pass

    class BrokenFreshClient:
        def __init__(self):
            self.close_count = 0
        def __enter__(self):
            raise LutouClientError("fixture-secret at safe-host")
        def close(self):
            self.close_count += 1

    class Live:
        def __init__(self, client):
            self.client = client

    broken = BrokenFreshClient()
    recovery_calls = []
    extraction_calls = []
    monkeypatch.setattr(providers, "LutouDomesticBasisLiveAdapter", Live)
    monkeypatch.setattr(
        providers, "run_domestic_basis_live",
        lambda **kwargs: extraction_calls.append(kwargs),
    )
    adapter = DomesticBasisRefreshAdapter(
        LutouConnectionSettings("safe-host", 3306, "readonly", "process-only"),
        runtime, "basis-reconnect-fail", Path("mapping.yaml"),
        recovery_client_factory=lambda: recovery_calls.append("fresh") or broken,
    )
    stale = StaleClient()
    adapter._client = stale  # type: ignore[assignment]
    adapter._adapter = Live(stale)  # type: ignore[assignment]

    with pytest.raises(ProviderFailure) as captured:
        adapter.refresh()

    assert captured.value.status is ProviderStatus.NETWORK_UNAVAILABLE
    assert recovery_calls == ["fresh"]
    assert extraction_calls == []
    assert broken.close_count == 1
    assert "connection-validation" in captured.value.safe_reason
    assert "SOURCE_CONNECTION_FAILURE" in captured.value.safe_reason
    assert "fixture-secret" not in captured.value.safe_reason
    assert "safe-host" not in captured.value.safe_reason
