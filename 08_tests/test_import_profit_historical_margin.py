from __future__ import annotations

from datetime import date
import hashlib
from pathlib import Path

from agri_research_agent.import_profit.historical_margin import (
    load_historical_margin_index,
)
from agri_research_agent.import_profit.query import (
    QueryMetric,
    load_soybean_query_dataset,
)
from agri_research_agent.import_profit.seasonality import (
    build_all_shipment_month_seasonality,
)
from test_import_profit_query import joined_rows, write_dataset


def test_lightweight_margin_provider_matches_legacy_seasonality(
    tmp_path: Path,
) -> None:
    rows = [
        joined_rows(
            date(year, 6, 15),
            "brazil",
            year,
            7,
            cnf=100.0,
            duty=3000.0,
            margin=float(year - 2020),
        )
        for year in range(2021, 2027)
    ]
    paths = write_dataset(tmp_path, rows)
    legacy = load_soybean_query_dataset(*paths)
    result_path = paths[2]
    source_sha = hashlib.sha256(result_path.read_bytes()).hexdigest().upper()
    lightweight = load_historical_margin_index(
        result_path,
        source_sha256=source_sha,
        origin="brazil",
        as_of_date=date(2026, 6, 25),
    )
    expected = build_all_shipment_month_seasonality(
        legacy,
        origin="brazil",
        as_of_date=date(2026, 6, 25),
        metric=QueryMetric.NET_CRUSH_MARGIN,
    )
    actual = build_all_shipment_month_seasonality(
        lightweight,
        origin="brazil",
        as_of_date=date(2026, 6, 25),
        metric=QueryMetric.NET_CRUSH_MARGIN,
    )
    assert actual == expected
    assert lightweight.prepared_row_count == len(rows)


def test_lightweight_margin_provider_rejects_invalid_source_identity(
    tmp_path: Path,
) -> None:
    paths = write_dataset(
        tmp_path,
        [joined_rows(date(2026, 6, 15), "brazil", 2026, 7)],
    )
    try:
        load_historical_margin_index(
            paths[2],
            source_sha256="invalid",
            origin="brazil",
            as_of_date=date(2026, 6, 25),
        )
    except ValueError as exc:
        assert "identity is invalid" in str(exc)
    else:
        raise AssertionError("wrong source identity must fail closed")
