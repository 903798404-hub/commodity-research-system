from datetime import date
from io import BytesIO
import importlib.util
from pathlib import Path
import sys
from zipfile import ZipFile

import openpyxl
import pytest
import requests

from agri_research_agent.oilseed_positions.model import config, metrics, preview_root
from agri_research_agent.oilseed_positions.sources import EURO_HEADERS, parse_euronext, parse_dce, parse_sina, SourceNotPublished, Sources
from agri_research_agent.sugar_positions.sources import parse_cftc, parse_czce

ROOT = Path(__file__).resolve().parents[2]
STAMP = "2026-10-08T00:00:00Z"
URL = "https://official.example/report"


def euro(kind="COMB", total="11.25", publication="07/10/2026"):
    title = f"Contract code ECO Identifier XMAT Report date 02/10/2026 Publication date {publication} Report type {kind}"
    def cells(values):
        return "".join(f"<td>{v}</td>" for v in values)
    header = cells(["", "Notation of the position quantity", ""]) + "".join(f'<td colspan="2">{h}</td>' for h in EURO_HEADERS)
    return (f'<table><tr><td colspan="13">{title}</td></tr><tr>{header}</tr>'
        f'<tr>{cells(["", "", ""] + ["Long", "Short"]*5)}</tr>'
        f'<tr><td rowspan="3">Number of positions</td><td rowspan="3">LOTS</td>{cells(["Risk Reducing directly related to commercial activities"]+["1"]*10)}</tr>'
        f'<tr>{cells(["Other"]+["10.25" if kind == "COMB" else "10"]*10)}</tr>'
        f'<tr>{cells(["Total"]+[total]*10)}</tr></table>').encode()


def test_euro_expands_row_and_column_spans_without_truncating_delta_lots():
    rows = parse_euronext(euro(), date(2026,10,7), URL, STAMP)
    assert len(rows) == 5 and rows[1]["group"] == "investment_funds"
    assert rows[1]["long"] == 11.25 and rows[1]["unit"] == "delta_equivalent_contracts"
    assert rows[1]["open_interest"] is None
    assert rows[1]["report_date"] == "2026-10-02" and rows[1]["publication_date"] == "2026-10-07"


@pytest.mark.parametrize("raw,match", [
    (euro().replace(b"ECO", b"EBM"), "非欧洲"),
    (euro(publication="06/10/2026"), "日期"),
    (euro(total="12.25"), "明细与合计"),
    (euro().replace(b"Investment Funds", b"Unknown Funds"), "表头"),
    (euro().replace(b"Report type COMB", b""), "口径"),
    (euro(kind="FUTR", total="11.25"), "整数"),
    (euro().replace(b"11.25", b"NaN"), "非负"),
])
def test_euro_rejects_changed_identity_grain_totals_or_headers(raw, match):
    with pytest.raises(ValueError, match=match):
        parse_euronext(raw, date(2026,10,7), URL, STAMP)


def test_euro_pure_and_combined_never_share_business_keys():
    rows = parse_euronext(euro(kind="FUTR", total="11") + euro(), date(2026,10,7), URL, STAMP)
    assert len(rows) == 10 and {r["report_type"] for r in rows} == {"futures_only", "combined"}
    with pytest.raises(ValueError, match="重复"):
        parse_euronext(euro()+euro(), date(2026,10,7), URL, STAMP)


def dce(day="20260930", contract="m2701", total=100, value="100", rank="1"):
    text = (f"名次\t会员简称\t持买单量\t增减\n{rank}\t东证期货\t{value}\t-1\n合计\t\t{total}\t\n"
        f"名次\t会员简称\t持卖单量\t增减\n1\t东证期货\t50\t2\n合计\t\t50\t\n")
    raw = BytesIO()
    with ZipFile(raw, "w") as archive:
        archive.writestr(f"{day}_{contract}_持仓排名.txt", text.encode("gb18030"))
    return raw.getvalue()


def test_dce_keeps_contract_scope_and_undisclosed_account():
    rows = parse_dce(dce(), date(2026,9,30), ("M","Y"), URL, STAMP)
    assert len(rows) == 2 and all(r["scope"] == "M2701" and r["account"] == "未区分" for r in rows)
    assert rows[0]["reported_change"] == -1


@pytest.mark.parametrize("raw,match", [
    (b"<html>412</html>", "ZIP"), (dce(day="20260929"), "日期"),
    (b"PK broken ZIP", "损坏"),
    (dce(contract="a2701"), "目标品种"), (dce(total=101), "合计"),
    (dce(value="1.5"), "整数"), (dce(rank="2"), "不连续"),
])
def test_dce_rejects_html_wrong_date_wrong_product_and_invalid_ranking(raw, match):
    with pytest.raises(ValueError, match=match):
        parse_dce(raw, date(2026,9,30), ("M","Y"), URL, STAMP)


def test_dce_requires_an_explicit_target_contract_before_network():
    with pytest.raises(ValueError, match="明确"):
        Sources().dce(date(2026,9,30), ("M", "Y"), "all")


def sina(contract="M2701", day="2026-09-30", total="100", empty=False):
    content = f'<select name="t_breed"><option selected value="{contract}">{contract}</option></select><input name="t_date" value="{day}">'
    for title in ("多单持仓", "空单持仓"):
        content += f"<table><tr><th>名次</th><th>会员简称</th><th>{title}</th><th>比上交易增减</th></tr>"
        if not empty:
            content += "<tr><td>1</td><td>东证期货</td><td>100</td><td>-2</td></tr>"
        content += f'<tr><td>合计</td><td></td><td>{"" if empty else total}</td><td></td></tr></table>'
    return content.encode("gb18030")


def test_sina_preserves_secondary_provenance_scope_and_account_disclosure():
    rows = parse_sina(sina(), date(2026,9,30), "M2701", URL, STAMP)
    assert len(rows) == 2 and all(r["source_provider"] == "sina" and r["scope"] == "M2701" and r["account"] == "未区分" for r in rows)
    assert rows[0]["reported_change"] == -2


@pytest.mark.parametrize("raw,match", [
    (sina(contract="Y2701"), "合约"), (sina(day="2026-09-29"), "日期"),
    (sina(total="101"), "合计"), (sina().replace("会员简称".encode("gb18030"), "未知列".encode("gb18030")), "表头"),
])
def test_sina_cannot_silently_return_default_contract_date_or_corrupted_values(raw,match):
    with pytest.raises(ValueError, match=match):
        parse_sina(raw, date(2026,9,30), "M2701", URL, STAMP)


def test_sina_empty_holiday_is_missing_not_zero():
    with pytest.raises(SourceNotPublished):
        parse_sina(sina(empty=True), date(2026,9,30), "M2701", URL, STAMP)


def test_soybean_scope_excludes_both_domestic_soybeans():
    spec = config(ROOT)["soybean"]
    assert set(spec["domestic"]) == {"M", "Y"}
    assert set(spec["foreign"]) == {"cbot_soybean", "cbot_meal", "cbot_oil"}


@pytest.mark.parametrize("market", ["canola", "cbot_soybean", "cbot_meal", "cbot_oil"])
def test_cftc_explicit_market_code_and_name_cannot_mix_products(market):
    source_spec = next(s for d in config(ROOT).values() for m,s in d["foreign"].items() if m == market)
    row = dict(cftc_contract_market_code=source_spec["code"], market_and_exchange_names=source_spec["name"],
        report_date_as_yyyy_mm_dd="2026-09-29T00:00:00", open_interest_all="100",
        tot_rept_positions_long_all="80", tot_rept_positions_short="80")
    for field in ("prod_merc_positions_long", "prod_merc_positions_short", "swap_positions_long_all",
        "swap__positions_short_all", "m_money_positions_long_all", "m_money_positions_short_all",
        "other_rept_positions_long", "other_rept_positions_short", "nonrept_positions_long_all", "nonrept_positions_short_all"):
        row[field] = "20"
    for field in ("swap__positions_spread_all", "m_money_positions_spread", "other_rept_positions_spread"):
        row[field] = "0"
    kwargs = dict(market=market, expected_code=source_spec["code"], expected_name=source_spec["name"])
    parsed = parse_cftc([row], "futures_only", URL, STAMP, **kwargs)
    assert {r["market"] for r in parsed} == {market}
    row["cftc_contract_market_code"] = "080732"
    with pytest.raises(ValueError, match="非目标"):
        parse_cftc([row], "futures_only", URL, STAMP, **kwargs)


def test_metrics_preserve_decimals_comparison_dates_and_large_gap():
    rows = [dict(market="euronext_rapeseed", report_type="combined", group="investment_funds",
        report_date=d, long=l, short=2.1) for d,l in [("2026-09-25",5.15),("2026-10-02",5.25),("2026-10-16",7.25)]]
    result = metrics(rows)
    assert result[1]["net_change"] == 0.1 and result[1]["previous_date"] == "2026-09-25"
    assert result[2]["net_change"] is None


def test_preview_never_targets_primary_checkout_or_another_domain(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / ".git").mkdir()
    with pytest.raises(ValueError, match="worktree"):
        preview_root(primary, "rapeseed")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: fixture", encoding="utf-8")
    (linked / "02_configs").mkdir()
    (linked / "02_configs/oilseed_positions.json").write_bytes((ROOT / "02_configs/oilseed_positions.json").read_bytes())
    assert preview_root(linked,"palm") != preview_root(linked,"soybean")
    with pytest.raises(ValueError, match="板块"):
        preview_root(linked,"soybean-profit")


def test_czce_retains_explicit_variety_and_contract_and_excludes_sugar():
    wb = openpyxl.Workbook()
    ws = wb.active
    for scope in ("OI", "OI701", "SR"):
        ws.append([f"品种：名称{scope} 日期：2026-09-30"])
        ws.append(["名次","会员简称","交易量（手）","增减量","会员简称","持买仓量","增减量","会员简称","持卖仓量","增减量"])
        ws.append([1,"甲",1,0,"甲（代客）",100,0,"乙（代客）",90,0])
        ws.append(["合计",None,1,None,None,100,None,None,90,None])
    raw = BytesIO(); wb.save(raw); wb.close()
    rows = parse_czce(raw.getvalue(), "2026-09-30", URL, STAMP, varieties=("RS","OI","RM"))
    assert {r["scope"] for r in rows} == {"OI","OI701"}


def test_collector_dce_failure_is_single_bounded_attempt_and_keeps_other_market_rows():
    path = ROOT / "04_scripts/oilseed_positions/update_positions.py"
    spec = importlib.util.spec_from_file_location("oilseed_collector_test", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    class Fake:
        calls = 0
        def cftc(self,*args):
            return [{"market": args[0]}], b"real-source", URL
        def dce(self,*args):
            self.calls += 1
            response = requests.Response(); response.status_code = 412
            raise requests.HTTPError(response=response)
    source = Fake()
    foreign, domestic, captures, attempts = module.collect(source, config(ROOT)["soybean"],
        markets="all", start_year=2025, start_day=date(2026,9,1), end_day=date(2026,9,30),
        euro_start=date(2026,9,30), seed_contract="m2701")
    assert source.calls == 1 and len(foreign) == 6 and not domestic and len(captures) == 6
    assert attempts[-1]["status"] == "failed" and attempts[-1]["error"] == "HTTP 412"
