from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from agri_research_agent.market_data.calendars import DeterministicBusinessDayPolicy
from agri_research_agent.market_data.intraday import MarketSession
from agri_research_agent.pipelines.public_intraday import (
    PublicIntradayCaptureError,
    PublicIntradaySourceIncompleteError,
    PublicIntradaySourceStaleError,
    RequiredIntradayContracts,
    capture_public_intraday_once,
)


CN = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 8, 28)
REQUIRED = RequiredIntradayContracts(("2609", "2701"), ("M2609", "M2701"), ("Y2609", "Y2701"))


class FakeClient:
    def __init__(self, updated: datetime, *, missing: str | None = None, null_price: str | None = None) -> None:
        self.updated = updated
        self.missing = missing
        self.null_price = null_price

    def stream_live(self, query, contracts=()):  # type: ignore[no-untyped-def]
        name = query.name
        if "foreign_futures" in name:
            rows = [
                {"exchange": "CBOT", "product_name": "大豆", "contract": code,
                 "last": None if self.null_price == f"C{code}" else 1200.0,
                 "ric": f"R{code}", "update_time": self.updated}
                for code in contracts if self.missing != f"C{code}"
            ]
        elif "soymeal" in name:
            rows = [self._dce("豆粕", code, "M") for code in contracts if self.missing != f"M{code}"]
        elif "soyoil" in name:
            rows = [self._dce("豆油", code, "Y") for code in contracts if self.missing != f"Y{code}"]
        else:
            rows = [{"tenor": "spot", "bid": 6.719, "ask": 6.721, "mid": 6.72,
                     "value_date": DAY, "update_time": self.updated}]
        return iter((SimpleNamespace(rows=tuple(rows)),))

    def _dce(self, product: str, code: str, prefix: str) -> dict[str, object]:
        return {"product_name": product, "contract": code, "bid": 3000.0, "ask": 3002.0,
                "last": None if self.null_price == f"{prefix}{code}" else 3001.0,
                "volume": 1, "open_interest": 2, "update_time": self.updated}


def capture(session: MarketSession, captured: datetime, updated: datetime, **kwargs):  # type: ignore[no-untyped-def]
    return capture_public_intraday_once(
        FakeClient(updated, **kwargs), business_date=DAY, session=session,
        captured_at=captured, required=REQUIRED,
        business_day_policy=DeterministicBusinessDayPolicy(frozenset({DAY})),
    ).snapshot


def test_am_exact_contracts_and_spot_mid_are_canonical() -> None:
    snapshot = capture(
        MarketSession.AM,
        datetime(2026, 8, 28, 9, 15, tzinfo=CN),
        datetime(2026, 8, 28, 9, 13),
    )
    assert {q.contract_code for q in snapshot.quotes} == {
        "2609", "2701", "M2609", "M2701", "Y2609", "Y2701", "",
    }
    fx = next(q for q in snapshot.quotes if q.instrument_id == "FX:USD/CNH:SPOT")
    assert (fx.price, fx.quote_type, fx.provenance["bid"], fx.provenance["ask"]) == (
        6.72, "MID", 6.719, 6.721,
    )
    assert all(q.source_table.startswith("market.") for q in snapshot.quotes)


@pytest.mark.parametrize(
    ("missing", "contract_code"),
    [("C2609", "2609"), ("M2609", "M2609"), ("Y2701", "Y2701")],
)
def test_missing_exact_contract_is_sealed_as_unavailable_evidence(
    missing: str,
    contract_code: str,
) -> None:
    snapshot = capture(
        MarketSession.AM,
        datetime(2026, 8, 28, 9, 15, tzinfo=CN),
        datetime(2026, 8, 28, 9, 13),
        missing=missing,
    )
    assert contract_code not in {quote.contract_code for quote in snapshot.quotes}
    evidence = next(
        item for item in snapshot.unavailable_instruments
        if item.contract_code == contract_code
    )
    assert evidence.status.value == "CONTRACT_NOT_AVAILABLE"
    assert evidence.provenance["listing_status"] == "UNVERIFIED"


@pytest.mark.parametrize(
    ("session", "hour", "minute", "allowed"),
    [
        (MarketSession.AM, 7, 59, False),
        (MarketSession.AM, 8, 0, True),
        (MarketSession.AM, 10, 30, True),
        (MarketSession.AM, 11, 59, True),
        (MarketSession.AM, 12, 0, False),
        (MarketSession.PM, 14, 59, False),
        (MarketSession.PM, 15, 0, True),
        (MarketSession.PM, 18, 30, True),
        (MarketSession.PM, 20, 59, True),
        (MarketSession.PM, 21, 0, False),
    ],
)
def test_manual_capture_window_boundaries(
    session: MarketSession,
    hour: int,
    minute: int,
    allowed: bool,
) -> None:
    captured_at = datetime(2026, 8, 28, hour, minute, tzinfo=CN)
    updated_at = datetime(2026, 8, 28, 0, 1)
    if allowed:
        snapshot = capture(session, captured_at, updated_at)
        assert snapshot.captured_at == captured_at
    else:
        with pytest.raises(PublicIntradayCaptureError, match="outside its manual window"):
            capture(session, captured_at, updated_at)


def test_different_same_day_source_times_do_not_fail_a_manual_observation() -> None:
    am = capture(
        MarketSession.AM,
        datetime(2026, 8, 28, 10, 30, tzinfo=CN),
        datetime(2026, 8, 28, 8, 5),
    )
    pm = capture(
        MarketSession.PM,
        datetime(2026, 8, 28, 18, 30, tzinfo=CN),
        datetime(2026, 8, 28, 15, 1),
    )
    assert am.captured_at.hour == 10
    assert pm.captured_at.hour == 18


def test_pm_accepts_post_close_exact_contracts_and_null_price_fails() -> None:
    snapshot = capture(
        MarketSession.PM,
        datetime(2026, 8, 28, 15, 15, tzinfo=CN),
        datetime(2026, 8, 28, 15, 13),
    )
    assert snapshot.session is MarketSession.PM
    with pytest.raises(PublicIntradaySourceIncompleteError):
        capture(
            MarketSession.PM,
            datetime(2026, 8, 28, 15, 15, tzinfo=CN),
            datetime(2026, 8, 28, 15, 13),
            null_price="M2609",
        )


def test_source_date_is_not_derived_into_business_date() -> None:
    with pytest.raises(PublicIntradaySourceStaleError):
        capture(
            MarketSession.AM,
            datetime(2026, 8, 28, 9, 15, tzinfo=CN),
            datetime(2026, 8, 27, 21, 3),
        )
