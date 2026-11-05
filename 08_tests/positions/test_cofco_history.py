from copy import deepcopy
from datetime import date
import json

import pytest

from agri_research_agent.oilseed_positions import stock_api as stock
from agri_research_agent.oilseed_positions.model import config
from agri_research_agent.positions import delivery
from agri_research_agent.sugar_positions.model import (
    COFCO_MEMBER, cofco_history_metrics, domestic_metrics, member_metrics, load_members,
)
from test_positions_delivery import ROOT, STAMP

FIELDS = ['trade_date', 'symbol', 'broker', 'long_hld', 'long_chg', 'short_hld', 'short_chg']


def capture():
    return dict(schema_version=stock.COFCO_HISTORY_SCHEMA, start_date='20220101', end_date='20261009',
        catalogue=dict(fields=['ts_code', 'symbol', 'exchange', 'fut_code', 'list_date', 'delist_date'], items=[
            ['SR2301.ZCE', 'SR301', 'CZCE', 'SR', '20220101', '20230110'],
            ['SR2601.ZCE', 'SR601', 'CZCE', 'SR', '20250101', '20260110']]),
        calendar=dict(fields=['exchange', 'cal_date', 'is_open'], items=[
            ['CZCE', '20221103', 1], ['CZCE', '20221104', 1], ['CZCE', '20260105', 1]]),
        reports=[dict(symbol='SR2301', pages=[dict(fields=FIELDS, items=[
            ['20221103', 'SR2301', '中粮期货', 100., 0., 30., -1.],
            ['20221104', 'SR2301', '其他期货', 10., 0., 20., 0.]])]),
            dict(symbol='SR2601', pages=[dict(fields=FIELDS, items=[
            ['20260105', 'SR2601', '中粮期货（代客）', 10., 1., None, None],
            ['20260105', 'SR2601', '中粮期货（自营）', 0., 0., 2., 1.]])])])


def wrapped_capture():
    body = capture()
    for report in body['reports']:
        report['windows'] = [dict(start_date=body['start_date'], end_date=body['end_date'], pages=report.pop('pages'))]
    return body


def parse(body):
    return stock.parse_cofco_history(delivery.canonical(body), stock.URL, STAMP)


def test_history_replays_exact_contracts_preserves_accounts_zeros_and_absence():
    body = wrapped_capture()
    original = deepcopy(body)
    rows = parse(body)
    assert body == original and all('rank' not in r for r in rows)
    assert {r['account'] for r in rows} == {'未区分', '未披露', '代客', '自营'}
    missing = [r for r in rows if r['report_date'] == '2022-11-04']
    assert len(missing) == 2 and all(r['positions'] is None and r['raw_member'] is None for r in missing)
    assert next(r['positions'] for r in rows if r['account'] == '自营' and r['side'] == 'long') == 0
    metrics = cofco_history_metrics(rows)
    assert [r['net'] for r in metrics] == [70, None, 8]
    assert metrics[-1]['long'] == 10 and metrics[-1]['short'] == 2
    assert '当日返回1个合约' in metrics[-1]['coverage']
    # A member slice must never become an exchange top-20 or fixed-five ranking.
    assert domestic_metrics(rows, load_members(ROOT / '02_configs/sugar_positions.json')) == []
    assert member_metrics(rows, COFCO_MEMBER) == []
    source = {'url': stock.URL, 'retrieved_at': STAMP}
    kind, replayed = delivery._replay_source('stockapi_CZCE_SR_cofco_20220101_20261009', source,
        delivery.canonical(body), 'sugar', config(ROOT))
    assert kind == 'domestic' and replayed == rows
    with pytest.raises(ValueError, match='source invalid'):
        delivery._replay_source('stockapi_CZCE_SR_cofco_20220101_20261009', source,
            delivery.canonical(body), 'soybean', config(ROOT))
    with pytest.raises(ValueError, match='range differs'):
        delivery._replay_source('stockapi_CZCE_SR_cofco_20220101_20261008', source,
            delivery.canonical(body), 'sugar', config(ROOT))


@pytest.mark.parametrize('mutation', ['missing_contract', 'duplicate_contract', 'date', 'symbol', 'member',
    'fraction', 'bool', 'negative', 'nonfinite', 'account_alias_collision', 'truncated', 'extra', 'url'])
def test_incomplete_or_wrong_identity_history_is_rejected(mutation):
    body = wrapped_capture()
    page = body['reports'][0]['windows'][0]['pages'][0]
    items = page['items']
    url = stock.URL
    if mutation == 'missing_contract': body['reports'].pop()
    if mutation == 'duplicate_contract': body['reports'][1] = deepcopy(body['reports'][0])
    if mutation == 'date': items[0][0] = '20211231'
    if mutation == 'symbol': items[0][1] = 'SR'
    if mutation == 'member': items.append(deepcopy(items[0]))
    if mutation == 'fraction': items[0][3] = 1.1
    if mutation == 'bool': items[0][3] = True
    if mutation == 'negative': items[0][3] = -1
    if mutation == 'nonfinite': items[0][3] = 'Infinity'
    if mutation == 'account_alias_collision': items.append([*items[0][:2], '中粮', *items[0][3:]])
    if mutation == 'truncated': body['reports'][0]['windows'][0]['pages'].append(deepcopy(page))
    if mutation == 'extra': body['token'] = 'not-a-credential'
    if mutation == 'url': url = 'https://example.org/'
    with pytest.raises((ValueError, TypeError)):
        stock.parse_cofco_history(json.dumps(body).encode(), url, STAMP)


def test_contract_set_change_and_missing_observations_do_not_infer_change():
    rows = parse(wrapped_capture())[:2]
    extra = [dict(r, report_date='2022-11-04', positions=200) for r in rows]
    rows += extra + [dict(r, scope='SR2305') for r in extra]
    metrics = cofco_history_metrics(rows)
    assert metrics[-1]['long'] == metrics[-1]['short'] == 400
    assert metrics[-1]['net_change'] is None


def test_known_open_day_without_reports_is_a_blank_not_interpolated_or_zero():
    body = wrapped_capture()
    body['calendar']['items'].append(['CZCE', '20221107', 1])
    metrics = cofco_history_metrics(parse(body))
    gap = next(r for r in metrics if r['report_date'] == '2022-11-07')
    assert gap['long'] is gap['short'] is gap['net'] is None
    assert gap['constituent_contracts'] == [] and '无白糖合约报表' in gap['coverage']


def daily_capture(day='20261009'):
    return dict(schema_version=stock.COFCO_HISTORY_SCHEMA, start_date=day, end_date=day,
        catalogue=dict(fields=['ts_code', 'symbol', 'exchange', 'fut_code', 'list_date', 'delist_date'],
            items=[['SR2701.ZCE', 'SR701', 'CZCE', 'SR', '20260119', '20270114']]),
        calendar=dict(fields=['exchange', 'cal_date', 'is_open'], items=[['CZCE', day, 1]]),
        reports=[dict(symbol='SR2701', windows=[dict(start_date=day, end_date=day,
            pages=[dict(fields=FIELDS, items=[[day, 'SR2701', '中粮期货（代客）', 20, 1, 10, 1]])])])])


def test_daily_gateway_keeps_credentials_out_and_unpublished_days_retryable():
    body, requests = daily_capture(), []
    class Response:
        status_code = 200
        def __init__(self, data): self.data = data
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def iter_content(self, size):
            yield json.dumps(dict(code=0, msg='TEST_SECRET', data=self.data)).encode()
    class Session:
        empty = False
        def post(self, url, **kwargs):
            request = kwargs['json']; requests.append(request)
            assert url == stock.URL and request['token'] == 'TEST_SECRET'
            assert kwargs['allow_redirects'] is False and kwargs['headers']['Accept-Encoding'] == 'gzip'
            if request['api_name'] == 'fut_basic':
                assert request['params']['fut_code'] == 'SR'
                return Response(body['catalogue'])
            if request['api_name'] == 'fut_trade_cal': return Response(body['calendar'])
            assert request['params']['symbol'] == 'SR2701'
            data = deepcopy(body['reports'][0]['windows'][0]['pages'][0])
            if self.empty: data['items'] = []
            return Response(data)
    session = Session()
    source = stock.StockApiSources(token='TEST_SECRET', session=session)
    rows, raw, _ = source.cofco_history(date(2026, 10, 9))
    assert cofco_history_metrics(rows)[0]['net'] == 10
    assert b'TEST_SECRET' not in raw and b'msg' not in raw
    session.empty = True
    with pytest.raises(stock.SourceNotPublished): source.cofco_history(date(2026, 10, 9))
    assert sum(r['api_name'] == 'fut_basic' for r in requests) == 1


@pytest.mark.parametrize('incremental', [False, True])
@pytest.mark.parametrize('unavailable', [False, True])
def test_daily_sugar_uses_czce_only_and_preserves_legacy_history(tmp_path, monkeypatch, incremental, unavailable):
    from io import BytesIO
    import openpyxl
    from agri_research_agent.automation import positions_scheduled_collection as collection
    from agri_research_agent.sugar_positions.sources import OfficialSources, parse_czce
    from agri_research_agent.sugar_positions.model import cofco_official_metrics
    from test_positions_delivery import archive
    from test_server_collection import Source as DceSource
    baseline = archive(day='2026-10-08')
    builder = delivery.IncrementBuilder(baseline, ROOT)
    for day in ('20261007', '20261008'):
        raw = delivery.canonical(daily_capture(day))
        builder.publish(tmp_path/'sugar', [], stock.parse_cofco_history(raw, stock.URL, STAMP),
            [(f'stockapi_CZCE_SR_cofco_{day}_{day}', raw, stock.URL, 'json')], [])
    baseline = delivery.resolve_increment(builder.finish(ROOT)[0], baseline)
    calls = []
    def official(source, spec=None, **kwargs):
        if spec is not None:
            return [], [], [], []
        assert isinstance(source, OfficialSources)
        calls.append(kwargs['existing_dates'])
        key = 'czce_20261009'
        if unavailable:
            return [], [], [], [dict(source_id=key, status='failed', rows=0)]
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.append(['品种：白糖SR 日期：2026-10-09'])
        sheet.append(['名次', '会员简称', '交易量（手）', '增减量', '会员简称',
                      '持买仓量', '增减量', '会员简称', '持卖仓量', '增减量'])
        sheet.append([1, '中粮期货（代客）', 100, 0, '中粮期货（代客）', 100, 0,
                      '中粮期货（代客）', 90, 0])
        sheet.append(['合计', None, 100, None, None, 100, None, None, 90, None])
        buffer = BytesIO()
        book.save(buffer)
        book.close()
        raw = buffer.getvalue()
        url = 'https://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/20261009/FutureDataHolding.xlsx'
        rows = parse_czce(raw, '2026-10-09', url, STAMP)
        return [], rows, [(key, raw, url, 'xlsx')], [dict(source_id=key, status='ok', rows=len(rows))]
    monkeypatch.setattr(collection.delivery, '_module', lambda *a: type('Collector', (), {'collect': staticmethod(official)}))
    class Source(DceSource):
        def cofco_history(self, day):
            pytest.fail('sugar must never request the third-party history provider')
    (tmp_path/'next').mkdir()
    result, attempts = collection.collect_bundle(baseline, tmp_path/'next', markets='domestic',
        today=date(2026, 10, 9), stock_source=Source(), incremental=incremental)
    candidate = (delivery.resolve_increment(result, baseline) if incremental
                 else delivery.archive_bundle(result, ROOT))
    old = delivery.validate_archive(baseline, ROOT)[0]['sugar']
    new = delivery.validate_archive(candidate, ROOT)[0]['sugar']
    assert old['foreign'] == new['foreign'] and all(r in new['domestic'] for r in old['domestic'])
    assert calls == [set()]  # Member history never masks a complete official ranking.
    assert cofco_history_metrics(new['domestic']) == cofco_history_metrics(old['domestic'])
    assert not any(a['source_id'].startswith('stockapi_CZCE_') for a in attempts)
    if unavailable:
        assert old == new
    else:
        latest = cofco_official_metrics(new['domestic'])[-1]
        assert (latest['report_date'], latest['long'], latest['short'], latest['net']) == ('2026-10-09', 100, 90, 10)
        assert latest['source_provider'] == 'CZCE'
    assert delivery.observations(candidate, baseline, ROOT)['revised_partitions'] == []
