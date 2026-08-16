from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from agri_research_agent.data_sources.lutou.three_oil_snapshot import (
    ThreeOilSnapshotError,
    load_three_oil_snapshot_records,
)
from agri_research_agent.research_data.three_oil_v1 import load_three_oil_v1


def _catalog_for_payload(payload: bytes):
    catalog = load_three_oil_v1()
    selected_ids = (
        "market.physical.soybean_oil.argentina.upper_river.spot",
        "market.basis.soybean_oil.argentina.upper_river.spot",
    )
    return replace(
        catalog,
        series=tuple(catalog.series_by_id(item) for item in selected_ids),
        source_snapshot_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _snapshot_payload() -> bytes:
    return (
        "CREATE TABLE `阿根廷_豆油_价格`  (\n"
        "  `Date` datetime NULL,\n"
        "  `豆油_现货价格_阿根廷_上河 (USD/T)` double NULL,\n"
        "  `豆油_现货基差_阿根廷_上河 (USC/LB)` double NULL\n"
        ");\n"
        "INSERT INTO `阿根廷_豆油_价格` VALUES "
        "('2026-08-10 00:00:00', 1000, -1450);\n"
    ).encode("utf-8")


def test_snapshot_adapter_reads_only_sealed_fields_and_preserves_raw_scale(
    tmp_path: Path,
) -> None:
    payload = _snapshot_payload()
    source = tmp_path / "snapshot.sql"
    source.write_bytes(payload)
    catalog = _catalog_for_payload(payload)

    records = load_three_oil_snapshot_records(catalog, source)

    assert set(records) == {item.series_id for item in catalog.series}
    flat = records["market.physical.soybean_oil.argentina.upper_river.spot"]
    basis = records["market.basis.soybean_oil.argentina.upper_river.spot"]
    assert flat[0]["price"] == Decimal("1000")
    assert basis[0]["price"] == Decimal("-1450")
    assert flat[0]["business_date"].isoformat() == "2026-08-10"


def test_snapshot_adapter_fails_closed_on_hash_drift(tmp_path: Path) -> None:
    payload = _snapshot_payload()
    source = tmp_path / "snapshot.sql"
    source.write_bytes(payload + b"\n")
    catalog = _catalog_for_payload(payload)

    with pytest.raises(ThreeOilSnapshotError, match="hash does not match"):
        load_three_oil_snapshot_records(catalog, source)


def test_snapshot_adapter_does_not_execute_sql(tmp_path: Path) -> None:
    source = (
        Path(__file__).resolve().parents[3]
        / "03_src/agri_research_agent/data_sources/lutou/three_oil_snapshot.py"
    )
    text = source.read_text(encoding="utf-8")

    assert "sqlalchemy" not in text.lower()
    assert ".execute(" not in text
    assert "connect(" not in text
    assert "open(\"wb\"" not in text
