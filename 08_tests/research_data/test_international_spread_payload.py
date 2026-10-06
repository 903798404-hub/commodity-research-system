from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import pyarrow as pa
import pyarrow.compute as pc

import agri_research_agent.application.international_spreads as application
from agri_research_agent.application.international_spreads import (
    InternationalSpreadReferenceError,
    MetricStatus,
    build_international_spread_payload,
    build_international_spread_payload_from_current_table,
    load_international_spread_public_current,
    load_international_spread_public_current_table,
    load_international_spread_reference_records,
)
from agri_research_agent.data_sources.lutou.three_oil_snapshot import (
    ThreeOilSnapshotError,
)
from agri_research_agent.research_data.canonical_spreads import CanonicalSpreadError
from agri_research_agent.research_data.three_oil_v1 import (
    load_three_oil_v1,
    resolve_series_observations,
)
from agri_research_agent.market_data.public_current import (
    PublicCurrentIdentity,
    PublicCurrentTableSnapshot,
)


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


def test_oil_page_dependency_projection_uses_only_required_source_series() -> None:
    catalog = load_three_oil_v1()

    selected = {
        oil: application._required_source_series_ids(catalog, oil)
        for oil in ("palm", "soy", "rape")
    }

    assert {oil: len(series_ids) for oil, series_ids in selected.items()} == {
        "palm": 8,
        "soy": 16,
        "rape": 11,
    }
    all_series = {item.series_id for item in catalog.series}
    assert all(series_ids < all_series for series_ids in selected.values())


def test_columnar_payload_rejects_missing_required_column() -> None:
    catalog = load_three_oil_v1()
    snapshot = PublicCurrentTableSnapshot(
        PublicCurrentIdentity(
            "release", "a" * 64, "lutou-goal-b-current/2", date(2026, 8, 18)
        ),
        pa.table(
            {
                "series_id": pa.array([], type=pa.string()),
                "business_date": pa.array([], type=pa.date32()),
            }
        ),
        0,
    )

    with pytest.raises(CanonicalSpreadError, match="schema is invalid"):
        build_international_spread_payload_from_current_table(
            catalog, "palm", snapshot
        )


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


def test_columnar_path_preserves_missing_dates_and_sorts_unsorted_input() -> None:
    catalog, source_records = _synthetic_records()
    future_date = date(2026, 8, 24)
    extra_historical_date = date(2020, 8, 10)
    for values in source_records.values():
        values.extend(
            (
                {
                    **values[-1],
                    "business_date": extra_historical_date,
                    "price": "90",
                },
                {
                    **values[-1],
                    "business_date": future_date,
                    "price": "120",
                },
            )
        )
    malaysia = "market.physical.palm_oil.malaysia.rbd_palm_oil.fob.p1"
    source_records[malaysia] = [
        item
        for item in source_records[malaysia]
        if item["business_date"].year != 2023
    ]
    canonical_records: dict[str, list[dict[str, object]]] = {}
    table_rows: list[dict[str, object]] = []
    for definition in catalog.series:
        observations = resolve_series_observations(
            catalog, definition.series_id, source_records
        )
        canonical_records[definition.series_id] = [
            {
                "provider_series_id": definition.provider_series_id,
                "business_date": item.business_date,
                "price": item.value,
                "value_semantics": "canonical",
            }
            for item in observations
        ]
        table_rows.extend(
            {
                "series_id": definition.series_id,
                "business_date": item.business_date,
                "value": item.value,
            }
            for item in observations
        )
    table_rows.reverse()
    snapshot = PublicCurrentTableSnapshot(
        PublicCurrentIdentity(
            "future-release", "c" * 64, "lutou-goal-b-current/2", future_date
        ),
        pa.Table.from_pylist(
            table_rows,
            schema=pa.schema(
                (
                    pa.field("series_id", pa.string(), nullable=False),
                    pa.field("business_date", pa.date32(), nullable=False),
                    pa.field("value", pa.decimal256(40, 20), nullable=False),
                )
            ),
        ),
        len(table_rows),
    )

    before = build_international_spread_payload(
        catalog,
        "palm",
        canonical_records,
        current_identity=snapshot.identity,
        acquisition_summary="Public Current",
    )
    after = build_international_spread_payload_from_current_table(
        catalog,
        "palm",
        snapshot,
        acquisition_summary="Public Current",
    )

    assert after == before
    assert after.current_identity == snapshot.identity
    assert after.as_of_date == future_date
    soy_palm = _metrics(after)[0]
    assert 2023 not in soy_palm.available_years
    assert extra_historical_date not in {
        item.business_date for item in soy_palm.observations
    }
    assert list(soy_palm.observations) == sorted(
        soy_palm.observations, key=lambda item: item.business_date
    )


def test_public_current_canonical_values_are_not_converted_twice() -> None:
    catalog, records = _synthetic_records()
    series_id = "market.basis.soybean_oil.argentina.upper_river.spot"
    records[series_id] = [
        {
            "provider_series_id": catalog.series_by_id(series_id).provider_series_id,
            "business_date": date(2026, 8, 10),
            "price": Decimal("1.25"),
            "value_semantics": "canonical",
        }
    ]

    observations = resolve_series_observations(catalog, series_id, records)

    assert observations[0].value == Decimal("1.25")


def test_mixed_source_native_and_canonical_values_fail_closed() -> None:
    catalog, records = _synthetic_records()
    series_id = "market.basis.soybean_oil.argentina.upper_river.spot"
    records[series_id][0]["value_semantics"] = "canonical"

    with pytest.raises(CanonicalSpreadError, match="mix canonical"):
        resolve_series_observations(catalog, series_id, records)


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


def test_real_reference_payload_matches_all_sealed_latest_dates(tmp_path) -> None:
    from three_oil_reference_fixture import reference_pair
    # Preserve the historical node identity; this is now a sealed offline fixture.
    root = os.getenv("INTERNATIONAL_SPREAD_LEGACY_REFERENCE_ROOT", "").strip()
    if root:
        catalog = load_three_oil_v1()
    else:
        catalog, root, _public = reference_pair(tmp_path)
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


def test_columnar_payload_exactly_matches_generic_public_current_payload() -> None:
    runtime_root = os.getenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", "").strip()
    if not runtime_root:
        pytest.skip("PUBLIC_MARKET_DATA_RUNTIME_ROOT is required for integration")
    current_root = Path(runtime_root) / "public-market-data" / "lutou-three-oil"
    catalog = load_three_oil_v1()

    for oil in ("palm", "soy", "rape"):
        generic = load_international_spread_public_current(
            catalog, current_root, oil
        )
        columnar = load_international_spread_public_current_table(
            catalog, current_root, oil
        )
        before = build_international_spread_payload(
            catalog,
            oil,
            generic.records_by_series_id,
            current_identity=generic.identity,
            acquisition_summary="Public Current",
        )
        after = build_international_spread_payload_from_current_table(
            catalog,
            oil,
            columnar,
            acquisition_summary="Public Current",
        )

        assert after == before
        assert columnar.observations.column_names == [
            "series_id", "business_date", "value"
        ]
        assert columnar.observations.num_rows < columnar.current_row_count
        for series_id in application._required_source_series_ids(catalog, oil):
            generic_values = {
                item.business_date: item.value
                for item in resolve_series_observations(
                    catalog, series_id, generic.records_by_series_id
                )
            }
            selected = columnar.observations.filter(
                pc.equal(columnar.observations["series_id"], series_id)
            )
            assert dict(
                zip(
                    selected["business_date"].to_pylist(),
                    selected["value"].to_pylist(),
                    strict=True,
                )
            ) == generic_values


def test_real_legacy_and_public_current_match_on_every_common_observation(tmp_path) -> None:
    from three_oil_reference_fixture import reference_pair
    legacy_root = os.getenv("INTERNATIONAL_SPREAD_LEGACY_REFERENCE_ROOT", "").strip()
    if legacy_root:
        runtime_root = os.getenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", "").strip()
        assert runtime_root, "explicit legacy parity also requires a Public Current root"
        catalog = load_three_oil_v1()
    else:
        catalog, legacy_root, runtime_root = reference_pair(tmp_path)
    legacy = load_international_spread_reference_records(catalog, legacy_root)
    current = load_international_spread_public_current(
        catalog,
        Path(runtime_root) / "public-market-data" / "lutou-three-oil",
    )

    for contract in catalog.series:
        legacy_values = {
            item.business_date: item.value
            for item in resolve_series_observations(
                catalog, contract.series_id, legacy
            )
        }
        public_values = {
            item.business_date: item.value
            for item in resolve_series_observations(
                catalog, contract.series_id, current.records_by_series_id
            )
        }
        common = legacy_values.keys() & public_values.keys()
        assert common
        assert {
            business_date: legacy_values[business_date]
            for business_date in common
        } == {
            business_date: public_values[business_date]
            for business_date in common
        }

    for oil in ("palm", "soy", "rape"):
        legacy_payload = build_international_spread_payload(catalog, oil, legacy)
        public_payload = build_international_spread_payload(
            catalog,
            oil,
            current.records_by_series_id,
            current_identity=current.identity,
            acquisition_summary="Public Current",
        )
        assert public_payload.current_identity == current.identity
        assert public_payload.acquisition_summary == "Public Current"
        legacy_metrics = _metrics(legacy_payload)
        public_metrics = _metrics(public_payload)
        assert len(legacy_metrics) == len(public_metrics)
        for before, after in zip(legacy_metrics, public_metrics, strict=True):
            assert (
                before.metric_type,
                before.contract_id,
                before.display_title,
                before.display_unit,
                before.row_index,
                before.column_index,
                before.provider_summary,
                before.formula_summary,
                before.leg_summary,
                before.fixed_assumptions,
                before.definition_evidence,
            ) == (
                after.metric_type,
                after.contract_id,
                after.display_title,
                after.display_unit,
                after.row_index,
                after.column_index,
                after.provider_summary,
                after.formula_summary,
                after.leg_summary,
                after.fixed_assumptions,
                after.definition_evidence,
            )
            before_values = {
                item.business_date: item.value for item in before.observations
            }
            after_values = {
                item.business_date: item.value for item in after.observations
            }
            common = before_values.keys() & after_values.keys()
            assert {
                business_date: before_values[business_date]
                for business_date in common
            } == {
                business_date: after_values[business_date]
                for business_date in common
            }
