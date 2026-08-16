from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agri_research_agent.research_data.identities import (
    AcquisitionChannel,
    DatasetId,
    DatasetRef,
    OriginSystem,
    Provenance,
    ProviderDatasetId,
    ProviderIdentity,
    ProviderSeriesId,
    SeriesId,
    SeriesRef,
    SourceLocator,
)


def _dataset() -> DatasetRef:
    return DatasetRef(DatasetId("lutou.weather.native-0123456789ab"), OriginSystem("lutou"))


def test_canonical_and_provider_identities_are_distinct() -> None:
    dataset = _dataset()
    series = SeriesRef(dataset, SeriesId("weather.precipitation.us.observed.daily"))
    provider = ProviderIdentity(
        dataset=dataset,
        acquisition_channel=AcquisitionChannel.MANUAL_SNAPSHOT,
        provider_dataset_id=ProviderDatasetId("lutou:weather:美国_降雨"),
        provider_series_id=ProviderSeriesId("lutou:weather:美国_降雨:Illinois"),
        source_locator=SourceLocator(
            "snapshot:01_data/manual/weather/source_dumps/天气2.0.sql#table=美国_降雨"
        ),
    )

    assert str(series.series_id) == "weather.precipitation.us.observed.daily"
    assert str(provider.provider_series_id) != str(series.series_id)
    assert provider.dataset == series.dataset


def test_acquisition_channel_can_change_without_changing_dataset_identity() -> None:
    dataset = _dataset()
    manual = ProviderIdentity(
        dataset,
        AcquisitionChannel.MANUAL_SNAPSHOT,
        ProviderDatasetId("lutou:weather:美国_降雨"),
        SourceLocator("snapshot:01_data/manual/weather/source_dumps/天气2.0.sql#table=美国_降雨"),
    )
    direct = ProviderIdentity(
        dataset,
        AcquisitionChannel.DIRECT_DATABASE,
        ProviderDatasetId("lutou:weather:美国_降雨"),
        SourceLocator("database:lutou/schema:weather/relation:美国_降雨"),
    )

    assert manual.dataset == direct.dataset
    assert manual.acquisition_channel != direct.acquisition_channel


def test_provenance_requires_aware_capture_and_valid_hashes() -> None:
    provider = ProviderIdentity(
        _dataset(),
        AcquisitionChannel.MANUAL_SNAPSHOT,
        ProviderDatasetId("lutou:weather:美国_降雨"),
        SourceLocator("snapshot:01_data/manual/weather/source_dumps/天气2.0.sql#table=美国_降雨"),
    )
    value = Provenance(
        provider,
        "source-schema-1",
        "weather-acl-1",
        datetime(2026, 8, 16, tzinfo=UTC),
        snapshot_sha256="a" * 64,
    )
    assert value.snapshot_sha256 == "a" * 64

    with pytest.raises(ValueError, match="timezone-aware"):
        Provenance(provider, "1", "1", datetime(2026, 8, 16))
    with pytest.raises(ValueError, match="SHA-256"):
        Provenance(provider, "1", "1", datetime.now(UTC), snapshot_sha256="short")


@pytest.mark.parametrize(
    "factory,value",
    [
        (DatasetId, "Tankan.Market.Price"),
        (SeriesId, "../escape"),
        (OriginSystem, "Lutou"),
        (SourceLocator, r"snapshot:C:\Users\person\secret.sql"),
        (SourceLocator, "snapshot:../secret.sql"),
        (SourceLocator, "snapshot:///absolute/secret.sql"),
        (SourceLocator, "https://example.invalid/data"),
    ],
)
def test_identity_values_fail_closed(factory, value: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((TypeError, ValueError)):
        factory(value)
