import math

import pandas as pd
import pytest

from agri_research_agent.data_sources.domestic_sugar_daily import validate_daily
from agri_research_agent.data_sources.domestic_sugar_history import SugarHistoryError


def payload():
    return dict(schema_version="domestic-sugar-daily/1", workbook_sha256="a" * 64,
                mapping_sha256="b" * 64, daily_sha256="c" * 64, records=[
                    dict(date="2026-11-10", region="广西糖厂", components=[
                        dict(name="东糖", price=5150, cell="J2701"),
                        dict(name="凤糖", price=5160, cell="K2701")],
                        contract="SR2701.ZCE", close=5195),
                    dict(date="2026-11-10", region="乌鲁木齐", components=[],
                         contract="SR2701.ZCE", close=5195),
                    dict(date="2026-11-10", region="昆明", components=[],
                         contract="SR2701.ZCE", close=5195)])


def test_mean_and_missing_basis():
    rows = validate_daily(payload()).set_index("region")
    assert rows.loc["广西糖厂", "cash_price"] == 5155
    assert rows.loc["广西糖厂", "basis"] == -40
    assert math.isnan(rows.loc["乌鲁木齐", "cash_price"])
    assert math.isnan(rows.loc["乌鲁木齐", "basis"])


def test_missing_same_day_close_stays_blank():
    data = payload()
    for row in data["records"]:
        row["close"] = None
    assert pd.isna(validate_daily(data).iloc[0]["basis"])


@pytest.mark.parametrize("mutate", [
    lambda p: p["records"].append(dict(p["records"][0])),
    lambda p: p["records"][0].update(date="2026-10-28"),
    lambda p: p["records"][0]["components"][0].update(price=0),
    lambda p: p["records"][0]["components"][0].update(cell="B2701"),
    lambda p: p["records"][0].update(contract=None),
    lambda p: p.update(mapping_sha256="bad"),
])
def test_rejects_invalid_data(mutate):
    data = payload(); mutate(data)
    with pytest.raises(SugarHistoryError):
        validate_daily(data)
