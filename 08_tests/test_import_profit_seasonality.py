from datetime import date, timedelta

import pytest

from agri_research_agent.import_profit.query import (
    QueryMetric,
    load_soybean_query_dataset,
)
from agri_research_agent.import_profit.seasonality import (
    SeasonalityPoint,
    SeasonalityPointStatus,
    SeasonalitySeries,
    _build_five_year_mean,
    build_all_shipment_month_seasonality,
    build_shipment_month_seasonality,
    seasonal_window,
)
from test_import_profit_query import joined_rows, write_dataset


def dataset_from(tmp_path, records):
    return load_soybean_query_dataset(*write_dataset(tmp_path, records))


def business_records_for_seasons(
    shipment_month: int,
    season_years: tuple[int, ...],
    *,
    values_by_year: dict[int, float] | None = None,
    every_weekday: bool = False,
    target_month_day: tuple[int, int] = (11, 1),
):
    records = []
    for season_year in season_years:
        start, end = seasonal_window(season_year, shipment_month)
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5 and (
                every_weekday
                or (cursor.month, cursor.day) == target_month_day
            ):
                value = (
                    values_by_year[season_year]
                    if values_by_year and season_year in values_by_year
                    else float(season_year)
                )
                records.append(
                    joined_rows(
                        cursor,
                        "brazil",
                        season_year,
                        shipment_month,
                        cnf=value,
                        duty=value * 10,
                        margin=value * -1,
                    )
                )
            cursor += timedelta(days=1)
    return records


def find_point(series, business_date):
    return next(
        point for point in series.points if point.business_date == business_date
    )


def find_mean(dataset, axis_date):
    return next(
        point for point in dataset.five_year_mean
        if point.axis_date == axis_date
    )


@pytest.mark.parametrize(
    ("shipment_year", "shipment_month", "expected"),
    [
        (2027, 1, (date(2026, 9, 1), date(2026, 12, 31))),
        (2027, 3, (date(2026, 11, 1), date(2027, 2, 28))),
        (2028, 3, (date(2027, 11, 1), date(2028, 2, 29))),
    ],
)
def test_seasonal_window_handles_cross_year_month_lengths_and_leap_year(
    shipment_year, shipment_month, expected
):
    assert seasonal_window(shipment_year, shipment_month) == expected


def test_current_year_reference_axis_and_six_fixed_seasons(tmp_path):
    dataset = dataset_from(
        tmp_path,
        [joined_rows(date(2026, 6, 25), "brazil", 2026, 7)],
    )
    january = build_shipment_month_seasonality(
        dataset, origin="brazil", as_of_date=date(2026, 6, 25),
        metric="cnf", shipment_month=1,
    )
    july = build_shipment_month_seasonality(
        dataset, origin="brazil", as_of_date=date(2026, 6, 25),
        metric="cnf", shipment_month=7,
    )
    assert january.current_shipment_year == 2027
    assert january.season_years == (2027, 2026, 2025, 2024, 2023, 2022)
    assert january.reference_window == (
        date(1999, 9, 1), date(1999, 12, 31)
    )
    assert july.current_shipment_year == 2026
    assert january.connect_gaps is False


def test_leap_day_maps_to_reference_leap_day_and_nonleap_has_no_point(tmp_path):
    records = [
        joined_rows(date(2028, 2, 29), "brazil", 2028, 3),
        joined_rows(date(2027, 2, 26), "brazil", 2027, 3),
    ]
    dataset = dataset_from(tmp_path, records)
    leap = build_shipment_month_seasonality(
        dataset, origin="brazil", as_of_date=date(2028, 2, 29),
        metric="cnf", shipment_month=3,
    )
    leap_point = find_point(leap.series[0], date(2028, 2, 29))
    assert leap_point.axis_date == date(2000, 2, 29)

    nonleap = build_shipment_month_seasonality(
        dataset, origin="brazil", as_of_date=date(2027, 2, 26),
        metric="cnf", shipment_month=3,
    )
    assert all(
        not (point.business_date.month == 2 and point.business_date.day == 29)
        for point in nonleap.series[0].points
    )


def test_current_series_is_truncated_before_and_inside_window(tmp_path):
    records = business_records_for_seasons(
        1, (2027,), every_weekday=True
    )
    dataset = dataset_from(tmp_path, records)
    before = build_shipment_month_seasonality(
        dataset, origin="brazil", as_of_date=date(2026, 6, 25),
        metric="cnf", shipment_month=1,
    )
    assert all(point.value is None for point in before.series[0].points)
    assert all(
        point.status is SeasonalityPointStatus.AFTER_AS_OF_DATE
        for point in before.series[0].points
    )

    inside = build_shipment_month_seasonality(
        dataset, origin="brazil", as_of_date=date(2026, 12, 15),
        metric="cnf", shipment_month=1,
    )
    assert any(point.value is not None for point in inside.series[0].points)
    assert all(
        point.value is None
        for point in inside.series[0].points
        if point.business_date > date(2026, 12, 15)
    )


def test_as_of_after_window_shows_full_current_window(tmp_path):
    records = business_records_for_seasons(
        1, (2027,), every_weekday=True
    )
    dataset = dataset_from(tmp_path, records)
    result = build_shipment_month_seasonality(
        dataset, origin="brazil", as_of_date=date(2026, 12, 31),
        metric="cnf", shipment_month=1,
    )
    assert all(point.value is not None for point in result.series[0].points)


def test_each_series_has_complete_weekday_axis_and_explicit_null_breaks(tmp_path):
    dataset = dataset_from(
        tmp_path,
        [joined_rows(date(2026, 11, 2), "brazil", 2027, 3, cnf=None,
                     duty=None, margin=None)],
    )
    result = build_shipment_month_seasonality(
        dataset, origin="brazil", as_of_date=date(2027, 2, 26),
        metric="cnf", shipment_month=3,
    )
    current = result.series[0]
    assert current.points
    assert all(point.business_date.weekday() < 5 for point in current.points)
    assert find_point(current, date(2026, 11, 2)).value is None
    assert find_point(current, date(2026, 11, 2)).status is (
        SeasonalityPointStatus.MISSING_VALUE
    )
    assert find_point(current, date(2026, 11, 3)).status is (
        SeasonalityPointStatus.MISSING_BUSINESS_DATE
    )
    assert result.connect_gaps is False


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        (QueryMetric.CNF, 2.0),
        (QueryMetric.DUTY_PAID_COST, 20.0),
        (QueryMetric.NET_CRUSH_MARGIN, -2.0),
    ],
)
def test_three_metrics_use_persisted_values_without_recalculation(
    tmp_path, metric, expected
):
    dataset = dataset_from(
        tmp_path,
        [joined_rows(
            date(2026, 11, 2), "brazil", 2027, 3,
            cnf=2.0, duty=20.0, margin=-2.0,
        )],
    )
    result = build_shipment_month_seasonality(
        dataset, origin="brazil", as_of_date=date(2027, 2, 26),
        metric=metric, shipment_month=3,
    )
    assert find_point(result.series[0], date(2026, 11, 2)).value == expected


@pytest.mark.parametrize("metric", tuple(QueryMetric))
def test_all_metric_gaps_remain_explicit_null_points(tmp_path, metric):
    dataset = dataset_from(
        tmp_path,
        [joined_rows(
            date(2026, 11, 2), "brazil", 2027, 3,
            cnf=None, duty=None, margin=None,
        )],
    )
    result = build_shipment_month_seasonality(
        dataset, origin="brazil", as_of_date=date(2027, 2, 26),
        metric=metric, shipment_month=3,
    )
    point = find_point(result.series[0], date(2026, 11, 2))
    assert point.value is None
    assert point.status is SeasonalityPointStatus.MISSING_VALUE


def test_five_year_mean_excludes_current_and_sixth_older_season(tmp_path):
    values = {
        2027: 1000.0,
        2026: 10.0,
        2025: 20.0,
        2024: 30.0,
        2023: 40.0,
        2022: 50.0,
        2021: 9999.0,
    }
    records = business_records_for_seasons(
        3, tuple(values), values_by_year=values
    )
    dataset = dataset_from(tmp_path, records)
    result = build_shipment_month_seasonality(
        dataset, origin="brazil", as_of_date=date(2027, 2, 26),
        metric="cnf", shipment_month=3,
    )
    point = find_mean(result, date(1999, 11, 1))
    # The 2026 season's 2025-11-01 is a weekend.
    assert point.contributing_season_years == (2025, 2024, 2023, 2022)
    assert point.value == pytest.approx((20 + 30 + 40 + 50) / 4)
    assert 2027 not in point.contributing_season_years
    assert 2021 not in point.contributing_season_years


@pytest.mark.parametrize(
    ("years", "values", "expected_count", "expected_value"),
    [
        ((2026, 2024), (0.0, -2.0), 2, None),
        ((2026, 2024, 2023), (0.0, -2.0, 5.0), 3, 1.0),
        (
            (2026, 2025, 2024, 2023, 2022),
            (0.0, -2.0, 5.0, 7.0, 10.0),
            4,
            5.5,
        ),
    ],
)
def test_mean_requires_three_valid_years_and_keeps_zero_negative(
    tmp_path, years, values, expected_count, expected_value
):
    value_map = dict(zip(years, values, strict=True))
    records = business_records_for_seasons(
        3, years, values_by_year=value_map, target_month_day=(11, 3)
    )
    # Ensure the dataset exposes the requested origin even with sparse history.
    dataset = dataset_from(tmp_path, records)
    result = build_shipment_month_seasonality(
        dataset, origin="brazil", as_of_date=date(2027, 2, 26),
        metric="cnf", shipment_month=3,
    )
    point = find_mean(result, date(1999, 11, 3))
    assert point.valid_sample_count == expected_count
    if expected_value is None:
        assert point.value is None
    else:
        assert point.value == pytest.approx(expected_value)
    assert point.contributing_season_years == tuple(
        year for year in years if date(year - 1, 11, 3).weekday() < 5
    )


def test_mean_uses_all_five_values_when_five_axis_samples_exist():
    axis_date = date(1999, 11, 3)
    series = tuple(
        SeasonalitySeries(
            season_year=year,
            window_start=date(year - 1, 11, 1),
            window_end=date(year, 2, 28),
            points=(
                SeasonalityPoint(
                    season_year=year,
                    business_date=date(year - 1, 11, 3),
                    axis_date=axis_date,
                    value=float(value),
                    status=SeasonalityPointStatus.AVAILABLE,
                    missing_reasons=(),
                    shipment_period=f"{year}-03",
                ),
            ),
        )
        for year, value in zip(
            (2026, 2025, 2024, 2023, 2022),
            (0, -2, 5, 7, 10),
            strict=True,
        )
    )
    point = _build_five_year_mean(series)[0]
    assert point.valid_sample_count == 5
    assert point.value == pytest.approx(4.0)
    assert point.contributing_season_years == (2026, 2025, 2024, 2023, 2022)


def test_all_twelve_months_return_fixed_structures_and_are_deterministic(tmp_path):
    dataset = dataset_from(
        tmp_path,
        [joined_rows(date(2026, 6, 25), "brazil", 2026, 7)],
    )
    before = dataset.records
    first = build_all_shipment_month_seasonality(
        dataset, origin="brazil", as_of_date=date(2026, 6, 25),
        metric="net_crush_margin",
    )
    second = build_all_shipment_month_seasonality(
        dataset, origin="brazil", as_of_date=date(2026, 6, 25),
        metric="net_crush_margin",
    )
    assert tuple(item.shipment_month for item in first) == tuple(range(1, 13))
    assert tuple(item.current_shipment_year for item in first[:6]) == (2027,) * 6
    assert tuple(item.current_shipment_year for item in first[6:]) == (2026,) * 6
    assert all(item.series and item.five_year_mean for item in first)
    assert first == second
    assert dataset.records == before
