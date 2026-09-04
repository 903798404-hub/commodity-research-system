"""Lutou Domestic Basis Standard, Candidate, Canonical and isolated Current."""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from agri_research_agent.data_sources.lutou.domestic_basis import (
    DomesticBasisCatalog,
    DomesticBasisEvidenceType,
    DomesticBasisExtraction,
    DomesticBasisSeries,
    DomesticBasisSourceAdapter,
    load_domestic_basis_catalog,
)
from agri_research_agent.market_data.basis import BasisMarket, BasisQuote, BasisQuoteType
from agri_research_agent.market_data.contracts import ContractId, Exchange
from agri_research_agent.market_data.quotes import Currency, PriceUnit
from agri_research_agent.shared.atomic_storage import atomic_write_json
from agri_research_agent.shared.async_update import SeriesUpdate, evaluate_update, validate_update_summary
from agri_research_agent.shared.file_identity import identify_file
from agri_research_agent.shared.immutable_candidate import seal_immutable_candidate, validate_candidate_id
from agri_research_agent.shared.runtime_context import RuntimeContext, assert_runtime_write

LOOKBACK_DAYS = 31
FORMAL_CUTOVER_DATE = date(2026, 6, 1)
FORMAL_BASELINE_SHA256 = "0e3e8eadb848b3c804ce6c458ec8cc331f3b67831e70a3e619d6dfc65ac13411"
HISTORICAL_SOURCE_SHA256 = "81fb31ed386528cf1ab703a9a5772e563356c3f1b3c649f7308f9dc56fd35dbb"
HISTORICAL_EXCEL_SHA256 = "3478e175b4a8de6f10bccce0d9a70412c6e7e186587d4d9ec88583f8f3c5715a"
HISTORICAL_SEED_ID = "basis-history-before-20260601-81fb31ed3865-v1"
DECIMAL_TYPE = pa.decimal128(20, 4)
STANDARD_STABLE_KEY = ("provider_series_id", "business_date", "source_row_identity")
CANONICAL_STABLE_KEY = ("series_id", "business_date")
FORMAL_STABLE_KEY = (
    "series_id", "business_date", "quote_type", "delivery_month", "futures_contract",
)

STANDARD_SCHEMA = pa.schema([
    pa.field("schema_version", pa.string(), False), pa.field("series_id", pa.string(), False),
    pa.field("provider_dataset_id", pa.string(), False), pa.field("provider_series_id", pa.string(), False),
    pa.field("source_series_id", pa.string(), False), pa.field("provider", pa.string(), False),
    pa.field("business_date", pa.date32(), False), pa.field("product", pa.string(), False),
    pa.field("consumer_product", pa.string(), False), pa.field("source_product", pa.string(), False),
    pa.field("location", pa.string(), False), pa.field("region_id", pa.string(), False),
    pa.field("raw_quote_type", pa.string(), True), pa.field("quote_type", pa.string(), False),
    pa.field("raw_contract_code", pa.string(), True), pa.field("underlying_exchange", pa.string(), False),
    pa.field("underlying_product", pa.string(), False), pa.field("underlying_futures_reference", pa.string(), True),
    pa.field("raw_basis_value", pa.string(), True), pa.field("basis_value", DECIMAL_TYPE, True),
    pa.field("cash_price", DECIMAL_TYPE, True), pa.field("futures_price", DECIMAL_TYPE, True),
    pa.field("currency", pa.string(), False), pa.field("unit", pa.string(), False),
    pa.field("source_locator", pa.string(), False), pa.field("source_row_identity", pa.string(), False),
    pa.field("source_identity_sha256", pa.string(), False), pa.field("source_row_sha256", pa.string(), False),
    pa.field("factory", pa.string(), True),
    pa.field("article_id", pa.string(), True), pa.field("article_title", pa.string(), True),
    pa.field("row_type", pa.string(), True), pa.field("province", pa.string(), True),
    pa.field("contract_situation", pa.string(), True), pa.field("contract_year_native", pa.string(), True),
    pa.field("delivery_month_native", pa.string(), True), pa.field("raw_price_text", pa.string(), True),
    pa.field("volume", DECIMAL_TYPE, True), pa.field("source_created_at", pa.timestamp("us"), True),
    pa.field("query_identity", pa.string(), False), pa.field("snapshot_identity", pa.string(), False),
    pa.field("captured_at", pa.timestamp("us", tz="UTC"), False), pa.field("mapping_version", pa.string(), False),
    pa.field("evidence_type", pa.string(), False), pa.field("legacy_status", pa.string(), False),
    pa.field("live_status", pa.string(), False), pa.field("is_usable", pa.bool_(), False),
    pa.field("quality_status", pa.string(), False), pa.field("exception_reason", pa.string(), True),
    pa.field("canonical_selection_status", pa.string(), False),
])

CANONICAL_SCHEMA = pa.schema([
    pa.field("schema_version", pa.string(), False), pa.field("series_id", pa.string(), False),
    pa.field("provider_dataset_id", pa.string(), False), pa.field("provider_series_id", pa.string(), False),
    pa.field("source_series_id", pa.string(), False), pa.field("provider", pa.string(), False),
    pa.field("business_date", pa.date32(), False), pa.field("product", pa.string(), False),
    pa.field("consumer_product", pa.string(), False), pa.field("location", pa.string(), False),
    pa.field("region_id", pa.string(), False), pa.field("value", DECIMAL_TYPE, False),
    pa.field("cash_price", DECIMAL_TYPE, True), pa.field("futures_price", DECIMAL_TYPE, True),
    pa.field("currency", pa.string(), False), pa.field("unit", pa.string(), False),
    pa.field("quote_type", pa.string(), False), pa.field("underlying_futures_reference", pa.string(), False),
    pa.field("source_locator", pa.string(), False), pa.field("source_row_count", pa.int32(), False),
    pa.field("far_contract_row_count", pa.int32(), False), pa.field("source_group_sha256", pa.string(), False),
    pa.field("aggregation_method", pa.string(), False), pa.field("query_identity", pa.string(), False),
    pa.field("snapshot_identity", pa.string(), False), pa.field("captured_at", pa.timestamp("us", tz="UTC"), False),
    pa.field("mapping_version", pa.string(), False), pa.field("evidence_type", pa.string(), False),
    pa.field("live_status", pa.string(), False),
])

FORMAL_CURRENT_SCHEMA = pa.schema([
    pa.field("schema_version", pa.string(), False), pa.field("segment", pa.string(), False),
    pa.field("series_id", pa.string(), False), pa.field("provider_dataset_id", pa.string(), False),
    pa.field("provider_series_id", pa.string(), False), pa.field("source_series_id", pa.string(), False),
    pa.field("provider", pa.string(), False), pa.field("business_date", pa.date32(), False),
    pa.field("date", pa.date32(), False), pa.field("commodity", pa.string(), False),
    pa.field("region", pa.string(), False),
    pa.field("product", pa.string(), False), pa.field("consumer_product", pa.string(), False),
    pa.field("location", pa.string(), False), pa.field("region_id", pa.string(), False),
    pa.field("quote_type", pa.string(), False), pa.field("canonical_quote_type", pa.string(), False),
    pa.field("delivery_month", pa.string(), False), pa.field("futures_contract", pa.string(), True),
    pa.field("value", DECIMAL_TYPE, False), pa.field("cash_price", DECIMAL_TYPE, True),
    pa.field("futures_price", DECIMAL_TYPE, True), pa.field("basis", DECIMAL_TYPE, True),
    pa.field("currency", pa.string(), False), pa.field("unit", pa.string(), False),
    pa.field("underlying_futures_reference", pa.string(), True), pa.field("source_sheet", pa.string(), False),
    pa.field("source_locator", pa.string(), False), pa.field("source_row_count", pa.int32(), False),
    pa.field("far_contract_row_count", pa.int32(), False), pa.field("source_group_sha256", pa.string(), False),
    pa.field("aggregation_method", pa.string(), False), pa.field("query_identity", pa.string(), False),
    pa.field("snapshot_identity", pa.string(), False), pa.field("captured_at", pa.timestamp("us", tz="UTC"), True),
    pa.field("mapping_version", pa.string(), False), pa.field("evidence_type", pa.string(), False),
    pa.field("live_status", pa.string(), False),
])


class DomesticBasisPipelineError(RuntimeError):
    def __init__(self, message: str, *, update_report: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.update_report = update_report


@dataclass(frozen=True, slots=True)
class DomesticBasisOfflineResult:
    run_id: str
    candidate_directory: Path
    canonical_simulation_directory: Path
    candidate_manifest: Mapping[str, object]
    canonical_simulation_manifest: Mapping[str, object]
    promoted: bool = False


@dataclass(frozen=True, slots=True)
class DomesticBasisCurrent:
    release_id: str
    directory: Path
    manifest: Mapping[str, object]
    observations: pa.Table


@dataclass(frozen=True, slots=True)
class DomesticBasisRunResult:
    run_id: str
    mode: str
    query_start_date: date
    query_end_date: date
    candidate_directory: Path
    canonical_directory: Path | None
    current: DomesticBasisCurrent | None
    promoted: bool
    candidate_quality: Mapping[str, object]
    canonical_quality: Mapping[str, object]

    @property
    def update_summary(self) -> Mapping[str, object]:
        return self.canonical_quality["async_update"]


@dataclass(frozen=True, slots=True)
class DomesticBasisCandidateState:
    standard: pa.Table
    next_state: pa.Table
    candidate_quality: Mapping[str, object]
    canonical_quality: Mapping[str, object]
    update_report: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class DomesticBasisHistoricalSeed:
    seed_id: str
    directory: Path
    manifest: Mapping[str, object]
    observations: pa.Table


@dataclass(frozen=True, slots=True)
class DomesticBasisAlignmentResult:
    run_id: str
    historical_seed: DomesticBasisHistoricalSeed
    current: DomesticBasisCurrent
    parity: Mapping[str, object]
    promoted: bool = True


def _parsed_contract(code: str | None, mapping: DomesticBasisSeries) -> ContractId | None:
    if not isinstance(code, str) or len(code) != 4 or not code.isdigit():
        return None
    year, month = 2000 + int(code[:2]), int(code[2:])
    if not 1 <= month <= 12:
        return None
    return ContractId(mapping.underlying_exchange, mapping.underlying_product, year, month)


def build_standard_table(extraction: DomesticBasisExtraction, catalog: DomesticBasisCatalog) -> pa.Table:
    rows: list[dict[str, object]] = []
    for source in extraction.records:
        mapping = catalog.match(source.source_product, source.region)
        contract = _parsed_contract(source.source_contract_code, mapping)
        reason: str | None = None
        if source.source_quote_type != "现货基差":
            reason = "NON_SPOT_QUOTE_TYPE"
        elif source.basis_value is None:
            reason = "BASIS_NULL_OR_NONNUMERIC"
        elif contract is None:
            reason = "CONTRACT_INVALID"
        elif (contract.year, contract.month) < (source.business_date.year, source.business_date.month):
            reason = "CONTRACT_EXPIRED"
        usable = reason is None
        rows.append({
            "schema_version": "public-basis-standard/2", "series_id": mapping.series_id,
            "provider_dataset_id": mapping.provider_dataset_id, "provider_series_id": mapping.provider_series_id,
            "source_series_id": mapping.source_series_id, "provider": catalog.provider,
            "business_date": source.business_date, "product": mapping.product,
            "consumer_product": mapping.consumer_product, "source_product": source.source_product,
            "location": source.region, "region_id": mapping.region_id,
            "raw_quote_type": source.source_quote_type, "quote_type": "DOMESTIC_SPOT_BASIS",
            "raw_contract_code": source.source_contract_code,
            "underlying_exchange": mapping.underlying_exchange.value,
            "underlying_product": mapping.underlying_product,
            "underlying_futures_reference": None if contract is None else str(contract),
            "raw_basis_value": source.raw_basis_value,
            "basis_value": None if source.basis_value is None else source.basis_value.quantize(Decimal("0.0001")),
            "cash_price": None if source.cash_price is None else source.cash_price.quantize(Decimal("0.0001")),
            "futures_price": None if source.futures_price is None else source.futures_price.quantize(Decimal("0.0001")),
            "currency": "CNY", "unit": "CNY/metric_tonne", "source_locator": mapping.source_locator,
            "source_row_identity": source.source_row_identity,
            "source_identity_sha256": source.source_identity_sha256,
            "source_row_sha256": source.source_row_sha256,
            "factory": source.factory, "article_id": source.article_id, "article_title": source.article_title,
            "row_type": source.row_type, "province": source.province,
            "contract_situation": source.contract_situation, "contract_year_native": source.contract_year_native,
            "delivery_month_native": source.delivery_month_native, "raw_price_text": source.raw_price_text,
            "volume": None if source.volume is None else source.volume.quantize(Decimal("0.0001")),
            "source_created_at": source.source_created_at, "query_identity": extraction.query_identity,
            "snapshot_identity": extraction.snapshot_identity, "captured_at": extraction.extracted_at,
            "mapping_version": catalog.mapping_version, "evidence_type": extraction.evidence_type.value,
            "legacy_status": mapping.legacy_status, "live_status": mapping.live_status, "is_usable": usable,
            "quality_status": "PASS" if usable else "RETAINED_EXCEPTION", "exception_reason": reason,
            "canonical_selection_status": "ELIGIBLE_PENDING_POLICY" if usable else "NOT_ELIGIBLE_FOR_CANONICAL",
        })
    groups: dict[tuple[str, date], list[dict[str, object]]] = {}
    for row in rows:
        if row["is_usable"]:
            groups.setdefault((str(row["series_id"]), row["business_date"]), []).append(row)
    for group in groups.values():
        nearest = min(
            (str(row["underlying_futures_reference"]) for row in group),
            key=_contract_sort_key,
        )
        for row in group:
            row["canonical_selection_status"] = (
                "SELECTED_BY_CANONICAL_POLICY" if row["underlying_futures_reference"] == nearest
                else "VALID_SOURCE_NOT_SELECTED_BY_CANONICAL_POLICY"
            )
    return pa.Table.from_pylist(rows, schema=STANDARD_SCHEMA).sort_by([(x, "ascending") for x in STANDARD_STABLE_KEY])


def validate_candidate(
    table: pa.Table,
    catalog: DomesticBasisCatalog,
    *,
    require_complete_series: bool = True,
    require_complete_usable_series: bool | None = None,
    allow_empty_increment: bool = False,
) -> dict[str, object]:
    if table.schema != STANDARD_SCHEMA or (table.num_rows == 0 and not allow_empty_increment):
        raise DomesticBasisPipelineError("Domestic Basis Candidate schema is invalid")
    duplicates = _duplicate_count(table, STANDARD_STABLE_KEY)
    if duplicates:
        raise DomesticBasisPipelineError("Domestic Basis source stable key is duplicated")
    rows = table.to_pylist()
    expected = {x.series_id for x in catalog.series}
    observed = {str(x["series_id"]) for x in rows}
    usable_series = {str(x["series_id"]) for x in rows if x["is_usable"]}
    if observed - expected or (require_complete_series and observed != expected):
        raise DomesticBasisPipelineError("Domestic Basis Candidate coverage is incomplete")
    require_usable = (
        require_complete_series
        if require_complete_usable_series is None
        else require_complete_usable_series
    )
    if require_usable and usable_series != expected:
        raise DomesticBasisPipelineError("Domestic Basis Candidate usable-series coverage is incomplete")
    required = ("series_id", "product", "location", "provider_series_id", "source_series_id",
                "source_locator", "source_row_identity", "source_identity_sha256", "source_row_sha256",
                "query_identity", "snapshot_identity")
    if any(not isinstance(row[field], str) or not row[field].strip() for row in rows for field in required):
        raise DomesticBasisPipelineError("Domestic Basis Candidate identity is incomplete")
    if rows and (set(table["currency"].to_pylist()) != {"CNY"} or set(table["unit"].to_pylist()) != {"CNY/metric_tonne"}):
        raise DomesticBasisPipelineError("Domestic Basis Candidate currency/unit is invalid")
    identity_contents: dict[str, set[str]] = {}
    for row in rows:
        identity_contents.setdefault(str(row["source_identity_sha256"]), set()).add(str(row["source_row_sha256"]))
    identity_collisions = sum(len(values) - 1 for values in identity_contents.values() if len(values) > 1)
    if identity_collisions:
        raise DomesticBasisPipelineError("Domestic Basis source identity collision")
    counts: dict[str, int] = {}
    for row in rows:
        if row["exception_reason"]:
            counts[str(row["exception_reason"])] = counts.get(str(row["exception_reason"]), 0) + 1
    spot_rows = [x for x in rows if x["raw_quote_type"] == "现货基差"]
    numeric_spot = [x for x in spot_rows if x["basis_value"] is not None]
    basis_values = [x["basis_value"] for x in numeric_spot]
    return {
        "quality_status": "PASS", "row_count": table.num_rows, "series_count": len(observed),
        "usable_series_count": len(usable_series), "expected_series_count": len(expected),
        "usable_row_count": sum(bool(x["is_usable"]) for x in rows),
        "retained_exception_count": sum(not bool(x["is_usable"]) for x in rows),
        "retained_exceptions_by_reason": counts,
        "far_contract_row_count": sum(x["canonical_selection_status"] == "VALID_SOURCE_NOT_SELECTED_BY_CANONICAL_POLICY" for x in rows),
        "spot_basis_row_count": len(spot_rows), "numeric_basis_row_count": len(numeric_spot),
        "null_or_nonnumeric_basis_row_count": len(spot_rows) - len(numeric_spot),
        "negative_basis_row_count": sum(x < 0 for x in basis_values),
        "zero_basis_row_count": sum(x == 0 for x in basis_values),
        "positive_basis_row_count": sum(x > 0 for x in basis_values),
        "products": sorted({str(x["source_product"]) for x in rows}),
        "regions": sorted({str(x["location"]) for x in rows}),
        "raw_quote_types": sorted({str(x["raw_quote_type"]) for x in rows}),
        "raw_contracts": sorted({str(x["raw_contract_code"]) for x in rows}),
        "min_date": min(x["business_date"] for x in rows).isoformat() if rows else None,
        "max_date": max(x["business_date"] for x in rows).isoformat() if rows else None,
        "stable_key_duplicate_count": 0, "source_identity_collision_count": 0,
        "missing_value_policy": "retain_as_exception",
        "date_fill_policy": "none", "signed_basis_policy": "negative_zero_positive_allowed",
    }


def build_canonical_table(
    table: pa.Table,
    catalog: DomesticBasisCatalog,
    *,
    require_complete_series: bool = True,
    require_complete_usable_series: bool | None = None,
    allow_empty_increment: bool = False,
) -> tuple[pa.Table, dict[str, object]]:
    gate = validate_candidate(
        table,
        catalog,
        require_complete_series=require_complete_series,
        require_complete_usable_series=require_complete_usable_series,
        allow_empty_increment=allow_empty_increment,
    )
    groups: dict[tuple[str, date], list[dict[str, object]]] = {}
    far_counts: dict[tuple[str, date], int] = {}
    for row in table.to_pylist():
        key = (str(row["series_id"]), row["business_date"])
        if row["canonical_selection_status"] == "SELECTED_BY_CANONICAL_POLICY":
            groups.setdefault(key, []).append(row)
        elif row["canonical_selection_status"] == "VALID_SOURCE_NOT_SELECTED_BY_CANONICAL_POLICY":
            far_counts[key] = far_counts.get(key, 0) + 1
    output: list[dict[str, object]] = []
    for (series_id, business_date), selected in sorted(groups.items()):
        references = {str(x["underlying_futures_reference"]) for x in selected}
        if len(references) != 1:
            raise DomesticBasisPipelineError("Domestic Basis logical contract collision")
        values = [x["basis_value"] for x in selected]
        if any(x is None for x in values):
            raise DomesticBasisPipelineError("Domestic Basis selected value is null")
        value = statistics.median(values).quantize(Decimal("0.0001"))
        first = min(selected, key=lambda x: str(x["source_row_identity"]))
        mapping = next(x for x in catalog.series if x.series_id == series_id)
        contract = _parse_reference(next(iter(references)))
        quote = BasisQuote(
            1, series_id, BasisMarket.DOMESTIC_CHINA, business_date, str(first["product"]),
            str(first["location"]), value, Currency.CNY, PriceUnit.CNY_PER_METRIC_TONNE,
            BasisQuoteType.DOMESTIC_SPOT, contract, catalog.provider, str(first["source_series_id"]),
            str(first["provider_dataset_id"]), str(first["provider_series_id"]), str(first["source_locator"]),
            _source_group_sha(selected), str(first["query_identity"]), str(first["snapshot_identity"]), first["captured_at"],
        )
        far = far_counts.get((series_id, business_date), 0)
        output.append({
            "schema_version": "public-basis-canonical/2", "series_id": quote.series_id,
            "provider_dataset_id": quote.provider_dataset_id, "provider_series_id": quote.provider_series_id,
            "source_series_id": quote.source_series_id, "provider": quote.source,
            "business_date": quote.business_date, "product": quote.product,
            "consumer_product": mapping.consumer_product, "location": quote.location,
            "region_id": mapping.region_id, "value": quote.value,
            "cash_price": None, "futures_price": None,
            "currency": quote.currency.value, "unit": quote.unit.value, "quote_type": quote.quote_type.value,
            "underlying_futures_reference": str(quote.underlying_futures), "source_locator": quote.source_locator,
            "source_row_count": len(selected), "far_contract_row_count": far,
            "source_group_sha256": quote.source_row_identity,
            "aggregation_method": "median_of_source_quotes_at_nearest_nonexpired_contract",
            "query_identity": quote.query_identity, "snapshot_identity": quote.snapshot_identity,
            "captured_at": quote.captured_at, "mapping_version": catalog.mapping_version,
            "evidence_type": str(first["evidence_type"]), "live_status": mapping.live_status,
        })
    result = pa.Table.from_pylist(output, schema=CANONICAL_SCHEMA).sort_by([(x, "ascending") for x in CANONICAL_STABLE_KEY])
    duplicates = _duplicate_count(result, CANONICAL_STABLE_KEY)
    if duplicates:
        raise DomesticBasisPipelineError("Domestic Basis canonical policy collision")
    return result, {
        "quality_status": "PASS", "policy_mode": "LIVE" if catalog.live_verified else "OFFLINE_SIMULATION_ONLY",
        "production_authorized": catalog.live_verified and set(table["evidence_type"].to_pylist()) == {"LIVE_DATABASE"},
        "row_count": result.num_rows, "series_count": len(set(result["series_id"].to_pylist())),
        "stable_key_duplicate_count": 0, "logical_collision_count": 0,
        "far_contract_row_count": gate["far_contract_row_count"],
        "aggregation_method": "median_of_source_quotes_at_nearest_nonexpired_contract",
        "cash_futures_policy": "legacy_compatible_null",
    }


def simulate_canonical_policy(table: pa.Table, catalog: DomesticBasisCatalog) -> tuple[pa.Table, dict[str, object]]:
    return build_canonical_table(table, catalog)


_HISTORICAL_PRODUCT_IDS = {
    "一豆": "soybean_oil", "24度": "palm_oil_24", "三菜": "rapeseed_oil_3",
    "豆粕": "soybean_meal", "菜粕": "rapeseed_meal", "一葵": "sunflower_oil_1",
    "一级玉米油": "corn_oil_grade_1", "葵粕": "sunflower_meal",
}
_HISTORICAL_REGION_IDS = {
    "华东": "east_china", "华南": "south_china", "华北": "north_china",
    "东北": "northeast_china", "华中": "central_china", "西北": "northwest_china",
    "西南": "southwest_china", "山东": "shandong", "广西": "guangxi", "成都": "chengdu",
}


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        return None
    return parsed.quantize(Decimal("0.0001"))


def _historical_series_identity(
    commodity: str, region: str, quote_type: str, catalog: DomesticBasisCatalog,
) -> tuple[str, str, str]:
    if quote_type == "基差报价":
        matched = next(
            (item for item in catalog.series if item.consumer_product == commodity and item.region == region),
            None,
        )
        if matched is not None:
            return matched.series_id, matched.product, matched.region_id
    product = _HISTORICAL_PRODUCT_IDS.get(commodity)
    region_id = _HISTORICAL_REGION_IDS.get(region)
    if product is None or region_id is None:
        raise DomesticBasisPipelineError("Historical Domestic Basis identity is not governed")
    quote_id = "basis" if quote_type == "基差报价" else "cash_price"
    return (
        f"market.basis.domestic.china.historical.{product}.{region_id}.{quote_id}",
        product,
        region_id,
    )


def _historical_row_sha(row: Mapping[str, object]) -> str:
    fields = (
        "date", "commodity", "region", "quote_type", "delivery_month",
        "futures_contract", "cash_price", "futures_price", "basis", "source_sheet",
    )
    payload = {
        field: (
            row[field].isoformat() if isinstance(row[field], (date, datetime))
            else str(row[field]) if isinstance(row[field], Decimal) else row[field]
        )
        for field in fields
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_historical_seed_table(
    source: pa.Table, catalog: DomesticBasisCatalog,
) -> tuple[pa.Table, dict[str, object]]:
    expected = {
        "date", "commodity", "region", "quote_type", "delivery_month", "futures_contract",
        "cash_price", "futures_price", "basis", "source_sheet",
    }
    if set(source.schema.names) != expected:
        raise DomesticBasisPipelineError("Historical Domestic Basis seed source schema is invalid")
    output: list[dict[str, object]] = []
    for raw in source.to_pylist():
        raw_date = raw["date"]
        business_date = raw_date.date() if isinstance(raw_date, datetime) else raw_date
        if type(business_date) is not date or business_date >= FORMAL_CUTOVER_DATE:
            raise DomesticBasisPipelineError("Historical Domestic Basis seed crosses the cutover")
        commodity, region, quote_type = str(raw["commodity"]), str(raw["region"]), str(raw["quote_type"])
        if quote_type not in {"基差报价", "一口价"}:
            raise DomesticBasisPipelineError("Historical Domestic Basis quote type is invalid")
        cash, futures, basis = (
            _decimal_or_none(raw["cash_price"]),
            _decimal_or_none(raw["futures_price"]),
            _decimal_or_none(raw["basis"]),
        )
        if quote_type == "基差报价":
            if cash is None or futures is None or basis is None:
                raise DomesticBasisPipelineError("Historical basis row is missing its sealed values")
            if abs(cash - futures - basis) > Decimal("1"):
                raise DomesticBasisPipelineError("Historical basis row violates its recovered value contract")
            value, canonical_quote_type = basis, "DOMESTIC_BASIS"
        else:
            if cash is None or futures is not None or basis is not None:
                raise DomesticBasisPipelineError("Historical cash-price row violates its recovered null contract")
            value, canonical_quote_type = cash, "DOMESTIC_CASH_PRICE"
        series_id, product, region_id = _historical_series_identity(commodity, region, quote_type, catalog)
        source_sheet = str(raw["source_sheet"])
        source_series = f"historical-excel:{source_sheet}:{commodity}:{region}:{quote_type}"
        row_sha = _historical_row_sha(raw)
        output.append({
            "schema_version": "public-domestic-basis-current/3", "segment": "SEALED_HISTORICAL",
            "series_id": series_id, "provider_dataset_id": f"sealed-history:{HISTORICAL_EXCEL_SHA256}",
            "provider_series_id": source_series, "source_series_id": source_series,
            "provider": "Historical Domestic Basis Excel", "business_date": business_date,
            "date": business_date, "commodity": commodity, "region": region,
            "product": product, "consumer_product": commodity, "location": region, "region_id": region_id,
            "quote_type": quote_type, "canonical_quote_type": canonical_quote_type,
            "delivery_month": str(raw["delivery_month"]),
            "futures_contract": None if raw["futures_contract"] is None else str(raw["futures_contract"]),
            "value": value, "cash_price": cash, "futures_price": futures, "basis": basis,
            "currency": "CNY", "unit": "CNY/metric_tonne", "underlying_futures_reference": None,
            "source_sheet": source_sheet,
            "source_locator": f"sealed-history:{HISTORICAL_SOURCE_SHA256}#{source_sheet}",
            "source_row_count": 1, "far_contract_row_count": 0, "source_group_sha256": row_sha,
            "aggregation_method": "source_row_preserved_no_aggregation",
            "query_identity": f"sealed-history:{HISTORICAL_SOURCE_SHA256}",
            "snapshot_identity": HISTORICAL_SOURCE_SHA256, "captured_at": None,
            "mapping_version": "formal-domestic-basis-history/1",
            "evidence_type": "SEALED_HISTORICAL_ASSET", "live_status": "SEALED",
        })
    result = pa.Table.from_pylist(output, schema=FORMAL_CURRENT_SCHEMA).sort_by(
        [(field, "ascending") for field in FORMAL_STABLE_KEY]
    )
    report = _validate_formal_current(result, catalog, require_live=False)
    if result.num_rows != 16_331:
        raise DomesticBasisPipelineError("Historical Domestic Basis seed row count is invalid")
    return result, report


def seal_historical_basis_seed(
    *, runtime: RuntimeContext, source_path: str | Path, mapping_path: str | Path,
) -> DomesticBasisHistoricalSeed:
    catalog = load_domestic_basis_catalog(mapping_path)
    public_root = assert_runtime_write(
        runtime, runtime.runtime_root / "public-market-data" / "lutou-domestic-basis"
    )
    public_root.mkdir(parents=True, exist_ok=True)
    seed_pointer = public_root / "historical-seed.json"
    pointer = _read_json(seed_pointer) if seed_pointer.is_file() else None
    existing = (
        load_historical_basis_seed(public_root)
        if isinstance(pointer, Mapping) and pointer.get("seed_id") == HISTORICAL_SEED_ID
        else None
    )
    if existing is not None:
        return existing
    source = Path(source_path)
    if not source.is_file() or identify_file(source).sha256 != HISTORICAL_SOURCE_SHA256:
        raise DomesticBasisPipelineError("Historical Domestic Basis sealed source identity mismatch")
    table, quality = build_historical_seed_table(pq.read_table(source), catalog)
    seed_root = assert_runtime_write(runtime, public_root / "historical-seeds")
    manifest_holder: dict[str, object] = {}

    def build(directory: Path) -> dict[str, object]:
        pq.write_table(table, directory / "observations.parquet")
        dates = table["business_date"].to_pylist()
        manifest = {
            "schema_version": "lutou-domestic-basis-historical-seed/1", "seed_id": HISTORICAL_SEED_ID,
            "sealed": True, "source_asset": "basis_quotes_before_20260601.parquet",
            "source_sha256": HISTORICAL_SOURCE_SHA256, "upstream_excel_sha256": HISTORICAL_EXCEL_SHA256,
            "cutover_date": FORMAL_CUTOVER_DATE.isoformat(), "row_count": table.num_rows,
            "min_date": min(dates).isoformat(), "max_date": max(dates).isoformat(),
            "contract_schema_version": "public-domestic-basis-current/3",
            "business_content_sha256": _business_sha(table), "quality_status": "PASS",
            "quality": quality, "data_sha256": identify_file(directory / "observations.parquet").sha256,
            "files": _file_identities(directory, ("observations.parquet",)),
        }
        _write_json(directory / "manifest.json", manifest)
        manifest_holder.update(manifest)
        return manifest

    directory, _ = seal_immutable_candidate(seed_root, HISTORICAL_SEED_ID, build)
    manifest_sha = identify_file(directory / "manifest.json").sha256
    atomic_write_json(
        assert_runtime_write(runtime, public_root / "historical-seed.json"),
        {"schema_version": 1, "seed_id": HISTORICAL_SEED_ID, "manifest_sha256": manifest_sha},
    )
    loaded = load_historical_basis_seed(public_root)
    if loaded is None:
        raise DomesticBasisPipelineError("Historical Domestic Basis seed post-seal validation failed")
    return loaded


def load_historical_basis_seed(public_root: str | Path) -> DomesticBasisHistoricalSeed | None:
    root = Path(public_root)
    pointer_path = root / "historical-seed.json"
    if not pointer_path.is_file():
        return None
    pointer = _read_json(pointer_path)
    if set(pointer) != {"schema_version", "seed_id", "manifest_sha256"} or pointer.get("seed_id") != HISTORICAL_SEED_ID:
        raise DomesticBasisPipelineError("Historical Domestic Basis seed pointer is invalid")
    directory = root / "historical-seeds" / HISTORICAL_SEED_ID
    manifest_path = directory / "manifest.json"
    if identify_file(manifest_path).sha256 != pointer["manifest_sha256"]:
        raise DomesticBasisPipelineError("Historical Domestic Basis seed manifest identity mismatch")
    manifest = _read_json(manifest_path)
    data_path = directory / "observations.parquet"
    observations = pq.read_table(data_path)
    if (
        manifest.get("sealed") is not True
        or manifest.get("source_sha256") != HISTORICAL_SOURCE_SHA256
        or manifest.get("quality_status") != "PASS"
        or manifest.get("data_sha256") != identify_file(data_path).sha256
        or manifest.get("business_content_sha256") != _business_sha(observations)
        or observations.schema != FORMAL_CURRENT_SCHEMA
    ):
        raise DomesticBasisPipelineError("Historical Domestic Basis seed contract is invalid")
    _validate_formal_current(observations, None, require_live=False)
    return DomesticBasisHistoricalSeed(HISTORICAL_SEED_ID, directory, manifest, observations)


def _formal_live_rows(canonical: pa.Table, catalog: DomesticBasisCatalog) -> pa.Table:
    by_series = {item.series_id: item for item in catalog.series}
    output: list[dict[str, object]] = []
    for row in canonical.to_pylist():
        if row["business_date"] < FORMAL_CUTOVER_DATE:
            continue
        mapping = by_series.get(str(row["series_id"]))
        if mapping is None:
            raise DomesticBasisPipelineError("Live Domestic Basis series is not governed")
        reference = _parse_reference(str(row["underlying_futures_reference"]))
        contract = f"{reference.year % 100:02d}{reference.month:02d}"
        if row["cash_price"] is not None or row["futures_price"] is not None:
            raise DomesticBasisPipelineError("Live Domestic Basis cash/futures must remain null")
        output.append({
            "schema_version": "public-domestic-basis-current/3", "segment": "LIVE_LUTOU",
            "series_id": row["series_id"], "provider_dataset_id": row["provider_dataset_id"],
            "provider_series_id": row["provider_series_id"], "source_series_id": row["source_series_id"],
            "provider": row["provider"], "business_date": row["business_date"], "product": row["product"],
            "date": row["business_date"], "commodity": row["consumer_product"], "region": row["location"],
            "consumer_product": row["consumer_product"], "location": row["location"],
            "region_id": row["region_id"], "quote_type": "基差报价",
            "canonical_quote_type": row["quote_type"], "delivery_month": "现货",
            "futures_contract": contract, "value": row["value"], "cash_price": None,
            "futures_price": None, "basis": row["value"], "currency": row["currency"], "unit": row["unit"],
            "underlying_futures_reference": row["underlying_futures_reference"],
            "source_sheet": f"basis_price:{mapping.source_product}", "source_locator": row["source_locator"],
            "source_row_count": row["source_row_count"], "far_contract_row_count": row["far_contract_row_count"],
            "source_group_sha256": row["source_group_sha256"], "aggregation_method": row["aggregation_method"],
            "query_identity": row["query_identity"], "snapshot_identity": row["snapshot_identity"],
            "captured_at": row["captured_at"], "mapping_version": row["mapping_version"],
            "evidence_type": row["evidence_type"], "live_status": row["live_status"],
        })
    return pa.Table.from_pylist(output, schema=FORMAL_CURRENT_SCHEMA)


def compose_formal_basis_current(
    seed: DomesticBasisHistoricalSeed, live_canonical: pa.Table, catalog: DomesticBasisCatalog,
) -> tuple[pa.Table, dict[str, object]]:
    _validate_canonical(live_canonical, catalog)
    live = _formal_live_rows(live_canonical, catalog)
    combined = pa.concat_tables([seed.observations, live]).sort_by(
        [(field, "ascending") for field in FORMAL_STABLE_KEY]
    )
    return combined, _validate_formal_current(combined, catalog, require_live=True)


def _validate_formal_current(
    table: pa.Table, catalog: DomesticBasisCatalog | None, *, require_live: bool,
) -> dict[str, object]:
    if table.schema != FORMAL_CURRENT_SCHEMA or table.num_rows == 0:
        raise DomesticBasisPipelineError("Formal Domestic Basis schema is invalid")
    if _duplicate_count(table, FORMAL_STABLE_KEY):
        raise DomesticBasisPipelineError("Formal Domestic Basis stable-key collision")
    rows = table.to_pylist()
    legacy_key = (
        "business_date", "consumer_product", "location", "quote_type", "delivery_month", "futures_contract",
    )
    seen: set[tuple[object, ...]] = set()
    for row in rows:
        if (
            row["date"] != row["business_date"]
            or row["commodity"] != row["consumer_product"]
            or row["region"] != row["location"]
        ):
            raise DomesticBasisPipelineError("Formal Domestic Basis consumer aliases are inconsistent")
        key = tuple(row[field] for field in legacy_key)
        if key in seen:
            raise DomesticBasisPipelineError("Formal Domestic Basis legacy contract collision")
        seen.add(key)
    historical = [row for row in rows if row["segment"] == "SEALED_HISTORICAL"]
    live = [row for row in rows if row["segment"] == "LIVE_LUTOU"]
    if len(historical) != 16_331 or any(row["business_date"] >= FORMAL_CUTOVER_DATE for row in historical):
        raise DomesticBasisPipelineError("Formal Domestic Basis historical boundary is invalid")
    if require_live and (not live or any(row["business_date"] < FORMAL_CUTOVER_DATE for row in live)):
        raise DomesticBasisPipelineError("Formal Domestic Basis live boundary is invalid")
    if set(table["currency"].to_pylist()) != {"CNY"} or set(table["unit"].to_pylist()) != {"CNY/metric_tonne"}:
        raise DomesticBasisPipelineError("Formal Domestic Basis currency/unit is invalid")
    for row in historical:
        if row["quote_type"] == "基差报价":
            if row["cash_price"] is None or row["futures_price"] is None or row["basis"] is None:
                raise DomesticBasisPipelineError("Formal historical basis values are incomplete")
            if abs(row["cash_price"] - row["futures_price"] - row["basis"]) > Decimal("1"):
                raise DomesticBasisPipelineError("Formal historical basis relation is invalid")
        elif row["quote_type"] == "一口价":
            if row["cash_price"] is None or row["futures_price"] is not None or row["basis"] is not None:
                raise DomesticBasisPipelineError("Formal historical cash-price null semantics are invalid")
        else:
            raise DomesticBasisPipelineError("Formal historical quote type is invalid")
    if any(row["cash_price"] is not None or row["futures_price"] is not None or row["basis"] is None for row in live):
        raise DomesticBasisPipelineError("Formal live cash/futures null semantics are invalid")
    if catalog is not None and live and {str(row["series_id"]) for row in live} != {item.series_id for item in catalog.series}:
        raise DomesticBasisPipelineError("Formal Domestic Basis live series coverage is incomplete")
    return {
        "quality_status": "PASS", "row_count": table.num_rows,
        "historical_row_count": len(historical), "live_row_count": len(live),
        "series_count": len(set(table["series_id"].to_pylist())), "stable_key_duplicate_count": 0,
        "legacy_contract_collision_count": 0,
        "historical_cash_non_null_count": sum(row["cash_price"] is not None for row in historical),
        "historical_futures_non_null_count": sum(row["futures_price"] is not None for row in historical),
        "live_cash_non_null_count": sum(row["cash_price"] is not None for row in live),
        "live_futures_non_null_count": sum(row["futures_price"] is not None for row in live),
        "cutover_date": FORMAL_CUTOVER_DATE.isoformat(),
    }


def compare_formal_basis_parity(table: pa.Table, baseline_path: str | Path) -> dict[str, object]:
    baseline_file = Path(baseline_path)
    if not baseline_file.is_file() or identify_file(baseline_file).sha256 != FORMAL_BASELINE_SHA256:
        raise DomesticBasisPipelineError("Formal Domestic Basis baseline identity mismatch")
    baseline = pq.read_table(baseline_file).to_pylist()
    fields = (
        "date", "commodity", "region", "quote_type", "delivery_month",
        "futures_contract", "cash_price", "futures_price", "basis", "source_sheet",
    )

    def normalized(row: Mapping[str, object], *, public: bool) -> dict[str, object]:
        business_date = row["business_date"] if public else row["date"]
        if isinstance(business_date, datetime):
            business_date = business_date.date()
        return {
            "date": business_date,
            "commodity": row["consumer_product"] if public else row["commodity"],
            "region": row["location"] if public else row["region"],
            "quote_type": row["quote_type"], "delivery_month": row["delivery_month"],
            "futures_contract": row["futures_contract"], "cash_price": _decimal_or_none(row["cash_price"]),
            "futures_price": _decimal_or_none(row["futures_price"]), "basis": _decimal_or_none(row["basis"]),
            "source_sheet": row["source_sheet"],
        }

    key_fields = ("date", "commodity", "region", "quote_type", "delivery_month", "futures_contract")
    old = {}
    for row in baseline:
        value = normalized(row, public=False); key = tuple(value[field] for field in key_fields)
        if key in old:
            raise DomesticBasisPipelineError("Formal Domestic Basis baseline key is duplicated")
        old[key] = value
    new = {}
    for row in table.to_pylist():
        value = normalized(row, public=True); key = tuple(value[field] for field in key_fields)
        if key in new:
            raise DomesticBasisPipelineError("Formal Domestic Basis Current key is duplicated")
        new[key] = value
    common = set(old) & set(new)
    differences = {
        field: sum(old[key][field] != new[key][field] for key in common)
        for field in fields
    }
    baseline_max = max(value["date"] for value in old.values())
    public_only = set(new) - set(old)
    nonextension = sum(new[key]["date"] <= baseline_max for key in public_only)
    historical_keys = {key for key, value in old.items() if value["date"] < FORMAL_CUTOVER_DATE}
    live_keys = set(old) - historical_keys
    report = {
        "quality_status": "PASS", "baseline_sha256": FORMAL_BASELINE_SHA256,
        "baseline_rows": len(old), "public_rows": len(new), "common_rows": len(common),
        "baseline_only_rows": len(set(old) - set(new)), "public_only_rows": len(public_only),
        "authoritative_live_extension_rows": sum(new[key]["date"] > baseline_max for key in public_only),
        "public_only_nonextension_rows": nonextension, "field_differences": differences,
        "historical_segment": {
            "baseline_rows": len(historical_keys), "common_rows": len(historical_keys & set(new)),
            "baseline_only_rows": len(historical_keys - set(new)),
        },
        "live_sql_segment": {
            "baseline_rows": len(live_keys), "common_rows": len(live_keys & set(new)),
            "baseline_only_rows": len(live_keys - set(new)),
        },
        "baseline_max_date": baseline_max.isoformat(),
    }
    if (
        len(old) != 17_042 or len(set(old) - set(new)) or nonextension
        or any(differences.values())
        or report["historical_segment"]["common_rows"] != 16_331
        or report["live_sql_segment"]["common_rows"] != 711
    ):
        raise DomesticBasisPipelineError("Formal Domestic Basis 17,042-row contract parity failed")
    return report


def _extract_live_canonical(current: DomesticBasisCurrent) -> pa.Table:
    if current.observations.schema == CANONICAL_SCHEMA:
        return current.observations
    if current.observations.schema != FORMAL_CURRENT_SCHEMA:
        raise DomesticBasisPipelineError("Domestic Basis Current schema is unsupported")
    output: list[dict[str, object]] = []
    for row in current.observations.to_pylist():
        if row["segment"] != "LIVE_LUTOU":
            continue
        output.append({
            "schema_version": "public-basis-canonical/2", "series_id": row["series_id"],
            "provider_dataset_id": row["provider_dataset_id"], "provider_series_id": row["provider_series_id"],
            "source_series_id": row["source_series_id"], "provider": row["provider"],
            "business_date": row["business_date"], "product": row["product"],
            "consumer_product": row["consumer_product"], "location": row["location"],
            "region_id": row["region_id"], "value": row["value"], "cash_price": row["cash_price"],
            "futures_price": row["futures_price"], "currency": row["currency"], "unit": row["unit"],
            "quote_type": row["canonical_quote_type"],
            "underlying_futures_reference": row["underlying_futures_reference"],
            "source_locator": row["source_locator"], "source_row_count": row["source_row_count"],
            "far_contract_row_count": row["far_contract_row_count"],
            "source_group_sha256": row["source_group_sha256"], "aggregation_method": row["aggregation_method"],
            "query_identity": row["query_identity"], "snapshot_identity": row["snapshot_identity"],
            "captured_at": row["captured_at"], "mapping_version": row["mapping_version"],
            "evidence_type": row["evidence_type"], "live_status": row["live_status"],
        })
    return pa.Table.from_pylist(output, schema=CANONICAL_SCHEMA).sort_by(
        [(field, "ascending") for field in CANONICAL_STABLE_KEY]
    )


def _seal_formal_release(
    *, runtime: RuntimeContext, public_root: Path, run_id: str,
    seed: DomesticBasisHistoricalSeed, live_canonical: pa.Table,
    catalog: DomesticBasisCatalog, parity: Mapping[str, object],
    canonical_manifest_sha256: str, candidate_manifest_sha256: str,
    failure_hook: Callable[[str], None] | None = None,
) -> DomesticBasisCurrent:
    table, quality = compose_formal_basis_current(seed, live_canonical, catalog)
    if parity.get("quality_status") != "PASS":
        raise DomesticBasisPipelineError("Formal Domestic Basis parity is not promotion-authorized")
    release_id = validate_candidate_id(run_id)
    releases = assert_runtime_write(runtime, public_root / "releases")

    def build(directory: Path) -> dict[str, object]:
        pq.write_table(table, directory / "observations.parquet")
        dates = table["business_date"].to_pylist()
        live_dates = live_canonical["business_date"].to_pylist()
        release_manifest = {
            "schema_version": "lutou-domestic-basis-current/3", "release_id": release_id,
            "source": "sealed_history_plus_lutou", "scope": "formal-domestic-basis",
            "quality_status": "PASS", "mapping_version": catalog.mapping_version,
            "cutover_date": FORMAL_CUTOVER_DATE.isoformat(),
            "historical_seed_id": seed.seed_id,
            "historical_seed_manifest_sha256": identify_file(seed.directory / "manifest.json").sha256,
            "historical_seed_data_sha256": str(seed.manifest["data_sha256"]),
            "historical_seed_business_sha256": str(seed.manifest["business_content_sha256"]),
            "canonical_manifest_sha256": canonical_manifest_sha256,
            "candidate_manifest_sha256": candidate_manifest_sha256,
            "data_sha256": identify_file(directory / "observations.parquet").sha256,
            "business_content_sha256": _business_sha(table),
            "live_business_content_sha256": _business_sha(live_canonical),
            "row_count": table.num_rows, "series_count": len(set(table["series_id"].to_pylist())),
            "historical_row_count": int(quality["historical_row_count"]),
            "live_row_count": int(quality["live_row_count"]),
            "source_max_date": max(live_dates).isoformat(),
            "min_date": min(dates).isoformat(), "max_date": max(dates).isoformat(),
            "formal_contract_parity": dict(parity), "quality": quality,
            "files": _file_identities(directory, ("observations.parquet",)),
        }
        _write_json(directory / "manifest.json", release_manifest)
        return release_manifest

    directory, _ = seal_immutable_candidate(releases, release_id, build)
    manifest_sha = identify_file(directory / "manifest.json").sha256
    if failure_hook:
        failure_hook("release_sealed_before_pointer")
    atomic_write_json(
        assert_runtime_write(runtime, public_root / "current.json"),
        {"schema_version": 1, "release_id": release_id, "manifest_sha256": manifest_sha},
    )
    loaded = load_domestic_basis_current(public_root)
    if loaded is None or loaded.release_id != release_id:
        raise DomesticBasisPipelineError("Formal Domestic Basis post-promotion verification failed")
    return loaded


def align_formal_domestic_basis_current(
    *, runtime: RuntimeContext, run_id: str, mapping_path: str | Path,
    historical_source_path: str | Path, formal_baseline_path: str | Path,
    failure_hook: Callable[[str], None] | None = None,
) -> DomesticBasisAlignmentResult:
    safe_run_id = validate_candidate_id(run_id)
    catalog = load_domestic_basis_catalog(mapping_path)
    if not catalog.live_verified:
        raise DomesticBasisPipelineError("Domestic Basis LIVE_CONFIRMED mapping is required")
    public_root = assert_runtime_write(
        runtime, runtime.runtime_root / "public-market-data" / "lutou-domestic-basis"
    )
    before = load_domestic_basis_current(public_root)
    if before is None or before.observations.schema != CANONICAL_SCHEMA:
        raise DomesticBasisPipelineError("Formal alignment requires the sealed Goal D3A live Current")
    seed = seal_historical_basis_seed(
        runtime=runtime, source_path=historical_source_path, mapping_path=mapping_path,
    )
    live = pa.Table.from_pylist(
        [row for row in before.observations.to_pylist() if row["business_date"] >= FORMAL_CUTOVER_DATE],
        schema=CANONICAL_SCHEMA,
    ).sort_by([(field, "ascending") for field in CANONICAL_STABLE_KEY])
    _validate_canonical(live, catalog)
    combined, _ = compose_formal_basis_current(seed, live, catalog)
    parity = compare_formal_basis_parity(combined, formal_baseline_path)
    if failure_hook:
        failure_hook("formal_contract_validated")
    current = _seal_formal_release(
        runtime=runtime, public_root=public_root, run_id=safe_run_id, seed=seed,
        live_canonical=live, catalog=catalog, parity=parity,
        canonical_manifest_sha256=str(before.manifest.get("canonical_manifest_sha256", "goal-d3a-current")),
        candidate_manifest_sha256=str(before.manifest.get("candidate_manifest_sha256", "goal-d3a-current")),
        failure_hook=failure_hook,
    )
    return DomesticBasisAlignmentResult(safe_run_id, seed, current, parity)


def _latest_dates(table: pa.Table | None) -> dict[str, date]:
    latest: dict[str, date] = {}
    if table is not None and {"series_id", "business_date"} <= set(table.column_names):
        for row in table.select(["series_id", "business_date"]).to_pylist():
            identity, day = row["series_id"], row["business_date"]
            latest[identity] = max(day, latest.get(identity, day))
    return latest


def _same_business(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    return all(left[key] == right[key] for key in CANONICAL_SCHEMA.names if key not in {"captured_at", "snapshot_identity", "query_identity"})


def _update_report(
    catalog: DomesticBasisCatalog, current: pa.Table | None, next_state: pa.Table | None,
    window: pa.Table | None, extraction: DomesticBasisExtraction | None, as_of: date,
    errors: Mapping[str, tuple[str, ...]],
) -> dict[str, object]:
    previous, following = _latest_dates(current), _latest_dates(next_state)
    required = {item.series_id for item in catalog.series}
    # A mapping cannot silently shrink the required lifecycle of an existing Current.
    required.update(previous)
    inventory = {} if extraction is None else {(item.source_product, item.region): item for item in extraction.source_inventory}
    old = {} if current is None or current.schema != CANONICAL_SCHEMA else {(row["series_id"], row["business_date"]): row for row in current.to_pylist()}
    new_counts, revisions = Counter(), Counter()
    if window is not None:
        for row in window.to_pylist():
            identity, day = row["series_id"], row["business_date"]
            if identity not in previous or day > previous[identity]:
                new_counts[identity] += 1
            elif (identity, day) not in old or not _same_business(old[identity, day], row):
                revisions[identity] += 1
    items = {}
    for mapping in catalog.series:
        identity = mapping.series_id
        source = inventory.get((mapping.source_product, mapping.region))
        items[identity] = SeriesUpdate(
            previous.get(identity), None if source is None else source.source_latest_date,
            following.get(identity), new_counts[identity], revisions[identity],
            0 if source is None else source.window_row_count, errors.get(identity, ()),
        )
    for identity in required - items.keys():
        items[identity] = SeriesUpdate(previous[identity], None, following.get(identity), errors=("MAPPING_IDENTITY_DRIFT",))
    report = evaluate_update(
        dataset_id="domestic_basis", required=required, series=items,
        next_identities=set(following), as_of_date=as_of, policy=catalog.freshness_policy,
    )
    report["source_latest_definition"] = "raw product/region latest across all quote types through query_end; not canonical freshness"
    report["mapping_version"] = catalog.mapping_version
    report["query_identity"] = None if extraction is None else extraction.query_identity
    report["snapshot_identity"] = None if extraction is None else extraction.snapshot_identity
    report["inventory_query_identity"] = None if extraction is None else extraction.inventory_query_identity
    return report


def assemble_domestic_basis_candidate(
    *, current: pa.Table | None, extraction: DomesticBasisExtraction,
    catalog: DomesticBasisCatalog, as_of_date: date,
) -> DomesticBasisCandidateState:
    """Pure candidate assembly. Does not read/write a runtime or call a provider."""
    required = {item.series_id for item in catalog.series}
    next_state, window = current, None
    errors: dict[str, tuple[str, ...]] = {}
    try:
        if current is not None:
            if set(current["series_id"].to_pylist()) != required:
                raise DomesticBasisPipelineError("MAPPING_OR_CURRENT_IDENTITY_DRIFT")
            _validate_canonical(current, catalog)
        inventory = {(item.source_product, item.region): item for item in extraction.source_inventory}
        if (len(inventory) != len(extraction.source_inventory) or set(inventory) != catalog.source_pairs
                or not extraction.inventory_query_identity or extraction.inventory_plan_estimated_rows is None
                or extraction.inventory_plan_estimated_rows < 0):
            raise DomesticBasisPipelineError("SOURCE_INVENTORY_UNVERIFIED")
        raw_counts = Counter((row.source_product, row.region) for row in extraction.records)
        if set(raw_counts) - catalog.source_pairs:
            raise DomesticBasisPipelineError("SOURCE_PRESENT_MAPPING_MISSING")
        previous = _latest_dates(current)
        for mapping in catalog.series:
            item = inventory[mapping.source_product, mapping.region]
            identity = mapping.series_id
            if type(item.window_row_count) is not int or item.window_row_count < 0 or raw_counts[mapping.source_product, mapping.region] != item.window_row_count:
                errors[identity] = ("SOURCE_PRESENT_EXTRACTION_DROPPED",)
            elif item.source_latest_date is None:
                errors[identity] = ("SOURCE_IDENTITY_UNVERIFIED",)
            elif extraction.source_min_date is None or extraction.source_max_date is None or item.source_latest_date > extraction.source_max_date:
                errors[identity] = ("SOURCE_INVENTORY_WINDOW_INVALID",)
            elif bool(item.window_row_count) != (item.source_latest_date >= extraction.source_min_date):
                errors[identity] = ("SOURCE_INVENTORY_COUNT_DATE_INCONSISTENT",)
            elif (item.window_row_count == 0 and identity in previous and item.source_latest_date > previous[identity]):
                errors[identity] = ("NEW_SOURCE_OUTSIDE_WINDOW_REQUIRES_BACKFILL",)
        if errors:
            raise DomesticBasisPipelineError("Domestic Basis source inventory reconciliation failed")
        for row in extraction.records:
            if (extraction.source_min_date is None or extraction.source_max_date is None
                    or not extraction.source_min_date <= row.business_date <= extraction.source_max_date
                    or row.business_date > inventory[row.source_product, row.region].source_latest_date):
                raise DomesticBasisPipelineError("SOURCE_WINDOW_IDENTITY_INVALID")
        standard = build_standard_table(extraction, catalog)
        candidate_gate = validate_candidate(
            standard, catalog, require_complete_series=current is None,
            require_complete_usable_series=current is None, allow_empty_increment=current is not None,
        )
        # Independent source -> Standard reconciliation, before any policy filtering.
        raw_keys = Counter((
            catalog.match(row.source_product, row.region).series_id, row.business_date,
            row.source_row_identity, row.source_identity_sha256, row.source_row_sha256,
            row.source_quote_type, row.source_contract_code,
            None if row.basis_value is None else row.basis_value.quantize(Decimal("0.0001")),
            row.source_created_at,
        ) for row in extraction.records)
        normalized_keys = Counter((
            row["series_id"], row["business_date"], row["source_row_identity"], row["source_identity_sha256"],
            row["source_row_sha256"], row["raw_quote_type"], row["raw_contract_code"], row["basis_value"], row["source_created_at"],
        ) for row in standard.to_pylist())
        if normalized_keys != raw_keys:
            raise DomesticBasisPipelineError("SOURCE_PRESENT_NORMALIZATION_DROPPED_OR_CHANGED")
        selected: dict[tuple[str, date], list[dict[str, object]]] = {}
        for row in standard.to_pylist():
            mapping = next(item for item in catalog.series if item.series_id == row["series_id"])
            if any(row[key] != value for key, value in {
                "provider_series_id": mapping.provider_series_id, "source_series_id": mapping.source_series_id,
                "provider_dataset_id": mapping.provider_dataset_id, "source_locator": mapping.source_locator,
                "product": mapping.product, "region_id": mapping.region_id, "location": mapping.region,
                "quote_type": "DOMESTIC_SPOT_BASIS", "mapping_version": catalog.mapping_version,
                "query_identity": extraction.query_identity, "snapshot_identity": extraction.snapshot_identity,
                "captured_at": extraction.extracted_at, "evidence_type": extraction.evidence_type.value,
            }.items()):
                raise DomesticBasisPipelineError("STANDARD_MAPPING_IDENTITY_MISMATCH")
            if row["exception_reason"] in {"BASIS_NULL_OR_NONNUMERIC", "CONTRACT_INVALID"}:
                errors[row["series_id"]] = ("SOURCE_PRESENT_NORMALIZATION_FAILED",)
            if row["raw_quote_type"] == "现货基差" and row["basis_value"] is not None:
                contract = _parsed_contract(row["raw_contract_code"], mapping)
                if contract is not None and (contract.year, contract.month) >= (row["business_date"].year, row["business_date"].month):
                    if not row["is_usable"]:
                        raise DomesticBasisPipelineError("VALID_SOURCE_INCORRECTLY_FILTERED")
                    selected.setdefault((row["series_id"], row["business_date"]), []).append(row)
        if errors:
            raise DomesticBasisPipelineError("Domestic Basis normalization failed")
        window, canonical_gate = build_canonical_table(
            standard, catalog, require_complete_series=current is None,
            require_complete_usable_series=current is None, allow_empty_increment=current is not None,
        )
        canonical_rows = {(row["series_id"], row["business_date"]): row for row in window.to_pylist()}
        if set(canonical_rows) != set(selected):
            raise DomesticBasisPipelineError("CANDIDATE_BUILD_DROPPED_OR_ADDED")
        for key, group in selected.items():
            nearest = min((str(row["underlying_futures_reference"]) for row in group), key=_contract_sort_key)
            chosen = [row for row in group if row["underlying_futures_reference"] == nearest]
            output = canonical_rows[key]
            if (output["source_row_count"] != len(chosen) or output["source_group_sha256"] != _source_group_sha(chosen)
                    or output["value"] != statistics.median(row["basis_value"] for row in chosen).quantize(Decimal("0.0001"))
                    or output["underlying_futures_reference"] != nearest
                    or output["query_identity"] != extraction.query_identity or output["snapshot_identity"] != extraction.snapshot_identity
                    or output["captured_at"] != extraction.extracted_at):
                raise DomesticBasisPipelineError("CANDIDATE_NORMALIZATION_OR_LINEAGE_INVALID")
        next_state = _merge_current(current, window, extraction.source_min_date)
        _validate_canonical(next_state, catalog)
        # Retaining identities is insufficient: do not silently drop historical keys.
        actual = {(row["series_id"], row["business_date"]): row for row in next_state.to_pylist()}
        old = {} if current is None else {(row["series_id"], row["business_date"]): row for row in current.to_pylist()}
        if set(actual) != set(old) | set(canonical_rows):
            raise DomesticBasisPipelineError("NEXT_STATE_HISTORY_DROPPED_OR_ADDED")
        for key, row in actual.items():
            expected = canonical_rows.get(key, old.get(key))
            if key in old and _same_business(old[key], expected):
                expected = old[key]
            if row != expected:
                raise DomesticBasisPipelineError("NEXT_STATE_CONTENT_OR_TIMESTAMP_CHANGED")
        report = _update_report(catalog, current, next_state, window, extraction, as_of_date, errors)
        try:
            validate_update_summary(report, required=required)
        except ValueError as exc:
            raise DomesticBasisPipelineError(str(exc)) from exc
        if report["summary"]["updates"]["ERROR"]:
            raise DomesticBasisPipelineError("Domestic Basis asynchronous series ERROR", update_report=report)
        candidate_gate["coverage_scope"] = "increment_only; complete identity gate is next_state"
        canonical_gate["window_row_count"] = window.num_rows
        canonical_gate["row_count"] = next_state.num_rows
        canonical_gate["series_count"] = len(required)
        canonical_gate["coverage_scope"] = "complete_next_state"
        canonical_gate["async_update"] = report
        return DomesticBasisCandidateState(standard, next_state, candidate_gate, canonical_gate, report)
    except (DomesticBasisPipelineError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, DomesticBasisPipelineError) and exc.update_report is not None:
            raise
        if not errors:
            reason = str(exc) if isinstance(exc, DomesticBasisPipelineError) else f"ASSEMBLY_{type(exc).__name__}"
            errors = {identity: (reason,) for identity in required}
        elif window is None:
            # A batch aborted before canonical assembly cannot certify the other
            # series as NO_CHANGE just because their old Current is still present.
            errors = {identity: errors.get(identity, ("CANDIDATE_NOT_EVALUATED_AFTER_BATCH_ERROR",)) for identity in required}
        report = _update_report(catalog, current, next_state, window, extraction, as_of_date, errors)
        raise DomesticBasisPipelineError(str(exc), update_report=report) from exc


def run_domestic_basis_live(
    *, runtime: RuntimeContext, run_id: str, adapter: DomesticBasisSourceAdapter,
    mapping_path: str | Path, mode: str = "auto", failure_hook: Callable[[str], None] | None = None,
    require_formal_current: bool = False, candidate_only: bool = False,
) -> DomesticBasisRunResult:
    """Persist a per-series report for success, NO_CHANGE, policy stops and errors."""
    safe_run_id = validate_candidate_id(run_id)
    root = assert_runtime_write(runtime, runtime.runtime_root / "public-market-data" / "lutou-domestic-basis")
    try:
        result = _run_domestic_basis_live(
            runtime=runtime, run_id=safe_run_id, adapter=adapter, mapping_path=mapping_path,
            mode=mode, failure_hook=failure_hook, require_formal_current=require_formal_current,
            candidate_only=candidate_only,
        )
    except Exception as exc:
        report = exc.update_report if isinstance(exc, DomesticBasisPipelineError) else None
        if report is None:
            # Do not invent source/next dates when acquisition never completed.
            reason = f"REFRESH_FAILED_{type(exc).__name__}"
            try:
                catalog = load_domestic_basis_catalog(mapping_path)
                try:
                    before = load_domestic_basis_current(root)
                    previous = None if before is None else _extract_live_canonical(before)
                except Exception:
                    previous = None
                report = _update_report(
                    catalog, previous, None, None, None, datetime.now(ZoneInfo("Asia/Shanghai")).date(),
                    {item.series_id: (reason,) for item in catalog.series},
                )
                for item in report["series"]:
                    item["reason"] = reason
                    item["coverage_status"] = "ERROR"
                report["summary"]["coverage"] = {"PRESENT": 0, "MISSING": 0, "ERROR": len(report["series"])}
            except Exception:
                report = {
                    "schema_version": "async-data-update/2", "dataset_id": "domestic_basis",
                    "required_identities_known": False, "series": [],
                    "summary": {"TOTAL_REQUIRED": 0,
                                "coverage": {"PRESENT": 0, "MISSING": 0, "ERROR": 0},
                                "updates": {"UPDATED": 0, "NO_CHANGE": 0, "ERROR": 0},
                                "freshness": {"FRESH": 0, "STALE": 0, "UNASSESSED": 0}},
                }
            report.update({
                "dataset_status": "FAILED", "promotion_allowed": False, "blocking_reasons": [reason],
                "identity_coverage": {"status": "NOT_EVALUATED", "expected_count": report["summary"]["TOTAL_REQUIRED"] or None,
                                      "actual_required_count": None, "missing": None, "unexpected": None},
            })
        try:
            _seal_update_report(runtime, root, safe_run_id, report)
        except Exception as report_error:
            exc.add_note(f"Async update report could not be sealed: {type(report_error).__name__}")
        raise
    _seal_update_report(runtime, root, safe_run_id, result.update_summary)
    return result


def _seal_update_report(runtime: RuntimeContext, root: Path, run_id: str, report: Mapping[str, object]) -> None:
    def build(directory: Path) -> dict[str, object]:
        manifest = {"run_id": run_id, **dict(report)}
        _write_json(directory / "manifest.json", manifest)
        return manifest
    seal_immutable_candidate(assert_runtime_write(runtime, root / "async-update-reports"), run_id, build)


def _run_domestic_basis_live(
    *, runtime: RuntimeContext, run_id: str, adapter: DomesticBasisSourceAdapter,
    mapping_path: str | Path, mode: str = "auto", failure_hook: Callable[[str], None] | None = None,
    require_formal_current: bool = False,
    candidate_only: bool = False,
) -> DomesticBasisRunResult:
    safe_run_id = validate_candidate_id(run_id)
    catalog = load_domestic_basis_catalog(mapping_path)
    if not catalog.live_verified:
        raise DomesticBasisPipelineError("Domestic Basis LIVE_CONFIRMED mapping is required")
    public_root = assert_runtime_write(runtime, runtime.runtime_root / "public-market-data" / "lutou-domestic-basis")
    public_root.mkdir(parents=True, exist_ok=True)
    before = load_domestic_basis_current(public_root)
    if require_formal_current and (
        before is None
        or before.observations.schema != FORMAL_CURRENT_SCHEMA
        or load_historical_basis_seed(public_root) is None
    ):
        raise DomesticBasisPipelineError("Formal Domestic Basis Current and sealed history seed are required")
    before_live = None if before is None else _extract_live_canonical(before)
    if mode not in {"auto", "full", "incremental"}:
        raise ValueError("Domestic Basis mode is invalid")
    actual_mode = ("full" if before is None else "incremental") if mode == "auto" else mode
    if actual_mode == "incremental" and before is None:
        raise DomesticBasisPipelineError("Domestic Basis incremental requires an existing Current")
    if actual_mode == "full" and before is not None:
        raise DomesticBasisPipelineError("Domestic Basis full seed refuses to replace an existing Current")
    source_min, source_max = adapter.date_bounds()
    if before is not None and source_max < date.fromisoformat(str(before.manifest["source_max_date"])):
        raise DomesticBasisPipelineError("Domestic Basis source maximum date regressed")
    start = source_min if actual_mode == "full" else date.fromisoformat(str(before.manifest["source_max_date"])) - timedelta(days=LOOKBACK_DAYS)
    extraction = adapter.extract(catalog=catalog, start_date=start, end_date=source_max)
    _validate_live_extraction(extraction, start, source_max)
    state = assemble_domestic_basis_candidate(
        current=before_live, extraction=extraction, catalog=catalog,
        as_of_date=extraction.extracted_at.astimezone(ZoneInfo("Asia/Shanghai")).date(),
    )
    standard, merged = state.standard, state.next_state
    candidate_gate, canonical_gate = state.candidate_quality, state.canonical_quality
    candidate_dir, _ = _seal_candidate(
        runtime, public_root, safe_run_id, standard, extraction, catalog, candidate_gate,
        next_state=merged, update_report=state.update_report,
    )
    if failure_hook:
        failure_hook("candidate_sealed")
    if candidate_only:
        return DomesticBasisRunResult(safe_run_id, actual_mode, start, source_max, candidate_dir, None, before, False, candidate_gate, canonical_gate)
    if not state.update_report["promotion_allowed"]:
        raise DomesticBasisPipelineError(
            "Domestic Basis async update policy blocks promotion", update_report=state.update_report,
        )
    if before_live is not None and _business_sha(before_live) == _business_sha(merged):
        return DomesticBasisRunResult(safe_run_id, actual_mode, start, source_max, candidate_dir, None, before, False, candidate_gate, canonical_gate)
    canonical_dir, manifest = _seal_canonical(runtime, public_root, safe_run_id, candidate_dir, merged, extraction, catalog, canonical_gate, source_max)
    if failure_hook:
        failure_hook("canonical_sealed")
    current = promote_domestic_basis(runtime=runtime, canonical_directory=canonical_dir, mapping_path=mapping_path, failure_hook=failure_hook)
    return DomesticBasisRunResult(safe_run_id, actual_mode, start, source_max, candidate_dir, canonical_dir, current, True, candidate_gate, canonical_gate)


def _validate_live_extraction(
    extraction: DomesticBasisExtraction, start: date, end: date
) -> None:
    if extraction.evidence_type is not DomesticBasisEvidenceType.LIVE_DATABASE:
        raise DomesticBasisPipelineError("Domestic Basis live run requires live database evidence")
    if extraction.source_min_date != start or extraction.source_max_date != end:
        raise DomesticBasisPipelineError("Domestic Basis live extraction window identity is invalid")
    proof = extraction.connection_proof
    if (
        not isinstance(proof, Mapping)
        or proof.get("transaction_read_only") is not True
        or proof.get("write_privileges") != []
    ):
        raise DomesticBasisPipelineError("Domestic Basis live read-only proof is invalid")
    schema = extraction.schema_proof
    if (
        not isinstance(schema, Mapping)
        or schema.get("column_count") != 19
        or schema.get("date_indexed") is not True
    ):
        raise DomesticBasisPipelineError("Domestic Basis live schema proof is invalid")
    if extraction.plan_estimated_rows is None or extraction.plan_estimated_rows < 0:
        raise DomesticBasisPipelineError("Domestic Basis live query-plan proof is invalid")


def run_domestic_basis_offline_fixture(*, runtime: RuntimeContext, run_id: str, extraction: DomesticBasisExtraction, mapping_path: str | Path) -> DomesticBasisOfflineResult:
    if extraction.evidence_type is not DomesticBasisEvidenceType.DETERMINISTIC_TEST_FIXTURE:
        raise DomesticBasisPipelineError("D3A offline runner accepts deterministic fixtures only")
    safe_run_id, catalog = validate_candidate_id(run_id), load_domestic_basis_catalog(mapping_path)
    if catalog.live_verified:
        raise DomesticBasisPipelineError("D3A offline runner must not use LIVE_CONFIRMED mapping")
    public_root = assert_runtime_write(runtime, runtime.runtime_root / "public-market-data" / "lutou-domestic-basis")
    public_root.mkdir(parents=True, exist_ok=True)
    standard = build_standard_table(extraction, catalog)
    gate = validate_candidate(standard, catalog)
    canonical, report = build_canonical_table(standard, catalog)
    candidate_dir, candidate_manifest = _seal_candidate(runtime, public_root, safe_run_id, standard, extraction, catalog, gate)
    simulation_dir, simulation_manifest = _seal_canonical_simulation(runtime, public_root, safe_run_id, candidate_dir, canonical, extraction, catalog, report)
    return DomesticBasisOfflineResult(safe_run_id, candidate_dir, simulation_dir, candidate_manifest, simulation_manifest)


def promote_domestic_basis(*, runtime: RuntimeContext, canonical_directory: str | Path, mapping_path: str | Path, failure_hook: Callable[[str], None] | None = None) -> DomesticBasisCurrent:
    catalog = load_domestic_basis_catalog(mapping_path)
    if not catalog.live_verified:
        raise DomesticBasisPipelineError("Domestic Basis LIVE_CONFIRMED mapping is required")
    canonical_path = Path(canonical_directory)
    manifest = _read_json(canonical_path / "manifest.json")
    update_report = manifest.get("quality", {}).get("async_update", {})
    try:
        validate_update_summary(update_report, required={item.series_id for item in catalog.series})
    except ValueError as exc:
        raise DomesticBasisPipelineError(str(exc)) from exc
    if (update_report.get("schema_version") != "async-data-update/2"
            or update_report.get("promotion_allowed") is not True
            or update_report.get("policy") != asdict(catalog.freshness_policy)):
        raise DomesticBasisPipelineError("Domestic Basis async update promotion evidence is invalid")
    data_path = canonical_path / "observations.parquet"
    if manifest.get("evidence_type") != "LIVE_DATABASE" or manifest.get("production_authorized") is not True or manifest.get("quality_status") != "PASS":
        raise DomesticBasisPipelineError("Domestic Basis Canonical is not promotion-authorized")
    expected = manifest.get("files", {}).get("observations.parquet", {}).get("sha256") if isinstance(manifest.get("files"), dict) else None
    if expected != identify_file(data_path).sha256:
        raise DomesticBasisPipelineError("Domestic Basis Canonical file identity is invalid")
    release_id = validate_candidate_id(str(manifest["run_id"]))
    public_root = assert_runtime_write(runtime, runtime.runtime_root / "public-market-data" / "lutou-domestic-basis")
    seed = load_historical_basis_seed(public_root)
    if seed is not None:
        before = load_domestic_basis_current(public_root)
        if before is None or before.observations.schema != FORMAL_CURRENT_SCHEMA:
            raise DomesticBasisPipelineError("Formal Domestic Basis promotion requires an aligned Current")
        parity = before.manifest.get("formal_contract_parity")
        if not isinstance(parity, Mapping):
            raise DomesticBasisPipelineError("Formal Domestic Basis parity evidence is missing")
        live_canonical = pq.read_table(data_path)
        _validate_canonical(live_canonical, catalog)
        return _seal_formal_release(
            runtime=runtime, public_root=public_root, run_id=release_id, seed=seed,
            live_canonical=live_canonical, catalog=catalog, parity=parity,
            canonical_manifest_sha256=identify_file(canonical_path / "manifest.json").sha256,
            candidate_manifest_sha256=str(manifest["candidate_manifest_sha256"]),
            failure_hook=failure_hook,
        )
    releases = assert_runtime_write(runtime, public_root / "releases")
    def build(directory: Path) -> dict[str, object]:
        (directory / "observations.parquet").write_bytes(data_path.read_bytes())
        release_manifest = {
            "schema_version": "lutou-domestic-basis-current/2", "release_id": release_id,
            "source": "lutou", "scope": "domestic-basis-consumer-needed", "quality_status": "PASS",
            "mapping_version": catalog.mapping_version,
            "canonical_manifest_sha256": identify_file(canonical_path / "manifest.json").sha256,
            "candidate_manifest_sha256": str(manifest["candidate_manifest_sha256"]),
            "query_identity": str(manifest["query_identity"]),
            "snapshot_identity": str(manifest["snapshot_identity"]),
            "data_sha256": identify_file(directory / "observations.parquet").sha256,
            "business_content_sha256": str(manifest["business_content_sha256"]),
            "row_count": int(manifest["row_count"]), "series_count": int(manifest["series_count"]),
            "source_max_date": str(manifest["source_max_date"]), "min_date": str(manifest["min_date"]),
            "max_date": str(manifest["max_date"]), "files": _file_identities(directory, ("observations.parquet",)),
        }
        _write_json(directory / "manifest.json", release_manifest)
        return release_manifest
    directory, _ = seal_immutable_candidate(releases, release_id, build)
    manifest_sha = identify_file(directory / "manifest.json").sha256
    if failure_hook:
        failure_hook("release_sealed_before_pointer")
    atomic_write_json(assert_runtime_write(runtime, public_root / "current.json"), {"schema_version": 1, "release_id": release_id, "manifest_sha256": manifest_sha})
    loaded = load_domestic_basis_current(public_root)
    if loaded is None or loaded.release_id != release_id:
        raise DomesticBasisPipelineError("Domestic Basis post-promotion verification failed")
    return loaded


def load_domestic_basis_current(public_root: str | Path) -> DomesticBasisCurrent | None:
    root, pointer_path = Path(public_root), Path(public_root) / "current.json"
    if not pointer_path.is_file():
        return None
    pointer = _read_json(pointer_path)
    if set(pointer) != {"schema_version", "release_id", "manifest_sha256"}:
        raise DomesticBasisPipelineError("Domestic Basis Current pointer is invalid")
    directory = root / "releases" / str(pointer["release_id"])
    manifest_path = directory / "manifest.json"
    if identify_file(manifest_path).sha256 != pointer["manifest_sha256"]:
        raise DomesticBasisPipelineError("Domestic Basis Current manifest identity mismatch")
    manifest, observations = _read_json(manifest_path), pq.read_table(directory / "observations.parquet")
    schema_version = manifest.get("schema_version")
    if manifest.get("quality_status") != "PASS":
        raise DomesticBasisPipelineError("Domestic Basis Current contract is invalid")
    if schema_version == "lutou-domestic-basis-current/2":
        if observations.schema != CANONICAL_SCHEMA:
            raise DomesticBasisPipelineError("Domestic Basis Current contract is invalid")
    elif schema_version == "lutou-domestic-basis-current/3":
        if observations.schema != FORMAL_CURRENT_SCHEMA:
            raise DomesticBasisPipelineError("Formal Domestic Basis Current contract is invalid")
    else:
        raise DomesticBasisPipelineError("Domestic Basis Current schema version is unsupported")
    if identify_file(directory / "observations.parquet").sha256 != manifest.get("data_sha256"):
        raise DomesticBasisPipelineError("Domestic Basis Current data identity mismatch")
    if _business_sha(observations) != manifest.get("business_content_sha256"):
        raise DomesticBasisPipelineError("Domestic Basis Current business identity mismatch")
    if observations.schema == CANONICAL_SCHEMA:
        _validate_canonical(observations, None)
    else:
        _validate_formal_current(observations, None, require_live=True)
        seed = load_historical_basis_seed(root)
        parity = manifest.get("formal_contract_parity")
        live = _extract_live_canonical(
            DomesticBasisCurrent(str(pointer["release_id"]), directory, manifest, observations)
        )
        if (
            seed is None
            or manifest.get("historical_seed_manifest_sha256")
            != identify_file(seed.directory / "manifest.json").sha256
            or manifest.get("historical_seed_data_sha256") != seed.manifest.get("data_sha256")
            or manifest.get("historical_seed_business_sha256")
            != seed.manifest.get("business_content_sha256")
            or manifest.get("live_business_content_sha256") != _business_sha(live)
            or manifest.get("historical_row_count") != 16_331
            or manifest.get("live_row_count") != live.num_rows
            or not isinstance(parity, Mapping)
            or parity.get("quality_status") != "PASS"
            or parity.get("baseline_sha256") != FORMAL_BASELINE_SHA256
            or parity.get("baseline_only_rows") != 0
            or parity.get("public_only_nonextension_rows") != 0
            or not isinstance(parity.get("field_differences"), Mapping)
            or any(parity["field_differences"].values())
        ):
            raise DomesticBasisPipelineError("Formal Domestic Basis manifest, seed or parity identity mismatch")
    return DomesticBasisCurrent(str(pointer["release_id"]), directory, manifest, observations)


def compare_legacy_parity(
    canonical: pa.Table,
    legacy_rows: Sequence[Mapping[str, object]],
    catalog: DomesticBasisCatalog,
) -> tuple[dict[str, object], ...]:
    """Compare the sealed legacy aggregation with Public Canonical, per series."""

    by_consumer = {(x.consumer_product, x.region): x for x in catalog.series}
    legacy: dict[str, dict[tuple[date, str], Mapping[str, object]]] = {
        x.series_id: {} for x in catalog.series
    }
    for row in legacy_rows:
        mapping = by_consumer.get((str(row["commodity"]), str(row["region"])))
        if mapping is None:
            continue
        raw_date = row["date"]
        business_date = raw_date.date() if hasattr(raw_date, "date") else date.fromisoformat(str(raw_date)[:10])
        key = (business_date, str(row["futures_contract"]))
        if key in legacy[mapping.series_id]:
            raise DomesticBasisPipelineError("Legacy Domestic Basis parity key is duplicated")
        legacy[mapping.series_id][key] = row
    public: dict[str, dict[tuple[date, str], Mapping[str, object]]] = {
        x.series_id: {} for x in catalog.series
    }
    for row in canonical.to_pylist():
        reference = _parse_reference(str(row["underlying_futures_reference"]))
        contract = f"{reference.year % 100:02d}{reference.month:02d}"
        public[str(row["series_id"])][(row["business_date"], contract)] = row
    reports: list[dict[str, object]] = []
    for mapping in catalog.series:
        old, new = legacy[mapping.series_id], public[mapping.series_id]
        common = sorted(set(old) & set(new))
        basis_diff = sum(
            Decimal(str(old[key]["basis"])).quantize(Decimal("0.0001")) != new[key]["value"]
            for key in common
        )
        cash_diff = sum(not _nullable_number_equal(old[key].get("cash_price"), new[key].get("cash_price")) for key in common)
        futures_diff = sum(not _nullable_number_equal(old[key].get("futures_price"), new[key].get("futures_price")) for key in common)
        reports.append({
            "series_id": mapping.series_id, "legacy_rows": len(old), "public_rows": len(new),
            "common_stable_keys": len(common), "basis_differences": basis_diff,
            "cash_price_differences": cash_diff, "futures_price_differences": futures_diff,
            "legacy_only": len(set(old) - set(new)), "public_only": len(set(new) - set(old)),
        })
    return tuple(reports)


def _nullable_number_equal(left: object, right: object) -> bool:
    def normalized(value: object) -> Decimal | None:
        if value is None:
            return None
        try:
            parsed = Decimal(str(value))
        except Exception:
            return None
        return None if not parsed.is_finite() else parsed.quantize(Decimal("0.0001"))
    return normalized(left) == normalized(right)


def _merge_current(current: pa.Table | None, window: pa.Table, start: date) -> pa.Table:
    if current is None:
        return window
    # The query window is not a deletion boundary. Preserve old observations and
    # their timestamps; upsert only valid canonical keys actually returned.
    rows = {(row["series_id"], row["business_date"]): row for row in current.to_pylist()}
    for row in window.to_pylist():
        key = row["series_id"], row["business_date"]
        if key not in rows or not _same_business(rows[key], row):
            rows[key] = row
    merged = pa.Table.from_pylist(list(rows.values()), schema=CANONICAL_SCHEMA)
    return merged.sort_by([(x, "ascending") for x in CANONICAL_STABLE_KEY])


def _validate_canonical(table: pa.Table, catalog: DomesticBasisCatalog | None) -> None:
    if table.schema != CANONICAL_SCHEMA or table.num_rows == 0 or _duplicate_count(table, CANONICAL_STABLE_KEY):
        raise DomesticBasisPipelineError("Domestic Basis Canonical contract or stable keys are invalid")
    if catalog is not None and set(table["series_id"].to_pylist()) != {x.series_id for x in catalog.series}:
        raise DomesticBasisPipelineError("Domestic Basis Canonical series coverage is incomplete")
    if set(table["currency"].to_pylist()) != {"CNY"} or set(table["unit"].to_pylist()) != {"CNY/metric_tonne"}:
        raise DomesticBasisPipelineError("Domestic Basis Canonical currency/unit is invalid")
    if catalog is not None:
        mappings = {item.series_id: item for item in catalog.series}
        for row in table.to_pylist():
            mapping = mappings[row["series_id"]]
            if any(row[key] != value for key, value in {
                "provider_series_id": mapping.provider_series_id, "source_series_id": mapping.source_series_id,
                "provider_dataset_id": mapping.provider_dataset_id, "source_locator": mapping.source_locator,
                "product": mapping.product, "consumer_product": mapping.consumer_product,
                "region_id": mapping.region_id, "location": mapping.region, "quote_type": "DOMESTIC_SPOT_BASIS",
            }.items()) or any(row[key] is None for key in CANONICAL_SCHEMA.names if not CANONICAL_SCHEMA.field(key).nullable):
                raise DomesticBasisPipelineError("Domestic Basis Canonical mapping/schema identity is invalid")


def _seal_candidate(runtime: RuntimeContext, public_root: Path, run_id: str, standard: pa.Table, extraction: DomesticBasisExtraction, catalog: DomesticBasisCatalog, gate: Mapping[str, object], *, next_state: pa.Table | None = None, update_report: Mapping[str, object] | None = None) -> tuple[Path, dict[str, object]]:
    result: dict[str, object] = {}
    def build(directory: Path) -> dict[str, object]:
        pq.write_table(standard, directory / "standard.parquet")
        names = ["standard.parquet"]
        if next_state is not None:
            pq.write_table(next_state, directory / "next-state.parquet")
            names.append("next-state.parquet")
        if update_report is not None:
            _write_json(directory / "async-update.json", update_report)
            names.append("async-update.json")
        dates = standard["business_date"].to_pylist()
        manifest = {
            "schema_version": "lutou-domestic-basis-candidate/2", "run_id": run_id, "source": "lutou",
            "scope": "domestic-basis-consumer-needed", "evidence_type": extraction.evidence_type.value,
            "live_verification_status": "LIVE_CONFIRMED" if catalog.live_verified else "LIVE_CONFIRMATION_PENDING",
            "mapping_version": catalog.mapping_version, "query_identity": extraction.query_identity,
            "snapshot_identity": extraction.snapshot_identity, "min_date": min(dates).isoformat() if dates else None,
            "max_date": max(dates).isoformat() if dates else None, "row_count": standard.num_rows,
            "source_min_date": None if extraction.source_min_date is None else extraction.source_min_date.isoformat(),
            "source_max_date": None if extraction.source_max_date is None else extraction.source_max_date.isoformat(),
            "query_plan_estimated_rows": extraction.plan_estimated_rows,
            "partition_count": extraction.partition_count,
            "partition_windows": [
                {"start_date": start.isoformat(), "end_date": end.isoformat()}
                for start, end in extraction.partition_windows
            ],
            "connection_proof": None if extraction.connection_proof is None else dict(extraction.connection_proof),
            "source_schema_proof": None if extraction.schema_proof is None else dict(extraction.schema_proof),
            "quality_status": "PASS", "quality": dict(gate),
            "promotion_authorized": bool(update_report and update_report["promotion_allowed"]) and catalog.live_verified and extraction.evidence_type is DomesticBasisEvidenceType.LIVE_DATABASE,
            "async_update": None if update_report is None else dict(update_report),
            "inventory_query_identity": extraction.inventory_query_identity,
            "inventory_plan_estimated_rows": extraction.inventory_plan_estimated_rows,
            "source_inventory": [
                {"source_product": item.source_product, "region": item.region,
                 "source_latest_date": None if item.source_latest_date is None else item.source_latest_date.isoformat(),
                 "window_row_count": item.window_row_count} for item in extraction.source_inventory
            ],
            "files": _file_identities(directory, names),
        }
        _write_json(directory / "manifest.json", manifest); result.update(manifest); return manifest
    directory, _ = seal_immutable_candidate(assert_runtime_write(runtime, public_root / "candidates"), run_id, build)
    return directory, result


def _seal_canonical(runtime: RuntimeContext, public_root: Path, run_id: str, candidate_dir: Path, canonical: pa.Table, extraction: DomesticBasisExtraction, catalog: DomesticBasisCatalog, report: Mapping[str, object], source_max: date) -> tuple[Path, dict[str, object]]:
    result: dict[str, object] = {}
    def build(directory: Path) -> dict[str, object]:
        pq.write_table(canonical, directory / "observations.parquet")
        dates = canonical["business_date"].to_pylist()
        manifest = {
            "schema_version": "lutou-domestic-basis-canonical/2", "run_id": run_id, "source": "lutou",
            "scope": "domestic-basis-consumer-needed", "evidence_type": extraction.evidence_type.value,
            "live_verification_status": "LIVE_CONFIRMED", "mapping_version": catalog.mapping_version,
            "candidate_manifest_sha256": identify_file(candidate_dir / "manifest.json").sha256,
            "query_identity": extraction.query_identity, "snapshot_identity": extraction.snapshot_identity,
            "row_count": canonical.num_rows, "series_count": len(set(canonical["series_id"].to_pylist())),
            "min_date": min(dates).isoformat(), "max_date": max(dates).isoformat(),
            "source_max_date": source_max.isoformat(), "business_content_sha256": _business_sha(canonical),
            "quality_status": "PASS", "quality": dict(report), "production_authorized": True,
            "files": _file_identities(directory, ("observations.parquet",)),
        }
        _write_json(directory / "manifest.json", manifest); result.update(manifest); return manifest
    directory, _ = seal_immutable_candidate(assert_runtime_write(runtime, public_root / "canonical"), run_id, build)
    return directory, result


def _seal_canonical_simulation(runtime: RuntimeContext, public_root: Path, run_id: str, candidate_dir: Path, canonical: pa.Table, extraction: DomesticBasisExtraction, catalog: DomesticBasisCatalog, report: Mapping[str, object]) -> tuple[Path, dict[str, object]]:
    result: dict[str, object] = {}
    def build(directory: Path) -> dict[str, object]:
        pq.write_table(canonical, directory / "observations.parquet")
        dates = canonical["business_date"].to_pylist()
        manifest = {
            "schema_version": "lutou-domestic-basis-canonical-simulation/2", "run_id": run_id,
            "source": "lutou", "scope": "domestic-basis-consumer-needed", "evidence_type": extraction.evidence_type.value,
            "live_verification_status": "LIVE_CONFIRMATION_PENDING", "mapping_version": catalog.mapping_version,
            "candidate_manifest_sha256": identify_file(candidate_dir / "manifest.json").sha256,
            "row_count": canonical.num_rows, "series_count": len(set(canonical["series_id"].to_pylist())),
            "min_date": min(dates).isoformat(), "max_date": max(dates).isoformat(),
            "quality_status": "PASS", "quality": dict(report), "production_authorized": False,
            "files": _file_identities(directory, ("observations.parquet",)),
        }
        _write_json(directory / "manifest.json", manifest); result.update(manifest); return manifest
    directory, _ = seal_immutable_candidate(assert_runtime_write(runtime, public_root / "canonical-simulations"), run_id, build)
    return directory, result


def _parse_reference(value: str) -> ContractId:
    exchange, product, contract = value.split(":")
    year, month = contract.split("-")
    return ContractId(Exchange(exchange), product, int(year), int(month))


def _contract_sort_key(value: str) -> tuple[int, int]:
    contract = _parse_reference(value)
    return contract.year, contract.month


def _source_group_sha(rows: Sequence[Mapping[str, object]]) -> str:
    payload = json.dumps(sorted(str(x["source_row_identity"]) for x in rows), separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _business_sha(table: pa.Table) -> str:
    fields = [x for x in CANONICAL_SCHEMA.names if x not in {"captured_at", "snapshot_identity", "query_identity"}]
    rows = []
    for row in table.select(fields).to_pylist():
        rows.append({k: (v.isoformat() if isinstance(v, date) else str(v) if isinstance(v, Decimal) else v) for k, v in row.items()})
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _duplicate_count(table: pa.Table, key: Sequence[str]) -> int:
    seen: set[tuple[object, ...]] = set(); duplicates = 0
    for row in table.select(key).to_pylist():
        identity = tuple(row[x] for x in key); duplicates += int(identity in seen); seen.add(identity)
    return duplicates


def _file_identities(directory: Path, names: Sequence[str]) -> dict[str, object]:
    return {name: {"sha256": identify_file(directory / name).sha256, "size_bytes": identify_file(directory / name).size_bytes} for name in names}


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DomesticBasisPipelineError("Domestic Basis manifest must be an object")
    return value


__all__ = [
    "LOOKBACK_DAYS", "FORMAL_CUTOVER_DATE", "FORMAL_CURRENT_SCHEMA", "FORMAL_STABLE_KEY",
    "CANONICAL_SCHEMA", "CANONICAL_STABLE_KEY", "STANDARD_SCHEMA", "STANDARD_STABLE_KEY",
    "DomesticBasisAlignmentResult", "DomesticBasisCurrent", "DomesticBasisHistoricalSeed",
    "DomesticBasisOfflineResult", "DomesticBasisPipelineError", "DomesticBasisRunResult",
    "align_formal_domestic_basis_current", "build_historical_seed_table", "compare_formal_basis_parity",
    "compose_formal_basis_current", "load_historical_basis_seed", "seal_historical_basis_seed",
    "build_canonical_table", "build_standard_table", "load_domestic_basis_current", "promote_domestic_basis",
    "compare_legacy_parity", "run_domestic_basis_live", "run_domestic_basis_offline_fixture",
    "simulate_canonical_policy", "validate_candidate",
]
