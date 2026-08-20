"""Lutou Domestic Basis Standard, Candidate, Canonical and isolated Current."""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping, Sequence

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
from agri_research_agent.shared.file_identity import identify_file
from agri_research_agent.shared.immutable_candidate import seal_immutable_candidate, validate_candidate_id
from agri_research_agent.shared.runtime_context import RuntimeContext, assert_runtime_write

LOOKBACK_DAYS = 31
DECIMAL_TYPE = pa.decimal128(20, 4)
STANDARD_STABLE_KEY = ("provider_series_id", "business_date", "source_row_identity")
CANONICAL_STABLE_KEY = ("series_id", "business_date")

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


class DomesticBasisPipelineError(RuntimeError):
    pass


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
    current: DomesticBasisCurrent
    promoted: bool
    candidate_quality: Mapping[str, object]
    canonical_quality: Mapping[str, object]


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
) -> dict[str, object]:
    if table.schema != STANDARD_SCHEMA or table.num_rows == 0:
        raise DomesticBasisPipelineError("Domestic Basis Candidate schema is invalid")
    duplicates = _duplicate_count(table, STANDARD_STABLE_KEY)
    if duplicates:
        raise DomesticBasisPipelineError("Domestic Basis source stable key is duplicated")
    rows = table.to_pylist()
    expected = {x.series_id for x in catalog.series}
    observed = {str(x["series_id"]) for x in rows}
    usable_series = {str(x["series_id"]) for x in rows if x["is_usable"]}
    if require_complete_series and observed != expected:
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
    if any(not str(row[field]).strip() for row in rows for field in required):
        raise DomesticBasisPipelineError("Domestic Basis Candidate identity is incomplete")
    if set(table["currency"].to_pylist()) != {"CNY"} or set(table["unit"].to_pylist()) != {"CNY/metric_tonne"}:
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
        "min_date": min(x["business_date"] for x in rows).isoformat(),
        "max_date": max(x["business_date"] for x in rows).isoformat(),
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
) -> tuple[pa.Table, dict[str, object]]:
    gate = validate_candidate(
        table,
        catalog,
        require_complete_series=require_complete_series,
        require_complete_usable_series=require_complete_usable_series,
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


def run_domestic_basis_live(
    *, runtime: RuntimeContext, run_id: str, adapter: DomesticBasisSourceAdapter,
    mapping_path: str | Path, mode: str = "auto", failure_hook: Callable[[str], None] | None = None,
) -> DomesticBasisRunResult:
    safe_run_id = validate_candidate_id(run_id)
    catalog = load_domestic_basis_catalog(mapping_path)
    if not catalog.live_verified:
        raise DomesticBasisPipelineError("Domestic Basis LIVE_CONFIRMED mapping is required")
    public_root = assert_runtime_write(runtime, runtime.runtime_root / "public-market-data" / "lutou-domestic-basis")
    public_root.mkdir(parents=True, exist_ok=True)
    before = load_domestic_basis_current(public_root)
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
    standard = build_standard_table(extraction, catalog)
    candidate_gate = validate_candidate(
        standard,
        catalog,
        require_complete_series=True,
        require_complete_usable_series=actual_mode == "full",
    )
    candidate_dir, _ = _seal_candidate(runtime, public_root, safe_run_id, standard, extraction, catalog, candidate_gate)
    if failure_hook:
        failure_hook("candidate_sealed")
    window, canonical_gate = build_canonical_table(
        standard,
        catalog,
        require_complete_series=True,
        require_complete_usable_series=actual_mode == "full",
    )
    merged = _merge_current(before, window, start)
    _validate_canonical(merged, catalog)
    if before is not None and _business_sha(before.observations) == _business_sha(merged):
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
    data_path = canonical_path / "observations.parquet"
    if manifest.get("evidence_type") != "LIVE_DATABASE" or manifest.get("production_authorized") is not True or manifest.get("quality_status") != "PASS":
        raise DomesticBasisPipelineError("Domestic Basis Canonical is not promotion-authorized")
    expected = manifest.get("files", {}).get("observations.parquet", {}).get("sha256") if isinstance(manifest.get("files"), dict) else None
    if expected != identify_file(data_path).sha256:
        raise DomesticBasisPipelineError("Domestic Basis Canonical file identity is invalid")
    release_id = validate_candidate_id(str(manifest["run_id"]))
    public_root = assert_runtime_write(runtime, runtime.runtime_root / "public-market-data" / "lutou-domestic-basis")
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
    if manifest.get("quality_status") != "PASS" or observations.schema != CANONICAL_SCHEMA:
        raise DomesticBasisPipelineError("Domestic Basis Current contract is invalid")
    if identify_file(directory / "observations.parquet").sha256 != manifest.get("data_sha256"):
        raise DomesticBasisPipelineError("Domestic Basis Current data identity mismatch")
    if _business_sha(observations) != manifest.get("business_content_sha256"):
        raise DomesticBasisPipelineError("Domestic Basis Current business identity mismatch")
    _validate_canonical(observations, None)
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


def _merge_current(current: DomesticBasisCurrent | None, window: pa.Table, start: date) -> pa.Table:
    if current is None:
        return window
    history = [x for x in current.observations.to_pylist() if x["business_date"] < start]
    merged = pa.Table.from_pylist(history + window.to_pylist(), schema=CANONICAL_SCHEMA)
    return merged.sort_by([(x, "ascending") for x in CANONICAL_STABLE_KEY])


def _validate_canonical(table: pa.Table, catalog: DomesticBasisCatalog | None) -> None:
    if table.schema != CANONICAL_SCHEMA or table.num_rows == 0 or _duplicate_count(table, CANONICAL_STABLE_KEY):
        raise DomesticBasisPipelineError("Domestic Basis Canonical contract or stable keys are invalid")
    if catalog is not None and set(table["series_id"].to_pylist()) != {x.series_id for x in catalog.series}:
        raise DomesticBasisPipelineError("Domestic Basis Canonical series coverage is incomplete")
    if set(table["currency"].to_pylist()) != {"CNY"} or set(table["unit"].to_pylist()) != {"CNY/metric_tonne"}:
        raise DomesticBasisPipelineError("Domestic Basis Canonical currency/unit is invalid")


def _seal_candidate(runtime: RuntimeContext, public_root: Path, run_id: str, standard: pa.Table, extraction: DomesticBasisExtraction, catalog: DomesticBasisCatalog, gate: Mapping[str, object]) -> tuple[Path, dict[str, object]]:
    result: dict[str, object] = {}
    def build(directory: Path) -> dict[str, object]:
        pq.write_table(standard, directory / "standard.parquet")
        dates = standard["business_date"].to_pylist()
        manifest = {
            "schema_version": "lutou-domestic-basis-candidate/2", "run_id": run_id, "source": "lutou",
            "scope": "domestic-basis-consumer-needed", "evidence_type": extraction.evidence_type.value,
            "live_verification_status": "LIVE_CONFIRMED" if catalog.live_verified else "LIVE_CONFIRMATION_PENDING",
            "mapping_version": catalog.mapping_version, "query_identity": extraction.query_identity,
            "snapshot_identity": extraction.snapshot_identity, "min_date": min(dates).isoformat(),
            "max_date": max(dates).isoformat(), "row_count": standard.num_rows,
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
            "promotion_authorized": catalog.live_verified and extraction.evidence_type is DomesticBasisEvidenceType.LIVE_DATABASE,
            "files": _file_identities(directory, ("standard.parquet",)),
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
    "LOOKBACK_DAYS", "CANONICAL_SCHEMA", "CANONICAL_STABLE_KEY", "STANDARD_SCHEMA", "STANDARD_STABLE_KEY",
    "DomesticBasisCurrent", "DomesticBasisOfflineResult", "DomesticBasisPipelineError", "DomesticBasisRunResult",
    "build_canonical_table", "build_standard_table", "load_domestic_basis_current", "promote_domestic_basis",
    "compare_legacy_parity", "run_domestic_basis_live", "run_domestic_basis_offline_fixture",
    "simulate_canonical_policy", "validate_candidate",
]
