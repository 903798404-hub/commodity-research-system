from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.warehouse_receipts.czce import SOURCE, acquire_sugar_reports, parse_sugar_report, report_url
from agri_research_agent.warehouse_receipts.model import PRODUCTS, ReceiptError, aggregate_daily, load_snapshot, normalize_rows, write_snapshot
from agri_research_agent.warehouse_receipts.update import prepare_incremental
from test_warehouse_receipts_update import Client, observation, seed

DAY = dt.date(2026, 10, 12)


def report():
    return ("品种：白糖SR 单位：张 日期：2026-10-12\n"
            "仓库编号|仓库简称|年度|等级|品牌|仓单数量|当日增减|有效预报|升贴水\n"
            "0103|仓库甲|2526|1|品牌甲|8|-2||60\n"
            "||2526|1|品牌乙|12|2||\n"
            "小计|||||20|0|5|\n"
            "0409|仓库乙||||0|0||-140\n"
            "小计|||||0|0|0|\n"
            "总计|||||20|0|5|\n"
            "品种：菜油OI 单位：张 日期：2026-10-12\n").encode("utf-8")


def test_parse_official_sugar_merged_cells_units_zero_and_source_delta():
    rows = parse_sugar_report(report(), DAY)
    assert len(rows) == 4
    assert [r["vol"] for r in rows] == [8, 12, 0, 20]
    assert [r["vol_chg"] for r in rows] == [-2, 2, 0, 0]
    assert rows[1]["warehouse"] == "仓库甲" and rows[1]["wh_id"] == "0103"
    assert rows[1]["pd"] is None and all(r["pre_vol"] is None for r in rows)
    assert all(r["symbol"] == "SR" and r["unit"] == "张" and r["exchange"] == "CZCE" for r in rows)


@pytest.mark.parametrize("before,after", [
    ("2026-10-12", "2026-10-09"), ("单位：张", "单位：吨"),
    ("总计|||||20", "总计|||||21"), ("小计|||||20", "小计|||||19"),
    ("|仓库甲|", "||"), ("|8|-2|", "||-2|"),
    ("仓单数量", "错误列"), ("总计|||||20|0|5|\n", ""),
])
def test_corrupt_wrong_day_and_incomplete_reports_fail_closed(before, after):
    with pytest.raises(ReceiptError):
        parse_sugar_report(report().decode().replace(before, after).encode(), DAY)


class Response:
    def __init__(self, url, content, status):
        self.url, self.content, self.status_code = url, content, status
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size): yield self.content


class Session:
    def __init__(self, status=200, content=None):
        self.calls = []
        self.status, self.content = status, report() if content is None else content
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(url, self.content, self.status)


def test_acquisition_is_official_https_bounded_original_bytes_and_replay(tmp_path):
    session = Session()
    result = acquire_sugar_reports([DAY], tmp_path, session=session)
    assert result[0]["status"] == "VALIDATED" and result[0]["source"] == SOURCE
    assert session.calls[0][0] == report_url(DAY)
    assert session.calls[0][1]["allow_redirects"] is False
    original = tmp_path / "SR-20261012-FutureDataWhsheet.txt"
    assert original.read_bytes() == report()
    assert acquire_sugar_reports([DAY], tmp_path, session=Session(500)) == result
    original.write_bytes(b"tampered")
    with pytest.raises(ReceiptError, match="身份"):
        acquire_sugar_reports([DAY], tmp_path, session=session)


def test_not_published_preserves_missing_and_other_http_errors_do_not_fallback(tmp_path):
    result = acquire_sugar_reports([DAY], tmp_path / "missing", session=Session(404))
    assert result[0]["status"] == "NOT_PUBLISHED" and result[0]["rows"] == []
    assert not list((tmp_path / "missing").glob("*.txt"))
    with pytest.raises(ReceiptError):
        acquire_sugar_reports([DAY], tmp_path / "refused", session=Session(412))
    with pytest.raises(ReceiptError, match="大小"):
        acquire_sugar_reports([DAY], tmp_path / "oversize", session=Session(content=b"x" * (4 * 1024**2 + 1)))


def test_official_cutover_retains_every_old_sugar_row_and_never_queries_gateway_sr(tmp_path, monkeypatch):
    from agri_research_agent.warehouse_receipts import czce
    baseline = tmp_path / "baseline"
    seed(baseline)
    before = (baseline / "current.json").read_bytes()
    old_daily, _ = load_snapshot(baseline)
    class NoSugarGateway(Client):
        def query(self, symbol, start, end):
            assert symbol != "SR"
            return super().query(symbol, start, end)
    def official(days, root):
        assert days == [DAY]
        return [{"source": SOURCE, "url": report_url(DAY), "rows": parse_sugar_report(report(), DAY)}]
    monkeypatch.setattr(czce, "acquire_sugar_reports", official)
    candidate = tmp_path / "candidate"
    result = prepare_incremental(NoSugarGateway([]), baseline, candidate, tmp_path / "raw",
                                 dt.datetime(2026, 10, 12, 16, 15, tzinfo=dt.timezone(dt.timedelta(hours=8))), official_sr=True)
    assert result["latest_dates"]["SR"] == "2026-10-12" and "SR" not in result["pending_products"]
    assert (baseline / "current.json").read_bytes() == before
    daily, manifest = load_snapshot(candidate)
    pd.testing.assert_frame_equal(daily[daily.date.le(pd.Timestamp("2026-10-09"))].reset_index(drop=True), old_daily)
    new = daily[daily.date.eq(pd.Timestamp("2026-10-12"))].iloc[0]
    assert new.source == SOURCE and new.vol == 20 and new.vol_chg == 0 and pd.isna(new.pre_vol)
    assert manifest["official_sugar_from"] == "2026-10-10"
    with pytest.raises(ReceiptError, match="禁止"):
        prepare_incremental(Client([]), candidate, tmp_path / "revert", tmp_path / "raw-revert",
                             dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc))


def test_unpublished_official_report_never_replaces_sugar_history(tmp_path, monkeypatch):
    from agri_research_agent.warehouse_receipts import czce
    baseline = tmp_path / "baseline"
    seed(baseline)
    monkeypatch.setattr(czce, "acquire_sugar_reports", lambda days, root: [{"rows": [], "status": "NOT_PUBLISHED"}])
    result = prepare_incremental(Client([]), baseline, tmp_path / "candidate", tmp_path / "raw",
                                 dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc), official_sr=True)
    assert result["status"] == "WAITING_FOR_SOURCE" and not result["business_changed"]
    assert result["latest_dates"]["SR"] == "2026-10-09"
    assert (tmp_path / "candidate/current.json").read_bytes() == (baseline / "current.json").read_bytes()


def test_followup_requeries_official_days_without_crossing_the_cutover(tmp_path, monkeypatch):
    from agri_research_agent.warehouse_receipts import czce
    baseline = tmp_path / "baseline"
    seed(baseline)
    calls = []
    def official(days, root):
        calls.append(days)
        return [{"source": SOURCE, "url": report_url(DAY), "rows": parse_sugar_report(report(), DAY)}]
    monkeypatch.setattr(czce, "acquire_sugar_reports", official)
    now = dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc)
    first = tmp_path / "first"
    prepare_incremental(Client([]), baseline, first, tmp_path / "raw1", now, official_sr=True)
    result = prepare_incremental(Client([]), first, tmp_path / "second", tmp_path / "raw2", now, official_sr=True)
    assert calls == [[DAY], [DAY]] and not result["business_changed"]
    assert result["official_sugar_from"] == "2026-10-10"
    assert (tmp_path / "second/current.json").read_bytes() == (first / "current.json").read_bytes()


def test_official_failure_does_not_write_candidate_pointer(tmp_path, monkeypatch):
    from agri_research_agent.warehouse_receipts import czce
    baseline = tmp_path / "baseline"
    seed(baseline)
    old = (baseline / "current.json").read_bytes()
    def broken(*args): raise ReceiptError("official unavailable")
    monkeypatch.setattr(czce, "acquire_sugar_reports", broken)
    with pytest.raises(ReceiptError):
        prepare_incremental(Client([]), baseline, tmp_path / "candidate", tmp_path / "raw",
                             dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc), official_sr=True)
    assert (baseline / "current.json").read_bytes() == old
    assert not (tmp_path / "candidate/current.json").exists()


def test_deploy_timer_has_1615_first_check_and_preserves_bounded_retry_admission():
    root = Path(__file__).resolve().parents[1]
    timer = (root / "09_deploy/warehouse_server/market-data-warehouse-receipts.timer.template").read_text()
    assert "OnCalendar=Mon..Fri *-*-* 16:15:00 Asia/Shanghai" in timer
    for t in ["17:30", "18:30", "20:00", "22:00", "08:40"]:
        assert t + ":00 Asia/Shanghai" in timer
    assert "Unit=market-data-task-enqueue@warehouse.service" in timer


def test_first_cutover_preserves_even_invalid_legacy_days(tmp_path, monkeypatch):
    from agri_research_agent.warehouse_receipts import czce
    baseline = tmp_path / "baseline"
    raw = normalize_rows([observation(s) for s in PRODUCTS] + [observation("SR", date="20261012", vol=-1)])
    write_snapshot(baseline, raw, aggregate_daily(raw), dict(source="Tushare-compatible", requested_start="2026-10-01", requested_end="2026-10-12"))
    before = (baseline / "current.json").read_bytes()
    def official(days, root):
        assert days == [dt.date(2026, 10, 13)]
        return []
    monkeypatch.setattr(czce, "acquire_sugar_reports", official)
    result = prepare_incremental(Client([]), baseline, tmp_path / "candidate", tmp_path / "raw",
                                 dt.datetime(2026, 10, 13, tzinfo=dt.timezone.utc), official_sr=True)
    assert result["official_sugar_from"] == "2026-10-13" and not result["business_changed"]
    assert (tmp_path / "candidate/current.json").read_bytes() == before
