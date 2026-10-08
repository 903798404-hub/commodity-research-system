import importlib.util
import json
from pathlib import Path

import pytest

from agri_research_agent.oilseed_positions.aggregation import (
    METHOD, aggregate_contract_rows, domestic_metrics, parse_browser_capture,
)
from agri_research_agent.sugar_positions.model import load_members

ROOT = Path(__file__).resolve().parents[2]
MEMBERS = load_members(ROOT / "02_configs/sugar_positions.json")


def row(scope, member, positions, side="long", rank=1, day="2026-09-30"):
    return dict(scope=scope, member=member, raw_member=member + "(代客)", account="代客",
        positions=positions, side=side, rank=rank, report_date=day, reported_change=0,
        unit="contracts", source_provider="eastmoney", source_url="https://example.test/raw",
        retrieved_at="2026-10-08T09:00:00Z")


def report(contract, contracts, day="2026-10-08"):
    tables = []
    for title, value in [("多头龙虎榜", 100), ("空头龙虎榜", 60)]:
        tables.append(dict(heading="豆粕" + contract[1:] + title,
            rows=[dict(id="10083138", cells=["1", "国泰君安(代客)", str(value), "0"])],
            text=f"本日合计 {value} 0 上日合计 {value}"))
    return dict(contract=contract, contracts=contracts, date=day, scope=["豆粕" + contract[1:]],
        tables=tables, source_url=f"https://qhweb.eastmoney.com/lhb/dkcc/dce/{contract.lower()}",
        retrieved_at="2026-10-08T09:00:00Z")


def capture():
    contracts = ["M2701", "M2705"]
    return dict(schema_version=1, acquisition="browser_dom", report_date="2026-10-08",
        reports=[report(c, contracts) for c in contracts])


def parsed(payload):
    return parse_browser_capture(json.dumps(payload, ensure_ascii=False).encode("utf-8"), ("M",))


def test_reranks_members_after_summing_and_keeps_all_observed_members():
    rows = []
    for scope in ("M2701", "M2705"):
        rows += [row(scope, "甲", 70 if scope.endswith("01") else 10),
            row(scope, "乙", 50, rank=2), row(scope, "丙", 5, side="short")]
    result = aggregate_contract_rows(rows, ["M2701", "M2705"])
    assert [(r["member"], r["positions"], r["rank"]) for r in result if r["side"] == "long"] == [("乙", 100, 1), ("甲", 80, 2)]
    assert all(r["aggregation"] == METHOD and r["constituent_contracts"] == ["M2701", "M2705"] for r in result)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "side", "date", "provider"])
def test_incomplete_or_mixed_contracts_never_publish_summary(mutation):
    rows = [row(c, "甲", 10, side=s) for c in ["M2701", "M2705"] for s in ["long", "short"]]
    if mutation == "missing": rows = rows[:2]
    if mutation == "duplicate": rows.append(dict(rows[0]))
    if mutation == "side": rows.pop()
    if mutation == "date": rows[0]["report_date"] = "2026-09-29"
    if mutation == "provider": rows[0]["source_provider"] = "sina"
    with pytest.raises(ValueError):
        aggregate_contract_rows(rows, ["M2701", "M2705"])


def test_unlisted_side_is_not_zero_and_contract_coverage_remains_visible():
    rows = [row(c, "高盛期货", 10) for c in ["M2701", "M2705"]]
    rows += [row(c, "其他", 20, side="short") for c in ["M2701", "M2705"]]
    result = domestic_metrics(aggregate_contract_rows(rows, ["M2701", "M2705"]), MEMBERS)
    goldman = next(r for r in result if r["group"] == "goldman")
    assert goldman["long"] == 20 and goldman["short"] is None and goldman["net"] is None
    assert "多仓 2/2" in goldman["coverage"] and "空仓 0/2" in goldman["coverage"]


def test_changes_use_saved_net_difference_and_suppress_changed_contract_universe():
    rows = []
    for day, contracts, amount in [("2026-09-29", ["M2701"], 10),
        ("2026-09-30", ["M2701", "M2705"], 20), ("2026-10-08", ["M2701", "M2705"], 25)]:
        inputs = [row(c, "国泰君安", amount, side=s, day=day) for c in contracts for s in ("long", "short")]
        for r in inputs:
            if r["side"] == "short": r["positions"] = 5
        rows.extend(aggregate_contract_rows(inputs, contracts))
    result = [r for r in domestic_metrics(rows, MEMBERS) if r["group"] == "guotai"]
    assert result[1]["net_change"] is None and "合约范围变化" in result[1]["coverage"]
    assert result[2]["net_change"] == 10 and result[2]["previous_date"] == "2026-09-30"


def test_browser_capture_checks_totals_and_skips_stale_contract_with_evidence():
    payload = capture()
    payload["reports"][1]["date"] = "2026-09-15"
    rows, exclusions = parsed(payload)
    assert {r["scope"] for r in rows} == {"M", "M2701"}
    assert exclusions[0]["scope"] == "M2705" and exclusions[0]["report_date"] == "2026-09-15"


@pytest.mark.parametrize("mutation", ["total", "selection", "net_page", "missing_contract", "future", "estimate", "direction"])
def test_browser_capture_cannot_accept_estimates_mismatched_or_partial_reports(mutation):
    payload = capture()
    r = payload["reports"][0]
    if mutation == "total": r["tables"][0]["text"] = "本日合计 101 0"
    if mutation == "selection": r["scope"] = ["豆粕2705"]
    if mutation == "net_page": r["source_url"] = r["source_url"].replace("dkcc", "jcc")
    if mutation == "missing_contract": payload["reports"].pop()
    if mutation == "future": r["date"] = "2026-10-09"
    if mutation == "estimate": r["tables"][0]["rows"][0]["cells"][2] = "100*"
    if mutation == "direction": r["tables"].reverse()
    with pytest.raises(ValueError):
        parsed(payload)


@pytest.mark.parametrize("bad_input", [False, True])
def test_import_validates_all_inputs_before_switching_local_pointer(tmp_path, monkeypatch, bad_input):
    from agri_research_agent.sugar_positions.storage import publish, read_snapshot
    module_spec = importlib.util.spec_from_file_location("import_rankings_test",
        ROOT / "04_scripts/oilseed_positions/import_contract_rankings.py")
    cli = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.delenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", raising=False)
    (tmp_path / ".git").write_text("gitdir: fixture", encoding="utf-8")
    (tmp_path / "02_configs").mkdir()
    (tmp_path / "02_configs/oilseed_positions.json").write_bytes((ROOT / "02_configs/oilseed_positions.json").read_bytes())
    data_root = tmp_path / "01_data/oilseed_positions_preview/soybean"
    publish(data_root, [], [row("M2701", "旧会员", 10)], [], [])
    old_pointer = (data_root / "current.json").read_bytes()
    paths = []
    for index in (0, 1):
        payload = capture()
        if index == 0:
            payload["report_date"] = "2026-09-30"
            for r in payload["reports"]: r["date"] = "2026-09-30"
        elif bad_input:
            payload["reports"][0]["tables"][0]["text"] = "本日合计 999 0"
        path = tmp_path / f"capture{index}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        paths.append(str(path))
    args = ["--domain", "soybean", "--captures", *paths]
    if bad_input:
        with pytest.raises(ValueError, match="合计"):
            cli.main(args)
        assert (data_root / "current.json").read_bytes() == old_pointer
    else:
        assert cli.main(args) == 0
        snapshot = read_snapshot(data_root)
        assert {r["report_date"] for r in snapshot["domestic"]} == {"2026-09-30", "2026-10-08"}
        assert len(list((data_root / "releases").iterdir())) == 2
        assert all(r["aggregation"] == METHOD for r in snapshot["domestic"] if r["scope"] == "M")
