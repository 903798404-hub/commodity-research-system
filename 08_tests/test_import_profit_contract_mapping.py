from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agri_research_agent.import_profit import load_soybean_config, map_soybean_contracts
from agri_research_agent.import_profit.config import ImportProfitConfigError


ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG = ROOT / "02_configs" / "import_profit_soybean.yaml"
CONFIG = load_soybean_config(REAL_CONFIG)
EXPECTED = [
    (1, "2026-01", "M2605", "Y2605"),
    (2, "2026-03", "M2605", "Y2605"),
    (3, "2026-03", "M2605", "Y2605"),
    (4, "2026-05", "M2609", "Y2609"),
    (5, "2026-05", "M2609", "Y2609"),
    (6, "2026-07", "M2609", "Y2609"),
    (7, "2026-07", "M2609", "Y2609"),
    (8, "2026-09", "M2701", "Y2701"),
    (9, "2026-09", "M2701", "Y2701"),
    (10, "2026-11", "M2701", "Y2701"),
    (11, "2026-11", "M2701", "Y2701"),
    (12, "2027-01", "M2701", "Y2701"),
]


def write_variant(tmp_path: Path, mutate) -> Path:
    payload = yaml.safe_load(REAL_CONFIG.read_text(encoding="utf-8"))
    mutate(payload)
    target = tmp_path / "mapping.yaml"
    target.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target


def test_all_twelve_months_map_from_the_single_config_source() -> None:
    actual = []
    for shipment_month in range(1, 13):
        result = map_soybean_contracts(CONFIG, 2026, shipment_month)
        actual.append((shipment_month, result.cbot.label, result.soymeal.code, result.soyoil.code))

    assert actual == EXPECTED
    assert len({rule.shipment_month for rule in CONFIG.contract_mapping}) == 12


def test_december_and_following_january_have_distinct_cross_year_mapping() -> None:
    december = map_soybean_contracts(CONFIG, 2026, 12)
    january = map_soybean_contracts(CONFIG, 2027, 1)

    assert (december.cbot.label, december.soymeal.code, december.soyoil.code) == (
        "2027-01",
        "M2701",
        "Y2701",
    )
    assert (january.cbot.label, january.soymeal.code, january.soyoil.code) == (
        "2027-01",
        "M2705",
        "Y2705",
    )
    assert december.mapping_identity == january.mapping_identity == CONFIG.contract_mapping_identity


@pytest.mark.parametrize("shipment_month", [8, 9, 10, 11])
def test_august_through_november_use_next_year_dce_contracts(shipment_month: int) -> None:
    result = map_soybean_contracts(CONFIG, 2026, shipment_month)
    assert result.soymeal.contract_year == 2027
    assert result.soyoil.contract_year == 2027
    assert result.cbot.contract_year == 2026


def test_mapping_with_missing_or_duplicate_shipment_month_is_rejected(tmp_path: Path) -> None:
    missing = write_variant(
        tmp_path,
        lambda payload: payload["contract_mapping"]["rows"].pop(),
    )
    with pytest.raises(ImportProfitConfigError, match="each shipment month exactly once"):
        load_soybean_config(missing)

    def duplicate_first(payload) -> None:
        payload["contract_mapping"]["rows"][-1]["shipment_month"] = 1

    duplicate = write_variant(tmp_path, duplicate_first)
    with pytest.raises(ImportProfitConfigError, match="each shipment month exactly once"):
        load_soybean_config(duplicate)


def test_invalid_target_contract_month_is_rejected(tmp_path: Path) -> None:
    invalid = write_variant(
        tmp_path,
        lambda payload: payload["contract_mapping"]["rows"][0]["dce"].update(
            {"contract_month": 13}
        ),
    )
    with pytest.raises(ImportProfitConfigError, match="between 1 and 12"):
        load_soybean_config(invalid)


def test_repeated_consumers_receive_identical_mapping_objects() -> None:
    page_facing_result = map_soybean_contracts(CONFIG, 2026, 12)
    calculator_facing_result = map_soybean_contracts(CONFIG, 2026, 12)
    assert page_facing_result == calculator_facing_result
