from copy import deepcopy
from datetime import date, datetime, timezone
import json

import pytest

from agri_research_agent.oilseed_positions import stock_api as stock
from agri_research_agent.oilseed_positions.aggregation import domestic_metrics
from agri_research_agent.positions import delivery, server_collection
from test_positions_delivery import archive, ROOT

DAY = date(2026, 10, 9)
STAMP = '2026-10-09T09:00:00Z'
FIELDS = ['trade_date', 'symbol', 'broker', 'long_hld', 'long_chg', 'short_hld', 'short_chg']


def capture(contracts=('M2701', 'Y2701', 'P2701')):
    items = []
    for contract in contracts:
        items.extend([['20261009', contract, '东证期货（代客）', 100., 3., 90., -2.],
                      ['20261009', contract, '永安期货（代客）', None, None, 80., 1.]])
    return dict(schema_version=stock.SCHEMA, trade_date='20261009', exchange='DCE',
                varieties=list(dict.fromkeys(c[0] for c in contracts)), scope_contract=None,
                pages=[dict(fields=list(FIELDS), items=items)])


def parse(value):
    return stock.parse_capture(delivery.canonical(value), stock.URL, STAMP)


def test_separate_sides_keep_missing_and_rerank_member_sums():
    rows = parse(capture(('M2701', 'M2705')))
    detail = [r for r in rows if r['scope'] == 'M2701']
    assert len(detail) == 3 and not any(r['member'] == '永安期货' and r['side'] == 'long' for r in detail)
    summary = [r for r in rows if r['scope'] == 'M']
    assert next(r['positions'] for r in summary if r['member'] == '东证期货' and r['side'] == 'long') == 200
    assert all(r['account'] == '代客' and r['source_provider'] == 'stock_api' for r in rows)
    assert [r['rank'] for r in detail if r['side'] == 'short'] == [1, 2]


@pytest.mark.parametrize('change', ['date', 'scope', 'duplicate', 'negative', 'fraction', 'side', 'unknown_field'])
def test_invalid_disclosure_is_not_published(change):
    value = capture()
    items = value['pages'][0]['items']
    if change == 'date': items[0][0] = '20261008'
    if change == 'scope': items[0][1] = 'M'
    if change == 'duplicate': items.append(items[0])
    if change == 'negative': items[0][3] = -1
    if change == 'fraction': items[0][3] = .5
    if change == 'side': items[0][3] = None
    if change == 'unknown_field': value['pages'][0]['fields'][0] = 'token'
    with pytest.raises(ValueError):
        parse(value)


def test_truncated_page_and_unexpected_contract_rejected():
    value = capture()
    value['pages'].append(deepcopy(value['pages'][0]))
    with pytest.raises(ValueError, match='pagination'):
        parse(value)
    value = capture(('M2701',))
    value['scope_contract'] = 'M2705'
    with pytest.raises(ValueError, match='contract_mismatch'):
        parse(value)


def test_exact_contract_does_not_fabricate_whole_variety():
    value = capture(('M2701',))
    value['scope_contract'] = 'M2701'
    assert {r['scope'] for r in parse(value)} == {'M2701'}


def test_source_switch_preserves_disclosures_and_ranked_net_semantics():
    rows = parse(capture(('M2701',)))
    assert not any(r['member'] == '永安期货' and r['side'] == 'long' for r in rows)
    new = [dict(r, report_date='2026-10-08', source_provider='sina') for r in rows]
    members = json.loads((ROOT/'02_configs/sugar_positions.json').read_text(encoding='utf-8'))['members']
    metrics = domestic_metrics(rows + new, members)
    assert all(m['net_change'] is None for m in metrics)
    assert all(m['net_method'] == 'ranked_missing_zero_v1' for m in metrics)
    yongan = [m for m in metrics if m['group'] == 'yongan']
    assert yongan and all(m['long'] is None and m['short'] == 80 and m['net'] == -80
                          for m in yongan)
    fixed = [m for m in metrics if m['group'] == 'fixed5']
    assert fixed and all(m['long'] == 100 and m['short'] == 170 and m['net'] == -70
                         for m in fixed)
    absent = [m for m in metrics if m['group'] in {'goldman', 'jpmorgan', 'guotai'}]
    assert absent and all(m['long'] is None and m['short'] is None and m['net'] == 0
                          for m in absent)


class Source:
    def holdings(self, day, varieties, *, contract=None):
        value = capture((contract,) if contract else ('M2701', 'Y2701', 'P2701'))
        value['scope_contract'] = contract
        raw = delivery.canonical(value)
        return stock.parse_capture(raw, stock.URL, STAMP), raw, stock.URL


def test_server_candidate_replays_and_preserves_unrelated_data(tmp_path):
    baseline = archive()
    original = deepcopy(baseline)
    output = tmp_path/'candidate'
    receipt = server_collection.collect(baseline, output, ROOT, DAY, source=Source(), include_official=False, calendar=lambda: ['2026-10-09'])
    assert not receipt['published'] and not receipt['observations']['revised_partitions']
    candidate = delivery.read_json(output/'positions_archive.json')
    before = delivery.validate_archive(baseline, ROOT)[0]
    after = delivery.validate_archive(candidate, ROOT)[0]
    assert before['sugar'] == after['sugar'] and before['rapeseed'] == after['rapeseed']
    assert before['soybean']['foreign'] == after['soybean']['foreign']
    assert before['palm']['domestic'][0] in after['palm']['domestic']
    assert baseline == original
    assert {r['scope'] for r in after['soybean']['domestic']} == {'M2701','Y2701','M','Y'}


def test_server_same_date_existing_manual_record_is_preserved(tmp_path):
    baseline = archive(day=DAY.isoformat())
    receipt = server_collection.collect(baseline, tmp_path/'candidate', ROOT, DAY,
        source=Source(), include_official=False, contracts=['P2701'], calendar=lambda: [DAY.isoformat()])
    candidate = delivery.read_json(tmp_path/'candidate/positions_archive.json')
    assert not receipt['observations']['business_changed']
    assert delivery.validate_archive(candidate, ROOT)[0]['palm'] == delivery.validate_archive(baseline, ROOT)[0]['palm']


def test_no_source_response_keeps_old_snapshot(tmp_path):
    class Empty:
        def holdings(self, *a, **kw):
            raise stock.SourceNotPublished('stock_api_report_not_published')
    receipt = server_collection.collect(archive(), tmp_path/'candidate', ROOT, DAY,
        source=Empty(), include_official=False, calendar=lambda: [DAY.isoformat()])
    assert not receipt['observations']['business_changed']
    assert receipt['attempts'][0]['status'] == 'not_published'


def test_gateway_gzip_auth_is_not_saved_or_printed():
    data = capture(('M2701',))['pages'][0]
    class Response:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def iter_content(self,size):
            yield json.dumps(dict(code=0,msg='credential should not persist',data=data)).encode()
    class Session:
        def post(self, url, **kwargs):
            assert kwargs['json']['token'] == 'TEST_SECRET'
            assert kwargs['json']['params']['symbol'] == 'M2701'
            assert kwargs['headers']['Accept-Encoding'] == 'gzip'
            assert kwargs['allow_redirects'] is False
            return Response()
    _, raw, url = stock.StockApiSources(token='TEST_SECRET',session=Session()).holdings(DAY,('M',),contract='M2701')
    assert b'TEST_SECRET' not in raw and b'credential' not in raw and url == stock.URL


def test_output_overlap_and_holiday_rejected(tmp_path):
    with pytest.raises(ValueError, match='仓库外'):
        server_collection.collect(archive(), ROOT/'06_outputs/server-candidate', ROOT, DAY, source=Source())
    with pytest.raises(ValueError, match='日历'):
        server_collection.collect(archive(), tmp_path/'candidate', ROOT, DAY,
            source=Source(), include_official=False, calendar=lambda: ['2026-10-08'])


def test_missing_dce_credential_does_not_block_official_czce(tmp_path, monkeypatch):
    from io import BytesIO
    import openpyxl
    from agri_research_agent.sugar_positions.sources import parse_czce
    monkeypatch.delenv('STOCK_API_TOKEN', raising=False)
    class Official:
        def czce(self, day, varieties=('SR',)):
            book = openpyxl.Workbook()
            sheet = book.active
            for variety in varieties:
                sheet.append([f'品种：{variety} 日期：{day.isoformat()}'])
                sheet.append(['名次','会员简称','交易量（手）','增减量','会员简称','持买仓量',
                              '增减量','会员简称','持卖仓量','增减量'])
                sheet.append([1,'东证期货（代客）',100,0,'东证期货（代客）',100,0,
                              '东证期货（代客）',90,0])
                sheet.append(['合计',None,100,None,None,100,None,None,90,None])
            buffer = BytesIO()
            book.save(buffer)
            book.close()
            raw = buffer.getvalue()
            url = day.strftime('https://www.czce.com.cn/cn/DFSStaticFiles/Future/%Y/%Y%m%d/FutureDataHolding.xlsx')
            return parse_czce(raw,day.isoformat(),url,STAMP,varieties=varieties),raw,url
    receipt = server_collection.collect(archive(), tmp_path/'candidate', ROOT, DAY,
        official_sources={'sugar': Official(), 'rapeseed': Official()}, calendar=lambda: [DAY.isoformat()])
    assert receipt['attempts'][0]['error'] == 'stock_api_credential_missing_or_invalid'
    candidate = delivery.read_json(tmp_path/'candidate/positions_archive.json')
    saved = delivery.validate_archive(candidate, ROOT)[0]
    assert {r['scope'] for r in saved['rapeseed']['domestic']} == {'RS','OI','RM'}
    assert {r['scope'] for r in saved['sugar']['domestic']} == {'SR'}
    assert not saved['soybean']['domestic']


def test_production_root_cannot_be_used_for_candidates(tmp_path, monkeypatch):
    formal = tmp_path/'production'
    monkeypatch.setenv('PUBLIC_MARKET_DATA_RUNTIME_ROOT', str(formal))
    with pytest.raises(ValueError, match='正式runtime'):
        server_collection.collect(archive(), formal/'new-subdir', ROOT, DAY, source=Source())


def test_gateway_error_body_and_transport_exception_are_redacted():
    class Response:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def iter_content(self,size): yield b'{"code":403,"msg":"TEST_SECRET"}'
    class Session:
        def post(self,*a,**kw): return Response()
    with pytest.raises(stock.StockApiError, match='request_rejected') as failure:
        stock.StockApiSources(token='TEST_SECRET',session=Session()).holdings(DAY,('M',))
    assert 'TEST_SECRET' not in str(failure.value)
    class Broken:
        def post(self,*a,**kw): raise RuntimeError('TEST_SECRET')
    with pytest.raises(stock.StockApiError, match='transport_or_schema_failed') as failure:
        stock.StockApiSources(token='TEST_SECRET',session=Broken()).holdings(DAY,('M',))
    assert 'TEST_SECRET' not in str(failure.value)
