from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agri_research_agent.data_sources.lutou.live import LutouConnectionSettings
from agri_research_agent.pipelines import public_data_providers
from agri_research_agent.pipelines.public_data_providers import LutouRefreshAdapter
from agri_research_agent.pipelines.public_data_refresh import ProviderStatus
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode


class Client:
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
    adapter._client = Client()  # type: ignore[assignment]
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
