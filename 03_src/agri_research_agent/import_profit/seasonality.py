"""Read-only seasonal data organization for soybean import-profit metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

import pandas as pd

from .historical_cnf_adapter import shipment_year_for
from .query import (
    QueryMetric,
    SoybeanQueryDataset,
    UnknownMetricError,
)


REFERENCE_SHIPMENT_YEAR = 2000
PRIOR_SEASON_COUNT = 5
MINIMUM_MEAN_SAMPLES = 3


class SeasonalityError(ValueError):
    pass


class SeasonalityPointStatus(StrEnum):
    AVAILABLE = "available"
    MISSING_BUSINESS_DATE = "missing_business_date"
    MISSING_VALUE = "missing_value"
    AFTER_AS_OF_DATE = "after_as_of_date"


@dataclass(frozen=True, slots=True)
class SeasonalityPoint:
    season_year: int
    business_date: date
    axis_date: date
    value: float | None
    status: SeasonalityPointStatus
    missing_reasons: tuple[str, ...]
    shipment_period: str


@dataclass(frozen=True, slots=True)
class SeasonalitySeries:
    season_year: int
    window_start: date
    window_end: date
    points: tuple[SeasonalityPoint, ...]


@dataclass(frozen=True, slots=True)
class SeasonalityMeanPoint:
    axis_date: date
    value: float | None
    valid_sample_count: int
    contributing_season_years: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SeasonalityDataset:
    metric: QueryMetric
    origin: str
    shipment_month: int
    current_shipment_year: int
    window_definition: str
    reference_window: tuple[date, date]
    season_years: tuple[int, ...]
    series: tuple[SeasonalitySeries, ...]
    five_year_mean: tuple[SeasonalityMeanPoint, ...]
    connect_gaps: bool = False

    def to_dataframe(self) -> pd.DataFrame:
        rows = [
            {
                "season_year": series.season_year,
                "business_date": point.business_date,
                "axis_date": point.axis_date,
                "value": point.value,
                "status": point.status.value,
                "shipment_period": point.shipment_period,
            }
            for series in self.series
            for point in series.points
        ]
        return pd.DataFrame(rows)


def seasonal_window(
    shipment_year: int, shipment_month: int
) -> tuple[date, date]:
    if (
        isinstance(shipment_year, bool)
        or not isinstance(shipment_year, int)
        or not 1 <= shipment_year <= 9999
    ):
        raise SeasonalityError("shipment_year must be a valid integer year")
    if (
        isinstance(shipment_month, bool)
        or not isinstance(shipment_month, int)
        or not 1 <= shipment_month <= 12
    ):
        raise SeasonalityError("shipment_month must be between 1 and 12")
    shipment_start = date(shipment_year, shipment_month, 1)
    return (
        _shift_month_start(shipment_start, -4),
        shipment_start - timedelta(days=1),
    )


def build_shipment_month_seasonality(
    dataset: SoybeanQueryDataset | object,
    *,
    origin: str,
    as_of_date: date,
    metric: QueryMetric | str,
    shipment_month: int,
) -> SeasonalityDataset:
    if not callable(getattr(dataset, "require_origin", None)) or not callable(
        getattr(dataset, "get", None)
    ):
        raise SeasonalityError("dataset must implement the seasonality provider contract")
    dataset.require_origin(origin)
    if type(as_of_date) is not date:
        raise SeasonalityError("as_of_date must be a real date")
    try:
        selected_metric = (
            metric if isinstance(metric, QueryMetric) else QueryMetric(metric)
        )
    except (TypeError, ValueError) as exc:
        raise UnknownMetricError(
            f"unknown query metric: {metric!r}"
        ) from exc
    if (
        isinstance(shipment_month, bool)
        or not isinstance(shipment_month, int)
        or not 1 <= shipment_month <= 12
    ):
        raise SeasonalityError("shipment_month must be between 1 and 12")

    current_year = shipment_year_for(as_of_date, shipment_month)
    season_years = tuple(
        current_year - offset for offset in range(PRIOR_SEASON_COUNT + 1)
    )
    reference_window = seasonal_window(
        REFERENCE_SHIPMENT_YEAR, shipment_month
    )
    series = tuple(
        _build_series(
            dataset,
            origin=origin,
            as_of_date=as_of_date,
            metric=selected_metric,
            shipment_month=shipment_month,
            season_year=season_year,
            current_season=season_year == current_year,
            reference_window=reference_window,
        )
        for season_year in season_years
    )
    five_year_mean = _build_five_year_mean(series[1:])
    return SeasonalityDataset(
        metric=selected_metric,
        origin=origin,
        shipment_month=shipment_month,
        current_shipment_year=current_year,
        window_definition=(
            "shipment_month_minus_4_month_start_to_"
            "shipment_month_minus_1_month_end"
        ),
        reference_window=reference_window,
        season_years=season_years,
        series=series,
        five_year_mean=five_year_mean,
        connect_gaps=False,
    )


def build_all_shipment_month_seasonality(
    dataset: SoybeanQueryDataset | object,
    *,
    origin: str,
    as_of_date: date,
    metric: QueryMetric | str,
) -> tuple[SeasonalityDataset, ...]:
    return tuple(
        build_shipment_month_seasonality(
            dataset,
            origin=origin,
            as_of_date=as_of_date,
            metric=metric,
            shipment_month=month,
        )
        for month in range(1, 13)
    )


def _build_series(
    dataset: SoybeanQueryDataset,
    *,
    origin: str,
    as_of_date: date,
    metric: QueryMetric,
    shipment_month: int,
    season_year: int,
    current_season: bool,
    reference_window: tuple[date, date],
) -> SeasonalitySeries:
    window_start, window_end = seasonal_window(season_year, shipment_month)
    points = []
    for business_date in _weekdays(window_start, window_end):
        axis_date = _axis_date(
            business_date,
            season_year=season_year,
            reference_shipment_year=REFERENCE_SHIPMENT_YEAR,
        )
        record = dataset.get(
            business_date=business_date,
            origin=origin,
            shipment_year=season_year,
            shipment_month=shipment_month,
        )
        if current_season and business_date > as_of_date:
            value = None
            status = SeasonalityPointStatus.AFTER_AS_OF_DATE
            reasons = (status.value,)
        elif record is None:
            value = None
            status = SeasonalityPointStatus.MISSING_BUSINESS_DATE
            reasons = (status.value,)
        else:
            value = record.metric_value(metric)
            status = (
                SeasonalityPointStatus.AVAILABLE
                if value is not None
                else SeasonalityPointStatus.MISSING_VALUE
            )
            reasons = record.missing_reasons
        points.append(
            SeasonalityPoint(
                season_year=season_year,
                business_date=business_date,
                axis_date=axis_date,
                value=value,
                status=status,
                missing_reasons=reasons,
                shipment_period=f"{season_year:04d}-{shipment_month:02d}",
            )
        )
    if any(
        point.axis_date < reference_window[0]
        or point.axis_date > reference_window[1]
        for point in points
    ):
        raise SeasonalityError("season points fall outside the reference window")
    return SeasonalitySeries(
        season_year=season_year,
        window_start=window_start,
        window_end=window_end,
        points=tuple(points),
    )


def _build_five_year_mean(
    historical_series: tuple[SeasonalitySeries, ...],
) -> tuple[SeasonalityMeanPoint, ...]:
    if len(historical_series) != PRIOR_SEASON_COUNT:
        raise SeasonalityError("five-year mean requires exactly five prior seasons")
    values: dict[date, list[tuple[int, float]]] = {}
    axis_dates: set[date] = set()
    for series in historical_series:
        for point in series.points:
            axis_dates.add(point.axis_date)
            if point.value is not None:
                values.setdefault(point.axis_date, []).append(
                    (series.season_year, point.value)
                )
    result = []
    for axis_date in sorted(axis_dates):
        contributions = values.get(axis_date, [])
        count = len(contributions)
        result.append(
            SeasonalityMeanPoint(
                axis_date=axis_date,
                value=(
                    sum(value for _, value in contributions) / count
                    if count >= MINIMUM_MEAN_SAMPLES
                    else None
                ),
                valid_sample_count=count,
                contributing_season_years=tuple(
                    year for year, _ in contributions
                ),
            )
        )
    return tuple(result)


def _shift_month_start(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(index, 12)
    return date(year, zero_based_month + 1, 1)


def _weekdays(start: date, end: date) -> tuple[date, ...]:
    result = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            result.append(cursor)
        cursor += timedelta(days=1)
    return tuple(result)


def _axis_date(
    business_date: date,
    *,
    season_year: int,
    reference_shipment_year: int,
) -> date:
    year_offset = business_date.year - season_year
    try:
        return date(
            reference_shipment_year + year_offset,
            business_date.month,
            business_date.day,
        )
    except ValueError as exc:
        raise SeasonalityError(
            "business date cannot be represented on the reference axis"
        ) from exc
