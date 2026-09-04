"""Pure targeted recalculation orchestration for explicit soybean business keys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .business_days import require_business_weekday
from .cnf_store import CnfQuoteRecord, business_key_tuple
from .config import SoybeanImportProfitConfig
from .market_snapshot import (
    CbotPricePoint,
    DcePricePoint,
    FxPricePoint,
    MARKET_MISSING_REASON_ORDER,
    SoybeanMarketSnapshot,
    build_soybean_market_snapshot_from_index,
    index_market_data,
    snapshot_to_calculation_input,
)
from .models import (
    BusinessKey,
    CalculationStatus,
    MissingReason,
)
from .parameter_snapshot import build_parameter_snapshot
from .soybean import (
    SoybeanCalculationOutput,
    calculate_soybean_net_crush_margin,
)


class RecalculationError(ValueError):
    """Base error for targeted recalculation requests."""


class RecalculationValidationError(RecalculationError):
    """Raised before calculation when a requested key is invalid."""


class DuplicateRecalculationKeyError(RecalculationValidationError):
    """Raised when the request contains the same full BusinessKey twice."""


@dataclass(frozen=True, slots=True)
class SoybeanRecalculationItem:
    business_key: BusinessKey
    market_snapshot: SoybeanMarketSnapshot
    calculation_result: SoybeanCalculationOutput


@dataclass(frozen=True, slots=True)
class SoybeanRecalculationBatch:
    requested_count: int
    success_count: int
    incomplete_count: int
    missing_reason_counts: tuple[tuple[MissingReason, int], ...]
    items: tuple[SoybeanRecalculationItem, ...]
    requested_keys: tuple[BusinessKey, ...]
    parameter_version: str
    parameter_hash: str
    mapping_identity: str
    mapping_hash: str
    contract_override_hash: str


def recalculate_soybean_keys(
    business_keys: Iterable[BusinessKey],
    *,
    config: SoybeanImportProfitConfig,
    cnf_records: Iterable[CnfQuoteRecord],
    cbot_records: Iterable[CbotPricePoint],
    fx_records: Iterable[FxPricePoint],
    dce_records: Iterable[DcePricePoint],
) -> SoybeanRecalculationBatch:
    """Recalculate only the explicit, unique keys supplied by the caller."""

    if not isinstance(config, SoybeanImportProfitConfig):
        raise RecalculationValidationError(
            "config must be a SoybeanImportProfitConfig"
        )
    requested = tuple(business_keys)
    canonical_keys = []
    for key in requested:
        if not isinstance(key, BusinessKey):
            raise RecalculationValidationError(
                "business_keys must contain validated BusinessKey objects"
            )
        if key.commodity != config.commodity or key.origin not in config.origin_codes:
            raise RecalculationValidationError(
                "requested business key is inconsistent with configuration"
            )
        require_business_weekday(key.business_date)
        canonical_keys.append(business_key_tuple(key))
    if len(canonical_keys) != len(set(canonical_keys)):
        raise DuplicateRecalculationKeyError(
            "business_keys contains a duplicate full BusinessKey"
        )

    sorted_keys = tuple(
        key
        for _, key in sorted(
            zip(canonical_keys, requested, strict=True),
            key=lambda pair: pair[0],
        )
    )
    market_index = index_market_data(
        cnf_records=cnf_records,
        cbot_records=cbot_records,
        fx_records=fx_records,
        dce_records=dce_records,
    )
    items = []
    for key in sorted_keys:
        snapshot = build_soybean_market_snapshot_from_index(
            key,
            config=config,
            market_index=market_index,
        )
        calculation_input = snapshot_to_calculation_input(
            snapshot,
            config=config,
        )
        calculation_result = calculate_soybean_net_crush_margin(
            calculation_input,
            config,
        )
        items.append(
            SoybeanRecalculationItem(
                business_key=key,
                market_snapshot=snapshot,
                calculation_result=calculation_result,
            )
        )

    immutable_items = tuple(items)
    success_count = sum(
        item.calculation_result.calculation_status is CalculationStatus.SUCCESS
        for item in immutable_items
    )
    missing_reason_counts = tuple(
        (
            reason,
            sum(
                reason in item.calculation_result.missing_reasons
                for item in immutable_items
            ),
        )
        for reason in MARKET_MISSING_REASON_ORDER
    )
    provenance = build_parameter_snapshot(config)
    assert provenance.parameter_hash is not None
    return SoybeanRecalculationBatch(
        requested_count=len(sorted_keys),
        success_count=success_count,
        incomplete_count=len(sorted_keys) - success_count,
        missing_reason_counts=missing_reason_counts,
        items=immutable_items,
        requested_keys=sorted_keys,
        parameter_version=str(config.schema_version),
        parameter_hash=provenance.parameter_hash,
        mapping_identity=config.contract_mapping_identity,
        mapping_hash=config.contract_mapping_hash,
        contract_override_hash=config.contract_override_hash,
    )
