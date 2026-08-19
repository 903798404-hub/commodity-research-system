from __future__ import annotations

import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest

import agri_research_agent.market_data.public_current as reader
from agri_research_agent.market_data.public_current import (
    PublicCurrentError,
    PublicCurrentErrorCode,
    PublicCurrentIdentity,
    PublicSeriesRequirement,
    load_three_oil_public_current,
)
from agri_research_agent.pipelines.lutou_goal_b import (
    CANONICAL_SCHEMA,
    GoalBCurrent,
)
from agri_research_agent.research_data.three_oil_v1 import load_three_oil_v1


DAY = date(2026, 8, 18)
SERIES_ID = "market.physical.soybean_oil.argentina.upper_river.spot"


def _row(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "lutou-three-oil-canonical/2",
        "dataset_id": "market.physical.soybean_oil.argentina.upper_river.spot",
        "series_id": SERIES_ID,
        "provider_dataset_id": "lutou:oils:world_oil_prices",
        "provider_series_id": "lutou:oils:world_oil_prices:approved",
        "provider": "Reuters",
        "metadata_source_type": "source_mapping",
        "metadata_status": "proven",
        "source_quote_unit": "US-$/T",
        "origin_system": "lutou",
        "acquisition_channel": "direct_database",
        "source_locator": "database:lutou/oils/world_oil_prices#approved",
        "business_date": DAY,
        "value": Decimal("1000"),
        "currency": "USD",
        "unit": "metric_tonne",
        "product": "Soybean Oil",
        "product_grade": "unspecified",
        "country": "Argentina",
        "region": "Upper River",
        "location_native": "Argentina Upper River",
        "quote_basis": "Spot",
        "tenor": "Spot",
        "price_type": "flat_price",
        "source_duplicate_count": 0,
        "source_group_sha256": "a" * 64,
        "source_policy_version": "three-oil-v1-sealed-direct-database/2",
        "captured_at": datetime(2026, 8, 19, tzinfo=timezone.utc),
    }
    value.update(changes)
    return value


def _current(tmp_path: Path, rows: list[dict[str, object]]) -> GoalBCurrent:
    return GoalBCurrent(
        "release-test",
        tmp_path,
        {
            "schema_version": "lutou-goal-b-current/2",
            "release_id": "release-test",
            "source_max_date": DAY.isoformat(),
            "scope": "three-oil-v1-sealed-series",
            "quality_status": "PASS",
            "series_count": 20,
        },
        pa.Table.from_pylist(rows, schema=CANONICAL_SCHEMA),
    )


def _install_current(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rows: list[dict[str, object]],
) -> None:
    identity = PublicCurrentIdentity(
        "release-test", "b" * 64, "lutou-goal-b-current/2", DAY
    )
    monkeypatch.setattr(
        reader,
        "_resolve_current",
        lambda _root: (identity, _current(tmp_path, rows)),
    )


def _requirement(**changes: str) -> PublicSeriesRequirement:
    value = {
        "series_id": SERIES_ID,
        "currency": "USD",
        "unit": "metric_tonne",
        "price_type": "flat_price",
    }
    value.update(changes)
    return PublicSeriesRequirement(**value)


def test_reader_returns_exact_series_and_current_traceability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_current(monkeypatch, tmp_path, [_row()])

    result = load_three_oil_public_current(tmp_path, [_requirement()])
    record = result.records_by_series_id[SERIES_ID][0]

    assert set(result.records_by_series_id) == {SERIES_ID}
    assert record["business_date"] == DAY
    assert record["price"] == Decimal("1000")
    assert record["value_semantics"] == "canonical"
    assert record["current_release_id"] == "release-test"
    assert record["current_manifest_sha256"] == "b" * 64


@pytest.mark.parametrize(
    ("changes", "requirement", "code"),
    [
        ({"currency": "INR"}, {}, PublicCurrentErrorCode.CURRENCY_MISMATCH),
        ({"unit": "US_cents_per_lb"}, {}, PublicCurrentErrorCode.UNIT_MISMATCH),
        (
            {"price_type": "basis"},
            {},
            PublicCurrentErrorCode.SERIES_METADATA_MISMATCH,
        ),
    ],
)
def test_reader_rejects_wrong_unit_currency_or_quote_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    changes: dict[str, object],
    requirement: dict[str, str],
    code: PublicCurrentErrorCode,
) -> None:
    _install_current(monkeypatch, tmp_path, [_row(**changes)])

    with pytest.raises(PublicCurrentError) as raised:
        load_three_oil_public_current(tmp_path, [_requirement(**requirement)])

    assert raised.value.code is code


def test_reader_requires_exact_series_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_current(
        monkeypatch,
        tmp_path,
        [_row(series_id="market.physical.soybean_oil.argentina.basis")],
    )

    with pytest.raises(PublicCurrentError) as raised:
        load_three_oil_public_current(tmp_path, [_requirement()])

    assert raised.value.code is PublicCurrentErrorCode.SERIES_NOT_FOUND


def test_missing_current_fails_closed_without_fallback(tmp_path: Path) -> None:
    with pytest.raises(PublicCurrentError) as raised:
        load_three_oil_public_current(tmp_path, [_requirement()])

    assert raised.value.code is PublicCurrentErrorCode.PUBLIC_CURRENT_UNAVAILABLE


def test_invalid_current_pointer_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "current.json").write_text("{}", encoding="utf-8")

    with pytest.raises(PublicCurrentError) as raised:
        load_three_oil_public_current(tmp_path, [_requirement()])

    assert raised.value.code is PublicCurrentErrorCode.INVALID_CURRENT_MANIFEST


def test_reader_has_no_database_or_sql_snapshot_fallback() -> None:
    source = Path(reader.__file__).read_text(encoding="utf-8")

    assert "load_current" in source
    assert "three_oil_live" not in source
    assert "three_oil_snapshot" not in source
    assert "pymysql" not in source
    assert ".sql" not in source


def test_real_current_resolves_all_approved_consumer_series() -> None:
    runtime_root = os.getenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", "").strip()
    if not runtime_root:
        pytest.skip("PUBLIC_MARKET_DATA_RUNTIME_ROOT is required for integration")
    catalog = load_three_oil_v1()
    requirements = tuple(
        PublicSeriesRequirement(
            item.series_id, item.currency, item.unit, item.price_type
        )
        for item in catalog.series
    )

    result = load_three_oil_public_current(
        Path(runtime_root) / "public-market-data" / "lutou-three-oil",
        requirements,
    )

    assert set(result.records_by_series_id) == {
        item.series_id for item in catalog.series
    }
    assert len(result.records_by_series_id) == 20
    assert result.identity.release_id
    assert len(result.identity.manifest_sha256) == 64

    approved = {
        series_id: result.records_by_series_id[series_id][0]
        for series_id in (
            "market.physical.soybean_oil.argentina.upper_river.spot",
            "market.physical.soybean_oil.netherlands.ex_mill_fob",
            "market.physical.rapeseed_oil.europe.spot",
            "market.physical.palm_oil.europe_nwe.rbd_palm_oil.cif.spot",
            "market.physical.sunflower_oil.europe.six_ports.ex_tank.spot",
        )
    }
    assert approved[
        "market.physical.soybean_oil.argentina.upper_river.spot"
    ]["provider"] == "Reuters"
    assert approved[
        "market.physical.soybean_oil.argentina.upper_river.spot"
    ]["location_native"] == "Upper River"
    assert approved[
        "market.physical.soybean_oil.netherlands.ex_mill_fob"
    ]["provider"] == "Oil World"
    assert approved[
        "market.physical.rapeseed_oil.europe.spot"
    ]["product_grade"] == "UNKNOWN"
    assert approved[
        "market.physical.palm_oil.europe_nwe.rbd_palm_oil.cif.spot"
    ]["product"] == "RBD Palm Oil"
    assert approved[
        "market.physical.palm_oil.europe_nwe.rbd_palm_oil.cif.spot"
    ]["quote_basis"] == "CIF"
    assert approved[
        "market.physical.sunflower_oil.europe.six_ports.ex_tank.spot"
    ]["quote_basis"] == "Ex-tank"
    assert {
        (item["currency"], item["unit"])
        for item in approved.values()
    } == {("USD", "metric_tonne")}

    flat = result.records_by_series_id[
        "market.physical.soybean_oil.argentina.upper_river.spot"
    ][0]
    basis = result.records_by_series_id[
        "market.basis.soybean_oil.argentina.upper_river.spot"
    ][0]
    assert (flat["price_type"], flat["unit"]) == (
        "Physical Spot Flat Price",
        "metric_tonne",
    )
    assert (basis["price_type"], basis["unit"]) == (
        "Physical Spot Basis",
        "US_cents_per_lb",
    )
