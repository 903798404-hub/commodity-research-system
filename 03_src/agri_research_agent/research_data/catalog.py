"""Read-only access to the approved candidate data asset catalog."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Mapping

from .identities import (
    AcquisitionChannel,
    DatasetId,
    DatasetRef,
    OriginSystem,
    ProviderDatasetId,
    ProviderSeriesId,
    SourceLocator,
)


CATALOG_SCHEMA_VERSION = "0.1-candidate"
CANDIDATE_CATALOG_STATUS = "HUMAN_GATE_A_PENDING"
APPROVAL_STATUS = "HUMAN_GATE_A_APPROVED"
_CONSTRUCTION_TOKEN = object()
_CANDIDATE_CANONICALIZATION_STATUSES = {"cataloged_only", "existing_acl"}


class CatalogError(ValueError):
    """Raised when a catalog cannot prove its declared identity or completeness."""


@dataclass(frozen=True, slots=True)
class ProviderSeriesCandidate:
    provider_series_id: ProviderSeriesId
    source_column: str
    canonicalization_status: str

    @property
    def candidate_only(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class CatalogDataset:
    dataset: DatasetRef
    acquisition_channel: AcquisitionChannel
    provider_dataset_id: ProviderDatasetId
    source_locator: SourceLocator
    domain: str
    asset_class: str
    canonicalization_status: str
    provider_series: tuple[ProviderSeriesCandidate, ...]
    raw: Mapping[str, Any]

    @property
    def provider_series_count(self) -> int:
        return len(self.provider_series)

    @property
    def candidate_only(self) -> bool:
        return True

    @property
    def is_promotable(self) -> bool:
        return False


class DataAssetCatalog:
    """Validated, immutable view of the Gate A candidate manifest.

    The catalog discovers identities and source metadata only. It deliberately
    does not read source data, select providers, calculate values, or promote a
    candidate to a canonical dataset.
    """

    def __init__(
        self,
        *,
        schema_version: str,
        status: str,
        source_catalog_status: str,
        datasets: tuple[CatalogDataset, ...],
        declared_summary: Mapping[str, Any],
        _token: object,
    ) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError("DataAssetCatalog must be created by a validated loader")
        self.schema_version = schema_version
        self.status = status
        self.source_catalog_status = source_catalog_status
        self.datasets = datasets
        self.declared_summary = _freeze(dict(declared_summary))
        self._by_id = MappingProxyType({str(item.dataset.dataset_id): item for item in datasets})

    @classmethod
    def load_candidate_for_audit(cls, path: str | Path) -> "DataAssetCatalog":
        """Load the pending Gate A artifact for audit tooling only."""

        payload = _read_json(Path(path), "catalog")
        return cls._from_candidate_payload(payload, status=CANDIDATE_CATALOG_STATUS)

    @classmethod
    def load_gate_a_approved(
        cls,
        path: str | Path,
        approval_path: str | Path,
    ) -> "DataAssetCatalog":
        """Load approved discovery metadata, never production-promotable assets."""

        catalog_path = Path(path)
        payload = _read_json(catalog_path, "catalog")
        approval = _read_json(Path(approval_path), "catalog approval")
        _validate_approval(catalog_path, payload, approval)
        return cls._from_candidate_payload(payload, status=APPROVAL_STATUS)

    @classmethod
    def _from_candidate_payload(
        cls,
        payload: Any,
        *,
        status: str,
    ) -> "DataAssetCatalog":
        if not isinstance(payload, dict):
            raise CatalogError("catalog root must be an object")
        _validate_safe_payload(payload)
        if payload.get("catalog_schema_version") != CATALOG_SCHEMA_VERSION:
            raise CatalogError("unsupported candidate catalog schema")
        if payload.get("status") != CANDIDATE_CATALOG_STATUS:
            raise CatalogError("candidate catalog status is invalid")
        raw_datasets = payload.get("datasets")
        summary = payload.get("summary")
        if not isinstance(raw_datasets, list) or not isinstance(summary, dict):
            raise CatalogError("catalog datasets or summary are invalid")

        datasets = tuple(_dataset(item) for item in raw_datasets)
        ids = [str(item.dataset.dataset_id) for item in datasets]
        if len(ids) != len(set(ids)):
            raise CatalogError("dataset_id candidates are not unique")
        provider_series_ids = [
            str(series["provider_series_id_candidate"])
            for item in datasets
            for series in item.raw["provider_series_candidates"]
        ]
        if len(provider_series_ids) != len(set(provider_series_ids)):
            raise CatalogError("provider series candidates are not unique")
        _validate_summary(datasets, summary)
        return cls(
            schema_version=payload["catalog_schema_version"],
            status=status,
            source_catalog_status=payload["status"],
            datasets=datasets,
            declared_summary=summary,
            _token=_CONSTRUCTION_TOKEN,
        )

    def get(self, dataset_id: DatasetId | str) -> CatalogDataset:
        key = str(dataset_id) if isinstance(dataset_id, DatasetId) else str(DatasetId(dataset_id))
        try:
            return self._by_id[key]
        except KeyError:
            raise KeyError(f"unknown dataset_id: {key}") from None

    def find(
        self,
        *,
        origin_system: OriginSystem | str | None = None,
        domain: str | None = None,
    ) -> tuple[CatalogDataset, ...]:
        origin = None
        if origin_system is not None:
            origin = str(
                origin_system
                if isinstance(origin_system, OriginSystem)
                else OriginSystem(origin_system)
            )
        if domain is not None:
            domain = domain.strip()
            if not domain:
                raise ValueError("domain cannot be blank")
        return tuple(
            item
            for item in self.datasets
            if (origin is None or str(item.dataset.origin_system) == origin)
            and (domain is None or item.domain == domain)
        )

    def iter_promotable(self) -> tuple[CatalogDataset, ...]:
        """Return explicitly canonicalized assets; Gate A discovery has none."""

        return tuple(item for item in self.datasets if item.is_promotable)

    def require_promotable(self, dataset_id: DatasetId | str) -> CatalogDataset:
        item = self.get(dataset_id)
        if not item.is_promotable:
            raise CatalogError(f"dataset is candidate-only and not promotable: {item.dataset.dataset_id}")
        return item


def _dataset(value: Any) -> CatalogDataset:
    if not isinstance(value, dict):
        raise CatalogError("catalog dataset must be an object")
    required = {
        "dataset_id_candidate",
        "origin_system",
        "acquisition_channel",
        "provider_dataset_id_candidate",
        "source_locator",
        "domain",
        "asset_class",
        "canonicalization_status",
        "provider_series_count",
        "provider_series_candidates",
        "identity_status",
        "canonical_contract_candidate",
    }
    if not required <= value.keys():
        raise CatalogError("catalog dataset is missing required identity fields")
    try:
        dataset_ref = DatasetRef(
            DatasetId(value["dataset_id_candidate"]),
            OriginSystem(value["origin_system"]),
        )
        acquisition = AcquisitionChannel(value["acquisition_channel"])
        provider_dataset_id = ProviderDatasetId(value["provider_dataset_id_candidate"])
        locator = SourceLocator(value["source_locator"])
    except (TypeError, ValueError) as exc:
        raise CatalogError(f"catalog dataset identity is invalid: {exc}") from None
    if acquisition.value in str(dataset_ref.dataset_id):
        raise CatalogError("dataset identity contains acquisition channel")
    if value["identity_status"] != "candidate":
        raise CatalogError("Gate A catalog dataset must remain candidate identity")
    for field_name in ("domain", "asset_class", "canonicalization_status"):
        if not isinstance(value[field_name], str) or not value[field_name].strip():
            raise CatalogError(f"catalog dataset {field_name} is invalid")
    if value["canonicalization_status"] not in _CANDIDATE_CANONICALIZATION_STATUSES:
        raise CatalogError("catalog dataset canonicalization status is invalid")
    series = value["provider_series_candidates"]
    count = value["provider_series_count"]
    if not isinstance(series, list) or type(count) is not int or count < 0 or len(series) != count:
        raise CatalogError("provider series count does not match its manifest")
    source_columns = value.get("source_columns")
    if not isinstance(source_columns, list) or not all(
        isinstance(item, dict) and isinstance(item.get("name"), str)
        for item in source_columns
    ):
        raise CatalogError("source columns are invalid")
    source_column_names = {item["name"] for item in source_columns}
    typed_series: list[ProviderSeriesCandidate] = []
    for item in series:
        if not isinstance(item, dict) or not isinstance(
            item.get("provider_series_id_candidate"), str
        ):
            raise CatalogError("provider series candidate is invalid")
        try:
            provider_series_id = ProviderSeriesId(item["provider_series_id_candidate"])
        except (TypeError, ValueError) as exc:
            raise CatalogError(f"provider series identity is invalid: {exc}") from None
        if item.get("source_column") not in source_column_names:
            raise CatalogError("provider series references an unknown source column")
        if item.get("series_id_candidate") is not None:
            raise CatalogError("unapproved canonical series identity found in candidate catalog")
        series_status = item.get("canonicalization_status")
        if not isinstance(series_status, str) or not series_status.startswith(
            ("requires_", "blocked_")
        ):
            raise CatalogError("provider series canonicalization status is invalid")
        typed_series.append(
            ProviderSeriesCandidate(
                provider_series_id=provider_series_id,
                source_column=item["source_column"],
                canonicalization_status=series_status,
            )
        )
    return CatalogDataset(
        dataset=dataset_ref,
        acquisition_channel=acquisition,
        provider_dataset_id=provider_dataset_id,
        source_locator=locator,
        domain=str(value["domain"]),
        asset_class=str(value["asset_class"]),
        canonicalization_status=str(value["canonicalization_status"]),
        provider_series=tuple(typed_series),
        raw=_freeze(dict(value)),
    )


def _validate_summary(datasets: tuple[CatalogDataset, ...], summary: Mapping[str, Any]) -> None:
    if summary.get("dataset_count") != len(datasets):
        raise CatalogError("declared dataset count does not match catalog")
    origin_counts: dict[str, int] = {}
    series_count = 0
    for item in datasets:
        origin = str(item.dataset.origin_system)
        origin_counts[origin] = origin_counts.get(origin, 0) + 1
        series_count += item.provider_series_count
    if summary.get("datasets_by_origin") != dict(sorted(origin_counts.items())):
        raise CatalogError("declared origin counts do not match catalog")
    if summary.get("provider_series_candidate_count") != series_count:
        raise CatalogError("declared provider series count does not match catalog")
    domain_counts: dict[str, int] = {}
    for item in datasets:
        domain_counts[item.domain] = domain_counts.get(item.domain, 0) + 1
    if summary.get("datasets_by_domain") != dict(sorted(domain_counts.items())):
        raise CatalogError("declared domain counts do not match catalog")
    unclassified = sum(
        item.domain == "unclassified"
        or item.raw["canonical_contract_candidate"] == "unclassified"
        for item in datasets
    )
    if summary.get("unclassified_dataset_count") != unclassified:
        raise CatalogError("declared unclassified count does not match catalog")


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"{label} cannot be read: {type(exc).__name__}") from None


def _validate_approval(
    catalog_path: Path,
    catalog: Any,
    approval: Any,
) -> None:
    if not isinstance(approval, dict) or set(approval) != {
        "approval_schema_version",
        "status",
        "approved_on",
        "candidate_catalog_path",
        "candidate_catalog_sha256",
        "candidate_catalog_sha256_mode",
        "candidate_catalog_schema_version",
        "dataset_count",
        "provider_series_candidate_count",
        "approval_scope",
        "approval_does_not_mean",
        "frozen_identity_invariants",
    }:
        raise CatalogError("catalog approval fields are invalid")
    if approval["approval_schema_version"] != 1 or approval["status"] != APPROVAL_STATUS:
        raise CatalogError("catalog approval status is invalid")
    expected_sha = approval["candidate_catalog_sha256"]
    if approval["candidate_catalog_sha256_mode"] != "utf8_lf_normalized":
        raise CatalogError("catalog approval hash mode is invalid")
    normalized_bytes = catalog_path.read_bytes().replace(b"\r\n", b"\n")
    actual_sha = hashlib.sha256(normalized_bytes).hexdigest()
    if not isinstance(expected_sha, str) or expected_sha.lower() != actual_sha:
        raise CatalogError("catalog bytes do not match Gate A approval")
    if not isinstance(catalog, dict):
        raise CatalogError("catalog root must be an object")
    if approval["candidate_catalog_schema_version"] != catalog.get("catalog_schema_version"):
        raise CatalogError("approved catalog schema does not match candidate")
    summary = catalog.get("summary")
    if not isinstance(summary, dict):
        raise CatalogError("catalog summary is invalid")
    if approval["dataset_count"] != summary.get("dataset_count"):
        raise CatalogError("approved dataset count does not match candidate")
    if approval["provider_series_candidate_count"] != summary.get(
        "provider_series_candidate_count"
    ):
        raise CatalogError("approved provider series count does not match candidate")
    logical_path = approval["candidate_catalog_path"]
    if (
        not isinstance(logical_path, str)
        or PurePosixPath(logical_path).is_absolute()
        or PureWindowsPath(logical_path).is_absolute()
        or ".." in PurePosixPath(logical_path.replace("\\", "/")).parts
    ):
        raise CatalogError("approved catalog path must be repository-relative")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _validate_safe_payload(value: Any) -> None:
    forbidden_keys = {
        "password",
        "passwd",
        "secret",
        "client_secret",
        "private_key",
        "token",
        "access_token",
        "api_key",
        "apikey",
        "dsn",
        "host",
        "user",
        "username",
    }
    absolute_path = re.compile(
        r"(?:[A-Za-z]:[\\/]Users[\\/]|/home/[^/]+/|/Users/[^/]+/)",
        re.IGNORECASE,
    )

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if str(key).casefold() in forbidden_keys:
                    raise CatalogError("catalog contains a forbidden sensitive field")
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
        elif isinstance(item, str) and absolute_path.search(item):
            raise CatalogError("catalog contains an absolute user path")

    visit(value)
