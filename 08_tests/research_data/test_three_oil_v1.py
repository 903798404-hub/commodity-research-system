from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from agri_research_agent.research_data.canonical_spreads import (
    CanonicalSpreadError,
    DuplicateConflictError,
    load_palm_core_six,
)
from agri_research_agent.research_data.three_oil_v1 import (
    RECOVERED_HISTORICAL_FORMULA,
    THREE_OIL_V1_STATUS,
    USER_APPROVED_BUSINESS_DEFINITION,
    calculate_spread_observations,
    load_three_oil_v1,
    resolve_series_observations,
)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "02_configs" / "international_three_oil_v1.sealed.json"
PALM_MANIFEST = ROOT / "02_configs" / "international_palm_core_six.sealed.json"
DAY = date(2026, 8, 10)


def _record(series, value: str, day: date = DAY) -> dict[str, object]:
    return {
        "provider_series_id": series.provider_series_id,
        "business_date": day,
        "price": value,
    }


def test_manifest_composes_unique_immutable_three_oil_contracts() -> None:
    catalog = load_three_oil_v1(MANIFEST)

    assert catalog.status == THREE_OIL_V1_STATUS
    assert catalog.approved_on == date(2026, 8, 16)
    assert (len(catalog.series), len(catalog.derived_series), len(catalog.spreads)) == (
        20,
        2,
        20,
    )
    assert len({item.series_id for item in catalog.series}) == 20
    assert len({item.provider_series_id for item in catalog.series}) == 20
    assert len({item.series_id for item in catalog.derived_series}) == 2
    assert len({item.spread_id for item in catalog.spreads}) == 20
    assert {item.status for item in catalog.series} == {"READY"}
    assert {item.status for item in catalog.derived_series} == {"READY"}
    assert {item.status for item in catalog.spreads} == {"READY"}
    with pytest.raises(FrozenInstanceError):
        catalog.status = "changed"  # type: ignore[misc]


def test_palm_core_six_is_composed_without_identity_or_coverage_drift() -> None:
    catalog = load_three_oil_v1(MANIFEST)
    palm = load_palm_core_six(PALM_MANIFEST)

    assert catalog.palm_core == palm
    assert [item.series_id for item in catalog.series[:6]] == [
        str(item.series_id) for item in palm.series
    ]
    assert [item.spread_id for item in catalog.spreads[:6]] == [
        item.spread_id for item in palm.spreads
    ]
    assert [item.common_observation_count for item in catalog.spreads[:6]] == [
        item.common_observation_count for item in palm.spreads
    ]


def test_provenance_and_oil_world_fallback_are_explicit() -> None:
    catalog = load_three_oil_v1(MANIFEST)

    assert {item.origin_system for item in catalog.series} == {"lutou"}
    assert {item.acquisition_channel for item in catalog.series} == {
        "manual_snapshot"
    }
    oil_world = [item for item in catalog.series if item.provider == "Oil World"]
    assert [item.series_id for item in oil_world] == [
        "market.physical.soybean_oil.netherlands.ex_mill_fob"
    ]
    assert oil_world[0].source_native_table == "oil_world_prices"
    assert all(
        item.provider == "Reuters"
        for item in catalog.series
        if item.source_native_table != "oil_world_prices"
    )


def test_unknown_metadata_and_product_boundaries_are_preserved() -> None:
    catalog = load_three_oil_v1(MANIFEST)
    argentina = catalog.series_by_id(
        "market.physical.soybean_oil.argentina.upper_river.spot"
    )
    rapeseed = catalog.series_by_id("market.physical.rapeseed_oil.europe.spot")
    hvo = catalog.series_by_id(
        "market.physical.hydrogenated_vegetable_oil.india.mumbai.spot"
    )
    rme = catalog.series_by_id("market.biofuel.rme.europe.ara.spot")

    assert (argentina.quote_basis, argentina.product_grade) == (
        "UNKNOWN",
        "UNKNOWN",
    )
    assert rapeseed.quote_basis == "UNKNOWN"
    assert rapeseed.product_grade != "Crude"
    assert hvo.product == "Hydrogenated Vegetable Oil"
    assert hvo.source_native_series.startswith("氢化植物油(菜油)")
    assert (rme.product, rme.location_native, rme.tenor) == (
        "RME Biodiesel",
        "ARA",
        "Spot",
    )
    assert "RED" not in rme.product and rme.tenor != "M1"


def test_mapping_scale_conversions_are_applied_once() -> None:
    catalog = load_three_oil_v1(MANIFEST)
    argentina_id = "market.basis.soybean_oil.argentina.upper_river.spot"
    brazil_id = "market.basis.soybean_oil.brazil.paranagua.spot"
    rin_id = "market.environmental_credit.rin.d4"

    argentina = catalog.series_by_id(argentina_id)
    brazil = catalog.series_by_id(brazil_id)
    rin = catalog.series_by_id(rin_id)
    assert resolve_series_observations(
        catalog, argentina_id, {argentina_id: [_record(argentina, "-1450")]}
    )[0].value == Decimal("-14.5")
    assert resolve_series_observations(
        catalog, brazil_id, {brazil_id: [_record(brazil, "-1310")]}
    )[0].value == Decimal("-13.1")
    assert resolve_series_observations(
        catalog, rin_id, {rin_id: [_record(rin, "217")]}
    )[0].value == Decimal("2.17")


def test_cbot_and_us_flat_derived_series_use_approved_220462_formula() -> None:
    catalog = load_three_oil_v1(MANIFEST)
    basis_id = "market.basis.soybean_oil.us.central_illinois.spot"
    cbot_id = "market.futures.soybean_oil.cbot.continuous_1"
    board_id = "market.derived.soybean_oil.cbot.continuous_1.usd_per_metric_tonne"
    flat_id = "market.derived.soybean_oil.us.central_illinois.spot"
    basis = catalog.series_by_id(basis_id)
    cbot = catalog.series_by_id(cbot_id)
    records = {
        basis_id: [_record(basis, "1.5")],
        cbot_id: [_record(cbot, "50")],
    }

    assert resolve_series_observations(catalog, board_id, records)[0].value == Decimal(
        "1102.3100"
    )
    assert resolve_series_observations(catalog, flat_id, records)[0].value == Decimal(
        "1135.37930"
    )


def test_boho_recovers_full_historical_formula_with_ice_diesel() -> None:
    catalog = load_three_oil_v1(MANIFEST)
    basis_id = "market.basis.soybean_oil.us.central_illinois.spot"
    cbot_id = "market.futures.soybean_oil.cbot.continuous_1"
    diesel_id = "market.energy.diesel.ice.continuous_1"
    records = {
        basis_id: [_record(catalog.series_by_id(basis_id), "1.5")],
        cbot_id: [_record(catalog.series_by_id(cbot_id), "50")],
        diesel_id: [_record(catalog.series_by_id(diesel_id), "900")],
    }

    spread = catalog.spread_by_id("spread.energy.boho.us_soy_ice_diesel")
    result = calculate_spread_observations(catalog, spread.spread_id, records)
    assert result[0].value == Decimal("235.37930")
    assert spread.definition_evidence == RECOVERED_HISTORICAL_FORMULA
    assert "LGOc1" in spread.formula
    assert "crude" not in spread.formula.lower()


def test_board_argentina_uses_fixed_freight_business_assumption() -> None:
    catalog = load_three_oil_v1(MANIFEST)
    cbot_id = "market.futures.soybean_oil.cbot.continuous_1"
    argentina_id = "market.physical.soybean_oil.argentina.upper_river.spot"
    records = {
        cbot_id: [_record(catalog.series_by_id(cbot_id), "50")],
        argentina_id: [_record(catalog.series_by_id(argentina_id), "1000")],
    }

    spread = catalog.spread_by_id(
        "spread.soybean_oil.cbot_board_argentina.freight_30"
    )
    result = calculate_spread_observations(catalog, spread.spread_id, records)
    assert result[0].value == Decimal("72.3100")
    assert spread.constant == Decimal("-30")
    assert spread.definition_evidence == USER_APPROVED_BUSINESS_DEFINITION
    assert spread.fixed_assumption_ids == (
        "assumption.freight.cbot_board_to_argentina",
    )


def test_india_inr_contracts_and_three_approved_spreads_are_preserved() -> None:
    catalog = load_three_oil_v1(MANIFEST)
    spread_ids = {
        "spread.india.hvo_refined_soy",
        "spread.india.refined_soy_refined_palm",
        "spread.india.refined_sunflower_refined_soy",
        "spread.india.hvo_refined_palm",
    }
    india_spreads = [catalog.spread_by_id(item) for item in spread_ids]

    assert {item.currency for item in india_spreads} == {"INR"}
    assert {item.unit for item in india_spreads} == {"metric_tonne"}
    assert all(item.constant == 0 for item in india_spreads)
    assert all(
        item.definition_evidence == USER_APPROVED_BUSINESS_DEFINITION
        for item in india_spreads
    )


def test_rme_premium_uses_spot_ara_rme_and_low_sulfur_diesel() -> None:
    catalog = load_three_oil_v1(MANIFEST)
    spread = catalog.spread_by_id("spread.europe.rme_ara_diesel_premium")

    assert tuple(item.series_id for item in spread.terms) == (
        "market.biofuel.rme.europe.ara.spot",
        "market.energy.diesel.europe.ara.spot",
    )
    assert tuple(item.multiplier for item in spread.terms) == (
        Decimal("1"),
        Decimal("-1"),
    )
    assert spread.currency == "USD" and spread.unit == "metric_tonne"


def test_duplicate_collapse_conflict_and_exact_date_inner_join() -> None:
    catalog = load_three_oil_v1(MANIFEST)
    arg_id = "market.physical.soybean_oil.argentina.upper_river.spot"
    bra_id = "market.physical.soybean_oil.brazil.paranagua.spot"
    arg = catalog.series_by_id(arg_id)
    bra = catalog.series_by_id(bra_id)
    next_day = date(2026, 8, 11)
    records = {
        arg_id: [
            _record(arg, "1000"),
            _record(arg, "1000"),
            _record(arg, "999", next_day),
        ],
        bra_id: [_record(bra, "950")],
    }

    result = calculate_spread_observations(
        catalog, "spread.soybean_oil.argentina_brazil", records
    )
    assert [(item.business_date, item.value) for item in result] == [
        (DAY, Decimal("50"))
    ]
    assert result[0].input_duplicate_counts[arg_id] == 1

    records[arg_id] = [_record(arg, "1000"), _record(arg, "1001")]
    with pytest.raises(DuplicateConflictError, match="conflicting non-null prices"):
        calculate_spread_observations(
            catalog, "spread.soybean_oil.argentina_brazil", records
        )


def test_all_spreads_have_full_2021_2026_ytd_seasonality_metadata() -> None:
    catalog = load_three_oil_v1(MANIFEST)

    assert all(item.seasonality_eligibility == "FULL" for item in catalog.spreads)
    assert all(
        set(item.common_observations_by_year) == {2021, 2022, 2023, 2024, 2025, 2026}
        for item in catalog.spreads
    )
    assert all(
        item.common_observations_by_year[2026] > 0 for item in catalog.spreads
    )
    assert all(item.latest_common_date.year == 2026 for item in catalog.spreads)


def test_page_rows_keep_approved_order_and_reuse_contracts() -> None:
    catalog = load_three_oil_v1(MANIFEST)
    expected_counts = {
        "page.palm.v1": 7,
        "page.soy.v1.page1": 9,
        "page.soy.v1.page2": 6,
        "page.rape.v1.page1": 9,
    }

    for page_id, count in expected_counts.items():
        page = catalog.page_by_id(page_id)
        assert [row.row_number for row in page.rows] == list(
            range(1, len(page.rows) + 1)
        )
        assert sum(len(row.metrics) for row in page.rows) == count
    assert catalog.page_by_id("page.soy.v1.page1").rows[0].metrics[0].contract_id == (
        "spread.international.rape_soy"
    )
    assert catalog.page_by_id("page.rape.v1.page1").rows[0].metrics[0].contract_id == (
        "spread.international.rape_soy"
    )
    assert sum(
        metric.contract_id == "spread.international.soy_palm"
        for page in catalog.pages
        for row in page.rows
        for metric in row.metrics
    ) == 2


def test_manifest_fails_closed_if_approved_conversion_drifts(tmp_path: Path) -> None:
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    raw["base_manifest"] = str(PALM_MANIFEST)
    raw["conversion_rules"][0]["operand"] = "22"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CanonicalSpreadError, match="conversion contracts drifted"):
        load_three_oil_v1(tampered)
