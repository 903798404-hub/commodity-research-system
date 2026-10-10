from datetime import date, datetime, timedelta
import copy
import json
import sqlite3
from threading import Event, Thread

import pytest

from agri_research_agent.soybean_margin import latest_quotes as latest, reference, store
from agri_research_agent.soybean_margin.api_sources import SHANGHAI, SourceError
from agri_research_agent.soybean_margin.model import contracts

DAY = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 9, 30, tzinfo=SHANGHAI)
VALUES = {m: 0. for m in range(1, 13)}


class Response:
    status_code = 200
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def raise_for_status(self):
        pass
    def iter_content(self, size):
        yield self.body


def quotes(day):
    return dict(business_date=day.isoformat(),
        cbot={contracts(day, m)[0]: 1300. for m in range(1, 13)},
        domestic={p+contracts(day, m)[1]: 3400. if p == 'M' else 9000.
                  for p in ('M', 'Y') for m in range(1, 13)},
        fx_curve={'0': 7., '3': 6.99, '6': 6.98, '9': 6.97, '12': 6.96},
        sources={}, errors={})


def test_submission_keeps_zero_null_version_and_independent_history(tmp_path):
    path = tmp_path / 'cnf.sqlite3'
    values = dict(VALUES, **{})
    values[3] = None
    result = reference.submit(path, DAY, 'brazil', values, 0,
        collector=quotes, authorize=lambda _: None, now=NOW)
    assert result['rows'][0]['net_margin'] is not None
    assert result['rows'][2]['net_margin'] is None
    assert store.load(path, DAY, 'brazil') == (values, 1)
    assert reference.read_latest(path, DAY, 'brazil', 1) == result
    assert reference.read_latest(path, DAY, 'brazil', 0) is None
    assert list(tmp_path.iterdir()) == [path]


def test_failed_capture_does_not_undo_cnf_or_expose_provider_error(tmp_path):
    def fail(day):
        raise RuntimeError('secret credential and upstream URL')
    result = reference.submit(tmp_path/'cnf.sqlite3', DAY, 'brazil', VALUES, 0,
        collector=fail, authorize=lambda _: None, now=NOW)
    assert result['error'] == 'collection_or_schema_failed'
    assert 'secret' not in json.dumps(result)
    assert store.load(tmp_path/'cnf.sqlite3', DAY, 'brazil')[1] == 1


def test_historical_submission_never_calls_current_price_collector(tmp_path):
    result = reference.submit(tmp_path/'cnf.sqlite3', date(2026, 10, 8), 'brazil', VALUES, 0,
        collector=lambda _: pytest.fail('historical fetch'), authorize=lambda _: None, now=NOW)
    assert result['error'] == 'historical_cnf_saved_without_current_quotes'


def test_failed_cas_cannot_fetch_or_create_a_submission(tmp_path):
    path = tmp_path/'cnf.sqlite3'
    store.save(path, DAY, 'brazil', VALUES, 0)
    with pytest.raises(ValueError, match='另一会话'):
        reference.submit(path, DAY, 'brazil', VALUES, 0,
            collector=lambda _: pytest.fail('stale version fetch'), authorize=lambda _: None, now=NOW)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM quote_submissions').fetchone()[0] == 0


def test_old_request_finishing_late_cannot_replace_new_cnf_reference(tmp_path):
    path = tmp_path/'cnf.sqlite3'
    started, finish = Event(), Event()
    results = []
    def delayed(day):
        started.set()
        assert finish.wait(5)
        return quotes(day)
    def first():
        results.append(reference.submit(path, DAY, 'brazil', VALUES, 0,
            collector=delayed, authorize=lambda _: None, now=NOW))
    thread = Thread(target=first)
    thread.start()
    assert started.wait(5)
    second = reference.submit(path, DAY, 'brazil', {m: 100. for m in VALUES}, 1,
        collector=quotes, authorize=lambda _: None, now=NOW)
    finish.set()
    thread.join(5)
    assert not thread.is_alive() and len(results) == 1
    assert reference.read_latest(path, DAY, 'brazil', 2) == second
    assert reference.read_latest(path, DAY, 'brazil', 1) == results[0]


def test_reference_tampering_and_unauthorized_write_rejected(tmp_path):
    path = tmp_path/'cnf.sqlite3'
    def denied(_):
        raise ValueError('denied')
    with pytest.raises(ValueError, match='denied'):
        reference.submit(path, DAY, 'brazil', VALUES, 0, authorize=denied, now=NOW)
    assert not path.exists()
    reference.submit(path, DAY, 'brazil', VALUES, 0, collector=quotes, authorize=lambda _: None, now=NOW)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE quote_submissions SET result_json='{}'")
    with pytest.raises(ValueError, match='身份'):
        reference.read_latest(path, DAY, 'brazil', 1)


def test_eastmoney_pagination_exact_months_and_unknown_time():
    calls = []
    pages = [dict(total=3, list=[dict(dm='ZS26X', p=1300, utime=123), dict(dm='ZS27F', p='-')]),
             dict(total=3, list=[dict(dm='ZS27H', p=1310)])]
    class Session:
        def get(self, url, **kw):
            calls.append(kw['params'].copy())
            return Response(json.dumps(pages[len(calls)-1]).encode())
    prices, evidence = latest.eastmoney(['2611', '2701', '2703', '2705'], session=Session())
    assert prices == {'2611': 1300., '2701': None, '2703': 1310., '2705': None}
    assert calls[1]['pageSize'] == 2 and calls[1]['pageIndex'] == 1
    assert evidence['quotes']['2611']['status'] == 'timestamp_unknown'
    assert evidence['quotes']['2611']['quoted_at'] is None


def test_eastmoney_repeated_page_is_rejected():
    class Session:
        def get(self, url, **kw):
            return Response(json.dumps(dict(total=2, list=[dict(dm='ZS26X', p=1300)])).encode())
    with pytest.raises(SourceError, match='incomplete_or_duplicate'):
        latest.eastmoney(['2611'], session=Session())


@pytest.mark.parametrize('quote_date,time,status,price', [
    ('2026-10-09', '092000', 'available', 3400.),
    ('2026-10-08', '092000', 'stale_or_future', None),
    ('2026-10-09', '093100', 'stale_or_future', None),
    ('2026-10-09', '080000', 'stale_or_future', None)])
def test_sina_accepts_delay_but_rejects_old_date_and_future(quote_date,time,status,price):
    fields = ['']*44
    fields[0] = '豆粕2701'
    fields[1], fields[8], fields[17] = time, '3400', quote_date
    raw = ('var hq_str_nf_M2701="'+','.join(fields)+'";').encode('gb18030')
    class Session:
        def get(self, url, **kw):
            assert '?list=nf_M2701' in url
            return Response(raw)
    values, source = latest.domestic(['M2701', 'M2801'], session=Session(), now=NOW)
    assert values == {'M2701': price, 'M2801': None}
    assert source['quotes']['M2701']['status'] == status


def test_capture_0930_independent_sources_and_calendar(monkeypatch):
    monkeypatch.setattr(latest, 'stamp', lambda: NOW)
    def fail():
        raise requests_error()
    class requests_error(Exception):
        pass
    value = latest.capture(DAY, now=NOW, calendar=lambda: [DAY.isoformat()], providers={
        'cbot': lambda: ({'2611': 1300.}, {'provider': 'Eastmoney'}),
        'domestic': fail, 'fx': lambda: ({'0': 7.}, {'provider': 'CFETS'})})
    assert value['cbot']['2611'] == 1300. and value['fx_curve']['0'] == 7.
    assert value['errors'] == {'domestic': 'transport_or_schema_error'}
    assert all('requested_at' in s and 'returned_at' in s for s in value['sources'].values())
    with pytest.raises(SourceError, match='holiday'):
        latest.capture(DAY, now=NOW, calendar=lambda: ['2026-10-12'])


def test_sina_quote_advancing_during_request_uses_response_time(monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(latest, 'stamp', lambda: clock[0])
    fields = [''] * 44
    fields[0] = '豆粕2701'
    fields[1], fields[8], fields[17] = '093005', '3400', DAY.isoformat()
    raw = ('var hq_str_nf_M2701="' + ','.join(fields) + '";').encode('gb18030')
    class Session:
        def get(self, url, **kw):
            clock[0] += timedelta(seconds=5)
            return Response(raw)
    values, source = latest.domestic(['M2701'], session=Session())
    assert values['M2701'] == 3400.
    assert source['quotes']['M2701']['status'] == 'available'


def test_legacy_save_remains_opt_out_and_reference_read_never_fetches(tmp_path):
    path = tmp_path/'cnf.sqlite3'
    store.save(path, DAY, 'brazil', VALUES, 0)
    assert reference.read_latest(path, DAY, 'brazil', 1) is None
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='quote_submissions'").fetchone()


def test_oversize_result_records_failure_while_preserving_cnf(tmp_path):
    def large(day):
        value = quotes(day)
        value['sources'] = {'raw': 'x'*(reference.MAX_BYTES+1)}
        return value
    path = tmp_path/'cnf.sqlite3'
    result = reference.submit(path,DAY,'brazil',VALUES,0,collector=large,authorize=lambda _: None,now=NOW)
    assert result['error'] == 'reference_result_invalid_or_too_large'
    assert store.load(path,DAY,'brazil')[1] == 1
    assert reference.read_latest(path,DAY,'brazil',1) == result


def test_partial_source_does_not_fill_with_previous_quotes(tmp_path):
    def partial(day):
        value = quotes(day)
        value['domestic']['M2801'] = None
        return value
    result = reference.submit(tmp_path/'cnf.sqlite3', DAY,'brazil',VALUES,0,
        collector=partial,authorize=lambda _: None,now=NOW)
    assert result['rows'][8]['soymeal_price_cny_per_tonne'] is None
    assert result['rows'][8]['net_margin'] is None
    assert result['status'] == 'partial'
