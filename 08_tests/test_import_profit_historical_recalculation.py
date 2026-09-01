from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.import_profit.config import load_soybean_config
from agri_research_agent.import_profit.contract_mapping import map_soybean_contracts
from agri_research_agent.import_profit.historical_cnf_adapter import (
    HISTORICAL_CNF_SCHEMA,
    HistoricalCnfQuote,
    shipment_year_for,
)
from agri_research_agent.import_profit.historical_dce_adapter import (
    HISTORICAL_DCE_CONTINUOUS_SCHEMA,
    HistoricalDceContinuousPoint,
    HistoricalDceError,
)
from agri_research_agent.import_profit.historical_recalculation import (
    HistoricalBusinessKeyError,
    HistoricalSourceSchemaError,
    business_key_rows,
    generate_historical_key_set,
    load_historical_cnf_parquet,
    load_historical_dce_continuous_parquet,
    recalculate_historical_soybean,
)
from agri_research_agent.import_profit.market_snapshot import (
    CbotPricePoint,
    FxPricePoint,
)
from agri_research_agent.import_profit.models import MissingReason


ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_soybean_config(ROOT / "02_configs" / "import_profit_soybean.yaml")
DAY = date(2026, 6, 10)
UPDATED_AT = datetime(2026, 7, 30, tzinfo=timezone.utc)


def cnf_grid(
    business_date: date = DAY,
    *,
    default: float | None = 150.0,
    special_values: bool = True,
) -> list[HistoricalCnfQuote]:
    result = []
    for origin in CONFIG.origin_codes:
        for month in range(1, 13):
            value = default
            if special_values and origin == "brazil" and month == 1:
                value = None
            elif special_values and origin == "brazil" and month == 2:
                value = 0.0
            elif special_values and origin == "brazil" and month == 3:
                value = -1.25
            result.append(
                HistoricalCnfQuote(
                    business_date=business_date,
                    commodity="soybean",
                    origin=origin,
                    shipment_year=shipment_year_for(business_date, month),
                    shipment_month=month,
                    cnf_cents_per_bushel=value,
                    source="historical_excel",
                    updated_at=UPDATED_AT,
                    batch_id="history-test",
                )
            )
    return result


def dce_points(
    business_date: date = DAY,
    *,
    missing: tuple[str, int] | None = None,
) -> list[HistoricalDceContinuousPoint]:
    result = []
    for instrument, base in (("soymeal", 3000.0), ("soyoil", 8000.0)):
        for month in (1, 5, 9):
            if missing == (instrument, month):
                continue
            result.append(
                HistoricalDceContinuousPoint(
                    business_date=business_date,
                    instrument=instrument,
                    delivery_month=month,
                    price_cny_per_tonne=base + month,
                    price_type="historical_continuous_close",
                    source="reuters_sql",
                    source_table="内盘期货价格_收盘",
                    source_column=f"{instrument}_{month}",
                    source_snapshot_sha256="DCE-SHA",
                )
            )
    return result


def cbot_points(
    business_date: date = DAY,
    *,
    omit: tuple[int, int] | None = None,
) -> list[CbotPricePoint]:
    contracts = {
        (
            mapped.cbot.contract_year,
            mapped.cbot.contract_month,
        )
        for month in range(1, 13)
        for mapped in [map_soybean_contracts(
            CONFIG, shipment_year_for(business_date, month), month
        )]
    }
    return [
        CbotPricePoint(
            market_date=business_date,
            contract_year=year,
            contract_month=month,
            price_cents_per_bushel=1200.0 + month,
            exchange_quality_status="standard_window",
            is_usable=True,
            eligible_for_import_profit=True,
            source="reuters_sql",
            source_table="us_cbot_soybean",
            source_column=f"F{year:04d}{month:02d}",
            source_snapshot_sha256="CBOT-SHA",
        )
        for year, month in sorted(contracts)
        if (year, month) != omit
    ]


def fx_points(
    business_date: date = DAY,
    *,
    tenors: tuple[int, ...] = tuple(range(13)),
) -> list[FxPricePoint]:
    return [
        FxPricePoint(
            market_date=business_date,
            tenor_months=tenor,
            fx_value=7.0 + tenor / 100,
            source="reuters_sql",
            source_table="usdcny_forward",
            source_column=f"Fwd_{tenor}",
            source_snapshot_sha256="FX-SHA",
        )
        for tenor in tenors
    ]


def run(
    records=None,
    *,
    dce=None,
    cbot=None,
    fx=None,
    batch_size=11,
):
    return recalculate_historical_soybean(
        cnf_grid() if records is None else records,
        config=CONFIG,
        as_of_date=DAY,
        continuous_dce_points=dce_points() if dce is None else dce,
        cbot_records=cbot_points() if cbot is None else cbot,
        fx_records=fx_points() if fx is None else fx,
        batch_size=batch_size,
    )


def test_key_generation_preserves_source_null_zero_negative_and_order():
    key_set = generate_historical_key_set(
        reversed(cnf_grid()), config=CONFIG, as_of_date=DAY
    )
    rows = business_key_rows(key_set)
    assert len(rows) == 48
    assert rows == sorted(
        rows,
        key=lambda row: tuple(row[name] for name in (
            "business_date", "commodity", "origin", "shipment_year",
            "shipment_month",
        )),
    )
    assert {row["origin"] for row in rows} == set(CONFIG.origin_codes)
    assert {row["shipment_month"] for row in rows} == set(range(1, 13))
    assert {point.source for point in key_set.cnf_points} == {"historical_excel"}
    assert (key_set.cnf_null_count, key_set.cnf_zero_count,
            key_set.cnf_negative_count) == (1, 1, 1)
    values = [point.cnf_cents_per_bushel for point in key_set.cnf_points]
    assert None in values and 0.0 in values and -1.25 in values


def test_as_of_date_inclusive_and_future_excluded():
    records = cnf_grid(DAY) + cnf_grid(date(2026, 6, 11))
    key_set = generate_historical_key_set(
        records, config=CONFIG, as_of_date=DAY
    )
    assert key_set.source_record_count == 96
    assert key_set.excluded_after_as_of_count == 48
    assert {key.business_date for key in key_set.business_keys} == {DAY}


@pytest.mark.parametrize("mutation", ["weekend", "duplicate", "year", "coverage"])
def test_structural_key_conflicts_are_fatal(mutation):
    records = cnf_grid()
    if mutation == "weekend":
        records = cnf_grid(date(2026, 6, 13))
    elif mutation == "duplicate":
        records.append(records[0])
    elif mutation == "year":
        records[0] = replace(records[0], shipment_year=records[0].shipment_year + 1)
    else:
        records.pop()
    with pytest.raises(HistoricalBusinessKeyError):
        generate_historical_key_set(records, config=CONFIG, as_of_date=date(2026, 6, 30))


def test_strict_parquet_loaders_reject_unsorted_and_wrong_schema(tmp_path):
    cnf_rows = [record.__dict__ for record in ()]  # slots: explicit rows below
    cnf_rows = [
        {
            name: getattr(record, name)
            for name in HISTORICAL_CNF_SCHEMA.names
        }
        for record in reversed(cnf_grid())
    ]
    cnf_path = tmp_path / "cnf.parquet"
    pq.write_table(pa.Table.from_pylist(cnf_rows, HISTORICAL_CNF_SCHEMA), cnf_path)
    with pytest.raises(HistoricalSourceSchemaError, match="stably sorted"):
        load_historical_cnf_parquet(cnf_path)

    dce_rows = [
        {name: getattr(record, name) for name in HISTORICAL_DCE_CONTINUOUS_SCHEMA.names}
        for record in dce_points()
    ]
    dce_path = tmp_path / "dce.parquet"
    wrong = HISTORICAL_DCE_CONTINUOUS_SCHEMA.remove(8)
    pq.write_table(pa.Table.from_pylist(
        [{key: value for key, value in row.items() if key != "source_snapshot_sha256"}
         for row in dce_rows],
        wrong,
    ), dce_path)
    with pytest.raises(HistoricalSourceSchemaError, match="Schema"):
        load_historical_dce_continuous_parquet(dce_path)


def test_dce_exact_resolution_deduplicates_and_preserves_provenance():
    result = run()
    assert len(result.resolved_dce_points) == 6
    assert {point.business_date for point in result.resolved_dce_points} == {DAY}
    assert {point.source for point in result.resolved_dce_points} == {"reuters_sql"}
    assert {point.price_type for point in result.resolved_dce_points} == {
        "historical_continuous_close"
    }
    assert {point.contract_code[0] for point in result.resolved_dce_points} == {"M", "Y"}


def test_dce_missing_is_not_filled_and_conflict_is_fatal():
    missing_run = run(dce=dce_points(missing=("soyoil", 1)))
    assert any(
        MissingReason.MISSING_SOYOIL in item.market_snapshot.missing_reasons
        for item in missing_run.recalculation_batch.items
    )
    duplicate = dce_points()
    duplicate.append(replace(duplicate[0], price_cny_per_tonne=9999))
    with pytest.raises(HistoricalDceError, match="conflicting"):
        run(dce=duplicate)


def test_complete_incomplete_and_all_missing_reasons_propagate():
    records = cnf_grid(default=150.0, special_values=False)
    complete = run(records=records)
    assert complete.recalculation_batch.success_count == 48

    target_contract = map_soybean_contracts(
        CONFIG, shipment_year_for(DAY, 1), 1
    ).cbot
    mixed = run(
        records=cnf_grid(),
        dce=dce_points(missing=("soymeal", 5)),
        cbot=cbot_points(omit=(
            target_contract.contract_year, target_contract.contract_month
        )),
        fx=[],
    )
    reasons = {
        reason
        for item in mixed.recalculation_batch.items
        for reason in item.market_snapshot.missing_reasons
    }
    assert {
        MissingReason.MISSING_CNF,
        MissingReason.MISSING_CBOT,
        MissingReason.MISSING_FX,
        MissingReason.MISSING_SOYMEAL,
    } <= reasons
    assert mixed.recalculation_batch.incomplete_count > 0
    for item in mixed.recalculation_batch.items:
        if item.calculation_result.missing_reasons:
            assert item.calculation_result.usd_cost_per_tonne is None
            assert item.calculation_result.duty_paid_cost_cny_per_tonne is None
            assert item.calculation_result.net_crush_margin_cny_per_tonne is None


def test_fx_direct_interpolation_and_missing_are_existing_semantics():
    direct = run(records=cnf_grid(default=150.0, special_values=False))
    assert all(
        item.market_snapshot.fx_selection_status.value == "direct"
        for item in direct.recalculation_batch.items
    )
    interpolated = run(
        records=cnf_grid(default=150.0, special_values=False),
        fx=fx_points(tenors=(0, 2, 4, 6, 8, 10, 12)),
    )
    assert any(
        item.market_snapshot.fx_is_interpolated
        for item in interpolated.recalculation_batch.items
    )
    missing = run(records=cnf_grid(default=150.0, special_values=False), fx=[])
    assert all(
        MissingReason.MISSING_FX in item.market_snapshot.missing_reasons
        for item in missing.recalculation_batch.items
    )


def test_batch_sizes_produce_identical_ordered_business_results():
    records = cnf_grid(default=150.0, special_values=False)
    one = run(records=records, batch_size=1)
    all_at_once = run(records=records, batch_size=1000)
    assert one.recalculation_batch.items == all_at_once.recalculation_batch.items
    assert one.recalculation_batch.requested_keys == tuple(
        sorted(
            one.recalculation_batch.requested_keys,
            key=lambda key: (
                key.business_date, key.commodity, key.origin,
                key.shipment_year, key.shipment_month,
            ),
        )
    )
