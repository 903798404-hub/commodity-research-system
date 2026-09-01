"""Canonical provenance for the backend soybean contract-override gate."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import hashlib
import json
from typing import Mapping, Sequence

from .config import (
    ContractOverrideConfig,
    ContractOverrideRule,
    SoybeanImportProfitConfig,
)
from .models import CbotContract, DceContract


CONTRACT_OVERRIDE_SNAPSHOT_SCHEMA = "soybean-import-profit-contract-override/1"


class ContractOverrideSnapshotError(ValueError):
    """Raised when Release override provenance is partial or invalid."""


@dataclass(frozen=True, slots=True)
class ContractOverrideProvenance:
    status: str
    contract_override_hash: str | None
    snapshot: Mapping[str, object] | None

    @property
    def available(self) -> bool:
        return self.status == "available"


def build_contract_override_snapshot(
    config: SoybeanImportProfitConfig,
) -> ContractOverrideProvenance:
    if not isinstance(config, SoybeanImportProfitConfig):
        raise ContractOverrideSnapshotError(
            "config must be a SoybeanImportProfitConfig"
        )
    snapshot = _snapshot_for_config(config.contract_override)
    validated = validate_contract_override_snapshot(snapshot)
    return ContractOverrideProvenance(
        status="available",
        contract_override_hash=contract_override_hash(validated),
        snapshot=validated,
    )


def contract_override_hash_for_config(config: ContractOverrideConfig) -> str:
    return contract_override_hash(_snapshot_for_config(config))


def canonical_contract_override_bytes(
    snapshot: Mapping[str, object],
) -> bytes:
    validated = validate_contract_override_snapshot(snapshot)
    return json.dumps(
        validated,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def contract_override_hash(snapshot: Mapping[str, object]) -> str:
    return hashlib.sha256(
        canonical_contract_override_bytes(snapshot)
    ).hexdigest().upper()


def read_contract_override_provenance(
    manifest: Mapping[str, object],
) -> ContractOverrideProvenance:
    if not isinstance(manifest, Mapping):
        raise ContractOverrideSnapshotError("Manifest must be an object")
    has_snapshot = "contract_override_snapshot" in manifest
    has_hash = "contract_override_hash" in manifest
    if not has_snapshot and not has_hash:
        return ContractOverrideProvenance("legacy_unavailable", None, None)
    if not has_snapshot or not has_hash:
        raise ContractOverrideSnapshotError(
            "contract override snapshot and hash must be present together"
        )
    raw_snapshot = manifest["contract_override_snapshot"]
    expected_hash = manifest["contract_override_hash"]
    if not isinstance(raw_snapshot, Mapping):
        raise ContractOverrideSnapshotError(
            "contract override snapshot must be an object"
        )
    snapshot = validate_contract_override_snapshot(raw_snapshot)
    actual_hash = contract_override_hash(snapshot)
    if expected_hash != actual_hash:
        raise ContractOverrideSnapshotError(
            "contract override snapshot hash mismatch"
        )
    return ContractOverrideProvenance("available", actual_hash, snapshot)


def config_with_contract_override_provenance(
    config: SoybeanImportProfitConfig,
    provenance: ContractOverrideProvenance,
) -> SoybeanImportProfitConfig:
    if not provenance.available or provenance.snapshot is None:
        raise ContractOverrideSnapshotError(
            "legacy contract override snapshot is unavailable"
        )
    snapshot = validate_contract_override_snapshot(provenance.snapshot)
    rules = tuple(_rule_from_payload(row) for row in snapshot["rules"])
    restored = replace(
        config,
        contract_override=ContractOverrideConfig(
            enabled=snapshot["enabled"],
            rules=rules,
        ),
    )
    if restored.contract_override_hash != provenance.contract_override_hash:
        raise ContractOverrideSnapshotError(
            "Release contract override configuration could not be restored"
        )
    return restored


def validate_contract_override_snapshot(
    snapshot: Mapping[str, object],
) -> Mapping[str, object]:
    expected = {"snapshot_schema", "commodity", "enabled", "rules"}
    if not isinstance(snapshot, Mapping) or set(snapshot) != expected:
        raise ContractOverrideSnapshotError(
            "contract override snapshot fields are missing or unknown"
        )
    if snapshot["snapshot_schema"] != CONTRACT_OVERRIDE_SNAPSHOT_SCHEMA:
        raise ContractOverrideSnapshotError(
            "contract override snapshot schema is unsupported"
        )
    if snapshot["commodity"] != "soybean":
        raise ContractOverrideSnapshotError(
            "contract override snapshot commodity is invalid"
        )
    if not isinstance(snapshot["enabled"], bool):
        raise ContractOverrideSnapshotError(
            "contract override enabled state must be boolean"
        )
    raw_rules = snapshot["rules"]
    if (
        not isinstance(raw_rules, Sequence)
        or isinstance(raw_rules, (str, bytes, bytearray))
    ):
        raise ContractOverrideSnapshotError(
            "contract override rules must be an array"
        )
    rules = [_validate_rule(row) for row in raw_rules]
    rules.sort(key=_rule_sort_key)
    try:
        ContractOverrideConfig(
            enabled=snapshot["enabled"],
            rules=tuple(_rule_from_payload(rule) for rule in rules),
        )
    except Exception as exc:
        raise ContractOverrideSnapshotError(
            "contract override rules are conflicting or invalid"
        ) from exc
    return {
        "snapshot_schema": CONTRACT_OVERRIDE_SNAPSHOT_SCHEMA,
        "commodity": "soybean",
        "enabled": snapshot["enabled"],
        "rules": rules,
    }


def _snapshot_for_config(config: ContractOverrideConfig) -> Mapping[str, object]:
    if not isinstance(config, ContractOverrideConfig):
        raise ContractOverrideSnapshotError(
            "contract override config is invalid"
        )
    return {
        "snapshot_schema": CONTRACT_OVERRIDE_SNAPSHOT_SCHEMA,
        "commodity": "soybean",
        "enabled": config.enabled,
        "rules": [_rule_payload(rule) for rule in config.rules],
    }


def _rule_payload(rule: ContractOverrideRule) -> dict[str, object]:
    return {
        "origin": rule.origin,
        "shipment_year": rule.shipment_year,
        "shipment_month": rule.shipment_month,
        "effective_from_business_date": (
            rule.effective_from_business_date.isoformat()
        ),
        "effective_to_business_date": (
            None
            if rule.effective_to_business_date is None
            else rule.effective_to_business_date.isoformat()
        ),
        "contracts": {
            "cbot": _contract_payload(rule.cbot_contract),
            "soymeal": _contract_payload(rule.soymeal_contract),
            "soyoil": _contract_payload(rule.soyoil_contract),
        },
        "reason": rule.reason,
    }


def _validate_rule(value: object) -> dict[str, object]:
    expected = {
        "origin",
        "shipment_year",
        "shipment_month",
        "effective_from_business_date",
        "effective_to_business_date",
        "contracts",
        "reason",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ContractOverrideSnapshotError("override rule fields are invalid")
    origin = value["origin"]
    if not isinstance(origin, str) or not origin:
        raise ContractOverrideSnapshotError("override rule origin is invalid")
    shipment_year = _integer(value["shipment_year"], "shipment year")
    if not 1900 <= shipment_year <= 2199:
        raise ContractOverrideSnapshotError("shipment year is invalid")
    shipment_month = _month(value["shipment_month"], "shipment month")
    effective_from = _iso_date(
        value["effective_from_business_date"], "effective from"
    )
    effective_to = (
        None
        if value["effective_to_business_date"] is None
        else _iso_date(value["effective_to_business_date"], "effective to")
    )
    if effective_to is not None and effective_to < effective_from:
        raise ContractOverrideSnapshotError("override date range is invalid")
    contracts = value["contracts"]
    if not isinstance(contracts, Mapping) or set(contracts) != {
        "cbot",
        "soymeal",
        "soyoil",
    }:
        raise ContractOverrideSnapshotError("override contracts are invalid")
    normalized_contracts = {
        leg: _validate_contract(contracts[leg], leg)
        for leg in ("cbot", "soymeal", "soyoil")
    }
    if all(item is None for item in normalized_contracts.values()):
        raise ContractOverrideSnapshotError(
            "override rule must contain a contract"
        )
    reason = value["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ContractOverrideSnapshotError("override reason is invalid")
    return {
        "origin": origin,
        "shipment_year": shipment_year,
        "shipment_month": shipment_month,
        "effective_from_business_date": effective_from.isoformat(),
        "effective_to_business_date": (
            None if effective_to is None else effective_to.isoformat()
        ),
        "contracts": normalized_contracts,
        "reason": reason.strip(),
    }


def _rule_from_payload(value: Mapping[str, object]) -> ContractOverrideRule:
    contracts = value["contracts"]
    assert isinstance(contracts, Mapping)
    return ContractOverrideRule(
        origin=value["origin"],
        shipment_year=value["shipment_year"],
        shipment_month=value["shipment_month"],
        effective_from_business_date=date.fromisoformat(
            value["effective_from_business_date"]
        ),
        effective_to_business_date=(
            None
            if value["effective_to_business_date"] is None
            else date.fromisoformat(value["effective_to_business_date"])
        ),
        cbot_contract=_contract_from_payload(contracts["cbot"], "cbot"),
        soymeal_contract=_contract_from_payload(contracts["soymeal"], "soymeal"),
        soyoil_contract=_contract_from_payload(contracts["soyoil"], "soyoil"),
        reason=value["reason"],
    )


def _contract_payload(
    contract: CbotContract | DceContract | None,
) -> dict[str, int] | None:
    if contract is None:
        return None
    return {
        "contract_year": contract.contract_year,
        "contract_month": contract.contract_month,
    }


def _validate_contract(value: object, leg: str) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "contract_year",
        "contract_month",
    }:
        raise ContractOverrideSnapshotError(f"{leg} contract is invalid")
    year = _integer(value["contract_year"], f"{leg} contract year")
    month = _month(value["contract_month"], f"{leg} contract month")
    try:
        _contract_from_values(year, month, leg)
    except Exception as exc:
        raise ContractOverrideSnapshotError(
            f"{leg} contract is invalid"
        ) from exc
    return {"contract_year": year, "contract_month": month}


def _contract_from_payload(
    value: object,
    leg: str,
) -> CbotContract | DceContract | None:
    if value is None:
        return None
    assert isinstance(value, Mapping)
    return _contract_from_values(
        value["contract_year"], value["contract_month"], leg
    )


def _contract_from_values(
    year: int,
    month: int,
    leg: str,
) -> CbotContract | DceContract:
    if leg == "cbot":
        return CbotContract(year, month)
    if leg == "soymeal":
        return DceContract.soymeal(year, month)
    return DceContract.soyoil(year, month)


def _rule_sort_key(rule: Mapping[str, object]) -> tuple[object, ...]:
    contracts = rule["contracts"]
    assert isinstance(contracts, Mapping)
    return (
        rule["origin"],
        rule["shipment_year"],
        rule["shipment_month"],
        rule["effective_from_business_date"],
        rule["effective_to_business_date"] or "9999-12-31",
        json.dumps(contracts, sort_keys=True, separators=(",", ":")),
        rule["reason"],
    )


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractOverrideSnapshotError(f"{label} must be an integer")
    return value


def _month(value: object, label: str) -> int:
    result = _integer(value, label)
    if not 1 <= result <= 12:
        raise ContractOverrideSnapshotError(f"{label} is invalid")
    return result


def _iso_date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ContractOverrideSnapshotError(f"{label} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ContractOverrideSnapshotError(
            f"{label} must be an ISO date"
        ) from exc
