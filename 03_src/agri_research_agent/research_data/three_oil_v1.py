"""Sealed three-oil V1 identities, formulas, conversions, and page topology."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .canonical_spreads import (
    EXACT_INNER_JOIN,
    FULL_SEASONALITY,
    READY_STATUS,
    CanonicalSpreadError,
    CollapsedObservation,
    PalmCoreSixCatalog,
    collapse_source_native_duplicates,
    load_palm_core_six,
)


THREE_OIL_V1_STATUS = "THREE_OIL_V1_READY"
USER_APPROVED_BUSINESS_DEFINITION = "USER_APPROVED_BUSINESS_DEFINITION"
RECOVERED_HISTORICAL_FORMULA = "RECOVERED_HISTORICAL_FORMULA"
PALM_CORE_SIX_SEALED = "PALM_CORE_SIX_SEALED"
_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_EXPECTED_COUNTS = {"series": 20, "derived_series": 2, "spreads": 20}
_EXPECTED_PAGE_TOPOLOGY = {
    "page.palm.v1": (
        ("spread.international.soy_palm", "spread.international.rape_palm", "spread.international.sunflower_palm"),
        ("spread.europe.soy_palm", "spread.europe.rape_palm", "spread.europe.sunflower_palm"),
        ("spread.energy.pogo.indonesia_cpo_ice_diesel",),
    ),
    "page.soy.v1.page1": (
        ("spread.international.rape_soy", "spread.europe.rape_soy", "spread.india.hvo_refined_soy"),
        ("spread.international.soy_palm", "spread.europe.soy_palm", "spread.india.refined_soy_refined_palm"),
        ("spread.international.sunflower_soy", "spread.india.refined_sunflower_refined_soy", "spread.energy.boho.us_soy_ice_diesel"),
    ),
    "page.soy.v1.page2": (
        ("market.basis.soybean_oil.argentina.upper_river.spot", "spread.soybean_oil.argentina_brazil", "spread.soybean_oil.argentina_us_central_illinois"),
        ("market.basis.soybean_oil.us.central_illinois.spot", "market.environmental_credit.rin.d4", "spread.soybean_oil.cbot_board_argentina.freight_30"),
    ),
    "page.rape.v1.page1": (
        ("spread.international.rape_soy", "spread.europe.rape_soy", "spread.india.hvo_refined_soy"),
        ("spread.international.rape_palm", "spread.europe.rape_palm", "spread.india.hvo_refined_palm"),
        ("market.biofuel.rme.europe.ara.spot", "spread.europe.sunflower_rape", "spread.europe.rme_ara_diesel_premium"),
    ),
}


@dataclass(frozen=True, slots=True)
class ConversionRule:
    conversion_id: str
    operation: str
    operand: Decimal
    input_unit: str
    output_unit: str
    deterministic: bool
    layer: str


@dataclass(frozen=True, slots=True)
class CanonicalSeriesContract:
    series_id: str
    status: str
    dataset_id: str
    provider_series_id: str
    provider: str
    origin_system: str
    acquisition_channel: str
    source_locator: str
    source_native_table: str
    source_native_series: str
    product: str
    product_grade: str
    country: str
    region: str
    location_native: str
    quote_basis: str
    tenor: str
    price_type: str
    currency: str
    unit: str
    source_unit: str
    conversion_id: str | None
    earliest_date: date
    latest_date: date
    observation_count: int | None
    observations_by_year: Mapping[int, int]
    same_value_duplicate_count: int
    conflicting_duplicate_date_count: int
    metadata_evidence: str


@dataclass(frozen=True, slots=True)
class LinearTerm:
    series_id: str
    multiplier: Decimal


@dataclass(frozen=True, slots=True)
class DerivedSeriesContract:
    series_id: str
    display_name: str
    status: str
    formula: str
    terms: tuple[LinearTerm, ...]
    constant: Decimal
    conversion_ids: tuple[str, ...]
    join: str
    currency: str
    unit: str
    earliest_date: date
    latest_date: date
    observation_count: int
    observations_by_year: Mapping[int, int]
    definition_evidence: str


@dataclass(frozen=True, slots=True)
class FixedAssumption:
    assumption_id: str
    value: Decimal
    currency: str
    unit: str
    purpose: str
    evidence: str


@dataclass(frozen=True, slots=True)
class SpreadContract:
    spread_id: str
    display_name: str
    status: str
    formula: str
    terms: tuple[LinearTerm, ...]
    constant: Decimal
    fixed_assumption_ids: tuple[str, ...]
    join: str
    currency: str
    unit: str
    common_earliest_date: date
    latest_common_date: date
    common_observation_count: int
    common_observations_by_year: Mapping[int, int]
    seasonality_eligibility: str
    definition_evidence: str


@dataclass(frozen=True, slots=True)
class PageMetric:
    metric_type: str
    contract_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class PageRow:
    row_number: int
    purpose: str
    metrics: tuple[PageMetric, ...]


@dataclass(frozen=True, slots=True)
class PageDefinition:
    page_id: str
    display_name: str
    rows: tuple[PageRow, ...]


@dataclass(frozen=True, slots=True)
class ContractObservation:
    business_date: date
    value: Decimal
    input_duplicate_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ThreeOilV1Catalog:
    schema_version: int
    status: str
    approved_on: date
    scope: str
    source_snapshot_sha256: str
    palm_core: PalmCoreSixCatalog
    conversion_rules: tuple[ConversionRule, ...]
    series: tuple[CanonicalSeriesContract, ...]
    derived_series: tuple[DerivedSeriesContract, ...]
    spreads: tuple[SpreadContract, ...]
    fixed_assumptions: tuple[FixedAssumption, ...]
    pages: tuple[PageDefinition, ...]
    duplicate_policy: Mapping[str, object]

    def series_by_id(self, series_id: str) -> CanonicalSeriesContract:
        for item in self.series:
            if item.series_id == series_id:
                return item
        raise KeyError(f"unknown canonical series_id: {series_id}")

    def derived_series_by_id(self, series_id: str) -> DerivedSeriesContract:
        for item in self.derived_series:
            if item.series_id == series_id:
                return item
        raise KeyError(f"unknown derived series_id: {series_id}")

    def spread_by_id(self, spread_id: str) -> SpreadContract:
        for item in self.spreads:
            if item.spread_id == spread_id:
                return item
        raise KeyError(f"unknown spread_id: {spread_id}")

    def page_by_id(self, page_id: str) -> PageDefinition:
        for item in self.pages:
            if item.page_id == page_id:
                return item
        raise KeyError(f"unknown page_id: {page_id}")

    def conversion_by_id(self, conversion_id: str) -> ConversionRule:
        for item in self.conversion_rules:
            if item.conversion_id == conversion_id:
                return item
        raise KeyError(f"unknown conversion_id: {conversion_id}")


def default_three_oil_manifest_path() -> Path:
    return Path(__file__).resolve().parents[3] / "02_configs" / "international_three_oil_v1.sealed.json"


def load_three_oil_v1(path: str | Path | None = None) -> ThreeOilV1Catalog:
    """Load the extension manifest and compose it with the immutable PALM core six."""

    manifest_path = default_three_oil_manifest_path() if path is None else Path(path)
    try:
        root = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CanonicalSpreadError(
            f"three-oil sealed manifest cannot be read: {type(exc).__name__}"
        ) from None
    root = _object(root, "manifest")
    _exact_keys(
        root,
        {
            "schema_version", "status", "approved_on", "scope", "base_manifest",
            "source_snapshot_sha256", "duplicate_policy", "seasonality_policy",
            "conversion_rules", "series", "derived_series", "fixed_assumptions",
            "spreads", "pages",
        },
        "manifest",
    )
    if root["schema_version"] != 1 or root["status"] != THREE_OIL_V1_STATUS:
        raise CanonicalSpreadError("three-oil manifest identity is invalid")
    base_path = manifest_path.parent / _text(root["base_manifest"], "base_manifest")
    palm = load_palm_core_six(base_path)
    if root["source_snapshot_sha256"] != palm.source_snapshot_sha256:
        raise CanonicalSpreadError("three-oil and PALM manifests use different snapshots")
    _validate_duplicate_policy(root["duplicate_policy"])
    _validate_seasonality_policy(root["seasonality_policy"])

    conversions = tuple(_conversion(item) for item in _list(root["conversion_rules"], "conversion_rules"))
    _unique((item.conversion_id for item in conversions), "conversion_id")
    _validate_approved_conversions(conversions)
    conversion_ids = {item.conversion_id for item in conversions}
    extra_series = tuple(_series(item, conversion_ids) for item in _list(root["series"], "series"))
    base_series = tuple(_base_series(item) for item in palm.series)
    series = base_series + extra_series
    _unique((item.series_id for item in series), "canonical series_id")
    _unique((item.provider_series_id for item in series), "provider_series_id")
    if len(series) != _EXPECTED_COUNTS["series"]:
        raise CanonicalSpreadError("three-oil canonical series count is invalid")

    derived = tuple(_derived(item, conversion_ids) for item in _list(root["derived_series"], "derived_series"))
    _unique((item.series_id for item in derived), "derived series_id")
    if len(derived) != _EXPECTED_COUNTS["derived_series"]:
        raise CanonicalSpreadError("three-oil derived series count is invalid")
    source_series_ids = {item.series_id for item in series}
    for item in derived:
        if not {term.series_id for term in item.terms} <= source_series_ids:
            raise CanonicalSpreadError("derived series references an unknown source series")
    all_series_ids = {item.series_id for item in series} | {item.series_id for item in derived}
    if len(all_series_ids) != len(series) + len(derived):
        raise CanonicalSpreadError("source and derived series identities overlap")

    assumptions = tuple(_assumption(item) for item in _list(root["fixed_assumptions"], "fixed_assumptions"))
    _unique((item.assumption_id for item in assumptions), "assumption_id")
    assumption_ids = {item.assumption_id for item in assumptions}
    new_spreads = tuple(
        _spread(item, all_series_ids, assumption_ids)
        for item in _list(root["spreads"], "spreads")
    )
    base_spreads = tuple(_base_spread(item) for item in palm.spreads)
    spreads = base_spreads + new_spreads
    _unique((item.spread_id for item in spreads), "spread_id")
    if len(spreads) != _EXPECTED_COUNTS["spreads"]:
        raise CanonicalSpreadError("three-oil spread count is invalid")
    _validate_approved_formulas(derived, assumptions, spreads)

    pages = tuple(_page(item) for item in _list(root["pages"], "pages"))
    _validate_pages(pages, all_series_ids, {item.spread_id for item in spreads})
    return ThreeOilV1Catalog(
        schema_version=1,
        status=THREE_OIL_V1_STATUS,
        approved_on=_iso_date(root["approved_on"], "approved_on"),
        scope=_text(root["scope"], "scope"),
        source_snapshot_sha256=palm.source_snapshot_sha256,
        palm_core=palm,
        conversion_rules=conversions,
        series=series,
        derived_series=derived,
        spreads=spreads,
        fixed_assumptions=assumptions,
        pages=pages,
        duplicate_policy=MappingProxyType(dict(root["duplicate_policy"])),
    )


def resolve_series_observations(
    catalog: ThreeOilV1Catalog,
    series_id: str,
    records_by_series_id: Mapping[str, Iterable[Mapping[str, object]]],
) -> tuple[ContractObservation, ...]:
    """Resolve a source or derived Series with canonical conversions applied once."""

    return _resolve_series(catalog, series_id, records_by_series_id, frozenset())


def calculate_spread_observations(
    catalog: ThreeOilV1Catalog,
    spread_id: str,
    records_by_series_id: Mapping[str, Iterable[Mapping[str, object]]],
) -> tuple[ContractObservation, ...]:
    definition = catalog.spread_by_id(spread_id)
    return _calculate_linear(
        catalog,
        definition.terms,
        definition.constant,
        records_by_series_id,
        frozenset(),
    )


def _resolve_series(
    catalog: ThreeOilV1Catalog,
    series_id: str,
    records_by_series_id: Mapping[str, Iterable[Mapping[str, object]]],
    stack: frozenset[str],
) -> tuple[ContractObservation, ...]:
    if series_id in stack:
        raise CanonicalSpreadError("derived series dependency cycle")
    try:
        definition = catalog.series_by_id(series_id)
    except KeyError:
        derived = catalog.derived_series_by_id(series_id)
        return _calculate_linear(
            catalog,
            derived.terms,
            derived.constant,
            records_by_series_id,
            stack | {series_id},
        )
    try:
        records = records_by_series_id[series_id]
    except KeyError:
        raise CanonicalSpreadError(f"missing source records for {series_id}") from None
    collapsed = collapse_source_native_duplicates(
        records, provider_series_id=definition.provider_series_id
    )
    conversion = (
        None
        if definition.conversion_id is None
        else catalog.conversion_by_id(definition.conversion_id)
    )
    return tuple(
        ContractObservation(
            business_date=item.business_date,
            value=_apply_conversion(item.price, conversion),
            input_duplicate_counts=MappingProxyType(
                {series_id: item.duplicate_count}
            ),
        )
        for item in collapsed.observations
    )


def _calculate_linear(
    catalog: ThreeOilV1Catalog,
    terms: tuple[LinearTerm, ...],
    constant: Decimal,
    records_by_series_id: Mapping[str, Iterable[Mapping[str, object]]],
    stack: frozenset[str],
) -> tuple[ContractObservation, ...]:
    resolved = [
        {
            item.business_date: item
            for item in _resolve_series(
                catalog, term.series_id, records_by_series_id, stack
            )
        }
        for term in terms
    ]
    dates = sorted(
        set(resolved[0]).intersection(*(set(items) for items in resolved[1:]))
    )
    output: list[ContractObservation] = []
    for business_date in dates:
        value = constant
        duplicates: dict[str, int] = {}
        for term, items in zip(terms, resolved, strict=True):
            item = items[business_date]
            value += item.value * term.multiplier
            for key, count in item.input_duplicate_counts.items():
                duplicates[key] = duplicates.get(key, 0) + count
        output.append(
            ContractObservation(
                business_date=business_date,
                value=value,
                input_duplicate_counts=MappingProxyType(duplicates),
            )
        )
    return tuple(output)


def _apply_conversion(price: Decimal, rule: ConversionRule | None) -> Decimal:
    if rule is None:
        return price
    if rule.operation == "multiply":
        return price * rule.operand
    if rule.operation == "divide":
        return price / rule.operand
    raise CanonicalSpreadError("unsupported conversion operation")


def _base_series(item: Any) -> CanonicalSeriesContract:
    return CanonicalSeriesContract(
        series_id=str(item.series_id), status=item.status,
        dataset_id=str(item.dataset_id), provider_series_id=str(item.provider_series_id),
        provider=item.provider, origin_system=str(item.origin_system),
        acquisition_channel=item.acquisition_channel.value,
        source_locator=str(item.source_locator), source_native_table=item.source_native_table,
        source_native_series=item.source_native_series, product=item.product,
        product_grade=item.product_grade, country=item.country, region=item.region,
        location_native=item.location_native, quote_basis=item.quote_basis,
        tenor=item.tenor, price_type=item.price_type, currency=item.currency,
        unit=item.unit, source_unit=item.unit, conversion_id=None,
        earliest_date=item.earliest_date, latest_date=item.latest_date,
        observation_count=None, observations_by_year=MappingProxyType({}),
        same_value_duplicate_count=item.same_value_duplicate_count,
        conflicting_duplicate_date_count=item.conflicting_duplicate_date_count,
        metadata_evidence=item.metadata_evidence,
    )


def _base_spread(item: Any) -> SpreadContract:
    return SpreadContract(
        spread_id=item.spread_id, display_name=item.display_name, status=item.status,
        formula=item.formula,
        terms=(LinearTerm(str(item.leg_a.series_id), Decimal("1")), LinearTerm(str(item.leg_b.series_id), Decimal("-1"))),
        constant=Decimal("0"), fixed_assumption_ids=(), join=EXACT_INNER_JOIN,
        currency=item.leg_a.currency, unit=item.leg_a.unit,
        common_earliest_date=item.common_earliest_date,
        latest_common_date=item.latest_common_date,
        common_observation_count=item.common_observation_count,
        common_observations_by_year=item.common_observations_by_year,
        seasonality_eligibility=item.seasonality_eligibility,
        definition_evidence=PALM_CORE_SIX_SEALED,
    )


def _conversion(value: Any) -> ConversionRule:
    raw = _object(value, "conversion")
    _exact_keys(raw, {"conversion_id", "operation", "operand", "input_unit", "output_unit", "deterministic", "layer"}, "conversion")
    operation = _text(raw["operation"], "conversion.operation")
    if operation not in {"multiply", "divide"} or raw["deterministic"] is not True:
        raise CanonicalSpreadError("conversion must be deterministic multiply or divide")
    operand = _decimal(raw["operand"], "conversion.operand")
    if operand <= 0:
        raise CanonicalSpreadError("conversion operand must be positive")
    return ConversionRule(
        conversion_id=_valid_id(raw["conversion_id"], "conversion_id"),
        operation=operation, operand=operand,
        input_unit=_text(raw["input_unit"], "conversion.input_unit"),
        output_unit=_text(raw["output_unit"], "conversion.output_unit"),
        deterministic=True, layer=_text(raw["layer"], "conversion.layer"),
    )


def _series(value: Any, conversion_ids: set[str]) -> CanonicalSeriesContract:
    raw = _object(value, "series")
    expected = set(CanonicalSeriesContract.__dataclass_fields__)
    _exact_keys(raw, expected, "series")
    conversion_id = raw["conversion_id"]
    if conversion_id is not None and conversion_id not in conversion_ids:
        raise CanonicalSpreadError("series references an unknown conversion")
    years = _coverage(raw["observations_by_year"])
    item = CanonicalSeriesContract(
        series_id=_valid_id(raw["series_id"], "series_id"),
        status=_text(raw["status"], "series.status"),
        dataset_id=_text(raw["dataset_id"], "dataset_id"),
        provider_series_id=_text(raw["provider_series_id"], "provider_series_id"),
        provider=_text(raw["provider"], "provider"),
        origin_system=_text(raw["origin_system"], "origin_system"),
        acquisition_channel=_text(raw["acquisition_channel"], "acquisition_channel"),
        source_locator=_text(raw["source_locator"], "source_locator"),
        source_native_table=_text(raw["source_native_table"], "source_native_table"),
        source_native_series=_text(raw["source_native_series"], "source_native_series"),
        product=_text(raw["product"], "product"),
        product_grade=_text(raw["product_grade"], "product_grade"),
        country=_text(raw["country"], "country"), region=_text(raw["region"], "region"),
        location_native=_text(raw["location_native"], "location_native"),
        quote_basis=_text(raw["quote_basis"], "quote_basis"),
        tenor=_text(raw["tenor"], "tenor"), price_type=_text(raw["price_type"], "price_type"),
        currency=_text(raw["currency"], "currency"), unit=_text(raw["unit"], "unit"),
        source_unit=_text(raw["source_unit"], "source_unit"), conversion_id=conversion_id,
        earliest_date=_iso_date(raw["earliest_date"], "earliest_date"),
        latest_date=_iso_date(raw["latest_date"], "latest_date"),
        observation_count=_positive_int(raw["observation_count"], "observation_count"),
        observations_by_year=years,
        same_value_duplicate_count=_nonnegative_int(raw["same_value_duplicate_count"], "same_value_duplicate_count"),
        conflicting_duplicate_date_count=_nonnegative_int(raw["conflicting_duplicate_date_count"], "conflicting_duplicate_date_count"),
        metadata_evidence=_text(raw["metadata_evidence"], "metadata_evidence"),
    )
    if item.status != READY_STATUS or item.conflicting_duplicate_date_count:
        raise CanonicalSpreadError("extension series is not READY")
    if item.earliest_date > item.latest_date:
        raise CanonicalSpreadError("series coverage is invalid")
    return item


def _derived(value: Any, conversion_ids: set[str]) -> DerivedSeriesContract:
    raw = _object(value, "derived_series")
    _exact_keys(raw, set(DerivedSeriesContract.__dataclass_fields__), "derived_series")
    referenced = tuple(raw["conversion_ids"])
    if not referenced or not set(referenced) <= conversion_ids:
        raise CanonicalSpreadError("derived series conversion references are invalid")
    return DerivedSeriesContract(
        series_id=_valid_id(raw["series_id"], "derived.series_id"),
        display_name=_text(raw["display_name"], "derived.display_name"),
        status=_ready(raw["status"]), formula=_text(raw["formula"], "derived.formula"),
        terms=_terms(raw["terms"]), constant=_decimal(raw["constant"], "derived.constant"),
        conversion_ids=referenced, join=_join(raw["join"]),
        currency=_text(raw["currency"], "derived.currency"), unit=_text(raw["unit"], "derived.unit"),
        earliest_date=_iso_date(raw["earliest_date"], "derived.earliest_date"),
        latest_date=_iso_date(raw["latest_date"], "derived.latest_date"),
        observation_count=_positive_int(raw["observation_count"], "derived.observation_count"),
        observations_by_year=_coverage(raw["observations_by_year"]),
        definition_evidence=_text(raw["definition_evidence"], "derived.definition_evidence"),
    )


def _assumption(value: Any) -> FixedAssumption:
    raw = _object(value, "fixed_assumption")
    _exact_keys(raw, set(FixedAssumption.__dataclass_fields__), "fixed_assumption")
    return FixedAssumption(
        assumption_id=_valid_id(raw["assumption_id"], "assumption_id"),
        value=_decimal(raw["value"], "assumption.value"),
        currency=_text(raw["currency"], "assumption.currency"),
        unit=_text(raw["unit"], "assumption.unit"),
        purpose=_text(raw["purpose"], "assumption.purpose"),
        evidence=_text(raw["evidence"], "assumption.evidence"),
    )


def _spread(value: Any, series_ids: set[str], assumption_ids: set[str]) -> SpreadContract:
    raw = _object(value, "spread")
    _exact_keys(raw, set(SpreadContract.__dataclass_fields__), "spread")
    terms = _terms(raw["terms"])
    if not {item.series_id for item in terms} <= series_ids:
        raise CanonicalSpreadError("spread references an unknown series")
    fixed = tuple(raw["fixed_assumption_ids"])
    if not set(fixed) <= assumption_ids:
        raise CanonicalSpreadError("spread references an unknown fixed assumption")
    earliest = _iso_date(raw["common_earliest_date"], "common_earliest_date")
    latest = _iso_date(raw["latest_common_date"], "latest_common_date")
    if earliest > latest:
        raise CanonicalSpreadError("spread coverage is invalid")
    return SpreadContract(
        spread_id=_valid_id(raw["spread_id"], "spread_id"),
        display_name=_text(raw["display_name"], "spread.display_name"),
        status=_ready(raw["status"]), formula=_text(raw["formula"], "spread.formula"),
        terms=terms, constant=_decimal(raw["constant"], "spread.constant"),
        fixed_assumption_ids=fixed, join=_join(raw["join"]),
        currency=_text(raw["currency"], "spread.currency"), unit=_text(raw["unit"], "spread.unit"),
        common_earliest_date=earliest, latest_common_date=latest,
        common_observation_count=_positive_int(raw["common_observation_count"], "common_observation_count"),
        common_observations_by_year=_coverage(raw["common_observations_by_year"]),
        seasonality_eligibility=_full(raw["seasonality_eligibility"]),
        definition_evidence=_text(raw["definition_evidence"], "definition_evidence"),
    )


def _page(value: Any) -> PageDefinition:
    raw = _object(value, "page")
    _exact_keys(raw, {"page_id", "display_name", "rows"}, "page")
    rows = []
    for item in _list(raw["rows"], "page.rows"):
        row = _object(item, "page.row")
        _exact_keys(row, {"row_number", "purpose", "metrics"}, "page.row")
        metrics = []
        for candidate in _list(row["metrics"], "page.metrics"):
            metric = _object(candidate, "page.metric")
            _exact_keys(metric, {"metric_type", "contract_id", "display_name"}, "page.metric")
            metric_type = _text(metric["metric_type"], "metric_type")
            if metric_type not in {"series", "spread"}:
                raise CanonicalSpreadError("page metric_type is invalid")
            metrics.append(PageMetric(metric_type, _valid_id(metric["contract_id"], "contract_id"), _text(metric["display_name"], "metric.display_name")))
        rows.append(PageRow(_positive_int(row["row_number"], "row_number"), _text(row["purpose"], "row.purpose"), tuple(metrics)))
    return PageDefinition(_valid_id(raw["page_id"], "page_id"), _text(raw["display_name"], "page.display_name"), tuple(rows))


def _validate_pages(pages: tuple[PageDefinition, ...], series_ids: set[str], spread_ids: set[str]) -> None:
    if {item.page_id for item in pages} != set(_EXPECTED_PAGE_TOPOLOGY):
        raise CanonicalSpreadError("page identities are invalid")
    for page in pages:
        if tuple(row.row_number for row in page.rows) != tuple(range(1, len(page.rows) + 1)):
            raise CanonicalSpreadError("page row order must be explicit and contiguous")
        actual = tuple(tuple(metric.contract_id for metric in row.metrics) for row in page.rows)
        if actual != _EXPECTED_PAGE_TOPOLOGY[page.page_id]:
            raise CanonicalSpreadError(f"page row topology drifted: {page.page_id}")
        for row in page.rows:
            for metric in row.metrics:
                valid = metric.contract_id in (spread_ids if metric.metric_type == "spread" else series_ids)
                if not valid:
                    raise CanonicalSpreadError("page references an unknown contract")


def _validate_duplicate_policy(value: Any) -> None:
    raw = _object(value, "duplicate_policy")
    if raw != {
        "business_key": ["provider_series_id", "business_date"],
        "identity_fields_when_applicable": ["contract", "tenor"],
        "null_price": "exclude_from_observation",
        "identical_non_null_prices": "collapse_and_record_duplicate_count",
        "conflicting_non_null_prices": "fail_closed",
        "forbidden_resolvers": ["mean", "median", "first", "last"],
    }:
        raise CanonicalSpreadError("three-oil duplicate policy is invalid")


def _validate_seasonality_policy(value: Any) -> None:
    if value != {
        "complete_years": [2021, 2022, 2023, 2024, 2025],
        "ytd_year": 2026,
        "join": EXACT_INNER_JOIN,
        "missing_dates": "remain_missing",
    }:
        raise CanonicalSpreadError("three-oil seasonality policy is invalid")


def _validate_approved_formulas(
    derived: tuple[DerivedSeriesContract, ...],
    assumptions: tuple[FixedAssumption, ...],
    spreads: tuple[SpreadContract, ...],
) -> None:
    derived_by_id = {item.series_id: item for item in derived}
    spread_by_id = {item.spread_id: item for item in spreads}
    assumption_by_id = {item.assumption_id: item for item in assumptions}

    board = derived_by_id[
        "market.derived.soybean_oil.cbot.continuous_1.usd_per_metric_tonne"
    ]
    if board.terms != (
        LinearTerm(
            "market.futures.soybean_oil.cbot.continuous_1", Decimal("22.0462")
        ),
    ):
        raise CanonicalSpreadError("CBOT board conversion formula drifted")

    us_flat = derived_by_id["market.derived.soybean_oil.us.central_illinois.spot"]
    if us_flat.terms != (
        LinearTerm(
            "market.basis.soybean_oil.us.central_illinois.spot",
            Decimal("22.0462"),
        ),
        LinearTerm(
            "market.futures.soybean_oil.cbot.continuous_1", Decimal("22.0462")
        ),
    ):
        raise CanonicalSpreadError("US soybean-oil flat-price formula drifted")

    boho = spread_by_id["spread.energy.boho.us_soy_ice_diesel"]
    if boho.terms != (
        LinearTerm("market.derived.soybean_oil.us.central_illinois.spot", Decimal("1")),
        LinearTerm("market.energy.diesel.ice.continuous_1", Decimal("-1")),
    ):
        raise CanonicalSpreadError("BOHO formula drifted")

    freight = assumption_by_id["assumption.freight.cbot_board_to_argentina"]
    board_argentina = spread_by_id[
        "spread.soybean_oil.cbot_board_argentina.freight_30"
    ]
    if (
        freight.value != Decimal("30")
        or freight.currency != "USD"
        or freight.unit != "metric_tonne"
        or board_argentina.constant != -freight.value
        or board_argentina.fixed_assumption_ids != (freight.assumption_id,)
    ):
        raise CanonicalSpreadError("30 USD/T freight assumption drifted")


def _validate_approved_conversions(
    conversions: tuple[ConversionRule, ...],
) -> None:
    expected = {
        "conversion.us_cents_per_lb_to_usd_per_metric_tonne": (
            "multiply",
            Decimal("22.0462"),
            "US_cents_per_lb",
            "metric_tonne",
            "canonical_contract",
        ),
        "conversion.soybean_oil_basis_raw_x100_to_us_cents_per_lb": (
            "divide",
            Decimal("100"),
            "raw_basis_x100",
            "US_cents_per_lb",
            "series_mapping",
        ),
        "conversion.rin_d4_raw_x100_to_usd_per_gallon": (
            "divide",
            Decimal("100"),
            "raw_credit_x100",
            "gallon",
            "series_mapping",
        ),
    }
    actual = {
        item.conversion_id: (
            item.operation,
            item.operand,
            item.input_unit,
            item.output_unit,
            item.layer,
        )
        for item in conversions
    }
    if actual != expected:
        raise CanonicalSpreadError("approved conversion contracts drifted")


def _coverage(value: Any) -> Mapping[int, int]:
    raw = _object(value, "coverage")
    years = (2021, 2022, 2023, 2024, 2025, 2026)
    if set(raw) != {str(year) for year in years}:
        raise CanonicalSpreadError("coverage years are incomplete")
    return MappingProxyType({year: _positive_int(raw[str(year)], f"coverage.{year}") for year in years})


def _terms(value: Any) -> tuple[LinearTerm, ...]:
    output = []
    for item in _list(value, "terms"):
        raw = _object(item, "term")
        _exact_keys(raw, {"series_id", "multiplier"}, "term")
        output.append(LinearTerm(_valid_id(raw["series_id"], "term.series_id"), _decimal(raw["multiplier"], "term.multiplier")))
    if not output:
        raise CanonicalSpreadError("linear contract requires terms")
    return tuple(output)


def _join(value: Any) -> str:
    if value != EXACT_INNER_JOIN:
        raise CanonicalSpreadError("only exact business-date inner join is allowed")
    return value


def _ready(value: Any) -> str:
    if value != READY_STATUS:
        raise CanonicalSpreadError("contract is not READY")
    return value


def _full(value: Any) -> str:
    if value != FULL_SEASONALITY:
        raise CanonicalSpreadError("seasonality is not FULL")
    return value


def _valid_id(value: Any, label: str) -> str:
    text = _text(value, label)
    if _ID.fullmatch(text) is None:
        raise CanonicalSpreadError(f"{label} is invalid")
    return text


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CanonicalSpreadError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CanonicalSpreadError(f"{label} must be a list")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise CanonicalSpreadError(f"{label} fields are invalid")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CanonicalSpreadError(f"{label} must be non-empty text")
    return value.strip()


def _iso_date(value: Any, label: str) -> date:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise CanonicalSpreadError(f"{label} must be an ISO date")


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception:
        raise CanonicalSpreadError(f"{label} must be a decimal") from None
    if isinstance(value, bool) or not result.is_finite():
        raise CanonicalSpreadError(f"{label} must be a finite decimal")
    return result


def _nonnegative_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise CanonicalSpreadError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: Any, label: str) -> int:
    result = _nonnegative_int(value, label)
    if not result:
        raise CanonicalSpreadError(f"{label} must be positive")
    return result


def _unique(values: Iterable[str], label: str) -> None:
    items = tuple(values)
    if len(items) != len(set(items)):
        raise CanonicalSpreadError(f"{label} values must be unique")
