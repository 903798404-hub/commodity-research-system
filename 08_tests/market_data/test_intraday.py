from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agri_research_agent.market_data.intraday import (
    FreshnessStatus,
    InstrumentAvailabilityStatus,
    IntradayQuote,
    IntradaySnapshot,
    IntradaySnapshotConflictError,
    IntradayUnavailableInstrument,
    MarketSession,
    SealStatus,
    load_intraday_snapshot,
    load_latest_intraday_snapshot,
    quote_by_instrument,
    seal_intraday_snapshot,
)


CN = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 8, 28)


def quote(session: MarketSession, captured: datetime, *, price: float = 1250.0) -> IntradayQuote:
    return IntradayQuote(
        business_date=DAY,
        session=session,
        captured_at=captured,
        instrument_id="CBOT:SOYBEAN:2026-09",
        contract_code="2609",
        exchange="CBOT",
        product="SOYBEAN",
        price=price,
        quote_type="LAST",
        currency="USD",
        unit="US_CENTS_PER_BUSHEL",
        source_system="TANKAN",
        source_table="market.foreign_futures_live",
        source_updated_at=captured,
        source_trade_date=None,
        freshness_status=FreshnessStatus.FRESH,
        provenance={"ric": "SU26", "source_trade_date_status": "NOT_PROVIDED"},
    )


def snapshot(session: MarketSession, hour: int, minute: int, *, price: float = 1250.0) -> IntradaySnapshot:
    captured = datetime(2026, 8, 28, hour, minute, tzinfo=CN)
    return IntradaySnapshot(
        DAY,
        session,
        captured,
        (quote(session, captured, price=price),),
        "fixture-calendar-v1",
        "fixture",
    )


def test_same_day_am_pm_are_immutable_and_session_indexed(tmp_path: Path) -> None:
    am = seal_intraday_snapshot(tmp_path, snapshot(MarketSession.AM, 9, 15))
    pm = seal_intraday_snapshot(tmp_path, snapshot(MarketSession.PM, 15, 15, price=1260.0))
    assert am.status is SealStatus.SEALED
    assert pm.status is SealStatus.SEALED
    assert am.release_dir != pm.release_dir
    assert load_intraday_snapshot(tmp_path, DAY, "AM").quotes[0].price == 1250.0
    assert load_intraday_snapshot(tmp_path, DAY, "PM").quotes[0].price == 1260.0
    assert load_latest_intraday_snapshot(tmp_path, "AM").session is MarketSession.AM
    assert load_latest_intraday_snapshot(tmp_path, "PM").session is MarketSession.PM
    index = json.loads((tmp_path / "intraday_index.json").read_text(encoding="utf-8"))
    assert set(index["by_date"][DAY.isoformat()]) == {"AM", "PM"}


def test_repeat_same_content_is_no_change_but_different_content_conflicts(tmp_path: Path) -> None:
    first = snapshot(MarketSession.AM, 9, 15)
    assert seal_intraday_snapshot(tmp_path, first).status is SealStatus.SEALED
    later_capture = snapshot(MarketSession.AM, 9, 16)
    later_quote = replace(
        later_capture.quotes[0],
        source_updated_at=first.quotes[0].source_updated_at,
    )
    later_capture = replace(later_capture, quotes=(later_quote,))
    assert seal_intraday_snapshot(tmp_path, later_capture).status is SealStatus.NO_CHANGE
    with pytest.raises(IntradaySnapshotConflictError):
        seal_intraday_snapshot(tmp_path, snapshot(MarketSession.AM, 9, 15, price=1251.0))


def test_fx_empty_contract_has_stable_identity(tmp_path: Path) -> None:
    captured = datetime(2026, 8, 28, 9, 15, tzinfo=CN)
    fx = IntradayQuote(
        DAY, MarketSession.AM, captured, "FX:USD/CNH:SPOT", "", "OTC", "USD/CNH",
        6.72, "MID", "CNH", "CNH_PER_USD", "TANKAN", "market.exchange_rate_live",
        captured, DAY, FreshnessStatus.FRESH, {"bid": 6.7198, "ask": 6.7202},
    )
    snap = IntradaySnapshot(DAY, MarketSession.AM, captured, (fx,), "fixture", "fixture")
    seal_intraday_snapshot(tmp_path, snap)
    loaded = load_intraday_snapshot(tmp_path, DAY, "AM")
    assert quote_by_instrument(loaded, "FX:USD/CNH:SPOT").quote_type == "MID"


def test_unavailable_instrument_evidence_is_immutable_and_manifested(tmp_path: Path) -> None:
    base = snapshot(MarketSession.AM, 9, 15)
    unavailable = IntradayUnavailableInstrument(
        instrument_id="DCE:SOYMEAL:2028-01",
        contract_code="M2801",
        exchange="DCE",
        product="SOYMEAL",
        status=InstrumentAvailabilityStatus.CONTRACT_NOT_AVAILABLE,
        reason="REQUESTED_EXACT_CONTRACT_ABSENT_FROM_LIVE_SOURCE",
        provenance={"source_table": "market.futures_live", "listing_status": "UNVERIFIED"},
    )
    seal = seal_intraday_snapshot(
        tmp_path, replace(base, unavailable_instruments=(unavailable,))
    )
    manifest = json.loads(
        (seal.release_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["available_instruments"] == [
        {"instrument_id": "CBOT:SOYBEAN:2026-09", "contract_code": "2609"}
    ]
    assert manifest["requested_but_unavailable"][0]["contract_code"] == "M2801"
    loaded = load_intraday_snapshot(tmp_path, DAY, MarketSession.AM)
    assert loaded.unavailable_instruments == (unavailable,)
