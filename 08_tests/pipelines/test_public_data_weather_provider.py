from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agri_research_agent.data_sources.lutou.live import (
    LutouConnectionSettings,
    LutouQuery,
    LutouSchemaError,
)
from agri_research_agent.pipelines import public_data_providers
from agri_research_agent.pipelines.public_data_providers import LutouRefreshAdapter
from agri_research_agent.pipelines.public_data_refresh import (
    ProviderFailure,
    ProviderStatus,
)
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


class Client:
    def __init__(self) -> None:
        self.ensure_count = 0

    def ensure_connected(self) -> None:
        self.ensure_count += 1

    def close(self) -> None:
        pass


def _runtime(path: Path) -> RuntimeContext:
    (path / ".market-data-runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_id": "weather-provider-test",
                "classification": "isolated-dev",
                "module_id": "international-spread",
                "created_at": "2026-08-19T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return RuntimeContext(RuntimeMode.ISOLATED_DEV, "international-spread", path)


def test_unified_lutou_provider_calls_weather_once_and_reports_domain(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        public_data_providers,
        "run_goal_b",
        lambda *args, **kwargs: SimpleNamespace(
            promoted=False, candidate_manifest={"source_max_date": "2026-08-18"}
        ),
    )
    monkeypatch.setattr(
        public_data_providers,
        "run_goal_b_soil",
        lambda *args, **kwargs: SimpleNamespace(
            promoted=False, candidate_manifest={"source_max_date": "2026-08-15"}
        ),
    )
    calls = []

    def weather(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        return SimpleNamespace(
            promoted=True,
            candidate_manifest={
                "source_max_dates": {
                    "observation": "2026-08-18",
                    "forecast_valid": "2026-09-02",
                }
            },
        )

    monkeypatch.setattr(public_data_providers, "run_lutou_weather", weather)
    adapter = LutouRefreshAdapter(
        LutouConnectionSettings("fixture.invalid", 3306, "reader", "fixture"),
        _runtime(tmp_path),
        "unified",
        public_data_providers.date(2026, 8, 19),
        Path("catalog.json"),
        Path("oil.json"),
        Path("weather.yaml"),
        Path("weather-baselines"),
    )
    client = Client()
    adapter._client = client  # type: ignore[assignment]
    adapter._weather_catalog = object()  # type: ignore[assignment]

    result = adapter.refresh()

    assert len(calls) == 1
    assert calls[0]["source_catalog"] is not None
    assert calls[0]["baseline_root"] == Path("weather-baselines")
    assert result.promoted is True
    assert result.status is None
    assert result.domains == {
        "three_oil": ProviderStatus.NO_CHANGE.value,
        "soil_moisture": ProviderStatus.NO_CHANGE.value,
        "weather": ProviderStatus.UPDATED.value,
    }
    assert result.source_max_dates["weather_observation"] == "2026-08-18"
    assert result.source_max_dates["weather_forecast_valid"] == "2026-09-02"
    assert client.ensure_count == 1


def test_lutou_preflight_probes_every_required_domain_query(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    queries = {
        "oil": LutouQuery("油脂油料价格", "oil", "Date", ("price",)),
        "soil": LutouQuery("天气2.0", "soil", "日期", ("value",)),
        "observation": LutouQuery("天气2.0", "weather_obs", "日期", ("value",)),
        "forecast": LutouQuery("天气2.0", "weather_fc", "日期", ("value",)),
    }
    calls = []

    class PreflightClient:
        proof = SimpleNamespace(transaction_read_only=True, write_privileges=())
        def __init__(self, settings): pass
        def __enter__(self): return self
        def probe_query(self, query):
            calls.append(query.table)
            return {"latest_date": "2026-08-23"}
        def close(self): pass

    monkeypatch.setattr(public_data_providers, "LutouClient", PreflightClient)
    monkeypatch.setattr(
        public_data_providers,
        "load_three_oil_v1",
        lambda path: SimpleNamespace(
            series=(SimpleNamespace(source_native_table="oil", source_native_series="price"),)
        ),
    )
    monkeypatch.setattr(
        public_data_providers,
        "load_soil_moisture_series",
        lambda path: (
            SimpleNamespace(source_table="soil", date_column="日期", source_column="value"),
        ),
    )
    monkeypatch.setattr(public_data_providers, "validate_weather_normal_baselines", lambda *args: {})
    monkeypatch.setattr(
        public_data_providers,
        "load_weather_source_catalog",
        lambda *args: SimpleNamespace(
            tables=(
                SimpleNamespace(data_family="observation", query=queries["observation"]),
                SimpleNamespace(data_family="forecast", query=queries["forecast"]),
            )
        ),
    )
    adapter = LutouRefreshAdapter(
        LutouConnectionSettings("fixture.invalid", 3306, "reader", "fixture"),
        _runtime(tmp_path), "preflight", public_data_providers.date(2026, 8, 24),
        Path("soil.json"), Path("oil.json"), Path("weather.yaml"), Path("baselines"),
        connector=lambda *args: True, network_check=lambda: True,
    )
    proof = adapter.preflight()
    assert calls == ["oil", "soil", "weather_obs", "weather_fc"]
    assert proof["readiness"] == {
        "connectivity": "READY",
        "required_query": "READY",
        "required_schema": "READY",
        "query_counts": {
            "soil_moisture": 1,
            "three_oil": 1,
            "weather_forecast_valid": 1,
            "weather_observation": 1,
        },
    }


def test_lutou_preflight_schema_failure_never_reaches_refresh(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    class BrokenClient:
        def __init__(self, settings): pass
        def __enter__(self): return self
        def probe_query(self, query): raise LutouSchemaError("fixture mismatch")
        def close(self): pass

    monkeypatch.setattr(public_data_providers, "LutouClient", BrokenClient)
    monkeypatch.setattr(
        public_data_providers,
        "load_three_oil_v1",
        lambda path: SimpleNamespace(
            series=(SimpleNamespace(source_native_table="oil", source_native_series="price"),)
        ),
    )
    monkeypatch.setattr(public_data_providers, "load_soil_moisture_series", lambda path: ())
    adapter = LutouRefreshAdapter(
        LutouConnectionSettings("fixture.invalid", 3306, "reader", "fixture"),
        _runtime(tmp_path), "broken", public_data_providers.date(2026, 8, 24),
        Path("soil.json"), Path("oil.json"), connector=lambda *args: True,
        network_check=lambda: True,
    )
    with pytest.raises(ProviderFailure) as captured:
        adapter.preflight()
    assert captured.value.status is ProviderStatus.SOURCE_SCHEMA_FAILURE
