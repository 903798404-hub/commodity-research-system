"""Provider-neutral payload service for the sealed international-spread pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from agri_research_agent.research_data.canonical_spreads import (
    CanonicalSpreadError,
    DuplicateConflictError,
)
from agri_research_agent.research_data.three_oil_v1 import (
    ContractObservation,
    LinearTerm,
    PageDefinition,
    PageMetric,
    ThreeOilV1Catalog,
    calculate_spread_observations,
    resolve_series_observations,
)


DISPLAY_YEARS = (2021, 2022, 2023, 2024, 2025, 2026)
YTD_YEAR = 2026
OIL_PAGE_IDS = MappingProxyType(
    {
        "palm": ("page.palm.v1",),
        "soy": ("page.soy.v1.page1", "page.soy.v1.page2"),
        "rape": ("page.rape.v1.page1",),
    }
)


class MetricStatus(StrEnum):
    READY = "READY"
    NO_DATA = "NO_DATA"
    STALE = "STALE"
    CONTRACT_ERROR = "CONTRACT_ERROR"
    DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"
    REFERENCE_DATA_UNAVAILABLE = "REFERENCE_DATA_UNAVAILABLE"


class InternationalSpreadReferenceError(RuntimeError):
    """Raised when the approved read-only reference snapshot is unavailable."""


@dataclass(frozen=True, slots=True)
class SeasonalityObservation:
    business_date: date
    year: int
    year_label: str
    month_day: str
    value: Decimal


@dataclass(frozen=True, slots=True)
class MetricPayload:
    metric_type: str
    contract_id: str
    display_title: str
    display_unit: str
    row_index: int
    column_index: int
    observations: tuple[SeasonalityObservation, ...]
    available_years: tuple[int, ...]
    latest_observation_date: date | None
    expected_latest_date: date
    provider_summary: str
    formula_summary: str
    leg_summary: tuple[str, ...]
    fixed_assumptions: tuple[str, ...]
    definition_evidence: str
    status: MetricStatus
    quality_summary: str


@dataclass(frozen=True, slots=True)
class PayloadRow:
    row_index: int
    purpose: str
    metrics: tuple[MetricPayload, ...]


@dataclass(frozen=True, slots=True)
class PayloadSection:
    page_id: str
    display_name: str
    rows: tuple[PayloadRow, ...]


@dataclass(frozen=True, slots=True)
class InternationalSpreadPayload:
    oil: str
    display_years: tuple[int, ...]
    ytd_year: int
    source_summary: str
    acquisition_summary: str
    sections: tuple[PayloadSection, ...]

    @property
    def metric_count(self) -> int:
        return sum(
            len(row.metrics) for section in self.sections for row in section.rows
        )

    @property
    def as_of_date(self) -> date | None:
        dates = tuple(
            metric.latest_observation_date
            for section in self.sections
            for row in section.rows
            for metric in row.metrics
            if metric.latest_observation_date is not None
        )
        return max(dates, default=None)

    @property
    def metric_latest_dates(self) -> tuple[date, ...]:
        return tuple(
            sorted(
                {
                    metric.latest_observation_date
                    for section in self.sections
                    for row in section.rows
                    for metric in row.metrics
                    if metric.latest_observation_date is not None
                }
            )
        )


def build_international_spread_payload(
    catalog: ThreeOilV1Catalog,
    oil: str,
    records_by_series_id: Mapping[
        str, Iterable[Mapping[str, object]]
    ],
) -> InternationalSpreadPayload:
    """Build only the selected oil page from sealed row metadata and results."""

    try:
        page_ids = OIL_PAGE_IDS[oil]
    except KeyError:
        raise ValueError(f"unsupported international-spread oil: {oil}") from None
    sections = tuple(
        _build_section(catalog, catalog.page_by_id(page_id), records_by_series_id)
        for page_id in page_ids
    )
    return InternationalSpreadPayload(
        oil,
        DISPLAY_YEARS,
        YTD_YEAR,
        _catalog_source_summary(catalog),
        _catalog_acquisition_summary(catalog),
        sections,
    )


def resolve_international_spread_snapshot(
    catalog: ThreeOilV1Catalog, reference_root: str | Path
) -> Path:
    """Resolve the one sealed snapshot under an explicitly supplied read-only root."""

    roots = {
        item.source_locator.partition("#")[0] for item in catalog.series
    }
    if len(roots) != 1:
        raise CanonicalSpreadError("sealed Series do not share one snapshot")
    locator = next(iter(roots))
    if not locator.startswith("snapshot:"):
        raise CanonicalSpreadError("sealed Series snapshot locator is invalid")
    try:
        root = Path(reference_root).resolve(strict=True)
        source = (root / locator.removeprefix("snapshot:")).resolve(strict=True)
    except FileNotFoundError as exc:
        raise InternationalSpreadReferenceError(
            "approved reference snapshot is unavailable"
        ) from exc
    if not source.is_relative_to(root):
        raise CanonicalSpreadError("sealed snapshot resolves outside reference root")
    if not source.is_file():
        raise InternationalSpreadReferenceError(
            "approved reference snapshot is unavailable"
        )
    return source


def load_international_spread_reference_records(
    catalog: ThreeOilV1Catalog, reference_root: str | Path
) -> Mapping[str, tuple[Mapping[str, object], ...]]:
    """Load all sealed source Series through the provider adapter boundary."""

    from agri_research_agent.data_sources.lutou.three_oil_snapshot import (
        ThreeOilSnapshotError,
        load_three_oil_snapshot_records,
    )

    source = resolve_international_spread_snapshot(catalog, reference_root)
    try:
        return load_three_oil_snapshot_records(catalog, source)
    except ThreeOilSnapshotError as exc:
        raise InternationalSpreadReferenceError(
            "approved reference snapshot cannot be consumed"
        ) from exc


def _build_section(
    catalog: ThreeOilV1Catalog,
    page: PageDefinition,
    records_by_series_id: Mapping[str, Iterable[Mapping[str, object]]],
) -> PayloadSection:
    rows = tuple(
        PayloadRow(
            row.row_number,
            row.purpose,
            tuple(
                _build_metric(
                    catalog,
                    metric,
                    row.row_number,
                    column_index,
                    records_by_series_id,
                )
                for column_index, metric in enumerate(row.metrics, start=1)
            ),
        )
        for row in page.rows
    )
    return PayloadSection(page.page_id, page.display_name, rows)


def _build_metric(
    catalog: ThreeOilV1Catalog,
    metric: PageMetric,
    row_index: int,
    column_index: int,
    records_by_series_id: Mapping[str, Iterable[Mapping[str, object]]],
) -> MetricPayload:
    expected_latest, unit, formula, terms, evidence, assumptions = _metadata(
        catalog, metric
    )
    providers = _providers(catalog, terms)
    try:
        observations = _resolve_metric(catalog, metric, records_by_series_id)
    except DuplicateConflictError:
        return _unavailable_metric(
            metric,
            row_index,
            column_index,
            expected_latest,
            unit,
            formula,
            terms,
            providers,
            assumptions,
            evidence,
            MetricStatus.DUPLICATE_CONFLICT,
            "同一业务日期存在冲突价格，已按合同停止展示。",
            catalog,
        )
    except KeyError:
        return _unavailable_metric(
            metric,
            row_index,
            column_index,
            expected_latest,
            unit,
            formula,
            terms,
            providers,
            assumptions,
            evidence,
            MetricStatus.REFERENCE_DATA_UNAVAILABLE,
            "只读参考数据缺少该指标所需 Series。",
            catalog,
        )
    except CanonicalSpreadError:
        return _unavailable_metric(
            metric,
            row_index,
            column_index,
            expected_latest,
            unit,
            formula,
            terms,
            providers,
            assumptions,
            evidence,
            MetricStatus.CONTRACT_ERROR,
            "该指标未通过 sealed contract 计算。",
            catalog,
        )

    selected = tuple(
        SeasonalityObservation(
            item.business_date,
            item.business_date.year,
            _year_label(item.business_date.year),
            item.business_date.strftime("%m-%d"),
            item.value,
        )
        for item in observations
        if item.business_date.year in DISPLAY_YEARS
    )
    if not observations:
        status = MetricStatus.NO_DATA
        quality = "没有可展示的 exact-date observation。"
        latest = None
    else:
        latest = max(item.business_date for item in observations)
        status = (
            MetricStatus.STALE if latest < expected_latest else MetricStatus.READY
        )
        duplicate_count = sum(
            sum(item.input_duplicate_counts.values()) for item in observations
        )
        quality = (
            f"exact business-date；相同值重复记录已折叠 {duplicate_count} 条。"
            if duplicate_count
            else "exact business-date；未发现冲突重复值。"
        )
    return MetricPayload(
        metric.metric_type,
        metric.contract_id,
        metric.display_name,
        unit,
        row_index,
        column_index,
        selected,
        tuple(sorted({item.year for item in selected})),
        latest,
        expected_latest,
        _provider_summary(providers),
        formula,
        tuple(_term_label(catalog, item) for item in terms),
        assumptions,
        evidence,
        status,
        quality,
    )


def _resolve_metric(
    catalog: ThreeOilV1Catalog,
    metric: PageMetric,
    records_by_series_id: Mapping[str, Iterable[Mapping[str, object]]],
) -> tuple[ContractObservation, ...]:
    required = (
        (LinearTerm(metric.contract_id, Decimal("1")),)
        if metric.metric_type == "series"
        else catalog.spread_by_id(metric.contract_id).terms
    )
    missing = {
        source_id
        for term in required
        for source_id in _source_dependencies(catalog, term.series_id)
        if source_id not in records_by_series_id
    }
    if missing:
        raise KeyError(f"missing source records: {sorted(missing)}")
    if metric.metric_type == "series":
        return resolve_series_observations(
            catalog, metric.contract_id, records_by_series_id
        )
    return calculate_spread_observations(
        catalog, metric.contract_id, records_by_series_id
    )


def _source_dependencies(
    catalog: ThreeOilV1Catalog, series_id: str
) -> tuple[str, ...]:
    try:
        catalog.series_by_id(series_id)
    except KeyError:
        derived = catalog.derived_series_by_id(series_id)
        return tuple(
            source_id
            for term in derived.terms
            for source_id in _source_dependencies(catalog, term.series_id)
        )
    return (series_id,)


def _catalog_source_summary(catalog: ThreeOilV1Catalog) -> str:
    providers = {item.provider for item in catalog.series}
    preferred = tuple(
        provider for provider in ("Reuters", "Oil World") if provider in providers
    )
    remaining = tuple(sorted(providers - set(preferred)))
    return " / ".join((*preferred, *remaining))


def _catalog_acquisition_summary(catalog: ThreeOilV1Catalog) -> str:
    channels = {item.acquisition_channel for item in catalog.series}
    labels = {
        "manual_snapshot": "人工快照",
        "direct_database": "数据库直连",
    }
    return " / ".join(labels.get(item, item) for item in sorted(channels))


def _metadata(
    catalog: ThreeOilV1Catalog,
    metric: PageMetric,
) -> tuple[
    date,
    str,
    str,
    tuple[LinearTerm, ...],
    str,
    tuple[str, ...],
]:
    if metric.metric_type == "series":
        series = catalog.series_by_id(metric.contract_id)
        return (
            series.latest_date,
            _display_unit(series.currency, series.unit),
            "原始 canonical Series（无页面层公式）",
            (LinearTerm(series.series_id, Decimal("1")),),
            series.metadata_evidence,
            (),
        )
    spread = catalog.spread_by_id(metric.contract_id)
    assumption_by_id = {
        item.assumption_id: item for item in catalog.fixed_assumptions
    }
    assumptions = tuple(
        f"{assumption_by_id[item].value} "
        f"{_display_unit(assumption_by_id[item].currency, assumption_by_id[item].unit)} "
        "fixed freight assumption"
        for item in spread.fixed_assumption_ids
    )
    return (
        spread.latest_common_date,
        _display_unit(spread.currency, spread.unit),
        spread.formula,
        spread.terms,
        spread.definition_evidence,
        assumptions,
    )


def _providers(
    catalog: ThreeOilV1Catalog, terms: tuple[LinearTerm, ...]
) -> tuple[str, ...]:
    providers: set[str] = set()
    for term in terms:
        _collect_providers(catalog, term.series_id, providers)
    return tuple(sorted(providers))


def _collect_providers(
    catalog: ThreeOilV1Catalog, series_id: str, providers: set[str]
) -> None:
    try:
        providers.add(catalog.series_by_id(series_id).provider)
    except KeyError:
        derived = catalog.derived_series_by_id(series_id)
        for term in derived.terms:
            _collect_providers(catalog, term.series_id, providers)


def _provider_summary(providers: tuple[str, ...]) -> str:
    if providers == ("Oil World",):
        return "Oil World fallback"
    if "Oil World" in providers:
        others = " + ".join(item for item in providers if item != "Oil World")
        return f"{others} + Oil World fallback"
    return " + ".join(providers)


def _term_label(catalog: ThreeOilV1Catalog, term: LinearTerm) -> str:
    sign = "+" if term.multiplier >= 0 else "−"
    try:
        item = catalog.series_by_id(term.series_id)
        details = [item.provider, item.location_native, item.product]
        details.extend(
            value
            for value in (item.quote_basis, item.tenor)
            if value != "UNKNOWN"
        )
        label = " / ".join(dict.fromkeys(details))
    except KeyError:
        label = catalog.derived_series_by_id(term.series_id).display_name
    return f"{sign} {label}"


def _display_unit(currency: str, unit: str) -> str:
    if unit == "metric_tonne":
        return f"{currency}/T"
    if unit == "US_cents_per_lb":
        return "US cents/lb"
    if unit == "gallon":
        return f"{currency}/GAL"
    return f"{currency}/{unit}"


def _year_label(year: int) -> str:
    return f"{year} YTD" if year == YTD_YEAR else str(year)


def _unavailable_metric(
    metric: PageMetric,
    row_index: int,
    column_index: int,
    expected_latest: date,
    unit: str,
    formula: str,
    terms: tuple[LinearTerm, ...],
    providers: tuple[str, ...],
    assumptions: tuple[str, ...],
    evidence: str,
    status: MetricStatus,
    quality: str,
    catalog: ThreeOilV1Catalog,
) -> MetricPayload:
    return MetricPayload(
        metric.metric_type,
        metric.contract_id,
        metric.display_name,
        unit,
        row_index,
        column_index,
        (),
        (),
        None,
        expected_latest,
        _provider_summary(providers),
        formula,
        tuple(_term_label(catalog, item) for item in terms),
        assumptions,
        evidence,
        status,
        quality,
    )
