from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.market_data.contracts import ContractId, Exchange
from agri_research_agent.market_data.loader import (
    MarketDataSchemaError,
    STANDARD_QUOTE_COLUMNS,
    load_market_quotes_frame,
    load_market_quotes_parquet,
)


def standard_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "schema_version": 1,
                "exchange": "DCE",
                "product": "P",
                "instrument_type": "DELIVERY_CONTRACT",
                "contract_year": 2026,
                "contract_month": 9,
                "business_date": "2026-08-14",
                "price": 8120.0,
                "price_type": "CLOSE",
                "session": "DAY",
                "currency": "CNY",
                "unit": "CNY/metric_tonne",
                "source": "fixture",
                "captured_at": "2026-08-15T12:00:00+00:00",
                "source_identity": "sha256:abc",
                "observed_at": None,
            }
        ],
        columns=STANDARD_QUOTE_COLUMNS,
    )


def test_loader_converts_only_standardized_schema() -> None:
    quotes = load_market_quotes_frame(standard_frame())
    assert quotes[0].instrument == ContractId(Exchange.DCE, "P", 2026, 9)


def test_loader_rejects_missing_or_extra_schema_columns() -> None:
    missing = standard_frame().drop(columns=["source_identity"])
    with pytest.raises(MarketDataSchemaError, match="schema mismatch"):
        load_market_quotes_frame(missing)


def test_loader_never_truncates_fractional_contract_identity() -> None:
    frame = standard_frame()
    frame["contract_year"] = frame["contract_year"].astype(float)
    frame.loc[0, "contract_year"] = 2026.5
    with pytest.raises(MarketDataSchemaError, match="contract_year must be an integer"):
        load_market_quotes_frame(frame)


def test_loader_rejects_duplicate_business_key_even_if_capture_time_differs() -> None:
    frame = pd.concat([standard_frame(), standard_frame()], ignore_index=True)
    frame.loc[1, "captured_at"] = "2026-08-15T13:00:00+00:00"
    with pytest.raises(MarketDataSchemaError, match="duplicate quote business key"):
        load_market_quotes_frame(frame)


def test_loader_reads_only_explicit_parquet_path(tmp_path: Path) -> None:
    path = tmp_path / "quotes.parquet"
    standard_frame().to_parquet(path, index=False)
    assert len(load_market_quotes_parquet(path)) == 1
    with pytest.raises(FileNotFoundError, match="explicit"):
        load_market_quotes_parquet(tmp_path / "missing.parquet")
