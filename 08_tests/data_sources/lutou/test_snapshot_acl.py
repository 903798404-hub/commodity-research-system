from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from agri_research_agent.data_sources.lutou import (
    LutouAclRoute,
    LutouSnapshotAcl,
    LutouSnapshotError,
    LutouSnapshotRegistry,
)
from agri_research_agent.research_data import (
    AcquisitionChannel,
    ProviderIdentity,
    SourceLocator,
)
from agri_research_agent.shared.file_identity import FileIdentity


ROOT = Path(__file__).resolve().parents[3]
CATALOG = ROOT / "02_configs" / "public_research_data_catalog.candidate.json"
APPROVAL = ROOT / "02_configs" / "public_research_data_catalog.approval.json"
OILS_SHA = "3ba324daf243af762b86bb9f283a3427bda17b8b3646b2e8295c1554249acc7e"
WEATHER_SHA = "e46e94a39f44273f21649389bc41c94f169ba28ee30a4bcb9f0c2a7e13177ad5"


def registry_for(sha256: str) -> LutouSnapshotRegistry:
    with patch(
        "agri_research_agent.data_sources.lutou.snapshot.identify_file",
        return_value=FileIdentity(sha256, 123),
    ):
        return LutouSnapshotRegistry.load_gate_a_approved(CATALOG, APPROVAL)


def test_registry_loads_only_gate_a_approved_lutou_snapshot_evidence() -> None:
    registry = registry_for(OILS_SHA)
    assert dict(registry.approved_sha256) == {
        "snapshot:01_data/manual/weather/source_dumps/天气2.0.sql": WEATHER_SHA,
        "snapshot:01_data/manual/榨利表/大豆-进口榨利.xlsx": (
            "af5eaf06c7e8ddf4f9a0b62ddebbee9f04248f5e179ce71b8779d0be3e8640af"
        ),
        "snapshot:01_data/manual/榨利表/油脂油料价格.sql": OILS_SHA,
    }


def test_registry_construction_cannot_bypass_gate_a_loader() -> None:
    approved = registry_for(OILS_SHA)
    with pytest.raises(TypeError, match="Gate A approved loader"):
        LutouSnapshotRegistry(
            catalog=approved.catalog,
            approved_sha256=approved.approved_sha256,
            _token=object(),
        )


def test_capture_many_hashes_shared_snapshot_once_and_emits_safe_provenance(tmp_path: Path) -> None:
    calls = 0

    def identify(_: str | Path) -> FileIdentity:
        nonlocal calls
        calls += 1
        return FileIdentity(OILS_SHA, 125_615_726)

    with patch(
        "agri_research_agent.data_sources.lutou.snapshot.identify_file",
        side_effect=identify,
    ):
        registry = LutouSnapshotRegistry.load_gate_a_approved(CATALOG, APPROVAL)
    captures = registry.capture_many(
        [
            "lutou.oils.us-cbot-soybean",
            "lutou.oils.native-53a8c476d93a",
        ],
        tmp_path / "private-location.sql",
        captured_at=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )

    assert calls == 1
    assert {item.provenance.provider.dataset.origin_system.value for item in captures} == {
        "lutou"
    }
    assert {item.provenance.provider.acquisition_channel.value for item in captures} == {
        "manual_snapshot"
    }
    manifest = captures[0].safe_manifest_fields()
    assert manifest["snapshot_sha256"] == OILS_SHA
    assert "private-location" not in str(manifest)
    assert "source_path" not in manifest


def test_capture_rejects_bytes_not_pinned_by_gate_a(tmp_path: Path) -> None:
    registry = registry_for("0" * 64)
    with pytest.raises(LutouSnapshotError, match="do not match Gate A"):
        registry.capture_many(
            ["lutou.oils.basis-price"], tmp_path / "wrong.sql"
        )


def test_acl_delegates_once_and_preserves_mature_adapter_result(tmp_path: Path) -> None:
    captures = registry_for(OILS_SHA).capture_many(
        [
            "lutou.oils.us-cbot-soybean",
            "lutou.oils.native-53a8c476d93a",
        ],
        tmp_path / "snapshot.sql",
    )
    mature_result = object()
    paths: list[Path] = []

    def existing_adapter(path: Path) -> object:
        paths.append(path)
        return mature_result

    result = LutouSnapshotAcl().execute(
        LutouAclRoute.IMPORT_PROFIT_REUTERS, captures, existing_adapter
    )
    assert result.result is mature_result
    assert len(paths) == 1
    assert result.captures == captures


def test_acl_rejects_route_dataset_mismatch_before_adapter(tmp_path: Path) -> None:
    captures = registry_for(OILS_SHA).capture_many(
        ["lutou.oils.basis-price"], tmp_path / "snapshot.sql"
    )
    called = False

    def adapter(_: Path) -> object:
        nonlocal called
        called = True
        return object()

    with pytest.raises(LutouSnapshotError, match="do not match"):
        LutouSnapshotAcl().execute(
            LutouAclRoute.IMPORT_PROFIT_HISTORICAL_DCE, captures, adapter
        )
    assert called is False


def test_weather_acl_accepts_only_weather_assets(tmp_path: Path) -> None:
    weather = registry_for(WEATHER_SHA).capture_many(
        ["lutou.weather.native-fb7c4a6855e5"], tmp_path / "weather.sql"
    )
    marker = object()
    result = LutouSnapshotAcl().execute(
        LutouAclRoute.WEATHER_IMPORT, weather, lambda _: marker
    )
    assert result.result is marker

    oils = registry_for(OILS_SHA).capture_many(
        ["lutou.oils.basis-price"], tmp_path / "oils.sql"
    )
    with pytest.raises(LutouSnapshotError, match="non-weather"):
        LutouSnapshotAcl().execute(
            LutouAclRoute.WEATHER_IMPORT, oils, lambda _: marker
        )


def test_acquisition_channel_changes_without_changing_business_identity(tmp_path: Path) -> None:
    capture = registry_for(OILS_SHA).capture_many(
        ["lutou.oils.basis-price"], tmp_path / "snapshot.sql"
    )[0]
    manual = capture.provenance.provider
    direct = ProviderIdentity(
        dataset=manual.dataset,
        acquisition_channel=AcquisitionChannel.DIRECT_DATABASE,
        provider_dataset_id=manual.provider_dataset_id,
        source_locator=SourceLocator("database:lutou/schema:market/relation:basis_price"),
    )
    assert direct.dataset == manual.dataset
    assert direct.provider_dataset_id == manual.provider_dataset_id
    assert direct.acquisition_channel != manual.acquisition_channel
    assert direct.source_locator != manual.source_locator
