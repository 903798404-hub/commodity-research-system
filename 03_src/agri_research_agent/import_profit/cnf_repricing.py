"""Pure, exact-key soybean repricing for a manual CNF override."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

import pyarrow as pa

from .config import SoybeanImportProfitConfig
from .contract_mapping import map_soybean_contracts
from .models import BusinessKey
from .query import (
    HISTORICAL_BUSINESS_KEY_SCHEMA,
    CanonicalKey,
    SoybeanQueryRecord,
)
from .result_store import RESULT_SCHEMA, SNAPSHOT_SCHEMA
from .soybean import (
    SoybeanCalculationInput,
    calculate_soybean_net_crush_margin,
)


class CnfRepricingError(ValueError):
    """Raised when one persisted row cannot be safely repriced."""


@dataclass(frozen=True, slots=True)
class CnfRepricingResult:
    business_key: BusinessKey
    updated_business_key_row: Mapping[str, object]
    updated_snapshot_row: Mapping[str, object]
    updated_result_row: Mapping[str, object]
    previous_cnf: float | None
    new_cnf: float | None
    previous_cnf_source: str | None
    new_cnf_source: str
    previous_status: str
    new_status: str
    previous_missing_reasons: tuple[str, ...]
    new_missing_reasons: tuple[str, ...]
    calculated_at: datetime


def reprice_query_record_with_manual_cnf(
    record: SoybeanQueryRecord,
    *,
    current_business_key_row: Mapping[str, object],
    current_snapshot_row: Mapping[str, object],
    config: SoybeanImportProfitConfig,
    cnf_cents_per_bushel: float | None,
    calculated_at: datetime,
) -> CnfRepricingResult:
    """Replace only CNF, preserving every other persisted snapshot field."""

    if not isinstance(record, SoybeanQueryRecord):
        raise CnfRepricingError("record must be a SoybeanQueryRecord")
    calculated_at = _utc_datetime(calculated_at)
    _validate_exact_row(
        current_business_key_row,
        HISTORICAL_BUSINESS_KEY_SCHEMA,
        "business key",
    )
    _validate_exact_row(
        current_snapshot_row, SNAPSHOT_SCHEMA, "market snapshot"
    )
    _validate_row_key(current_business_key_row, record.key, "business key")
    _validate_row_key(current_snapshot_row, record.key, "market snapshot")

    mapped = map_soybean_contracts(
        config, record.shipment_year, record.shipment_month
    )
    if (
        record.cbot_contract != mapped.cbot.label
        or record.soymeal_contract != mapped.soymeal.code
        or record.soyoil_contract != mapped.soyoil.code
        or record.mapping_identity != mapped.mapping_identity
        or record.parameter_version != str(config.schema_version)
    ):
        raise CnfRepricingError(
            "persisted contracts or identities do not match configuration"
        )
    key = BusinessKey(
        record.business_date,
        record.commodity,
        record.origin,
        record.shipment_year,
        record.shipment_month,
        config.origin_codes,
        config.commodity,
        record.shipment_period,
    )
    calculation = calculate_soybean_net_crush_margin(
        SoybeanCalculationInput(
            business_key=key,
            cnf_cents_per_bushel=cnf_cents_per_bushel,
            cbot_contract=mapped.cbot,
            cbot_daily_price_cents_per_bushel=(
                record.cbot_price_cents_per_bushel
            ),
            fx_value=record.fx_value,
            soymeal_contract=mapped.soymeal,
            soymeal_price_cny_per_tonne=(
                record.soymeal_price_cny_per_tonne
            ),
            soyoil_contract=mapped.soyoil,
            soyoil_price_cny_per_tonne=(
                record.soyoil_price_cny_per_tonne
            ),
            resolved_parameters=config.resolve_parameters(record.origin),
            mapping_identity=record.mapping_identity,
        ),
        config,
    )
    reasons = tuple(reason.value for reason in calculation.missing_reasons)
    new_status = calculation.calculation_status.value

    key_row = dict(current_business_key_row)
    key_row["cnf_is_null"] = cnf_cents_per_bushel is None
    key_row["cnf_source"] = "manual_ui"

    snapshot_row = dict(current_snapshot_row)
    snapshot_row["cnf_cents_per_bushel"] = cnf_cents_per_bushel
    snapshot_row["cnf_source"] = "manual_ui"
    snapshot_row["snapshot_status"] = (
        "complete" if new_status == "success" else "incomplete"
    )
    snapshot_row["missing_reasons"] = list(reasons)

    result_row = {
        "business_date": record.business_date,
        "commodity": record.commodity,
        "origin": record.origin,
        "shipment_year": record.shipment_year,
        "shipment_month": record.shipment_month,
        "shipment_period": record.shipment_period,
        "usd_cost_per_tonne": calculation.usd_cost_per_tonne,
        "duty_paid_cost_cny_per_tonne": (
            calculation.duty_paid_cost_cny_per_tonne
        ),
        "net_crush_margin_cny_per_tonne": (
            calculation.net_crush_margin_cny_per_tonne
        ),
        "calculation_status": new_status,
        "missing_reasons": list(reasons),
        "parameter_version": calculation.parameter_version,
        "mapping_identity": calculation.mapping_identity,
        "calculated_at": calculated_at,
    }
    _validate_arrow_row(key_row, HISTORICAL_BUSINESS_KEY_SCHEMA)
    _validate_arrow_row(snapshot_row, SNAPSHOT_SCHEMA)
    _validate_arrow_row(result_row, RESULT_SCHEMA)
    return CnfRepricingResult(
        business_key=key,
        updated_business_key_row=MappingProxyType(key_row),
        updated_snapshot_row=MappingProxyType(snapshot_row),
        updated_result_row=MappingProxyType(result_row),
        previous_cnf=record.cnf_cents_per_bushel,
        new_cnf=(
            None
            if cnf_cents_per_bushel is None
            else float(cnf_cents_per_bushel)
        ),
        previous_cnf_source=record.cnf_source,
        new_cnf_source="manual_ui",
        previous_status=record.calculation_status,
        new_status=new_status,
        previous_missing_reasons=record.missing_reasons,
        new_missing_reasons=reasons,
        calculated_at=calculated_at,
    )


def _validate_exact_row(
    row: Mapping[str, object], schema: pa.Schema, label: str
) -> None:
    if not isinstance(row, Mapping) or tuple(row) != tuple(schema.names):
        raise CnfRepricingError(
            f"{label} row does not match the fixed schema and field order"
        )


def _validate_row_key(
    row: Mapping[str, object], expected: CanonicalKey, label: str
) -> None:
    actual = (
        row["business_date"],
        row["commodity"],
        row["origin"],
        row["shipment_year"],
        row["shipment_month"],
    )
    if actual != expected:
        raise CnfRepricingError(f"{label} row key does not match record")


def _validate_arrow_row(row: dict[str, object], schema: pa.Schema) -> None:
    try:
        table = pa.Table.from_pylist([row], schema=schema)
    except Exception as exc:
        raise CnfRepricingError("repriced row violates fixed schema") from exc
    if table.schema != schema or table.num_rows != 1:
        raise CnfRepricingError("repriced row failed Arrow validation")


def _utc_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CnfRepricingError("calculated_at must be timezone-aware")
    return value.astimezone(timezone.utc)
