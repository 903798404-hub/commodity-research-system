"""Dated archives stay independent of late manual quotes and the page clock."""
from copy import deepcopy
from datetime import date, datetime
import hashlib
import sqlite3

import pytest

from agri_research_agent.commodity_import_margin.live import parse_fx
from agri_research_agent.sugar_import_profit import model, runtime
from agri_research_agent.sugar_import_profit.store import Store, packed


def daily_snapshot():
    stamp = datetime.fromisoformat('2026-10-29T15:15:20+08:00')
    raw = [''] * 28
    raw[0], raw[1], raw[8], raw[17] = '白糖2701', '150000', '5267', '2026-10-29'
    fx = dict(spot=dict(data=dict(showDateCN='2026-10-29 15:10:00'),
        records=[dict(ccyPair='USD/CNY', bidPrc='6.66', askPrc='6.68')]),
        swap=dict(data=dict(showDateCN='2026-10-29 15:10:00'), records=[dict(ccyPair='USD/CNY',
            **{'label_' + tenor: '-10/-8' for tenor in ('1M', '3M', '6M', '9M', '1Y')})]))
    curve, _, _ = parse_fx(fx, stamp)
    ice_raw = dict(dm='SB27H', uid='NYBOT|SB27H', p=19.36,
                   utime=int(datetime.fromisoformat('2026-10-29T01:00:00+08:00').timestamp()))
    return dict(schema_version='sugar-monthly-market/1', date='2026-10-29', captured_at=stamp.isoformat(),
        units=dict(ice='US_cents/lb', domestic='CNY/tonne', fx='CNY_per_USD'),
        ice=dict(SBH27=dict(price=19.36, contract='SBH27', provider='Eastmoney', raw=ice_raw,
            quoted_at='2026-10-29T01:00:00+08:00')),
        domestic=dict(SR2701=dict(price=5267., contract='SR2701', provider='Sina', raw=raw,
            quoted_at='2026-10-29T15:00:00+08:00', time_kind='daily_close')),
        fx_raw=fx, fx_curve=curve, errors={}, archive_policy=runtime.DAILY_POLICY,
        scheduled_at='2026-10-29T15:15:00+08:00',
        calendar=dict(provider='AkShare/Sina', trading_day='2026-10-29', covered_through='2026-12-31'))


def test_archive_without_quotes_then_late_save_preserves_snapshot_and_independent_history(tmp_path):
    from test_sugar_import_profit import monthly_payload
    store = Store(tmp_path / 'archive.sqlite3', lambda _: None)
    assert store.daily_market(date(2026, 10, 29)) is None and not store.path.exists()
    snapshot = daily_snapshot()
    version = store.save_daily_market(snapshot, expected_version=None)
    assert store.monthly_records() == []  # No basis quote is required by the timer.
    assert store.daily_market(date(2026, 10, 28)) is None
    assert store.save_daily_market(snapshot, expected_version=None) == version
    payload = monthly_payload(date(2026, 10, 29))
    payload.update(schema_version=model.RESEARCH_VERSION, parameters=dict(model.RESEARCH_PARAMETERS),
                   market=snapshot, market_archive_version=version)
    for row in payload['quotes']: row.update(quote=None, freight=54.5)
    next(r for r in payload['quotes'] if r['period'] == '2026-11')['quote'] = 0.
    store.save_monthly(payload, expected_version=None)
    saved = store.monthly_records()[0]['payload']
    rows = model.monthly_rows(saved)
    assert rows[0]['margin_out'] is not None and rows[0]['quote'] == 0.
    assert all(r['margin_out'] is None for r in rows[1:])
    # An explicit archive revision does not rewrite calculations already saved.
    revised = deepcopy(snapshot); revised['errors']['source_note'] = 'verified later'
    with pytest.raises(ValueError, match='版本'): store.save_daily_market(revised, expected_version=None)
    newer = store.save_daily_market(revised, expected_version=version)
    assert newer != version and store.monthly_records()[0]['payload'] == saved
    with store.read() as db:
        assert db.execute('SELECT COUNT(*) FROM sugar_daily_market_revisions').fetchone()[0] == 1
    forged = deepcopy(saved); forged['market_archive_version'] = newer
    with pytest.raises(ValueError, match='版本'): model.validate_monthly(forged)
    forged = deepcopy(saved); forged['date'] = '2026-10-28'
    with pytest.raises(ValueError, match='日期'): model.validate_monthly(forged)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE sugar_daily_markets SET payload='{}'")
    with pytest.raises(ValueError, match='校验'): store.daily_market(date(2026, 10, 29))


def test_capture_holidays_window_calendar_and_stale_components_fail_independently():
    calls = []
    def capture(day, quotes):
        calls.append((day, quotes))
        value = daily_snapshot()
        value['domestic']['SR2701'].update(quoted_at='2026-10-28T15:00:00+08:00')
        value['domestic']['SR2701']['raw'][17] = '2026-10-28'
        for body in value['fx_raw'].values(): body['data']['showDateCN'] = '2026-10-28 15:10:00'
        return value
    now = datetime.fromisoformat('2026-10-29T15:15:00+08:00')
    value = runtime.capture_daily_market(now=now, calendar=['2026-10-29'], capture=capture)
    assert len(calls[0][1]) == 12
    assert value['domestic']['SR2701']['price'] is None and value['fx_curve'] == {}
    assert value['ice']['SBH27']['price'] == 19.36 and value['errors']['fx_archive']
    for stamp in ('2026-10-29T15:14:59+08:00', '2026-10-29T21:15:00+08:00'):
        with pytest.raises(ValueError, match='窗口'):
            runtime.capture_daily_market(now=datetime.fromisoformat(stamp), calendar=['2026-10-29'], capture=capture)
    with pytest.raises(ValueError, match='覆盖'):
        runtime.capture_daily_market(now=now, calendar=['2026-10-28'], capture=capture)
    assert runtime.capture_daily_market(now=now, calendar=['2026-10-30'], capture=capture) is None
    assert runtime.capture_daily_market(now=datetime.fromisoformat('2026-10-31T15:15:00+08:00'), capture=capture) is None
    assert len(calls) == 1


@pytest.mark.parametrize('stamp', ['2026-10-29T14:59:59+08:00', '2026-10-29T21:00:00+08:00'])
def test_domestic_open_or_night_price_is_never_labelled_close(stamp):
    value = daily_snapshot()
    row = value['domestic']['SR2701']
    row['quoted_at'] = stamp
    row['raw'][1] = datetime.fromisoformat(stamp).strftime('%H%M%S')
    with pytest.raises(ValueError): runtime.validate_daily_market(value)


def test_cli_check_and_repeat_do_not_collect_or_write(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'empty.sqlite3'
    monkeypatch.setattr(runtime, 'storage_context', lambda: (path, None, lambda _: None, True))
    monkeypatch.setattr(runtime, 'monthly_capture_enabled', lambda: True)
    monkeypatch.setattr(runtime, 'capture_daily_market', lambda **_: pytest.fail('read-only command collected'))
    assert runtime.archive_main(['--check']) == 0 and not path.exists()
    assert 'CHECK_ONLY' in capsys.readouterr().out
    snapshot = daily_snapshot()
    monkeypatch.setattr(Store, 'daily_market', lambda _, day: dict(payload=snapshot, version='f' * 64))
    assert runtime.archive_main([]) == 0 and not path.exists()
    assert 'ALREADY_ARCHIVED' in capsys.readouterr().out


def test_empty_archive_cannot_block_later_valid_capture(tmp_path):
    value = daily_snapshot()
    value.update(ice={}, domestic={}, fx_curve={}, fx_raw={})
    store = Store(tmp_path / 'never.sqlite3', lambda _: None)
    with pytest.raises(ValueError, match='空白归档'): store.save_daily_market(value, expected_version=None)
    assert not store.path.exists()


def test_page_late_quote_binds_only_selected_day_archive_without_network(tmp_path, monkeypatch):
    from test_sugar_import_profit_page import page
    app, db = page(tmp_path, monkeypatch)
    store = Store(db, lambda _: None)
    version = store.save_daily_market(daily_snapshot(), expected_version=None)
    monkeypatch.setattr(runtime, 'capture_monthly_market', lambda *_: pytest.fail('late input fetched live prices'))
    app.date_input(key='sugar-month-day').set_value(date(2026, 10, 29)).run()
    key = 'sugar-month:巴西:2026-10-29'
    assert not app.exception and not app.button(key=key + ':refresh').disabled
    app.session_state[key + ':editor'] = dict(edited_rows={'0': {'升贴水（美分/磅）': 0., '海运费（美元/吨）': 54.5}}, added_rows=[], deleted_rows=[])
    app.button(key=key + ':save').click().run()
    assert not app.exception
    saved = store.monthly_records()[0]['payload']
    assert saved['date'] == '2026-10-29' and saved['market_archive_version'] == version
    assert hashlib.sha256(packed(saved['market']).encode()).hexdigest() == version
    assert model.monthly_rows(saved)[0]['margin_out'] is not None
    app.date_input(key='sugar-month-day').set_value(date(2026, 10, 28)).run()
    assert app.button(key='sugar-month:巴西:2026-10-28:refresh').disabled
    assert store.daily_market(date(2026, 10, 28)) is None


def test_upgrade_restores_optional_reference_dropped_by_retained_page_without_refetch(tmp_path, monkeypatch):
    from test_sugar_import_profit_page import page
    from test_sugar_import_profit import monthly_payload
    app, db = page(tmp_path, monkeypatch)
    store = Store(db, lambda _: None)
    snapshot = daily_snapshot()
    version = store.save_daily_market(snapshot, expected_version=None)
    payload = monthly_payload(date(2026, 10, 29))
    payload.update(schema_version=model.RESEARCH_VERSION, parameters=dict(model.RESEARCH_PARAMETERS), market=snapshot)
    # A retained old UI builds a fresh top-level object, losing the optional new
    # reference while preserving the complete dated market and original archive.
    store.save_monthly(payload, expected_version=None)
    assert 'market_archive_version' not in store.monthly_records()[0]['payload']
    monkeypatch.setattr(runtime, 'capture_monthly_market', lambda *_: pytest.fail('upgrade fetched live data'))
    app.date_input(key='sugar-month-day').set_value(date(2026, 10, 29)).run()
    app.button(key='sugar-month:巴西:2026-10-29:save').click().run()
    assert not app.exception
    saved = store.monthly_records()[0]['payload']
    assert saved['market_archive_version'] == version and saved['market'] == snapshot
    assert store.daily_market(date(2026, 10, 29))['payload'] == snapshot


def test_timer_has_fixed_shanghai_time_and_no_late_catchup():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / '09_deploy/sugar_market'
    timer = (root / 'market-data-sugar-market.timer.template').read_text()
    service = (root / 'market-data-sugar-market.service.template').read_text()
    assert 'OnCalendar=*-*-* 15:15:00 Asia/Shanghai' in timer and 'Persistent=false' in timer
    assert 'RandomizedDelaySec=0' in timer and 'archive_main' in service
    assert 'RequiresMountsFor=/var/lib/market-data' in service
