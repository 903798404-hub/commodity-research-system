from datetime import date, datetime, timezone

import pytest

from agri_research_agent.import_profit.business_days import (
    NonBusinessWeekdayError,
)
from agri_research_agent.import_profit.cnf_store import CnfQuoteRecord
from agri_research_agent.import_profit.config import (
    BusinessCalendarPolicy,
    ContractLegRule,
    ContractMappingRule,
    DisplayPolicy,
    FxPolicy,
    Origin,
    SeasonalityPolicy,
    SoybeanImportProfitConfig,
    SoybeanParameters,
)
from agri_research_agent.import_profit.market_snapshot import (
    CbotPricePoint,
    DcePricePoint,
    FxPricePoint,
    SnapshotStatus,
)
from agri_research_agent.import_profit.models import (
    BusinessKey,
    CalculationStatus,
    MissingReason,
)
from agri_research_agent.import_profit.recalculation import (
    DuplicateRecalculationKeyError,
    recalculate_soybean_keys,
)


BUSINESS_DATE = date(2026, 7, 28)
MAPPING = (
    (1, 1, 0, 5, 0),
    (2, 3, 0, 5, 0),
    (3, 3, 0, 5, 0),
    (4, 5, 0, 9, 0),
    (5, 5, 0, 9, 0),
    (6, 7, 0, 9, 0),
    (7, 7, 0, 9, 0),
    (8, 9, 0, 1, 1),
    (9, 9, 0, 1, 1),
    (10, 11, 0, 1, 1),
    (11, 11, 0, 1, 1),
    (12, 1, 1, 1, 1),
)


def make_config() -> SoybeanImportProfitConfig:
    return SoybeanImportProfitConfig(
        schema_version=1,
        commodity="soybean",
        page_title="进口大豆盘面净榨利",
        origins=tuple(
            Origin(code, code)
            for code in ("brazil", "us_gulf", "us_pnw", "argentina")
        ),
        default_parameters=SoybeanParameters(
            meal_yield=0.785,
            oil_yield=0.185,
            cents_per_bushel_to_usd_per_tonne=0.367437,
            tariff_rate=0.03,
            vat_rate=0.09,
            port_charge_cny_per_tonne=100,
            processing_fee_cny_per_tonne=150,
            additional_fees_cny_per_tonne=0,
        ),
        origin_overrides=(),
        cnf_unit="cents_per_bushel",
        stable_key=(
            "business_date",
            "commodity",
            "origin",
            "shipment_year",
            "shipment_month",
        ),
        contract_mapping=tuple(
            ContractMappingRule(
                shipment_month,
                ContractLegRule(cbot_month, cbot_offset),
                ContractLegRule(dce_month, dce_offset),
            )
            for shipment_month, cbot_month, cbot_offset, dce_month, dce_offset in MAPPING
        ),
        contract_mapping_identity="import_profit_soybean:schema_version=1",
        fx_policy=FxPolicy(
            minimum_tenor_months=0,
            maximum_tenor_months=12,
            tenor_columns=tuple((tenor, f"Fwd_{tenor}") for tenor in range(13)),
            exact_tenor_first=True,
            interpolation_method="linear",
            same_business_date_only=True,
            adjacent_valid_tenors_only=True,
            extrapolation_allowed=False,
        ),
        business_calendar_policy=BusinessCalendarPolicy(
            timezone="Asia/Shanghai",
            calendar_type="weekday",
            weekdays=("monday", "tuesday", "wednesday", "thursday", "friday"),
            exclude_weekends=True,
            exchange_holiday_calendar_required=False,
            retain_weekday_without_market_data=True,
            infer_exchange_open_from_market_data=False,
            allow_previous_business_day_fallback=False,
        ),
        display_policy=DisplayPolicy(
            application="spread-dashboard",
            displayed_profit_metrics=("screen_net_crush_margin",),
            forbidden_profit_metrics=(),
            recent_business_days=10,
        ),
        seasonality_policy=SeasonalityPolicy(
            window_start="shipment_month_minus_4_month_start",
            window_end="shipment_month_minus_1_month_end",
            prior_shipment_years=5,
            prior_year_mean_years=5,
            minimum_valid_years=3,
            smoothing_enabled=False,
            null_line_break=True,
        ),
    )


CONFIG = make_config()


def key(
    *,
    origin: str = "brazil",
    shipment_year: int = 2026,
    shipment_month: int = 12,
    business_date: date = BUSINESS_DATE,
) -> BusinessKey:
    return BusinessKey(
        business_date,
        "soybean",
        origin,
        shipment_year,
        shipment_month,
        CONFIG.origin_codes,
        CONFIG.commodity,
    )


def cnf(business_key: BusinessKey, value: float | None = 150) -> CnfQuoteRecord:
    return CnfQuoteRecord(
        business_key=business_key,
        cnf_cents_per_bushel=value,
        source="manual_ui",
        updated_at=datetime(2026, 7, 28, 7, 0, tzinfo=timezone.utc),
        batch_id="recalc-fixed",
    )


def cbot(
    year: int = 2027,
    month: int = 1,
    price: float = 1200,
    *,
    business_date: date = BUSINESS_DATE,
) -> CbotPricePoint:
    return CbotPricePoint(
        market_date=business_date,
        contract_year=year,
        contract_month=month,
        price_cents_per_bushel=price,
        exchange_quality_status="accepted",
        is_usable=True,
        eligible_for_import_profit=True,
        source="reuters_sql",
        source_table="us_cbot_soybean",
        source_column=f"F{year:04d}{month:02d}",
        source_snapshot_sha256="CBOT-SHA",
    )


def fx(tenor: int, value: float = 7.2) -> FxPricePoint:
    return FxPricePoint(
        market_date=BUSINESS_DATE,
        tenor_months=tenor,
        fx_value=value,
        source="reuters_sql",
        source_table="fx_curve",
        source_column=f"Fwd_{tenor}",
        source_snapshot_sha256="FX-SHA",
    )


def dce(code: str, price: float) -> DcePricePoint:
    return DcePricePoint(
        business_date=BUSINESS_DATE,
        contract_code=code,
        price_cny_per_tonne=price,
        price_type="post_close_current_price",
        source="akshare",
        source_function="futures_zh_spot",
        is_usable=True,
        source_snapshot_sha256="DCE-SHA",
    )


def run(
    requested,
    *,
    cnf_records,
    cbot_records=None,
    fx_records=None,
    dce_records=None,
):
    return recalculate_soybean_keys(
        requested,
        config=CONFIG,
        cnf_records=cnf_records,
        cbot_records=[cbot()] if cbot_records is None else cbot_records,
        fx_records=[fx(5)] if fx_records is None else fx_records,
        dce_records=(
            [dce("M2701", 3200), dce("Y2701", 8000)]
            if dce_records is None
            else dce_records
        ),
    )


def test_fixed_formula_reconciliation_uses_existing_calculator() -> None:
    business_key = key()
    batch = run([business_key], cnf_records=[cnf(business_key)])
    item = batch.items[0]
    result = item.calculation_result

    assert batch.requested_count == 1
    assert batch.success_count == 1
    assert batch.incomplete_count == 0
    assert item.market_snapshot.snapshot_status is SnapshotStatus.COMPLETE
    assert result.calculation_status is CalculationStatus.SUCCESS
    assert result.usd_cost_per_tonne == pytest.approx(
        496.039950000000,
        abs=1e-12,
    )
    assert result.duty_paid_cost_cny_per_tonne == pytest.approx(
        4009.709173428001,
        abs=1e-12,
    )
    assert result.net_crush_margin_cny_per_tonne == pytest.approx(
        -267.709173428001,
        abs=1e-12,
    )


def test_only_explicit_keys_are_returned_and_sorting_is_stable() -> None:
    keys = [
        key(origin="us_pnw"),
        key(origin="brazil"),
        key(origin="us_gulf"),
    ]
    cnf_records = [cnf(item) for item in keys]
    extra_key = key(origin="argentina")
    batch = run(
        keys,
        cnf_records=[*cnf_records, cnf(extra_key)],
        cbot_records=[cbot(), cbot(month=3)],
        fx_records=[fx(5), fx(6)],
        dce_records=[
            dce("M2701", 3200),
            dce("Y2701", 8000),
            dce("M2705", 3300),
            dce("Y2705", 8100),
        ],
    )
    expected = tuple(sorted(keys, key=lambda item: (item.business_date, item.commodity, item.origin, item.shipment_year, item.shipment_month)))
    assert batch.requested_count == 3
    assert batch.requested_keys == expected
    assert tuple(item.business_key for item in batch.items) == expected
    assert extra_key not in batch.requested_keys


def test_duplicate_and_weekend_requests_fail_before_calculation() -> None:
    business_key = key()
    with pytest.raises(DuplicateRecalculationKeyError):
        run(
            [business_key, business_key],
            cnf_records=[cnf(business_key)],
        )

    weekend = key(business_date=date(2026, 8, 1))
    with pytest.raises(NonBusinessWeekdayError):
        run([business_key, weekend], cnf_records=[cnf(business_key), cnf(weekend)])


def test_one_complete_and_one_missing_cnf_are_independent() -> None:
    complete_key = key(origin="brazil")
    incomplete_key = key(origin="us_gulf")
    batch = run(
        [incomplete_key, complete_key],
        cnf_records=[cnf(complete_key)],
    )
    by_origin = {item.business_key.origin: item for item in batch.items}
    assert batch.success_count == 1
    assert batch.incomplete_count == 1
    assert by_origin["brazil"].calculation_result.calculation_status is CalculationStatus.SUCCESS
    assert by_origin["us_gulf"].calculation_result.missing_reasons == (
        MissingReason.MISSING_CNF,
    )
    assert dict(batch.missing_reason_counts)[MissingReason.MISSING_CNF] == 1


def test_one_complete_and_one_missing_cbot_are_independent() -> None:
    december = key(shipment_month=12)
    november = key(origin="us_gulf", shipment_month=11)
    batch = run(
        [november, december],
        cnf_records=[cnf(december), cnf(november)],
        cbot_records=[cbot()],
        fx_records=[fx(4), fx(5)],
    )
    by_month = {item.business_key.shipment_month: item for item in batch.items}
    assert by_month[12].calculation_result.calculation_status is CalculationStatus.SUCCESS
    assert by_month[11].calculation_result.missing_reasons == (
        MissingReason.MISSING_CBOT,
    )
    assert dict(batch.missing_reason_counts)[MissingReason.MISSING_CBOT] == 1


@pytest.mark.parametrize("cnf_value", [0, -25])
def test_zero_and_negative_cnf_are_successful_quotes(cnf_value: float) -> None:
    business_key = key()
    batch = run(
        [business_key],
        cnf_records=[cnf(business_key, cnf_value)],
    )
    assert batch.success_count == 1
    assert batch.items[0].calculation_result.calculation_status is CalculationStatus.SUCCESS


def test_interpolated_fx_and_december_cross_year_contracts_calculate() -> None:
    business_key = key()
    batch = run(
        [business_key],
        cnf_records=[cnf(business_key)],
        fx_records=[fx(4, 7.0), fx(6, 7.4)],
    )
    item = batch.items[0]
    assert item.market_snapshot.fx_value == pytest.approx(7.2)
    assert item.market_snapshot.fx_is_interpolated is True
    assert item.market_snapshot.cbot_contract_year == 2027
    assert item.market_snapshot.soymeal_contract_code == "M2701"
    assert item.calculation_result.calculation_status is CalculationStatus.SUCCESS


def test_existing_calculator_is_invoked_for_every_requested_key(monkeypatch) -> None:
    import agri_research_agent.import_profit.recalculation as recalculation

    first = key(origin="brazil")
    second = key(origin="us_gulf")
    calls = []
    real_calculator = recalculation.calculate_soybean_net_crush_margin

    def recording_calculator(calculation_input, config):
        calls.append(calculation_input.business_key)
        return real_calculator(calculation_input, config)

    monkeypatch.setattr(
        recalculation,
        "calculate_soybean_net_crush_margin",
        recording_calculator,
    )
    run([first, second], cnf_records=[cnf(first), cnf(second)])
    assert tuple(calls) == tuple(
        sorted(
            (first, second),
            key=lambda item: (
                item.business_date,
                item.commodity,
                item.origin,
                item.shipment_year,
                item.shipment_month,
            ),
        )
    )


def test_inputs_are_not_mutated_and_fixed_input_is_repeatable() -> None:
    requested = [key()]
    cnf_records = [cnf(requested[0])]
    cbot_records = [cbot()]
    fx_records = [fx(5)]
    dce_records = [dce("M2701", 3200), dce("Y2701", 8000)]
    originals = (
        tuple(requested),
        tuple(cnf_records),
        tuple(cbot_records),
        tuple(fx_records),
        tuple(dce_records),
    )
    first = recalculate_soybean_keys(
        requested,
        config=CONFIG,
        cnf_records=cnf_records,
        cbot_records=cbot_records,
        fx_records=fx_records,
        dce_records=dce_records,
    )
    second = recalculate_soybean_keys(
        requested,
        config=CONFIG,
        cnf_records=cnf_records,
        cbot_records=cbot_records,
        fx_records=fx_records,
        dce_records=dce_records,
    )
    assert first == second
    assert originals == (
        tuple(requested),
        tuple(cnf_records),
        tuple(cbot_records),
        tuple(fx_records),
        tuple(dce_records),
    )
