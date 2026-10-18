import base64
from copy import deepcopy
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    spreads = load('test_preflight_spreads', '09_deploy/scheduled_server/spreads.py')
    updater = load('test_preflight_updater', '04_scripts/update_price_long_from_akshare.py')
    helpers = load('test_preflight_helpers', '08_tests/test_update_price_long_from_akshare.py')
    symbols = updater.build_active_candidates(helpers.CONFIG, date(2026, 10, 16))['symbol'].tolist()
    prices = helpers._price_rows(updater, [(s, '2026-10-15', 1000 + i) for i, s in enumerate(symbols)])
    baseline = tmp_path / 'baseline'
    path = baseline / '01_data/historical_price_long.xlsx'
    path.parent.mkdir(parents=True)
    prices.to_excel(path, sheet_name='price_long', index=False)
    raw = path.read_bytes()
    request = {'business_date': '2026-10-16', 'job': 'akshare',
               'baseline_files': {'01_data/historical_price_long.xlsx':
                   {'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw)}},
               'public_pointer': {'package_id': 'public-current-' + 'a' * 24}}
    output = tmp_path / 'preflight'
    output.mkdir()
    monkeypatch.setattr(updater.ak, 'futures_zh_daily_sina', lambda *, symbol:
        pd.DataFrame({'date': ['2026-10-15', '2026-10-16'],
                      'close': [1000 + symbols.index(symbol), 1010 + symbols.index(symbol)]}))
    return SimpleNamespace(spreads=spreads, updater=updater, request=request, baseline=baseline,
                           raw=raw, output=output, symbols=symbols, helpers=helpers)


def check(f):
    result = f.spreads.preflight(f.request, f.output, base64.b64encode(f.raw).decode())
    proof = json.loads((f.output / 'preflight.json').read_text(encoding='utf-8'))
    assert set(p.name for p in f.output.iterdir()) == {'source-evidence.xlsx', 'preflight.json'}
    assert (f.baseline / '01_data/historical_price_long.xlsx').read_bytes() == f.raw
    return result, proof


def test_incomplete_source_writes_evidence_without_history_copy(fixture, monkeypatch):
    f = fixture
    original = f.updater.ak.futures_zh_daily_sina
    monkeypatch.setattr(f.updater.ak, 'futures_zh_daily_sina',
        lambda *, symbol: original(symbol=symbol).iloc[:1] if symbol == f.symbols[0] else original(symbol=symbol))
    result, proof = check(f)
    assert result['business_status'] == 'WAITING_FOR_SOURCE'
    assert proof['missing_contracts'] == [f.symbols[0]]
    assert proof['metrics']['to_append_rows'] == 17
    assert not proof['metrics']['full_bundle_written']


def test_source_failure_is_not_no_change(fixture, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError('provider unavailable')
    monkeypatch.setattr(fixture.updater.ak, 'futures_zh_daily_sina', fail)
    result, proof = check(fixture)
    assert result['business_status'] == 'SOURCE_FAILED'
    assert proof['endpoint_failure_contracts'] == 18


def test_no_change_skips_requests_and_writes_no_bundle(fixture, monkeypatch):
    f = fixture
    f.request['business_date'] = '2026-10-15'
    monkeypatch.setattr(f.updater.ak, 'futures_zh_daily_sina', lambda **k: pytest.fail('current contracts queried'))
    result, proof = check(f)
    assert result['business_status'] == 'NO_CHANGE'
    assert proof['new_rows'] == []
    assert proof['metrics']['endpoint_requested_contracts'] == 0


def test_historical_revision_keeps_evidence_and_refuses_candidate(fixture, monkeypatch):
    f = fixture
    monkeypatch.setattr(f.updater.ak, 'futures_zh_daily_sina', lambda **k:
        pd.DataFrame({'date': ['2026-10-15', '2026-10-16'], 'close': [99999, 1001]}))
    with pytest.raises(RuntimeError, match='conflict'):
        check(f)
    assert (f.output / 'source-evidence.xlsx').exists()
    assert not (f.output / 'project').exists()


@pytest.mark.parametrize('tamper', ['baseline', 'date', 'price', 'coverage', 'duplicate'])
def test_offline_replay_rejects_tampered_increment_before_copy(fixture, tamper):
    f = fixture
    _, proof = check(f)
    if tamper == 'baseline': proof['baseline_files'] = {}
    elif tamper == 'date': proof['new_rows'][0]['date'] = '2026-10-17'
    elif tamper == 'price':
        proof['new_rows'][0]['date'] = '2026-10-15'
        proof['new_rows'][0]['price'] = 999999
    elif tamper == 'coverage': proof['new_rows'].pop()
    else: proof['new_rows'].append(proof['new_rows'][0])
    f.request['public_preflight'] = proof
    with pytest.raises(ValueError):
        f.spreads.materialize(f.request, f.baseline, f.output)
    assert not (f.output / 'project').exists()


@pytest.mark.parametrize('incremental',[False,True])
def test_changed_data_materializes_offline_and_keeps_history_and_other_domains(fixture, monkeypatch, incremental):
    from agri_research_agent.pipelines.public_data_delivery import build_production_package, validate_production_package
    from agri_research_agent.pipelines.domestic_spread_integrity import derive_affected_spread_keys
    f = fixture
    helpers = load('test_public_fixture', '08_tests/pipelines/test_public_data_delivery.py')
    public = f.baseline / 'public-market-data'
    helpers._current(public, 'tankan', 'r1', 'immutable source evidence')
    calculator = load('test_preflight_calculator', '04_scripts/calculate_historical_spreads.py')
    config = pd.read_excel(f.helpers.CONFIG, sheet_name='spread_config')
    keys = derive_affected_spread_keys(config, [(s, date(2026, 10, 15)) for s in f.symbols])
    old = calculator.calculate_affected_spreads(config,
        pd.read_excel(f.baseline / '01_data/historical_price_long.xlsx'), keys, 'original')
    artifact = f.baseline / 'historical_spread_database.parquet'
    old.to_parquet(artifact, index=False)
    package = build_production_package(public_current_root=public, packages_root=f.baseline / 'public-package',
        source_max_dates={}, delivery_artifacts={'domestic-spread': artifact}, partitioned_delivery_artifacts=True)
    old = pd.read_parquet(artifact)
    old.to_excel(f.baseline / '01_data/historical_spread_database.xlsx', sheet_name='spread_long', index=False)
    f.request.update(public_manifest=package.manifest, public_package_relative=str(package.directory.relative_to(f.baseline)),
                     public_pointer={'package_id': package.package_id})
    f.request['incremental_public_packages']=incremental
    # Match Docker's mount-parent layout; the package id remains the leaf name.
    monkeypatch.setattr(f.spreads,'PUBLIC_BASELINE',package.directory.parent)
    _, proof = check(f)
    f.request['public_preflight'] = proof
    monkeypatch.setattr(f.updater.ak, 'futures_zh_daily_sina', lambda **k: pytest.fail('offline materializer queried source'))
    result = f.spreads.materialize(f.request, f.baseline, f.output)
    if incremental:
        from agri_research_agent.pipelines.public_data_increment import validate_increment
        from agri_research_agent.pipelines.public_data_delivery import ProductionPackage
        path=f.output/result['package_path']
        assert validate_increment(path,package.directory)['status']=='PASS'
        built=ProductionPackage(result['package_id'],path,json.loads((path/'manifest.json').read_bytes()))
        assert not result['metrics']['full_bundle_written']
        assert not (path/'data/public-market-data').exists()
    else:
        built = validate_production_package(f.output / result['package_path'])
    current = pd.read_parquet(built.directory / 'data' / built.manifest['delivery_artifacts']['domestic-spread']['package_path'])
    retained = current.loc[pd.to_datetime(current['date']).eq('2026-10-15'), old.columns].reset_index(drop=True)
    pd.testing.assert_frame_equal(retained.sort_values(['spread_name','season']).reset_index(drop=True),
        old.sort_values(['spread_name','season']).reset_index(drop=True), check_dtype=False)
    assert result['metrics']['calculated_spread_rows'] > 0
    status=json.loads((f.output/'project/01_data/update_status.json').read_text())
    assert status['target_business_date']=='2026-10-16' and status['target_date_data_completeness']=='COMPLETE'
    assert status['required_contracts']==status['success_contracts']==18
    assert built.manifest['current_identities'] == package.manifest['current_identities']
    assert not (f.output / 'project/01_data/backups').exists()
    assert (f.baseline / '01_data/historical_price_long.xlsx').read_bytes() == f.raw


@pytest.mark.parametrize('outcome', ['WAITING_FOR_SOURCE', 'NO_CHANGE'])
def test_runner_exits_before_full_snapshot_copy(fixture, monkeypatch, outcome):
    f = fixture
    runner = load('test_public_runner', '09_deploy/scheduled_server/run.py')
    public = load('test_public_host', '09_deploy/scheduled_server/public.py')
    fx, host = runner.helpers()
    work = f.output / 'work'; work.mkdir()
    monkeypatch.setattr(runner, 'check_identity', lambda *a: None)
    monkeypatch.setattr(fx, 'volume_usage', lambda *a: {})
    monkeypatch.setattr(fx, 'volume_change', lambda *a: {})
    monkeypatch.setattr(fx, 'write_state', lambda *a: None)
    monkeypatch.setattr(runner, 'module', lambda *a: public)
    monkeypatch.setattr(public, 'snapshot', lambda *a: ({'files': f.request['baseline_files'],
        'contents': {'01_data/historical_price_long.xlsx': base64.b64encode(f.raw).decode()}}, {'public_pointer': f.request['public_pointer']}))
    monkeypatch.setattr(runner, 'disk_budget', lambda *a: {})
    monkeypatch.setattr(runner, 'collect', lambda *a, **k: {'business_status': outcome})
    monkeypatch.setattr(public, 'read_preflight', lambda *a: {})
    monkeypatch.setattr(host, '_make_tree_readonly', lambda *a, **k: None)
    monkeypatch.setattr(public, 'unchanged', lambda *a: {'status': 'NO_CHANGE'})
    monkeypatch.setattr(runner, 'copy_snapshot', lambda *a: pytest.fail('full snapshot copied'))
    result = runner.execute(fx, host, {'job': 'akshare', 'producer': {}}, work, True)
    assert result['status'] == outcome and result['published'] is False
    assert not (work / 'baseline').exists() and not (work / 'collection').exists()


def test_materialization_container_has_no_network():
    runner = load('test_public_command', '09_deploy/scheduled_server/run.py')
    command = runner.collector_command({'job': 'akshare', 'image_id': 'image'}, Path('/work'))
    assert command[command.index('--network') + 1] == 'none'
    check = runner.collector_command({'job': 'akshare', 'image_id': 'image'}, Path('/work'), public_preflight=True)
    assert check[check.index('--network') + 1] == 'bridge'
    assert '--public-preflight' in check


def test_public_worker_entrypoint_accepts_only_preflight_price_input(fixture, monkeypatch):
    import io
    f=fixture
    collector=load('test_preflight_entrypoint','09_deploy/scheduled_server/collect.py')
    request_path=f.baseline/'request.json'
    request_path.write_text(json.dumps(f.request))
    monkeypatch.setattr(collector,'SOURCE',ROOT)
    monkeypatch.setattr(collector,'Path',lambda value:
        f.output if value=='/output' else request_path if value=='/baseline/request.json' else Path(value))
    monkeypatch.setattr(collector.sys,'argv',['collect.py','--public-preflight'])
    monkeypatch.setattr(collector.sys,'stdin',io.StringIO(json.dumps({'price_baseline':base64.b64encode(f.raw).decode()})))
    assert collector.main()==0
    result=json.loads((f.output/'collection-result.json').read_text())
    assert result['business_status']=='CHANGED' and result['status']=='PASS'
    assert not (f.output/'project').exists()


@pytest.mark.parametrize('tamper', ['none', 'pointer', 'manifest', 'file', 'reader'])
def test_unchanged_release_still_requires_cas_hashes_and_actual_reader(tmp_path, monkeypatch, tamper):
    public = load('test_unchanged_public', '09_deploy/scheduled_server/public.py')
    runner = load('test_unchanged_runner', '09_deploy/scheduled_server/run.py')
    host = runner.helpers()[1]
    store = tmp_path/'01_data/public-data-server-store'
    package_id = 'public-current-'+'a'*24
    package = store/'releases'/package_id
    (package/'data').mkdir(parents=True)
    (package/'data/value').write_bytes(b'original')
    manifest = {'files':[{'path':'value','sha256':host.sha256_file(package/'data/value'),'size_bytes':8}]}
    (package/'manifest.json').write_text(json.dumps(manifest))
    pointer = {'package_id':package_id}
    (store/'current.json').write_text(json.dumps(pointer))
    extra = {'public_pointer':pointer,'public_manifest':manifest,'public_manifest_sha256':host.sha256_file(package/'manifest.json')}
    if tamper=='pointer': (store/'current.json').write_text('{}')
    if tamper=='manifest': (package/'manifest.json').write_text('{}')
    if tamper=='file': (package/'data/value').write_bytes(b'changed!')
    transport = SimpleNamespace(_discover_application_runtime=lambda *a: {'container_store':'/store'},
        _run_application_runtime_gate=lambda *a,**k:{'STATUS':'FAIL' if tamper=='reader' else 'PASS'})
    monkeypatch.setattr(public,'transport',lambda:transport)
    if tamper=='none':
        receipt=public.unchanged(host,{'allocation_root':str(tmp_path)},extra,True)
        assert receipt['status']=='NO_CHANGE' and receipt['upload_created'] is False
    else:
        with pytest.raises(ValueError): public.unchanged(host,{'allocation_root':str(tmp_path)},extra,True)
    assert not (store/'incoming').exists()
    assert (package/'data/value').exists()
