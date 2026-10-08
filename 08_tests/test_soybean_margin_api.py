from datetime import date, datetime, timedelta
import copy
import json

import pandas as pd
import pytest

from agri_research_agent.soybean_margin import api_inputs as inputs, api_sources as sources
from agri_research_agent.soybean_margin.model import contracts, FIELDS, daily_rows

DAY = date(2026, 10, 8)


def snapshot():
    return dict(schema_version=inputs.SCHEMA, business_date=DAY.isoformat(),
                captured_at="2026-10-08T09:35:00+08:00", fx_currency="USD/CNY",
                fx_unit="CNY_per_USD", cbot_unit="US_cents/bushel", domestic_unit="CNY/tonne",
                cbot={contracts(DAY, m)[0]: 1200. for m in range(1, 13)},
                domestic={p + contracts(DAY, m)[1]: (3000. if p == "M" else 8000.) for p in ("M", "Y") for m in range(1, 13)},
                fx_curve={"0": 7., "1": 6.99, "3": 6.97, "6": 6.94, "9": 6.91, "12": 6.88},
                sources={"cbot": {}, "fx": {}, "domestic": {}}, errors={})


def history(day=DAY):
    return pd.DataFrame([dict(business_date=day, origin="brazil", shipment_year=contracts(day, 1)[2],
                             shipment_month=1, **dict(zip(FIELDS, [0., 999., 8., 1., 2.])))])


def test_curve_matches_shipment_tenor_and_midpoint_without_extrapolation():
    curve = snapshot()["fx_curve"]
    assert inputs.select_fx(curve, 1) == (7., "spot", False, 0, 0)
    assert inputs.select_fx(curve, 3) == (6.97, "forward", False, 3, 3)
    assert inputs.select_fx(curve, 4)[0] == pytest.approx(6.96)
    assert inputs.select_fx(curve, 4)[2:] == (True, 3, 6)
    assert inputs.select_fx(curve, 13)[0] is None
    assert inputs.select_fx({"0": 7., "3": None, "6": 6.94}, 3)[0] is None


def test_missing_far_contracts_do_not_substitute_old_or_adjacent_prices():
    value = snapshot()
    value["domestic"]["M2801"] = None
    value["domestic"]["Y2801"] = None
    value["cbot"] = dict.fromkeys(value["cbot"])
    merged = inputs.apply_api_inputs(history(), {DAY: value}, DAY)
    rows = daily_rows(merged, DAY, "brazil")
    assert len(merged) == 48 and len(rows) == 12
    assert rows[0][FIELDS[0]] == 0
    assert rows[0][FIELDS[2]] == 6.97
    assert rows[1][FIELDS[2]] == pytest.approx(6.96)
    assert rows[10][FIELDS[2]] == 7
    assert rows[8][FIELDS[3]] is None
    assert rows[8]["net_margin"] is None
    assert all(row[FIELDS[1]] is None for row in rows)
    absent = inputs.apply_api_inputs(history(), {}, DAY)
    assert absent[FIELDS[1:]].isna().all().all()


def test_old_history_and_cnf_are_preserved_and_date_does_not_roll_forward():
    old = date(2026, 10, 7)
    combined = pd.concat([history(old), history()], ignore_index=True)
    merged = inputs.apply_api_inputs(combined, {DAY: snapshot()}, DAY + timedelta(days=1))
    assert merged.loc[merged.business_date.eq(old), FIELDS].iloc[0].tolist() == [0., 999., 8., 1., 2.]
    assert merged.loc[merged.business_date.eq(DAY + timedelta(days=1)), FIELDS[1:]].isna().all().all()
    assert merged.loc[merged.business_date.eq(DAY) & merged.origin.eq("brazil") & merged.shipment_month.eq(1), FIELDS[0]].iloc[0] == 0


def test_snapshot_integrity_and_write_authorization(tmp_path):
    authorized = []
    sha = inputs.publish(tmp_path, snapshot(), authorize=authorized.append)
    assert inputs.read_day(tmp_path, DAY) == snapshot()
    assert tmp_path / DAY.isoformat() / ".lock" in authorized
    assert inputs.read_day(tmp_path, DAY + timedelta(days=1)) is None
    target = tmp_path / DAY.isoformat() / (sha + ".json")
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="身份"):
        inputs.read_day(tmp_path, DAY)
    def reject(path):
        raise RuntimeError("denied")
    with pytest.raises(RuntimeError, match="denied"):
        inputs.publish(tmp_path / "denied", snapshot(), authorize=reject)
    assert not (tmp_path / "denied").exists()


@pytest.mark.parametrize("key,value", [("fx_currency", "USD/CNH"), ("fx_unit", "CNH_per_USD"),
                                      ("cbot_unit", "USD/bushel"), ("captured_at", "2026-10-08T09:35:00")])
def test_reject_wrong_currency_unit_or_naive_time(key, value):
    data = snapshot()
    data[key] = value
    with pytest.raises(ValueError):
        inputs.validate_snapshot(data)


class Response:
    def __init__(self, value, status=200):
        self.raw = value if isinstance(value, str) else json.dumps(value)
        self.status_code = status
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def iter_content(self, size): yield self.raw.encode()
    def raise_for_status(self):
        if self.status_code >= 400:
            raise sources.requests.HTTPError("sensitive request detail")


class Session:
    def __init__(self, *responses): self.responses, self.calls = list(responses), []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_historical_exact_minute_close_units_and_no_future_bar():
    assert sources.raw_symbol("2611") == "ZSX6"
    assert sources.raw_symbol("2701") == "ZSF7"
    start, end = sources.window(DAY)
    record = dict(hd={"ts_event": start.isoformat()}, symbol="ZSX6", close="1200.25")
    session = Session(Response(json.dumps(record)))
    prices, meta = sources.historical_cbot(DAY, "dummy", session=session)
    assert prices["2611"] == 1200.25
    assert prices["2701"] is None
    assert session.calls[0][1]["data"]["end"] == "2026-10-08T01:00:00+00:00"
    assert end - start == timedelta(minutes=1)
    record["hd"]["ts_event"] = end.isoformat()
    with pytest.raises(sources.SourceError, match="identity"):
        sources.historical_cbot(DAY, "dummy", session=Session(Response(json.dumps(record))))


def test_today_historical_rejection_keeps_prices_empty_and_sanitizes_errors():
    session = Session(Response({"detail": {"case": "dataset_unavailable_range"}}, 422))
    values, meta = sources.historical_cbot(DAY, "secret", session=session)
    assert meta["status"] == "historical_not_yet_available" and not any(values.values())
    with pytest.raises(sources.SourceError, match="transport_or_schema") as error:
        sources.historical_cbot(DAY, "secret", session=Session(Response("", 401)))
    assert "sensitive" not in str(error.value) and "secret" not in str(error.value)


def test_cfets_points_become_forward_full_price_and_wrong_pair_rejected():
    now = datetime(2026, 10, 8, 9, 35, tzinfo=sources.SHANGHAI)
    spot = dict(data={"showDateCN": "2026-10-08 09:34:50"}, records=[dict(ccyPair="USD/CNY", bidPrc="7.0000", askPrc="7.0002")])
    swap = dict(data=spot["data"], records=[dict(ccyPair="USD/CNY", label_3M="-459.25/-459.25", label_6M="-948/-946.5")])
    curve, meta = sources.fx_curve(now, session=Session(Response(spot), Response(swap)))
    assert curve["0"] == pytest.approx(7.0001)
    assert curve["3"] == pytest.approx(7.0001 - .045925)
    assert curve["6"] == pytest.approx(7.0001 - .094725)
    assert curve["9"] is None and meta["points_divisor"] == 10000
    swap["records"][0]["ccyPair"] = "USD/CNH"
    with pytest.raises(sources.SourceError, match="pair"):
        sources.fx_curve(now, session=Session(Response(spot), Response(swap)))


def test_quote_dates_must_be_same_day_fresh_and_never_future():
    now = datetime(2026, 10, 8, 9, 35, tzinfo=sources.SHANGHAI)
    for delta in (timedelta(days=-1), timedelta(minutes=-6), timedelta(seconds=1)):
        with pytest.raises(sources.SourceError, match="stale_or_future"):
            sources.fresh(now + delta, now)


def test_backfill_only_changes_cbot_and_preserves_original_collection_time(monkeypatch):
    value = snapshot()
    value["cbot"] = dict.fromkeys(value["cbot"])
    before = copy.deepcopy(value)
    monkeypatch.setattr(sources, "historical_cbot", lambda *a: ({k: 1300. for k in value["cbot"]}, {"status": "available"}))
    result = sources.backfill(value, "secret")
    assert value == before
    for field in ("captured_at", "fx_curve", "domestic"):
        assert result[field] == before[field]
    assert all(v == 1300. for v in result["cbot"].values())


def test_worker_timeout_fails_closed(monkeypatch):
    def fail(*args, **kwargs):
        raise sources.subprocess.TimeoutExpired("akshare", 35)
    monkeypatch.setattr(sources.subprocess, "run", fail)
    with pytest.raises(sources.SourceError, match="worker_failed"):
        sources.domestic(["M2705"])


def evidence():
    return dict(sha256="a" * 64, filename="a" * 64 + ".png")


def test_manual_quotes_override_only_confirmed_contracts_and_archive_unused_month():
    result = sources.manual_cbot(snapshot(), {"2611": 1299.50, "2708": 1322.25}, evidence())
    assert result["cbot"]["2611"] == 1299.50
    assert result["cbot"]["2701"] is None
    assert result["sources"]["cbot"]["manual"]["unused_contracts"] == ["2708"]
    assert result["sources"]["cbot"]["manual"]["quoted_at"] is None
    assert result["sources"]["cbot"]["status"] == "manual_quotes"


@pytest.mark.parametrize("prices", [{"11": 1299}, {"2611": 0}, {"2611": True}, {"2602": 1000}, {"9911": 1000}])
def test_manual_quotes_reject_incomplete_contract_years_and_invalid_values(prices):
    with pytest.raises(sources.SourceError):
        sources.manual_cbot(snapshot(), prices, evidence())


def test_historical_backfill_never_overwrites_user_uploaded_quotes(monkeypatch):
    value = sources.manual_cbot(snapshot(), {"2611": 1299.5}, evidence())
    monkeypatch.setattr(sources, "historical_cbot", lambda *args: ({k: 1300. for k in value["cbot"]}, {"status": "available"}))
    result = sources.backfill(value, "secret")
    assert result["cbot"]["2611"] == 1299.5
    assert result["cbot"]["2701"] == 1300.
    assert result["sources"]["cbot"]["manual"] == value["sources"]["cbot"]["manual"]


def test_new_upload_replaces_manual_but_keeps_available_historical_contracts(monkeypatch):
    value = sources.manual_cbot(snapshot(), {"2611": 1299.5, "2701": 1316.25}, evidence())
    monkeypatch.setattr(sources, "historical_cbot", lambda *args: ({k: 1300. for k in value["cbot"]}, {"status": "available"}))
    filled = sources.backfill(value, "secret")
    result = sources.manual_cbot(filled, {"2611": 1298.}, evidence())
    assert result["cbot"]["2611"] == 1298.
    assert result["cbot"]["2701"] == 1300.
    assert result["domestic"] == value["domestic"] and result["fx_curve"] == value["fx_curve"]


def test_manual_evidence_is_archived_and_verified_on_read(tmp_path):
    source = tmp_path / "upload.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\noriginal evidence")
    store = tmp_path / "store"
    authorized = []
    pointer = inputs.archive_evidence(store, source, authorize=authorized.append)
    target = store / "evidence" / pointer["filename"]
    assert target in authorized and target.read_bytes() == source.read_bytes()
    value = sources.manual_cbot(snapshot(), {"2611": 1299.5}, pointer)
    inputs.publish(store, value, authorize=authorized.append)
    assert inputs.read_day(store, DAY)["cbot"]["2611"] == 1299.5
    target.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="证据身份"):
        inputs.read_day(store, DAY)


def test_manual_evidence_write_denial_precedes_any_directory_creation(tmp_path):
    source = tmp_path / "upload.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\noriginal evidence")
    def deny(path): raise RuntimeError("not authorized")
    with pytest.raises(RuntimeError, match="not authorized"):
        inputs.archive_evidence(tmp_path / "store", source, authorize=deny)
    assert not (tmp_path / "store").exists()


def test_concurrent_supplement_cannot_overwrite_newer_revision(tmp_path):
    first = inputs.publish(tmp_path, snapshot(), authorize=lambda p: None)
    updated = snapshot()
    updated["domestic"]["M2705"] = 3010.
    inputs.publish(tmp_path, updated, authorize=lambda p: None)
    with pytest.raises(ValueError, match="并发"):
        inputs.publish(tmp_path, snapshot(), authorize=lambda p: None, expected_identity=first)
    assert inputs.read_day(tmp_path, DAY)["domestic"]["M2705"] == 3010.


def test_domestic_adapter_preserves_date_handles_appended_depth_and_absent_contract(monkeypatch):
    fixed = datetime(2026, 10, 8, 9, 35, tzinfo=sources.SHANGHAI)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return fixed
    monkeypatch.setattr(sources, "datetime", Clock)
    fields = ["豆粕2705", "093459", "2984", "3020", "2981", "0", "3019", "3020", "3019", "0", "2984", "201", "881", "829585", "83207", "连", "豆粕", "2026-10-08"] + [""] * 10 + ["3004.368", "0"] * 10
    def get(url, **kwargs):
        response = sources.requests.Response()
        response.status_code, response.url, response.encoding = 200, url, "utf-8"
        text = ",".join(fields) if "nf_M2705" in url else ""
        symbol = "M2705" if text else "M2801"
        response._content = ('var hq_str_nf_' + symbol + '="' + text + '";\n').encode()
        return response
    monkeypatch.setattr(sources.requests, "get", get)
    prices, meta, errors = sources.domestic_worker(["M2705", "M2801"])
    assert prices == {"M2705": 3019., "M2801": None}
    assert meta["quotes"]["M2705"]["quoted_at"] == "2026-10-08T09:34:59+08:00"
    assert errors == {"M2801": "contract_not_available"}
    fields[17] = "2026-10-07"
    prices, _, errors = sources.domestic_worker(["M2705"])
    assert prices["M2705"] is None and errors["M2705"] == "quote_stale_or_future"


def test_current_capture_requires_real_trading_calendar_before_quotes(monkeypatch):
    now = datetime(2026, 10, 8, 9, 35, tzinfo=sources.SHANGHAI)
    monkeypatch.setattr(sources, "_worker", lambda *args: ["2026-09-30"])
    with pytest.raises(sources.SourceError, match="calendar_out_of_range"):
        sources.capture("secret", now=now)
    monkeypatch.setattr(sources, "_worker", lambda *args: ["2026-10-07", "2026-10-09"])
    with pytest.raises(sources.SourceError, match="market_holiday"):
        sources.capture("secret", now=now)


def test_cli_local_writer_cannot_bypass_deployed_service_grant(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "04_scripts/import_profit/capture_soybean_api.py"
    spec = importlib.util.spec_from_file_location("soybean_api_cli", script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setenv("MARKET_DATA_GIT_HEAD", "a" * 40)
    monkeypatch.setenv("SOYBEAN_MARGIN_LOCAL_PREVIEW", "1")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    def reject(**kwargs): raise RuntimeError("missing service grant")
    monkeypatch.setattr(cli, "establish_application_service_context", reject)
    with pytest.raises(RuntimeError, match="service grant"):
        cli.writer()
    assert not (tmp_path / "market-data-runtime").exists()
