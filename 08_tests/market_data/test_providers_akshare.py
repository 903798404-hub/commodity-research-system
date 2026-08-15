from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from agri_research_agent.market_data.contracts import ContractId, ContinuousInstrumentId, Exchange
from agri_research_agent.market_data.providers.akshare import AkShareAdapterConfig, AkShareQuoteAdapter
from agri_research_agent.market_data.quotes import Currency, PriceType, PriceUnit, TradingSession


def adapter() -> AkShareQuoteAdapter:
    return AkShareQuoteAdapter(
        AkShareAdapterConfig(
            exchange=Exchange.DCE,
            price_type=PriceType.LAST,
            session=TradingSession.NIGHT,
            currency=Currency.CNY,
            unit=PriceUnit.CNY_PER_METRIC_TONNE,
            source="akshare-fixture",
            source_identity="fixture-sha256:abc",
            captured_at=datetime(2026, 8, 15, 12, tzinfo=timezone.utc),
        )
    )


def evidence_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["P2609", "P0"],
            "business_date": ["2026-08-14", "2026-08-14"],
            "price": [8120.0, 8120.0],
            "observed_at": ["2026-08-15T12:00:00+00:00", None],
        }
    )


def test_fixture_frame_preserves_raw_symbol_evidence_and_identity() -> None:
    quotes = adapter().from_frame(evidence_frame())
    assert quotes[0].instrument == ContractId(Exchange.DCE, "P", 2026, 9)
    assert quotes[1].instrument == ContinuousInstrumentId(Exchange.DCE, "P")
    assert "raw_symbol=P2609" in quotes[0].source_identity


def test_only_explicitly_injected_callable_is_invoked() -> None:
    calls = 0

    def supplied_fixture() -> pd.DataFrame:
        nonlocal calls
        calls += 1
        return evidence_frame()

    assert len(adapter().from_callable(supplied_fixture)) == 2
    assert calls == 1


def test_missing_provider_evidence_has_no_fallback() -> None:
    with pytest.raises(ValueError, match="columns missing"):
        adapter().from_frame(evidence_frame().drop(columns=["price"]))


def test_invalid_price_is_not_calculated_or_promoted() -> None:
    frame = evidence_frame()
    frame.loc[0, "price"] = None
    with pytest.raises(ValueError, match="invalid AkShare evidence row"):
        adapter().from_frame(frame)
