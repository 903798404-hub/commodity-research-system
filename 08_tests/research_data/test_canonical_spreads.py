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
    calculate_exact_inner_spread,
    collapse_source_native_duplicates,
    load_palm_core_six,
)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "02_configs" / "international_palm_core_six.sealed.json"


EXPECTED_COVERAGE = {
    "spread.international.soy_palm": (
        "2017-03-01",
        "2026-08-10",
        1206,
        (131, 110, 121, 148, 160, 112),
    ),
    "spread.international.rape_palm": (
        "2003-10-31",
        "2026-08-12",
        5491,
        (240, 237, 239, 242, 237, 145),
    ),
    "spread.international.sunflower_palm": (
        "2003-10-31",
        "2026-08-12",
        5425,
        (238, 194, 225, 240, 238, 145),
    ),
    "spread.europe.soy_palm": (
        "2015-11-19",
        "2026-08-10",
        2234,
        (194, 211, 193, 192, 198, 116),
    ),
    "spread.europe.rape_palm": (
        "2001-03-05",
        "2026-08-12",
        6021,
        (243, 240, 240, 241, 237, 145),
    ),
    "spread.europe.sunflower_palm": (
        "2001-03-05",
        "2026-08-12",
        5958,
        (241, 197, 226, 239, 238, 145),
    ),
}


def test_core_six_manifest_loads_as_immutable_ready_contract() -> None:
    catalog = load_palm_core_six(MANIFEST)

    assert catalog.status == "PALM_CORE_SIX_READY"
    assert catalog.approved_on == date(2026, 8, 16)
    assert len(catalog.series) == 6
    assert len(catalog.spreads) == 6
    assert {item.status for item in catalog.series} == {"READY"}
    assert {item.status for item in catalog.spreads} == {"READY"}
    assert {item.currency for item in catalog.series} == {"USD"}
    assert {item.unit for item in catalog.series} == {"metric_tonne"}
    assert {item.conflicting_duplicate_date_count for item in catalog.series} == {0}
    with pytest.raises(FrozenInstanceError):
        catalog.status = "changed"  # type: ignore[misc]


def test_identity_boundaries_preserve_unknowns_and_native_locations() -> None:
    catalog = load_palm_core_six(MANIFEST)
    argentina = catalog.series_by_id(
        "market.physical.soybean_oil.argentina.upper_river.spot"
    )
    rapeseed = catalog.series_by_id("market.physical.rapeseed_oil.europe.spot")
    sunflower = catalog.series_by_id(
        "market.physical.sunflower_oil.europe.six_ports.ex_tank.spot"
    )
    malaysia = catalog.series_by_id(
        "market.physical.palm_oil.malaysia.rbd_palm_oil.fob.p1"
    )
    europe_palm = catalog.series_by_id(
        "market.physical.palm_oil.europe_nwe.rbd_palm_oil.cif.spot"
    )

    assert (argentina.quote_basis, argentina.product_grade) == ("UNKNOWN", "UNKNOWN")
    assert argentina.location_native == "Upper River"
    assert rapeseed.quote_basis == "UNKNOWN"
    assert rapeseed.location_native == "Netherlands/Europe"
    assert sunflower.quote_basis == "Ex-tank"
    assert sunflower.location_native == "欧洲六港"
    assert malaysia.product == "RBD Palm Oil"
    assert (malaysia.quote_basis, malaysia.tenor) == ("FOB", "P1")
    assert europe_palm.product == "RBD Palm Oil"
    assert (europe_palm.quote_basis, europe_palm.tenor) == ("CIF", "Spot")
    assert all(item.product != "RBD Palm Olein" for item in catalog.series)


def test_provenance_keeps_origin_and_acquisition_channel_separate() -> None:
    catalog = load_palm_core_six(MANIFEST)

    assert {str(item.origin_system) for item in catalog.series} == {"lutou"}
    assert {item.acquisition_channel.value for item in catalog.series} == {
        "manual_snapshot"
    }
    assert {item.provider for item in catalog.series} == {"Reuters", "Oil World"}
    assert all(
        str(item.source_locator).startswith("snapshot:01_data/manual/榨利表/")
        for item in catalog.series
    )
    assert len({str(item.provider_series_id) for item in catalog.series}) == 6


def test_six_formulas_leg_order_and_history_coverage_are_sealed() -> None:
    catalog = load_palm_core_six(MANIFEST)
    expected_years = (2021, 2022, 2023, 2024, 2025, 2026)

    assert {item.spread_id for item in catalog.spreads} == set(EXPECTED_COVERAGE)
    for spread in catalog.spreads:
        earliest, latest, total, counts = EXPECTED_COVERAGE[spread.spread_id]
        assert spread.formula == "price_A - price_B"
        assert spread.leg_a.series_id != spread.leg_b.series_id
        assert spread.common_earliest_date.isoformat() == earliest
        assert spread.latest_common_date.isoformat() == latest
        assert spread.common_observation_count == total
        assert tuple(spread.common_observations_by_year) == expected_years
        assert tuple(spread.common_observations_by_year.values()) == counts
        assert spread.seasonality_eligibility == "FULL"


def test_calculation_policy_forbids_filling_and_adjustments() -> None:
    catalog = load_palm_core_six(MANIFEST)
    policy = catalog.calculation_policy

    assert policy.formula == "price_A - price_B"
    assert policy.join == "exact_business_date_inner_join"
    assert policy.currency == "USD"
    assert policy.unit == "metric_tonne"
    assert not any(
        (
            policy.interpolation,
            policy.forward_fill,
            policy.fx_conversion,
            policy.unit_conversion,
            policy.freight_adjustment,
            policy.basis_conversion,
            policy.quality_adjustment,
        )
    )


def test_identical_duplicates_collapse_and_retain_count() -> None:
    result = collapse_source_native_duplicates(
        [
            {"business_date": "2026-08-10", "price": 1400},
            {"business_date": "2026-08-10", "price": "1400"},
            {"business_date": "2026-08-10", "price": None},
            {"business_date": "2026-08-11", "price": 1405},
        ],
        provider_series_id="provider:test:series",
    )

    assert result.duplicate_count == 1
    assert len(result.observations) == 2
    assert result.observations[0].price == Decimal("1400")
    assert result.observations[0].duplicate_count == 1
    assert result.observations[1].duplicate_count == 0


def test_conflicting_duplicate_blocks_instead_of_choosing_a_resolver() -> None:
    with pytest.raises(DuplicateConflictError, match="conflicting non-null prices"):
        collapse_source_native_duplicates(
            [
                {"business_date": "2026-08-10", "price": 1400},
                {"business_date": "2026-08-10", "price": 1401},
            ],
            provider_series_id="provider:test:series",
        )

    catalog = load_palm_core_six(MANIFEST)
    assert catalog.duplicate_policy.forbidden_resolvers == (
        "mean",
        "median",
        "first",
        "last",
    )


def test_exact_inner_join_never_interpolates_or_forward_fills() -> None:
    definition = load_palm_core_six(MANIFEST).spread_by_id(
        "spread.international.soy_palm"
    )
    result = calculate_exact_inner_spread(
        definition,
        [
            {"business_date": "2026-08-10", "price": 1400},
            {"business_date": "2026-08-11", "price": 1410},
            {"business_date": "2026-08-12", "price": 1420},
        ],
        [
            {"business_date": "2026-08-10", "price": 1100},
            {"business_date": "2026-08-10", "price": 1100},
            {"business_date": "2026-08-12", "price": 1125},
        ],
    )

    assert [item.business_date.isoformat() for item in result] == [
        "2026-08-10",
        "2026-08-12",
    ]
    assert [item.value for item in result] == [Decimal("300"), Decimal("295")]
    assert result[0].leg_b_duplicate_count == 1


def test_duplicate_resolution_rejects_mixed_provider_series() -> None:
    with pytest.raises(CanonicalSpreadError, match="different provider_series_id"):
        collapse_source_native_duplicates(
            [
                {
                    "provider_series_id": "provider:other:series",
                    "business_date": "2026-08-10",
                    "price": 1400,
                }
            ],
            provider_series_id="provider:expected:series",
        )


def test_manifest_tampering_fails_closed(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["calculation_policy"]["forward_fill"] = True
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CanonicalSpreadError, match="must be disabled"):
        load_palm_core_six(path)


def test_conflict_audit_cannot_be_sealed_ready(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["series"][0]["conflicting_duplicate_date_count"] = 1
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CanonicalSpreadError, match="not READY"):
        load_palm_core_six(path)


def test_core_six_leg_topology_cannot_drift(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["spreads"][0]["leg_a_series_id"] = payload["spreads"][1][
        "leg_a_series_id"
    ]
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CanonicalSpreadError, match="leg topology"):
        load_palm_core_six(path)
