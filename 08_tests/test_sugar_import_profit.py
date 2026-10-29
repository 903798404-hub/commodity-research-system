from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import pytest
from agri_research_agent.sugar_import_profit import history, model, runtime
from agri_research_agent.sugar_import_profit.store import Store, DDL


def sources():
    return {"巴西": dict(filename="巴西原表.xlsx", rows=[["2025-10-01", 0, 0, 29, 3348, None, 4252, -100]]),
            "泰国": dict(filename="泰国原表.xlsx", rows=[["2025-10-01", 3798, 0, 4838, -200, 10, -1]]),
            "日照": dict(filename="日照原表.xlsx", rows=[["2025-10-01", 5000]])}


def quote():
    value = history.defaults(sources(), "巴西", "2025-10-01")
    value.update(ice=17., insurance_fee=0., fx=7., contract="SBH26", ice_date="2025-10-01", fx_date="2025-10-01")
    return value


def bundle(root):
    root.mkdir(parents=True)
    raw = json.dumps(sources(), ensure_ascii=False).encode("utf-8")
    (root / "steel_sources.json").write_bytes(raw)
    (root / "manifest.json").write_text(json.dumps(dict(schema_version=history.SCHEMA, source_bytes=len(raw),
        source_sha256=hashlib.sha256(raw).hexdigest(), currency="CNY", profit_unit="yuan/tonne",
        row_counts={key: len(value["rows"]) for key, value in sources().items()})), encoding="utf-8")


def test_formula_matches_accepted_model_zero_and_two_loss_definitions():
    value = model.validate_inputs(quote())
    result = model.calculate(value)
    fob = 17 * 1.042 * 22.046
    taxed = (fob + 29) * 7 * 1.15 * 1.13
    assert result["cost_in"] == pytest.approx(taxed * 1.04 + 400)
    assert result["profit_out"] == pytest.approx(5000 - ((fob + 29) * 7 * 1.5 * 1.13 * 1.04 + 400))
    value["loss_mode"] = "实际损耗"
    assert model.calculate(value)["cost_in"] == pytest.approx(taxed / .96 + 400)


@pytest.mark.parametrize("change", [{"fx": None}, {"ice": True}, {"fx": float("nan")},
                                    {"ice_date": "2025-10-02"}, {"contract": ""}, {"example": True}])
def test_incomplete_future_or_example_cannot_save(tmp_path, change):
    value = dict(quote(), **change)
    path = tmp_path / "never.sqlite3"
    with pytest.raises(ValueError):
        Store(path, lambda _: None).save("巴西", value, expected_version=None)
    assert not path.exists()


def test_history_hash_null_zero_and_no_future_default(tmp_path):
    root = tmp_path / "history"
    bundle(root)
    src, manifest = history.load_sources(root)
    rows = history.history_rows(src)
    assert rows[0]["profit_in"] is None
    assert rows[1]["profit_in"] == 0
    old = history.defaults(src, "巴西", "2020-01-01")
    assert old["premium"] is old["domestic"] is None
    (root / "steel_sources.json").write_bytes((root / "steel_sources.json").read_bytes() + b" ")
    with pytest.raises(ValueError, match="校验"):
        history.load_sources(root)


def test_duplicates_invalid_numbers_and_date_range():
    src = sources()
    src["巴西"]["rows"] *= 2
    with pytest.raises(ValueError, match="重复"):
        history.validate_sources(src)
    with pytest.raises(ValueError):
        history.history_rows(sources(), "2026-01-01", "2025-01-01")


def test_transactional_idempotence_revision_and_concurrent_sessions(tmp_path):
    calls = []
    path = tmp_path / "own.sqlite3"
    store = Store(path, lambda p: calls.append(p))
    assert not path.exists() and store.records() == []
    q = quote()
    saved = store.save("巴西", q, expected_version=None)
    assert saved["status"] == "saved"
    original = store.records()[0]
    assert saved["version"] == original["version"]
    assert store.save("巴西", q, expected_version=None)["status"] == "unchanged"
    q["fx"] = 7.1
    assert store.save("巴西", q, expected_version=original["version"])["status"] == "conflict"
    assert store.save("巴西", q, expected_version=original["version"], revise=True)["status"] == "revised"
    latest = store.records()[0]
    assert latest["version"] != original["version"]
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from records").fetchone()[0] == 1
        row = db.execute("select inputs,results from revisions").fetchone()
        assert json.loads(row[0]) == original["inputs"]
        assert json.loads(row[1]) == original["results"]
    with pytest.raises(ValueError, match="另一会话"):
        store.save("巴西", dict(q, fx=7.2), expected_version=original["version"], revise=True)
    assert store.records()[0] == latest
    assert calls


def test_legacy_sqlite_read_and_no_authority_no_write(tmp_path):
    path = tmp_path / "old.sqlite3"
    q = model.validate_inputs(quote())
    with sqlite3.connect(path) as db:
        db.executescript(DDL)
        db.execute("insert into records(date,country,inputs,results,model_version,saved_at) values(?,?,?,?,?,?)",
                   (q["date"], "巴西", json.dumps(q), json.dumps(model.calculate(q)), model.MODEL_VERSION, "legacy-time"))
    raw = path.read_bytes()
    def deny(_):
        raise ValueError("没有权限")
    store = Store(path, deny)
    original = store.records()[0]
    assert original["saved_at"] == "legacy-time"
    with pytest.raises(ValueError, match="权限"):
        store.save("巴西", dict(q, fx=7.2), expected_version=original["version"], revise=True)
    assert path.read_bytes() == raw


def test_note_cas_and_independent_business_keys(tmp_path):
    store = Store(tmp_path / "own.sqlite3", lambda _: None)
    store.save("巴西", quote(), expected_version=None)
    store.save("泰国", quote(), expected_version=None)
    store.save_note(dict(text="研究", date="2025-10-01"), None)
    note = store.setting("research-note")
    with pytest.raises(ValueError, match="笔记已变化"):
        store.save_note(dict(text="旧会话", date="2025-10-01"), None)
    assert store.setting("research-note") == note
    assert len(store.records()) == 2


def test_runtime_fail_closed_and_dedicated_local_path(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "SOURCE", tmp_path)
    monkeypatch.setattr(runtime, "deployed", lambda: False)
    monkeypatch.delenv("SUGAR_IMPORT_ALLOW_SAVE", raising=False)
    monkeypatch.setenv("SUGAR_IMPORT_LOCAL_ROOT", str(tmp_path / "01_data/candidates/sugar_import_profit/test"))
    monkeypatch.setenv("SUGAR_IMPORT_LOCAL_PREVIEW", "1")
    path, _, auth, enabled = runtime.storage_context()
    assert enabled
    auth(path)
    with pytest.raises(ValueError):
        auth(tmp_path / "some-other.sqlite3")
    monkeypatch.setenv("SUGAR_IMPORT_ALLOW_SAVE", "1")
    with pytest.raises(ValueError, match="正式身份"):
        runtime.storage_context()


def test_prepare_preserves_complete_legacy_database_and_source(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "04_scripts/import_profit/prepare_sugar_import_history.py"
    spec = importlib.util.spec_from_file_location("sugar_prepare", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    source = tmp_path / "source.json"
    source.write_text(json.dumps(sources()), encoding="utf-8")
    db = tmp_path / "legacy.sqlite3"
    store = Store(db, lambda _: None)
    store.save("巴西", quote(), expected_version=None)
    old = store.records()[0]
    store.save("巴西", dict(quote(), fx=7.2), expected_version=old["version"], revise=True)
    store.save_note(dict(text="保留全部笔记", date="2025-10-01"), None)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, db)}
    dest = tmp_path / "01_data/candidates/sugar_import_profit/new"
    result = module.prepare(source, before[source], db, before[db], dest)
    assert result["sqlite_counts"] == dict(records=1, revisions=1, settings=2)
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items())
    with sqlite3.connect(db) as a, sqlite3.connect(dest / "sugar-import.sqlite3") as b:
        for table in ("settings", "records", "revisions"):
            assert a.execute(f"select * from {table}").fetchall() == b.execute(f"select * from {table}").fetchall()
    history.load_sources(dest / "history")
    with pytest.raises(ValueError, match="独立新建"):
        module.prepare(source, before[source], db, before[db], dest)


def monthly_payload(day=date(2026, 10, 15)):
    return dict(schema_version=model.MONTHLY_VERSION, date=day.isoformat(), country='巴西',
        quote_mode=model.QUOTE_MODES[0], source='测试夹具，仅验证计算', parameters=dict(model.MONTHLY_PARAMETERS), market=None,
        quotes=[dict(period=p, quote=0., ice_contract=model.default_mapping(p)[0], sr_contract=model.default_mapping(p)[1],
                     freight=30., insurance=None, rizhao=None, rizhao_date='') for p in model.month_periods(day)])


def research_payload():
    value = monthly_payload()
    value.update(schema_version=model.RESEARCH_VERSION, parameters=dict(model.RESEARCH_PARAMETERS))
    return value


def test_reference_screenshot_formula_and_fee_order():
    payload = research_payload()
    item = payload['quotes'][10]
    item.update(quote=-.9, freight=54.)
    payload['market'] = dict(fx_curve={'0': 6.6807}, ice={item['ice_contract']: {'price': 20.59}},
                             domestic={item['sr_contract']: {'price': 6500.}})
    row = model.monthly_rows(payload)[0]
    assert row['fob_usd'] == pytest.approx(434.08574)
    assert row['insurance'] == pytest.approx(434.08574 * .004616)
    expected = ((434.08574 * 1.004616 + 54) * 6.6807 * 1.5 * 1.13
                * 1.02 * 1.00125 * 1.01 * 1.04 + 25 + 500)
    assert row['cost_out'] == pytest.approx(expected)
    assert round(expected, 2) == 6478.39
    assert row['margin_out'] == pytest.approx(6500 - expected)
    item['freight'] = None
    assert model.monthly_rows(payload)[0]['cost_out'] is None
    item.update(freight=0., quote=0.)
    assert model.monthly_rows(payload)[0]['margin_out'] is not None


def test_mapping_ice_march_sr_159_all_twelve_and_rollover():
    expected_sr = (5, 5, 5, 9, 9, 9, 9, 1, 1, 1, 1, 5)
    for month, sr_month in enumerate(expected_sr, 1):
        ice, sr = model.default_mapping(f'2027-{month:02}')
        assert int(sr[-2:]) == sr_month
        assert int(sr[2:4]) == (28 if month >= 8 else 27)
    assert model.default_mapping('2027-01') == ('SBH27', 'SR2705')
    assert model.default_mapping('2027-12') == ('SBH28', 'SR2805')


def test_research_version_validation_and_legacy_record_preservation(tmp_path):
    payload = research_payload()
    model.validate_monthly(payload)
    store = Store(tmp_path / 'own.sqlite3', lambda _: None)
    old = monthly_payload()
    version = store.save_monthly(old, expected_version=None)
    store.save_monthly(payload, expected_version=None)
    assert store.monthly_records()[0]['payload']['parameters'] == model.RESEARCH_PARAMETERS
    with sqlite3.connect(store.path) as db:
        raw = db.execute('SELECT payload FROM monthly_records').fetchone()[0]
    assert json.loads(raw) == old
    payload['quotes'][0]['sr_contract'] = 'SR2703'
    with pytest.raises(ValueError, match='合约'):
        model.validate_monthly(payload)


def test_monthly_fixed_skeleton_rollover_and_partial_insurance_and_spot():
    payload = monthly_payload()
    validated = model.validate_monthly(payload)
    assert len(model.monthly_rows(validated)) == 12
    assert '2027-10' in model.month_periods(date(2026, 10, 15))
    assert '2026-11' in model.month_periods(date(2026, 10, 15))
    item = payload['quotes'][10]  # November 2026
    # Calculation-only fixture, intentionally distinct from accepted captured evidence.
    payload['market'] = dict(fx_curve={'0': 7.}, ice={item['ice_contract']: {'price': 20.}}, domestic={item['sr_contract']: {'price': 6000.}})
    row = model.monthly_rows(payload)[0]
    expected = 6000 - (20 * 1.042 * 22.046 + 30) * 7 * 1.15 * 1.13 * 1.04 - 400
    assert row['margin_in'] is row['spot_in'] is None
    assert row['excluding_insurance_in'] == pytest.approx(expected)
    item['insurance'] = 0.  # Explicit observed/user-entered zero differs from missing.
    row = model.monthly_rows(payload)[0]
    assert row['margin_in'] == pytest.approx(expected) and row['spot_in'] is None
    item['quote'] = None
    assert model.monthly_rows(payload)[0]['margin_in'] is None


def test_monthly_cnf_modes_do_not_double_add_freight_or_ice():
    payload = monthly_payload()
    item = payload['quotes'][10]
    payload['market'] = dict(fx_curve={'0': 7.}, ice={item['ice_contract']: {'price': 20.}}, domestic={item['sr_contract']: {'price': 6000.}})
    item.update(quote=-.2, insurance=2.)
    payload['quote_mode'] = model.QUOTE_MODES[1]
    row = model.monthly_rows(payload)[0]
    assert row['cfr_usd'] == pytest.approx(19.8 * 1.042 * 22.046)
    payload['quote_mode'] = model.QUOTE_MODES[2]
    item['quote'] = 450.
    payload['market']['ice'] = {}
    row = model.monthly_rows(payload)[0]
    assert row['cfr_usd'] == 450. and row['margin_in'] is not None
    payload['parameters']['loss_mode'] = '实际损耗'
    assert model.monthly_rows(payload)[0]['cost_in'] == pytest.approx(452 * 7 * 1.15 * 1.13 / .96 + 400)


def test_monthly_store_atomic_cas_revision_hash_and_legacy_preservation(tmp_path):
    store = Store(tmp_path / 'own.sqlite3', lambda _: None)
    store.save('巴西', quote(), expected_version=None)
    legacy = store.records()
    assert store.monthly_records() == []
    payload = monthly_payload()
    first = store.save_monthly(payload, expected_version=None)
    assert store.save_monthly(payload, expected_version=first) == first
    payload['quotes'][0]['quote'] = -1.
    second = store.save_monthly(payload, expected_version=first)
    with pytest.raises(ValueError, match='另一会话'):
        store.save_monthly(payload, expected_version=first)
    assert store.records() == legacy
    assert store.monthly_records()[0]['version'] == second
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM monthly_revisions').fetchone()[0] == 1
        db.execute("UPDATE monthly_records SET payload=payload || ' '")
    with pytest.raises(ValueError, match='校验'):
        store.monthly_records()


@pytest.mark.parametrize('change', ['incomplete', 'bad-contract', 'bad-price', 'spot-date', 'future', 'bad-market'])
def test_monthly_rejects_invalid_inputs_before_database_write(tmp_path, change):
    payload = monthly_payload()
    if change == 'incomplete': payload['quotes'].pop()
    if change == 'bad-contract': payload['quotes'][0]['ice_contract'] = 'SBH26'
    if change == 'bad-price': payload['quotes'][0]['insurance'] = float('nan')
    if change == 'spot-date': payload['quotes'][0]['rizhao'] = 6000.
    if change == 'future': payload['date'] = '2099-10-15'
    if change == 'bad-market': payload['market'] = {'date': '2026-10-14'}
    store = Store(tmp_path / 'never.sqlite3', lambda _: None)
    with pytest.raises((ValueError, KeyError)):
        store.save_monthly(payload, expected_version=None)
    assert not store.path.exists()


def test_monthly_capture_source_failures_and_no_historical_requests(monkeypatch):
    from agri_research_agent.commodity_import_margin import live
    calls = []
    def unavailable(url, **kwargs):
        calls.append(url)
        raise ValueError('unavailable')
    monkeypatch.setattr(live, 'get_text', unavailable)
    monkeypatch.setattr(runtime, 'deployed', lambda: False)
    day = date.fromisoformat(model.today())
    payload = monthly_payload(day)
    snapshot = runtime.capture_monthly_market(day, payload['quotes'])
    assert len(calls) == 5 and snapshot['fx_curve'] == {}
    assert all(v['price'] is None for k in ('ice', 'domestic') for v in snapshot[k].values())
    runtime.validate_monthly_market(snapshot, day)
    with pytest.raises(ValueError, match='历史'):
        runtime.capture_monthly_market(date(2025, 10, 15), payload['quotes'])
    assert len(calls) == 5


def test_monthly_capture_exact_available_quotes_and_detect_tampered_evidence(monkeypatch):
    from datetime import datetime
    from copy import deepcopy
    from agri_research_agent.commodity_import_margin import live
    now = datetime.now(model.TZ)
    day = now.date()
    ice_code, sr_code = model.default_mapping(model.month_periods(day)[0])
    fields = [''] * 28
    fields[0], fields[1], fields[8], fields[17] = '白糖' + sr_code[-4:], now.strftime('%H%M%S'), '6000', day.isoformat()
    em_symbol = 'SB' + ice_code[-2:] + ice_code[2]
    record = dict(dm=em_symbol, uid='NYBOT|' + em_symbol, p=20., utime=int(now.timestamp()))
    def source(url, **kwargs):
        if 'NYBOT' in url: return json.dumps(dict(list=[record], total=1))
        if 'sinajs' in url: return f'var hq_str_nf_{sr_code}="' + ','.join(fields) + '";'
        raise ValueError('CFETS not supplied by this test fixture')
    monkeypatch.setattr(live, 'get_text', source)
    monkeypatch.setattr(runtime, 'deployed', lambda: False)
    monkeypatch.setattr(runtime, 'daily_close_text', lambda url: pytest.fail('有效最新价不应回退收盘价'))
    snapshot = runtime.capture_monthly_market(day, [dict(ice_contract=ice_code, sr_contract=sr_code)])
    assert snapshot['ice'][ice_code]['price'] == 20.
    assert snapshot['domestic'][sr_code]['price'] == 6000.
    payload = monthly_payload(day)
    payload['market'] = snapshot
    model.validate_monthly(payload)
    forged = deepcopy(snapshot)
    forged['ice'][ice_code]['price'] = 21.
    with pytest.raises(ValueError, match='凭证'):
        runtime.validate_monthly_market(forged, day)
    forged = deepcopy(snapshot)
    forged['date'] = '2025-01-01'
    with pytest.raises(ValueError, match='日期'):
        runtime.validate_monthly_market(forged, day)


def test_prepare_preserves_monthly_tables_and_legacy_without_source_mutation(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / '04_scripts/import_profit/prepare_sugar_import_history.py'
    spec = importlib.util.spec_from_file_location('sugar_monthly_prepare', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    src = tmp_path / 'sources.json'
    src.write_text(json.dumps(sources()), encoding='utf-8')
    db = tmp_path / 'own.sqlite3'
    store = Store(db, lambda _: None)
    payload = monthly_payload()
    version = store.save_monthly(payload, expected_version=None)
    payload['quotes'][0]['quote'] = -.2
    store.save_monthly(payload, expected_version=version)
    research = research_payload()
    research_version = store.save_monthly(research, expected_version=None)
    research['parameters']['processing'] = 510.
    store.save_monthly(research, expected_version=research_version)
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    dest = tmp_path / '01_data/candidates/sugar_import_profit/monthly'
    report = module.prepare(src, hashlib.sha256(src.read_bytes()).hexdigest(), db, digest, dest)
    assert report['sqlite_counts']['monthly_records'] == report['sqlite_counts']['monthly_revisions'] == 1
    assert report['sqlite_counts']['monthly_records_v2'] == report['sqlite_counts']['monthly_revisions_v2'] == 1
    assert Store(dest / 'sugar-import.sqlite3', lambda _: None).monthly_records() == store.monthly_records()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == digest


@pytest.fixture(autouse=True)
def no_real_yahoo_requests(monkeypatch):
    def unavailable(url):
        raise ValueError('test source unavailable')
    monkeypatch.setattr(runtime, 'yahoo_text', unavailable)
    monkeypatch.setattr(runtime, 'daily_close_text', unavailable)


def test_ice_fallback_preserves_contract_unit_time_and_detects_tampering(monkeypatch):
    from datetime import datetime
    from copy import deepcopy
    from agri_research_agent.commodity_import_margin import live
    now = datetime.now(model.TZ)
    day = now.date()
    code, sr = model.default_mapping(model.month_periods(day)[0])
    def unavailable(url, **kwargs):
        raise ValueError('primary unavailable')
    monkeypatch.setattr(live, 'get_text', unavailable)
    monkeypatch.setattr(runtime, 'deployed', lambda: False)
    meta = dict(symbol=code+'.NYB', currency='USX', exchangeName='NYB', instrumentType='FUTURE',
                regularMarketPrice=20.26, regularMarketTime=int(now.timestamp()))
    monkeypatch.setattr(runtime, 'yahoo_text', lambda url: json.dumps(dict(chart=dict(result=[dict(meta=meta)]))))
    snapshot = runtime.capture_monthly_market(day, [dict(ice_contract=code, sr_contract=sr)])
    assert snapshot['ice'][code]['price'] == 20.26
    assert snapshot['ice'][code]['provider'] == 'Yahoo Finance'
    assert snapshot['ice'][code]['quoted_at'] == datetime.fromtimestamp(meta['regularMarketTime'], model.TZ).isoformat()
    runtime.validate_monthly_market(snapshot, day)
    forged = deepcopy(snapshot)
    forged['ice'][code]['raw']['currency'] = 'USD'
    with pytest.raises(ValueError, match='单位'):
        runtime.validate_monthly_market(forged, day)
    forged = deepcopy(snapshot)
    forged['ice'][code]['price'] = 30.
    with pytest.raises(ValueError, match='凭证'):
        runtime.validate_monthly_market(forged, day)
    forged = deepcopy(snapshot)
    forged['ice'][code]['raw']['symbol'] = 'SB=F'
    with pytest.raises(ValueError, match='合约'):
        runtime.validate_monthly_market(forged, day)


def daily_body(code='SB27H', day='2026-10-27', closing='19.20', volume='81465'):
    return dict(rc=0, data=dict(code=code, market=108, name='糖11号2703', decimal=2,
        klines=[f'{day},19.10,{closing},19.42,19.03,{volume},0.00,2.04,0.52,0.10,0.00']))


def test_daily_close_asof_excludes_the_unfinished_new_york_session_and_handles_dst():
    from datetime import datetime
    now = datetime.fromisoformat('2026-10-28T16:25:17+08:00')
    item = runtime.eastmoney_daily_close(daily_body(), 'SBH27', now)
    assert item['price'] == 19.20 and item['quote_date'] == item['quoted_at'] == '2026-10-27'
    assert item['time_kind'] == 'daily_close'
    assert 'secid=108.SB27H' in item['source_url'] and 'end=20261027' in item['source_url']
    with pytest.raises(ValueError, match='尚未收盘'):
        runtime.eastmoney_daily_close(daily_body(day='2026-10-28'), 'SBH27', now)
    for stamp, expected in [('2026-10-29T01:29:00+08:00', '20261027'),
                            ('2026-10-29T01:30:00+08:00', '20261028'),
                            ('2026-11-03T02:29:00+08:00', '20261101'),
                            ('2026-11-03T02:30:00+08:00', '20261102')]:
        assert 'end=' + expected in runtime.daily_close_url('SBH27', datetime.fromisoformat(stamp))


@pytest.mark.parametrize('body', [daily_body(code='SB00Y'), daily_body(code='SB27K'),
    daily_body(day='2026-10-15'), daily_body(volume='0'), daily_body(closing='99'),
    daily_body(day='2026-10-25'), dict(rc=0, data=None)])
def test_daily_close_rejects_wrong_contract_stale_untraded_or_invalid_bars(body):
    from datetime import datetime
    with pytest.raises(ValueError):
        runtime.eastmoney_daily_close(body, 'SBH27', datetime.fromisoformat('2026-10-28T16:25:17+08:00'))


def test_daily_close_cutoff_is_bound_to_request_across_the_close_boundary():
    from datetime import datetime
    requested = datetime.fromisoformat('2026-10-29T01:29:59+08:00')
    captured = datetime.fromisoformat('2026-10-29T01:30:01+08:00')
    value = dict(schema_version='sugar-monthly-market/1', date='2026-10-29', captured_at=captured.isoformat(),
        units=dict(ice='US_cents/lb', domestic='CNY/tonne', fx='CNY_per_USD'),
        ice={'SBH27':dict(price=None)}, domestic={}, fx_raw={}, fx_curve={}, errors={},
        ice_closes={'SBH27':runtime.eastmoney_daily_close(daily_body(), 'SBH27', requested)})
    runtime.validate_monthly_market(value, captured.date())
    value['ice_closes']['SBH27']['asof'] = '2026-10-29T01:30:02+08:00'
    with pytest.raises(ValueError, match='取价时点'): runtime.validate_monthly_market(value, captured.date())


def test_capture_recovers_missing_latest_using_dated_close_and_verifies_saved_evidence(monkeypatch, tmp_path):
    from datetime import datetime, timedelta
    from copy import deepcopy
    from agri_research_agent.commodity_import_margin import live
    now = datetime.now(model.TZ)
    day = now.date()
    code, sr = model.default_mapping(model.month_periods(day)[0])
    # Use a completed weekday session that is valid at any time this test runs.
    closed_day = now.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date() - timedelta(days=1)
    while closed_day.weekday() >= 5: closed_day -= timedelta(days=1)
    body = daily_body(code=f'SB{code[-2:]}{code[2]}', day=closed_day.isoformat())
    primary = dict(dm=f'SB{code[-2:]}{code[2]}', uid=f'NYBOT|SB{code[-2:]}{code[2]}',
                   p='-', qrspj=19.36, zjsj=19.34, utime=int(now.timestamp()))
    monkeypatch.setattr(live, 'get_text', lambda url, **k: json.dumps(dict(total=1, list=[primary])) if 'NYBOT' in url else '{}')
    monkeypatch.setattr(runtime, 'deployed', lambda: False)
    calls = []
    monkeypatch.setattr(runtime, 'daily_close_text', lambda url: calls.append(url) or json.dumps(body))
    snapshot = runtime.capture_monthly_market(day, [dict(ice_contract=code, sr_contract=sr)])
    assert len(calls) == 1 and snapshot['ice_closes'][code]['price'] == 19.2
    assert snapshot['ice'][code]['price'] is None  # retained readers still accept the original snapshot
    assert not {'ice', code}.intersection(snapshot['errors'])
    runtime.validate_monthly_market(snapshot, day)
    forged = deepcopy(snapshot)
    forged['ice_closes'][code]['price'] = 19.3
    with pytest.raises(ValueError, match='凭证'): runtime.validate_monthly_market(forged, day)
    forged = deepcopy(snapshot)
    forged['ice_closes'][code]['quote_date'] = day.isoformat()
    with pytest.raises(ValueError, match='凭证'): runtime.validate_monthly_market(forged, day)
    payload = monthly_payload(day)
    payload.update(schema_version=model.RESEARCH_VERSION, parameters=dict(model.RESEARCH_PARAMETERS), market=snapshot)
    row = next(r for r in model.monthly_rows(payload) if r['ice_contract'] == code)
    assert row['ice_quote_date'] == closed_day.isoformat() and row['ice_price_kind'] == '收盘价'
    # Missing FX/domestic prices remain missing even after ICE recovers.
    assert row['cost_out'] is None and row['margin_out'] is None
    store = Store(tmp_path / 'daily-close.sqlite3', lambda _: None)
    version = store.save_monthly(payload, expected_version=None)
    assert store.monthly_records()[0]['payload']['market'] == snapshot
    assert store.save_monthly(payload, expected_version=version) == version
    assert model.ice_quote(dict(ice={code:dict(price=20.)}, ice_closes=snapshot['ice_closes']), code)['price'] == 20.


def test_public_freight_integrity_exact_dates_and_missing_zero(tmp_path):
    from copy import deepcopy
    from agri_research_agent.sugar_import_profit.freight import (
        SCHEMA, DEFINITION, load_freight, reference_on, provenance, validate_provenance)
    assert load_freight(tmp_path) == [] and not list(tmp_path.iterdir())
    source = dict(article_url='https://www.hisugar.com/home/articleContent?id=2025010308000000000000',
        image_url='https://www.hisugar.com/image/20250103/table.png', image_sha256='a'*64,
        published_at='2025-01-03T08:00:00+08:00')
    row = dict(date='2025-01-02', country='巴西', freight_usd_per_tonne=54., sources=[source])
    bundle = dict(schema_version=SCHEMA, provider='HiSugar', unit='USD/tonne', definition=DEFINITION, observations=[row])
    def write(value, count=1):
        raw = json.dumps(value, ensure_ascii=False).encode()
        (tmp_path / 'freight.json').write_bytes(raw)
        (tmp_path / 'current.json').write_text(json.dumps(dict(schema_version=SCHEMA, file='freight.json',
            row_count=count, sha256=hashlib.sha256(raw).hexdigest())), encoding='utf-8')
    write(bundle)
    rows = load_freight(tmp_path)
    assert reference_on(rows, '2025-01-02', '巴西') == row
    assert reference_on(rows, '2025-01-03', '巴西') is None
    assert reference_on(rows, '2025-01-02', '泰国') is None
    assert provenance(row, 0.)['kind'] == 'manual'
    assert provenance(row, None) == {'kind':'missing'}
    origin = provenance(row, 54.)
    validate_provenance(origin, 54., date(2025, 1, 2))
    with pytest.raises(ValueError, match='日期'):
        validate_provenance(origin, 54., date(2025, 1, 3))
    (tmp_path / 'freight.json').write_bytes(b'{}')
    with pytest.raises(ValueError, match='校验'):
        load_freight(tmp_path)
    duplicate = deepcopy(bundle)
    duplicate['observations'].append(row)
    write(duplicate, count=2)
    with pytest.raises(ValueError, match='日期'):
        load_freight(tmp_path)
    wrong = dict(bundle, unit='USD/day')
    write(wrong)
    with pytest.raises(ValueError, match='单位'):
        load_freight(tmp_path)


def test_hisugar_ocr_conflicts_and_unrecognized_values_are_not_observations(tmp_path):
    script = Path(__file__).resolve().parents[1] / '04_scripts/import_profit/collect_hisugar_freight.py'
    spec = importlib.util.spec_from_file_location('sugar_public_collect', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    articles, jobs, ocr = [], [], []
    for identity, values in [('1', [('20250102', '54.5'), ('20250103', '55'), ('20250106', '5S')]),
                             ('2', [('20250102', '54.5'), ('20250103', '56'), ('20250107', '53')])]:
        articles.append(dict(id=identity, title='巴西食糖进口成本估算', published_at='2025-01-06 08:00:00',
            article_url='https://www.hisugar.com/home/articleContent?id='+identity, retrieved_at='2025-01-06T10:00:00+08:00',
            images=[dict(url='https://www.hisugar.com/image/table.png', sha256='a'*64)], html_sha256='b'*64))
        jobs.append(dict(id=identity, cells=[{} for _ in values]))
        words = [dict(text=text, x=x, y=index*100+10, height=20) for index, pair in enumerate(values)
                 for x, text in zip((10, 650), pair)]
        ocr.append(dict(id=identity, text='巴西 海运费 美元/吨', words=words))
    (tmp_path / 'capture.json').write_text(json.dumps(dict(articles=articles)), encoding='utf-8')
    (tmp_path / 'ocr-jobs.json').write_text(json.dumps(jobs), encoding='utf-8')
    (tmp_path / 'ocr-results.jsonl').write_text('\n'.join(map(json.dumps, ocr)), encoding='utf-8')
    module.parse_ocr(tmp_path)
    review = json.loads((tmp_path / 'extracted-review.json').read_text(encoding='utf-8'))
    assert [(r['date'], r['freight_usd_per_tonne']) for r in review['observations']] == [('2025-01-02', 54.5)]
    assert len(review['observations'][0]['sources']) == 2
    assert [r['date'] for r in review['conflicts']] == ['2025-01-03']
    assert {r['reason'] for r in review['rejected']} == {'Unrecognized freight number', 'Future source date'}


def test_public_report_discovery_retains_dated_titles_without_sugar_word(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / '04_scripts/import_profit/collect_hisugar_freight.py'
    spec = importlib.util.spec_from_file_location('sugar_public_discovery', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    response = dict(code=0, data=[dict(list=[dict(id='a',title='20230203进口成本估算'),
        dict(id='b',title='20261023食糖进口成本及利润估算'), dict(id='c',title='进口成本研判')])])
    def fetch(url):
        calls.append(url)
        return json.dumps(response, ensure_ascii=False).encode()
    monkeypatch.setattr(module, 'fetch', fetch)
    assert [r['id'] for r in module.discover(tmp_path)] == ['a','b']
    assert len(module.discover(tmp_path)) == 2 and len(calls) == 1
    assert '%E8%BF%9B%E5%8F%A3%E6%88%90%E6%9C%AC' in calls[0]


def test_deployed_capture_switch_is_declared_and_independent_from_saving(monkeypatch):
    import yaml
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / '02_configs/runtime_contracts/spread-production-runtime.json').read_text(encoding='utf-8'))
    compose = yaml.safe_load((root / '09_deploy/spread_runtime/compose.yml').read_text(encoding='utf-8'))
    name = 'SUGAR_IMPORT_ALLOW_CAPTURE'
    assert name in manifest['required_environment']
    assert compose['services']['spread-dashboard']['environment'][name] == '${SUGAR_IMPORT_ALLOW_CAPTURE:-0}'
    assert next(r for r in manifest['environment_bindings'] if r['name'] == name) == dict(
        name=name, kind='deployment', value_type='nonempty', candidate_value='1')
    monkeypatch.setattr(runtime, 'deployed', lambda: True)
    monkeypatch.setenv('SUGAR_IMPORT_ALLOW_SAVE', '1')
    monkeypatch.delenv(name, raising=False)
    assert not runtime.monthly_capture_enabled()
    monkeypatch.setenv(name, '1')
    assert runtime.monthly_capture_enabled()
