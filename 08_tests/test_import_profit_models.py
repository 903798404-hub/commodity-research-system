from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from agri_research_agent.import_profit import (
    BusinessKey,
    CbotContract,
    ContractMappingError,
    DceContract,
    load_soybean_config,
)
from agri_research_agent.import_profit.models import BusinessKeyError, MissingReason


ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_soybean_config(ROOT / "02_configs" / "import_profit_soybean.yaml")


def make_key(
    *,
    business_date: date = date(2026, 7, 28),
    commodity: str = "soybean",
    origin: str = "brazil",
    shipment_year: int = 2027,
    shipment_month: int = 1,
    shipment_period: str | None = None,
) -> BusinessKey:
    return BusinessKey(
        business_date,
        commodity,
        origin,
        shipment_year,
        shipment_month,
        CONFIG.origin_codes,
        CONFIG.commodity,
        shipment_period,
    )


def test_business_key_normalizes_period_and_is_hashable() -> None:
    first = make_key(shipment_period="2027-01")
    same = make_key(shipment_period="2027-01")

    assert first.shipment_period == "2027-01"
    assert first == same
    assert hash(first) == hash(same)
    assert {first: "value"}[same] == "value"


def test_year_is_part_of_business_key_identity() -> None:
    key_2026 = make_key(shipment_year=2026, shipment_month=12)
    key_2027 = make_key(shipment_year=2027, shipment_month=12)

    assert key_2026 != key_2027
    assert key_2026.shipment_period == "2026-12"
    assert key_2027.shipment_period == "2027-12"


@pytest.mark.parametrize(
    "overrides",
    [
        {"shipment_month": 0},
        {"shipment_month": 13},
        {"commodity": ""},
        {"commodity": "rapeseed"},
        {"origin": "unknown"},
        {"shipment_year": 1899},
        {"shipment_year": "2027"},
        {"business_date": "2026-07-28"},
        {"shipment_period": "2027-1"},
        {"shipment_period": "2026-01"},
    ],
)
def test_invalid_business_keys_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(BusinessKeyError) as exc_info:
        make_key(**overrides)  # type: ignore[arg-type]
    assert exc_info.value.reason is MissingReason.INVALID_BUSINESS_KEY


def test_contract_labels_and_dce_codes_keep_full_year_identity() -> None:
    cbot = CbotContract(2027, 1)
    meal = DceContract.soymeal(2027, 5)
    oil = DceContract.soyoil(2027, 5)

    assert cbot.market == "CBOT"
    assert cbot.commodity == "soybean"
    assert cbot.label == "2027-01"
    assert meal.code == "M2705"
    assert oil.code == "Y2705"


@pytest.mark.parametrize("year", [1999, 2100])
def test_dce_two_digit_year_encoding_rejects_ambiguous_centuries(year: int) -> None:
    with pytest.raises(ContractMappingError) as exc_info:
        DceContract.soymeal(year, 1)
    assert exc_info.value.reason is MissingReason.INVALID_CONTRACT_MAPPING
