from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from agri_research_agent.market_data.contracts import ContractId, Exchange
from agri_research_agent.market_data.soybean_readiness import (
    ExistingInputReadiness,
    evaluate_soybean_market_input_readiness,
)
from agri_research_agent.market_data.soybean_shadow import (
    CbotSoybeanShadowQuote,
    FxSpotShadowQuote,
)


NOW = datetime(2026, 8, 16, 1, tzinfo=timezone.utc)
TARGET = date(2026, 8, 14)
NOV = ContractId(Exchange.CBOT, "SOYBEAN", 2026, 11)
JAN = ContractId(Exchange.CBOT, "SOYBEAN", 2027, 1)


def cbot(contract: ContractId, *, business_date: date = TARGET) -> CbotSoybeanShadowQuote:
    return CbotSoybeanShadowQuote(
        business_date=business_date,
        contract=contract,
        price=1_025.5,
        provider_dataset_id="tankan:market.foreign_futures_price_raw",
        provider_series_id="tankan.ffpr.cbot.soybean.en",
        source_updated_at=NOW,
        captured_at=NOW,
        source_snapshot_sha256="a" * 64,
        source_row_sha256="b" * 64,
        quality_status="valid",
        is_usable=True,
    )


def spot(*, quote_date: date = TARGET) -> FxSpotShadowQuote:
    return FxSpotShadowQuote(
        quote_date=quote_date,
        rate=7.1821,
        provider_dataset_id="tankan:market.exchange_rate",
        provider_series_id="tankan:market.exchange_rate:spot",
        source_updated_at=NOW,
        captured_at=NOW,
        source_snapshot_sha256="c" * 64,
        source_row_sha256="d" * 64,
        quality_status="valid",
        is_usable=True,
    )


def existing(name: str, ready: bool) -> ExistingInputReadiness:
    return ExistingInputReadiness(name=name, status="ready" if ready else "missing", ready=ready)


def evaluate(
    cbot_quotes: tuple[CbotSoybeanShadowQuote, ...],
    fx_quotes: tuple[FxSpotShadowQuote, ...],
    *,
    captured_at: datetime = NOW,
):
    return evaluate_soybean_market_input_readiness(
        target_business_date=TARGET,
        required_cbot_contracts=(NOV, JAN),
        cbot_quotes=cbot_quotes,
        fx_spot_quotes=fx_quotes,
        existing_dce_status=existing("DCE", True),
        existing_forward_fx_status=existing("FX_FORWARD", True),
        cnf_status=existing("CNF_MANUAL", True),
        captured_at=captured_at,
    )


def test_readiness_is_fail_closed_without_fill_or_contract_fallback() -> None:
    stale = cbot(JAN, business_date=date(2026, 8, 13))
    report = evaluate((cbot(NOV), stale), (spot(),))
    assert report.cbot.latest_source_date == TARGET
    assert report.cbot.available_contracts == (str(NOV),)
    assert report.cbot.missing_contracts == (str(JAN),)
    assert report.cbot.ready is False
    assert report.overall_ready is False
    assert "cbot_missing_contract:CBOT:SOYBEAN:2027-01" in report.blocking_reasons


def test_readiness_can_change_from_not_ready_to_ready_on_idempotent_rerun() -> None:
    first = evaluate((cbot(NOV),), ())
    repeated = evaluate((cbot(NOV),), ())
    later = evaluate(
        (cbot(NOV), cbot(JAN)),
        (spot(),),
        captured_at=NOW + timedelta(minutes=30),
    )
    assert first == repeated
    assert first.overall_ready is False
    assert later.overall_ready is True
    assert later.blocking_reasons == ()
    assert later.captured_at > first.captured_at
