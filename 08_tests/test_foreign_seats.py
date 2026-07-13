import datetime as dt
from pathlib import Path

import pandas as pd

from agri_research_agent.data_sources.foreign_seats import normalize_rankings, write_parquet


CONFIG = {"foreign_seats": ["摩根大通期货"], "key_seats": ["摩根大通期货", "乾坤期货"], "aliases": {"摩根大通期货": ["摩根大通"], "乾坤期货": ["乾坤"]}}


def test_position_calculation_and_not_ranked(tmp_path: Path):
    table = pd.DataFrame({"long_party_name":["摩根大通", "其他"], "long_open_interest":[10000, 1], "long_open_interest_chg":[1200, 1], "short_party_name":["摩根大通", "乾坤"], "short_open_interest":[7000, 2], "short_open_interest_chg":[-300, 2]})
    rows = normalize_rankings(dt.date(2026, 7, 10), "DCE", "P", {"P": table}, CONFIG, "now")
    morgan = next(row for row in rows if row["seat_name_normalized"] == "摩根大通期货")
    qian = next(row for row in rows if row["seat_name_normalized"] == "乾坤期货")
    assert (morgan["net_position"], morgan["net_change"], morgan["data_status"]) == (3000, 1500, "listed")
    assert pd.isna(qian["net_position"]) and qian["data_status"] == "listed"


def test_parquet_deduplicates_normalized_key(tmp_path: Path):
    record = {"trade_date":"2026-07-10","exchange":"DCE","variety":"P","seat_name_raw":"摩根大通","seat_name_normalized":"摩根大通期货","seat_group":"foreign","long_position":1,"long_change":1,"short_position":1,"short_change":1,"net_position":0,"net_change":0,"price_contract":"P0","settlement_price":1,"data_status":"listed","updated_at":"a"}
    output = tmp_path / "positions.parquet"
    result = write_parquet([record, {**record, "net_position": 5, "updated_at":"b"}], pd.DataFrame(), output)
    assert len(result) == 1 and result.iloc[0].net_position == 5
