from __future__ import annotations

import pandas as pd
import pytest

from agri_research_agent.domains.spreads.models import SpreadCalculation
from agri_research_agent.domains.spreads.parsing import (
    classify_board,
    configured_spreads,
    definition_from_legacy,
    display_spread_name,
    parse_season,
    parse_spread_name,
    spread_sort_key,
)


def test_legacy_name_and_season_parsing() -> None:
    assert parse_spread_name("M 9-1") == (["M"], [9, 1])
    assert parse_spread_name("OI-Y 5") == (["OI", "Y"], [5])
    assert parse_spread_name("M 13-1") is None
    assert parse_spread_name("M09-M01") is None
    assert parse_season("2025/2026") == (2025, 2026)
    assert parse_season("mean") is None
    assert parse_season("2025/2027") is None


def test_legacy_definition_resolves_exchange_product_year_and_month() -> None:
    month_spread = definition_from_legacy("M 9-1", "2025/2026")
    assert str(month_spread.leg1.instrument) == "DCE:M:2025-09"
    assert str(month_spread.leg2.instrument) == "DCE:M:2026-01"

    forward_months = definition_from_legacy("M 1-5", "2025/2026")
    assert str(forward_months.leg1.instrument) == "DCE:M:2026-01"
    assert str(forward_months.leg2.instrument) == "DCE:M:2026-05"

    cross_product = definition_from_legacy(
        "OI-Y 5", "2025/2026", SpreadCalculation.RATIO
    )
    assert str(cross_product.leg1.instrument) == "CZCE:OI:2026-05"
    assert str(cross_product.leg2.instrument) == "DCE:Y:2026-05"
    assert cross_product.calculation is SpreadCalculation.RATIO


def test_invalid_legacy_definition_fails_closed() -> None:
    with pytest.raises(ValueError, match="spread name"):
        definition_from_legacy("unknown", "2025/2026")
    with pytest.raises(ValueError, match="season"):
        definition_from_legacy("M 9-1", "mean")


def test_board_subgroup_display_and_sort_preserve_dashboard_contract() -> None:
    assert display_spread_name("OI-Y 5") == "豆油-菜油 05"
    assert classify_board("M 9-1") == ("豆系月差", "月差")
    assert classify_board("P 1-5") == ("棕榈油与菜系月差", "月差")
    assert classify_board("OI-Y 5") == ("品种间套利", "油脂之间套利")
    assert classify_board("M-RM 5") == ("品种间套利", "粕之间套利")
    assert classify_board("Y-M 5") == ("排除", "油粕跨类")
    assert classify_board("ratio M/RM") == ("排除", "比值或利润")
    assert spread_sort_key("Y 9-1") < spread_sort_key("M 9-1")
    assert spread_sort_key("M 9-1") < spread_sort_key("M 1-5")


def test_configured_spreads_keeps_available_unconfigured_names_and_excludes_forbidden() -> None:
    data = pd.DataFrame(
        {"spread_name": ["M 5-9", "Y 9-1", "M 9-1", "OI-Y 5", "Y-M 5"]}
    )
    config = pd.DataFrame({"spread_name": ["M 5-9", "Y 9-1"]})
    assert configured_spreads(data, config) == {
        "豆系月差": ["Y 9-1", "M 9-1", "M 5-9"],
        "棕榈油与菜系月差": [],
        "品种间套利": ["OI-Y 5"],
    }
