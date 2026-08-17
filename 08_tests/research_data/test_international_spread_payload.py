from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

import agri_research_agent.application.international_spreads as application
from agri_research_agent.application.international_spreads import (
    InternationalSpreadReferenceError,
    MetricStatus,
    build_international_spread_payload,
    load_international_spread_reference_records,
)
from agri_research_agent.data_sources.lutou.three_oil_snapshot import (
    ThreeOilSnapshotError,
)
from agri_research_agent.research_data.canonical_spreads import CanonicalSpreadError
from agri_research_agent.research_data.three_oil_v1 import load_three_oil_v1


DISPLAY_DATES = tuple(date(year, 8, 10) for year in range(2021, 2027))


def _synthetic_records():
    catalog = load_three_oil_v1()
    return catalog, {
        item.series_id: [
            {
                "provider_series_id": item.provider_series_id,
                "business_date": business_date,
                "price": str(100 + index),
            }
            for index, business_date in enumerate(DISPLAY_DATES)
        ]
        for item in catalog.series
    }


def _metrics(payload):
    return [
        metric
        for section in payload.sections
        for row in section.rows
        for metric in row.metrics
    ]


def test_payload_has_all_31_approved_placements_in_sealed_row_order() -> None:
    catalog, records = _synthetic_records()
    palm = build_international_spread_payload(catalog, "palm", records)
    soy = build_international_spread_payload(catalog, "soy", records)
    rape = build_international_spread_payload(catalog, "rape", records)

    assert [len(row.metrics) for row in palm.sections[0].rows] == [3, 3, 1]
    assert [len(row.metrics) for row in soy.sections[0].rows] == [3, 3, 3]
    assert [len(row.metrics) for row in soy.sections[1].rows] == [3, 3]
    assert [len(row.metrics) for row in rape.sections[0].rows] == [3, 3, 3]
    assert (palm.metric_count, soy.metric_count, rape.metric_count) == (7, 15, 9)
    assert palm.metric_count + soy.metric_count + rape.metric_count == 31
    assert palm.source_summary == "Reuters / Oil World"
    assert palm.acquisition_summary == "人工快照"
    assert palm.as_of_date == date(2026, 8, 10)
    assert palm.metric_latest_dates == (date(2026, 8, 10),)
    pogo = palm.sections[0].rows[2].metrics[0]
    assert pogo.contract_id == "spread.energy.pogo.indonesia_cpo_ice_diesel"
    assert (pogo.row_index, pogo.column_index) == (3, 1)


def test_payload_reuses_contract_ids_and_excludes_rejected_metrics() -> None:
    catalog, records = _synthetic_records()
    payloads = [
        build_international_spread_payload(catalog, oil, records)
        for oil in ("palm", "soy", "rape")
    ]
    placements = [item for payload in payloads for item in _metrics(payload)]
    identities = {item.contract_id for item in placements}
    rendered_text = " ".join(
        f"{item.contract_id} {item.display_title} {item.formula_summary}"
        for item in placements
    ).lower()

    assert len(placements) == 31
    assert len(identities) < len(placements)
    assert "bloomberg" not in rendered_text
    assert "russia" not in rendered_text
    assert "ukraine" not in rendered_text
    assert "singapore" not in rendered_text


def test_units_ytd_provider_formula_and_fixed_assumption_come_from_contracts() -> None:
    catalog, records = _synthetic_records()
    soy = build_international_spread_payload(catalog, "soy", records)
    rape = build_international_spread_payload(catalog, "rape", records)
    soy_metrics = {item.contract_id: item for item in _metrics(soy)}
    rape_metrics = {item.contract_id: item for item in _metrics(rape)}

    assert {item.display_unit for item in soy_metrics.values()} == {
        "USD/T",
        "INR/T",
        "USC/LB",
        "USD/GAL",
    }
    assert {
        item.year_label
        for item in soy_metrics["market.environmental_credit.rin.d4"].observations
        if item.year == 2026
    } == {"2026 YTD"}
    assert (
        soy_metrics["spread.europe.rape_soy"].provider_summary
        == "Reuters + Oil World fallback"
    )
    boho = soy_metrics["spread.energy.boho.us_soy_ice_diesel"]
    assert "LGOc1" in boho.formula_summary
    assert "Heating Oil" not in boho.formula_summary
    assert "crude" not in boho.formula_summary.lower()
    freight = soy_metrics["spread.soybean_oil.cbot_board_argentina.freight_30"]
    assert freight.fixed_assumptions == (
        "30 USD/T fixed freight assumption",
    )
    rme = rape_metrics["market.biofuel.rme.europe.ara.spot"]
    assert "ARA" in " ".join(rme.leg_summary)
    assert "RED" not in " ".join(rme.leg_summary)
    assert "M1" not in " ".join(rme.leg_summary)


def test_exact_date_missing_observation_remains_missing() -> None:
    catalog, records = _synthetic_records()
    malaysia = "market.physical.palm_oil.malaysia.rbd_palm_oil.fob.p1"
    records[malaysia] = [
        item for item in records[malaysia] if item["business_date"].year != 2023
    ]

    payload = build_international_spread_payload(catalog, "palm", records)
    soy_palm = _metrics(payload)[0]

    assert 2023 not in soy_palm.available_years
    assert all(item.year != 2023 for item in soy_palm.observations)


def test_latest_value_and_date_come_from_the_same_exact_date_observation() -> None:
    catalog, records = _synthetic_records()
    for values in records.values():
        values.reverse()

    payload = build_international_spread_payload(catalog, "palm", records)

    for metric in _metrics(payload):
        latest = max(metric.observations, key=lambda item: item.business_date)
        assert metric.latest_observation_date == latest.business_date
        assert metric.latest_value == latest.value
        assert any(item.year_label == "2026 YTD" for item in metric.observations)


def test_hvo_dependency_is_automatically_suppressed_without_losing_positions() -> None:
    catalog, records = _synthetic_records()
    soy = build_international_spread_payload(catalog, "soy", records)
    rape = build_international_spread_payload(catalog, "rape", records)
    affected = [
        item
        for payload in (soy, rape)
        for item in _metrics(payload)
        if item.status is MetricStatus.SOURCE_DATA_UNDER_REVIEW
    ]

    assert (soy.metric_count, rape.metric_count) == (15, 9)
    assert [item.contract_id for item in affected] == [
        "spread.india.hvo_refined_soy",
        "spread.india.hvo_refined_soy",
        "spread.india.hvo_refined_palm",
    ]
    assert all(not item.observations for item in affected)
    assert all(item.latest_value is None for item in affected)
    assert all(item.latest_observation_date is None for item in affected)
    assert all(item.status is not MetricStatus.NO_DATA for item in affected)


def test_duplicate_conflict_isolated_to_affected_card() -> None:
    catalog, records = _synthetic_records()
    argentina = "market.physical.soybean_oil.argentina.upper_river.spot"
    records[argentina].append({**records[argentina][-1], "price": "999"})

    payload = build_international_spread_payload(catalog, "palm", records)
    metrics = _metrics(payload)

    assert metrics[0].status is MetricStatus.DUPLICATE_CONFLICT
    assert any(item.status is not MetricStatus.DUPLICATE_CONFLICT for item in metrics[1:])


def test_missing_series_isolated_to_affected_cards_as_unavailable() -> None:
    catalog, records = _synthetic_records()
    records.pop("market.physical.soybean_oil.argentina.upper_river.spot")

    payload = build_international_spread_payload(catalog, "palm", records)
    metrics = _metrics(payload)

    assert metrics[0].status is MetricStatus.REFERENCE_DATA_UNAVAILABLE
    assert "Series" in metrics[0].quality_summary
    assert any(item.status is MetricStatus.READY for item in metrics[1:])


def test_snapshot_adapter_failure_is_converted_at_application_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "snapshot.sql"
    source.write_text("not a valid sealed snapshot", encoding="utf-8")
    catalog = load_three_oil_v1()
    sentinel = {"called": False}

    monkeypatch.setattr(
        application,
        "resolve_international_spread_snapshot",
        lambda _catalog, _root: source,
    )

    def fail_adapter(_catalog, _source):
        sentinel["called"] = True
        raise ThreeOilSnapshotError("forced damaged snapshot")

    monkeypatch.setattr(
        "agri_research_agent.data_sources.lutou.three_oil_snapshot."
        "load_three_oil_snapshot_records",
        fail_adapter,
    )

    with pytest.raises(
        InternationalSpreadReferenceError,
        match="approved reference snapshot cannot be consumed",
    ):
        load_international_spread_reference_records(catalog, tmp_path)

    assert sentinel["called"] is True


def test_sealed_manifest_integrity_failure_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = load_three_oil_v1()

    def fail_integrity(_catalog, _root):
        raise CanonicalSpreadError("sealed integrity failure")

    monkeypatch.setattr(
        application,
        "resolve_international_spread_snapshot",
        fail_integrity,
    )

    with pytest.raises(CanonicalSpreadError, match="sealed integrity failure"):
        load_international_spread_reference_records(catalog, tmp_path)


def test_real_reference_payload_matches_all_sealed_latest_dates() -> None:
    root = os.getenv("SPREAD_REFERENCE_DATA_ROOT", "").strip()
    if not root:
        pytest.skip("SPREAD_REFERENCE_DATA_ROOT is required for read-only integration")
    catalog = load_three_oil_v1()
    records = load_international_spread_reference_records(catalog, root)

    for oil in ("palm", "soy", "rape"):
        payload = build_international_spread_payload(catalog, oil, records)
        for metric in _metrics(payload):
            if metric.status is MetricStatus.SOURCE_DATA_UNDER_REVIEW:
                assert not metric.observations
                assert metric.latest_observation_date is None
            else:
                assert metric.status is MetricStatus.READY
                assert metric.latest_observation_date == metric.expected_latest_date
                assert metric.latest_value is not None
