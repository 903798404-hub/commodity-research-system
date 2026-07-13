import datetime as dt
from pathlib import Path

import pandas as pd

from agri_research_agent.data_sources import foreign_seats
from agri_research_agent.data_sources.foreign_seats import source_error_rows
from report_foreign_seats_coverage import coverage


def test_412_is_never_marked_success():
    rows = source_error_rows(dt.date(2026, 7, 10), "DCE", {"foreign_seats": ["A"], "key_seats": []}, "now", {"source_status": "failed", "error_message": "HTTP 412"})
    assert all(row["data_status"] == "source_error" and row["source_status"] == "failed" for row in rows)


def test_dce_fallback_is_used_after_akshare_failure(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(foreign_seats, "fetch_rank_tables", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("HTTP 412")))
    expected = {"P": pd.DataFrame()}
    monkeypatch.setattr(foreign_seats, "fetch_dce_official_file", lambda *args, **kwargs: (expected, {"source_name":"DCE official", "source_status":"success"}))
    tables, provenance = foreign_seats.fetch_dce_with_fallback(dt.date(2026, 7, 10), tmp_path, retries=0)
    assert set(tables) == {"P"} and provenance["source_name"] == "DCE official"


def test_coverage_and_provenance():
    data = pd.DataFrame([{"trade_date":"2026-06-01","variety":"P","seat_name_normalized":"A","data_status":"listed","long_position":2,"short_position":1,"net_position":1,"source_name":"AKShare","source_method":"x","source_status":"success","error_message":""}, {"trade_date":"2026-06-02","variety":"P","seat_name_normalized":"A","data_status":"source_error","long_position":None,"short_position":None,"net_position":None,"source_name":"DCE official","source_method":"zip","source_status":"failed","error_message":"HTTP 412"}])
    summary, failures, assessment = coverage(data, "2026-06-01")
    assert summary.iloc[0].strict_net_coverage == 1 and len(failures) == 1 and assessment.iloc[0].continuity_gain_seat_days == 0
