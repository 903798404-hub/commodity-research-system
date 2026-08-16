from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agri_research_agent.market_data.contracts import (
    ChinaFuturesSymbolParser,
    ContractId,
    ContinuousInstrumentId,
    Exchange,
    InstrumentType,
    StandardInstrumentFormatter,
    parse_standard_instrument,
)


def test_delivery_contract_has_canonical_full_identity() -> None:
    contract = ContractId(Exchange.DCE, "m", 2026, 9)
    assert str(contract) == "DCE:M:2026-09"
    assert contract.instrument_type is InstrumentType.DELIVERY_CONTRACT
    assert parse_standard_instrument(str(contract)) == contract


@pytest.mark.parametrize("month", [0, 13, -1])
def test_contract_rejects_invalid_month(month: int) -> None:
    with pytest.raises(ValueError, match="month"):
        ContractId(Exchange.DCE, "M", 2026, month)


@pytest.mark.parametrize("year", [26, 999, 10000, True])
def test_contract_requires_four_digit_full_year(year: int) -> None:
    with pytest.raises(ValueError, match="four-digit"):
        ContractId(Exchange.DCE, "M", year, 9)


def test_continuous_identity_is_not_a_delivery_contract() -> None:
    continuous = ContinuousInstrumentId(Exchange.DCE, "p")
    delivery = ContractId(Exchange.DCE, "P", 2026, 9)
    assert str(continuous) == "DCE:P:CONTINUOUS_MAIN"
    assert continuous.instrument_type is InstrumentType.CONTINUOUS_MAIN
    assert continuous != delivery
    assert parse_standard_instrument(str(continuous)) == continuous


def test_raw_symbol_is_parser_evidence_not_identity() -> None:
    parser = ChinaFuturesSymbolParser()
    upper = parser.parse("Y2609", Exchange.DCE)
    lower = parser.parse("y2609", Exchange.DCE)
    main = parser.parse("Y0", Exchange.DCE)
    assert upper.instrument == lower.instrument == ContractId(Exchange.DCE, "Y", 2026, 9)
    assert upper.raw_code != lower.raw_code
    assert main.instrument == ContinuousInstrumentId(Exchange.DCE, "Y")
    assert StandardInstrumentFormatter().format(upper.instrument) == "DCE:Y:2026-09"


def test_contract_is_immutable() -> None:
    contract = ContractId(Exchange.CZCE, "RM", 2027, 1)
    with pytest.raises(FrozenInstanceError):
        contract.month = 2  # type: ignore[misc]


@pytest.mark.parametrize("exchange", [Exchange.BMD, Exchange.ICE, Exchange.EURONEXT])
def test_standard_contract_identity_supports_international_exchanges(exchange: Exchange) -> None:
    contract = ContractId(exchange, "CANOLA", 2026, 11)
    assert parse_standard_instrument(str(contract)) == contract


def test_raw_parser_rejects_ambiguous_or_wrong_exchange_symbols() -> None:
    parser = ChinaFuturesSymbolParser()
    with pytest.raises(ValueError, match="unsupported"):
        parser.parse("Y609", Exchange.DCE)
    with pytest.raises(ValueError, match="DCE or CZCE"):
        parser.parse("Y2609", Exchange.CBOT)
