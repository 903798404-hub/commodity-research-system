from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import refresh_public_data

from agri_research_agent.automation import full_daily_windows as wrapper
from agri_research_agent.pipelines.public_data_refresh import ProviderStatus


DOMAINS = {
    "tankan": ("tankan_market", "fx"),
    "lutou": (
        "three_oil", "soil_moisture", "weather_observation", "weather_forecast",
    ),
    "lutou_domestic_basis": ("domestic_basis",),
}
DATASETS = {
    "tankan": ("tankan",),
    "lutou": ("lutou-three-oil", "lutou-soil-moisture", "lutou-weather"),
    "lutou_domestic_basis": ("lutou-domestic-basis",),
}


def _source(provider: str) -> dict[str, object]:
    identities = {dataset: f"{dataset}-old" for dataset in DATASETS[provider]}
    return {
        "source": provider,
        "status": "NO_CHANGE",
        "domains": {domain: "NO_CHANGE" for domain in DOMAINS[provider]},
        "current_before": {"dataset_identities": identities},
        "current_after": {"dataset_identities": identities.copy()},
    }


def _partial_manifest(run_id: str) -> dict[str, object]:
    return {
        "schema_version": wrapper.DAILY_SCHEMA,
        "run_id": run_id,
        "business_status": "PARTIAL_SUCCESS",
        "succeeded": True,
        "sources": [_source(provider) for provider in DOMAINS],
        "consumer_freshness_validation": {"status": "PASS", "results": []},
        "production_data_package": {"status": "GENERATED", "package_id": "package"},
        "server_sync": "SYNCED",
        "manifest": "PASS",
        "sha": "PASS",
        "atomic_current_switch": "PASS",
        "formal_read_validation": "PASS",
        "prewarm": {"status": "PASS", "targets": {}},
    }


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.mark.parametrize("failed_provider", ["tankan", "lutou"])
def test_partial_manifest_accepts_independent_provider_outage(
    tmp_path: Path, failed_provider: str,
) -> None:
    run_id = f"partial-{failed_provider}"
    value = _partial_manifest(run_id)
    failed = next(
        item for item in value["sources"] if item["source"] == failed_provider
    )
    failed["status"] = "SOURCE_UNAVAILABLE"
    failed["domains"] = {domain: "MISSING" for domain in failed["domains"]}
    result = wrapper.validate_daily_manifest(
        _write(tmp_path / "manifest.json", value), run_id, 0
    )
    assert result["manifest"]["business_status"] == "PARTIAL_SUCCESS"


def test_partial_manifest_rejects_failed_domain_current_mutation(tmp_path: Path) -> None:
    run_id = "partial-lutou-weather"
    value = _partial_manifest(run_id)
    lutou = next(item for item in value["sources"] if item["source"] == "lutou")
    lutou["status"] = "INGESTION_FAILURE"
    lutou["domains"]["three_oil"] = "UPDATED"
    lutou["domains"]["weather_observation"] = "ERROR"
    lutou["domains"]["weather_forecast"] = "ERROR"
    lutou["current_after"]["dataset_identities"]["lutou-three-oil"] = "oil-new"
    path = _write(tmp_path / "manifest.json", value)
    assert wrapper.validate_daily_manifest(path, run_id, 0)["manifest"] == value

    lutou["current_after"]["dataset_identities"]["lutou-weather"] = "weather-corrupt"
    _write(path, value)
    with pytest.raises(wrapper.WrapperFailure, match="incomplete domain changed"):
        wrapper.validate_daily_manifest(path, run_id, 0)


def test_partial_success_does_not_overwrite_last_full_success(tmp_path: Path) -> None:
    last_success = {"run_id": "last-full", "status": "SUCCESS"}
    wrapper.atomic_write_json(tmp_path / "last-success.json", last_success)
    partial = {"run_id": "partial", "status": "PARTIAL_SUCCESS"}
    wrapper.persist_completed_status(tmp_path, partial, full_success=False)
    assert json.loads((tmp_path / "current-status.json").read_text(encoding="utf-8")) == partial
    assert json.loads((tmp_path / "last-success.json").read_text(encoding="utf-8")) == last_success


def test_provider_preflight_keeps_ready_providers_when_one_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "schema_version": "unified-public-data-dry-run/1",
        "run_id": "run-preflight",
        "dry_run": True,
        "sources": [
            {
                "source": "tankan", "status": "SOURCE_UNAVAILABLE",
                "domains": {"tankan_market": "MISSING", "fx": "MISSING"},
                "current_identity": {"dataset_identities": {}},
            },
            {
                "source": "lutou", "status": "READY",
                "domains": {domain: "READY" for domain in DOMAINS["lutou"]},
                "current_identity": {"dataset_identities": {}},
            },
            {
                "source": "lutou_domestic_basis", "status": "READY",
                "domains": {"domestic_basis": "READY"},
                "current_identity": {"dataset_identities": {}},
            },
        ],
    }
    monkeypatch.setattr(
        wrapper, "tool_path",
        lambda name: str(Path(sys.executable).parent / (name + ".exe")),
    )

    def complete(command, **_kwargs):
        evidence = Path(command[command.index("--evidence-output") + 1])
        evidence.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(wrapper.subprocess, "run", complete)
    result = wrapper.run_provider_preflight(
        Path("python.exe"), tmp_path, tmp_path / "runtime", "run",
        tmp_path / "t.env", tmp_path / "l.env",
    )
    assert result["ready_provider_count"] == 2
    assert result["sources"][0]["status"] == "SOURCE_UNAVAILABLE"


def test_execution_plan_builds_only_ready_provider_and_preserves_blocked_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = {
        "release_id": "old",
        "manifest_sha256": "a" * 64,
        "source_max_dates": {"data": "2026-09-22"},
        "dataset_identities": {"dataset": {"release_id": "old"}},
    }
    evidence = {
        "sources": [
            {
                "source": "tankan", "status": "READY",
                "current_identity": identity,
            },
            {
                "source": "lutou", "status": "DEPENDENCY_UNAVAILABLE",
                "current_identity": identity,
                "domains": {
                    domain: "SKIPPED_DEPENDENCY_UNAVAILABLE"
                    for domain in DOMAINS["lutou"]
                },
            },
            {
                "source": "lutou_domestic_basis",
                "status": "DEPENDENCY_UNAVAILABLE",
                "current_identity": identity,
                "domains": {"domestic_basis": "SKIPPED_DEPENDENCY_UNAVAILABLE"},
            },
        ]
    }
    built: list[set[str]] = []

    def build(**kwargs):
        built.append(kwargs["allowed_providers"])
        return [object()]

    monkeypatch.setattr(refresh_public_data, "_build_adapters", build)
    adapters, preset = refresh_public_data._execution_plan_from_evidence(
        args=object(),
        runtime=object(),
        run_id="run",
        sources=("tankan", "lutou"),
        weather_baseline_root=Path("weather"),
        evidence=evidence,
    )
    assert len(adapters) == 1
    assert built == [{"tankan"}]
    assert [item.provider for item in preset] == ["lutou", "lutou_domestic_basis"]
    assert all(item.status is ProviderStatus.DEPENDENCY_UNAVAILABLE for item in preset)
    assert all(item.current_before == item.current_after for item in preset)
