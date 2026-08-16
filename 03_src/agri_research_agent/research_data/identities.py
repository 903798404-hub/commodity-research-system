"""Stable public identities separated from provider acquisition details."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath


_PUBLIC_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_ORIGIN_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
_LOCATOR_SCHEMES = {"database", "snapshot", "extraction-source"}


def _nonempty(value: str, field_name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    result = value.strip()
    if not result or len(result) > maximum or any(ord(char) < 32 for char in result):
        raise ValueError(f"{field_name} is invalid")
    return result


def _public_id(value: str, field_name: str) -> str:
    result = _nonempty(value, field_name, maximum=200)
    if _PUBLIC_ID.fullmatch(result) is None:
        raise ValueError(f"{field_name} must be a lowercase public identifier")
    return result


@dataclass(frozen=True, slots=True)
class DatasetId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _public_id(self.value, "dataset_id"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SeriesId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _public_id(self.value, "series_id"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class OriginSystem:
    value: str

    def __post_init__(self) -> None:
        result = _nonempty(self.value, "origin_system", maximum=64)
        if _ORIGIN_ID.fullmatch(result) is None:
            raise ValueError("origin_system must be a lowercase source identifier")
        object.__setattr__(self, "value", result)

    def __str__(self) -> str:
        return self.value


class AcquisitionChannel(StrEnum):
    DIRECT_DATABASE = "direct_database"
    MANUAL_SNAPSHOT = "manual_snapshot"


@dataclass(frozen=True, slots=True)
class ProviderDatasetId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _nonempty(self.value, "provider_dataset_id"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ProviderSeriesId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _nonempty(self.value, "provider_series_id"))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SourceLocator:
    value: str

    def __post_init__(self) -> None:
        result = _nonempty(self.value, "source_locator", maximum=1024)
        scheme, separator, _ = result.partition(":")
        if not separator or scheme not in _LOCATOR_SCHEMES:
            raise ValueError("source_locator uses an unsupported logical scheme")
        if re.search(r"(?:[A-Za-z]:[\\/]|^/|\\\\)", result):
            raise ValueError("source_locator must not contain an absolute local path")
        locator_path = result.partition(":")[2].replace("\\", "/")
        if locator_path.startswith("/") or ".." in PurePosixPath(locator_path).parts:
            raise ValueError("source_locator must be repository-logical and non-traversing")
        object.__setattr__(self, "value", result)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class DatasetRef:
    dataset_id: DatasetId
    origin_system: OriginSystem

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_id, DatasetId):
            raise TypeError("dataset_id must be DatasetId")
        if not isinstance(self.origin_system, OriginSystem):
            raise TypeError("origin_system must be OriginSystem")


@dataclass(frozen=True, slots=True)
class SeriesRef:
    dataset: DatasetRef
    series_id: SeriesId

    def __post_init__(self) -> None:
        if not isinstance(self.dataset, DatasetRef):
            raise TypeError("dataset must be DatasetRef")
        if not isinstance(self.series_id, SeriesId):
            raise TypeError("series_id must be SeriesId")


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    dataset: DatasetRef
    acquisition_channel: AcquisitionChannel
    provider_dataset_id: ProviderDatasetId
    source_locator: SourceLocator
    provider_series_id: ProviderSeriesId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.dataset, DatasetRef):
            raise TypeError("dataset must be DatasetRef")
        if not isinstance(self.acquisition_channel, AcquisitionChannel):
            raise TypeError("acquisition_channel must be AcquisitionChannel")
        if not isinstance(self.provider_dataset_id, ProviderDatasetId):
            raise TypeError("provider_dataset_id must be ProviderDatasetId")
        if not isinstance(self.source_locator, SourceLocator):
            raise TypeError("source_locator must be SourceLocator")
        if self.provider_series_id is not None and not isinstance(
            self.provider_series_id, ProviderSeriesId
        ):
            raise TypeError("provider_series_id must be ProviderSeriesId or None")


@dataclass(frozen=True, slots=True)
class Provenance:
    provider: ProviderIdentity
    schema_version: str
    mapping_version: str
    captured_at: datetime
    snapshot_sha256: str | None = None
    source_row_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider, ProviderIdentity):
            raise TypeError("provider must be ProviderIdentity")
        object.__setattr__(
            self, "schema_version", _nonempty(self.schema_version, "schema_version", maximum=128)
        )
        object.__setattr__(
            self, "mapping_version", _nonempty(self.mapping_version, "mapping_version", maximum=128)
        )
        if not isinstance(self.captured_at, datetime) or self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        if self.captured_at.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        for field_name in ("snapshot_sha256", "source_row_sha256"):
            value = getattr(self, field_name)
            if value is not None and _SHA256.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a SHA-256 identity")
