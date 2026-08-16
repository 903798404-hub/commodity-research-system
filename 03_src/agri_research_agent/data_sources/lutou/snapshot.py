"""Bind Lutou business identities to content-addressed manual snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from agri_research_agent.research_data import (
    AcquisitionChannel,
    CatalogDataset,
    DataAssetCatalog,
    OriginSystem,
    Provenance,
    ProviderIdentity,
)
from agri_research_agent.shared.file_identity import FileIdentity, identify_file


class LutouSnapshotError(ValueError):
    """Raised when a file cannot prove its approved Lutou snapshot identity."""


_REGISTRY_TOKEN = object()


@dataclass(frozen=True, slots=True)
class SnapshotCapture:
    asset: CatalogDataset
    file_identity: FileIdentity
    provenance: Provenance
    _path: Path = field(repr=False)

    @property
    def source_path(self) -> Path:
        """Runtime-only path; never include it in a manifest."""

        return self._path

    def safe_manifest_fields(self) -> dict[str, object]:
        provider = self.provenance.provider
        return {
            "dataset_id": str(provider.dataset.dataset_id),
            "provider_dataset_id": str(provider.provider_dataset_id),
            "origin_system": str(provider.dataset.origin_system),
            "acquisition_channel": provider.acquisition_channel.value,
            "source_locator": str(provider.source_locator),
            "snapshot_sha256": self.file_identity.sha256,
            "snapshot_size_bytes": self.file_identity.size_bytes,
            "schema_version": self.provenance.schema_version,
            "mapping_version": self.provenance.mapping_version,
            "captured_at": self.provenance.captured_at.isoformat(),
        }


class LutouSnapshotRegistry:
    """Gate-A-approved mapping from logical snapshot locators to content hashes."""

    def __init__(
        self,
        *,
        catalog: DataAssetCatalog,
        approved_sha256: Mapping[str, str],
        _token: object,
    ) -> None:
        if _token is not _REGISTRY_TOKEN or catalog.status != "HUMAN_GATE_A_APPROVED":
            raise TypeError("LutouSnapshotRegistry requires a Gate A approved loader")
        self.catalog = catalog
        self.approved_sha256 = MappingProxyType(dict(approved_sha256))
        self._identifier = identify_file

    @classmethod
    def load_gate_a_approved(
        cls,
        catalog_path: str | Path,
        approval_path: str | Path,
    ) -> "LutouSnapshotRegistry":
        catalog_file = Path(catalog_path)
        catalog = DataAssetCatalog.load_gate_a_approved(catalog_file, approval_path)
        try:
            payload = json.loads(catalog_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LutouSnapshotError(
                f"approved catalog cannot be read: {type(exc).__name__}"
            ) from None
        audit_sources = payload.get("audit_sources")
        if not isinstance(audit_sources, list):
            raise LutouSnapshotError("approved catalog has no audit sources")
        approved: dict[str, str] = {}
        for item in audit_sources:
            if not isinstance(item, dict):
                raise LutouSnapshotError("approved audit source is invalid")
            if (
                item.get("origin_system") != "lutou"
                or item.get("acquisition_channel") != AcquisitionChannel.MANUAL_SNAPSHOT.value
            ):
                continue
            locator = item.get("source_locator")
            sha256 = item.get("source_sha256")
            if not isinstance(locator, str) or not isinstance(sha256, str):
                raise LutouSnapshotError("Lutou audit source identity is incomplete")
            normalized_sha = sha256.lower()
            try:
                FileIdentity(normalized_sha, 0)
            except ValueError as exc:
                raise LutouSnapshotError("Lutou audit source SHA-256 is invalid") from exc
            if locator in approved and approved[locator] != normalized_sha:
                raise LutouSnapshotError("Lutou audit source locator is ambiguous")
            approved[locator] = normalized_sha
        if not approved:
            raise LutouSnapshotError("approved catalog contains no Lutou snapshots")
        return cls(
            catalog=catalog,
            approved_sha256=approved,
            _token=_REGISTRY_TOKEN,
        )

    def capture_many(
        self,
        dataset_ids: Sequence[str],
        source_path: str | Path,
        *,
        captured_at: datetime | None = None,
    ) -> tuple[SnapshotCapture, ...]:
        if (
            isinstance(dataset_ids, (str, bytes))
            or not dataset_ids
            or len(dataset_ids) != len(set(dataset_ids))
        ):
            raise LutouSnapshotError("dataset_ids must be a non-empty unique sequence")
        assets = tuple(self.catalog.get(dataset_id) for dataset_id in dataset_ids)
        logical_locators = {self._snapshot_root(asset) for asset in assets}
        if len(logical_locators) != 1:
            raise LutouSnapshotError("datasets do not share one approved snapshot")
        logical_locator = next(iter(logical_locators))
        expected_sha = self.approved_sha256.get(logical_locator)
        if expected_sha is None:
            raise LutouSnapshotError("dataset snapshot is not in Gate A audit evidence")
        try:
            identity = self._identifier(source_path)
        except (OSError, ValueError) as exc:
            raise LutouSnapshotError(
                f"snapshot identity failed: {type(exc).__name__}"
            ) from None
        if identity.sha256.lower() != expected_sha:
            raise LutouSnapshotError("snapshot bytes do not match Gate A evidence")
        timestamp = captured_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise LutouSnapshotError("captured_at must be timezone-aware")
        return tuple(
            self._capture(asset, identity, Path(source_path), timestamp) for asset in assets
        )

    @staticmethod
    def _snapshot_root(asset: CatalogDataset) -> str:
        if asset.dataset.origin_system != OriginSystem("lutou"):
            raise LutouSnapshotError("catalog asset origin is not Lutou")
        if asset.acquisition_channel is not AcquisitionChannel.MANUAL_SNAPSHOT:
            raise LutouSnapshotError("catalog asset is not a manual snapshot")
        locator = str(asset.source_locator)
        root = locator.partition("#")[0]
        if not root.startswith("snapshot:"):
            raise LutouSnapshotError("catalog asset has no snapshot locator")
        return root

    @staticmethod
    def _capture(
        asset: CatalogDataset,
        identity: FileIdentity,
        path: Path,
        captured_at: datetime,
    ) -> SnapshotCapture:
        provider = ProviderIdentity(
            dataset=asset.dataset,
            acquisition_channel=asset.acquisition_channel,
            provider_dataset_id=asset.provider_dataset_id,
            source_locator=asset.source_locator,
        )
        provenance = Provenance(
            provider=provider,
            schema_version=str(asset.raw["schema_version"]),
            mapping_version="lutou_snapshot_acl/1",
            captured_at=captured_at,
            snapshot_sha256=identity.sha256.lower(),
        )
        return SnapshotCapture(asset, identity, provenance, path)
