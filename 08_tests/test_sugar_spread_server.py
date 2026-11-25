from datetime import date, datetime, timezone
from hashlib import sha256
import importlib.util
from io import BytesIO
import json
from pathlib import Path
from zipfile import BadZipFile

from openpyxl import Workbook
import pytest
import requests

from agri_research_agent.application.sugar_spread_sources import report_url
from agri_research_agent.application.sugar_spreads import load_quotes, resolve_server_quotes


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "09_deploy/sugar_spread_server"
TRADE_DAY = date(2026, 11, 24)
NOW = datetime(2026, 11, 25, 10, 30, tzinfo=timezone.utc)


def load_tool(name):
    spec = importlib.util.spec_from_file_location(f"sugar_spread_server_{name}", DEPLOY / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def adapters(monkeypatch):
    import agri_research_agent.application.sugar_spread_sources as sources

    monkeypatch.setattr(sources, "sleep", lambda seconds: None)
    book = Workbook(write_only=True)
    sheet = book.create_sheet()
    sheet.append([f"郑州商品交易所期货每日行情表({TRADE_DAY})"])
    sheet.append(["合约代码", "今收盘", "成交量(手)"])
    sheet.append(["SR701", 5114, 495948])
    sheet.append(["SR705", 5159, 9])
    buffer = BytesIO()
    book.save(buffer)
    czapp = b'''<h1>Daily Market Price Updates and Commentary 24th November 2026</h1>
    <script>{"datePublished":"2026-11-24T18:11:05+00:00"}</script>
    <h2>ICE Futures U.S. Sugar No.11 Contract</h2>
    <table><tr><th>Settlement (cUS$/lb)</th></tr>
    <tr><td>Mar27</td><td>17.66</td></tr><tr><td>May27</td><td>17.08</td></tr></table>
    <h2>ICE Europe White Sugar Futures Contract</h2>
    <table><tr><th>Settlement (US$/mt)</th></tr>
    <tr><td>Mar27</td><td>501.00</td></tr><tr><td>May27</td><td>505.40</td></tr></table>'''
    return {report_url(TRADE_DAY, "domestic"): buffer.getvalue(), report_url(TRADE_DAY, "foreign"): czapp}


def test_joint_candidate_replays_and_publishes_only_sugar(tmp_path, monkeypatch, adapters):
    collector, runner = load_tool("collect"), load_tool("run")
    base = tmp_path / "processed/sugar_spreads"
    base.mkdir(parents=True)
    unrelated = tmp_path / "historical_spread_database.parquet"
    unrelated.write_bytes(b"unrelated formal 1-5 spread history")
    target = tmp_path / "candidate"
    result = collector.collect(base, target, NOW, fetcher=adapters.get)
    assert collector.validate(base, target, NOW) == result
    assert result["changes"] == {"domestic": 2, "foreign": 4}
    assert result["latest"] == {"domestic": str(TRADE_DAY), "foreign": str(TRADE_DAY)}
    assert not resolve_server_quotes(tmp_path)

    host = runner.load_host()
    monkeypatch.setattr(host, "_protected", lambda path, **kwargs: host._no_links(path))
    store = base / "official-releases"
    assert runner.publish_candidate(host, store, target, result, runner.EMPTY_POINTER)
    paths = resolve_server_quotes(tmp_path)
    assert {market: len(load_quotes(path, as_of=NOW.date())) for market, path in paths.items()} == result["changes"]
    assert unrelated.read_bytes() == b"unrelated formal 1-5 spread history"
    with pytest.raises(ValueError, match="baseline changed"):
        runner.publish_candidate(host, store, target, result, runner.EMPTY_POINTER)

    later = tmp_path / "next-candidate"
    calls = []
    repeated = collector.collect(base, later, NOW, fetcher=lambda url: calls.append(url))
    assert collector.validate(base, later, NOW) == repeated
    assert repeated["status"] == "NO_CHANGE" and repeated["pointer"] == result["pointer"]
    assert all(url not in calls for url in adapters)
    old_pointer = (store / "current.json").read_bytes()
    assert not runner.publish_candidate(host, store, later, repeated, old_pointer)
    assert (store / "current.json").read_bytes() == old_pointer


def test_failed_source_preserves_other_market_and_tampering_stops_replay(tmp_path, adapters):
    collector = load_tool("collect")
    base = tmp_path / "empty"
    base.mkdir()
    target = tmp_path / "candidate"

    def fetch(url):
        if url == report_url(TRADE_DAY, "foreign"):
            raise requests.Timeout("local fixture timeout")
        return adapters.get(url)

    result = collector.collect(base, target, NOW, fetcher=fetch)
    assert result["status"] == "PARTIAL_FAILURE"
    assert result["latest"] == {"domestic": str(TRADE_DAY), "foreign": None}
    assert set(result["pointer"]["markets"]) == {"domestic"}
    assert collector.validate(base, target, NOW) == result
    result_file = target / "result.json"
    raw = next((target / "raw").iterdir())
    raw.write_bytes(b"other file")
    with pytest.raises(ValueError, match="identity differs"):
        collector.validate(base, target, NOW)
    receipt = next(receipt for receipt in result["receipts"] if receipt["status"] == "verified")
    receipt["sha256"] = sha256(raw.read_bytes()).hexdigest()
    result_file.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(BadZipFile, match="zip file"):
        collector.validate(base, target, NOW)


def test_workers_have_separate_network_and_write_boundaries(tmp_path):
    runner = load_tool("run")
    baseline, output = tmp_path / "baseline", tmp_path / "output"
    configs = {"image_id": "sha256:" + "d" * 64}
    collect = runner.worker_command(configs, baseline, output, NOW.isoformat(), "collect", validate=False)
    verify = runner.worker_command(configs, baseline, output, NOW.isoformat(), "verify", validate=True)
    assert collect[collect.index("--network") + 1] == "bridge"
    assert verify[verify.index("--network") + 1] == "none"
    assert f"type=bind,src={baseline},dst=/baseline,readonly" in collect and "--read-only" in collect
    assert f"type=bind,src={output},dst=/output,readonly" in verify and "--read-only" in verify
    assert collect[collect.index("--pull") + 1] == verify[verify.index("--pull") + 1] == "never"
