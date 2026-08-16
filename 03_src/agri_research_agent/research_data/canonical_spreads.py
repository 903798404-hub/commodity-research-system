"""Sealed identities and deterministic calculations for canonical physical spreads."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .identities import (
    AcquisitionChannel,
    DatasetId,
    OriginSystem,
    ProviderSeriesId,
    SeriesId,
    SourceLocator,
)


PALM_CORE_SIX_STATUS = "PALM_CORE_SIX_READY"
READY_STATUS = "READY"
EXACT_INNER_JOIN = "exact_business_date_inner_join"
DIFFERENCE_FORMULA = "price_A - price_B"
FULL_SEASONALITY = "FULL"
_SPREAD_ID = re.compile(r"^spread\.[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_SPREAD_LEGS = {
    "spread.international.soy_palm": (
        "market.physical.soybean_oil.argentina.upper_river.spot",
        "market.physical.palm_oil.malaysia.rbd_palm_oil.fob.p1",
    ),
    "spread.international.rape_palm": (
        "market.physical.rapeseed_oil.europe.spot",
        "market.physical.palm_oil.malaysia.rbd_palm_oil.fob.p1",
    ),
    "spread.international.sunflower_palm": (
        "market.physical.sunflower_oil.europe.six_ports.ex_tank.spot",
        "market.physical.palm_oil.malaysia.rbd_palm_oil.fob.p1",
    ),
    "spread.europe.soy_palm": (
        "market.physical.soybean_oil.netherlands.ex_mill_fob",
        "market.physical.palm_oil.europe_nwe.rbd_palm_oil.cif.spot",
    ),
    "spread.europe.rape_palm": (
        "market.physical.rapeseed_oil.europe.spot",
        "market.physical.palm_oil.europe_nwe.rbd_palm_oil.cif.spot",
    ),
    "spread.europe.sunflower_palm": (
        "market.physical.sunflower_oil.europe.six_ports.ex_tank.spot",
        "market.physical.palm_oil.europe_nwe.rbd_palm_oil.cif.spot",
    ),
}


class CanonicalSpreadError(ValueError):
    """Raised when a sealed manifest or price series violates its contract."""


class DuplicateConflictError(CanonicalSpreadError):
    """Raised when one source-native series has conflicting prices on one date."""


@dataclass(frozen=True, slots=True)
class DuplicatePolicy:
    grain: tuple[str, str]
    null_price: str
    identical_non_null_prices: str
    conflicting_non_null_prices: str
    forbidden_resolvers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CalculationPolicy:
    formula: str
    join: str
    interpolation: bool
    forward_fill: bool
    fx_conversion: bool
    unit_conversion: bool
    freight_adjustment: bool
    basis_conversion: bool
    quality_adjustment: bool
    currency: str
    unit: str


@dataclass(frozen=True, slots=True)
class SeasonalityPolicy:
    required_complete_years: tuple[int, ...]
    current_ytd_year: int
    eligibility_rule: str


@dataclass(frozen=True, slots=True)
class CanonicalPhysicalSeries:
    series_id: SeriesId
    status: str
    dataset_id: DatasetId
    provider_series_id: ProviderSeriesId
    provider: str
    origin_system: OriginSystem
    acquisition_channel: AcquisitionChannel
    source_locator: SourceLocator
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
    earliest_date: date
    latest_date: date
    same_value_duplicate_count: int
    conflicting_duplicate_date_count: int
    metadata_evidence: str


@dataclass(frozen=True, slots=True)
class CanonicalSpreadDefinition:
    spread_id: str
    display_name: str
    status: str
    leg_a: CanonicalPhysicalSeries
    leg_b: CanonicalPhysicalSeries
    formula: str
    common_earliest_date: date
    latest_common_date: date
    common_observation_count: int
    common_observations_by_year: Mapping[int, int]
    seasonality_eligibility: str


@dataclass(frozen=True, slots=True)
class CollapsedObservation:
    business_date: date
    price: Decimal
    duplicate_count: int


@dataclass(frozen=True, slots=True)
class ResolvedSeries:
    observations: tuple[CollapsedObservation, ...]
    duplicate_count: int


@dataclass(frozen=True, slots=True)
class SpreadObservation:
    business_date: date
    price_a: Decimal
    price_b: Decimal
    value: Decimal
    leg_a_duplicate_count: int
    leg_b_duplicate_count: int


@dataclass(frozen=True, slots=True)
class PalmCoreSixCatalog:
    schema_version: int
    status: str
    approved_on: date
    scope: str
    source_snapshot_sha256: str
    duplicate_policy: DuplicatePolicy
    calculation_policy: CalculationPolicy
    seasonality_policy: SeasonalityPolicy
    series: tuple[CanonicalPhysicalSeries, ...]
    spreads: tuple[CanonicalSpreadDefinition, ...]

    def series_by_id(self, series_id: str | SeriesId) -> CanonicalPhysicalSeries:
        key = str(series_id)
        for item in self.series:
            if str(item.series_id) == key:
                return item
        raise KeyError(f"unknown canonical series_id: {key}")

    def spread_by_id(self, spread_id: str) -> CanonicalSpreadDefinition:
        for item in self.spreads:
            if item.spread_id == spread_id:
                return item
        raise KeyError(f"unknown canonical spread_id: {spread_id}")


def default_manifest_path() -> Path:
    return Path(__file__).resolve().parents[3] / "02_configs" / "international_palm_core_six.sealed.json"


def load_palm_core_six(path: str | Path | None = None) -> PalmCoreSixCatalog:
    """Load and fail-closed validate the approved six-spread sealing manifest."""

    manifest_path = default_manifest_path() if path is None else Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CanonicalSpreadError(
            f"sealed spread manifest cannot be read: {type(exc).__name__}"
        ) from None
    return _catalog(payload)


def collapse_source_native_duplicates(
    records: Iterable[Mapping[str, object]],
    *,
    provider_series_id: str,
) -> ResolvedSeries:
    """Collapse only identical non-null values at source-series/date grain."""

    grouped: dict[date, list[Decimal]] = defaultdict(list)
    for record in records:
        if not isinstance(record, Mapping):
            raise TypeError("price records must be mappings")
        record_provider = record.get("provider_series_id")
        if record_provider is not None and str(record_provider) != provider_series_id:
            raise CanonicalSpreadError(
                "price records contain a different provider_series_id"
            )
        price = record.get("price")
        if price is None:
            continue
        business_date = _date(record.get("business_date"), "business_date")
        grouped[business_date].append(_decimal(price))

    collapsed: list[CollapsedObservation] = []
    duplicate_count = 0
    for business_date in sorted(grouped):
        prices = grouped[business_date]
        distinct = set(prices)
        if len(distinct) != 1:
            rendered = ", ".join(str(item) for item in sorted(distinct))
            raise DuplicateConflictError(
                f"conflicting non-null prices for {provider_series_id} on "
                f"{business_date.isoformat()}: {rendered}"
            )
        repeated = len(prices) - 1
        duplicate_count += repeated
        collapsed.append(
            CollapsedObservation(
                business_date=business_date,
                price=prices[0],
                duplicate_count=repeated,
            )
        )
    return ResolvedSeries(tuple(collapsed), duplicate_count)


def calculate_exact_inner_spread(
    definition: CanonicalSpreadDefinition,
    leg_a_records: Iterable[Mapping[str, object]],
    leg_b_records: Iterable[Mapping[str, object]],
) -> tuple[SpreadObservation, ...]:
    """Calculate A-B only where both legs have an exact business-date value."""

    leg_a = collapse_source_native_duplicates(
        leg_a_records, provider_series_id=str(definition.leg_a.provider_series_id)
    )
    leg_b = collapse_source_native_duplicates(
        leg_b_records, provider_series_id=str(definition.leg_b.provider_series_id)
    )
    by_a = {item.business_date: item for item in leg_a.observations}
    by_b = {item.business_date: item for item in leg_b.observations}
    return tuple(
        SpreadObservation(
            business_date=business_date,
            price_a=by_a[business_date].price,
            price_b=by_b[business_date].price,
            value=by_a[business_date].price - by_b[business_date].price,
            leg_a_duplicate_count=by_a[business_date].duplicate_count,
            leg_b_duplicate_count=by_b[business_date].duplicate_count,
        )
        for business_date in sorted(by_a.keys() & by_b.keys())
    )


def _catalog(value: Any) -> PalmCoreSixCatalog:
    root = _object(value, "manifest")
    _exact_keys(
        root,
        {
            "schema_version",
            "status",
            "approved_on",
            "scope",
            "source_snapshot_sha256",
            "duplicate_policy",
            "calculation_policy",
            "seasonality_policy",
            "series",
            "spreads",
        },
        "manifest",
    )
    if root["schema_version"] != 1 or root["status"] != PALM_CORE_SIX_STATUS:
        raise CanonicalSpreadError("sealed spread manifest identity is invalid")
    snapshot_sha = _text(root["source_snapshot_sha256"], "source_snapshot_sha256")
    if _SHA256.fullmatch(snapshot_sha) is None:
        raise CanonicalSpreadError("source_snapshot_sha256 is invalid")
    duplicate_policy = _duplicate_policy(root["duplicate_policy"])
    calculation_policy = _calculation_policy(root["calculation_policy"])
    seasonality_policy = _seasonality_policy(root["seasonality_policy"])

    raw_series = root["series"]
    if not isinstance(raw_series, list) or len(raw_series) != 6:
        raise CanonicalSpreadError("sealed manifest must contain exactly six series")
    series = tuple(_series(item) for item in raw_series)
    series_by_id = {str(item.series_id): item for item in series}
    if len(series_by_id) != 6:
        raise CanonicalSpreadError("canonical series_id values must be unique")
    expected_series_ids = {
        series_id for legs in _EXPECTED_SPREAD_LEGS.values() for series_id in legs
    }
    if set(series_by_id) != expected_series_ids:
        raise CanonicalSpreadError("core-six canonical series topology is invalid")
    provider_ids = {str(item.provider_series_id) for item in series}
    if len(provider_ids) != 6:
        raise CanonicalSpreadError("provider_series_id values must be unique")

    raw_spreads = root["spreads"]
    if not isinstance(raw_spreads, list) or len(raw_spreads) != 6:
        raise CanonicalSpreadError("sealed manifest must contain exactly six spreads")
    spreads = tuple(
        _spread(item, series_by_id, calculation_policy, seasonality_policy)
        for item in raw_spreads
    )
    if len({item.spread_id for item in spreads}) != 6:
        raise CanonicalSpreadError("spread_id values must be unique")
    actual_legs = {
        item.spread_id: (str(item.leg_a.series_id), str(item.leg_b.series_id))
        for item in spreads
    }
    if actual_legs != _EXPECTED_SPREAD_LEGS:
        raise CanonicalSpreadError("core-six spread leg topology is invalid")
    return PalmCoreSixCatalog(
        schema_version=1,
        status=PALM_CORE_SIX_STATUS,
        approved_on=_date(root["approved_on"], "approved_on"),
        scope=_text(root["scope"], "scope"),
        source_snapshot_sha256=snapshot_sha,
        duplicate_policy=duplicate_policy,
        calculation_policy=calculation_policy,
        seasonality_policy=seasonality_policy,
        series=series,
        spreads=spreads,
    )


def _duplicate_policy(value: Any) -> DuplicatePolicy:
    raw = _object(value, "duplicate_policy")
    _exact_keys(
        raw,
        {
            "grain",
            "null_price",
            "identical_non_null_prices",
            "conflicting_non_null_prices",
            "forbidden_resolvers",
        },
        "duplicate_policy",
    )
    grain = tuple(raw["grain"]) if isinstance(raw["grain"], list) else ()
    forbidden = (
        tuple(raw["forbidden_resolvers"])
        if isinstance(raw["forbidden_resolvers"], list)
        else ()
    )
    expected_forbidden = ("mean", "median", "first", "last")
    if grain != ("provider_series_id", "business_date") or forbidden != expected_forbidden:
        raise CanonicalSpreadError("duplicate policy grain or resolvers are invalid")
    policy = DuplicatePolicy(
        grain=grain,
        null_price=_text(raw["null_price"], "duplicate_policy.null_price"),
        identical_non_null_prices=_text(
            raw["identical_non_null_prices"],
            "duplicate_policy.identical_non_null_prices",
        ),
        conflicting_non_null_prices=_text(
            raw["conflicting_non_null_prices"],
            "duplicate_policy.conflicting_non_null_prices",
        ),
        forbidden_resolvers=forbidden,
    )
    if (
        policy.null_price != "exclude_from_observation"
        or policy.identical_non_null_prices
        != "collapse_and_record_duplicate_count"
        or policy.conflicting_non_null_prices != "block_series_readiness"
    ):
        raise CanonicalSpreadError("duplicate policy actions are invalid")
    return policy


def _calculation_policy(value: Any) -> CalculationPolicy:
    raw = _object(value, "calculation_policy")
    expected = {
        "formula",
        "join",
        "interpolation",
        "forward_fill",
        "fx_conversion",
        "unit_conversion",
        "freight_adjustment",
        "basis_conversion",
        "quality_adjustment",
        "currency",
        "unit",
    }
    _exact_keys(raw, expected, "calculation_policy")
    disabled = tuple(
        raw[name]
        for name in (
            "interpolation",
            "forward_fill",
            "fx_conversion",
            "unit_conversion",
            "freight_adjustment",
            "basis_conversion",
            "quality_adjustment",
        )
    )
    if any(item is not False for item in disabled):
        raise CanonicalSpreadError("all adjustment and filling policies must be disabled")
    if raw["formula"] != DIFFERENCE_FORMULA or raw["join"] != EXACT_INNER_JOIN:
        raise CanonicalSpreadError("spread calculation or join policy is invalid")
    if raw["currency"] != "USD" or raw["unit"] != "metric_tonne":
        raise CanonicalSpreadError("core-six calculation unit must be USD/metric tonne")
    return CalculationPolicy(**{key: raw[key] for key in expected})


def _seasonality_policy(value: Any) -> SeasonalityPolicy:
    raw = _object(value, "seasonality_policy")
    _exact_keys(
        raw,
        {"required_complete_years", "current_ytd_year", "eligibility_rule"},
        "seasonality_policy",
    )
    years = tuple(raw["required_complete_years"])
    if years != (2021, 2022, 2023, 2024, 2025) or raw["current_ytd_year"] != 2026:
        raise CanonicalSpreadError("seasonality coverage years are invalid")
    rule = _text(raw["eligibility_rule"], "seasonality_policy.eligibility_rule")
    if rule != "each_required_year_and_current_ytd_has_common_observations":
        raise CanonicalSpreadError("seasonality eligibility rule is invalid")
    return SeasonalityPolicy(years, 2026, rule)


def _series(value: Any) -> CanonicalPhysicalSeries:
    raw = _object(value, "series")
    expected = set(CanonicalPhysicalSeries.__dataclass_fields__)
    _exact_keys(raw, expected, "series")
    item = CanonicalPhysicalSeries(
        series_id=SeriesId(raw["series_id"]),
        status=_text(raw["status"], "series.status"),
        dataset_id=DatasetId(raw["dataset_id"]),
        provider_series_id=ProviderSeriesId(raw["provider_series_id"]),
        provider=_text(raw["provider"], "series.provider"),
        origin_system=OriginSystem(raw["origin_system"]),
        acquisition_channel=AcquisitionChannel(raw["acquisition_channel"]),
        source_locator=SourceLocator(raw["source_locator"]),
        source_native_table=_text(raw["source_native_table"], "source_native_table"),
        source_native_series=_text(raw["source_native_series"], "source_native_series"),
        product=_text(raw["product"], "series.product"),
        product_grade=_text(raw["product_grade"], "series.product_grade"),
        country=_text(raw["country"], "series.country"),
        region=_text(raw["region"], "series.region"),
        location_native=_text(raw["location_native"], "series.location_native"),
        quote_basis=_text(raw["quote_basis"], "series.quote_basis"),
        tenor=_text(raw["tenor"], "series.tenor"),
        price_type=_text(raw["price_type"], "series.price_type"),
        currency=_text(raw["currency"], "series.currency"),
        unit=_text(raw["unit"], "series.unit"),
        earliest_date=_date(raw["earliest_date"], "series.earliest_date"),
        latest_date=_date(raw["latest_date"], "series.latest_date"),
        same_value_duplicate_count=_nonnegative_int(
            raw["same_value_duplicate_count"], "same_value_duplicate_count"
        ),
        conflicting_duplicate_date_count=_nonnegative_int(
            raw["conflicting_duplicate_date_count"],
            "conflicting_duplicate_date_count",
        ),
        metadata_evidence=_text(raw["metadata_evidence"], "metadata_evidence"),
    )
    if item.status != READY_STATUS or item.conflicting_duplicate_date_count != 0:
        raise CanonicalSpreadError("a sealed canonical series is not READY")
    if item.currency != "USD" or item.unit != "metric_tonne":
        raise CanonicalSpreadError("all sealed canonical series must be USD/metric tonne")
    if item.earliest_date > item.latest_date:
        raise CanonicalSpreadError("series date coverage is invalid")
    return item


def _spread(
    value: Any,
    series_by_id: Mapping[str, CanonicalPhysicalSeries],
    calculation_policy: CalculationPolicy,
    seasonality_policy: SeasonalityPolicy,
) -> CanonicalSpreadDefinition:
    raw = _object(value, "spread")
    expected = {
        "spread_id",
        "display_name",
        "status",
        "leg_a_series_id",
        "leg_b_series_id",
        "formula",
        "common_earliest_date",
        "latest_common_date",
        "common_observation_count",
        "common_observations_by_year",
        "seasonality_eligibility",
    }
    _exact_keys(raw, expected, "spread")
    spread_id = _text(raw["spread_id"], "spread.spread_id")
    if _SPREAD_ID.fullmatch(spread_id) is None:
        raise CanonicalSpreadError("spread_id is invalid")
    try:
        leg_a = series_by_id[raw["leg_a_series_id"]]
        leg_b = series_by_id[raw["leg_b_series_id"]]
    except (KeyError, TypeError):
        raise CanonicalSpreadError("spread references an unknown canonical series") from None
    if leg_a.series_id == leg_b.series_id:
        raise CanonicalSpreadError("spread legs must differ")
    if (leg_a.currency, leg_a.unit) != (leg_b.currency, leg_b.unit):
        raise CanonicalSpreadError("spread legs must have directly comparable units")
    raw_years = _object(raw["common_observations_by_year"], "coverage years")
    expected_years = (*seasonality_policy.required_complete_years, seasonality_policy.current_ytd_year)
    if set(raw_years) != {str(year) for year in expected_years}:
        raise CanonicalSpreadError("spread coverage years are incomplete")
    years = MappingProxyType(
        {
            year: _positive_int(raw_years[str(year)], f"coverage.{year}")
            for year in expected_years
        }
    )
    earliest = _date(raw["common_earliest_date"], "common_earliest_date")
    latest = _date(raw["latest_common_date"], "latest_common_date")
    if earliest > latest or earliest < max(leg_a.earliest_date, leg_b.earliest_date):
        raise CanonicalSpreadError("spread common coverage start is invalid")
    if latest > min(leg_a.latest_date, leg_b.latest_date):
        raise CanonicalSpreadError("spread latest common date exceeds a leg")
    if (
        raw["status"] != READY_STATUS
        or raw["formula"] != calculation_policy.formula
        or raw["seasonality_eligibility"] != FULL_SEASONALITY
    ):
        raise CanonicalSpreadError("spread readiness, formula, or seasonality is invalid")
    return CanonicalSpreadDefinition(
        spread_id=spread_id,
        display_name=_text(raw["display_name"], "spread.display_name"),
        status=READY_STATUS,
        leg_a=leg_a,
        leg_b=leg_b,
        formula=DIFFERENCE_FORMULA,
        common_earliest_date=earliest,
        latest_common_date=latest,
        common_observation_count=_positive_int(
            raw["common_observation_count"], "common_observation_count"
        ),
        common_observations_by_year=years,
        seasonality_eligibility=FULL_SEASONALITY,
    )


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CanonicalSpreadError(f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise CanonicalSpreadError(f"{label} fields are invalid")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
        raise CanonicalSpreadError(f"{label} must be non-empty text")
    return value.strip()


def _date(value: Any, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise CanonicalSpreadError(f"{label} must be an ISO business date")


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise CanonicalSpreadError("price must be a finite decimal number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise CanonicalSpreadError("price must be a finite decimal number") from None
    if not result.is_finite():
        raise CanonicalSpreadError("price must be a finite decimal number")
    return result


def _nonnegative_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise CanonicalSpreadError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: Any, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result == 0:
        raise CanonicalSpreadError(f"{label} must be positive")
    return result
