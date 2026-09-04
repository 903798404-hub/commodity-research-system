from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, time, timezone

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
    HistoricalCnfMarketPoint,
    MarketSnapshotDuplicateKeyError,
    MarketSnapshotValidationError,
    SnapshotStatus,
    build_soybean_market_snapshot,
    snapshot_to_calculation_input,
)
from agri_research_agent.import_profit.models import (
    BusinessKey,
    FxSelectionStatus,
    MissingReason,
)
from agri_research_agent.import_profit.soybean import (
    calculate_soybean_net_crush_margin,
)


BUSINESS_DATE = date(2026, 7, 28)
ORIGINS = ("brazil", "us_gulf", "us_pnw", "argentina")
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
        origins=tuple(Origin(code, code) for code in ORIGINS),
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


def make_key(
    *,
    business_date: date = BUSINESS_DATE,
    origin: str = "brazil",
    shipment_year: int = 2026,
    shipment_month: int = 12,
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


def make_cnf(
    key: BusinessKey,
    value: float | None = 150,
) -> CnfQuoteRecord:
    return CnfQuoteRecord(
        business_key=key,
        cnf_cents_per_bushel=value,
        source="manual_ui",
        updated_at=datetime(2026, 7, 28, 7, 0, tzinfo=timezone.utc),
        batch_id="fixed-batch",
    )


def make_cbot(
    *,
    market_date: date = BUSINESS_DATE,
    contract_year: int = 2027,
    contract_month: int = 1,
    price: float = 1200,
    usable: bool = True,
    eligible: bool = True,
) -> CbotPricePoint:
    return CbotPricePoint(
        market_date=market_date,
        contract_year=contract_year,
        contract_month=contract_month,
        price_cents_per_bushel=price,
        exchange_quality_status="accepted",
        is_usable=usable,
        eligible_for_import_profit=eligible,
        source="reuters_sql",
        source_table="us_cbot_soybean",
        source_column="F202701",
        source_snapshot_sha256="CBOT-SHA",
        source_statement_index=12,
    )


def make_fx(
    tenor: int = 5,
    value: float = 7.2,
    *,
    market_date: date = BUSINESS_DATE,
    source: str = "reuters_sql",
    snapshot: str = "FX-SHA",
) -> FxPricePoint:
    return FxPricePoint(
        market_date=market_date,
        tenor_months=tenor,
        fx_value=value,
        source=source,
        source_table="fx_curve",
        source_column=f"Fwd_{tenor}",
        source_snapshot_sha256=snapshot,
    )


def make_dce(
    contract_code: str,
    price: float,
    *,
    business_date: date = BUSINESS_DATE,
    price_type: str = "post_close_current_price",
    source: str = "akshare",
    usable: bool = True,
    quote_date_evidence_status: str = "source_confirmed",
) -> DcePricePoint:
    return DcePricePoint(
        business_date=business_date,
        contract_code=contract_code,
        price_cny_per_tonne=price,
        price_type=price_type,
        source=source,
        source_function="futures_zh_spot",
        is_usable=usable,
        quote_date_evidence_status=quote_date_evidence_status,
        contract_identity_status="source_confirmed_exact",
        source_contract_code=contract_code,
        source_delivery_month=int(contract_code[3:5]),
        source_quote_date=(
            None
            if quote_date_evidence_status == "time_only_unconfirmed"
            else business_date
        ),
        source_quote_time=time(15, 1),
        source_snapshot_sha256="DCE-SHA",
    )


def build_snapshot(
    key: BusinessKey,
    *,
    cnf_records=None,
    cbot_records=None,
    fx_records=None,
    dce_records=None,
):
    return build_soybean_market_snapshot(
        key,
        config=CONFIG,
        cnf_records=[make_cnf(key)] if cnf_records is None else cnf_records,
        cbot_records=[make_cbot()] if cbot_records is None else cbot_records,
        fx_records=[make_fx()] if fx_records is None else fx_records,
        dce_records=(
            [make_dce("M2701", 3200), make_dce("Y2701", 8000)]
            if dce_records is None
            else dce_records
        ),
    )


def test_complete_snapshot_and_calculation_input_use_exact_standard_points() -> None:
    key = make_key()
    snapshot = build_snapshot(key)

    assert snapshot.snapshot_status is SnapshotStatus.COMPLETE
    assert snapshot.missing_reasons == ()
    assert snapshot.cbot_contract_year == 2027
    assert snapshot.cbot_contract_month == 1
    assert snapshot.soymeal_contract_code == "M2701"
    assert snapshot.soyoil_contract_code == "Y2701"
    assert snapshot.fx_target_tenor == 5
    assert snapshot.fx_selection_status is FxSelectionStatus.DIRECT
    assert snapshot.cbot_source == "reuters_sql"
    assert snapshot.fx_source == "reuters_sql"

    calculation_input = snapshot_to_calculation_input(snapshot, config=CONFIG)
    assert calculation_input.cnf_cents_per_bushel == 150
    assert calculation_input.cbot_daily_price_cents_per_bushel == 1200
    assert calculation_input.fx_value == 7.2
    assert calculation_input.soymeal_price_cny_per_tonne == 3200
    assert calculation_input.soyoil_price_cny_per_tonne == 8000


def test_contract_identity_provenance_has_zero_numerical_effect() -> None:
    key = make_key()
    legacy_points = [make_dce("M2701", 3200), make_dce("Y2701", 8000)]
    inferred_points = [
        replace(
            point,
            contract_identity_status="continuous_inferred",
            source_contract_code=None,
            source_delivery_month=1,
        )
        for point in legacy_points
    ]

    before = build_snapshot(key, dce_records=legacy_points)
    after = build_snapshot(key, dce_records=inferred_points)
    before_result = calculate_soybean_net_crush_margin(
        snapshot_to_calculation_input(before, config=CONFIG), CONFIG
    )
    after_result = calculate_soybean_net_crush_margin(
        snapshot_to_calculation_input(after, config=CONFIG), CONFIG
    )

    assert before.business_key == after.business_key
    assert before.mapped_contracts == after.mapped_contracts
    assert before.cbot_price_cents_per_bushel == after.cbot_price_cents_per_bushel
    assert before.fx_value == after.fx_value
    assert before.cnf_cents_per_bushel == after.cnf_cents_per_bushel
    assert before.soymeal_price_cny_per_tonne == after.soymeal_price_cny_per_tonne
    assert before.soyoil_price_cny_per_tonne == after.soyoil_price_cny_per_tonne
    assert before.snapshot_status == after.snapshot_status
    assert before.missing_reasons == after.missing_reasons
    assert before_result == after_result
    assert after.soymeal_contract_code == "M2701"
    assert after.soymeal_contract_identity_status == "continuous_inferred"


def test_source_confirmed_quote_keeps_exact_pre_goal_c_numerics() -> None:
    key = make_key()
    snapshot = build_snapshot(key)
    result = calculate_soybean_net_crush_margin(
        snapshot_to_calculation_input(snapshot, config=CONFIG), CONFIG
    )

    assert snapshot.business_key == key
    assert snapshot.soymeal_price_cny_per_tonne == 3200
    assert snapshot.soyoil_price_cny_per_tonne == 8000
    assert snapshot.soymeal_quote_date_evidence_status == "source_confirmed"
    assert snapshot.soyoil_quote_date_evidence_status == "source_confirmed"
    assert result.usd_cost_per_tonne == pytest.approx(496.03995000000003)
    assert result.duty_paid_cost_cny_per_tonne == pytest.approx(4009.7091734280007)
    assert result.net_crush_margin_cny_per_tonne == pytest.approx(
        -267.70917342800067
    )


def test_time_only_exact_contract_is_not_selected_for_formal_snapshot() -> None:
    key = make_key()
    time_only = [
        make_dce(
            "M2701",
            3200,
            usable=False,
            quote_date_evidence_status="time_only_unconfirmed",
        ),
        make_dce(
            "Y2701",
            8000,
            usable=False,
            quote_date_evidence_status="time_only_unconfirmed",
        ),
    ]
    snapshot = build_snapshot(key, dce_records=time_only)

    assert snapshot.soymeal_contract_identity_status == "source_confirmed_exact"
    assert snapshot.soymeal_quote_date_evidence_status == "time_only_unconfirmed"
    assert snapshot.soymeal_price_cny_per_tonne is None
    assert snapshot.soyoil_price_cny_per_tonne is None
    assert snapshot.snapshot_status is SnapshotStatus.INCOMPLETE


@pytest.mark.parametrize("value", [150, -25, 0])
def test_finite_cnf_values_are_matched_without_truthiness_loss(value: float) -> None:
    key = make_key()
    snapshot = build_snapshot(key, cnf_records=[make_cnf(key, value)])
    assert snapshot.cnf_cents_per_bushel == value
    assert MissingReason.MISSING_CNF not in snapshot.missing_reasons


@pytest.mark.parametrize("records", ["null", "absent"])
def test_null_or_absent_cnf_is_missing(records: str) -> None:
    key = make_key()
    cnf = [make_cnf(key, None)] if records == "null" else []
    snapshot = build_snapshot(key, cnf_records=cnf)
    assert snapshot.snapshot_status is SnapshotStatus.INCOMPLETE
    assert snapshot.missing_reasons == (MissingReason.MISSING_CNF,)


def test_cbot_requires_exact_date_contract_and_eligibility() -> None:
    key = make_key()
    surrounding = [
        make_cbot(market_date=date(2026, 7, 27)),
        make_cbot(market_date=date(2026, 7, 29)),
        make_cbot(contract_month=3),
    ]
    snapshot = build_snapshot(key, cbot_records=surrounding)
    assert snapshot.cbot_price_cents_per_bushel is None
    assert snapshot.missing_reasons == (MissingReason.MISSING_CBOT,)

    ineligible = build_snapshot(
        key,
        cbot_records=[make_cbot(eligible=False)],
    )
    assert ineligible.cbot_price_cents_per_bushel is None
    assert ineligible.missing_reasons == (MissingReason.MISSING_CBOT,)


def test_fx_direct_spot_forward_and_same_day_interpolation() -> None:
    spot_key = make_key(shipment_year=2026, shipment_month=7)
    spot = build_snapshot(
        spot_key,
        cnf_records=[make_cnf(spot_key)],
        cbot_records=[make_cbot(contract_year=2026, contract_month=7)],
        fx_records=[make_fx(0, 7.1)],
        dce_records=[make_dce("M2609", 3200), make_dce("Y2609", 8000)],
    )
    assert spot.fx_target_tenor == 0
    assert spot.fx_value == 7.1
    assert spot.fx_selection_status is FxSelectionStatus.DIRECT

    forward = build_snapshot(make_key())
    assert forward.fx_value == 7.2
    assert forward.fx_selection_status is FxSelectionStatus.DIRECT

    interpolated = build_snapshot(
        make_key(),
        fx_records=[make_fx(4, 7.0), make_fx(6, 7.4)],
    )
    assert interpolated.fx_value == pytest.approx(7.2)
    assert interpolated.fx_is_interpolated is True
    assert interpolated.fx_lower_tenor == 4
    assert interpolated.fx_upper_tenor == 6
    assert tuple(item.tenor_months for item in interpolated.fx_point_provenance) == (4, 6)


@pytest.mark.parametrize(
    ("fx_records", "lower", "upper"),
    [
        ([make_fx(6, 7.4)], None, 6),
        ([make_fx(4, 7.0)], 4, None),
        (
            [
                make_fx(4, 7.0, market_date=date(2026, 7, 27)),
                make_fx(6, 7.4, market_date=date(2026, 7, 29)),
            ],
            None,
            None,
        ),
    ],
)
def test_fx_never_crosses_dates_or_extrapolates(
    fx_records,
    lower: int | None,
    upper: int | None,
) -> None:
    snapshot = build_snapshot(make_key(), fx_records=fx_records)
    assert snapshot.fx_value is None
    assert snapshot.fx_lower_tenor == lower
    assert snapshot.fx_upper_tenor == upper
    assert snapshot.fx_selection_status is FxSelectionStatus.INTERPOLATION_UNAVAILABLE
    assert snapshot.missing_reasons == (MissingReason.MISSING_FX,)


def test_dce_requires_exact_date_contract_and_usable_record() -> None:
    key = make_key()
    wrong_points = [
        make_dce("M2701", 3200, business_date=date(2026, 7, 27)),
        make_dce("Y2701", 8000, business_date=date(2026, 7, 27)),
        make_dce("M2705", 3300),
        make_dce("Y2705", 8100),
    ]
    snapshot = build_snapshot(key, dce_records=wrong_points)
    assert snapshot.missing_reasons == (
        MissingReason.MISSING_SOYMEAL,
        MissingReason.MISSING_SOYOIL,
    )

    unusable = build_snapshot(
        key,
        dce_records=[
            make_dce("M2701", 3200, usable=False),
            make_dce("Y2701", 8000),
        ],
    )
    assert unusable.missing_reasons == (MissingReason.MISSING_SOYMEAL,)


@pytest.mark.parametrize(
    ("dce_records", "reason"),
    [
        ([make_dce("Y2701", 8000)], MissingReason.MISSING_SOYMEAL),
        ([make_dce("M2701", 3200)], MissingReason.MISSING_SOYOIL),
    ],
)
def test_each_required_dce_leg_is_reported_independently(
    dce_records,
    reason: MissingReason,
) -> None:
    snapshot = build_snapshot(make_key(), dce_records=dce_records)
    assert snapshot.missing_reasons == (reason,)


def test_dce_source_and_price_type_are_preserved_without_normalization() -> None:
    snapshot = build_snapshot(
        make_key(),
        dce_records=[
            make_dce(
                "M2701",
                3200,
                source="historical_vendor",
                price_type="historical_daily_close",
            ),
            make_dce(
                "Y2701",
                8000,
                source="akshare",
                price_type="post_close_current_price",
            ),
        ],
    )
    assert snapshot.soymeal_source == "historical_vendor"
    assert snapshot.soymeal_price_type == "historical_daily_close"
    assert snapshot.soyoil_source == "akshare"
    assert snapshot.soyoil_price_type == "post_close_current_price"


@pytest.mark.parametrize(
    ("collection", "records"),
    [
        ("cnf", lambda key: [make_cnf(key), make_cnf(key)]),
        ("cbot", lambda key: [make_cbot(), make_cbot()]),
        ("fx", lambda key: [make_fx(), make_fx()]),
        ("dce", lambda key: [make_dce("M2701", 3200), make_dce("M2701", 3300)]),
    ],
)
def test_every_source_collection_rejects_duplicate_canonical_keys(
    collection: str,
    records,
) -> None:
    key = make_key()
    kwargs = {f"{collection}_records": records(key)}
    with pytest.raises(MarketSnapshotDuplicateKeyError):
        build_snapshot(key, **kwargs)


def test_mapping_reuses_cross_year_and_next_january_rules() -> None:
    december_key = make_key()
    december = build_snapshot(december_key)
    assert (
        december.cbot_contract_year,
        december.cbot_contract_month,
        december.soymeal_contract_code,
        december.soyoil_contract_code,
    ) == (2027, 1, "M2701", "Y2701")

    january_key = make_key(shipment_year=2027, shipment_month=1)
    january = build_snapshot(
        january_key,
        cnf_records=[make_cnf(january_key)],
        cbot_records=[make_cbot()],
        fx_records=[make_fx(6)],
        dce_records=[make_dce("M2705", 3200), make_dce("Y2705", 8000)],
    )
    assert (
        january.cbot_contract_year,
        january.cbot_contract_month,
        january.soymeal_contract_code,
        january.soyoil_contract_code,
    ) == (2027, 1, "M2705", "Y2705")


def test_multiple_missing_reasons_have_calculator_order() -> None:
    key = make_key()
    snapshot = build_snapshot(
        key,
        cnf_records=[],
        cbot_records=[],
        fx_records=[],
        dce_records=[],
    )
    assert snapshot.snapshot_status is SnapshotStatus.INCOMPLETE
    assert snapshot.missing_reasons == (
        MissingReason.MISSING_CNF,
        MissingReason.MISSING_CBOT,
        MissingReason.MISSING_FX,
        MissingReason.MISSING_SOYMEAL,
        MissingReason.MISSING_SOYOIL,
    )
    calculation_input = snapshot_to_calculation_input(snapshot, config=CONFIG)
    assert calculation_input.cnf_cents_per_bushel is None
    assert calculation_input.cbot_daily_price_cents_per_bushel is None
    assert calculation_input.fx_value is None
    assert calculation_input.soymeal_price_cny_per_tonne is None
    assert calculation_input.soyoil_price_cny_per_tonne is None


def test_inputs_are_not_mutated_output_is_frozen_and_repeated_result_is_equal() -> None:
    key = make_key()
    cnf_records = [make_cnf(key)]
    cbot_records = [make_cbot(), make_cbot(contract_month=3)]
    fx_records = [make_fx()]
    dce_records = [make_dce("M2701", 3200), make_dce("Y2701", 8000)]
    originals = (
        tuple(cnf_records),
        tuple(cbot_records),
        tuple(fx_records),
        tuple(dce_records),
    )
    first = build_soybean_market_snapshot(
        key,
        config=CONFIG,
        cnf_records=cnf_records,
        cbot_records=cbot_records,
        fx_records=fx_records,
        dce_records=dce_records,
    )
    second = build_soybean_market_snapshot(
        key,
        config=CONFIG,
        cnf_records=cnf_records,
        cbot_records=cbot_records,
        fx_records=fx_records,
        dce_records=dce_records,
    )
    assert first == second
    assert originals == (
        tuple(cnf_records),
        tuple(cbot_records),
        tuple(fx_records),
        tuple(dce_records),
    )
    with pytest.raises(FrozenInstanceError):
        first.fx_value = 8.0


def test_weekend_key_is_rejected_before_snapshot_generation() -> None:
    key = make_key(business_date=date(2026, 8, 1))
    with pytest.raises(NonBusinessWeekdayError):
        build_snapshot(key)


def test_standard_point_models_reject_invalid_structure() -> None:
    with pytest.raises(MarketSnapshotValidationError):
        make_cbot(price=0)
    with pytest.raises(MarketSnapshotValidationError):
        make_cbot(usable=False, eligible=True)
    with pytest.raises(MarketSnapshotValidationError):
        CbotPricePoint(
            market_date=BUSINESS_DATE,
            contract_year=2027,
            contract_month=1,
            price_cents_per_bushel=1200,
            exchange_quality_status="accepted",
            is_usable=True,
            eligible_for_import_profit=True,
            source="reuters_sql",
            source_table="us_cbot_soybean",
            source_column="F202701",
            source_snapshot_sha256="",
        )
    with pytest.raises(MarketSnapshotValidationError):
        make_fx(13)
    with pytest.raises(MarketSnapshotValidationError):
        make_dce("m2701", 3200)


def test_historical_cnf_source_enters_snapshot_without_relabeling() -> None:
    key = make_key()
    historical = HistoricalCnfMarketPoint(
        business_key=key,
        cnf_cents_per_bushel=-5,
        source="historical_excel",
        updated_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
        batch_id="historical-batch",
    )
    snapshot = build_snapshot(key, cnf_records=[historical])
    assert snapshot.cnf_cents_per_bushel == -5
    assert snapshot.cnf_source == "historical_excel"
    assert snapshot.snapshot_status is SnapshotStatus.COMPLETE


def test_historical_cnf_rejects_unknown_or_manual_source() -> None:
    key = make_key()
    for source in ("manual_ui", "unknown"):
        with pytest.raises(
            MarketSnapshotValidationError,
            match="source must remain historical_excel",
        ):
            HistoricalCnfMarketPoint(
                business_key=key,
                cnf_cents_per_bushel=0,
                source=source,
                updated_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
                batch_id="historical-batch",
            )


def test_manual_cnf_record_path_remains_unchanged() -> None:
    key = make_key()
    manual = make_cnf(key, 0)
    snapshot = build_snapshot(key, cnf_records=[manual])
    assert isinstance(manual, CnfQuoteRecord)
    assert manual.source == "manual_ui"
    assert snapshot.cnf_source == "manual_ui"
    assert snapshot.cnf_cents_per_bushel == 0
