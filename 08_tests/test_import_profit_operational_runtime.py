"""Operational CNF persistence and unchanged immutable inputs."""
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from agri_research_agent.import_profit import operational_runtime as op
from agri_research_agent.import_profit.cnf_store import load_cnf_store
from agri_research_agent.import_profit.intraday_store import load_intraday_profit_batch
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode
import agri_research_agent.shared.runtime_context as runtime_context
from test_import_profit_intraday import CONFIG, DAY, key, public_snapshot, seal_fixture_snapshot
from agri_research_agent.market_data.intraday import MarketSession
import import_profit_intraday_page as page
import import_profit_intraday_runtime_page as entry


@pytest.fixture
def operational(tmp_path):
    root = tmp_path / 'runtime'
    cnf, results = root / op.CNF_RELATIVE, root / op.AM_RELATIVE
    cnf.parent.mkdir(parents=True)
    results.mkdir(parents=True)
    (root / '.market-data-runtime.json').write_text(json.dumps(dict(schema_version=1,
        runtime_id='operational-fixture', classification='fixture', module_id='soybean-pm',
        created_at='2026-09-14T00:00:00+00:00')), encoding='utf-8')
    context = RuntimeContext(RuntimeMode.FIXTURE, 'soybean-pm', root)
    return context, cnf, results


@pytest.mark.parametrize("classification", ["fixture", "formal", "candidate-validation"])
def test_real_save_reload_zero_am_and_twelve_months(operational, tmp_path, monkeypatch, classification):
    context, cnf, results = operational
    if classification != 'fixture':
        marker = context.runtime_root / '.market-data-runtime.json'
        raw = json.loads(marker.read_text())
        raw.update(classification=classification, module_id='shared-intraday')
        marker.write_text(json.dumps(raw))
        monkeypatch.setattr(op, 'ROOT', context.runtime_root)
        monkeypatch.setattr(runtime_context, 'verify_execution',
            lambda *a, **k: SimpleNamespace(writable_roots=(cnf.parent, results)))
        context = op.operational_write_context()
    environment = 'TEST_ISOLATED_NON_PRODUCTION' if classification == 'fixture' else 'FORMAL'
    snapshots = tmp_path / 'sealed-snapshots'
    from dataclasses import replace
    from agri_research_agent.import_profit.historical_cnf_adapter import shipment_year_for
    quotes = {}
    for month in range(1, 13):
        snapshot = public_snapshot(MarketSession.AM, key(year=shipment_year_for(DAY,month),month=month))
        for quote in snapshot.quotes:
            quotes[quote.instrument_id] = quote
    snapshot = replace(snapshot, quotes=tuple(quotes.values()))
    seal_fixture_snapshot(snapshots, snapshot)
    if environment == 'FORMAL':
        # Construct a synthetic production-like input, never an actual release.
        from agri_research_agent.market_data.intraday import _manifest, canonical_json
        snapshot = replace(snapshot, environment='FORMAL')
        (snapshots/'releases'/snapshot.release_id/'manifest.json').write_bytes(canonical_json(_manifest(snapshot)))
    before = {p: p.read_bytes() for p in snapshots.rglob('*') if p.is_file()}
    historical = tmp_path / 'history'; historical.mkdir()
    old = historical / 'profit.parquet'
    resolved = SimpleNamespace(manual_cnf_path=historical/'cnf.parquet',
        business_keys_path=historical/'keys.parquet', snapshots_path=historical/'snapshot.parquet',
        results_path=old, manifest={'output_files': {'profit.parquet': {'sha256': 'a'*64}},
                                   'date_range': ['2020-01-01','2026-08-27']})
    monkeypatch.setattr(entry, 'resolve_current_runtime_release', lambda _: resolved)
    seen = {}
    monkeypatch.setattr(entry, 'render_import_profit_intraday_page', lambda paths, **kw: seen.update(paths=paths, **kw))
    entry.render_import_profit_intraday_runtime_page(historical, result_root=historical/'results',
        snapshot_root=snapshots, config_path=Path(__file__).parents[1]/'02_configs/import_profit_soybean.yaml',
        preview_historical_cnf_path=historical/'cache.parquet', intraday_cnf_store_path=cnf,
        operational_result_root=results, allow_cnf_save=True, business_date=DAY,
        environment=environment, write_context=context)
    assert callable(seen['save_cnf_handler'])
    assert not cnf.exists() and not list(results.iterdir())  # Rendering is not a data update.
    values = {(o,m): None for o in CONFIG.origin_codes for m in range(1,13)}
    values['brazil',12] = 0.0
    receipt = seen['save_cnf_handler'](values)
    assert receipt.snapshot_immutability_pass and receipt.am_record_count == 48
    records = load_cnf_store(cnf, allowed_origins=CONFIG.origin_codes).records
    assert next(r for r in records if r.business_key.origin=='brazil' and r.business_key.shipment_month==12).cnf_cents_per_bushel == 0
    am = load_intraday_profit_batch(results, DAY, MarketSession.AM)
    assert page._load_selected_release(seen['paths'], DAY, MarketSession.AM) == am
    assert page._load_selected_release(seen['paths'], date(2026,8,31), MarketSession.AM) is None
    for session, release in [(MarketSession.AM,am),(MarketSession.PM,None)]:
        rows=page._result_profit_rows(release,business_date=DAY,session=session,origin='brazil',config=CONFIG)
        assert len(rows)==12
    assert not resolved.manual_cnf_path.exists()
    assert all(p.read_bytes()==raw for p,raw in before.items())


@pytest.mark.parametrize('fault', ['history','result-history','missing','cnf-readonly','result-readonly','grant'])
def test_half_configured_or_unapproved_write_fails(operational, monkeypatch, fault):
    context, cnf, results = operational
    if fault=='history': cnf=context.runtime_root/'history/releases/sealed/manual_cnf_quotes.parquet'
    if fault=='result-history': results=context.runtime_root/'history/results'
    if fault=='missing': results.rmdir()
    if fault in ('cnf-readonly','result-readonly'):
        denied=cnf.parent if fault=='cnf-readonly' else results
        original=op.os.access
        monkeypatch.setattr(op.os,'access',lambda path, mode: False if path==denied else original(path,mode))
    if fault=='grant': monkeypatch.setattr(op,'assert_runtime_write',lambda *a: (_ for _ in ()).throw(ValueError('grant rejected')))
    with pytest.raises((ValueError,RuntimeError)):
        op.validate_operational_write(context, cnf, results)
    assert not cnf.exists()


@pytest.mark.parametrize('classification', ['formal','candidate-validation'])
def test_fresh_oci_context_uses_service_identity_and_scoped_roots(operational,monkeypatch,classification):
    context,cnf,results=operational
    root=context.runtime_root
    marker=root/'.market-data-runtime.json';raw=json.loads(marker.read_text())
    raw.update(classification=classification,module_id='shared-intraday');marker.write_text(json.dumps(raw))
    monkeypatch.setattr(op,'ROOT',root)
    calls=[]
    def verify(request,**kw):
        calls.append((request,kw))
        return SimpleNamespace(writable_roots=(cnf.parent,results))
    monkeypatch.setattr(runtime_context,'verify_execution',verify)
    for k,v in {'IMPORT_PROFIT_INTRADAY_ALLOW_CNF_SAVE':'1','IMPORT_PROFIT_INTRADAY_CNF_STORE_PATH':str(cnf),'IMPORT_PROFIT_INTRADAY_AM_RESULT_ROOT':str(results)}.items():monkeypatch.setenv(k,v)
    result=op.configured_operational_write()
    assert result.mode is (RuntimeMode.PRODUCTION_WRITE if classification=='formal' else RuntimeMode.CANDIDATE_VALIDATION)
    assert len(calls)>=3 and all(kw['module_id']=='shared-intraday' for _,kw in calls)
    assert calls[0][0].grant_path==Path('/run/market-data-grants/grant.json')
    assert not cnf.exists()
    monkeypatch.setenv('IMPORT_PROFIT_INTRADAY_ALLOW_CNF_SAVE','0')
    assert op.configured_operational_write() is None


def test_save_rechecks_grant_after_page_was_rendered(operational, tmp_path, monkeypatch):
    _, cnf, results = operational
    root = cnf.parents[3]
    marker = root / '.market-data-runtime.json'
    raw = json.loads(marker.read_text(encoding='utf-8'))
    raw.update(classification='formal', module_id='shared-intraday')
    marker.write_text(json.dumps(raw), encoding='utf-8')
    allowed = {'value': True}

    def verify(*_args, **_kwargs):
        if not allowed['value']:
            raise runtime_context.ProductionIdentityError('execution grant is not currently valid')
        return SimpleNamespace(writable_roots=(cnf.parent, results))

    monkeypatch.setattr(runtime_context, 'verify_execution', verify)
    monkeypatch.setattr(op, 'ROOT', root)
    context = op.operational_write_context()
    historical = tmp_path / 'history'
    historical.mkdir()
    resolved = SimpleNamespace(
        manual_cnf_path=historical / 'cnf.parquet',
        business_keys_path=historical / 'keys.parquet',
        snapshots_path=historical / 'snapshots.parquet',
        results_path=historical / 'profit.parquet',
        manifest={'output_files': {'profit.parquet': {'sha256': 'a' * 64}},
                  'date_range': ['2020-01-01', '2026-08-27']},
    )
    monkeypatch.setattr(entry, 'resolve_current_runtime_release', lambda _: resolved)
    seen = {}
    monkeypatch.setattr(entry, 'render_import_profit_intraday_page',
                        lambda paths, **kwargs: seen.update(kwargs))
    entry.render_import_profit_intraday_runtime_page(
        historical, result_root=historical / 'results',
        snapshot_root=tmp_path / 'snapshots',
        config_path=Path(__file__).parents[1] / '02_configs/import_profit_soybean.yaml',
        preview_historical_cnf_path=historical / 'cache.parquet',
        intraday_cnf_store_path=cnf, operational_result_root=results,
        allow_cnf_save=True, business_date=DAY, write_context=context,
    )
    assert callable(seen['save_cnf_handler'])
    allowed['value'] = False  # The page was left open until its grant expired.
    with pytest.raises(runtime_context.RuntimeAuthorizationError, match='not currently valid'):
        seen['save_cnf_handler']({})
    assert not cnf.exists() and not list(results.iterdir())


@pytest.mark.parametrize('enabled',[True,False])
def test_editor_capability_and_zero_payload(operational, enabled):
    _,cnf,_=operational
    script=f'''import streamlit as st
from datetime import date
from pathlib import Path
from agri_research_agent.import_profit.config import load_soybean_config
from import_profit_intraday_page import _render_preview_cnf_editor
config=load_soybean_config({str(Path(__file__).parents[1]/'02_configs/import_profit_soybean.yaml')!r})
_render_preview_cnf_editor(Path({str(cnf)!r}),business_date=date(2026,8,28),
    labels={{o.code:o.label for o in config.origins}},config=config,
    save_cnf_handler=lambda v:st.session_state.update(saved=v),editable={enabled})
'''
    app=AppTest.from_string(script).run()
    assert not app.exception
    button=next(b for b in app.button if b.label=='保存今日 CNF')
    assert button.disabled is not enabled
    editor=app.dataframe[0]
    columns=json.loads(editor.proto.columns)
    assert all(columns[f'{m}月船期'].get('disabled',False) is not enabled for m in range(1,13))


def test_history_merge_uses_complete_keys_and_keeps_zero(operational,tmp_path):
    from agri_research_agent.import_profit.cnf_store import CnfQuoteUpdate,upsert_cnf_quotes
    from dataclasses import replace
    from datetime import datetime,timezone
    _,cnf,_=operational;historical=tmp_path/'history.parquet';at=datetime.now(timezone.utc)
    original=key()
    upsert_cnf_quotes(historical,[CnfQuoteUpdate(original,150,'manual_ui',at,'old')],allowed_origins=CONFIG.origin_codes,expected_store_sha256=None)
    before=historical.read_bytes()
    another=key(year=2027)
    upsert_cnf_quotes(cnf,[CnfQuoteUpdate(original,0,'manual_ui',at,'new'),CnfQuoteUpdate(another,None,'manual_ui',at,'new')],allowed_origins=CONFIG.origin_codes,expected_store_sha256=None)
    rows=page._manual_cnf_records(cnf,historical,CONFIG.origin_codes)
    assert len(rows)==2 and {r.business_key.shipment_year:r.cnf_cents_per_bushel for r in rows}=={2026:0,2027:None}
    assert historical.read_bytes()==before


def test_historical_cache_cannot_fake_operational_save(operational, tmp_path):
    from datetime import datetime, timezone
    from agri_research_agent.import_profit.cnf_store import CnfQuoteUpdate, upsert_cnf_quotes
    from agri_research_agent.pipelines.soybean_intraday import save_manual_cnf_and_materialize_am
    _, cnf, _ = operational
    historical = tmp_path / 'historical-cnf.parquet'
    upsert_cnf_quotes(
        historical, [CnfQuoteUpdate(key(), 175.0, 'manual_ui',
                                    datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc), 'historical')],
        allowed_origins=CONFIG.origin_codes, expected_store_sha256=None,
    )
    historical_bytes = historical.read_bytes()
    script = f'''import streamlit as st
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from agri_research_agent.import_profit.config import load_soybean_config
from import_profit_intraday_page import _render_preview_cnf_editor
config=load_soybean_config({str(Path(__file__).parents[1]/'02_configs/import_profit_soybean.yaml')!r})
_render_preview_cnf_editor(Path({str(cnf)!r}),
    historical_cnf_store_path=Path({str(historical)!r}),
    business_date=date(2026,8,28),labels={{o.code:o.label for o in config.origins}},
    config=config,save_cnf_handler=lambda values: SimpleNamespace(am_materialization_status='INPUT_INCOMPLETE'),
    editable=True)
'''
    app = AppTest.from_string(script).run()
    assert not app.exception
    button = next(item for item in app.button if item.label == '保存今日 CNF')
    assert not button.disabled
    button.click().run()
    assert not app.exception
    assert any('已正式保存' in item.value for item in app.success)
    assert any('待计算' in item.value for item in app.info)

    values = {(origin, month): None for origin in CONFIG.origin_codes for month in range(1, 13)}
    values['brazil', 12] = 0.0
    receipt = save_manual_cnf_and_materialize_am(
        snapshot_root=tmp_path / 'missing-am', result_root=tmp_path / 'results',
        cnf_store_path=cnf, business_date=DAY, values=values, config=CONFIG,
    )
    assert receipt.am_materialization_status == 'INPUT_INCOMPLETE'
    operational = load_cnf_store(cnf, allowed_origins=CONFIG.origin_codes)
    assert len(operational.records) == 1
    assert operational.records[0].cnf_cents_per_bushel == 0.0
    rows = page._manual_cnf_records(cnf, historical, CONFIG.origin_codes)
    current = next(row for row in rows if row.key == operational.records[0].key)
    assert current.cnf_cents_per_bushel == 0.0
    assert historical.read_bytes() == historical_bytes
    reloaded = AppTest.from_string(script).run()
    assert not reloaded.exception
    assert not next(item for item in reloaded.button if item.label == '重试上午盘面榨利').disabled


def test_editor_reports_storage_failure_without_saved_message(operational):
    _, cnf, _ = operational
    script = f'''import streamlit as st
from datetime import date
from pathlib import Path
from agri_research_agent.import_profit.config import load_soybean_config
from import_profit_intraday_page import _render_preview_cnf_editor
config=load_soybean_config({str(Path(__file__).parents[1]/'02_configs/import_profit_soybean.yaml')!r})
_render_preview_cnf_editor(Path({str(cnf)!r}),
    business_date=date(2026,8,28),labels={{o.code:o.label for o in config.origins}},
    config=config,save_cnf_handler=lambda _: (_ for _ in ()).throw(OSError('storage denied')),
    editable=True)
'''
    app = AppTest.from_string(script).run()
    assert not app.exception
    next(item for item in app.button if item.label == '保存今日 CNF').click().run()
    assert not app.exception
    assert any('保存失败' in item.value for item in app.error)
    assert not app.success
    assert not cnf.exists()


def test_enabled_missing_environment_paths_fail_before_render(operational,monkeypatch):
    context,_,_=operational
    monkeypatch.setattr(op,'operational_write_context',lambda:context)
    monkeypatch.setenv('IMPORT_PROFIT_INTRADAY_ALLOW_CNF_SAVE','1')
    monkeypatch.delenv('IMPORT_PROFIT_INTRADAY_CNF_STORE_PATH',raising=False)
    monkeypatch.delenv('IMPORT_PROFIT_INTRADAY_AM_RESULT_ROOT',raising=False)
    with pytest.raises(ValueError,match='operational path'):
        op.configured_operational_write()


def test_production_preflight_checks_write_capability_before_consumers(monkeypatch):
    from test_spread_runtime_contract import load_script
    preflight=load_script('_operational_preflight','04_scripts/runtime/spread_runtime_preflight.py')
    monkeypatch.setattr(preflight,'initialize_preflight_identity',lambda _:object())
    def reject(): raise ValueError('operational store readonly')
    monkeypatch.setattr(op,'configured_operational_write',reject)
    with pytest.raises(ValueError,match='operational store readonly'):
        preflight.readonly_preflight(SimpleNamespace())


def test_formal_source_contract_has_only_scoped_business_rw():
    from test_spread_runtime_contract import contract
    m=contract()
    access={r['role']:r['access'] for r in m['runtime_roots']}
    assert all(access[k]=='ro' for k in ('history','cnf','results','snapshots','data','weather'))
    assert access['manual-cnf']==access['am-results']=='rw'
    assert next(b for b in m['environment_bindings'] if b['name']=='IMPORT_PROFIT_INTRADAY_ALLOW_CNF_SAVE')['value']=='1'
    assert 'IMPORT_PROFIT_INTRADAY_WRITE_MODE' in m['forbidden_environment']
    assert 'IMPORT_PROFIT_INTRADAY_EXPECTED_RUNTIME_ID' in m['forbidden_environment']
