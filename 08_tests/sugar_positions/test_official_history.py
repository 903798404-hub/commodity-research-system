from io import BytesIO
import hashlib
import json
from zipfile import ZipFile, ZIP_DEFLATED

import pytest

from agri_research_agent.sugar_positions.sources import parse_czce_cofco_capture as parse_capture
from agri_research_agent.sugar_positions.model import (
    COFCO_MEMBER, COFCO_OFFICIAL_RECORD, GOLDMAN_OFFICIAL_RECORD,
    cofco_official_metrics, domestic_metrics, goldman_official_metrics, member_metrics,
)
from agri_research_agent.sugar_positions.charts import seasonality


def capture(day='2026-11-02', account='（代客）', bad_total=False):
    url=f'https://www.czce.com.cn/cn/DFSStaticFiles/Future/{day[:4]}/{day.replace("-", "")}/FutureDataHolding.txt'
    text=(f'郑州商品交易所期货持仓排名表({day})\n\n品种：白糖SR    日期：{day}\n'
          '名次|会员简称|成交量（手）|增减量|会员简称|持买仓量|增减量|会员简称|持卖仓量|增减量\n'
          f'1|中粮期货{account}|10|0|中粮期货{account}|68,525|0|其他期货{account}|100|0\n'
          f'2|其他期货{account}|10|0|其他期货{account}|100|0|中粮期货{account}|62,254|0\n'
          f'合计||20|0||{68626 if bad_total else 68625}|0||62354|0\n'
          f'合约：SR701    日期：{day}\n')
    raw=text.encode('utf-8')
    manifest=dict(schema_version='czce-text-lossless/1',report_date=day,source_url=url,
                  source_bytes=len(raw),source_sha256=hashlib.sha256(raw).hexdigest())
    out=BytesIO()
    with ZipFile(out,'w',compression=ZIP_DEFLATED) as z:
        z.writestr('FutureDataHolding.txt',raw);z.writestr('manifest.json',json.dumps(manifest))
    return out.getvalue(),url


def test_official_product_slice_matches_wenhua_example_and_never_becomes_a_full_leaderboard():
    raw,url=capture()
    rows=parse_capture(raw,'2026-11-02',url,'2026-11-03T00:00:00+00:00')
    assert [(r['side'],r['positions'],r['rank']) for r in rows]==[('long',68525,1),('short',62254,2)]
    assert all(r['scope']=='SR' and r['record_type']==COFCO_OFFICIAL_RECORD for r in rows)
    metric=cofco_official_metrics(rows)[0]
    assert metric['net']==6271 and metric['reported_account']=='代客'
    assert member_metrics(rows,COFCO_MEMBER)==[]
    assert domestic_metrics(rows,[])==[]


def test_official_lossless_capture_is_replayed_only_for_sugar():
    from pathlib import Path
    from agri_research_agent.positions.delivery import _replay_source
    from agri_research_agent.oilseed_positions.model import config
    raw,url=capture()
    source=dict(url=url,retrieved_at='2026-11-03T00:00:00+00:00')
    spec=config(Path(__file__).resolve().parents[2])
    kind,rows=_replay_source('czce_cofco_official_20261102',source,raw,'sugar',spec)
    assert kind=='domestic' and len(rows)==2 and rows[0]['positions']==68525
    with pytest.raises(ValueError):_replay_source('czce_cofco_official_20261102',source,raw,'rapeseed',spec)


def test_legacy_account_remains_unclassified_and_account_boundary_breaks_line():
    old,url=capture('2024-05-23',account='')
    new,url2=capture('2024-05-24')
    rows=parse_capture(old,'2024-05-23',url,'2026-11-03T00:00:00+00:00')
    rows+=parse_capture(new,'2024-05-24',url2,'2026-11-03T00:00:00+00:00')
    metrics=cofco_official_metrics(rows)
    assert [r['reported_account'] for r in metrics]==['未区分','代客']
    assert metrics[1]['net_change'] is None
    figure=seasonality(metrics,'中粮官方汇总',current_year=2026)
    assert list(figure.data[0].y)==[6271,None,6271]


def test_goldman_projection_replays_existing_official_evidence_without_changing_cofco():
    from pathlib import Path
    from agri_research_agent.positions.delivery import _part, _replay_source
    from agri_research_agent.oilseed_positions.model import config
    from agri_research_agent.sugar_positions.model import load_members
    from agri_research_agent.sugar_positions.sources import parse_czce_goldman_capture
    root = Path(__file__).resolve().parents[2]
    member = next(m for m in load_members(root / "02_configs/sugar_positions.json") if m["id"] == "goldman")
    original, url = capture()
    with ZipFile(BytesIO(original)) as z:
        body = z.read("FutureDataHolding.txt").replace("中粮期货".encode(), "乾坤期货".encode())
        manifest = json.loads(z.read("manifest.json"))
    manifest["source_bytes"] = len(body)
    manifest["source_sha256"] = hashlib.sha256(body).hexdigest()
    out = BytesIO()
    with ZipFile(out, "w", compression=ZIP_DEFLATED) as z:
        z.writestr("FutureDataHolding.txt", body)
        z.writestr("manifest.json", json.dumps(manifest))
    raw = out.getvalue()
    stamp = "2026-11-03T00:00:00+00:00"
    goldman = parse_czce_goldman_capture(raw, "2026-11-02", url, stamp, member)
    assert all(row["record_type"] == GOLDMAN_OFFICIAL_RECORD for row in goldman)
    assert goldman_official_metrics(goldman, member)[0]["net"] == 6271
    assert _part(goldman[0], "domestic") != _part(parse_capture(raw, "2026-11-02", url, stamp)[0], "domestic")
    kind, replayed = _replay_source("czce_goldman_official_20261102",
        dict(url=url, retrieved_at=stamp), raw, "sugar", config(root), project_root=root)
    assert kind == "domestic" and replayed == goldman
    assert cofco_official_metrics(goldman) == []
    assert member_metrics(goldman, member) == []
    cofco = parse_capture(original, "2026-11-02", url, stamp)
    assert cofco_official_metrics(cofco + goldman)[0]["net"] == 6271
    assert goldman_official_metrics(cofco + goldman, member)[0]["net"] == 6271
    old = [dict(row, report_date="2026-10-30", account="未区分") for row in goldman]
    series = goldman_official_metrics(old + goldman, member)
    assert series[-1]["net"] == 6271 and series[-1]["net_change"] is None
    assert series[-1]["reported_account"] == "代客"


@pytest.mark.parametrize('change',['date','url','total','duplicate_entry','sha'])
def test_history_capture_rejects_identity_or_ranking_damage(change):
    raw,url=capture(bad_total=change=='total')
    day='2026-11-02'
    if change=='date':day='2026-11-01'
    if change=='url':url+='?unverified=1'
    if change in {'duplicate_entry','sha'}:
        with ZipFile(BytesIO(raw)) as z:body=z.read('FutureDataHolding.txt');manifest=json.loads(z.read('manifest.json'))
        if change=='sha':manifest['source_sha256']='0'*64
        out=BytesIO()
        with ZipFile(out,'w',compression=ZIP_DEFLATED) as z:
            z.writestr('FutureDataHolding.txt',body);z.writestr('manifest.json',json.dumps(manifest))
            if change=='duplicate_entry':z.writestr('unexpected.txt',b'not official data')
        raw=out.getvalue()
    with pytest.raises(ValueError):parse_capture(raw,day,url,'2026-11-03T00:00:00+00:00')
