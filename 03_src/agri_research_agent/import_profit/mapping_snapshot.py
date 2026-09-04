"""Canonical contract-mapping provenance for immutable soybean Releases."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Mapping, Sequence

from .config import (
    ContractLegRule,
    ContractMappingRule,
    SoybeanImportProfitConfig,
)


MAPPING_SNAPSHOT_SCHEMA = "soybean-import-profit-contract-mapping/1"
CONTRACT_YEAR_EXPRESSION = "shipment_year + year_offset"
CODE_POLICY = "month_and_year_offset_only"


class MappingSnapshotError(ValueError):
    """Raised when mapping provenance is missing, partial, or invalid."""


@dataclass(frozen=True, slots=True)
class MappingProvenance:
    status: str
    mapping_hash: str | None
    snapshot: Mapping[str, object] | None

    @property
    def available(self) -> bool:
        return self.status == "available"


def build_mapping_snapshot(
    config: SoybeanImportProfitConfig,
) -> MappingProvenance:
    """Freeze every rule that affects shipment contract resolution."""

    if not isinstance(config, SoybeanImportProfitConfig):
        raise MappingSnapshotError(
            "config must be a SoybeanImportProfitConfig"
        )
    snapshot = _snapshot_for_rules(config.commodity, config.contract_mapping)
    validated = validate_mapping_snapshot(snapshot)
    return MappingProvenance(
        status="available",
        mapping_hash=mapping_hash(validated),
        snapshot=validated,
    )


def mapping_hash_for_rules(
    commodity: str,
    rules: Sequence[ContractMappingRule],
) -> str:
    return mapping_hash(_snapshot_for_rules(commodity, rules))


def canonical_mapping_bytes(snapshot: Mapping[str, object]) -> bytes:
    validated = validate_mapping_snapshot(snapshot)
    return json.dumps(
        validated,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def mapping_hash(snapshot: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_mapping_bytes(snapshot)).hexdigest().upper()


def read_mapping_provenance(
    manifest: Mapping[str, object],
) -> MappingProvenance:
    """Read sealed mapping provenance without current-config fallback."""

    if not isinstance(manifest, Mapping):
        raise MappingSnapshotError("Manifest must be an object")
    has_snapshot = "mapping_snapshot" in manifest
    has_hash = "mapping_hash" in manifest
    if not has_snapshot and not has_hash:
        return MappingProvenance("legacy_unavailable", None, None)
    if not has_snapshot or not has_hash:
        raise MappingSnapshotError(
            "mapping snapshot and hash must be present together"
        )
    snapshot = manifest["mapping_snapshot"]
    expected_hash = manifest["mapping_hash"]
    if not isinstance(snapshot, Mapping):
        raise MappingSnapshotError("mapping snapshot must be an object")
    validated = validate_mapping_snapshot(snapshot)
    actual_hash = mapping_hash(validated)
    if expected_hash != actual_hash:
        raise MappingSnapshotError("mapping snapshot hash mismatch")
    return MappingProvenance("available", actual_hash, validated)


def config_with_mapping_provenance(
    config: SoybeanImportProfitConfig,
    provenance: MappingProvenance,
) -> SoybeanImportProfitConfig:
    """Restore mapping rules from a Release while preserving other config."""

    if not provenance.available or provenance.snapshot is None:
        raise MappingSnapshotError("legacy mapping snapshot is unavailable")
    validated = validate_mapping_snapshot(provenance.snapshot)
    rows = validated["rows"]
    assert isinstance(rows, list)
    rules = tuple(
        ContractMappingRule(
            shipment_month=row["shipment_month"],
            cbot=ContractLegRule(**row["cbot"]),
            dce=ContractLegRule(**row["soymeal"]),
        )
        for row in rows
    )
    restored = replace(config, contract_mapping=rules)
    if restored.contract_mapping_hash != provenance.mapping_hash:
        raise MappingSnapshotError(
            "Release mapping could not be restored exactly"
        )
    return restored


def validate_mapping_snapshot(
    snapshot: Mapping[str, object],
) -> Mapping[str, object]:
    expected = {
        "snapshot_schema",
        "commodity",
        "contract_year_expression",
        "code_policy",
        "rows",
    }
    if not isinstance(snapshot, Mapping) or set(snapshot) != expected:
        raise MappingSnapshotError(
            "mapping snapshot fields are missing or unknown"
        )
    if snapshot["snapshot_schema"] != MAPPING_SNAPSHOT_SCHEMA:
        raise MappingSnapshotError("mapping snapshot schema is unsupported")
    if snapshot["commodity"] != "soybean":
        raise MappingSnapshotError("mapping snapshot commodity is invalid")
    if snapshot["contract_year_expression"] != CONTRACT_YEAR_EXPRESSION:
        raise MappingSnapshotError("contract year expression is invalid")
    if snapshot["code_policy"] != CODE_POLICY:
        raise MappingSnapshotError("mapping code policy is invalid")
    raw_rows = snapshot["rows"]
    if (
        not isinstance(raw_rows, Sequence)
        or isinstance(raw_rows, (str, bytes, bytearray))
    ):
        raise MappingSnapshotError("mapping rows must be an array")
    normalized = []
    for row in raw_rows:
        if not isinstance(row, Mapping) or set(row) != {
            "shipment_month",
            "cbot",
            "soymeal",
            "soyoil",
        }:
            raise MappingSnapshotError("mapping row fields are invalid")
        shipment_month = _month(row["shipment_month"], "shipment month")
        cbot = _leg(row["cbot"], "CBOT")
        soymeal = _leg(row["soymeal"], "soymeal")
        soyoil = _leg(row["soyoil"], "soyoil")
        if soymeal != soyoil:
            raise MappingSnapshotError(
                "current soybean mapping requires shared DCE meal/oil rules"
            )
        normalized.append(
            {
                "shipment_month": shipment_month,
                "cbot": cbot,
                "soymeal": soymeal,
                "soyoil": soyoil,
            }
        )
    months = [row["shipment_month"] for row in normalized]
    if sorted(months) != list(range(1, 13)) or len(set(months)) != 12:
        raise MappingSnapshotError(
            "mapping snapshot must contain each shipment month exactly once"
        )
    return {
        "snapshot_schema": MAPPING_SNAPSHOT_SCHEMA,
        "commodity": "soybean",
        "contract_year_expression": CONTRACT_YEAR_EXPRESSION,
        "code_policy": CODE_POLICY,
        "rows": sorted(normalized, key=lambda row: row["shipment_month"]),
    }


def _snapshot_for_rules(
    commodity: str,
    rules: Sequence[ContractMappingRule],
) -> Mapping[str, object]:
    return {
        "snapshot_schema": MAPPING_SNAPSHOT_SCHEMA,
        "commodity": commodity,
        "contract_year_expression": CONTRACT_YEAR_EXPRESSION,
        "code_policy": CODE_POLICY,
        "rows": [
            {
                "shipment_month": rule.shipment_month,
                "cbot": _leg_payload(rule.cbot),
                "soymeal": _leg_payload(rule.dce),
                "soyoil": _leg_payload(rule.dce),
            }
            for rule in rules
        ],
    }


def _leg_payload(rule: ContractLegRule) -> dict[str, int]:
    return {
        "contract_month": rule.contract_month,
        "year_offset": rule.year_offset,
    }


def _leg(value: object, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != {
        "contract_month",
        "year_offset",
    }:
        raise MappingSnapshotError(f"{label} mapping leg fields are invalid")
    month = _month(value["contract_month"], f"{label} contract month")
    offset = value["year_offset"]
    if isinstance(offset, bool) or not isinstance(offset, int) or offset not in {
        0,
        1,
    }:
        raise MappingSnapshotError(f"{label} year offset is invalid")
    return {"contract_month": month, "year_offset": offset}


def _month(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
        raise MappingSnapshotError(f"{label} must be between 1 and 12")
    return value
