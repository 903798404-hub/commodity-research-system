"""Pure orchestration for explicit-key soybean result candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Iterable

from agri_research_agent.import_profit.cnf_store import CnfQuoteRecord
from agri_research_agent.import_profit.config import SoybeanImportProfitConfig
from agri_research_agent.import_profit.market_snapshot import (
    CbotPricePoint,
    DcePricePoint,
    FxPricePoint,
)
from agri_research_agent.import_profit.models import BusinessKey
from agri_research_agent.import_profit.recalculation import (
    SoybeanRecalculationBatch,
    recalculate_soybean_keys,
)
from agri_research_agent.import_profit.standard_io import StandardFileIdentity


PIPELINE_VERSION = "1"


class ResultPipelineError(ValueError):
    """Base error for result-candidate orchestration."""


class CandidateStatus(StrEnum):
    PASSED = "passed"
    PASSED_WITH_INCOMPLETE = "passed_with_incomplete"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SoybeanResultCandidate:
    pipeline_version: str
    candidate_status: CandidateStatus
    calculated_at: datetime
    generated_at: datetime
    synthetic_input: bool
    input_files: tuple[StandardFileIdentity, ...]
    recalculation_batch: SoybeanRecalculationBatch
    config: SoybeanImportProfitConfig


def build_soybean_result_candidate(
    business_keys: Iterable[BusinessKey],
    *,
    config: SoybeanImportProfitConfig,
    cnf_records: Iterable[CnfQuoteRecord],
    cbot_records: Iterable[CbotPricePoint],
    fx_records: Iterable[FxPricePoint],
    dce_records: Iterable[DcePricePoint],
    calculated_at: datetime,
    generated_at: datetime | None = None,
    input_files: Iterable[StandardFileIdentity] = (),
    synthetic_input: bool = False,
) -> SoybeanResultCandidate:
    """Build a deterministic candidate for only the explicit requested keys."""

    calculation_time = _utc_datetime(calculated_at, "calculated_at")
    generation_time = _utc_datetime(
        calculated_at if generated_at is None else generated_at,
        "generated_at",
    )
    if type(synthetic_input) is not bool:
        raise ResultPipelineError("synthetic_input must be boolean")
    identities = tuple(input_files)
    if any(not isinstance(item, StandardFileIdentity) for item in identities):
        raise ResultPipelineError(
            "input_files must contain StandardFileIdentity objects"
        )
    filenames = [item.filename for item in identities]
    if len(filenames) != len(set(filenames)):
        raise ResultPipelineError("input file identities must have unique filenames")

    batch = recalculate_soybean_keys(
        business_keys,
        config=config,
        cnf_records=cnf_records,
        cbot_records=cbot_records,
        fx_records=fx_records,
        dce_records=dce_records,
    )
    status = (
        CandidateStatus.PASSED
        if batch.incomplete_count == 0
        else CandidateStatus.PASSED_WITH_INCOMPLETE
    )
    return SoybeanResultCandidate(
        pipeline_version=PIPELINE_VERSION,
        candidate_status=status,
        calculated_at=calculation_time,
        generated_at=generation_time,
        synthetic_input=synthetic_input,
        input_files=identities,
        recalculation_batch=batch,
        config=config,
    )


def _utc_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ResultPipelineError(f"{field_name} must be timezone-aware")
    result = value.astimezone(timezone.utc)
    if result.utcoffset() != timezone.utc.utcoffset(result):
        raise ResultPipelineError(f"{field_name} must normalize to UTC")
    return result
